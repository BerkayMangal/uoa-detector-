"""Versioned history capture with verified, bounded option-chain pagination.

The v1 collector remains byte-for-byte reproducible for its sealed historical
report. New capture runs use this module and a distinct profile hash/destination.
An HTTP 200 chain page is not evidence that the entire chain was returned.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from pydantic import Field
from ruamel.yaml import YAML

from uoa_detector.live_alpha.calendar import is_session_day
from uoa_detector.live_alpha.settings import load_live_alpha_settings
from webapp.alpha_lab.flow import timestamp
from webapp.history_capture.capture import (
    ET,
    ROOT,
    CaptureStoppedError,
    Client,
    digest,
    flow,
    read,
    rows,
    seal,
)
from webapp.history_capture.capture import (
    Settings as LegacySettings,
)


class Settings(LegacySettings):
    max_chain_pages: int = Field(ge=1)
    chain_page_limit: int = Field(ge=1, le=500)


def settings() -> Settings:
    return Settings(
        **YAML(typ="safe").load((ROOT / "profiles/history_capture_v2.yaml").read_text())
    )


async def chain(client: Client, ticker: str, day: date, cfg: Settings) -> dict[str, Any]:
    """Collect zero-based pages; only exhaustion establishes completeness.

    Identical boundary rows are deduplicated. Conflicting identities, ignored
    pagination, transport/quota errors and a page cap retain an incomplete packet.
    Request metadata and payload hashes preserve evidence for every received page.
    """
    seen: dict[str, dict[str, Any]] = {}
    pages: list[dict[str, Any]] = []
    reason = "page_limit"
    complete = False
    for index in range(cfg.max_chain_pages):
        try:
            packet = await client.get(
                f"/api/stock/{ticker}/option-chains",
                {
                    "date": day.isoformat(),
                    "greeks": "true",
                    "limit": cfg.chain_page_limit,
                    "page": index,
                },
            )
            records = rows(packet)
        except CaptureStoppedError as error:
            reason = str(error)
            break
        pages.append(
            {k: v for k, v in packet.items() if k != "payload"}
            | {"row_count": len(records), "payload_hash": digest(packet["payload"])}
        )
        if len(records) > cfg.chain_page_limit:
            reason = "oversized_page"
            break
        added = 0
        invalid = False
        for row in records:
            symbol = row.get("option_symbol")
            if not isinstance(symbol, str) or not symbol:
                reason, invalid = "missing_contract_identity", True
                break
            if symbol in seen and digest(seen[symbol]) != digest(row):
                reason, invalid = "conflicting_contract_identity", True
                break
            if symbol not in seen:
                seen[symbol] = row
                added += 1
        if invalid:
            break
        # A repeated nonempty page may mean the provider ignored page=N. It is
        # never exhaustion, even if its length is below the configured limit.
        if records and not added:
            reason = "page_stall"
            break
        if len(records) < cfg.chain_page_limit:
            reason, complete = "pages_exhausted", True
            break
    return {
        "ticker": ticker,
        "day": day.isoformat(),
        "kind": "chain",
        "complete": complete,
        "stop_reason": reason,
        "date_evidence": "requested_chain_date_only_quality_B",
        "pagination": "zero_based_page_v2",
        "payload": {"data": list(seen.values())},
        "pages": pages,
        "received_at": datetime.now(UTC).isoformat(),
    }


async def capture(
    destination: Path,
    *,
    cfg: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Capture new sealed packets without mutating or trusting v1 completeness."""
    cfg = cfg or settings()
    key = os.getenv("UNUSUAL_WHALES_API_KEY")
    if not key:
        raise CaptureStoppedError("missing_credential")
    profile_hash = digest(cfg.model_dump(mode="json"))
    destination.mkdir(parents=True, exist_ok=True)
    state_path = destination / "state.json"
    previous = json.loads(state_path.read_text()) if state_path.exists() else {}
    if previous and previous.get("profile_hash") != profile_hash:
        raise CaptureStoppedError("capture_profile_mismatch_use_new_destination")
    state: dict[str, Any] = {
        "profile_hash": profile_hash,
        "profile": cfg.model_dump(mode="json"),
        "started_at": datetime.now(UTC).isoformat(),
        "calls_this_run": 0,
        "sealed": 0,
        "resumed": 0,
        "status": "running",
        "remaining": None,
        "previous_runs": [
            *previous.get("previous_runs", []),
            {k: previous.get(k) for k in (
                "started_at", "calls_this_run", "sealed", "resumed", "status", "remaining"
            )},
        ] if previous else [],
        "packets": [],
    }

    def checkpoint() -> None:
        temp = state_path.with_suffix(".part")
        temp.write_text(json.dumps(state, indent=2, default=str) + "\n")
        temp.replace(state_path)

    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer " + key},
        timeout=cfg.timeout_seconds,
        follow_redirects=False,
        transport=transport,
    ) as http:
        client = Client(http, cfg)

        async def collect(ticker: str, kind: str, day: date | None = None) -> None:
            path = (
                destination / f"{ticker}-daily.json.gz" if day is None
                else destination / day.isoformat() / f"{ticker}-{kind}.json.gz"
            )
            if path.exists():
                packet = read(path)
                if packet.get("profile_hash") != profile_hash or not packet.get("complete"):
                    raise CaptureStoppedError("resume_incomplete_or_mismatched")
                if kind == "chain" and packet.get("pagination") != "zero_based_page_v2":
                    raise CaptureStoppedError("resume_chain_pagination_unverified")
                state["resumed"] += 1
            else:
                if day is None:
                    packet = await client.get(f"/api/stock/{ticker}/ohlc/1d", {})
                    keep = []
                    for row in rows(packet):
                        at = timestamp(row.get("start_time", row.get("time")))
                        raw_day = row.get("date")
                        native_day = (
                            at.astimezone(ET).date() if at else
                            date.fromisoformat(raw_day[:10]) if isinstance(raw_day, str) else None
                        )
                        if native_day is None:
                            raise CaptureStoppedError("daily_unknown_native_date")
                        if native_day <= cfg.end:
                            keep.append(row)
                    packet["payload"] = {"data": keep}
                    packet.update(ticker=ticker, kind=kind, complete=True)
                elif kind == "flow":
                    packet = await flow(client, ticker, day)
                elif kind == "chain":
                    packet = await chain(client, ticker, day, cfg)
                else:
                    packet = await client.get(
                        f"/api/stock/{ticker}/ohlc/1m", {"date": day.isoformat()}
                    )
                    records = rows(packet)
                    stamps = [timestamp(r.get("start_time", r.get("time"))) for r in records]
                    if not records or any(
                        stamp is None or stamp.astimezone(ET).date() != day for stamp in stamps
                    ):
                        raise CaptureStoppedError("minute_date_unverified")
                    packet.update(
                        ticker=ticker, day=day.isoformat(), kind=kind, complete=True,
                        date_evidence="native_minute_times",
                    )
                seal(path, packet | {"profile_hash": profile_hash})
                state["sealed"] += 1
                if not packet["complete"]:
                    raise CaptureStoppedError("incomplete_" + kind + "_" + packet["stop_reason"])
            state["packets"].append(str(path.relative_to(destination)))
            state.update(calls_this_run=client.calls, remaining=client.remaining)
            if day is not None:
                state["last_day"] = day.isoformat()
            checkpoint()

        try:
            for ticker in (*cfg.tickers, cfg.benchmark):
                await collect(ticker, "daily")
            day = cfg.start
            cal = load_live_alpha_settings().calendar
            while day <= cfg.end:
                session = is_session_day(day, cal)
                if session is None:
                    raise CaptureStoppedError("calendar_year_unknown")
                if session:
                    for ticker in (*cfg.tickers, cfg.benchmark):
                        kinds = ("minute", "flow", "chain") if ticker in cfg.tickers else ("minute",)
                        for kind in kinds:
                            await collect(ticker, kind, day)
                day += timedelta(days=1)
            state["status"] = "complete"
        except CaptureStoppedError as error:
            state.update(
                status="stopped", reason=str(error), calls_this_run=client.calls,
                remaining=client.remaining,
            )
        finally:
            checkpoint()
    return state
