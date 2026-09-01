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

import logging
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from pokered_harness.pyboy_protocol import PyBoyLike
from pokered_harness.symbols.loader import SymbolTable

TickSource = Callable[[], int]
HookCallback = Callable[[object], None]
DEFAULT_EVENT_LOG_CAPACITY = 4096
HookKey = tuple[int, int]
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GameEvent:
    tick: int
    name: str
    bank: int
    addr: int
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class HookRegistration:
    """Owned registration for one callback at one PyBoy address.

    PyBoy exposes deregistration by address rather than by callback.  The
    :class:`EventBus` therefore installs one dispatcher per address and keeps
    individual registrations as logical children of that dispatcher.  Closing
    one registration cannot remove another registration owned by the same
    bus.
    """

    _bus: EventBus
    pyboy: PyBoyLike
    bank: int
    addr: int
    callback: HookCallback
    context: object
    symbol_name: str | None = None
    _active: bool = True

    @property
    def active(self) -> bool:
        return self._active

    def close(self) -> None:
        """Remove this callback from its owning event bus.

        Closing is idempotent so rollback and normal teardown can safely
        share the same cleanup path.
        """
        self._bus.unregister(self)


@dataclass(slots=True)
class RawHookRegistration:
    """Ownership handle for a callback installed outside :class:`EventBus`.

    ``SerialBridge`` predates the event-bus ownership API and registers raw
    callbacks through ``Session.serial_hook``.  The pair keeps one of these
    handles for each callback observed during bridge installation so teardown
    can remove that callback without blindly removing another callback at the
    same address on a multi-hook test double.
    """

    pyboy: PyBoyLike
    bank: int
    addr: int
    callback: HookCallback
    context: object
    _active: bool = True

    @property
    def active(self) -> bool:
        return self._active

    def close(self) -> bool:
        """Remove this raw callback once; return whether it was present."""
        if not self._active:
            return False
        removed = _remove_physical_callback(self)
        self._active = False
        return removed


