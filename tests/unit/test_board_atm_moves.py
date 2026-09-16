"""Phase 5.2.B2a: ATM straddle data layer (webapp/board/atm.py) and move arithmetic (moves.py).

Fixtures are trimmed from live UW responses captured 2026-09-15 (SMCI and SPY
atm-chains, SMCI expiry-breakdown).
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import inspect
from webapp.board import atm, moves
from webapp.board.atm import (
    AtmView,
    ensure_atm_tables,
    load_atm,
    load_listed_expiries,
    refresh_atm_chains,
    refresh_expiry_breakdown,
    select_atm_expiries,
)
from webapp.board.db import make_engine, session_factory
from webapp.board.honesty import ensure_clean
from webapp.board.settings import BoardSettings, load_board_settings

from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesAuthError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)

_REPO = Path(__file__).resolve().parents[2]
_NOW = datetime(2026, 9, 15, 15, 30, tzinfo=UTC)

_BREAKDOWN_SMCI: dict[str, Any] = {"data": [
    {"expires": "2026-09-18", "open_interest": 531720, "volume": 25428, "chains": 150},
    {"expires": "2026-09-25", "open_interest": 42125, "volume": 6353, "chains": 124},
    {"expires": "2026-10-02", "open_interest": 27903, "volume": 1537, "chains": 94},
    {"expires": "2026-10-16", "open_interest": 57767, "volume": 6587, "chains": 40},
    {"expires": "not-a-date", "open_interest": 1, "volume": 1, "chains": 1},
]}

_ATM_SMCI: dict[str, Any] = {"data": [
    {"option_symbol": "SMCI260918C00036500", "bid": "1.14", "ask": "1.18",
     "iv": "0.818124754691178", "stock_price": "36.695", "tape_time": "2026-09-15T15:29:58Z",
     "date": "2026-09-15", "volume": 310, "open_interest": 1027},
    {"option_symbol": "SMCI260918P00036500", "bid": "0.97", "ask": "1.02",
     "iv": "0.825296790656841", "stock_price": "36.695", "tape_time": "2026-09-15T15:29:54Z",
     "date": "2026-09-15", "volume": 536, "open_interest": 1149},
    {"option_symbol": "SMCI260925C00036500", "bid": "1.66", "ask": "1.76",
     "iv": "0.731965822765703", "stock_price": "36.695", "tape_time": "2026-09-15T15:22:04Z",
     "date": "2026-09-15", "volume": 36, "open_interest": 10},
    {"option_symbol": "SMCI260925P00036500", "bid": "1.64", "ask": "1.75",
     "iv": "0.702564000782026", "stock_price": "36.695", "tape_time": "2026-09-15T15:22:04Z",
     "date": "2026-09-15", "volume": 82, "open_interest": 142},
]}


class _FakeClient:
    """Records (path, params); answers by path, or raises a per-path error."""

    def __init__(
        self,
        responses: dict[str, dict[str, Any]] | None = None,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self._responses = responses or {}
        self._errors = errors or {}

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        del method
        self.calls.append((path, params))
        if path in self._errors:
            raise self._errors[path]
        return self._responses.get(path, {"data": []})


@pytest.fixture
def settings() -> BoardSettings:
    return load_board_settings(_REPO / "profiles" / "board_v1.yaml")


@pytest.fixture
def sessions(tmp_path: Path) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'board.db'}")
    ensure_atm_tables(engine)
    return session_factory(engine)


def _bd(ticker: str) -> str:
    return atm.EXPIRY_BREAKDOWN_PATH.format(ticker=ticker)


def _chains(ticker: str) -> str:
    return atm.ATM_CHAINS_PATH.format(ticker=ticker)


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------


def test_ensure_tables_is_idempotent_and_prefixed(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 't.db'}")
    ensure_atm_tables(engine)
    ensure_atm_tables(engine)
    names = set(inspect(engine).get_table_names())
    assert {"alfa_atm", "alfa_atm_expiry"} <= names
    assert all(n.startswith("alfa_") for n in names)


# ---------------------------------------------------------------------------
# expiry-breakdown
# ---------------------------------------------------------------------------


async def test_expiry_breakdown_stores_listed_expiries(
    sessions: Any, settings: BoardSettings,
) -> None:
    client = _FakeClient({_bd("SMCI"): _BREAKDOWN_SMCI})
    report = await refresh_expiry_breakdown(
        client, sessions, tickers=["smci", "SMCI"], settings=settings, now=_NOW,  # type: ignore[arg-type]
    )
    assert client.calls == [(_bd("SMCI"), None)]
    assert report.stored == (("SMCI", 4),)
    assert report.requests == 1
    with sessions() as s:
        assert load_listed_expiries(s, "SMCI") == (
            date(2026, 9, 18), date(2026, 9, 25), date(2026, 10, 2), date(2026, 10, 16),
        )


async def test_expiry_breakdown_replaces_the_previous_list(
    sessions: Any, settings: BoardSettings,
) -> None:
    await refresh_expiry_breakdown(
        _FakeClient({_bd("SMCI"): _BREAKDOWN_SMCI}), sessions,  # type: ignore[arg-type]
        tickers=["SMCI"], settings=settings, now=_NOW,
    )
    shorter = {"data": [{"expires": "2026-09-25", "open_interest": 1, "volume": 1, "chains": 2}]}
    await refresh_expiry_breakdown(
        _FakeClient({_bd("SMCI"): shorter}), sessions,  # type: ignore[arg-type]
        tickers=["SMCI"], settings=settings, now=_NOW,
    )
    with sessions() as s:
        assert load_listed_expiries(s, "SMCI") == (date(2026, 9, 25),)


# ---------------------------------------------------------------------------
# expiry selection
# ---------------------------------------------------------------------------


_LISTED = (date(2026, 9, 11), date(2026, 9, 18), date(2026, 9, 25), date(2026, 10, 16))


def test_select_snaps_wanted_to_listed_and_fills_nearest() -> None:
    today = date(2026, 9, 15)
    # 2026-10-14 is not listed: nearest listed is 10-16. Free slot takes the nearest (09-18).
    assert select_atm_expiries(
        _LISTED, [date(2026, 10, 14)], today=today, max_expiries=2,
    ) == (date(2026, 9, 18), date(2026, 10, 16))


def test_select_ignores_expired_and_dedupes() -> None:
    today = date(2026, 9, 15)
    assert select_atm_expiries(
        _LISTED, [date(2026, 9, 11), date(2026, 9, 18), date(2026, 9, 19)],
        today=today, max_expiries=3,
    ) == (date(2026, 9, 18), date(2026, 9, 25), date(2026, 10, 16))


def test_select_tie_goes_to_earlier_and_caps() -> None:
    today = date(2026, 9, 15)
    # 09-21 is 3 days from both 09-18 and 09-24: the tie goes to the earlier expiry.
    assert select_atm_expiries(
        (date(2026, 9, 18), date(2026, 9, 24)), [date(2026, 9, 21)], today=today, max_expiries=1,
    ) == (date(2026, 9, 18),)
    assert select_atm_expiries((), [date(2026, 9, 21)], today=today, max_expiries=3) == ()


# ---------------------------------------------------------------------------
# atm-chains
# ---------------------------------------------------------------------------


async def _seed_listed(sessions: Any, settings: BoardSettings, ticker: str = "SMCI") -> None:
    await refresh_expiry_breakdown(
        _FakeClient({_bd(ticker): _BREAKDOWN_SMCI}), sessions,  # type: ignore[arg-type]
        tickers=[ticker], settings=settings, now=_NOW,
    )


async def test_atm_chains_pairs_legs_and_parses_osi(
    sessions: Any, settings: BoardSettings,
) -> None:
    await _seed_listed(sessions, settings)
    client = _FakeClient({_chains("SMCI"): _ATM_SMCI})
    report = await refresh_atm_chains(
        client, sessions, wanted={"smci": [date(2026, 9, 18)]},  # type: ignore[arg-type]
        settings=settings, now=_NOW,
    )
    path, params = client.calls[0]
    assert path == _chains("SMCI")
    assert params == {"expirations[]": ["2026-09-18", "2026-09-25", "2026-10-02"]}
    assert report.stored_rows == 2
    assert report.missing_expiries == (("SMCI", date(2026, 10, 2)),)
    assert report.degraded == () and report.no_data == ()
    with sessions() as s:
        rows = load_atm(s, "SMCI")
    assert [r.expiry for r in rows] == [date(2026, 9, 18), date(2026, 9, 25)]
    first = rows[0]
    assert first.strike == 36.5
    assert (first.call_bid, first.call_ask, first.put_bid, first.put_ask) == (1.14, 1.18, 0.97, 1.02)
    assert first.stock_price == 36.695
    assert first.trade_date == date(2026, 9, 15)
    assert first.fetched_at.tzinfo is not None


async def test_atm_chains_upserts_and_removes_expired_rows(
    sessions: Any, settings: BoardSettings,
) -> None:
    await _seed_listed(sessions, settings)
    await refresh_atm_chains(
        _FakeClient({_chains("SMCI"): _ATM_SMCI}), sessions,  # type: ignore[arg-type]
        wanted={"SMCI": []}, settings=settings, now=_NOW,
    )
    newer = {"data": [dict(_ATM_SMCI["data"][2], bid="1.70", ask="1.80"), _ATM_SMCI["data"][3]]}
    later = datetime(2026, 9, 19, 15, 0, tzinfo=UTC)  # 09-18 has expired
    await refresh_atm_chains(
        _FakeClient({_chains("SMCI"): newer}), sessions,  # type: ignore[arg-type]
        wanted={"SMCI": []}, settings=settings, now=later,
    )
    with sessions() as s:
        rows = load_atm(s, "SMCI")
    assert [r.expiry for r in rows] == [date(2026, 9, 25)]
    assert (rows[0].call_bid, rows[0].call_ask) == (1.70, 1.80)


async def test_atm_chains_malformed_rows_are_counted_not_stored(
    sessions: Any, settings: BoardSettings,
) -> None:
    await _seed_listed(sessions, settings)
    bad = {"data": [
        {"option_symbol": "garbage", "bid": "1", "ask": "2"},
        {"option_symbol": "SMCI260918C00036500", "bid": "n/a", "ask": None, "iv": "x",
         "stock_price": "36.695", "tape_time": "bad"},
        {"option_symbol": "SMCI260918P00037000", "bid": "1", "ask": "1.1"},  # strike mismatch
        "not-a-row",
    ]}
    report = await refresh_atm_chains(
        _FakeClient({_chains("SMCI"): bad}), sessions,  # type: ignore[arg-type]
        wanted={"SMCI": []}, settings=settings, now=_NOW,
    )
    assert report.malformed_rows == 2
    with sessions() as s:
        (row,) = load_atm(s, "SMCI")
    assert row.call_bid is None and row.call_ask is None and row.put_ask is None


async def test_atm_chains_without_listed_expiries_makes_no_call(
    sessions: Any, settings: BoardSettings,
) -> None:
    client = _FakeClient()
    report = await refresh_atm_chains(
        client, sessions, wanted={"NVDA": []}, settings=settings, now=_NOW,  # type: ignore[arg-type]
    )
    assert client.calls == []
    assert report.no_listed_expiries == ("NVDA",)
    assert report.requests == 0


@pytest.mark.parametrize(
    "error",
    [
        UnusualWhalesRateLimitError("429"),
        UnusualWhalesTransientError("503"),
        CircuitBreakerOpenError("open"),
    ],
)
async def test_degraded_errors_skip_the_ticker_and_continue(
    sessions: Any, settings: BoardSettings, error: Exception,
) -> None:
    await _seed_listed(sessions, settings, "SMCI")
    await _seed_listed(sessions, settings, "SPY")
    client = _FakeClient(
        responses={_chains("SPY"): {"data": [
            dict(_ATM_SMCI["data"][0], option_symbol="SPY260918C00757000"),
        ]}},
        errors={_chains("SMCI"): error},
    )
    report = await refresh_atm_chains(
        client, sessions, wanted={"SMCI": [], "SPY": []},  # type: ignore[arg-type]
        settings=settings, now=_NOW,
    )
    assert report.degraded == ("SMCI",)
    assert report.stored_rows == 1
    assert [c[0] for c in client.calls] == [_chains("SMCI"), _chains("SPY")]


async def test_not_found_is_no_data(sessions: Any, settings: BoardSettings) -> None:
    client = _FakeClient(errors={_bd("ZZZZ"): UnusualWhalesNotFoundError("422", status_code=422)})
    report = await refresh_expiry_breakdown(
        client, sessions, tickers=["ZZZZ"], settings=settings, now=_NOW,  # type: ignore[arg-type]
    )
    assert report.no_data == ("ZZZZ",)
    assert report.degraded == ()


@pytest.mark.parametrize(
    "error",
    [UnusualWhalesDailyLimitError("daily_request_limit"), UnusualWhalesAuthError("401")],
)
async def test_daily_limit_and_auth_propagate(
    sessions: Any, settings: BoardSettings, error: Exception,
) -> None:
    client = _FakeClient(errors={_bd("SMCI"): error})
    with pytest.raises(type(error)):
        await refresh_expiry_breakdown(
            client, sessions, tickers=["SMCI", "SPY"], settings=settings, now=_NOW,  # type: ignore[arg-type]
        )
    assert len(client.calls) == 1


# ---------------------------------------------------------------------------
# moves.py
# ---------------------------------------------------------------------------


def _view(**over: Any) -> AtmView:
    base: dict[str, Any] = {
        "ticker": "SMCI", "expiry": date(2026, 9, 18), "strike": 36.5, "stock_price": 36.695,
        "call_bid": 1.14, "call_ask": 1.18, "call_iv": 0.818124754691178,
        "put_bid": 0.97, "put_ask": 1.02, "put_iv": 0.825296790656841,
        "trade_date": date(2026, 9, 15), "fetched_at": _NOW,
    }
    base.update(over)
    return AtmView(**base)


def test_required_move_call_and_put_at_ask() -> None:
    call = moves.required_move_pct(option_type="call", strike=36.5, ask=1.18, spot=36.695)
    put = moves.required_move_pct(option_type="put", strike=36.5, ask=1.02, spot=36.695)
    assert call == pytest.approx(((36.5 + 1.18) / 36.695 - 1) * 100)
    assert put == pytest.approx((1 - (36.5 - 1.02) / 36.695) * 100)
    assert moves.required_move_pct(option_type="call", strike=36.5, ask=None, spot=36.695) is None
    assert moves.required_move_pct(option_type="call", strike=36.5, ask=1.0, spot=0.0) is None


def test_expected_move_is_straddle_mid_over_spot_with_offset() -> None:
    em = moves.expected_move(atm_rows=[_view()], expiry=date(2026, 9, 18), now=_NOW)
    assert em is not None
    assert em.source == "straddle"
    assert em.same_expiry is True
    assert em.expected_move_pct == pytest.approx((1.16 + 0.995) / 36.695 * 100)
    assert em.strike_offset_pct == pytest.approx((36.5 - 36.695) / 36.695 * 100)
    # 2026-09-15 15:30Z to 2026-09-18 16:00 ET (20:00Z) = 3.1875 days.
    assert em.days_to_expiry == pytest.approx(3.1875)


def test_expected_move_uses_nearest_expiry_and_discloses_it() -> None:
    rows = [_view(), _view(expiry=date(2026, 9, 25), call_bid=1.66, call_ask=1.76,
                           put_bid=1.64, put_ask=1.75)]
    em = moves.expected_move(atm_rows=rows, expiry=date(2026, 9, 23), now=_NOW)
    assert em is not None
    assert em.atm_expiry == date(2026, 9, 25)
    assert em.same_expiry is False
    assert "en yakın ATM vadesi 25.09" in moves.move_disclosure(em)


def test_iv_fallback_when_legs_have_no_two_sided_quote() -> None:
    row = _view(call_bid=None, put_ask=None)
    em = moves.expected_move(atm_rows=[row], expiry=date(2026, 9, 18), now=_NOW)
    assert em is not None
    assert em.source == "iv_estimate"
    iv = (0.818124754691178 + 0.825296790656841) / 2
    assert em.expected_move_pct == pytest.approx(iv * math.sqrt(3.1875 / 365) * 100)


def test_iv_fallback_without_any_atm_row_needs_a_caller_iv() -> None:
    assert moves.expected_move(atm_rows=[], expiry=date(2026, 9, 18), now=_NOW) is None
    em = moves.expected_move(
        atm_rows=[], expiry=date(2026, 9, 18), now=_NOW, fallback_iv=0.5,
    )
    assert em is not None and em.source == "iv_estimate" and em.atm_expiry is None


def test_expired_contract_has_no_expected_move() -> None:
    after = datetime(2026, 9, 18, 20, 1, tzinfo=UTC)
    assert moves.days_to_expiry(date(2026, 9, 18), after) is None
    assert moves.expected_move(atm_rows=[_view()], expiry=date(2026, 9, 18), now=after) is None


def test_compare_moves_sentence_matches_contract_wording() -> None:
    cmp_ = moves.compare_moves(
        option_type="call", strike=36.5, expiry=date(2026, 9, 18), ask=1.18,
        atm_rows=[_view()], now=_NOW,
    )
    assert cmp_.text == "Başabaş için %2.7 gerekir · ATM straddle bu vadeye %5.9 fiyatlıyor"
    assert cmp_.disclosure == "ATM strike 36.5, spot 36.695, fark %-0.5"
    assert cmp_.spot == 36.695


def test_compare_moves_unknowns_are_labelled() -> None:
    cmp_ = moves.compare_moves(
        option_type="put", strike=36.5, expiry=date(2026, 9, 18), ask=1.02,
        atm_rows=[], now=_NOW, fallback_iv=0.8,
    )
    assert cmp_.required_move_pct is None
    assert cmp_.text.startswith(moves.REQUIRED_UNKNOWN)
    assert moves.IV_FALLBACK_LABEL in cmp_.text
    assert "straddle bu vadeye" not in cmp_.text.replace(moves.IV_FALLBACK_LABEL, "")


def test_every_generated_move_string_is_clean() -> None:
    texts = [
        moves.REQUIRED_TEMPLATE, moves.STRADDLE_TEMPLATE, moves.IV_FALLBACK_LABEL,
        moves.IV_FALLBACK_TEMPLATE, moves.REQUIRED_UNKNOWN, moves.EXPECTED_UNKNOWN,
        moves.NEAREST_EXPIRY_TEMPLATE, moves.STRIKE_OFFSET_TEMPLATE,
    ]
    rows = [_view(), _view(expiry=date(2026, 9, 25))]
    for expiry in (date(2026, 9, 18), date(2026, 9, 22)):
        for option_type in ("call", "put"):
            for ask in (1.0, None):
                for fallback in (None, 0.4):
                    for atm_rows in (rows, [], [_view(call_bid=None)]):
                        c = moves.compare_moves(
                            option_type=option_type, strike=36.5, expiry=expiry, ask=ask,  # type: ignore[arg-type]
                            atm_rows=atm_rows, now=_NOW, fallback_iv=fallback,
                        )
                        texts.extend([c.text, c.disclosure])
    for text in texts:
        assert ensure_clean(text) == text
    assert "olasılık" not in " ".join(texts)
