"""Phase 5.2.D-fix1: a delayed family never dates a check from a failed attempt.

Review findings FD-01 and FD-03; contract ``docs/phase-5.2-alfa-board-acceptance.md``
§2 R-UN1 (an unconfirmed state is never painted as a confirmed one) and §7.

Before this fix the bucket read the newest ATTEMPT time as if it were a check:

- a family whose source answered weeks ago and has been rate-limited since read
  ``kayıt yok — kaynak yanıt verdi, pencerede kayıt yok (son kontrol <today>)``
  with no disclosure at all (the empty branch carried no notes, so the
  staleness and truncation lines were unreachable);
- stored rows whose coverage never recorded a success read a bare
  ``son kontrol <today>``, because the staleness note was gated on a success
  time that does not exist in that case.

Both claimed a check that never reached the source. The 429 history of this
project (30k/day, 120/min) makes the first case the normal live state after a
rate-limited post-close job, not an edge case.

Pins:
  - an empty family dates ``kayıt yok`` from the last SUCCESS and discloses the
    failed newest attempt;
  - an answered-and-current empty family is unchanged (no new noise);
  - an empty family discloses the source's truncation signal;
  - stored rows with no recorded success say the check date is unknown and name
    the failed attempt, instead of dating a check from it;
  - the end-to-end path (the real post-close job: answering once, then rate
    limited) renders the honest state;
  - every generated string here passes ``ensure_clean`` (R-WD1).
"""

from __future__ import annotations

import asyncio
import string
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from webapp.board import delayed_panel as panel_module
from webapp.board.db import make_engine
from webapp.board.delayed import (
    CONGRESS_RECENT_TRADES_PATH,
    build_delayed_evidence,
    parse_congress_trades,
    run_delayed_job,
)
from webapp.board.delayed_coverage import CoverageView
from webapp.board.delayed_panel import (
    DelayedFamilyPanel,
    DelayedPanel,
    build_delayed_panel,
    load_delayed_panels,
)
from webapp.board.honesty import ensure_clean
from webapp.board.settings import load_board_settings

from uoa_detector.sources.unusual_whales.client import UnusualWhalesRateLimitError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml").delayed
_TODAY = date(2026, 9, 15)
_ANSWERED_AT = datetime(2026, 8, 1, 21, 0, tzinfo=UTC)  # the last attempt that reached the source
_ATTEMPT = datetime(2026, 9, 15, 21, 0, tzinfo=UTC)  # 17:00 ET, today's post-close attempt

_EMPTY_FROM_SUCCESS = "kayıt yok — kaynak yanıt verdi, pencerede kayıt yok (son kontrol 2026-08-01)"
_STALE_EMPTY = "son deneme (2026-09-15) başarısız; en son 2026-08-01 tarihinde doğrulandı"
_TRUNCATED = "kaynak, döndürdüğünden fazla kayıt olduğunu bildirdi; liste eksik olabilir"


def _coverage(
    *,
    status: str,
    success: datetime | None,
    attempt: datetime = _ATTEMPT,
    truncated: bool = False,
) -> dict[str, CoverageView]:
    return {
        "congress": CoverageView(
            ticker="NVDA", family="congress", last_attempt_at=attempt, last_status=status,
            last_success_at=success, truncated=truncated,
        ),
    }


def _congress_row() -> dict[str, Any]:
    return {
        "name": "Gilbert Cisneros", "ticker": "NVDA", "issuer": "undisclosed",
        "transaction_date": "2026-08-18", "txn_type": "Sell", "politician_id": "p-1",
        "amounts": "$1,001 - $15,000", "filed_at_date": "2026-09-11",
        "reporter": "Hon. Gilbert Cisneros", "member_type": "house",
    }


def _items(rows: list[dict[str, Any]]) -> tuple[Any, ...]:
    records = parse_congress_trades(rows, ticker="NVDA", today=_TODAY, settings=_SETTINGS).records
    return build_delayed_evidence("NVDA", records, (), today=_TODAY, settings=_SETTINGS).items


def _panel(items: tuple[Any, ...], coverage: dict[str, CoverageView]) -> DelayedPanel:
    return build_delayed_panel("NVDA", items, coverage, settings=_SETTINGS)


def _congress(panel: DelayedPanel) -> DelayedFamilyPanel:
    return next(f for f in panel.families if f.family == "congress")


class _Answering:
    """Every family answers, with nothing inside the window."""

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        del path, params, method
        return {"data": []}


class _RateLimited:
    """Every family is rate limited: no attempt reaches the source."""

    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
    ) -> dict[str, Any]:
        del params, method
        msg = f"429 rate limited ({path})"
        raise UnusualWhalesRateLimitError(msg)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    panel_module._tables_ready_for.clear()
    made = make_engine(f"sqlite:///{tmp_path / 'board.db'}")
    try:
        yield made
    finally:
        made.dispose()
        panel_module._tables_ready_for.clear()


# ---------------------------------------------------------------------------
# FD-01: the empty branch
# ---------------------------------------------------------------------------


def test_an_empty_family_dates_kayit_yok_from_the_last_success_not_a_failed_attempt() -> None:
    family = _congress(_panel((), _coverage(status="degraded", success=_ANSWERED_AT)))

    assert family.state == "empty"
    assert family.state_text == _EMPTY_FROM_SUCCESS
    assert "2026-09-15" not in (family.state_text or "")  # the failed attempt is not a check
    assert family.notes == (_STALE_EMPTY,)
    assert family.dimmed is True  # nothing to show is still not a clean read


