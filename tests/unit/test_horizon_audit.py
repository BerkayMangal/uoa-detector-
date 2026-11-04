"""Independent synthetic truth and adversarial ledger checks; no real prices read."""

from __future__ import annotations

import copy
import gzip
import hashlib
import json
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from scripts import audit_horizon_research as independent
from webapp.horizon_research.engine import run
from webapp.horizon_research.model import Candle, Signal, Source, profile_hash

AT = datetime(2026, 8, 3, 14, 15, tzinfo=UTC)


def fixture() -> tuple[dict[str, Any], dict[str, Any], Source]:
    cfg = independent.config()
    days = tuple(independent.sessions())
    original_rows = []
    for arm in cfg["arms"]:
        for cost in cfg["costs_bps_per_side"]:
            r = cost / 10000
            q = int(2500 // (100 * (1 + r)))
            debit = q * 100 * (1 + r)
            net = -q * 100 * 2 * r
            old_unit = dict(
                ticker="NVDA",
                day=str(AT.date()),
                signal_at=AT.isoformat(),
                entry_at=(AT + timedelta(minutes=2)).isoformat(),
                exit_at=(AT + timedelta(minutes=62)).isoformat(),
                quantity=q,
                debit=debit,
                net_usd=net,
                spy_matched_usd=net,
                excess_usd=0.0,
                return_pct=net / debit * 100,
            )
            original_rows.append(
                dict(
                    arm=arm,
                    bps_per_side=cost,
                    selected=1,
                    resolved=1,
                    unresolved=0,
                    ledger=[old_unit],
                    missing=[],
                    net_unit_usd=net,
                    net_known_unit_usd=net,
                    spy_excess_known_unit_usd=0.0,
                    without_best_1_usd=0.0,
                    without_best_3_usd=0.0,
                )
            )
    manifest = {
        f"{d}/{t}-{kind}.json.gz": "a" * 64
        for d in days
        for t in ("AAPL", "NVDA", "NFLX", "SPY")
        for kind in ("minute", "flow")
        if t != "SPY" or kind == "minute"
    }
    original: dict[str, Any] = dict(rows=original_rows, source_packet_hashes=manifest)
    original["report_hash"] = independent.checksum(original)
    signals = tuple(
        Signal(
            independent.checksum((original["report_hash"], arm, "NVDA", AT.date(), AT)),
            arm,
            "NVDA",
            AT.date(),
            AT,
        )
        for arm in cfg["arms"]
    )
    signals = tuple(sorted(signals, key=lambda s: (s.at, s.arm, s.ticker)))
    packets = tuple(sorted(manifest.items()))
    source_hash = independent.checksum(
        dict(
            original_report_hash=original["report_hash"],
            new_profile_hash=profile_hash(),
            signals=[asdict(s) for s in signals],
            packet_hashes=packets,
        )
    )
    points = []
    for day in days:
        if not date(2026, 8, 3) <= day <= date(2026, 8, 12):
            continue
        at, end = independent.bounds(day)
        while at < end:
            for ticker in ("NVDA", "SPY"):
                points.append(Candle(ticker, day, at, 100.0, 100.0, 100.0, 100.0, 1.0))
            at += timedelta(minutes=1)
    source = Source(
        days,
        days[cfg["warmup_sessions"] :],
        signals,
        tuple(points),
        source_hash,
        original["report_hash"],
        packets,
    )
    report = json.loads(json.dumps(run(source), default=str))
    return report, original, source


@pytest.fixture(scope="module")
def sample() -> tuple[dict[str, Any], dict[str, Any], Source]:
    return fixture()


def first_unit(report: dict[str, Any]) -> dict[str, Any]:
    return next(u for u in report["units"] if not u["missing"])


def test_full_synthetic_source_and_all_twelve_controls_reconcile(sample) -> None:
    report, original, source = sample
    out = independent.audit(report, original, source)
    assert (
        out["status"] == "RECONCILED" and out["source_replay"] == "ALL_MARKS_GATES_AND_EXCURSIONS"
    )
    assert out["audited_cells"] == 1440 and out["audited_original_controls"] == 12
    assert out["audited_outcome_units"] == 360
    # Independently specified flat-stock and flat-SPY truth: exactly equal net
    # returns and genuine zero excess, despite nonzero transaction costs.
    zero = next(
        r
        for r in report["rows"]
        if r["filter_minutes"] == 0 and r["horizon"] == "m60" and r["cost_bps_per_side"] == 5
    )
    assert zero["strict_net_usd"] == pytest.approx(-2.4)
    assert zero["strict_excess_usd"] == pytest.approx(0, abs=1e-10)
    filtered = next(r for r in report["rows"] if r["filter_minutes"] == 5)
    assert filtered["gate_false"] == 1 and filtered["strict_net_usd"] == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("net_usd", 0.0),
        ("quantity", 25),
        ("debit", 2500.0),
        ("spy_matched_usd", 10.0),
        ("entry_price", 101.0),
    ],
)
def test_rehashed_or_unhashed_false_math_is_refused(sample, field, value) -> None:
    report, original, source = sample
    changed = copy.deepcopy(report)
    first_unit(changed)[field] = value
    with pytest.raises(ValueError):
        independent.audit(changed, original, source)


