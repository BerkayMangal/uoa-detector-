"""Read only sealed pre-cutoff minute/flow evidence and frozen signal rosters."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

from webapp.history_capture import report as original_report
from webapp.horizon_research.model import Candle, Signal, Source, profile_hash, settings

from uoa_detector.live_alpha.calendar import close_time, is_session_day
from uoa_detector.live_alpha.settings import load_live_alpha_settings


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, allow_nan=False).encode()
    ).hexdigest()


def _stamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("source timestamp missing")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("source timestamp invalid") from None
    if result.tzinfo is None:
        raise ValueError("source timestamp is not aware")
    return result.astimezone(UTC)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def _safe_root(source: Path) -> Path:
    if any(p.casefold().replace("-", "_") == "study_g" for p in source.parts):
        raise ValueError("reserved source path")
    root = source.resolve(strict=True)
    if not root.is_dir() or any(p.casefold().replace("-", "_") == "study_g" for p in root.parts):
        raise ValueError("reserved or invalid source directory")
    return root


def _file(root: Path, relative: str) -> Path:
    path = root / relative
    # No symlinks or alternate lexical paths: validate BEFORE reading contents.
    if path.resolve(strict=True) != path:
        raise ValueError("source path redirects")
    if not path.is_file():
        raise ValueError("source packet absent")
    return path


def _packet(
    path: Path, expected: str, profile: str, ticker: str, day: date, kind: str
) -> dict[str, Any]:
    # Any is confined to the sealed vendor-JSON adapter boundary.
    packet: object = json.loads(gzip.decompress(path.read_bytes()))
    if not isinstance(packet, dict):
        raise ValueError("source packet shape")
    saved = packet.get("packet_hash")
    body = {k: v for k, v in packet.items() if k != "packet_hash"}
    if saved != expected or digest(body) != expected:
        raise ValueError("source packet checksum")
    if (
        packet.get("complete") is not True
        or packet.get("profile_hash") != profile
        or packet.get("ticker") != ticker
        or packet.get("day") != day.isoformat()
        or packet.get("kind") != kind
    ):
        raise ValueError("source packet identity")
    if _stamp(packet.get("received_at")).date() <= settings().source_end:
        raise ValueError("historical receipt was backdated")
    payload = packet.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("source packet data shape")
    return packet


def load(source: Path) -> Source:
    cfg = settings()
    original = original_report.load()
    original_hash = original.get("report_hash")
    if not isinstance(original_hash, str) or not original_hash:
        raise ValueError("original report hash absent")
    root = _safe_root(source)
    state: object = json.loads(_file(root, "state.json").read_text())
    if (
        not isinstance(state, dict)
        or digest(state) != original.get("source_state_hash")
        or state.get("status") != "complete"
    ):
        raise ValueError("capture state identity")
    profile = state.get("profile")
    if not isinstance(profile, dict) or digest(profile) != state.get("profile_hash"):
        raise ValueError("capture profile checksum")
    if (
        profile.get("start") != cfg.source_start.isoformat()
        or profile.get("end") != cfg.source_end.isoformat()
    ):
        raise ValueError("source window differs from registration")
    symbols, benchmark = profile.get("tickers"), profile.get("benchmark")
    if (
        not isinstance(symbols, list)
        or not symbols
        or not isinstance(benchmark, str)
        or any(
            not isinstance(t, str) or not re.fullmatch(r"[A-Z][A-Z0-9.\-]*", t)
            for t in [*symbols, benchmark]
        )
        or len(set(symbols)) != len(symbols)
        or benchmark in symbols
    ):
        raise ValueError("capture ticker roster invalid")
    names: tuple[str, ...] = (*symbols, benchmark)
    cal = load_live_alpha_settings().calendar
    zone = ZoneInfo(cal.timezone)
    sessions = []
    day = cfg.source_start
    while day <= cfg.source_end:
        valid = is_session_day(day, cal)
        if valid is None:
            raise ValueError("source calendar unknown")
        if valid:
            sessions.append(day)
        day += timedelta(days=1)
    if (
        len(sessions) != cfg.source_sessions
        or original.get("sessions") != cfg.source_sessions
        or original.get("warmup_sessions") != cfg.warmup_sessions
        or original.get("evaluation_sessions") != cfg.evaluation_sessions
        or len(sessions[cfg.warmup_sessions :]) != cfg.evaluation_sessions
    ):
        raise ValueError("source session support differs from frozen report")
    evaluation = tuple(sessions[cfg.warmup_sessions :])
    manifest = original.get("source_packet_hashes")
    if not isinstance(manifest, dict):
        raise ValueError("source packet manifest absent")
    expected = {
        f"{d}/{ticker}-{kind}.json.gz": (ticker, d, kind)
        for d in sessions
        for ticker in names
        for kind in ("minute", "flow")
        if kind == "minute" or ticker != benchmark
    }
    # Validate ALL names first. A malicious extra reference is rejected without
    # opening any packet, especially daily, reserved, traversal or later labels.
    for relative, checksum in manifest.items():
        if not isinstance(relative, str) or not isinstance(checksum, str):
            raise ValueError("source manifest entry invalid")
        pure = PurePosixPath(relative)
        if (
            pure.is_absolute()
            or "\\" in relative
            or len(pure.parts) != 2
            or any(p in (".", "..") for p in pure.parts)
        ):
            raise ValueError("unsafe source packet path")
        try:
            stamp = date.fromisoformat(pure.parts[0])
        except ValueError:
            raise ValueError("source path has no exact session") from None
        if not cfg.source_start <= stamp <= cfg.source_end or relative not in expected:
            raise ValueError("unregistered source packet path")
        if len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum):
            raise ValueError("source packet hash invalid")
    if set(manifest) != set(expected):
        raise ValueError("source manifest incomplete")
    paths = {relative: _file(root, relative) for relative in sorted(manifest)}
    rows = original.get("rows")
    if not isinstance(rows, list):
        raise ValueError("original ledger absent")
    signals = []
    identities: set[tuple[str, str, date]] = set()
    for arm in cfg.arms:
        originals = [
            r
            for r in rows
            if isinstance(r, dict)
            and r.get("arm") == arm
            and r.get("bps_per_side") == cfg.primary_cost_bps_per_side
        ]
        if len(originals) != 1:
            raise ValueError("original arm/cost is not unique")
        row = originals[0]
        ledger = row.get("ledger")
        if (
            not isinstance(ledger, list)
            or row.get("selected") != len(ledger)
            or row.get("resolved") != len(ledger)
            or row.get("unresolved") != 0
            or row.get("missing") != []
        ):
            raise ValueError("original selection unresolved")
        for unit in ledger:
            if not isinstance(unit, dict) or unit.get("ticker") not in symbols:
                raise ValueError("original signal ticker invalid")
            at = _stamp(unit.get("signal_at"))
            ticker = unit["ticker"]
            try:
                d = date.fromisoformat(unit["day"])
            except (ValueError, TypeError, KeyError):
                raise ValueError("original signal session invalid") from None
            opening = datetime.combine(d, cal.regular_open, zone).astimezone(UTC)
            closing = datetime.combine(d, close_time(d, cal), zone).astimezone(UTC)
            if (
                d not in evaluation
                or at.astimezone(zone).date() != d
                or not opening <= at < closing
            ):
                raise ValueError("original signal outside evaluation session")
            identity = arm, ticker, d
            if identity in identities:
                raise ValueError("duplicate original ticker/session")
            identities.add(identity)
            key = digest((original_hash, arm, ticker, d, at))
            signals.append(Signal(key, arm, ticker, d, at))
    candles: dict[tuple[str, date, datetime], Candle] = {}
    quality: Counter[str] = Counter(
        {
            name: 0
            for name in (
                "minute_packets",
                "flow_packets",
                "rth_minutes",
                "rth_expected_missing_minutes",
                "outside_calendar_rth_rows",
                "identical_duplicate_minutes",
                "negative_volume_rows",
                "unknown_volume_rows",
                "zero_volume_rows",
                "negative_volume_ticker_dates",
            )
        }
    )
    negative_dates: set[tuple[str, date]] = set()
    for relative in sorted(paths):
        ticker, d, kind = expected[relative]
        packet = _packet(
            paths[relative], manifest[relative], state["profile_hash"], ticker, d, kind
        )
        quality[kind + "_packets"] += 1
        if kind != "minute":
            continue
        opening = datetime.combine(d, cal.regular_open, zone).astimezone(UTC)
        closing = datetime.combine(d, close_time(d, cal), zone).astimezone(UTC)
        expected_minutes = int((closing - opening).total_seconds() // 60)
        present: set[datetime] = set()
        for raw in packet["payload"]["data"]:
            if not isinstance(raw, dict):
                raise ValueError("minute row shape invalid")
            at = _stamp(raw.get("start_time", raw.get("time")))
            if at.astimezone(zone).date() != d:
                raise ValueError("minute row outside source session")
            if not opening <= at < closing:
                quality["outside_calendar_rth_rows"] += 1
                continue
            if at.second or at.microsecond or raw.get("market_time") != "r":
                raise ValueError("minute regular-session identity invalid")
            values = [_number(raw.get(k)) for k in ("open", "high", "low", "close")]
            if any(v is None or v <= 0 for v in values):
                raise ValueError("minute OHLC missing/invalid")
            op, hi, lo, cl = (float(v) for v in values if v is not None)
            if not lo <= min(op, cl) <= max(op, cl) <= hi:
                raise ValueError("minute OHLC geometry invalid")
            volume = _number(raw.get("volume"))
            candle = Candle(ticker, d, at, op, hi, lo, cl, volume)
            old = candles.get((ticker, d, at))
            if old is not None:
                if old != candle:
                    raise ValueError("conflicting minute timestamp")
                quality["identical_duplicate_minutes"] += 1
                continue
            candles[ticker, d, at] = candle
            present.add(at)
            quality["rth_minutes"] += 1
            if volume is None:
                quality["unknown_volume_rows"] += 1
            elif volume < 0:
                quality["negative_volume_rows"] += 1
                quality[f"negative_volume|{ticker}|{d}"] += 1
                negative_dates.add((ticker, d))
            elif volume == 0:
                quality["zero_volume_rows"] += 1
        quality["rth_expected_missing_minutes"] += expected_minutes - len(present)
    quality["negative_volume_ticker_dates"] = len(negative_dates)
    ordered_signals = tuple(sorted(signals, key=lambda s: (s.at, s.arm, s.ticker)))
    packets = tuple(sorted(manifest.items()))
    checksum = digest(
        {
            "original_report_hash": original_hash,
            "new_profile_hash": profile_hash(),
            "signals": [asdict(s) for s in ordered_signals],
            "packet_hashes": packets,
        }
    )
    return Source(
        tuple(sessions),
        evaluation,
        ordered_signals,
        tuple(sorted(candles.values(), key=lambda c: (c.at, c.ticker))),
        checksum,
        original_hash,
        packets,
        tuple(sorted(quality.items())),
    )
