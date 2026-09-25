"""Unit tests for the standalone Hermes Telegram notification plugin."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import shutil
import stat
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
HERMES_SRC = Path(os.environ.get("HERMES_SRC", "/home/max/.hermes/hermes-agent"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if HERMES_SRC.exists() and str(HERMES_SRC) not in sys.path:
    sys.path.insert(0, str(HERMES_SRC))

from hermes_telegram_notify import cli, config, formatting, hooks  # noqa: E402
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
        "CODEX_TELEGRAM_NOTIFY_ENABLED", "CODEX_TELEGRAM_PERMISSION_ALERTS",
        "HERMES_TELEGRAM_NOTIFY_ENABLED", "HERMES_TELEGRAM_NOTIFY_START",
        "HERMES_TELEGRAM_NOTIFY_COMPLETION", "HERMES_TELEGRAM_NOTIFY_APPROVAL",
        "HERMES_TELEGRAM_NOTIFY_APPROVAL_RESPONSE",
        "HERMES_TELEGRAM_APPROVAL_DEBOUNCE", "HERMES_TELEGRAM_TIMEOUT",
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


def test_multiplex_secondary_secret_scope_beats_ambient_default(hermes_home, monkeypatch):
    from agent import secret_scope

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "default-profile-sentinel")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "default-chat-sentinel")
    monkeypatch.setenv("HERMES_TELEGRAM_CHAT_ID", "default-prefixed-chat-sentinel")
    scope = {
        "TELEGRAM_BOT_TOKEN": "secondary-profile-sentinel",
        "TELEGRAM_CHAT_ID": "secondary-profile-chat",
        # Hermes' HERMES_TELEGRAM_ prefix is process-global and is not a
        # profile-scoped destination, even if present in a raw .env mapping.
        "HERMES_TELEGRAM_CHAT_ID": "secondary-hermes-chat",
    }
    token = secret_scope.set_secret_scope(scope, profile_home=str(hermes_home))
    try:
        cfg = config.load_config()
    finally:
        secret_scope.reset_secret_scope(token)

    assert cfg.token == "secondary-profile-sentinel"
    assert cfg.chat_id == "secondary-profile-chat"
    assert "default-profile-sentinel" not in cfg.token
    assert "default-prefixed-chat-sentinel" not in cfg.chat_id


def test_multiplex_scoped_miss_does_not_borrow_ambient_credentials(hermes_home, monkeypatch):
    from agent import secret_scope

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "default-profile-sentinel")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "default-chat-sentinel")
    monkeypatch.setenv("HERMES_TELEGRAM_CHAT_ID", "default-prefixed-chat-sentinel")
    monkeypatch.setenv("CODEX_TELEGRAM_CHAT_ID", "default-legacy-chat-sentinel")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "default-home-channel-sentinel")
    token = secret_scope.set_secret_scope({}, profile_home=str(hermes_home))
    try:
        cfg = config.load_config()
    finally:
        secret_scope.reset_secret_scope(token)

    assert cfg.token == ""
    assert cfg.chat_id == ""
    assert cfg.configured is False


def test_hermes_notification_tuning_remains_process_global(hermes_home, monkeypatch):
    from agent import secret_scope

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    monkeypatch.setenv("HERMES_TELEGRAM_NOTIFY_ENABLED", "0")
    scope = {
        "TELEGRAM_BOT_TOKEN": "secondary-profile-sentinel",
        "TELEGRAM_CHAT_ID": "secondary-profile-chat",
        "HERMES_TELEGRAM_NOTIFY_ENABLED": "1",
    }
    token = secret_scope.set_secret_scope(scope, profile_home=str(hermes_home))
    try:
        cfg = config.load_config()
    finally:
        secret_scope.reset_secret_scope(token)

    # Hermes classifies the HERMES_TELEGRAM_ prefix as process-global tuning;
    # profile-specific persistent settings remain in config.json.
    assert cfg.enabled is False


def test_multiplex_without_secret_scope_fails_closed(hermes_home, monkeypatch):
    from agent import secret_scope

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "default-profile-sentinel")
    scope_token = secret_scope.set_secret_scope(None)
    try:
        with pytest.raises(secret_scope.UnscopedSecretError):
            config.load_config()
    finally:
        secret_scope.reset_secret_scope(scope_token)


def test_single_profile_still_accepts_environment_credentials(hermes_home, monkeypatch):
    from agent import secret_scope

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "single-profile-sentinel")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "single-profile-chat")
    cfg = config.load_config()
    assert cfg.token == "single-profile-sentinel"
    assert cfg.chat_id == "single-profile-chat"


def test_cli_token_env_resolves_from_active_profile_scope(hermes_home, monkeypatch):
    from agent import secret_scope

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    monkeypatch.setenv("MY_TELEGRAM_TOKEN", "default-profile-sentinel")
    scope_token = secret_scope.set_secret_scope(
        {"MY_TELEGRAM_TOKEN": "secondary-profile-sentinel"},
        profile_home=str(hermes_home),
    )
    try:
        assert config.resolve_profile_environment_value("MY_TELEGRAM_TOKEN") == "secondary-profile-sentinel"
    finally:
        secret_scope.reset_secret_scope(scope_token)


def test_cli_token_env_rejects_hermes_global_name_in_multiplex(hermes_home, monkeypatch):
    from agent import secret_scope

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    monkeypatch.setenv("HERMES_TELEGRAM_BOT_TOKEN", "default-profile-sentinel")
    scope_token = secret_scope.set_secret_scope(
        {"HERMES_TELEGRAM_BOT_TOKEN": "secondary-profile-sentinel"},
        profile_home=str(hermes_home),
    )
    try:
        assert config.resolve_profile_environment_value("HERMES_TELEGRAM_BOT_TOKEN") == ""
    finally:
        secret_scope.reset_secret_scope(scope_token)


def test_log_backup_bytes_is_no_longer_supported_configuration(hermes_home):
    assert "log_backup_bytes" not in config.DEFAULTS
    with pytest.raises(ValueError, match="unknown configuration key"):
        config.save_config({"log_backup_bytes": 100})


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


def test_cli_test_message_redacts_credentials_before_telegram_send(hermes_home, monkeypatch, capsys):
    configure(hermes_home, monkeypatch)
    sender = Mock()
    monkeypatch.setattr(cli, "TelegramClient", lambda *a, **k: sender)
    message = (
        "Status 987654:synthetic_test_value_only_not_real; "
        "Bearer synthetic_bearer_value_only_not_real; still useful"
    )

    assert cli._test(argparse.Namespace(message=message)) == 0
    assert capsys.readouterr().out == "Telegram test notification sent\n"
    sent = sender.send_message.call_args.args[1]
    assert "987654:synthetic_test_value_only_not_real" not in sent
    assert "synthetic_bearer_value_only_not_real" not in sent
    assert "Status" in sent and "still useful" in sent


def test_redaction_covers_numeric_bot_tokens():
    from hermes_telegram_notify.logging_utils import redact
    assert "123456:abcdefghijklmnopqrstuv" not in redact("error 123456:abcdefghijklmnopqrstuv")
    assert "[REDACTED]" in redact("error 123456:abcdefghijklmnopqrstuv")


def test_log_creation_rotation_and_permissions_are_secure(tmp_path):
    from hermes_telegram_notify import logging_utils

    path = tmp_path / "telegram-notify.log"
    old_umask = os.umask(0o022)
    try:
        log = logging_utils.configure_logging(path, max_bytes=1)
        assert path.exists()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        log.info("first diagnostic")
        log.info("second diagnostic triggers rotation")
        backup = path.with_name(path.name + ".1")
        assert backup.exists()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    finally:
        os.umask(old_umask)


def test_log_max_bytes_reconfiguration_updates_existing_handler(tmp_path):
    from hermes_telegram_notify import logging_utils

    path = tmp_path / "telegram-notify.log"
    log = logging_utils.configure_logging(path, max_bytes=1024)
    handler = next(
        h for h in log.handlers if isinstance(h, logging_utils._SecureRotatingFileHandler)
    )
    assert handler.maxBytes == 1024

    logging_utils.configure_logging(path, max_bytes=64)
    assert handler.maxBytes == 64


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform has no O_NOFOLLOW")
def test_logger_rejects_symlinked_log_file(tmp_path):
    from hermes_telegram_notify import logging_utils

    target = tmp_path / "outside.log"
    target.write_text("leave this target alone", encoding="utf-8")
    target.chmod(0o644)
    link = tmp_path / "telegram-notify.log"
    link.symlink_to(target)

    with pytest.raises(OSError):
        logging_utils.configure_logging(link)
    assert target.read_text(encoding="utf-8") == "leave this target alone"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_profile_loggers_cannot_cross_write(tmp_path):
    from hermes_telegram_notify import logging_utils

    path_a = tmp_path / "profile-a" / "telegram-notify.log"
    path_b = tmp_path / "profile-b" / "telegram-notify.log"
    log_a = logging_utils.configure_logging(path_a)
    log_b = logging_utils.configure_logging(path_b)

    # Reconfigure B between A's handler selection and its actual write. A
    # process-global mutable logger used to route this A record into B's file.
    log_a.info("only-profile-a")
    log_b.info("only-profile-b")

    assert "only-profile-a" in path_a.read_text(encoding="utf-8")
    assert "only-profile-a" not in path_b.read_text(encoding="utf-8")
    assert "only-profile-b" in path_b.read_text(encoding="utf-8")
    assert "only-profile-b" not in path_a.read_text(encoding="utf-8")


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


def test_safe_text_redacts_credentials_and_preserves_surrounding_text():
    text = formatting.safe_text(
        "Approval context 987654:synthetic_test_value_only_not_real and "
        "Bearer synthetic_bearer_value_only_not_real remain useful", 200,
    )
    assert "987654:synthetic_test_value_only_not_real" not in text
    assert "synthetic_bearer_value_only_not_real" not in text
    assert "Approval context" in text
    assert "remain useful" in text


def test_safe_text_redacts_secret_assignments_and_quoted_command_arguments():
    context = formatting.safe_text(
        'setup password="synthetic phrase secret" and api_key=synthetic-api-value',
    )
    command = formatting.safe_command(
        'run --password "synthetic phrase secret" --token=synthetic-token-value',
    )
    for secret in (
        "synthetic phrase secret",
        "synthetic-api-value",
        "synthetic-token-value",
    ):
        assert secret not in context
        assert secret not in command
    assert "setup" in context
    assert "run" in command


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


@pytest.mark.parametrize(
    ("setting", "event"),
    [
        ("enabled", "start"),
        ("notify_on_start", "start"),
        ("notify_on_completion", "completion"),
        ("notify_on_approval", "approval"),
        ("notify_on_approval_response", "approval_response"),
    ],
)
@pytest.mark.parametrize("value", [False, True])
def test_notification_boolean_settings_change_telegram_output(
    hermes_home, monkeypatch, setting, event, value,
):
    configure(hermes_home, monkeypatch, **{setting: value})
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    identity = f"{setting}-{value}"

    if event == "start":
        hooks.on_pre_llm_call(session_id=identity, turn_id=identity, cwd="/tmp/project")
    elif event == "completion":
        hooks.on_session_end(
            session_id=identity, turn_id=identity,
            completed=True, failed=False, interrupted=False,
        )
    elif event == "approval":
        hooks.on_pre_approval_request(
            session_id=identity, session_key=identity, turn_id=identity,
            tool_call_id=identity, command="git status", surface="gateway",
        )
    else:
        hooks.on_post_approval_response(
            session_id=identity, session_key=identity, turn_id=identity,
            tool_call_id=identity, choice="once",
        )

    assert sender.send_message.call_count == int(value)
    if value:
        text = sender.send_message.call_args.args[1]
        assert text


@pytest.mark.parametrize("include_model", [False, True])
def test_include_model_controls_start_and_completion_text(hermes_home, monkeypatch, include_model):
    configure(hermes_home, monkeypatch, include_model=include_model)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    hooks.on_pre_llm_call(
        session_id="model-start", turn_id="model-start", model="provider/model-test",
    )
    hooks.on_session_end(
        session_id="model-end", turn_id="model-end", model="provider/model-test",
        completed=True, failed=False, interrupted=False,
    )
    texts = [call.args[1] for call in sender.send_message.call_args_list]
    assert len(texts) == 2
    assert all(("Model: provider/model-test" in text) is include_model for text in texts)


@pytest.mark.parametrize("include_cwd", [False, True])
def test_include_cwd_controls_project_identity_in_telegram_text(hermes_home, monkeypatch, include_cwd):
    configure(hermes_home, monkeypatch, include_cwd=include_cwd)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    hooks.on_pre_llm_call(
        session_id="cwd-test", turn_id="cwd-test", cwd="/home/operator/private-project",
    )
    text = sender.send_message.call_args.args[1]
    assert ("Project: private-project" in text) is include_cwd


@pytest.mark.parametrize("include_session", [False, True])
def test_include_session_controls_session_text(hermes_home, monkeypatch, include_session):
    configure(hermes_home, monkeypatch, include_session=include_session)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    hooks.on_pre_llm_call(
        session_id="session-toggle", turn_id="session-toggle", session_name="Visible title",
    )
    text = sender.send_message.call_args.args[1]
    assert ("Session: Visible title" in text) is include_session


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


def test_started_message_uses_new_session_when_title_is_not_persisted(hermes_home, monkeypatch):
    configure(hermes_home, monkeypatch)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    monkeypatch.setattr(
        formatting,
        "_stored_session_metadata",
        lambda _sid: {"cwd": "/home/max/Projects/Tripwire", "profile_name": "zorro", "title": ""},
    )
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
    assert "📝 Session: New session" in text
    assert "Analyze the Tripwire event study" not in text
    assert "Tripwire event-study research" not in text


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
    assert "Model: m" in text


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
    assert "Model: m" in text
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


def test_approval_description_is_redacted_before_telegram_delivery(hermes_home, monkeypatch):
    configure(hermes_home, monkeypatch)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    synthetic_secret = "987654:synthetic_test_value_only_not_real"
    hooks.on_pre_approval_request(
        session_key="s", turn_id="t", tool_call_id="c", surface="gateway",
        command="git status",
        description=f"Review context {synthetic_secret} and keep this detail",
    )
    text = sender.send_message.call_args.args[1]
    assert synthetic_secret not in text
    assert "Review context" in text
    assert "keep this detail" in text


@pytest.mark.parametrize(
    ("failed", "interrupted", "title"),
    [(True, False, "Failed"), (False, True, "Interrupted")],
)
def test_failure_and_interruption_reasons_are_redacted(
    hermes_home, monkeypatch, failed, interrupted, title,
):
    configure(hermes_home, monkeypatch)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    synthetic_secret = "987654:synthetic_test_value_only_not_real"
    hooks.on_session_end(
        session_id=title, turn_id=title, completed=False, failed=failed,
        interrupted=interrupted,
        turn_exit_reason=f"Useful {title.lower()} context {synthetic_secret} remains",
    )
    text = sender.send_message.call_args.args[1]
    assert f"Hermes · {title}" in text
    assert synthetic_secret not in text
    assert "Useful" in text and "remains" in text


def test_session_title_is_redacted_before_telegram_delivery(hermes_home, monkeypatch):
    configure(hermes_home, monkeypatch)
    sender = Mock()
    monkeypatch.setattr(hooks, "TelegramClient", lambda *a, **k: sender)
    synthetic_secret = "987654:synthetic_test_value_only_not_real"
    hooks.on_pre_llm_call(
        session_id="title-redaction", turn_id="title-redaction",
        session_title=f"Review {synthetic_secret} is ongoing",
    )
    text = sender.send_message.call_args.args[1]
    assert synthetic_secret not in text
    assert "Review" in text and "is ongoing" in text


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


def test_plugin_manager_hook_uses_secondary_profile_secret_scope(tmp_path, monkeypatch):
    """Load the real plugin and dispatch its real callback under Hermes' scoped runtime."""
    from agent import secret_scope
    import hermes_constants
    from hermes_cli.plugins import PluginManager

    default_token = "default-profile-sentinel"
    secondary_token = "secondary-profile-sentinel"
    secondary_chat = "secondary-profile-chat"
    default_home = tmp_path / ".hermes"
    secondary_home = default_home / "profiles" / "secondary"
    secondary_home.mkdir(parents=True)
    plugins_dir = secondary_home / "plugins"
    plugin_dir = plugins_dir / "telegram-notify"
    plugins_dir.mkdir(parents=True)
    shutil.copytree(
        ROOT, plugin_dir,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"),
    )
    (secondary_home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - telegram-notify\n", encoding="utf-8",
    )
    (secondary_home / ".env").write_text(
        f"TELEGRAM_BOT_TOKEN={secondary_token}\n"
        f"TELEGRAM_CHAT_ID={secondary_chat}\n"
        "HERMES_TELEGRAM_CHAT_ID=secondary-prefixed-chat\n",
        encoding="utf-8",
    )
    empty_bundled = tmp_path / "empty-bundled"
    empty_bundled.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(empty_bundled))
    monkeypatch.delenv("HERMES_ENABLE_PROJECT_PLUGINS", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", default_token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "default-profile-chat")
    monkeypatch.setenv("HERMES_TELEGRAM_CHAT_ID", "default-prefixed-chat")
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)

    delivered = []

    class StubTelegramClient:
        def __init__(self, token, timeout=4):
            delivered.append({"token": token})

        def send_message(self, chat_id, text):
            delivered[-1].update(chat_id=chat_id, text=text)

    home_token = hermes_constants.set_hermes_home_override(secondary_home)
    try:
        secondary_scope = secret_scope.build_profile_secret_scope(secondary_home)
        assert secondary_scope["TELEGRAM_BOT_TOKEN"] == secondary_token
        scope_token = secret_scope.set_secret_scope(
            secondary_scope, profile_home=str(secondary_home),
        )
        manager = PluginManager(scope_key=str(secondary_home))
        try:
            manager.discover_and_load()
            assert manager.has_hook("pre_llm_call")
            for callback in manager._hooks["pre_llm_call"]:
                monkeypatch.setitem(callback.__globals__, "TelegramClient", StubTelegramClient)
            manager.invoke_hook(
                "pre_llm_call", session_id="multiplex", turn_id="multiplex",
                cwd="/tmp/secondary-workspace", user_message="safe test message",
                is_first_turn=True, platform="cli",
            )
        finally:
            manager.unload()
            secret_scope.reset_secret_scope(scope_token)
    finally:
        hermes_constants.reset_hermes_home_override(home_token)

    assert len(delivered) == 1
    assert delivered[0]["token"] == secondary_token
    assert delivered[0]["token"] != default_token
    assert delivered[0]["chat_id"] == secondary_chat
    assert "Hermes · Started" in delivered[0]["text"]


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
