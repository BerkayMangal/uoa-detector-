"""The two Unusual Whales reads Live Alpha adds (contract §3, §9).

- ``GET /api/news/headlines?ticker=`` → ``alfa_live_news`` + one
  ``alfa_live_news_check`` row per call. The UW schema has no URL and no id, so a
  headline is keyed by our hash of (source, headline, created_at), and the page
  never builds a link. ``created_at`` is kept under its provider name.
- ``GET /api/stock/{t}/option-contracts?option_symbol[]=`` through the board's own
  ``fetch_contract_quotes``. A symbol UW drops does not exist.

Every call first reserves budget with the board's atomic, database-backed
``quota_ledger.reserve``. A refusal skips the call and says so; a reservation is
never refunded, because a timed-out request may still have been counted by UW.
401/403, 404, 429, daily limit, transient errors and a bad payload are separate
states, and none of them is turned into "no news" or into a price.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from uoa_detector.live_alpha.model import CheckState, NewsCheck, NewsItem, OptionQuote
from uoa_detector.sources.unusual_whales.client import (
    UnusualWhalesAuthError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
)
from webapp.board import quota_ledger
from webapp.board.db import session_factory
from webapp.board.quotes import fetch_contract_quotes
from webapp.live_alpha.inputs import as_utc
from webapp.live_alpha.store import LiveNews, LiveNewsCheck, dumps, new_id

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.engine import Engine

_logger = logging.getLogger(__name__)

NEWS_PATH = "/api/news/headlines"


class JsonClient(Protocol):
    async def request_json(
        self, path: str, *, params: dict[str, Any] | None = ..., method: str = ...,
    ) -> dict[str, Any]: ...


@dataclass
class Budget:
    """Quota reservation against the shared ledger, counting what this cycle used."""

    engine: Engine
    day: date
    cap: int
    client: object
    reserved: int = 0
    refused: int = 0

    def take(self, now: datetime) -> bool:
        observed = getattr(self.client, "last_daily_request_count", None)
        got = quota_ledger.reserve(
            self.engine, day=self.day, n=1, cap=self.cap,
            observed=observed if isinstance(observed, int) else None, now=now,
        )
        if got.granted:
            self.reserved += 1
        else:
            self.refused += 1
        return got.granted


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------


def _ref(source: str, headline: str, created: str) -> str:
    return hashlib.sha256(f"{source}|{headline}|{created}".encode()).hexdigest()[:32]


def _parse_time(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return as_utc(parsed)


def parse_headlines(payload: object, ticker: str, now: datetime) -> list[NewsItem]:
    """Rows mentioning ``ticker``. Raises ValueError on an unexpected shape."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("headlines payload has no data list")
    out: list[NewsItem] = []
    want = ticker.upper()
    for row in payload["data"]:
        if not isinstance(row, dict):
            continue
        headline = row.get("headline")
        created = _parse_time(row.get("created_at"))
        if not isinstance(headline, str) or not headline.strip() or created is None:
            continue
        tickers = tuple(str(t).upper() for t in (row.get("tickers") or []) if t)
        if tickers and want not in tickers:
            continue   # a headline that does not name the ticker is not this ticker's news
        source = str(row.get("source") or "kaynak belirtilmemiş")
        sentiment = row.get("sentiment")
        out.append(NewsItem(
            headline=headline.strip(),
            source=source,
            provider_created_at=created,
            first_seen_at=now,
            sentiment=str(sentiment) if isinstance(sentiment, str) and sentiment else None,
            is_major=bool(row.get("is_major")),
            tags=tuple(str(t) for t in (row.get("tags") or []) if t),
            tickers=tickers,
            ref=_ref(source, headline.strip(), str(row.get("created_at"))),
        ))
    # The same story syndicated several times is one story (same headline text).
    seen: set[str] = set()
    unique: list[NewsItem] = []
    for item in sorted(out, key=lambda i: i.provider_created_at):
        key = item.headline.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _store_items(engine: Engine, items: Sequence[NewsItem]) -> dict[str, datetime]:
    """Insert new headlines; return each ref's first_seen_at (the stored one wins)."""
    first_seen: dict[str, datetime] = {}
    if not items:
        return first_seen
    refs = [i.ref for i in items]
    with session_factory(engine)() as s:
        for ref, seen in s.execute(select(LiveNews.ref, LiveNews.first_seen_at).where(LiveNews.ref.in_(refs))):
            first_seen[ref] = as_utc(seen)
    for item in items:
        if item.ref in first_seen:
            continue
        try:
            with session_factory(engine)() as s, s.begin():
                s.add(LiveNews(
                    ref=item.ref, headline=item.headline, source=item.source,
                    provider_created_at=item.provider_created_at, first_seen_at=item.first_seen_at,
                    sentiment=item.sentiment, is_major=item.is_major,
                    tags_json=dumps(list(item.tags)), tickers_json=dumps(list(item.tickers)),
                ))
            first_seen[item.ref] = item.first_seen_at
        except IntegrityError:
            first_seen[item.ref] = item.first_seen_at
    return first_seen


