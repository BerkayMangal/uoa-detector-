"""UOA Screener — FastAPI app.

Read-only dashboard over the detector's signals, a /gamma vol board, and a
/journal. Filters/sort are a plain GET form (no JS framework); live runs
auto-refresh via a meta reload. Templates use Tailwind via CDN — no build
step. Repo calls are wrapped in `_safe` and a global exception handler shows
a clean error page, so a DB/feed blip degrades instead of 500-ing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from webapp import explanations, gamma, journal, pricing
from webapp.repo import SignalFilters, SignalRepo
from webapp.worker import live_config_from_env, run_live_worker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_logger = logging.getLogger(__name__)
_BASE = Path(__file__).parent


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Opt-in live worker: only starts when LIVE_TICKERS (+ UW key + DB) is set.
    # Otherwise the app just serves stored signals (local dev, sample data).
    config = live_config_from_env()
    task: asyncio.Task[None] | None = None
    if config is not None:
        _logger.info("starting live worker for %s", config["tickers"])
        task = asyncio.create_task(run_live_worker(**config))  # type: ignore[arg-type]
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="UOA Screener", lifespan=_lifespan)
templates = Jinja2Templates(directory=str(_BASE / "templates"))
# Disable Jinja's template cache: its LRU cache key path errors on Python
# 3.14. Templates are tiny, so re-parsing per request is negligible.
templates.env.cache = None


@app.exception_handler(Exception)
async def _on_error(request: Request, exc: Exception) -> HTMLResponse:
    # Terminal-grade: never show a raw stack trace. Log it, show a clean page.
    _logger.exception("unhandled error on %s", request.url.path)
    try:
        return templates.TemplateResponse(request, "error.html", {}, status_code=500)
    except Exception:
        return HTMLResponse(
            "<h1>Temporarily unavailable</h1><p>Refresh in a moment.</p>",
            status_code=500,
        )

# Shared template context — passed per render (not via env.globals, which
# can poison Jinja's template cache key with unhashable dict/list values).
_EXPLAIN = {
    "axes": explanations.AXES,
    "glossary": explanations.GLOSSARY,
    "label_meaning": explanations.label_meaning,
    "headline": explanations.headline,
    "conviction": explanations.conviction,
}

_REPO: SignalRepo | None = None
_JOURNAL: journal.JournalRepo | None = None


def _repo() -> SignalRepo:
    global _REPO
    if _REPO is None:
        _REPO = SignalRepo()
    return _REPO


def _journal() -> journal.JournalRepo:
    global _JOURNAL
    if _JOURNAL is None:
        _JOURNAL = journal.JournalRepo()
    return _JOURNAL


_GAMMA: gamma.GammaRepo | None = None


def _gamma() -> gamma.GammaRepo:
    global _GAMMA
    if _GAMMA is None:
        _GAMMA = gamma.GammaRepo()
    return _GAMMA


def _filters(
    ticker: str, label: str, min_score: float | None,
    sort: str, run_id: str | None,
) -> SignalFilters:
    return SignalFilters(
        ticker=ticker or None, label=label or None,
        min_score=min_score, sort=sort or "score",
        run_id=run_id, limit=150,
    )


def _safe(fn: object, default: object) -> object:
    """Call a repo accessor; on any DB error return a default so one failing
    widget can't blank the whole page. Logged for diagnosis."""
    try:
        return fn()  # type: ignore[operator]
    except Exception:
        _logger.exception("repo call failed; serving fallback")
        return default


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request, ticker: str = "", label: str = "",
    min_score: float | None = None, sort: str = "score", run: str = "",
) -> HTMLResponse:
    repo = _repo()
    runs = _safe(repo.runs, [])
    # Default to the newest run (today's live flow if the worker is running,
    # else the sample backtest). An explicit ?run= overrides.
    run_ids = {r.run_id for r in runs}  # type: ignore[attr-defined]
    active = run if run in run_ids else (runs[0].run_id if runs else None)  # type: ignore[index]
    current = next((r for r in runs if r.run_id == active), None)  # type: ignore[attr-defined]
    flt = _filters(ticker, label, min_score, sort, active)
    matched = _safe(lambda: repo.signals(flt), [])
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "signals": matched,
            "tickers": _safe(lambda: repo.tickers(active), []),
            "labels": _safe(lambda: repo.labels(active), []),
            "total": _safe(lambda: repo.count(active), 0),
            "shown": len(matched),  # type: ignore[arg-type]
            "runs": runs, "current": current, "run": active or "",
            "ticker": ticker, "label": label, "min_score": min_score,
            "sort": sort,
            "gamma": _safe(lambda: _gamma().latest(), {}),
            **_EXPLAIN,
        },
    )


