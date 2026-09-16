"""Phase 5.2.C32-fix2: the slippage summary names the sample it actually read.

Contract: ``docs/phase-5.2-decision-cards-acceptance.md`` §5 (C3, the
count-gated slippage display).

The card page's summary was labelled ``tüm dolum kayıtları`` — every recorded
fill — while the read behind it stopped at the repository's default bound. With
more fills than that bound the page would have claimed a count smaller than the
table's, and above the gate a median and an interquartile range describing a
silently truncated window.

The window is now ``fills.max_fills_per_summary`` from the board profile, and
the scope line says which sample it covers: every stored fill, or the newest
``n``.

Pins:
  - the scope line in both directions, and the window read from the profile
    rather than a literal in the page;
  - a capped read is labelled ``son {n} dolum kaydı`` and never ``tüm``, and the
    count it shows below the gate is the window's;
  - an uncapped read still says ``tüm dolum kayıtları`` and its count is the
    stored count;
  - the new copy passes ``honesty.ensure_clean``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from webapp.board import fills
from webapp.board.honesty import ensure_clean
from webapp.board.settings import load_board_settings

from tests.unit._webapp_auth import authed_client
from tests.unit.test_alfa_fill_routes import (
    _NOW,
    _log_card,
    _reset,
    _seed,
    _visible,
    _write_fills,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

    from fastapi.testclient import TestClient

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_WINDOW = _SETTINGS.fills.max_fills_per_summary  # D8: profile-driven, never a literal
_MIN_N = _SETTINGS.fills.min_n_for_stats


@pytest.fixture
def board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, ModuleType]]:
    url = f"sqlite:///{tmp_path / 'fill-scope.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.delenv("LIVE_TICKERS", raising=False)
    monkeypatch.delenv("UNUSUAL_WHALES_API_KEY", raising=False)
    import webapp.main as m

    _reset(m)
    monkeypatch.setattr(m, "_now", lambda: _NOW)
    _seed(url)
    yield authed_client(m.app, monkeypatch), m
    _reset(m)


def _narrow(m: ModuleType, monkeypatch: pytest.MonkeyPatch, window: int) -> None:
    """Shrink the summary window so the cap can be reached with a handful of fills."""
    narrowed = _SETTINGS.model_copy(
        update={"fills": _SETTINGS.fills.model_copy(update={"max_fills_per_summary": window})},
    )
    monkeypatch.setattr(m, "_BOARD_SETTINGS", narrowed)


# ---------------------------------------------------------------------------
# The scope line itself
# ---------------------------------------------------------------------------


def test_the_scope_line_names_the_window_only_when_the_read_was_capped() -> None:
    assert fills.stats_scope_text(4, capped=False) == "tüm dolum kayıtları"
    assert fills.stats_scope_text(500, capped=True) == "son 500 dolum kaydı"
    assert fills.stats_scope_text(3, capped=True) == "son 3 dolum kaydı"
    # A capped label never claims the whole table.
    assert "tüm" not in fills.stats_scope_text(3, capped=True)
    for text in (fills.stats_scope_text(3, capped=True), fills.stats_scope_text(3, capped=False)):
        assert ensure_clean(text) == text


def test_the_window_is_a_board_profile_key() -> None:
    assert _WINDOW == 500  # profiles/board_v1.yaml
    # A window below the sample gate could never produce a statistic at all.
    assert _WINDOW >= _MIN_N


# ---------------------------------------------------------------------------
# Rendered, in both directions
# ---------------------------------------------------------------------------


def test_a_capped_summary_names_its_window_and_counts_only_it(
    board: tuple[TestClient, ModuleType], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, m = board
    card_id = _log_card(client)
    _narrow(m, monkeypatch, 3)
    _write_fills(m, card_id, 4)  # one more fill than the window

    text = _visible(client.get(f"/kart/{card_id}").text)
    assert "Kayma özeti · son 3 dolum kaydı" in text
    assert "tüm dolum kayıtları" not in text
    # The count below the gate is the window's, and the label says so, so the
    # two agree instead of the page undercounting the table in silence.
    assert "3 dolum kaydı; istatistik için yetersiz örnek" in text
    assert len(m._fill_repo().list_fills()) == 4  # nothing was dropped from the table


def test_an_uncapped_summary_still_covers_every_stored_fill(
    board: tuple[TestClient, ModuleType],
) -> None:
    client, m = board
    card_id = _log_card(client)
    _write_fills(m, card_id, 2)

    text = _visible(client.get(f"/kart/{card_id}").text)
    assert "Kayma özeti · tüm dolum kayıtları" in text
    assert "son 2 dolum kaydı" not in text  # no window is named when none was hit
    assert "2 dolum kaydı; istatistik için yetersiz örnek" in text


def test_the_window_bounds_the_read_and_not_the_table(
    board: tuple[TestClient, ModuleType], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap is a page bound: every fill stays readable through the repository."""
    client, m = board
    card_id = _log_card(client)
    _narrow(m, monkeypatch, 2)
    _write_fills(m, card_id, 5)

    assert client.get(f"/kart/{card_id}").status_code == 200
    assert len(m._fill_repo().list_fills(limit=100)) == 5
    text = _visible(client.get(f"/kart/{card_id}").text)
    assert "son 2 dolum kaydı" in text
