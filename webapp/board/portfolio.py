"""Portfolio overlap for the Alfa Board (Phase 5.2.B6a, contract §6 B6). Pure.

- Open-journal overlap badge ``zaten bu bahittesin``: an open trade with the same
  ticker and direction. ``TradeRow`` is read, never changed. Its ``direction``,
  ``instrument`` and ``expiry`` are unvalidated strings, so they are parsed
  defensively; an unparseable value is ``bilinmiyor`` and never matches.
- Capital header: open long premium at risk (contracts x entry x multiplier: 100 for
  options, 1 for shares) against ``sizing.capital_usd``, in dollars and percent. While
  ``sizing.values_confirmed_by_owner`` is false the header carries
  ``(varsayılan değer)``. An open row whose option has expired carries no premium at
  risk, so it leaves the sum and is disclosed separately (Phase 5.2.B-fix2, review
  FB-H2); a row whose expiry cannot be read is unknown, not expired, and stays counted.
- Single-bet clusters (strong link): board and journal tickers that each weigh at
  least ``portfolio.cluster_min_member_weight_pct`` in the same focused ETF are joined
  (union-find); clusters are the connected components with two or more members.
  Share classes merge through ``portfolio.share_class_aliases`` (weights add up).
  ETFs outside ``portfolio.focused_etfs`` or with more than ``max_focused_holdings``
  holdings are ignored. Output is alphabetical and deterministic.
- Same sector (weak link): shown, never merged into a cluster. The sector mapping is a
  plain argument; the ticker info table belongs to A3.

Turkish copy comes only from the frozen constants below.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from webapp.board.etf_holdings import HoldingView
    from webapp.board.settings import BoardSettings
    from webapp.journal import TradeRow

Direction = Literal["yukarı", "aşağı"]
Instrument = Literal["call", "put", "shares"]

# ---------------------------------------------------------------------------
# Frozen copy
# ---------------------------------------------------------------------------

OVERLAP_BADGE: Final = "zaten bu bahittesin"
UNKNOWN: Final = "bilinmiyor"
DEFAULT_VALUE_MARK: Final = "(varsayılan değer)"
UNPARSED_DIRECTION_NOTE: Final = "aynı hissede yönü okunamayan açık işlem var (bilinmiyor)"
CAPITAL_HEADER_TEMPLATE: Final = "Açıktaki prim riski: {usd} · sermayenin %{pct}"
CAPITAL_UNPARSED_TEMPLATE: Final = "{n} açık işlem okunamadı (bilinmiyor)"
# Phase 5.2.B-fix2 (review FB-H2): an expired long option carries no premium at risk.
# It leaves the at-risk sum and is disclosed here, never silently dropped.
CAPITAL_EXPIRED_TEMPLATE: Final = "{n} işlemin vadesi geçti ({usd} hariç)"
TRADE_DETAIL_TEMPLATE: Final = "{ticker} {instrument} {strike} {expiry} · {contracts} kontrat @ {entry}"
EXPIRED_MARK: Final = "(vadesi geçti)"
EXPIRY_UNKNOWN: Final = "vade bilinmiyor"
INSTRUMENT_LABELS: Final[Mapping[str, str]] = {"call": "call", "put": "put", "shares": "hisse"}
CLUSTER_TEMPLATE: Final = "Tek bahis: {members} ({links}; her biri ≥ %{min_weight})"
CLUSTER_LINK_TEMPLATE: Final = "{etf} {updated} tarihli"
SECTOR_LINK_TEMPLATE: Final = "Aynı sektör (zayıf bağ, kümeye katılmaz): {members} · {sector}"
_LIST_SEPARATOR: Final = ", "
_PART_SEPARATOR: Final = " · "

_OPTION_MULTIPLIER: Final = 100.0  # contract structure: one option controls 100 shares
_SHARE_MULTIPLIER: Final = 1.0
_PERCENT: Final = 100.0
_OPEN_STATUS: Final = "open"
_DIRECTIONS: Final[Mapping[str, Direction]] = {
    "bullish": "yukarı", "yukarı": "yukarı", "bearish": "aşağı", "aşağı": "aşağı",
}
_INSTRUMENTS: Final[Mapping[str, Instrument]] = {"call": "call", "put": "put", "shares": "shares"}


# ---------------------------------------------------------------------------
# Journal parsing and overlap
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedTrade:
    trade_id: str
    ticker: str
    is_open: bool
    direction: Direction | None
    instrument: Instrument | None
    strike: float | None
    expiry: date | None
    expiry_raw: str | None
    expired: bool | None
    contracts: float | None
    entry_price: float | None


@dataclass(frozen=True)
class OverlapView:
    ticker: str
    direction: Direction
    matched: tuple[ParsedTrade, ...]
    unparsed_same_ticker: tuple[ParsedTrade, ...]
    badge: str | None
    note: str | None
    details: tuple[str, ...]


def parse_trade(trade: TradeRow, *, today: date) -> ParsedTrade:
    raw_expiry = trade.expiry.strip() if isinstance(trade.expiry, str) else None
    expiry = _iso_date(raw_expiry)
    return ParsedTrade(
        trade_id=str(trade.id),
        ticker=str(trade.ticker or "").strip().upper(),
        is_open=str(trade.status or "").strip().lower() == _OPEN_STATUS,
        direction=_DIRECTIONS.get(str(trade.direction or "").strip().lower()),
        instrument=_INSTRUMENTS.get(str(trade.instrument or "").strip().lower()),
        strike=_positive(trade.strike),
        expiry=expiry,
        expiry_raw=raw_expiry or None,
        expired=(expiry < today) if expiry is not None else None,
        contracts=_positive(trade.contracts),
        entry_price=_positive(trade.entry_price),
    )


def journal_overlap(
    trades: Sequence[TradeRow], *, ticker: str, direction: Direction, today: date,
) -> OverlapView:
    """Open trades on ``ticker`` in ``direction``; same-ticker trades with unreadable direction."""
    symbol = ticker.strip().upper()
    parsed = [parse_trade(t, today=today) for t in trades]
    same = [p for p in parsed if p.is_open and p.ticker == symbol]
    matched = tuple(sorted((p for p in same if p.direction == direction), key=_trade_key))
    unparsed = tuple(sorted((p for p in same if p.direction is None), key=_trade_key))
    return OverlapView(
        ticker=symbol, direction=direction, matched=matched, unparsed_same_ticker=unparsed,
        badge=OVERLAP_BADGE if matched else None,
        note=UNPARSED_DIRECTION_NOTE if unparsed else None,
        details=tuple(trade_detail_text(p) for p in matched),
    )


def trade_detail_text(trade: ParsedTrade) -> str:
    instrument = INSTRUMENT_LABELS[trade.instrument] if trade.instrument else UNKNOWN
    if trade.instrument == "shares":
        strike = expiry = ""
    else:
        strike = f"{trade.strike:g}" if trade.strike is not None else UNKNOWN
        if trade.expiry is None:
            expiry = EXPIRY_UNKNOWN
        else:
            expiry = f"{trade.expiry:%d.%m.%Y}" + (f" {EXPIRED_MARK}" if trade.expired else "")
    text = TRADE_DETAIL_TEMPLATE.format(
        ticker=trade.ticker, instrument=instrument, strike=strike, expiry=expiry,
        contracts=f"{trade.contracts:g}" if trade.contracts is not None else UNKNOWN,
        entry=f"${trade.entry_price:.2f}" if trade.entry_price is not None else UNKNOWN,
    )
    return " ".join(text.split())


# ---------------------------------------------------------------------------
# Capital header
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapitalHeader:
    open_trades: int
    at_risk_usd: float
    capital_usd: float
    at_risk_pct: float
    unparsed: tuple[str, ...]
    values_confirmed: bool
    text: str
    # Phase 5.2.B-fix2: open rows whose option has expired, and the entry premium they
    # would have added. They are disclosed, never counted as premium at risk.
    expired_trades: int = 0
    expired_usd: float = 0.0


def capital_header(trades: Sequence[TradeRow], *, settings: BoardSettings, today: date) -> CapitalHeader:
    capital = settings.sizing.capital_usd
    at_risk = 0.0
    expired_usd = 0.0
    open_count = 0
    expired_count = 0
    unparsed: list[str] = []
    for trade in trades:
        parsed = parse_trade(trade, today=today)
        if not parsed.is_open:
            continue
        open_count += 1
        if parsed.instrument is None or parsed.contracts is None or parsed.entry_price is None:
            unparsed.append(parsed.trade_id)
            continue
        multiplier = _SHARE_MULTIPLIER if parsed.instrument == "shares" else _OPTION_MULTIPLIER
        premium = parsed.contracts * parsed.entry_price * multiplier
        if parsed.expired:
            # An expired option is worth nothing: counting its entry premium would
            # overstate the board's only account-level number (review FB-H2). An
            # unreadable expiry is None here, which is unknown, not expired.
            expired_count += 1
            expired_usd += premium
            continue
        at_risk += premium
    pct = at_risk / capital * _PERCENT
    text = CAPITAL_HEADER_TEMPLATE.format(usd=f"${at_risk:,.0f}", pct=f"{pct:.1f}")
    if not settings.sizing.values_confirmed_by_owner:
        text = f"{text} {DEFAULT_VALUE_MARK}"
    if expired_count:
        text = text + _PART_SEPARATOR + CAPITAL_EXPIRED_TEMPLATE.format(
            n=expired_count, usd=f"${expired_usd:,.0f}",
        )
    if unparsed:
        text = text + _PART_SEPARATOR + CAPITAL_UNPARSED_TEMPLATE.format(n=len(unparsed))
    return CapitalHeader(
        open_trades=open_count, at_risk_usd=at_risk, capital_usd=capital, at_risk_pct=pct,
        unparsed=tuple(sorted(unparsed)),
        values_confirmed=settings.sizing.values_confirmed_by_owner, text=text,
        expired_trades=expired_count, expired_usd=expired_usd,
    )


# ---------------------------------------------------------------------------
# Clusters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClusterLink:
    etf: str
    updated: date
    weights: tuple[tuple[str, float], ...]  # (member, weight %), alphabetical


@dataclass(frozen=True)
class SingleBetCluster:
    members: tuple[str, ...]
    label_etf: str
    links: tuple[ClusterLink, ...]
    text: str


@dataclass(frozen=True)
class SectorLink:
    sector: str
    members: tuple[str, ...]
    text: str


def canonical_ticker(ticker: str, *, settings: BoardSettings) -> str:
    symbol = ticker.strip().upper()
    aliases = {k.strip().upper(): v.strip().upper() for k, v in settings.portfolio.share_class_aliases.items()}
    return aliases.get(symbol, symbol)


def single_bet_clusters(
    tickers: Iterable[str],
    holdings: Sequence[HoldingView],
    *,
    settings: BoardSettings,
) -> tuple[SingleBetCluster, ...]:
    cfg = settings.portfolio
    members = sorted({canonical_ticker(t, settings=settings) for t in tickers if t.strip()})
    if len(members) < 2:
        return ()
    member_set = set(members)
    focused = {e.strip().upper() for e in cfg.focused_etfs}

    snapshots: dict[str, dict[str, tuple[date, dict[str, float], set[str]]]] = {}
    for holding in holdings:
        etf = holding.etf.strip().upper()
        if etf not in focused:
            continue
        by_date = snapshots.setdefault(etf, {})
        key = holding.updated.isoformat()
        _, weights, raw_tickers = by_date.setdefault(key, (holding.updated, {}, set()))
        raw_tickers.add(holding.ticker.strip().upper())
        canon = canonical_ticker(holding.ticker, settings=settings)
        weights[canon] = weights.get(canon, 0.0) + holding.weight_pct

    links: list[ClusterLink] = []
    for etf in sorted(snapshots):
        updated, weights, raw_tickers = snapshots[etf][max(snapshots[etf])]
        if len(raw_tickers) > cfg.max_focused_holdings:
            continue
        qualifying = sorted(
            m for m in member_set
            if weights.get(m, 0.0) >= cfg.cluster_min_member_weight_pct
        )
        if len(qualifying) >= 2:
            links.append(ClusterLink(
                etf=etf, updated=updated, weights=tuple((m, weights[m]) for m in qualifying),
            ))

    parent = {m: m for m in members}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for link in links:
        root = find(link.weights[0][0])
        for other, _ in link.weights[1:]:
            other_root = find(other)
            if other_root != root:
                keep, drop = sorted((root, other_root))
                parent[drop] = keep
                root = keep

    components: dict[str, list[str]] = {}
    for member in members:
        components.setdefault(find(member), []).append(member)

    clusters: list[SingleBetCluster] = []
    for group in components.values():
        if len(group) < 2:
            continue
        group_set = set(group)
        group_links = tuple(
            link for link in links if all(m in group_set for m, _ in link.weights)
        )
        label = sorted(group_links, key=lambda lk: (-sum(w for _, w in lk.weights), lk.etf))[0]
        text = CLUSTER_TEMPLATE.format(
            members=_LIST_SEPARATOR.join(group),
            links=_LIST_SEPARATOR.join(
                CLUSTER_LINK_TEMPLATE.format(etf=lk.etf, updated=f"{lk.updated:%d.%m.%Y}")
                for lk in group_links
            ),
            min_weight=f"{cfg.cluster_min_member_weight_pct:g}",
        )
        clusters.append(SingleBetCluster(
            members=tuple(group), label_etf=label.etf, links=group_links, text=text,
        ))
    return tuple(sorted(clusters, key=lambda c: c.members))


def same_sector_links(
    tickers: Iterable[str],
    sectors: Mapping[str, str | None],
    *,
    settings: BoardSettings,
) -> tuple[SectorLink, ...]:
    """Weak links: tickers sharing a non-empty sector. Never merged into a cluster."""
    lookup = {k.strip().upper(): v for k, v in sectors.items()}
    groups: dict[str, set[str]] = {}
    for ticker in tickers:
        symbol = ticker.strip().upper()
        if not symbol:
            continue
        canon = canonical_ticker(symbol, settings=settings)
        sector = lookup.get(symbol) or lookup.get(canon)
        if isinstance(sector, str) and sector.strip():
            groups.setdefault(sector.strip(), set()).add(canon)
    out = [
        SectorLink(
            sector=sector, members=tuple(sorted(names)),
            text=SECTOR_LINK_TEMPLATE.format(
                members=_LIST_SEPARATOR.join(sorted(names)), sector=sector,
            ),
        )
        for sector, names in groups.items()
        if len(names) >= 2
    ]
    return tuple(sorted(out, key=lambda s: (s.sector, s.members)))


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _trade_key(trade: ParsedTrade) -> tuple[str, str]:
    return (trade.expiry_raw or "", trade.trade_id)


def _positive(raw: object) -> float | None:
    if raw is None or isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    value = float(raw)
    return value if math.isfinite(value) and value > 0 else None


def _iso_date(raw: str | None) -> date | None:
    if not raw or len(raw) != len("YYYY-MM-DD"):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None
