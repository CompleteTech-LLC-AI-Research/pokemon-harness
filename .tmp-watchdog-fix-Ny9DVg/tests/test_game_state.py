from __future__ import annotations

from pokered_harness.state import parse_game_state
from pokered_harness.state.base import Direction
from pokered_harness.state.battle import BattleKind


def test_game_state_composes_all_subparsers(mem, symbols):
    mem[0xD35E] = 0x0B  # wCurMap
    mem[0xD362] = 17
    mem[0xD361] = 20
    mem[0xD46A] = 0
    mem[0xC109] = 0x04  # UP
    mem[0xCC26] = 1
    mem[0xCC28] = 3
    mem.write_word_le(0xCC3A, 0x9800)

    gs = parse_game_state(mem, symbols)

    assert gs.overworld.map_id == 0x0B
    assert (gs.overworld.x, gs.overworld.y) == (17, 20)
    assert gs.overworld.direction is Direction.UP
    assert gs.menu.current_item == 1
    assert gs.text.dest_in_vram_tilemap is True
    assert gs.battle.kind is BattleKind.NONE
    assert gs.party.count == 0


def test_game_state_during_trainer_battle_with_party(mem, symbols):
    # Mid-battle snapshot: in trainer battle, party slot 0 taking a hit.
    mem[0xD057] = 2  # trainer battle
    mem[0xD058] = 5  # trainer class
    mem[0xD163] = 1  # party count
    base = 0xD16B
    mem[base + 0] = 0x19  # Pikachu-ish species
    mem[base + 1] = 0x00  # HP hi
    mem[base + 2] = 0x08  # HP lo → 8
    mem[base + 33] = 12  # level
    mem[base + 34] = 0x00  # max HP hi
    mem[base + 35] = 0x1A  # max HP lo → 26

    gs = parse_game_state(mem, symbols)

    assert gs.battle.active is True
    assert gs.battle.kind is BattleKind.TRAINER
    assert gs.battle.engaged_trainer_class == 5
    assert gs.party.count == 1
    lead = gs.party.lead
    assert lead is not None
    assert lead.hp == 8
    assert lead.max_hp == 26
    assert 0.3 < lead.hp_fraction < 0.31
