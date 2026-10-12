"""Phase 5.27: automatic peers from stored closes (contract §3.1, §3.2)."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError
from webapp.board import peers
from webapp.board.baskets import Basket, BasketSettings, basket_lags
from webapp.board.daily_close import AlfaDailyClose, ClosePoint, ensure_daily_close_tables
from webapp.board.peers import (
    PeerSettings,
    auto_lags,
    excess_returns,
    load_peer_settings,
    select_peers,
)
from webapp.live_alpha import board as live_board

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from sqlalchemy.engine import Engine

N_DAYS = 70
END = date(2026, 9, 24)


def _weekdays(n: int, end: date = END) -> list[date]:
    out: list[date] = []
    day = end
    while len(out) < n:
        if day.weekday() < 5:
            out.append(day)
        day -= timedelta(days=1)
    return sorted(out)


DAYS = _weekdays(N_DAYS)


def _cfg(**over: object) -> PeerSettings:
    base: dict[str, object] = {
        "peers_version": "t", "corr_sessions": 60, "min_overlap": 40, "peer_count": 5,
        "min_corr": 0.5, "min_peers": 3, "market_proxy": "SPY",
    }
    base.update(over)
    return PeerSettings.model_validate(base)


def _basket_cfg() -> BasketSettings:
    return BasketSettings.model_validate({
        "basket_version": "t", "window_sessions": 5, "min_members_with_data": 3,
        "lag_flag_pct": 3.0, "min_basket_move_pct": 2.0, "baskets": [],
    })


def _closes_from(returns: Sequence[float], days: Sequence[date] = DAYS, start: float = 100.0) -> tuple[ClosePoint, ...]:
    px = start
    points = [ClosePoint(day=days[0], close=px)]
    for day, ret in zip(days[1:], returns, strict=True):
        px *= 1.0 + ret
        points.append(ClosePoint(day=day, close=px))
    return tuple(points)


def _world(*, lag_last: int = 0) -> dict[str, tuple[ClosePoint, ...]]:
    """SPY, a four-name group sharing one factor, and two high-beta names with no factor.

    ``lag_last`` > 0 overrides the final sessions: group name A falls 1% a day while
    B, C, D rise 0.8% a day, so A lags its peers by far more than the 3% flag and the
    peers have moved far more than the 2% flat cut.
    """
    rng = random.Random(7)
    n = N_DAYS - 1
    market = [rng.gauss(0, 0.01) for _ in range(n)]
    factor = [rng.gauss(0, 0.012) for _ in range(n)]

    def group(noise: float) -> list[float]:
        return [market[i] + factor[i] + rng.gauss(0, noise) for i in range(n)]

    out: dict[str, tuple[ClosePoint, ...]] = {"SPY": _closes_from(market)}
    members = {name: group(0.003) for name in ("A", "B", "C", "D")}
    if lag_last:
        for i in range(n - lag_last, n):
            members["A"][i] = -0.01
            for peer in ("B", "C", "D"):
                members[peer][i] = 0.008
    for name, rets in members.items():
        out[name] = _closes_from(rets)
    for name in ("E", "F"):  # high beta, no shared factor: must not be A's peers
        out[name] = _closes_from([1.5 * market[i] + rng.gauss(0, 0.004) for i in range(n)])
    return out


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_the_shipped_peers_profile_loads_with_the_contract_numbers() -> None:
    cfg = load_peer_settings()
    assert (cfg.corr_sessions, cfg.min_overlap, cfg.peer_count, cfg.min_corr, cfg.min_peers) == (60, 40, 5, 0.5, 3)
    assert cfg.market_proxy == "SPY" and cfg.peers_version == "peers_v1"


@pytest.mark.parametrize("over", [
    {"min_overlap": 61}, {"min_peers": 6}, {"min_corr": 1.0}, {"min_corr": 0.0}, {"extra_key": 1},
])
def test_the_peers_profile_rejects_inconsistent_values(over: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _cfg(**over)


def test_a_non_mapping_peers_profile_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "p.yaml"
    path.write_text("- not\n- a mapping\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a mapping"):
        load_peer_settings(path)


# ---------------------------------------------------------------------------
# Excess returns and peer selection
# ---------------------------------------------------------------------------


def test_without_the_proxy_there_is_no_market_to_subtract_and_no_peers() -> None:
    world = _world()
    del world["SPY"]
    assert excess_returns(world, _cfg()) == {}
    assert select_peers("A", excess_returns(world, _cfg()), _cfg()) == ()


def test_a_hole_in_a_close_removes_both_returns_it_touches() -> None:
    world = _world()
    full = excess_returns(world, _cfg())["B"]
    holed = dict(world)
    holed["B"] = tuple(p for p in world["B"] if p.day != DAYS[40])
    cut = excess_returns(holed, _cfg())["B"]
    assert len(full) - len(cut) == 2  # the day itself and the day after, never bridged
    assert DAYS[40] not in cut and DAYS[41] not in cut


def test_peers_are_the_names_that_share_the_factor_and_not_the_market() -> None:
    cfg = _cfg()
    found = select_peers("A", excess_returns(_world(), cfg), cfg)
    assert set(found) == {"B", "C", "D"}
    assert "A" not in found  # self is never its own peer
    assert "E" not in found and "F" not in found  # high beta alone is not a peer


def test_a_name_with_too_little_history_cannot_be_a_peer() -> None:
    world = _world()
    world["B"] = world["B"][-30:]  # 30 sessions: under min_overlap
    cfg = _cfg(min_peers=3)
    assert select_peers("A", excess_returns(world, cfg), cfg) == ()  # only C, D left: under min_peers


def test_fewer_than_min_peers_qualifying_names_gives_no_peers() -> None:
    cfg = _cfg(min_corr=0.99)
    assert select_peers("A", excess_returns(_world(), cfg), cfg) == ()


def test_ties_are_broken_by_ticker_so_the_set_is_stable() -> None:
    base = {DAYS[i]: ((-1) ** i) * 0.01 + i * 1e-4 for i in range(1, 50)}
    excess = {"X": dict(base), "ZED": dict(base), "ABE": dict(base), "MID": dict(base)}
    assert select_peers("X", excess, _cfg(peer_count=3)) == ("ABE", "MID", "ZED")


def test_peer_count_caps_the_set() -> None:
    base = {DAYS[i]: ((-1) ** i) * 0.01 + i * 1e-4 for i in range(1, 50)}
    excess = {name: dict(base) for name in ("X", "P1", "P2", "P3", "P4", "P5", "P6")}
    assert select_peers("X", excess, _cfg(peer_count=5)) == ("P1", "P2", "P3", "P4", "P5")


def test_a_flat_series_has_no_correlation_and_is_never_a_peer() -> None:
    base = {DAYS[i]: ((-1) ** i) * 0.01 for i in range(1, 50)}
    flat = {DAYS[i]: 0.0 for i in range(1, 50)}
    assert select_peers("X", {"X": base, "FLAT": flat, "A": base, "B": base, "C": base}, _cfg()) == ("A", "B", "C")


# ---------------------------------------------------------------------------
# The reading
# ---------------------------------------------------------------------------


def test_a_lagging_name_is_measured_against_its_automatic_peers() -> None:
    world = _world(lag_last=5)
    lags = auto_lags(["A"], world, _cfg(), _basket_cfg())
    a = lags["A"]
    assert a.state == "measured" and a.source == "auto" and a.basket_id == "auto"
    assert set(a.peers) == {"B", "C", "D"}
    assert a.lagging and not a.leading
    assert a.residual_pct is not None and a.residual_pct < -3.0
    # The arithmetic is the 5.26 arithmetic, not a second copy of it.
    direct = basket_lags(Basket(id="x", name="x", members=("A", *a.peers)), world, _basket_cfg())["A"]
    assert a.residual_pct == pytest.approx(direct.residual_pct)
    assert a.members_priced == 4 and a.members_total == 4


def test_a_name_the_closes_cannot_place_answers_no_peers_with_no_number() -> None:
    lags = auto_lags(["NEWCO", "A"], _world(), _cfg(), _basket_cfg())
    new = lags["NEWCO"]
    assert new.state == "no_peers" and new.residual_pct is None and new.source == "auto"
    assert lags["A"].state in {"measured", "flat_basket"}


def test_duplicates_and_blanks_in_the_ticker_list_are_ignored() -> None:
    lags = auto_lags(["a", "A", " ", ""], _world(), _cfg(), _basket_cfg())
    assert list(lags) == ["A"]


def test_the_cell_text_for_automatic_peers_names_them_and_never_advises() -> None:
    from webapp.board.honesty import forbidden_words

    a = auto_lags(["A"], _world(lag_last=5), _cfg(), _basket_cfg())["A"]
    text, tone = live_board.basket_text(a)
    assert text is not None and tone == "amber"
    assert text.startswith("geride: akranlarına göre %-")
    assert "akranlar " in text and "grupta" in text and "4/4 isim fiyatlı" in text
    assert forbidden_words(text) == []
    assert live_board.basket_text(auto_lags(["NEWCO"], _world(), _cfg(), _basket_cfg())["NEWCO"])[0] == (
        "akran bulunamadı · yeterince benzer hareket eden isim yok"
    )


# ---------------------------------------------------------------------------
# The closes universe
# ---------------------------------------------------------------------------


def test_the_universe_loader_returns_closes_on_or_after_the_cutoff(tmp_path: Path) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine: Engine = create_engine(f"sqlite:///{tmp_path / 'u.db'}")
    ensure_daily_close_tables(engine)
    stamp = datetime(2026, 9, 24, 21, 0, tzinfo=UTC)
    with Session(engine) as s:
        for ticker in ("AAA", "BBB"):
            for offset in range(4):
                s.add(AlfaDailyClose(ticker=ticker, day=date(2026, 9, 1) + timedelta(days=offset), close=10.0 + offset, fetched_at=stamp))
        s.commit()
    got: Mapping[str, tuple[ClosePoint, ...]] = peers.load_universe_closes(engine, since=date(2026, 9, 3))
    assert sorted(got) == ["AAA", "BBB"]
    assert [p.day for p in got["AAA"]] == [date(2026, 9, 3), date(2026, 9, 4)]
    assert peers.load_universe_closes(engine, since=date(2027, 1, 1)) == {}
