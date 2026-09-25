"""Bounded, token-redacting JSONL logging."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_LOGGER_PREFIX = "hermes.telegram_notify"
_LOGGER_CONFIG_LOCK = threading.RLock()
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


def _logger_name(path: Path) -> str:
    digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:20]
    return f"{_LOGGER_PREFIX}.{digest}"


def _restrict_fd(fd: int, path: Path) -> None:
    if hasattr(os, "fchmod"):
        os.fchmod(fd, 0o600)
    else:  # pragma: no cover - Windows fallback
        os.chmod(path, 0o600)


def _create_secure_log(path: Path) -> None:
    flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        _restrict_fd(fd, path)
    finally:
        os.close(fd)


class _SecureRotatingFileHandler(RotatingFileHandler):
    """Restrict active and rotated diagnostics before they can be written."""

    _hermes_telegram_notify = True

    def _open(self):
        stream = super()._open()
        try:
            _restrict_fd(stream.fileno(), Path(self.baseFilename))
        except Exception:
            stream.close()
            raise
        return stream

    def doRollover(self) -> None:
        super().doRollover()
        backup = Path(f"{self.baseFilename}.1")
        if backup.exists():
            backup.chmod(0o600)


def configure_logging(path, max_bytes: int = 524_288) -> logging.Logger:
    path = Path(os.path.abspath(Path(path).expanduser()))
    path.parent.mkdir(parents=True, exist_ok=True)
    _create_secure_log(path)
    log = logging.getLogger(_logger_name(path))
    log.setLevel(logging.INFO)
    log.propagate = False
    with _LOGGER_CONFIG_LOCK:
        handlers = [h for h in log.handlers if isinstance(h, _SecureRotatingFileHandler)]
        if not handlers:
            handler = _SecureRotatingFileHandler(
                path,
                maxBytes=max_bytes,
                backupCount=1,
                encoding="utf-8",
                delay=False,
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            log.addHandler(handler)
        else:
            for handler in handlers:
                handler.maxBytes = max(0, int(max_bytes))
    return log


def record(path, event: str, *, max_bytes: int = 524_288, level: int = logging.INFO, **fields: Any) -> None:
    log = configure_logging(path, max_bytes=max_bytes)
    payload = {"event": event, **redact(fields)}
    log.log(level, json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str))
