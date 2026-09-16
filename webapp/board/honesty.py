"""Forbidden-word guard for Alfa Board generated text (Phase 5.2, contract R-WD1).

The owner bans these words from generated text: ``al``, ``öneri``, ``fırsat``,
``en iyi``, ``garanti``. Matching is word-boundary based on a folded form of
the text:

- Turkish-aware lowercasing first (``I``→``ı``, ``İ``→``i``), so ``FIRSAT``
  matches ``fırsat`` and ``İyi`` matches ``iyi``;
- then ASCII folding (``ı``→``i``, ``ö``→``o``, ``ü``→``u``, ``ş``→``s``,
  ``ç``→``c``, ``ğ``→``g``), so text typed without Turkish letters
  (``GARANTI``, ``EN IYI``, ``ONERI``) is caught as well (review FA-06).

Per entry:

- ``al`` matches its advisory forms only:
  - imperative and optative: ``al``, ``alın``, ``alınız``, ``alsana``,
    ``alsanıza``, ``alalım``, ``alsın``, ``alsınlar``;
  - necessitative: ``almalı...`` and ``alınmalı...`` (``almalısın``);
  - abilitative: ``alabilir...`` and ``alınabilir...`` (``alabilirsin``,
    ``canlı alınabilir fiyat``).
  Nouns and descriptive forms such as ``alış``, ``alım``, ``alınan`` and
  ``alındı`` are not advice and do not match.
- The other entries match their stem, so ``önerilir``, ``fırsatı``,
  ``garantili`` and ``en iyisi`` are caught.
- The owner-named gate label ``Alabileceklerimi göster`` is static UI chrome.
  It also does not match: ``alabilecek...`` is a participle, not one of the
  advisory forms above.
"""

from __future__ import annotations

import re
from typing import Final

_ASCII_FOLD: Final = str.maketrans({"ı": "i", "ö": "o", "ü": "u", "ş": "s", "ç": "c", "ğ": "g"})

# Advisory forms of "al", written ASCII-folded (see the module docstring).
_AL_FORMS: Final[tuple[str, ...]] = (
    "al", "alin", "aliniz",
    "alsana", "alsaniza", "alalim", "alsin", "alsinlar",
    r"almali\w*", r"alinmali\w*",
    r"alabilir\w*", r"alinabilir\w*",
)

_FORBIDDEN: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("al", re.compile(r"(?<!\w)(?:" + "|".join(_AL_FORMS) + r")(?!\w)")),
    ("öneri", re.compile(r"(?<!\w)oner\w*")),
    ("fırsat", re.compile(r"(?<!\w)firsat\w*")),
    ("en iyi", re.compile(r"(?<!\w)en\s+iyi\w*")),
    ("garanti", re.compile(r"(?<!\w)garanti\w*")),
)


def turkish_lower(text: str) -> str:
    """Lowercase with Turkish dotted/dotless I rules (``I``→``ı``, ``İ``→``i``)."""
    return text.replace("I", "ı").replace("İ", "i").lower()


def _folded(text: str) -> str:
    return turkish_lower(text).translate(_ASCII_FOLD)


def forbidden_words(text: str) -> list[str]:
    """Names of the forbidden entries found in ``text``, in rule order, no repeats."""
    folded = _folded(text)
    return [name for name, pattern in _FORBIDDEN if pattern.search(folded)]


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
