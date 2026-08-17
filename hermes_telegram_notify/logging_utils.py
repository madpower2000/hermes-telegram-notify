"""Bounded, token-redacting JSONL logging."""

from __future__ import annotations

import json
import logging
import re
from logging.handlers import RotatingFileHandler
from typing import Any

_LOGGER_NAME = "hermes.telegram_notify"
_TOKEN_RE = re.compile(r"(?:bot)?\d{6,}:[A-Za-z0-9_-]{20,}", re.IGNORECASE)
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~-]+")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): redact(v) for k, v in value.items() if str(k).lower() not in {"token", "bot_token", "telegram_bot_token", "authorization"}}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = _TOKEN_RE.sub("[REDACTED]", value)
        return _BEARER_RE.sub(r"\1[REDACTED]", value)
    return value


def logger() -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME)


def configure_logging(path, max_bytes: int = 524_288, backup_bytes: int = 262_144) -> logging.Logger:
    log = logger()
    log.setLevel(logging.INFO)
    log.propagate = False
    path = path.resolve()
    for existing in list(log.handlers):
        if not getattr(existing, "_hermes_telegram_notify", False):
            continue
        if getattr(existing, "baseFilename", None) != str(path):
            log.removeHandler(existing)
            existing.close()
    if not any(getattr(h, "_hermes_telegram_notify", False) for h in log.handlers):
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=max_bytes,
            backupCount=1,
            encoding="utf-8",
            delay=True,
        )
        handler._hermes_telegram_notify = True
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    return log


def record(path, event: str, *, max_bytes: int = 524_288, backup_bytes: int = 262_144, level: int = logging.INFO, **fields: Any) -> None:
    log = configure_logging(path, max_bytes=max_bytes, backup_bytes=backup_bytes)
    payload = {"event": event, **redact(fields)}
    log.log(level, json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