def test_mathematical_only_audit_does_not_claim_source_replay(sample) -> None:
    report, original, _ = sample
    out = independent.audit(report, original)
    assert (
        out["source_replay"] == "NOT_REPLAYED_LOADER_HASH_BOUND_ONLY" and out["source_limitations"]
    )


def test_future_candle_cannot_change_a_completed_gate(sample) -> None:
    report, original, source = sample
    changed = copy.deepcopy(report)
    gate = next(g for g in changed["gates"] if g["minutes"] == 5)
    gate["available_at"] = (AT + timedelta(minutes=1)).isoformat()
    with pytest.raises(ValueError, match=r"candle|gate"):
        independent.audit(changed, original, source)


def test_source_path_extrema_are_independently_rederived(sample) -> None:
    report, original, source = sample
    changed = copy.deepcopy(report)
    u = first_unit(changed)
    u["gross_underlying_mfe_pct"] = 10.0
    # Ten-percent MFE passes endpoint bounds on a flat-price trade, but the
    # actual synthetic source has no such high. Source replay catches it.
    with pytest.raises(ValueError, match="MFE"):
        independent.audit(changed, original, source)


def test_unknown_gate_cannot_be_published_as_a_zero_exposure_complete_cell(sample) -> None:
    report, original, _ = sample
    changed = copy.deepcopy(report)
    row = next(r for r in changed["rows"] if r["filter_minutes"] == 5)
    key = row["false_signal_keys"][0]
    gate = next(g for g in changed["gates"] if g["signal_key"] == key and g["minutes"] == 5)
    gate["state"], gate["missing"] = None, ["completed_bar_minute_missing"]
    changed["signal_gates"] = copy.deepcopy(changed["gates"])
    row.update(
        gate_false=0,
        gate_unknown=1,
        false_signal_keys=[],
        unknown_signal_keys=[key],
        missing_reasons={"completed_bar_minute_missing": 1},
    )
    with pytest.raises(ValueError, match=r"status|zero|coverage"):
        independent.audit(changed, original)


def test_old_control_ledger_change_is_detected_even_after_hash_rebinding(sample) -> None:
    report, original, source = sample
    changed = copy.deepcopy(original)
    changed["rows"][0]["ledger"][0]["net_usd"] += 1
    with pytest.raises(ValueError, match="checksum"):
        independent.audit(report, changed, source)


def test_source_manifest_change_or_new_control_selection_fails_closed(sample) -> None:
    report, original, _ = sample
    changed = copy.deepcopy(report)
    changed["source_packet_hashes"][0][1] = "b" * 64
    with pytest.raises(ValueError, match="manifest"):
        independent.audit(changed, original)


def test_close_uses_the_last_rth_minute_not_an_extended_hours_open(sample) -> None:
    report, original, source = sample
    changed = copy.deepcopy(report)
    unit = next(u for u in changed["units"] if u["horizon"] == "c0")
    unit["exit_source_at"] = unit["exit_at"]
    with pytest.raises(ValueError, match=r"close|source"):
        independent.audit(changed, original, source)


def test_a_missing_source_endpoint_cannot_retain_a_resolved_return(sample) -> None:
    report, original, source = sample
    u = first_unit(report)
    removed = replace(
        source,
        candles=tuple(
            c
            for c in source.candles
            if not (c.ticker == u["ticker"] and c.at == independent.stamp(u["exit_source_at"]))
        ),
    )
    with pytest.raises(ValueError, match=r"absent|source"):
        independent.audit(report, original, removed)


