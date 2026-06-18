"""UOA Screener — FastAPI app.

Read-only dashboard over the detector's signals. ``/`` renders the full
page; ``/signals`` returns just the card list (HTMX swaps it in on filter
change and polls it for live updates). One Python service; templates use
Tailwind + HTMX via CDN, so there is no build step.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from webapp import explanations
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

# Shared template context — passed per render (not via env.globals, which
# can poison Jinja's template cache key with unhashable dict/list values).
_EXPLAIN = {
    "axes": explanations.AXES,
    "glossary": explanations.GLOSSARY,
    "label_meaning": explanations.label_meaning,
}

_REPO: SignalRepo | None = None


def _repo() -> SignalRepo:
    global _REPO
    if _REPO is None:
        _REPO = SignalRepo()
    return _REPO


def _filters(
    ticker: str, label: str, min_score: float | None,
    since_min: int | None, sort: str, run_id: str | None,
) -> SignalFilters:
    since = (
        datetime.now(UTC) - timedelta(minutes=since_min)
        if since_min else None
    )
    return SignalFilters(
        ticker=ticker or None, label=label or None,
        min_score=min_score, since=since, sort=sort or "score",
        run_id=run_id, limit=150,
    )


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request, ticker: str = "", label: str = "",
    min_score: float | None = None, since_min: int | None = None,
    sort: str = "score", run: str = "",
) -> HTMLResponse:
    repo = _repo()
    runs = repo.runs()
    # Default to the newest run (today's live flow if the worker is running,
    # else the sample backtest). An explicit ?run= overrides.
    run_ids = {r.run_id for r in runs}
    active = run if run in run_ids else (runs[0].run_id if runs else None)
    current = next((r for r in runs if r.run_id == active), None)
    flt = _filters(ticker, label, min_score, since_min, sort, active)
    matched = repo.signals(flt)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "signals": matched,
            "tickers": repo.tickers(active),
            "labels": repo.labels(active),
            "total": repo.count(active),
            "shown": len(matched),
            "runs": runs, "current": current, "run": active or "",
            "ticker": ticker, "label": label, "min_score": min_score,
            "since_min": since_min, "sort": sort,
            **_EXPLAIN,
        },
    )


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}
