"""UOA Screener — FastAPI app.

``GET /`` is the Alfa Board (Phase 5.2.A7; ``GET /alfa`` stays as an alias):
one row per ticker and direction with cost, evidence and a mandatory
counter-argument, and the vol-premium board as a section. Also a /gamma vol
board and a /journal. Controls are plain GET forms (no JS framework); a live
run reloads once per board refresh cadence. Templates use Tailwind via CDN —
no build step. Repo calls are wrapped in `_safe` and a global exception handler shows
a clean error page, so a DB/feed blip degrades instead of 500-ing.

Every route except ``GET /health`` is behind HTTP Basic auth (Phase 5.0.9):
credentials come from ``WEB_AUTH_USER`` / ``WEB_AUTH_PASSWORD`` at request
time, and the gate fails closed (503) when either is unset or empty.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import os
import secrets
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import event
from sqlalchemy.engine import Engine
from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse

from webapp import explanations, gamma, journal, pricing
from webapp.board import alfa_page, cards, decision_ledger, fills, outcomes
from webapp.board.copy_tr import IV_NOT_SELL_VOL
from webapp.board.evidence import request_for
from webapp.board.refresher import board_refresh_loop
from webapp.board.settings import BoardSettings, load_board_settings
from webapp.board.signals import BoardSignalReader
from webapp.gamma_live import gamma_refresh_loop
from webapp.repo import RunInfo, SignalRepo
from webapp.vol_board import VolBoardRow, build_vol_board, vol_board_summary
from webapp.worker import live_config_from_env, run_live_worker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping, Sequence

    from starlette.types import ASGIApp, Receive, Scope, Send

    from webapp.board.evidence import LegacyScores
    from webapp.board.signals import BoardPrint

_logger = logging.getLogger(__name__)
_BASE = Path(__file__).parent
_T = TypeVar("_T")


async def _supervise(make_coro: object, name: str) -> None:
    """Keep a long-running background coroutine alive forever: if it ever exits
    (clean return OR an exception that escaped its own loop), log and restart it
    after a backoff. Cancellation (shutdown) propagates. This self-heals the
    'worker silently died and live data went stale' failure mode."""
    while True:
        try:
            await make_coro()  # type: ignore[operator]
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("%s crashed; restarting in 30s", name)
        else:
            _logger.warning("%s exited unexpectedly; restarting in 30s", name)
        await asyncio.sleep(30)


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Phase 5.2.A-fix3 (review RT-4): the board profile and the calibration values
    # the board reads are loaded and validated before the app accepts traffic. A
    # missing or invalid file fails startup, so a broken deploy fails its /health
    # check instead of answering /health 200 while GET / returns 500.
    try:
        _load_board_profiles()
    except Exception:
        _logger.exception("board profiles could not be loaded; refusing to start")
        raise
    # Opt-in live tasks: only start when LIVE_TICKERS (+ UW key + DB) is set.
    # Otherwise the app just serves stored signals (local dev, sample data).
    config = live_config_from_env()
    tasks: list[asyncio.Task[None]] = []
    if config is not None:
        _logger.info("starting live worker + gamma refresh for %s", config["tickers"])
        tasks.append(asyncio.create_task(_supervise(
            lambda: run_live_worker(**config), "live-worker",  # type: ignore[arg-type]
        )))
        tasks.append(asyncio.create_task(_supervise(
            lambda: gamma_refresh_loop(
                tickers=config["tickers"],  # type: ignore[arg-type]
                database_url=config["database_url"],  # type: ignore[arg-type]
            ),
            "gamma-refresh",
        )))
        # Phase 5.2.A2: the Alfa Board refresher (quotes, exit depth) on its own
        # long-lived UW client. GET /alfa only reads what it writes.
        tasks.append(asyncio.create_task(_supervise(
            lambda: board_refresh_loop(
                database_url=config["database_url"],  # type: ignore[arg-type]
            ),
            "board-refresh",
        )))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="UOA Screener", lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Access gate: HTTP Basic auth, fail closed (Phase 5.0.9, contract §3.10)
# ---------------------------------------------------------------------------

_AUTH_USER_ENV = "WEB_AUTH_USER"
_AUTH_PASSWORD_ENV = "WEB_AUTH_PASSWORD"
_OPEN_ROUTE = ("GET", "/health")
_CHALLENGE = {"WWW-Authenticate": 'Basic realm="uoa"'}


def _auth_failure(headers: Headers) -> int | None:
    """None when the request carries the configured credentials, else the
    status to answer with: 503 if the gate is not configured, 401 otherwise.

    Credentials are read from the environment per request. Both the user and
    the password comparison run (constant-time) before they are combined.
    """
    user = os.environ.get(_AUTH_USER_ENV, "")
    password = os.environ.get(_AUTH_PASSWORD_ENV, "")
    if not user or not password:
        return 503
    scheme, _, token = headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic" or not token.strip():
        return 401
    try:
        decoded = base64.b64decode(token.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return 401
    got_user, sep, got_password = decoded.partition(":")
    if not sep:
        return 401
    user_ok = secrets.compare_digest(got_user.encode("utf-8"), user.encode("utf-8"))
    password_ok = secrets.compare_digest(
        got_password.encode("utf-8"), password.encode("utf-8"),
    )
    return None if user_ok and password_ok else 401


class _BasicAuthGate:
    """ASGI middleware in front of every route, mount and 404. Only
    ``GET /health`` (the Railway healthcheck) is open. Lifespan events pass
    through untouched. Credentials are never logged."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return
        if scope["type"] == "http" and (scope["method"], scope["path"]) == _OPEN_ROUTE:
            await self._app(scope, receive, send)
            return
        failure = _auth_failure(Headers(scope=scope))
        if failure is None:
            await self._app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if failure == 503:
            _logger.warning(
                "%s / %s not configured; refusing %s",
                _AUTH_USER_ENV, _AUTH_PASSWORD_ENV, scope["path"],
            )
            response = PlainTextResponse("auth not configured", status_code=503)
        else:
            response = PlainTextResponse(
                "authentication required", status_code=401, headers=_CHALLENGE,
            )
        await response(scope, receive, send)


