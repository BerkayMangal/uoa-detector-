"""Regime band data layer for the Alfa Board (Phase 5.2.B5a, contract §6 B5).

``alfa_regime`` is an append-only history of (source, fetched_at, source_time,
payload_json). Tide persistence is counted from it. Payloads are the parsed
readings below, not raw responses: spot-exposures alone is about 74 KB per call.

Sources (per refresher cycle unless noted):
- ``market_tide``: ``/api/market/market-tide?interval_5m=true``. Values are running
  totals since the open. The newest bucket is still filling, so only the last
  COMPLETE bucket (start + bucket interval <= fetch time) is stored. The interval
  comes from the row spacing.
- ``spot_exposures:{SPY,QQQ}``: ``/api/stock/{t}/spot-exposures``. The latest
  regular-session ``gamma_per_one_percent_move_oi`` (USD per 1% move, OI basis)
  plus the session's first regular-session value. Pre-market rows are dropped:
  time >= 09:30 ET on the session date (13:30Z in EDT, 14:30Z in EST).
- ``gex_levels:{SPY,QQQ}``: ``/api/stock/{t}/gex-levels?source=oi``. The flip is the
  nearest per-strike sign change, labelled that way. It is context only; the board
  computes no flip of its own.
- ``iv_term:SPY``: ``/api/stock/SPY/volatility/term-structure``. ATM IV at the DTE
  nearest each profile anchor, ignoring ``dte <= exclude_event_hump_max_dte``.
- ``vix_spot``: ``/api/stock/VIX/volatility/term-structure``. VIX spot derived as
  ``implied_move / implied_move_perc`` (vol-of-vol rows; not a futures curve).
- ``greek_exposure:{SPY,QQQ}`` (daily): ``/api/stock/{t}/greek-exposure``. One-year
  percentile of daily net share gamma and the count of negative days.

The VIX futures term structure (``/api/volatility/vix-term-structure``) returns 403
``volatility_scope_required`` on this key. It is never called; the band states it
is out of scope.

Pure: ``build_regime_band`` turns loaded readings into sentence parts, chips and
the three tripwires, with the ``BoardSettings.regime`` cutoffs. A source older than
``regime.max_source_age_seconds`` renders ``bilinmiyor``. The daily greek-exposure
reading counts as fresh when it was fetched on the current ET date. The band never
enters the evidence count.

UW errors: NotFound is no data; rate limit, transient and an open breaker mark the
source degraded and the job continues; daily limit and auth errors propagate.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final, Literal, TypeVar, cast
from zoneinfo import ZoneInfo

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import DateTime, String, Text, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from uoa_detector.sources.unusual_whales.client import (
    CircuitBreakerOpenError,
    UnusualWhalesDailyLimitError,
    UnusualWhalesNotFoundError,
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
)
from webapp.board.db import AlfaBase

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from sqlalchemy import Table
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import sessionmaker

    from webapp.board.settings import BoardSettings, RegimeSettings
    from webapp.board.uw_errors import JsonClient

MARKET_TIDE_PATH: Final = "/api/market/market-tide"
SPOT_EXPOSURES_PATH: Final = "/api/stock/{ticker}/spot-exposures"
GEX_LEVELS_PATH: Final = "/api/stock/{ticker}/gex-levels"
TERM_STRUCTURE_PATH: Final = "/api/stock/{ticker}/volatility/term-structure"
GREEK_EXPOSURE_PATH: Final = "/api/stock/{ticker}/greek-exposure"

GAMMA_TICKERS: Final[tuple[str, ...]] = ("SPY", "QQQ")
CURVE_TICKER: Final = "SPY"
VIX_TICKER: Final = "VIX"

SOURCE_TIDE: Final = "market_tide"
SOURCE_CURVE: Final = "iv_term:SPY"
SOURCE_VIX: Final = "vix_spot"


def spot_source(ticker: str) -> str:
    return f"spot_exposures:{ticker}"


def gex_source(ticker: str) -> str:
    return f"gex_levels:{ticker}"


def history_source(ticker: str) -> str:
    return f"greek_exposure:{ticker}"


# ---------------------------------------------------------------------------
# Frozen copy (contract §6 B5 strings are byte for byte)
# ---------------------------------------------------------------------------

TRIPWIRE_HEAD: Final = "fikrimi ne değiştirir"
UNKNOWN: Final = "bilinmiyor"
VIX_CURVE_OUT_OF_SCOPE: Final = "VIX vade yapısı: kapsam-dışı (volatilite eklentisi yok)"
VIX_SPOT_TEMPLATE: Final = "VIX ≈ {value} (türetilmiş)"
BASE_RATE_TEMPLATE: Final = "son 1 yılın {k}/{n} gününde kısa gamma"
FLIP_LABEL: Final = "en yakın strike işaret değişimi"
CURVE_LABEL: Final = "SPY IV vadesi"
TIDE_LABEL: Final = "Piyasa akışı"

TIDE_WORDS: Final[Mapping[str, str]] = {
    "up": "yukarı yönlü", "down": "aşağı yönlü", "flat": "yatay",
}
GAMMA_WORDS: Final[Mapping[str, str]] = {
    "short": "kısa gamma", "long": "uzun gamma", "flat": "gamma ölü bant içinde",
}
CURVE_WORDS: Final[Mapping[str, str]] = {
    "contango": "contango", "flat": "düz", "inverted": "ters",
}
TIDE_TEMPLATE: Final = (
    "Piyasa akışı {word}: net prim {premium}, net hacim {volume} kontrat ({bucket} ET kovası)"
)
GAMMA_TEMPLATE: Final = "{ticker} {word}: {value}/%1"
PERCENTILE_TEMPLATE: Final = "1 yıllık yüzdelik %{pct} ({as_of} itibarıyla)"
FLIP_TEMPLATE: Final = "{ticker} en yakın strike işaret değişimi {level} (spottan %{distance})"
CURVE_TEMPLATE: Final = (
    "SPY IV vadesi {word} (IV{short_dte}G %{short_iv} · IV{long_dte}G %{long_iv})"
)
SOURCE_UNKNOWN_TEMPLATE: Final = "{label}: bilinmiyor"
TRIPWIRE_GAMMA_TEMPLATE: Final = (
    "SPY veya QQQ gamma ölü bandı (±{deadband}) seans başına göre öbür tarafa geçerse: {values}"
)
TRIPWIRE_GAMMA_VALUE: Final = "{ticker} {value} (seans başı {first})"
TRIPWIRE_TIDE_TEMPLATE: Final = (
    "Akış yön değiştirip {needed} tamamlanmış kova boyunca kalırsa (ölü bant ±{deadband}): "
    "şu an {premium}, ters yönde {run}/{needed} kova"
)
TRIPWIRE_CURVE_TEMPLATE: Final = (
    "SPY IV{short_dte}G ≥ IV{long_dte}G + {deadband} vol puanı olursa: "
    "şu an %{short_iv} / %{long_iv}"
)
TRIPWIRE_STATUS: Final[Mapping[str, str]] = {
    "fired": "tetiklendi", "not_fired": "tetiklenmedi", "unknown": "bilinmiyor",
}
# Phase 5.2.B5b: between a tripwire line and its status, on the rendered band.
STATUS_SEPARATOR: Final = " — "
PART_SEPARATOR: Final = " · "

_ET: Final = ZoneInfo("America/New_York")
# Market structure, not a cutoff: the regular session opens at 09:30 ET.
_SESSION_OPEN_ET: Final = time(9, 30)
_MIDNIGHT: Final = time(0, 0)
_PERCENT: Final = 100.0
_BILLION: Final = 1e9
_MILLION: Final = 1e6
_THOUSAND: Final = 1e3
_DEGRADED: Final = (
    UnusualWhalesRateLimitError,
    UnusualWhalesTransientError,
    CircuitBreakerOpenError,
)


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------


class AlfaRegime(AlfaBase):
    """Append-only history of parsed regime readings."""

    __tablename__ = "alfa_regime"

    source: Mapped[str] = mapped_column(String, primary_key=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    source_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload_json: Mapped[str] = mapped_column(Text)


def ensure_regime_tables(engine: Engine) -> None:
    """Create ``alfa_regime`` if missing. Append-only: never dropped or reset."""
    cast("Table", AlfaRegime.__table__).create(engine, checkfirst=True)


# ---------------------------------------------------------------------------
# Readings (stored as payload_json)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TideReading:
    bucket_start: datetime
    bucket_end: datetime
    trade_date: date | None
    net_call_premium: float
    net_put_premium: float
    net_premium: float  # net_call_premium - net_put_premium
    net_volume: int | None
    fetched_at: datetime


@dataclass(frozen=True)
class GammaReading:
    ticker: str
    time: datetime
    price: float | None
    gamma_oi: float
    session_first_time: datetime
    session_first_gamma_oi: float
    fetched_at: datetime


@dataclass(frozen=True)
class GexLevels:
    ticker: str
    time: datetime | None
    gamma_flip: float | None
    call_wall: float | None
    put_wall: float | None
    gamma_magnet: float | None
    fetched_at: datetime


@dataclass(frozen=True)
class CurveReading:
    ticker: str
    as_of: date | None
    short_expiry: date | None
    short_dte: int
    short_iv: float
    long_expiry: date | None
    long_dte: int
    long_iv: float
    fetched_at: datetime


@dataclass(frozen=True)
class VixReading:
    as_of: date | None
    vix_spot: float
    rows_used: int
    fetched_at: datetime


@dataclass(frozen=True)
class GammaHistory:
    ticker: str
    as_of: date
    latest_net: float  # call_gamma + put_gamma, share gamma
    percentile: float  # percent of days with net <= latest
    negative_days: int
    total_days: int
    fetched_at: datetime


_TIDE_ADAPTER: Final = TypeAdapter(TideReading)
_GAMMA_ADAPTER: Final = TypeAdapter(GammaReading)
_GEX_ADAPTER: Final = TypeAdapter(GexLevels)
_CURVE_ADAPTER: Final = TypeAdapter(CurveReading)
_VIX_ADAPTER: Final = TypeAdapter(VixReading)
_HISTORY_ADAPTER: Final = TypeAdapter(GammaHistory)

_R = TypeVar("_R")


@dataclass(frozen=True)
class RegimeRefreshReport:
    fetched_at: datetime
    stored: tuple[str, ...]
    no_data: tuple[str, ...]
    degraded: tuple[str, ...]
    requests: int


@dataclass(frozen=True)
class RegimeInputs:
    tide_buckets: tuple[TideReading, ...]  # today's complete buckets, oldest first
    gamma: tuple[GammaReading, ...]
    gex: tuple[GexLevels, ...]
    curve: CurveReading | None
    vix: VixReading | None
    history: tuple[GammaHistory, ...]


@dataclass(frozen=True)
class RegimeChip:
    key: str
    state: str
    known: bool
    text: str


@dataclass(frozen=True)
class Tripwire:
    key: Literal["gamma", "tide", "curve"]
    fired: bool | None
    status_text: str
    text: str


@dataclass(frozen=True)
class RegimeBand:
    sentence_parts: tuple[str, ...]
    sentence: str
    chips: tuple[RegimeChip, ...]
    tripwire_head: str
    tripwires: tuple[Tripwire, ...]


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


async def refresh_regime(
    client: JsonClient,
    sessions: sessionmaker[Session],
    *,
    settings: BoardSettings,
    now: datetime,
) -> RegimeRefreshReport:
    """Cycle job: tide, SPY/QQQ spot exposures and gex levels, SPY and VIX term structure (7 calls)."""
    reports = [
        await refresh_market_tide(client, sessions, settings=settings, now=now),
        await refresh_spot_exposures(client, sessions, settings=settings, now=now),
        await refresh_gex_levels(client, sessions, settings=settings, now=now),
        await refresh_iv_term_structure(client, sessions, settings=settings, now=now),
        await refresh_vix_spot(client, sessions, settings=settings, now=now),
    ]
    return _merge(reports, _as_utc(now))


async def refresh_market_tide(
    client: JsonClient, sessions: sessionmaker[Session], *,
    settings: BoardSettings, now: datetime,
) -> RegimeRefreshReport:
    del settings
    fetched_at = _as_utc(now)
    acc = _Acc()
    await _one(
        client, sessions, acc, source=SOURCE_TIDE, path=MARKET_TIDE_PATH,
        params={"interval_5m": "true"}, fetched_at=fetched_at,
        encode=lambda resp: _encode(
            _parse_tide(resp, fetched_at=fetched_at), _TIDE_ADAPTER, lambda r: r.bucket_end,
        ),
    )
    return acc.report(fetched_at)


async def refresh_spot_exposures(
    client: JsonClient, sessions: sessionmaker[Session], *,
    tickers: Sequence[str] = GAMMA_TICKERS, settings: BoardSettings, now: datetime,
) -> RegimeRefreshReport:
    del settings
    fetched_at = _as_utc(now)
    acc = _Acc()
    for ticker in tickers:
        symbol = ticker.upper()
        await _one(
            client, sessions, acc, source=spot_source(symbol),
            path=SPOT_EXPOSURES_PATH.format(ticker=symbol), params=None, fetched_at=fetched_at,
            encode=_spot_encoder(symbol, fetched_at),
        )
    return acc.report(fetched_at)


async def refresh_gex_levels(
    client: JsonClient, sessions: sessionmaker[Session], *,
    tickers: Sequence[str] = GAMMA_TICKERS, settings: BoardSettings, now: datetime,
) -> RegimeRefreshReport:
    del settings
    fetched_at = _as_utc(now)
    acc = _Acc()
    for ticker in tickers:
        symbol = ticker.upper()
        await _one(
            client, sessions, acc, source=gex_source(symbol),
            path=GEX_LEVELS_PATH.format(ticker=symbol), params={"source": "oi"},
            fetched_at=fetched_at,
            encode=_gex_encoder(symbol, fetched_at),
        )
    return acc.report(fetched_at)


async def refresh_iv_term_structure(
    client: JsonClient, sessions: sessionmaker[Session], *,
    settings: BoardSettings, now: datetime,
) -> RegimeRefreshReport:
    fetched_at = _as_utc(now)
    acc = _Acc()
    cfg = settings.regime
    await _one(
        client, sessions, acc, source=SOURCE_CURVE,
        path=TERM_STRUCTURE_PATH.format(ticker=CURVE_TICKER), params=None, fetched_at=fetched_at,
        encode=lambda resp: _encode(
            _parse_curve(resp, cfg=cfg, fetched_at=fetched_at), _CURVE_ADAPTER, lambda _r: None,
        ),
    )
    return acc.report(fetched_at)


async def refresh_vix_spot(
    client: JsonClient, sessions: sessionmaker[Session], *,
    settings: BoardSettings, now: datetime,
) -> RegimeRefreshReport:
    del settings
    fetched_at = _as_utc(now)
    acc = _Acc()
    await _one(
        client, sessions, acc, source=SOURCE_VIX,
        path=TERM_STRUCTURE_PATH.format(ticker=VIX_TICKER), params=None, fetched_at=fetched_at,
        encode=lambda resp: _encode(
            _parse_vix(resp, fetched_at=fetched_at), _VIX_ADAPTER, lambda _r: None,
        ),
    )
    return acc.report(fetched_at)


async def refresh_gamma_history(
    client: JsonClient, sessions: sessionmaker[Session], *,
    tickers: Sequence[str] = GAMMA_TICKERS, settings: BoardSettings, now: datetime,
) -> RegimeRefreshReport:
    """Daily job: one-year daily net gamma percentile and negative-day base rate."""
    del settings
    fetched_at = _as_utc(now)
    acc = _Acc()
    for ticker in tickers:
        symbol = ticker.upper()
        await _one(
            client, sessions, acc, source=history_source(symbol),
            path=GREEK_EXPOSURE_PATH.format(ticker=symbol), params=None, fetched_at=fetched_at,
            encode=_history_encoder(symbol, fetched_at),
        )
    return acc.report(fetched_at)


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


# The engine whose regime history is known to exist (the render path checks once).
_tables_ready_for: list[Engine] = []


def read_regime_inputs(engine: Engine, *, now: datetime) -> RegimeInputs:
    """The band's inputs for the render path: database reads only, and no UW call.

    Creates the table once per engine, so a fresh database renders the band's
    ``bilinmiyor`` chips instead of failing.
    """
    if not _tables_ready_for or _tables_ready_for[0] is not engine:
        ensure_regime_tables(engine)
        _tables_ready_for[:] = [engine]
    with Session(engine) as session:
        return load_regime_inputs(session, now=now)


def load_regime_inputs(session: Session, *, now: datetime) -> RegimeInputs:
    """Latest reading per source fetched at or before ``now``, plus today's tide buckets."""
    now_utc = _as_utc(now)
    day_start = datetime.combine(now_utc.astimezone(_ET).date(), _MIDNIGHT, tzinfo=_ET)
    tide_rows = session.scalars(
        select(AlfaRegime)
        .where(
            AlfaRegime.source == SOURCE_TIDE,
            AlfaRegime.fetched_at >= day_start.astimezone(UTC),
            AlfaRegime.fetched_at <= now_utc,
        )
        .order_by(AlfaRegime.fetched_at),
    ).all()
    buckets: dict[datetime, TideReading] = {}
    for row in tide_rows:
        reading = _decode(_TIDE_ADAPTER, row.payload_json)
        if reading is not None and reading.bucket_start.astimezone(_ET) >= day_start:
            buckets[reading.bucket_start] = reading
    gamma = _latest_each(
        session, [spot_source(t) for t in GAMMA_TICKERS], _GAMMA_ADAPTER, now_utc,
    )
    gex = _latest_each(session, [gex_source(t) for t in GAMMA_TICKERS], _GEX_ADAPTER, now_utc)
    history = _latest_each(
        session, [history_source(t) for t in GAMMA_TICKERS], _HISTORY_ADAPTER, now_utc,
    )
    return RegimeInputs(
        tide_buckets=tuple(buckets[k] for k in sorted(buckets)),
        gamma=gamma, gex=gex,
        curve=_latest(session, SOURCE_CURVE, _CURVE_ADAPTER, now_utc),
        vix=_latest(session, SOURCE_VIX, _VIX_ADAPTER, now_utc),
        history=history,
    )


