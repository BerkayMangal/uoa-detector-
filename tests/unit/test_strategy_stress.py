"""Fixed hypotheses: causal delayed execution, concentration and artifact isolation."""

from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from webapp.alpha_lab.db import digest
from webapp.calibration.model import load_csv
from webapp.robustness import report, research
from webapp.tournament import engine, model
from webapp.tournament import report as base_report
from webapp.tournament.settings import settings

from tests.unit.test_tournament import C, panel, selected


def test_delay_freezes_ranking_atr_and_signal_eligibility_without_moving_prices() -> None:
    p = model.prepare(panel())
    c = replace(C, slots=1, sizing="atr_budget", horizon=2)
    mask = selected(p, [(23, "AAA"), (23, "BBB")])
    a, b = p.tickers.index("AAA"), p.tickers.index("BBB")
    p.scores[c.family, c.lookback][23, [a, b]] = [2, 1]
    p.scores[c.family, c.lookback][24, [a, b]] = [-2, 50]
    p.atr[23, a], p.atr[24, a] = 2, 100
    q, deferred = research.delayed_inputs(p, c, mask, p.days[23], 1)
    assert q.open is p.open and q.close is p.close
    assert not deferred[23].any() and deferred[24, a] and deferred[24, b]
    assert q.atr[24, a] == 2 and p.atr[24, a] == 100
    assert list(q.scores[c.family, c.lookback][24, [a, b]]) == [2, 1]
    sim = engine.simulate(q, c, deferred, p.days[23], p.days[30], 5, ledger=True)
    assert len(sim.trades) == 1
    t = sim.trades[0]
    assert (t.ticker, t.signal, t.entry, t.exit) == ("AAA", p.days[24], p.days[25], p.days[26])
    assert t.quantity == 24  # min(floor(2500/100.05), floor(100/(1.5*2)))
    assert t.pnl == pytest.approx(24 * (101 * 0.9995 - 100 * 1.0005))
    encoded = research.serialized(sim, p, 1)
    assert encoded["ledger"][0]["original_signal"] == p.days[23]


def test_delay_cannot_import_a_pre_split_signal_or_export_exit_outside_window() -> None:
    p = model.prepare(panel())
    c = replace(C, horizon=3)
    original = selected(p, [(22, "AAA"), (28, "BBB")])
    q, deferred = research.delayed_inputs(p, c, original, p.days[23], 2)
    assert not deferred[24].any()  # signal 22 is outside split even after delay
    assert deferred[30, p.tickers.index("BBB")]
    sim = engine.simulate(q, c, deferred, p.days[23], p.days[32], 5, ledger=True)
    assert not sim.trades and sim.metrics.closed == 0
    with pytest.raises(ValueError, match="nonnegative"):
        research.delayed_inputs(p, c, original, p.days[23], -1)
    _, empty = research.delayed_inputs(p, c, original, p.days[23], len(p.days) + 1)
    assert not empty.any()


def test_missing_delayed_execution_price_is_not_replaced_by_a_later_day() -> None:
    p = model.prepare(panel())
    a = p.tickers.index("AAA")
    p.open[25, a] = np.nan
    q, deferred = research.delayed_inputs(p, C, selected(p, [(23, "AAA")]), p.days[23], 1)
    sim = engine.simulate(q, C, deferred, p.days[23], p.days[32], 5, ledger=True)
    assert sim.metrics.missed_entries == 1 and not sim.trades


def test_name_omission_reallocates_slots_rather_than_subtracting_pnl() -> None:
    p = model.prepare(panel())
    c = replace(C, slots=1)
    p.scores[c.family, c.lookback][23, :2] = [2, 1]
    mask = selected(p, [(23, "AAA"), (23, "BBB")])
    baseline = engine.simulate(p, c, mask, p.days[23], p.days[32], 5, ledger=True)
    omitted = mask.copy()
    omitted[:, p.tickers.index("AAA")] = False
    rerun = engine.simulate(p, c, omitted, p.days[23], p.days[32], 5, ledger=True)
    assert baseline.trades[0].ticker == "AAA" and rerun.trades[0].ticker == "BBB"
    assert rerun.metrics.net_usd == pytest.approx(baseline.metrics.net_usd)


def test_signal_month_omission_retains_old_signals_exiting_during_omitted_month() -> None:
    p = model.prepare(panel())
    # The monthly boundary is only a signal mask, not removal of execution bars.
    last_jan = max(i for i, d in enumerate(p.days) if d.month == 1)
    first_feb = last_jan + 1
    original = selected(p, [(last_jan, "AAA"), (first_feb, "BBB")])
    filtered = research.omit(p, original, "signal_month", "2026-02")
    sim = engine.simulate(p, C, filtered, p.days[0], p.days[-1], 5, ledger=True)
    assert len(sim.trades) == 1 and sim.trades[0].ticker == "AAA"
    assert sim.trades[0].signal.month == 1 and sim.trades[0].exit.month == 2
    assert original[first_feb, p.tickers.index("BBB")]
    with pytest.raises(ValueError, match="unknown omission"):
        research.omit(p, original, "pnl_month", "2026-02")


def trade(pnl: float | None, excess: float | None = None, ticker: str = "AAA") -> engine.Trade:
    return engine.Trade(
        ticker,
        date(2026, 7, 8),
        date(2026, 7, 9),
        date(2026, 7, 10),
        1,
        100,
        100 + pnl if pnl is not None else None,
        pnl,
        excess,
    )


