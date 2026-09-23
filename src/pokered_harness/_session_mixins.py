"""Private method mixins for :mod:`pokered_harness.session` (issue #144).

Holds the hook-registration/deactivation methods and the timed-execution
binding methods extracted from ``Session`` so the canonical module stays below
the 1000-line split target.  The methods are moved verbatim; ``Session``
composes them via multiple inheritance and nothing here is public API.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from pokered_harness._session_support import (
    _DEFAULT_CLOSE_TIMEOUT_S,
    SessionError,
    SessionLockTimeout,
    _HookState,
    _validate_timeout,
)
from pokered_harness.events.hooks import HookRegistration


class _SessionEventMixin:
    """Hook registration and deactivation methods for ``Session``."""

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


class _SessionTimedMixin:
    """Timed-execution binding methods for ``Session``."""

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
