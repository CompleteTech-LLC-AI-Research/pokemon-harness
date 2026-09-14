"""ROM-free tests for the additive battle enemy/phase observations.

These tests exercise the synthetic symbol-backed parsers, the validity
semantics, and the MCP resource/JSON exposure.  No emulator or ROM is
loaded: ``FakePyBoy`` and the sparse ``DictMemory`` from the unit suite are
used, exactly as in ``tests/test_mcp_server.py``.

Real-ROM qualification of the derived phase and enemy combatant remains a
separate, unclaimed gate.
"""

from __future__ import annotations

import base64
import json

import pytest

from pokered_harness.events import EventBus
from pokered_harness.mcp_server import (
    _resource_specs,
    dispatch_tool,
    read_resource,
)
from pokered_harness.session import Session
from pokered_harness.state.battle import (
    BattleKind,
    BattlePhase,
    parse_battle,
)
from pokered_harness.state.party import parse_battle_combatant
from pokered_harness.symbols.loader import SymbolTable, load_sym_text
from tests.conftest import DictMemory
from tests.fakes import FakePyBoy

# Enemy fields are placed at relocated, non-canonical addresses using the
# engine's real battle-struct offsets (PP=25, MaxHP=15).  The party-struct
# offsets (PP=29, MaxHP=34) are then filled with decoys below so a parser
# that applied party offsets would read the wrong values.
_ENEMY_BASE = 0xC100
_ENEMY_SPECIES = _ENEMY_BASE + 0
_ENEMY_HP = _ENEMY_BASE + 1
_ENEMY_PARTY_POS = _ENEMY_BASE + 3
_ENEMY_STATUS = _ENEMY_BASE + 4
_ENEMY_TYPE1 = _ENEMY_BASE + 5
_ENEMY_TYPE2 = _ENEMY_BASE + 6
_ENEMY_MOVES = _ENEMY_BASE + 8
_ENEMY_LEVEL = _ENEMY_BASE + 14
_ENEMY_MAX_HP = _ENEMY_BASE + 15
_ENEMY_PP = _ENEMY_BASE + 25

_FULL_SYM = """\
00:C000 wIsInBattle
00:C001 wBattleResult
00:C002 wInHandlePlayerMonFainted
00:C003 wMoveMenuType
00:C004 wPlayerMoveListIndex
00:C005 wActionResultOrTookBattleTurn
00:C007 wBattleType
00:C008 wEngagedTrainerClass
00:C009 wEngagedTrainerSet
00:C00A wPlayerMonNumber
00:C00B wPlayerSelectedMove
00:C00C wEnemySelectedMove
00:C100 wEnemyMonSpecies
00:C101 wEnemyMonHP
00:C103 wEnemyMonPartyPos
00:C104 wEnemyMonStatus
00:C105 wEnemyMonType1
00:C106 wEnemyMonType2
00:C108 wEnemyMonMoves
00:C10E wEnemyMonLevel
00:C10F wEnemyMonMaxHP
00:C119 wEnemyMonPP
"""

_TRANSIENT_SYMBOLS = {
    "wBattleResult",
    "wInHandlePlayerMonFainted",
    "wMoveMenuType",
    "wPlayerMoveListIndex",
    "wActionResultOrTookBattleTurn",
}


def _symbols() -> SymbolTable:
    return load_sym_text(_FULL_SYM)


def _write_enemy(
    mem,
    *,
    species: int = 0x99,
    hp: int = 32,
    max_hp: int = 40,
    level: int = 7,
    slot: int = 0,
    status: int = 0,
    moves: tuple[int, int, int, int] = (0x21, 0x0A, 0, 0),
    pp: tuple[int, int, int, int] = (20, 25, 0, 0),
) -> None:
    mem[_ENEMY_SPECIES] = species
    mem[_ENEMY_HP] = (hp >> 8) & 0xFF
    mem[_ENEMY_HP + 1] = hp & 0xFF
    mem[_ENEMY_PARTY_POS] = slot
    mem[_ENEMY_STATUS] = status
    mem[_ENEMY_TYPE1] = 0x16
    mem[_ENEMY_TYPE2] = 0x16
    for index, move in enumerate(moves):
        mem[_ENEMY_MOVES + index] = move
    mem[_ENEMY_LEVEL] = level
    mem[_ENEMY_MAX_HP] = (max_hp >> 8) & 0xFF
    mem[_ENEMY_MAX_HP + 1] = max_hp & 0xFF
    for index, value in enumerate(pp):
        mem[_ENEMY_PP + index] = value


