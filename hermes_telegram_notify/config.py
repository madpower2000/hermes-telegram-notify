"""Configuration and profile-safe secret resolution for telegram-notify.

Hermes profile secrets are resolved with ``agent.secret_scope``. In a
multiplexed runtime that scope is authoritative; this module never falls back
to the launch profile's process environment. The token remains outside
config.json, with the profile-local credentials file as a separate fallback.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

try:
    from hermes_constants import get_hermes_home
except Exception:  # pragma: no cover - only used outside an Hermes checkout
    def get_hermes_home() -> Path:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "notify_on_start": True,
    "notify_on_completion": True,
    "notify_on_approval": True,
    "notify_on_approval_response": False,
    "approval_debounce_seconds": 60,
    "max_message_chars": 3900,
    "final_response_max_chars": 3200,
    "telegram_timeout_seconds": 4,
    "state_retention_days": 7,
    "log_max_bytes": 524_288,
    "include_model": True,
    "include_cwd": True,
    "include_session": True,
}

_BOOL_ENV = {
    "enabled": ("HERMES_TELEGRAM_NOTIFY_ENABLED", "CODEX_TELEGRAM_NOTIFY_ENABLED"),
    "notify_on_start": ("HERMES_TELEGRAM_NOTIFY_START",),
    "notify_on_completion": ("HERMES_TELEGRAM_NOTIFY_COMPLETION",),
    "notify_on_approval": ("HERMES_TELEGRAM_NOTIFY_APPROVAL", "CODEX_TELEGRAM_PERMISSION_ALERTS"),
    "notify_on_approval_response": ("HERMES_TELEGRAM_NOTIFY_APPROVAL_RESPONSE",),
}
_CHAT_ENV = (
    "HERMES_TELEGRAM_CHAT_ID",
    "TELEGRAM_CHAT_ID",
    "CODEX_TELEGRAM_CHAT_ID",
    "TELEGRAM_HOME_CHANNEL",
)
_TOKEN_ENV = ("TELEGRAM_BOT_TOKEN",)

# Hermes treats the exact HERMES_TELEGRAM_ prefix as process-global. The
# notification switches and timeout/debounce knobs are global tuning; the
# legacy CHAT_ID destination is single-profile-only and is ignored under
# multiplexing to prevent cross-profile routing.
_GLOBAL_TUNING_ENV = frozenset({
    "HERMES_TELEGRAM_NOTIFY_ENABLED",
    "HERMES_TELEGRAM_NOTIFY_START",
    "HERMES_TELEGRAM_NOTIFY_COMPLETION",
    "HERMES_TELEGRAM_NOTIFY_APPROVAL",
    "HERMES_TELEGRAM_NOTIFY_APPROVAL_RESPONSE",
    "HERMES_TELEGRAM_APPROVAL_DEBOUNCE",
    "HERMES_TELEGRAM_TIMEOUT",
})


@dataclass(frozen=True)
class ResolvedConfig:
    values: dict[str, Any]
    token: str
    chat_id: str
    root: Path
    config_path: Path
    credential_path: Path
    state_path: Path
    lock_path: Path
    log_path: Path

    @property
    def enabled(self) -> bool:
        return bool(self.values.get("enabled", True))

    def event_enabled(self, event: str) -> bool:
        return self.enabled and bool(self.values.get(f"notify_on_{event}", False))

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)


def plugin_root() -> Path:
    root = get_hermes_home() / "telegram-notify"
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_json_write(path: Path, data: Mapping[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        try:
            path.chmod(mode)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def _read_dotenv_value(key: str) -> str:
    """Read one key from the active profile .env without logging its value."""
    env_path = get_hermes_home() / ".env"
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return ""
    prefix = f"{key}="
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or not line.startswith(prefix):
            continue
        value = line[len(prefix):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value
    return ""


def _secret_scope_api():
    """Return Hermes' required profile secret-scope API; never silently downgrade."""
    from agent.secret_scope import get_secret_str, is_multiplex_active
    return get_secret_str, is_multiplex_active