@dataclass(slots=True)
class _HookSlot:
    pyboy: PyBoyLike
    bank: int
    addr: int
    registrations: list[HookRegistration] = field(default_factory=list)
    dispatcher: HookCallback | None = None


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
        # The key includes object identity because two emulator instances can
        # legitimately use the same ROM address and have independent buses.
        self._hook_slots: dict[tuple[int, int, int], _HookSlot] = {}

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
    ) -> HookRegistration:
        """Register an execution hook at a symbol label.

        ``tick_source`` is any callable returning the current tick number —
        normally :meth:`Session.current_tick`.
        """
        bank, addr = symbols.bank_addr(symbol_name)
        bus = self

        def _callback(_ctx: object) -> None:
            bus.emit(tick=tick_source(), name=event_name, bank=bank, addr=addr)

        return self.register_at(
            pyboy,
            bank,
            addr,
            _callback,
            symbol_name=symbol_name,
        )

    def register_at(
        self,
        pyboy: PyBoyLike,
        bank: int,
        addr: int,
        callback: HookCallback,
        context: object | None = None,
        *,
        symbol_name: str | None = None,
        replace_existing: bool = False,
    ) -> HookRegistration:
        """Register an owned callback at an explicit bank/address.

        Multiple callbacks registered through this bus at the same address
        share one PyBoy dispatcher.  ``replace_existing`` is reserved for a
        caller that deliberately owns the protocol hook currently occupying
        the address (the link pair replaces the bridge's byte-exchange hook).
        It removes that physical hook before installing the dispatcher.  An
        absent hook is normal and represented by PyBoy as ``ValueError``.

        A missing ``hook_deregister`` is tolerated: the empty dispatcher is
        retained and reused on later registrations, so teardown still makes
        old callbacks inert without attempting an unsupported operation.
        """
        key = (id(pyboy), bank, addr)
        slot = self._hook_slots.get(key)
        if slot is None:
            if replace_existing:
                self._deregister_physical(pyboy, bank, addr)

            slot = _HookSlot(pyboy=pyboy, bank=bank, addr=addr)

            def _dispatch(_ctx: object) -> None:
                # Snapshot so a callback may close its own registration
                # without mutating the list being iterated.
                for registration in tuple(slot.registrations):
                    if registration._active:
                        registration.callback(registration.context)

            slot.dispatcher = _dispatch
            pyboy.hook_register(bank, addr, _dispatch, None)
            self._hook_slots[key] = slot

        registration = HookRegistration(
            _bus=self,
            pyboy=pyboy,
            bank=bank,
            addr=addr,
            callback=callback,
            context=context,
            symbol_name=symbol_name,
        )
        slot.registrations.append(registration)
        if symbol_name is not None:
            self._registered_symbols.add(symbol_name)
        return registration

    @staticmethod
    def _deregister_physical(pyboy: PyBoyLike, bank: int, addr: int) -> None:
        deregister = getattr(pyboy, "hook_deregister", None)
        if not callable(deregister):
            return
        try:
            deregister(bank, addr)
        except ValueError:
            # PyBoy raises this when no breakpoint is present at the address.
            pass

    def unregister(self, registration: HookRegistration) -> None:
        """Close one owned callback and release its physical hook if unused."""
        if registration._bus is not self:
            raise ValueError("hook registration belongs to a different EventBus")
        if not registration._active:
            return
        registration._active = False

        key = (id(registration.pyboy), registration.bank, registration.addr)
        slot = self._hook_slots.get(key)
        if slot is None:
            return
        try:
            slot.registrations.remove(registration)
        except ValueError:
            return
        if slot.registrations:
            return

        if slot.dispatcher is None:
            self._hook_slots.pop(key, None)
            return
        physical = RawHookRegistration(
            pyboy=registration.pyboy,
            bank=registration.bank,
            addr=registration.addr,
            callback=slot.dispatcher,
            context=None,
        )
        try:
            removed = physical.close()
        except Exception:
            # The dispatcher may still be installed. Keep the empty slot and
            # its inert callback so later registrations reuse it without
            # duplicating a PyBoy breakpoint. Teardown must not mask the
            # caller's failure.
            _LOGGER.debug(
                "unable to deregister empty event hook slot",
                exc_info=True,
            )
            return
        if not removed:
            # A runtime without hook_deregister must retain the empty logical
            # slot so the next registration reuses its still-installed,
            # inert dispatcher instead of attempting a duplicate physical
            # breakpoint. If the dispatcher disappeared or was replaced,
            # dropping the stale logical slot is safe.
            current = snapshot_hooks(registration.pyboy)
            if current is None or any(
                callback is slot.dispatcher and context is None
                for callback, context in current.get(
                    (registration.bank, registration.addr), ()
                )
            ):
                return
        self._hook_slots.pop(key, None)

    def deactivate_at(self, pyboy: PyBoyLike, bank: int, addr: int) -> None:
        """Make all bus-owned callbacks at an address inactive.

        Session-level legacy cleanup removes every physical hook at an
        address, so any logical registrations for that dispatcher must be
        invalidated too. This keeps a later registration from reusing a
        dispatcher whose physical breakpoint was already removed.
        """
        key = (id(pyboy), bank, addr)
        slot = self._hook_slots.pop(key, None)
        if slot is not None:
            for registration in slot.registrations:
                registration._active = False
            slot.registrations.clear()
        self._deregister_physical(pyboy, bank, addr)

    @property
    def registered_symbols(self) -> frozenset[str]:
        return frozenset(self._registered_symbols)


def _decode_hook_key(raw_key: object) -> HookKey | None:
    """Decode the private key used by PyBoy and the project's fake."""
    if (
        isinstance(raw_key, tuple)
        and len(raw_key) == 2
        and all(isinstance(value, int) and not isinstance(value, bool) for value in raw_key)
    ):
        return raw_key[0], raw_key[1]
    if isinstance(raw_key, int) and not isinstance(raw_key, bool):
        # PyBoy packs bank, address, and the replaced opcode into one integer.
        return (raw_key >> 24) & 0xFF, (raw_key >> 8) & 0xFFFF
    return None


def _decode_hook_value(
    raw_value: object,
) -> tuple[tuple[HookCallback, object], ...] | None:
    """Normalize PyBoy's single callback and the fake's callback list."""
    if isinstance(raw_value, list):
        values = raw_value
    elif (
        isinstance(raw_value, tuple)
        and len(raw_value) == 2
        and callable(raw_value[0])
    ):
        values = (raw_value,)
    else:
        return None

    decoded: list[tuple[HookCallback, object]] = []
    for value in values:
        if not isinstance(value, tuple) or len(value) != 2 or not callable(value[0]):
            return None
        decoded.append((value[0], value[1]))
    return tuple(decoded)


