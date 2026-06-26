# tests/unit/test_gamma_live_earnings.py
from __future__ import annotations


class _FakeClient:
    def __init__(self, earnings_payload: object) -> None:
        self._p = earnings_payload

    async def request_json(self, path: str) -> object:
        if "earnings" in path:
            return self._p
        if "greek-exposure" in path:
            return {"data": [{"strike": 100.0, "call_gex": 1.0, "put_gex": -0.5}]}
        return {"data": [{"date": "2026-06-26", "close": 100.0,
                          "volatility": 0.5, "iv_rank_1y": 90.0}]}


def test_next_earnings_picked_from_payload() -> None:
    from webapp.gamma_live import _next_earnings_date
    payload = {"data": [{"report_date": "2026-05-01"},
                        {"report_date": "2026-07-30"}]}
    # soonest FUTURE relative to a reference is chosen by the loop; the helper
    # returns sorted future dates' first element given 'today'.
    assert _next_earnings_date(payload, today="2026-06-26") == "2026-07-30"


def test_next_earnings_none_on_garbage() -> None:
    from webapp.gamma_live import _next_earnings_date
    assert _next_earnings_date({"nope": 1}, today="2026-06-26") is None
    assert _next_earnings_date({"data": []}, today="2026-06-26") is None
