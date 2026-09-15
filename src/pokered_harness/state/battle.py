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
closed and only reports a valid phase when *every* required evidence symbol
is present and the surviving signals agree.  Contradictory evidence yields
``None``/``phase_valid=False``; missing evidence or an all-clear (but
ambiguous) snapshot yields :attr:`BattlePhase.UNKNOWN`/``phase_valid=False``.
The exact symbols consulted are exposed through ``phase_evidence``.

The raw ``wBattleResult`` byte is exposed as
:attr:`BattleState.raw_battle_result`; it is never promoted to a confirmed
:attr:`BattleState.terminal_result` on its own.  The engine writes zero at
battle start (``InitBattleVariables``), on ``EnemyRan``, and again when a
blackout is processed (``ResetStatusAndHalveMoneyOnBlackout``), so zero is
ambiguous; positive bytes can also be overwritten in an ongoing battle.  A
confirmed outcome therefore also requires lifecycle evidence: the caller
tracks the previous observation in :class:`BattleLifecycle` and a terminal
result is only reported on the observed transition from an active battle to
``wIsInBattle == 0``.

The ambiguity is deliberate and documented in ``_derive_phase``: neither
``wMoveMenuType == 0`` (the regular-mode default written before every
``MoveSelectionMenu`` call) nor an all-zero flag block is positive evidence
that a menu is open or that the intro animation is playing, so neither
produces an observed phase.  :attr:`BattlePhase.INTRO` is retained for
schema compatibility but is never derived from the available symbols.
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
    means the battle is active but the available evidence was insufficient
    to name a phase (a required symbol was absent, or every signal was
    clear and therefore ambiguous); ``None`` means the evidence was
    contradictory or the battle kind itself was not decodable.  ``INTRO``
    is retained for schema compatibility but is never derived, because no
    symbol proves the intro animation is playing.
    """

    INACTIVE = 0
    INTRO = 1
    COMMAND_SELECTION = 2
    ACTION_RESOLUTION = 3
    FORCED_REPLACEMENT = 4
    TERMINAL_RETURN = 5
    UNKNOWN = 6


# Symbols consulted to derive :class:`BattlePhase`.  ``wIsInBattle`` is
# mandatory; the rest are required ROM-owned evidence.  Never hardcode
# their addresses.  If any is absent the phase is UNKNOWN (fail closed).
_PHASE_TRANSIENT_SYMBOLS = (
    "wBattleResult",
    "wInHandlePlayerMonFainted",
    "wMoveMenuType",
    "wPlayerMoveListIndex",
    "wActionResultOrTookBattleTurn",
)

# Valid ``wBattleResult`` outcomes (engine/battle/end_of_battle.asm):
# 0 player win, 1 player lose, 2 draw.  0 is also written at battle start
# (init_battle_variables.asm), at EnemyRan (core.asm), and when a blackout
# is processed (events/black_out.asm), so it is not terminal-only evidence;
# values 3..255 have no engine writer and are rejected.
_VALID_BATTLE_RESULTS = frozenset((0, 1, 2))


@dataclass(frozen=True, slots=True)
class BattleLifecycle:
    """Prior observation used to qualify a terminal battle outcome.

    ``wBattleResult`` alone cannot establish an outcome: the engine writes
    zero at battle start (``InitBattleVariables``), on ``EnemyRan``, and
    when a blackout is processed (``ResetStatusAndHalveMoneyOnBlackout``),
    and a positive byte written for a faint can be left behind even when the
    battle continues.  A terminal result is only reported when the caller
    has observed the battle *end*: ``was_active`` records whether the
    immediately preceding snapshot of the same emulator decoded
    ``wIsInBattle`` as a live wild or trainer battle.  Confirmation happens
    on the falling edge (previous snapshot active, current raw value zero).
    """

    was_active: bool = False


# ``wMoveMenuType`` is a mode selector, not an open/closed flag: 0 is written
# immediately before every regular ``MoveSelectionMenu`` call, and modes 1
# (mimic) and 2 (relearn/PP) are left set after ``MoveSelectionMenu`` returns
# (they persist through the subsequent animation/text).  No value therefore
# proves that move selection is *currently* active, so it is required as
# evidence (fail closed when absent) but never sufficient to name
# ``COMMAND_SELECTION``.


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
    raw_battle_result: int | None = None
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


def parse_battle(
    memory: MemoryLike,
    symbols: SymbolTable,
    *,
    lifecycle: BattleLifecycle | None = None,
) -> BattleState:
    raw = symbols.read_u8(memory, "wIsInBattle")
    kind = _safe_enum(BattleKind, raw)
    enemy_mon, enemy_mon_valid = _parse_enemy_mon(memory, symbols, kind=kind)
    raw_battle_result = _opt(memory, symbols, "wBattleResult")
    # A battle has ended only when a previously active battle now reports
    # wIsInBattle == 0.  Without that transition the raw result byte stays
    # unqualified (stated in BattleLifecycle).
    battle_ended = lifecycle is not None and lifecycle.was_active and raw == 0
    terminal_result = (
        raw_battle_result if battle_ended and raw_battle_result in _VALID_BATTLE_RESULTS else None
    )
    phase, phase_valid, phase_evidence = _derive_phase(
        memory, symbols, raw=raw, kind=kind, battle_ended=battle_ended
    )
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
        action_result_or_took_turn=_opt(memory, symbols, "wActionResultOrTookBattleTurn"),
        enemy_mon=enemy_mon,
        enemy_mon_valid=enemy_mon_valid,
        phase=phase,
        phase_valid=phase_valid,
        phase_evidence=phase_evidence,
        raw_battle_result=raw_battle_result,
        terminal_result=terminal_result,
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
    battle_ended: bool = False,
) -> tuple[BattlePhase | None, bool, tuple[str, ...]]:
    """Derive a battle phase candidate from ROM-owned observations.

    Returns ``(phase, phase_valid, evidence)``.  The derivation fails closed:

    * an inactive battle (``wIsInBattle == 0``) is ``INACTIVE``, unless
      ``battle_ended`` proves the battle just ended, in which case a valid
      ``wBattleResult`` yields ``TERMINAL_RETURN``;
    * an active battle whose ``wIsInBattle`` value is not a known kind is
      ``None`` (undecodable);
    * if any required evidence symbol is absent the result is
      ``UNKNOWN``/``False`` and no observed phase is reported;
    * two or more surviving signals yield ``None``/``False`` (contradiction);
    * one surviving signal yields that phase with ``phase_valid=True``;
    * no surviving signal yields ``UNKNOWN``/``False``.  An all-clear flag
      block is *not* enough to claim ``INTRO``: absence of the other flags
      does not prove the intro animation is playing.

    ``wBattleResult`` only emits ``TERMINAL_RETURN`` on the observed
    battle-end transition (``battle_ended``); its reset value 0 and its
    mid-battle writes are never terminal on their own.  ``wMoveMenuType``
    and ``wPlayerMoveListIndex`` are required evidence but never sufficient
    alone: no value proves move selection is currently active, so
    ``COMMAND_SELECTION`` is never derived.
    """
    if raw == 0:
        if battle_ended:
            if "wBattleResult" in symbols:
                result = symbols.read_u8(memory, "wBattleResult")
                if result in _VALID_BATTLE_RESULTS:
                    return (
                        BattlePhase.TERMINAL_RETURN,
                        True,
                        ("wIsInBattle", "wBattleResult"),
                    )
            return BattlePhase.UNKNOWN, False, ("wIsInBattle",)
        return BattlePhase.INACTIVE, True, ("wIsInBattle",)
    if kind is None:
        return None, False, ("wIsInBattle",)

    evidence = ["wIsInBattle"]
    if any(name not in symbols for name in _PHASE_TRANSIENT_SYMBOLS):
        evidence.extend(name for name in _PHASE_TRANSIENT_SYMBOLS if name in symbols)
        return BattlePhase.UNKNOWN, False, tuple(evidence)
    evidence.extend(_PHASE_TRANSIENT_SYMBOLS)

    forced = symbols.read_u8(memory, "wInHandlePlayerMonFainted") != 0
    action = symbols.read_u8(memory, "wActionResultOrTookBattleTurn") != 0

    observed: set[BattlePhase] = set()
    if forced:
        observed.add(BattlePhase.FORCED_REPLACEMENT)
    if action:
        observed.add(BattlePhase.ACTION_RESOLUTION)

    if len(observed) > 1:
        return None, False, tuple(evidence)
    if observed:
        return next(iter(observed)), True, tuple(evidence)
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
