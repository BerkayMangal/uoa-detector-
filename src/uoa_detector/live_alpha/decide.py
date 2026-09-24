"""The ``live_alpha_v1`` decision policy (contract §5) and the card text.

Two steps, because the option alternative needs quotes the job fetches only for
the tickers that need them:

1. :func:`evaluate` — flow + price + news + session → a :class:`Decision`
   (recommendation, readiness, path, blockers, stock plan).
2. :func:`build_card` — the decision plus the priced option structures → the
   immutable :class:`Card` the page renders.

Every sentence is a deterministic template filled from source fields or from the
arithmetic in ``stock_plan`` / ``option_plan``. No number is written that is not
in the inputs or computed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from uoa_detector.live_alpha.calendar import MarketMode, Session
from uoa_detector.live_alpha.flow import qualifying_direction
from uoa_detector.live_alpha.model import (
    Card,
    CheckState,
    Direction,
    EvidenceStatus,
    FlowSummary,
    InstrumentChoice,
    NewsCheck,
    NewsItem,
    OptionStructureView,
    Path,
    PriceContext,
    Readiness,
    Recommendation,
    StockPlan,
)
from uoa_detector.live_alpha.option_plan import choose_instrument, kind_tr
from uoa_detector.live_alpha.plain import verdict
from uoa_detector.live_alpha.stock_plan import build_stock_plan, chase_limit

if TYPE_CHECKING:
    from collections.abc import Sequence

    from uoa_detector.live_alpha.settings import LiveAlphaSettings

RANK = {
    Recommendation.BUY: 6,
    Recommendation.CONDITIONAL_BUY: 5,
    Recommendation.BEARISH_SETUP: 4,
    Recommendation.EXIT_REVIEW: 3,
    Recommendation.AVOID: 2,
    Recommendation.WATCH: 1,
}

REC_TR = {
    Recommendation.BUY: "ALIM",
    Recommendation.CONDITIONAL_BUY: "KOŞULLU ALIM",
    Recommendation.WATCH: "İZLE",
    Recommendation.AVOID: "KAÇIN",
    Recommendation.BEARISH_SETUP: "AŞAĞI YÖNLÜ KURULUM",
    Recommendation.EXIT_REVIEW: "ÇIKIŞ İNCELEMESİ",
}

DIR_TR = {"up": "yukarı", "down": "aşağı"}


@dataclass(frozen=True)
class Decision:
    ticker: str
    direction: Direction
    recommendation: Recommendation
    readiness: Readiness
    path: Path
    blockers: tuple[str, ...]
    stock_plan: StockPlan | None
    needs_options: bool
    flow: FlowSummary
    price: PriceContext
    news: NewsCheck
    session: Session
    decided_at: datetime
    fresh_news: tuple[NewsItem, ...]


def spot_is_fresh(price: PriceContext, session: Session, now: datetime, max_age: int) -> bool:
    if session.mode is not MarketMode.LIVE or price.spot_fetched_at is None:
        return False
    age = (now - price.spot_fetched_at).total_seconds()
    return 0 <= age <= max_age


def fresh_news_items(news: NewsCheck, now: datetime, window_hours: float) -> tuple[NewsItem, ...]:
    if news.state is not CheckState.CHECKED_FOUND:
        return ()
    horizon = now - timedelta(hours=window_hours)
    return tuple(
        sorted(
            (i for i in news.items if horizon <= i.provider_created_at <= now),
            key=lambda i: i.provider_created_at, reverse=True,
        ),
    )


def news_state_text(news: NewsCheck, fresh: Sequence[NewsItem], window_hours: float) -> str:
    if news.state is CheckState.FAILED:
        return f"haber kontrol edilemedi ({news.detail or 'sağlayıcı hatası'})"
    if news.state is CheckState.NOT_CHECKED:
        return "haber henüz kontrol edilmedi"
    if news.state is CheckState.STALE:
        return "haber verisi bayat; yeniden kontrol edilemedi"
    if not fresh:
        return f"son {window_hours:g} saatte bu hisseye bağlı başlık yok (kontrol edildi)"
    return f"son {window_hours:g} saatte {len(fresh)} başlık"


def evaluate(
    flow: FlowSummary,
    price: PriceContext,
    news: NewsCheck,
    session: Session,
    now: datetime,
    settings: LiveAlphaSettings,
    *,
    flow_current: bool = True,
) -> Decision:
    """Apply paths P1–P4 in their fixed order (contract §5).

    ``flow_current`` is False when the flow run is not today's session run (the
    worker has not written a print yet today, or is down). Such flow can still
    describe a thesis, but it can never make an entry READY.
    """
    ps = settings.price
    fresh = fresh_news_items(news, now, settings.news.window_hours)
    direction, flow_why = qualifying_direction(flow, settings.flow)

    def _done(
        d: Direction, rec: Recommendation, ready: Readiness, path: Path, blockers: list[str],
        plan: StockPlan | None = None, needs_options: bool = False,
    ) -> Decision:
        return Decision(
            ticker=flow.ticker, direction=d, recommendation=rec, readiness=ready, path=path,
            blockers=tuple(b for b in blockers if b), stock_plan=plan, needs_options=needs_options,
            flow=flow, price=price, news=news, session=session, decided_at=now, fresh_news=fresh,
        )

    dominant: Direction = "up" if flow.premium_up >= flow.premium_down else "down"
    if direction is None:
        return _done(dominant, Recommendation.WATCH, Readiness.INVALID, Path.NONE, [flow_why])
    if price.state is not CheckState.CHECKED_FOUND or price.move_atr is None:
        return _done(
            direction, Recommendation.WATCH, Readiness.INVALID, Path.NONE,
            [f"fiyat verisi eksik: {price.blocker or 'spot/önceki kapanış/ATR yok'}"],
        )
    move_atr = price.move_atr
    live = session.mode is MarketMode.LIVE
    fresh_spot = spot_is_fresh(price, session, now, ps.spot_max_age_seconds)
    timing: list[str] = []
    if not live:
        timing.append(f"piyasa {session.mode.value} ({session.reason}); giriş yalnız canlı seansta")
    elif not fresh_spot:
        timing.append("fiyat bayat; taze fiyat gelmeden giriş yok")
    if live and not flow_current:
        timing.append(f"akış kaydı ({flow.run_id}) bugünkü seansa ait değil; bugünün akışı gelmeden giriş yok")
    entry_ok = live and fresh_spot and flow_current

    if direction == "down":
        if move_atr <= -ps.confirm_min_atr:
            plan = build_stock_plan("down", price, ps, settings.stock_plan)
            ready = Readiness.READY if entry_ok else Readiness.TRIGGER_PENDING
            return _done("down", Recommendation.BEARISH_SETUP, ready, Path.P4_BEARISH, timing, plan, True)
        return _done(
            "down", Recommendation.WATCH, Readiness.INVALID, Path.NONE,
            [f"aşağı yönlü akış var ama fiyat teyit etmiyor ({move_atr:+.2f} ATR)"],
        )

    # direction == "up"
    if move_atr <= -ps.confirm_min_atr:
        return _done(
            "up", Recommendation.AVOID, Readiness.INVALID, Path.NONE,
            [f"yukarı yönlü akışa karşın fiyat ters yönde ({move_atr:+.2f} ATR)"],
        )
    news_ok = bool(fresh)
    rel = price.relative
    rel_ok = rel is not None and rel >= ps.relative_min
    if move_atr < ps.confirm_min_atr:
        return _done(
            "up", Recommendation.WATCH, Readiness.INVALID, Path.NONE,
            [f"fiyat teyidi yok ({move_atr:+.2f} ATR < {ps.confirm_min_atr:g} ATR)"],
        )
    if not news_ok and not rel_ok:
        rel_txt = (
            f"{ps.benchmark}'a göre {rel * 100:+.2f}% (< {ps.relative_min * 100:.1f}%)"
            if rel is not None else f"{ps.benchmark} karşılaştırması yapılamadı"
        )
        return _done(
            "up", Recommendation.WATCH, Readiness.INVALID, Path.NONE,
            [news_state_text(news, fresh, settings.news.window_hours), rel_txt],
        )
    thesis = Path.P1_NEWS_CONTINUATION if news_ok else Path.P2_MARKET_RELATIVE
    if move_atr > ps.chase_max_atr:
        assert price.prev_close is not None and price.atr is not None  # checked by state
        level = chase_limit("up", price.prev_close, price.atr, ps.chase_max_atr)
        plan = build_stock_plan("up", price, ps, settings.stock_plan, entry_override=level)
        return _done(
            "up", Recommendation.CONDITIONAL_BUY, Readiness.TRIGGER_PENDING, Path.P3_PULLBACK,
            [f"fiyat önceki kapanıştan {move_atr:+.2f} ATR uzakta (> {ps.chase_max_atr:g}); kovalanmaz",
             *timing],
            plan, True,
        )
    plan = build_stock_plan("up", price, ps, settings.stock_plan)
    if entry_ok:
        return _done("up", Recommendation.BUY, Readiness.READY, thesis, [], plan, True)
    return _done("up", Recommendation.CONDITIONAL_BUY, Readiness.TRIGGER_PENDING, thesis, timing, plan, True)


# ---------------------------------------------------------------------------
# Card text
# ---------------------------------------------------------------------------


def _usd(v: float) -> str:
    return f"${v:,.0f}"


def _px(v: float | None) -> str:
    return "—" if v is None else f"${v:,.2f}"


def _et(ts: datetime | None, session: Session) -> str:
    if ts is None:
        return "—"
    return ts.astimezone(session.et_now.tzinfo).strftime("%d.%m %H:%M ET")


def _option_evidence(d: Decision) -> str:
    f = d.flow
    parts = [
        f"Akış kaydı {f.run_id}: {f.prints_deduped} tekil baskı ({f.prints_raw} ham): "
        f"yukarı {_usd(f.premium_up)}, aşağı {_usd(f.premium_down)}, tarafı bilinmeyen {_usd(f.premium_side_unknown)}",
        f"{DIR_TR[d.direction]} payı %{f.share(d.direction) * 100:.0f}, {f.distinct_contracts} farklı kontrat",
    ]
    if f.top_contracts:
        tops = ", ".join(f"{c} {_usd(p)} ({DIR_TR.get(dr, 'taraf ?')})" for c, p, dr in f.top_contracts)
        parts.append(f"en büyükler: {tops}")
    if f.first_ts and f.last_ts:
        parts.append(f"ilk {_et(f.first_ts, d.session)}, son {_et(f.last_ts, d.session)}")
    parts.append("yön, alıcı/satıcı tarafından; OI artışı açılış kanıtı değildir")
    return "; ".join(parts) + "."


def _news_evidence(d: Decision, window_hours: float) -> str:
    state = news_state_text(d.news, d.fresh_news, window_hours)
    if not d.fresh_news:
        return state[:1].upper() + state[1:] + "."
    items = "; ".join(
        f"“{i.headline}” — {i.source}, {_et(i.provider_created_at, d.session)}"
        f"{' (önemli)' if i.is_major else ''}"
        f"{f', UW duygu etiketi: {i.sentiment}' if i.sentiment else ''}"
        for i in d.fresh_news[:3]
    )
    return (
        f"{state}: {items}. Yalnız başlık okundu; tam metin ve beklenti/sürpriz verisi yok, "
        "zaman sağlayıcının created_at alanıdır."
    )


def _price_evidence(d: Decision, benchmark: str) -> str:
    p = d.price
    if p.state is not CheckState.CHECKED_FOUND or p.move is None or p.move_atr is None:
        return f"Fiyat verisi eksik: {p.blocker or 'spot/önceki kapanış/ATR yok'}."
    rel = p.relative
    if d.ticker.upper() == benchmark.upper():
        rel_txt = f" ({benchmark} karşılaştırma ölçütünün kendisi)"
    elif rel is not None:
        rel_txt = f", {benchmark}'a göre {rel * 100:+.2f}%"
    else:
        rel_txt = f", {benchmark} karşılaştırması yok (aynı seans ve taze fiyat gerekir)"
    return (
        f"Spot {_px(p.spot)} ({_et(p.spot_fetched_at, d.session)}), önceki kapanış {_px(p.prev_close)} "
        f"({p.prev_close_day:%d.%m} günü): {p.move * 100:+.2f}% = {p.move_atr:+.2f} ATR "
        f"(ATR14 {_px(p.atr)}){rel_txt}. Sektör verisi bu sürümde yok."
        if p.prev_close_day else
        f"Spot {_px(p.spot)}, hareket {p.move * 100:+.2f}% = {p.move_atr:+.2f} ATR{rel_txt}."
    )


def _stock_plan_text(d: Decision) -> str:
    s = d.stock_plan
    if s is None:
        return "Hisse planı kurulamadı (fiyat/ATR eksik)."
    if d.recommendation is Recommendation.BEARISH_SETUP:
        return (
            f"Hisse: alımdan kaçın; açığa satış varsayılmaz. Aşağı tez için referans {_px(s.entry_ref)}, "
            f"tez bozulma seviyesi {_px(s.stop)} üstü, politika hedefi {_px(s.target)}, "
            f"ufuk {s.horizon_sessions} seans."
        )
    head = (
        f"Giriş bölgesi {_px(s.entry_zone_low)} - {_px(s.entry_zone_high)}; {_px(s.chase_limit)} üstünde kovalanmaz. "
        if d.path is not Path.P3_PULLBACK else
        f"Tetik: fiyat {_px(s.entry_zone_high)} veya altına geri çekilirse; o seviyede plan. "
    )
    return (
        head
        + f"Referans giriş {_px(s.entry_ref)}, stop {_px(s.stop)} ({s.risk_per_share:.2f}$/hisse), "
        f"hedef {_px(s.target)} ({s.target_label}), ufuk {s.horizon_sessions} seans. "
        f"PAPER profil (R={_usd(s.r_usd)}, gerçek hesabın değil): {s.shares} hisse ≈ {_usd(s.notional_usd)}, "
        f"planlanan risk {_usd(s.planned_risk_usd)}; gap ve kötü dolum bu riski aşabilir."
        + (f" {s.note}." if s.note else "")
    )


def _option_text(structures: Sequence[OptionStructureView], quotes_state: str) -> str:
    if not structures:
        return f"Opsiyon alternatifi fiyatlanmadı: {quotes_state}."
    lines = []
    for v in structures:
        legs = " / ".join(
            f"{'AL' if leg.is_long else 'SAT'} {leg.option_symbol} (bid {_px(leg.bid)} ask {_px(leg.ask)})"
            for leg in v.legs
        )
        if v.readiness is Readiness.READY or v.readiness is Readiness.RISK_BLOCKED:
            lines.append(
                f"{kind_tr(v.kind)} [{v.readiness.value}] {legs}; vade {v.dte} gün; giriş limiti (net debit) "
                f"{_px(v.entry_debit)}, 1 lot maliyet {_px(v.entry_cost_usd)} + komisyon {_px(v.commission_usd)}, "
                f"azami zarar {_px(v.max_loss_usd)}"
                + (f", azami kâr {_px(v.max_profit_usd)}" if v.max_profit_usd is not None else "")
                + f", başabaş {_px(v.breakeven)}, şimdi kapatma değeri {_px(v.exit_credit)}, "
                f"gidiş-dönüş maliyet %{v.roundtrip_cost_pct:.0f}; lot {v.lots}"
                + (f" — {v.blocker}" if v.blocker else "")
            )
        else:
            lines.append(f"{kind_tr(v.kind)} [{v.readiness.value}] {legs or '—'}: {v.blocker}")
    return " | ".join(lines)


def _invalidation(d: Decision, settings: LiveAlphaSettings) -> str:
    s = d.stock_plan
    h = settings.stock_plan.horizon_sessions
    if s is None:
        return "Plan yok; tez bozulma seviyesi hesaplanamadı."
    if d.direction == "up":
        return (
            f"Fiyat {_px(s.stop)} altına inerse ({settings.stock_plan.stop_atr:g} ATR), aynı hissede aşağı yönlü "
            f"akış eşikleri geçerse veya {h} seans içinde hedefe gidilmezse (süre çıkışı)."
        )
    return (
        f"Fiyat {_px(s.stop)} üstüne çıkarsa, yukarı yönlü akış eşikleri geçerse veya {h} seans dolarsa."
    )


def _counter(d: Decision) -> str:
    f = d.flow
    opposite = f.premium_down if d.direction == "up" else f.premium_up
    parts = []
    if opposite > 0:
        parts.append(f"karşı yönde de {_usd(opposite)} prim var")
    if d.path is Path.P2_MARKET_RELATIVE:
        parts.append("destekleyen haber yok; tek günlük göreli güç sık sık geri verilir")
    if d.path is Path.P1_NEWS_CONTINUATION:
        parts.append("haber zaten fiyatlanmış olabilir; yalnız başlık okundu")
    parts.append(
        "akış tabanlı yön kuralları geçmiş araştırmada maliyet sonrası kaybettirdi (phase-3.6, H01/H02); "
        "bu politika henüz ileriye dönük doğrulanmadı"
    )
    return "; ".join(parts)[:1].upper() + "; ".join(parts)[1:] + "."


def _headline(d: Decision, instrument: InstrumentChoice) -> str:
    s = d.stock_plan
    t = d.ticker
    via = {"stock": "hisse", "none": "uygulanabilir araç yok"}.get(
        instrument.preferred, kind_tr(instrument.preferred),
    )
    if d.recommendation is Recommendation.BUY and s is not None:
        return (
            f"{t} için {_px(s.chase_limit)} üstüne kovalamadan alım görüşü (referans {_px(s.entry_ref)}); "
            f"tez {_px(s.stop)} altında bozulur; tercih edilen uygulama: {via}."
        )
    if d.recommendation is Recommendation.CONDITIONAL_BUY and s is not None:
        if d.path is Path.P3_PULLBACK:
            return f"{t} için koşullu alım: fiyat {_px(s.entry_zone_high)} veya altına geri çekilirse; şimdi kovalanmaz."
        return (
            f"{t} için koşullu alım: canlı seansta taze fiyat {_px(s.entry_zone_low)} - {_px(s.entry_zone_high)} "
            f"bölgesindeyse; tez {_px(s.stop)} altında bozulur."
        )
    if d.recommendation is Recommendation.BEARISH_SETUP:
        return f"{t} için alımdan kaçın; aşağı yönlü kurulum — {via}."
    if d.recommendation is Recommendation.AVOID:
        return f"{t} için alımdan kaçın: akış ile fiyat çelişiyor."
    return f"{t} izlemede: {d.blockers[0] if d.blockers else 'koşullar tamamlanmadı'}."


def _why_today(d: Decision, window_hours: float) -> str:
    f = d.flow
    bits = [f"bugünkü {DIR_TR[d.direction]} yönlü akış {_usd(f.premium_up if d.direction == 'up' else f.premium_down)}"]
    if d.fresh_news:
        bits.append(f"yeni başlık: “{d.fresh_news[0].headline}” ({d.fresh_news[0].source})")
    else:
        bits.append(news_state_text(d.news, d.fresh_news, window_hours))
    if d.price.move_atr is not None:
        bits.append(f"fiyat {d.price.move_atr:+.2f} ATR")
    return "; ".join(bits) + "."


def build_card(
    d: Decision,
    structures: Sequence[OptionStructureView],
    quotes_state: str,
    settings: LiveAlphaSettings,
) -> Card:
    stock_ok = (
        d.stock_plan is not None
        and d.stock_plan.shares > 0
        and d.recommendation in (Recommendation.BUY, Recommendation.CONDITIONAL_BUY)
    )
    if d.recommendation in (Recommendation.WATCH, Recommendation.AVOID):
        instrument = InstrumentChoice(preferred="none", reason="işlem görüşü yok")
    else:
        instrument = choose_instrument(structures, settings.option_plan, stock_available=stock_ok)
    ready = d.readiness
    blockers = list(d.blockers)
    if (
        d.recommendation in (Recommendation.BUY, Recommendation.BEARISH_SETUP)
        and instrument.preferred == "none"
        and ready is Readiness.READY
    ):
        # A view with no executable instrument is a view, not an entry: never a green READY.
        ready = next(
            (v.readiness for v in structures if v.readiness is not Readiness.READY), Readiness.RISK_BLOCKED,
        )
        blockers.append(f"uygulanabilir araç yok: {instrument.reason}")
    window = settings.news.window_hours
    rel = d.price.relative
    dims = {
        "veri": "tamam" if d.price.state is CheckState.CHECKED_FOUND else "eksik",
        "akış_açıklığı": f"%{d.flow.share(d.direction) * 100:.0f} {DIR_TR[d.direction]}",
        "haber": news_state_text(d.news, d.fresh_news, window),
        "girişe_yakınlık": f"{d.price.move_atr:+.2f} ATR (sınır {settings.price.chase_max_atr:g})"
        if d.price.move_atr is not None else "—",
        "piyasaya_göre": f"{rel * 100:+.2f}%" if rel is not None else "—",
        "araç": instrument.preferred,
    }
    sources = [
        f"UW flow-alerts → signal/alfa_print_meta, run {d.flow.run_id}",
        "UW atm-chains → alfa_atm (spot), UW ohlc/1d → alfa_daily_bar (önceki kapanış, ATR)",
    ]
    if d.news.state is not CheckState.NOT_CHECKED:
        sources.append(
            f"UW /api/news/headlines?ticker={d.ticker} (kontrol {_et(d.news.checked_at, d.session)}; URL alanı yok)",
        )
    if structures:
        sources.append("UW /api/stock/{t}/option-contracts (NBBO; borsa kotasyon zamanı yok, alındığı an gösterilir)")
    rank_premium = d.flow.premium_up if d.direction == "up" else d.flow.premium_down
    plain_action, plain_reason = verdict(d, instrument, settings, ready=ready is Readiness.READY)
    return Card(
        opportunity_id=f"{d.session.session_date or d.session.et_now.date()}:{d.ticker}:{d.direction}",
        ticker=d.ticker,
        direction=d.direction,
        recommendation=d.recommendation,
        readiness=ready,
        evidence_status=EvidenceStatus.EXPERIMENTAL_RULES,
        path=d.path,
        market_mode=d.session.mode.value,
        decision_as_of=d.decided_at,
        headline=_headline(d, instrument),
        why_today=_why_today(d, window),
        option_evidence=_option_evidence(d),
        news_evidence=_news_evidence(d, window),
        price_evidence=_price_evidence(d, settings.price.benchmark),
        stock_plan=d.stock_plan,
        stock_plan_text=_stock_plan_text(d),
        options=tuple(structures),
        option_text=_option_text(structures, quotes_state),
        instrument=instrument,
        invalidation=_invalidation(d, settings),
        counter_argument=_counter(d),
        validation_text=(
            f"Deneysel kural ({settings.policy_version}); ölçülmüş işlem avantajı yok. "
            f"Türetildiği reddedilmiş çalışmalar: {'; '.join(settings.derived_from)}. "
            f"Fark: {settings.design_difference}"
        ),
        blockers=tuple(blockers),
        sources=tuple(sources),
        derived_from=settings.derived_from,
        policy_version=settings.policy_version,
        profile_sha256=settings.profile_sha256,
        rank_key=(RANK[d.recommendation], rank_premium),
        dimensions=dims,
        plain_action=plain_action,
        plain_reason=plain_reason,
    )


def sort_cards(cards: Sequence[Card]) -> list[Card]:
    return sorted(cards, key=lambda c: (c.rank_key[0], c.rank_key[1]), reverse=True)
