"""Sealed fixed rosters load without new selection, hidden labels or file writes."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from webapp.horizon_research import source
from webapp.horizon_research.model import settings

DAY = date(2026, 6, 24)
OPEN = datetime(2026, 6, 24, 13, 30, tzinfo=UTC)
RECEIVED = datetime(2026, 11, 1, 20, tzinfo=UTC).isoformat()


def write_packet(path: Path, packet: dict[str, Any]) -> str:
    body = dict(packet)
    checksum = source.digest(body)
    body["packet_hash"] = checksum
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps(body).encode()))
    return checksum


def minute(at: datetime, **changes: object) -> dict[str, object]:
    return (
        dict(
            start_time=at.isoformat(),
            open="100",
            high="103",
            low="99",
            close="101",
            volume="20",
            market_time="r",
        )
        | changes
    )


@pytest.fixture
def sample(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, dict[str, Any]]:
    root = tmp_path / "source"
    root.mkdir()
    cfg = settings().model_copy(
        update=dict(
            source_start=DAY,
            source_end=DAY,
            source_sessions=1,
            warmup_sessions=0,
            evaluation_sessions=1,
        )
    )
    monkeypatch.setattr(source, "settings", lambda: cfg)
    profile = dict(
        version="capture_v1", start=str(DAY), end=str(DAY), tickers=["AAPL"], benchmark="SPY"
    )
    state = dict(status="complete", profile=profile, profile_hash=source.digest(profile))
    (root / "state.json").write_text(json.dumps(state))
    manifest = {}
    for ticker in ("AAPL", "SPY"):
        for kind in ("minute", "flow"):
            if ticker == "SPY" and kind == "flow":
                continue
            relative = f"{DAY}/{ticker}-{kind}.json.gz"
            data = (
                [
                    minute(OPEN, volume="-5"),
                    minute(OPEN + timedelta(minutes=1), volume=None),
                    minute(OPEN + timedelta(minutes=389)),
                ]
                if kind == "minute"
                else []
            )
            packet = dict(
                complete=True,
                profile_hash=state["profile_hash"],
                ticker=ticker,
                day=str(DAY),
                kind=kind,
                received_at=RECEIVED,
                payload={"data": data},
            )
            manifest[relative] = write_packet(root / relative, packet)
    report = dict(
        report_hash="f" * 64,
        source_state_hash=source.digest(state),
        sessions=1,
        warmup_sessions=0,
        evaluation_sessions=1,
        source_packet_hashes=manifest,
        rows=[],
    )
    for arm in cfg.arms:
        report["rows"].append(
            dict(
                arm=arm,
                bps_per_side=cfg.primary_cost_bps_per_side,
                selected=1,
                resolved=1,
                unresolved=0,
                missing=[],
                ledger=[
                    dict(
                        ticker="AAPL",
                        day=str(DAY),
                        signal_at=(OPEN + timedelta(minutes=30)).isoformat(),
                    )
                ],
            )
        )
    monkeypatch.setattr(source.original_report, "load", lambda: report)
    return root, report


def test_load_preserves_frozen_rosters_gaps_and_unknown_volume(
    sample: tuple[Path, dict[str, Any]],
) -> None:
    root, report = sample
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = source.load(root)
    assert len(result.signals) == 3 and {s.arm for s in result.signals} == set(settings().arms)
    assert len(result.candles) == 6 and all(c.ticker in ("AAPL", "SPY") for c in result.candles)
    assert result.candles[0].volume == -5 and any(c.volume is None for c in result.candles)
    quality = dict(result.quality)
    assert quality["negative_volume_rows"] == 2 and quality["unknown_volume_rows"] == 2
    assert quality["zero_volume_rows"] == quality["identical_duplicate_minutes"] == 0
    assert quality["rth_expected_missing_minutes"] == 774
    assert result.original_report_hash == report["report_hash"] and len(result.packet_hashes) == 3
    assert before == {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    assert source.load(root).source_hash == result.source_hash


@pytest.mark.parametrize(
    "path",
    [
        "../bars.csv",
        "/tmp/bars.csv",
        "2026-06-25/AAPL-minute.json.gz",
        "2026-06-24/AAPL-daily.json.gz",
        "2026-06-24/AAPL-chain.json.gz",
        "study_g/bars.csv",
        "2026-06-24\\AAPL-minute.json.gz",
    ],
)
def test_unsafe_and_unregistered_manifest_paths_are_rejected_before_packet_reads(
    sample: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    root, report = sample
    report["source_packet_hashes"][path] = "a" * 64
    reads = []
    original = source._packet

    def tracked(*args: Any, **kwargs: Any) -> dict[str, Any]:
        reads.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(source, "_packet", tracked)
    with pytest.raises(ValueError):
        source.load(root)
    assert reads == []


def test_packet_symlink_redirect_is_rejected_before_read(
    sample: tuple[Path, dict[str, Any]], tmp_path: Path
) -> None:
    root, _ = sample
    p = root / f"{DAY}/AAPL-minute.json.gz"
    other = tmp_path / "reserved-later.json.gz"
    p.rename(other)
    p.symlink_to(other)
    with pytest.raises(ValueError, match="redirect"):
        source.load(root)


@pytest.mark.parametrize(
    "damage",
    [
        "checksum",
        "profile",
        "date",
        "ticker",
        "complete",
        "invalid_ohlc",
        "conflicting_timestamp",
        "market_time",
        "future_row",
    ],
)
def test_packet_integrity_and_calendar_are_required(
    sample: tuple[Path, dict[str, Any]], damage: str
) -> None:
    root, report = sample
    rel = f"{DAY}/AAPL-minute.json.gz"
    p = root / rel
    packet = json.loads(gzip.decompress(p.read_bytes()))
    packet.pop("packet_hash")
    if damage == "profile":
        packet["profile_hash"] = "wrong"
    elif damage == "date":
        packet["day"] = "2026-09-19"
    elif damage == "ticker":
        packet["ticker"] = "NVDA"
    elif damage == "complete":
        packet["complete"] = False
    elif damage == "invalid_ohlc":
        packet["payload"]["data"][0]["high"] = "90"
    elif damage == "conflicting_timestamp":
        packet["payload"]["data"].append(minute(OPEN, close="102"))
    elif damage == "market_time":
        packet["payload"]["data"][0]["market_time"] = "pr"
    elif damage == "future_row":
        packet["payload"]["data"][0]["start_time"] = "2026-09-19T14:00:00+00:00"
    else:
        packet["payload"]["data"][0]["close"] = "102"
    checksum = write_packet(p, packet)
    if damage != "checksum":
        report["source_packet_hashes"][rel] = checksum
    with pytest.raises(ValueError):
        source.load(root)


@pytest.mark.parametrize("damage", ["missing", "unresolved", "duplicate", "outside_eval", "state"])
def test_original_ledger_and_capture_state_cannot_drift(
    sample: tuple[Path, dict[str, Any]], damage: str
) -> None:
    root, report = sample
    row = report["rows"][0]
    if damage == "missing":
        row["missing"] = [{"ticker": "AAPL"}]
    elif damage == "unresolved":
        row["resolved"] = 0
    elif damage == "duplicate":
        row["ledger"] *= 2
        row["selected"] = row["resolved"] = 2
    elif damage == "outside_eval":
        row["ledger"][0]["day"] = "2026-06-25"
    else:
        (root / "state.json").write_text("{}")
    with pytest.raises(ValueError):
        source.load(root)


def test_calendar_early_close_changes_expected_minutes_without_filling_rows(
    sample: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = sample
    live = source.load_live_alpha_settings()
    cal = live.calendar.model_copy(update={"early_closes": (*live.calendar.early_closes, DAY)})
    monkeypatch.setattr(
        source, "load_live_alpha_settings", lambda: live.model_copy(update={"calendar": cal})
    )
    result = source.load(root)
    assert len(result.candles) == 4  # The16:00close's15:59row is outside early-close RTH.
    assert dict(result.quality)["rth_expected_missing_minutes"] == 416
    assert all(c.at < datetime(2026, 6, 24, 17, tzinfo=UTC) for c in result.candles)


def test_manifest_requires_flow_packets_even_though_only_candles_are_loaded(
    sample: tuple[Path, dict[str, Any]],
) -> None:
    root, report = sample
    report["source_packet_hashes"].pop(f"{DAY}/AAPL-flow.json.gz")
    with pytest.raises(ValueError, match="manifest incomplete"):
        source.load(root)