# ---------------------------------------------------------------------------
# Trade journal — forward edge measurement
# ---------------------------------------------------------------------------


@app.get("/journal", response_class=HTMLResponse)
def journal_page(request: Request) -> HTMLResponse:
    repo = _journal()
    trades = _safe(repo.list, [])
    return templates.TemplateResponse(
        request,
        "journal.html",
        {
            "open_trades": [t for t in trades if t.status == "open"],  # type: ignore[attr-defined]
            "closed_trades": [t for t in trades if t.status == "closed"],  # type: ignore[attr-defined]
            "stats": journal.aggregate(trades),  # type: ignore[arg-type]
            "pnl": journal.option_pnl_usd,
            "excess": journal.directional_excess,
            **_EXPLAIN,
        },
    )


@app.get("/journal/new", response_class=HTMLResponse)
def journal_new(request: Request, run: str = "", event: str = "") -> HTMLResponse:
    signal = _safe(lambda: _repo().get_signal(run, event), None) if run and event else None
    return templates.TemplateResponse(
        request, "trade_form.html",
        {"signal": signal, "run": run, "event": event, **_EXPLAIN},
    )


@app.post("/journal")
def journal_create(
    ticker: str = Form(...),
    direction: str = Form(...),
    instrument: str = Form(...),
    contracts: float = Form(...),
    entry_price: float = Form(...),
    strike: float | None = Form(None),
    expiry: str = Form(""),
    thesis: str = Form(""),
    signal_run_id: str = Form(""),
    signal_event_id: str = Form(""),
    signal_score: float | None = Form(None),
    signal_label: str = Form(""),
) -> RedirectResponse:
    underlying, spy = pricing.snapshot(ticker)
    _journal().add(
        entry_ts=datetime.now(UTC),
        ticker=ticker.upper(),
        direction=direction,
        instrument=instrument,
        contracts=contracts,
        entry_price=entry_price,
        strike=strike,
        expiry=expiry or None,
        entry_underlying_px=underlying,
        entry_spy_px=spy,
        signal_run_id=signal_run_id or None,
        signal_event_id=signal_event_id or None,
        signal_score=signal_score,
        signal_label=signal_label or None,
        thesis=thesis,
    )
    return RedirectResponse("/journal", status_code=303)


@app.post("/journal/{trade_id}/close")
def journal_close(
    trade_id: str,
    exit_price: float = Form(...),
    exit_reason: str = Form(""),
) -> RedirectResponse:
    trade = _journal().get(trade_id)
    if trade is not None:
        underlying, spy = pricing.snapshot(trade.ticker)
        _journal().close(
            trade_id,
            exit_ts=datetime.now(UTC),
            exit_price=exit_price,
            exit_underlying_px=underlying,
            exit_spy_px=spy,
            exit_reason=exit_reason or None,
        )
    return RedirectResponse("/journal", status_code=303)


# ---------------------------------------------------------------------------
# Gamma + vol overview board
# ---------------------------------------------------------------------------


@app.get("/gamma", response_class=HTMLResponse)
def gamma_page(request: Request) -> HTMLResponse:
    rows = list(_safe(lambda: _gamma().latest(), {}).values())  # type: ignore[union-attr]
    # Sell-vol candidates first, then by IV percentile (richest first).
    order = {"sell": 0, "buy": 1, "neutral": 2}
    rows.sort(key=lambda g: (order.get(g.vol_signal, 9), -(g.iv_pct or 0)))
    return templates.TemplateResponse(request, "gamma.html", {"rows": rows, **_EXPLAIN})


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}
