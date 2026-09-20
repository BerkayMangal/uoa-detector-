"""``ParquetExitQuoteProvider`` — exit bids from replay parquet data.

Phase 3.5.5: the real-data ``ExitQuoteProvider`` (the Protocol +
the test-only ``DictExitQuoteProvider`` are in ``simple_pnl.py``).

``SimplePnLProvider`` needs an option's bid at the holding-window
close to realize a trade. For the real-data 4-cell backtest that
quote comes from the same bulk parquet the replay source reads —
``data/historical/{source}/{TICKER}/{YYYY-MM}.parquet``.

Lookup model:
  - The bulk parquet is sorted ascending by event timestamp
    (Phase 3.5.3.10), so a contract's rows within a month file
    are already in time order.
  - ``get_bid`` resolves the month ``at`` falls in, lazily loads
    that ``{TICKER}/{YYYY-MM}.parquet`` (only the columns it
    needs), and indexes rows by contract → ascending ``(ts, bid)``.
  - The latest bid at-or-before ``at``, **from the exit's own
    trading day**. A quote from any earlier session is refused.
  - Returns ``None`` when no such bid exists — ``SimplePnL`` then
    marks the trade open, exactly as with a missing dict quote.

Staleness (pinned decision #2 of ``docs/phase-3.5.0-acceptance.md``,
and the reason the same-day rule is here): this provider used to
walk back up to three calendar months and take whatever bid it
found. The 2026-09-19 audit proved that priced an exit off a bid
recorded eighteen days BEFORE the position was opened and booked a
fabricated winner, on the very path that produced the Phase 3.6
verdict. ``sanity_audit.check_lookahead`` could not see it, because
it compared the trade's own timestamps and never the timestamp of
the quote that priced it. That is fixed as of 2026-09-20: exit-quote
sources answer ``get_quote`` with a ``QuotedBid`` carrying the
quote's timestamp, ``RealizedTrade`` records it, and the audit fails
a trade whose quote sits outside the position or on another session.
The guarded sibling in ``simple_pnl.py`` has enforced the same-day
rule since Phase 3.5.0.1; this one matches it, and the audit now
verifies both rather than trusting either.

Memory: month tables are cached as parsed per-contract series.
Exits cluster near entries (holding window < ~1 month for the v5
profiles), so only a handful of months per ticker are ever
touched in one backtest cell.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from uoa_detector.backtest.pnl_provider import QuotedBid

if TYPE_CHECKING:
    from pathlib import Path

_logger = logging.getLogger(__name__)

# A contract's identity within a ticker: (option_type, strike, expiry).
_ContractKey = tuple[str, Decimal, date]

# Columns pulled from the parquet — the full RawPrint schema is 21
# columns; the exit-quote index needs only these five.
_NEEDED_COLUMNS = ("timestamp", "option_type", "strike", "expiry", "bid")


class ParquetExitQuoteProvider:
    """``ExitQuoteProvider`` backed by the replay parquet dataset.

    Constructor:
      ``data_dir`` — the per-source root, e.g.
        ``data/historical/bulk``; the provider expects
        ``{TICKER}/{YYYY-MM}.parquet`` directly under it (the same
        layout ``ParquetReplaySource`` reads).

    Stateless across logical calls apart from the month cache, so
    repeated ``get_bid`` calls for the same contract are cheap.
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        # (ticker, "YYYY-MM") -> {contract_key: [(ts, bid), ...ascending]}
        # A cached value of ``None`` records "file absent / empty" so a
        # missing month is not re-statted on every walk-back.
        self._cache: dict[
            tuple[str, str],
            dict[_ContractKey, list[tuple[datetime, Decimal]]] | None,
        ] = {}

    # -- ExitQuoteProvider Protocol --------------------------------------

    def get_bid(
        self,
        *,
        ticker: str,
        strike: Decimal,
        expiry: datetime,
        option_type: str,
        at: datetime,
    ) -> Decimal | None:
        """The bid alone, for readers that do not need the quote's timestamp.

        A thin view over ``get_quote`` so the two can never disagree about which
        quote is the right one. The pricing path uses ``get_quote``; this exists
        because the provider's tests read the bid directly and there is no reason to
        rewrite them.
        """
        quote = self.get_quote(
            ticker=ticker, strike=strike, expiry=expiry,
            option_type=option_type, at=at,
        )
        return None if quote is None else quote.bid

    def get_quote(
        self,
        *,
        ticker: str,
        strike: Decimal,
        expiry: datetime,
        option_type: str,
        at: datetime,
    ) -> QuotedBid | None:
        """Return the option's bid at-or-before ``at`` with its own timestamp.

        The timestamp travels with the bid so ``sanity_audit.check_lookahead`` can
        verify the exit was priced inside the position's life and session. Before
        2026-09-20 this method returned the bid alone and the audit was blind to the
        very leak this class's docstring describes.
        """
        key: _ContractKey = (option_type, strike, expiry.date())
        found = self._load_month(ticker.upper(), at.year, at.month).get(key)
        if not found:
            return None
        quote = _latest_at_or_before(found, at)
        if quote is None:
            return None
        quote_ts, bid = quote
        # The quote must belong to the exit's own trading day. Without this the
        # provider walked back up to three MONTHS and priced an exit off a bid
        # recorded before the position was even opened — b5fcd7a's leak, reopened
        # in this second provider. It was invisible to sanity_audit.check_lookahead
        # until 2026-09-20, because that check compared the trade's own timestamps
        # and never the quote's; it now receives ``exit_quote_ts`` and fails such a
        # trade, so this guard is verified rather than merely trusted.
        # The guarded sibling in simple_pnl.py has enforced this since 3.5.0.1;
        # pinned decision #2 of docs/phase-3.5.0-acceptance.md says a missing bid
        # leaves the trade OPEN and is never fabricated.
        if quote_ts.date() != at.date():
            return None
        return QuotedBid(bid=bid, quote_ts=quote_ts)

    # -- Month loading ---------------------------------------------------

    def _load_month(
        self, ticker: str, year: int, month: int,
    ) -> dict[_ContractKey, list[tuple[datetime, Decimal]]]:
        """Load + index one ticker-month, memoised. Empty dict if absent."""
        cache_key = (ticker, f"{year:04d}-{month:02d}")
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            return cached if cached is not None else {}

        path = self._data_dir / ticker / f"{year:04d}-{month:02d}.parquet"
        if not path.exists():
            self._cache[cache_key] = None
            return {}

        index = self._read_and_index(path)
        self._cache[cache_key] = index
        return index

    @staticmethod
    def _read_and_index(
        path: Path,
    ) -> dict[_ContractKey, list[tuple[datetime, Decimal]]]:
        """Read the needed columns and group rows by contract."""
        import pyarrow.parquet as pq  # heavy import, kept local

        table = pq.read_table(  # type: ignore[no-untyped-call]
            path, columns=list(_NEEDED_COLUMNS),
        )
        index: dict[_ContractKey, list[tuple[datetime, Decimal]]] = {}
        for row in table.to_pylist():
            ts = row["timestamp"]
            bid = row["bid"]
            if not isinstance(ts, datetime) or bid is None:
                continue
            key: _ContractKey = (
                str(row["option_type"]),
                Decimal(str(row["strike"])),
                _as_date(row["expiry"]),
            )
            index.setdefault(key, []).append((ts, Decimal(str(bid))))
        # The bulk parquet is timestamp-sorted, so each per-contract
        # series is already ascending; sort defensively (cheap) so the
        # walking-back lookup's bisect precondition always holds.
        for series in index.values():
            series.sort(key=lambda pair: pair[0])
        return index


def _as_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _latest_at_or_before(
    series: list[tuple[datetime, Decimal]], at: datetime,
) -> tuple[datetime, Decimal] | None:
    """Return the latest ``(ts, bid)`` with ``ts <= at``, or ``None``.

    The timestamp comes back with the bid on purpose: the caller has to be able
    to refuse a quote from another session, and it cannot do that if the lookup
    only hands it a price.

    ``series`` is ascending by timestamp. Linear walk — per-contract
    series are short (one contract's trades in one month).
    """
    latest: tuple[datetime, Decimal] | None = None
    for ts, bid in series:
        if ts <= at:
            latest = (ts, bid)
        else:
            break
    return latest
