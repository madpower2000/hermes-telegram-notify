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
    text = sender.send_message.call_args.args[1]
    assert "Hermes · Started" in text
    assert "📁 Project: zorro" in text
    assert "👤 Profile: default" in text
    assert "📝 Session: New session" in text


def test_started_message_prefers_stored_profile_and_session_title(hermes_home, monkeypatch):
    configure(hermes_home, monkeypatch)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    session_db = Mock()
    session_db.get_session.return_value = {
        "cwd": "/home/max/Projects/Tripwire",
        "profile_name": "quantlab",
        "title": "Tripwire event-study review",
    }
    session_db.__enter__ = Mock(return_value=session_db)
    session_db.__exit__ = Mock(return_value=False)
    fake_module = type("FakeHermesState", (), {"SessionDB": Mock(return_value=session_db)})

    with patch.dict(sys.modules, {"hermes_state": fake_module}):
        hooks.on_pre_llm_call(
            session_id="sid", task_id="task", turn_id="turn",
            user_message="This is not the saved title", is_first_turn=True,
        )

    assert sender.send_message.call_args.args[1].splitlines() == [
        "🚀 Hermes · Started",
        "📁 Project: Tripwire",
        "👤 Profile: quantlab",
        "📝 Session: Tripwire event-study review",
    ]


def test_started_message_uses_hermes_derived_title_before_title_persists(hermes_home, monkeypatch):
    configure(hermes_home, monkeypatch)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    monkeypatch.setattr(
        formatting,
        "_stored_session_metadata",
        lambda _sid: {"cwd": "/home/max/Projects/Tripwire", "profile_name": "zorro", "title": ""},
    )
    derived = Mock(return_value="Tripwire event-study research")
    monkeypatch.setattr(formatting, "_derived_session_title", derived)

    hooks.on_pre_llm_call(
        session_id="sid", task_id="task", turn_id="turn",
        user_message="Analyze the Tripwire event study", is_first_turn=True,
        conversation_history=[
            {"role": "user", "display_metadata": {"title_preview": "Tripwire event-study research"}},
        ],
    )

    text = sender.send_message.call_args.args[1]
    assert "📁 Project: Tripwire" in text
    assert "👤 Profile: zorro" in text
    assert "📝 Session: Tripwire event-study research" in text
    derived.assert_called_once_with("Analyze the Tripwire event study", "Tripwire event-study research")


def test_subagent_start_is_suppressed_before_root_start_claim(hermes_home, monkeypatch):
    configure(hermes_home, monkeypatch)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    payload = {
        "session_id": "s",
        "task_id": "task",
        "turn_id": "turn",
        "user_message": "hello",
        "cwd": "/tmp/zorro",
    }

    hooks.on_pre_llm_call(**payload, platform="subagent", parent_session_id="root")
    assert sender.send_message.call_count == 0

    # An ignored child event must not claim the key for a later root event.
    # Parent lineage alone is deliberately not a suppression criterion.
    hooks.on_pre_llm_call(**payload, platform="cli", parent_session_id="lineage-parent")
    assert sender.send_message.call_count == 1
    assert "Hermes · Started" in sender.send_message.call_args.args[1]


def test_named_profile_is_used_as_project_when_cwd_is_absent(tmp_path, monkeypatch):
    home = tmp_path / ".hermes" / "profiles" / "zorro"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert formatting.project_name() == "zorro"
    assert formatting._profile_label(None, None) == "zorro"


