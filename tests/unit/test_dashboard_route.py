from __future__ import annotations

from fastapi.testclient import TestClient


def test_dashboard_renders_vol_board(monkeypatch) -> None:
    import webapp.main as m
    from webapp.gamma import GammaContext

    ctx = {"NVDA": GammaContext(
        ticker="NVDA", as_of="2026-06-26", spot=1000.0, net_gex=1.0, flip=None,
        call_wall=1100.0, put_wall=950.0, atm_iv=0.5, iv_pct=0.9,
        next_earnings=None)}

    class _G:
        def latest(self) -> dict[str, GammaContext]:
            return ctx

    class _R:
        def signals(self, _f: object) -> list[object]:
            return []

        def runs(self) -> list[object]:
            return []

        def ticker_label_options(self, *a: object, **k: object) -> tuple[list[str], list[str]]:
            return ([], [])

    monkeypatch.setattr(m, "_gamma", lambda: _G())
    monkeypatch.setattr(m, "_repo", lambda: _R())

    body = TestClient(m.app).get("/").text
    assert "NVDA" in body
    assert "IV-rank" in body or "IV rank" in body
    assert "Notable flow" in body
    # Honesty is rendered: the vol-board caveat shows on the page. (The stronger
    # no-"buy"-language guarantee is unit-tested at the copy level in
    # test_vol_board.py::test_vol_structure_is_defined_risk_template_no_buy_word;
    # base.html also carries a global "not a buy signal" disclaimer.)
    assert "not extra return" in body