# ---------------------------------------------------------------------------
# Pure band
# ---------------------------------------------------------------------------


def build_regime_band(
    inputs: RegimeInputs, *, settings: BoardSettings, now: datetime,
) -> RegimeBand:
    cfg = settings.regime
    now_utc = _as_utc(now)
    max_age = timedelta(seconds=cfg.max_source_age_seconds)

    def fresh(stamp: datetime | None, fetched_at: datetime) -> bool:
        return now_utc - (stamp or fetched_at) <= max_age

    tide = inputs.tide_buckets[-1] if inputs.tide_buckets else None
    tide_ok = tide is not None and fresh(tide.bucket_end, tide.fetched_at)
    gamma = {g.ticker: g for g in inputs.gamma if fresh(g.time, g.fetched_at)}
    gex = {g.ticker: g for g in inputs.gex if fresh(g.time, g.fetched_at)}
    today = now_utc.astimezone(_ET).date()
    history = {
        h.ticker: h for h in inputs.history if h.fetched_at.astimezone(_ET).date() == today
    }
    curve = inputs.curve if inputs.curve and fresh(None, inputs.curve.fetched_at) else None
    vix = inputs.vix if inputs.vix and fresh(None, inputs.vix.fetched_at) else None

    chips: list[RegimeChip] = []
    parts: list[str] = []

    if tide is not None and tide_ok:
        word = _side(tide.net_premium, cfg.tide_deadband_usd, pos="up", neg="down")
        text = TIDE_TEMPLATE.format(
            word=TIDE_WORDS[word], premium=_usd(tide.net_premium),
            volume=_count(tide.net_volume) if tide.net_volume is not None else UNKNOWN,
            bucket=f"{tide.bucket_start.astimezone(_ET):%H:%M}",
        )
        chips.append(RegimeChip(key="tide", state=word, known=True, text=text))
    else:
        text = SOURCE_UNKNOWN_TEMPLATE.format(label=TIDE_LABEL)
        chips.append(RegimeChip(key="tide", state="unknown", known=False, text=text))
    parts.append(text)

    for ticker in GAMMA_TICKERS:
        reading = gamma.get(ticker)
        if reading is None:
            text = SOURCE_UNKNOWN_TEMPLATE.format(label=f"{ticker} gamma")
            chips.append(RegimeChip(key=f"gamma:{ticker}", state="unknown", known=False, text=text))
            parts.append(text)
            continue
        word = _side(reading.gamma_oi, cfg.gamma_deadband_usd, pos="long", neg="short")
        text = GAMMA_TEMPLATE.format(
            ticker=ticker, word=GAMMA_WORDS[word], value=_usd(reading.gamma_oi),
        )
        parts.append(text)
        hist = history.get(ticker)
        magnitude = (
            PERCENTILE_TEMPLATE.format(pct=f"{hist.percentile:.0f}", as_of=f"{hist.as_of:%d.%m}")
            + PART_SEPARATOR
            + BASE_RATE_TEMPLATE.format(k=hist.negative_days, n=hist.total_days)
            if hist is not None
            else SOURCE_UNKNOWN_TEMPLATE.format(label="1 yıllık yüzdelik")
        )
        chips.append(RegimeChip(
            key=f"gamma:{ticker}", state=word, known=True,
            text=text + PART_SEPARATOR + magnitude,
        ))

    for ticker in GAMMA_TICKERS:
        levels = gex.get(ticker)
        spot = gamma[ticker].price if ticker in gamma else None
        if levels is None or levels.gamma_flip is None or spot is None or spot <= 0:
            chips.append(RegimeChip(
                key=f"flip:{ticker}", state="unknown", known=False,
                text=SOURCE_UNKNOWN_TEMPLATE.format(label=f"{ticker} {FLIP_LABEL}"),
            ))
            continue
        distance = (levels.gamma_flip - spot) / spot * _PERCENT
        chips.append(RegimeChip(
            key=f"flip:{ticker}", state="context", known=True,
            text=FLIP_TEMPLATE.format(
                ticker=ticker, level=f"{levels.gamma_flip:g}", distance=f"{distance:+.1f}",
            ),
        ))

    curve_state = _curve_state(curve, cfg) if curve is not None else None
    if curve is not None and curve_state is not None:
        text = CURVE_TEMPLATE.format(
            word=CURVE_WORDS[curve_state], short_dte=curve.short_dte,
            short_iv=_vol(curve.short_iv), long_dte=curve.long_dte, long_iv=_vol(curve.long_iv),
        )
        chips.append(RegimeChip(key="curve", state=curve_state, known=True, text=text))
    else:
        text = SOURCE_UNKNOWN_TEMPLATE.format(label=CURVE_LABEL)
        chips.append(RegimeChip(key="curve", state="unknown", known=False, text=text))
    parts.append(text)

    chips.append(RegimeChip(
        key="vix_curve", state="out_of_scope", known=False, text=VIX_CURVE_OUT_OF_SCOPE,
    ))
    if vix is not None:
        text = VIX_SPOT_TEMPLATE.format(value=f"{vix.vix_spot:.1f}")
        chips.append(RegimeChip(key="vix_spot", state="context", known=True, text=text))
    else:
        text = SOURCE_UNKNOWN_TEMPLATE.format(label=VIX_TICKER)
        chips.append(RegimeChip(key="vix_spot", state="unknown", known=False, text=text))
    parts.append(text)

    tripwires = (
        _gamma_tripwire(gamma, cfg),
        _tide_tripwire(inputs.tide_buckets if tide_ok else (), cfg),
        _curve_tripwire(curve, cfg),
    )
    return RegimeBand(
        sentence_parts=tuple(parts), sentence=PART_SEPARATOR.join(parts), chips=tuple(chips),
        tripwire_head=TRIPWIRE_HEAD, tripwires=tripwires,
    )


