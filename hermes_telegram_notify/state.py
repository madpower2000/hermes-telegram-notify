"""Small locked, atomically-written state store for notification idempotence."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator, Mapping

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

try:
    from .logging_utils import record
except ImportError:  # pragma: no cover
    record = None


_EMPTY = {"version": 1, "runs": {}, "approvals": {}, "updated_at": 0.0}


def _timestamp(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def fingerprint(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:32]


class StateStore:
    def __init__(self, path: Path, lock_path: Path, retention_days: int = 7, log_path: Path | None = None):
        self.path = path
        self.lock_path = lock_path
        self.retention_seconds = max(1, retention_days) * 86400
        self.log_path = log_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.touch(exist_ok=True)
        try:
            self.lock_path.chmod(0o600)
        except OSError:
            pass

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, Any]]:
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                data = self._read()
                self._purge(data)
                yield data
                data["updated_at"] = time.time()
                self._write(data)
            finally:
                if fcntl is not None:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return dict(_EMPTY)
        if not isinstance(value, dict):
            return dict(_EMPTY)
        runs = value.get("runs") if isinstance(value.get("runs"), dict) else {}
        approvals = value.get("approvals") if isinstance(value.get("approvals"), dict) else {}
        return {"version": 1, "runs": runs, "approvals": approvals, "updated_at": _timestamp(value.get("updated_at"))}

    def _purge(self, data: dict[str, Any]) -> None:
        cutoff = time.time() - self.retention_seconds
        data["runs"] = {
            key: value for key, value in data["runs"].items()
            if isinstance(value, dict) and _timestamp(value.get("updated_at") or value.get("started_at")) >= cutoff
        }
        data["approvals"] = {
            key: value for key, value in data["approvals"].items()
            if isinstance(value, dict) and _timestamp(value.get("updated_at")) >= cutoff
        }

    def _write(self, data: Mapping[str, Any]) -> None:
        fd, name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=str(self.path.parent), text=True)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        except Exception:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise

    def claim_start(self, key: str, metadata: Mapping[str, Any]) -> bool:
        now = time.time()
        with self._locked() as data:
            if key in data["runs"]:
                return False
            data["runs"][key] = {**metadata, "started_at": now, "updated_at": now, "completion_claimed": False}
            return True

    def claim_completion(self, key: str, metadata: Mapping[str, Any]) -> tuple[bool, dict[str, Any] | None]:
        now = time.time()
        with self._locked() as data:
            previous = data["runs"].get(key)
            if isinstance(previous, dict) and previous.get("completion_claimed"):
                return False, previous
            item = dict(previous) if isinstance(previous, dict) else {}
            item.update(metadata)
            item["completion_claimed"] = True
            item["updated_at"] = now
            data["runs"][key] = item
            return True, previous if isinstance(previous, dict) else None

    def claim_approval(self, key: str, metadata: Mapping[str, Any], debounce_seconds: int) -> bool:
        now = time.time()
        with self._locked() as data:
            previous = data["approvals"].get(key)
            if isinstance(previous, dict) and now - _timestamp(previous.get("updated_at")) < max(0, debounce_seconds):
                return False
            data["approvals"][key] = {**metadata, "updated_at": now}
            return True

    def snapshot(self) -> dict[str, Any]:
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                return self._read()
            finally:
                if fcntl is not None:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