def test_an_answered_and_current_empty_family_is_unchanged() -> None:
    family = _congress(_panel((), _coverage(status="no_data", success=_ATTEMPT)))

    assert family.state == "empty"
    assert family.state_text == (
        "kayıt yok — kaynak yanıt verdi, pencerede kayıt yok (son kontrol 2026-09-15)"
    )
    assert family.notes == ()  # a successful check today needs no disclosure


def test_an_empty_family_discloses_the_sources_truncation_signal() -> None:
    family = _congress(_panel((), _coverage(status="ok", success=_ATTEMPT, truncated=True)))

    assert family.state == "empty"
    assert family.notes == (_TRUNCATED,)


def test_an_empty_family_that_never_answered_still_reads_bilinmiyor() -> None:
    family = _congress(_panel((), _coverage(status="degraded", success=None)))

    assert family.state == "unanswered"
    assert family.state_text == "bilinmiyor — kaynak yanıt vermedi (son deneme 2026-09-15)"
    assert family.notes == ()  # the state text already carries the attempt date


# ---------------------------------------------------------------------------
# FD-03: the items branch
# ---------------------------------------------------------------------------


def test_stored_rows_with_no_recorded_success_say_the_check_date_is_unknown() -> None:
    family = _congress(_panel(_items([_congress_row()]), _coverage(status="degraded", success=None)))

    assert family.state == "items"
    assert family.notes == ("son kontrol tarihi bilinmiyor", "son deneme (2026-09-15) başarısız")
    assert "son kontrol 2026-09-15" not in family.notes


def test_a_failed_newest_attempt_over_stored_rows_still_names_the_as_of_date() -> None:
    """The 5.2.D1 disclosure is untouched: this branch already told the truth."""
    coverage = _coverage(status="degraded", success=_ANSWERED_AT)
    family = _congress(_panel(_items([_congress_row()]), coverage))

    assert family.notes == (
        "son kontrol 2026-09-15",
        "son deneme (2026-09-15) başarısız; kayıtlar 2026-08-01 itibarıyla",
    )


# ---------------------------------------------------------------------------
# End to end: the real job, answering once and then rate limited
# ---------------------------------------------------------------------------


def test_a_rate_limited_job_after_one_answer_never_claims_a_fresh_check(engine: Engine) -> None:
    asyncio.run(
        run_delayed_job(_Answering(), engine, ["NVDA"], now=_ANSWERED_AT, settings=_SETTINGS),  # type: ignore[arg-type]
    )
    for _ in range(3):  # every post-close run since is a 429
        asyncio.run(
            run_delayed_job(_RateLimited(), engine, ["NVDA"], now=_ATTEMPT, settings=_SETTINGS),  # type: ignore[arg-type]
        )

    panels = load_delayed_panels(engine, ["NVDA"], today=_TODAY, settings=_SETTINGS)
    for family in panels["NVDA"].families:
        assert family.state == "empty"
        assert family.state_text == _EMPTY_FROM_SUCCESS
        assert family.notes == (_STALE_EMPTY,)


def test_a_job_that_has_only_ever_failed_reads_bilinmiyor(engine: Engine) -> None:
    asyncio.run(
        run_delayed_job(_RateLimited(), engine, ["NVDA"], now=_ATTEMPT, settings=_SETTINGS),  # type: ignore[arg-type]
    )

    panels = load_delayed_panels(engine, ["NVDA"], today=_TODAY, settings=_SETTINGS)
    for family in panels["NVDA"].families:
        assert family.state == "unanswered"
        assert "kayıt yok" not in (family.state_text or "")


def test_the_congress_endpoint_is_still_the_one_the_job_asks_for(engine: Engine) -> None:
    """Guards the fixture: the e2e tests above drive the real job, not a stub."""
    asked: list[str] = []

    class _Recording(_Answering):
        async def request_json(
            self, path: str, *, params: dict[str, Any] | None = None, method: str = "GET",
        ) -> dict[str, Any]:
            asked.append(path)
            return await super().request_json(path, params=params, method=method)

    asyncio.run(
        run_delayed_job(_Recording(), engine, ["NVDA"], now=_ATTEMPT, settings=_SETTINGS),  # type: ignore[arg-type]
    )
    assert CONGRESS_RECENT_TRADES_PATH in asked


# ---------------------------------------------------------------------------
# R-WD1
# ---------------------------------------------------------------------------


def test_every_freshness_string_is_clean() -> None:
    for template in panel_module._TEXT.values():
        names = {name for _, name, _, _ in string.Formatter().parse(template) if name}
        ensure_clean(template.format(**dict.fromkeys(names, "7")))

    panels = [
        _panel((), _coverage(status="degraded", success=_ANSWERED_AT)),
        _panel((), _coverage(status="ok", success=_ATTEMPT, truncated=True)),
        _panel(_items([_congress_row()]), _coverage(status="degraded", success=None)),
    ]
    generated = [
        text
        for panel in panels
        for family in panel.families
        for text in (family.state_text, *family.notes)
        if text is not None
    ]
    assert len(generated) >= 10
    for text in generated:
        assert ensure_clean(text) == text