def tide_reversal(
    buckets: Sequence[TideReading], deadband_usd: float,
) -> tuple[int, int, int]:
    """(run sign, run length, prior non-flat sign) over complete buckets, newest last."""
    signs = [_sign(b.net_premium, deadband_usd) for b in buckets]
    if not signs:
        return 0, 0, 0
    run_sign = signs[-1]
    run = 0
    for sign in reversed(signs):
        if sign != run_sign:
            break
        run += 1
    prior = next((s for s in reversed(signs[: len(signs) - run]) if s != 0), 0)
    return run_sign, run, prior


# ---------------------------------------------------------------------------
# Private: tripwires
# ---------------------------------------------------------------------------


def _gamma_tripwire(gamma: Mapping[str, GammaReading], cfg: RegimeSettings) -> Tripwire:
    values: list[str] = []
    fired_any = False
    unknown_any = False
    for ticker in GAMMA_TICKERS:
        reading = gamma.get(ticker)
        if reading is None:
            unknown_any = True
            values.append(f"{ticker} {UNKNOWN}")
            continue
        first = _sign(reading.session_first_gamma_oi, cfg.gamma_deadband_usd)
        latest = _sign(reading.gamma_oi, cfg.gamma_deadband_usd)
        fired_any = fired_any or (first != 0 and latest != 0 and first != latest)
        values.append(TRIPWIRE_GAMMA_VALUE.format(
            ticker=ticker, value=_usd(reading.gamma_oi),
            first=_usd(reading.session_first_gamma_oi),
        ))
    fired: bool | None = True if fired_any else (None if unknown_any else False)
    return Tripwire(
        key="gamma", fired=fired, status_text=_status(fired),
        text=TRIPWIRE_GAMMA_TEMPLATE.format(
            deadband=_usd(cfg.gamma_deadband_usd), values=", ".join(values),
        ),
    )


