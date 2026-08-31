"""Symbol-labelled execution hooks and an in-memory event bus.

The ADR anchors this module: hooks are "the durable substitute for a
missing global-mode byte" — there is no single RAM location that reports
'dialog opened' or 'yes/no prompt shown' in pokered, but the control-flow
always passes through a labelled routine. Registering a PyBoy execution
hook at that label turns the routine's invocation into an observable
semantic event.

Typical wiring at session startup::

    bus = EventBus()
    bus.register(pyboy, symbols, "DisplayTextID", "dialog_open",
                 tick_source=session.current_tick)
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from pokered_harness.pyboy_protocol import PyBoyLike
from pokered_harness.symbols.loader import SymbolTable

TickSource = Callable[[], int]
DEFAULT_EVENT_LOG_CAPACITY = 4096


@dataclass(frozen=True, slots=True)
class GameEvent:
    tick: int
    name: str
    bank: int
    addr: int
    payload: dict[str, Any] = field(default_factory=dict)


class EventBus:
    """Latched log of semantic events emitted by execution hooks.

    The bus does not own the current tick counter — the session passes it
    in via ``tick_source`` when emitting. That keeps the bus free of any
    emulator dependency and therefore fully unit-testable.
    """

    def __init__(self, capacity: int = DEFAULT_EVENT_LOG_CAPACITY) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity must be positive, got {capacity}")
        self._log: deque[GameEvent] = deque(maxlen=capacity)
        self._counts: dict[str, int] = {}
        self._latest: dict[str, GameEvent] = {}
        self._registered_symbols: set[str] = set()

    # --- emission -------------------------------------------------------

    def emit(
        self,
        *,
        tick: int,
        name: str,
        bank: int,
        addr: int,
        payload: dict[str, Any] | None = None,
    ) -> GameEvent:
        event = GameEvent(
            tick=tick, name=name, bank=bank, addr=addr,
            payload=dict(payload) if payload else {},
        )
        self._log.append(event)
        self._counts[name] = self._counts.get(name, 0) + 1
        self._latest[name] = event
        return event

    # --- queries --------------------------------------------------------

    def __len__(self) -> int:
        return len(self._log)

    def __iter__(self) -> Iterator[GameEvent]:
        return iter(self._log)

    def count(self, name: str) -> int:
        return self._counts.get(name, 0)

    def latest(self, name: str) -> GameEvent | None:
        return self._latest.get(name)

    def events_since(self, tick: int) -> list[GameEvent]:
        """Return all events whose ``tick`` field is strictly greater than
        ``tick``. Cheap linear scan — event volume is tiny vs tick rate."""
        return [e for e in self._log if e.tick > tick]

    def clear(self) -> None:
        self._log.clear()
        self._counts.clear()
        self._latest.clear()

    # --- registration ---------------------------------------------------

    def register(
        self,
        pyboy: PyBoyLike,
        symbols: SymbolTable,
        symbol_name: str,
        event_name: str,
        *,
        tick_source: TickSource,
    ) -> None:
        """Register an execution hook at a symbol label.

        ``tick_source`` is any callable returning the current tick number —
        normally :meth:`Session.current_tick`.
        """
        bank, addr = symbols.bank_addr(symbol_name)
        bus = self

        def _callback(_ctx: object) -> None:
            bus.emit(tick=tick_source(), name=event_name, bank=bank, addr=addr)

        pyboy.hook_register(bank, addr, _callback, None)
        self._registered_symbols.add(symbol_name)

    @property
    def registered_symbols(self) -> frozenset[str]:
        return frozenset(self._registered_symbols)
