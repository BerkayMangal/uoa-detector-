"""UOA Screener — FastAPI app.

Read-only dashboard over the detector's signals. ``/`` renders the full
page; ``/signals`` returns just the card list (HTMX swaps it in on filter
change and polls it for live updates). One Python service; templates use
Tailwind + HTMX via CDN, so there is no build step.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from webapp import explanations
from webapp.repo import SignalFilters, SignalRepo

if TYPE_CHECKING:
    pass

_BASE = Path(__file__).parent
app = FastAPI(title="UOA Screener")
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
    since_min: int | None, sort: str,
) -> SignalFilters:
    since = (
        datetime.now(UTC) - timedelta(minutes=since_min)
        if since_min else None
    )
    return SignalFilters(
        ticker=ticker or None, label=label or None,
        min_score=min_score, since=since, sort=sort or "score", limit=150,
    )


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request, ticker: str = "", label: str = "",
    min_score: float | None = None, since_min: int | None = None,
    sort: str = "score",
) -> HTMLResponse:
    repo = _repo()
    flt = _filters(ticker, label, min_score, since_min, sort)
    matched = repo.signals(flt)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "signals": matched,
            "tickers": repo.tickers(),
            "labels": repo.labels(),
            "total": repo.count(),
            "shown": len(matched),
            "ticker": ticker, "label": label, "min_score": min_score,
            "since_min": since_min, "sort": sort,
            **_EXPLAIN,
        },
    )


@app.get("/health")
def health() -> dict[str, bool]:
    return {"ok": True}
