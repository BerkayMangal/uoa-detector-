"""Phase 5.2.A-fix6: R-WD1 guard catches ASCII spellings and advisory "al" forms (review FA-06).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §2 R-WD1 ("Word-boundary
and stem matching; ``Alabileceklerimi göster`` ... is not generated text").

Pins:
  - text typed without Turkish letters is caught: ``GARANTI``, ``EN IYI``,
    ``ONERI``, ``FIRSATI``;
  - advisory inflections of ``al`` are caught: modal (``alabilirsin``,
    ``alınabilir``), necessitative (``almalısın``, ``alınmalı``) and
    imperative/optative (``alsana``, ``alalım``);
  - descriptive nouns and participles stay clean (``alış``, ``alım``,
    ``alındı``, ``alınan``, ``alan``, ``aleyhte``, ``alfa``, ``altında``), and
    so does the owner gate label;
  - every string the board's frozen dictionaries hold still passes.
"""

from __future__ import annotations

import pytest
from webapp.board import alfa_page, copy_tr, evidence, narrative, penalty_ledger, tradability
from webapp.board.honesty import ensure_clean, forbidden_words


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("GARANTI", ["garanti"]),
        ("GARANTILI GETIRI", ["garanti"]),
        ("EN IYI kurulum", ["en iyi"]),
        ("ONERI", ["öneri"]),
        ("FIRSATI kacirma", ["fırsat"]),
        ("firsat", ["fırsat"]),
        ("Bunu alabilirsin", ["al"]),
        ("ALABILIRSIN", ["al"]),
        ("hemen alınmalı", ["al"]),
        ("bunu almalısın", ["al"]),
        ("alınabilir", ["al"]),
        ("canlı alınabilir fiyat", ["al"]),
        ("alsana şunu", ["al"]),
        ("Alalım", ["al"]),
        ("Bu kontratı alin", ["al"]),
    ],
)
def test_ascii_spellings_and_advisory_forms_are_caught(text: str, expected: list[str]) -> None:
    assert forbidden_words(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        copy_tr.GATE_LABEL,
        "alış fiyatı (ask)",
        "alım/satım tarafı",
        "kotasyon 41 sn önce alındı",
        "alınan prim",
        "alan",
        "2 aile aleyhte",
        "Alfa Board",
        "ask bid'in altında",
        "ONEMLI NOT",
        "iyi bir kurulum değil",
    ],
)
def test_descriptive_forms_stay_clean(text: str) -> None:
    assert forbidden_words(text) == []


def _strings(mapping: object) -> list[str]:
    out: list[str] = []
    values = mapping.values() if hasattr(mapping, "values") else ()  # type: ignore[union-attr]
    for value in values:
        if isinstance(value, str):
            out.append(value)
        elif hasattr(value, "values"):
            out.extend(_strings(value))
    return out


def test_every_frozen_dictionary_still_passes() -> None:
    dictionaries = [
        alfa_page.ALFA_COPY, alfa_page.AUDIT_INPUT_LABELS, alfa_page.FILL_SIDE_LABELS,
        evidence.FAMILY_LABELS, evidence.STATE_LABELS, evidence.NOTE_TEMPLATES, evidence.EVIDENCE_COPY,
        evidence.STRENGTH_LABELS, evidence.LEGACY_HOVER,
        narrative.NARRATIVE_COPY, narrative.REASON_TEMPLATES, narrative.DIRECTION_OBJECTS,
        narrative.POSITION_CLAUSES, narrative.COUNTER_TEMPLATES, narrative.CHECK_TEMPLATES,
        tradability.STATE_LABELS, tradability.REASON_TEMPLATES, tradability.CHIP_COPY,
        penalty_ledger.LEDGER_COPY,
    ]
    texts = [text for mapping in dictionaries for text in _strings(mapping)]
    texts += [copy_tr.EVIDENCE_HOVER, copy_tr.UNKNOWN_NOT_CLEAN, copy_tr.NO_COUNTER_FOUND,
              copy_tr.IV_NOT_SELL_VOL, copy_tr.NO_CLEAN_CANDIDATE, copy_tr.STRONG_LABEL]
    assert len(texts) > 100
    for text in texts:
        assert ensure_clean(text) == text