def _tide_tripwire(buckets: Sequence[TideReading], cfg: RegimeSettings) -> Tripwire:
    needed = cfg.tide_persistence_buckets
    if not buckets:
        return Tripwire(
            key="tide", fired=None, status_text=_status(None),
            text=TRIPWIRE_TIDE_TEMPLATE.format(
                needed=needed, deadband=_usd(cfg.tide_deadband_usd), premium=UNKNOWN, run=0,
            ),
        )
    run_sign, run, prior = tide_reversal(buckets, cfg.tide_deadband_usd)
    reversing = run_sign != 0 and prior == -run_sign
    progress = run if reversing else 0
    fired = reversing and run >= needed
    return Tripwire(
        key="tide", fired=fired, status_text=_status(fired),
        text=TRIPWIRE_TIDE_TEMPLATE.format(
            needed=needed, deadband=_usd(cfg.tide_deadband_usd),
            premium=_usd(buckets[-1].net_premium), run=progress,
        ),
    )


def _curve_tripwire(curve: CurveReading | None, cfg: RegimeSettings) -> Tripwire:
    deadband = f"{cfg.iv_curve_deadband_vol_pts:g}"
    if curve is None:
        return Tripwire(
            key="curve", fired=None, status_text=_status(None),
            text=TRIPWIRE_CURVE_TEMPLATE.format(
                short_dte=cfg.iv_anchor_short_dte, long_dte=cfg.iv_anchor_long_dte,
                deadband=deadband, short_iv=UNKNOWN, long_iv=UNKNOWN,
            ),
        )
    fired = _curve_state(curve, cfg) == "inverted"
    return Tripwire(
        key="curve", fired=fired, status_text=_status(fired),
        text=TRIPWIRE_CURVE_TEMPLATE.format(
            short_dte=curve.short_dte, long_dte=curve.long_dte, deadband=deadband,
            short_iv=_vol(curve.short_iv), long_iv=_vol(curve.long_iv),
        ),
    )


