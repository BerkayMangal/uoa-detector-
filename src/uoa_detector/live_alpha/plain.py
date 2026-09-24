"""Plain-Turkish verdict for each card: one line a non-specialist can act on.

The technical card text stays (it is the audit trail), but the page leads with
this: what to do, at which price, and the one reason, in everyday words. Every
number here comes from the decision's own fields; nothing is re-estimated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from uoa_detector.live_alpha.calendar import MarketMode
from uoa_detector.live_alpha.flow import qualifying_direction
from uoa_detector.live_alpha.model import CheckState, Path, Recommendation

if TYPE_CHECKING:
    from uoa_detector.live_alpha.decide import Decision
    from uoa_detector.live_alpha.model import InstrumentChoice
    from uoa_detector.live_alpha.settings import LiveAlphaSettings


def money(v: float) -> str:
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f} milyon $"
    if v >= 1_000:
        return f"{v / 1_000:.0f} bin $"
    return f"{v:.0f} $"


def px(v: float | None) -> str:
    return "—" if v is None else f"${v:,.2f}"


def _flow_reason(d: Decision, settings: LiveAlphaSettings) -> str:
    f = d.flow
    fs = settings.flow
    if f.prints_deduped == 0:
        return "Bugün bu hissede kayda değer opsiyon işlemi yok."
    if f.side_aware_share < fs.side_aware_share_min:
        return "Opsiyon işlemlerinin çoğunda alanın mı satanın mı agresif olduğu belli değil; yön okunamıyor."
    up, down = f.premium_up, f.premium_down
    dominant_prem = max(up, down)
    if dominant_prem < fs.min_directional_premium_usd:
        return f"Yönlü opsiyon parası az ({money(dominant_prem)}); anlamlı bir bahis yok."
    share = f.share("up" if up >= down else "down")
    if share < fs.dominance_min:
        return (
            f"Opsiyon oyuncuları ikiye bölünmüş: yükselişe {money(up)}, düşüşe {money(down)} bahis var. "
            "Net yön yok."
        )
    return "Para tek bir kontratta toplanmış; tek bir oyuncunun pozisyonu olabilir, kalabalık değil."


def _bet_text(d: Decision) -> str:
    f = d.flow
    if d.direction == "up":
        return f"opsiyon parası yükselişe oynuyor ({money(f.premium_up)}, payı %{f.share('up') * 100:.0f})"
    return f"opsiyon parası düşüşe oynuyor ({money(f.premium_down)}, payı %{f.share('down') * 100:.0f})"


def verdict(
    d: Decision, instrument: InstrumentChoice, settings: LiveAlphaSettings, *, ready: bool,
) -> tuple[str, str]:
    """(short action, one-sentence reason) in plain Turkish."""
    s = d.stock_plan
    stale_flow = d.blockers and any("bugünkü seansa ait değil" in b for b in d.blockers)
    tail = " Bu dünkü işlemlere dayanıyor; bugünün işlemleri gelince yeniden bakılacak." if stale_flow else ""
    via = {"stock": "hisse", "none": ""}.get(instrument.preferred, instrument.preferred.replace("_", " "))

    if d.recommendation is Recommendation.BUY and s is not None and ready:
        action = (
            f"AL — {px(s.entry_ref)} civarından, en fazla {px(s.chase_limit)}'e kadar. "
            f"Zarar-kes {px(s.stop)}, hedef {px(s.target)}, en çok {s.horizon_sessions} gün."
        )
        why = f"{_bet_text(d).capitalize()} ve hisse de aynı yöne gidiyor"
        why += (
            f"; yeni haber: “{d.fresh_news[0].headline}”." if d.fresh_news
            else f"; piyasadan (SPY) {(d.price.relative or 0) * 100:.1f} puan güçlü."
        )
        if via and via != "hisse":
            why += f" Opsiyonla yapılacaksa: {via}."
        return action, why
    if d.recommendation in (Recommendation.BUY, Recommendation.CONDITIONAL_BUY) and s is not None:
        if d.path is Path.P3_PULLBACK:
            return (
                f"BEKLE, KOVALAMA — {px(s.entry_zone_high)} altına geri gelirse al "
                f"(zarar-kes {px(s.stop)}, hedef {px(s.target)}).",
                f"{_bet_text(d).capitalize()} ama hisse bugün zaten çok yükseldi; bu fiyattan girmek geç kalmak olur.{tail}",
            )
        if d.session.mode is not MarketMode.LIVE:
            return (
                f"PİYASA AÇILINCA AL — fiyat {px(s.entry_zone_low)} - {px(s.entry_zone_high)} arasındaysa "
                f"(zarar-kes {px(s.stop)}, hedef {px(s.target)}).",
                f"{_bet_text(d).capitalize()}; piyasa kapalı olduğu için şimdilik plan.{tail}",
            )
        return (
            f"HENÜZ DEĞİL — fiyat {px(s.entry_zone_low)} - {px(s.entry_zone_high)} arasında teyit edilirse al.",
            f"{_bet_text(d).capitalize()} ama {'bugünün akışı henüz yok' if stale_flow else 'fiyat bilgisi taze değil'}.{tail}",
        )
    if d.recommendation is Recommendation.BEARISH_SETUP:
        return (
            "ALMA — düşüş kurulumu.",
            f"{_bet_text(d).capitalize()} ve hisse de düşüyor.{' Düşüşe oynamak istersen: ' + via + '.' if via and via != 'hisse' else ''}{tail}",
        )
    if d.recommendation is Recommendation.AVOID:
        return "ALMA — çelişki var.", f"{_bet_text(d).capitalize()} ama hisse ters yönde, düşüyor.{tail}"

    # WATCH: say which condition is missing, in words
    direction, _ = qualifying_direction(d.flow, settings.flow)
    if direction is None:
        return "BEKLE — işlem yok.", _flow_reason(d, settings) + tail
    if d.price.state is not CheckState.CHECKED_FOUND:
        return "BEKLE — veri eksik.", "Fiyat geçmişi eksik; giriş ve zarar-kes hesaplanamıyor."
    move = d.price.move_atr or 0.0
    if direction == "down":
        return "BEKLE.", f"{_bet_text(d).capitalize()} ama hisse düşmüyor; teyit yok.{tail}"
    if move < settings.price.confirm_min_atr:
        return "BEKLE.", f"{_bet_text(d).capitalize()} ama hisse henüz yükselmiyor; teyit yok.{tail}"
    news_txt = (
        "haber kontrol edilemedi" if d.news.state is CheckState.FAILED else "destekleyen yeni haber yok"
    )
    return (
        "BEKLE.",
        f"{_bet_text(d).capitalize()} ve hisse yükseliyor, ama {news_txt} ve hisse piyasadan belirgin güçlü değil.{tail}",
    )