def test_concentration_is_explicit_fixed_ledger_accounting_with_missing_excess() -> None:
    result = research.accounting((trade(100, 90), trade(-80), trade(30, 20, "BBB"), trade(None)))
    assert result["net_usd"] == 50 and result["unresolved"] == 1
    assert result["gross_gains_usd"] == 130 and result["ticker_count"] == 2
    assert result["top_win_share_pct"] == pytest.approx(100 / 130 * 100)
    assert result["without_best"] == {
        "1": {"net_usd": -50, "removed": 1},
        "3": {"net_usd": -80, "removed": 2},
    }
    assert result["groups"]["ticker"][0]["excess_usd"] is None
    assert sum(g["net_usd"] for g in result["groups"]["ticker"]) == 50


def test_no_closed_results_are_unknown_and_all_losses_do_not_invent_winners() -> None:
    empty = research.accounting((trade(None),))
    assert empty["net_usd"] is None and empty["gross_gains_usd"] is None
    losses = research.accounting((trade(-30), trade(-20)))
    assert losses["top_win_share_pct"] is None and losses["gross_gains_usd"] == 0
    assert losses["without_best"]["3"] == {"net_usd": -50, "removed": 0}
    with pytest.raises(ValueError, match="nonfinite"):
        research.accounting((trade(float("nan")),))


def test_changed_source_is_rejected_before_prices_are_loaded(tmp_path: Path) -> None:
    source = tmp_path / "bars.csv"
    source.write_text("not the historical source")
    with pytest.raises(ValueError, match="source differs"):
        research.run(source)


def test_all_six_frozen_baselines_reconcile_exactly() -> None:
    frozen = base_report.load()
    p, s = model.prepare(load_csv(Path("data/study_f/bars.csv"))), settings()
    for w in frozen["winners"]:
        c = model.Config(**w["config"])
        q, mask = research.delayed_inputs(p, c, model.mask(p, c), s.test_start, 0)
        sim = engine.simulate(q, c, mask, s.test_start, s.test_end, s.slippage_bps, ledger=True)
        research.assert_baseline(sim, w["test"])
        with pytest.raises(ValueError, match="baseline differs"):
            research.assert_baseline(sim, w["test"] | {"equity": []})


def test_offline_runner_refuses_both_existing_summary_and_existing_replays(tmp_path: Path) -> None:
    path = tmp_path / "report.json"
    path.write_text("do not overwrite")
    command = [sys.executable, "scripts/run_strategy_stress.py", str(path)]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0 and "refusing to overwrite" in result.stderr
    assert path.read_text() == "do not overwrite"
    path.unlink()
    (tmp_path / "replays.jsonl.gz").write_bytes(b"preserve")
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0 and "refusing to overwrite" in result.stderr
    assert (tmp_path / "replays.jsonl.gz").read_bytes() == b"preserve"


def test_real_artifact_is_complete_reproducible_and_matches_each_summary() -> None:
    r = report.load()
    path = report.REPORT_PATH.with_name(r["replay_artifact"]["name"])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == r["replay_artifact"]["sha256"]
    rows = [json.loads(line) for line in gzip.decompress(path.read_bytes()).splitlines()]
    assert len(rows) == r["portfolio_evaluations"] == 504
    assert len({row["key"] for row in rows}) == 504
    assert r["baseline_reconciliations"] == r["selected_configurations"] == 6
    for row in rows:
        ledger, m = row["ledger"], row["metrics"]
        closed = [t for t in ledger if t["pnl"] is not None]
        assert len(closed) == m["closed"]
        assert sum(t["pnl"] for t in closed) == pytest.approx(m["net_usd"])
        assert sum(row["daily_excess"]) * 10000 == pytest.approx(m["excess_usd"])
        assert row["equity"][-1] == pytest.approx(10000 + m["net_usd"])
    for family in r["families"]:
        for group in ("ticker", "signal_month"):
            counts = family["accounting"]["groups"][group]
            assert sum(x["net_usd"] for x in counts) == pytest.approx(
                family["accounting"]["net_usd"]
            )


@pytest.mark.parametrize(
    "field", ["code_hash", "profile_hash", "base_report_hash", "source_hashes"]
)
def test_artifact_tamper_fails_supplement_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field: str
) -> None:
    body: dict[str, Any] = json.loads(report.REPORT_PATH.read_text())
    body[field] = {} if field == "source_hashes" else "0" * 64
    body.pop("report_hash")
    body["report_hash"] = digest(body)
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(body))
    monkeypatch.setattr(report, "REPORT_PATH", path)
    report.load.cache_clear()
    try:
        with pytest.raises(ValueError, match="mismatch"):
            report.load()
        assert report.view() is None
    finally:
        report.load.cache_clear()


def test_private_page_renders_real_stress_and_preserves_report_if_supplement_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    from fastapi.testclient import TestClient
    from webapp import main
    from webapp.board.db import make_engine
    from webapp.calibration import storage

    from tests.unit._webapp_auth import set_web_auth

    e = make_engine(f"sqlite:///{tmp_path / 'route.db'}")
    storage.ensure(e)
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=e))
    headers = set_web_auth(monkeypatch)
    client = TestClient(main.app)
    assert client.get("/stratejiler").status_code == 401
    response = client.get("/stratejiler", headers=headers)
    assert response.status_code == 200
    assert "504 ek portföy kontrolü" in response.text
    assert "en iyi 3" in response.text.lower()
    assert response.headers["cache-control"] == "private, no-store"
    assert "noindex" in response.headers["x-robots-tag"]

    def fail() -> dict[str, Any]:
        raise ValueError("unreadable supplemental artifact")

    monkeypatch.setattr(report, "load", fail)
    response = client.get("/stratejiler", headers=headers)
    assert response.status_code == 200
    assert "Ek stres raporu şu an okunamıyor" in response.text
    assert "Gelecekteki PAPER yarışı" in response.text