app.add_middleware(_BasicAuthGate)


templates = Jinja2Templates(directory=str(_BASE / "templates"))
# Phase 5.2.PERF1: compiled templates are kept in a plain dict.
# The cache used to be off (``env.cache = None``) because Jinja's default
# LRUCache key path errors on Python 3.14. Re-parsing is NOT negligible: the
# board's ``{% include "_alfa_row.html" %}`` sits inside the row loop, so an
# uncached environment lexed, parsed and compiled that template once PER ROW —
# ~95% of the board render at 50 rows.
# A dict is Jinja's own "unlimited cache" shape (``create_cache`` returns ``{}``
# for a negative cache size) and never touches the LRUCache path, so the 3.14
# concern stays addressed. The template set is seven files; it cannot grow
# unbounded. ``auto_reload`` stays on, so editing a template still takes effect.
templates.env.cache = {}


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
    "offline_axes": explanations.OFFLINE_LIVE_AXES,
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


def _safe(fn: Callable[[], _T], default: _T) -> _T:
    """Call a repo accessor; on any DB error return a default so one failing
    widget can't blank the whole page. Logged for diagnosis."""
    try:
        return fn()
    except Exception:
        _logger.exception("repo call failed; serving fallback")
        return default


# ---------------------------------------------------------------------------
# Trade journal — forward edge measurement
# ---------------------------------------------------------------------------


@app.get("/journal", response_class=HTMLResponse)
def journal_page(request: Request) -> HTMLResponse:
    repo = _journal()
    trades: list[journal.TradeRow] = _safe(repo.list, [])
    return templates.TemplateResponse(
        request,
        "journal.html",
        {
            "open_trades": [t for t in trades if t.status == "open"],
            "closed_trades": [t for t in trades if t.status == "closed"],
            "stats": journal.aggregate(trades),
            "pnl": journal.option_pnl_usd,
            "excess": journal.directional_excess,
            # Phase 5.2.C3: the fill form on a trade that came from a board card.
            **_journal_fill_context(trades),
            **fills.template_context(),
            **_EXPLAIN,
        },
    )


@app.get("/journal/new", response_class=HTMLResponse)
def journal_new(
    request: Request, run: str = "", event: str = "", ticker: str = "", card_id: str = "",
) -> HTMLResponse:
    signal = _safe(lambda: _repo().get_signal(run, event), None) if run and event else None
    # Vol-board "Log to journal" links here with ?ticker=; with no signal, prefill
    # the ticker (and a vol-structure thesis from its live gamma regime, if any)
    # so the form isn't blank — closing the board -> journal loop.
    prefill_ticker = ticker.upper() if ticker and signal is None else ""
    prefill_thesis = ""
    if prefill_ticker:
        g = _safe(lambda: _gamma().latest().get(prefill_ticker), None)
        if g is not None:
            prefill_thesis = (
                f"Vol-premium board: {prefill_ticker} — "
                f"{explanations.vol_structure(regime=g.regime)}"
            )
    return templates.TemplateResponse(
        request, "trade_form.html",
        {"signal": signal, "run": run, "event": event,
         "prefill_ticker": prefill_ticker, "prefill_thesis": prefill_thesis,
         # Phase 5.2.C1b: set when "Logla" opened this form from a board card.
         "card_id": card_id,
         **_EXPLAIN},
    )


