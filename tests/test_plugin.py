"""Unit tests for the standalone Hermes Telegram notification plugin."""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERMES_SRC = Path("/home/max/.hermes/hermes-agent")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if HERMES_SRC.exists() and str(HERMES_SRC) not in sys.path:
    sys.path.insert(0, str(HERMES_SRC))

from hermes_telegram_notify import config, formatting, hooks  # noqa: E402
from hermes_telegram_notify.state import StateStore  # noqa: E402
from hermes_telegram_notify.telegram import TelegramClient, TelegramError  # noqa: E402


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for name in (
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "HERMES_TELEGRAM_CHAT_ID",
        "TELEGRAM_HOME_CHANNEL", "CODEX_TELEGRAM_CHAT_ID",
        "HERMES_TELEGRAM_NOTIFY_ENABLED", "HERMES_TELEGRAM_NOTIFY_START",
        "HERMES_TELEGRAM_NOTIFY_COMPLETION", "HERMES_TELEGRAM_NOTIFY_APPROVAL",
        "HERMES_TELEGRAM_NOTIFY_APPROVAL_RESPONSE",
    ):
        monkeypatch.delenv(name, raising=False)
    return home


def configure(home: Path, monkeypatch, *, token="123456:abcdefghijklmnopqrstuv", chat="42", **updates):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", chat)
    cfg = config.load_config()
    config.save_config({"telegram_chat_id": chat, **updates})
    return cfg


def test_config_defaults_and_environment_override(hermes_home, monkeypatch):
    cfg = config.load_config()
    assert cfg.enabled is True
    assert cfg.configured is False
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:abcdefghijklmnopqrstuv")
    monkeypatch.setenv("CODEX_TELEGRAM_CHAT_ID", "-100123")
    monkeypatch.setenv("HERMES_TELEGRAM_NOTIFY_APPROVAL", "0")
    cfg = config.load_config()
    assert cfg.token.endswith("uv")
    assert cfg.chat_id == "-100123"
    assert cfg.event_enabled("approval") is False


def test_config_missing_and_secret_file_permissions(hermes_home, monkeypatch):
    cfg = config.load_config()
    assert not cfg.configured
    config.save_token("123456:abcdefghijklmnopqrstuv")
    cfg = config.load_config()
    assert cfg.token
    assert config.token_file_is_restricted(cfg.credential_path)
    assert cfg.credential_path.stat().st_mode & 0o077 == 0


def test_token_is_not_in_status_or_log(hermes_home, monkeypatch, capsys):
    token = "123456:abcdefghijklmnopqrstuv"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: Mock())
    hooks.on_pre_llm_call(session_id="s", turn_id="t", model="m", cwd="/tmp/project")
    text = capsys.readouterr().out
    assert token not in text
    log_text = (hermes_home / "telegram-notify" / "telegram-notify.log").read_text()
    assert token not in log_text


def test_redaction_covers_numeric_bot_tokens():
    from hermes_telegram_notify.logging_utils import redact
    assert "123456:abcdefghijklmnopqrstuv" not in redact("error 123456:abcdefghijklmnopqrstuv")
    assert "[REDACTED]" in redact("error 123456:abcdefghijklmnopqrstuv")


def test_telegram_request_generation_and_success():
    response = Mock()
    response.read.return_value = b'{"ok":true,"result":{"message_id":1}}'
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock(return_value=response)
    result = TelegramClient("123456:abcdefghijklmnopqrstuv", opener=opener).send_message("42", "Hermes test")
    assert result["message_id"] == 1
    request = opener.call_args.args[0]
    assert request.full_url.endswith("/sendMessage")
    payload = json.loads(request.data.decode())
    assert payload["chat_id"] == "42"
    assert payload["text"] == "Hermes test"


def test_telegram_network_and_malformed_failures_are_sanitized():
    def fail(*args, **kwargs):
        raise OSError("secret 123456:abcdefghijklmnopqrstuv")
    with pytest.raises(TelegramError, match="network"):
        TelegramClient("123456:abcdefghijklmnopqrstuv", opener=fail).send_message("42", "x")

    response = Mock()
    response.read.return_value = b"not-json"
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    with pytest.raises(TelegramError, match="malformed"):
        TelegramClient("123456:abcdefghijklmnopqrstuv", opener=Mock(return_value=response)).send_message("42", "x")


def test_formatting_truncates_and_redacts():
    text = formatting.approval(
        command="run --api-key=supersecret-value " + "x" * 2000,
        description="reason",
        session_key="s",
        cwd="/home/max/project",
        max_chars=300,
    )
    assert text.startswith("⚠️ Hermes · Approval required")
    assert len(text) <= 300
    assert "supersecret-value" not in text
    assert "project" in text


def test_start_event_is_deduplicated(hermes_home, monkeypatch):
    configure(hermes_home, monkeypatch)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    hooks.on_pre_llm_call(session_id="s", task_id="task", turn_id="turn", user_message="hello", cwd="/tmp/zorro")
    hooks.on_pre_llm_call(session_id="s", task_id="task", turn_id="turn", user_message="hello", cwd="/tmp/zorro")
    assert sender.send_message.call_count == 1
    assert "Hermes · Started" in sender.send_message.call_args.args[1]
    assert "Session: New session" in sender.send_message.call_args.args[1]


def test_named_profile_is_used_as_project_when_cwd_is_absent(tmp_path, monkeypatch):
    home = tmp_path / ".hermes" / "profiles" / "zorro"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert formatting.project_name() == "zorro"


