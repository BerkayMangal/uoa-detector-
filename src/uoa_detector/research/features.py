"""Study F feature library: daily OHLCV patterns, computed without look-ahead.

Contract: ``docs/study-F-preregistration.md`` §4. The family list is frozen there
and must not grow after a result is seen.

The anti-leak property is structural, not conventional. Every function takes the
**prefix** of a series ending at the day being described, never the whole series
plus an index:

    features(history[: i + 1], market[: i + 1])

so there is no `t + 1` to reach for. ``tests`` pins this by computing the panel
twice — once from the full series, once from truncated prefixes — and asserting
every value is identical.

A window that is not long enough yields ``None``. Nothing here substitutes 0 for
a missing value: a zero is a measurement and a gap is not, and this repository has
a standing rule against letting the two read alike.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

_TRADING_DAYS = 252.0


@dataclass(frozen=True)
class Bar:
    """One regular session. Every field is required: the panel is gap-free by §3."""

    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float


def _log_return(later: float, earlier: float) -> float | None:
    if later <= 0.0 or earlier <= 0.0:
        return None
    return math.log(later / earlier)


def _ret(bars: Sequence[Bar], window: int) -> float | None:
    """Log return over ``window`` sessions ending at the last bar."""
    if len(bars) <= window:
        return None
    return _log_return(bars[-1].close, bars[-1 - window].close)


def _closes(bars: Sequence[Bar], window: int) -> list[float] | None:
    if len(bars) < window:
        return None
    return [bar.close for bar in bars[-window:]]


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _stdev(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    mu = _mean(values)
    variance = sum((v - mu) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance) if variance > 0.0 else None


def _daily_log_returns(bars: Sequence[Bar]) -> list[float]:
    out: list[float] = []
    for previous, current in pairwise(bars):
        value = _log_return(current.close, previous.close)
        if value is not None:
            out.append(value)
    return out


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    mx, my = _mean(xs), _mean(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0.0 or syy <= 0.0:
        return None
    return sxy / math.sqrt(sxx * syy)


def _slope(values: Sequence[float]) -> float | None:
    """Least-squares slope against 0..n-1, normalised by the series' own scale."""
    n = len(values)
    if n < 3:
        return None
    xs = list(range(n))
    mx, my = _mean([float(x) for x in xs]), _mean(values)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0.0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, values, strict=True))
    scale = max(abs(my), 1e-12)
    return (sxy / sxx) / scale


# ---------------------------------------------------------------------------
# Range and volatility
# ---------------------------------------------------------------------------


def true_ranges(bars: Sequence[Bar]) -> list[float]:
    """``max(h-l, |h-prev_c|, |l-prev_c|)`` per session. The first has no previous close."""
    out: list[float] = []
    for previous, current in pairwise(bars):
        out.append(max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ))
    return out


def wilder_atr(ranges: Sequence[float], period: int) -> float | None:
    """Wilder's smoothing: seed with the mean of the first ``period``, then recurse.

    Deliberately a second implementation of ``webapp.board.spot.wilder_atr``:
    ``src/`` must not import ``webapp/``. A test asserts the two agree on the same
    input, so a drift between them fails the gate rather than quietly changing what
    the research means by ATR.
    """
    if period <= 0 or len(ranges) < period:
        return None
    atr = sum(ranges[:period]) / period
    for value in ranges[period:]:
        atr = (atr * (period - 1) + value) / period
    return atr


def _parkinson(bars: Sequence[Bar], window: int) -> float | None:
    """High-low volatility estimator, annualised. Uses the range a close-only study cannot."""
    if len(bars) < window:
        return None
    total = 0.0
    for bar in bars[-window:]:
        if bar.high <= 0.0 or bar.low <= 0.0:
            return None
        total += math.log(bar.high / bar.low) ** 2
    return math.sqrt(total / (4.0 * math.log(2.0) * window) * _TRADING_DAYS)


def _garman_klass(bars: Sequence[Bar], window: int) -> float | None:
    """OHLC volatility estimator, annualised. Strictly more efficient than close-to-close."""
    if len(bars) < window:
        return None
    total = 0.0
    for bar in bars[-window:]:
        if min(bar.high, bar.low, bar.open, bar.close) <= 0.0:
            return None
        hl = math.log(bar.high / bar.low) ** 2
        co = math.log(bar.close / bar.open) ** 2
        total += 0.5 * hl - (2.0 * math.log(2.0) - 1.0) * co
    return math.sqrt(max(total, 0.0) / window * _TRADING_DAYS)


def _realised_vol(bars: Sequence[Bar], window: int) -> float | None:
    if len(bars) < window + 1:
        return None
    returns = _daily_log_returns(bars[-(window + 1):])
    sd = _stdev(returns)
    return None if sd is None else sd * math.sqrt(_TRADING_DAYS)


# ---------------------------------------------------------------------------
# The public feature map
# ---------------------------------------------------------------------------


