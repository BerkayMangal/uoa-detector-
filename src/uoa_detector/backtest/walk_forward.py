"""Walk-forward windowing — split a backtest period into N equal-time slices.

Phase 3.2.4.1: the windowing infrastructure. Each slice has an
in-sample boundary and an out-of-sample boundary; in 3.2.4 the same
frozen profile runs everywhere (in-sample = "data seen, profile NOT
tuned"). Phase 3.4+ will add real in-sample auto-tuning; the
window-shape contract here is what that future work hangs off.

Two split modes:

  * ``equal_time_slices`` — partition the [start, end] period into N
    contiguous, equal-duration slices. Used by the 4-cell runner to
    define the walk-forward windows. Last slice absorbs any rounding
    remainder (matches the metric calculator's "remainder to last"
    convention from 3.2.3.4).
  * ``equal_trade_count_slices`` — partition a sorted list of trade
    exit timestamps into N chunks of equal trade count. Used post-
    backtest by ``compute_metrics`` for walk-forward consistency.
    Already implemented inline in ``metrics.py`` — exposed as a
    helper here for cell-runner re-use (the 4-cell comparison report
    wants per-window E values).

decision (in-sample placeholder):
  Phase 3.2.4 ships a ``no_op_tuner`` callable that takes a window's
  in-sample data and returns the input profile unchanged. The cell
  runner accepts a ``Tuner`` Protocol so 3.4+ can drop in a real
  auto-tuner without touching the windowing code. This keeps 3.2.4's
  surface honest: the data flow from windowing → tuning → out-of-
  sample evaluation is wired, even though tuning is a no-op today.

decision (slice boundaries closed on left, open on right):
  ``[start_i, start_{i+1})`` for slice i, with the last slice closed
  on both ends. Standard half-open convention; avoids double-counting
  events on slice boundaries. Documented in the WalkForwardWindow
  docstring and exercised by test_window_boundaries_half_open.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from uoa_detector.backtest.pnl_provider import RealizedTrade
    from uoa_detector.calibration.profile import CalibrationProfile

    Tuner = Callable[["CalibrationProfile", "WalkForwardWindow"], "CalibrationProfile"]
    """Callable type alias for tuners. Phase 3.4+ will introduce real
    auto-tuning implementations; for 3.2.4 the only one is
    ``no_op_tuner`` which returns the input profile unchanged."""


class WalkForwardWindow(BaseModel):
    """One walk-forward slice with explicit in-sample / out-of-sample boundaries.

    Boundary convention: ``[start, end)`` half-open — events with
    timestamp >= start AND timestamp < end belong to this window.
    The very last window is closed-closed so the period's end-of-day
    isn't lost.

    For Phase 3.2.4 the in-sample / out-of-sample split is symbolic:
    in-sample is "data seen, profile NOT tuned"; out-of-sample is the
    same data evaluated. When 3.4+ adds real auto-tuning, in-sample
    will become the tuning data and out-of-sample will be the
    held-out evaluation set. The boundary mathematics already
    distinguish the two — see ``in_sample_fraction``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0, description="0-based slice index.")
    start: datetime
    end: datetime
    in_sample_fraction: float = Field(
        ge=0.0, le=1.0,
        description=(
            "Fraction of the window's duration that is in-sample. "
            "1.0 = whole window in-sample (Phase 3.2.4 default — no "
            "auto-tuning); 0.5 = first half in-sample, second half "
            "out-of-sample (typical Phase 3.4+ pattern)."
        ),
    )
    is_last: bool = Field(
        description=(
            "True for the final window; its right boundary is closed "
            "rather than half-open so the period's last instant is "
            "included."
        ),
    )

    @model_validator(mode="after")
    def _validate_ordering(self) -> WalkForwardWindow:
        if self.end <= self.start:
            msg = (
                f"WalkForwardWindow #{self.index}: end ({self.end}) must "
                f"be strictly greater than start ({self.start})"
            )
            raise ValueError(msg)
        return self

    @property
    def in_sample_end(self) -> datetime:
        """The boundary between in-sample and out-of-sample within this window.

        At ``in_sample_fraction=1.0`` (Phase 3.2.4 default), this
        equals ``end`` — the entire window is in-sample.
        """
        delta_s = (self.end - self.start).total_seconds() * self.in_sample_fraction
        return self.start + timedelta(seconds=delta_s)

    def contains(self, ts: datetime) -> bool:
        """Half-open membership: ``start <= ts < end`` (last window closed)."""
        if ts < self.start:
            return False
        if self.is_last:
            return ts <= self.end
        return ts < self.end


