"""Unit tests for the trade-journal edge metrics (Phase 4.7).

These guard the anti-self-deception math: market-neutral excess, t-stat,
significance-gated verdict, and the option P&L stats.
"""

from __future__ import annotations

from datetime import UTC, datetime

from webapp.journal import (
    TradeRow,
    _t_stat,
    _verdict,
    aggregate,
    directional_excess,
    option_pnl_usd,
    option_return_pct,
)

_T0 = datetime(2026, 6, 22, 14, 0, tzinfo=UTC)


def _trade(**over: object) -> TradeRow:
    base: dict[str, object] = {
        "id": "x",
        "created_at": _T0,
        "entry_ts": _T0,
        "ticker": "TSLA",
        "direction": "bullish",
        "instrument": "call",
        "contracts": 1.0,
        "entry_price": 3.0,
        "thesis": "",
        "status": "closed",
    }
    base.update(over)
    return TradeRow(**base)  # type: ignore[arg-type]


# ---- option P&L ----------------------------------------------------------


def test_option_pnl_long_call_profit() -> None:
    t = _trade(entry_price=3.0, exit_price=5.0, contracts=2.0)
    assert option_pnl_usd(t) == 400.0  # (5-3)*2*100
    assert option_return_pct(t) == (5.0 - 3.0) / 3.0


def test_option_pnl_total_loss() -> None:
    t = _trade(entry_price=3.0, exit_price=0.0)
    assert option_pnl_usd(t) == -300.0


def test_shares_multiplier_is_one() -> None:
    t = _trade(instrument="shares", entry_price=100.0, exit_price=110.0, contracts=10.0)
    assert option_pnl_usd(t) == 100.0  # (110-100)*10*1


def test_pnl_none_when_open() -> None:
    assert option_pnl_usd(_trade(status="open", exit_price=None)) is None


# ---- market-neutral directional excess -----------------------------------


def test_directional_excess_bullish_beats_market() -> None:
    # underlying +3%, SPY +1% -> bullish excess +2%
    t = _trade(
        direction="bullish",
        entry_underlying_px=100.0, exit_underlying_px=103.0,
        entry_spy_px=500.0, exit_spy_px=505.0,
        exit_price=4.0,
    )
    assert directional_excess(t) is not None
    assert abs(directional_excess(t) - 0.02) < 1e-9  # type: ignore[operator]


def test_directional_excess_bearish_sign_flips() -> None:
    # underlying -3%, SPY -1% -> bearish thesis was right -> +2% excess
    t = _trade(
        direction="bearish",
        entry_underlying_px=100.0, exit_underlying_px=97.0,
        entry_spy_px=500.0, exit_spy_px=495.0,
        exit_price=4.0,
    )
    assert abs(directional_excess(t) - 0.02) < 1e-9  # type: ignore[operator]


def test_directional_excess_none_without_prices() -> None:
    assert directional_excess(_trade(exit_price=4.0)) is None


# ---- t-stat --------------------------------------------------------------


def test_t_stat_none_below_two_samples() -> None:
    assert _t_stat([0.02]) is None


def test_t_stat_none_zero_variance() -> None:
    assert _t_stat([0.02, 0.02, 0.02]) is None


def test_t_stat_positive_for_consistent_positive() -> None:
    t = _t_stat([0.02, 0.025, 0.018, 0.022, 0.019])
    assert t is not None and t > 5  # tight positive cluster -> large t


# ---- verdict thresholds --------------------------------------------------


def test_verdict_building_below_min_sample() -> None:
    tier, _ = _verdict(n_closed=5, excess_n=5, mean=0.05, t=10.0)
    assert tier == "building"


def test_verdict_none_when_insignificant() -> None:
    tier, _ = _verdict(n_closed=20, excess_n=20, mean=0.01, t=1.2)
    assert tier == "none"


def test_verdict_edge_when_significant_positive() -> None:
    tier, _ = _verdict(n_closed=20, excess_n=20, mean=0.02, t=3.1)
    assert tier == "edge"


def test_verdict_negative_when_significant_negative() -> None:
    tier, _ = _verdict(n_closed=20, excess_n=20, mean=-0.02, t=-3.1)
    assert tier == "negative"


# ---- aggregate integration -----------------------------------------------


def test_aggregate_wires_pnl_and_winrate() -> None:
    trades = [
        _trade(entry_price=3.0, exit_price=5.0),   # +200
        _trade(entry_price=3.0, exit_price=1.0),   # -200
        _trade(entry_price=2.0, exit_price=4.0),   # +200
        _trade(status="open", exit_price=None),    # ignored
    ]
    stats = aggregate(trades)
    assert stats.n_closed == 3
    assert stats.pnl_total == 200.0  # 200 - 200 + 200
    assert stats.pnl_win_rate == 2 / 3
    assert stats.pnl_profit_factor == 400.0 / 200.0
    assert stats.verdict_tier == "building"  # only 3, no price data