def feature_names() -> tuple[str, ...]:
    """Every key ``features`` can return, in a stable order. Frozen by §4."""
    return (
        # 1 trend / momentum
        "ret_1", "ret_5", "ret_10", "ret_20", "ret_60", "ret_20_ex_5",
        "ma_ratio_10_50", "dist_from_252_high", "dist_from_252_low", "up_streak",
        # 2 range / volatility
        "atr_14_pct", "parkinson_10", "garman_klass_10", "vol_ratio_10_60",
        "nr7", "range_vs_atr", "inside_bar", "outside_bar",
        # 3 gap / overnight decomposition
        "gap_pct", "overnight_ret_5", "intraday_ret_5", "gap_filled",
        # 4 location within the bar
        "clv", "clv_ma_5",
        # 5 volume
        "rel_volume_20", "volume_z_20", "dollar_volume_log",
        "price_volume_corr_20", "obv_slope_20",
        # 6 mean reversion
        "rsi_14", "zscore_close_20", "bollinger_pos_20",
        # 7 cross-sectional / relative
        "ret_5_vs_spy", "beta_60", "idio_ret_5", "corr_60_spy",
    )


def _trend(bars: Sequence[Bar], out: dict[str, float | None]) -> None:
    for window in (1, 5, 10, 20, 60):
        out[f"ret_{window}"] = _ret(bars, window)
    if len(bars) > 20:
        recent, older = _ret(bars, 5), _ret(bars, 20)
        out["ret_20_ex_5"] = None if recent is None or older is None else older - recent
    else:
        out["ret_20_ex_5"] = None
    fast, slow = _closes(bars, 10), _closes(bars, 50)
    out["ma_ratio_10_50"] = (
        _log_return(_mean(fast), _mean(slow)) if fast is not None and slow is not None else None
    )
    lookback = _closes(bars, 252) or _closes(bars, len(bars))
    if lookback is not None and len(lookback) >= 60:
        high, low, last = max(lookback), min(lookback), bars[-1].close
        out["dist_from_252_high"] = _log_return(last, high)
        out["dist_from_252_low"] = _log_return(last, low)
    else:
        out["dist_from_252_high"] = None
        out["dist_from_252_low"] = None
    streak = 0
    for previous, current in zip(reversed(bars[:-1]), reversed(bars[1:]), strict=False):
        if current.close > previous.close and streak >= 0:
            streak += 1
        elif current.close < previous.close and streak <= 0:
            streak -= 1
        else:
            break
    out["up_streak"] = float(streak)


def _ranges(bars: Sequence[Bar], out: dict[str, float | None]) -> None:
    atr = wilder_atr(true_ranges(bars), 14)
    last = bars[-1]
    out["atr_14_pct"] = None if atr is None or last.close <= 0.0 else atr / last.close * 100.0
    out["parkinson_10"] = _parkinson(bars, 10)
    out["garman_klass_10"] = _garman_klass(bars, 10)
    fast_vol, slow_vol = _realised_vol(bars, 10), _realised_vol(bars, 60)
    out["vol_ratio_10_60"] = (
        None if fast_vol is None or slow_vol is None or slow_vol <= 0.0 else fast_vol / slow_vol
    )
    spans = [bar.high - bar.low for bar in bars[-7:]]
    # Strictly narrowest, not merely tied: a constant-range series would
    # otherwise flag NR7 on every session, which is not a pattern.
    out["nr7"] = float(len(spans) == 7 and all(spans[-1] < s for s in spans[:-1]))
    span = last.high - last.low
    out["range_vs_atr"] = None if atr is None or atr <= 0.0 else span / atr
    if len(bars) >= 2:
        previous = bars[-2]
        out["inside_bar"] = float(last.high <= previous.high and last.low >= previous.low)
        out["outside_bar"] = float(last.high > previous.high and last.low < previous.low)
    else:
        out["inside_bar"] = None
        out["outside_bar"] = None


def _gaps(bars: Sequence[Bar], out: dict[str, float | None]) -> None:
    if len(bars) < 2:
        out["gap_pct"] = out["gap_filled"] = None
    else:
        previous, last = bars[-2], bars[-1]
        gap = _log_return(last.open, previous.close)
        out["gap_pct"] = None if gap is None else gap * 100.0
        if gap is None:
            out["gap_filled"] = None
        elif gap > 0.0:
            out["gap_filled"] = float(last.low <= previous.close)
        elif gap < 0.0:
            out["gap_filled"] = float(last.high >= previous.close)
        else:
            out["gap_filled"] = 1.0
    overnight = 0.0
    intraday = 0.0
    usable = 0
    for previous, current in zip(bars[-6:-1], bars[-5:], strict=False):
        on = _log_return(current.open, previous.close)
        idd = _log_return(current.close, current.open)
        if on is None or idd is None:
            continue
        overnight += on
        intraday += idd
        usable += 1
    out["overnight_ret_5"] = overnight if usable == 5 else None
    out["intraday_ret_5"] = intraday if usable == 5 else None


