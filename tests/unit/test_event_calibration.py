"""Synthetic inputs test software only; no synthetic result is market evidence."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event
from webapp.event_calibration.archive import export
from webapp.event_calibration.features import build
from webapp.event_calibration.instruments import compare
from webapp.event_calibration.model import (
    Alert,
    Context,
    Dataset,
    Decision,
    Leg,
    Poll,
    Price,
    Structure,
    load,
)
from webapp.event_calibration.probe import probe
from webapp.event_calibration.replay import Candidate, candidates, replay
from webapp.event_calibration.research import run

from uoa_detector.live_alpha.calendar import is_session_day
from uoa_detector.live_alpha.settings import load_live_alpha_settings

ET = ZoneInfo("America/New_York")


def fixture(days: int = 15) -> Dataset:
    cal = load_live_alpha_settings().calendar
    sessions = []
    day = date(2026, 2, 2)
    while len(sessions) < days:
        if is_session_day(day, cal):
            sessions.append(day)
        day += timedelta(days=1)
    prices, alerts, polls, decisions = [], [], [], []
    for n, day in enumerate(sessions):
        at = (
            datetime.combine(day, datetime.min.time(), ET)
            .replace(hour=9, minute=45, second=30)
            .astimezone(UTC)
        )
        for ticker in ("AAA", "BBB", "CCC"):
            decisions.append(
                Decision(
                    key=f"{day}-{ticker}",
                    ticker=ticker,
                    at=at,
                    baseline=True,
                    risk_fraction=0.03,
                    news=True,
                )
            )
            for offset in range(0, 900, 120):
                polls.append(
                    Poll(
                        ticker=ticker,
                        at=at.replace(second=0) - timedelta(minutes=15) + timedelta(seconds=offset),
                        saturated=False,
                    )
                )
            premium = 500000 * 1.2**n
            alerts.append(
                Alert(
                    ticker=ticker,
                    key=f"a-{day}-{ticker}",
                    known_at=at - timedelta(seconds=30),
                    payload={
                        "created_at": (at - timedelta(seconds=31)).isoformat(),
                        "option_chain": f"{ticker}261218C00100000",
                        "type": "call",
                        "total_premium": premium,
                        "total_ask_side_prem": premium,
                        "total_bid_side_prem": 0,
                        "has_multileg": False,
                    },
                )
            )
        points = {at.replace(second=0) - timedelta(minutes=5): 99.0, at.replace(second=0): 100.0}
        for entry_minute in (47, 51):
            entry = at.replace(minute=entry_minute, second=0)
            points[entry] = 100.0
            for hold in (15, 60, 180):
                points[entry + timedelta(minutes=hold)] = 102.0
        for when, value in points.items():
            for ticker in ("AAA", "BBB", "CCC", "SPY"):
                v = value if ticker != "SPY" else 100.0 + (value - 100.0) / 2
                prices.append(
                    Price(ticker=ticker, at=when, known_at=when + timedelta(seconds=1), value=v)
                )
    return Dataset(
        origin="SYNTHETIC_SOFTWARE_TEST_ONLY",
        asof=max(p.known_at for p in prices) + timedelta(days=1),
        sessions=tuple(sessions),
        decisions=tuple(decisions),
        prices=tuple(prices),
        alerts=tuple(alerts),
        polls=tuple(polls),
    )


def test_frozen_grid_and_real_data_blocking():
    assert len(candidates()) == len({c.key for c in candidates()}) == 438
    result = run(fixture(5))
    assert result["status"] == "DATA_BLOCKED"
    assert result["executed_replays"] == 0 and result["winner"] is None
    assert result["validated_alpha"] is False


def test_normalization_is_same_bucket_strictly_previous_sessions():
    data = fixture()
    samples = build(data)
    assert all(s.ratio is None for s in samples[:30])
    first_ready = next(s for s in samples if s.ratio is not None)
    assert first_ready.history == 10
    assert first_ready.percentile == 1
    earlier = data.model_copy(
        update={
            "decisions": tuple(d for d in data.decisions if d.at <= first_ready.decision.at),
            "alerts": tuple(a for a in data.alerts if a.known_at <= first_ready.decision.at),
            "polls": tuple(p for p in data.polls if p.at <= first_ready.decision.at),
            "prices": tuple(p for p in data.prices if p.known_at <= first_ready.decision.at),
        }
    )
    assert build(earlier) == tuple(s for s in samples if s.decision.at <= first_ready.decision.at)


@pytest.mark.parametrize(
    "fault", ["gap", "saturated", "future_receipt", "unknown_side", "multileg"]
)
def test_bad_flow_is_unknown_and_not_a_normalization_zero(fault):
    data = fixture(2)
    at = data.decisions[-1].at
    ticker = "CCC"
    if fault == "gap":
        data = data.model_copy(
            update={
                "polls": tuple(
                    p
                    for p in data.polls
                    if not (
                        p.ticker == ticker
                        and at - timedelta(minutes=8) < p.at < at - timedelta(minutes=3)
                    )
                )
            }
        )
    elif fault == "saturated":
        data = data.model_copy(
            update={
                "polls": tuple(
                    p.model_copy(update={"saturated": True}) if p.ticker == ticker else p
                    for p in data.polls
                )
            }
        )
    else:
        alerts = []
        for a in data.alerts:
            if a.ticker == ticker and a.known_at.date() == at.date():
                a = (
                    a.model_copy(update={"known_at": at + timedelta(seconds=1)})
                    if fault == "future_receipt"
                    else a.model_copy(
                        update={
                            "payload": a.payload
                            | (
                                {"has_multileg": True}
                                if fault == "multileg"
                                else {"total_ask_side_prem": 0}
                            )
                        }
                    )
                )
            alerts.append(a)
        data = data.model_copy(update={"alerts": tuple(alerts)})
    sample = next(s for s in build(data) if s.decision.ticker == ticker and s.day == at.date())
    if fault == "future_receipt":
        assert sample.up == 0  # genuinely complete observed window with no received alert
    else:
        assert sample.up is None


def test_later_alert_revision_does_not_repaint_an_earlier_sample():
    data = fixture(1)
    original = data.alerts[0]
    revised = original.model_copy(
        update={
            "known_at": data.decisions[0].at + timedelta(minutes=1),
            "payload": original.payload | {"total_premium": 99999999},
        }
    )
    assert build(data) == build(data.model_copy(update={"alerts": (*data.alerts, revised)}))


def test_unknown_context_cannot_pass_its_ablation():
    sample = build(fixture())[33]
    from webapp.event_calibration.replay import qualifies

    for arm in ("normalized_sector", "normalized_gamma"):
        assert not qualifies(sample, Candidate(arm, 1.5, 0.65, 0.8, 1, 60))
    assert qualifies(sample, Candidate("normalized_price", 1.5, 0.65, 0.8, 1, 60))


def test_independent_reference_arithmetic_and_exact_spy_dates():
    data = fixture(1)
    c = Candidate("baseline", 0, 0, 0, 1, 60)
    result = replay(data, build(data), c, data.sessions, 5)
    assert result.selected == 3 and result.unresolved == 0
    for u in result.units:
        assert u.entry_at > u.decision_at + timedelta(minutes=1)
        assert u.net_pct == pytest.approx((102 * 0.9995 / (100 * 1.0005) - 1) * 100)
        assert u.spy_pct == pytest.approx((101 * 0.9995 / (100 * 1.0005) - 1) * 100)
        assert u.excess_pp == pytest.approx(u.net_pct - u.spy_pct)
        assert u.risk_r == pytest.approx(u.net_pct / 3)
    removed = data.model_copy(
        update={
            "prices": tuple(
                p
                for p in data.prices
                if not (p.ticker == "SPY" and p.at == result.units[0].exit_at)
            )
        }
    )
    pending = replay(removed, build(removed), c, removed.sessions, 5)
    assert pending.selected == 3 and pending.unresolved == 3 and not pending.units


def test_missing_first_selected_unit_is_not_replaced_with_a_later_signal():
    data = fixture(1)
    original = data.decisions[0]
    later = original.model_copy(update={"key": "later", "at": original.at + timedelta(minutes=15)})
    missing = data.model_copy(
        update={
            "decisions": (*data.decisions, later),
            "prices": tuple(p for p in data.prices if p.ticker != original.ticker),
        }
    )
    r = replay(missing, build(missing), Candidate("baseline", 0, 0, 0, 1, 60), missing.sessions, 5)
    assert r.selected == 3 and r.unresolved == 1
    assert all(u.key != "later" for u in r.units)


def test_labels_received_after_training_cutoff_stay_unresolved():
    data = fixture(1)
    r = replay(
        data,
        build(data),
        Candidate("baseline", 0, 0, 0, 1, 60),
        data.sessions,
        5,
        label_asof=data.decisions[0].at,
    )
    assert r.unresolved == r.selected == 3


def test_source_bounds_duplicate_prices_and_reserved_file_rejected(tmp_path):
    data = fixture(1)
    with pytest.raises(ValidationError):
        Price(ticker="X", at=data.asof, known_at=data.asof - timedelta(seconds=1), value=100)
    with pytest.raises(ValidationError):
        Dataset.model_validate(data.model_dump() | {"prices": (*data.prices, data.prices[0])})
    with pytest.raises(ValidationError):
        Context(at=data.asof, known_at=data.asof, value=float("nan"))
    from webapp.event_calibration.model import ROOT

    with pytest.raises(ValueError, match="reserved"):
        load(ROOT / "data/study_g/not-read.json")


def test_final_price_changes_cannot_select_another_candidate():
    data = fixture(80)
    initial = run(data)
    assert initial["winner"] is not None
    final_days = set(data.sessions[60:])
    changed = tuple(
        p.model_copy(update={"value": p.value * 0.8})
        if p.at.astimezone(ET).date() in final_days
        and p.ticker != "SPY"
        and p.at.astimezone(ET).hour >= 10
        else p
        for p in data.prices
    )
    repeated = run(data.model_copy(update={"prices": changed}))
    assert initial["winner"]["key"] == repeated["winner"]["key"]
    assert initial["rows"] == repeated["rows"]
    assert initial["final"]["mean_net_pct"] != repeated["final"]["mean_net_pct"]
    assert all(
        not row["eligible"]
        for row in initial["rows"]
        if row["parameters"]["arm"] in ("normalized_gamma", "normalized_sector")
    )
    assert initial["live_promotion"] is False


def test_read_only_export_does_not_create_tables():
    engine = create_engine("sqlite://")
    statements = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda con, cur, sql, args, context, many: statements.append(sql),
    )
    data = export(engine, datetime(2026, 10, 31, tzinfo=UTC))
    assert not data.decisions and not data.sessions
    assert not any(
        s.lstrip().upper().startswith(("INSERT", "UPDATE", "CREATE", "DELETE")) for s in statements
    )
    engine.dispose()


@pytest.mark.asyncio
async def test_probe_without_credential_makes_zero_network_requests(monkeypatch):
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)

    def forbidden(request):
        pytest.fail("No network without a credential")

    result = await probe(
        "NFLX", (date(2026, 10, 30), date(2026, 9, 22)), transport=httpx.MockTransport(forbidden)
    )
    assert result["status"] == "MISSING_CREDENTIAL" and result["requests"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 429, 500])
async def test_probe_stops_on_first_failure_and_never_reports_a_ready_dataset(monkeypatch, status):
    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "synthetic-test-only")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="provider response")

    result = await probe(
        "NFLX", (date(2026, 10, 30), date(2026, 9, 22)), transport=httpx.MockTransport(handler)
    )
    assert len(calls) == result["requests"] == 1
    assert not result["ready_for_calibration"]


def test_paired_call_and_spread_commission_arithmetic_and_fixed_exit_symbols():
    data = fixture(1)
    unit = replay(data, build(data), Candidate("baseline", 0, 0, 0, 1, 60), data.sessions, 5).units[
        0
    ]
    original = next(d for d in data.decisions if d.key == unit.key)

    def structures(at, exit_):
        def leg(symbol, strike, long, bid, ask):
            return Leg(
                symbol=symbol,
                strike=strike,
                expiry=date(2026, 12, 18),
                right="call",
                is_long=long,
                bid=bid,
                ask=ask,
                fetched_at=at,
            )

        return (
            Structure(
                kind="long_call",
                multiplier=100,
                legs=(
                    leg(
                        "AAA261218C00100000",
                        100,
                        True,
                        0.48 if not exit_ else 0.68,
                        0.5 if not exit_ else 0.7,
                    ),
                ),
            ),
            Structure(
                kind="bull_call_debit",
                multiplier=100,
                legs=(
                    leg(
                        "AAA261218C00100000",
                        100,
                        True,
                        0.48 if not exit_ else 0.68,
                        0.5 if not exit_ else 0.7,
                    ),
                    leg("AAA261218C00105000", 105, False, 0.2, 0.22),
                ),
            ),
        )

    initial = original.model_copy(update={"options": structures(original.at, False)})
    entry = original.model_copy(
        update={"key": "entry", "at": unit.entry_at, "options": structures(unit.entry_at, False)}
    )
    exit_ = original.model_copy(
        update={"key": "exit", "at": unit.exit_at, "options": structures(unit.exit_at, True)}
    )
    dataset = data.model_copy(
        update={
            "decisions": (
                *(initial if d.key == original.key else d for d in data.decisions),
                entry,
                exit_,
            )
        }
    )
    rows = {r.kind: r for r in compare(dataset, unit)}
    assert rows["long_call"].net_usd == pytest.approx(68 - 0.65 - (50 + 0.65))
    # Three spread units: entry (50-20)+1.30; exit (68-22)-1.30.
    assert rows["bull_call_debit"].units == 3
    assert rows["bull_call_debit"].net_usd == pytest.approx(3 * ((68 - 22 - 1.3) - (50 - 20 + 1.3)))
    wrong = exit_.model_copy(
        update={
            "options": tuple(
                o.model_copy(
                    update={
                        "legs": tuple(
                            leg.model_copy(update={"symbol": leg.symbol + "X"}) for leg in o.legs
                        )
                    }
                )
                for o in exit_.options
            )
        }
    )
    missing = dataset.model_copy(
        update={"decisions": tuple(wrong if d.key == "exit" else d for d in dataset.decisions)}
    )
    assert all(r.net_usd is None for r in compare(missing, unit) if r.kind != "stock")


def test_manifest_roundtrip_and_tamper_rejection(tmp_path):
    import json

    from webapp.event_calibration.model import digest

    data = fixture(1).model_dump(mode="json")
    path = tmp_path / "events.json"
    path.write_text(json.dumps(data | {"report_hash": digest(data)}))
    assert load(path) == fixture(1)
    data["prices"][0]["value"] += 1
    path.write_text(json.dumps(data | {"report_hash": "wrong"}))
    with pytest.raises(ValueError, match="manifest"):
        load(path)


def test_stock_budget_includes_slippage():
    data = fixture(1)
    original = data.decisions[0].model_copy(update={"risk_fraction": 0.01})
    data = data.model_copy(
        update={
            "decisions": tuple(original if d.key == original.key else d for d in data.decisions)
        }
    )
    unit = replay(data, build(data), Candidate("baseline", 0, 0, 0, 1, 60), data.sessions, 5).units[
        0
    ]
    stock = compare(data, unit)[0]
    assert stock.units == 24 and stock.debit <= 2500


def test_mixed_expiry_spread_is_not_a_debit_vertical():
    at = datetime(2026, 10, 30, 15, tzinfo=UTC)
    a = Leg(
        symbol="A",
        strike=100,
        expiry=date(2026, 11, 20),
        right="call",
        is_long=True,
        bid=0.4,
        ask=0.5,
        fetched_at=at,
    )
    b = Leg(
        symbol="B",
        strike=105,
        expiry=date(2026, 12, 18),
        right="call",
        is_long=False,
        bid=0.2,
        ask=0.3,
        fetched_at=at,
    )
    with pytest.raises(ValidationError, match="expiries"):
        Structure(kind="bull_call_debit", legs=(a, b), multiplier=100)


def test_export_freezes_incomplete_first_snapshot_and_rejects_future_news():
    import json

    from sqlalchemy.orm import Session
    from webapp.live_alpha.store import LiveScan

    engine = create_engine("sqlite://")
    LiveScan.__table__.create(engine)
    base = load_live_alpha_settings()
    at = datetime(2026, 10, 30, 13, 45, 30, tzinfo=UTC)
    card = {
        "ticker": "AAA",
        "direction": "up",
        "market_mode": "LIVE",
        "recommendation": "BUY",
        "readiness": "READY",
        "policy_version": base.policy_version,
        "profile_sha256": base.profile_sha256,
        "stock_plan": {"entry_ref": 100, "stop": 97},
        "options": [],
    }
    first = {
        "policy_version": base.policy_version,
        "profile_sha256": base.profile_sha256,
        "universe": ["AAA"],
        "cards": [],
    }
    later = first | {
        "cards": [card | {"decision_as_of": (at + timedelta(seconds=55)).isoformat()}],
        "news_observations": {
            "AAA": {"state": "checked_found", "checked_at": (at + timedelta(days=1)).isoformat()}
        },
    }
    with Session(engine) as s, s.begin():
        for key, when, payload in [
            ("first", at, first),
            ("later", at + timedelta(minutes=1), later),
        ]:
            s.add(
                LiveScan(
                    scan_id=key,
                    started_at=when - timedelta(seconds=10),
                    finished_at=when,
                    market_mode="LIVE",
                    status="ok",
                    policy_version=base.policy_version,
                    snapshot_json=json.dumps(payload),
                )
            )
    data = export(engine, at + timedelta(hours=1))
    assert len(data.decisions) == 2
    assert all(not d.usable for d in data.decisions)
    assert data.decisions[-1].news is None
    samples = build(data)
    assert len(samples) == 1 and samples[0].decision.key == data.decisions[0].key
    assert samples[0].up is None
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("quota_case", ["current", "reserve", "stale", "missing"])
async def test_probe_budget_date_and_eight_request_bound(monkeypatch, quota_case):
    from email.utils import format_datetime

    monkeypatch.setenv("UNUSUAL_WHALES_API_KEY", "synthetic-test-only")
    calls = []

    def handler(request):
        calls.append(request)
        day = request.url.params.get("date")
        if day is None:
            day = (
                datetime.fromtimestamp(int(request.url.params["older_than"]), UTC)
                .astimezone(ET)
                .date()
                .isoformat()
            )
        field = "created_at" if "flow-alerts" in request.url.path else "date"
        headers = {
            "x-uw-daily-req-count": "100",
            "x-uw-token-req-limit": "30000",
            "date": format_datetime(datetime.now(UTC)),
        }
        if quota_case == "reserve":
            headers["x-uw-daily-req-count"] = "29999"
        if quota_case == "stale":
            headers["date"] = format_datetime(datetime.now(UTC) - timedelta(days=2))
        if quota_case == "missing":
            headers = {}
        return httpx.Response(200, json={"data": [{field: day}]}, headers=headers)

    report = await probe(
        "NFLX", (date(2026, 10, 30), date(2026, 9, 22)), transport=httpx.MockTransport(handler)
    )
    assert report["requests"] == (8 if quota_case == "current" else 1)
    assert not report["ready_for_calibration"] and not report["bulk_harvest_performed"]
    if quota_case in ("stale", "missing"):
        assert report["status"] == "QUOTA_UNVERIFIED"


def test_unresolved_reference_is_not_reported_as_zero_daily_return():
    from webapp.event_calibration.research import summary

    data = fixture(1)
    data = data.model_copy(update={"prices": tuple(p for p in data.prices if p.ticker != "SPY")})
    result = replay(data, build(data), Candidate("baseline", 0, 0, 0, 1, 60), data.sessions, 5)
    metrics = summary(result, data.sessions)
    assert metrics["unresolved"] == 3
    assert metrics["daily_excess_pp"] == [None]
    assert metrics["mean_daily_excess_pp"] is None


def test_future_context_and_future_dataset_receipt_are_rejected():
    data = fixture(1)
    d = data.decisions[0]
    with pytest.raises(ValidationError):
        Decision.model_validate(
            d.model_dump()
            | {"market": {"value": 1, "at": d.at, "known_at": d.at + timedelta(seconds=1)}}
        )
    with pytest.raises(ValidationError):
        Dataset.model_validate(data.model_dump() | {"asof": d.at})
