from __future__ import annotations

from datetime import date, datetime
from html import escape

from src.models import IpoRecord, SignalResult
from src.scraper import CLOSE_WITHIN_DAYS, IST

_WHY_COPY = (
    ("useful listing-gain band", "Grey-market premium is in a useful range"),
    ("modest premium", "Grey-market premium is modest"),
    ("cheaper than listed peers", "Priced cheaper than listed peers"),
    ("QIB demand", "Institutions are bidding"),
    ("institutional demand supports", "Institutions are supporting demand"),
    ("mainboard liquidity", "Mainboard — easier to sell after listing"),
    ("fresh IPO Watch", "Figures were updated recently"),
    ("GMP trend is rising", "Grey-market premium is rising"),
    ("annual PAT is growing", "Profit has been growing"),
    ("revenue is growing", "Sales have been growing"),
    ("PAT margin is healthy", "Profit margin looks healthy"),
    ("primarily fresh capital", "Company is raising new money"),
    ("growth capex", "Money is going into the business"),
    ("closes today", "Last day to apply"),
    ("closes in", "Closing soon"),
    ("alternate source broadly agrees", "Two grey-market sources roughly agree"),
)

_CAUTION_COPY = (
    ("weak QIB", "Demand is mostly retail or HNI, not institutions"),
    ("institutional demand is weak", "Institutions have barely subscribed"),
    ("undersubscribed", "Not fully subscribed"),
    ("subscription is still thin", "Subscription is still thin"),
    ("allotment is a lottery", "Very oversubscribed — allotment is a lottery"),
    ("speculative", "Grey-market premium looks stretched"),
    ("extreme SME GMP", "SME grey-market premium looks stretched"),
    ("often listed below", "Similar premiums recently listed below grey-market"),
    ("expensive versus listed peers", "Looks expensive versus listed peers"),
    ("priced above listed peers", "Priced above listed peers"),
    ("high versus book value", "Price is high versus book value"),
    ("mainly to repay debt", "Most of the issue is to repay debt"),
    ("land acquisition", "Proceeds include buying land"),
    ("mostly OFS", "Mostly existing investors selling"),
    ("mix of fresh issue and OFS", "Part new money, part existing holders selling"),
    ("GMP trend is falling", "Grey-market premium is falling"),
    ("non-positive", "Latest year was not profitable"),
    ("fell more than 20%", "Profit dropped versus last year"),
    ("jumped too sharply", "Profit jumped too sharply to trust"),
    ("PAT margin is thin", "Profit margin is thin"),
    ("unusually high", "Reported ROE looks unusually high"),
    ("stale or undated", "Grey-market data may be old"),
    ("SME lot is expensive", "SME minimum investment is high"),
    ("may be illiquid", "Small SME — harder to sell after listing"),
    ("sources differ", "Grey-market sites disagree"),
    ("rich versus", "Premium looks expensive versus a typical listing gain"),
    ("no row-level update", "Backup source has no timestamp"),
    ("subscription data unavailable", "Live subscription numbers are missing"),
    ("detail metadata unavailable", "Company detail page could not be verified"),
    ("less than 50%", "Promoters will own less than half after listing"),
    ("drops sharply", "Promoter holding drops sharply"),
    ("leveraged", "Company has meaningful debt"),
)


def _format_day(day: date) -> str:
    try:
        return day.strftime("%-d %b %Y")
    except ValueError:
        return day.strftime("%d %b %Y").lstrip("0")


def _money(value: float | None) -> str:
    if value is None:
        return "—"
    numeric = float(value)
    if numeric.is_integer():
        return f"₹{numeric:,.0f}"
    return f"₹{numeric:,.2f}".rstrip("0").rstrip(".")


def _date(value: date | None) -> str:
    return _format_day(value) if value else "—"


def _name(record: IpoRecord) -> str:
    display = record.name if len(record.name) <= 180 else record.name[:177].rstrip() + "…"
    name = escape(display)
    if record.url and len(record.url) <= 500 and record.url.startswith(("https://", "http://")):
        return f'<a href="{escape(record.url, quote=True)}">{name}</a>'
    return name


def _verdict(label: str) -> str:
    if label == "HIGH SIGNAL":
        return "🚨 HIGH SIGNAL"
    if label == "CONSIDER":
        return "Apply"
    if label == "WATCH":
        return "Wait"
    return "Skip"


def _times(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:g}x"


def _closes_line(record: IpoRecord, today: date) -> str:
    if record.close_date is None:
        return f"Closes {_date(record.close_date)}"
    remaining = (record.close_date - today).days
    if remaining == 0:
        return "Closes <b>today</b>"
    if remaining == 1:
        return "Closes <b>tomorrow</b>"
    return f"Closes {_date(record.close_date)}"


def _humanize(items: list[str], mapping: tuple[tuple[str, str], ...], limit: int) -> list[str]:
    seen: list[str] = []
    for item in items:
        for needle, copy in mapping:
            if needle.lower() in item.lower() and copy not in seen:
                seen.append(copy)
                break
        if len(seen) >= limit:
            break
    return seen