@app.post("/journal")
def journal_create(
    request: Request,
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
    card_id: str = Form(""),
) -> RedirectResponse:
    underlying, spy = pricing.snapshot(ticker)
    trade_id = _journal().add(
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
    if card_id and not _foreign_origin(request):
        # Phase 5.2.C1b: link this trade onto the board card that led to it, once
        # (decision-cards contract §3). A failed link never costs the trade.
        # Phase 5.2.C1-fix2: and never from another site's page. This is the only
        # write to an append-only card outside the guarded card POST; the trade
        # itself is saved either way, so the guard costs a cross-site client
        # nothing but the link it had no business making.
        _safe(lambda: _card_repo().link_trade(card_id, trade_id), False)
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
    latest: dict[str, gamma.GammaContext] = _safe(lambda: _gamma().latest(), {})
    rows = list(latest.values())
    # Rich-IV long-gamma rows first, then cheap-IV short-gamma, then by IV percentile.
    order = {"sell": 0, "buy": 1, "neutral": 2}
    rows.sort(key=lambda g: (order.get(g.vol_signal, 9), -(g.iv_pct or 0)))
    return templates.TemplateResponse(
        request, "gamma.html", {"rows": rows, "iv_not_sell_vol": IV_NOT_SELL_VOL, **_EXPLAIN},
    )


# ---------------------------------------------------------------------------
# Alfa Board (Phase 5.2): GET / since A7, GET /alfa kept as an alias (decision P12)
# ---------------------------------------------------------------------------

_BOARD_READER: BoardSignalReader | None = None
_BOARD_SETTINGS: BoardSettings | None = None


def _board_reader() -> BoardSignalReader:
    global _BOARD_READER
    if _BOARD_READER is None:
        _BOARD_READER = BoardSignalReader()
    return _BOARD_READER


def _board_settings() -> BoardSettings:
    global _BOARD_SETTINGS
    if _BOARD_SETTINGS is None:
        _BOARD_SETTINGS = load_board_settings()
    return _BOARD_SETTINGS


_SPREAD_CUTOFF_PCT: float | None = None


def _spread_cutoff_pct() -> float:
    """``penalty_triggers.spread_pct_threshold`` of the live calibration profile (read only)."""
    global _SPREAD_CUTOFF_PCT
    if _SPREAD_CUTOFF_PCT is None:
        _SPREAD_CUTOFF_PCT = alfa_page.load_spread_cutoff_pct()
    return _SPREAD_CUTOFF_PCT


_LEGACY_SCORES: LegacyScores | None = None


def _legacy_scores() -> LegacyScores:
    """Legacy evidence scores of the live calibration profile (read only; Phase 5.2.A4)."""
    global _LEGACY_SCORES
    if _LEGACY_SCORES is None:
        _LEGACY_SCORES = alfa_page.load_live_legacy_scores()
    return _LEGACY_SCORES


def _load_board_profiles() -> None:
    """Load and validate every profile value the board reads; raises on a missing or invalid file.

    Runs at startup (``_lifespan``) and replaces the lazily cached values, so the
    files are checked on every start rather than on the first page request.
    """
    global _BOARD_SETTINGS, _SPREAD_CUTOFF_PCT, _LEGACY_SCORES
    settings = load_board_settings()
    cutoff = alfa_page.load_spread_cutoff_pct()
    legacy = alfa_page.load_live_legacy_scores()
    _BOARD_SETTINGS, _SPREAD_CUTOFF_PCT, _LEGACY_SCORES = settings, cutoff, legacy


def _now() -> datetime:
    """The board page's wall clock (quote and tape ages, the R-EM1 session date); a seam for tests."""
    return datetime.now(UTC)


# Phase 5.2.PERF9b: which half of a source's time is SQL and which is everything
# else. The four web engines are created with no pool arguments at all, so
# SQLAlchemy's defaults apply (pool_size=5, max_overflow=10, pool_timeout=30) and
# the 19-29 s renders sit just under that timeout. A source can be slow because
# its query is slow or because it waited for a connection; those are different
# bugs with different fixes, and one number cannot tell them apart. The listeners
# below are registered on the Engine CLASS, so every engine is covered without
# touching any repository constructor.
_SOURCE_MARKS: ContextVar[dict[str, float] | None] = ContextVar("_source_marks", default=None)
_SOURCE_NAME: ContextVar[str] = ContextVar("_source_name", default="")


@event.listens_for(Engine, "before_cursor_execute")
def _sql_started(
    conn: Any, cursor: Any, statement: Any, parameters: Any, context: Any, executemany: Any,
) -> None:
    del cursor, statement, parameters, context, executemany
    conn.info["_sql_t0"] = perf_counter()


@event.listens_for(Engine, "after_cursor_execute")
def _sql_finished(
    conn: Any, cursor: Any, statement: Any, parameters: Any, context: Any, executemany: Any,
) -> None:
    del cursor, statement, parameters, context, executemany
    marks = _SOURCE_MARKS.get()
    started = conn.info.pop("_sql_t0", None)
    if marks is None or started is None:
        return
    key = f"{_SOURCE_NAME.get()}.sql"
    marks[key] = marks.get(key, 0.0) + (perf_counter() - started)


@event.listens_for(Engine, "engine_connect")
def _connection_checked_out(conn: Any) -> None:
    del conn
    marks = _SOURCE_MARKS.get()
    if marks is None:
        return
    key = f"{_SOURCE_NAME.get()}.conns"
    marks[key] = marks.get(key, 0.0) + 1.0


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _timed(
    marks: dict[str, float], name: str, fn: Callable[_P, _R],
) -> Callable[_P, _R]:
    """Wrap one board data source so its wall time lands in ``marks``.

    Phase 5.2.PERF9. The page stage has measured anywhere from 0.7 s to 23 s on
    identical code, and nothing said WHICH read cost it: ``build_alfa_page`` runs
    a dozen independent sources and reports one number. Timing them here rather
    than inside the page model is deliberate — only ``webapp.main``'s logger
    reaches the Railway deployment log (REG-9), and a source that is added later
    but not wrapped is caught by a test rather than silently going unmeasured.
    """

    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        start = perf_counter()
        # Restore by assignment rather than with a ContextVar token. The durability
        # scan in test_alfa_card_durability rejects any new destructive-looking call
        # in webapp/, and it is right to: that guard exists so a table-destroying
        # path cannot slip in unnoticed, and a textual scan cannot read intent.
        # Sources never nest, so putting the previous values back is equivalent.
        previous_marks = _SOURCE_MARKS.get()
        previous_name = _SOURCE_NAME.get()
        _SOURCE_MARKS.set(marks)
        _SOURCE_NAME.set(name)
        try:
            return fn(*args, **kwargs)
        finally:
            _SOURCE_NAME.set(previous_name)
            _SOURCE_MARKS.set(previous_marks)
            marks[name] = marks.get(name, 0.0) + (perf_counter() - start)

    return wrapper


def _build_board_page(
    prints: Sequence[BoardPrint] | None,
    *,
    gate_on: bool,
    run_latest_ts: datetime | None,
) -> alfa_page.AlfaPage:
    """The board's page model for one run's prints — the only row-view builder.

    ``GET /`` renders these views and ``POST /alfa/card`` freezes the very same
    object into a decision card (decision-cards contract §3: the server rebuilds
    the row's view model from the database at that moment). One call site is the
    point — a second, drifting builder would let a card claim a row the owner
    never saw, and would quietly stop freezing fields added here later.
    """
    settings = _board_settings()
    marks: dict[str, float] = {}
    page = alfa_page.build_alfa_page(
        prints,
        settings,
        gate_on=gate_on,
        spread_cutoff_pct=_spread_cutoff_pct(),
        now=_now(),
        # Every source stays lazy: with prints=None build_alfa_page returns before
        # calling any of them, and the reader is never touched (a failed run read
        # must still render "veriyi okuyamadım", not raise).
        quote_source=_timed(
            marks, "quote",
            lambda symbols: alfa_page.db_quote_source(_board_reader().engine)(symbols),
        ),
        evidence_source=_timed(
            marks, "evidence",
            lambda run_id, requests: alfa_page.db_evidence_source(_board_reader().engine)(
                run_id, requests,
            ),
        ),
        legacy_scores=_legacy_scores(),
        profile_hash_source=_timed(
            marks, "profile_hash",
            lambda run_id, event_ids: alfa_page.db_profile_hash_source(_board_reader().engine)(
                run_id, event_ids,
            ),
        ),
        delayed_source=_timed(
            marks, "delayed",
            lambda tickers, today: alfa_page.db_delayed_source(
                _board_reader().engine, settings.delayed,
            )(tickers, today),
        ),
        atm_source=_timed(
            marks, "atm",
            lambda tickers: alfa_page.db_atm_source(_board_reader().engine)(tickers),
        ),
        # 5.3.3: the stored daily bars behind the ATR stop. One query, no UW call —
        # the daily_close job already fetched these bars (contract §3.1).
        bars_source=_timed(
            marks, "bars",
            lambda tickers: alfa_page.db_bars_source(_board_reader().engine)(tickers),
        ),
        flow_since_source=_timed(
            marks, "flow_since",
            lambda keys: alfa_page.db_flow_since_source(_board_reader().engine)(keys),
        ),
        oi_source=_timed(
            marks, "oi",
            lambda keys: alfa_page.db_oi_source(_board_reader().engine)(keys),
        ),
        catalyst_source=_timed(
            marks, "catalyst",
            lambda keys, moment: alfa_page.db_catalyst_source(
                _board_reader().engine, settings,
            )(keys, moment),
        ),
        regime_source=_timed(
            marks, "regime",
            lambda moment: alfa_page.db_regime_source(_board_reader().engine)(moment),
        ),
        # B6: the open journal, the focused-ETF holdings and the sectors behind the strip.
        # A failed read is handled inside build_alfa_page, which then claims nothing.
        trades_source=_timed(marks, "trades", lambda: _journal().list("open")),
        holdings_source=_timed(
            marks, "holdings",
            lambda: alfa_page.db_holdings_source(_board_reader().engine)(),
        ),
        sector_source=_timed(
            marks, "sector",
            lambda tickers: alfa_page.db_sector_source(_board_reader().engine)(tickers),
        ),
        profile_resolver=alfa_page.resolve_writing_profile,
        run_latest_ts=run_latest_ts,
    )
    # Phase 5.2.PERF9: one line per render naming which source cost what. The page
    # stage has measured 0.7 s and 23 s on identical code; this is what tells them apart.
    _logger.warning(
        "board sources: %s",
        " ".join(f"{key}={value:.3f}" for key, value in sorted(marks.items())),
    )
    return page


# ---------------------------------------------------------------------------
# Decision cards (Phase 5.2.C1b; docs/phase-5.2-decision-cards-acceptance.md §3)
# ---------------------------------------------------------------------------

_CARDS: cards.CardRepo | None = None


def _card_repo() -> cards.CardRepo:
    """The append-only decision-card repository, bound to the board reader's engine."""
    global _CARDS
    engine = _board_reader().engine
    if _CARDS is None or _CARDS.engine is not engine:
        _CARDS = cards.CardRepo(engine)
    return _CARDS


def _same_origin(request: Request) -> bool:
    """True when the request's ``Origin`` (else its ``Referer``) names its own host.

    A CSRF guard on top of Basic auth (contract §3): a card POST driven from
    another site's page carries that site's origin and is refused before
    anything is written. A request carrying neither header is refused too.
    """
    host = request.headers.get("host", "")
    if not host:
        return False
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if value:
            return urlsplit(value).netloc == host
    return False


def _foreign_origin(request: Request) -> bool:
    """True when the request declares an ``Origin`` (else a ``Referer``) of another host.

    ``POST /journal`` is the app's older route, and the contract keeps its shape
    ("the journal route and ``TradeRow`` are unchanged, apart from an optional
    hidden ``card_id`` form field"): it does not require the header the card POST
    requires, so a client sending neither still saves its trade and links its
    card. But it is the second route that writes to an append-only card, so a
    request declaring another host never gets to touch one — a cross-site form
    POST from a browser always carries that site's ``Origin``, and that is
    exactly the request this refuses.

    Deliberately not the complement of :func:`_same_origin`: a missing header is
    refused there and allowed here. Each rule states what its own route needs.
    """
    host = request.headers.get("host", "")
    for header in ("origin", "referer"):
        value = request.headers.get(header)
        if value:
            return urlsplit(value).netloc != host
    return False


def _calibration_hash(run_id: str, view: alfa_page.AlfaRowView) -> str | None:
    """Content hash of the calibration profile that scored the row's source print.

    ``None`` when the row has no readable source print or the read fails: the
    card then records that the hash is missing instead of claiming one.
    """
    event_id = request_for(view.row).event_id
    if not event_id:
        return None
    empty: Mapping[str, str] = {}
    hashes = _safe(
        lambda: alfa_page.db_profile_hash_source(_board_reader().engine)(run_id, [event_id]),
        empty,
    )
    return hashes.get(event_id)


def _write_decision_card(
    view: alfa_page.AlfaRowView, *, decision: cards.Decision, run_id: str,
    page: alfa_page.AlfaPage,
) -> str:
    """Freeze one rebuilt row view, and the page it sat on, into a card; return its id."""
    board_hash = _board_settings().content_hash()
    calibration_hash = _calibration_hash(run_id, view)
    return _card_repo().write_card(
        decision=decision,
        ticker=view.row.ticker,
        direction=cards.direction_value(view.row.direction),
        run_id=run_id,
        dominant_option_symbol=view.symbol,
        card=cards.build_card_view(
            view,
            board_profile_hash=board_hash,
            calibration_profile_hash=calibration_hash,
            page=page,
        ),
        board_profile_hash=board_hash,
        calibration_profile_hash=calibration_hash,
    )


@app.get("/", response_class=HTMLResponse)
@app.get("/alfa", response_class=HTMLResponse)
def alfa_board(request: Request, run: str = "", gate: str = "", pas: str = "") -> HTMLResponse:
    """The Alfa Board: one row per (ticker, side-aware direction) over the whole selected run.

    Served at ``GET /`` since Phase 5.2.A7; ``GET /alfa`` stays as an alias
    (decision P12). Reads the database only; no Unusual Whales call (contract
    §4.1). A failed read renders an explicit "could not read" state, never an
    empty board. The cost gate ``Alabileceklerimi göster`` is on unless
    ``gate=off`` (§5 A2). Each row carries its evidence strip, reason and
    counter-argument; the combined score is only in the row's audit block
    (§5 A4-A6). ``Bugün temiz aday yok`` renders when no row is a clean
    candidate (R-EM1). The vol-premium board stays as a section, with the
    R-IV1 sentence (§5 A7).
    """
    _t0 = perf_counter()
    settings = _board_settings()
    runs_read: list[RunInfo] | None = _safe(_repo().runs, None)
    _t_runs = perf_counter()
    runs = runs_read or []
    run_ids = {r.run_id for r in runs}
    active = run if run in run_ids else (runs[0].run_id if runs else None)
    current = next((r for r in runs if r.run_id == active), None)
    prints: list[BoardPrint] | None = None if runs_read is None else []
    if active is not None:
        run_id = active
        prints = _safe(lambda: _board_reader().load_run(run_id), None)
    _t_prints = perf_counter()
    page = _build_board_page(
        prints,
        gate_on=gate != alfa_page.GATE_OFF_PARAM,
        run_latest_ts=current.latest_ts if current is not None else None,
    )
    _t_page = perf_counter()
    gamma_ctx: dict[str, gamma.GammaContext] = _safe(lambda: _gamma().latest(), {})
    vol_rows: list[VolBoardRow] = _safe(lambda: build_vol_board(
        gamma_ctx,
        earnings={t: c.next_earnings for t, c in gamma_ctx.items()},
        now=datetime.now(UTC).date(),
        rich_threshold=settings.regime.vol_rich_iv_pct,
    ), [])
    # Phase 5.2.C1b: ``?pas=`` confirms a recorded pass, and only when that card
    # really exists — a made-up link must never claim something was written.
    pas_card = _safe(lambda: _card_repo().get_card(pas), None) if pas else None
    _t_vol = perf_counter()
    _response = templates.TemplateResponse(
        request,
        "alfa.html",
        {
            "page": page,
            "runs": runs,
            "run": active or "",
            "board_path": request.url.path,
            # A live run reloads once per board refresh cadence (new prints and quotes).
            "live_reload_seconds": (
                settings.refresh.cadence_seconds if current is not None and current.is_live else None
            ),
            "vol_board": vol_rows,
            # The same cutoff the ranking used, as the page states it. It was spelled
            # out a third time in the template divider (registry REG-3).
            "vol_rich_iv_rank": round(settings.regime.vol_rich_iv_pct * 100),
            "vol_summary": vol_board_summary(
                vol_rows, rich_threshold=settings.regime.vol_rich_iv_pct,
            ),
            "vol_structure": explanations.vol_structure,
            "vol_read": explanations.vol_read,
            "vol_caveat": explanations.vol_caveat,
            "pas_card": pas_card,
            "pas_recorded_text": cards.pas_recorded_text,
            **alfa_page.template_context(),
        },
    )
    # Phase 5.2.PERF3 (temporary): production render time swings between 0.4 s and
    # 9.2 s on identical code, so the stages are timed in production. WARNING, not
    # INFO: application INFO never reaches the Railway deployment log (REG-6).
    _logger.warning(
        "board timing: runs=%.3f prints=%.3f page=%.3f vol=%.3f render=%.3f total=%.3f rows=%d prints_n=%d",
        _t_runs - _t0, _t_prints - _t_runs, _t_page - _t_prints, _t_vol - _t_page,
        perf_counter() - _t_vol, perf_counter() - _t0, len(page.rows), page.print_count,
    )
    return _response


@app.post("/alfa/card", response_model=None)  # the union of two Response types is not a model
def alfa_card(
    request: Request,
    decision: str = Form(...),
    # The identity fields are namespaced on purpose: the board must never carry a
    # control named "ticker", "label", "sort" or "min_score" again — those were the
    # old dashboard's score filters, and test_board_honesty pins that they are gone.
    card_run_id: str = Form(...),
    card_ticker: str = Form(...),
    card_direction: str = Form(...),
    gate: str = Form(""),
) -> RedirectResponse | PlainTextResponse:
    """Record the owner's own decision on one board row (decision-cards contract §3).

    The form carries the row's identity only. The server rebuilds that row's view
    model from the database at this moment and freezes it into the card, so no
    client-sent value can change what a card says. The rebuild reads the database
    only — no Unusual Whales call. Nothing is written when the row cannot be
    rebuilt: a card describing a row nobody saw would be worse than no card.
    """
    if not _same_origin(request):
        return PlainTextResponse(cards.CARD_COPY["forbidden_origin"], status_code=403)
    chosen = cards.decision_for(decision)
    if chosen is None:
        return PlainTextResponse(cards.CARD_COPY["unknown_decision"], status_code=400)
    prints = _safe(lambda: _board_reader().load_run(card_run_id), None) if card_run_id else None
    # The press carries the owner's cost-gate state, so the page frozen into the
    # card is the board as it actually was. The gate only groups rows into
    # sections — it never filters ``page.views`` — so the row is found either way.
    page = _build_board_page(
        prints, gate_on=gate != alfa_page.GATE_OFF_PARAM, run_latest_ts=None,
    )
    view = next(
        (
            v for v in page.views
            if v.row.ticker == card_ticker and v.row.direction == card_direction
        ),
        None,
    )
    if view is None:
        return PlainTextResponse(cards.CARD_COPY["row_not_found"], status_code=404)
    try:
        card_id = _write_decision_card(view, decision=chosen, run_id=card_run_id, page=page)
    except Exception:
        # Deliberately not _safe: a failed write must never look like a recorded
        # decision. And it must not fall through to the generic error page, whose
        # "Nothing is lost" is written for read-only views — here the press was not
        # recorded, and an unrecorded pass cannot be reconstructed tomorrow.
        _logger.exception(
            "decision card could not be written for %s %s", card_ticker, card_direction,
        )
        return PlainTextResponse(cards.CARD_COPY["write_failed"], status_code=500)
    if chosen == "log":
        return RedirectResponse(f"/journal/new?{urlencode({'card_id': card_id})}", status_code=303)
    params = {"run": card_run_id, "pas": card_id}
    if gate == alfa_page.GATE_OFF_PARAM:
        params["gate"] = alfa_page.GATE_OFF_PARAM
    # The target is built here, never from a client value: no open redirect.
    return RedirectResponse(f"/?{urlencode(params)}", status_code=303)


# ---------------------------------------------------------------------------
# Fill capture (Phase 5.2.C3; docs/phase-5.2-decision-cards-acceptance.md §5)
# ---------------------------------------------------------------------------

_FILLS: fills.FillRepo | None = None
_NO_FILLS: tuple[fills.Fill, ...] = ()


def _fill_repo() -> fills.FillRepo:
    """The append-only fill repository, bound to the board reader's engine."""
    global _FILLS
    engine = _board_reader().engine
    if _FILLS is None or _FILLS.engine is not engine:
        _FILLS = fills.FillRepo(engine)
    return _FILLS


def _assumed_quote(card: cards.DecisionCard) -> fills.AssumedQuote | None:
    """The quote frozen into ``card``, or ``None`` when its snapshot holds no usable pair.

    Always read on the server from the card itself (contract §5). A client value
    could not be trusted here: the whole point of a fill record is to measure the
    board's own cost assumption, and an assumption the client may rewrite
    measures nothing.
    """
    return _safe(lambda: fills.assumed_quote_from_card(card.card), None)


def _journal_fill_context(trades: Sequence[journal.TradeRow]) -> dict[str, object]:
    """The decision cards linked onto these trades, with their quotes and fills.

    Two reads for the whole page, never one per trade, and both wrapped: a
    database without the ``alfa_`` tables (the journal predates the board) leaves
    the journal exactly as it was.
    """
    empty: Mapping[str, cards.DecisionCard] = {}
    linked = _safe(
        lambda: cards.cards_for_trades(_board_reader().engine, [t.id for t in trades]), empty,
    )
    if not linked:
        return {"fill_cards": empty, "fill_quotes": {}, "fill_rows": {}}
    rows: dict[str, list[fills.Fill]] = {card.id: [] for card in linked.values()}
    # Filtered in SQL, not in Python: a global read spends its row budget on the
    # whole table, so a linked trade's own records would vanish from this page
    # once other cards had filled it up.
    for fill in _safe(lambda: _fill_repo().list_fills(card_ids=list(rows)), _NO_FILLS):
        if fill.card_id in rows:
            rows[fill.card_id].append(fill)
    return {
        "fill_cards": linked,
        "fill_quotes": {card.id: _assumed_quote(card) for card in linked.values()},
        "fill_rows": {card_id: tuple(found) for card_id, found in rows.items()},
    }


# ---------------------------------------------------------------------------
# The pass ledger (Phase 5.2.C2a; docs/phase-5.2-decision-cards-acceptance.md §4)
# ---------------------------------------------------------------------------

_OUTCOMES: outcomes.OutcomeRepo | None = None
_NO_OUTCOMES: tuple[outcomes.Outcome, ...] = ()


def _outcome_repo() -> outcomes.OutcomeRepo:
    """The append-only outcome repository, bound to the board reader's engine."""
    global _OUTCOMES
    engine = _board_reader().engine
    if _OUTCOMES is None or _OUTCOMES.engine is not engine:
        _OUTCOMES = outcomes.OutcomeRepo(engine)
    return _OUTCOMES


@app.get("/defter", response_class=HTMLResponse)
def defter_page(request: Request, karar: str = "", hisse: str = "") -> HTMLResponse:
    """The pass ledger: every recorded decision, taken and passed (contract §4).

    Reads the database only; no Unusual Whales call. Two queries serve the whole
    page — one for the cards the filters select, one for those cards' outcomes —
    and the card list is capped at ``ledger.max_cards_per_page``, disclosed on
    the page, so the route stays fast once the ledger holds thousands of cards.

    An unknown ``karar`` value lists every decision rather than nothing: a
    mistyped filter must not look like an empty ledger. A failed read renders an
    explicit "could not read" state that says nothing was deleted.
    """
    settings = _board_settings()
    limit = settings.ledger.max_cards_per_page
    decision = cards.decision_for(karar.strip())
    ticker = hisse.strip().upper()
    listed: tuple[cards.DecisionCard, ...] | None = _safe(
        lambda: _card_repo().list_cards(decision=decision, ticker=ticker or None, limit=limit),
        None,
    )
    found = listed or ()
    # Bounded by THIS page: one row per listed card per horizon, plus one. A read
    # that leant on the repository's module default would start dropping stored
    # outcomes the moment ledger.max_cards_per_page x horizons outgrew it, and a
    # dropped row renders as "henüz hesaplanmadı" — a card that was measured
    # reading as one that was not.
    needed = len(found) * len(settings.outcomes.horizons_trading_days) + 1
    stored = (
        _safe(
            lambda: _outcome_repo().list_outcomes(
                card_ids=[c.id for c in found], limit=needed,
            ),
            _NO_OUTCOMES,
        )
        if found
        else _NO_OUTCOMES
    )
    page = decision_ledger.build_ledger_page(
        found,
        stored,
        horizons=settings.outcomes.horizons_trading_days,
        min_n=settings.fills.min_n_for_stats,
        limit=limit,
        decision=decision or "",
        ticker=ticker,
        load_failed=listed is None,
    )
    return templates.TemplateResponse(
        request,
        "defter.html",
        {
            "page": page,
            **decision_ledger.template_context(),
            **outcomes.template_context(),
            **_EXPLAIN,
        },
    )


@app.get("/kart/{card_id}", response_class=HTMLResponse)
def card_page(request: Request, card_id: str, dolum: str = "") -> HTMLResponse:
    """One decision card, its assumed quote and its fills (contract §5).

    Reads the database only; no Unusual Whales call. A card id that names nothing
    renders the missing-card state with a 404 — never an invented card. ``?dolum=``
    confirms a recorded fill, and only when that fill really belongs to this card.

    The slippage summary on this page covers every recorded fill, not this card's
    handful: the cost assumption is a property of the board, and one card's fills
    will never reach ``fills.min_n_for_stats``. Below that sample size the summary
    shows counts and nothing else (§5).
    """
    card = _safe(lambda: _card_repo().get_card(card_id), None)
    recorded = _safe(lambda: _fill_repo().get_fill(dolum), None) if dolum else None
    found = card is not None
    window = _board_settings().fills.max_fills_per_summary
    card_fills = (
        _safe(lambda: _fill_repo().list_fills(card_id=card_id, limit=window), _NO_FILLS)
        if found
        else _NO_FILLS
    )
    # One row past the window tells the page whether the sample IS every stored
    # fill or only its newest page, so the scope line can say which (§5: the
    # summary must not claim a wider sample than it read).
    sampled = (
        _safe(lambda: _fill_repo().list_fills(limit=window + 1), _NO_FILLS)
        if found
        else _NO_FILLS
    )
    capped = len(sampled) > window
    every_fill = sampled[:window] if capped else sampled
    context: dict[str, object] = {
        "card": card,
        "can_fill": card is not None and cards.is_logged(card),
        "assumed": _assumed_quote(card) if card is not None else None,
        "fills": card_fills,
        "summaries": fills.side_summaries(
            every_fill, min_n=_board_settings().fills.min_n_for_stats,
        ),
        "summary_scope": fills.stats_scope_text(len(every_fill), capped=capped),
        "card_meta": (
            fills.card_meta_text(card.ticker, card.direction, card.created_at, card.id)
            if card is not None
            else ""
        ),
        "decision_text": (
            fills.FILL_COPY["decision_log" if cards.is_logged(card) else "decision_pas"]
            if card is not None
            else ""
        ),
        "trade_line": fills.trade_line_text(card.trade_id) if card is not None else "",
        "recorded_line": (
            fills.recorded_text(recorded, ticker=card.ticker)
            if recorded is not None and card is not None and recorded.card_id == card.id
            else None
        ),
        **fills.template_context(),
        **_EXPLAIN,
    }
    return templates.TemplateResponse(
        request, "kart.html", context, status_code=200 if found else 404,
    )


@app.post("/alfa/fill", response_model=None)  # the union of two Response types is not a model
def alfa_fill(
    request: Request,
    # Namespaced like the card form's fields, for the same reason: the board must
    # never carry a control named "ticker", "label", "sort" or "min_score" again.
    fill_card_id: str = Form(...),
    fill_side: str = Form(...),
    fill_price: float = Form(...),
    fill_contracts: float = Form(...),
) -> RedirectResponse | PlainTextResponse:
    """Record one actual fill against a logged card's frozen quote (contract §5).

    The form carries the owner's own three numbers and the card's id. Everything
    else — the assumed bid, ask and mid, the quote's age at the card, and the
    journal trade the fill belongs to — is read from the card on the server. The
    route reads and writes the database only; no Unusual Whales call.

    Nothing is written unless the card exists, records a taken decision, and
    carries a usable quote: slippage measured against an invented quote would be
    a fabricated number in an append-only table.
    """
    if not _same_origin(request):
        return PlainTextResponse(cards.CARD_COPY["forbidden_origin"], status_code=403)
    side = fills.side_for(fill_side)
    if side is None:
        return PlainTextResponse(fills.FILL_COPY["unknown_side"], status_code=400)
    if not fills.usable_numbers(fill_price, fill_contracts):
        # Finite AND positive: a form field typed ``float`` accepts ``inf``,
        # ``1e400`` and ``nan``, none of which a fill table can ever unrecord.
        return PlainTextResponse(fills.FILL_COPY["bad_numbers"], status_code=400)
    card = _safe(lambda: _card_repo().get_card(fill_card_id), None) if fill_card_id else None
    if card is None:
        return PlainTextResponse(fills.FILL_COPY["card_not_found"], status_code=404)
    if not cards.is_logged(card):
        return PlainTextResponse(fills.FILL_COPY["not_logged"], status_code=400)
    quote = _assumed_quote(card)
    if quote is None:
        return PlainTextResponse(fills.FILL_COPY["no_assumed_quote"], status_code=400)
    try:
        fill_id = _fill_repo().write_fill(
            card_id=card.id,
            trade_id=card.trade_id,
            side=side,
            fill_price=fill_price,
            contracts=fill_contracts,
            quote=quote,
        )
    except Exception:
        # Deliberately not _safe: a failed write must never look like a recorded
        # fill, and the generic error page ("Nothing is lost") is written for
        # read-only views. Here nothing was recorded, and the owner must retype it.
        _logger.exception("fill could not be written for card %s", card.id)
        return PlainTextResponse(fills.FILL_COPY["write_failed"], status_code=500)
    # Built from the stored card id, never from a client value: no open redirect.
    return RedirectResponse(f"/kart/{card.id}?{urlencode({'dolum': fill_id})}", status_code=303)


@app.get("/health")
def health() -> dict[str, str | bool]:
    """Liveness, plus the commit this instance is running.

    Phase 5.2.RAIL2: a deploy check must be able to prove WHICH build answered,
    not only that something answered. Railway sets RAILWAY_GIT_COMMIT_SHA; the
    value is a public commit id, never a secret.
    """
    sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")
    return {"ok": True, "sha": sha[:7] if sha else "unknown"}
