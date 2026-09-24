"""Private observation mixin for :mod:`pokered_harness.session` (issue #113).

Holds the session-epoch/snapshot accessors and the ROM-observed battle
menu/resolution/end bracketing methods so the canonical ``session`` module
stays below the 1000-line split target.  ``Session`` composes them via
multiple inheritance; ``SessionEpoch`` and ``StateSnapshot`` are re-exported
from ``pokered_harness.session`` so every existing import path keeps
resolving.
"""

from __future__ import annotations

from dataclasses import replace

from pokered_harness._session_support import (
    _BATTLE_END_SYMBOL,
    _BATTLE_MENU_CLOSE_SYMBOLS,
    _BATTLE_MENU_OPEN_SYMBOLS,
    _BATTLE_RESOLUTION_CLOSE_SYMBOLS,
    _BATTLE_RESOLUTION_OPEN_SYMBOLS,
    SessionEpoch,
    StateSnapshot,
    _next_session_id,
)
from pokered_harness.state import (
    BattleKind,
    BattleLifecycle,
    BattleMenuObservation,
    BattleResolutionObservation,
    GameState,
    parse_battle,
    parse_game_state,
)


class _SessionObservationMixin:
    """Epoch/snapshot accessors and hook-derived battle observations."""

    def _init_observation_state(self) -> None:
        """Initialise the epoch counters and battle-observation state."""
        self._load_generation: int = 0
        self._reset_generation: int = 0
        self._session_id: str = _next_session_id()
        self._battle_lifecycle = BattleLifecycle()
        # Battle command/move menu entry/exit state, maintained by execution
        # hooks when ``enable_battle_menu_observation`` succeeds.  ``None``
        # means the hooks are not installed, so COMMAND_SELECTION is unknown.
        self._battle_menu_observed = False
        self._battle_menu_open: bool | None = None
        self._battle_menu_evidence: tuple[str, ...] = ()
        # Move-execution entry/exit state, maintained by execution hooks when
        # ``enable_battle_resolution_observation`` succeeds.  ``None`` means
        # the hooks are not installed, so ACTION_RESOLUTION for an ordinary
        # FIGHT turn stays unknown.
        self._battle_resolution_observed = False
        self._battle_resolution_open: bool | None = None
        self._battle_resolution_evidence: tuple[str, ...] = ()
        # Battle-end outcome evidence, sampled by an ``EndOfBattle`` execution
        # hook when ``enable_battle_end_observation`` succeeds.  ``None`` means
        # no ROM-observed end is pending, so the result byte cannot be
        # attributed to a battle end (see ``BattleLifecycle.end_observed``).
        self._battle_end_observed = False
        self._battle_end_result: int | None = None
        self._battle_end_escaped = False

    def current_load_generation(self) -> int:
        """Monotonic count of successful ``load_state`` calls.

        Unlike :meth:`current_tick`, this counter is not restored by a
        ``load_state``; callers can pair it with a game-state snapshot to
        detect that the emulated clock was rewound by a state load.
        """
        with self._emulator_access(allow_closed=True):
            return self._load_generation

    @property
    def session_id(self) -> str:
        """Identity naming the process lifetime and in-process session index.

        Distinct across replacement sessions in one process *and* across
        process restarts, so a client comparing epochs across a server restart
        cannot accept a stale snapshot.
        """
        return self._session_id

    def _epoch_locked(self) -> SessionEpoch:
        return SessionEpoch(
            tick=self._tick,
            load_generation=self._load_generation,
            reset_generation=self._reset_generation,
            session_id=self._session_id,
        )

    def read_epoch(self) -> SessionEpoch:
        """Capture tick, generations, and identity under one lock scope.

        Unlike reading :meth:`current_tick` and
        :meth:`current_load_generation` separately, the counters are captured
        atomically, so concurrent stepping/loading cannot pair a torn tick
        with a mismatched generation.
        """
        with self._emulator_access(allow_closed=True):
            return self._epoch_locked()


    def read_state_snapshot(self) -> StateSnapshot:
        """Read the game state and its :class:`SessionEpoch` under one lock.

        This is the atomic form of ``read_game_state()`` plus
        ``read_epoch()``: a concurrent step or load cannot slip between the
        parsed memory and the epoch counters it is paired with.
        """
        self._ensure_open()
        with self._emulator_access():
            self._ensure_open()
            state = self._read_game_state_locked()
            return StateSnapshot(state=state, epoch=self._epoch_locked())

    def _read_game_state_locked(self) -> GameState:
        """Parse the aggregate and qualify terminal outcomes by lifecycle."""
        memory = self._pyboy.memory
        state = parse_game_state(memory, self._symbols)
        battle = state.battle
        if battle is None:
            return state
        previous = self._battle_lifecycle
        # When the ``EndOfBattle`` hook is installed the outcome may only come
        # from the bytes the ROM held at that routine's entry, so a
        # client-timed read can never promote a byte left by an earlier faint
        # in the same battle (see ``enable_battle_end_observation``).  A read
        # that does not report the end leaves the sample pending.
        if self._battle_end_observed and previous.was_active and battle.raw_is_in_battle == 0:
            end_result, end_escaped = self._take_battle_end_observation()
            previous = replace(
                previous,
                end_observed=True,
                end_result=end_result,
                end_escaped=end_escaped,
            )
        qualified = parse_battle(
            memory,
            self._symbols,
            lifecycle=previous,
            menu=self._battle_menu_observation(),
            resolution=self._battle_resolution_observation(),
        )
        if qualified.kind in (BattleKind.WILD, BattleKind.TRAINER):
            # Accumulate outcome-specific evidence while a battle is live.
            # ``escaped_from_battle`` is observed before the engine clears
            # it, so a later ambiguous zero cannot be read as a win.
            self._battle_lifecycle = BattleLifecycle(
                was_active=True,
                escaped=previous.escaped or bool(qualified.escaped_from_battle),
            )
        else:
            # The battle ended (or no battle is live); drop the history so a
            # load or a subsequent encounter cannot inherit stale evidence.
            self._battle_lifecycle = BattleLifecycle()
        return replace(state, battle=qualified)

    def enable_battle_menu_observation(self) -> bool:
        """Install execution hooks that bracket the battle command/move menu.

        Returns ``True`` when the hooks are (now) installed.  The observation
        is unavailable, and :attr:`BattlePhase.COMMAND_SELECTION` is never
        derived, when any bracketing label is absent from the loaded ``.sym``.
        The hooks only read the program counter: they write no RAM, press no
        input, and advance no frame.  They are closed with the session.
        """
        self._ensure_open()
        with self._emulator_access():
            self._ensure_open()
            if self._battle_menu_observed:
                return True
            labels = _BATTLE_MENU_OPEN_SYMBOLS + _BATTLE_MENU_CLOSE_SYMBOLS
            if any(name not in self._symbols for name in labels):
                return False

            def _mark_open(_ctx: object) -> None:
                self._battle_menu_open = True

            def _mark_closed(_ctx: object) -> None:
                self._battle_menu_open = False

            for name in _BATTLE_MENU_OPEN_SYMBOLS:
                bank, addr = self._symbols.bank_addr(name)
                self._register_event_hook_at_locked(bank, addr, _mark_open)
            for name in _BATTLE_MENU_CLOSE_SYMBOLS:
                bank, addr = self._symbols.bank_addr(name)
                self._register_event_hook_at_locked(bank, addr, _mark_closed)
            self._battle_menu_observed = True
            self._battle_menu_open = False
            self._battle_menu_evidence = labels
            return True

    def _battle_menu_observation(self) -> BattleMenuObservation | None:
        """Current hook-derived menu state, or ``None`` when not observed."""
        if not self._battle_menu_observed:
            return None
        return BattleMenuObservation(
            open=self._battle_menu_open,
            evidence=self._battle_menu_evidence,
        )

    def enable_battle_resolution_observation(self) -> bool:
        """Install execution hooks that bracket ROM move execution.

        Returns ``True`` when the hooks are (now) installed.  The observation
        is unavailable, and ``ACTION_RESOLUTION`` is then only derivable from a
        non-zero ``wActionResultOrTookBattleTurn``, when any bracketing label is
        absent from the loaded ``.sym``.

        An ordinary FIGHT move is otherwise unobservable: the flag
        ``ExecutePlayerMoveDone`` clears to zero on the way out is the only
        byte the engine writes around a normal attack turn, so a client
        polling at any interval can miss it entirely.  The hooks only read the
        program counter: they write no RAM, press no input, and advance no
        frame.  They are closed with the session.
        """
        self._ensure_open()
        with self._emulator_access():
            self._ensure_open()
            if self._battle_resolution_observed:
                return True
            labels = _BATTLE_RESOLUTION_OPEN_SYMBOLS + _BATTLE_RESOLUTION_CLOSE_SYMBOLS
            if any(name not in self._symbols for name in labels):
                return False

            def _mark_resolving(_ctx: object) -> None:
                self._battle_resolution_open = True

            def _mark_resolved(_ctx: object) -> None:
                self._battle_resolution_open = False

            for name in _BATTLE_RESOLUTION_OPEN_SYMBOLS:
                bank, addr = self._symbols.bank_addr(name)
                self._register_event_hook_at_locked(bank, addr, _mark_resolving)
            for name in _BATTLE_RESOLUTION_CLOSE_SYMBOLS:
                bank, addr = self._symbols.bank_addr(name)
                self._register_event_hook_at_locked(bank, addr, _mark_resolved)
            self._battle_resolution_observed = True
            self._battle_resolution_open = False
            self._battle_resolution_evidence = labels
            return True

    def _battle_resolution_observation(
        self,
    ) -> BattleResolutionObservation | None:
        """Current hook-derived move-execution state, or ``None`` if unknown."""
        if not self._battle_resolution_observed:
            return None
        return BattleResolutionObservation(
            open=self._battle_resolution_open,
            evidence=self._battle_resolution_evidence,
        )

    def enable_battle_end_observation(self) -> bool:
        """Install an execution hook that samples the ROM's ``EndOfBattle``.

        Returns ``True`` when the hook is (now) installed.  The observation is
        unavailable when the ``EndOfBattle`` label is absent from the loaded
        ``.sym``; without it a terminal outcome is never promoted, because a
        client-timed read cannot tell which event wrote the result byte.

        ``EndOfBattle`` is the last instant ``wBattleResult`` and
        ``wEscapedFromBattle`` are both meaningful: the routine clears the
        escape flag itself, and it never writes the result byte, so a byte
        left from an earlier faint in the same battle survives teardown.  The
        hook samples both bytes at entry and writes no RAM, presses no input,
        and advances no frame.  It is closed with the session.
        """
        self._ensure_open()
        with self._emulator_access():
            self._ensure_open()
            if self._battle_end_observed:
                return True
            if _BATTLE_END_SYMBOL not in self._symbols:
                return False
            if "wBattleResult" not in self._symbols or "wEscapedFromBattle" not in self._symbols:
                return False

            def _sample_end(_ctx: object) -> None:
                # ``EndOfBattle`` runs for every battle end, including an
                # escape whose cleanup happens entirely between two client
                # reads, so this is the one place the outcome bytes can be
                # attributed to *this* end.
                self._battle_end_result = self._symbols.read_u8(self._pyboy.memory, "wBattleResult")
                self._battle_end_escaped = (
                    self._symbols.read_u8(self._pyboy.memory, "wEscapedFromBattle") != 0
                )

            bank, addr = self._symbols.bank_addr(_BATTLE_END_SYMBOL)
            self._register_event_hook_at_locked(
                bank,
                addr,
                _sample_end,
                symbol_name=_BATTLE_END_SYMBOL,
            )
            self._battle_end_observed = True
            return True

    def _take_battle_end_observation(self) -> tuple[int | None, bool]:
        """Claim the sampled ``EndOfBattle`` bytes for the falling edge.

        The hook fires on ``EndOfBattle`` entry, while ``wIsInBattle`` is still
        non-zero (the routine clears it later), so a read may legitimately
        arrive between the sample and the observed end.  The sample is
        therefore only claimed by the read that actually reports the end;
        earlier reads leave it pending.
        """
        result = self._battle_end_result
        escaped = self._battle_end_escaped
        self._battle_end_result = None
        self._battle_end_escaped = False
        return result, escaped

    def _invalidate_battle_observations(self) -> None:
        """Drop every battle observation tied to the current epoch.

        Called when an operation starts a new observation epoch (``load_state``
        or ``reset_tick``).  The pending ``EndOfBattle`` sample must go with the
        lifecycle history: it was taken while the *previous* emulated instant
        was running, so if it survived, a later read that merely crosses the
        active->inactive transition would promote the old epoch's outcome and
        report a battle end this epoch never observed.  Menu entry/exit state
        is equally stale and becomes unknown until a fresh hook event fires.
        """
        self._battle_lifecycle = BattleLifecycle()
        self._battle_end_result = None
        self._battle_end_escaped = False
        if self._battle_menu_observed:
            self._battle_menu_open = None
        if self._battle_resolution_observed:
            self._battle_resolution_open = None
