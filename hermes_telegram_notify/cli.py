"""Management CLI for the telegram-notify plugin."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any

from . import config as config_mod
from . import formatting
from . import logging_utils
from .telegram import TelegramClient, TelegramError, discover_chat_ids


def setup_cli(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="telegram_notify_action")
    configure = sub.add_parser("configure", help="Save chat ID and optionally a bot token")
    configure.add_argument("--chat-id", help="Telegram chat ID")
    configure.add_argument("--token-env", help="Read the token from this environment variable")
    configure.add_argument("--disable", action="store_true", help="Disable notifications after configuring")
    configure.set_defaults(telegram_notify_action="configure")
    sub.add_parser("status", help="Show configuration and file status").set_defaults(telegram_notify_action="status")
    test = sub.add_parser("test", help="Send one test notification")
    test.add_argument("--message", default="Hermes · Test notification", help="Short test text")
    test.set_defaults(telegram_notify_action="test")
    sub.add_parser("discover-chat-id", help="List chat IDs visible to the bot").set_defaults(telegram_notify_action="discover-chat-id")
    logs = sub.add_parser("logs", help="Show recent plugin log lines")
    logs.add_argument("--tail", type=int, default=20)
    logs.set_defaults(telegram_notify_action="logs")


def _print_status() -> int:
    cfg = config_mod.load_config()
    print("plugin: telegram-notify")
    print(f"enabled: {cfg.enabled}")
    print(f"configured: {cfg.configured}")
    print(f"token_present: {bool(cfg.token)}")
    print(f"chat_id_present: {bool(cfg.chat_id)}")
    print(f"chat_id: {cfg.chat_id or '(unset)'}")
    for event in ("start", "completion", "approval", "approval_response"):
        print(f"notify_on_{event}: {cfg.event_enabled(event)}")
    print(f"config: {cfg.config_path}")
    print(f"credentials: {cfg.credential_path}")
    print(f"state: {cfg.state_path}")
    print(f"log: {cfg.log_path}")
    return 0


def _configure(args: argparse.Namespace) -> int:
    cfg = config_mod.load_config()
    chat_id = (args.chat_id or cfg.chat_id or "").strip()
    if not chat_id:
        chat_id = input("Telegram chat ID: ").strip()
    token = ""
    if args.token_env:
        token = config_mod.resolve_profile_environment_value(args.token_env)
        if not token:
            print(f"Environment variable {args.token_env} is empty", file=sys.stderr)
            return 2
    elif not cfg.token:
        token = getpass.getpass("Telegram bot token (hidden): ").strip()
    try:
        if chat_id:
            config_mod.save_config({"telegram_chat_id": chat_id, "enabled": not args.disable})
        elif args.disable:
            config_mod.save_config({"enabled": False})
        if token:
            config_mod.save_token(token)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print("telegram-notify configuration saved")
    return 0


def _test(args: argparse.Namespace) -> int:
    cfg = config_mod.load_config()
    if not cfg.configured:
        print("telegram-notify is not configured (token and chat ID are required)", file=sys.stderr)
        return 2
    try:
        message = formatting.safe_text(args.message, 3900)
        TelegramClient(cfg.token, int(cfg.values.get("telegram_timeout_seconds", 4))).send_message(cfg.chat_id, message)
    except TelegramError as exc:
        print(f"Telegram test failed: {exc}", file=sys.stderr)
        return 1
    print("Telegram test notification sent")
    return 0


def _discover() -> int:
    cfg = config_mod.load_config()
    if not cfg.token:
        print("Telegram bot token is not configured", file=sys.stderr)
        return 2
    try:
        for item in discover_chat_ids(TelegramClient(cfg.token, int(cfg.values.get("telegram_timeout_seconds", 4)))):
            print(json.dumps(item, ensure_ascii=False, sort_keys=True))
    except TelegramError as exc:
        print(f"Chat discovery failed: {exc}", file=sys.stderr)
        return 1
    return 0


def _logs(args: argparse.Namespace) -> int:
    cfg = config_mod.load_config()
    try:
        lines = cfg.log_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        print(f"No log yet: {cfg.log_path}")
        return 0
    for line in lines[-max(0, args.tail):]:
        print(line)
    return 0


def handle_cli(args: argparse.Namespace) -> int:
    action = getattr(args, "telegram_notify_action", None)
    if action == "configure":
        return _configure(args)
    if action == "status":
        return _print_status()
    if action == "test":
        return _test(args)
    if action == "discover-chat-id":
        return _discover()
    if action == "logs":
        return _logs(args)
    _print_status()
    return 0