def test_approval_notification_uses_session_workspace_when_cwd_is_missing(hermes_home, monkeypatch):
    home = hermes_home / "profiles" / "zorro"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    configure(home, monkeypatch)

    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    session_db = Mock()
    session_db.get_session.return_value = {"cwd": "/home/max/Projects/hermes-telegram-notify"}
    session_db.get_session_title.return_value = "Reduce unnecessary Telegram approval notifications"
    session_db.__enter__ = Mock(return_value=session_db)
    session_db.__exit__ = Mock(return_value=False)
    fake_module = type("FakeHermesState", (), {"SessionDB": Mock(return_value=session_db)})

    with patch.dict(sys.modules, {"hermes_state": fake_module}):
        hooks.on_pre_approval_request(
            session_id="session-id", session_key="session-key", turn_id="turn",
            tool_call_id="call", command="git branch -D example", surface="gateway",
        )

    text = sender.send_message.call_args.args[1]
    assert "Project: hermes-telegram-notify" in text
    assert "Project: zorro" not in text
    assert "Session: Reduce unnecessary Telegram approval notifications" in text


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


@pytest.mark.parametrize(
    ("callback_name", "extra", "expected"),
    [
        ("on_post_llm_call", {"assistant_response": "Final answer."}, "Hermes · Completed"),
        ("on_session_end", {"completed": True, "failed": False, "interrupted": False}, "Hermes · Completed"),
        ("on_session_end", {"completed": False, "failed": True, "interrupted": False}, "Hermes · Failed"),
        ("on_session_end", {"completed": False, "failed": False, "interrupted": True}, "Hermes · Interrupted"),
    ],
)
def test_subagent_completion_is_suppressed_before_root_completion_claim(
    hermes_home, monkeypatch, callback_name, extra, expected,
):
    configure(hermes_home, monkeypatch)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    payload = {
        "session_id": "s",
        "task_id": "task",
        "turn_id": "turn",
        "model": "m",
        **extra,
    }

    callback = getattr(hooks, callback_name)
    callback(**payload, platform="subagent", parent_session_id="root")
    assert sender.send_message.call_count == 0

    # The same identity can still produce the root's notification, even when
    # parent lineage metadata is present.
    callback(**payload, platform="cli", parent_session_id="lineage-parent")
    assert sender.send_message.call_count == 1
    assert expected in sender.send_message.call_args.args[1]


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


def test_approval_diagnostics_log_surface_and_outcome_without_payload(hermes_home, monkeypatch):
    configure(hermes_home, monkeypatch)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    payload = {
        "session_key": "private-session-id",
        "turn_id": "private-turn-id",
        "tool_call_id": "private-tool-call-id",
        "pattern_key": "dangerous-command",
        "command": "rm -rf /private/path --token=must-not-be-logged",
        "description": "recursive delete",
        "surface": "smart",
    }

    hooks.on_pre_approval_request(**payload)
    hooks.on_post_approval_response(**payload, choice="smart_approve", decided_by="aux_llm")

    assert sender.send_message.call_count == 0
    log_path = hermes_home / "telegram-notify" / "telegram-notify.log"
    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    request = next(row for row in records if row["event"] == "approval_request_observed")
    skipped = next(row for row in records if row["event"] == "approval_skipped")
    response = next(row for row in records if row["event"] == "approval_response_observed")
    assert request["surface"] == skipped["surface"] == response["surface"] == "smart"
    assert request["approval_id"] == response["approval_id"]
    assert response["choice"] == "smart_approve"
    assert response["decided_by"] == "aux_llm"
    assert isinstance(request["observed_at"], float)
    for secret in (
        payload["command"], payload["description"], payload["session_key"],
        payload["turn_id"], payload["tool_call_id"],
    ):
        assert secret not in log_path.read_text()


def test_approval_notification_log_identifies_its_surface(hermes_home, monkeypatch):
    configure(hermes_home, monkeypatch)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    hooks.on_pre_approval_request(
        session_key="s", turn_id="t", tool_call_id="c", pattern_key="dangerous-command",
        command="git branch -D example", description="git branch force delete", surface="gateway",
    )

    assert sender.send_message.call_count == 1
    log_path = hermes_home / "telegram-notify" / "telegram-notify.log"
    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    request = next(row for row in records if row["event"] == "approval_request_observed")
    sent = next(row for row in records if row.get("event") == "notification_sent")
    assert request["surface"] == sent["surface"] == "gateway"
    assert request["approval_id"] == sent["approval_id"]
    assert sent["notification"] == "approval"
    assert "git branch -D example" not in log_path.read_text()


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