# --- phase truth table ------------------------------------------------------


def test_phase_inactive_when_battle_flag_is_zero(mem, symbols):
    mem[symbols.addr_of("wIsInBattle")] = 0
    state = parse_battle(mem, symbols)
    assert state.phase is BattlePhase.INACTIVE
    assert state.phase_valid is True
    assert state.phase_evidence == ("wIsInBattle",)


def test_phase_all_clear_evidence_is_unknown_not_intro():
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 2
    state = parse_battle(mem, sym)
    assert state.phase is BattlePhase.UNKNOWN
    assert state.phase_valid is False
    assert state.phase_evidence[0] == "wIsInBattle"
    assert set(_TRANSIENT_SYMBOLS) <= set(state.phase_evidence)


def test_phase_regular_menu_mode_is_not_command_selection():
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 1
    mem[0xC003] = 0  # wMoveMenuType: regular-mode default, menu may be closed
    state = parse_battle(mem, sym)
    assert state.phase is BattlePhase.UNKNOWN
    assert state.phase_valid is False


def test_phase_command_selection_when_move_menu_is_open():
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 1
    mem[0xC003] = 1  # wMoveMenuType: mimic mode
    state = parse_battle(mem, sym)
    assert state.phase is BattlePhase.COMMAND_SELECTION
    assert state.phase_valid is True


def test_phase_command_selection_corroborating_index_is_not_contradiction():
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 1
    mem[0xC003] = 1  # wMoveMenuType
    mem[0xC004] = 2  # wPlayerMoveListIndex
    state = parse_battle(mem, sym)
    assert state.phase is BattlePhase.COMMAND_SELECTION
    assert state.phase_valid is True


def test_phase_move_index_absent_required_symbols_is_unknown():
    sym = load_sym_text(
        """
        00:C000 wIsInBattle
        00:C004 wPlayerMoveListIndex
        """
    )
    mem = DictMemory({0xC000: 1, 0xC004: 2})
    state = parse_battle(mem, sym)
    assert state.phase is BattlePhase.UNKNOWN
    assert state.phase_valid is False


def test_phase_regular_menu_flag_ignores_stale_move_index():
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 1
    mem[0xC003] = 0  # wMoveMenuType: regular-mode default
    mem[0xC004] = 3  # stale move-list index from an earlier menu
    state = parse_battle(mem, sym)
    assert state.phase is BattlePhase.UNKNOWN
    assert state.phase_valid is False


def test_phase_action_resolution_when_turn_was_consumed():
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 2
    mem[0xC005] = 1  # wActionResultOrTookBattleTurn
    state = parse_battle(mem, sym)
    assert state.phase is BattlePhase.ACTION_RESOLUTION
    assert state.phase_valid is True


def test_phase_forced_replacement_when_player_faint_handler_is_set():
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 2
    mem[0xC002] = 1  # wInHandlePlayerMonFainted
    state = parse_battle(mem, sym)
    assert state.phase is BattlePhase.FORCED_REPLACEMENT
    assert state.phase_valid is True


@pytest.mark.parametrize("result", [1, 2])
def test_phase_terminal_return_for_valid_outcomes(result):
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 2
    mem[0xC001] = result  # wBattleResult: lose / draw
    state = parse_battle(mem, sym)
    assert state.phase is BattlePhase.TERMINAL_RETURN
    assert state.phase_valid is True
    assert state.terminal_result == result


def test_phase_zero_result_is_not_terminal_and_exposed_as_raw_value():
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 2
    mem[0xC001] = 0  # wBattleResult: also the reset value, ambiguous
    state = parse_battle(mem, sym)
    assert state.phase is BattlePhase.UNKNOWN
    assert state.phase_valid is False
    assert state.terminal_result == 0


