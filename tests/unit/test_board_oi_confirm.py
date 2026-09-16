"""Phase 5.2.B4a: T+1 open-interest confirmation (webapp/board/oi_confirm.py).

Historic rows are trimmed from the 2026-09-15 probes of SPY261023C00766000 (flagged)
and SPY260918P00742000 (cross-check against oi-change).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, inspect, select
from webapp.board import oi_confirm as oc
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
_CALL = "SPY261023C00766000"
_PUT = "SPY260918P00742000"
# 2026-09-15 07:15 ET, the pre-market job time.
_NOW = datetime(2026, 9, 15, 11, 15, tzinfo=UTC)
_T = date(2026, 9, 14)

_CALL_CHAINS: dict[str, Any] = {"chains": [
    {"date": "2026-09-15", "open_interest": 118, "volume": 214},
    {"date": "2026-09-14", "open_interest": 97, "volume": 80},
    {"date": "2026-09-11", "open_interest": 35, "volume": 117},
    {"date": "2026-09-10", "open_interest": 28, "volume": 32},
    {"date": "2026-09-09", "open_interest": 9, "volume": 71},
], "etf_holdings": []}

_PUT_CHAINS: dict[str, Any] = {"chains": [
    {"date": "2026-09-15", "open_interest": 13831, "volume": 1202},
    {"date": "2026-09-14", "open_interest": 18695, "volume": 17556},
    {"date": "2026-09-11", "open_interest": 4366, "volume": 17150},
    {"date": "2026-09-10", "open_interest": "3905", "volume": 1527},
    {"date": "bad", "open_interest": 1},
]}


class _FakeClient:
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
        return self._responses.get(path, {"chains": []})


def _hist(symbol: str) -> str:
    return oc.HISTORIC_PATH.format(symbol=symbol)


@pytest.fixture
def settings() -> BoardSettings:
    return load_board_settings(_REPO / "profiles" / "board_v1.yaml")


@pytest.fixture
def sessions(tmp_path: Path) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'board.db'}")
    oc.ensure_oi_confirm_tables(engine)
    return session_factory(engine)


def _flag(symbol: str = _CALL, size: int = 40, trade_date: date = _T) -> oc.FlaggedContract:
    return oc.FlaggedContract(option_symbol=symbol, ticker="SPY", trade_date=trade_date,
                              flagged_size=size)


async def _run(
    sessions: Any, settings: BoardSettings, flagged: list[oc.FlaggedContract],
    client: _FakeClient, now: datetime = _NOW,
) -> oc.OiConfirmReport:
    return await oc.confirm_open_interest(
        client, sessions, flagged=flagged, settings=settings, now=now,  # type: ignore[arg-type]
    )


def _view(sessions: Any, symbol: str, trade_date: date = _T) -> oc.OiConfirmView:
    with sessions() as s:
        view = oc.load_oi_confirm(s, symbol, trade_date)
    assert view is not None
    return view


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


async def test_opening_confirmed_by_next_day_oi(sessions: Any, settings: BoardSettings) -> None:
    client = _FakeClient({_hist(_CALL): _CALL_CHAINS})
    # ΔOI = 118 - 97 = 21 >= 0.5 x 40.
    report = await _run(sessions, settings, [_flag(size=40)], client)
    assert client.calls == [(_hist(_CALL), {"limit": oc.HISTORIC_LIMIT})]
    assert report.recorded == 1
    assert report.resolved == ((_CALL, _T, "acilis"),)
    view = _view(sessions, _CALL)
    assert (view.oi_t, view.oi_t1, view.t1_date, view.delta_oi) == (97, 118, date(2026, 9, 15), 21)
    assert view.label == "açılış (T+1 OI teyitli)"
    assert view.evidence_state == "lehte"
    assert view.final is True
    assert view.delta_ratio == pytest.approx(21 / 40)
    assert (view.option_type, view.strike, view.expiry) == ("call", 766.0, date(2026, 10, 23))


async def test_closing_confirmed_when_oi_falls(sessions: Any, settings: BoardSettings) -> None:
    client = _FakeClient({_hist(_PUT): _PUT_CHAINS})
    # ΔOI = 13831 - 18695 = -4864 <= -0.5 x 5000.
    report = await _run(sessions, settings, [_flag(_PUT, size=5000)], client)
    assert report.resolved == ((_PUT, _T, "kapanis"),)
    view = _view(sessions, _PUT)
    assert view.label == "kapanış (T+1 OI düştü)"
    assert view.evidence_state == "aleyhte"


async def test_between_cutoffs_is_final_but_reads_not_verified(
    sessions: Any, settings: BoardSettings,
) -> None:
    client = _FakeClient({_hist(_CALL): _CALL_CHAINS})
    await _run(sessions, settings, [_flag(size=60)], client)  # 21 is between -30 and 30
    view = _view(sessions, _CALL)
    assert view.status == "arada" and view.final is True
    assert view.label == "henüz doğrulanmadı"
    assert view.evidence_state == "bilinmiyor"


def test_classify_uses_board_ratios(settings: BoardSettings) -> None:
    assert oc.classify_delta(50, 100, settings=settings) == "acilis"
    assert oc.classify_delta(49, 100, settings=settings) == "arada"
    assert oc.classify_delta(-50, 100, settings=settings) == "kapanis"
    assert oc.classify_delta(-49, 100, settings=settings) == "arada"
    assert oc.classify_delta(10, 0, settings=settings) == "arada"


# ---------------------------------------------------------------------------
# scope, timing, pending
# ---------------------------------------------------------------------------


def test_next_session_skips_weekends() -> None:
    assert oc.next_session(date(2026, 9, 11)) == date(2026, 9, 14)  # Fri -> Mon
    assert oc.next_session(date(2026, 9, 14)) == date(2026, 9, 15)
    assert oc.expires_before_next_session(date(2026, 9, 11), date(2026, 9, 11)) is True
    assert oc.expires_before_next_session(date(2026, 9, 12), date(2026, 9, 11)) is True
    assert oc.expires_before_next_session(date(2026, 9, 14), date(2026, 9, 11)) is False


async def test_contract_expiring_before_t1_is_out_of_scope_without_a_call(
    sessions: Any, settings: BoardSettings,
) -> None:
    client = _FakeClient()
    friday = date(2026, 9, 11)
    report = await _run(sessions, settings, [_flag("SPY260911C00650000", trade_date=friday)], client)
    assert client.calls == []
    assert report.out_of_scope == 1
    view = _view(sessions, "SPY260911C00650000", friday)
    assert view.status == "kapsam_disi" and view.final is True
    assert view.label == "kapsam-dışı (T+1'den önce vade)"
    assert view.evidence_state == "kapsam-dışı"


async def test_not_queried_before_the_next_session(sessions: Any, settings: BoardSettings) -> None:
    client = _FakeClient({_hist(_CALL): _CALL_CHAINS})
    same_day = datetime(2026, 9, 14, 20, 0, tzinfo=UTC)
    report = await _run(sessions, settings, [_flag()], client, now=same_day)
    assert client.calls == []
    assert report.recorded == 1 and report.resolved == ()
    assert _view(sessions, _CALL).label == "henüz doğrulanmadı"


async def test_missing_t1_row_stays_pending_and_retries(
    sessions: Any, settings: BoardSettings,
) -> None:
    only_t = {"chains": [row for row in _CALL_CHAINS["chains"] if row["date"] != "2026-09-15"]}
    first = await _run(sessions, settings, [_flag()], _FakeClient({_hist(_CALL): only_t}))
    assert first.awaiting == ((_CALL, _T),)
    view = _view(sessions, _CALL)
    assert view.status == "bekliyor" and view.final is False
    assert view.evidence_state == "bilinmiyor"
    later = datetime(2026, 9, 16, 11, 15, tzinfo=UTC)
    second = await _run(sessions, settings, [], _FakeClient({_hist(_CALL): _CALL_CHAINS}), now=later)
    assert second.resolved == ((_CALL, _T, "acilis"),)


async def test_expired_pending_rows_are_not_queried(sessions: Any, settings: BoardSettings) -> None:
    symbol = "SPY260915C00700000"  # expires 2026-09-15, T+1 of 09-14
    await _run(sessions, settings, [_flag(symbol)], _FakeClient({_hist(symbol): {"chains": []}}))
    client = _FakeClient()
    await _run(sessions, settings, [], client, now=datetime(2026, 9, 16, 11, 15, tzinfo=UTC))
    assert client.calls == []
    assert _view(sessions, symbol).status == "bekliyor"


# ---------------------------------------------------------------------------
# append-only: recorded once, advanced once
# ---------------------------------------------------------------------------


async def test_status_advances_only_once(sessions: Any, settings: BoardSettings) -> None:
    await _run(sessions, settings, [_flag(size=40)], _FakeClient({_hist(_CALL): _CALL_CHAINS}))
    reversed_oi = {"chains": [
        {"date": "2026-09-15", "open_interest": 10}, {"date": "2026-09-14", "open_interest": 97},
    ]}
    client = _FakeClient({_hist(_CALL): reversed_oi})
    report = await _run(sessions, settings, [_flag(size=40)], client)
    assert client.calls == []  # a final row is never queried again
    assert report.already_recorded == 1 and report.resolved == ()
    view = _view(sessions, _CALL)
    assert (view.status, view.oi_t1) == ("acilis", 118)


async def test_duplicate_flags_are_recorded_once_with_the_first_size(
    sessions: Any, settings: BoardSettings,
) -> None:
    client = _FakeClient({_hist(_CALL): {"chains": []}})
    report = await _run(sessions, settings, [_flag(size=40), _flag(size=999)], client)
    assert (report.recorded, report.already_recorded) == (1, 1)
    await _run(sessions, settings, [_flag(size=7)], client)
    assert _view(sessions, _CALL).flagged_size == 40
    with sessions() as s:
        assert s.scalar(select(func.count()).select_from(oc.AlfaOiConfirm)) == 1


async def test_invalid_flags_are_rejected(sessions: Any, settings: BoardSettings) -> None:
    report = await _run(sessions, settings, [
        _flag("NOT-OSI"), _flag(size=0), _flag(trade_date=date(2026, 9, 13)),  # Sunday
        oc.FlaggedContract(option_symbol=_PUT, ticker=" ", trade_date=_T, flagged_size=5),
    ], _FakeClient())
    assert len(report.rejected) == 4
    assert report.recorded == 0


def test_ensure_tables_is_idempotent(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 't.db'}")
    oc.ensure_oi_confirm_tables(engine)
    oc.ensure_oi_confirm_tables(engine)
    assert "alfa_oi_confirm" in inspect(engine).get_table_names()


# ---------------------------------------------------------------------------
# UW errors
# ---------------------------------------------------------------------------


async def test_not_found_is_no_data_and_stays_pending(sessions: Any, settings: BoardSettings) -> None:
    client = _FakeClient(errors={_hist(_CALL): UnusualWhalesNotFoundError("422", status_code=422)})
    report = await _run(sessions, settings, [_flag()], client)
    assert report.no_data == (_CALL,)
    assert _view(sessions, _CALL).status == "bekliyor"


@pytest.mark.parametrize(
    "error",
    [UnusualWhalesRateLimitError("429"), UnusualWhalesTransientError("502"),
     CircuitBreakerOpenError("open")],
)
async def test_degraded_contract_is_skipped_and_the_job_continues(
    sessions: Any, settings: BoardSettings, error: Exception,
) -> None:
    client = _FakeClient({_hist(_PUT): _PUT_CHAINS}, errors={_hist(_CALL): error})
    report = await _run(sessions, settings, [_flag(), _flag(_PUT, size=5000)], client)
    assert report.degraded == (_CALL,)
    assert report.resolved == ((_PUT, _T, "kapanis"),)


@pytest.mark.parametrize(
    "error", [UnusualWhalesDailyLimitError("daily_request_limit"), UnusualWhalesAuthError("403")],
)
async def test_daily_limit_and_auth_propagate_after_recording(
    sessions: Any, settings: BoardSettings, error: Exception,
) -> None:
    client = _FakeClient(errors={_hist(_CALL): error})
    with pytest.raises(type(error)):
        await _run(sessions, settings, [_flag()], client)
    assert _view(sessions, _CALL).status == "bekliyor"  # the flag itself was kept


# ---------------------------------------------------------------------------
# views and copy
# ---------------------------------------------------------------------------


def test_missing_row_reads_not_verified_and_unknown() -> None:
    assert oc.oi_label(None) == "henüz doğrulanmadı"
    assert oc.oi_evidence_state(None) == "bilinmiyor"


def test_status_labels_are_byte_exact_and_clean() -> None:
    assert dict(oc.STATUS_LABELS) == {
        "bekliyor": "henüz doğrulanmadı",
        "arada": "henüz doğrulanmadı",
        "acilis": "açılış (T+1 OI teyitli)",
        "kapanis": "kapanış (T+1 OI düştü)",
        "kapsam_disi": "kapsam-dışı (T+1'den önce vade)",
    }
    for text in [*oc.STATUS_LABELS.values(), oc.NO_ROW_LABEL]:
        assert ensure_clean(text) == text


async def test_load_many_views(sessions: Any, settings: BoardSettings) -> None:
    await _run(sessions, settings, [_flag(size=40), _flag(_PUT, size=5000)],
               _FakeClient({_hist(_CALL): _CALL_CHAINS, _hist(_PUT): _PUT_CHAINS}))
    with sessions() as s:
        views = oc.load_oi_confirms(s, [(_CALL, _T), (_PUT, _T), ("SPY261023C00999000", _T)])
    assert {k: v.status for k, v in views.items()} == {(_CALL, _T): "acilis", (_PUT, _T): "kapanis"}
    assert all(v.resolved_at is not None and v.resolved_at.tzinfo is UTC for v in views.values())
