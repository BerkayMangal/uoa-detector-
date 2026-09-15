"""Phase 5.2.A1: side-aware direction (``webapp/board/direction.py``).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §5 A1; decision P11.

Pins:
  - bought (``at_ask``/``above_ask``): call → up, put → down;
  - sold (``at_bid``/``below_bid``): call → down, put → up;
  - ``midpoint``, ``unknown``, an unexpected value or a legacy row (no meta)
    fall back to option type and are flagged ``side_aware=False``;
  - labels and the fallback marker are the contract strings, frozen, and
    pass the forbidden-word guard.
"""

from __future__ import annotations

import pytest
from webapp.board.direction import (
    DIRECTION_LABELS,
    FALLBACK_MARKER,
    direction_for,
    opposite,
    option_type_direction,
)
from webapp.board.honesty import ensure_clean


@pytest.mark.parametrize(
    ("option_type", "fill_side", "direction", "side_aware"),
    [
        ("call", "at_ask", "up", True),
        ("put", "at_ask", "down", True),
        ("call", "at_bid", "down", True),
        ("put", "at_bid", "up", True),
        ("call", "above_ask", "up", True),
        ("put", "above_ask", "down", True),
        ("call", "below_bid", "down", True),
        ("put", "below_bid", "up", True),
        ("call", "midpoint", "up", False),
        ("put", "midpoint", "down", False),
        ("call", "unknown", "up", False),
        ("put", "unknown", "down", False),
        ("call", None, "up", False),
        ("put", None, "down", False),
        ("call", "not-a-side", "up", False),
    ],
)
def test_direction_for(
    option_type: str, fill_side: str | None, direction: str, side_aware: bool,
) -> None:
    read = direction_for(option_type, fill_side)
    assert read.direction == direction
    assert read.side_aware is side_aware


def test_option_type_direction_and_opposite() -> None:
    assert option_type_direction("call") == "up"
    assert option_type_direction("put") == "down"
    assert opposite("up") == "down"
    assert opposite("down") == "up"


def test_labels_are_the_contract_strings() -> None:
    assert dict(DIRECTION_LABELS) == {"up": "yukarı", "down": "aşağı"}
    assert FALLBACK_MARKER == "yön opsiyon tipinden (alım/satım tarafı bilinmiyor)"
    assert direction_for("call", "at_bid").label == "aşağı"
    assert direction_for("put", "at_bid").label == "yukarı"


def test_labels_are_frozen() -> None:
    with pytest.raises(TypeError):
        DIRECTION_LABELS["up"] = "x"  # type: ignore[index]


def test_every_direction_string_is_clean() -> None:
    for text in (*DIRECTION_LABELS.values(), FALLBACK_MARKER):
        assert ensure_clean(text) == text