def test_session_title_is_loaded_from_hermes_database(monkeypatch):
    session_db = Mock()
    session_db.get_session_title.return_value = "ZAP M6"
    session_db.__enter__ = Mock(return_value=session_db)
    session_db.__exit__ = Mock(return_value=False)
    fake_module = type("FakeHermesState", (), {"SessionDB": Mock(return_value=session_db)})
    with patch.dict(sys.modules, {"hermes_state": fake_module}):
        assert formatting.session_name(session_id="s") == "ZAP M6"


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ({"completed": True, "failed": False, "interrupted": False}, "completed"),
        ({"completed": False, "failed": False, "interrupted": True}, "interrupted"),
        ({"completed": False, "failed": True, "interrupted": False}, "failed"),
    ],
)
def test_completion_statuses(hermes_home, monkeypatch, flags, expected):
    configure(hermes_home, monkeypatch)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    hooks.on_session_end(session_id="s", turn_id=expected, model="m", cwd="/tmp/zorro", **flags)
    text = sender.send_message.call_args.args[1]
    assert text.startswith({
        "completed": "✅ Hermes · Completed",
        "interrupted": "⏸️ Hermes · Interrupted",
        "failed": "❌ Hermes · Failed",
    }[expected])
    assert "Session: New session" in text
    assert "Turn:" not in text
    assert "Model:" not in text


def test_post_llm_sends_final_response_and_session_end_does_not_duplicate(hermes_home, monkeypatch):
    configure(hermes_home, monkeypatch)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    payload = {
        "session_id": "s",
        "task_id": "task",
        "turn_id": "t",
        "model": "m",
        "platform": "cli",
    }
    hooks.on_post_llm_call(**payload, assistant_response="Final answer from Hermes.")
    hooks.on_session_end(**payload, completed=True, failed=False, interrupted=False)
    assert sender.send_message.call_count == 1
    text = sender.send_message.call_args.args[1]
    assert text.startswith("✅ Hermes · Completed")
    assert "Project:" in text
    assert "Final answer from Hermes." in text
    assert "Session: New session" in text
    assert "Turn:" not in text


def test_final_response_is_redacted_and_bounded(hermes_home, monkeypatch):
    configure(hermes_home, monkeypatch, final_response_max_chars=80)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    hooks.on_post_llm_call(
        session_id="s",
        turn_id="t",
        assistant_response="safe 123456:abcdefghijklmnopqrstuv " + "x" * 1000,
        cwd="/tmp/zorro",
    )
    text = sender.send_message.call_args.args[1]
    assert "123456:abcdefghijklmnopqrstuv" not in text
    assert len(text) <= 3900
    assert text.endswith("…")


def test_completion_is_deduplicated(hermes_home, monkeypatch):
    configure(hermes_home, monkeypatch)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    payload = {"session_id": "s", "turn_id": "t", "completed": True, "failed": False, "interrupted": False}
    hooks.on_session_end(**payload)
    hooks.on_session_end(**payload)
    assert sender.send_message.call_count == 1


def test_approval_request_and_optional_response(hermes_home, monkeypatch):
    configure(hermes_home, monkeypatch, notify_on_approval_response=True)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    hooks.on_pre_approval_request(
        session_key="s", turn_id="t", tool_call_id="c", command="shell --password=bad",
        description="needs permission", cwd="/tmp/p", surface="gateway",
    )
    hooks.on_post_approval_response(session_key="s", turn_id="t", tool_call_id="c", choice="smart_deny", command="shell")
    assert sender.send_message.call_count == 2
    assert "Approval required" in sender.send_message.call_args_list[0].args[1]
    assert "smart-deny" in sender.send_message.call_args_list[1].args[1]
    assert "bad" not in sender.send_message.call_args_list[0].args[1]


def test_smart_assessment_does_not_consume_real_prompt_debounce(hermes_home, monkeypatch):
    configure(hermes_home, monkeypatch)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    payload = {
        "session_key": "s",
        "turn_id": "t",
        "tool_call_id": "c",
        "command": "rm -rf /tmp/example",
        "description": "needs permission",
        "cwd": "/tmp/p",
    }

    hooks.on_pre_approval_request(**payload, surface="smart")
    assert sender.send_message.call_count == 0

    # The real prompt has the same debounce identity, but a different surface.
    hooks.on_pre_approval_request(**payload, surface="gateway")
    assert sender.send_message.call_count == 1
    assert "Approval required" in sender.send_message.call_args.args[1]


def test_malformed_payload_and_disabled_notifications_are_nonfatal(hermes_home, monkeypatch):
    configure(hermes_home, monkeypatch, enabled=False)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    hooks.on_pre_llm_call(object="malformed")
    hooks.on_session_end(object="malformed")
    hooks.on_pre_approval_request(object="malformed")
    assert sender.send_message.call_count == 0


def test_state_atomic_claim_and_locking(hermes_home):
    root = hermes_home / "telegram-notify"
    store = StateStore(root / "state.json", root / "state.lock")
    assert store.claim_start("x", {"session_id": "s"})
    assert not store.claim_start("x", {"session_id": "s"})
    claimed, previous = store.claim_completion("x", {"status": "completed"})
    assert claimed and previous is not None
    claimed, _ = store.claim_completion("x", {"status": "completed"})
    assert not claimed
    assert root / "state.json" in list(root.iterdir())
    assert (root / "state.json").stat().st_mode & 0o077 == 0


def test_chat_discovery_uses_only_safe_fields(monkeypatch):
    from hermes_telegram_notify.telegram import discover_chat_ids
    client = Mock()
    client.get_updates.return_value = [{"update_id": 1, "message": {"chat": {"id": 42, "title": "Work"}, "text": "secret"}}]
    assert discover_chat_ids(client) == [{"chat_id": "42", "label": "Work"}]
