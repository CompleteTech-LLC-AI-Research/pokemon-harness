"""Session manager: owns the PyBoy lifecycle and exposes a typed action/
observation surface to callers (MCP server, scripts, tests).

The session does *not* hard-code the agent loop — it offers ``step``,
``press``, ``save_state``, ``load_state``, and ``run_until_event``
primitives that callers compose. That split matches the ADR boundary
between "emulator control" (this module) and "policy" (external).
"""

from __future__ import annotations

import hashlib
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable, Iterable, Iterator

from pokered_harness.events.hooks import EventBus, GameEvent
from pokered_harness.input import Button, validate_button
from pokered_harness.pyboy_protocol import PyBoyLike
from pokered_harness.state import GameState, parse_game_state
from pokered_harness.symbols.loader import SymbolTable, load_sym_file

PINNED_PYBOY_VERSION = "2.7.0"
PINNED_PYBOY_REVISION = "c565df66c3731fad2856169a90f6bbec99925915"


class SessionError(RuntimeError):
    """Base class for failures at the emulator-session boundary."""

    code = "session_error"


class SessionClosedError(SessionError):
    """Raised when an operation is attempted after session shutdown."""

    code = "session_closed"


class SessionConfigurationError(ValueError):
    """Raised when a ROM, symbol file, or version pin is unusable."""

    code = "invalid_session_configuration"


class InvalidStateError(ValueError):
    """Raised when a save-state payload is not usable."""

    code = "invalid_state"


