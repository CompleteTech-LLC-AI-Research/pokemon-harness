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
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from pokered_harness.events import EventBus
from pokered_harness.mcp_server import (
    LinkState,
    _resource_specs,
    dispatch_tool,
    read_resource,
)
from pokered_harness.session import Session
from pokered_harness.state.battle import (
    BattleKind,
    BattleLifecycle,
    BattlePhase,
    parse_battle,
)
from pokered_harness.state.party import parse_battle_combatant
from pokered_harness.symbols.loader import SymbolTable, load_sym_text
from tests.conftest import DictMemory
from tests.fakes import FakePyBoy

ROOT = Path(__file__).resolve().parents[1]

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
00:C006 wEscapedFromBattle
00:C007 wBattleType
00:C008 wEngagedTrainerClass
00:C009 wEngagedTrainerSet
00:C00A wPlayerMonNumber
00:C00B wPlayerSelectedMove
00:C00C wEnemySelectedMove
00:C00D wCurrentMenuItem
00:C024 wPartyCount
00:C02E wPartyMenuTypeOrMessageID
00:C02F wPartyMenuAnimMonEnabled
00:D00E wBattleMonHP
00:D130 wPartyMon1HP
00:C018 wPlayerMonAttackMod
00:C019 wPlayerMonDefenseMod
00:C01A wPlayerMonSpeedMod
00:C01B wPlayerMonSpecialMod
00:C01C wPlayerMonAccuracyMod
00:C01D wPlayerMonEvasionMod
00:C01E wEnemyMonAttackMod
00:C01F wEnemyMonDefenseMod
00:C020 wEnemyMonSpeedMod
00:C021 wEnemyMonSpecialMod
00:C022 wEnemyMonAccuracyMod
00:C023 wEnemyMonEvasionMod
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
0F:4233 MainInBattleLoop
0F:42A6 MainInBattleLoop.selectEnemyMove
0F:4F1A DisplayBattleMenu.handleBattleMenuInput
0F:52FE SelectMenuItem
0F:3725 LoadScreenTilesFromBuffer1
0F:565E ExecutePlayerMove
0F:580A ExecutePlayerMoveDone
0F:66BC ExecuteEnemyMove
0F:688C ExecuteEnemyMoveDone
04:77AA EndOfBattle
"""

# Execution-hook addresses the session installs for the battle command/move
# menu; firing them drives the same entry/exit state a real ROM program
# counter would produce.
_MENU_OPEN_HOOKS = ((0x0F, 0x52FE), (0x0F, 0x4F1A))
_MENU_CLOSE_HOOKS = ((0x0F, 0x4233), (0x0F, 0x42A6))
# The shared post-menu redraw path fired on the regular move-selection return
# and on the Mimic submenu return into animation/result text.
_MIMIC_CLOSE_HOOK = (0x0F, 0x3725)
# ``EndOfBattle`` entry, where the session samples the outcome bytes.
_BATTLE_END_HOOK = (0x04, 0x77AA)
_PLAYER_STAT_MOD_BASE = 0xC018
_ENEMY_STAT_MOD_BASE = 0xC01E
_CURRENT_MENU_ITEM = 0xC00D
# ``wBattleMonHP`` is big-endian; the synthetic table places it in the
# switchable WRAM half so the read exercises the bank-1 path.
_PLAYER_BATTLE_MON_HP = 0xD00E
# ``ChooseNextMon`` writes BATTLE_PARTY_MENU (2) to the type byte and
# ``DisplayPartyMenu``'s input loop raises the animation flag to ``$40`` while
# the menu keeps awaiting input; the exit path clears the flag.
_PARTY_MENU_TYPE = 0xC02E
_PARTY_MENU_ANIM = 0xC02F
_PARTY_MENU_SYMBOLS = ("wPartyMenuTypeOrMessageID", "wPartyMenuAnimMonEnabled")
# Move-execution brackets the session installs to observe ACTION_RESOLUTION.
_RESOLUTION_OPEN_HOOKS = ((0x0F, 0x565E), (0x0F, 0x66BC))
_RESOLUTION_CLOSE_HOOKS = ((0x0F, 0x580A), (0x0F, 0x688C))

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


def test_phase_mimic_menu_mode_is_not_command_selection():
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 1
    mem[0xC003] = 1  # wMoveMenuType: mimic mode persists after the menu closes
    state = parse_battle(mem, sym)
    assert state.phase is BattlePhase.UNKNOWN
    assert state.phase_valid is False
    assert "wMoveMenuType" in state.phase_evidence


def test_phase_menu_mode_with_stale_index_is_not_command_selection():
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 1
    mem[0xC003] = 2  # wMoveMenuType: relearn/PP mode, also persistent
    mem[0xC004] = 2  # wPlayerMoveListIndex
    state = parse_battle(mem, sym)
    assert state.phase is BattlePhase.UNKNOWN
    assert state.phase_valid is False


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
    # ChooseNextMon writes BATTLE_PARTY_MENU and DisplayPartyMenu's input loop
    # raises wPartyMenuAnimMonEnabled to $40 while the menu awaits input; the
    # ROM also requires AnyPartyAlive to be non-zero (HandlePlayerMonFainted
    # jumps to HandlePlayerBlackOut otherwise).
    mem[_PARTY_MENU_TYPE] = 2
    mem[_PARTY_MENU_ANIM] = 0x40
    mem[0xC024] = 6  # wPartyCount
    mem[0xD130] = 0  # wPartyMon1HP (slot 0) high byte
    mem[0xD131] = 0  # ... low byte: fainted
    mem[0xD130 + 5 * 44] = 0x00  # slot 5 high byte
    mem[0xD130 + 5 * 44 + 1] = 148  # slot 5 low byte: living replacement
    state = parse_battle(mem, sym)
    assert state.phase is BattlePhase.FORCED_REPLACEMENT
    assert state.phase_valid is True
    assert set(_PARTY_MENU_SYMBOLS) <= set(state.phase_evidence)


def test_phase_live_replacement_after_simultaneous_knockout_without_faint_flag():
    # HandleEnemyMonFainted clears wInHandlePlayerMonFainted and then calls
    # ChooseNextMon itself when the player's combatant also hit zero, so a real
    # replacement menu can be open with the faint flag at zero.  Requiring the
    # flag reported UNKNOWN for a live replacement; the menu is the real signal.
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 2
    mem[0xC002] = 0  # cleared by HandleEnemyMonFainted
    mem[_PARTY_MENU_TYPE] = 2  # BATTLE_PARTY_MENU
    mem[_PARTY_MENU_ANIM] = 0x40  # live menu input loop
    mem[0xC024] = 2  # wPartyCount
    mem[0xD130] = 0
    mem[0xD131] = 0  # slot 0 fainted
    mem[0xD130 + 44] = 0x00
    mem[0xD130 + 44 + 1] = 100  # slot 1 living
    state = parse_battle(mem, sym)
    assert state.phase is BattlePhase.FORCED_REPLACEMENT
    assert state.phase_valid is True


def test_phase_final_enemy_knockout_does_not_open_a_replacement_menu():
    # When the enemy's last mon faints beside the player, HandlePlayerMonFainted
    # jumps to TrainerBattleVictory before any menu opens.  The faint flag and a
    # zeroed combatant are both present, yet there is no live party menu, so the
    # phase must not claim a replacement.
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 2
    mem[0xC002] = 1  # set by HandlePlayerMonFainted
    mem[_PLAYER_BATTLE_MON_HP] = 0
    mem[_PLAYER_BATTLE_MON_HP + 1] = 0
    # No live menu: wPartyMenuTypeOrMessageID/AnimMonEnabled stay at their
    # defaults, i.e. the menu either never opened or has already closed.
    mem[0xC024] = 6
    state = parse_battle(mem, sym)
    assert state.phase is not BattlePhase.FORCED_REPLACEMENT
    assert state.phase_valid is False


def test_phase_final_faint_with_no_living_party_is_not_forced_replacement():
    # HandlePlayerMonFainted calls AnyPartyAlive and jumps to
    # HandlePlayerBlackOut when nothing survives, so the ROM does not open the
    # menu.  Even if a stale menu signal were somehow present, an all-fainted
    # party is contradictory and must not be reported as a replacement.
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 2
    mem[0xC002] = 1  # wInHandlePlayerMonFainted
    mem[_PARTY_MENU_TYPE] = 2
    mem[_PARTY_MENU_ANIM] = 0x40
    mem[0xC024] = 6  # wPartyCount
    # Every slot's HP bytes stay zero (DictMemory defaults to 0).
    state = parse_battle(mem, sym)
    assert state.phase is BattlePhase.UNKNOWN
    assert state.phase_valid is False
    assert state.in_handle_player_mon_fainted == 1


def test_phase_stale_faint_flag_with_living_combatant_is_not_forced_replacement():
    # HandlePlayerMonFainted sets wInHandlePlayerMonFainted and only
    # HandleEnemyMonFainted clears it, so the byte survives ChooseNextMon
    # returning to the battle loop.  With the party menu closed (the anim flag
    # is cleared on exit) it is stale evidence, not a live replacement.
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 2
    mem[0xC002] = 1  # stale wInHandlePlayerMonFainted
    mem[_PARTY_MENU_TYPE] = 2  # type byte left behind after the menu closed
    mem[_PARTY_MENU_ANIM] = 0  # HandlePartyMenuInput cleared it on exit
    state = parse_battle(mem, sym)
    assert state.phase is BattlePhase.UNKNOWN
    assert state.phase_valid is False
    assert state.in_handle_player_mon_fainted == 1


def test_phase_faint_flag_without_menu_symbols_fails_closed():
    # Without the party-menu symbols the derivation cannot tell a live
    # replacement from a stale flag, so it must not name FORCED_REPLACEMENT.
    sym = load_sym_text(
        """
        00:C000 wIsInBattle
        00:C001 wBattleResult
        00:C002 wInHandlePlayerMonFainted
        00:C003 wMoveMenuType
        00:C004 wPlayerMoveListIndex
        00:C005 wActionResultOrTookBattleTurn
        """
    )
    mem = DictMemory({0xC000: 2, 0xC002: 1})
    state = parse_battle(mem, sym)
    assert state.phase is None
    assert state.phase_valid is False


@pytest.mark.parametrize("result", [0, 1, 2])
def test_phase_ended_battle_reports_terminal_phase(result):
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 0  # wIsInBattle: battle has ended
    mem[0xC001] = result  # wBattleResult: win / lose / draw
    # Promotion needs the bytes the ROM held at EndOfBattle entry; a plain
    # falling edge cannot attribute the byte to this end.
    state = parse_battle(
        mem,
        sym,
        lifecycle=BattleLifecycle(
            was_active=True, end_observed=True, end_result=result
        ),
    )
    assert state.phase is BattlePhase.TERMINAL_RETURN
    assert state.phase_valid is True
    assert state.raw_battle_result == result
    # Only a non-zero byte survives teardown; zero is ambiguous.
    assert state.terminal_result == (result if result in (1, 2) else None)


def test_phase_blackout_zero_is_not_a_confirmed_win():
    # ResetStatusAndHalveMoneyOnBlackout writes wBattleResult = 0 and
    # wIsInBattle = 0, so the zero after an active battle cannot be a win.
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 0
    mem[0xC001] = 0
    state = parse_battle(mem, sym, lifecycle=BattleLifecycle(was_active=True))
    assert state.phase is BattlePhase.TERMINAL_RETURN
    assert state.phase_valid is True
    assert state.terminal_result is None
    assert state.raw_battle_result == 0


def test_phase_escape_zero_is_not_a_confirmed_win():
    # Roar/Whirlwind/Teleport set wEscapedFromBattle and leave the result at
    # zero; captured escape evidence keeps the outcome unknown.
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 0
    mem[0xC001] = 0
    mem[0xC006] = 1  # wEscapedFromBattle left set after cleanup
    state = parse_battle(mem, sym, lifecycle=BattleLifecycle(was_active=True, escaped=True))
    assert state.phase is BattlePhase.TERMINAL_RETURN
    assert state.terminal_result is None
    assert state.escaped_from_battle == 1


def test_phase_escape_evidence_suppresses_nonzero_result():
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 0
    mem[0xC001] = 2
    mem[0xC006] = 1  # wEscapedFromBattle
    state = parse_battle(mem, sym, lifecycle=BattleLifecycle(was_active=True))
    assert state.phase is BattlePhase.TERMINAL_RETURN
    assert state.terminal_result is None
    assert state.raw_battle_result == 2


def test_phase_fresh_inactive_result_zero_is_not_terminal():
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 0
    mem[0xC001] = 0  # reset value, no battle was observed
    state = parse_battle(mem, sym)
    assert state.phase is BattlePhase.INACTIVE
    assert state.phase_valid is True
    assert state.terminal_result is None
    assert state.raw_battle_result == 0


def test_phase_active_result_zero_is_not_terminal():
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 2
    mem[0xC001] = 0  # also written by InitBattleVariables at battle start
    state = parse_battle(mem, sym)
    assert state.phase is BattlePhase.UNKNOWN
    assert state.phase_valid is False
    assert state.terminal_result is None
    assert state.raw_battle_result == 0


@pytest.mark.parametrize("result", [1, 2])
def test_phase_active_positive_result_is_raw_only(result):
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 2
    mem[0xC001] = result  # mid-battle faint/capture marker, not the end
    state = parse_battle(mem, sym)
    assert state.phase is BattlePhase.UNKNOWN
    assert state.phase_valid is False
    assert state.terminal_result is None
    assert state.raw_battle_result == result


def test_phase_ended_with_invalid_result_is_not_terminal():
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 0
    mem[0xC001] = 255  # no engine writer emits this
    state = parse_battle(mem, sym, lifecycle=BattleLifecycle(was_active=True))
    assert state.phase is BattlePhase.UNKNOWN
    assert state.phase_valid is False
    assert state.terminal_result is None
    assert state.raw_battle_result == 255


def test_phase_lifecycle_active_but_still_in_battle_is_not_terminal():
    mem = DictMemory()
    sym = _symbols()
    mem[0xC000] = 1
    mem[0xC001] = 0
    state = parse_battle(mem, sym, lifecycle=BattleLifecycle(was_active=True))
    assert state.phase is BattlePhase.UNKNOWN
    assert state.terminal_result is None


@pytest.mark.parametrize(
    "flags",
    [
        # A live replacement menu and a consumed action are contradictory: the
        # ROM opens the party menu only after the turn's action is finished.
        # The faint flag is deliberately not part of this case -- it can be
        # zero during a genuine replacement (HandleEnemyMonFainted clears it
        # before calling ChooseNextMon), so it is not a replacement signal.
        {
            "wPartyMenuTypeOrMessageID": 2,
            "wPartyMenuAnimMonEnabled": 0x40,
            "wActionResultOrTookBattleTurn": 1,
        },
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
        "wPartyMenuTypeOrMessageID": _PARTY_MENU_TYPE,
        "wPartyMenuAnimMonEnabled": _PARTY_MENU_ANIM,
    }
    for name, value in flags.items():
        mem[addresses[name]] = value
    state = parse_battle(mem, sym)
    assert state.phase is None
    assert state.phase_valid is False


def test_phase_terminal_missing_result_symbol_fails_closed():
    sym = load_sym_text(
        """
        00:C000 wIsInBattle
        00:C002 wInHandlePlayerMonFainted
        00:C003 wMoveMenuType
        00:C004 wPlayerMoveListIndex
        00:C005 wActionResultOrTookBattleTurn
        """
    )
    mem = DictMemory({0xC000: 0})
    state = parse_battle(mem, sym, lifecycle=BattleLifecycle(was_active=True))
    assert state.phase is BattlePhase.UNKNOWN
    assert state.phase_valid is False
    assert state.terminal_result is None
    assert state.raw_battle_result is None


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


# --- command selection via session execution hooks --------------------------


def test_command_selection_derived_from_move_menu_hook_entry():
    session = _session()
    session._pyboy.memory[0xC000] = 1  # wild battle
    session._pyboy.memory[0xC003] = 0  # stale regular-mode byte, menu closed

    closed = session.read_game_state().battle
    assert closed.phase is BattlePhase.UNKNOWN
    assert closed.phase_valid is False
    assert closed.menu_open is False
    assert closed.menu_evidence[:1] == ("SelectMenuItem",)

    session._pyboy.fire(*_MENU_OPEN_HOOKS[0])
    opened = session.read_game_state().battle
    assert opened.phase is BattlePhase.COMMAND_SELECTION
    assert opened.phase_valid is True
    assert opened.menu_open is True
    assert "SelectMenuItem" in opened.phase_evidence

    session._pyboy.fire(*_MENU_CLOSE_HOOKS[0])
    closed_again = session.read_game_state().battle
    assert closed_again.phase is BattlePhase.UNKNOWN
    assert closed_again.phase_valid is False
    assert closed_again.menu_open is False


def test_command_selection_derived_from_command_menu_hook_entry():
    session = _session()
    session._pyboy.memory[0xC000] = 2
    session._pyboy.fire(*_MENU_OPEN_HOOKS[1])
    opened = session.read_game_state().battle
    assert opened.phase is BattlePhase.COMMAND_SELECTION
    assert opened.menu_open is True
    assert "DisplayBattleMenu.handleBattleMenuInput" in opened.phase_evidence
    session._pyboy.fire(*_MENU_CLOSE_HOOKS[1])
    assert session.read_game_state().battle.menu_open is False


def test_command_selection_closes_on_mimic_submenu_return():
    session = _session()
    session._pyboy.memory[0xC000] = 1  # wild battle
    session._pyboy.fire(*_MENU_OPEN_HOOKS[0])
    opened = session.read_game_state().battle
    assert opened.phase is BattlePhase.COMMAND_SELECTION
    assert opened.menu_open is True

    # Mimic returns from MoveSelectionMenu straight into animation/result text,
    # so the per-turn MainInBattleLoop close hooks do not fire.  The shared
    # post-menu redraw does, and it must end the selection observation.
    session._pyboy.fire(*_MIMIC_CLOSE_HOOK)
    closed = session.read_game_state().battle
    assert closed.phase is not BattlePhase.COMMAND_SELECTION
    assert closed.menu_open is False


# --- action resolution via session execution hooks --------------------------


def test_action_resolution_derived_from_move_execution_hook_entry():
    # An ordinary FIGHT move leaves wActionResultOrTookBattleTurn at zero
    # (ExecutePlayerMoveDone clears it), so the flag can never report this
    # turn.  The move-execution hook is the ROM-owned evidence.
    session = _session()
    session._pyboy.memory[0xC000] = 2  # trainer battle
    session._pyboy.memory[0xC005] = 0  # flag already cleared by *Done

    before = session.read_game_state().battle
    assert before.phase is not BattlePhase.ACTION_RESOLUTION
    assert before.resolution_open is False

    session._pyboy.fire(*_RESOLUTION_OPEN_HOOKS[0])
    resolving = session.read_game_state().battle
    assert resolving.phase is BattlePhase.ACTION_RESOLUTION
    assert resolving.phase_valid is True
    assert resolving.resolution_open is True
    assert "ExecutePlayerMove" in resolving.phase_evidence

    session._pyboy.fire(*_RESOLUTION_CLOSE_HOOKS[0])
    resolved = session.read_game_state().battle
    assert resolved.phase is not BattlePhase.ACTION_RESOLUTION
    assert resolved.resolution_open is False


def test_action_resolution_observed_for_enemy_move_execution():
    session = _session()
    session._pyboy.memory[0xC000] = 2
    session._pyboy.memory[0xC005] = 0
    session._pyboy.fire(*_RESOLUTION_OPEN_HOOKS[1])
    resolving = session.read_game_state().battle
    assert resolving.phase is BattlePhase.ACTION_RESOLUTION
    assert resolving.resolution_open is True
    assert "ExecuteEnemyMove" in resolving.phase_evidence
    session._pyboy.fire(*_RESOLUTION_CLOSE_HOOKS[1])
    assert session.read_game_state().battle.resolution_open is False


def test_resolution_observation_invalidated_by_reset_tick():
    # A load/reset starts a new observation epoch; the previous epoch's
    # in-flight resolution must not survive into it.
    session = _session()
    session._pyboy.memory[0xC000] = 2
    session._pyboy.fire(*_RESOLUTION_OPEN_HOOKS[0])
    assert session.read_game_state().battle.resolution_open is True
    session.reset_tick()
    after = session.read_game_state().battle
    assert after.resolution_open is None
    assert after.phase is not BattlePhase.ACTION_RESOLUTION


def test_advance_tick_preserves_observation_but_reset_tick_invalidates():
    session = _session()
    session._pyboy.memory[0xC000] = 2  # trainer battle
    session._pyboy.fire(*_MENU_OPEN_HOOKS[0])
    assert session.read_game_state().battle.menu_open is True

    # Ordinary interleaved-step bookkeeping advancement must not discard the
    # observation the execution hooks just recorded.
    session._advance_tick(5)
    still_open = session.read_game_state().battle
    assert still_open.menu_open is True
    assert still_open.phase is BattlePhase.COMMAND_SELECTION
    assert still_open.phase_valid is True

    # An intentional rewind still starts a new epoch and invalidates evidence.
    session.reset_tick(0)
    invalidated = session.read_game_state().battle
    assert invalidated.menu_open is None


def test_command_selection_stale_mode_byte_without_hook_is_not_selection():
    session = _session()
    # Mimic mode (1) and relearn mode (2) persist after the menu closes; the
    # move-list index and selected-move bytes also persist.  Without a hook
    # entry none is evidence that the menu is open.
    for mode in (0, 1, 2):
        session._pyboy.memory[0xC000] = 1
        session._pyboy.memory[0xC003] = mode
        session._pyboy.memory[0xC004] = 2
        session._pyboy.memory[0xC00B] = 0x21
        state = session.read_game_state().battle
        assert state.phase is BattlePhase.UNKNOWN
        assert state.phase_valid is False
        assert state.menu_open is False


def test_command_selection_requires_observation_enabled():
    mem = DictMemory({0xC000: 1, 0xC003: 0})
    session = Session(pyboy=FakePyBoy(mem), symbols=_symbols(), event_bus=EventBus())
    state = session.read_game_state().battle
    assert state.menu_open is None
    assert state.phase is BattlePhase.UNKNOWN
    assert state.phase_valid is False


def test_command_selection_absent_menu_symbols_is_uncertain():
    sym = load_sym_text(
        """
        00:C000 wIsInBattle
        00:C001 wBattleResult
        00:C002 wInHandlePlayerMonFainted
        00:C003 wMoveMenuType
        00:C004 wPlayerMoveListIndex
        00:C005 wActionResultOrTookBattleTurn
        """
    )
    mem = DictMemory({0xC000: 1, 0xC003: 0})
    session = Session(pyboy=FakePyBoy(mem), symbols=sym, event_bus=EventBus())
    assert session.enable_battle_menu_observation() is False
    state = session.read_game_state().battle
    assert state.menu_open is None
    assert state.phase is BattlePhase.UNKNOWN
    assert state.phase_valid is False


def test_command_selection_contradicting_action_flag_fails_closed():
    session = _session()
    session._pyboy.memory[0xC000] = 2
    session._pyboy.memory[0xC005] = 1  # wActionResultOrTookBattleTurn
    session._pyboy.fire(*_MENU_OPEN_HOOKS[0])
    state = session.read_game_state().battle
    # Two surviving signals (menu open + action resolution) contradict.
    assert state.phase is None
    assert state.phase_valid is False
    assert state.menu_open is True


def test_load_state_resets_menu_observation_to_unknown():
    session = _session()
    session._pyboy.memory[0xC000] = 1
    session._pyboy.fire(*_MENU_OPEN_HOOKS[0])
    assert session.read_game_state().battle.menu_open is True
    payload = base64.b64encode(b"OVERWORLD").decode("ascii")
    dispatch_tool(session, "load_state", {"data": payload})
    state = session.read_game_state().battle
    assert state.menu_open is None
    assert state.phase is not BattlePhase.COMMAND_SELECTION


def test_reset_tick_resets_menu_observation_to_unknown():
    session = _session()
    session._pyboy.memory[0xC000] = 1
    session._pyboy.fire(*_MENU_OPEN_HOOKS[0])
    session.read_game_state()
    session.reset_tick(0)
    assert session.read_game_state().battle.menu_open is None


# --- transient / stat-stage fields ------------------------------------------


def test_transient_battle_fields_read_named_symbols():
    session = _session()
    mem = session._pyboy.memory
    mem[0xC000] = 2
    mem[0xC004] = 2  # wPlayerMoveListIndex
    mem[_CURRENT_MENU_ITEM] = 3  # wCurrentMenuItem
    mem[0xC002] = 1  # wInHandlePlayerMonFainted
    state = session.read_game_state().battle
    assert state.player_move_list_index == 2
    assert state.current_menu_item == 3
    assert state.in_handle_player_mon_fainted == 1
    assert state.action_result_or_took_turn == 0
    assert state.turn_already_consumed is False


def test_transient_fields_absent_symbols_are_none():
    sym = load_sym_text(
        """
        00:C000 wIsInBattle
        00:C001 wBattleResult
        00:C002 wInHandlePlayerMonFainted
        00:C003 wMoveMenuType
        00:C004 wPlayerMoveListIndex
        00:C005 wActionResultOrTookBattleTurn
        """
    )
    mem = DictMemory({0xC000: 1})
    state = parse_battle(mem, sym)
    assert state.current_menu_item is None
    assert state.player_move_list_index == 0
    assert state.in_handle_player_mon_fainted == 0


def test_stat_stages_decode_engine_offset_and_validity():
    session = _session()
    mem = session._pyboy.memory
    mem[0xC000] = 2
    for offset, raw in enumerate((7, 8, 6, 13, 1, 9)):
        mem[_PLAYER_STAT_MOD_BASE + offset] = raw
    for offset in range(6):
        mem[_ENEMY_STAT_MOD_BASE + offset] = 7  # neutral stat modifiers
    state = session.read_game_state().battle
    stages = state.player_stat_stages
    assert stages is not None
    assert stages.valid is True
    assert (
        stages.attack,
        stages.defense,
        stages.speed,
        stages.special,
        stages.accuracy,
        stages.evasion,
    ) == (0, 1, -1, 6, -6, 2)
    assert state.enemy_stat_stages is not None
    assert state.enemy_stat_stages.attack == 0
    assert state.enemy_stat_stages.valid is True


@pytest.mark.parametrize("raw", [0, 14, 255])
def test_stat_stages_out_of_range_byte_is_invalid_not_guessed(raw):
    session = _session()
    mem = session._pyboy.memory
    mem[0xC000] = 2
    for offset in range(6):
        mem[_PLAYER_STAT_MOD_BASE + offset] = 7
    mem[_PLAYER_STAT_MOD_BASE + 2] = raw  # wPlayerMonSpeedMod
    stages = session.read_game_state().battle.player_stat_stages
    assert stages is not None
    assert stages.speed is None
    assert stages.valid is False


def test_stat_stages_partial_symbols_report_unknown_validity():
    sym = load_sym_text(
        """
        00:C000 wIsInBattle
        00:C001 wBattleResult
        00:C002 wInHandlePlayerMonFainted
        00:C003 wMoveMenuType
        00:C004 wPlayerMoveListIndex
        00:C005 wActionResultOrTookBattleTurn
        00:C018 wPlayerMonAttackMod
        """
    )
    state = parse_battle(DictMemory({0xC000: 1, 0xC018: 8}), sym)
    assert state.player_stat_stages is not None
    assert state.player_stat_stages.attack == 1
    assert state.player_stat_stages.defense is None
    assert state.player_stat_stages.valid is None


def test_stat_stages_absent_family_is_none():
    sym = load_sym_text(
        """
        00:C000 wIsInBattle
        00:C001 wBattleResult
        00:C002 wInHandlePlayerMonFainted
        00:C003 wMoveMenuType
        00:C004 wPlayerMoveListIndex
        00:C005 wActionResultOrTookBattleTurn
        """
    )
    state = parse_battle(DictMemory({0xC000: 2}), sym)
    assert state.player_stat_stages is None
    assert state.enemy_stat_stages is None


def test_stat_stages_are_not_exposed_out_of_battle():
    session = _session()
    for offset in range(6):
        session._pyboy.memory[_PLAYER_STAT_MOD_BASE + offset] = 8
    state = session.read_game_state().battle
    assert state.active is False
    assert state.player_stat_stages is None


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
    # The whole wEnemyMon* family is absent, so the data is unavailable rather
    # than observed-invalid: the contract reserves False for a value the
    # harness actually read and rejected.
    assert state.enemy_mon_valid is None
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
    assert state.enemy_mon_valid is None


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


def _session(*, observe: bool = True) -> Session:
    mem = DictMemory()
    session = Session(
        pyboy=FakePyBoy(mem),
        symbols=_symbols(),
        event_bus=EventBus(),
    )
    if observe:
        assert session.enable_battle_menu_observation() is True
        assert session.enable_battle_resolution_observation() is True
        assert session.enable_battle_end_observation() is True
    return session


def test_game_state_resource_contains_additive_battle_fields():
    session = _session()
    session._pyboy.memory[0xC000] = 2  # trainer battle
    session._pyboy.memory[0xC005] = 1  # wActionResultOrTookBattleTurn
    _write_enemy(session._pyboy.memory)
    body = json.loads(read_resource(session, "pokered://game-state"))
    battle = body["battle"]
    assert battle["phase"] == int(BattlePhase.ACTION_RESOLUTION)
    assert battle["phase_valid"] is True
    assert "wMoveMenuType" in battle["phase_evidence"]
    assert battle["enemy_mon_valid"] is True
    assert battle["enemy_mon"]["hp"] == 32
    assert battle["enemy_mon"]["max_hp"] == 40
    assert battle["enemy_mon"]["pp"] == [20, 25, 0, 0]
    # The raw byte is always exposed; an unended battle never confirms it.
    assert battle["raw_battle_result"] == 0
    assert battle["terminal_result"] is None


def test_game_state_resource_exposes_menu_and_transient_fields():
    session = _session()
    mem = session._pyboy.memory
    mem[0xC000] = 2  # trainer battle
    mem[0xC004] = 1  # wPlayerMoveListIndex
    mem[_CURRENT_MENU_ITEM] = 2  # wCurrentMenuItem
    for offset, raw in enumerate((7, 8, 7, 7, 7, 7)):
        mem[_PLAYER_STAT_MOD_BASE + offset] = raw
    session._pyboy.fire(*_MENU_OPEN_HOOKS[0])
    battle = json.loads(read_resource(session, "pokered://game-state"))["battle"]
    assert battle["phase"] == int(BattlePhase.COMMAND_SELECTION)
    assert battle["menu_open"] is True
    assert battle["menu_evidence"][:1] == ["SelectMenuItem"]
    assert battle["player_move_list_index"] == 1
    assert battle["current_menu_item"] == 2
    assert battle["player_stat_stages"] == {
        "attack": 0,
        "defense": 1,
        "speed": 0,
        "special": 0,
        "accuracy": 0,
        "evasion": 0,
        "valid": True,
    }


def test_game_state_resource_embeds_snapshot_epoch():
    session = _session()
    dispatch_tool(session, "step", {"count": 4})
    body = json.loads(read_resource(session, "pokered://game-state"))
    assert body["epoch"] == {
        "tick": 4,
        "load_generation": 0,
        "reset_generation": 0,
        "session_id": session.session_id,
    }


def test_session_read_state_snapshot_pairs_state_with_epoch():
    session = _session()
    session._pyboy.memory[0xC000] = 2
    snapshot = session.read_state_snapshot()
    assert snapshot.state.battle is not None
    assert snapshot.epoch == session.read_epoch()
    assert snapshot.epoch.session_id == session.session_id


def test_replacement_sessions_have_distinct_identities():
    first = _session()
    second = _session()
    assert first.session_id != second.session_id
    assert read_resource(first, "pokered://state-epoch") != read_resource(
        second, "pokered://state-epoch"
    )


def test_session_identities_are_distinct_across_process_restarts():
    """Two fresh processes must not report the same first-session identity.

    A bare per-process counter restarts at 1 in every process, so a client
    comparing epochs across a server restart could accept a stale snapshot.
    """
    code = (
        "import json;"
        "from pokered_harness.events import EventBus;"
        "from pokered_harness.session import Session;"
        "from pokered_harness.symbols.loader import load_sym_text;"
        "from tests.conftest import DictMemory;"
        "from tests.fakes import FakePyBoy;"
        "s = Session(pyboy=FakePyBoy(DictMemory()),"
        " symbols=load_sym_text('00:C000 wIsInBattle'), event_bus=EventBus());"
        "print(json.dumps({'session_id': s.session_id, 'epoch': "
        "s.read_epoch().session_id}))"
    )
    runs = [
        json.loads(
            subprocess.run(
                [sys.executable, "-c", code],
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{ROOT}"},
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        for _ in range(2)
    ]
    for run in runs:
        assert run["session_id"] == run["epoch"]
    assert runs[0]["session_id"] != runs[1]["session_id"], runs


def test_peer_game_state_resource_embeds_peer_epoch():
    primary = _session()
    peer = _session()
    peer._pyboy.memory[0xC000] = 1  # wild battle on the peer
    link = LinkState(peer_session=peer)
    body = json.loads(read_resource(primary, "pokered://peer-game-state", link=link))
    assert body["battle"]["raw_is_in_battle"] == 1
    assert body["epoch"] == {
        "tick": 0,
        "load_generation": 0,
        "reset_generation": 0,
        "session_id": peer.session_id,
    }


def test_state_epoch_resource_tracks_session_tick():
    session = _session()
    assert json.loads(read_resource(session, "pokered://state-epoch")) == {
        "tick": 0,
        "load_generation": 0,
        "reset_generation": 0,
        "session_id": session.session_id,
    }
    dispatch_tool(session, "step", {"count": 3})
    assert json.loads(read_resource(session, "pokered://state-epoch")) == {
        "tick": 3,
        "load_generation": 0,
        "reset_generation": 0,
        "session_id": session.session_id,
    }


def test_state_epoch_reset_generation_advances_on_reset_tick():
    session = _session()
    dispatch_tool(session, "step", {"count": 3})
    session.reset_tick(3)
    assert json.loads(read_resource(session, "pokered://state-epoch")) == {
        "tick": 3,
        "load_generation": 0,
        "reset_generation": 1,
        "session_id": session.session_id,
    }


def test_state_epoch_load_generation_advances_on_load_state():
    session = _session()
    dispatch_tool(session, "step", {"count": 5})
    payload = base64.b64encode(b"SNAPSHOT").decode("ascii")
    dispatch_tool(session, "load_state", {"data": payload})
    assert json.loads(read_resource(session, "pokered://state-epoch")) == {
        "tick": 5,
        "load_generation": 1,
        "reset_generation": 0,
        "session_id": session.session_id,
    }


def test_read_game_state_keeps_ambiguous_zero_unknown_on_battle_end():
    session = _session()
    session._pyboy.memory[0xC000] = 2  # battle active
    active = session.read_game_state()
    assert active.battle is not None
    assert active.battle.terminal_result is None
    # EndOfBattle clears wIsInBattle but leaves wBattleResult at zero for a
    # win; a blackout or escape also leaves/clears zero, so the outcome stays
    # unknown even though the battle is observed to end.
    session._pyboy.memory[0xC000] = 0
    session._pyboy.memory[0xC001] = 0
    ended = session.read_game_state()
    assert ended.battle is not None
    assert ended.battle.phase is BattlePhase.TERMINAL_RETURN
    assert ended.battle.terminal_result is None
    assert ended.battle.raw_battle_result == 0
    # Confirmation is edge-triggered: a later overworld read is inactive.
    later = session.read_game_state()
    assert later.battle is not None
    assert later.battle.phase is BattlePhase.INACTIVE
    assert later.battle.terminal_result is None


def test_read_game_state_confirms_surviving_nonzero_outcome():
    session = _session()
    session._pyboy.memory[0xC000] = 2
    session._pyboy.memory[0xC001] = 1  # player-faint marker while active
    assert session.read_game_state().battle.terminal_result is None
    # The ROM's own EndOfBattle is what attributes the surviving byte to this
    # end; the session samples the outcome bytes at that routine's entry.
    session._pyboy.fire(*_BATTLE_END_HOOK)
    session._pyboy.memory[0xC000] = 0
    session._pyboy.memory[0xC001] = 1  # survived teardown
    ended = session.read_game_state()
    assert ended.battle is not None
    assert ended.battle.terminal_result == 1


def test_read_game_state_requires_rom_observed_end_to_promote_result():
    # Without the EndOfBattle hook a client-timed read cannot attribute a
    # surviving byte to this end, so promotion stays disabled.
    mem = DictMemory({0xC000: 2, 0xC001: 1})
    session = Session(pyboy=FakePyBoy(mem), symbols=_symbols(), event_bus=EventBus())
    assert session.enable_battle_end_observation() is True
    assert session.read_game_state().battle.terminal_result is None
    # No hook fired: the falling edge alone must not promote the stale byte.
    session._pyboy.memory[0xC000] = 0
    ended = session.read_game_state()
    assert ended.battle is not None
    assert ended.battle.phase is BattlePhase.TERMINAL_RETURN
    assert ended.battle.terminal_result is None


def test_unsampled_escape_does_not_promote_stale_player_faint_result():
    """An escape that finishes between two reads must not confirm a faint.

    ``SwitchAndTeleportEffect``/``ItemUsePokeDoll`` set ``wEscapedFromBattle``
    and ``EndOfBattle`` clears it on the way out without touching
    ``wBattleResult``.  A player faint earlier in the same battle leaves a
    ``1`` there, so a reader that only compares snapshots would promote that
    stale byte as if the escape had been a loss.
    """
    session = _session()
    session._pyboy.memory[0xC000] = 1  # live wild battle
    session._pyboy.memory[0xC001] = 1  # earlier player-faint result byte
    assert session.read_game_state().battle.terminal_result is None
    # Escape flag is set and then cleared again entirely between reads; the
    # session's EndOfBattle hook still samples both bytes for this end.
    session._pyboy.memory[0xC006] = 1  # wEscapedFromBattle
    session._pyboy.fire(*_BATTLE_END_HOOK)
    session._pyboy.memory[0xC000] = 0  # EndOfBattle clears wIsInBattle
    session._pyboy.memory[0xC006] = 0  # ... and wEscapedFromBattle
    ended = session.read_game_state()
    assert ended.battle is not None
    assert ended.battle.phase is BattlePhase.TERMINAL_RETURN
    assert ended.battle.terminal_result is None


@pytest.mark.parametrize("transition", ["load", "reset"])
def test_pending_end_sample_cannot_cross_an_epoch(transition):
    """A sampled ``EndOfBattle`` outcome must not outlive its epoch.

    ``load_state`` and ``reset_tick`` start a new observation epoch and drop the
    battle lifecycle, but the pending ``EndOfBattle`` sample was taken while the
    *previous* emulated instant was running.  If it survived, a later read that
    merely crosses the active->inactive transition would promote the old
    epoch's outcome -- reporting a battle end this epoch never observed.
    """
    session = _session()
    try:
        session._pyboy.memory[0xC000] = 2  # live link battle
        session._pyboy.memory[0xC001] = 1  # wBattleResult: player loss
        session.read_game_state()
        session._pyboy.fire(*_BATTLE_END_HOOK)
        if transition == "load":
            session.load_state(b"state loaded after EndOfBattle entry")
        else:
            session.reset_tick(0)
        # The new epoch is already past EndOfBattle entry, so its routine never
        # runs again and the pre-epoch sample must not be attributed to it.
        session._pyboy.memory[0xC001] = 0
        assert session.read_game_state().battle.terminal_result is None
        session._pyboy.memory[0xC000] = 0
        ended = session.read_game_state()
        assert ended.battle is not None
        assert ended.battle.raw_battle_result == 0
        assert ended.battle.terminal_result is None
    finally:
        session.close()


def test_read_game_state_blackout_zero_does_not_confirm_win():
    session = _session()
    session._pyboy.memory[0xC000] = 2
    session._pyboy.memory[0xC001] = 1  # player-faint handler ran while active
    session.read_game_state()
    # ResetStatusAndHalveMoneyOnBlackout zeroes both bytes before the
    # overworld read; the captured faint byte cannot be turned into a win.
    session._pyboy.memory[0xC000] = 0
    session._pyboy.memory[0xC001] = 0
    ended = session.read_game_state()
    assert ended.battle is not None
    assert ended.battle.phase is BattlePhase.TERMINAL_RETURN
    assert ended.battle.terminal_result is None


def test_read_game_state_escape_zero_does_not_confirm_win():
    session = _session()
    session._pyboy.memory[0xC000] = 2
    session._pyboy.memory[0xC006] = 1  # wEscapedFromBattle observed while active
    session.read_game_state()
    session._pyboy.memory[0xC000] = 0
    session._pyboy.memory[0xC001] = 0
    session._pyboy.memory[0xC006] = 0
    ended = session.read_game_state()
    assert ended.battle is not None
    assert ended.battle.phase is BattlePhase.TERMINAL_RETURN
    assert ended.battle.terminal_result is None


def test_load_state_resets_battle_lifecycle():
    session = _session()
    session._pyboy.memory[0xC000] = 2  # active battle observed
    assert session.read_game_state().battle is not None
    # The loaded state is an overworld snapshot; its zero must not be
    # qualified by the pre-load active-battle history.
    session._pyboy.memory[0xC000] = 0
    session._pyboy.memory[0xC001] = 0
    payload = base64.b64encode(b"OVERWORLD").decode("ascii")
    dispatch_tool(session, "load_state", {"data": payload})
    loaded = session.read_game_state()
    assert loaded.battle is not None
    assert loaded.battle.phase is BattlePhase.INACTIVE
    assert loaded.battle.phase_valid is True
    assert loaded.battle.terminal_result is None


def test_reset_tick_resets_battle_lifecycle():
    session = _session()
    session._pyboy.memory[0xC000] = 2
    session.read_game_state()
    session._pyboy.memory[0xC000] = 0
    session._pyboy.memory[0xC001] = 0
    session.reset_tick(0)
    loaded = session.read_game_state()
    assert loaded.battle is not None
    assert loaded.battle.phase is BattlePhase.INACTIVE
    assert loaded.battle.terminal_result is None


class _BlockingMemory(DictMemory):
    """DictMemory that parks the first read of ``block_addr`` on an event."""

    def __init__(
        self, *, block_addr: int, entered: threading.Event, release: threading.Event
    ) -> None:
        super().__init__()
        self._block_addr = block_addr
        self._entered = entered
        self._release = release
        self._blocked = False

    def __getitem__(self, key):
        if key == self._block_addr and not self._blocked:
            self._blocked = True
            self._entered.set()
            if not self._release.wait(timeout=5):
                raise AssertionError("snapshot parse was never released")
        return super().__getitem__(key)


def test_state_snapshot_holds_owner_lock_across_parse_and_epoch():
    entered = threading.Event()
    release = threading.Event()
    memory = _BlockingMemory(block_addr=0xC000, entered=entered, release=release)
    session = Session(pyboy=FakePyBoy(memory), symbols=_symbols(), event_bus=EventBus())
    captured: dict[str, object] = {}

    def snapshot() -> None:
        captured["snapshot"] = session.read_state_snapshot()

    def step() -> None:
        session.step(1)
        captured["stepped"] = True

    snapshot_thread = threading.Thread(target=snapshot)
    snapshot_thread.start()
    assert entered.wait(timeout=5)
    step_thread = threading.Thread(target=step)
    step_thread.start()
    # The owner lock is held across parsing and epoch capture, so a
    # concurrent step cannot advance the tick mid-snapshot.
    assert "stepped" not in captured
    release.set()
    snapshot_thread.join(timeout=5)
    step_thread.join(timeout=5)
    assert not snapshot_thread.is_alive() and not step_thread.is_alive()
    result = captured["snapshot"]
    assert result.epoch.tick == 0
    assert result.epoch.session_id == session.session_id


def test_resource_specs_advertise_state_epoch():
    names = {spec.name for spec in _resource_specs()}
    assert "State Epoch" in names
    uris = {str(spec.uri) for spec in _resource_specs()}
    assert "pokered://state-epoch" in uris
