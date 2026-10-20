from __future__ import annotations

import logging
import sys
from collections import Counter
from datetime import date, datetime

from src.config import load_bot_token, load_min_gain_pct
from src.formatter import format_command_response
from src.models import IpoRecord, finalize_ipo_record
from src.recommend import recommend_ipos
from src.scraper import (
    IST,
    ScrapeError,
    fetch_html,
    normalize_name,
    parse_detail_html,
    scrape_market_data,
)
from src.telegram_client import TelegramError, get_updates, send_message, set_my_commands

logger = logging.getLogger(__name__)

_COMMAND_DESCRIPTIONS = (
    ("start", "Welcome and command list"),
    ("help", "Show available commands"),
    ("date", "Show today's date"),
    ("search", "Search IPOs by name"),
    ("live_sme", "Show live SME IPOs"),
    ("live_mainboard", "Show live Mainboard IPOs"),
    ("upcoming_sme", "Show upcoming SME IPOs"),
    ("upcoming_mainboard", "Show upcoming Mainboard IPOs"),
    ("ending_sme_today", "SME IPOs closing today"),
    ("ending_mainboard_today", "Mainboard IPOs closing today"),
    ("ending_all_today", "All IPOs closing today"),
    ("allotment_sme", "SME IPOs in allotment phase"),
    ("allotment_mainboard", "Mainboard IPOs in allotment phase"),
    ("allotment_awaited_sme", "SME IPOs awaiting allotment"),
    ("allotment_awaited_mainboard", "Mainboard IPOs awaiting allotment"),
    ("listed_sme", "Show listed SME IPOs"),
    ("listed_mainboard", "Show listed Mainboard IPOs"),
    ("details", "Get detailed IPO info"),
    ("mystats", "View your usage statistics"),
    ("highsignal", "Show HIGH SIGNAL IPOs"),
    ("today", "Show IPOs closing today"),
    ("upcoming", "Show upcoming IPOs"),
    ("gmp", "Show active IPO GMP data"),
)
_COMMANDS = {command for command, _ in _COMMAND_DESCRIPTIONS}
_HELP = (
    "<b>IPO bot commands</b>\n"
    "/start — welcome and command list\n"
    "/help — show available commands\n"
    "/date — current date\n"
    "/search &lt;IPO_NAME&gt; — search IPOs by name\n"
    "/details &lt;IPO_NAME&gt; — detailed IPO information\n"
    "/live_sme, /live_mainboard — live IPOs by type\n"
    "/upcoming_sme, /upcoming_mainboard — upcoming IPOs by type\n"
    "/ending_sme_today, /ending_mainboard_today, /ending_all_today — IPOs closing today\n"
    "/allotment_sme, /allotment_mainboard — IPOs in allotment phase\n"
    "/allotment_awaited_sme, /allotment_awaited_mainboard — awaiting allotment\n"
    "/listed_sme, /listed_mainboard — listed IPOs by type\n"
    "/mystats — usage statistics since listener started\n"
    "/highsignal — HIGH SIGNAL IPOs\n"
    "/today — IPOs closing today\n"
    "/upcoming — upcoming IPOs\n"
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
    usage: Counter[str] | None = None,
) -> list[str]:
    parsed = _parse_command(text)
    if parsed is None:
        return []
    command, query = parsed
    if command not in _COMMANDS:
        return [_HELP]
    if command in {"search", "details"} and not query:
        return [f"Usage: /{command} &lt;IPO_NAME&gt;"]

    now = now or datetime.now(IST)
    if command in {"start", "help"}:
        return [_HELP]
    if command == "date":
        return [f"Today's date: <b>{now.strftime('%d %B %Y').lstrip('0')}</b>"]
    if command == "mystats":
        counts = usage or Counter()
        total = sum(counts.values())
        if not total:
            return ["No commands used yet since the listener started."]
        popular = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:3]
        top_commands = "\n".join(f"/{name}: {count}" for name, count in popular)
        return [
            "<b>Your usage since the listener started</b>\n"
            f"Commands used: {total}\n{top_commands}"
        ]

    signals = recommend_ipos(records, min_gain_pct, now=now, include_all=True)
    if command in {"live_sme", "live_mainboard"}:
        ipo_type = "SME" if command.endswith("_sme") else "Mainboard"
        matches = [
            signal
            for signal in signals
            if signal.ipo.ipo_type == ipo_type and _is_open_today(signal.ipo, now.date())
        ]
        title = f"Live {ipo_type} IPOs"
    elif command in {"upcoming_sme", "upcoming_mainboard"}:
        ipo_type = "SME" if command.endswith("_sme") else "Mainboard"
        matches = [
            signal
            for signal in signals
            if signal.ipo.ipo_type == ipo_type and signal.ipo.status == "Upcoming"
        ]
        title = f"Upcoming {ipo_type} IPOs"
    elif command in {
        "ending_sme_today",
        "ending_mainboard_today",
        "ending_all_today",
        "today",
    }:
        ipo_type = (
            "SME" if command == "ending_sme_today"
            else "Mainboard" if command == "ending_mainboard_today"
            else None
        )
        matches = [
            signal
            for signal in signals
            if signal.ipo.close_date == now.date()
            and _is_open_today(signal.ipo, now.date())
            and (ipo_type is None or signal.ipo.ipo_type == ipo_type)
        ]
        title = f"{ipo_type + ' ' if ipo_type else ''}IPOs closing today"
    elif command.startswith("allotment_"):
        awaited = command.startswith("allotment_awaited_")
        ipo_type = "SME" if command.endswith("_sme") else "Mainboard"
        accepted_statuses = (
            {"allotment awaited", "awaiting allotment"}
            if awaited
            else {"allotment"}
        )
        matches = [
            signal
            for signal in signals
            if signal.ipo.ipo_type == ipo_type
            and signal.ipo.status.lower() in accepted_statuses
        ]
        if not matches:
            return [
                f"{ipo_type} allotment updates are not available in the current IPO data feed."
            ]
        title = f"{ipo_type} IPOs awaiting allotment" if awaited else f"{ipo_type} IPOs in allotment phase"
    elif command.startswith("listed_"):
        ipo_type = "SME" if command.endswith("_sme") else "Mainboard"
        matches = [
            signal
            for signal in signals
            if signal.ipo.ipo_type == ipo_type and signal.ipo.status.lower() == "listed"
        ]
        if not matches:
            return [
                f"Listed {ipo_type} IPOs are not available in the current IPO data feed."
            ]
        title = f"Listed {ipo_type} IPOs"
    elif command == "highsignal":
        matches = [signal for signal in signals if signal.label == "HIGH SIGNAL"]
        title = "HIGH SIGNAL IPOs"
    elif command == "upcoming":
        matches = [signal for signal in signals if signal.ipo.status == "Upcoming"]
        title = "Upcoming IPOs"
    elif command in {"search", "details"}:
        normalized_query = normalize_name(query)
        if not normalized_query:
            return [f"Usage: /{command} &lt;IPO_NAME&gt;"]
        matches = [
            signal
            for signal in signals
            if normalized_query in normalize_name(signal.ipo.name)
        ]
        title = f"{'Details' if command == 'details' else 'Search'}: {query}"
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
        details=command in {"search", "details"},
        gmp_only=command == "gmp",
    )


