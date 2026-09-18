"""Session manager: owns the PyBoy lifecycle and exposes a typed action/
observation surface to callers (MCP server, scripts, tests).

The session does *not* hard-code the agent loop — it offers ``step``,
``press``, ``save_state``, ``load_state``, and ``run_until_event``
primitives that callers compose. That split matches the ADR boundary
between "emulator control" (this module) and "policy" (external).
"""

from __future__ import annotations

import hashlib
import itertools
import math
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from typing import Self

from pokered_harness.events.hooks import EventBus, GameEvent, HookRegistration
from pokered_harness.input import Button, validate_button
from pokered_harness.ownership import (
    EmulatorOwnershipError,
    owner_for,
    owner_group,
)
from pokered_harness.pyboy_protocol import PyBoyLike
from pokered_harness.state import (
    BattleKind,
    BattleLifecycle,
    BattleMenuObservation,
    GameState,
    parse_battle,
    parse_game_state,
)
from pokered_harness.symbols.loader import SymbolTable, load_sym_file


class SessionError(RuntimeError):
    """Base class for failures at the emulator-session boundary."""

    code = "session_error"


class SessionClosedError(SessionError):
    """Raised when an operation is attempted after session shutdown."""

    code = "session_closed"


class SessionCloseTimeout(SessionError, TimeoutError):
    """Raised when shutdown cannot complete before its deadline."""

    code = "session_close_timeout"


class SessionCloseError(SessionError):
    """Raised when the emulator rejects a shutdown attempt."""

    code = "session_close_failed"


# Compatibility spelling used by the earlier ownership API.  Keep the main
# typed timeout class as the canonical public exception while allowing raw
# provider integrations to share their existing import contract.
SessionCleanupTimeoutError = SessionCloseTimeout


class SessionLockTimeout(SessionError):
    """Raised when a bounded compound-operation lock cannot be acquired."""

    code = "session_lock_timeout"


class SessionConfigurationError(ValueError):
    """Raised when a ROM, symbol file, or version pin is unusable."""

    code = "invalid_session_configuration"


class RomNotFoundError(SessionConfigurationError):
    """Raised when the configured ROM file is not available."""

    code = "rom_not_found"


class SymbolNotFoundError(SessionConfigurationError):
    """Raised when the configured symbol file is not available."""

    code = "symbol_not_found"


class InvalidStateError(ValueError):
    """Raised when a save-state payload is not usable."""

    code = "invalid_state"


class VersionMismatch(SessionError):
    """Raised when a loaded ROM, symbol file, or PyBoy version misses its pin."""

    code = "version_mismatch"


class RomHashMismatch(VersionMismatch):
    """Raised when the ROM bytes do not match the configured SHA-1 pin."""

    code = "rom_hash_mismatch"


class SymbolHashMismatch(VersionMismatch):
    """Raised when the symbol bytes do not match the configured SHA-1 pin."""

    code = "symbol_hash_mismatch"


_DEFAULT_CLOSE_TIMEOUT_S = 5.0

# Monotonic identity for each Session.  A replacement session (a new Session
# object over a fresh emulator) must not be able to reuse the tick/load
# generation pair of the session it replaced, so the identity is included in
# every epoch snapshot.
#
# A bare per-process counter is not enough: it restarts at 1 in every new
# process, so two fresh processes would report the same ``session_id`` for
# their first session and a client comparing epochs across a restart could
# accept a stale snapshot.
#
# The id is therefore a string naming the process lifetime and the in-process
# session index:
#
#     <pid>-<process-start-ns>-<index>
#
# ``pid`` separates concurrent processes, and the system-wide monotonic clock
# separates sequential process lifetimes that reuse a pid.  A string (rather
# than a packed integer) keeps both components exact: a 53-bit float-safe
# integer cannot hold a nanosecond timestamp together with a pid and a
# counter without mangling one of them, and JSON transports numbers through
# IEEE-754 doubles.
_PROCESS_START_NS = time.monotonic_ns()
_PROCESS_LIFETIME_ID = f"{os.getpid()}-{_PROCESS_START_NS}"
_SESSION_ID_SEQUENCE = itertools.count(1)
_SESSION_ID_LOCK = threading.Lock()


def _next_session_id() -> str:
    with _SESSION_ID_LOCK:
        return f"{_PROCESS_LIFETIME_ID}-{next(_SESSION_ID_SEQUENCE)}"


# ROM labels whose execution brackets the battle command/move menu.  There is
# no RAM byte that reports "menu open": ``wMoveMenuType`` is a mode selector
# (0 regular, 1 mimic, 2 relearn/PP) written before every ``MoveSelectionMenu``
# call and left set after the menu closes.  The session instead observes the
# control flow.  Entry into ``SelectMenuItem`` (the move-menu input loop) or
# ``DisplayBattleMenu.handleBattleMenuInput`` (the FIGHT/ITEM/POKéMON/RUN
# input setup) proves the menu was drawn and is awaiting input; entry into the
# per-turn ``MainInBattleLoop`` or its post-selection continuation
# ``MainInBattleLoop.selectEnemyMove`` proves the menu closed.  Observation is
# only available when every one of these labels exists in the loaded ``.sym``;
# otherwise the session reports no menu evidence and ``COMMAND_SELECTION`` is
# never derived (fail closed).
_BATTLE_MENU_OPEN_SYMBOLS = (
    "SelectMenuItem",
    "DisplayBattleMenu.handleBattleMenuInput",
)
# The routine every battle end funnels through (``core.asm``:
# ``_InitBattleCommon`` calls ``callfar EndOfBattle``).  Its entry is the last
# instant the outcome bytes are still meaningful, so the session samples them
# there rather than trusting a client-timed read.
_BATTLE_END_SYMBOL = "EndOfBattle"
_BATTLE_MENU_CLOSE_SYMBOLS = (
    "MainInBattleLoop",
    "MainInBattleLoop.selectEnemyMove",
    # Both the regular move-selection return (core.asm: ``call MoveSelectionMenu
    # / call LoadScreenTilesFromBuffer1``) and the Mimic submenu return
    # (effects.asm: ``call MoveSelectionMenu / call LoadScreenTilesFromBuffer1``)
    # redraw the screen immediately after the menu closes.  ``MainInBattleLoop``
    # alone does not fire on Mimic's return into animation/result text, so this
    # closes the observation on the shared post-menu redraw path.
    "LoadScreenTilesFromBuffer1",
)