def _write_check(engine: Engine, ticker: str, at: datetime, state: CheckState, detail: str, refs: list[str]) -> None:
    with session_factory(engine)() as s, s.begin():
        s.add(LiveNewsCheck(
            check_id=new_id(), ticker=ticker, checked_at=at, state=state.value, detail=detail,
            refs_json=dumps(refs),
        ))


def _load_items(engine: Engine, refs: Sequence[str]) -> list[NewsItem]:
    if not refs:
        return []
    with session_factory(engine)() as s:
        rows = s.execute(select(LiveNews).where(LiveNews.ref.in_(list(refs)))).scalars().all()
        return [
            NewsItem(
                headline=r.headline, source=r.source,
                provider_created_at=as_utc(r.provider_created_at), first_seen_at=as_utc(r.first_seen_at),
                sentiment=r.sentiment, is_major=r.is_major,
                tags=tuple(json.loads(r.tags_json)), tickers=tuple(json.loads(r.tickers_json)), ref=r.ref,
            )
            for r in rows
        ]


def last_check(engine: Engine, ticker: str) -> NewsCheck | None:
    stmt = (
        select(LiveNewsCheck).where(LiveNewsCheck.ticker == ticker)
        .order_by(LiveNewsCheck.checked_at.desc()).limit(1)
    )
    with session_factory(engine)() as s:
        row = s.execute(stmt).scalars().first()
        if row is None:
            return None
        state, detail, at, refs = CheckState(row.state), row.detail, as_utc(row.checked_at), json.loads(row.refs_json)
    return NewsCheck(ticker=ticker, state=state, checked_at=at, items=tuple(_load_items(engine, refs)), detail=detail)


