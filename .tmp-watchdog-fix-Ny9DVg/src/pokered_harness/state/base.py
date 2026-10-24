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
    """Map a raw sprite-facing byte to :class:`Direction`.

    Values outside the byte range and values that are not one of the four
    canonical encodings are unknown, so both return ``None``.  Do not wrap
    an out-of-range caller value into a valid direction: that would turn
    malformed state into a guessed observation.  Symbol-backed reads already
    pass through :meth:`SymbolTable.read_u8`, so this check does not alter
    normal emulator reads.
    """
    if not isinstance(raw, int) or not 0 <= raw <= 0xFF:
        return None
    try:
        return Direction(raw)
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
_STATUS_KNOWN_MASK = (
    _SLEEP_COUNTER_MASK
    | (1 << _POISON_BIT)
    | (1 << _BURN_BIT)
    | (1 << _FREEZE_BIT)
    | (1 << _PARALYSIS_BIT)
)
_STATUS_UNKNOWN_MASK = 0xFF ^ _STATUS_KNOWN_MASK


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
    def unknown_bits(self) -> int:
        """Return reserved bits that were present in the status byte.

        The known condition fields remain a lossless decomposition of their
        corresponding bits, but callers must not treat a status as a fully
        validated game value while this is non-zero.  In Gen 1, bit 7 is
        unused by the non-volatile status encoding.
        """
        return self.raw & _STATUS_UNKNOWN_MASK

    @property
    def is_valid(self) -> bool:
        """Whether the normalized byte contains only known status bits."""
        return 0 <= self.raw <= 0xFF and self.unknown_bits == 0

    @property
    def is_unknown(self) -> bool:
        """Whether reserved status bits make this observation unknown."""
        return not self.is_valid

    @property
    def is_healthy(self) -> bool:
        # Reserved bits are deliberately not interpreted as a healthy state.
        return self.is_valid and self.raw == 0


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