def _trend_word(trend: str) -> str:
    if trend == "rising":
        return "rising"
    if trend == "falling":
        return "falling"
    if trend == "stable":
        return "flat"
    return "unknown"


def _issue_mix_line(mix: str | None) -> str | None:
    if mix == "fresh":
        return "Company raising new money"
    if mix == "ofs":
        return "Mostly existing investors selling"
    if mix == "mixed":
        return "New money + existing holders selling"
    return None


def _value_line(ipo: IpoRecord) -> str | None:
    details = ipo.details
    if details is None or details.implied_pe is None or details.peer_median_pe is None:
        return None
    if details.implied_pe < details.peer_median_pe * 0.9:
        vs = "cheaper"
    elif details.implied_pe > details.peer_median_pe * 1.2:
        vs = "expensive"
    else:
        vs = "similar"
    return escape(
        f"Value: P/E ~{details.implied_pe:.0f} vs listed peers ~{details.peer_median_pe:.0f} ({vs})"
    )


def _proceeds_line(details) -> str | None:
    if details.objects_debt_share is not None and details.objects_debt_share >= 0.5:
        return "Proceeds mainly to repay debt"
    if "land" in details.objects_flags:
        return "Proceeds include buying land"
    if "capex" in details.objects_flags:
        return "Proceeds include business expansion"
    return None


def _promoter_line(details) -> str | None:
    if details.promoter_post_pct is None:
        return None
    owned = f"Promoters will own {details.promoter_post_pct:.0f}% after listing"
    if details.promoter_pre_pct is not None:
        return f"{owned} (from {details.promoter_pre_pct:.0f}%)"
    return owned


def _full_signal(result: SignalResult, today: date, index: int | None = None) -> str:
    ipo = result.ipo
    title = "Today's pick" if index == 1 else (f"Also #{index}" if index else "IPO")
    lines = [
        f"<b>{title}: {_name(ipo)}</b>",
        (
            f"Verdict: <b>{escape(_verdict(result.label))}</b>"
            f" · {escape(ipo.ipo_type)}"
            f" · Strength {result.score:.0f}/100"
        ),
        _closes_line(ipo, today),
        "",
        (
            f"Grey market: <b>{_money(ipo.gmp_rs)} ({(ipo.gain_pct or 0):.0f}%)</b>"
            f" · {escape(_trend_word(ipo.trend))}"
        ),
        (
            f"Issue price {_money(ipo.price_high)}"
            f" → if listed near GMP {_money(ipo.estimated_listing)}"
        ),
    ]
    if ipo.details and ipo.details.min_application is not None:
        lines.append(f"Min. to apply: <b>{_money(ipo.details.min_application)}</b>")
    if ipo.subscription:
        sub = ipo.subscription
        lines.append(
            escape(
                f"Demand: institutions {_times(sub.qib)} · "
                f"HNI {_times(sub.nii)} · "
                f"retail {_times(sub.retail)} · overall {_times(sub.total)}"
            )
        )
    value_line = _value_line(ipo)
    if value_line:
        lines.append(value_line)
    if ipo.details:
        extras: list[str] = []
        if ipo.details.issue_size:
            extras.append(f"Size {escape(ipo.details.issue_size)}")
        mix = _issue_mix_line(ipo.details.issue_mix)
        if mix:
            extras.append(escape(mix))
        if extras:
            lines.append(" · ".join(extras))
        proceeds = _proceeds_line(ipo.details)
        if proceeds:
            lines.append(escape(proceeds))
        promoters = _promoter_line(ipo.details)
        if promoters:
            lines.append(escape(promoters))
    lines.append(
        f"Open {_date(ipo.open_date)} → Close {_date(ipo.close_date)}"
        + (f" · Lists {_date(ipo.details.listing_date)}" if ipo.details and ipo.details.listing_date else "")
    )

    why = _humanize(result.reasons, _WHY_COPY, 3)
    if why:
        lines.append("")
        lines.append("<b>Why</b>")
        lines.extend(f"• {escape(item)}" for item in why)
    cautions = _humanize(result.risk_flags, _CAUTION_COPY, 3)
    if cautions:
        lines.append("<b>Watch out</b>")
        lines.extend(f"• {escape(item)}" for item in cautions)
    return "\n".join(lines)