class VersionMismatch(SessionError):
    """Raised when the loaded ROM or PyBoy version does not match the pin."""

    code = "version_mismatch"


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
        self._stop_lock = threading.Lock()
        self._closed = False
        self._stop_complete = False
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
        pyboy_factory: Callable[[str], PyBoyLike] | None = None,
        view: bool = False,
    ) -> "Session":
        rom_path = Path(rom_path)
        sym_path = Path(sym_path)

        if not rom_path.is_file():
            raise SessionConfigurationError(f"ROM file not found: {rom_path}")
        if not sym_path.is_file():
            raise SessionConfigurationError(
                f"symbol file not found: {sym_path}"
            )

        if expected_rom_sha1 is not None:
            expected_rom_sha1 = _normalise_sha1(expected_rom_sha1)
            actual = sha1_of_file(rom_path)
            if actual.lower() != expected_rom_sha1.lower():
                raise VersionMismatch(
                    f"ROM SHA-1 mismatch: expected {expected_rom_sha1}, "
                    f"got {actual} for {rom_path}"
                )

        if expected_pyboy_version is not None:
            expected_pyboy_version = expected_pyboy_version.strip()
            if not expected_pyboy_version:
                raise SessionConfigurationError(
                    "expected PyBoy version must not be empty"
                )
            import pyboy as _pyboy_module

            actual_version = getattr(_pyboy_module, "__version__", None)
            if actual_version != expected_pyboy_version:
                raise VersionMismatch(
                    f"PyBoy version mismatch: expected {expected_pyboy_version}, "
                    f"got {actual_version}"
                )
            actual_revision = getattr(_pyboy_module, "__pokered_harness_revision__", None)
            if actual_revision != PINNED_PYBOY_REVISION:
                raise VersionMismatch(
                    "PyBoy runtime is not the pinned pokered-harness build; "
                    "revision mismatch: expected "
                    f"{PINNED_PYBOY_REVISION}, got {actual_revision!r}"
                )

        if expected_symbol_sha1 is not None:
            expected_symbol_sha1 = _normalise_sha1(expected_symbol_sha1)
            actual_symbol = sha1_of_file(sym_path)
            if actual_symbol.lower() != expected_symbol_sha1.lower():
                raise VersionMismatch(
                    f"symbol SHA-1 mismatch: expected {expected_symbol_sha1}, "
                    f"got {actual_symbol} for {sym_path}"
                )

        try:
            symbols = load_sym_file(sym_path)
        except (OSError, UnicodeError) as exc:
            raise SessionConfigurationError(
                f"unable to load symbol file {sym_path}: {exc}"
            ) from exc
        if pyboy_factory is not None:
            # Injected factory (tests, custom wrappers) is called with just
            # the ROM path — it's responsible for its own window/cgb config.
            try:
                pyboy = pyboy_factory(str(rom_path))
            except Exception as exc:  # noqa: BLE001
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
            except Exception as exc:  # noqa: BLE001
                raise SessionConfigurationError(
                    f"unable to create emulator for {rom_path}: {exc}"
                ) from exc
        return cls(pyboy=pyboy, symbols=symbols, view=view)

    # --- lifecycle -----------------------------------------------------

    def close(self, save: bool = False) -> None:
        # Mark the session closed before waiting on the emulator lock. A raw
        # serial hook can be blocked in a network exchange while the PyBoy
        # tick lock is held; teardown must still make guarded callbacks no-op
        # and return promptly instead of waiting behind that exchange.
        with self._lifecycle_lock:
            if not self._closed:
                self._closed = True
                for state, _bank, _addr, _symbol_name in self._serial_hooks:
                    state.active = False
            if self._stop_complete:
                return
        with self._stop_lock:
            with self._lifecycle_lock:
                if self._stop_complete:
                    return
            self._pyboy.stop(save=save)
            with self._lifecycle_lock:
                self._stop_complete = True

    @property
    def closed(self) -> bool:
        return self._closed

    @contextmanager
    def locked(self) -> Iterator["Session"]:
        """Serialize a compound operation that touches this emulator.

        Individual session methods already take this same re-entrant lock.
        This context manager is for callers that need a consistent snapshot
        across more than one method without exposing the lock object itself.
        """
        with self._lock:
            self._ensure_open()
            yield self

    def __enter__(self) -> "Session":
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

    def register_hook(self, symbol_name: str, event_name: str) -> None:
        """Register an execution hook at a symbol label, tagging fired
        events with the current tick. Encapsulates the ``EventBus``
        interaction so callers don't reach into ``_pyboy``."""
        with self._lock:
            self._ensure_open()
            self._events.register(
                pyboy=self._pyboy,
                symbols=self._symbols,
                symbol_name=symbol_name,
                event_name=event_name,
                tick_source=self.current_tick,
            )

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

    def deactivate_serial_hooks(self) -> int:
        """Disable all raw serial callbacks previously registered here.

        PyBoy 2.7 does not expose a stable per-callback removal API. The
        guarded callbacks therefore become no-ops, which makes reconnect and
        shutdown safe even when the underlying emulator retains a hook.
        Returns the number of callbacks deactivated.
        """
        count = 0
        for state, _bank, _addr, _symbol_name in self._serial_hooks:
            if state.active:
                state.active = False
                count += 1
        return count

    def deactivate_hooks_at(self, symbol_name: str) -> None:
        """Best-effort removal of every PyBoy hook at ``symbol_name``.

        This is used for legacy link callbacks installed directly by the
        link endpoint. The guarded callbacks registered through
        :meth:`serial_hook` are also disabled at the same address.
        """
        with self._lock:
            self._ensure_open()
            bank, addr = self._symbols.bank_addr(symbol_name)
            for state, hook_bank, hook_addr, _hook_symbol in self._serial_hooks:
                if hook_bank == bank and hook_addr == addr:
                    state.active = False
            deregister = getattr(self._pyboy, "hook_deregister", None)
            if deregister is not None:
                deregister(bank, addr)

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
            raise ValueError(f"tick must be a non-negative integer, got {value!r}")
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


def _normalise_sha1(value: str) -> str:
    candidate = value.strip()
    if not _SHA1_RE.fullmatch(candidate):
        raise SessionConfigurationError(
            f"ROM SHA-1 must be exactly 40 hexadecimal characters, got {value!r}"
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
    return PyBoy(rom_path, window=window, cgb=cgb)  # type: ignore[return-value]
