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


_AMA_ROW = '<article data-row data-chip="no_quote"><p>AMA 1 aile aleyhte: Sektör.</p></article>'
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


def test_an_empty_board_is_vacuous_not_a_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "empty.html"
    path.write_text("<html><body></body></html>", encoding="utf-8")
    module = _audit_module()
    assert module.main(str(path)) == 1
    assert "VACUOUS" in capsys.readouterr().out