def test_phase_invalid_result_is_not_terminal_and_not_exposed():
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 2
    mem[0xC001] = 255  # wBattleResult: no engine writer emits this
    state = parse_battle(mem, sym)
    assert state.phase is BattlePhase.UNKNOWN
    assert state.phase_valid is False
    assert state.terminal_result is None


@pytest.mark.parametrize(
    "flags",
    [
        {"wBattleResult": 1, "wMoveMenuType": 1},
        {"wInHandlePlayerMonFainted": 1, "wActionResultOrTookBattleTurn": 1},
        {"wBattleResult": 1, "wInHandlePlayerMonFainted": 1},
    ],
)
def test_phase_contradictory_flags_fail_closed_to_none(flags):
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 2
    addresses = {
        "wBattleResult": 0xC001,
        "wInHandlePlayerMonFainted": 0xC002,
        "wMoveMenuType": 0xC003,
        "wActionResultOrTookBattleTurn": 0xC005,
    }
    for name, value in flags.items():
        mem[addresses[name]] = value
    state = parse_battle(mem, sym)
    assert state.phase is None
    assert state.phase_valid is False


def test_phase_missing_evidence_fails_closed_to_unknown():
    sym = load_sym_text("00:C000 wIsInBattle\n")
    mem = DictMemory({0xC000: 1})
    state = parse_battle(mem, sym)
    assert state.phase is BattlePhase.UNKNOWN
    assert state.phase_valid is False
    assert state.phase_evidence == ("wIsInBattle",)


def test_phase_invalid_kind_is_none_not_guessed():
    sym = load_sym_text("00:C000 wIsInBattle\n")
    mem = DictMemory({0xC000: 0xFF})
    state = parse_battle(mem, sym)
    assert state.kind is None
    assert state.phase is None
    assert state.phase_valid is False


# --- enemy combatant --------------------------------------------------------


def test_enemy_mon_reads_named_battle_offsets_not_party_offsets():
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 2  # trainer battle
    _write_enemy(mem)
    # Party-layout decoys at the same relocated base: PP at +29, MaxHP at +34.
    for index, value in enumerate((1, 1, 1, 1)):
        mem[_ENEMY_BASE + 29 + index] = value
    mem[_ENEMY_BASE + 34] = 0x00
    mem[_ENEMY_BASE + 35] = 0x63  # 99
    # Canonical Red/Blue enemy address decoys to catch hardcoded reads.
    mem[0xCFE5] = 0x11
    mem[0xCFE6] = 0x22
    mem[0xCFE7] = 0x33

    state = parse_battle(mem, sym)

    enemy = state.enemy_mon
    assert enemy is not None
    assert enemy.species == 0x99
    assert enemy.hp == 32
    assert enemy.max_hp == 40
    assert enemy.pp == (20, 25, 0, 0)
    assert enemy.moves == (0x21, 0x0A, 0, 0)
    assert enemy.slot == 0
    assert state.enemy_mon_valid is True


def test_enemy_mon_missing_fields_is_unknown_not_zero():
    sym = load_sym_text(
        """
        00:C000 wIsInBattle
        00:C103 wEnemyMonPartyPos
        """
    )
    mem = DictMemory({0xC000: 2})
    state = parse_battle(mem, sym)
    assert state.enemy_mon is None
    assert state.enemy_mon_valid is False
    assert state.terminal_result is None


def test_enemy_mon_absent_slot_symbol_is_unknown():
    sym = load_sym_text(
        """
        00:C000 wIsInBattle
        00:C100 wEnemyMonSpecies
        00:C101 wEnemyMonHP
        00:C104 wEnemyMonStatus
        00:C105 wEnemyMonType1
        00:C106 wEnemyMonType2
        00:C108 wEnemyMonMoves
        00:C10E wEnemyMonLevel
        00:C10F wEnemyMonMaxHP
        00:C119 wEnemyMonPP
        """
    )
    mem = DictMemory({0xC000: 2})
    _write_enemy(mem)
    state = parse_battle(mem, sym)
    assert state.enemy_mon is None
    assert state.enemy_mon_valid is False