def snapshot_hooks(
    pyboy: PyBoyLike,
) -> dict[HookKey, tuple[tuple[HookCallback, object], ...]] | None:
    """Return the inspectable callback table, or ``None`` if unsupported.

    PyBoy does not expose callback enumeration publicly.  Its supported
    runtime and the repository fake both expose ``_hooks`` with compatible
    shapes, so ownership-sensitive callers can use this optional diagnostic
    capability and fall back conservatively when another implementation does
    not expose it.
    """
    raw_hooks = getattr(pyboy, "_hooks", None)
    if not isinstance(raw_hooks, dict):
        return None
    result: dict[HookKey, list[tuple[HookCallback, object]]] = {}
    for raw_key, raw_value in raw_hooks.items():
        key = _decode_hook_key(raw_key)
        entries = _decode_hook_value(raw_value)
        if key is None or entries is None:
            return None
        result.setdefault(key, []).extend(entries)
    return {key: tuple(entries) for key, entries in result.items()}


def hooks_added_since(
    before: dict[HookKey, tuple[tuple[HookCallback, object], ...]] | None,
    after: dict[HookKey, tuple[tuple[HookCallback, object], ...]] | None,
) -> tuple[tuple[int, int, HookCallback, object], ...]:
    """Return callback records newly present in ``after``.

    Identity, not equality, distinguishes callbacks and contexts.  This is
    important for mutable contexts and for independently created closures.
    """
    if before is None or after is None:
        return ()
    remaining: dict[HookKey, list[tuple[HookCallback, object]]] = {
        key: list(entries) for key, entries in before.items()
    }
    added: list[tuple[int, int, HookCallback, object]] = []
    for (bank, addr), entries in after.items():
        prior = remaining.setdefault((bank, addr), [])
        for callback, context in entries:
            match_index = next(
                (
                    index
                    for index, (old_callback, old_context) in enumerate(prior)
                    if old_callback is callback and old_context is context
                ),
                None,
            )
            if match_index is None:
                added.append((bank, addr, callback, context))
            else:
                prior.pop(match_index)
    return tuple(added)


def _remove_physical_callback(registration: RawHookRegistration) -> bool:
    """Remove one raw callback, preserving other callbacks if visible."""
    key = (registration.bank, registration.addr)
    current = snapshot_hooks(registration.pyboy)
    if current is not None:
        entries = list(current.get(key, ()))
        match_index = next(
            (
                index
                for index, (callback, context) in enumerate(entries)
                if callback is registration.callback and context is registration.context
            ),
            None,
        )
        if match_index is None:
            return False

        raw_hooks = getattr(registration.pyboy, "_hooks", None)
        if isinstance(raw_hooks, dict) and key in raw_hooks:
            raw_value = raw_hooks[key]
            if isinstance(raw_value, list):
                for index, (callback, context) in enumerate(raw_value):
                    if callback is registration.callback and context is registration.context:
                        raw_value.pop(index)
                        if not raw_value:
                            raw_hooks.pop(key, None)
                        return True

        return _deregister_address_preserving(
            registration.pyboy,
            registration.bank,
            registration.addr,
            entries,
            match_index,
        )

    deregister = getattr(registration.pyboy, "hook_deregister", None)
    if not callable(deregister):
        return False
    try:
        deregister(registration.bank, registration.addr)
    except ValueError:
        return False
    return True


def _deregister_address_preserving(
    pyboy: PyBoyLike,
    bank: int,
    addr: int,
    entries: list[tuple[HookCallback, object]],
    owned_index: int,
) -> bool:
    deregister = getattr(pyboy, "hook_deregister", None)
    if not callable(deregister):
        return False
    try:
        deregister(bank, addr)
    except ValueError:
        return False
    for callback, context in entries[:owned_index] + entries[owned_index + 1 :]:
        pyboy.hook_register(bank, addr, callback, context)
    return True


__all__ = [
    "EventBus",
    "GameEvent",
    "HookRegistration",
    "RawHookRegistration",
    "hooks_added_since",
    "snapshot_hooks",
]