@dataclass(frozen=True, slots=True)
class SessionEpoch:
    """Identity and clock counters captured atomically with a snapshot.

    ``tick`` advances with :meth:`Session.step`; ``load_generation`` advances
    on every successful :meth:`Session.load_state` (the tick is restored by a
    load, so the pair alone can repeat); ``reset_generation`` advances on
    every :meth:`Session.reset_tick` (which can intentionally rewind the
    tick); ``session_id`` distinguishes a replacement :class:`Session` from
    the one it replaced, including across process restarts (it names the
    process lifetime and the in-process session index).
    """

    tick: int
    load_generation: int
    reset_generation: int
    session_id: str


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    """A game-state observation paired with its :class:`SessionEpoch`.

    Both fields are read under one owner-locked emulator scope, so the epoch
    always describes the same instant as ``state``; a concurrent step or load
    cannot tear the two apart.
    """

    state: GameState
    epoch: SessionEpoch


@dataclass(frozen=True, slots=True)
class RunUntilResult:
    event: GameEvent | None
    ticks_spent: int

    @property
    def reached(self) -> bool:
        return self.event is not None


class _HookState:
    """Small mutable flag shared by a guarded raw PyBoy hook."""

    __slots__ = ("active",)

    def __init__(self) -> None:
        self.active = True


class _StopAttempt:
    """Immutable identity for one asynchronous ``PyBoy.stop`` attempt.

    The lifecycle fields on :class:`Session` are reused by a retry.  Keeping
    the completion event and result on this per-attempt record means an older
    caller cannot accidentally wait on, or read the result from, a later
    retry that replaced the session's current attempt.
    """

    __slots__ = ("done", "error", "owner_id", "stopped")

    def __init__(self, owner_id: int) -> None:
        self.owner_id = owner_id
        self.done = threading.Event()
        self.error: BaseException | None = None
        self.stopped = False