def test_enemy_mon_wild_validity_is_unknown_not_slot_zero():
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 1  # wild battle
    _write_enemy(mem, slot=3)
    state = parse_battle(mem, sym)
    assert state.enemy_mon is not None
    assert state.enemy_mon.slot == 3
    assert state.enemy_mon_valid is None


def test_enemy_mon_inactive_leaves_values_unknown():
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 0
    _write_enemy(mem)
    state = parse_battle(mem, sym)
    assert state.enemy_mon is None
    assert state.enemy_mon_valid is None


@pytest.mark.parametrize(
    ("species", "hp", "max_hp", "slot", "status"),
    [
        (0x00, 32, 40, 0, 0),  # empty species
        (0xFF, 32, 40, 0, 0),  # sentinel species
        (0x99, 32, 0, 0, 0),  # zero max HP
        (0x99, 41, 40, 0, 0),  # HP above max HP
        (0x99, 32, 40, 6, 0),  # slot outside 0..5
        (0x99, 32, 40, 0, 0x80),  # reserved status bit
    ],
)
def test_enemy_mon_invalid_values_mark_invalid(species, hp, max_hp, slot, status):
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 2
    _write_enemy(mem, species=species, hp=hp, max_hp=max_hp, slot=slot, status=status)
    state = parse_battle(mem, sym)
    assert state.enemy_mon is not None
    assert state.enemy_mon_valid is False


def test_parse_battle_combatant_absent_prefix_is_none():
    sym = load_sym_text("00:C000 wIsInBattle\n")
    mem = DictMemory()
    assert parse_battle_combatant(mem, sym, "wEnemyMon", slot=0) is None


def test_battle_kind_and_existing_fields_unchanged_by_new_observation():
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 2
    mem[0xC008] = 0x0A
    mem[0xC009] = 3
    state = parse_battle(mem, sym)
    assert state.kind is BattleKind.TRAINER
    assert state.engaged_trainer_class == 0x0A
    assert state.engaged_trainer_set == 3
    assert state.is_trainer_battle is True


# --- resource / JSON exposure ----------------------------------------------


def _session() -> Session:
    mem = DictMemory()
    return Session(
        pyboy=FakePyBoy(mem),
        symbols=_symbols(),
        event_bus=EventBus(),
    )


def test_game_state_resource_contains_additive_battle_fields():
    session = _session()
    session._pyboy.memory[0xC000] = 2  # trainer battle
    session._pyboy.memory[0xC003] = 1  # wMoveMenuType: mimic selection
    _write_enemy(session._pyboy.memory)
    body = json.loads(read_resource(session, "pokered://game-state"))
    battle = body["battle"]
    assert battle["phase"] == int(BattlePhase.COMMAND_SELECTION)
    assert battle["phase_valid"] is True
    assert "wMoveMenuType" in battle["phase_evidence"]
    assert battle["enemy_mon_valid"] is True
    assert battle["enemy_mon"]["hp"] == 32
    assert battle["enemy_mon"]["max_hp"] == 40
    assert battle["enemy_mon"]["pp"] == [20, 25, 0, 0]
    assert battle["terminal_result"] == 0


def test_state_epoch_resource_tracks_session_tick():
    session = _session()
    assert json.loads(read_resource(session, "pokered://state-epoch")) == {
        "tick": 0,
        "load_generation": 0,
    }
    dispatch_tool(session, "step", {"count": 3})
    assert json.loads(read_resource(session, "pokered://state-epoch")) == {
        "tick": 3,
        "load_generation": 0,
    }


def test_state_epoch_load_generation_advances_on_load_state():
    session = _session()
    dispatch_tool(session, "step", {"count": 5})
    payload = base64.b64encode(b"SNAPSHOT").decode("ascii")
    dispatch_tool(session, "load_state", {"data": payload})
    assert json.loads(read_resource(session, "pokered://state-epoch")) == {
        "tick": 5,
        "load_generation": 1,
    }


def test_resource_specs_advertise_state_epoch():
    names = {spec.name for spec in _resource_specs()}
    assert "State Epoch" in names
    uris = {str(spec.uri) for spec in _resource_specs()}
    assert "pokered://state-epoch" in uris
