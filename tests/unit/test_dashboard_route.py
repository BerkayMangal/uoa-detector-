from __future__ import annotations

from fastapi.testclient import TestClient


def _ctx(ticker: str, iv_pct: float) -> object:
    from webapp.gamma import GammaContext
    return GammaContext(
        ticker=ticker, as_of="2026-06-26", spot=1000.0, net_gex=1.0, flip=None,
        call_wall=1100.0, put_wall=950.0, atm_iv=0.5, iv_pct=iv_pct,
        next_earnings=None)


def _body(monkeypatch, gamma: dict[str, object]) -> str:
    import webapp.main as m

    class _G:
        def latest(self) -> dict[str, object]:
            return gamma

    class _R:
        def signals(self, _f: object) -> list[object]:
            return []

        def runs(self) -> list[object]:
            return []

        def ticker_label_options(self, *a: object, **k: object) -> tuple[list[str], list[str]]:
            return ([], [])

    monkeypatch.setattr(m, "_gamma", lambda: _G())
    monkeypatch.setattr(m, "_repo", lambda: _R())
    return TestClient(m.app).get("/").text


def test_dashboard_renders_vol_board(monkeypatch) -> None:
    body = _body(monkeypatch, {"NVDA": _ctx("NVDA", 0.9)})
    assert "NVDA" in body
    assert "IV-rank" in body or "IV rank" in body
    assert "Notable flow" in body
    # Honesty is rendered: the vol-board caveat shows on the page. (The stronger
    # no-"buy"-language guarantee is unit-tested at the copy level in
    # test_vol_board.py; base.html also carries a global "not a buy signal" line.)
    assert "not extra return" in body


def test_empty_vol_board_shows_market_hours_message(monkeypatch) -> None:
    # No live gamma (pre-open / weekend) -> section is NOT hidden; it shows the
    # header + an honest "populates during market hours" empty-state.
    body = _body(monkeypatch, {})
    assert "Vol-premium board" in body
    assert "populates during US market hours" in body


def test_threshold_divider_between_rich_and_thin(monkeypatch) -> None:
    # One above-threshold (0.9) and one below (0.5) name -> the rich-vol
    # threshold divider is rendered once between them.
    body = _body(monkeypatch, {"RICH": _ctx("RICH", 0.9), "THIN": _ctx("THIN", 0.5)})
    assert "rich-vol threshold" in body


def test_journal_new_prefills_from_vol_board_ticker(monkeypatch) -> None:
    # The vol-board "Log to journal" link is /journal/new?ticker=X. With no
    # signal, the form must prefill the ticker + a vol-structure thesis (was a
    # blank form — the broken loop this fixes).
    import webapp.main as m

    class _G:
        def latest(self) -> dict[str, object]:
            return {"NVDA": _ctx("NVDA", 0.9)}  # net_gex>0 -> long regime

    monkeypatch.setattr(m, "_gamma", lambda: _G())
    body = TestClient(m.app).get("/journal/new?ticker=nvda").text
    assert 'value="NVDA"' in body                       # ticker prefilled
    assert "Vol-premium board: NVDA" in body            # thesis prefilled
    assert "iron fly" in body                            # long-gamma structure
    assert "Pre-filled from the vol-premium board" in body
