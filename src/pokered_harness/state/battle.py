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

:attr:`BattlePhase.COMMAND_SELECTION` cannot be derived from RAM: there is
no "menu open" byte.  ``wMoveMenuType`` is a *mode selector* (0 regular, 1
mimic, 2 relearn/PP) that is written before every ``MoveSelectionMenu`` call
and left set after the menu closes, so it is required evidence but never
sufficient.  The menu descriptor bytes (`wCurrentMenuItem`, `wTopMenuItemY/X`,
`wMaxMenuItem`, `wMenuWatchedKeys`) and the draw buffers are also left in
place after the menu returns, and ``wMenuWrappingEnabled`` is cleared both on
exit *and* before entry, so none of them distinguishes an open menu from a
stale one.  The phase is therefore only reported when a
:class:`BattleMenuObservation` supplied by the session proves that an
execution hook fired on entry to the ROM routines that bracket the battle
command/move menu and has not since left them (see ``session.py``).  Without
that hook evidence the menu state is unknown and ``COMMAND_SELECTION`` is
never emitted.

The raw ``wBattleResult`` byte is exposed as
:attr:`BattleState.raw_battle_result`; it is never promoted to a confirmed
:attr:`BattleState.terminal_result` on its own.  The engine's writers split
into two classes (all verified against the pinned sources in
``asset-build-pokered``):

* zero writers — battle start (``InitBattleVariables``), a link
  ``EnemyRan`` (``core.asm``), every enemy-mon faint
  (``HandleEnemyMonFainted``), a blackout
  (``ResetStatusAndHalveMoneyOnBlackout``), and a fly/dungeon warp
  (``HandleFlyWarpOrDungeonWarp``);
* non-zero outcome writers — ``RemoveFaintedPlayerMon`` writes ``1`` when a
  player mon faints, ``TryRunningFromBattle`` writes ``2`` on a successful
  single-player run (``1``/``2`` in link play), and a successful capture
  writes ``2``.

A battle ends when a previously active ``wIsInBattle`` falls to zero
(:class:`BattleLifecycle`).  A *zero* at that falling edge is ambiguous:
``EndOfBattle`` leaves the byte untouched, so a win (``HandleEnemyMonFainted``
wrote ``0``), a blackout, a Roar/Whirlwind/Teleport escape, and a link
``EnemyRan`` are indistinguishable.  The byte is therefore only promoted to
:attr:`BattleState.terminal_result` when it is a non-zero outcome value that
survived teardown, and no escape evidence (``wEscapedFromBattle``) was
captured before cleanup.  A confirmed *win* is consequently not derivable
from the specified symbols and is reported as an unknown result rather than
fabricated.

The ambiguity is deliberate and documented in ``_derive_phase``: neither
``wMoveMenuType == 0`` (the regular-mode default written before every
``MoveSelectionMenu`` call) nor an all-zero flag block is positive evidence
that a menu is open or that the intro animation is playing, so neither
produces an observed phase.  :attr:`BattlePhase.INTRO` is retained for
schema compatibility but is never derived from the available symbols.

Beyond the phase candidate, :class:`BattleState` exposes the supported
transient bytes (raw move-menu mode, move-list index, selected moves, the
current menu cursor, the action/turn flag, and the player-faint handler
flag) and decoded Gen-1 stat stages for both combatants.  Every optional
field is ``None`` when its backing symbol is absent from the loaded ``.sym``;
a value is never guessed.  The stat-stage aggregate additionally reports a
``valid`` tri-state: ``False`` when a present byte has no engine writer and
``None`` when a required symbol is missing.
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


# ``w*MonStatMods`` store the Gen-1 stat modifier as ``stage + 7``: 1 is the
# -6 floor, 7 is neutral, and 13 is the +6 ceiling.  ``StatModifierUpEffect``
# and ``StatModifierDownEffect`` never write outside 1..13, so any other byte
# is reported as unknown rather than decoded into a guessed stage.
STAT_MOD_NEUTRAL = 7
STAT_MOD_MIN = 1
STAT_MOD_MAX = 13
_STAT_MOD_FIELDS = (
    ("attack", "AttackMod"),
    ("defense", "DefenseMod"),
    ("speed", "SpeedMod"),
    ("special", "SpecialMod"),
    ("accuracy", "AccuracyMod"),
    ("evasion", "EvasionMod"),
)


