"""Progression state parser.

Covers the handful of RAM fields that together describe "how far into the
game am I" — badges, trainer identity, money, play time, and raw event
flags. Specific event flags (e.g. "defeated Brock") are addressed by bit
index rather than by named constant; the full constant → bit map lives in
``pret/pokered/constants/event_constants.asm`` and is too large to
duplicate here. Callers pass the bit index they care about, sourced from
that file.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from pokered_harness.symbols.loader import MemoryLike, SymbolTable


class Badge(IntEnum):
    """Bit positions inside ``wObtainedBadges``.

    Order matches pokered's ``constants/event_constants.asm`` badge
    constants: Boulder first, Earth last.
    """

    BOULDER = 0
    CASCADE = 1
    THUNDER = 2
    RAINBOW = 3
    SOUL = 4
    MARSH = 5
    VOLCANO = 6
    EARTH = 7


ALL_BADGES: tuple[Badge, ...] = tuple(Badge)


@dataclass(frozen=True, slots=True)
class ProgressState:
    badges_raw: int
    trainer_id: int | None
    money: int | None
    play_time_hours: int | None
    play_time_minutes: int | None
    play_time_seconds: int | None
    play_time_maxed: bool

    def has_badge(self, badge: Badge) -> bool:
        return bool((self.badges_raw >> badge.value) & 1)

    @property
    def badges(self) -> tuple[Badge, ...]:
        return tuple(b for b in ALL_BADGES if self.has_badge(b))

    @property
    def badge_count(self) -> int:
        return (self.badges_raw & 0xFF).bit_count()


def parse_progress(memory: MemoryLike, symbols: SymbolTable) -> ProgressState:
    badges_raw = symbols.read_u8(memory, "wObtainedBadges")

    trainer_id: int | None = None
    if "wPlayerID" in symbols:
        trainer_id = symbols.read_u16_be(memory, "wPlayerID")

    money: int | None = None
    if "wPlayerMoney" in symbols:
        money = _read_bcd3(memory, symbols.addr_of("wPlayerMoney"))

    hours = _opt_u8(memory, symbols, "wPlayTimeHours")
    minutes = _opt_u8(memory, symbols, "wPlayTimeMinutes")
    seconds = _opt_u8(memory, symbols, "wPlayTimeSeconds")

    play_time_maxed = False
    if "wPlayTimeMaxed" in symbols:
        play_time_maxed = symbols.read_u8(memory, "wPlayTimeMaxed") != 0

    return ProgressState(
        badges_raw=badges_raw,
        trainer_id=trainer_id,
        money=money,
        play_time_hours=hours,
        play_time_minutes=minutes,
        play_time_seconds=seconds,
        play_time_maxed=play_time_maxed,
    )


def read_event_flag(
    memory: MemoryLike,
    symbols: SymbolTable,
    bit_index: int,
) -> bool:
    """Read event flag at bit index ``bit_index`` inside ``wEventFlags``.

    Event flags are a contiguous bit array: byte ``bit_index >> 3`` holds
    the bit at position ``bit_index & 7`` (low-bit first). Bit indices are
    the values of ``EVENT_*`` constants in pokered, not byte offsets.
    """
    if bit_index < 0:
        raise ValueError(f"bit_index must be non-negative, got {bit_index}")
    base = symbols.addr_of("wEventFlags")
    byte_offset, bit = divmod(bit_index, 8)
    value = int(memory[base + byte_offset]) & 0xFF
    return bool((value >> bit) & 1)


# --- helpers ---------------------------------------------------------------


def _opt_u8(memory: MemoryLike, symbols: SymbolTable, name: str) -> int | None:
    return symbols.read_u8(memory, name) if name in symbols else None


def _read_bcd3(memory: MemoryLike, addr: int) -> int:
    """Decode 3 bytes of packed binary-coded decimal.

    Pokémon Red stores money as 6 decimal digits in 3 bytes, most
    significant first. Each nibble is one decimal digit; e.g.
    ``[0x01, 0x23, 0x45]`` means 12345(5), i.e. 123,455? No — it is six
    digits read left-to-right: ``123_456`` if the bytes are
    ``[0x12, 0x34, 0x56]``. The implementation below matches that
    digit-pair-per-byte encoding directly.
    """
    result = 0
    for i in range(3):
        b = int(memory[addr + i]) & 0xFF
        hi, lo = (b >> 4) & 0xF, b & 0xF
        result = result * 100 + hi * 10 + lo
    return result
