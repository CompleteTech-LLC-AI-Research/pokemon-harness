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

# ``NUM_EVENTS`` in pret/pokered and pret/pokeyellow.  The event array is
# 0xA00 bits (320 bytes); the following WRAM union must never be interpreted
# as an event flag just because a caller supplied a large index.
NUM_EVENT_FLAGS = 0xA00


@dataclass(frozen=True, slots=True)
class ProgressState:
    badges_raw: int
    trainer_id: int | None
    money: int | None
    play_time_hours: int | None
    play_time_minutes: int | None
    play_time_seconds: int | None
    play_time_maxed: bool
    # Kept separate from the legacy boolean above so a missing optional
    # symbol is observable rather than indistinguishable from a real zero.
    play_time_maxed_raw: int | None = None

    def has_badge(self, badge: Badge) -> bool:
        return bool((self.badges_raw >> badge.value) & 1)

    @property
    def badges(self) -> tuple[Badge, ...]:
        return tuple(b for b in ALL_BADGES if self.has_badge(b))

    @property
    def badge_count(self) -> int:
        return (self.badges_raw & 0xFF).bit_count()

    @property
    def play_time_maxed_known(self) -> bool:
        """Whether ``wPlayTimeMaxed`` was present in the symbol table."""
        return self.play_time_maxed_raw is not None

    @property
    def play_time_maxed_value(self) -> bool | None:
        """Return the maxed flag, or ``None`` when its symbol was absent.

        ``play_time_maxed`` remains a boolean for compatibility with older
        callers; use this property when unknown and false must be distinct.
        """
        return self.play_time_maxed if self.play_time_maxed_known else None

    @property
    def play_time_known(self) -> bool:
        """Whether all exposed play-time components are symbol-backed and valid."""
        return (
            self.play_time_maxed_known
            and self.play_time_hours is not None
            and self.play_time_minutes is not None
            and self.play_time_seconds is not None
        )


def parse_progress(memory: MemoryLike, symbols: SymbolTable) -> ProgressState:
    badges_raw = symbols.read_u8(memory, "wObtainedBadges")

    trainer_id: int | None = None
    if "wPlayerID" in symbols:
        trainer_id = symbols.read_u16_be(memory, "wPlayerID")

    money: int | None = None
    if "wPlayerMoney" in symbols:
        money = _read_bcd3(memory, symbols.addr_of("wPlayerMoney"))

    hours = _opt_u8(memory, symbols, "wPlayTimeHours")
    minutes = _opt_u8_range(memory, symbols, "wPlayTimeMinutes", 59)
    seconds = _opt_u8_range(memory, symbols, "wPlayTimeSeconds", 59)

    play_time_maxed_raw = _opt_u8(memory, symbols, "wPlayTimeMaxed")
    # Preserve the historical bool field while retaining explicit unknown
    # semantics in play_time_maxed_raw/play_time_maxed_value.
    play_time_maxed = play_time_maxed_raw is not None and play_time_maxed_raw != 0

    return ProgressState(
        badges_raw=badges_raw,
        trainer_id=trainer_id,
        money=money,
        play_time_hours=hours,
        play_time_minutes=minutes,
        play_time_seconds=seconds,
        play_time_maxed=play_time_maxed,
        play_time_maxed_raw=play_time_maxed_raw,
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
    if (
        not isinstance(bit_index, int)
        or isinstance(bit_index, bool)
        or not 0 <= bit_index < NUM_EVENT_FLAGS
    ):
        raise ValueError(
            f"bit_index must be an integer in 0..{NUM_EVENT_FLAGS - 1}, got {bit_index!r}"
        )
    base = symbols.addr_of("wEventFlags")
    byte_offset, bit = divmod(bit_index, 8)
    value = int(memory[base + byte_offset]) & 0xFF
    return bool((value >> bit) & 1)


# --- helpers ---------------------------------------------------------------


def _opt_u8(memory: MemoryLike, symbols: SymbolTable, name: str) -> int | None:
    return symbols.read_u8(memory, name) if name in symbols else None


def _opt_u8_range(
    memory: MemoryLike,
    symbols: SymbolTable,
    name: str,
    maximum: int,
) -> int | None:
    value = _opt_u8(memory, symbols, name)
    return value if value is None or value <= maximum else None


def _read_bcd3(memory: MemoryLike, addr: int) -> int | None:
    """Decode 3 bytes of packed binary-coded decimal.

    Pokémon Red/Blue/Yellow store money as 6 decimal digits in 3 bytes,
    most significant first. Each nibble must be a decimal digit; malformed
    RAM is unknown rather than being converted into a guessed integer.
    """
    result = 0
    for i in range(3):
        b = int(memory[addr + i]) & 0xFF
        hi, lo = (b >> 4) & 0xF, b & 0xF
        if hi > 9 or lo > 9:
            return None
        result = result * 100 + hi * 10 + lo
    return result
