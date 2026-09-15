"""ThetaData contract enumeration.

Phase 3.3.4.5: production wiring for ``contracts_for_ticker``
that the orchestrator (3.3.4.3) consumes.

Phase 3.3.10 (2026-05-12): migrated from the retired v2 endpoint
``/v2/list/contracts/option/quote`` to v3
``/v3/option/list/contracts/quote``. The v3 endpoint:

  - Requires ``date=YYYYMMDD`` (single date, not a range) — we
    use the filter's ``as_of_date`` (default: today UTC).
  - Renames ``root`` → ``symbol``.
  - Returns ``{"response": [...]}`` (dict wrapper). Each row is
    ``{symbol, strike (float dollars), expiration (YYYY-MM-DD),
     right (CALL|PUT)}``.
  - Legacy v2 row shape (4-element array, 1/10-cent int strike,
    YYYYMMDD int expiration, C|P right) is still accepted by the
    row decoder for fixture back-compat.

The class is constructed once per script run; ``list_contracts``
is called per ticker. No caching beyond the request semaphore
already enforced by the underlying client.

Filtering knobs (optional, applied client-side):
  - ``min_dte``: drop contracts expiring sooner than ``min_dte``
    days from ``as_of_date``.
  - ``max_dte``: drop contracts expiring further than ``max_dte``.
  - ``rights``: subset of {'C', 'P'} — defaults to both.
  - ``max_contracts``: hard cap on returned count (operator
    safety net for a ticker with thousands of strikes).

decision (one endpoint call per ticker, no per-(ticker, month) call):
  ThetaData's contract list is the universe of contracts ever
  listed, NOT 'contracts active in this month'. We fetch once
  per ticker and let the orchestrator drive the month range
  per contract. The downloader then handles per-day NULL responses
  for contracts that didn't trade in the requested month.

decision (filtering at the client side, not via endpoint params):
  ThetaData's list endpoint accepts very limited query parameters.
  We pull the full list and apply DTE/right filters in-process.
  Cheaper than per-call params and lets the operator see what was
  filtered (counts logged).

decision (rights default to both calls and puts):
  Tier-2 download captures both legs. Operators who want only one
  side pass ``rights={'C'}`` explicitly.

decision (max_contracts as a hard cap, not a TODO):
  A single liquid ticker can list 5000+ contracts (years of
  expirations × dozens of strikes). Operators running a sandbox
  test should pass ``max_contracts=20`` to keep the run small.
  Default is None (no cap); production runs need the full set.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from uoa_detector.sources.thetadata.historical import ContractSpec
from uoa_detector.sources.thetadata.mapping import (
    OptionRight,
    thetadata_strike_to_dollars,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from uoa_detector.sources.thetadata.client import ThetaDataClient


_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContractListFilter:
    """Optional client-side filters for contract enumeration."""

    min_dte: int | None = None
    max_dte: int | None = None
    rights: frozenset[OptionRight] = frozenset({"C", "P"})
    max_contracts: int | None = None
    as_of_date: date | None = None  # for DTE math; defaults to today UTC


class ThetaDataContractLister:
    """Enumerate listed option contracts per ticker."""

    def __init__(
        self,
        *,
        client: ThetaDataClient,
        endpoint: str = "/v3/option/list/contracts/quote",
    ) -> None:
        self._client = client
        self._endpoint = endpoint

    async def list_contracts(
        self,
        ticker: str,
        *,
        filters: ContractListFilter | None = None,
    ) -> list[ContractSpec]:
        """Return all listed contracts for ``ticker``, post-filter.

        Raises whatever the underlying client raises on transport
        failure; the orchestrator catches and records it as a
        task-level error.
        """
        f = filters or ContractListFilter()
        as_of = f.as_of_date or datetime.now(UTC).date()
        params = {
            "symbol": ticker.upper(),
            "date": as_of.strftime("%Y%m%d"),
        }
        resp = await self._client.request_json(self._endpoint, params=params)
        rows = _decode_contract_rows(resp)
        contracts: list[ContractSpec] = []
        for row in rows:
            spec = _row_to_contract_spec(row, ticker=ticker)
            if spec is None:
                continue
            contracts.append(spec)
        return _apply_filters(contracts, f)


def _decode_contract_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Phase 3.3.10: v3 returns ``{"response": [...]}``; legacy v2
    fixtures use ``{"data": [...]}`` (list of dicts or 4-arrays).
    """
    raw = response.get("response")
    if raw is None:
        raw = response.get("data", [])
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for row in raw:
        if isinstance(row, dict):
            out.append(row)
        elif isinstance(row, list) and len(row) >= 3:
            out.append({
                "expiration": row[0],
                "strike": row[1],
                "right": row[2],
            })
    return out


