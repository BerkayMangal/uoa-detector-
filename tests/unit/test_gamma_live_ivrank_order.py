"""Phase 5.2.0 hotfix: the gamma/vol board uses the NEWEST iv-rank row.

Live ``GET /api/stock/SPY/iv-rank`` (probed 2026-09-15 during RTH) returns the
last five sessions OLDEST first. ``fetch_one`` took ``data[0]``, so the live
board showed the 2026-09-09 close, IV rank and ``as_of`` six days later. The
fixture below keeps the probed rows' shape (string numbers, ISO dates).
"""

from __future__ import annotations

import pytest
from webapp.gamma_live import _latest_iv_row, fetch_one

_IV_ROWS_OLDEST_FIRST: list[dict[str, object]] = [
    {"date": "2026-09-09", "close": "762.4", "iv_rank_1y": "15.0414", "volatility": "0.134"},
    {"date": "2026-09-10", "close": "757.83", "iv_rank_1y": "22.6896", "volatility": "0.146"},
    {"date": "2026-09-11", "close": "759.1", "iv_rank_1y": "19.0021", "volatility": "0.138"},
    {"date": "2026-09-14", "close": "760.88", "iv_rank_1y": "15.6788", "volatility": "0.135"},
    {"date": "2026-09-15", "close": "757.525", "iv_rank_1y": "18.8655", "volatility": "0.14"},
]


class _FakeClient:
    async def request_json(self, path: str) -> object:
        if "greek-exposure" in path:
            return {"data": [
                {"date": "2026-09-15", "strike": float(k), "call_gex": 1000.0 - k, "put_gex": 740.0 - k}
                for k in range(720, 800, 5)
            ]}
        if "iv-rank" in path:
            return {"data": _IV_ROWS_OLDEST_FIRST}
        return {"data": []}


def test_latest_iv_row_picks_newest_date_from_oldest_first_payload() -> None:
    row = _latest_iv_row(_IV_ROWS_OLDEST_FIRST)
    assert row is not None
    assert row["date"] == "2026-09-15"


def test_latest_iv_row_is_order_independent() -> None:
    shuffled = [_IV_ROWS_OLDEST_FIRST[i] for i in (3, 0, 4, 1, 2)]
    row = _latest_iv_row(shuffled)
    assert row is not None
    assert row["date"] == "2026-09-15"


def test_latest_iv_row_ignores_undated_rows_and_passes_dicts_through() -> None:
    assert _latest_iv_row([{"close": "1"}, {"date": "", "close": "2"}]) is None
    assert _latest_iv_row([]) is None
    assert _latest_iv_row("garbage") is None
    single = {"date": "2026-09-15", "close": "1"}
    assert _latest_iv_row(single) is single


async def test_fetch_one_uses_newest_session_for_spot_iv_rank_and_as_of() -> None:
    result = await fetch_one(_FakeClient(), "SPY")  # type: ignore[arg-type]
    assert result is not None
    as_of, ctx = result
    assert as_of == "2026-09-15"
    assert ctx["spot"] == pytest.approx(757.525)
    assert ctx["iv_pct"] == pytest.approx(0.188655)