@dataclass(frozen=True, slots=True)
class StatStages:
    """Decoded Gen-1 stat stages for one combatant.

    Each field is the stage in ``-6..+6`` (``raw - 7``), or ``None`` when its
    ``w*Mon*Mod`` symbol is absent or holds a byte with no engine writer.
    ``valid`` is ``True`` only when every one of the six symbols is present
    and in range, ``False`` when a present byte is out of range, and ``None``
    when none is out of range but at least one symbol is absent.
    """

    attack: int | None = None
    defense: int | None = None
    speed: int | None = None
    special: int | None = None
    accuracy: int | None = None
    evasion: int | None = None
    valid: bool | None = None


@dataclass(frozen=True, slots=True)
class BattleMenuObservation:
    """Session execution-hook evidence for the battle command/move menu.

    ``open`` is the session-maintained entry/exit state of the ROM routines
    that bracket the battle menus (``True`` entered and not yet left,
    ``False`` observed closed, ``None`` unknown after a load/reset);
    ``evidence`` names the exact ROM labels whose execution was hooked.  There
    is no RAM byte that reports "menu open" (see the module docstring), so the
    observation is only available when the session installed the hooks and is
    never fabricated from a mode byte.
    """

    open: bool | None
    evidence: tuple[str, ...] = ()


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

# Raw ``wBattleResult`` bytes with an engine writer (end_of_battle.asm maps
# 0 to a win, 1 to a loss, and 2 to a draw in link battles).  0 is also
# written at battle start, on a link EnemyRan, on every enemy faint, on a
# blackout, and on a fly/dungeon warp, so a zero at the falling edge cannot
# establish an outcome; values 3..255 have no engine writer and are rejected.
_VALID_BATTLE_RESULTS = frozenset((0, 1, 2))

# Non-zero outcome bytes that survive teardown, so they can be confirmed on
# the active -> inactive transition.  A surviving 1 is the player-faint /
# run marker and a surviving 2 is the run/capture/draw marker; neither is a
# win.  Zero is deliberately excluded because it is ambiguous (see above).
_CONFIRMED_BATTLE_RESULTS = frozenset((1, 2))


@dataclass(frozen=True, slots=True)
class BattleLifecycle:
    """Prior observations used to qualify a terminal battle outcome.

    ``wBattleResult`` alone cannot establish an outcome: the engine writes
    zero at battle start (``InitBattleVariables``), on ``EnemyRan``, on
    every enemy faint, and when a blackout is processed
    (``ResetStatusAndHalveMoneyOnBlackout``), and a non-zero byte written for
    a faint can be left behind even when the battle continues.  A terminal
    result is only reported when the caller has observed the battle *end*:
    ``was_active`` records whether the immediately preceding snapshot of the
    same emulator decoded ``wIsInBattle`` as a live wild or trainer battle.
    Confirmation happens on the falling edge (previous snapshot active,
    current raw value zero), and only for a non-zero outcome byte that
    survived the teardown.

    ``escaped`` records outcome-specific ROM evidence captured before
    cleanup: ``wEscapedFromBattle`` set while a battle was active
    (``SwitchAndTeleportEffect`` / item escape).  An escape leaves or clears
    the result byte, so an outcome that coincides with it stays unknown.
    Both fields are cleared once the battle is no longer active so evidence
    cannot leak into a later encounter.
    """

    was_active: bool = False
    escaped: bool = False


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
    escaped_from_battle: int | None = None
    terminal_result: int | None = None
    player_move_list_index: int | None = None
    current_menu_item: int | None = None
    in_handle_player_mon_fainted: int | None = None
    player_stat_stages: StatStages | None = None
    enemy_stat_stages: StatStages | None = None
    menu_open: bool | None = None
    menu_evidence: tuple[str, ...] = ()

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
    menu: BattleMenuObservation | None = None,
) -> BattleState:
    raw = symbols.read_u8(memory, "wIsInBattle")
    kind = _safe_enum(BattleKind, raw)
    enemy_mon, enemy_mon_valid = _parse_enemy_mon(memory, symbols, kind=kind)
    raw_battle_result = _opt(memory, symbols, "wBattleResult")
    escaped_from_battle = _opt(memory, symbols, "wEscapedFromBattle")
    # A battle has ended only when a previously active battle now reports
    # wIsInBattle == 0.  Without that transition the raw result byte stays
    # unqualified (stated in BattleLifecycle).  A zero result at the edge is
    # never promoted: win, blackout, and escape all leave or clear zero, so
    # the outcome stays unknown unless a non-zero byte survived teardown and
    # no escape evidence was captured before cleanup.
    battle_ended = lifecycle is not None and lifecycle.was_active and raw == 0
    escaped = bool(escaped_from_battle) or (lifecycle.escaped if lifecycle is not None else False)
    terminal_result = (
        raw_battle_result
        if battle_ended and raw_battle_result in _CONFIRMED_BATTLE_RESULTS and not escaped
        else None
    )
    phase, phase_valid, phase_evidence = _derive_phase(
        memory,
        symbols,
        raw=raw,
        kind=kind,
        battle_ended=battle_ended,
        menu=menu,
    )
    in_battle = kind is BattleKind.WILD or kind is BattleKind.TRAINER
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
        escaped_from_battle=escaped_from_battle,
        terminal_result=terminal_result,
        player_move_list_index=_opt(memory, symbols, "wPlayerMoveListIndex"),
        current_menu_item=_opt(memory, symbols, "wCurrentMenuItem"),
        in_handle_player_mon_fainted=_opt(memory, symbols, "wInHandlePlayerMonFainted"),
        # Stat stages are only meaningful for a live battle mon; out of battle
        # the bytes are stale and must not be presented as live state.
        player_stat_stages=(
            _parse_stat_stages(memory, symbols, "wPlayerMon") if in_battle else None
        ),
        enemy_stat_stages=(_parse_stat_stages(memory, symbols, "wEnemyMon") if in_battle else None),
        menu_open=(menu.open if menu is not None else None),
        menu_evidence=(menu.evidence if menu is not None else ()),
    )