def _curve_state(
    curve: CurveReading, cfg: RegimeSettings,
) -> Literal["contango", "flat", "inverted"]:
    short_pts = curve.short_iv * _PERCENT
    long_pts = curve.long_iv * _PERCENT
    if short_pts >= long_pts + cfg.iv_curve_deadband_vol_pts:
        return "inverted"
    if short_pts <= long_pts - cfg.iv_curve_deadband_vol_pts:
        return "contango"
    return "flat"


def _status(fired: bool | None) -> str:
    if fired is None:
        return TRIPWIRE_STATUS["unknown"]
    return TRIPWIRE_STATUS["fired" if fired else "not_fired"]


def _sign(value: float, deadband: float) -> int:
    if value > deadband:
        return 1
    if value < -deadband:
        return -1
    return 0


def _side(value: float, deadband: float, *, pos: str, neg: str) -> str:
    sign = _sign(value, deadband)
    return pos if sign > 0 else neg if sign < 0 else "flat"


# ---------------------------------------------------------------------------
# Private: fetch, store, decode
# ---------------------------------------------------------------------------


@dataclass
class _Acc:
    stored: list[str] = field(default_factory=list)
    no_data: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)
    requests: int = 0

    def report(self, fetched_at: datetime) -> RegimeRefreshReport:
        return RegimeRefreshReport(
            fetched_at=fetched_at, stored=tuple(self.stored), no_data=tuple(self.no_data),
            degraded=tuple(self.degraded), requests=self.requests,
        )


