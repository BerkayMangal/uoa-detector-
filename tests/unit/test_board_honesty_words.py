"""Phase 5.2 contract R-WD1 and the frozen Turkish copy (webapp/board)."""

from __future__ import annotations

import pytest
from webapp.board import copy_tr
from webapp.board.honesty import ensure_clean, forbidden_words, turkish_lower


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("NVDA al", ["al"]),
        ("Hemen AL!", ["al"]),
        ("Bu kontratı alın.", ["al"]),
        ("Lütfen alınız", ["al"]),
        ("Bu bir öneri değildir", ["öneri"]),
        ("Önerilir: dikkat", ["öneri"]),
        ("Kaçırılmayacak FIRSAT", ["fırsat"]),
        ("fırsatı kaçırma", ["fırsat"]),
        ("en iyi kurulum", ["en iyi"]),
        ("En İyisi bu", ["en iyi"]),
        ("garantili getiri", ["garanti"]),
        ("al, öneri, fırsat, en iyi, garanti", ["al", "öneri", "fırsat", "en iyi", "garanti"]),
    ],
)
def test_forbidden_words_detected(text: str, expected: list[str]) -> None:
    assert forbidden_words(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        copy_tr.GATE_LABEL,
        "alış fiyatı (ask) ile giriş",
        "alınan prim tek strike'ta toplandı",
        "kalan süre 4 gün",
        "sinyal almadıklarım defterinde",
        "Kalıcı bir pozisyon mu, dağınık envanter mi?",
        "iyi bir kurulum değil",
        copy_tr.EVIDENCE_HOVER,
        copy_tr.UNKNOWN_NOT_CLEAN,
        copy_tr.NO_COUNTER_FOUND,
        copy_tr.IV_NOT_SELL_VOL,
        copy_tr.NO_CLEAN_CANDIDATE,
    ],
)
def test_clean_texts_pass(text: str) -> None:
    assert forbidden_words(text) == []
    assert ensure_clean(text) == text


def test_ensure_clean_raises_with_names() -> None:
    with pytest.raises(ValueError, match="fırsat"):
        ensure_clean("Bugünün fırsatı")


def test_turkish_lower_handles_dotted_and_dotless_i() -> None:
    assert turkish_lower("FIRSAT İYİ") == "fırsat iyi"


def test_mandated_copy_is_byte_exact() -> None:
    assert copy_tr.EVIDENCE_HOVER == "Kâr olasılığı DEĞİL."
    assert copy_tr.UNKNOWN_NOT_CLEAN == "bilgi yok, temiz demek değil"
    assert copy_tr.NO_COUNTER_FOUND == (
        "Bariz bir karşı argüman bulunamadı — bu bir onay değildir"
    )
    assert copy_tr.IV_NOT_SELL_VOL == (
        "Bu 'vol sat' demek DEĞİLDİR — o tez test edildi, "
        "maliyet ve örnek-dışı testten sonra ayakta kalmadı."
    )
    assert copy_tr.NO_CLEAN_CANDIDATE == "Bugün temiz aday yok"
    assert copy_tr.GATE_LABEL == "Alabileceklerimi göster"
    assert copy_tr.COUNTER_LEAD == "AMA"
    assert copy_tr.STRONG_LABEL == "Güçlü"