def _parse_stat_stages(
    memory: MemoryLike,
    symbols: SymbolTable,
    prefix: str,
) -> StatStages | None:
    """Decode one ``w*MonStatMods`` block into Gen-1 stages.

    Returns ``None`` when none of the six ``*Mod`` symbols is present, so an
    absent symbol family is distinguishable from a decoded neutral block.
    """
    if not any(prefix + suffix in symbols for _field, suffix in _STAT_MOD_FIELDS):
        return None
    stages: dict[str, int | None] = {}
    any_invalid = False
    any_missing = False
    for field, suffix in _STAT_MOD_FIELDS:
        name = prefix + suffix
        if name not in symbols:
            stages[field] = None
            any_missing = True
            continue
        raw_mod = symbols.read_u8(memory, name)
        if STAT_MOD_MIN <= raw_mod <= STAT_MOD_MAX:
            stages[field] = raw_mod - STAT_MOD_NEUTRAL
        else:
            stages[field] = None
            any_invalid = True
    if any_invalid:
        valid: bool | None = False
    elif any_missing:
        valid = None
    else:
        valid = True
    return StatStages(valid=valid, **stages)


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
    menu: BattleMenuObservation | None = None,
) -> tuple[BattlePhase | None, bool, tuple[str, ...]]:
    """Derive a battle phase candidate from ROM-owned observations.

    Returns ``(phase, phase_valid, evidence)``.  The derivation fails closed:

    * an inactive battle (``wIsInBattle == 0``) is ``INACTIVE``, unless
      ``battle_ended`` proves the battle just ended, in which case a valid
      ``wBattleResult`` yields ``TERMINAL_RETURN`` (the phase only names the
      terminal/return state; ``terminal_result`` separately stays ``None``
      when the byte is an ambiguous zero);
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
    alone: no value proves move selection is currently active.  The only
    positive source of ``COMMAND_SELECTION`` is a session hook observation
    (``menu``) that proves the menu routine was entered and not yet left.
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
        _extend_unique(evidence, menu.evidence if menu is not None else ())
        return BattlePhase.UNKNOWN, False, tuple(evidence)
    evidence.extend(_PHASE_TRANSIENT_SYMBOLS)
    _extend_unique(evidence, menu.evidence if menu is not None else ())

    forced = symbols.read_u8(memory, "wInHandlePlayerMonFainted") != 0
    action = symbols.read_u8(memory, "wActionResultOrTookBattleTurn") != 0
    menu_open = menu is not None and menu.open is True

    observed: set[BattlePhase] = set()
    if forced:
        observed.add(BattlePhase.FORCED_REPLACEMENT)
    if action:
        observed.add(BattlePhase.ACTION_RESOLUTION)
    if menu_open:
        observed.add(BattlePhase.COMMAND_SELECTION)

    if len(observed) > 1:
        return None, False, tuple(evidence)
    if observed:
        return next(iter(observed)), True, tuple(evidence)
    return BattlePhase.UNKNOWN, False, tuple(evidence)


def _extend_unique(target: list[str], extra: tuple[str, ...]) -> None:
    for name in extra:
        if name not in target:
            target.append(name)


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
