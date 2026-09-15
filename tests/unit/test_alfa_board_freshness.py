"""Phase 5.2.A-fix8: board-level freshness line.

A7 removed the old dashboard's page badge. The board keeps per-row quote ages
(R-CO1) and now also states when the run's newest print happened, in ET, with
its date and age. It is a plain dated line, never a "live" claim.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from webapp.board.alfa_page import ALFA_COPY, AlfaPage, age_text, build_alfa_page
from webapp.board.honesty import ensure_clean
from webapp.board.settings import load_board_settings

from tests.unit.test_alfa_clean_candidate_honesty import _TS, _prints

_BOARD = Path(__file__).resolve().parents[2] / "profiles" / "board_v1.yaml"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0 sn"),
        (41, "41 sn"),
        (60, "1 dk"),
        (3599, "59 dk"),
        (3600, "1 sa 0 dk"),
        (5 * 3600 + 7 * 60, "5 sa 7 dk"),
        (86400 * 2 + 5, "2 gün"),
        (-30, "0 sn"),
    ],
)
def test_age_text_units(seconds: int, expected: str) -> None:
    assert age_text(timedelta(seconds=seconds)) == expected


def test_freshness_states_the_newest_print_in_et_with_date_and_age() -> None:
    newest = datetime(2026, 9, 15, 19, 42, tzinfo=UTC)  # 15:42 EDT
    page = AlfaPage(
        rows=(), views=(), sections=(), print_count=0, load_failed=False,
        newest_print_at=newest, rendered_at=newest + timedelta(minutes=12, seconds=30),
    )
    assert page.freshness == "Son baskı 15:42 ET (2026-09-15) · 12 dk önce"


def test_freshness_is_unknown_without_a_print_time() -> None:
    page = AlfaPage(rows=(), views=(), sections=(), print_count=0, load_failed=False)
    assert page.freshness == ALFA_COPY["freshness_unknown"]


def test_builder_uses_the_newest_print_and_the_page_clock() -> None:
    settings = load_board_settings(_BOARD)
    prints = _prints()
    newest = _TS  # _prints() seeds a single print at _TS
    now = newest + timedelta(hours=2, minutes=3)
    page = build_alfa_page(prints, settings, now=now)
    assert page.newest_print_at == newest
    assert page.freshness.endswith("· 2 sa 3 dk önce")


def test_builder_falls_back_to_the_run_latest_time_without_prints() -> None:
    settings = load_board_settings(_BOARD)
    latest = datetime(2026, 9, 14, 20, 0, tzinfo=UTC)
    page = build_alfa_page([], settings, now=latest + timedelta(days=1), run_latest_ts=latest)
    assert page.freshness == "Son baskı 16:00 ET (2026-09-14) · 1 gün önce"


def test_freshness_copy_is_clean_and_never_claims_live() -> None:
    for key in ("freshness", "freshness_unknown", "age_seconds", "age_minutes", "age_hours", "age_days"):
        ensure_clean(ALFA_COPY[key])
        assert "canlı" not in ALFA_COPY[key].lower()
