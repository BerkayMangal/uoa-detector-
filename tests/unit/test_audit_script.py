"""The live honesty auditor itself (``scripts/audit_board_html.py``).

That script is what verifies every deploy, so a false positive in it is as
expensive as a real regression: it has now failed a correct page twice
(2026-09-16 the DAR chip, 2026-09-17 the R-CA1 fallback). These tests pin the
rule it got wrong both times, in both directions.

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §2, R-CA1 — every row
carries a counter-argument clause starting ``AMA``; if none is found, the row
renders ``Bariz bir karşı argüman bulunamadı — bu bir onay değildir`` followed
by the list of what was checked.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from webapp.board.copy_tr import NO_COUNTER_FOUND

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "audit_board_html.py"


def _audit_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("audit_board_html", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ``data-counter`` because that is what the board renders (_alfa_row.html) and what
# the R-CA1 check now reads. Anchoring on the row's whole text passed any row whose
# TICKER contained the letters "AMA", counter-argument or not (audit 2026-09-19).
_AMA_ROW = (
    '<article data-row data-chip="no_quote">'
    "<p data-counter>AMA 1 aile aleyhte: Sektör.</p></article>"
)
# The shape the live board renders when nothing counts against the row.
_FALLBACK_ROW = (
    '<article data-row data-chip="no_quote"><p>'
    f"{NO_COUNTER_FOUND}</p><p>Bakılanlar: maliyet (kotasyon yok), aleyhte aile (0), "
    "bilinmeyen aile (1).</p></article>"
)
# Neither shape: no counter-argument and no admission that none was found.
_SILENT_ROW = '<article data-row data-chip="no_quote"><p>Lehte okuma.</p></article>'


def _page(*rows: str) -> str:
    return "<html><body>" + "".join(rows) + "</body></html>"


def _ca1_line(capsys: pytest.CaptureFixture[str], tmp_path: Path, page: str) -> str:
    path = tmp_path / "page.html"
    path.write_text(page, encoding="utf-8")
    _audit_module().main(str(path))
    out = capsys.readouterr().out
    return next(line for line in out.splitlines() if "R-CA1" in line)


def test_a_row_with_an_ama_clause_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    assert "ok" in _ca1_line(capsys, tmp_path, _page(_AMA_ROW))


def test_the_no_counter_fallback_with_its_checked_list_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    # The 2026-09-17 false positive: four correct rows were failed for lacking "AMA".
    assert "ok" in _ca1_line(capsys, tmp_path, _page(_AMA_ROW, _FALLBACK_ROW))


def test_the_fallback_without_the_checked_list_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    bare = f'<article data-row><p>{NO_COUNTER_FOUND}</p></article>'
    assert "FAIL" in _ca1_line(capsys, tmp_path, _page(bare))


def test_a_row_with_neither_shape_still_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    line = _ca1_line(capsys, tmp_path, _page(_AMA_ROW, _FALLBACK_ROW, _SILENT_ROW))
    assert "FAIL" in line
    assert "[2]" in line


# ---------------------------------------------------------------------------
# The three holes the 2026-09-19 audit found. Each fixture PASSED the old check.
# ---------------------------------------------------------------------------

# R-CA1 read COUNTER_LEAD ("AMA") out of the row's whole visible text, so a ticker
# spelling those letters satisfied it with no counter-argument anywhere.
_AMA_TICKER_ROW = (
    '<article data-row data-ticker="AMAT" data-chip="no_quote">'
    "<p>AMAT lehte okuma.</p></article>"
)
# R-CO1 accepted any "... önce", and the LAST-TRADE line is one. This row states no
# quote age at all.
_LAST_TRADE_ONLY_ROW = (
    '<article data-row data-chip="tradable">'
    "<p>son işlem 4 dk önce</p></article>"
)
# R-DL1 matched the strip with a non-greedy regex that stopped at the first closing
# tag, so only the strip's first child was inspected. This item sits after it.
_STRIP_HIDING_A_DELAYED_ITEM = (
    '<article data-row data-chip="no_quote">'
    '<div data-evidence-strip><span data-family="flow">akış</span>'
    "<span data-delayed-item>Kongre 12.09.2026</span></div></article>"
)


def _line(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, page: str, marker: str,
) -> str:
    path = tmp_path / "page.html"
    path.write_text(page, encoding="utf-8")
    _audit_module().main(str(path))
    out = capsys.readouterr().out
    return next(line for line in out.splitlines() if marker in line)


def test_a_ticker_spelling_ama_is_not_a_counter_argument(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """The check reads the counter element, not the row's letters."""
    assert "FAIL" in _line(capsys, tmp_path, _page(_AMA_TICKER_ROW), "R-CA1")


def test_a_last_trade_line_is_not_a_quote_age(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """R-CO1 asks when the QUOTE was taken; when the tape last printed is a different fact."""
    assert "FAIL" in _line(capsys, tmp_path, _page(_LAST_TRADE_ONLY_ROW), "quote age")


def test_a_delayed_item_hidden_after_the_first_chip_is_still_caught(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """R-DL1: the strip is matched by depth now, so its later children are visible."""
    line = _line(
        capsys, tmp_path, _page(_STRIP_HIDING_A_DELAYED_ITEM),
        "stay out of the evidence strip",
    )
    assert "FAIL" in line


def test_an_empty_board_is_vacuous_not_a_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "empty.html"
    path.write_text("<html><body></body></html>", encoding="utf-8")
    module = _audit_module()
    assert module.main(str(path)) == 1
    assert "VACUOUS" in capsys.readouterr().out
