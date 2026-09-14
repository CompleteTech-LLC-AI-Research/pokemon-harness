"""Party state parser.

Reads the 6-slot party block as 44-byte ``party_struct`` records. The
struct layout is fixed by pokered's ``macros/ram.asm`` — we trust it
because it is the game engine's binary ABI, not a guessed address layout.

``wPartyCount`` and the ``wPartySpecies`` list are separate ROM-owned
invariants.  The count remains clamped for backwards compatibility, but the
raw count and terminator check are exposed so a caller cannot mistake a
corrupt snapshot for a valid six-mon party.  The species list is optional for
older/minimal symbol fixtures; when it is absent the result is explicitly
unknown rather than silently treated as a validated party.

During a battle, ``wPartyMons`` is not the active battle struct.  If the
matching battle symbols are available, the active ``wBattleMon`` snapshot is
parsed separately using those symbols and layout fields.  It is never
fabricated from the party record or by applying party offsets to a battle
record.

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

_PARTY_SPECIES_SENTINEL = 0xFF
_NO_MON = 0x00
_BATTLE_ACTIVE_VALUES = frozenset((0x01, 0x02))

# Field suffixes shared by the ``wBattleMon`` and ``wEnemyMon`` battle-struct
# symbol families.  The battle record is a distinct binary ABI from
# ``party_struct`` (for example PP at offset 25 versus 29 and MaxHP at offset
# 15 versus 34), so combatants are always read through these named symbols and
# never by applying party offsets to a battle record.
_BATTLE_STRUCT_FIELD_SUFFIXES = (
    "Species",
    "HP",
    "Status",
    "Type1",
    "Type2",
    "Moves",
    "PP",
    "Level",
    "MaxHP",
)


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
    # The list entry is the engine's party-order/presence record.  It is
    # optional for compatibility with reduced symbol fixtures.
    party_species: int | None = None
    species_valid: bool | None = None
    hp_valid: bool | None = None
    status_valid: bool | None = None
    valid: bool | None = None

    @property
    def fainted(self) -> bool | None:
        """Whether the observed HP proves this mon is fainted.

        A zeroed/uninitialised record has ``max_hp == 0`` and must not be
        reported as a fainted Pokémon.  ``None`` is the explicit unknown
        result for that case or for an impossible ``hp > max_hp`` pair.
        """
        if self.species_valid is False or self.hp_valid is not True:
            return None
        return self.hp == 0

    @property
    def hp_fraction(self) -> float | None:
        """Return the validated HP fraction when possible.

        The historical ``0.0`` result for ``max_hp == 0`` is retained for
        compatibility, but ``hp_valid`` is false in that case and callers
        must not treat the fraction as evidence of zero HP.  Impossible
        positive-over-maximum values return ``None`` rather than a guessed
        ratio.
        """
        if self.max_hp == 0:
            return 0.0
        if self.hp_valid is not True:
            return None
        return self.hp / self.max_hp

    @property
    def is_valid(self) -> bool | None:
        """Whether all fields required for a trustworthy party mon agree."""
        return self.valid

    @property
    def is_unknown(self) -> bool:
        return self.valid is None


@dataclass(frozen=True, slots=True)
class Party:
    count: int
    mons: tuple[PartyMon, ...]
    count_raw: int | None = None
    count_valid: bool | None = None
    species_terminator: int | None = None
    sentinel_valid: bool | None = None
    valid: bool | None = None
    active_battle_raw: int | None = None
    active_slot: int | None = None
    active_mon: PartyMon | None = None
    active_mon_valid: bool | None = None

    @property
    def raw_count(self) -> int | None:
        """Backward/forward-friendly alias for the un-clamped count byte."""
        return self.count_raw

    @property
    def sentinel(self) -> int | None:
        """Raw byte following the count entries in ``wPartySpecies``."""
        return self.species_terminator

    @property
    def battle_active(self) -> bool | None:
        """Whether the symbol-backed battle flag proves an active battle."""
        if self.active_battle_raw is None:
            return None
        if (
            self.active_battle_raw == 0
            or self.active_battle_raw == _PARTY_SPECIES_SENTINEL
        ):
            return False
        if self.active_battle_raw in _BATTLE_ACTIVE_VALUES:
            return True
        return None

    @property
    def is_valid(self) -> bool | None:
        return self.valid

    @property
    def is_unknown(self) -> bool:
        return self.valid is None

    @property
    def lead(self) -> PartyMon | None:
        return self.mons[0] if self.mons else None

    @property
    def all_fainted(self) -> bool | None:
        # Empty party is not a blackout condition.  A known-bad count or
        # terminator, or an invalid HP/species record, cannot establish a
        # blackout and therefore returns unknown.
        if not self.mons:
            return False
        if self.count_valid is False or self.sentinel_valid is False:
            return None
        fainted = tuple(mon.fainted for mon in self.mons)
        if any(value is None for value in fainted):
            return None
        return all(fainted)


def parse_party(memory: MemoryLike, symbols: SymbolTable) -> Party:
    count_raw = symbols.read_u8(memory, "wPartyCount")
    # Defensive clamp: engine invariant, but we never want to scan into
    # the name table past slot 6 if a save is corrupt or uninitialised.
    count = min(count_raw, MAX_PARTY_SLOTS)

    species_list: tuple[int, ...] | None = None
    species_terminator: int | None = None
    sentinel_valid: bool | None = None
    if "wPartySpecies" in symbols:
        species_base = symbols.addr_of("wPartySpecies")
        species_list = tuple(
            _read_u8(memory, species_base + slot)
            for slot in range(MAX_PARTY_SLOTS + 1)
        )
        if count_raw <= MAX_PARTY_SLOTS:
            species_terminator = species_list[count_raw]
            sentinel_valid = species_terminator == _PARTY_SPECIES_SENTINEL
        else:
            # There is no in-bounds terminator for a count outside the
            # engine's PARTY_LENGTH invariant.
            sentinel_valid = False

    mons: tuple[PartyMon, ...] = ()
    if count:
        base = symbols.addr_of("wPartyMons")
        mons = tuple(
            _parse_party_slot(
                memory,
                slot,
                base + slot * PARTY_STRUCT_SIZE,
                party_species=(species_list[slot] if species_list is not None else None),
            )
            for slot in range(count)
        )

    active_battle_raw, active_slot, active_mon, active_mon_valid = _parse_active_battle(
        memory, symbols, mons
    )
    count_valid = count_raw <= MAX_PARTY_SLOTS
    valid = _combine_validity(
        count_valid,
        sentinel_valid,
        *(mon.is_valid for mon in mons),
    )
    return Party(
        count=count,
        mons=mons,
        count_raw=count_raw,
        count_valid=count_valid,
        species_terminator=species_terminator,
        sentinel_valid=sentinel_valid,
        valid=valid,
        active_battle_raw=active_battle_raw,
        active_slot=active_slot,
        active_mon=active_mon,
        active_mon_valid=active_mon_valid,
    )


def _parse_party_slot(
    memory: MemoryLike,
    slot: int,
    base: int,
    *,
    party_species: int | None = None,
) -> PartyMon:
    species = _read_u8(memory, base + _OFFSET_SPECIES)
    hp = _read_u16_be(memory, base + _OFFSET_HP)
    status = parse_status(_read_u8(memory, base + _OFFSET_STATUS))
    type1 = _read_u8(memory, base + _OFFSET_TYPE1)
    type2 = _read_u8(memory, base + _OFFSET_TYPE2)
    moves = _read_tuple4(memory, base + _OFFSET_MOVES)
    pp = _read_tuple4(memory, base + _OFFSET_PP)
    level = _read_u8(memory, base + _OFFSET_LEVEL)
    max_hp = _read_u16_be(memory, base + _OFFSET_MAX_HP)
    return _make_party_mon(
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
        party_species=party_species,
    )


def _make_party_mon(
    *,
    slot: int,
    species: int,
    level: int,
    hp: int,
    max_hp: int,
    status: StatusCondition,
    type1: int,
    type2: int,
    moves: tuple[int, int, int, int],
    pp: tuple[int, int, int, int],
    party_species: int | None = None,
    standalone_species: bool = False,
) -> PartyMon:
    species_valid = (
        _record_species_valid(species)
        if standalone_species
        else _species_valid(species, party_species)
    )
    hp_valid = max_hp > 0 and 0 <= hp <= max_hp
    status_valid = bool(getattr(status, "is_valid", (status.raw & 0x80) == 0))
    valid = _combine_validity(species_valid, hp_valid, status_valid)
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
        party_species=party_species,
        species_valid=species_valid,
        hp_valid=hp_valid,
        status_valid=status_valid,
        valid=valid,
    )


def parse_battle_combatant(
    memory: MemoryLike,
    symbols: SymbolTable,
    prefix: str,
    *,
    slot: int,
) -> PartyMon | None:
    """Parse one combatant from a named battle-struct symbol family.

    ``prefix`` is the ROM-label prefix (``wBattleMon`` for the player and
    ``wEnemyMon`` for the opposing side).  Every field is read through its
    named symbol, never by applying ``party_struct`` offsets, because the
    battle record uses a different layout (PP at offset 25 versus 29 and
    MaxHP at offset 15 versus 34).  When any required symbol is absent the
    result is ``None`` so callers can distinguish unknown data from a real
    zero value.
    """
    if any(prefix + suffix not in symbols for suffix in _BATTLE_STRUCT_FIELD_SUFFIXES):
        return None
    return _make_party_mon(
        slot=slot,
        species=symbols.read_u8(memory, prefix + "Species"),
        level=symbols.read_u8(memory, prefix + "Level"),
        hp=symbols.read_u16_be(memory, prefix + "HP"),
        max_hp=symbols.read_u16_be(memory, prefix + "MaxHP"),
        status=parse_status(symbols.read_u8(memory, prefix + "Status")),
        type1=symbols.read_u8(memory, prefix + "Type1"),
        type2=symbols.read_u8(memory, prefix + "Type2"),
        moves=_read_tuple4(memory, symbols.addr_of(prefix + "Moves")),
        pp=_read_tuple4(memory, symbols.addr_of(prefix + "PP")),
        standalone_species=True,
    )


def _parse_active_battle(
    memory: MemoryLike,
    symbols: SymbolTable,
    mons: tuple[PartyMon, ...],
) -> tuple[int | None, int | None, PartyMon | None, bool | None]:
    """Read the current ``wBattleMon`` only when its symbols prove it exists.

    The battle struct has a different layout and length from ``party_struct``.
    Missing symbols leave the active snapshot unavailable; no party-field
    fallback is used.
    """
    if "wIsInBattle" not in symbols:
        return None, None, None, None
    raw = symbols.read_u8(memory, "wIsInBattle")
    if raw not in _BATTLE_ACTIVE_VALUES:
        return raw, None, None, None
    if "wPlayerMonNumber" not in symbols:
        return raw, None, None, False

    slot = symbols.read_u8(memory, "wPlayerMonNumber")
    if slot >= len(mons):
        return raw, slot, None, False
    active_mon = parse_battle_combatant(memory, symbols, "wBattleMon", slot=slot)
    if active_mon is None:
        return raw, slot, None, False
    return raw, slot, active_mon, active_mon.is_valid is True


def _species_valid(species: int, party_species: int | None) -> bool | None:
    # 0 is NO_MON and FF is the list terminator in all three Gen-1 targets.
    # Without wPartySpecies we can establish only that the record is not an
    # empty/sentinel record; the list-order cross-check remains unknown.
    if species in (_NO_MON, _PARTY_SPECIES_SENTINEL):
        return False
    if party_species is None:
        return None
    return (
        party_species not in (_NO_MON, _PARTY_SPECIES_SENTINEL)
        and species == party_species
    )


def _record_species_valid(species: int) -> bool:
    return species not in (_NO_MON, _PARTY_SPECIES_SENTINEL)


def _combine_validity(*values: bool | None) -> bool | None:
    if any(value is False for value in values):
        return False
    if all(value is True for value in values):
        return True
    return None


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
