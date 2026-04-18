"""Shared state primitives."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class Direction(IntEnum):
    """Player / sprite facing direction.

    Values match pokered's ``SPRITE_FACING_*`` encoding in
    ``constants/sprite_data_constants.asm``: DOWN=0, UP=4, LEFT=8, RIGHT=12.
    """

    DOWN = 0
    UP = 4
    LEFT = 8
    RIGHT = 12


def parse_direction(raw: int) -> Direction | None:
    """Map a raw sprite-facing byte to :class:`Direction`, or ``None`` if
    the value is not one of the four canonical encodings (e.g. mid-turn
    animation frames or uninitialised sprite slots)."""
    try:
        return Direction(raw & 0xFF)
    except ValueError:
        return None


# Non-volatile status byte layout, per pokered ``constants/status_constants.asm``:
#   bits 0-2: sleep counter (0 = not asleep, otherwise turns remaining)
#   bit 3   : poison
#   bit 4   : burn
#   bit 5   : freeze
#   bit 6   : paralysis
# Bit 7 is unused by the Gen-1 non-volatile status byte.
_SLEEP_COUNTER_MASK = 0b0000_0111
_POISON_BIT = 3
_BURN_BIT = 4
_FREEZE_BIT = 5
_PARALYSIS_BIT = 6


@dataclass(frozen=True, slots=True)
class StatusCondition:
    raw: int
    sleep_turns: int
    poisoned: bool
    burned: bool
    frozen: bool
    paralyzed: bool

    @property
    def asleep(self) -> bool:
        return self.sleep_turns > 0

    @property
    def is_healthy(self) -> bool:
        return self.raw == 0


def parse_status(raw: int) -> StatusCondition:
    """Decode a non-volatile status byte into its component conditions."""
    raw &= 0xFF
    return StatusCondition(
        raw=raw,
        sleep_turns=raw & _SLEEP_COUNTER_MASK,
        poisoned=bool((raw >> _POISON_BIT) & 1),
        burned=bool((raw >> _BURN_BIT) & 1),
        frozen=bool((raw >> _FREEZE_BIT) & 1),
        paralyzed=bool((raw >> _PARALYSIS_BIT) & 1),
    )
