"""Battle state parser.

Observes whether a battle is active, its class (wild vs trainer), the
sub-kind (normal / old-man tutorial / safari), the move-menu selection,
and turn gating. Battle sub-phase (intro animation vs command vs result
text vs forced switch vs move-learning prompt) is *not* a single byte in
pokered — that composition happens in the session layer via execution
hooks, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from pokered_harness.symbols.loader import MemoryLike, SymbolTable


class BattleKind(IntEnum):
    """Value of ``wIsInBattle``.

    The "0 none / 1 wild / 2 trainer" encoding is the community-canonical
    interpretation cited by every pokered-derived fork, though the ADR
    flagged that mainline pokered source comments should be reconfirmed
    against a built ``.sym`` before relying on the numeric values in
    critical control flow.
    """

    NONE = 0
    WILD = 1
    TRAINER = 2


class BattleType(IntEnum):
    """Value of ``wBattleType`` — ``BATTLE_TYPE_*`` constants in pokered."""

    NORMAL = 0
    OLD_MAN = 1
    SAFARI = 2


def _safe_enum(enum: type[IntEnum], value: int) -> IntEnum | None:
    try:
        return enum(value)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class BattleState:
    kind: BattleKind | None
    raw_is_in_battle: int
    battle_type: BattleType | None
    engaged_trainer_class: int | None
    engaged_trainer_set: int | None
    player_mon_slot: int | None
    move_menu_type: int | None
    player_selected_move: int | None
    enemy_selected_move: int | None
    action_result_or_took_turn: int | None

    @property
    def active(self) -> bool:
        return self.raw_is_in_battle != 0

    @property
    def is_trainer_battle(self) -> bool:
        return self.kind is BattleKind.TRAINER

    @property
    def is_wild_battle(self) -> bool:
        return self.kind is BattleKind.WILD

    @property
    def turn_already_consumed(self) -> bool:
        """``wActionResultOrTookBattleTurn`` non-zero means the player
        spent their turn via an item, switch, or run — the normal move
        menu will not appear this tick."""
        return bool(self.action_result_or_took_turn)


def parse_battle(memory: MemoryLike, symbols: SymbolTable) -> BattleState:
    raw = symbols.read_u8(memory, "wIsInBattle")
    return BattleState(
        kind=_safe_enum(BattleKind, raw),  # type: ignore[arg-type]
        raw_is_in_battle=raw,
        battle_type=_opt_enum(memory, symbols, "wBattleType", BattleType),
        engaged_trainer_class=_opt(memory, symbols, "wEngagedTrainerClass"),
        engaged_trainer_set=_opt(memory, symbols, "wEngagedTrainerSet"),
        player_mon_slot=_opt(memory, symbols, "wPlayerMonNumber"),
        move_menu_type=_opt(memory, symbols, "wMoveMenuType"),
        player_selected_move=_opt(memory, symbols, "wPlayerSelectedMove"),
        enemy_selected_move=_opt(memory, symbols, "wEnemySelectedMove"),
        action_result_or_took_turn=_opt(
            memory, symbols, "wActionResultOrTookBattleTurn"
        ),
    )


def _opt(memory: MemoryLike, symbols: SymbolTable, name: str) -> int | None:
    return symbols.read_u8(memory, name) if name in symbols else None


def _opt_enum(
    memory: MemoryLike,
    symbols: SymbolTable,
    name: str,
    enum: type[IntEnum],
) -> IntEnum | None:
    if name not in symbols:
        return None
    return _safe_enum(enum, symbols.read_u8(memory, name))
