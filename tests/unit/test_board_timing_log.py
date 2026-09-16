"""Phase 5.2.PERF2: the board says where its render time goes.

Production renders the board in about 3.8 s while the same render takes 0.024 s
locally and does not scale with the number of prints, so the next fix has to be
aimed by measurement. One INFO line per render carries the per-stage timings.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

from tests.unit.test_alfa_route import _LIVE, _budget_specs, _seed, board  # noqa: F401

if TYPE_CHECKING:
    import pytest


def test_a_board_render_logs_its_stage_timings(board, caplog: pytest.LogCaptureFixture) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    url, client = board
    _seed(url, _LIVE, _budget_specs(12))
    with caplog.at_level(logging.INFO, logger="webapp.main"):
        response = client.get("/alfa")
    assert response.status_code == 200
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("board timing:")]
    assert len(lines) == 1, lines
    line = lines[0]
    for field in ("runs=", "prints=", "page=", "vol=", "render=", "total=", "rows=", "print_count="):
        assert field in line, line
    total = float(re.search(r"total=([0-9.]+)", line).group(1))  # type: ignore[union-attr]
    stages = sum(float(re.search(rf"{f}=([0-9.]+)", line).group(1))  # type: ignore[union-attr]
                 for f in ("runs", "prints", "page", "vol", "render"))
    assert stages <= total + 0.05, line
