"""Predefined operational categories; safe counts never include vendor bodies."""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping

from uoa_detector.live_alpha.model import CheckState, NewsCheck

NEWS_LABELS = {
    "http_401": "Haber anahtarı kabul edilmedi",
    "http_403": "Haber kaynağı erişimi reddetti",
    "http_404": "Haber adresi veya istek bulunamadı",
    "http_422": "Haber isteği geçersiz bulundu",
    "auth_or_request": "Haber erişimi veya istek hatası",
    "daily_quota": "Haber sorgusu günlük kotaya takıldı",
    "rate_limit": "Haber sorgusu hız sınırına takıldı",
    "payload": "Haber yanıtının biçimi okunamadı",
    "transport": "Haber bağlantısı tamamlanamadı",
    "other": "Haber kontrolü tamamlanamadı",
}


def safe_http_detail(exc: Exception) -> str:
    match = re.search(r"\bHTTP\s+(401|403|404|422)\b", str(exc))
    code = match.group(1) if match else getattr(exc, "status_code", None)
    return f"UW HTTP {code}: haber isteği reddedildi" if str(code) in ("401", "403", "404", "422") else "UW yetki/istek hatası"


def news_kind(detail: str) -> str:
    if "404/422" in detail:
        return "auth_or_request"
    match = re.search(r"\bHTTP\s+(401|403|404|422)\b", detail)
    if match:
        return f"http_{match.group(1)}"
    if "günlük" in detail or "kota reddi" in detail:
        return "daily_quota"
    if "429" in detail or "hız sınırı" in detail:
        return "rate_limit"
    if "yanıt biçimi" in detail:
        return "payload"
    if "yetki/istek" in detail:
        return "auth_or_request"
    if "geçici hata" in detail:
        return "transport"
    return "other"


def news_counts(checks: Iterable[NewsCheck]) -> dict[str, int]:
    return dict(Counter(news_kind(n.detail) for n in checks if n.state is CheckState.FAILED))


def news_message(counts: Mapping[str, int]) -> str:
    return "; ".join(f"{NEWS_LABELS[k]} ({n} hisse)" for k, n in counts.items() if k in NEWS_LABELS and n > 0)
