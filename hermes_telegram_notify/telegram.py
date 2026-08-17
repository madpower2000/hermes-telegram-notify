"""Minimal standard-library Telegram Bot API client."""

from __future__ import annotations

import json
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class TelegramError(RuntimeError):
    """A sanitized Telegram transport/API error."""


class TelegramClient:
    def __init__(self, token: str, timeout: int = 4, opener: Callable[..., Any] | None = None):
        if not token or any(ch.isspace() for ch in token):
            raise TelegramError("Telegram bot token is missing or malformed")
        self._token = token
        self._timeout = max(1, int(timeout))
        self._opener = opener or urlopen

    def _request(self, method: str, payload: dict[str, Any]) -> Any:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "hermes-telegram-notify/1.0"}, method="POST")
        try:
            with self._opener(request, timeout=self._timeout) as response:
                raw = response.read(1_000_000)
        except HTTPError as exc:
            raise TelegramError(f"Telegram HTTP error {exc.code}") from None
        except (URLError, TimeoutError, OSError):
            raise TelegramError("Telegram network request failed") from None
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise TelegramError("Telegram returned malformed JSON") from None
        if not isinstance(decoded, dict) or decoded.get("ok") is not True:
            description = decoded.get("description") if isinstance(decoded, dict) else ""
            # Deliberately omit server descriptions: they may echo request data.
            if description and "rate" in str(description).lower():
                raise TelegramError("Telegram request was rate limited")
            raise TelegramError("Telegram API rejected the request")
        return decoded.get("result")

    def send_message(self, chat_id: str, text: str) -> Any:
        if not chat_id:
            raise TelegramError("Telegram chat ID is missing")
        if not text:
            raise TelegramError("Telegram message is empty")
        return self._request("sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": True})

    def get_updates(self, offset: int | None = None, timeout: int = 1) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": max(0, min(5, int(timeout)))}
        if offset is not None:
            payload["offset"] = int(offset)
        result = self._request("getUpdates", payload)
        return result if isinstance(result, list) else []


def discover_chat_ids(client: TelegramClient) -> list[dict[str, str]]:
    """Return safe chat metadata from recent updates, without raw payloads."""
    found: dict[str, dict[str, str]] = {}
    for update in client.get_updates():
        message = update.get("message") if isinstance(update, dict) else None
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        if not isinstance(chat, dict) or chat.get("id") is None:
            continue
        chat_id = str(chat["id"])
        label = str(chat.get("title") or chat.get("username") or chat.get("first_name") or "")[:120]
        found[chat_id] = {"chat_id": chat_id, "label": label}
    return list(found.values())
