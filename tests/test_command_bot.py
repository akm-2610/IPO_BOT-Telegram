from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from src.command_bot import build_command_responses, run as run_command_bot
from src.config import Config
from src.digest import build_digest
from src.formatter import format_digest
from src.main import run
from src.models import IpoDetails, IpoRecord, SubscriptionData
from src.recommend import CONSIDER_SCORE, recommend_ipos, score_ipo
from src.scraper import IST
from src.telegram_client import TelegramError, get_updates

NOW = datetime(2026, 9, 11, 9, 0, tzinfo=IST)


def ipo(
    name: str,
    *,
    status: str = "Open",
    gain: float = 25,
    open_date: date = date(2026, 9, 9),
    close_date: date = date(2026, 9, 11),
) -> IpoRecord:
    return IpoRecord(
        name=name,
        ipo_type="Mainboard",
        status=status,
        source="IPO Watch",
        source_url="https://ipowatch.in/gmp/",
        gmp_rs=gain,
        gain_pct=gain,
        trend="rising",
        price_low=95,
        price_high=100,
        estimated_listing=100 + gain,
        open_date=open_date,
        close_date=close_date,
        updated_at=NOW - timedelta(hours=1),
        fetched_at=NOW,
        url="https://ipowatch.in/example/",
        alternate_gmp_rs=gain,
        subscription=SubscriptionData(
            qib=5,
            nii=10,
            retail=8,
            total=10,
            updated_at=NOW - timedelta(hours=1),
        ),
        details=IpoDetails(latest_pat=20, previous_pat=15),
    )


def test_high_signal_requires_every_threshold_and_overrides_consider() -> None:
    result = score_ipo(ipo("Strong IPO"), 10, now=NOW)

    assert result.score >= CONSIDER_SCORE
    assert result.label == "HIGH SIGNAL"
    assert "Today's IPO pick" in format_digest([result], 10, now=NOW)[0]
    assert "HIGH SIGNAL" in format_digest([result], 10, now=NOW)[0]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("close_date", date(2026, 9, 12)),
        ("gain_pct", 19.9),
        ("qib", 4.9),
        ("retail", 1.9),
        ("total", 9.9),
    ],
)
def test_high_signal_thresholds_are_inclusive_and_required(field: str, value) -> None:
    candidate = ipo("Near Threshold")
    if field in {"qib", "retail", "total"}:
        setattr(candidate.subscription, field, value)
    else:
        setattr(candidate, field, value)

    result = score_ipo(candidate, 10, now=NOW)

    assert result.label == "CONSIDER"


def test_high_signal_also_requires_the_existing_score_threshold() -> None:
    candidate = ipo("Low Score")
    candidate.trend = "falling"
    candidate.details = IpoDetails(latest_pat=-2, previous_pat=15)
    result = score_ipo(candidate, 10, now=NOW)

    assert result.score < CONSIDER_SCORE
    assert result.label != "HIGH SIGNAL"


def test_recommendation_full_scope_is_opt_in() -> None:
    closing_tomorrow = ipo(
        "Closes Tomorrow",
        close_date=date(2026, 9, 12),
    )
    upcoming = ipo(
        "Future IPO",
        status="Upcoming",
        open_date=date(2026, 9, 15),
        close_date=date(2026, 9, 17),
    )

    default = recommend_ipos([closing_tomorrow, upcoming], 10, now=NOW)
    all_results = recommend_ipos(
        [closing_tomorrow, upcoming],
        10,
        now=NOW,
        include_all=True,
    )

    assert [result.ipo.name for result in default] == ["Closes Tomorrow"]
    assert {result.ipo.name for result in all_results} == {
        "Closes Tomorrow",
        "Future IPO",
    }


def test_default_recommendation_keeps_all_high_signal_alerts() -> None:
    candidates = [ipo(f"High Signal {index}") for index in range(4)]

    results = recommend_ipos(candidates, 10, now=NOW)

    assert len(results) == 4
    assert all(result.label == "HIGH SIGNAL" for result in results)


