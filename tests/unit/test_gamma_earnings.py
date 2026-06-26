from __future__ import annotations

from datetime import date

from webapp.gamma import GammaRepo


def test_upsert_and_read_next_earnings(tmp_path) -> None:
    repo = GammaRepo(f"sqlite:///{tmp_path/'g.db'}")
    repo.reset()
    repo.upsert("NVDA", "2026-06-26", {
        "spot": 1000.0, "net_gex": 1.0, "flip": None, "call_wall": None,
        "put_wall": None, "atm_iv": 0.5, "iv_pct": 0.9,
        "next_earnings": "2026-07-30",
    })
    ctx = repo.latest()["NVDA"]
    assert ctx.next_earnings == date(2026, 7, 30)


def test_missing_next_earnings_is_none(tmp_path) -> None:
    repo = GammaRepo(f"sqlite:///{tmp_path/'g.db'}")
    repo.reset()
    repo.upsert("XOM", "2026-06-26", {
        "spot": 100.0, "net_gex": 1.0, "flip": None, "call_wall": None,
        "put_wall": None, "atm_iv": 0.2, "iv_pct": 0.3,
    })
    assert repo.latest()["XOM"].next_earnings is None
