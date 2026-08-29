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

from pyboy.core.serial import (
    NullBackend,
    ROLE_INTERNAL,
    SerialBackend,
    SerialCore,
)


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
        on_peer_transfer_complete: "callable | None" = None,
    ) -> None:
        self._peer = peer
        self._on_peer_transfer_complete = on_peer_transfer_complete

    @property
    def peer(self) -> SerialCore:
        return self._peer

    def on_edge(self, our_bit: int, our_role: int) -> int:
        peer = self._peer
        # Peer must be armed and in slave mode to accept a driven edge.
        # If it's not armed (e.g. hasn't written SC bit 7 yet) or is
        # also in master mode (protocol error / both sides self-clocking),
        # fall back to pull-up so the master sees 0xFF.
        if not peer.transfer_enabled or peer.internal_clock:
            return 1
        peer_bit = peer.peek_out_bit()
        completed = peer.apply_external_edge(our_bit & 1)
        # Fire the peer-side IRQ if the 8th edge just completed the
        # slave's transfer. Without this the halted slave CPU never
        # wakes and Pokémon's tight serial-sync loops stall forever.
        if completed and self._on_peer_transfer_complete is not None:
            self._on_peer_transfer_complete()
        return peer_bit


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
        on_a_transfer_complete: "callable | None" = None,
        on_b_transfer_complete: "callable | None" = None,
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
        self._attached = False
        self.attach()

    # --- attach / detach -----------------------------------------------

    def attach(self) -> None:
        """Install :class:`CoordinatedBackend` on both cores. Idempotent."""
        if self._attached:
            return
        self._prev_backend_a = self._a.backend
        self._prev_backend_b = self._b.backend
        # A's backend drives edges into B; completion on B means B's
        # slave IRQ callback should fire (wakes B's halted CPU).
        self._a.backend = CoordinatedBackend(
            self._b, on_peer_transfer_complete=self._on_b_done
        )
        self._b.backend = CoordinatedBackend(
            self._a, on_peer_transfer_complete=self._on_a_done
        )
        self._attached = True

    def detach(self) -> None:
        """Restore the backends the cores had before :meth:`attach`.
        Idempotent."""
        if not self._attached:
            return
        self._a.backend = self._prev_backend_a or NullBackend()
        self._b.backend = self._prev_backend_b or NullBackend()
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
        masters = [c for c in (self._a, self._b) if c.internal_clock and c.transfer_enabled]
        if len(masters) != 1:
            return False
        master = masters[0]
        target = master.last_cycles + cycles
        return master.tick(target)


__all__ = [
    "CoordinatedBackend",
    "LockstepCoordinator",
]
