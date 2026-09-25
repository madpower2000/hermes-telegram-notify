"""Hermes lifecycle hook adapter.

Every callback is intentionally fail-open. The host already isolates plugin
callbacks, but this module also catches its own errors so direct integration
and management tooling cannot turn Telegram outages into agent failures.
"""

from __future__ import annotations

import time
from typing import Any

from . import config as config_mod
from . import formatting
from . import logging_utils
from .state import StateStore, fingerprint
from .telegram import TelegramClient, TelegramError


def _state(cfg: config_mod.ResolvedConfig) -> StateStore:
    return StateStore(
        cfg.state_path,
        cfg.lock_path,
        retention_days=int(cfg.values.get("state_retention_days", 7)),
        log_path=cfg.log_path,
    )


def _record(cfg: config_mod.ResolvedConfig, event: str, **fields: Any) -> None:
    try:
        logging_utils.record(
            cfg.log_path,
            event,
            max_bytes=int(cfg.values.get("log_max_bytes", 524_288)),
            **fields,
        )
    except Exception:
        pass


def _notify(
    cfg: config_mod.ResolvedConfig,
    text: str,
    event: str,
    *,
    diagnostics: dict[str, Any] | None = None,
) -> bool:
    diagnostic_fields = diagnostics or {}
    if not cfg.configured:
        _record(
            cfg, "notification_skipped", notification=event,
            reason="missing_configuration", **diagnostic_fields,
        )
        return False
    try:
        TelegramClient(cfg.token, int(cfg.values.get("telegram_timeout_seconds", 4))).send_message(cfg.chat_id, text)
    except (TelegramError, OSError, Exception) as exc:  # noqa: E722 - fail-open boundary
        _record(
            cfg, "notification_failed", notification=event,
            error=type(exc).__name__, **diagnostic_fields,
        )
        return False
    _record(cfg, "notification_sent", notification=event, **diagnostic_fields)
    return True


def _run_key(kwargs: dict[str, Any]) -> str:
    session_id = str(kwargs.get("session_id") or "session")
    turn_id = str(kwargs.get("turn_id") or "")
    if turn_id:
        return f"turn:{session_id}:{turn_id}"
    # Older/exit-only payloads may omit turn_id. The task/message fingerprint
    # is stable across repeated pre_llm callbacks, while allowing distinct
    # user messages in one persistent session to produce separate starts.
    return f"fallback:{session_id}:{fingerprint({'task': kwargs.get('task_id'), 'message': kwargs.get('user_message')})}"


def _completion_key(kwargs: dict[str, Any]) -> str:
    session_id = str(kwargs.get("session_id") or "session")
    turn_id = str(kwargs.get("turn_id") or kwargs.get("task_id") or "")
    if turn_id:
        return f"completion:{session_id}:{turn_id}"
    return f"completion:{session_id}:{fingerprint({'reason': kwargs.get('turn_exit_reason') or kwargs.get('reason'), 'status': (kwargs.get('completed'), kwargs.get('failed'), kwargs.get('interrupted') )})}"


def _status(kwargs: dict[str, Any]) -> str:
    if bool(kwargs.get("interrupted")):
        return "interrupted"
    if bool(kwargs.get("failed")) or not bool(kwargs.get("completed")):
        return "failed"
    return "completed"


def _approval_surface(value: Any) -> str:
    """Return a bounded surface category, never an arbitrary payload value."""
    if not isinstance(value, str):
        return "unknown"
    if value in {"smart", "cli", "gateway", "mcp-elicitation"}:
        return value
    if value.startswith("transport:"):
        return "transport"
    return "other"


def _approval_diagnostic_id(kwargs: dict[str, Any]) -> str:
    """Hash only bounded approval identifiers; never persist commands or descriptions."""
    identity: dict[str, Any] = {}
    for key in ("session_key", "turn_id", "tool_call_id", "pattern_key"):
        value = kwargs.get(key)
        if isinstance(value, str) and value:
            identity[key] = value[:256]
    pattern_keys = kwargs.get("pattern_keys")
    if isinstance(pattern_keys, (list, tuple)):
        safe_pattern_keys = [item[:128] for item in pattern_keys[:16] if isinstance(item, str)]
        if safe_pattern_keys:
            identity["pattern_keys"] = safe_pattern_keys
    return fingerprint(identity) if identity else "unavailable"


def _approval_choice(value: Any) -> str:
    """Normalize known approval outcomes and collapse unexpected values."""
    if not isinstance(value, str):
        return "unknown"
    choice = value.strip().lower()
    if choice in {
        "once", "session", "always", "deny", "timeout", "cancelled", "notify_failed",
        "smart_approve", "smart_deny",
    }:
        return choice
    if choice.startswith("transport_"):
        return "transport_failure"
    return "other"


