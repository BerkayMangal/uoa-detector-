"""Existing bearish policy reaches the canonical feed without a short-stock fallback."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from webapp import main
from webapp.board.db import make_engine
from webapp.live_alpha import store
from webapp.opening import alerts, policy, storage, view
from webapp.opening.model import clock

from tests.unit._webapp_auth import set_web_auth
from tests.unit.test_opening_desk import BASE, T0, option_source


@pytest.mark.parametrize("instrument", ["long_put", "bear_put_debit"])
@pytest.mark.parametrize(
    "damage", ["valid", "stale", "future", "unpriced", "stock", "cost", "bad_stop"]
)
def test_priced_bearish_source_to_real_publication_and_feed(tmp_path, instrument, damage):
    e = make_engine(f"sqlite:///{tmp_path / 'down.db'}")
    store.ensure_live_tables(e)
    storage.ensure(e)
    scan = option_source()
    card = scan.snapshot["cards"][0]
    card["instrument"]["preferred"] = instrument
    selected = card["options"][0]
    selected["kind"] = instrument
    if instrument == "bear_put_debit":
        selected["legs"].append(
            dict(option_symbol="NVDA261120P00095000", fetched_at=T0.isoformat(), bid=0.8, ask=1)
        )
    if damage in {"stale", "future"}:
        selected["legs"][0]["fetched_at"] = (
            T0 + timedelta(seconds=-901 if damage == "stale" else 1)
        ).isoformat()
    elif damage == "unpriced":
        selected["readiness"] = "QUOTE_PENDING"
    elif damage == "stock":
        card["instrument"]["preferred"] = "stock"
    elif damage == "cost":
        selected["roundtrip_cost_pct"] = 26
    elif damage == "bad_stop":
        card["stock_plan"]["stop"] = 97
    advice = policy.project(scan, card, T0, clock(T0), set())
    store.write_scan(e, scan, BASE.policy_version)
    assert storage.publish(e, scan, T0, clock(T0), (advice,))
    feed = alerts.feed(e, T0)
    entries = [s for s in feed.signals if s.event_type == "entry"]
    assert len(entries) == int(damage == "valid")
    if entries:
        s = entries[0]
        assert s.instrument == instrument and "PUT" in s.label
        assert s.underlying_target < s.underlying_ref < s.underlying_stop
        assert len(s.contracts) == (1 if instrument == "long_put" else 2)
        assert view.build(e, T0).statements[0].advice.direction == "down"
        assert not [
            s
            for s in alerts.feed(e, T0 + timedelta(seconds=301)).signals
            if s.event_type == "entry"
        ]
    e.dispose()


def test_downside_canonical_api_is_private_and_get_never_creates_a_call(tmp_path, monkeypatch):
    e = make_engine(f"sqlite:///{tmp_path / 'route.db'}")
    store.ensure_live_tables(e)
    storage.ensure(e)
    scan = option_source()
    advice = policy.project(scan, scan.snapshot["cards"][0], T0, clock(T0), set())
    store.write_scan(e, scan, BASE.policy_version)
    storage.publish(e, scan, T0, clock(T0), (advice,))
    monkeypatch.setattr(main, "_board_reader", lambda: SimpleNamespace(engine=e))
    monkeypatch.setattr(main, "_now", lambda: T0)
    writes = []
    event.listen(
        e,
        "before_cursor_execute",
        lambda _c, _cur, sql, *_a: (
            writes.append(sql)
            if sql.lstrip().upper().startswith(("CREATE", "UPDATE", "INSERT", "DELETE"))
            else None
        ),
    )
    client = TestClient(main.app)
    auth = set_web_auth(monkeypatch)
    assert client.get("/api/acilis/uyarilar").status_code == 401
    r = client.get("/api/acilis/uyarilar", headers=auth)
    assert r.status_code == 200
    s = next(s for s in r.json()["signals"] if s["event_type"] == "entry")
    assert s["instrument"] == "long_put" and "PUT" in s["label"]
    assert not writes
    page = client.get("/acilis", headers=auth)
    assert page.status_code == 200 and "Düşüş tezi · PUT alımı" in page.text
    e.dispose()


@pytest.mark.parametrize("kind", ["long_put", "bear_put_debit"])
@pytest.mark.parametrize(
    "damage", ["valid", "stock", "unpriced", "no_structure", "disabled", "pending"]
)
def test_actual_bearish_decision_and_priced_structures_enter_source_paper_once(
    tmp_path, kind, damage
):
    import json
    from dataclasses import replace

    from webapp.live_alpha.job import open_papers_for_new_buys, record_changes

    from tests.unit.test_live_alpha_engine import COSTS, LIVE_NOW, S, _news, _price, _print, _q
    from uoa_detector.live_alpha.calendar import classify
    from uoa_detector.live_alpha.decide import build_card, evaluate
    from uoa_detector.live_alpha.flow import summarise
    from uoa_detector.live_alpha.model import InstrumentChoice, Readiness, Recommendation
    from uoa_detector.live_alpha.option_plan import build_structures

    e = make_engine(f"sqlite:///{tmp_path / 'paper.db'}")
    store.ensure_live_tables(e)
    flow = summarise("NVDA", "synthetic", [_print(i, option_type="put") for i in (1, 2, 3)], S.flow)
    d = evaluate(flow, _price(prev=102), _news(), classify(LIVE_NOW, S.calendar), LIVE_NOW, S)
    assert d.recommendation is Recommendation.BEARISH_SETUP and d.readiness is Readiness.READY
    quotes = (_q("100", ".48", ".50", right="put"), _q("95", ".18", ".20", right="put"))
    views = build_structures(
        "down",
        {q.option_symbol: q for q in quotes},
        100,
        d.stock_plan.target,
        LIVE_NOW,
        LIVE_NOW.date(),
        S.option_plan,
        COSTS,
        S.stock_plan.r_usd,
    )
    chosen = next(v for v in views if v.kind == kind)
    assert chosen.readiness is Readiness.READY and chosen.lots > 0
    c = replace(
        build_card(d, views, "", S),
        instrument=InstrumentChoice(kind, "synthetic chosen priced structure"),
    )
    cfg = S
    if damage == "stock":
        c = replace(c, instrument=InstrumentChoice("stock", "unsupported short"))
    elif damage == "unpriced":
        c = replace(c, options=tuple(replace(v, entry_debit=None) for v in views))
    elif damage == "no_structure":
        c = replace(c, options=())
    elif damage == "disabled":
        cfg = S.model_copy(update={"paper": S.paper.model_copy(update={"enabled": False})})
    elif damage == "pending":
        c = replace(c, readiness=Readiness.QUOTE_PENDING)
    recs = record_changes(e, (c,), "synthetic-bearish-scan", LIVE_NOW)
    ctx = SimpleNamespace(engine=e, settings=cfg, now=LIVE_NOW)
    assert open_papers_for_new_buys(ctx, (c,), recs) == int(damage == "valid")
    assert open_papers_for_new_buys(ctx, (c,), recs) == 0
    papers = store.papers(e)
    assert len(papers) == int(damage == "valid")
    if papers:
        paper = papers[0]
        assert (
            paper.direction == "down"
            and paper.instrument == kind
            and paper.state == "PAPER_PENDING"
        )
        assert paper.entry_price is None
        assert json.loads(paper.plan_json)["auto_paper_scope"] == "downside_source_v1"
        # Continue through the actual frozen-leg repricer/tracker: PUT premiums
        # rise when the underlying falls; do not apply the stock short sign to P&L.
        import asyncio

        from webapp.board.daily_close import ensure_daily_bar_tables
        from webapp.live_alpha.job import track_papers

        ensure_daily_bar_tables(e)
        ctx.costs = COSTS
        ctx.now = LIVE_NOW + timedelta(seconds=1)
        ctx.session = classify(ctx.now, S.calendar)
        ctx.quotes_cache = {
            "NVDA": {q.option_symbol: replace(q, fetched_at=ctx.now) for q in quotes}
        }
        prices = {"NVDA": replace(_price(prev=102, fetched=ctx.now), source_session=ctx.now.date())}
        assert asyncio.run(track_papers(ctx, prices, {}))["filled"] == 1
        opened = store.papers(e)[0]
        assert opened.state == "PAPER_OPEN" and opened.entry_price > 0
        ctx.now = LIVE_NOW + timedelta(seconds=2)
        ctx.session = classify(ctx.now, S.calendar)
        ctx.quotes_cache = {
            "NVDA": {
                q.option_symbol: replace(
                    q,
                    fetched_at=ctx.now,
                    bid=type(q.bid)(".80") if float(q.strike) == 100 else q.bid,
                    ask=type(q.ask)(".82") if float(q.strike) == 100 else q.ask,
                )
                for q in quotes
            }
        }
        prices = {
            "NVDA": replace(
                _price(spot=94, prev=102, fetched=ctx.now), source_session=ctx.now.date()
            )
        }
        assert asyncio.run(track_papers(ctx, prices, {}))["closed"] == 1
        closed = store.papers(e)[0]
        assert closed.state == "CLOSED_SIMULATED" and closed.exit_reason == "hedef"
        assert closed.exit_price > closed.entry_price and closed.pnl_usd > 0
        commission = json.loads(closed.plan_json)["structure"]["commission_usd"]
        assert closed.pnl_usd == round(
            (closed.exit_price - closed.entry_price) * 100 * closed.quantity
            - commission * closed.quantity,
            2,
        )
    e.dispose()
