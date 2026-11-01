"""Reference arithmetic and assumed-time causality; fixtures are not market data."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from ruamel.yaml import YAML
from scripts.diagnose_vendor_history import diagnose, flow_value
from webapp.history_capture.capture import ROOT, read, seal

from uoa_detector.live_alpha.calendar import is_session_day
from uoa_detector.live_alpha.settings import load_live_alpha_settings

ET = ZoneInfo("America/New_York")


def record(at, premium, chain):
    return {
        "created_at": at.isoformat(),
        "end_time": int(at.timestamp() * 1000),
        "total_premium": premium,
        "total_ask_side_prem": premium,
        "total_bid_side_prem": 0,
        "has_multileg": False,
        "type": "call",
        "option_chain": chain,
    }


def archive(root):
    days = []
    day = date(2026, 6, 24)
    cal = load_live_alpha_settings().calendar
    while len(days) < 21:
        if is_session_day(day, cal):
            days.append(day)
        day += timedelta(days=1)
    state = {
        "status": "complete",
        "profile_hash": "fixture",
        "profile": {
            "start": str(days[0]),
            "end": str(days[-1]),
            "tickers": ["NFLX"],
            "benchmark": "SPY",
        },
    }
    (root / "state.json").write_text(json.dumps(state))
    for n, day in enumerate(days):
        at = datetime.combine(day, cal.regular_open, ET).astimezone(UTC) + timedelta(minutes=15)
        price_points = [
            (at - timedelta(minutes=7), 99),
            (at - timedelta(minutes=2), 100),
            (at + timedelta(minutes=2), 100),
            (at + timedelta(minutes=62), 102),
        ]
        for ticker in ("NFLX", "SPY"):
            rows = [
                {
                    "start_time": t.isoformat(),
                    "open": v if ticker == "NFLX" else 100,
                    "close": v if ticker == "NFLX" else 100,
                }
                for t, v in price_points
            ]
            seal(
                root / str(day) / f"{ticker}-minute.json.gz",
                {
                    "complete": True,
                    "profile_hash": "fixture",
                    "day": str(day),
                    "ticker": ticker,
                    "payload": {"data": rows},
                },
            )
        premium = 1500000 if n == 20 else 500000
        seal(
            root / str(day) / "NFLX-flow.json.gz",
            {
                "complete": True,
                "profile_hash": "fixture",
                "day": str(day),
                "ticker": "NFLX",
                "payload": {
                    "data": [
                        record(at - timedelta(minutes=3), premium, "A"),
                        record(at - timedelta(minutes=3), premium, "B"),
                    ]
                },
            },
        )
    return days


def test_native_aggregation_end_and_declared_lag_are_not_backdated():
    cfg = YAML(typ="safe").load((ROOT / "profiles/history_diagnostic_v1.yaml").read_text())
    at = datetime(2026, 6, 24, 14, tzinfo=UTC)
    row = record(at - timedelta(minutes=5), 300000, "A")
    row["end_time"] = int((at + timedelta(minutes=1)).timestamp() * 1000)
    assert flow_value([row], at, cfg)[0] == 0
    assert flow_value([row], at + timedelta(minutes=3), cfg)[0] == 300000


def test_fixed_references_do_not_select_a_winner_or_claim_observed_publication(tmp_path):
    archive(tmp_path)
    result = diagnose(tmp_path)
    assert result["executed_fixed_replays"] == 12 and result["winner"] is None
    assert result["classification"] == "EXPLORATORY_VENDOR_EVENT_TIME_ASSUMPTION"
    assert result["evaluation_sessions"] == 1 and not result["full_438_calibration_executed"]
    for row in result["rows"]:
        assert row["selected"] == row["resolved"] == 1 and row["unresolved"] == 0
        u = row["ledger"][0]
        slip = row["bps_per_side"] / 10000
        q = int(2500 // (100 * (1 + slip)))
        assert u["quantity"] == q
        assert u["net_usd"] == pytest.approx(q * (102 * (1 - slip) - 100 * (1 + slip)))
        assert u["entry_at"] > u["signal_at"] + timedelta(minutes=1)


def test_incomplete_capture_never_generates_outcomes(tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"status": "stopped"}))
    result = diagnose(tmp_path)
    assert result["status"] == "CAPTURE_INCOMPLETE" and result["executed_fixed_replays"] == 0


def test_exact_exit_gap_stays_unknown(tmp_path):
    days = archive(tmp_path)
    path = tmp_path / str(days[-1]) / "SPY-minute.json.gz"
    packet = read(path)
    packet.pop("packet_hash")
    packet["payload"]["data"].pop()
    path.unlink()
    seal(path, packet)
    result = diagnose(tmp_path)
    for row in result["rows"]:
        assert row["unresolved"] == 1 and row["net_unit_usd"] is None
        assert row["descriptive_daily_excess_interval_usd"] is None


def test_native_premium_decimal_splits_are_not_false_conflicts():
    cfg = YAML(typ="safe").load((ROOT / "profiles/history_diagnostic_v1.yaml").read_text())
    at = datetime(2026, 6, 24, 14, tzinfo=UTC)
    row = record(at - timedelta(minutes=3), 0.3, "A")
    row.update(total_ask_side_prem=0.1, total_bid_side_prem=0.2)
    value = flow_value([row], at, cfg)
    assert value is not None and value[0] == pytest.approx(0.1)
