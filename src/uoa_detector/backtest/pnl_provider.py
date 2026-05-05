"""``PnLProvider`` Protocol — the metric calculator's pricing boundary.

Phase 3.2.3 design choice: the metric calculator does NOT compute
realized R itself. It receives a ``PnLProvider`` implementation that
maps decision records to realized R values. This keeps the metric
formulas (Sharpe, expectancy, walk-forward, max DD) clean and pinnable;
it also lets us swap pricing without touching anything else.

Three implementations ship with 3.2.3:

  * ``MockPnLProvider``  — fixture-driven, used by metric-calculator
    unit tests to exercise every formula edge case.
  * ``NoOpPnLProvider``  — returns ``None`` for everything; lets us
    exercise SQLite store + metric calculator wiring end-to-end
    without taking a position on pricing.
  * ``SimplePnLProvider`` — the basic-but-consistent option-pricing
    model approved for 3.2.3 (entry=ask, exit=bid, slippage,
    holding strategy). Lives in ``simple_pnl.py``; this module is
    just the Protocol + the trivial implementations.

Trade outcome semantics:
  Each call returns a ``RealizedTrade | None``. ``None`` means "this
  decision either didn't take a position (label maps to zero max_r)
  or the holding window hasn't closed yet". A returned
  ``RealizedTrade`` has ``realized_r``, ``entry_ts``, ``exit_ts``,
  ``exit_reason`` — enough for the metric calculator to compute
  Sharpe, walk-forward windows, etc.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from uoa_detector.backtest.store import StoredSignal


ExitReason = Literal[
    "fixed_window_elapsed",
    "dte_threshold_reached",
    "exit_on_dte_lte_floor",
    "take_profit_hit",         # reserved; 3.2.3 SimplePnLProvider raises
    "stop_loss_hit",           # reserved; 3.2.3 SimplePnLProvider raises
    "holding_window_open",     # not yet exited (returned by NoOp / open positions)
]


class RealizedTrade(BaseModel):
    """One trade's realized outcome, in R-units (unitless multiples of risk).

    Phase 3.2.3 contract:
      - ``realized_r`` is None when the trade is still open (e.g.
        NoOp provider, or a holding window that has not closed by
        the end of the available data).
      - When non-None, ``realized_r`` is signed (positive = winner).
      - ``entry_ts`` always populated (== StoredSignal.timestamp).
      - ``exit_ts`` populated for closed trades; None for open.
      - ``exit_reason`` always populated; ``"holding_window_open"``
        for open trades.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    realized_r: float | None
    entry_ts: datetime
    exit_ts: datetime | None
    exit_reason: ExitReason


@runtime_checkable
class PnLProvider(Protocol):
    """Maps decision records to realized R values.

    Implementations are stateless — they may consult an internal
    quote/replay buffer, but a single ``provide()`` call must be
    deterministic given the same inputs.
    """

    def provide(self, signal: StoredSignal) -> RealizedTrade:
        """Compute the realized outcome for ``signal``.

        Implementations MUST return a ``RealizedTrade`` for every
        signal — they decide whether ``realized_r`` is None (open /
        not-yet-exited / no position taken) or a real number.

        Stateless contract: same input → same output. The metric
        calculator's reproducibility test relies on this.
        """


class NoOpPnLProvider:
    """Always returns ``realized_r=None``, ``exit_reason='holding_window_open'``.

    Useful as the placeholder PnL provider in:
      - end-to-end SQLite store + metric calculator wiring tests
        where the focus is plumbing, not pricing
      - early-stage backtests where we want to verify the data flow
        before turning on real PnL math
    """

    def provide(self, signal: StoredSignal) -> RealizedTrade:
        return RealizedTrade(
            event_id=signal.event_id or "",
            realized_r=None,
            entry_ts=signal.timestamp,
            exit_ts=None,
            exit_reason="holding_window_open",
        )


class MockPnLProvider:
    """Fixture-driven: returns whatever was registered per event_id.

    Used by metric-calculator unit tests to fabricate specific
    outcome streams (3 winners + 7 losers, equal-trade-count walk-
    forward partitions, exact Sharpe inputs, etc.).

    Construction: pass a dict mapping ``event_id -> RealizedTrade``.
    Unknown event_ids return a default open-position trade.
    """

    def __init__(self, fixtures: dict[str, RealizedTrade]) -> None:
        self._fixtures = dict(fixtures)

    def provide(self, signal: StoredSignal) -> RealizedTrade:
        eid = signal.event_id or ""
        if eid in self._fixtures:
            return self._fixtures[eid]
        return RealizedTrade(
            event_id=eid,
            realized_r=None,
            entry_ts=signal.timestamp,
            exit_ts=None,
            exit_reason="holding_window_open",
        )