def format_digest(
    signals: list[SignalResult],
    min_gain_pct: float,
    now: datetime | None = None,
) -> list[str]:
    now = now or datetime.now(IST)
    today = now.date()
    actionable = any(
        signal.label in {"CONSIDER", "HIGH SIGNAL"} for signal in signals
    )
    title = "Today's IPO pick" if actionable else "No clear IPO to apply today"
    header = f"<b>{title} — {_format_day(today)}</b>"
    if actionable:
        header += f"\nOnly issues closing within {CLOSE_WITHIN_DAYS} days are listed."
    disclaimer = (
        "\n\n<i>Grey-market premium is unofficial and not a guaranteed listing gain. "
        "This is not investment advice. Check the RHP before applying.</i>"
    )

    if not signals:
        return [
            f"{header}\n\nNothing open and closing within {CLOSE_WITHIN_DAYS} days "
            "looks worth applying today."
            f"\nMinimum grey-market gain used: {min_gain_pct:g}%{disclaimer}"
        ]

    sources = sorted({result.ipo.source for result in signals})
    footer = f"\nData: {escape(', '.join(sources))}"
    limit = 3800
    messages: list[str] = []
    current = [header]
    for index, result in enumerate(signals, start=1):
        block = _full_signal(result, today, index=index)
        candidate = "\n\n".join(current + [block])
        if len(candidate) > limit and len(current) > 1:
            messages.append("\n\n".join(current))
            current = [f"<b>More IPOs — {_format_day(today)}</b>", block]
        else:
            current.append(block)

    last = "\n\n".join(current) + footer + disclaimer
    if len(last) > limit and len(current) > 2:
        messages.append("\n\n".join(current[:-1]))
        last = (
            f"<b>More IPOs — {_format_day(today)}</b>\n\n"
            f"{current[-1]}{footer}{disclaimer}"
        )
    messages.append(last)
    return messages


def format_command_response(
    title: str,
    signals: list[SignalResult],
    now: datetime | None = None,
    *,
    details: bool = False,
    gmp_only: bool = False,
) -> list[str]:
    now = now or datetime.now(IST)
    display_title = title if len(title) <= 180 else title[:177].rstrip() + "…"
    header = f"<b>{escape(display_title)} — {_format_day(now.date())}</b>"
    if not signals:
        return [f"{header}\n\nNo matching IPOs found."]

    messages: list[str] = []
    current = [header]
    for index, result in enumerate(signals, start=1):
        ipo = result.ipo
        if gmp_only:
            block = "\n".join(
                (
                    f"<b>{_name(ipo)}</b> · {escape(ipo.ipo_type)} · {escape(ipo.status)}",
                    (
                        f"GMP {_money(ipo.gmp_rs)} "
                        f"({(ipo.gain_pct or 0):.1f}%) · {_trend_word(ipo.trend)}"
                    ),
                    f"Open {_date(ipo.open_date)} → Close {_date(ipo.close_date)}",
                )
            )
            if ipo.subscription:
                block += (
                    "\nDemand: "
                    f"QIB {_times(ipo.subscription.qib)} · "
                    f"retail {_times(ipo.subscription.retail)} · "
                    f"overall {_times(ipo.subscription.total)}"
                )
        else:
            block = "\n".join(
                (
                    f"<b>{index}. {_name(ipo)}</b> · {escape(ipo.ipo_type)}",
                    (
                        f"Status: {escape(ipo.status)} · "
                        f"Open {_date(ipo.open_date)} → Close {_date(ipo.close_date)}"
                    ),
                    (
                        f"GMP {_money(ipo.gmp_rs)} "
                        f"({(ipo.gain_pct or 0):.1f}%) · "
                        f"Strength {result.score:.0f}/100 · "
                        f"{escape(_verdict(result.label))}"
                    ),
                )
            )
            if ipo.price_high is not None:
                block += (
                    f"\nIssue price {_money(ipo.price_high)}"
                    f" → if listed near GMP {_money(ipo.estimated_listing)}"
                )
            if ipo.subscription:
                block += (
                    "\nDemand: "
                    f"QIB {_times(ipo.subscription.qib)} · "
                    f"HNI {_times(ipo.subscription.nii)} · "
                    f"retail {_times(ipo.subscription.retail)} · "
                    f"overall {_times(ipo.subscription.total)}"
                )
            if details and ipo.details:
                detail_lines: list[str] = []
                if ipo.details.min_application is not None:
                    detail_lines.append(f"Minimum application {_money(ipo.details.min_application)}")
                if ipo.details.issue_size:
                    detail_lines.append(f"Issue size {escape(ipo.details.issue_size[:200])}")
                if ipo.details.listing_venue:
                    detail_lines.append(f"Listing venue {escape(ipo.details.listing_venue[:120])}")
                if ipo.details.listing_date:
                    detail_lines.append(f"Listing date {_date(ipo.details.listing_date)}")
                valuation = _value_line(ipo)
                if valuation:
                    detail_lines.append(valuation)
                if detail_lines:
                    block += "\n" + "\n".join(detail_lines)
            if (
                ipo.url
                and len(ipo.url) <= 500
                and ipo.url.startswith(("https://", "http://"))
            ):
                block += f'\n<a href="{escape(ipo.url, quote=True)}">Source details</a>'

        candidate = "\n\n".join(current + [block])
        if len(candidate) > 3800 and len(current) > 1:
            messages.append("\n\n".join(current))
            current = [f"<b>{escape(display_title)} — more</b>", block]
        else:
            current.append(block)

    message = "\n\n".join(current)
    if len(message) > 4096:
        raise ValueError("A command response exceeds Telegram's message limit")
    messages.append(message)
    return messages