def test_compressed_ledger_tampering_is_rejected_before_calculation(sample, tmp_path: Path) -> None:
    report, _, _ = sample
    body = copy.deepcopy(report)
    units = body.pop("units")
    encoded = json.dumps(units, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    zipped = gzip.compress(encoded, mtime=0)
    body.update(
        ledger_file="units.json.gz",
        ledger_sha256=hashlib.sha256(zipped).hexdigest(),
        ledger_content_hash=independent.checksum(units),
    )
    body["report_hash"] = independent.checksum(body)
    path = tmp_path / "report.json"
    path.write_text(json.dumps(body))
    (tmp_path / "units.json.gz").write_bytes(zipped)
    assert independent.load_report(path)["units"] == units
    (tmp_path / "units.json.gz").write_bytes(zipped + b"tamper")
    with pytest.raises(ValueError, match="compressed"):
        independent.load_report(path)


@pytest.fixture(scope="module")
def nonzero_bootstrap(sample) -> tuple[dict[str, Any], dict[str, Any], Source]:
    _, original, source = sample
    # The original delay1/m60 entry and exit remain flat and unchanged. A later
    # synthetic stock jump produces nonzero longer-horizon differences and a
    # positive resampling radius; SPY remains flat. No market data are read.
    future = AT + timedelta(minutes=62)
    revised = replace(
        source,
        candles=tuple(
            replace(c, open=120.0, high=120.0, low=120.0, close=120.0)
            if c.ticker == "NVDA" and c.day == AT.date() and c.at > future
            else c
            for c in source.candles
        ),
    )
    report = json.loads(json.dumps(run(revised), default=str))
    return report, original, revised


def test_available_nonzero_bootstrap_keeps_the_anchor_exactly_zero(nonzero_bootstrap) -> None:
    report, original, source = nonzero_bootstrap
    primary = report["primary_family"]
    assert (
        len(primary["administrative_days"]) >= 2 * independent.config()["bootstrap_block_sessions"]
    )
    assert primary["bootstrap"]["status"] == "DESCRIPTIVE_ONLY"
    anchor = next(c for c in primary["contrasts"] if c["horizon"] == "m60")
    other = next(c for c in primary["contrasts"] if c["horizon"] == "m120")
    assert anchor["shared_max_deviation_descriptive_95pct_band"] == [0.0, 0.0]
    assert other["mean_daily_difference_vs_m60_usd"] != 0
    assert (
        other["shared_max_deviation_descriptive_95pct_band"][1]
        > other["shared_max_deviation_descriptive_95pct_band"][0]
    )
    assert independent.audit(report, original, source)["status"] == "RECONCILED"


def test_anchor_band_cannot_hide_nonzero_values_inside_numeric_tolerance(nonzero_bootstrap) -> None:
    report, original, _ = nonzero_bootstrap
    changed = copy.deepcopy(report)
    anchor = next(c for c in changed["primary_family"]["contrasts"] if c["horizon"] == "m60")
    anchor["shared_max_deviation_descriptive_95pct_band"] = [-1e-9, 1e-9]
    with pytest.raises(ValueError, match="anchor self-difference"):
        independent.audit(changed, original)


def test_nonanchor_shared_band_still_uses_the_independent_radius(nonzero_bootstrap) -> None:
    report, original, _ = nonzero_bootstrap
    changed = copy.deepcopy(report)
    other = next(c for c in changed["primary_family"]["contrasts"] if c["horizon"] == "m120")
    other["shared_max_deviation_descriptive_95pct_band"][1] += 1.0
    with pytest.raises(ValueError, match="descriptive shared band"):
        independent.audit(changed, original)


def test_nonanchor_pointwise_quantiles_remain_independently_checked(nonzero_bootstrap) -> None:
    report, original, _ = nonzero_bootstrap
    changed = copy.deepcopy(report)
    other = next(c for c in changed["primary_family"]["contrasts"] if c["horizon"] == "m120")
    other["pointwise_descriptive_95pct_difference"][0] -= 1.0
    with pytest.raises(ValueError, match="descriptive percentile mismatch"):
        independent.audit(changed, original)
