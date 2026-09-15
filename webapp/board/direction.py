"""Side-aware row direction for the Alfa Board (Phase 5.2.A1).

Contract: ``docs/phase-5.2-alfa-board-acceptance.md`` §5 A1 ("Direction is
side-aware"); decision P11.

The old dashboard called every call BULLISH, including sold calls. The board
reads the aggressor side the worker records in ``alfa_print_meta.fill_side``:

- bought (``at_ask``, and ``above_ask``, an even more aggressive buyer):
  call → ``yukarı``, put → ``aşağı``;
- sold (``at_bid``, and ``below_bid``): call → ``aşağı``, put → ``yukarı``;
- ``midpoint``, ``unknown`` or a legacy row with no meta: the direction falls
  back to option type (call → ``yukarı``, put → ``aşağı``), and the row
  carries ``FALLBACK_MARKER`` with the count of such prints.

Every label comes from the frozen dictionaries below (rule R-WD1).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

Direction = Literal["up", "down"]

# Contract §5 A1, byte for byte.
DIRECTION_LABELS: Final[Mapping[Direction, str]] = MappingProxyType(
    {"up": "yukarı", "down": "aşağı"},
)
FALLBACK_MARKER: Final = "yön opsiyon tipinden (alım/satım tarafı bilinmiyor)"

_BOUGHT_SIDES: Final = frozenset({"at_ask", "above_ask"})
_SOLD_SIDES: Final = frozenset({"at_bid", "below_bid"})
_OPPOSITE: Final[Mapping[Direction, Direction]] = MappingProxyType({"up": "down", "down": "up"})


@dataclass(frozen=True)
class DirectionRead:
    """A print's board direction and whether the aggressor side decided it."""

    direction: Direction
    side_aware: bool

    @property
    def label(self) -> str:
        return DIRECTION_LABELS[self.direction]


def option_type_direction(option_type: str) -> Direction:
    """The option-type direction: call → up, put → down."""
    return "up" if option_type == "call" else "down"


def opposite(direction: Direction) -> Direction:
    return _OPPOSITE[direction]


def direction_for(option_type: str, fill_side: str | None) -> DirectionRead:
    """Side-aware direction; ``fill_side=None`` means a legacy row without meta."""
    by_type = option_type_direction(option_type)
    if fill_side in _BOUGHT_SIDES:
        return DirectionRead(direction=by_type, side_aware=True)
    if fill_side in _SOLD_SIDES:
        return DirectionRead(direction=opposite(by_type), side_aware=True)
    return DirectionRead(direction=by_type, side_aware=False)