def _approval_decided_by(value: Any) -> str:
    if value == "aux_llm":
        return "aux_llm"
    return "unknown" if value in (None, "") else "other"


def _send_completion(
    cfg: config_mod.ResolvedConfig,
    kwargs: dict[str, Any],
    *,
    status: str,
    response: Any = None,
) -> None:
    """Claim and send one completion/result notification.

    Successful turns reach this helper from ``post_llm_call`` with the final
    assistant response. Interrupted/failed turns reach it from
    ``on_session_end``. The shared claim prevents the latter from sending a
    second success message after ``post_llm_call`` has already sent the result.
    """
    if kwargs.get("platform") == "subagent":
        return None
    if not cfg.event_enabled("completion"):
        return None
    if not cfg.configured:
        _record(cfg, "completion_skipped", reason="missing_configuration")
        return None
    key = _completion_key(kwargs)
    claimed, previous = _state(cfg).claim_completion(
        key,
        {
            "session_id": str(kwargs.get("session_id") or ""),
            "task_id": str(kwargs.get("task_id") or ""),
            "turn_id": str(kwargs.get("turn_id") or ""),
            "status": status,
        },
    )
    if not claimed:
        _record(cfg, "completion_suppressed", reason="duplicate_turn")
        return None
    try:
        started_at = float((previous or {}).get("started_at") or 0)
    except (TypeError, ValueError):
        started_at = 0.0
    elapsed = time.time() - started_at if started_at else None
    # The successful path has the final response; failure/interruption paths
    # deliberately expose only a short reason and no internal transcript.
    reason = None if status == "completed" else kwargs.get("turn_exit_reason") or kwargs.get("reason")
    text = formatting.completion(
        status=status,
        session_id=kwargs.get("session_id"),
        session_name_value=kwargs.get("session_name") or kwargs.get("session_title") or kwargs.get("title"),
        task_id=kwargs.get("task_id"),
        turn_id=kwargs.get("turn_id"),
        model=kwargs.get("model") if cfg.values.get("include_model", True) else None,
        cwd=(kwargs.get("cwd") or kwargs.get("working_directory"))
        if cfg.values.get("include_cwd", True) else None,
        include_project=bool(cfg.values.get("include_cwd", True)),
        include_session=bool(cfg.values.get("include_session", True)),
        reason=reason,
        elapsed_seconds=elapsed,
        response=response,
        response_max_chars=int(cfg.values.get("final_response_max_chars", 3200)),
        max_chars=int(cfg.values.get("max_message_chars", 3900)),
    )
    _notify(cfg, text, "completion")


def on_post_llm_call(**kwargs: Any) -> None:
    """Send the final assistant response after a successful Hermes turn."""
    try:
        cfg = config_mod.load_config()
        _send_completion(
            cfg,
            kwargs,
            status="completed",
            response=kwargs.get("assistant_response"),
        )
    except Exception as exc:
        try:
            _record(config_mod.load_config(), "hook_failed", hook="post_llm_call", error=type(exc).__name__)
        except Exception:
            pass
    return None


def on_pre_llm_call(**kwargs: Any) -> None:
    try:
        if kwargs.get("platform") == "subagent":
            return None
        cfg = config_mod.load_config()
        if not cfg.event_enabled("start"):
            return None
        if not cfg.configured:
            _record(cfg, "start_skipped", reason="missing_configuration")
            return None
        key = _run_key(kwargs)
        store = _state(cfg)
        metadata = {
            "session_id": str(kwargs.get("session_id") or ""),
            "task_id": str(kwargs.get("task_id") or ""),
            "turn_id": str(kwargs.get("turn_id") or ""),
            "cwd": str(kwargs.get("cwd") or kwargs.get("working_directory") or ""),
            "model": str(kwargs.get("model") or ""),
        }
        if not store.claim_start(key, metadata):
            _record(cfg, "start_suppressed", reason="duplicate_turn")
            return None
        profile_name_value = kwargs.get("profile_name")
        if not isinstance(profile_name_value, str) or not profile_name_value.strip():
            profile_candidate = kwargs.get("profile")
            profile_name_value = profile_candidate if isinstance(profile_candidate, str) else None
        text = formatting.started(
            session_id=kwargs.get("session_id"),
            session_name_value=kwargs.get("session_name") or kwargs.get("session_title") or kwargs.get("title"),
            profile_name_value=profile_name_value,
            task_id=kwargs.get("task_id"),
            turn_id=kwargs.get("turn_id"),
            model=kwargs.get("model") if cfg.values.get("include_model", True) else None,
            cwd=metadata["cwd"] if cfg.values.get("include_cwd", True) else None,
            include_project=bool(cfg.values.get("include_cwd", True)),
            include_session=bool(cfg.values.get("include_session", True)),
            max_chars=int(cfg.values.get("max_message_chars", 3900)),
        )
        _notify(cfg, text, "start")
    except Exception as exc:
        try:
            cfg = config_mod.load_config()
            _record(cfg, "hook_failed", hook="pre_llm_call", error=type(exc).__name__)
        except Exception:
            pass
    return None


