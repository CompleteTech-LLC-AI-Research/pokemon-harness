"""Shared constants and helpers for the split battle-state test modules.

Split from ``tests/test_mcp_battle_state.py`` (#122) with no behavior change.
The synthetic symbol text, the enemy-struct writers, and the ``_session``
factory moved here verbatim so both collected modules keep every original node
ID while neither file reaches the 1000-line ceiling.
"""

from __future__ import annotations

from pathlib import Path

from pokered_harness.events import EventBus
from pokered_harness.session import Session
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
0F:43BD HandlePoisonBurnLeechSeed
0F:4525 HandleEnemyMonFainted
0F:4700 HandlePlayerMonFainted
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
# The first two are the ordinary ``Execute*MoveDone`` tails; the rest are the
# post-move continuations a status move (``JumpMoveEffect``) or a lethal hit
# (the direct faint return) reaches instead of those tails.
_RESOLUTION_CLOSE_HOOKS = (
    (0x0F, 0x580A),
    (0x0F, 0x688C),
    (0x0F, 0x43BD),
    (0x0F, 0x4700),
    (0x0F, 0x4525),
    (0x0F, 0x4233),
    (0x04, 0x77AA),
)

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

