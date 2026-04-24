"""Session manager: owns the PyBoy lifecycle and exposes a typed action/
observation surface to callers (MCP server, scripts, tests).

The session does *not* hard-code the agent loop — it offers ``step``,
``press``, ``save_state``, ``load_state``, and ``run_until_event``
primitives that callers compose. That split matches the ADR boundary
between "emulator control" (this module) and "policy" (external).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable, Iterable

from pokered_harness.events.hooks import EventBus, GameEvent
from pokered_harness.input import Button, validate_button
from pokered_harness.pyboy_protocol import PyBoyLike
from pokered_harness.state import GameState, parse_game_state
from pokered_harness.symbols.loader import SymbolTable, load_sym_file


class VersionMismatch(RuntimeError):
    """Raised when the loaded ROM or PyBoy version does not match the pin."""


@dataclass(frozen=True, slots=True)
class RunUntilResult:
    event: GameEvent | None
    ticks_spent: int

    @property
    def reached(self) -> bool:
        return self.event is not None


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
        expected_pyboy_version: str | None = None,
        pyboy_factory: Callable[[str], PyBoyLike] | None = None,
        view: bool = False,
    ) -> "Session":
        rom_path = Path(rom_path)
        sym_path = Path(sym_path)

        if expected_rom_sha1 is not None:
            actual = sha1_of_file(rom_path)
            if actual.lower() != expected_rom_sha1.lower():
                raise VersionMismatch(
                    f"ROM SHA-1 mismatch: expected {expected_rom_sha1}, "
                    f"got {actual} for {rom_path}"
                )

        if expected_pyboy_version is not None:
            import pyboy as _pyboy_module

            actual_version = getattr(_pyboy_module, "__version__", None)
            if actual_version != expected_pyboy_version:
                raise VersionMismatch(
                    f"PyBoy version mismatch: expected {expected_pyboy_version}, "
                    f"got {actual_version}"
                )

        symbols = load_sym_file(sym_path)
        if pyboy_factory is not None:
            # Injected factory (tests, custom wrappers) is called with just
            # the ROM path — it's responsible for its own window/cgb config.
            pyboy = pyboy_factory(str(rom_path))
        else:
            # The built-in factory accepts window/cgb kwargs; ``view`` picks
            # SDL2 for a visible window, otherwise stays headless ("null").
            window = "SDL2" if view else "null"
            pyboy = _default_pyboy_factory(str(rom_path), window=window, cgb=True)
        return cls(pyboy=pyboy, symbols=symbols, view=view)

    # --- lifecycle -----------------------------------------------------

    def close(self, save: bool = False) -> None:
        self._pyboy.stop(save=save)

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # --- clock / events ------------------------------------------------

    def current_tick(self) -> int:
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
        bank, addr = self._symbols.bank_addr(symbol_name)
        self._pyboy.hook_register(bank, addr, callback, context)

    # --- actions -------------------------------------------------------

    def step(self, count: int = 1, *, render: bool | None = None) -> None:
        if count <= 0:
            raise ValueError(f"count must be positive, got {count}")
        # Increment BEFORE pyboy.tick so hooks firing mid-step read the
        # post-step tick value via ``current_tick()``. This makes the
        # invariant "events emitted during step → evt.tick > pre_step_tick"
        # hold, which is what ``run_until_event`` relies on.
        self._tick += count
        if render is None:
            render = self._view
        self._pyboy.tick(count, render=render)

    def press(self, button: str | Button, *, duration: int = 1) -> None:
        name = validate_button(str(button)).value
        self._pyboy.button(name, duration)

    def hold(self, button: str | Button) -> None:
        name = validate_button(str(button)).value
        self._pyboy.button_press(name)

    def release(self, button: str | Button) -> None:
        name = validate_button(str(button)).value
        self._pyboy.button_release(name)

    # --- observation ---------------------------------------------------

    def read_game_state(self) -> GameState:
        return parse_game_state(self._pyboy.memory, self._symbols)

    # --- save / load ---------------------------------------------------

    def save_state(self) -> bytes:
        buf = BytesIO()
        self._pyboy.save_state(buf)
        return buf.getvalue()

    def load_state(self, data: bytes) -> None:
        self._pyboy.load_state(BytesIO(data))
        # After load_state the emulated clock has been restored, but our
        # external tick counter is just bookkeeping — callers can reset it
        # via ``reset_tick`` if they care about matching exactly.

    def reset_tick(self, value: int = 0) -> None:
        if value < 0:
            raise ValueError(f"tick must be non-negative, got {value}")
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
        if max_ticks <= 0:
            raise ValueError(f"max_ticks must be positive, got {max_ticks}")
        if chunk <= 0:
            raise ValueError(f"chunk must be positive, got {chunk}")

        wanted = {event_names} if isinstance(event_names, str) else set(event_names)
        if not wanted:
            raise ValueError("event_names must be non-empty")

        start_tick = self._tick
        deadline = start_tick + max_ticks

        while self._tick < deadline:
            for name in wanted:
                evt = self._events.latest(name)
                if evt is not None and evt.tick > start_tick:
                    return RunUntilResult(event=evt, ticks_spent=self._tick - start_tick)

            ticks_left = deadline - self._tick
            self.step(min(chunk, ticks_left), render=render)

        # Final check after the last chunk.
        for name in wanted:
            evt = self._events.latest(name)
            if evt is not None and evt.tick > start_tick:
                return RunUntilResult(event=evt, ticks_spent=self._tick - start_tick)
        return RunUntilResult(event=None, ticks_spent=self._tick - start_tick)


def sha1_of_file(path: str | Path, *, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha1()
    with Path(path).open("rb") as f:
        while True:
            buf = f.read(chunk_size)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _default_pyboy_factory(
    rom_path: str, *, window: str = "null", cgb: bool = True
) -> PyBoyLike:
    from pyboy import PyBoy

    # ``window`` is configurable: "null" (headless, ADR default — MCP/tests
    # stay fast and windowless) vs "SDL2" (visible window for local viewing).
    # ``cgb=True`` enables Game Boy Color mode so Pokemon Red renders with
    # its stock CGB auto-palette instead of the DMG grayscale fallback.
    return PyBoy(rom_path, window=window, cgb=cgb)  # type: ignore[return-value]
