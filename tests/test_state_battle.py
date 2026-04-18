from __future__ import annotations

from pokered_harness.state.battle import BattleKind, BattleType, parse_battle
from pokered_harness.symbols.loader import load_sym_text


def test_battle_inactive_by_default(mem, symbols):
    state = parse_battle(mem, symbols)
    assert state.active is False
    assert state.kind is BattleKind.NONE
    assert state.is_trainer_battle is False
    assert state.is_wild_battle is False


def test_battle_wild_encounter(mem, symbols):
    mem[0xD057] = 1  # wIsInBattle
    mem[0xD05A] = 0  # wBattleType = NORMAL
    state = parse_battle(mem, symbols)
    assert state.active is True
    assert state.kind is BattleKind.WILD
    assert state.battle_type is BattleType.NORMAL
    assert state.is_wild_battle is True
    assert state.is_trainer_battle is False


def test_battle_trainer_with_class_and_set(mem, symbols):
    mem[0xD057] = 2
    mem[0xD058] = 0x0A  # some trainer class id
    mem[0xD059] = 3  # trainer set within class
    state = parse_battle(mem, symbols)
    assert state.kind is BattleKind.TRAINER
    assert state.is_trainer_battle is True
    assert state.engaged_trainer_class == 0x0A
    assert state.engaged_trainer_set == 3


def test_battle_type_safari(mem, symbols):
    mem[0xD057] = 1
    mem[0xD05A] = 2
    state = parse_battle(mem, symbols)
    assert state.battle_type is BattleType.SAFARI


def test_battle_type_old_man(mem, symbols):
    mem[0xD057] = 1
    mem[0xD05A] = 1
    state = parse_battle(mem, symbols)
    assert state.battle_type is BattleType.OLD_MAN


def test_battle_turn_already_consumed(mem, symbols):
    mem[0xD057] = 1
    mem[0xD05E] = 1  # wActionResultOrTookBattleTurn
    state = parse_battle(mem, symbols)
    assert state.turn_already_consumed is True


def test_battle_move_menu_fields(mem, symbols):
    mem[0xD057] = 1
    mem[0xCC2F] = 0  # move menu active
    mem[0xCCDC] = 0x21  # wPlayerSelectedMove = THUNDERBOLT (ish)
    mem[0xCCDD] = 0x5E  # enemy selected
    mem[0xCCD5] = 2  # third party slot active
    state = parse_battle(mem, symbols)
    assert state.move_menu_type == 0
    assert state.player_selected_move == 0x21
    assert state.enemy_selected_move == 0x5E
    assert state.player_mon_slot == 2


def test_battle_unknown_kind_value_returns_none(mem, symbols):
    mem[0xD057] = 99  # not NONE/WILD/TRAINER
    state = parse_battle(mem, symbols)
    assert state.raw_is_in_battle == 99
    assert state.kind is None
    assert state.active is True  # still "in a battle" from the raw flag


def test_battle_unknown_type_value_returns_none(mem, symbols):
    mem[0xD057] = 1
    mem[0xD05A] = 99
    state = parse_battle(mem, symbols)
    assert state.battle_type is None


def test_battle_optional_symbols_absent(mem):
    sym = load_sym_text("00:D057 wIsInBattle\n")
    state = parse_battle(mem, sym)
    assert state.battle_type is None
    assert state.engaged_trainer_class is None
    assert state.engaged_trainer_set is None
    assert state.player_selected_move is None