def test_smart_auto_verdicts_never_send_user_action_alert(hermes_home, monkeypatch):
    """Smart auto-approve/deny are internal assessments: with the optional
    approval-response notification disabled (default) nothing may be sent, and
    in particular no 'Approval required' alert."""
    configure(hermes_home, monkeypatch)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    payload = {
        "session_key": "s", "turn_id": "t", "tool_call_id": "c",
        "pattern_key": "dangerous-command",
        "command": "rm -rf /tmp/example", "description": "needs permission",
        "surface": "smart",
    }
    hooks.on_pre_approval_request(**payload)
    hooks.on_post_approval_response(**payload, choice="smart_approve", decided_by="aux_llm")
    hooks.on_post_approval_response(**payload, choice="smart_deny", decided_by="aux_llm")
    assert sender.send_message.call_count == 0


def test_smart_verdict_response_is_informational_when_enabled(hermes_home, monkeypatch):
    """When the operator enables approval-response notifications, a smart
    verdict is delivered as an informational message, not an action alert."""
    configure(hermes_home, monkeypatch, notify_on_approval_response=True)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    hooks.on_post_approval_response(
        session_key="s", turn_id="t", tool_call_id="c",
        command="rm -rf /tmp/example", surface="smart",
        choice="smart_deny", decided_by="aux_llm",
    )
    assert sender.send_message.call_count == 1
    text = sender.send_message.call_args.args[1]
    assert "Approval required" not in text
    assert "smart-deny" in text
    assert "aux_llm" in text


def test_approval_response_default_is_off(hermes_home, monkeypatch):
    configure(hermes_home, monkeypatch)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    hooks.on_post_approval_response(
        session_key="s", turn_id="t", tool_call_id="c",
        command="git push", choice="once",
    )
    assert sender.send_message.call_count == 0


@pytest.mark.parametrize("surface", ["cli", "gateway", "transport:mytransport", "mcp-elicitation"])
def test_user_facing_approval_surfaces_notify(hermes_home, monkeypatch, surface):
    configure(hermes_home, monkeypatch)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    hooks.on_pre_approval_request(
        session_key="s", turn_id=f"t-{surface}", tool_call_id=f"c-{surface}",
        command="git branch -D example", description="force delete",
        surface=surface,
    )
    assert sender.send_message.call_count == 1
    assert "Approval required" in sender.send_message.call_args.args[1]


def test_coalesced_approval_still_notifies_once_then_debounces(hermes_home, monkeypatch):
    configure(hermes_home, monkeypatch)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    payload = {
        "session_key": "s", "turn_id": "t", "tool_call_id": "c",
        "command": "rm -rf /tmp/example", "description": "needs permission",
        "surface": "gateway", "coalesced": True,
    }
    hooks.on_pre_approval_request(**payload)
    assert sender.send_message.call_count == 1
    # A repeated coalesced event in the debounce window is suppressed.
    hooks.on_pre_approval_request(**payload)
    assert sender.send_message.call_count == 1


def test_malformed_post_approval_payload_is_nonfatal(hermes_home, monkeypatch):
    """A payload missing every expected key must degrade to safe defaults and
    never raise — the turn is never broken by a malformed approval event."""
    configure(hermes_home, monkeypatch, notify_on_approval_response=True)
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: Mock())
    # All keys absent: the handler must tolerate it, not raise.
    hooks.on_post_approval_response()
    # And the observed record must be present, never an uncaught error.
    log_path = hermes_home / "telegram-notify" / "telegram-notify.log"
    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert any(row.get("event") == "approval_response_observed" for row in records)
    assert all(row.get("event") != "hook_failed" for row in records)