class Session:
    """Owns one PyBoy instance, a loaded symbol table, and an event bus.

    Construct via :meth:`from_files` for real use (loads the ROM and
    ``.sym`` from disk and instantiates PyBoy). The raw constructor takes
    pre-built objects for unit tests that swap in a fake PyBoy.
    """

    def __init__(
        self,
        *,
        pyboy: PyBoyLike,
        symbols: SymbolTable,
        event_bus: EventBus | None = None,
        view: bool = False,
    ) -> None:
        self._pyboy = pyboy
        self._symbols = symbols
        # Explicit None check — EventBus defines __len__, so an empty
        # bus is falsy and ``event_bus or EventBus()`` would drop it.
        self._events = event_bus if event_bus is not None else EventBus()
        self._tick: int = 0
        self._load_generation: int = 0
        self._reset_generation: int = 0
        self._session_id: str = _next_session_id()
        self._battle_lifecycle = BattleLifecycle()
        # The owner registry is shared with raw link providers.  Keep the
        # Session-facing ``_lock`` property below for compatibility with
        # existing tests/instrumentation, but make the owner lock the one
        # canonical re-entrant mutex used by both APIs.
        self._owner = owner_for(pyboy)
        self._execution = self._owner.execution
        self._lifecycle_lock = threading.Lock()
        self._close_done = threading.Event()
        self._close_done.set()
        self._close_owner: int | None = None
        self._stop_thread: threading.Thread | None = None
        self._stop_error: BaseException | None = None
        self._stop_attempt: _StopAttempt | None = None
        self._stopped = False
        self._closed = False
        self._event_hooks: list[HookRegistration] = []
        self._serial_hooks: list[tuple[_HookState, int, int, str]] = []
        # Battle command/move menu entry/exit state, maintained by execution
        # hooks when ``enable_battle_menu_observation`` succeeds.  ``None``
        # means the hooks are not installed, so COMMAND_SELECTION is unknown.
        self._battle_menu_observed = False
        self._battle_menu_open: bool | None = None
        self._battle_menu_evidence: tuple[str, ...] = ()
        # Battle-end outcome evidence, sampled by an ``EndOfBattle`` execution
        # hook when ``enable_battle_end_observation`` succeeds.  ``None`` means
        # no ROM-observed end is pending, so the result byte cannot be
        # attributed to a battle end (see ``BattleLifecycle.end_observed``).
        self._battle_end_observed = False
        self._battle_end_result: int | None = None
        self._battle_end_escaped = False
        # ``view`` is stashed for introspection; the actual wiring into the
        # PyBoy factory happens in ``from_files`` where the ROM is loaded.
        self._view = view
        self._timed_endpoint = None
        self._timed_attachment = None
        self._timed_executing = False
        self._owner.bind_session(self, SessionClosedError)

    @property
    def _lock(self):
        """Compatibility view of the canonical emulator ownership lock."""
        return self._owner.lock

    @_lock.setter
    def _lock(self, value):
        self._owner.lock = value

    @contextmanager
    def _emulator_access(
        self,
        *,
        timeout_s: float | None = None,
        allow_closed: bool = False,
    ) -> Iterator[None]:
        """Enter the shared owner scope and optional native backend scope.

        The native backend may expose an owner-pump scope of its own.  It is
        entered only after the Python owner lock, preserving the canonical
        owner -> backend/session ordering and preventing a raw callback from
        re-entering the MCP operation lock in reverse.
        """
        owner_scope = self._owner.access(timeout=timeout_s)
        try:
            owner_scope.__enter__()
        except TimeoutError as exc:
            timeout = timeout_s if timeout_s is not None else 0.0
            raise SessionLockTimeout(
                "could not acquire the emulator lock before the "
                f"{timeout:g}s deadline"
            ) from exc
        try:
            if not allow_closed:
                self._ensure_open()
            core = getattr(getattr(self._pyboy, "mb", None), "serial", None)
            scope = getattr(getattr(core, "backend", None), "owner_scope", None)
            if callable(scope):
                with scope():
                    yield
            else:
                yield
        finally:
            owner_scope.__exit__(None, None, None)

    # --- construction --------------------------------------------------

    @classmethod
    def from_files(
        cls,
        rom_path: str | Path,
        sym_path: str | Path,
        *,
        expected_rom_sha1: str | None = None,
        expected_symbol_sha1: str | None = None,
        expected_pyboy_version: str | None = None,
        expected_pyboy_revision: str | None = None,
        pyboy_factory: Callable[[str], PyBoyLike] | None = None,
        view: bool = False,
    ) -> Session:
        rom_path = Path(rom_path)
        sym_path = Path(sym_path)

        if not rom_path.is_file():
            raise RomNotFoundError(f"ROM file not found: {rom_path}")
        if not sym_path.is_file():
            raise SymbolNotFoundError(
                f"symbol file not found: {sym_path}"
            )

        if expected_rom_sha1 is not None:
            expected_rom_sha1 = _normalise_sha1(expected_rom_sha1)
            try:
                actual = sha1_of_file(rom_path)
            except OSError as exc:
                raise RomNotFoundError(
                    f"unable to read ROM file {rom_path}: {exc}"
                ) from exc
            if actual.lower() != expected_rom_sha1.lower():
                raise RomHashMismatch(
                    f"ROM SHA-1 mismatch: expected {expected_rom_sha1}, "
                    f"got {actual} for {rom_path}"
                )

        if expected_symbol_sha1 is not None:
            expected_symbol_sha1 = _normalise_sha1(
                expected_symbol_sha1, label="symbol SHA-1"
            )
            try:
                actual = sha1_of_file(sym_path)
            except OSError as exc:
                raise SymbolNotFoundError(
                    f"unable to read symbol file {sym_path} for SHA-1 "
                    f"verification: {exc}"
                ) from exc
            if actual.lower() != expected_symbol_sha1.lower():
                raise SymbolHashMismatch(
                    f"symbol SHA-1 mismatch: expected {expected_symbol_sha1}, "
                    f"got {actual} for {sym_path}"
                )

        if (
            expected_pyboy_version is not None
            or expected_pyboy_revision is not None
        ):
            import pyboy as _pyboy_module

            if expected_pyboy_version is not None:
                if not isinstance(expected_pyboy_version, str):
                    raise SessionConfigurationError(
                        "expected PyBoy version must be a string"
                    )
                expected_pyboy_version = expected_pyboy_version.strip()
                if not expected_pyboy_version:
                    raise SessionConfigurationError(
                        "expected PyBoy version must not be empty"
                    )
                actual_version = getattr(_pyboy_module, "__version__", None)
                if actual_version != expected_pyboy_version:
                    raise VersionMismatch(
                        f"PyBoy version mismatch: expected {expected_pyboy_version}, "
                        f"got {actual_version}"
                    )
            actual_revision = getattr(
                _pyboy_module, "__pokered_harness_revision__", None
            )
            if not actual_revision:
                raise VersionMismatch(
                    "PyBoy runtime is not the pinned pokered-harness build; "
                    "install this project's bundled PyBoy source"
                )
            if expected_pyboy_revision is not None:
                expected_pyboy_revision = _normalise_sha1(
                    expected_pyboy_revision, label="PyBoy revision"
                )
                if actual_revision != expected_pyboy_revision:
                    raise VersionMismatch(
                        f"PyBoy revision mismatch: expected {expected_pyboy_revision}, "
                        f"got {actual_revision}"
                    )

        try:
            symbols = load_sym_file(sym_path)
        except FileNotFoundError as exc:
            raise SymbolNotFoundError(
                f"symbol file not found: {sym_path}"
            ) from exc
        except (OSError, UnicodeError) as exc:
            raise SessionConfigurationError(
                f"unable to load symbol file {sym_path}: {exc}"
            ) from exc
        if pyboy_factory is not None:
            # Injected factory (tests, custom wrappers) is called with just
            # the ROM path — it's responsible for its own window/cgb config.
            try:
                pyboy = pyboy_factory(str(rom_path))
            except FileNotFoundError as exc:
                raise RomNotFoundError(
                    f"unable to open ROM file {rom_path}: {exc}"
                ) from exc
            except Exception as exc:  # Wrap factory errors at the session boundary.
                raise SessionConfigurationError(
                    f"unable to create emulator for {rom_path}: {exc}"
                ) from exc
        else:
            # The built-in factory accepts window/cgb kwargs; ``view`` picks
            # SDL2 for a visible window, otherwise stays headless ("null").
            window = "SDL2" if view else "null"
            try:
                pyboy = _default_pyboy_factory(
                    str(rom_path), window=window, cgb=True
                )
            except Exception as exc:  # Wrap factory errors at the session boundary.
                raise SessionConfigurationError(
                    f"unable to create emulator for {rom_path}: {exc}"
                ) from exc
        return cls(pyboy=pyboy, symbols=symbols, view=view)

    # --- lifecycle -----------------------------------------------------

    def close(
        self, save: bool = False, *, timeout_s: float = _DEFAULT_CLOSE_TIMEOUT_S
    ) -> None:
        """Stop the emulator exactly once with a retryable bounded contract.

        The closed flag is published before waiting for ``_lock`` so new
        operations and raw callbacks fail closed. A caller that already owns
        the emulator lock is allowed to finish before shutdown begins. Once
        the lock is observed idle, ``PyBoy.stop`` runs in a daemon worker so a
        broken emulator implementation cannot block the MCP/request thread
        forever. A later :meth:`close` waits for that same worker and reports
        completion, failure, or another bounded timeout; it never starts a
        second concurrent stop.
        """
        # ``close`` is a terminal lifecycle boundary.  A zero deadline is
        # not useful here (there is always at least a lock hand-off), and
        # accepting strings or arbitrarily large integers makes the public
        # contract depend on the platform's float conversion.  Keep the
        # more permissive non-negative validator for ordinary owner scopes,
        # but make shutdown's deadline strictly positive and bounded.
        timeout_s = _validate_close_timeout(timeout_s)
        stop_deadline = time.monotonic() + timeout_s
        current_thread_id = threading.get_ident()
        endpoint = self._timed_endpoint
        if endpoint is not None:
            if endpoint._owner != current_thread_id:
                self.cancel_timed_execution()
                raise SessionError("timed execution requires owner cleanup before close")
            self.unbind_timed_execution(
                endpoint, timeout_s=max(0.0, stop_deadline - time.monotonic())
            )
        attempt: _StopAttempt | None = None
        with self._lifecycle_lock:
            if self._stopped:
                return
            current_attempt = self._stop_attempt
            if current_attempt is not None and current_attempt.done.is_set():
                # A completed failed attempt is retryable.  Drop only the
                # session's pointer; callers already waiting on the old
                # attempt retain its immutable event/result object.
                self._stop_attempt = None
                self._stop_error = None
                current_attempt = None
            if current_attempt is not None:
                attempt = current_attempt
                close_done = attempt.done
                # An attempt's owner is the caller that launched the worker,
                # not a lock lease.  Even that same caller must wait when a
                # previous close timed out while ``stop`` remained active;
                # only the worker itself is handled by the recursive guard
                # below.
                owned_by_current_thread = False
            elif self._close_owner is not None:
                # The first closer is still in the bounded lock phase.  Its
                # attempt is published before it waits for the emulator lock
                # so observers retain this identity if a retry replaces the
                # session's current attempt later.
                attempt = _StopAttempt(self._close_owner)
                self._stop_attempt = attempt
                close_done = attempt.done
                owned_by_current_thread = self._close_owner == current_thread_id
            else:
                # Publish the per-attempt identity before claiming the
                # lifecycle boundary.  A concurrent close can then wait on
                # this exact result even while the owner is still acquiring
                # the emulator lock.
                attempt = _StopAttempt(current_thread_id)
                self._closed = True
                self._owner.closed = True
                self._close_owner = current_thread_id
                self._close_done.clear()
                self._stop_error = None
                self._stop_attempt = attempt
                for state, _bank, _addr, _symbol_name in self._serial_hooks:
                    state.active = False
                close_done = None
                owned_by_current_thread = True

        # ``PyBoy.stop`` runs in a daemon worker so a broken runtime cannot
        # pin the request thread.  If that worker recursively calls
        # ``Session.close`` (some faulted native runtimes do), waiting on its
        # own completion event would deadlock until the outer deadline.  Fail
        # the recursive cleanup immediately; the outer worker publishes this
        # typed failure and a later close can retry.
        if (
            close_done is not None
            and self._stop_thread is threading.current_thread()
        ):
            raise SessionCloseTimeout(
                "recursive emulator shutdown cannot wait for its own stop worker"
            )

        # A raw provider callback or a canonical multi-session scope may call
        # close re-entrantly.  Publishing ``closed`` above is intentional so
        # callbacks fail closed, but stopping must wait until the outer owner
        # scope exits; otherwise PyBoy.stop could run halfway through a tick.
        if getattr(self._execution, "depth", 0):
            error = SessionCloseTimeout(
                "cleanup deferred until the current emulator operation exits"
            )
            with self._lifecycle_lock:
                if (
                    self._close_owner == current_thread_id
                    and self._stop_thread is None
                ):
                    self._stop_error = error
                    if attempt is not None and not attempt.done.is_set():
                        attempt.error = error
                        attempt.stopped = False
                        attempt.done.set()
                    self._close_owner = None
                    self._close_done.set()
            raise error
        try:
            self._owner.assert_lock_order()
        except EmulatorOwnershipError as exc:
            error = SessionCloseTimeout(
                "cleanup deferred until other emulator ownership scopes exit"
            )
            with self._lifecycle_lock:
                if (
                    self._close_owner == current_thread_id
                    and self._stop_thread is None
                ):
                    self._stop_error = error
                    if attempt is not None and not attempt.done.is_set():
                        attempt.error = error
                        attempt.stopped = False
                        attempt.done.set()
                    self._close_owner = None
                    self._close_done.set()
            raise error from exc

        if close_done is not None:
            if not owned_by_current_thread:
                # A concurrent closer observes the same canonical emulator
                # lock as the stop worker.  The short probe keeps teardown
                # ordering explicit (and lets instrumented locks observe the
                # bounded hand-off) without ever running a second ``stop``.
                remaining = max(0.0, stop_deadline - time.monotonic())
                acquired = self._lock.acquire(
                    timeout=min(remaining, 0.01)
                )
                if not acquired:
                    # The stop worker may legitimately hold the lock for the
                    # whole runtime-defined cleanup interval.  The immutable
                    # attempt event below remains the authoritative wait;
                    # this probe is diagnostic/ordering evidence only.
                    pass
                else:
                    self._lock.release()
                if not close_done.wait(
                    timeout=max(0.0, stop_deadline - time.monotonic())
                ):
                    raise SessionCloseTimeout(
                        "another session close is still in progress before the "
                        f"{timeout_s:g}s shutdown deadline (cleanup deadline)"
                    )
            with self._lifecycle_lock:
                if attempt is None:
                    attempt = self._stop_attempt
                if attempt is not None:
                    if not attempt.done.is_set():
                        raise SessionCloseTimeout(
                            "emulator shutdown is still in progress after the "
                            f"{timeout_s:g}s shutdown deadline"
                        )
                    if attempt.stopped:
                        return
                    if attempt.error is not None:
                        # Read the result from this caller's captured attempt,
                        # never from the mutable session-wide retry fields.
                        raise attempt.error
                if self._stopped:
                    return
                if self._stop_error is not None:
                    raise self._stop_error
                if self._stop_thread is not None or self._close_owner is not None:
                    if self._close_done.is_set():
                        return
                    raise SessionCloseTimeout(
                        "emulator shutdown is still in progress after the "
                        f"{timeout_s:g}s shutdown deadline"
                    )
                # The previous close caller timed out before it could launch
                # the stop worker. Claim the retry slot instead of treating
                # the session as successfully closed.
                self._close_owner = current_thread_id
                self._close_done.clear()
                attempt = _StopAttempt(current_thread_id)
                self._stop_attempt = attempt
                close_done = None
                owned_by_current_thread = True

        lock_acquired = False
        close_failure: BaseException | None = None
        try:
            if not self._lock.acquire(
                timeout=max(0.0, stop_deadline - time.monotonic())
            ):
                raise SessionCloseTimeout(
                    "an emulator operation is still active after "
                    f"the {timeout_s:g}s shutdown deadline (cleanup deadline)"
                )
            lock_acquired = True
            if self._timed_endpoint is not None:
                raise SessionError("timed execution must be detached before shutdown")
            # EventBus hooks are physical PyBoy callbacks too. Deactivate and
            # deregister them while the session lock proves that no callback
            # is currently running. Raw serial hooks are guarded separately
            # above because their physical removal API is not stable.
            self._close_event_hooks_locked()
            # Prove that no operation which started before close is active.
            # Public operations reject ``_closed`` before attempting the lock.
            self._lock.release()
            lock_acquired = False

            def _stop_worker() -> None:
                error: BaseException | None = None
                acquired = self._lock.acquire(timeout=0.1)
                if not acquired:
                    error = SessionCloseTimeout(
                        "emulator lock became busy during shutdown"
                    )
                else:
                    try:
                        with self._owner.access():
                            self._pyboy.stop(save=save)
                    except BaseException as exc:  # noqa: BLE001 - publish exact failure
                        error = exc
                    finally:
                        self._lock.release()
                with self._lifecycle_lock:
                    self._stop_error = error
                    if error is None:
                        self._stopped = True
                    if attempt is not None:
                        attempt.error = error
                        attempt.stopped = error is None
                        attempt.done.set()
                    # The worker's result is now committed.  Keeping the
                    # Thread object here made every later close look like an
                    # in-flight shutdown and prevented a failed stop from
                    # ever being retried.
                    self._stop_thread = None
                    self._close_owner = None
                    self._close_done.set()

            worker = threading.Thread(
                target=_stop_worker,
                name="pokered-session-stop",
                daemon=True,
            )
            with self._lifecycle_lock:
                if attempt is None:
                    attempt = _StopAttempt(current_thread_id)
                self._stop_attempt = attempt
                self._stop_thread = worker
                self._close_owner = None
            try:
                worker.start()
            except BaseException as exc:
                with self._lifecycle_lock:
                    if self._stop_thread is worker:
                        self._stop_thread = None
                    if (
                        self._stop_attempt is attempt
                        and attempt is not None
                        and not attempt.done.is_set()
                    ):
                        self._stop_error = exc
                        attempt.error = exc
                        attempt.stopped = False
                        attempt.done.set()
                    self._close_owner = None
                    self._close_done.set()
                raise
            if not attempt.done.wait(
                timeout=max(0.0, stop_deadline - time.monotonic())
            ):
                raise SessionCloseTimeout(
                    "PyBoy.stop did not return before the "
                    f"{timeout_s:g}s shutdown deadline (cleanup deadline)"
                )
            # Keep the historical completion event observable for teardown
            # instrumentation, while taking the actual result from the
            # immutable attempt record.  A later retry may clear this shared
            # event; that cannot change the result captured above.
            self._close_done.wait(timeout=0)
            with self._lifecycle_lock:
                if attempt.error is not None:
                    raise attempt.error
                if attempt.stopped:
                    return
                # This branch is reachable only for a pre-attempt lifecycle
                # hand-off that was completed by another closer.
                if self._stopped:
                    return
                raise SessionCloseTimeout(
                    "emulator shutdown did not complete before the "
                    f"{timeout_s:g}s shutdown deadline (cleanup deadline)"
                )
        except BaseException as exc:
            close_failure = exc
            raise
        finally:
            if lock_acquired:
                self._lock.release()
            with self._lifecycle_lock:
                if (
                    close_failure is not None
                    and attempt is not None
                    and self._stop_thread is None
                    and self._stop_attempt is attempt
                    and not attempt.done.is_set()
                ):
                    self._stop_error = close_failure
                    attempt.error = close_failure
                    attempt.stopped = False
                    attempt.done.set()
                if self._close_owner == current_thread_id:
                    self._close_owner = None
                    # No stop worker was launched, so another close may retry
                    # the lock phase. Do not mark the session stopped.
                    if self._stop_thread is None:
                        self._close_done.set()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def _stop_complete(self) -> bool:
        """Compatibility view of the committed native stop state.

        Older integrations inspect this private flag when deciding whether a
        failed cleanup may be retried.  Keep it derived from the lifecycle
        state so an in-flight or failed stop can never be mistaken for a
        successful shutdown.
        """
        return self._stopped

    @contextmanager
    def locked(
        self,
        *,
        timeout_s: float = _DEFAULT_CLOSE_TIMEOUT_S,
        allow_closed: bool = False,
    ) -> Iterator[Session]:
        """Serialize a compound operation that touches this emulator.

        Individual session methods already take this same re-entrant lock.
        This context manager is for callers that need a consistent snapshot
        across more than one method without exposing the lock object itself.
        Lock acquisition is bounded by ``timeout_s`` (five seconds by
        default); callers that need a shorter request deadline should pass it
        explicitly. Teardown code may set ``allow_closed`` to release
        session-owned hooks after the public lifecycle has been closed; normal
        emulator operations must leave it false.
        """
        timeout = _validate_timeout(timeout_s, "timeout_s")
        if not allow_closed:
            self._ensure_open()
        with self._emulator_access(
            timeout_s=timeout,
            allow_closed=allow_closed,
        ):
            yield self

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # --- clock / events ------------------------------------------------

    def current_tick(self) -> int:
        # Tick bookkeeping remains inspectable after close; this was the
        # historical status-path contract and does not touch native state.
        with self._emulator_access(allow_closed=True):
            return self._tick

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

    @property
    def events(self) -> EventBus:
        return self._events

    @property
    def symbols(self) -> SymbolTable:
        return self._symbols

    def register_hook(self, symbol_name: str, event_name: str) -> HookRegistration:
        """Register an execution hook at a symbol label, tagging fired
        events with the current tick. Encapsulates the ``EventBus``
        interaction so callers don't reach into ``_pyboy``."""
        self._ensure_open()
        with self._emulator_access():
            self._ensure_open()
            bank, addr = self._symbols.bank_addr(symbol_name)

            def _emit(_ctx: object) -> None:
                self._events.emit(
                    tick=self.current_tick(),
                    name=event_name,
                    bank=bank,
                    addr=addr,
                )

            return self._register_event_hook_at_locked(
                bank,
                addr,
                _emit,
                symbol_name=symbol_name,
            )

    def register_hook_at(
        self,
        symbol_name: str,
        callback: Callable[[object], None],
        *,
        context: object | None = None,
        replace_existing: bool = False,
    ) -> HookRegistration:
        """Register a session-serialized callback at a symbol address.

        Link orchestration uses this for callbacks that mutate serial WRAM or
        CPU state rather than emitting a :class:`GameEvent`.  The EventBus
        dispatcher still gives each callback an owned, independently closable
        registration and avoids duplicate physical PyBoy breakpoints.
        """
        self._ensure_open()
        with self._emulator_access():
            self._ensure_open()
            bank, addr = self._symbols.bank_addr(symbol_name)
            return self._register_event_hook_at_locked(
                bank,
                addr,
                callback,
                context,
                symbol_name=symbol_name,
                replace_existing=replace_existing,
            )

    def register_hook_at_address(
        self,
        bank: int,
        addr: int,
        callback: Callable[[object], None],
        *,
        context: object | None = None,
        replace_existing: bool = False,
    ) -> HookRegistration:
        """Register a session-serialized callback at a raw address."""
        self._ensure_open()
        with self._emulator_access():
            self._ensure_open()
            return self._register_event_hook_at_locked(
                bank,
                addr,
                callback,
                context,
                replace_existing=replace_existing,
            )

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
            if (
                "wBattleResult" not in self._symbols
                or "wEscapedFromBattle" not in self._symbols
            ):
                return False

            def _sample_end(_ctx: object) -> None:
                # ``EndOfBattle`` runs for every battle end, including an
                # escape whose cleanup happens entirely between two client
                # reads, so this is the one place the outcome bytes can be
                # attributed to *this* end.
                self._battle_end_result = self._symbols.read_u8(
                    self._pyboy.memory, "wBattleResult"
                )
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

    def _register_event_hook_at_locked(
        self,
        bank: int,
        addr: int,
        callback: Callable[[object], None],
        context: object | None = None,
        *,
        symbol_name: str | None = None,
        replace_existing: bool = False,
    ) -> HookRegistration:
        """Register a guarded EventBus callback; caller owns ``_lock``."""

        def _guarded_callback(ctx: object) -> None:
            # Close publishes ``_closed`` before waiting for the session lock,
            # so callbacks arriving concurrently with teardown fail closed.
            if self._closed:
                return
            with self._emulator_access():
                if self._closed:
                    return
                callback(ctx)

        registration = self._events.register_at(
            self._pyboy,
            bank,
            addr,
            _guarded_callback,
            context,
            symbol_name=symbol_name,
            replace_existing=replace_existing,
        )
        self._event_hooks.append(registration)
        return registration

    def _close_event_hooks_locked(self) -> None:
        """Make all session-owned EventBus callbacks inert and release them."""
        for registration in reversed(self._event_hooks):
            try:
                registration.close()
            except Exception:  # noqa: BLE001, S110 - teardown must continue
                # EventBus marks a logical registration inactive before
                # attempting physical deregistration. Continue stopping the
                # emulator even if a custom PyBoy hook API rejects removal.
                pass
        self._event_hooks.clear()

    def serial_hook(
        self,
        symbol_name: str,
        callback: Callable[[object], None],
        *,
        context: object | None = None,
    ) -> None:
        """Register a raw callback at a symbol label, bypassing the event bus.

        Used by the link-cable bridge to mutate emulator memory when serial
        routines fire. For plain event emission prefer :meth:`register_hook`."""
        self._ensure_open()
        with self._emulator_access():
            self._ensure_open()
            bank, addr = self._symbols.bank_addr(symbol_name)
            state = _HookState()

            def _guarded_callback(ctx: object) -> None:
                # The flag is intentionally checked before taking the
                # session lock. Teardown must be able to deactivate a hook
                # while another thread is blocked in a remote exchange.
                if not state.active or self._closed:
                    return
                with self._emulator_access():
                    if not state.active or self._closed:
                        return
                    callback(ctx)

            self._pyboy.hook_register(bank, addr, _guarded_callback, context)
            self._serial_hooks.append((state, bank, addr, symbol_name))

    def deactivate_serial_hooks(
        self, *, timeout_s: float = _DEFAULT_CLOSE_TIMEOUT_S
    ) -> int:
        """Disable all raw serial callbacks previously registered here.

        PyBoy 2.7 does not expose a stable per-callback removal API. The
        guarded callbacks therefore become no-ops, which makes reconnect and
        shutdown safe even when the underlying emulator retains a hook. The
        state transition is serialized with emulator operations and waits no
        longer than ``timeout_s``. Returns the number of callbacks
        deactivated.
        """
        timeout = _validate_timeout(timeout_s, "timeout_s")
        try:
            with self._emulator_access(timeout_s=timeout, allow_closed=True):
                count = 0
                for state, _bank, _addr, _symbol_name in self._serial_hooks:
                    if state.active:
                        state.active = False
                        count += 1
                return count
        except SessionLockTimeout as exc:
            raise SessionLockTimeout(
                "could not acquire the emulator lock before the "
                f"{timeout:g}s deactivation deadline"
            ) from exc

    def deactivate_hooks_at(
        self,
        symbol_name: str,
        *,
        timeout_s: float = _DEFAULT_CLOSE_TIMEOUT_S,
    ) -> None:
        """Best-effort removal of every PyBoy hook at ``symbol_name``.

        This is used for legacy link callbacks installed directly by the
        link endpoint. The guarded callbacks registered through
        :meth:`serial_hook` are also disabled at the same address. Cleanup
        waits at most ``timeout_s`` for the emulator lock, including when the
        session is already closed and only hook teardown remains.
        """
        timeout = _validate_timeout(timeout_s, "timeout_s")
        try:
            with self._emulator_access(timeout_s=timeout, allow_closed=True):
                symbol = self._symbols.get(symbol_name)
                if symbol is None:
                    return
                bank, addr = symbol.bank, symbol.addr
                for state, hook_bank, hook_addr, _hook_symbol in self._serial_hooks:
                    if hook_bank == bank and hook_addr == addr:
                        state.active = False
                self._events.deactivate_at(self._pyboy, bank, addr)
                self._serial_hooks[:] = [
                    record
                    for record in self._serial_hooks
                    if record[1] != bank or record[2] != addr
                ]
        except SessionLockTimeout as exc:
            raise SessionLockTimeout(
                "could not acquire the emulator lock before the "
                f"{timeout:g}s deactivation deadline"
            ) from exc

    # --- actions -------------------------------------------------------

    def bind_timed_execution(
        self, endpoint, *, timeout_s: float = _DEFAULT_CLOSE_TIMEOUT_S
    ) -> None:
        """Route whole public ticks through an already attached timed endpoint.

        Attach and bind on the same thread while holding :meth:`locked`.
        Load fixtures before attachment. This adapter deliberately checks the
        concrete endpoint's private ownership contract; a callable alone cannot
        prove emulator identity or governor ownership. It supplies no peer
        scheduling or rendezvous guarantee.
        """
        from pokered_harness.link.timed_remote import TimedRemoteEndpoint

        timeout_s = _validate_timeout(timeout_s, "timeout_s")
        if not isinstance(endpoint, TimedRemoteEndpoint):
            raise SessionError("timed execution requires a TimedRemoteEndpoint")
        if (
            endpoint._owner != threading.get_ident()
            or endpoint.session._owner != threading.get_ident()
        ):
            raise SessionError("timed execution requires the attaching owner thread")
        self._check_timed_owner()
        with self.locked(timeout_s=min(timeout_s, threading.TIMEOUT_MAX)):
            if self._timed_endpoint is not None:
                raise SessionError("timed execution is already bound")
            self._validate_timed_endpoint(endpoint)
            timed = endpoint.session
            self._timed_attachment = (
                timed._board,
                timed._core,
                timed._previous_backend,
                timed._previous_dispatch,
            )
            self._timed_endpoint = endpoint

    def unbind_timed_execution(
        self, endpoint, *, timeout_s: float = _DEFAULT_CLOSE_TIMEOUT_S
    ) -> None:
        """Close on the owner and restore ordinary execution after detachment.

        The lock wait is bounded; endpoint cleanup itself is synchronous and
        cannot be preempted. A failed cleanup retains the binding, preventing
        an ordinary tick from bypassing a live governor. Retry on the owner.
        """
        timeout_s = _validate_timeout(timeout_s, "timeout_s")
        self._check_timed_owner()
        with self.locked(
            timeout_s=min(timeout_s, threading.TIMEOUT_MAX), allow_closed=True
        ):
            if endpoint is not self._timed_endpoint or endpoint is None:
                raise SessionError("timed execution endpoint does not match binding")
            self._check_timed_owner()
            self._check_timed_idle(endpoint)
            endpoint.close()
            timed = endpoint.session
            if any(value is not None for value in (timed._pyboy, timed._board, timed._core)):
                raise SessionError("timed endpoint cleanup did not detach emulator")
            if timed._adapter is not None and timed._adapter._board is not None:
                raise SessionError("timed endpoint governor remains attached")
            board, core, backend, dispatch = self._timed_attachment
            if (
                self._pyboy.mb is not board
                or board.serial is not core
                or core.backend is not backend
                or core.owner_dispatch_callback is not dispatch[0]
                or core.owner_dispatch_enabled != dispatch[1]
                or board.execution_before is not None
                or board.execution_after is not None
            ):
                raise SessionError("timed endpoint cleanup did not restore native ownership")
            self._timed_endpoint = None
            self._timed_attachment = None

    def cancel_timed_execution(self) -> None:
        """Signal active execution/waits without acquiring the emulator lock.

        Any thread may cancel. The attaching owner must still unbind; neither
        cancellation nor an execution failure restores ordinary tick routing.
        """
        endpoint = self._timed_endpoint
        if endpoint is not None:
            endpoint.cancel()

    def _check_timed_owner(self) -> None:
        endpoint = self._timed_endpoint
        if endpoint is not None and (
            endpoint._owner != threading.get_ident()
            or endpoint.session._owner != threading.get_ident()
        ):
            raise SessionError("timed execution requires the attaching owner thread")

    def _check_timed_idle(self, endpoint) -> None:
        timed = endpoint.session
        if self._timed_executing or any(
            (timed._active, timed._attaching, timed._control_active, timed._in_edge, timed._pumping)
        ):
            raise SessionError("timed execution requires an idle owner boundary")

    def _validate_timed_endpoint(self, endpoint) -> None:
        timed = endpoint.session
        owner = threading.get_ident()
        if endpoint._owner != owner or timed._owner != owner:
            raise SessionError("timed execution requires the attaching owner thread")
        self._check_timed_idle(endpoint)
        endpoint._check_cancelled()
        timed._check()
        if timed._pyboy is not self._pyboy or timed._board is None:
            raise SessionError("timed endpoint is not attached to this Session emulator")
        if type(getattr(self._pyboy, "frame_count", None)) is not int or self._pyboy.frame_count < 0:
            raise SessionError("timed execution requires the native public frame_count")
        timed._verify_registration()

    def _admit_timed_execution(self) -> None:
        if self._timed_endpoint is not None:
            self._validate_timed_endpoint(self._timed_endpoint)

    def step(self, count: int = 1, *, render: bool | None = None) -> None:
        _validate_positive_int(count, "count")
        self._ensure_open()
        self._check_timed_owner()
        with self._emulator_access():
            self._ensure_open()
            self._step_locked(count, render=render)

    def _step_locked(self, count: int, *, render: bool | None = None) -> None:
        """Advance an operation that already owns and passed the session lock.

        ``run_until_event`` is one compound operation: it admits the caller
        once, then performs several bounded steps while retaining ``_lock``.
        ``close()`` publishes ``_closed`` before waiting for that lock, so
        routing each later chunk through public :meth:`step` would reject an
        operation that was already in flight. Keep the admission check in
        :meth:`step` and use this private primitive for the admitted compound
        path.
        """
        # Increment BEFORE pyboy.tick so hooks firing mid-step read the
        # anticipated post-step value. Timed calls reconcile completed frames
        # from PyBoy's real public counter on every outcome, including a
        # successful return while paused or quitting and an interrupted frame.
        # Ordinary execution retains its historical exception rollback.
        self._admit_timed_execution()
        self._validate_link_operation("step")
        endpoint = self._timed_endpoint
        start_frame = self._pyboy.frame_count if endpoint is not None else None
        old_tick = self._tick
        self._tick += count
        if render is None:
            render = self._view
        was_executing = self._timed_executing
        self._timed_executing = True
        try:
            if endpoint is None:
                self._pyboy.tick(count, render=render)
            else:
                endpoint.tick(count, render=render, sound=True)
                self._tick = old_tick + (self._pyboy.frame_count - start_frame)
        except BaseException as exc:
            if endpoint is not None:
                self._tick = old_tick + (self._pyboy.frame_count - start_frame)
            elif isinstance(exc, Exception):
                self._tick = old_tick
            raise
        finally:
            self._timed_executing = was_executing

    def press(self, button: str | Button, *, duration: int = 1) -> None:
        _validate_positive_int(duration, "duration")
        self._ensure_open()
        self._check_timed_owner()
        with self._emulator_access():
            self._ensure_open()
            self._admit_timed_execution()
            name = validate_button(str(button)).value
            self._pyboy.button(name, duration)

    def hold(self, button: str | Button) -> None:
        self._ensure_open()
        self._check_timed_owner()
        with self._emulator_access():
            self._ensure_open()
            self._admit_timed_execution()
            name = validate_button(str(button)).value
            self._pyboy.button_press(name)

    def release(self, button: str | Button) -> None:
        self._ensure_open()
        self._check_timed_owner()
        with self._emulator_access():
            self._ensure_open()
            self._admit_timed_execution()
            name = validate_button(str(button)).value
            self._pyboy.button_release(name)

    # --- observation ---------------------------------------------------

    def read_game_state(self) -> GameState:
        self._ensure_open()
        with self._emulator_access():
            self._ensure_open()
            return self._read_game_state_locked()

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

    def event_snapshot(self) -> list[GameEvent]:
        """Return a consistent copy of the current event log."""
        self._ensure_open()
        with self._emulator_access():
            self._ensure_open()
            return list(self._events)

    # --- save / load ---------------------------------------------------

    def save_state(self) -> bytes:
        self._ensure_open()
        with self._emulator_access():
            self._ensure_open()
            buf = BytesIO()
            self._pyboy.save_state(buf)
            data = buf.getvalue()
            if not data:
                raise InvalidStateError("emulator returned an empty save-state")
            return data

    def load_state(self, data: bytes) -> None:
        if self._timed_endpoint is not None:
            raise SessionError("cannot load state during a bound timed epoch")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise InvalidStateError("save-state must be bytes-like")
        payload = bytes(data)
        if not payload:
            raise InvalidStateError("save-state must not be empty")
        self._ensure_open()
        with self._emulator_access():
            self._ensure_open()
            if self._timed_endpoint is not None:
                raise SessionError("cannot load state during a bound timed epoch")
            self._validate_link_operation("load_state")
            self._pyboy.load_state(BytesIO(payload))
            # After load_state the emulated clock has been restored, but our
            # external tick counter is just bookkeeping — callers can reset
            # it via reset_tick if they care about matching exactly.  The
            # monotonic load-generation counter always advances so callers
            # can detect that a load happened even when the tick repeats.
            self._load_generation += 1
            # A load replaces the emulated state with a different instant;
            # any battle observed before the load must not qualify a
            # terminal outcome afterwards.  Menu entry/exit state recorded
            # before the load is equally stale, so it becomes unknown until
            # a fresh hook event is observed.
            self._invalidate_battle_observations()

    def reset_tick(self, value: int = 0) -> None:
        if self._timed_endpoint is not None:
            raise SessionError("cannot reset tick during a bound timed epoch")
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(  # noqa: TRY004 - preserve the public ValueError contract
                f"tick must be a non-negative integer, got {value!r}"
            )
        if value < 0:
            raise ValueError(f"tick must be non-negative, got {value}")
        self._ensure_open()
        with self._emulator_access():
            self._ensure_open()
            if self._timed_endpoint is not None:
                raise SessionError("cannot reset tick during a bound timed epoch")
            self._tick = value
            self._reset_generation += 1
            # A tick reset starts a distinct observation epoch; stale battle
            # history must not span it.
            self._invalidate_battle_observations()

    def _advance_tick(self, count: int) -> int:
        """Advance the bookkeeping clock for an interleaved link step.

        The linked path drives the emulators directly and only needs the
        session's bookkeeping clock kept in step.  This is ordinary forward
        advancement, not a rewind, so it must not start a new observation
        epoch or discard the battle lifecycle/menu evidence the execution
        hooks recorded while stepping.  :meth:`reset_tick` remains the
        intentional-rewind path that invalidates that evidence.
        """
        if not isinstance(count, int) or isinstance(count, bool) or count < 0:
            raise ValueError(
                f"advance must be a non-negative integer, got {count!r}"
            )
        self._ensure_open()
        with self._emulator_access():
            self._ensure_open()
            if self._timed_endpoint is not None:
                raise SessionError("cannot advance tick during a bound timed epoch")
            self._tick += count
            return self._tick

    # --- event-driven advance -----------------------------------------

    def run_until_event(
        self,
        event_names: str | Iterable[str],
        *,
        max_ticks: int,
        chunk: int = 16,
        render: bool | None = None,
    ) -> RunUntilResult:
        """Tick forward until any of ``event_names`` fires or the budget
        runs out.

        ``chunk`` balances responsiveness against call overhead — larger
        chunks spend fewer Python frames per emulated frame at the cost
        of overshooting the event by up to ``chunk-1`` ticks.
        """
        _validate_positive_int(max_ticks, "max_ticks")
        _validate_positive_int(chunk, "chunk")

        self._ensure_open()
        self._check_timed_owner()
        with self._emulator_access():
            self._ensure_open()
            self._validate_link_operation("run_until_event")
            # Consume an arbitrary iterable only after the optional backend
            # ownership guard has admitted the operation.  Generators can
            # have observable side effects, so validation order is part of
            # the public boundary: numeric payload validation happens first,
            # then owner admission, then event-name materialization.
            wanted = {event_names} if isinstance(event_names, str) else set(event_names)
            if not wanted:
                raise ValueError("event_names must be non-empty")
            start_tick = self._tick
            deadline = start_tick + max_ticks

            while self._tick < deadline:
                for name in wanted:
                    evt = self._events.latest(name)
                    if evt is not None and evt.tick > start_tick:
                        return RunUntilResult(
                            event=evt, ticks_spent=self._tick - start_tick
                        )

                ticks_left = deadline - self._tick
                previous_tick = self._tick
                self._step_locked(min(chunk, ticks_left), render=render)
                if self._timed_endpoint is not None and self._tick == previous_tick:
                    break

            # Final check after the last chunk.
            for name in wanted:
                evt = self._events.latest(name)
                if evt is not None and evt.tick > start_tick:
                    return RunUntilResult(
                        event=evt, ticks_spent=self._tick - start_tick
                    )
            return RunUntilResult(event=None, ticks_spent=self._tick - start_tick)

    def _ensure_open(self) -> None:
        if self._closed:
            raise SessionClosedError("session is closed")
        self._owner.ensure_open()

    def _validate_link_operation(self, operation: str) -> None:
        """Let an attached serial backend reject independently-owned work."""
        motherboard = getattr(self._pyboy, "mb", None)
        serial = getattr(motherboard, "serial", None)
        backend = getattr(serial, "backend", None)
        validate = getattr(backend, "validate_session_operation", None)
        if callable(validate):
            validate(operation)


