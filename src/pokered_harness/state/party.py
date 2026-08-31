"""Party state parser.

Reads the 6-slot party block as 44-byte ``party_struct`` records. The
struct layout is fixed by pokered's ``macros/wram.asm`` — we trust it
absolutely because it's part of the game engine's binary ABI with its
own save format, not documentation.

Layout (offsets within a party_struct, starting at ``wPartyMonN``)::

    0      Species              (u8)
    1-2    HP                   (u16, big-endian)
    3      BoxLevel             (u8)   — always 0 in party
    4      Status               (u8)
    5      Type1                (u8)
    6      Type2                (u8)
    7      CatchRate            (u8)
    8-11   Moves                (4 x u8)
    12-13  OTID                 (u16, big-endian)
    14-16  Exp                  (u24, big-endian)
    17-26  stat experience      (5 x u16, big-endian)
    27-28  DVs                  (packed u16)
    29-32  PP                   (4 x u8)
    33     Level                (u8)
    34-35  MaxHP                (u16, big-endian)
    36-37  Attack               (u16, big-endian)
    38-39  Defense              (u16, big-endian)
    40-41  Speed                (u16, big-endian)
    42-43  Special              (u16, big-endian)

Most multi-byte scalars are **big-endian** inside the struct, which
differs from raw CPU memory convention — that's why the symbol-layer
readers offer both ``read_u16_le`` and ``read_u16_be``.
"""

from __future__ import annotations

from dataclasses import dataclass

from pokered_harness.state.base import StatusCondition, parse_status
from pokered_harness.symbols.loader import MemoryLike, SymbolTable

PARTY_STRUCT_SIZE = 44
MAX_PARTY_SLOTS = 6
NUM_MOVES = 4

# Offsets within party_struct (see module docstring).
_OFFSET_SPECIES = 0
_OFFSET_HP = 1
_OFFSET_STATUS = 4
_OFFSET_TYPE1 = 5
_OFFSET_TYPE2 = 6
_OFFSET_MOVES = 8
_OFFSET_PP = 29
_OFFSET_LEVEL = 33
_OFFSET_MAX_HP = 34


@dataclass(frozen=True, slots=True)
class PartyMon:
    slot: int
    species: int
    level: int
    hp: int
    max_hp: int
    status: StatusCondition
    type1: int
    type2: int
    moves: tuple[int, int, int, int]
    pp: tuple[int, int, int, int]

    @property
    def fainted(self) -> bool:
        return self.hp == 0

    @property
    def hp_fraction(self) -> float:
        return (self.hp / self.max_hp) if self.max_hp else 0.0


@dataclass(frozen=True, slots=True)
class Party:
    count: int
    mons: tuple[PartyMon, ...]

    @property
    def lead(self) -> PartyMon | None:
        return self.mons[0] if self.mons else None

    @property
    def all_fainted(self) -> bool:
        # Empty party is vacuously not a blackout condition — the engine
        # never permits party_count=0 mid-run.
        return bool(self.mons) and all(m.fainted for m in self.mons)


def parse_party(memory: MemoryLike, symbols: SymbolTable) -> Party:
    count = symbols.read_u8(memory, "wPartyCount")
    # Defensive clamp: engine invariant, but we never want to scan into
    # the name table past slot 6 if a save is corrupt or uninitialised.
    count = min(count, MAX_PARTY_SLOTS)

    if count == 0:
        return Party(count=0, mons=())

    base = symbols.addr_of("wPartyMons")
    mons = tuple(
        _parse_party_slot(memory, slot, base + slot * PARTY_STRUCT_SIZE)
        for slot in range(count)
    )
    return Party(count=count, mons=mons)


def _parse_party_slot(memory: MemoryLike, slot: int, base: int) -> PartyMon:
    species = _read_u8(memory, base + _OFFSET_SPECIES)
    hp = _read_u16_be(memory, base + _OFFSET_HP)
    status = parse_status(_read_u8(memory, base + _OFFSET_STATUS))
    type1 = _read_u8(memory, base + _OFFSET_TYPE1)
    type2 = _read_u8(memory, base + _OFFSET_TYPE2)
    moves = _read_tuple4(memory, base + _OFFSET_MOVES)
    pp = _read_tuple4(memory, base + _OFFSET_PP)
    level = _read_u8(memory, base + _OFFSET_LEVEL)
    max_hp = _read_u16_be(memory, base + _OFFSET_MAX_HP)
    return PartyMon(
        slot=slot,
        species=species,
        level=level,
        hp=hp,
        max_hp=max_hp,
        status=status,
        type1=type1,
        type2=type2,
        moves=moves,
        pp=pp,
    )


def _read_u8(memory: MemoryLike, addr: int) -> int:
    return int(memory[addr]) & 0xFF


def _read_u16_be(memory: MemoryLike, addr: int) -> int:
    hi = _read_u8(memory, addr)
    lo = _read_u8(memory, addr + 1)
    return (hi << 8) | lo


def _read_tuple4(memory: MemoryLike, addr: int) -> tuple[int, int, int, int]:
    return (
        _read_u8(memory, addr),
        _read_u8(memory, addr + 1),
        _read_u8(memory, addr + 2),
        _read_u8(memory, addr + 3),
    )
