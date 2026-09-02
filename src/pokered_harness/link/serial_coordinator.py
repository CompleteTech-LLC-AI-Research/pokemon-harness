"""Lockstep coordinator for bit-accurate :class:`SerialCore` pairs.

Implements milestone 3 of the PyBoy serial-overhaul design
(:doc:`docs/pyboy_serial_overhaul_design.md`). Given two
:class:`SerialCore` instances this installs a
:class:`CoordinatedBackend` on each that exchanges a bit atomically at
every master-mode edge, emulating a physical link cable between two
Game Boys.

Design
------

Only *one* side is master (``SC`` bit 0 = 1) at a time; the other is
slave (``SC`` bit 0 = 0). The master's internal clock generates the
edges. When the master's :meth:`SerialCore.tick` hits an edge it calls
``backend.on_edge(our_bit, ROLE_INTERNAL)``; our
:class:`CoordinatedBackend` responds by:

1. Peeking the slave's outgoing bit (``peer.peek_out_bit()``).
2. Shifting the master's outgoing bit into the slave
   (``peer.apply_external_edge(our_bit)``) — this advances the slave
   one edge and completes the slave's transfer on the 8th call.
3. Returning the slave's peeked bit so the master shifts it in and
   advances its own edge.

Both cores advance by exactly one edge per master edge, so the pair
stays deterministically aligned without a separate "scheduler" —
the master's own tick-loop is the clock.

Edge cases
----------

* **Peer not armed.** If the master fires an edge while the slave
  hasn't armed a transfer yet (``transfer_enabled == 0``), the
  coordinator returns pull-up (``1``) — matching Pan Docs disconnect
  behavior. Pokémon's own sync loops use this "read 0xFF until peer
  is ready" signal to detect the other side has booted.
* **Peer also in master mode.** Two masters on one cable is a
  protocol error; the coordinator treats the peer as "not reachable"
  and returns pull-up. Game code would never emit this; guards against
  buggy tests.
* **Role swap mid-session.** After a transfer completes, either side
  can arm a new transfer with a different ``SC`` bit 0. The coordinator
  stays correct because ``CoordinatedBackend`` inspects the peer's
  current state at each call, not at attach time.

The coordinator itself has no step method: the master core's
:meth:`SerialCore.tick` advances both sides. Higher-level sessions
(notably ``PyBoyLinkSession.local()`` — milestone 4) layer per-frame
scheduling on top.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from types import TracebackType
from typing import Any, Self, TypeVar

from pokered_harness.link.serial_core import (
    NullBackend,
    SerialBackend,
    SerialCore,
)

_T = TypeVar("_T")


def _validate_positive_cycles(cycles: int) -> int:
    if isinstance(cycles, bool) or not isinstance(cycles, int):
        raise TypeError("cycles must be a positive integer")
    if cycles <= 0:
        raise ValueError("cycles must be a positive integer")
    return cycles


class SerialOperationGate(AbstractContextManager[Self]):
    """Serialize every operation which can touch one emulator's serial core.

    PyBoy calls ``Serial.tick`` from its motherboard thread, while a remote
    link may receive an externally clocked edge on a network thread.  A
    ``SerialOperationGate`` is the small, runtime-independent part of the
    ownership contract: the caller that drives PyBoy holds it across the
    complete emulator tick, and the owner-side network dispatcher holds it
    while applying one queued edge and raising its IRQ.

    The lock is deliberately re-entrant.  PyBoy callbacks and compatibility
    adapters can make a nested call into the link layer without creating a
    self-deadlock, while distinct threads still cannot overlap native serial
    operations.  This class does not move work to a hidden thread and does
    not alter Python's scheduler.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def __enter__(self) -> Self:
        self._lock.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._lock.release()

    def run(self, callback: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        """Run one callback while holding the gate."""
        with self:
            return callback(*args, **kwargs)


class CoordinatedBackend:
    """Backend that atomically exchanges a bit with a peer :class:`SerialCore`.

    Installed by :class:`LockstepCoordinator` on each paired core.
    When master-mode ``on_edge`` fires, peeks the peer's out-bit,
    shifts our bit into the peer (advancing its transfer by one edge),
    and returns the peer's peeked bit.

    When the peer-side transfer completes (8th edge), the slave's CPU
    also needs a serial IRQ so halted code (the common Pokémon idiom
    ``halt; wait for INTR_SERIAL``) wakes up and processes the received
    byte. The coordinator-side callback ``on_peer_transfer_complete``
    is invoked for that purpose when provided.
    """

    def __init__(
        self,
        peer: SerialCore,
        on_peer_transfer_complete: Callable[[], None] | None = None,
        on_peer_unarmed: Callable[[], bool] | None = None,
        *,
        active: bool = True,
    ) -> None:
        self._peer = peer
        self._on_peer_transfer_complete = on_peer_transfer_complete
        self._on_peer_unarmed = on_peer_unarmed
        self._lifecycle_lock = threading.RLock()
        self._active = active
        # Lightweight counters make real-ROM diagnostics able to
        # distinguish a clean cable exchange from pull-up fallback. They
        # are intentionally plain integers: local coordination is driven
        # by one emulator thread.
        self.edge_count = 0
        self.peer_unarmed_edges = 0
        self.peer_master_edges = 0
        self.peer_rearm_attempts = 0
        self.peer_rearm_successes = 0

    @property
    def peer(self) -> SerialCore:
        return self._peer

    def prepare_peer_for_edge(self) -> bool:
        """Give a same-process peer a bounded chance to arm before an edge."""
        with self._lifecycle_lock:
            return self._prepare_peer_for_edge_locked()

    def _prepare_peer_for_edge_locked(self) -> bool:
        if not self._active:
            return False
        peer = self._peer
        if peer.transfer_enabled:
            return not peer.internal_clock
        if self._on_peer_unarmed is None:
            return False
        for _ in range(256):
            if peer.transfer_enabled or peer.internal_clock:
                break
            self.peer_rearm_attempts += 1
            if not self._on_peer_unarmed():
                break
        if peer.transfer_enabled and not peer.internal_clock:
            self.peer_rearm_successes += 1
            return True
        return False

    def on_edge(self, our_bit: int, our_role: int) -> int:
        """Exchange one bit, or return the pulled-up line when inactive."""
        with self._lifecycle_lock:
            return self._on_edge_locked(our_bit, our_role)

    def _on_edge_locked(self, our_bit: int, our_role: int) -> int:
        if not self._active:
            return 1
        peer = self._peer
        self.edge_count += 1
        # Peer must be armed and in slave mode to accept a driven edge.
        # If it's not armed (e.g. hasn't written SC bit 7 yet) or is
        # also in master mode (protocol error / both sides self-clocking),
        # fall back to pull-up so the master sees 0xFF.
        if not peer.transfer_enabled and self._on_peer_unarmed is not None:
            # In a same-process pair the peer cannot execute while this
            # master callback is active. Give it a bounded cooperative
            # chance to reach the ROM's SB/SC re-arm point before treating
            # the line as disconnected.
            self._prepare_peer_for_edge_locked()
        if not peer.transfer_enabled:
            self.peer_unarmed_edges += 1
            return 1
        if peer.internal_clock:
            self.peer_master_edges += 1
            return 1
        peer_bit = peer.peek_out_bit()
        completed = peer.apply_external_edge(our_bit & 1)
        # Fire the peer-side IRQ if the 8th edge just completed the
        # slave's transfer. Without this the halted slave CPU never
        # wakes and Pokémon's tight serial-sync loops stall forever.
        if completed and self._on_peer_transfer_complete is not None:
            self._on_peer_transfer_complete()
        return peer_bit

    @property
    def active(self) -> bool:
        with self._lifecycle_lock:
            return self._active

    def activate(self) -> None:
        with self._lifecycle_lock:
            self._active = True

    def deactivate(self) -> None:
        """Stop future edge callbacks after any in-flight callback completes."""
        with self._lifecycle_lock:
            self._active = False


class LockstepCoordinator:
    """Pairs two :class:`SerialCore` instances for bit-accurate exchange.

    Usage::

        a = SerialCore()
        b = SerialCore()
        coord = LockstepCoordinator(a, b)
        # Now arm one side as master and the other as slave; master.tick()
        # drives both:
        a.set_SB(0xAA); b.set_SB(0x55)
        a.set_SC(0x81)    # master
        b.set_SC(0x80)    # slave
        a.tick(1024)      # 8 edges — full byte exchanged
        assert a.SB == 0x55 and b.SB == 0xAA

    :meth:`detach` restores :class:`NullBackend` on both cores so they
    revert to disconnected behavior.
    """

    def __init__(
        self,
        core_a: SerialCore,
        core_b: SerialCore,
        *,
        on_a_transfer_complete: Callable[[], None] | None = None,
        on_b_transfer_complete: Callable[[], None] | None = None,
        on_a_peer_unarmed: Callable[[], bool] | None = None,
        on_b_peer_unarmed: Callable[[], bool] | None = None,
    ) -> None:
        """
        ``on_a_transfer_complete`` / ``on_b_transfer_complete`` are
        optional callbacks invoked when the respective core's slave-mode
        transfer completes via a peer-driven edge (see
        :class:`CoordinatedBackend`). :class:`PyBoyLinkSession` wires
        these to each motherboard's ``cpu.set_interruptflag(INTR_SERIAL)``
        so halted game code wakes on receive.
        """
        if core_a is core_b:
            raise ValueError("coordinator requires two distinct cores")
        self._a = core_a
        self._b = core_b
        self._prev_backend_a: SerialBackend | None = core_a.backend
        self._prev_backend_b: SerialBackend | None = core_b.backend
        self._on_a_done = on_a_transfer_complete
        self._on_b_done = on_b_transfer_complete
        self._on_a_peer_unarmed = on_a_peer_unarmed
        self._on_b_peer_unarmed = on_b_peer_unarmed
        self._attached = False
        self._lifecycle_lock = threading.RLock()
        self._backend_a: CoordinatedBackend | None = None
        self._backend_b: CoordinatedBackend | None = None
        self.attach()

    # --- attach / detach -----------------------------------------------

    def attach(self) -> None:
        """Install :class:`CoordinatedBackend` on both cores. Idempotent."""
        with self._lifecycle_lock:
            if self._attached:
                return
            prev_backend_a = self._a.backend
            prev_backend_b = self._b.backend
            # A's backend drives edges into B; completion on B means B's
            # slave IRQ callback should fire (wakes B's halted CPU).
            backend_a = CoordinatedBackend(
                self._b,
                on_peer_transfer_complete=self._on_b_done,
                on_peer_unarmed=self._on_a_peer_unarmed,
                active=False,
            )
            backend_b = CoordinatedBackend(
                self._a,
                on_peer_transfer_complete=self._on_a_done,
                on_peer_unarmed=self._on_b_peer_unarmed,
                active=False,
            )
            try:
                self._a.backend = backend_a
                self._b.backend = backend_b
                # Do not let a concurrently ticking core use a half-attached
                # pair while the second backend assignment is still pending.
                backend_a.activate()
                backend_b.activate()
            except BaseException as exc:
                # A partially attached coordinator must never leave one core
                # pointing at a backend whose peer side was not installed.
                backend_a.deactivate()
                backend_b.deactivate()
                rollback_errors: list[BaseException] = []
                try:
                    if self._a.backend is backend_a:
                        self._a.backend = prev_backend_a
                except BaseException as rollback_error:  # noqa: BLE001 - preserve original attach failure during rollback
                    rollback_errors.append(rollback_error)
                try:
                    if self._b.backend is backend_b:
                        self._b.backend = prev_backend_b
                except BaseException as rollback_error:  # noqa: BLE001 - preserve original attach failure during rollback
                    rollback_errors.append(rollback_error)
                for rollback_error in rollback_errors:
                    exc.add_note(
                        f"coordinator attach rollback failed: {rollback_error!r}"
                    )
                raise
            self._prev_backend_a = prev_backend_a
            self._prev_backend_b = prev_backend_b
            self._backend_a = backend_a
            self._backend_b = backend_b
            self._attached = True

    def detach(self) -> None:
        """Restore the backends the cores had before :meth:`attach`.
        Idempotent."""
        with self._lifecycle_lock:
            if not self._attached:
                return
            errors: list[Exception] = []
            for core, backend, previous in (
                (self._a, self._backend_a, self._prev_backend_a),
                (self._b, self._backend_b, self._prev_backend_b),
            ):
                if backend is not None:
                    backend.deactivate()
                # Do not overwrite a backend installed by another owner
                # after attach; only restore the coordinator's own object.
                if core.backend is not backend:
                    continue
                try:
                    core.backend = previous if previous is not None else NullBackend()
                except Exception as exc:  # noqa: BLE001 - detach both sides
                    errors.append(exc)
            if errors:
                # Keep the handles and attached state so a caller can retry
                # restoration after a transient native backend-assignment
                # failure. The successfully restored side is skipped on the
                # retry because it no longer points at its coordinator
                # backend; the failed side remains identifiable and safe to
                # clean up.
                raise RuntimeError(
                    "one or more coordinated serial backends could not be detached"
                ) from errors[0]
            self._backend_a = None
            self._backend_b = None
            self._attached = False

    @property
    def attached(self) -> bool:
        return self._attached

    # --- introspection -------------------------------------------------

    @property
    def core_a(self) -> SerialCore:
        return self._a

    @property
    def core_b(self) -> SerialCore:
        return self._b

    # --- optional: coordinator-driven step helper ----------------------

    def advance_master(self, cycles: int) -> bool:
        """Convenience: advance whichever core is currently master by
        ``cycles`` CPU cycles.

        Returns ``True`` if a master-side transfer completed during
        the advance. If neither side is master (or both are), returns
        ``False`` without advancing — the caller is expected to handle
        that condition itself.

        Most callers should advance cores directly via
        :meth:`SerialCore.tick` and let the coordinator's backends do
        their job transparently; this helper exists for simple tests
        and examples.
        """
        cycles = _validate_positive_cycles(cycles)
        with self._lifecycle_lock:
            masters = [
                c for c in (self._a, self._b)
                if c.internal_clock and c.transfer_enabled
            ]
            if len(masters) != 1:
                return False
            master = masters[0]
            target = master.last_cycles + cycles
            return master.tick(target)


__all__ = [
    "CoordinatedBackend",
    "LockstepCoordinator",
    "SerialOperationGate",
]
