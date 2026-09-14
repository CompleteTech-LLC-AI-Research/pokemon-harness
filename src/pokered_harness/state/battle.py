"""Battle state parser.

Observes whether a battle is active, its class (wild vs trainer), the
sub-kind (normal / old-man tutorial / safari), the move-menu selection,
turn gating, the enemy combatant, and a derived battle phase.

Battle sub-phase (intro animation vs command vs result text vs forced
switch vs move-learning prompt) is *not* a single byte in pokered.  This
module therefore derives a best-effort :class:`BattlePhase` candidate from
ROM-owned observations (``wIsInBattle``, ``wMoveMenuType``,
``wPlayerMoveListIndex``, ``wActionResultOrTookBattleTurn``,
``wInHandlePlayerMonFainted`` and ``wBattleResult``).  The derivation fails
closed: when the evidence is contradictory the phase is ``None``, when a
required symbol is absent it is :attr:`BattlePhase.UNKNOWN`, and
``phase_valid`` is ``False`` in both cases.  The exact symbols consulted are
exposed through ``phase_evidence`` so callers can audit the decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from pokered_harness.state.party import (
    MAX_PARTY_SLOTS,
    PartyMon,
    parse_battle_combatant,
)
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


class BattlePhase(IntEnum):
    """Best-effort derived battle sub-phase.

    pokered has no single authoritative sub-phase byte, so this is a
    candidate derived from several ROM-owned observations.  ``UNKNOWN``
    means the battle is active but the required evidence symbols were not
    all present; ``None`` means the evidence was contradictory or the
    battle kind itself was not decodable.
    """

    INACTIVE = 0
    INTRO = 1
    COMMAND_SELECTION = 2
    ACTION_RESOLUTION = 3
    FORCED_REPLACEMENT = 4
    TERMINAL_RETURN = 5
    UNKNOWN = 6


# Symbols consulted to derive :class:`BattlePhase`.  ``wIsInBattle`` is
# mandatory; the rest are ROM-owned flags.  Never hardcode their addresses.
_PHASE_TRANSIENT_SYMBOLS = (
    "wBattleResult",
    "wInHandlePlayerMonFainted",
    "wMoveMenuType",
    "wPlayerMoveListIndex",
    "wActionResultOrTookBattleTurn",
)


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
    enemy_mon: PartyMon | None = None
    enemy_mon_valid: bool | None = None
    phase: BattlePhase | None = None
    phase_valid: bool | None = None
    phase_evidence: tuple[str, ...] = ()
    terminal_result: int | None = None

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
    kind = _safe_enum(BattleKind, raw)
    enemy_mon, enemy_mon_valid = _parse_enemy_mon(memory, symbols, kind=kind)
    phase, phase_valid, phase_evidence = _derive_phase(memory, symbols, raw=raw, kind=kind)
    return BattleState(
        kind=kind,  # type: ignore[arg-type]
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
        enemy_mon=enemy_mon,
        enemy_mon_valid=enemy_mon_valid,
        phase=phase,
        phase_valid=phase_valid,
        phase_evidence=phase_evidence,
        terminal_result=_opt(memory, symbols, "wBattleResult"),
    )


def _parse_enemy_mon(
    memory: MemoryLike,
    symbols: SymbolTable,
    *,
    kind: IntEnum | None,
) -> tuple[PartyMon | None, bool | None]:
    """Parse the opposing combatant through its named ``wEnemyMon`` symbols.

    A wild battle has no meaningful party slot, so its ``enemy_mon_valid``
    is ``None`` (unknown) rather than a fabricated ``slot == 0``; the
    observed fields are still exposed when the symbols exist.  A trainer
    battle additionally requires ``wEnemyMonPartyPos`` in ``0..5``.  An
    inactive or undecodable battle leaves both values unknown.
    """
    if kind is not BattleKind.WILD and kind is not BattleKind.TRAINER:
        return None, None
    if "wEnemyMonPartyPos" not in symbols:
        return None, False
    slot = symbols.read_u8(memory, "wEnemyMonPartyPos")
    mon = parse_battle_combatant(memory, symbols, "wEnemyMon", slot=slot)
    if mon is None:
        return None, False
    if kind is BattleKind.WILD:
        return mon, None
    return mon, mon.valid is True and 0 <= slot < MAX_PARTY_SLOTS


def _derive_phase(
    memory: MemoryLike,
    symbols: SymbolTable,
    *,
    raw: int,
    kind: IntEnum | None,
) -> tuple[BattlePhase | None, bool, tuple[str, ...]]:
    """Derive a battle phase candidate from ROM-owned observations.

    Returns ``(phase, phase_valid, evidence)``.  Contradictory flags yield
    ``(None, False, evidence)``; a missing evidence symbol yields
    ``(BattlePhase.UNKNOWN, False, evidence)``; an active battle whose
    ``wIsInBattle`` value is not a known kind also yields ``None``.
    """
    if raw == 0:
        return BattlePhase.INACTIVE, True, ("wIsInBattle",)
    if kind is None:
        return None, False, ("wIsInBattle",)

    evidence = ["wIsInBattle"]

    def _flag(name: str) -> bool | None:
        if name not in symbols:
            return None
        evidence.append(name)
        return symbols.read_u8(memory, name) != 0

    terminal = _flag("wBattleResult")
    forced = _flag("wInHandlePlayerMonFainted")
    action = _flag("wActionResultOrTookBattleTurn")
    menu = _flag("wMoveMenuType")
    move_index = _flag("wPlayerMoveListIndex")

    # Distinct candidate phases.  ``wMoveMenuType`` is authoritative for the
    # command menu; the move-list index is a fallback only when the primary
    # menu byte is unavailable.  Two signals for the same phase are not a
    # contradiction.
    observed: set[BattlePhase] = set()
    if terminal:
        observed.add(BattlePhase.TERMINAL_RETURN)
    if forced:
        observed.add(BattlePhase.FORCED_REPLACEMENT)
    if action:
        observed.add(BattlePhase.ACTION_RESOLUTION)
    if menu is True or (menu is None and move_index is True):
        observed.add(BattlePhase.COMMAND_SELECTION)

    if len(observed) > 1:
        return None, False, tuple(evidence)
    if observed:
        return next(iter(observed)), True, tuple(evidence)
    if all(name in symbols for name in _PHASE_TRANSIENT_SYMBOLS):
        return BattlePhase.INTRO, True, tuple(evidence)
    return BattlePhase.UNKNOWN, False, tuple(evidence)


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