@contextmanager
def locked_sessions(
    *sessions: Session,
    allow_closed: bool = False,
    timeout_s: float | None = None,
) -> Iterator[None]:
    """Acquire one or more Session owners in canonical identity order.

    Pair operations must enter this group before touching provider/network
    locks.  The ordered owner scope rejects reverse single-owner acquisition
    before it can become a two-thread deadlock; cleanup may opt into closed
    sessions while still waiting for active emulator work to finish.
    """
    with owner_group(
        (session._owner for session in sessions),
        allow_closed=allow_closed,
        timeout=(
            None
            if timeout_s is None
            else _validate_timeout(timeout_s, "timeout_s")
        ),
    ):
        if not allow_closed:
            for session in sessions:
                session._ensure_open()
        yield


def sha1_of_file(path: str | Path, *, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with Path(path).open("rb") as f:
        while True:
            buf = f.read(chunk_size)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


_SHA1_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _validate_timeout(value: float, name: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be finite and non-negative")
    try:
        candidate = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite and non-negative") from exc
    if not math.isfinite(candidate) or candidate < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return candidate


def _validate_close_timeout(value: float) -> float:
    """Validate the strict positive deadline used by :meth:`Session.close`."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(  # noqa: TRY004 - preserve close() ValueError contract
            "timeout_s must be finite and positive, at most threading.TIMEOUT_MAX"
        )
    try:
        candidate = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "timeout_s must be finite and positive, at most threading.TIMEOUT_MAX"
        ) from exc
    if not math.isfinite(candidate) or candidate <= 0:
        raise ValueError(
            "timeout_s must be finite and positive, at most threading.TIMEOUT_MAX"
        )
    # ``threading.Lock.acquire`` rejects values above TIMEOUT_MAX even though
    # a caller may legitimately use a very large finite deadline.  Preserve
    # that public contract by clamping after validation; integer conversion
    # overflow remains a rejected input above.
    return min(candidate, threading.TIMEOUT_MAX)


def _normalise_sha1(value: str, *, label: str = "ROM SHA-1") -> str:
    if not isinstance(value, str):
        raise SessionConfigurationError(
            f"{label} must be exactly 40 hexadecimal characters, got {value!r}"
        )
    candidate = value.strip()
    if not _SHA1_RE.fullmatch(candidate):
        raise SessionConfigurationError(
            f"{label} must be exactly 40 hexadecimal characters, got {value!r}"
        )
    return candidate.lower()


def _validate_positive_int(value: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")


def _default_pyboy_factory(
    rom_path: str, *, window: str = "null", cgb: bool = True
) -> PyBoyLike:
    from pyboy import PyBoy

    # ``window`` is configurable: "null" (headless, ADR default — MCP/tests
    # stay fast and windowless) vs "SDL2" (visible window for local viewing).
    # ``cgb=True`` enables Game Boy Color mode so Pokemon Red renders with
    # its stock CGB auto-palette instead of the DMG grayscale fallback.
    # Headless harness consumers do not expose audio, and PyBoy's default
    # sound emulation is a significant per-frame cost in the source runtime.
    # Keep audio for visible sessions while making the documented headless
    # path deterministic and suitable for bounded automation.
    sound_emulated = window not in {"null", "headless", "dummy"}
    return PyBoy(  # type: ignore[return-value]
        rom_path,
        window=window,
        cgb=cgb,
        sound_emulated=sound_emulated,
    )
