"""Forbidden-word guard for Alfa Board generated text (Phase 5.2, contract R-WD1).

The owner bans these words from generated text: ``al``, ``öneri``, ``fırsat``,
``en iyi``, ``garanti``. Matching is word-boundary based after Turkish-aware
lowercasing, so ``FIRSAT`` matches ``fırsat`` and ``İyi`` matches ``iyi``.

- ``al`` matches only the standalone imperative forms (``al``, ``alın``,
  ``alınız``). Nouns such as ``alış`` and participles such as ``alınan`` are
  descriptive, not advice.
- The other entries match their stem, so ``önerilir``, ``fırsatı``,
  ``garantili`` and ``en iyisi`` are caught as well.
- The owner-named gate label ``Alabileceklerimi göster`` is static UI chrome.
  It also does not match: ``alabilecek...`` is not a standalone ``al``.
"""

from __future__ import annotations

import re
from typing import Final

_FORBIDDEN: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("al", re.compile(r"(?<!\w)(?:al|alın|alınız)(?!\w)")),
    ("öneri", re.compile(r"(?<!\w)öner\w*")),
    ("fırsat", re.compile(r"(?<!\w)fırsat\w*")),
    ("en iyi", re.compile(r"(?<!\w)en\s+iyi\w*")),
    ("garanti", re.compile(r"(?<!\w)garanti\w*")),
)


def turkish_lower(text: str) -> str:
    """Lowercase with Turkish dotted/dotless I rules (``I``→``ı``, ``İ``→``i``)."""
    return text.replace("I", "ı").replace("İ", "i").lower()


def forbidden_words(text: str) -> list[str]:
    """Names of the forbidden entries found in ``text``, in rule order, no repeats."""
    lowered = turkish_lower(text)
    return [name for name, pattern in _FORBIDDEN if pattern.search(lowered)]


def ensure_clean(text: str) -> str:
    """Return ``text`` unchanged, or raise ``ValueError`` naming the forbidden words.

    Used by the frozen dictionaries' builders so a bad template fails loudly in
    tests instead of rendering advice language.
    """
    found = forbidden_words(text)
    if found:
        msg = f"forbidden words in generated board text: {found}"
        raise ValueError(msg)
    return text