def _merge(reports: Sequence[RegimeRefreshReport], fetched_at: datetime) -> RegimeRefreshReport:
    return RegimeRefreshReport(
        fetched_at=fetched_at,
        stored=tuple(s for r in reports for s in r.stored),
        no_data=tuple(s for r in reports for s in r.no_data),
        degraded=tuple(s for r in reports for s in r.degraded),
        requests=sum(r.requests for r in reports),
    )


async def _one(
    client: JsonClient,
    sessions: sessionmaker[Session],
    acc: _Acc,
    *,
    source: str,
    path: str,
    params: dict[str, Any] | None,
    fetched_at: datetime,
    encode: Callable[[Mapping[str, Any]], tuple[datetime | None, str] | None],
) -> None:
    acc.requests += 1
    try:
        resp = await client.request_json(path, params=params)
    except UnusualWhalesDailyLimitError:
        raise
    except UnusualWhalesNotFoundError:
        acc.no_data.append(source)
        return
    except _DEGRADED:
        acc.degraded.append(source)
        return
    encoded = encode(resp)
    if encoded is None:
        acc.no_data.append(source)
        return
    source_time, payload = encoded
    with sessions() as session, session.begin():
        if session.get(AlfaRegime, (source, fetched_at)) is None:
            session.add(AlfaRegime(
                source=source, fetched_at=fetched_at, source_time=source_time,
                payload_json=payload,
            ))
    acc.stored.append(source)


