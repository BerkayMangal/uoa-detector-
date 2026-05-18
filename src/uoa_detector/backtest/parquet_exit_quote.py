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
  - Walking-back lookup: the latest bid at-or-before ``at``. If
    the ``at`` month has no row at-or-before ``at`` for the
    contract (it first traded after the exit instant, or did not
    trade that month at all), the search walks to earlier months
    and takes their last bid — capped at ``_MAX_WALKBACK_MONTHS``
    so a contract with no data anywhere does not scan forever.
  - Returns ``None`` when no bid exists in range — ``SimplePnL``
    then marks the trade open, exactly as with a missing dict
    quote.

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

if TYPE_CHECKING:
    from pathlib import Path

_logger = logging.getLogger(__name__)

# A contract's identity within a ticker: (option_type, strike, expiry).
_ContractKey = tuple[str, Decimal, date]

# How many months earlier than the exit month to scan before giving
# up. The v5 holding windows are well under a month; 3 is generous
# and bounds the walk for contracts with no data near the exit.
_MAX_WALKBACK_MONTHS = 3

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
        """Return the option's bid at-or-before ``at``, or ``None``."""
        key: _ContractKey = (option_type, strike, expiry.date())
        ticker_u = ticker.upper()

        year, month = at.year, at.month
        for _ in range(_MAX_WALKBACK_MONTHS + 1):
            series = self._load_month(ticker_u, year, month).get(key)
            if series:
                bid = _latest_at_or_before(series, at)
                if bid is not None:
                    return bid
            # Walk to the previous month. Once we are before ``at``'s
            # own month, "at-or-before at" means "the month's last
            # row" — _latest_at_or_before with a far-future cutoff.
            year, month = (year - 1, 12) if month == 1 else (year, month - 1)
            at = datetime.max.replace(tzinfo=at.tzinfo)
        return None

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
) -> Decimal | None:
    """Return the bid of the latest entry with ``ts <= at``, or ``None``.

    ``series`` is ascending by timestamp. Linear walk — per-contract
    series are short (one contract's trades in one month).
    """
    latest: Decimal | None = None
    for ts, bid in series:
        if ts <= at:
            latest = bid
        else:
            break
    return latest
