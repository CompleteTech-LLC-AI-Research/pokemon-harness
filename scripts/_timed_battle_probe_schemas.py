"""Shared immutable constants for the bounded timed battle probe (#159).

Split from ``scripts/_timed_battle_probe.py`` with no behavior change. The
original module re-exports these names so its public surface is unchanged.
"""

from __future__ import annotations

PROVENANCE = "unknown historical provenance; pristine acquisition unverified"
READINESS_PHASES = (
    "party_qualified",
    "link_menu_colosseum_ready",
    "colosseum_reached",
    "battle_menu_ready",
    "party_inspected",
    "party_cancelled",
    "move_ready",
    "settled_turn",
    "room_returned",
)
PHASES = READINESS_PHASES
SUPPORTED_EFFECTS = frozenset((0, 6, 44))
# pret data/moves/moves.asm: all Gen-I damage moves in the supported effects.
MOVE_EFFECTS = dict.fromkeys(
    (
        1,
        2,
        5,
        10,
        11,
        15,
        16,
        17,
        21,
        22,
        25,
        30,
        33,
        55,
        56,
        57,
        64,
        65,
        68,
        70,
        75,
        88,
        89,
        98,
        121,
        127,
        146,
        152,
        157,
        161,
        163,
    ),
    0,
)
MOVE_EFFECTS.update({9: 6, 84: 6, 85: 6, 87: 6, 24: 44, 155: 44})
UNSUPPORTED_MOVES = {68: "Counter special return path is not qualified"}
VERIFICATION_SYMBOLS = (
    "SelectEnemyMove",
    "LoadScreenTilesFromBuffer1",
    "FullyParalyzedText",
    "PrintText",
    "CheckPlayerStatusConditions.MonHurtItselfOrFullyParalysed",
    "CheckEnemyStatusConditions.monHurtItselfOrFullyParalysed",
)
OBSERVATION_SYMBOLS = (
    "SaveGameData",
    "Serial_SyncAndExchangeNybble",
    "LinkMenu.waitForInputLoop",
    "LinkMenu.doneChoosingMenuSelection",
    "PrepareForSpecialWarp",
    "CableClubLeftGameboy",
    "CableClubRightGameboy",
    "CableClub_DoBattleOrTrade",
    "MainInBattleLoop",
    "DisplayBattleMenu",
    "HandleMenuInput",
    "HandlePartyMenuInput",
    "MoveSelectionMenu",
    "LinkBattleExchangeData",
    "ExecutePlayerMove",
    "ExecuteEnemyMove",
    "PlayerCanExecuteMove",
    "EnemyCanExecuteMove",
    "ExecutePlayerMoveDone",
    "ExecuteEnemyMoveDone",
    "ApplyDamageToEnemyPokemon",
    "ApplyDamageToPlayerPokemon",
    "ApplyAttackToEnemyPokemonDone",
    "ApplyAttackToPlayerPokemonDone",
    "DecrementPP",
    "HandlePlayerMonFainted",
    "HandleEnemyMonFainted",
    "EndOfBattle",
    "ReturnToCableClubRoom",
)
MENU_FIELDS = (
    "wCurMap",
    "wLinkState",
    "wIsInBattle",
    "hSerialConnectionStatus",
    "wCurrentMenuItem",
    "wMaxMenuItem",
    "wMenuWatchedKeys",
    "wTopMenuItemX",
    "wTopMenuItemY",
    "wPartyMenuTypeOrMessageID",
    "wPlayerMoveListIndex",
    "wPlayerSelectedMove",
    "wEnemySelectedMove",
    "wSerialExchangeNybbleSendData",
    "wSerialExchangeNybbleReceiveData",
    "wBattleResult",
    "hWhoseTurn",
    "wPlayerDisabledMove",
    "wMoveMissed",
    "wDamage",
    "wXCoord",
    "wYCoord",
    "wSpritePlayerStateData1FacingDirection",
    "wJoyIgnore",
    "wWalkCounter",
    "wStatusFlags5",
)
HIDDEN_EVENT_INPUT_LIMIT = 6
