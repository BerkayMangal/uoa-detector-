"""Phase 5.2.B-fix6: a T+1 confirmation that can no longer resolve is retired.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §4.4 ("T+1 open-interest
confirmation ... <= 60" requests a day) and §6 B4 (the tri-state reading);
§9 (the Açık pozisyon row maps an unconfirmed reading to ``bilinmiyor``).

Review finding RB-01: a pending row first attempted more than
``HISTORIC_LIMIT`` sessions after its print can never resolve - the ``historic``
window no longer contains the print's session - yet nothing retired it. It stayed
``bekliyor`` and spent one UW request every pre-market until the contract expired
(measured: 28 requests over four weeks for one LEAP row, about 316 more to come),
while the row read ``henüz doğrulanmadı`` forever.

Pins:
  - a pending row still inside the historic window is queried as before;
  - a row past it is retired to the terminal status without a request;
  - the retired row never costs a request again, and it renders an honest
    permanent unknown instead of an eternal "not yet";
  - the terminal status carries the cutoffs and the board profile hash it was
    decided under, like every other final status;
  - its label is frozen and clean, and the evidence family reads bilinmiyor.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from webapp.board import oi_confirm as oc
from webapp.board.db import make_engine, session_factory
from webapp.board.honesty import ensure_clean
from webapp.board.settings import BoardSettings, load_board_settings

_REPO = Path(__file__).resolve().parents[2]
_LEAP = "SPY271217C00700000"  # expires 2027-12-17: it stays in scope for over a year
_TRADE_DATE = date(2026, 9, 15)  # a Tuesday


@pytest.fixture
def settings() -> BoardSettings:
    return load_board_settings(_REPO / "profiles" / "board_v1.yaml")


@pytest.fixture
def sessions(tmp_path: Path) -> Any:
    engine = make_engine(f"sqlite:///{tmp_path / 'oi.db'}")
    oc.ensure_oi_confirm_tables(engine)
    return session_factory(engine)


class _Client:
    """Answers ``historic`` with the five sessions ending on ``today``."""

    def __init__(self, today: date) -> None:
        self.calls: list[str] = []
        self.today = today

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        del params, method
        self.calls.append(path)
        days: list[date] = []
        day = self.today
        while len(days) < oc.HISTORIC_LIMIT:
            if day.weekday() < 5:
                days.append(day)
            day -= timedelta(days=1)
        return {"chains": [
            {"date": d.isoformat(), "open_interest": 500, "volume": 10} for d in days
        ]}


def _at(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(hour=11, minute=15)


def _flag() -> oc.FlaggedContract:
    return oc.FlaggedContract(
        option_symbol=_LEAP, ticker="SPY", trade_date=_TRADE_DATE, flagged_size=100,
    )


async def _run(
    sessions: Any, settings: BoardSettings, flagged: list[oc.FlaggedContract], day: date,
) -> tuple[oc.OiConfirmReport, _Client]:
    client = _Client(day)
    report = await oc.confirm_open_interest(
        client, sessions, flagged=flagged, settings=settings, now=_at(day),  # type: ignore[arg-type]
    )
    return report, client


def _view(sessions: Any) -> oc.OiConfirmView:
    with sessions() as session:
        view = oc.load_oi_confirm(session, _LEAP, _TRADE_DATE)
    assert view is not None
    return view


def test_sessions_since_counts_weekdays_only() -> None:
    assert oc.sessions_since(_TRADE_DATE, _TRADE_DATE) == 0
    assert oc.sessions_since(_TRADE_DATE, date(2026, 9, 16)) == 1
    assert oc.sessions_since(_TRADE_DATE, date(2026, 9, 21)) == 4  # a weekend in between
    assert oc.sessions_since(_TRADE_DATE, date(2026, 9, 22)) == 5


async def test_a_row_inside_the_historic_window_is_still_queried(
    sessions: Any, settings: BoardSettings,
) -> None:
    cutoff = settings.opening_closing.max_confirm_age_sessions
    assert cutoff == oc.HISTORIC_LIMIT  # the window the request itself asks for
    # Record the flag on its own session: T+1 has not been published yet, so nothing
    # is queried and the row is left pending, which is the state that used to leak.
    await _run(sessions, settings, [_flag()], _TRADE_DATE)
    # Four sessions later the print's own session is still in the five-row window.
    boundary = date(2026, 9, 21)
    assert oc.sessions_since(_TRADE_DATE, boundary) == cutoff - 1
    _report, client = await _run(sessions, settings, [], boundary)
    assert len(client.calls) == 1
    # It was queried, so it reached a final reading rather than being retired.
    assert _view(sessions).status != "kacirildi"


async def test_a_row_past_the_window_is_retired_without_a_request(
    sessions: Any, settings: BoardSettings,
) -> None:
    # Record the flag on its own session: T+1 has not been published yet, so nothing
    # is queried and the row is left pending, which is the state that used to leak.
    await _run(sessions, settings, [_flag()], _TRADE_DATE)
    past = date(2026, 9, 22)  # five sessions on: the print's session has rolled out
    report, client = await _run(sessions, settings, [], past)
    assert client.calls == []
    assert report.retired == ((_LEAP, _TRADE_DATE),)
    view = _view(sessions)
    assert view.status == "kacirildi"
    assert view.final is True
    assert view.label == "doğrulanamadı (T+1 verisi penceresi kapandı)"
    assert view.evidence_state == "bilinmiyor"
    assert view.resolved_at is not None


async def test_the_retired_row_never_costs_another_request(
    sessions: Any, settings: BoardSettings,
) -> None:
    # Record the flag on its own session: T+1 has not been published yet, so nothing
    # is queried and the row is left pending, which is the state that used to leak.
    await _run(sessions, settings, [_flag()], _TRADE_DATE)
    spent = 0
    for offset in range(7, 40):  # from the first session past the window
        day = _TRADE_DATE + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        _report, client = await _run(sessions, settings, [], day)
        spent += len(client.calls)
    # Before the fix this was one request per session until the 2027 expiry.
    assert spent == 0
    assert _view(sessions).status == "kacirildi"


async def test_retiring_records_the_cutoffs_it_was_decided_under(
    sessions: Any, settings: BoardSettings,
) -> None:
    # Record the flag on its own session: T+1 has not been published yet, so nothing
    # is queried and the row is left pending, which is the state that used to leak.
    await _run(sessions, settings, [_flag()], _TRADE_DATE)
    await _run(sessions, settings, [], date(2026, 9, 22))
    with sessions() as session:
        row = session.get(oc.AlfaOiConfirm, (_LEAP, _TRADE_DATE))
    assert row is not None
    assert row.board_profile_hash == settings.content_hash()
    assert row.oi_t is None and row.oi_t1 is None  # nothing was measured, nothing is claimed


def test_the_terminal_label_is_frozen_clean_and_reads_unknown() -> None:
    label = oc.STATUS_LABELS["kacirildi"]
    assert label == "doğrulanamadı (T+1 verisi penceresi kapandı)"
    assert ensure_clean(label) == label
    assert oc.BOARD_STATES["kacirildi"] == "unconfirmed"
