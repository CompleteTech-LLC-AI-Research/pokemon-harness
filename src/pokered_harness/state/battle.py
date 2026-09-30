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

from dataclasses import dataclass, replace
from enum import IntEnum

from pokered_harness.state.party import (
    MAX_PARTY_SLOTS,
    PARTY_STRUCT_SIZE,
    PartyMon,
    parse_battle_combatant,
)
from pokered_harness.symbols.loader import MemoryLike, SymbolTable, read_wram_u8


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


@dataclass(frozen=True, slots=True)
class BattleResolutionObservation:
    """Session execution-hook evidence for ROM move execution.

    ``open`` is ``True`` while the ROM is inside its move-execution routines
    (``ExecutePlayerMove`` / ``ExecuteEnemyMove``) and has not yet reached the
    matching ``*Done`` exit; ``None`` when the hooks are not installed (so
    resolution is unknown) or after a load/reset, which invalidates the
    observation.  ``evidence`` names the exact ROM labels whose execution was
    hooked.

    ``wActionResultOrTookBattleTurn`` cannot report an ordinary FIGHT move:
    ``ExecutePlayerMoveDone`` clears it to zero as it returns, so a client
    polling at any interval only ever sees the flag set for the item / switch
    / run turns that never execute a move.  The hook is the ROM-owned proof
    that a move is being resolved right now, so it is the only source of
    :attr:`BattlePhase.ACTION_RESOLUTION` for a normal attack turn.
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

# The ROM's own live signal for "the player is choosing a replacement right
# now" is the battle party menu.  ``ChooseNextMon`` writes
# ``BATTLE_PARTY_MENU`` (2) to ``wPartyMenuTypeOrMessageID`` and calls
# ``DisplayPartyMenu``, whose input loop (``HandlePartyMenuInput``, reached
# from ``DisplayPartyMenu``) raises ``wPartyMenuAnimMonEnabled`` to ``$40``
# for as long as the menu is awaiting input and clears it on the way out
# (``home/pokemon.asm``).  ``$40`` is the only non-zero value any engine
# writer stores there, so the pair is live menu evidence rather than a stale
# leftover: the type byte alone survives the menu, the animation flag does
# not.
#
# ``BATTLE_PARTY_MENU`` has exactly two in-battle writers in ``core.asm``:
# ``ChooseNextMon`` (:1086, the forced replacement after a faint or a forced
# switch) and the trainer shift-in prompt in ``EnemySendOutFirstMon``
# (:1389, "about to use X, will you switch?").  Both are the player being
# made to pick a party mon mid-battle, which is the phase's contract.  The
# *voluntary* battle-menu switch and the Run->party path store
# ``NORMAL_PARTY_MENU`` (0) at :2316/:2335 instead, and the optional
# shift-in prompt is skipped entirely in a link battle (``wLinkState``
# check at :1372), so neither is reported here.
#
# The faint flag and the zeroed combatant are *not* sufficient and are not
# equivalent to this signal.  ``wInHandlePlayerMonFainted`` is set by
# ``HandlePlayerMonFainted`` and cleared only by ``HandleEnemyMonFainted``
# (``core.asm``), so it reads stale at a healthy command boundary; and both
# paths reach ``ChooseNextMon``, including the enemy-faint path that *clears*
# the flag before opening the menu, so a real replacement can have the flag at
# zero.  Conversely the final-faint path sets the flag, zeroes the combatant,
# and then jumps to ``HandlePlayerBlackOut``/``TrainerBattleVictory`` without
# ever opening the menu.  Reading the menu itself separates those cases, which
# the flag triple cannot.
#
# ``AnyPartyAlive`` corroborates: ``HandlePlayerMonFainted`` jumps to
# ``HandlePlayerBlackOut`` when nothing survives, so a live party menu with a
# proven all-fainted party is contradictory and must not be reported as a
# replacement.  The party is read from the linker's own ``wPartyMon1HP`` block
# so a banked-out ``0xD000`` window cannot hide it.
_PARTY_MENU_TYPE_SYMBOL = "wPartyMenuTypeOrMessageID"
_PARTY_MENU_ANIM_SYMBOL = "wPartyMenuAnimMonEnabled"
# ``BATTLE_PARTY_MENU`` in ``constants/menu_constants.asm``.
_BATTLE_PARTY_MENU_TYPE = 2
# ``HandlePartyMenuInput`` stores ``$40``; ``HandleMenuInput`` and the
# ``HandlePartyMenuInput`` exit path store 0.
_PARTY_MENU_ANIM_ACTIVE = 0x40
_PARTY_COUNT_SYMBOL = "wPartyCount"
_PARTY_HP_SYMBOL = "wPartyMon1HP"

# Symbols the replacement decision consults.  They are reported through
# ``phase_evidence`` so a caller can see exactly what the derivation relied on.
_FORCED_REPLACEMENT_EVIDENCE_SYMBOLS = (
    _PARTY_MENU_TYPE_SYMBOL,
    _PARTY_MENU_ANIM_SYMBOL,
    _PARTY_COUNT_SYMBOL,
    _PARTY_HP_SYMBOL,
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

    ``end_observed`` records that the session watched the ROM's own
    ``EndOfBattle`` routine for this battle instead of inferring the end from
    a client-timed reading.  A client can poll at any interval, so an escape
    that happens entirely between two reads is invisible to the reader: the
    engine clears ``wEscapedFromBattle`` on its way out and the stale result
    byte from an *earlier* faint in the same battle survives.  Without the
    ROM-observed end the result byte cannot be attributed to this end, so
    promotion stays disabled.

    ``end_result`` and ``end_escaped`` are the ``wBattleResult`` and
    ``wEscapedFromBattle`` bytes sampled at that ``EndOfBattle`` entry, which is
    the last instant both are still meaningful: ``EndOfBattle`` itself clears
    the escape flag, and the outcome byte is only ever written for the event
    that ended the battle.  Promotion uses ``end_result`` rather than a later
    read of the same address, so a byte overwritten between the ROM's end and
    the client's next read cannot be misattributed.

    All fields are cleared once the battle is no longer active so evidence
    cannot leak into a later encounter.
    """

    was_active: bool = False
    escaped: bool = False
    end_observed: bool = False
    end_result: int | None = None
    end_escaped: bool = False


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
    resolution_open: bool | None = None
    resolution_evidence: tuple[str, ...] = ()

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
    resolution: BattleResolutionObservation | None = None,
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
    # An outcome is only promoted from evidence the ROM itself produced for
    # *this* battle end.  ``EndOfBattle`` is the last instant the escape flag
    # and the result byte are both meaningful, and the session samples both at
    # its entry (``lifecycle.end_observed``).  Without that sample a client
    # polling interval can hide an entire escape -- ``EndOfBattle`` clears
    # ``wEscapedFromBattle`` on the way out and never touches
    # ``wBattleResult``, so a stale byte written for an earlier faint in the
    # same battle would otherwise be attributed to this end.  Absent the
    # ROM-observed end the outcome stays unknown rather than guessed.
    if battle_ended and lifecycle is not None and lifecycle.end_observed:
        end_escaped = escaped or lifecycle.end_escaped
        end_result = lifecycle.end_result
        terminal_result = (
            end_result if end_result in _CONFIRMED_BATTLE_RESULTS and not end_escaped else None
        )
    else:
        terminal_result = None
    # A live battle menu is proof the move routine has already returned: both
    # the command menu (``MainInBattleLoop`` -> ``DisplayBattleMenu`` /
    # ``SelectMenuItem``) and the replacement menu (``ChooseNextMon`` ->
    # ``DisplayPartyMenu``) are only reached from the post-move continuation.
    # The bracket is a control-flow observation, so it must not be reported
    # open once that continuation has run, even if a close hook was missed.
    resolution = _resolution_bracket_ended_by_menu(
        memory, symbols, menu=menu, resolution=resolution
    )
    phase, phase_valid, phase_evidence = _derive_phase(
        memory,
        symbols,
        raw=raw,
        kind=kind,
        battle_ended=battle_ended,
        menu=menu,
        resolution=resolution,
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
        resolution_open=(resolution.open if resolution is not None else None),
        resolution_evidence=(resolution.evidence if resolution is not None else ()),
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

    ``None`` means the evidence is unavailable: an inactive or undecodable
    battle, an absent ``wEnemyMon*`` symbol family, or a missing
    ``wEnemyMonPartyPos``.  ``False`` is reserved for an *observed* invalid
    value -- a present slot outside ``0..5``, or a combatant whose own
    validity check failed -- so a client can never mistake "the harness
    could not read this" for "the harness read it and it is wrong".
    """
    if kind is not BattleKind.WILD and kind is not BattleKind.TRAINER:
        return None, None
    if "wEnemyMonPartyPos" not in symbols:
        return None, None
    slot = symbols.read_u8(memory, "wEnemyMonPartyPos")
    mon = parse_battle_combatant(memory, symbols, "wEnemyMon", slot=slot)
    if mon is None:
        return None, None
    if kind is BattleKind.WILD:
        return mon, None
    return mon, mon.valid is True and 0 <= slot < MAX_PARTY_SLOTS


def _resolution_bracket_ended_by_menu(
    memory: MemoryLike,
    symbols: SymbolTable,
    *,
    menu: BattleMenuObservation | None,
    resolution: BattleResolutionObservation | None,
) -> BattleResolutionObservation | None:
    """Close a move-execution bracket the ROM has already left.

    A live battle menu is proof that the move routine returned: the command
    menu is entered from ``MainInBattleLoop``, which is only reached after
    ``ExecutePlayerMove`` / ``ExecuteEnemyMove`` has returned (a status move
    returns through ``JumpMoveEffect`` and a lethal hit returns straight to
    the faint check, so neither necessarily reaches ``Execute*MoveDone``), and
    the replacement menu is opened by ``ChooseNextMon`` from the faint
    continuations that run after that same return.  ``ChooseNextMon`` and
    ``DisplayPartyMenu`` report their wait through RAM
    (:func:`_battle_party_menu_is_live`), not through a hook, so a bracket left
    open by a missed close label is reconciled here rather than reported as
    contradicting the menu.  The bracket stays untouched while the ROM is
    genuinely inside a move, so a later command or replacement menu still
    cannot be mistaken for resolution.
    """
    if resolution is None or resolution.open is not True:
        return resolution
    if menu is not None and menu.open is True:
        return replace(resolution, open=False)
    if _battle_party_menu_is_live(memory, symbols) is True:
        return replace(resolution, open=False)
    return resolution


def _derive_phase(
    memory: MemoryLike,
    symbols: SymbolTable,
    *,
    raw: int,
    kind: IntEnum | None,
    battle_ended: bool = False,
    menu: BattleMenuObservation | None = None,
    resolution: BattleResolutionObservation | None = None,
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

    ``wInHandlePlayerMonFainted`` alone never proves ``FORCED_REPLACEMENT``:
    the ROM sets it in ``HandlePlayerMonFainted`` and clears it only in
    ``HandleEnemyMonFainted``, so it stays set after ``ChooseNextMon`` returns
    to the battle loop and reads stale at a healthy command boundary.  The
    flag is also *cleared* on the enemy-faint path before that path calls
    ``ChooseNextMon``, so the byte is absent during a genuine replacement as
    well.  The phase therefore requires the ROM's own current-replacement
    signal: the battle party menu itself
    (``wPartyMenuTypeOrMessageID == BATTLE_PARTY_MENU`` while
    ``wPartyMenuAnimMonEnabled == $40``), which only ``ChooseNextMon``'s
    ``DisplayPartyMenu`` raises and only its exit clears.  That also excludes
    the final-faint path, which reaches ``HandlePlayerBlackOut`` /
    ``TrainerBattleVictory`` without opening the menu, and the enemy-faint
    path's simultaneous double knockout, which does open it.

    ``wBattleResult`` only emits ``TERMINAL_RETURN`` on the observed
    battle-end transition (``battle_ended``); its reset value 0 and its
    mid-battle writes are never terminal on their own.  ``wMoveMenuType``
    and ``wPlayerMoveListIndex`` are required evidence but never sufficient
    alone: no value proves move selection is currently active.  The only
    positive source of ``COMMAND_SELECTION`` is a session hook observation
    (``menu``) that proves the menu routine was entered and not yet left.

    ``ACTION_RESOLUTION`` has two sources.  A non-zero
    ``wActionResultOrTookBattleTurn`` proves an item / switch / run turn
    consumed the action, but an ordinary FIGHT move is invisible in RAM:
    ``ExecutePlayerMoveDone`` clears the flag to zero on the way out, so any
    client polling interval can miss it.  The session's move-execution hook
    observation (``resolution``) is the ROM-owned proof that the engine is
    inside ``ExecutePlayerMove``/``ExecuteEnemyMove`` right now.
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

    forced = _forced_replacement_is_current(memory, symbols)
    # The replacement decision consults the party menu and the party block as
    # well as the faint flag, so name those symbols whenever the derivation
    # actually looked at them.  A caller can then see whether the live-menu
    # signal and the living-party corroboration were applied.
    if (
        symbols.read_u8(memory, "wInHandlePlayerMonFainted") != 0
        or _battle_party_menu_is_live(memory, symbols) is True
    ):
        _extend_unique(
            evidence,
            tuple(name for name in _FORCED_REPLACEMENT_EVIDENCE_SYMBOLS if name in symbols),
        )
    action = symbols.read_u8(memory, "wActionResultOrTookBattleTurn") != 0
    menu_open = menu is not None and menu.open is True
    resolving = resolution is not None and resolution.open is True
    if resolving:
        _extend_unique(
            evidence,
            tuple(resolution.evidence) if resolution is not None else (),
        )

    observed: set[BattlePhase] = set()
    if forced is True:
        observed.add(BattlePhase.FORCED_REPLACEMENT)
    if action or resolving:
        observed.add(BattlePhase.ACTION_RESOLUTION)
    if menu_open:
        observed.add(BattlePhase.COMMAND_SELECTION)

    # ``None`` means the faint flag is set but the evidence cannot say whether
    # the replacement is current, so no observed phase may be reported.
    if forced is None:
        return None, False, tuple(evidence)
    if len(observed) > 1:
        return None, False, tuple(evidence)
    if observed:
        return next(iter(observed)), True, tuple(evidence)
    return BattlePhase.UNKNOWN, False, tuple(evidence)


def _forced_replacement_is_current(memory: MemoryLike, symbols: SymbolTable) -> bool | None:
    """Whether a forced player replacement is happening *right now*.

    The decisive signal is the ROM's battle party menu
    (see :func:`_battle_party_menu_is_live`), because it is the only evidence
    that positively identifies the ``ChooseNextMon`` input wait.  The faint
    flag alone cannot: ``wInHandlePlayerMonFainted`` is *set* on the
    player-faint path and cleared only by the enemy-faint path, so it reads
    stale after the menu closes, and it is *cleared* on the enemy-faint path
    before that same path calls ``ChooseNextMon`` -- so a genuine replacement
    can also have the flag at zero.  The zeroed combatant alone cannot
    either: the final faint zeroes ``wBattleMonHP`` and then branches to
    ``HandlePlayerBlackOut`` / ``TrainerBattleVictory`` without opening a menu.

    When the menu is live, ``AnyPartyAlive`` corroborates: the ROM jumps to
    ``HandlePlayerBlackOut`` instead of opening the menu when no member
    survives, so a live menu over a proven all-fainted party is contradictory
    and is not reported as a replacement.

    Returns ``True``/``False`` when the symbols decide it, and ``None`` when
    the faint handler ran but the menu evidence needed to resolve the question
    is absent -- the caller must then fail closed rather than assert a
    replacement it cannot prove.
    """
    live = _battle_party_menu_is_live(memory, symbols)
    if live is None:
        # The menu cannot be read.  A raised faint flag means the ROM may be
        # waiting on ``ChooseNextMon`` right now, so the caller must not claim
        # either answer.
        if symbols.read_u8(memory, "wInHandlePlayerMonFainted") != 0:
            return None
        return False
    if live is False:
        return False
    living = _party_has_living_member(memory, symbols)
    if living is None:
        return None
    return living


def _battle_party_menu_is_live(memory: MemoryLike, symbols: SymbolTable) -> bool | None:
    """Whether the ROM's battle party menu is open and awaiting input.

    ``ChooseNextMon`` writes ``BATTLE_PARTY_MENU`` to
    ``wPartyMenuTypeOrMessageID`` and calls ``DisplayPartyMenu``; the menu's
    input loop raises ``wPartyMenuAnimMonEnabled`` to ``$40`` while it awaits
    input and clears it on exit (``home/pokemon.asm``).  The type byte left
    behind after the menu closes is stale, so both are required: the type
    proves *which* menu, the animation flag proves it is *live*.  The only
    other in-battle writer of the type byte is the trainer shift-in prompt
    (``EnemySendOutFirstMon``), which is also a mid-battle party selection
    and is skipped in link battles; the voluntary switch and Run->party
    paths store ``NORMAL_PARTY_MENU`` (0) and so are never matched.

    Returns ``None`` when either symbol is absent, so a caller can
    distinguish "the harness cannot read the menu" from "the menu is shut".
    """
    if _PARTY_MENU_TYPE_SYMBOL not in symbols or _PARTY_MENU_ANIM_SYMBOL not in symbols:
        return None
    if symbols.read_u8(memory, _PARTY_MENU_TYPE_SYMBOL) != _BATTLE_PARTY_MENU_TYPE:
        return False
    return symbols.read_u8(memory, _PARTY_MENU_ANIM_SYMBOL) == _PARTY_MENU_ANIM_ACTIVE


def _party_has_living_member(memory: MemoryLike, symbols: SymbolTable) -> bool | None:
    """Whether the party block holds a living member, or ``None`` if unknown.

    Mirrors the ROM's ``AnyPartyAlive`` (``core.asm``): it ORs the two HP bytes
    of every ``party_struct`` from ``wPartyMon1HP`` and tests the result, so the
    party's *living* member is what decides whether a faint leads to a
    replacement menu or to ``HandlePlayerBlackOut``.

    ``None`` means the symbols needed for the scan are absent or the count is
    outside the engine's ``PARTY_LENGTH`` invariant, so no answer can be given
    and the caller must fail closed.
    """
    if _PARTY_COUNT_SYMBOL not in symbols or _PARTY_HP_SYMBOL not in symbols:
        return None
    count = symbols.read_u8(memory, _PARTY_COUNT_SYMBOL)
    if not 0 < count <= MAX_PARTY_SLOTS:
        return None
    base = symbols.addr_of(_PARTY_HP_SYMBOL)
    for slot in range(count):
        offset = base + slot * PARTY_STRUCT_SIZE
        # Party HP is a big-endian u16 in the ``SVBK``-remapped WRAM half, so
        # both bytes are read from the linker's bank rather than the mapped
        # window (the same rule :mod:`pokered_harness.state.party` follows).
        if (read_wram_u8(memory, offset) | read_wram_u8(memory, offset + 1)) != 0:
            return True
    return False


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
