from __future__ import annotations

import logging
import sys
from datetime import date, datetime

from src.config import load_bot_token, load_min_gain_pct
from src.formatter import format_command_response
from src.models import IpoRecord
from src.recommend import recommend_ipos
from src.scraper import IST, ScrapeError, normalize_name, scrape_market_data
from src.telegram_client import TelegramError, get_updates, send_message

logger = logging.getLogger(__name__)

_COMMANDS = {"highsignal", "today", "upcoming", "search", "gmp"}
_HELP = (
    "<b>IPO bot commands</b>\n"
    "/highsignal — HIGH SIGNAL IPOs\n"
    "/today — IPOs closing today\n"
    "/upcoming — upcoming IPOs\n"
    "/search &lt;IPO_NAME&gt; — search IPO details\n"
    "/gmp — active IPO GMP data"
)


def _parse_command(text: str) -> tuple[str, str] | None:
    parts = text.strip().split(maxsplit=1)
    if not parts or not parts[0].startswith("/"):
        return None
    command = parts[0][1:].split("@", maxsplit=1)[0].lower()
    return command, parts[1].strip() if len(parts) == 2 else ""


def _is_open_today(record: IpoRecord, today: date) -> bool:
    return (
        record.status == "Open"
        and record.open_date is not None
        and record.close_date is not None
        and record.open_date <= today <= record.close_date
        and "status/date mismatch" not in record.warnings
    )


def build_command_responses(
    text: str,
    records: list[IpoRecord],
    min_gain_pct: float,
    *,
    now: datetime | None = None,
) -> list[str]:
    parsed = _parse_command(text)
    if parsed is None:
        return []
    command, query = parsed
    if command not in _COMMANDS:
        return [_HELP]
    if command == "search" and not query:
        return ["Usage: /search &lt;IPO_NAME&gt;"]

    now = now or datetime.now(IST)
    signals = recommend_ipos(records, min_gain_pct, now=now, include_all=True)
    if command == "highsignal":
        matches = [signal for signal in signals if signal.label == "HIGH SIGNAL"]
        title = "HIGH SIGNAL IPOs"
    elif command == "today":
        matches = [
            signal
            for signal in signals
            if signal.ipo.close_date == now.date() and _is_open_today(signal.ipo, now.date())
        ]
        title = "IPOs closing today"
    elif command == "upcoming":
        matches = [signal for signal in signals if signal.ipo.status == "Upcoming"]
        title = "Upcoming IPOs"
    elif command == "search":
        normalized_query = normalize_name(query)
        if not normalized_query:
            return ["Usage: /search &lt;IPO_NAME&gt;"]
        matches = [
            signal
            for signal in signals
            if normalized_query in normalize_name(signal.ipo.name)
        ]
        title = f"Search: {query}"
    else:
        matches = [
            signal
            for signal in signals
            if signal.ipo.status in {"Open", "Upcoming"}
        ]
        title = "Active IPO GMP"

    return format_command_response(
        title,
        matches,
        now=now,
        details=command == "search",
        gmp_only=command == "gmp",
    )


def _send_responses(bot_token: str, chat_id: str, responses: list[str]) -> None:
    logger.info("Chat ID: %s", chat_id)
    for response in responses:
        result = send_message(bot_token, chat_id, response)
        logger.info("Telegram result: %s", result)


def run() -> int:
    try:
        bot_token = load_bot_token()
        min_gain_pct = load_min_gain_pct()
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    logger.info("Starting Telegram command listener")
    offset: int | None = None
    try:
        while True:
            for update in get_updates(bot_token, offset=offset):
                logger.info("Received update: %s", update)
                update_id = update.get("update_id")
                if not isinstance(update_id, int):
                    raise TelegramError("Telegram update is missing a valid update_id")
                offset = max(offset or 0, update_id + 1)

                message = update.get("message")
                if not isinstance(message, dict):
                    continue
                text = message.get("text")
                chat = message.get("chat")
                if not isinstance(text, str) or not isinstance(chat, dict):
                    continue
                chat_id = chat.get("id")
                if not isinstance(chat_id, (str, int)):
                    raise TelegramError("Telegram message is missing a valid chat id")

                parsed = _parse_command(text)
                if parsed is None:
                    continue
                command, query = parsed
                if command not in _COMMANDS:
                    _send_responses(bot_token, str(chat_id), [_HELP])
                    continue
                if command == "search" and not query:
                    _send_responses(bot_token, str(chat_id), ["Usage: /search &lt;IPO_NAME&gt;"])
                    continue

                try:
                    records = scrape_market_data()
                except ScrapeError as exc:
                    logger.error("Could not load market data for Telegram command: %s", exc)
                    _send_responses(
                        bot_token,
                        str(chat_id),
                        ["IPO market data is temporarily unavailable. Please try again later."],
                    )
                    continue

                responses = build_command_responses(text, records, min_gain_pct)
                logger.info("Responses: %s", responses)
                _send_responses(bot_token, str(chat_id), responses)
                logger.info("Sent successfully")
    except TelegramError as exc:
        logger.error("Telegram command listener failed: %s", exc)
        return 1
    return 0


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    sys.exit(run())


if __name__ == "__main__":
    main()