def test_command_filters_return_requested_ipo_sets() -> None:
    highsignal = ipo("Strong IPO")
    upcoming = ipo(
        "Future IPO",
        status="Upcoming",
        open_date=date(2026, 9, 15),
        close_date=date(2026, 9, 17),
    )
    closed = ipo(
        "Closed IPO",
        status="Closed",
        open_date=date(2026, 9, 1),
        close_date=date(2026, 9, 10),
    )
    records = [highsignal, upcoming, closed]

    highsignal_text = "\n".join(
        build_command_responses("/highsignal", records, 10, now=NOW)
    )
    today_text = "\n".join(build_command_responses("/today", records, 10, now=NOW))
    upcoming_text = "\n".join(
        build_command_responses("/upcoming", records, 10, now=NOW)
    )
    search_text = "\n".join(
        build_command_responses("/search Strong", records, 10, now=NOW)
    )
    gmp_text = "\n".join(build_command_responses("/gmp", records, 10, now=NOW))

    assert "Strong IPO" in highsignal_text
    assert "Future IPO" not in highsignal_text
    assert "Strong IPO" in today_text
    assert "Future IPO" in upcoming_text
    assert "Strong IPO" in search_text and "Issue price" in search_text
    assert "GMP" in gmp_text and "Future IPO" in gmp_text
    assert "Closed IPO" not in gmp_text


def test_commands_escape_search_content_and_report_no_matches() -> None:
    unsafe = ipo("<script>IPO</script>")
    unsafe.url = "javascript:alert(1)"
    response = "\n".join(
        build_command_responses("/search script", [unsafe], 10, now=NOW)
    )
    missing = "\n".join(
        build_command_responses("/search missing", [unsafe], 10, now=NOW)
    )

    assert "<script>" not in response
    assert "&lt;script&gt;" in response
    assert "javascript:" not in response
    assert "No matching IPOs found." in missing
    assert build_command_responses("/search", [unsafe], 10, now=NOW) == [
        "Usage: /search &lt;IPO_NAME&gt;"
    ]


def test_command_bot_help_handles_bot_suffix_and_unknown_commands() -> None:
    assert build_command_responses("/today@ipo_bot", [], 10, now=NOW) == [
        "<b>IPOs closing today — 11 Sep 2026</b>\n\nNo matching IPOs found."
    ]
    assert "/highsignal" in build_command_responses("/unknown", [], 10, now=NOW)[0]


def test_digest_is_empty_when_no_consider_or_high_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watch = score_ipo(
        ipo("Watch IPO", gain=5),
        10,
        now=NOW,
    )
    monkeypatch.setattr("src.digest.scrape_market_data", lambda now: [])
    monkeypatch.setattr(
        "src.digest.recommend_ipos",
        lambda records, min_gain_pct, now: [watch],
    )

    assert build_digest(10, now=NOW) == []


def test_main_exits_silently_without_calling_telegram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.main.load_config", lambda: Config("token", "channel", 10))
    monkeypatch.setattr("src.main.build_digest", lambda min_gain_pct: [])
    monkeypatch.setattr(
        "src.main.send_message",
        lambda *args: pytest.fail("No message should be sent"),
    )

    assert run() == 0


def test_get_updates_uses_offset_and_validates_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"ok": True, "result": [{"update_id": 9}]}

    def fake_post(url, *, json, timeout):
        captured.update(url=url, payload=json, timeout=timeout)
        return Response()

    monkeypatch.setattr("src.telegram_client.httpx.post", fake_post)

    updates = get_updates("fake-token", offset=10, timeout=25)

    assert updates == [{"update_id": 9}]
    assert captured["payload"]["offset"] == 10
    assert captured["payload"]["allowed_updates"] == ["message"]
    assert captured["timeout"] == 35


def test_get_updates_rejects_invalid_result_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        status_code = 200

        @staticmethod
        def json() -> dict:
            return {"ok": True, "result": "invalid"}

    monkeypatch.setattr("src.telegram_client.httpx.post", lambda *args, **kwargs: Response())

    with pytest.raises(TelegramError, match="invalid updates payload"):
        get_updates("fake-token")


def test_command_listener_replies_to_the_requesting_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    poll_count = 0
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr("src.command_bot.load_bot_token", lambda: "fake-token")
    monkeypatch.setattr("src.command_bot.load_min_gain_pct", lambda: 10)
    monkeypatch.setattr("src.command_bot.scrape_market_data", lambda: [ipo("Strong IPO")])

    class FixedDateTime:
        @staticmethod
        def now(tz) -> datetime:
            return NOW

    monkeypatch.setattr("src.command_bot.datetime", FixedDateTime)

    def poll(token: str, *, offset: int | None):
        nonlocal poll_count
        poll_count += 1
        if poll_count > 1:
            raise TelegramError("stop test polling")
        return [
            {
                "update_id": 4,
                "message": {
                    "text": "/today",
                    "chat": {"id": 12345},
                },
            }
        ]

    monkeypatch.setattr("src.command_bot.get_updates", poll)
    monkeypatch.setattr(
        "src.command_bot.send_message",
        lambda token, chat_id, text: sent.append((chat_id, text)),
    )

    assert run_command_bot() == 1
    assert len(sent) == 1
    assert sent[0][0] == "12345"
    assert "Strong IPO" in sent[0][1]