def _profile_env_value(key: str) -> str:
    """Resolve profile credentials/config without borrowing ambient multiplex values."""
    get_secret_str, is_multiplex_active = _secret_scope_api()
    multiplexed = is_multiplex_active()
    if key == "HERMES_TELEGRAM_CHAT_ID" and multiplexed:
        # Hermes classifies the HERMES_TELEGRAM_ prefix as process-global, so
        # this destination cannot safely be used for a routed profile. Ignore
        # it under multiplexing rather than sending a secondary profile's text
        # to the launch profile's chat; profile chat IDs use the other aliases.
        value = ""
    else:
        value = get_secret_str(key, "")

    # Outside multiplexing preserve the plugin's historical support for a
    # profile .env even when Hermes' CLI did not load it into os.environ.
    if not value and not multiplexed:
        value = _read_dotenv_value(key)
    return str(value or "").strip()


def _global_tuning_value(key: str) -> str:
    """Read only explicitly classified process-global Hermes tuning values."""
    get_secret_str, _ = _secret_scope_api()
    return str(get_secret_str(key, "") or "").strip()


def resolve_profile_environment_value(key: str) -> str:
    """Resolve a user-selected environment key in the active profile scope."""
    _, is_multiplex_active = _secret_scope_api()
    if is_multiplex_active():
        # get_secret_str deliberately reads Hermes-global names from
        # os.environ even with a profile scope bound. --token-env is different:
        # it must never turn into an ambient credential escape hatch. Use
        # Hermes' own classifier and refuse any global name while multiplexing.
        from agent.secret_scope import _is_global_env
        if _is_global_env(key):
            return ""
    return _profile_env_value(key)


def _environment_value(key: str) -> str:
    if key in _GLOBAL_TUNING_ENV:
        return _global_tuning_value(key)
    return _profile_env_value(key)


def _first_env(keys: tuple[str, ...]) -> str:
    for key in keys:
        value = _environment_value(key)
        if value:
            return value
    return ""


def _parse_bool(value: str, default: bool) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _apply_environment(values: dict[str, Any]) -> None:
    for key, env_names in _BOOL_ENV.items():
        raw = _first_env(env_names)
        if raw:
            values[key] = _parse_bool(raw, bool(values[key]))
    numeric = {
        "approval_debounce_seconds": ("HERMES_TELEGRAM_APPROVAL_DEBOUNCE",),
        "telegram_timeout_seconds": ("HERMES_TELEGRAM_TIMEOUT",),
    }
    for key, names in numeric.items():
        raw = _first_env(names)
        if raw:
            try:
                values[key] = max(0, int(raw))
            except ValueError:
                pass


def load_config() -> ResolvedConfig:
    root = plugin_root()
    config_path = root / "config.json"
    credential_path = root / "credentials.json"
    state_path = root / "state.json"
    lock_path = root / "state.lock"
    log_path = root / "telegram-notify.log"

    values = dict(DEFAULTS)
    values.update({k: v for k, v in _read_json(config_path).items() if k in DEFAULTS})
    _apply_environment(values)

    token = _first_env(_TOKEN_ENV)
    if not token:
        credentials = _read_json(credential_path)
        token = str(credentials.get("telegram_bot_token") or credentials.get("bot_token") or "").strip()
    configured_chat_id = _read_json(config_path).get("telegram_chat_id", "")
    chat_id = _first_env(_CHAT_ENV) or str(configured_chat_id or "").strip()

    return ResolvedConfig(
        values=values,
        token=token,
        chat_id=chat_id,
        root=root,
        config_path=config_path,
        credential_path=credential_path,
        state_path=state_path,
        lock_path=lock_path,
        log_path=log_path,
    )


def save_config(updates: Mapping[str, Any]) -> Path:
    cfg = load_config()
    current = _read_json(cfg.config_path)
    for key, value in updates.items():
        if key not in DEFAULTS and key != "telegram_chat_id":
            raise ValueError(f"unknown configuration key: {key}")
        current[key] = value
    _atomic_json_write(cfg.config_path, current)
    return cfg.config_path


def save_token(token: str) -> Path:
    token = token.strip()
    if not token or any(ch.isspace() for ch in token):
        raise ValueError("token must be non-empty and contain no whitespace")
    cfg = load_config()
    _atomic_json_write(cfg.credential_path, {"telegram_bot_token": token})
    return cfg.credential_path


def token_file_is_restricted(path: Path) -> bool:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return False
    return mode & 0o077 == 0
