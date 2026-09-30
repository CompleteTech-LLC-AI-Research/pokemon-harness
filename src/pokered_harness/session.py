"""Session manager: owns the PyBoy lifecycle and exposes a typed action/
observation surface to callers (MCP server, scripts, tests).

The session does *not* hard-code the agent loop — it offers ``step``,
``press``, ``save_state``, ``load_state``, and ``run_until_event``
primitives that callers compose. That split matches the ADR boundary
between "emulator control" (this module) and "policy" (external).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Self

from pokered_harness._session_mixins import _SessionEventMixin, _SessionTimedMixin
from pokered_harness._session_observation import _SessionObservationMixin
from pokered_harness._session_support import (
    _DEFAULT_CLOSE_TIMEOUT_S,
    InvalidStateError,
    RomHashMismatch,
    RomNotFoundError,
    RunUntilResult,
    SessionCleanupTimeoutError,
    SessionClosedError,
    SessionCloseError,
    SessionCloseTimeout,
    SessionConfigurationError,
    SessionEpoch,
    SessionError,
    SessionLockTimeout,
    StateSnapshot,
    SymbolHashMismatch,
    SymbolNotFoundError,
    VersionMismatch,
    _default_pyboy_factory,
    _HookState,
    _normalise_sha1,
    _StopAttempt,
    _validate_close_timeout,
    _validate_positive_int,
    _validate_timeout,
    locked_sessions,
    sha1_of_file,
)
from pokered_harness.events.hooks import EventBus, GameEvent, HookRegistration
from pokered_harness.input import Button, validate_button
from pokered_harness.ownership import EmulatorOwnershipError, owner_for
from pokered_harness.pyboy_protocol import PyBoyLike
from pokered_harness.state import (
    GameState,
    PartyRecords,
    parse_party_records,
)
from pokered_harness.symbols.loader import SymbolTable, load_sym_file

__all__ = [
    "InvalidStateError",
    "RomHashMismatch",
    "RomNotFoundError",
    "RunUntilResult",
    "Session",
    "SessionCleanupTimeoutError",
    "SessionCloseError",
    "SessionCloseTimeout",
    "SessionClosedError",
    "SessionConfigurationError",
    "SessionEpoch",
    "SessionError",
    "SessionLockTimeout",
    "StateSnapshot",
    "SymbolHashMismatch",
    "SymbolNotFoundError",
    "VersionMismatch",
    "locked_sessions",
    "sha1_of_file",
]

class Session(_SessionEventMixin, _SessionTimedMixin, _SessionObservationMixin):
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
        self._init_observation_state()
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

    @property
    def events(self) -> EventBus:
        return self._events

    @property
    def symbols(self) -> SymbolTable:
        return self._symbols

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

    def read_party_records(self) -> PartyRecords:
        """Read-only per-slot raw party-record digests under the owner lock.

        This mirrors :meth:`read_game_state`: it takes the same emulator owner
        scope and never ticks, writes RAM, touches serial state, or calls a
        gameplay driver.
        """
        self._ensure_open()
        with self._emulator_access():
            self._ensure_open()
            return parse_party_records(self._pyboy.memory, self._symbols)

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
            raise ValueError(f"advance must be a non-negative integer, got {count!r}")
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