async def news_for(
    engine: Engine, client: JsonClient, budget: Budget, ticker: str, now: datetime,
    *, refresh_seconds: int, limit: int,
) -> NewsCheck:
    """The ticker's news state: a fresh stored check, or a new call, or an honest failure."""
    previous = last_check(engine, ticker)
    if previous is not None and previous.checked_at is not None:
        age = (now - previous.checked_at).total_seconds()
        if 0 <= age < refresh_seconds and previous.state is not CheckState.FAILED:
            return previous
    if not budget.take(now):
        if previous is not None and previous.checked_at is not None and previous.state is not CheckState.FAILED:
            stale = (now - previous.checked_at).total_seconds() > 2 * refresh_seconds
            if stale:
                return NewsCheck(ticker=ticker, state=CheckState.STALE, checked_at=previous.checked_at,
                                 items=previous.items, detail="kota reddi; son kontrol eski")
            return previous
        return NewsCheck(ticker=ticker, state=CheckState.FAILED, checked_at=now, detail="günlük kota reddi")
    try:
        payload = await client.request_json(NEWS_PATH, params={"ticker": ticker, "limit": limit})
    except UnusualWhalesDailyLimitError:
        state, detail = CheckState.FAILED, "UW günlük istek limiti"
        payload = None
    except UnusualWhalesNotFoundError:
        state, detail, payload = CheckState.CHECKED_NONE, "UW 404/422: veri yok", {"data": []}
    except UnusualWhalesAuthError as exc:
        state, detail, payload = CheckState.FAILED, f"UW yetki/istek hatası ({type(exc).__name__})", None
    except UnusualWhalesRateLimitError:
        state, detail, payload = CheckState.FAILED, "UW 429 hız sınırı", None
    except Exception as exc:
        state, detail, payload = CheckState.FAILED, f"UW geçici hata ({type(exc).__name__})", None
    else:
        state, detail = CheckState.CHECKED_NONE, ""
    items: list[NewsItem] = []
    if payload is not None:
        try:
            items = parse_headlines(payload, ticker, now)
        except ValueError as exc:
            state, detail = CheckState.FAILED, f"beklenmeyen yanıt biçimi ({exc})"
        else:
            if items:
                state = CheckState.CHECKED_FOUND
    first_seen = _store_items(engine, items)
    items = [
        NewsItem(**{**item.__dict__, "first_seen_at": first_seen.get(item.ref, item.first_seen_at)})
        for item in items
    ]
    _write_check(engine, ticker, now, state, detail, [i.ref for i in items])
    if state is CheckState.FAILED:
        _logger.warning("live alpha news %s failed: %s", ticker, detail)
    return NewsCheck(ticker=ticker, state=state, checked_at=now, items=tuple(items), detail=detail)


# ---------------------------------------------------------------------------
# Contract quotes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    right: str
    strike: Decimal
    expiry: date


@dataclass(frozen=True)
class QuotesResult:
    quotes: dict[str, OptionQuote]
    state: str          # human text for the card when nothing is priced


async def quotes_for(
    client: JsonClient, budget: Budget, ticker: str, specs: Sequence[ContractSpec],
    now: datetime, *, in_session: bool,
) -> QuotesResult:
    if not specs:
        return QuotesResult(quotes={}, state="aday kontrat yok (vade bulunamadı)")
    if not budget.take(now):
        return QuotesResult(quotes={}, state="günlük kota reddi; kotasyon alınmadı")
    by_symbol: Mapping[str, ContractSpec] = {s.symbol: s for s in specs}
    try:
        fetched = await fetch_contract_quotes(client, ticker, list(by_symbol))
    except UnusualWhalesDailyLimitError:
        return QuotesResult(quotes={}, state="UW günlük istek limiti")
    except UnusualWhalesAuthError as exc:
        return QuotesResult(quotes={}, state=f"UW yetki/istek hatası ({type(exc).__name__})")
    if fetched.degraded:
        return QuotesResult(quotes={}, state="kotasyon çağrısı başarısız (geçici hata)")
    out: dict[str, OptionQuote] = {}
    for q in fetched.quotes:
        spec = by_symbol.get(q.option_symbol)
        if spec is None or not q.returned:
            continue
        out[q.option_symbol] = OptionQuote(
            option_symbol=q.option_symbol, underlying=ticker.upper(), right=spec.right,
            strike=spec.strike, expiry=spec.expiry,
            bid=Decimal(str(q.nbbo_bid)) if q.nbbo_bid is not None else None,
            ask=Decimal(str(q.nbbo_ask)) if q.nbbo_ask is not None else None,
            fetched_at=now, in_session=in_session, volume=q.volume, open_interest=q.open_interest,
        )
    if not out:
        return QuotesResult(quotes={}, state="UW aday sembollerin hiçbirini döndürmedi")
    return QuotesResult(quotes=out, state="")


def utc_now() -> datetime:
    return datetime.now(UTC)


def older_than(at: datetime | None, now: datetime, seconds: int) -> bool:
    return at is None or now - at > timedelta(seconds=seconds)
