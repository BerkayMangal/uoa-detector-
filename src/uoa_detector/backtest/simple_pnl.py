"""``SimplePnLProvider`` — basic-but-consistent option PnL.

Phase 3.2.3.3: the realistic-enough placeholder PnL provider. Crude
on purpose — the goal is consistency (if the edge is real, it shows
up here; if it doesn't, a more sophisticated pricer is unlikely to
rescue it). Real pricing — Black-Scholes, take-profit logic,
volatility surface evolution — is Phase 3.4 territory.

Pricing model (per acceptance doc 3.2.3):
  - **Entry price**: option ``ask`` at the decision's timestamp,
    taken from the StoredSignal itself.
  - **Exit price**: option ``bid`` at the holding-window close,
    looked up via an ``ExitQuoteProvider`` (Protocol; the test fixture
    impl returns a dict-driven mock quote, the Phase 3.3+ replay impl
    would consult the parquet stream).
  - **Slippage**: ``profile.backtest.slippage_pct`` of entry premium,
    applied as a haircut to realized PnL. Default 2%.

Holding strategies (``profile.backtest.holding_strategy``):
  - ``fixed_window``: close at ``entry_ts + holding_window_days``
    walltime, OR when DTE ≤ ``exit_on_dte_lte`` (whichever first).
  - ``dte_based``: close when DTE drops to
    ``dte_based_close_threshold``, OR ``exit_on_dte_lte`` floor
    (whichever first).
  - ``take_profit_or_stop``: 3.2.3 raises ``NotImplementedError`` with
    a clear "Phase 3.4" message. The strategy is reserved in the
    schema so profiles can mention it before the implementation
    lands.

Realized R formula:
  Per-contract PnL = (exit_bid - entry_ask) - slippage_amount.
  Slippage amount = entry_ask * slippage_pct.
  Realized R = per-contract PnL / (entry_ask * max_r_per_R_unit).

  decision: ``max_r_per_R_unit = entry_ask`` — i.e. one R unit ==
  the entry premium. This makes "I lost 1R" mean "I lost the
  premium I paid". When the size is fractional (max_r=0.5 for a
  STANDARD_UOA bucket), realized R is scaled by max_r.
  Phase 3.4's real PnL provider may redefine R-units (e.g. tied to
  initial vega exposure or kelly fraction); for 3.2.3 the simple
  premium-equals-R definition is sufficient and matches how Phase 1
  spec discusses risk magnitudes.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from uoa_detector.backtest.pnl_provider import RealizedTrade

if TYPE_CHECKING:
    from uoa_detector.backtest.store import StoredSignal
    from uoa_detector.calibration.profile import BacktestConfig


@runtime_checkable
class ExitQuoteProvider(Protocol):
    """Looks up the bid quote for an option at a given exit timestamp.

    The metric calculator + SimplePnLProvider need exit prices for
    closed positions. In a real backtest, this is fed by the replay
    stream (Phase 3.3 will wire one); in tests it's a mock that
    returns dict-driven values.

    Returns ``None`` when no quote is available (e.g. exit timestamp
    falls outside the data window, or the option is too illiquid to
    have a quoted bid). The provider then marks the trade as open
    (realized_r=None, exit_reason='holding_window_open') and the
    metric calculator excludes it from win/loss tallies.
    """

    def get_bid(
        self,
        *,
        ticker: str,
        strike: Decimal,
        expiry: datetime,
        option_type: str,
        at: datetime,
    ) -> Decimal | None:
        """Return the option bid at ``at``, or None if unavailable."""


class DictExitQuoteProvider:
    """Test-friendly ExitQuoteProvider: returns quotes from a dict.

    Key format: ``(ticker, option_type, strike, expiry_iso)`` → list
    of ``(timestamp, bid)`` pairs sorted ascending. ``get_bid`` does a
    walking-back lookup: returns the latest bid at-or-before ``at``,
    or None if the dict has nothing earlier than ``at``.

    Built in tests by constructing a small price path per option.
    """

    def __init__(
        self,
        quotes: dict[tuple[str, str, Decimal, str], list[tuple[datetime, Decimal]]],
    ) -> None:
        # Defensive copy of the inner lists so external mutation
        # doesn't perturb us.
        self._quotes = {k: list(v) for k, v in quotes.items()}
        for k, series in self._quotes.items():
            # Sanity: each price path is sorted.
            for i in range(len(series) - 1):
                if series[i][0] > series[i + 1][0]:
                    msg = (
                        f"DictExitQuoteProvider: price path for {k} not "
                        "sorted by timestamp"
                    )
                    raise ValueError(msg)

    def get_bid(
        self,
        *,
        ticker: str,
        strike: Decimal,
        expiry: datetime,
        option_type: str,
        at: datetime,
    ) -> Decimal | None:
        # Encode key the same way callers do.
        expiry_iso = expiry.date().isoformat() if hasattr(expiry, "date") else str(expiry)
        key = (ticker, option_type, strike, expiry_iso)
        series = self._quotes.get(key)
        if not series:
            return None
        # Walking-back lookup: find the latest entry <= at.
        latest: Decimal | None = None
        for ts, bid in series:
            if ts <= at:
                latest = bid
            else:
                break
        return latest


class SimplePnLProvider:
    """Basic-but-consistent option PnL.

    Constructor arguments:
      ``backtest_config`` — the profile's ``BacktestConfig`` (drives
        slippage, holding strategy, holding window, DTE floor,
        dte_based threshold).
      ``exit_quote_provider`` — supplies the bid at the exit
        timestamp.

    The provider is stateless across calls; same input → same output.
    Phase 3.2.3 reproducibility test relies on this.
    """

    def __init__(
        self,
        backtest_config: BacktestConfig,
        exit_quote_provider: ExitQuoteProvider,
    ) -> None:
        self._cfg = backtest_config
        self._quotes = exit_quote_provider

    def provide(self, signal: StoredSignal) -> RealizedTrade:
        # Decisions that didn't take a position (max_r == 0) round-trip
        # as open trades — they don't contribute to win/loss tallies.
        if signal.max_r == 0.0:
            return self._open_trade(signal)

        # Compute scheduled exit time per holding strategy.
        exit_plan = self._compute_exit_plan(signal)
        if exit_plan is None:
            # Strategy not implemented yet (take_profit_or_stop in 3.2.3).
            # The function raised; we never reach here. Defensive return.
            return self._open_trade(signal)

        exit_ts, exit_reason = exit_plan

        # Phase 3.6 leak fix: a signal already at/inside the DTE floor has
        # no forward holding window — its scheduled exit lands at or before
        # entry. Realizing it would price the exit off a pre-entry quote
        # (``get_bid`` walks back to the latest bid ≤ exit_ts), i.e.
        # look-ahead: a negative holding period and a fabricated PnL. Keep
        # such a signal open/un-realized instead of inventing a past exit.
        if exit_ts <= signal.timestamp:
            return self._open_trade(signal)

        # Look up the exit bid. None → mark as open (data not available).
        exit_bid = self._quotes.get_bid(
            ticker=signal.ticker,
            strike=signal.strike,
            expiry=datetime.combine(signal.expiry, datetime.min.time()),
            option_type=signal.option_type,
            at=exit_ts,
        )
        if exit_bid is None:
            return self._open_trade(signal)

        # PnL math. Entry was the ask the operator paid.
        entry_ask = signal.option_price  # the StoredSignal carries the
        # opportunity-cost trade price; for simple-PnL we treat this as
        # the entry ask. Phase 3.4 may distinguish ask-at-decision vs
        # actual fill more precisely.
        slippage_amount = entry_ask * Decimal(str(self._cfg.slippage_pct))
        per_contract_pnl = (exit_bid - entry_ask) - slippage_amount

        # Realized R: per-contract PnL divided by entry premium, scaled
        # by the position's max_r. With max_r=1.0 the R unit equals the
        # entry premium ($1 in the underlying option = 1R). With
        # max_r=0.5 (a smaller-bucket sized position) one R is half as
        # impactful, so realized_r doubles in absolute value when
        # expressed back in R-units. This matches the Phase 1 spec's
        # discussion of risk magnitudes.
        if entry_ask <= Decimal("0"):
            # Defensive: degenerate option price, treat as open.
            return self._open_trade(signal)
        ratio = float(per_contract_pnl / entry_ask)
        realized_r = ratio * signal.max_r

        return RealizedTrade(
            event_id=signal.event_id or "",
            realized_r=realized_r,
            entry_ts=signal.timestamp,
            exit_ts=exit_ts,
            exit_reason=exit_reason,
        )

    # ---- Helpers ------------------------------------------------------

    def _open_trade(self, signal: StoredSignal) -> RealizedTrade:
        return RealizedTrade(
            event_id=signal.event_id or "",
            realized_r=None,
            entry_ts=signal.timestamp,
            exit_ts=None,
            exit_reason="holding_window_open",
        )

    def _compute_exit_plan(
        self,
        signal: StoredSignal,
    ) -> tuple[datetime, str] | None:
        """Return (exit_ts, exit_reason) for the active holding strategy.

        Strategy dispatch:
          - fixed_window: exit at min(entry+window_days,
            dte_floor_date).
          - dte_based: exit at min(dte_threshold_date,
            dte_floor_date).
          - take_profit_or_stop: raises NotImplementedError.
        """
        strategy = self._cfg.holding_strategy
        entry_ts = signal.timestamp
        expiry = datetime.combine(signal.expiry, datetime.min.time())
        # Make expiry tz-aware (UTC) to compare with entry_ts.
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=entry_ts.tzinfo)

        # Hard safety floor: exit when DTE drops to <= exit_on_dte_lte.
        # In days from entry to that floor:
        floor_exit_ts = expiry - timedelta(days=self._cfg.exit_on_dte_lte)

        if strategy == "fixed_window":
            window_exit = entry_ts + timedelta(days=self._cfg.holding_window_days)
            if window_exit <= floor_exit_ts:
                return (window_exit, "fixed_window_elapsed")
            return (floor_exit_ts, "exit_on_dte_lte_floor")

        if strategy == "dte_based":
            tactical_exit = expiry - timedelta(
                days=self._cfg.dte_based_close_threshold,
            )
            # Choose the earlier of tactical and floor.
            if tactical_exit <= floor_exit_ts:
                return (tactical_exit, "dte_threshold_reached")
            return (floor_exit_ts, "exit_on_dte_lte_floor")

        if strategy == "take_profit_or_stop":
            msg = (
                "SimplePnLProvider does not implement "
                "holding_strategy='take_profit_or_stop'. Real PnL with "
                "intra-window stop/TP logic is Phase 3.4 territory; for "
                "3.2.3 use 'fixed_window' or 'dte_based'."
            )
            raise NotImplementedError(msg)

        msg = f"Unknown holding_strategy: {strategy!r}"
        raise ValueError(msg)