def equal_time_slices(
    start: datetime,
    end: datetime,
    n: int,
    *,
    in_sample_fraction: float = 1.0,
) -> tuple[WalkForwardWindow, ...]:
    """Partition ``[start, end]`` into ``n`` equal-duration windows.

    The last window absorbs any rounding remainder so the union of
    windows exactly covers the period. Boundary convention:
    ``[start_i, start_{i+1})`` half-open, last window closed.

    decision: equal-time, not equal-trade-count, for the cell-runner
    walk-forward partitioning. Equal-time matches the acceptance doc
    ("default N=8 over 2 years = 3-month slices"); equal-trade-count
    is what the metric calculator's consistency formula uses
    post-hoc on realized trades. Two distinct uses, two distinct
    helpers.
    """
    if n < 1:
        msg = f"n must be >= 1, got {n}"
        raise ValueError(msg)
    if end <= start:
        msg = f"end ({end}) must be strictly greater than start ({start})"
        raise ValueError(msg)
    total_seconds = (end - start).total_seconds()
    slice_seconds = total_seconds / n

    windows: list[WalkForwardWindow] = []
    for i in range(n):
        slice_start = start + timedelta(seconds=slice_seconds * i)
        if i == n - 1:  # noqa: SIM108 — clearer as if/else than ternary
            # Last window absorbs any rounding remainder.
            slice_end = end
        else:
            slice_end = start + timedelta(seconds=slice_seconds * (i + 1))
        windows.append(
            WalkForwardWindow(
                index=i,
                start=slice_start,
                end=slice_end,
                in_sample_fraction=in_sample_fraction,
                is_last=(i == n - 1),
            ),
        )
    return tuple(windows)


def equal_trade_count_slices(
    trades: Sequence[RealizedTrade], n: int,
) -> tuple[tuple[RealizedTrade, ...], ...]:
    """Partition closed trades into ``n`` chunks of equal count.

    Trades are sorted by ``exit_ts`` ascending; chunks are
    ``floor(len(trades) / n)`` long, and the last chunk absorbs the
    remainder. Used by the 4-cell runner to extract per-window E
    values for the comparison report.

    decision: only closed trades (realized_r is not None) are
    partitioned. Open trades have no exit_ts and don't contribute to
    walk-forward consistency anyway. Caller should pre-filter if
    they want to exclude break-evens or other categories.
    """
    if n < 1:
        msg = f"n must be >= 1, got {n}"
        raise ValueError(msg)
    closed = [t for t in trades if t.exit_ts is not None and t.realized_r is not None]
    if len(closed) < n:
        msg = (
            f"need at least {n} closed trades for {n} slices, got "
            f"{len(closed)}"
        )
        raise ValueError(msg)
    closed_sorted = sorted(closed, key=lambda t: t.exit_ts or datetime.min)
    chunk_size = len(closed_sorted) // n

    chunks: list[tuple[RealizedTrade, ...]] = []
    for i in range(n):
        if i == n - 1:
            chunks.append(tuple(closed_sorted[i * chunk_size :]))
        else:
            chunks.append(tuple(closed_sorted[i * chunk_size : (i + 1) * chunk_size]))
    return tuple(chunks)


# ---------------------------------------------------------------------------
# Tuner — placeholder for Phase 3.4+ auto-tuning
# ---------------------------------------------------------------------------


def no_op_tuner(
    profile: CalibrationProfile,
    window: WalkForwardWindow,
) -> CalibrationProfile:
    """Return the input profile unchanged.

    The default Tuner for Phase 3.2.4. Documents that the windowing
    machinery exists but no actual tuning is happening yet. Phase
    3.4+ will add real auto-tuners (grid search over a parameter
    subset, Bayesian optimisation, etc.); they share the
    ``(profile, window) -> profile`` callable shape so the cell
    runner doesn't need to change.
    """
    return profile
