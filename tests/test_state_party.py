from __future__ import annotations

import pytest

from pokered_harness.state.party import (
    PARTY_STRUCT_SIZE,
    parse_party,
)
from pokered_harness.symbols.loader import load_sym_text


def _write_slot(
    mem,
    base: int,
    *,
    species: int,
    hp: int,
    max_hp: int,
    status: int = 0,
    type1: int = 0,
    type2: int = 0,
    level: int = 1,
    moves=(0, 0, 0, 0),
    pp=(0, 0, 0, 0),
) -> None:
    mem[base + 0] = species
    mem[base + 1] = (hp >> 8) & 0xFF  # HP big-endian
    mem[base + 2] = hp & 0xFF
    mem[base + 4] = status
    mem[base + 5] = type1
    mem[base + 6] = type2
    for i, m in enumerate(moves):
        mem[base + 8 + i] = m
    for i, p in enumerate(pp):
        mem[base + 29 + i] = p
    mem[base + 33] = level
    mem[base + 34] = (max_hp >> 8) & 0xFF
    mem[base + 35] = max_hp & 0xFF


def test_empty_party(mem, symbols):
    mem[symbols.addr_of("wPartyCount")] = 0
    # Stale/uninitialised slot bytes, including a sentinel-like byte, must not
    # turn a zero-count party into a guessed party member.
    mem[symbols.addr_of("wPartyMons")] = 0xFF
    party = parse_party(mem, symbols)
    assert party.count == 0
    assert party.mons == ()
    assert party.lead is None
    assert party.all_fainted is False


def test_single_pokemon_at_full_hp(mem, symbols):
    mem[symbols.addr_of("wPartyCount")] = 1
    base = symbols.addr_of("wPartyMons")
    _write_slot(
        mem,
        base,
        species=0x99,
        hp=25,
        max_hp=25,
        level=7,
        moves=(0x21, 0x0A, 0, 0),
        pp=(20, 25, 0, 0),
    )
    party = parse_party(mem, symbols)
    assert party.count == 1
    lead = party.lead
    assert lead is not None
    assert lead.slot == 0
    assert lead.species == 0x99
    assert lead.level == 7
    assert lead.hp == 25
    assert lead.max_hp == 25
    assert lead.moves == (0x21, 0x0A, 0, 0)
    assert lead.pp == (20, 25, 0, 0)
    assert lead.fainted is False
    assert lead.hp_fraction == 1.0


def test_multiple_slots_parsed_independently(mem, symbols):
    mem[symbols.addr_of("wPartyCount")] = 3
    base = symbols.addr_of("wPartyMons")
    _write_slot(mem, base, species=1, hp=10, max_hp=20, level=5)
    _write_slot(
        mem,
        base + PARTY_STRUCT_SIZE,
        species=2,
        hp=0,
        max_hp=30,
        level=10,
    )
    _write_slot(
        mem,
        base + 2 * PARTY_STRUCT_SIZE,
        species=3,
        hp=50,
        max_hp=50,
        level=20,
    )
    party = parse_party(mem, symbols)
    assert party.count == 3
    assert [m.species for m in party.mons] == [1, 2, 3]
    assert party.mons[0].hp_fraction == 0.5
    assert party.mons[1].fainted is True
    assert party.mons[2].level == 20


def test_all_fainted_detection(mem, symbols):
    mem[symbols.addr_of("wPartyCount")] = 2
    base = symbols.addr_of("wPartyMons")
    _write_slot(mem, base, species=1, hp=0, max_hp=20)
    _write_slot(mem, base + PARTY_STRUCT_SIZE, species=2, hp=0, max_hp=30)
    party = parse_party(mem, symbols)
    assert party.all_fainted is True


def test_status_decoded_per_slot(mem, symbols):
    mem[symbols.addr_of("wPartyCount")] = 1
    base = symbols.addr_of("wPartyMons")
    _write_slot(
        mem,
        base,
        species=1,
        hp=10,
        max_hp=20,
        status=1 << 4,  # burned
    )
    party = parse_party(mem, symbols)
    assert party.mons[0].status.burned is True


def test_hp_is_big_endian_inside_struct(mem, symbols):
    # HP = 300 → 0x012C → hi=0x01, lo=0x2C
    mem[symbols.addr_of("wPartyCount")] = 1
    base = symbols.addr_of("wPartyMons")
    mem[base + 1] = 0x01
    mem[base + 2] = 0x2C
    party = parse_party(mem, symbols)
    assert party.mons[0].hp == 300


def test_party_count_clamped_to_max_slots(mem, symbols):
    # Corrupt save / uninitialised memory edge case.
    mem[symbols.addr_of("wPartyCount")] = 200
    party = parse_party(mem, symbols)
    assert party.count == 6
    assert len(party.mons) == 6


def test_party_reads_relocated_symbol_addresses_not_canonical_guesses(mem):
    sym = load_sym_text(
        """
        00:C001 wPartyCount
        00:C100 wPartyMons
        """
    )
    # Decoys at the familiar Red/Blue addresses catch a parser that ignores
    # the supplied symbols and reaches into a guessed layout.
    mem[0xD163] = 6
    mem[0xD16B] = 1
    mem[0xC001] = 1
    _write_slot(mem, 0xC100, species=0xFE, hp=12, max_hp=12, level=9)

    party = parse_party(mem, sym)

    assert party.count == 1
    assert len(party.mons) == 1
    assert party.mons[0].species == 0xFE
    assert party.mons[0].level == 9
    assert party.mons[0].hp == 12


def test_party_sentinel_bytes_are_exposed_without_guessing(mem, symbols):
    mem[symbols.addr_of("wPartyCount")] = 1
    base = symbols.addr_of("wPartyMons")
    _write_slot(
        mem,
        base,
        species=0xFF,
        hp=0,
        max_hp=0,
        status=0xFF,
        type1=0xFF,
        type2=0xFF,
        level=0,
        moves=(0xFF, 0xFF, 0xFF, 0xFF),
        pp=(0xFF, 0xFF, 0xFF, 0xFF),
    )

    mon = parse_party(mem, symbols).mons[0]

    assert mon.species == 0xFF
    assert mon.type1 == 0xFF
    assert mon.type2 == 0xFF
    assert mon.moves == (0xFF, 0xFF, 0xFF, 0xFF)
    assert mon.pp == (0xFF, 0xFF, 0xFF, 0xFF)
    assert mon.status.raw == 0xFF
    assert mon.level == 0
    assert mon.hp == 0
    assert mon.max_hp == 0


def test_party_parser_requires_core_symbols():
    sym = load_sym_text("00:C001 wPartyCount\n")  # no wPartyMons
    # Without wPartyMons we cannot address slots. Empty party still
    # parseable because count=0 never reads wPartyMons.
    from tests.conftest import DictMemory

    mem = DictMemory({0xC001: 0})
    party = parse_party(mem, sym)
    assert party.count == 0


def test_nonempty_party_requires_party_mons_symbol():
    sym = load_sym_text("00:C001 wPartyCount\n")
    from tests.conftest import DictMemory

    mem = DictMemory({0xC001: 1})

    with pytest.raises(KeyError, match="wPartyMons"):
        parse_party(mem, sym)
