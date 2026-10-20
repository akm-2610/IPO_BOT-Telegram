from __future__ import annotations

import time
from typing import Any

import httpx


class TelegramError(RuntimeError):
    """Raised when the Telegram Bot API request fails."""


def _api_call(
    bot_token: str,
    method: str,
    *,
    payload: dict[str, Any],
    timeout: float = 30.0,
) -> dict:
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    for attempt in range(3):
        try:
            response = httpx.post(url, json=payload, timeout=timeout)
            try:
                data = response.json()
            except ValueError as exc:
                raise TelegramError("Telegram returned invalid JSON") from exc
        except httpx.TransportError as exc:
            if attempt == 2:
                raise TelegramError("Telegram request failed after retries") from exc
            time.sleep(0.5 * (2**attempt))
            continue

        if response.status_code == 429 or response.status_code >= 500:
            if attempt == 2:
                raise TelegramError(f"Telegram transient HTTP error {response.status_code}")
            retry_after = data.get("parameters", {}).get("retry_after", 0.5 * (2**attempt))
            time.sleep(min(float(retry_after), 5.0))
            continue
        if not data.get("ok"):
            description = data.get("description", "unknown error")
            raise TelegramError(f"Telegram API error: {description}")
        return data
    raise TelegramError("Telegram request failed")


def send_message(
    bot_token: str,
    chat_id: str,
    text: str,
    timeout: float = 30.0,
) -> dict:
    """Post an escaped HTML message to a Telegram chat or channel."""
    return _api_call(
        bot_token,
        "sendMessage",
        payload={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=timeout,
    )


def get_updates(
    bot_token: str,
    *,
    offset: int | None = None,
    timeout: int = 25,
) -> list[dict[str, Any]]:
    """Long-poll Telegram for new message updates."""
    if not 0 <= timeout <= 50:
        raise ValueError("Telegram long-poll timeout must be between 0 and 50 seconds")
    payload: dict[str, Any] = {
        "timeout": timeout,
        "allowed_updates": ["message"],
    }
    if offset is not None:
        payload["offset"] = offset
    data = _api_call(
        bot_token,
        "getUpdates",
        payload=payload,
        timeout=timeout + 10,
    )
    updates = data.get("result")
    if not isinstance(updates, list) or not all(isinstance(item, dict) for item in updates):
        raise TelegramError("Telegram returned an invalid updates payload")
    return updates


def set_my_commands(bot_token: str, commands: list[dict[str, str]]) -> None:
    """Register the command menu shown in Telegram clients."""
    _api_call(
        bot_token,
        "setMyCommands",
        payload={"commands": commands},
    )
