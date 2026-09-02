"""Session manager: owns the PyBoy lifecycle and exposes a typed action/
observation surface to callers (MCP server, scripts, tests).

The session does *not* hard-code the agent loop — it offers ``step``,
``press``, ``save_state``, ``load_state``, and ``run_until_event``
primitives that callers compose. That split matches the ADR boundary
between "emulator control" (this module) and "policy" (external).
"""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Self

from pokered_harness.events.hooks import EventBus, GameEvent, HookRegistration
from pokered_harness.input import Button, validate_button
from pokered_harness.pyboy_protocol import PyBoyLike
from pokered_harness.state import GameState, parse_game_state
from pokered_harness.symbols.loader import SymbolTable, load_sym_file


class SessionError(RuntimeError):
    """Base class for failures at the emulator-session boundary."""

    code = "session_error"


class SessionClosedError(SessionError):
    """Raised when an operation is attempted after session shutdown."""

    code = "session_closed"


class SessionCloseTimeout(SessionError):
    """Raised when shutdown cannot complete before its deadline."""

    code = "session_close_timeout"


class SessionCloseError(SessionError):
    """Raised when the emulator rejects a shutdown attempt."""

    code = "session_close_failed"


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
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._close_done = threading.Event()
        self._close_done.set()
        self._close_owner: int | None = None
        self._stop_thread: threading.Thread | None = None
        self._stop_error: BaseException | None = None
        self._stopped = False
        self._closed = False
        self._event_hooks: list[HookRegistration] = []
        self._serial_hooks: list[tuple[_HookState, int, int, str]] = []
        # ``view`` is stashed for introspection; the actual wiring into the
        # PyBoy factory happens in ``from_files`` where the ROM is loaded.
        self._view = view

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
        timeout_s = _validate_timeout(timeout_s, "timeout_s")
        stop_deadline = time.monotonic() + timeout_s
        current_thread_id = threading.get_ident()
        with self._lifecycle_lock:
            if self._stopped:
                return
            if self._stop_thread is not None or self._close_owner is not None:
                close_done = self._close_done
                owned_by_current_thread = self._close_owner == current_thread_id
            else:
                self._closed = True
                self._close_owner = current_thread_id
                self._close_done.clear()
                self._stop_error = None
                for state, _bank, _addr, _symbol_name in self._serial_hooks:
                    state.active = False
                close_done = None
                owned_by_current_thread = True

        if close_done is not None:
            if not owned_by_current_thread and not close_done.wait(
                timeout=max(0.0, stop_deadline - time.monotonic())
            ):
                raise SessionCloseTimeout(
                    "another session close is still in progress before the "
                    f"{timeout_s:g}s shutdown deadline"
                )
            with self._lifecycle_lock:
                if self._stopped:
                    return
                if self._stop_error is not None:
                    raise SessionCloseError(
                        f"PyBoy.stop failed: {self._stop_error}"
                    ) from self._stop_error
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
                close_done = None
                owned_by_current_thread = True

        lock_acquired = False
        try:
            if not self._lock.acquire(
                timeout=max(0.0, stop_deadline - time.monotonic())
            ):
                raise SessionCloseTimeout(
                    "an emulator operation is still active after "
                    f"the {timeout_s:g}s shutdown deadline"
                )
            lock_acquired = True
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
                        self._pyboy.stop(save=save)
                    except BaseException as exc:  # noqa: BLE001 - publish exact failure
                        error = exc
                    finally:
                        self._lock.release()
                with self._lifecycle_lock:
                    self._stop_error = error
                    if error is None:
                        self._stopped = True
                    self._close_owner = None
                    self._close_done.set()

            worker = threading.Thread(
                target=_stop_worker,
                name="pokered-session-stop",
                daemon=True,
            )
            with self._lifecycle_lock:
                self._stop_thread = worker
                self._close_owner = None
            worker.start()
            if not self._close_done.wait(
                timeout=max(0.0, stop_deadline - time.monotonic())
            ):
                raise SessionCloseTimeout(
                    "PyBoy.stop did not return before the "
                    f"{timeout_s:g}s shutdown deadline"
                )
            with self._lifecycle_lock:
                if self._stopped:
                    return
                if self._stop_error is not None:
                    raise SessionCloseError(
                        f"PyBoy.stop failed: {self._stop_error}"
                    ) from self._stop_error
                raise SessionCloseTimeout(
                    "emulator shutdown did not complete before the "
                    f"{timeout_s:g}s shutdown deadline"
                )
        finally:
            if lock_acquired:
                self._lock.release()
            with self._lifecycle_lock:
                if self._close_owner == current_thread_id:
                    self._close_owner = None
                    # No stop worker was launched, so another close may retry
                    # the lock phase. Do not mark the session stopped.
                    if self._stop_thread is None:
                        self._close_done.set()

    @property
    def closed(self) -> bool:
        return self._closed

    @contextmanager
    def locked(
        self, *, timeout_s: float = _DEFAULT_CLOSE_TIMEOUT_S
    ) -> Iterator[Session]:
        """Serialize a compound operation that touches this emulator.

        Individual session methods already take this same re-entrant lock.
        This context manager is for callers that need a consistent snapshot
        across more than one method without exposing the lock object itself.
        Lock acquisition is bounded by ``timeout_s`` (five seconds by
        default); callers that need a shorter request deadline should pass it
        explicitly.
        """
        timeout = _validate_timeout(timeout_s, "timeout_s")
        acquired = self._lock.acquire(timeout=timeout)
        if not acquired:
            raise SessionLockTimeout(
                "could not acquire the emulator lock before the "
                f"{timeout:g}s deadline"
            )
        try:
            self._ensure_open()
            yield self
        finally:
            self._lock.release()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # --- clock / events ------------------------------------------------

    def current_tick(self) -> int:
        with self._lock:
            return self._tick

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
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            self._ensure_open()
            return self._register_event_hook_at_locked(
                bank,
                addr,
                callback,
                context,
                replace_existing=replace_existing,
            )

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
            with self._lock:
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
        with self._lock:
            self._ensure_open()
            bank, addr = self._symbols.bank_addr(symbol_name)
            state = _HookState()

            def _guarded_callback(ctx: object) -> None:
                # The flag is intentionally checked before taking the
                # session lock. Teardown must be able to deactivate a hook
                # while another thread is blocked in a remote exchange.
                if not state.active or self._closed:
                    return
                with self._lock:
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
        if not self._lock.acquire(timeout=timeout):
            raise SessionLockTimeout(
                "could not acquire the emulator lock before the "
                f"{timeout:g}s deactivation deadline"
            )
        try:
            count = 0
            for state, _bank, _addr, _symbol_name in self._serial_hooks:
                if state.active:
                    state.active = False
                    count += 1
            return count
        finally:
            self._lock.release()

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
        if not self._lock.acquire(timeout=timeout):
            raise SessionLockTimeout(
                "could not acquire the emulator lock before the "
                f"{timeout:g}s deactivation deadline"
            )
        try:
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
        finally:
            self._lock.release()

    # --- actions -------------------------------------------------------

    def step(self, count: int = 1, *, render: bool | None = None) -> None:
        _validate_positive_int(count, "count")
        with self._lock:
            self._ensure_open()
            # Increment BEFORE pyboy.tick so hooks firing mid-step read the
            # post-step tick value. Roll it back if the emulator rejects the
            # tick, so bookkeeping never claims frames that were not run.
            old_tick = self._tick
            self._tick += count
            if render is None:
                render = self._view
            try:
                self._pyboy.tick(count, render=render)
            except Exception:
                self._tick = old_tick
                raise

    def press(self, button: str | Button, *, duration: int = 1) -> None:
        _validate_positive_int(duration, "duration")
        with self._lock:
            self._ensure_open()
            name = validate_button(str(button)).value
            self._pyboy.button(name, duration)

    def hold(self, button: str | Button) -> None:
        with self._lock:
            self._ensure_open()
            name = validate_button(str(button)).value
            self._pyboy.button_press(name)

    def release(self, button: str | Button) -> None:
        with self._lock:
            self._ensure_open()
            name = validate_button(str(button)).value
            self._pyboy.button_release(name)

    # --- observation ---------------------------------------------------

    def read_game_state(self) -> GameState:
        with self._lock:
            self._ensure_open()
            return parse_game_state(self._pyboy.memory, self._symbols)

    def event_snapshot(self) -> list[GameEvent]:
        """Return a consistent copy of the current event log."""
        with self._lock:
            return list(self._events)

    # --- save / load ---------------------------------------------------

    def save_state(self) -> bytes:
        with self._lock:
            self._ensure_open()
            buf = BytesIO()
            self._pyboy.save_state(buf)
            data = buf.getvalue()
            if not data:
                raise InvalidStateError("emulator returned an empty save-state")
            return data

    def load_state(self, data: bytes) -> None:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise InvalidStateError("save-state must be bytes-like")
        payload = bytes(data)
        if not payload:
            raise InvalidStateError("save-state must not be empty")
        with self._lock:
            self._ensure_open()
            self._pyboy.load_state(BytesIO(payload))
            # After load_state the emulated clock has been restored, but our
            # external tick counter is just bookkeeping — callers can reset
            # it via reset_tick if they care about matching exactly.

    def reset_tick(self, value: int = 0) -> None:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(  # noqa: TRY004 - preserve the public ValueError contract
                f"tick must be a non-negative integer, got {value!r}"
            )
        if value < 0:
            raise ValueError(f"tick must be non-negative, got {value}")
        with self._lock:
            self._ensure_open()
            self._tick = value

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

        wanted = {event_names} if isinstance(event_names, str) else set(event_names)
        if not wanted:
            raise ValueError("event_names must be non-empty")

        with self._lock:
            self._ensure_open()
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
                self.step(min(chunk, ticks_left), render=render)

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


def _normalise_sha1(value: str, *, label: str = "ROM SHA-1") -> str:
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
