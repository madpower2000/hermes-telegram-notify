"""Compact, bounded, secret-conscious Telegram message formatting."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping

from .logging_utils import redact

_SECRET_ARG_RE = re.compile(r"(?i)(--?(?:token|password|passwd|secret|api[-_]?key|authorization)|(?:token|password|secret|api[-_]?key)\s*[=:])(?:\s+|\s*=\s*)[^\s]+")


def truncate(value: Any, limit: int = 700) -> str:
    text = "" if value is None else str(value)
    text = " ".join(text.replace("\x00", "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def safe_command(value: Any, limit: int = 700) -> str:
    if isinstance(value, Mapping):
        for key in ("command", "cmd", "action", "tool", "name"):
            if value.get(key):
                value = value[key]
                break
        else:
            value = "operation"
    text = _SECRET_ARG_RE.sub(r"\1 [REDACTED]", truncate(value, limit))
    return truncate(text, limit)


def project_name(cwd: Any = None, session_id: Any = None) -> str:
    if not cwd and session_id:
        cwd = _stored_session_cwd(session_id)
    if not cwd:
        # Gateway services commonly run from the user's home directory rather
        # than the project that owns the conversation. A named Hermes profile
        # is the fallback when neither the event nor the session record has a
        # working directory (for example, ``~/.hermes/profiles/zorro`` ->
        # ``zorro``). An explicit cwd always wins.
        try:
            from hermes_constants import get_hermes_home

            home = get_hermes_home().expanduser().resolve()
            if home.parent.name == "profiles" and home.name:
                return truncate(home.name, 80)
        except (ImportError, OSError, RuntimeError, ValueError):
            pass
        cwd = os.getcwd()
    try:
        path = Path(str(cwd)).expanduser()
        name = path.name or path.parent.name
    except (OSError, ValueError):
        name = "unknown"
    return truncate(name or "unknown", 80)


def _stored_session_title(session_id: Any) -> str:
    """Read Hermes' user/auto-generated session title when available.

    Hook payloads intentionally carry stable IDs rather than a title. The
    title lives in Hermes' profile-scoped ``state.db``; use the public
    ``SessionDB.get_session_title`` API as a best-effort enrichment and never
    make Telegram delivery depend on the database being readable.
    """
    value = str(session_id or "").strip()
    if not value:
        return ""
    title = ""
    try:
        from hermes_state import SessionDB

        with SessionDB(read_only=True) as session_db:
            title = session_db.get_session_title(value)
        return redact(title or "").strip()
    except Exception:
        return ""


def _stored_session_cwd(session_id: Any) -> str:
    """Resolve a session's recorded workspace when a hook omits its cwd."""
    value = str(session_id or "").strip()
    if not value:
        return ""
    session: Any = None
    try:
        from hermes_state import SessionDB

        with SessionDB(read_only=True) as session_db:
            session = session_db.get_session(value)
        cwd = session.get("cwd") if isinstance(session, Mapping) else None
        return cwd.strip() if isinstance(cwd, str) else ""
    except Exception:
        return ""


def session_name(*, session_id: Any = None, explicit: Any = None) -> str:
    """Return a safe human-readable session name without exposing raw IDs."""
    for candidate in (explicit, _stored_session_title(session_id)):
        value = redact(candidate or "").strip()
        if value:
            return truncate(value, 120)
    return "New session"


def _session_field(session_id: Any, explicit: Any = None) -> tuple[str, str]:
    return ("Session", session_name(session_id=session_id, explicit=explicit))


def _lines(title: str, fields: list[tuple[str, Any]], max_chars: int, body: str = "") -> str:
    output = [title]
    for label, value in fields:
        if value is None or value == "":
            continue
        output.append(f"{label}: {truncate(value, 700)}")
    if body:
        output.extend(("", body))
    message = "\n".join(output)
    if len(message) <= max_chars:
        return message
    return message[: max(0, max_chars - 1)].rstrip() + "…"


def started(*, session_id: Any = None, session_name_value: Any = None, task_id: Any = None, turn_id: Any = None, model: Any = None, cwd: Any = None, max_chars: int = 3900) -> str:
    del task_id, turn_id, model
    return _lines(
        "🚀 Hermes · Started",
        [("Project", project_name(cwd, session_id)), _session_field(session_id, session_name_value)],
        max_chars,
    )


def safe_response(value: Any, limit: int = 3200) -> str:
    """Bound and redact final assistant text before sending it to Telegram."""
    text = "" if value is None else str(value)
    text = text.replace("\x00", "").strip()
    text = redact(text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def completion(*, status: str, session_id: Any = None, session_name_value: Any = None, task_id: Any = None, turn_id: Any = None, model: Any = None, cwd: Any = None, reason: Any = None, elapsed_seconds: Any = None, response: Any = None, response_max_chars: int = 3200, max_chars: int = 3900) -> str:
    del task_id, turn_id, model, elapsed_seconds
    status = str(status or "failed").lower()
    title = {
        "completed": "✅ Hermes · Completed",
        "interrupted": "⏸️ Hermes · Interrupted",
        "failed": "❌ Hermes · Failed",
    }.get(status, "ℹ️ Hermes · Finished")
    body = safe_response(response, response_max_chars) if status == "completed" else ""
    fields: list[tuple[str, Any]] = [
        ("Project", project_name(cwd, session_id)),
        _session_field(session_id, session_name_value),
    ]
    if reason and not body:
        fields.append(("Reason", reason))
    return _lines(title, fields, max_chars, body)


def approval(*, command: Any = None, description: Any = None, session_id: Any = None, session_name_value: Any = None, session_key: Any = None, turn_id: Any = None, cwd: Any = None, surface: Any = None, max_chars: int = 3900) -> str:
    del session_key, turn_id, surface
    fields = [
        ("Project", project_name(cwd, session_id)),
        _session_field(session_id, session_name_value),
        ("Command", safe_command(command)),
    ]
    if description:
        fields.append(("Reason", truncate(description, 500)))
    return _lines("⚠️ Hermes · Approval required", fields, max_chars)


def approval_response(*, choice: Any = None, command: Any = None, session_id: Any = None, session_name_value: Any = None, session_key: Any = None, turn_id: Any = None, decided_by: Any = None, max_chars: int = 3900) -> str:
    del session_key, turn_id
    value = str(choice or "unknown").replace("_", "-")
    emoji = {
        "once": "✅",
        "session": "✅",
        "always": "✅",
        "deny": "❌",
        "timeout": "⏱️",
        "notify-failed": "⚠️",
        "smart-approve": "🤖✅",
        "smart-deny": "🤖❌",
    }.get(value, "ℹ️")
    fields = [_session_field(session_id, session_name_value), ("Command", safe_command(command))]
    if decided_by:
        fields.append(("Decided by", decided_by))
    return _lines(f"{emoji} Hermes · Approval {value}", fields, max_chars)
