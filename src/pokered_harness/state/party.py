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

import hashlib
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

_ACTIVE_BATTLE_SYMBOLS = (
    "wBattleMonSpecies",
    "wBattleMonHP",
    "wBattleMonStatus",
    "wBattleMonType1",
    "wBattleMonType2",
    "wBattleMonMoves",
    "wBattleMonPP",
    "wBattleMonLevel",
    "wBattleMonMaxHP",
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
    if any(name not in symbols for name in _ACTIVE_BATTLE_SYMBOLS):
        return raw, slot, None, False

    species = symbols.read_u8(memory, "wBattleMonSpecies")
    hp = symbols.read_u16_be(memory, "wBattleMonHP")
    status = parse_status(symbols.read_u8(memory, "wBattleMonStatus"))
    type1 = symbols.read_u8(memory, "wBattleMonType1")
    type2 = symbols.read_u8(memory, "wBattleMonType2")
    moves = _read_tuple4(memory, symbols.addr_of("wBattleMonMoves"))
    pp = _read_tuple4(memory, symbols.addr_of("wBattleMonPP"))
    level = symbols.read_u8(memory, "wBattleMonLevel")
    max_hp = symbols.read_u16_be(memory, "wBattleMonMaxHP")
    active_mon = _make_party_mon(
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
        standalone_species=True,
    )
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


# --- read-only raw party-record digest / exchange audit ---------------------
#
# The public :class:`PartyMon` DTO decodes only the fields ordinary observation
# needs, so it cannot prove a byte-exact cross-owner trade: two same-species
# members can share every decoded field while holding different raw records.
# The helpers below expose the complete source-defined 44-byte ``party_struct``
# identity as a stable SHA-256 digest and audit an intended-slot exchange.
# They are pure readers: they never tick, write RAM, touch serial state, or
# call a gameplay driver, and they never fabricate a value from a species
# match.  Missing symbols stay explicitly unknown; an out-of-range slot or a
# record that is not 44 bytes stays an explicit failure.

_DIGEST_ALGORITHM = "sha256"
_RECORD_SPECIES_OFFSET = _OFFSET_SPECIES
_RECORD_LEVEL_OFFSET = _OFFSET_LEVEL


@dataclass(frozen=True, slots=True)
class PartyRecord:
    """One occupied ``wPartyMons`` slot as raw bytes plus a stable digest."""

    slot: int
    raw: bytes
    digest: str
    species: int
    level: int

    @property
    def size(self) -> int:
        return len(self.raw)

    def to_resource_dict(self) -> dict[str, object]:
        """Bounded, sanitized projection; deliberately omits ``raw`` bytes."""
        return {
            "slot": self.slot,
            "digest": self.digest,
            "record_size": len(self.raw),
            "species": self.species,
            "level": self.level,
        }


@dataclass(frozen=True, slots=True)
class PartyRecords:
    """Read-only per-slot record view with explicit validity provenance.

    ``valid is None`` means the backing symbols/provenance are unavailable and
    the records must not be treated as an observed party.  ``valid is False``
    means the raw count is outside the engine's party-length invariant.  Raw
    bytes are retained for the pure audit helper but are never serialized by
    :meth:`to_resource_payload`.
    """

    records: tuple[PartyRecord, ...] = ()
    count: int | None = None
    count_raw: int | None = None
    count_valid: bool | None = None
    valid: bool | None = None
    missing_symbols: tuple[str, ...] = ()

    @property
    def is_unknown(self) -> bool:
        return self.valid is None

    def record_at(self, slot: int) -> PartyRecord | None:
        if not isinstance(slot, int) or isinstance(slot, bool):
            return None
        if slot < 0 or slot >= len(self.records):
            return None
        return self.records[slot]

    def digests(self) -> tuple[str, ...]:
        return tuple(record.digest for record in self.records)

    def to_resource_payload(self, *, source: str = "party-records") -> dict[str, object]:
        """JSON-ready, sanitized shape for the read-only MCP resource.

        ``source`` labels the owning observation (primary vs peer) so two
        owners' payloads stay distinguishable; it never carries raw bytes.
        """
        return {
            "source": source,
            "digest_algorithm": _DIGEST_ALGORITHM,
            "record_size": PARTY_STRUCT_SIZE,
            "count": self.count,
            "count_raw": self.count_raw,
            "count_valid": self.count_valid,
            "valid": self.valid,
            "missing_symbols": list(self.missing_symbols),
            "records": [record.to_resource_dict() for record in self.records],
        }


def parse_party_records(memory: MemoryLike, symbols: SymbolTable) -> PartyRecords:
    """Read every occupied party slot's raw 44-byte record and digest it.

    Missing ``wPartyCount`` or (for a non-empty party) ``wPartyMons`` returns
    an explicitly unknown result rather than a guessed empty party.  The read
    is bounded to :data:`MAX_PARTY_SLOTS` records and never mutates ``memory``.
    """
    if "wPartyCount" not in symbols:
        return PartyRecords(valid=None, missing_symbols=("wPartyCount",))
    count_raw = symbols.read_u8(memory, "wPartyCount")
    count = min(count_raw, MAX_PARTY_SLOTS)
    count_valid = count_raw <= MAX_PARTY_SLOTS
    if count == 0:
        return PartyRecords(
            records=(),
            count=0,
            count_raw=count_raw,
            count_valid=count_valid,
            valid=count_valid,
        )
    if "wPartyMons" not in symbols:
        return PartyRecords(
            records=(),
            count=count,
            count_raw=count_raw,
            count_valid=count_valid,
            valid=None,
            missing_symbols=("wPartyMons",),
        )
    base = symbols.addr_of("wPartyMons")
    records = tuple(
        _read_party_record(memory, slot, base + slot * PARTY_STRUCT_SIZE) for slot in range(count)
    )
    return PartyRecords(
        records=records,
        count=count,
        count_raw=count_raw,
        count_valid=count_valid,
        valid=count_valid,
    )


def _read_party_record(memory: MemoryLike, slot: int, base: int) -> PartyRecord:
    raw = bytes(int(memory[base + offset]) & 0xFF for offset in range(PARTY_STRUCT_SIZE))
    return PartyRecord(
        slot=slot,
        raw=raw,
        digest=hashlib.sha256(raw).hexdigest(),
        species=raw[_RECORD_SPECIES_OFFSET],
        level=raw[_RECORD_LEVEL_OFFSET],
    )


@dataclass(frozen=True, slots=True)
class ExchangeCheck:
    """One named Boolean (or unknown) condition inside an exchange audit."""

    name: str
    status: bool | None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ExchangeAudit:
    """Explicit outcome of :func:`audit_exact_party_exchange`.

    ``status`` is ``"exact"`` (valid), ``"mismatch"`` (invalid), ``"unknown"``
    (provenance unavailable), or ``"out_of_range"`` (invalid slot/size).
    """

    valid: bool | None
    status: str
    slot_a: int
    slot_b: int
    checks: tuple[ExchangeCheck, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def is_exact(self) -> bool:
        return self.valid is True

    @property
    def is_unknown(self) -> bool:
        return self.valid is None

    def check(self, name: str) -> bool | None:
        for item in self.checks:
            if item.name == name:
                return item.status
        return None


def audit_exact_party_exchange(
    *,
    owner_a_before: PartyRecords,
    owner_a_after: PartyRecords,
    owner_b_before: PartyRecords,
    owner_b_after: PartyRecords,
    slot_a: int,
    slot_b: int,
) -> ExchangeAudit:
    """Audit a byte-exact trade of two owners' selected outgoing slots.

    Gen I removes each selected record, compacts the surviving records, then
    appends the received record. Receiving slots are therefore each owner's
    final occupied slot, independently of the selected outgoing indexes.
    ``valid is True`` only when the full 44-byte records arrive there and every
    survivor remains byte-identical in its source-defined order. Missing
    symbols return ``valid is None``; an out-of-range slot or a record that is
    not 44 bytes is an explicit ``valid is False``.  A same-species pair is
    still compared by raw record identity, never by species alone.
    """
    positions = (
        ("owner_a_before", owner_a_before, slot_a),
        ("owner_a_after", owner_a_after, slot_a),
        ("owner_b_before", owner_b_before, slot_b),
        ("owner_b_after", owner_b_after, slot_b),
    )
    for name, records, slot in positions:
        problem = _slot_problem(name, records, slot)
        if problem is not None:
            return ExchangeAudit(
                valid=False,
                status="out_of_range",
                slot_a=slot_a,
                slot_b=slot_b,
                reasons=(problem,),
            )
    for name, records, _slot in positions:
        if records.valid is None:
            missing = ", ".join(records.missing_symbols) or "symbols"
            return ExchangeAudit(
                valid=None,
                status="unknown",
                slot_a=slot_a,
                slot_b=slot_b,
                reasons=(f"{name}: party records unavailable ({missing})",),
            )
        if records.valid is False:
            return ExchangeAudit(
                valid=None,
                status="unknown",
                slot_a=slot_a,
                slot_b=slot_b,
                reasons=(f"{name}: party count outside the engine invariant",),
            )

    a_before_slot = owner_a_before.records[slot_a]
    a_after_slot = owner_a_after.records[-1]
    b_before_slot = owner_b_before.records[slot_b]
    b_after_slot = owner_b_after.records[-1]
    involved = tuple(record for _, party, _ in positions for record in party.records)

    size_ok = all(record.size == PARTY_STRUCT_SIZE for record in involved)
    intended_swap = a_after_slot.raw == b_before_slot.raw and b_after_slot.raw == a_before_slot.raw
    unrelated_unchanged = _other_records_unchanged(
        owner_a_before, owner_a_after, slot_a
    ) and _other_records_unchanged(owner_b_before, owner_b_after, slot_b)
    if a_before_slot.species == b_before_slot.species:
        distinguished: bool | None = a_before_slot.raw != b_before_slot.raw and intended_swap
    else:
        distinguished = True

    checks = (
        ExchangeCheck(
            "record_size_44",
            size_ok,
            "a party record is not the 44-byte party_struct size" if not size_ok else "",
        ),
        ExchangeCheck(
            "intended_slots_swapped",
            intended_swap,
            "the final slots did not receive the selected outgoing records"
            if not intended_swap
            else "",
        ),
        ExchangeCheck(
            "unrelated_records_unchanged",
            unrelated_unchanged,
            "surviving records changed or did not compact in order" if not unrelated_unchanged else "",
        ),
        ExchangeCheck(
            "same_species_distinguished",
            distinguished,
            "same-species members were not distinguished by raw record"
            if distinguished is False
            else "",
        ),
    )
    reasons = tuple(check.detail for check in checks if check.status is False)
    if all(check.status is True for check in checks):
        return ExchangeAudit(
            valid=True,
            status="exact",
            slot_a=slot_a,
            slot_b=slot_b,
            checks=checks,
        )
    return ExchangeAudit(
        valid=False,
        status="mismatch",
        slot_a=slot_a,
        slot_b=slot_b,
        checks=checks,
        reasons=reasons,
    )


def _slot_problem(name: str, records: PartyRecords, slot: int) -> str | None:
    if not isinstance(slot, int) or isinstance(slot, bool) or slot < 0:
        return f"{name}: slot {slot!r} is not a non-negative index"
    if slot >= MAX_PARTY_SLOTS:
        return f"{name}: slot {slot} exceeds the {MAX_PARTY_SLOTS}-slot party"
    # A bounded, validated count proves an index is out of range even when the
    # record bytes themselves are unavailable (for example a missing
    # ``wPartyMons`` symbol).  Only slots that could still be valid stay
    # unknown.
    if records.count is not None and records.count_valid is True and slot >= records.count:
        return f"{name}: slot {slot} outside party of {records.count} record(s)"
    if records.valid is True and slot >= len(records.records):
        return f"{name}: slot {slot} outside party of {len(records.records)} record(s)"
    return None


def _other_records_unchanged(
    before: PartyRecords,
    after: PartyRecords,
    intended_slot: int,
) -> bool:
    if len(before.records) != len(after.records):
        return False
    survivors = before.records[:intended_slot] + before.records[intended_slot + 1 :]
    return all(
        old.raw == new.raw
        for old, new in zip(survivors, after.records[:-1], strict=True)
    )