def _encode(
    reading: _R | None, adapter: TypeAdapter[_R], stamp: Callable[[_R], datetime | None],
) -> tuple[datetime | None, str] | None:
    if reading is None:
        return None
    return stamp(reading), adapter.dump_json(reading).decode("utf-8")


def _spot_encoder(
    ticker: str, fetched_at: datetime,
) -> Callable[[Mapping[str, Any]], tuple[datetime | None, str] | None]:
    def encode(resp: Mapping[str, Any]) -> tuple[datetime | None, str] | None:
        reading = _parse_spot(resp, ticker=ticker, fetched_at=fetched_at)
        return _encode(reading, _GAMMA_ADAPTER, _gamma_time)

    return encode


def _gex_encoder(
    ticker: str, fetched_at: datetime,
) -> Callable[[Mapping[str, Any]], tuple[datetime | None, str] | None]:
    def encode(resp: Mapping[str, Any]) -> tuple[datetime | None, str] | None:
        reading = _parse_gex(resp, ticker=ticker, fetched_at=fetched_at)
        return _encode(reading, _GEX_ADAPTER, _gex_time)

    return encode


def _history_encoder(
    ticker: str, fetched_at: datetime,
) -> Callable[[Mapping[str, Any]], tuple[datetime | None, str] | None]:
    def encode(resp: Mapping[str, Any]) -> tuple[datetime | None, str] | None:
        reading = _parse_history(resp, ticker=ticker, fetched_at=fetched_at)
        return _encode(reading, _HISTORY_ADAPTER, _no_time)

    return encode


def _gamma_time(reading: GammaReading) -> datetime | None:
    return reading.time


def _gex_time(reading: GexLevels) -> datetime | None:
    return reading.time


def _no_time(reading: object) -> datetime | None:
    del reading
    return None


def _decode(adapter: TypeAdapter[_R], payload: str) -> _R | None:
    try:
        return adapter.validate_json(payload)
    except ValidationError:
        return None


def _latest(
    session: Session, source: str, adapter: TypeAdapter[_R], now: datetime,
) -> _R | None:
    row = session.scalars(
        select(AlfaRegime)
        .where(AlfaRegime.source == source, AlfaRegime.fetched_at <= now)
        .order_by(AlfaRegime.fetched_at.desc())
        .limit(1),
    ).first()
    return _decode(adapter, row.payload_json) if row is not None else None


def _latest_each(
    session: Session, sources: Sequence[str], adapter: TypeAdapter[_R], now: datetime,
) -> tuple[_R, ...]:
    found: list[_R] = []
    for source in sources:
        reading = _latest(session, source, adapter, now)
        if reading is not None:
            found.append(reading)
    return tuple(found)


# ---------------------------------------------------------------------------
# Private: parsers
# ---------------------------------------------------------------------------


def _parse_tide(resp: Mapping[str, Any], *, fetched_at: datetime) -> TideReading | None:
    by_start: dict[datetime, tuple[float, float, int | None, date | None]] = {}
    for row in _rows(resp.get("data")):
        start = _ts(row.get("timestamp"))
        call = _num(row.get("net_call_premium"))
        put = _num(row.get("net_put_premium"))
        if start is None or call is None or put is None:
            continue
        by_start[start] = (call, put, _int(row.get("net_volume")), _day(row.get("date")))
    starts = sorted(by_start)
    if len(starts) < 2:
        return None
    interval = starts[-1] - starts[-2]
    complete = [s for s in starts if s + interval <= fetched_at]
    if interval <= timedelta(0) or not complete:
        return None
    start = complete[-1]
    call, put, volume, day = by_start[start]
    return TideReading(
        bucket_start=start, bucket_end=start + interval, trade_date=day,
        net_call_premium=call, net_put_premium=put, net_premium=call - put,
        net_volume=volume, fetched_at=fetched_at,
    )


def _parse_spot(
    resp: Mapping[str, Any], *, ticker: str, fetched_at: datetime,
) -> GammaReading | None:
    rows: list[tuple[datetime, float, float | None]] = []
    for row in _rows(resp.get("data")):
        stamp = _ts(row.get("time"))
        gamma = _num(row.get("gamma_per_one_percent_move_oi"))
        if stamp is not None and gamma is not None:
            rows.append((stamp, gamma, _num(row.get("price"))))
    if not rows:
        return None
    rows.sort(key=lambda r: r[0])
    session_day = rows[-1][0].astimezone(_ET).date()
    regular = [
        r for r in rows
        if r[0].astimezone(_ET).date() == session_day
        and r[0].astimezone(_ET).time() >= _SESSION_OPEN_ET
    ]
    if not regular:
        return None
    first, last = regular[0], regular[-1]
    return GammaReading(
        ticker=ticker, time=last[0], price=last[2], gamma_oi=last[1],
        session_first_time=first[0], session_first_gamma_oi=first[1], fetched_at=fetched_at,
    )