def _send_responses(bot_token: str, chat_id: str, responses: list[str]) -> None:
    for response in responses:
        send_message(bot_token, chat_id, response)


def run() -> int:
    try:
        bot_token = load_bot_token()
        min_gain_pct = load_min_gain_pct()
    except ValueError as exc:
        logger.error("%s", exc)
        return 1

    logger.info("Starting Telegram command listener")
    try:
        set_my_commands(
            bot_token,
            [
                {"command": command, "description": description}
                for command, description in _COMMAND_DESCRIPTIONS
            ],
        )
    except TelegramError as exc:
        logger.error("Could not register Telegram command menu: %s", exc)
        return 1

    offset: int | None = None
    usage_by_user: dict[str, Counter[str]] = {}
    try:
        while True:
            for update in get_updates(bot_token, offset=offset):
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
                sender = message.get("from")
                sender_id = sender.get("id") if isinstance(sender, dict) else None
                user_key = str(sender_id if isinstance(sender_id, (str, int)) else chat_id)
                usage = usage_by_user.setdefault(user_key, Counter())
                usage[command] += 1
                if command in {"search", "details"} and not query:
                    _send_responses(
                        bot_token,
                        str(chat_id),
                        [f"Usage: /{command} &lt;IPO_NAME&gt;"],
                    )
                    continue
                if command in {"start", "help", "date", "mystats"}:
                    _send_responses(
                        bot_token,
                        str(chat_id),
                        build_command_responses(
                            text,
                            [],
                            min_gain_pct,
                            usage=usage,
                        ),
                    )
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

                if command == "details":
                    normalized_query = normalize_name(query)
                    for record in records:
                        if (
                            normalized_query not in normalize_name(record.name)
                            or not record.url
                            or record.details is not None
                        ):
                            continue
                        try:
                            record.details = parse_detail_html(
                                fetch_html(record.url),
                                record.name,
                                datetime.now(IST).date(),
                            )
                            finalize_ipo_record(record)
                        except ScrapeError as exc:
                            logger.warning(
                                "Could not load verified details for %s: %s",
                                record.name,
                                exc,
                            )
                            record.warnings.append("verified detail metadata unavailable")

                responses = build_command_responses(
                    text,
                    records,
                    min_gain_pct,
                    usage=usage,
                )
                _send_responses(bot_token, str(chat_id), responses)
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