def test_telegram_failure_is_fail_open_at_hook_boundary(hermes_home, monkeypatch):
    configure(hermes_home, monkeypatch)

    def boom(*args, **kwargs):
        raise TelegramError("Telegram network request failed")

    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: Mock(send_message=boom))
    # Must not raise; the turn is never aborted by a delivery failure.
    hooks.on_pre_llm_call(session_id="s", turn_id="t", user_message="hello")
    hooks.on_post_llm_call(session_id="s", turn_id="t", assistant_response="done")
    hooks.on_session_end(session_id="s", turn_id="t", completed=False, failed=True, interrupted=False)
    log_path = hermes_home / "telegram-notify" / "telegram-notify.log"
    records = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert any(row.get("event") == "notification_failed" for row in records)
    assert all("123456" not in line for line in log_path.read_text().splitlines())


def test_missing_chat_id_skips_safely(hermes_home, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:abcdefghijklmnopqrstuv")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    config.save_config({"telegram_chat_id": "", "enabled": True})
    assert config.load_config().configured is False
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    hooks.on_pre_llm_call(session_id="s", turn_id="t", user_message="hello")
    hooks.on_post_llm_call(session_id="s", turn_id="t", assistant_response="done")
    assert sender.send_message.call_count == 0


def test_profiles_do_not_share_credentials_or_state(tmp_path, monkeypatch):
    """Two HERMES_HOME roots must resolve separate credentials, config, and
    state — one profile cannot read another profile's stored token or dedupe
    state. Credentials are written to each profile's own credentials.json
    (no process env) so the test isolates file-level scoping."""
    homes = {}
    for name in ("alpha", "beta"):
        home = tmp_path / f".hermes-{name}"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        for env in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "HERMES_TELEGRAM_CHAT_ID"):
            monkeypatch.delenv(env, raising=False)
        config.save_token(f"123456:{name}{'x' * 13}")
        config.save_config({"telegram_chat_id": f"100_{name}"})
        homes[name] = home

    # Load beta with no process token: it must read beta's own credentials,
    # never alpha's file.
    monkeypatch.setenv("HERMES_HOME", str(homes["beta"]))
    beta_cfg = config.load_config()
    assert beta_cfg.chat_id == "100_beta"
    assert beta_cfg.configured is True
    assert beta_cfg.token.endswith("beta" + "x" * 13)
    assert "alpha" not in beta_cfg.token
    # Alpha's credentials file must not appear under beta's home.
    assert "alpha" not in (homes["beta"] / "telegram-notify" / "credentials.json").read_text()
    # State is written per profile and never shared.
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: Mock())
    monkeypatch.setenv("HERMES_HOME", str(homes["alpha"]))
    hooks.on_pre_llm_call(session_id="s", turn_id="t1", user_message="hello")
    alpha_state = homes["alpha"] / "telegram-notify" / "state.json"
    beta_state = homes["beta"] / "telegram-notify" / "state.json"
    assert alpha_state.exists()
    assert not beta_state.exists()  # beta never claimed a run, so no state file


def test_hook_exception_is_fail_open(hermes_home, monkeypatch):
    """Any internal error in a hook callback must not propagate to Hermes."""
    monkeypatch.setattr(config, "load_config", Mock(side_effect=RuntimeError("boom")))
    for callback in (hooks.on_pre_llm_call, hooks.on_post_llm_call, hooks.on_session_end,
                     hooks.on_pre_approval_request, hooks.on_post_approval_response):
        callback(session_id="s", turn_id="t")  # must not raise


def test_chat_discovery_uses_only_safe_fields(monkeypatch):
    from hermes_telegram_notify.telegram import discover_chat_ids
    client = Mock()
    client.get_updates.return_value = [{"update_id": 1, "message": {"chat": {"id": 42, "title": "Work"}, "text": "secret"}}]
    assert discover_chat_ids(client) == [{"chat_id": "42", "label": "Work"}]