def _parse_gex(resp: Mapping[str, Any], *, ticker: str, fetched_at: datetime) -> GexLevels | None:
    data = resp.get("data")
    if not isinstance(data, dict):
        return None
    levels = GexLevels(
        ticker=ticker, time=_ts(data.get("time")), gamma_flip=_num(data.get("gamma_flip")),
        call_wall=_num(data.get("call_wall")), put_wall=_num(data.get("put_wall")),
        gamma_magnet=_num(data.get("gamma_magnet")), fetched_at=fetched_at,
    )
    if all(v is None for v in (levels.gamma_flip, levels.call_wall, levels.put_wall,
                               levels.gamma_magnet)):
        return None
    return levels


def _parse_curve(
    resp: Mapping[str, Any], *, cfg: RegimeSettings, fetched_at: datetime,
) -> CurveReading | None:
    rows: list[tuple[int, float, date | None, date | None]] = []
    for row in _rows(resp.get("data")):
        dte = _int(row.get("dte"))
        iv = _num(row.get("volatility"))
        if dte is None or iv is None or iv <= 0 or dte <= cfg.exclude_event_hump_max_dte:
            continue
        rows.append((dte, iv, _day(row.get("expiry")), _day(row.get("date"))))
    if not rows:
        return None
    short = min(rows, key=lambda r: (abs(r[0] - cfg.iv_anchor_short_dte), r[0]))
    long = min(rows, key=lambda r: (abs(r[0] - cfg.iv_anchor_long_dte), r[0]))
    if short[0] >= long[0]:
        return None
    return CurveReading(
        ticker=CURVE_TICKER, as_of=short[3], short_expiry=short[2], short_dte=short[0],
        short_iv=short[1], long_expiry=long[2], long_dte=long[0], long_iv=long[1],
        fetched_at=fetched_at,
    )


def _parse_vix(resp: Mapping[str, Any], *, fetched_at: datetime) -> VixReading | None:
    ratios: list[float] = []
    as_of: date | None = None
    for row in _rows(resp.get("data")):
        move = _num(row.get("implied_move"))
        perc = _num(row.get("implied_move_perc"))
        if move is None or perc is None or move <= 0 or perc <= 0:
            continue
        ratios.append(move / perc)
        as_of = as_of or _day(row.get("date"))
    if not ratios:
        return None
    return VixReading(
        as_of=as_of, vix_spot=statistics.median(ratios), rows_used=len(ratios),
        fetched_at=fetched_at,
    )


def _parse_history(
    resp: Mapping[str, Any], *, ticker: str, fetched_at: datetime,
) -> GammaHistory | None:
    by_day: dict[date, float] = {}
    for row in _rows(resp.get("data")):
        day = _day(row.get("date"))
        call = _num(row.get("call_gamma"))
        put = _num(row.get("put_gamma"))
        if day is not None and call is not None and put is not None:
            by_day[day] = call + put
    if not by_day:
        return None
    days = sorted(by_day)
    latest = by_day[days[-1]]
    values = [by_day[d] for d in days]
    return GammaHistory(
        ticker=ticker, as_of=days[-1], latest_net=latest,
        percentile=sum(1 for v in values if v <= latest) / len(values) * _PERCENT,
        negative_days=sum(1 for v in values if v < 0), total_days=len(values),
        fetched_at=fetched_at,
    )


def _rows(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict)]


def _num(raw: object) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = float(Decimal(str(raw).strip()))
    except (InvalidOperation, ValueError):
        return None
    return value if math.isfinite(value) else None


def _int(raw: object) -> int | None:
    value = _num(raw)
    if value is None or not value.is_integer():
        return None
    return int(value)


def _day(raw: object) -> date | None:
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError:
        return None


def _ts(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


# ---------------------------------------------------------------------------
# Private: formatting
# ---------------------------------------------------------------------------


def _usd(value: float) -> str:
    sign = "-" if value < 0 else ""
    size = abs(value)
    if size >= _BILLION:
        return f"{sign}${size / _BILLION:.1f}B"
    if size >= _MILLION:
        return f"{sign}${size / _MILLION:.1f}M"
    if size >= _THOUSAND:
        return f"{sign}${size / _THOUSAND:.1f}K"
    return f"{sign}${size:.0f}"


def _count(value: int) -> str:
    sign = "-" if value < 0 else ""
    size = abs(value)
    if size >= _MILLION:
        return f"{sign}{size / _MILLION:.1f}M"
    if size >= _THOUSAND:
        return f"{sign}{size / _THOUSAND:.0f}k"
    return f"{sign}{size}"


def _vol(fraction: float) -> str:
    return f"{fraction * _PERCENT:.1f}"
