"""Safe native connection hints; raw SQL, parameters and messages never leave here."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.exc import DBAPIError, SQLAlchemyError


class Reason(StrEnum):
    RECOVERY = "recovery"
    STARTING = "starting"
    SHUTDOWN = "shutdown"
    NOT_ACCEPTING = "not_accepting"
    REFUSED = "refused"
    DNS = "dns"
    TIMEOUT = "connection_timeout"
    AUTH = "authentication"
    TLS = "tls"
    CONNECTIONS = "connection_capacity"
    LOST = "connection_lost"


@dataclass(frozen=True)
class Hint:
    code: Reason
    label: str
    action: str


_COPY = {
    Reason.RECOVERY: (
        "Veritabanı kurtarma sürecinde.",
        "Railway'de PostgreSQL servisinin logundan kurtarma sürecini kontrol et. Bu ekran sürecin tamamlandığını doğrulayamaz.",
    ),
    Reason.STARTING: (
        "Veritabanı henüz açılıyor.",
        "PostgreSQL servisinin açılış logunu kontrol et.",
    ),
    Reason.SHUTDOWN: (
        "Veritabanı kapanıyor veya yeniden başlatılıyor.",
        "PostgreSQL servisinin çalışma durumunu ve son loglarını kontrol et.",
    ),
    Reason.NOT_ACCEPTING: (
        "Veritabanı bağlantı kabul etmiyor.",
        "Bu kod tek başına kurtarma sürecini kanıtlamaz. PostgreSQL servisinin kendi logu gerekiyor.",
    ),
    Reason.REFUSED: (
        "Veritabanı bağlantısı reddediliyor.",
        "Railway'de PostgreSQL servisinin çalıştığını ve uygulamanın veritabanı bağlantı tanımını kontrol et.",
    ),
    Reason.DNS: (
        "Veritabanı adresi çözümlenemiyor.",
        "Railway servis bağlantısını ve adres tanımını kontrol et; bağlantı bilgisini burada paylaşma.",
    ),
    Reason.TIMEOUT: (
        "Veritabanı bağlantısı zaman aşımına uğruyor.",
        "PostgreSQL servisinin erişilebilirliğini ve kaynak kullanımını kontrol et.",
    ),
    Reason.AUTH: (
        "Veritabanı kimlik doğrulaması başarısız.",
        "Railway'de uygulamanın bağlantı değişkenini PostgreSQL servis referansıyla karşılaştır; şifreyi sohbete yapıştırma.",
    ),
    Reason.TLS: (
        "Veritabanı güvenli bağlantısı doğrulanamadı.",
        "Bağlantının TLS/sertifika ayarını kontrol et; doğrulamayı kapatarak geçme.",
    ),
    Reason.CONNECTIONS: (
        "Veritabanı bağlantı kapasitesi dolu.",
        "PostgreSQL bağlantı sayısını ve aynı veritabanını kullanan servisleri kontrol et.",
    ),
    Reason.LOST: (
        "Veritabanı bağlantısı kesildi.",
        "PostgreSQL servisinin yeniden başlama ve bağlantı loglarını kontrol et.",
    ),
}


def describe(code: Reason) -> Hint:
    label, action = _COPY[code]
    return Hint(code, label, action)


def native_hint(exc: Exception) -> Hint | None:
    if not isinstance(exc, DBAPIError) or not isinstance(exc.orig, BaseException):
        return None
    original = exc.orig
    if isinstance(original, SQLAlchemyError):
        return None
    try:
        code = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        text = str(original).lower()
    except Exception:
        return None
    # Native detail is more precise than 57P03, which has several possible causes.
    patterns = (
        (Reason.RECOVERY, ("database system is in recovery mode",)),
        (Reason.STARTING, ("database system is starting up",)),
        (Reason.SHUTDOWN, ("database system is shutting down",)),
        (
            Reason.AUTH,
            (
                "password authentication failed",
                "no pg_hba.conf entry",
            ),
        ),
        (
            Reason.CONNECTIONS,
            ("too many connections", "remaining connection slots", "too many clients already"),
        ),
        (
            Reason.DNS,
            (
                "could not translate host name",
                "name or service not known",
                "temporary failure in name resolution",
                "nodename nor servname provided",
            ),
        ),
        (
            Reason.TLS,
            (
                "certificate verify failed",
                "server does not support ssl",
                "ssl certificate verification failed",
            ),
        ),
        (Reason.REFUSED, ("connection refused",)),
        (Reason.TIMEOUT, ("connection timeout expired", "connection timed out", "timeout expired")),
    )
    for reason, fragments in patterns:
        if any(fragment in text for fragment in fragments):
            return describe(reason)
    by_code = {
        "57P03": Reason.NOT_ACCEPTING,
        "57P01": Reason.SHUTDOWN,
        "28P01": Reason.AUTH,
        "28000": Reason.AUTH,
        "53300": Reason.CONNECTIONS,
        "08006": Reason.LOST,
        "08003": Reason.LOST,
    }
    known_reason = by_code.get(code) if isinstance(code, str) else None
    return describe(known_reason) if known_reason else None
