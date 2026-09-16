"""Phase 5.2.D-fix3: the row finds its delayed panel under the key the reader wrote.

Review finding FD-05; contract ``docs/phase-5.2-alfa-board-acceptance.md`` §2
R-UN1 and §7.

``load_delayed_panels`` keys its result by ``ticker.strip().upper()`` and drops
blank tickers; ``_row_panel`` looked its row up with ``ticker.upper()`` alone.
A row ticker carrying surrounding whitespace therefore missed a panel that had
been read successfully and rendered the read-failure state ("gecikmeli ek kanıt
okunamadı"), hiding real filings behind a failure that never happened.

``StoredSignal.ticker`` is a plain ``str`` with no validator and the board
reader rebuilds rows with ``StoredSignal.model_validate_json``
(webapp/board/signals.py), so nothing between the stored row and the lookup
normalises the spelling.

Pins:
  - a row ticker with surrounding whitespace finds its panel;
  - the ordinary spelling is unaffected;
  - a ticker the reader genuinely returned nothing for still renders the
    read-failure state, never a silently empty bucket.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from webapp.board.alfa_page import build_alfa_page
from webapp.board.delayed_coverage import CoverageView
from webapp.board.delayed_panel import build_delayed_panel
from webapp.board.settings import load_board_settings
from webapp.board.signals import BoardPrint, PrintMetaView

from tests.conftest import build_print
from uoa_detector.backtest.store import BacktestStore, StoredSignal
from uoa_detector.domain.events import EnrichedEvent
from uoa_detector.domain.labels import LabelDecision, SignalLabel
from uoa_detector.domain.risk import PositionSize, RiskBucket

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date

    from webapp.board.delayed_panel import DelayedPanel

_REPO = Path(__file__).resolve().parents[2]
_SETTINGS = load_board_settings(_REPO / "profiles" / "board_v1.yaml")
_RUN = "live-2026-09-15"
_TS = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
_NOW = datetime(2026, 9, 15, 21, 5, tzinfo=UTC)
_ANSWERED = datetime(2026, 9, 15, 21, 0, tzinfo=UTC)


def _prints(ticker: str) -> list[BoardPrint]:
    """One board print whose stored signal carries ``ticker`` exactly as spelled."""
    printed = build_print(
        event_id="e1", ts=_TS, ticker="NVDA", option_type="call", strike="100",
        dte=3, premium="100000",
    )
    stored = BacktestStore().add(
        EnrichedEvent(print=printed),
        LabelDecision(label=SignalLabel.STANDARD_UOA, reason="test"),
        PositionSize(bucket=RiskBucket.STANDARD_UOA, max_r=0.5),
    )
    # The print model normalises its ticker; a stored row read back through
    # StoredSignal.model_validate_json is not re-normalised.
    signal: StoredSignal = stored.model_copy(update={"ticker": ticker})
    return [
        BoardPrint(
            run_id=_RUN, event_id="e1", signal=signal,
            meta=PrintMetaView(fill_side="at_ask", option_chain=None),
        ),
    ]


def _answered_panel(ticker: str) -> DelayedPanel:
    """What the reader returns for a ticker it read: congress answered, nothing in the window."""
    coverage = {
        "congress": CoverageView(
            ticker=ticker.strip().upper(), family="congress", last_attempt_at=_ANSWERED,
            last_status="no_data", last_success_at=_ANSWERED, truncated=False,
        ),
    }
    return build_delayed_panel(ticker, (), coverage, settings=_SETTINGS.delayed)


def _reader(
    *, blind_to: str | None = None,
) -> tuple[list[str], object]:
    """A stand-in for ``load_delayed_panels``: keys by strip().upper(), like the real one."""
    asked: list[str] = []

    def _source(tickers: Sequence[str], today: date) -> Mapping[str, DelayedPanel]:
        del today
        asked.extend(tickers)
        return {
            t.strip().upper(): _answered_panel(t)
            for t in tickers
            if blind_to is None or t.strip().upper() != blind_to
        }

    return asked, _source


def _states(page_view: object) -> list[str]:
    panel = page_view.delayed  # type: ignore[attr-defined]
    assert panel is not None
    return [family.state for family in panel.families]


def test_a_row_ticker_with_whitespace_finds_the_panel_that_was_read() -> None:
    asked, source = _reader()
    page = build_alfa_page(
        _prints(" NVDA"), _SETTINGS, now=_NOW, delayed_source=source,  # type: ignore[arg-type]
    )
    (view,) = page.views

    assert asked == [" NVDA"]  # the row spelling is what the reader is asked for
    assert "unreadable" not in _states(view)
    assert _states(view)[0] == "empty"  # congress answered with nothing
    assert view.delayed is not None
    assert view.delayed.ticker == "NVDA"
    assert page.delayed_failed is False


def test_the_ordinary_spelling_is_unaffected() -> None:
    _asked, source = _reader()
    page = build_alfa_page(
        _prints("NVDA"), _SETTINGS, now=_NOW, delayed_source=source,  # type: ignore[arg-type]
    )
    (view,) = page.views

    assert _states(view)[0] == "empty"
    assert "unreadable" not in _states(view)


def test_a_ticker_the_reader_returned_nothing_for_still_reads_unreadable() -> None:
    _asked, source = _reader(blind_to="NVDA")
    page = build_alfa_page(
        _prints("NVDA"), _SETTINGS, now=_NOW, delayed_source=source,  # type: ignore[arg-type]
    )
    (view,) = page.views

    assert set(_states(view)) == {"unreadable"}  # a miss is never a silently empty bucket
    assert view.delayed is not None
    assert view.delayed.families[0].state_text == (
        "gecikmeli ek kanıt okunamadı; bu, kayıt yok demek değil"
    )
