from __future__ import annotations

import pytest

from pokered_harness.state.battle import BattleKind, BattleType, parse_battle
from pokered_harness.symbols.loader import load_sym_text


def _set_symbol(mem, symbols, name: str, value: int) -> None:
    """Write a byte through the address named by the supplied symbol table."""

    mem[symbols.addr_of(name)] = value


def test_battle_inactive_by_default(mem, symbols):
    state = parse_battle(mem, symbols)
    assert state.active is False
    assert state.kind is BattleKind.NONE
    assert state.is_trainer_battle is False
    assert state.is_wild_battle is False


def test_battle_wild_encounter(mem, symbols):
    _set_symbol(mem, symbols, "wIsInBattle", 1)
    _set_symbol(mem, symbols, "wBattleType", 0)  # BATTLE_TYPE_NORMAL
    state = parse_battle(mem, symbols)
    assert state.active is True
    assert state.kind is BattleKind.WILD
    assert state.battle_type is BattleType.NORMAL
    assert state.is_wild_battle is True
    assert state.is_trainer_battle is False


def test_battle_trainer_with_class_and_set(mem, symbols):
    _set_symbol(mem, symbols, "wIsInBattle", 2)
    _set_symbol(mem, symbols, "wEngagedTrainerClass", 0x0A)
    _set_symbol(mem, symbols, "wEngagedTrainerSet", 3)
    state = parse_battle(mem, symbols)
    assert state.kind is BattleKind.TRAINER
    assert state.is_trainer_battle is True
    assert state.engaged_trainer_class == 0x0A
    assert state.engaged_trainer_set == 3


def test_battle_type_safari(mem, symbols):
    _set_symbol(mem, symbols, "wIsInBattle", 1)
    _set_symbol(mem, symbols, "wBattleType", 2)  # BATTLE_TYPE_SAFARI
    state = parse_battle(mem, symbols)
    assert state.battle_type is BattleType.SAFARI


def test_battle_type_old_man(mem, symbols):
    _set_symbol(mem, symbols, "wIsInBattle", 1)
    _set_symbol(mem, symbols, "wBattleType", 1)  # BATTLE_TYPE_OLD_MAN
    state = parse_battle(mem, symbols)
    assert state.battle_type is BattleType.OLD_MAN


def test_battle_turn_already_consumed(mem, symbols):
    _set_symbol(mem, symbols, "wIsInBattle", 1)
    _set_symbol(mem, symbols, "wActionResultOrTookBattleTurn", 1)
    state = parse_battle(mem, symbols)
    assert state.action_result_or_took_turn == 1
    assert state.turn_already_consumed is True


def test_battle_move_menu_fields(mem, symbols):
    _set_symbol(mem, symbols, "wIsInBattle", 1)
    _set_symbol(mem, symbols, "wMoveMenuType", 0)
    _set_symbol(mem, symbols, "wPlayerSelectedMove", 0x21)  # opaque move id
    _set_symbol(mem, symbols, "wEnemySelectedMove", 0x5E)  # opaque move id
    _set_symbol(mem, symbols, "wPlayerMonNumber", 2)
    state = parse_battle(mem, symbols)
    assert state.move_menu_type == 0
    assert state.player_selected_move == 0x21
    assert state.enemy_selected_move == 0x5E
    assert state.player_mon_slot == 2