def _row_to_contract_spec(
    row: dict[str, Any], *, ticker: str,
) -> ContractSpec | None:
    """Phase 3.3.10: parse v3 schema (ISO date / float dollars /
    CALL|PUT) with v2 fallback (YYYYMMDD int / 1/10-cent int / C|P).
    """
    try:
        expiry = _parse_expiration(row["expiration"])
        strike_dollars = _parse_strike(row["strike"])
        right_raw = _parse_right(row["right"])
    except (KeyError, ValueError, TypeError):
        return None
    if right_raw not in ("C", "P"):
        return None
    return ContractSpec(
        ticker=ticker.upper(),
        expiry=expiry,
        strike_dollars=strike_dollars,
        right=right_raw,  # type: ignore[arg-type]
    )


def _parse_expiration(raw: object) -> date:
    """Phase 3.3.10: prefer v3 ISO ``YYYY-MM-DD``; fall back to
    v2 ``YYYYMMDD`` (int or string).
    """
    if isinstance(raw, str) and "-" in raw:
        return date.fromisoformat(raw)
    return _parse_yyyymmdd(raw)


def _parse_strike(raw: object) -> Decimal:
    """Phase 3.3.10: prefer v3 float / string dollars; fall back to
    v2 1/10-cent integer.
    """
    if isinstance(raw, float):
        return Decimal(str(raw))
    if isinstance(raw, str):
        return Decimal(raw)
    if isinstance(raw, int):
        # v2 1/10 cent (1500000 = $150.00); v3 wouldn't ship this as int.
        return thetadata_strike_to_dollars(raw)
    msg = f"unparseable strike: {raw!r}"
    raise TypeError(msg)


def _parse_right(raw: object) -> str:
    """Phase 3.3.10: accept v3 CALL/PUT and legacy v2 C/P; normalise
    to internal canonical C/P.
    """
    s = str(raw).upper()
    if s in ("CALL", "C"):
        return "C"
    if s in ("PUT", "P"):
        return "P"
    return s  # caller's downstream check rejects anything else


def _parse_yyyymmdd(raw: object) -> date:
    """Parse 20240216 or '20240216' to date."""
    if isinstance(raw, int):
        s = str(raw)
    elif isinstance(raw, str):
        s = raw
    else:
        msg = f"unparseable expiration: {raw!r}"
        raise TypeError(msg)
    if len(s) != 8:
        msg = f"expiration not YYYYMMDD: {s!r}"
        raise ValueError(msg)
    return date(int(s[:4]), int(s[4:6]), int(s[6:8]))


def _apply_filters(
    contracts: Iterable[ContractSpec], filters: ContractListFilter,
) -> list[ContractSpec]:
    """Apply rights / DTE / cap filters to a contract list."""
    as_of = filters.as_of_date or datetime.now(UTC).date()
    out: list[ContractSpec] = []
    for c in contracts:
        if c.right not in filters.rights:
            continue
        dte = (c.expiry - as_of).days
        if filters.min_dte is not None and dte < filters.min_dte:
            continue
        if filters.max_dte is not None and dte > filters.max_dte:
            continue
        out.append(c)
    if filters.max_contracts is not None and len(out) > filters.max_contracts:
        # Sort deterministically (by expiry, strike, right) before truncation
        out.sort(key=lambda c: (c.expiry, c.strike_dollars, c.right))
        # Make sure max_contracts is treated as int even if None slipped through
        out = out[: int(filters.max_contracts)]
    # Decimal isn't hashable across all Decimal objects of same value
    # without normalisation; assume callers don't expect a stable order
    # beyond truncation.
    return out


# Required imports surfaced for callers that want to construct
# their own ContractListFilter / ContractSpec without re-importing.
__all__ = [
    "ContractListFilter",
    "ContractSpec",
    "ThetaDataContractLister",
]