def on_session_end(**kwargs: Any) -> None:
    try:
        cfg = config_mod.load_config()
        status = _status(kwargs)
        _send_completion(cfg, kwargs, status=status)
    except Exception as exc:
        try:
            _record(config_mod.load_config(), "hook_failed", hook="on_session_end", error=type(exc).__name__)
        except Exception:
            pass
    return None


def on_pre_approval_request(**kwargs: Any) -> None:
    try:
        cfg = config_mod.load_config()
        diagnostics = {
            "approval_id": _approval_diagnostic_id(kwargs),
            "surface": _approval_surface(kwargs.get("surface")),
            "coalesced": kwargs.get("coalesced") is True,
            "observed_at": time.time(),
        }
        _record(cfg, "approval_request_observed", **diagnostics)
        if not cfg.event_enabled("approval") or not cfg.configured:
            if cfg.event_enabled("approval"):
                _record(cfg, "approval_skipped", reason="missing_configuration", **diagnostics)
            return None
        if diagnostics["surface"] == "smart":
            # Smart approval is still deciding whether to auto-approve or
            # escalate; a user-facing prompt emits its own hook if needed.
            _record(cfg, "approval_skipped", reason="smart_assessment", **diagnostics)
            return None
        request_key = fingerprint({
            "session": kwargs.get("session_key"),
            "turn": kwargs.get("turn_id"),
            "tool_call": kwargs.get("tool_call_id"),
            "pattern": kwargs.get("pattern_key") or kwargs.get("pattern_keys"),
            "command": formatting.safe_command(kwargs.get("command")),
        })
        if not _state(cfg).claim_approval(
            f"pre:{request_key}",
            {"event": "pre_approval", "tool_call_id": str(kwargs.get("tool_call_id") or "")},
            int(cfg.values.get("approval_debounce_seconds", 60)),
        ):
            _record(cfg, "approval_suppressed", reason="debounced", **diagnostics)
            return None
        text = formatting.approval(
            command=kwargs.get("command"),
            description=kwargs.get("description"),
            session_id=kwargs.get("session_id"),
            session_name_value=kwargs.get("session_name") or kwargs.get("session_title") or kwargs.get("title"),
            session_key=kwargs.get("session_key"),
            turn_id=kwargs.get("turn_id"),
            cwd=kwargs.get("cwd") or kwargs.get("working_directory"),
            surface=kwargs.get("surface"),
            include_project=bool(cfg.values.get("include_cwd", True)),
            include_session=bool(cfg.values.get("include_session", True)),
            max_chars=int(cfg.values.get("max_message_chars", 3900)),
        )
        _notify(cfg, text, "approval", diagnostics=diagnostics)
    except Exception as exc:
        try:
            _record(config_mod.load_config(), "hook_failed", hook="pre_approval_request", error=type(exc).__name__)
        except Exception:
            pass
    return None


def on_post_approval_response(**kwargs: Any) -> None:
    try:
        cfg = config_mod.load_config()
        choice = str(kwargs.get("choice") or "unknown")
        diagnostics = {
            "approval_id": _approval_diagnostic_id(kwargs),
            "surface": _approval_surface(kwargs.get("surface")),
            "choice": _approval_choice(choice),
            "decided_by": _approval_decided_by(kwargs.get("decided_by")),
            "coalesced": kwargs.get("coalesced") is True,
            "observed_at": time.time(),
        }
        _record(cfg, "approval_response_observed", **diagnostics)
        if not cfg.event_enabled("approval_response") or not cfg.configured:
            return None
        key = f"post:{fingerprint({'session': kwargs.get('session_key'), 'turn': kwargs.get('turn_id'), 'tool_call': kwargs.get('tool_call_id'), 'choice': choice})}"
        if not _state(cfg).claim_approval(key, {"event": "post_approval", "choice": choice}, 86400):
            return None
        text = formatting.approval_response(
            choice=choice,
            command=kwargs.get("command"),
            session_id=kwargs.get("session_id"),
            session_name_value=kwargs.get("session_name") or kwargs.get("session_title") or kwargs.get("title"),
            session_key=kwargs.get("session_key"),
            turn_id=kwargs.get("turn_id"),
            decided_by=kwargs.get("decided_by"),
            include_session=bool(cfg.values.get("include_session", True)),
            max_chars=int(cfg.values.get("max_message_chars", 3900)),
        )
        _notify(cfg, text, "approval_response", diagnostics=diagnostics)
    except Exception as exc:
        try:
            _record(config_mod.load_config(), "hook_failed", hook="post_approval_response", error=type(exc).__name__)
        except Exception:
            pass
    return None