def _location(bars: Sequence[Bar], out: dict[str, float | None]) -> None:
    def clv(bar: Bar) -> float | None:
        span = bar.high - bar.low
        if span <= 0.0:
            return None
        return ((bar.close - bar.low) - (bar.high - bar.close)) / span

    out["clv"] = clv(bars[-1])
    values = [v for v in (clv(bar) for bar in bars[-5:]) if v is not None]
    out["clv_ma_5"] = _mean(values) if len(values) == 5 else None


def _volume(bars: Sequence[Bar], out: dict[str, float | None]) -> None:
    last = bars[-1]
    recent = [bar.volume for bar in bars[-21:-1]]
    if len(recent) == 20 and _mean(recent) > 0.0:
        out["rel_volume_20"] = last.volume / _mean(recent)
        sd = _stdev(recent)
        out["volume_z_20"] = None if sd is None else (last.volume - _mean(recent)) / sd
    else:
        out["rel_volume_20"] = None
        out["volume_z_20"] = None
    dollar = last.close * last.volume
    out["dollar_volume_log"] = math.log(dollar) if dollar > 0.0 else None
    if len(bars) >= 21:
        window = bars[-21:]
        returns = _daily_log_returns(window)
        volumes = [bar.volume for bar in window[1:]]
        out["price_volume_corr_20"] = _pearson(returns, volumes)
    else:
        out["price_volume_corr_20"] = None
    if len(bars) >= 21:
        obv = 0.0
        series: list[float] = []
        for previous, current in zip(bars[-21:-1], bars[-20:], strict=False):
            if current.close > previous.close:
                obv += current.volume
            elif current.close < previous.close:
                obv -= current.volume
            series.append(obv)
        out["obv_slope_20"] = _slope(series)
    else:
        out["obv_slope_20"] = None


def _reversion(bars: Sequence[Bar], out: dict[str, float | None]) -> None:
    if len(bars) >= 15:
        gains: list[float] = []
        losses: list[float] = []
        for previous, current in zip(bars[-15:-1], bars[-14:], strict=False):
            change = current.close - previous.close
            gains.append(max(change, 0.0))
            losses.append(max(-change, 0.0))
        avg_gain, avg_loss = _mean(gains), _mean(losses)
        if avg_loss == 0.0:
            out["rsi_14"] = 100.0 if avg_gain > 0.0 else None
        else:
            out["rsi_14"] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    else:
        out["rsi_14"] = None
    window = _closes(bars, 20)
    if window is None:
        out["zscore_close_20"] = out["bollinger_pos_20"] = None
        return
    mu, sd = _mean(window), _stdev(window)
    if sd is None:
        out["zscore_close_20"] = out["bollinger_pos_20"] = None
        return
    out["zscore_close_20"] = (bars[-1].close - mu) / sd
    upper, lower = mu + 2.0 * sd, mu - 2.0 * sd
    out["bollinger_pos_20"] = (bars[-1].close - lower) / (upper - lower)


def _relative(
    bars: Sequence[Bar], market: Sequence[Bar] | None, out: dict[str, float | None],
) -> None:
    if market is None or len(market) < 61 or len(bars) < 61:
        out["ret_5_vs_spy"] = out["beta_60"] = out["idio_ret_5"] = out["corr_60_spy"] = None
        return
    own_5, market_5 = _ret(bars, 5), _ret(market, 5)
    out["ret_5_vs_spy"] = None if own_5 is None or market_5 is None else own_5 - market_5
    own = _daily_log_returns(bars[-61:])
    mkt = _daily_log_returns(market[-61:])
    if len(own) != len(mkt) or len(own) < 30:
        out["beta_60"] = out["idio_ret_5"] = out["corr_60_spy"] = None
        return
    out["corr_60_spy"] = _pearson(own, mkt)
    mm = _mean(mkt)
    sxx = sum((m - mm) ** 2 for m in mkt)
    if sxx <= 0.0:
        out["beta_60"] = out["idio_ret_5"] = None
        return
    om = _mean(own)
    beta = sum((m - mm) * (o - om) for m, o in zip(mkt, own, strict=True)) / sxx
    out["beta_60"] = beta
    out["idio_ret_5"] = (
        None if own_5 is None or market_5 is None else own_5 - beta * market_5
    )


def features(
    history: Sequence[Bar], market: Sequence[Bar] | None = None,
) -> dict[str, float | None]:
    """Every §4 feature for the LAST bar of ``history``, from ``history`` alone.

    ``market`` is the index series (SPY) truncated to the same day. Passing a
    longer market series than the history would be a leak, so the caller truncates
    both together and the panel builder is the only caller.
    """
    if not history:
        return dict.fromkeys(feature_names())
    out: dict[str, float | None] = {}
    _trend(history, out)
    _ranges(history, out)
    _gaps(history, out)
    _location(history, out)
    _volume(history, out)
    _reversion(history, out)
    _relative(history, market, out)
    missing = set(feature_names()) - set(out)
    if missing:  # a family that forgot a key it declared
        msg = f"feature map is missing declared keys: {sorted(missing)}"
        raise AssertionError(msg)
    return out
