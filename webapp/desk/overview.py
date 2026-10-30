"""Presentation of already recorded opening advice; no strategy or I/O."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from webapp.opening.model import Clock, clock
from webapp.opening.view import Statement, View


@dataclass(frozen=True)
class Overview:
    clock: Clock
    kind: str
    title: str
    detail: str
    record_label: str
    next_at: datetime | None
    next_label: str
    buys: tuple[Statement, ...]
    exits: tuple[Statement, ...]
    reasons: tuple[tuple[str, int], ...]


def build(opening: View | None, now: datetime) -> Overview:
    c = opening.clock if opening else clock(now)
    buys: tuple[Statement, ...] = ()
    exits: tuple[Statement, ...] = ()
    reasons: tuple[tuple[str, int], ...] = ()
    kind, title, detail = "wait", "Yeni işlem için bekle", c.reason
    label = "Kayıtlar okunamadı"
    if opening:
        label = (
            ("Son kontrolde kayıt güncel" if opening.current else "Güncel kayıt bekleniyor")
            if opening.recorded
            else "İlk kayıt bekleniyor"
        )

        def current(s: Statement) -> bool:
            return bool(
                opening
                and opening.current
                and s.current
                and s.advice.expires_at
                and now < s.advice.expires_at
            )

        buys = tuple(
            s
            for s in opening.statements
            if c.phase == "assessment" and current(s) and s.advice.action == "BUY"
        )
        exits = tuple(
            s for s in opening.statements if s.advice.action in ("REVIEW", "SELL", "PAPER_EXIT")
        )
        counts = Counter(
            reason
            for s in opening.statements
            if s.advice.action == "WAIT"
            for reason in dict.fromkeys(s.advice.reasons)
        )
        reasons = tuple(
            sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:3]
        )  # Three presentation rows, not a trading threshold.
    if not opening:
        title, detail = (
            "Önce veri bağlantısını kontrol et",
            "Açılış kayıtları okunamadı. Güncel AL / SAT doğrulanamıyor.",
        )
    elif exits:
        kind, title, detail = (
            "risk",
            "Önce açık pozisyonlarını kontrol et",
            "Çıkış veya risk incelemesi isteyen kayıt var. PAPER ve kendi işlemlerin ayrı izlenir.",
        )
    elif buys:
        kind, title, detail = (
            "plan",
            f"{len(buys)} kayıtlı giriş planı var",
            "Güncel açılış kurallarını sağlayan planlar aşağıda. Fiyatı, stopu ve karşı görüşü plan ekranından kontrol et.",
        )
    elif c.phase == "before":
        title = "Hazırlık saatini bekle"
    elif c.phase == "preparation":
        title = "Listeyi hazırla, giriş için bekle"
    elif c.phase == "settling":
        title = "İlk 15 dakikanın tamamlanmasını bekle"
    elif c.phase == "closed":
        title = "Yeni giriş zamanı değil"
    elif not opening.recorded:
        title, detail = "Henüz kayıtlı karar yok", "Canlı tarama ve kalıcı karar kaydı bekleniyor."
    elif not opening.current:
        title, detail = (
            "Güncel karar kaydı bekleniyor",
            "Son kayıt güncel değil. Eski AL / SAT planları işlem çağrısı olarak gösterilmiyor.",
        )
    else:
        title, detail = (
            "Şu an yeni giriş planı yok",
            "Kayıtlar güncel; giriş koşullarını sağlayan kayıtlı AL planı yok.",
        )
    next_at = (
        c.preparation
        if c.phase == "before"
        else c.assessment
        if c.phase in ("preparation", "settling")
        else None
    )
    next_label = (
        "Hazırlık listesini gözden geçir"
        if c.phase == "before"
        else "Açılış sonrası giriş kontrolü"
        if next_at
        else "Sonraki seansın hazırlığında"
        if c.phase == "closed"
        else "Yeni tarama ve karar kaydı geldiğinde"
    )
    if kind in ("risk", "plan"):
        next_at = None
        next_label = (
            "Açık pozisyon riskini şimdi incele"
            if kind == "risk"
            else "Planı ve güncel fiyatı kontrol et"
        )
    return Overview(c, kind, title, detail, label, next_at, next_label, buys, exits, reasons)
