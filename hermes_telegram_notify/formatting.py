"""Compact, bounded, secret-conscious Telegram message formatting."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Mapping

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


def project_name(cwd: Any = None) -> str:
    if not cwd:
        cwd = os.getcwd()
    try:
        path = Path(str(cwd)).expanduser()
        name = path.name or path.parent.name
    except (OSError, ValueError):
        name = "unknown"
    return truncate(name or "unknown", 80)


def _lines(title: str, fields: list[tuple[str, Any]], max_chars: int) -> str:
    output = [f"Hermes · {title}"]
    for label, value in fields:
        if value is None or value == "":
            continue
        output.append(f"{label}: {truncate(value, 700)}")
    return truncate("\n".join(output), max_chars)


def started(*, session_id: Any = None, task_id: Any = None, turn_id: Any = None, model: Any = None, cwd: Any = None, max_chars: int = 3900) -> str:
    fields = [("Project", project_name(cwd)), ("Status", "started")]
    if model:
        fields.append(("Model", model))
    if session_id:
        fields.append(("Session", session_id))
    if turn_id:
        fields.append(("Turn", turn_id))
    elif task_id:
        fields.append(("Task", task_id))
    return _lines("Started", fields, max_chars)


def completion(*, status: str, session_id: Any = None, task_id: Any = None, turn_id: Any = None, model: Any = None, cwd: Any = None, reason: Any = None, elapsed_seconds: Any = None, max_chars: int = 3900) -> str:
    fields: list[tuple[str, Any]] = [("Project", project_name(cwd)), ("Status", status)]
    if reason:
        fields.append(("Reason", reason))
    if elapsed_seconds is not None:
        try:
            fields.append(("Elapsed", f"{float(elapsed_seconds):.1f}s"))
        except (TypeError, ValueError):
            pass
    if model:
        fields.append(("Model", model))
    if session_id:
        fields.append(("Session", session_id))
    if turn_id:
        fields.append(("Turn", turn_id))
    elif task_id:
        fields.append(("Task", task_id))
    return _lines(status.title(), fields, max_chars)


def approval(*, command: Any = None, description: Any = None, session_key: Any = None, turn_id: Any = None, cwd: Any = None, surface: Any = None, max_chars: int = 3900) -> str:
    fields = [("Project", project_name(cwd)), ("Action", safe_command(command))]
    if description:
        fields.append(("Reason", truncate(description, 500)))
    if surface:
        fields.append(("Surface", surface))
    if session_key:
        fields.append(("Session", session_key))
    if turn_id:
        fields.append(("Turn", turn_id))
    return _lines("Approval required", fields, max_chars)


def approval_response(*, choice: Any = None, command: Any = None, session_key: Any = None, turn_id: Any = None, decided_by: Any = None, max_chars: int = 3900) -> str:
    value = str(choice or "unknown").replace("_", "-")
    fields = [("Status", value), ("Action", safe_command(command))]
    if decided_by:
        fields.append(("Decided by", decided_by))
    if session_key:
        fields.append(("Session", session_key))
    if turn_id:
        fields.append(("Turn", turn_id))
    return _lines("Approval response", fields, max_chars)
