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
            backup_bytes=int(cfg.values.get("log_backup_bytes", 262_144)),
            **fields,
        )
    except Exception:
        pass


def _notify(cfg: config_mod.ResolvedConfig, text: str, event: str) -> bool:
    if not cfg.configured:
        _record(cfg, "notification_skipped", notification=event, reason="missing_configuration")
        return False
    try:
        TelegramClient(cfg.token, int(cfg.values.get("telegram_timeout_seconds", 4))).send_message(cfg.chat_id, text)
    except (TelegramError, OSError, Exception) as exc:  # noqa: E722 - fail-open boundary
        _record(cfg, "notification_failed", notification=event, error=type(exc).__name__)
        return False
    _record(cfg, "notification_sent", notification=event)
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
        task_id=kwargs.get("task_id"),
        turn_id=kwargs.get("turn_id"),
        model=kwargs.get("model") if cfg.values.get("include_model", True) else None,
        cwd=kwargs.get("cwd") or kwargs.get("working_directory") if cfg.values.get("include_cwd", True) else None,
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
        text = formatting.started(
            session_id=kwargs.get("session_id"),
            task_id=kwargs.get("task_id"),
            turn_id=kwargs.get("turn_id"),
            model=kwargs.get("model") if cfg.values.get("include_model", True) else None,
            cwd=metadata["cwd"] if cfg.values.get("include_cwd", True) else None,
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
        if not cfg.event_enabled("approval") or not cfg.configured:
            if cfg.event_enabled("approval"):
                _record(cfg, "approval_skipped", reason="missing_configuration")
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
            _record(cfg, "approval_suppressed", reason="debounced")
            return None
        text = formatting.approval(
            command=kwargs.get("command"),
            description=kwargs.get("description"),
            session_key=kwargs.get("session_key"),
            turn_id=kwargs.get("turn_id"),
            cwd=kwargs.get("cwd") or kwargs.get("working_directory"),
            surface=kwargs.get("surface"),
            max_chars=int(cfg.values.get("max_message_chars", 3900)),
        )
        _notify(cfg, text, "approval")
    except Exception as exc:
        try:
            _record(config_mod.load_config(), "hook_failed", hook="pre_approval_request", error=type(exc).__name__)
        except Exception:
            pass
    return None


def on_post_approval_response(**kwargs: Any) -> None:
    try:
        cfg = config_mod.load_config()
        if not cfg.event_enabled("approval_response") or not cfg.configured:
            return None
        choice = str(kwargs.get("choice") or "unknown")
        key = f"post:{fingerprint({'session': kwargs.get('session_key'), 'turn': kwargs.get('turn_id'), 'tool_call': kwargs.get('tool_call_id'), 'choice': choice})}"
        if not _state(cfg).claim_approval(key, {"event": "post_approval", "choice": choice}, 86400):
            return None
        text = formatting.approval_response(
            choice=choice,
            command=kwargs.get("command"),
            session_key=kwargs.get("session_key"),
            turn_id=kwargs.get("turn_id"),
            decided_by=kwargs.get("decided_by"),
            max_chars=int(cfg.values.get("max_message_chars", 3900)),
        )
        _notify(cfg, text, "approval_response")
    except Exception as exc:
        try:
            _record(config_mod.load_config(), "hook_failed", hook="post_approval_response", error=type(exc).__name__)
        except Exception:
            pass
    return None