def test_battle_reads_named_symbols_not_guessed_addresses(mem):
    # Move every battle field away from conventional Red/Blue/Yellow RAM
    # addresses. A parser that falls back to guessed addresses must not pass.
    symbols = load_sym_text(
        """
        00:C200 wIsInBattle
        00:C201 wBattleType
        00:C202 wEngagedTrainerClass
        00:C203 wEngagedTrainerSet
        00:C204 wPlayerMonNumber
        00:C205 wMoveMenuType
        00:C206 wPlayerSelectedMove
        00:C207 wEnemySelectedMove
        00:C208 wActionResultOrTookBattleTurn
        """
    )
    _set_symbol(mem, symbols, "wIsInBattle", 2)
    _set_symbol(mem, symbols, "wBattleType", 0)
    _set_symbol(mem, symbols, "wEngagedTrainerClass", 0xA5)
    _set_symbol(mem, symbols, "wEngagedTrainerSet", 0x5A)
    _set_symbol(mem, symbols, "wPlayerMonNumber", 4)
    _set_symbol(mem, symbols, "wMoveMenuType", 0xFE)
    _set_symbol(mem, symbols, "wPlayerSelectedMove", 0x21)
    _set_symbol(mem, symbols, "wEnemySelectedMove", 0x5E)
    _set_symbol(mem, symbols, "wActionResultOrTookBattleTurn", 1)

    state = parse_battle(mem, symbols)

    assert state.raw_is_in_battle == 2
    assert state.active is True
    assert state.kind is BattleKind.TRAINER
    assert state.battle_type is BattleType.NORMAL
    assert state.engaged_trainer_class == 0xA5
    assert state.engaged_trainer_set == 0x5A
    assert state.player_mon_slot == 4
    assert state.move_menu_type == 0xFE
    assert state.player_selected_move == 0x21
    assert state.enemy_selected_move == 0x5E
    assert state.action_result_or_took_turn == 1
    assert state.turn_already_consumed is True


def test_battle_zero_activity_flag_wins_over_stale_invalid_type(mem, symbols):
    _set_symbol(mem, symbols, "wIsInBattle", 0)
    _set_symbol(mem, symbols, "wBattleType", 0xFF)

    state = parse_battle(mem, symbols)

    assert state.raw_is_in_battle == 0
    assert state.active is False
    assert state.kind is BattleKind.NONE
    assert state.is_wild_battle is False
    assert state.is_trainer_battle is False
    assert state.battle_type is None


def test_battle_unknown_kind_value_returns_none(mem, symbols):
    _set_symbol(mem, symbols, "wIsInBattle", 0xFF)  # outside known kind values
    state = parse_battle(mem, symbols)
    assert state.raw_is_in_battle == 0xFF
    assert state.kind is None
    assert state.active is True  # raw activity is known; kind remains unknown
    assert state.is_wild_battle is False
    assert state.is_trainer_battle is False


def test_battle_unknown_type_value_returns_none(mem, symbols):
    _set_symbol(mem, symbols, "wIsInBattle", 1)
    _set_symbol(mem, symbols, "wBattleType", 0xFF)
    state = parse_battle(mem, symbols)
    assert state.active is True
    assert state.kind is BattleKind.WILD
    assert state.battle_type is None


def test_battle_missing_optional_symbols_stay_unknown_not_zero(mem, symbols):
    # Give the canonical optional addresses non-zero values as decoys. Only
    # the activity symbol is supplied to the parser, so no optional field may
    # be inferred from an address or a default zero.
    for name in (
        "wBattleType",
        "wEngagedTrainerClass",
        "wEngagedTrainerSet",
        "wPlayerMonNumber",
        "wMoveMenuType",
        "wPlayerSelectedMove",
        "wEnemySelectedMove",
        "wActionResultOrTookBattleTurn",
    ):
        _set_symbol(mem, symbols, name, 0xA5)

    sym = load_sym_text("00:C300 wIsInBattle\n")
    _set_symbol(mem, sym, "wIsInBattle", 1)
    state = parse_battle(mem, sym)

    assert state.active is True
    assert state.kind is BattleKind.WILD
    assert state.battle_type is None
    assert state.engaged_trainer_class is None
    assert state.engaged_trainer_set is None
    assert state.player_mon_slot is None
    assert state.move_menu_type is None
    assert state.player_selected_move is None
    assert state.enemy_selected_move is None
    assert state.action_result_or_took_turn is None
    assert state.turn_already_consumed is False


def test_battle_missing_activity_symbol_fails_closed(mem):
    symbols = load_sym_text("00:D05A wBattleType\n")

    with pytest.raises(KeyError, match="wIsInBattle"):
        parse_battle(mem, symbols)
