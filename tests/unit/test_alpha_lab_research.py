"""Prospective entry timing, matched prices and conservative headline audit."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from webapp.alpha_lab.news import annotate
from webapp.alpha_lab.research import following_sessions, outcome
from webapp.ohlc import RegularBar

from uoa_detector.live_alpha.model import NewsItem
from uoa_detector.live_alpha.settings import load_live_alpha_settings

CAL = load_live_alpha_settings().calendar
NOW = datetime(2026, 10, 29, 21, tzinfo=UTC)
SIGNAL = date(2026, 10, 23)
ENTRY, EXIT = date(2026, 10, 26), date(2026, 10, 28)


def bars() -> dict[str, tuple[RegularBar, ...]]:
    return {
        t: (
            RegularBar(ENTRY, opening, opening, opening, opening),
            RegularBar(EXIT, end, end, end, end),
        )
        for t, opening, end in [("X", 100.0, 110.0), ("P", 200.0, 210.0), ("SPY", 500.0, 510.0)]
    }


def test_entry_is_next_session_open_and_exit_is_exact_matched_third_close() -> None:
    result = outcome(SIGNAL, "X", ["P"], "SPY", 3, bars(), NOW, CAL, 5)
    assert result.state == "priced" and result.entry_day == ENTRY and result.exit_day == EXIT
    assert result.stock_net_pct == pytest.approx((110 * 0.9995 / (100 * 1.0005) - 1) * 100)
    assert result.peer_excess_pp == pytest.approx(
        (110 * 0.9995 / (100 * 1.0005) - 210 * 0.9995 / (200 * 1.0005)) * 100
    )
    assert result.spy_excess_pp is not None


@pytest.mark.parametrize("missing", ["entry", "exit", "peer", "spy", "nan", "zero"])
def test_missing_required_price_never_borrows_another_day(missing: str) -> None:
    data = bars()
    if missing == "entry":
        data["X"] = data["X"][1:]
    elif missing == "exit":
        data["X"] = data["X"][:1]
    elif missing == "peer":
        data.pop("P")
    elif missing == "spy":
        data.pop("SPY")
    else:
        data["X"] = (
            replace(data["X"][0], open=float("nan") if missing == "nan" else 0),
            data["X"][1],
        )
    result = outcome(SIGNAL, "X", ["P"], "SPY", 3, data, NOW, CAL, 5)
    assert result.state == "unknown_prices"
    assert result.stock_net_pct is None and result.peer_excess_pp is None


def test_before_close_is_pending_and_unknown_calendar_is_not_a_weekday_guess() -> None:
    assert (
        outcome(SIGNAL, "X", ["P"], "SPY", 3, bars(), NOW.replace(day=28, hour=19), CAL, 5).state
        == "pending"
    )
    assert (
        outcome(date(2027, 12, 31), "X", ["P"], "SPY", 3, bars(), NOW, CAL, 5).state
        == "calendar_unknown"
    )


def test_holiday_week_and_early_close() -> None:
    signal = date(2026, 11, 25)
    target = date(2026, 11, 27)
    assert following_sessions(signal, 3, CAL) == (target, date(2026, 11, 30), date(2026, 12, 1))
    data = {t: (RegularBar(target, 100, 101, 99, 101),) for t in ("X", "P", "SPY")}
    before = datetime(2026, 11, 27, 17, 59, tzinfo=UTC)
    assert outcome(signal, "X", ["P"], "SPY", 1, data, before, CAL, 5).state == "pending"
    assert (
        outcome(signal, "X", ["P"], "SPY", 1, data, before + timedelta(minutes=2), CAL, 5).state
        == "priced"
    )


def item(headline: str, **over: object) -> NewsItem:
    values = dict(
        headline=headline,
        source="provider",
        provider_created_at=NOW - timedelta(minutes=5),
        first_seen_at=NOW - timedelta(minutes=4),
        sentiment="positive",
        is_major=True,
        tags=(),
        tickers=("X",),
        ref=headline,
    )
    values.update(over)
    return NewsItem(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("headline", "state"),
    [
        ("X raises guidance", "positive"),
        ("X cuts guidance", "negative"),
        ("X faces fraud investigation", "negative"),
        ("X does not raise guidance", "unclassified"),
        ("X may win a contract", "unclassified"),
        ("X reports quarterly results", "unclassified"),
        ("X raises guidance but faces investigation", "mixed"),
    ],
)
def test_news_is_not_positive_just_because_a_headline_exists(headline: str, state: str) -> None:
    assert annotate([item(headline)], "checked_found", NOW, 36).state == state


def test_news_syndication_conflicts_availability_and_retrieval_failure() -> None:
    positive = item("X raises guidance!")
    syndicated = item("X raises guidance.", source="other")
    negative = item("X cuts guidance")
    audit = annotate([positive, syndicated, negative], "checked_found", NOW, 36)
    assert audit.state == "mixed" and audit.positive == audit.negative == 1
    assert len(audit.evidence) == 2
    assert annotate([positive], "failed", NOW, 36).state == "failed"
    assert (
        annotate(
            [replace(positive, first_seen_at=NOW + timedelta(seconds=1))], "checked_found", NOW, 36
        ).state
        == "checked_none"
    )
    assert (
        annotate(
            [replace(positive, provider_created_at=NOW + timedelta(seconds=1))],
            "checked_found",
            NOW,
            36,
        ).state
        == "checked_none"
    )
    assert (
        annotate(
            [replace(positive, provider_created_at=NOW - timedelta(hours=37))],
            "checked_found",
            NOW,
            36,
        ).state
        == "checked_none"
    )


def test_news_context_orders_observations_and_refuses_unavailable_price_history() -> None:
    from webapp.alpha_lab.flow import ObservedAlert
    from webapp.alpha_lab.news import CapturedPrice, with_context
    from webapp.alpha_lab.settings import load_settings
    from webapp.live_alpha.inputs import SpotRead

    cfg = load_settings()
    headline = item("X raises guidance")
    audit = annotate([headline], "checked_found", NOW, 36)
    seen = headline.first_seen_at
    alert = ObservedAlert(
        "a",
        seen - timedelta(seconds=30),
        {
            "created_at": (seen - timedelta(seconds=60)).isoformat(),
            "option_chain": "X261120C00100000",
            "type": "call",
            "total_premium": "500000",
            "total_ask_side_prem": "500000",
            "total_bid_side_prem": "0",
            "has_multileg": False,
        },
    )
    quote = CapturedPrice(
        seen - timedelta(seconds=60), seen - timedelta(seconds=90), 100, seen.date().isoformat()
    )
    spot = SpotRead(102, NOW, seen.date())
    enriched = with_context(audit, [alert], [quote], spot, NOW, cfg).evidence[0]
    assert enriched.flow_order == "flow_seen_first"
    assert enriched.price_change_pct == pytest.approx(2)
    revision = replace(
        alert,
        observed_at=NOW,
        payload={**alert.payload, "total_ask_side_prem": "0", "total_bid_side_prem": "500000"},
    )
    assert (
        with_context(audit, [alert, revision], [quote], spot, NOW, cfg).evidence[0].flow_order
        == "not_observed"
    )
    future_revision = replace(revision, observed_at=NOW + timedelta(seconds=1))
    assert (
        with_context(audit, [alert, future_revision], [quote], spot, NOW, cfg)
        .evidence[0]
        .flow_order
        == "flow_seen_first"
    )
    late_quote = replace(quote, observed_at=seen + timedelta(seconds=1))
    assert (
        with_context(audit, [alert], [late_quote], spot, NOW, cfg).evidence[0].price_change_pct
        is None
    )
    old_quote = replace(quote, source_at=seen - timedelta(seconds=901))
    assert (
        with_context(audit, [alert], [old_quote], spot, NOW, cfg).evidence[0].price_change_pct
        is None
    )
    stale = replace(spot, fetched_at=seen - timedelta(seconds=1))
    assert (
        with_context(audit, [alert], [quote], stale, NOW, cfg).evidence[0].price_change_pct is None
    )
