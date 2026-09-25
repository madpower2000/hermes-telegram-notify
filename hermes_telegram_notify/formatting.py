"""Compact, bounded, secret-conscious Telegram message formatting."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping

from .logging_utils import redact

_SECRET_ARG_RE = re.compile(
    r'''(?i)(--?(?:token|password|passwd|secret|api[-_]?key|authorization)\b|'''
    r'''(?:token|password|passwd|secret|api[-_]?key|authorization)\b)'''
    r'''(?:\s*[:=]\s*|\s+)'''
    r'''(?:"(?:\\.|[^"])*"|'(?:\\.|[^'])*'|[^\s]+)'''
)
_UNSET = object()


def safe_text(value: Any, limit: int = 700, *, compact: bool = True) -> str:
    """Normalize, redact known credentials, then bound arbitrary outbound text."""
    text = "" if value is None else str(value)
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = redact(text)
    text = _SECRET_ARG_RE.sub(r"\1 [REDACTED]", text)
    if compact:
        text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def truncate(value: Any, limit: int = 700) -> str:
    return safe_text(value, limit)


def safe_command(value: Any, limit: int = 700) -> str:
    if isinstance(value, Mapping):
        for key in ("command", "cmd", "action", "tool", "name"):
            if value.get(key):
                value = value[key]
                break
        else:
            value = "operation"
    return safe_text(value, limit)


def project_name(cwd: Any = None, session_id: Any = None) -> str:
    if not cwd and session_id:
        cwd = _stored_session_metadata(session_id).get("cwd")
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
    return safe_text(name or "unknown", 80)


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
        return safe_text(title or "", 120)
    except Exception:
        return ""


def _stored_session_metadata(session_id: Any) -> dict[str, str]:
    """Read only display metadata from the owning profile's session row."""
    value = str(session_id or "").strip()
    if not value:
        return {}
    session: Any = None
    try:
        from hermes_state import SessionDB

        with SessionDB(read_only=True) as session_db:
            session = session_db.get_session(value)
        if not isinstance(session, Mapping):
            return {}

        def _field(key: str) -> str:
            field_value = session.get(key)
            if not isinstance(field_value, str):
                return ""
            return field_value.strip() if key == "cwd" else field_value

        return {key: _field(key) for key in ("title", "cwd", "profile_name")}
    except Exception:
        return {}


def _profile_label(explicit: Any, stored: Any) -> str:
    for candidate in (explicit, stored):
        value = safe_text(candidate or "", 80)
        if value:
            return value
    try:
        from hermes_constants import get_hermes_home, profile_name_for_home

        return truncate(profile_name_for_home(get_hermes_home()) or "default", 80)
    except Exception:
        return "default"


def session_name(
    *, session_id: Any = None, explicit: Any = None, stored_title: Any = _UNSET,
) -> str:
    """Return a safe human-readable session name without exposing raw IDs."""
    if stored_title is _UNSET:
        stored_title = _stored_session_title(session_id)
    for candidate in (explicit, stored_title):
        value = safe_text(candidate or "", 120)
        if value and value.casefold() != "new session":
            return value
    return "New session"


def _session_field(session_id: Any, explicit: Any = None) -> tuple[str, str]:
    return ("Session", session_name(session_id=session_id, explicit=explicit))


def _lines(title: str, fields: list[tuple[str, Any]], max_chars: int, body: str = "") -> str:
    output = [title]
    for label, value in fields:
        if value is None or value == "":
            continue
        output.append(f"{safe_text(label, 80)}: {safe_text(value, 700)}")
    if body:
        output.extend(("", body))
    message = "\n".join(output)
    if len(message) <= max_chars:
        return message
    return message[: max(0, max_chars - 1)].rstrip() + "…"


def started(
    *, session_id: Any = None, session_name_value: Any = None, profile_name_value: Any = None,
    task_id: Any = None, turn_id: Any = None, model: Any = None, cwd: Any = None,
    include_project: bool = True, include_session: bool = True,
    max_chars: int = 3900,
) -> str:
    del task_id, turn_id
    metadata = _stored_session_metadata(session_id)
    fields: list[tuple[str, Any]] = []
    if include_project:
        fields.append(("📁 Project", project_name(cwd or metadata.get("cwd"))))
    fields.append(("👤 Profile", _profile_label(profile_name_value, metadata.get("profile_name"))))
    if include_session:
        fields.append((
            "📝 Session",
            session_name(session_id=session_id, explicit=session_name_value, stored_title=metadata.get("title", "")),
        ))
    if model:
        fields.append(("Model", model))
    return _lines(
        "🚀 Hermes · Started",
        fields,
        max_chars,
    )


def safe_response(value: Any, limit: int = 3200) -> str:
    """Bound and redact final assistant text before sending it to Telegram."""
    return safe_text(value, limit, compact=False)


def completion(*, status: str, session_id: Any = None, session_name_value: Any = None, task_id: Any = None, turn_id: Any = None, model: Any = None, cwd: Any = None, include_project: bool = True, include_session: bool = True, reason: Any = None, elapsed_seconds: Any = None, response: Any = None, response_max_chars: int = 3200, max_chars: int = 3900) -> str:
    del task_id, turn_id, elapsed_seconds
    status = str(status or "failed").lower()
    title = {
        "completed": "✅ Hermes · Completed",
        "interrupted": "⏸️ Hermes · Interrupted",
        "failed": "❌ Hermes · Failed",
    }.get(status, "ℹ️ Hermes · Finished")
    body = safe_response(response, response_max_chars) if status == "completed" else ""
    fields: list[tuple[str, Any]] = []
    if include_project:
        fields.append(("Project", project_name(cwd, session_id)))
    if include_session:
        fields.append(_session_field(session_id, session_name_value))
    if model:
        fields.append(("Model", model))
    if reason and not body:
        fields.append(("Reason", reason))
    return _lines(title, fields, max_chars, body)


def approval(*, command: Any = None, description: Any = None, session_id: Any = None, session_name_value: Any = None, session_key: Any = None, turn_id: Any = None, cwd: Any = None, surface: Any = None, include_project: bool = True, include_session: bool = True, max_chars: int = 3900) -> str:
    del session_key, turn_id, surface
    fields = []
    if include_project:
        fields.append(("Project", project_name(cwd, session_id)))
    if include_session:
        fields.append(_session_field(session_id, session_name_value))
    fields.append(("Command", safe_command(command)))
    if description:
        fields.append(("Reason", safe_text(description, 500)))
    return _lines("⚠️ Hermes · Approval required", fields, max_chars)


def approval_response(*, choice: Any = None, command: Any = None, session_id: Any = None, session_name_value: Any = None, session_key: Any = None, turn_id: Any = None, decided_by: Any = None, include_session: bool = True, max_chars: int = 3900) -> str:
    del session_key, turn_id
    value = str(choice or "unknown").replace("_", "-")
    allowed_choices = {
        "once", "session", "always", "deny", "timeout", "cancelled", "notify-failed",
        "smart-approve", "smart-deny", "transport-failure", "unknown", "other",
    }
    if value not in allowed_choices:
        value = "unknown"
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
    fields = []
    if include_session:
        fields.append(_session_field(session_id, session_name_value))
    fields.append(("Command", safe_command(command)))
    if decided_by:
        fields.append(("Decided by", safe_text(decided_by, 80)))
    return _lines(f"{emoji} Hermes · Approval {value}", fields, max_chars)
