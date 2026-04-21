"""Bit-accurate Game Boy serial core (design-doc milestone 1).

This is a drop-in replacement for ``pyboy.core.serial.Serial`` that
implements the ``FF01``/``FF02`` synchronous shift-register contract
described in Pan Docs instead of the legacy "force SB=0xFF, count down
8x8192Hz" model in mainline PyBoy.

See :doc:`docs/pyboy_serial_overhaul_design.md` for the full design.
The duck-type matches PyBoy's :class:`Serial` (``SB``, ``SC``,
``set_SB``, ``set_SC``, ``tick``, ``save_state``, ``load_state``,
``_cycles_to_interrupt``) so a :class:`PyBoyLinkSession` can swap it
in via ``pyboy.mb.serial = SerialCore(...)`` without touching PyBoy
internals.

Bit-level model
---------------

Each transfer is 8 edges. At every edge the MSB of the shift register
is clocked out and a peer bit is clocked into the LSB. After edge 8
the shift register holds the received byte, it is copied back to
``SB``, ``SC`` bit 7 clears, and a serial interrupt is requested.

Master (internal clock) vs slave (external clock)
-------------------------------------------------

Master mode (``SC`` bit 0 = 1) generates its own edges at
``CYCLES_PER_EDGE_DMG`` CPU cycles apiece. Slave mode (``SC`` bit 0 = 0)
has no internal timebase; edges must be driven externally via
:meth:`SerialCore.apply_external_edge`. With no peer, a slave transfer
never completes (Pan Docs; Pokémon uses software timeout loops to
recover).

Backend contract
----------------

A :class:`SerialBackend` provides peer bits. Master mode calls
``backend.on_edge(our_bit, ROLE_INTERNAL)`` on every edge and receives
the peer's bit. Slave mode is driven by a coordinator / peer directly
via :meth:`apply_external_edge` — the backend is not consulted because
the slave core has no timebase to ask "is an edge due yet?".

:class:`NullBackend` always returns ``1`` (disconnected cable RX is
pulled high). :class:`LocalBackend.pair()` returns two backends that
swap bits on matching edges — correct only under lockstep coordination
(see the coordinator milestone); without a coordinator the first caller
gets pull-up and the second gets the first's bit, which is by design.
"""

from __future__ import annotations

from typing import Protocol

try:
    from pyboy.utils import MAX_CYCLES as _PYBOY_MAX_CYCLES
except ImportError:  # pragma: no cover - exercised only without PyBoy
    _PYBOY_MAX_CYCLES = 1 << 31

MAX_CYCLES: int = _PYBOY_MAX_CYCLES

#: DMG serial is 8192 Hz. PyBoy's existing constant (128 CPU cycles
#: per edge) is preserved so SerialCore stays compatible with PyBoy's
#: tick loop.
CYCLES_PER_EDGE_DMG: int = 128

#: Full DMG transfer = 8 edges = 1024 cycles.
CYCLES_PER_BYTE_DMG: int = 8 * CYCLES_PER_EDGE_DMG

# --- FF02 (SC) bits --------------------------------------------------------

SC_TRANSFER_ENABLE: int = 0x80  # bit 7: 1 = transfer requested / in progress
SC_CLOCK_SPEED: int = 0x02      # bit 1: CGB fast clock (milestone 9 — unused)
SC_CLOCK_SOURCE: int = 0x01     # bit 0: 1 = internal (master), 0 = external

# --- FF0F (IF) bit for serial -----------------------------------------------

IF_SERIAL: int = 0x08

# --- role constants (passed to SerialBackend.on_edge) ----------------------

ROLE_INTERNAL: int = 1  # we are the master
ROLE_EXTERNAL: int = 0  # we are the slave


class SerialBackend(Protocol):
    """Peer side of the cable.

    Called by :class:`SerialCore` on each master-mode edge to fetch the
    peer's outgoing bit. Slave-mode edges bypass the backend and are
    driven via :meth:`SerialCore.apply_external_edge` by the
    coordinator / peer.
    """

    def on_edge(self, our_bit: int, our_role: int) -> int: ...


class NullBackend:
    """Disconnected cable.

    Pan Docs: the master's RX line is pulled high when no cable is
    attached, so every received bit is ``1`` and the full byte reads as
    ``0xFF``. Slave with no peer never edges, so :meth:`on_edge` is
    never called in slave mode.
    """

    def on_edge(self, our_bit: int, our_role: int) -> int:
        return 1


class LocalBackend:
    """Two in-process backends bridged bit-at-a-time.

    Use :meth:`pair` to build a linked pair. Correct behavior requires
    the coordinator to drive both cores to the same edge cycle before
    reading — under strict lockstep, each side's :meth:`on_edge` call
    deposits into the peer and drains the peer's previous deposit, so
    both sides see the other's real bit.

    Without a coordinator (first caller gets pull-up), that asymmetry
    is the documented limitation of the raw backend; use
    :class:`LockstepCoordinator` (a later milestone) to make it
    symmetric.
    """

    def __init__(self) -> None:
        self._peer: "LocalBackend | None" = None
        # Bit the peer deposited into us, waiting for us to consume.
        self._inbox: int | None = None

    @classmethod
    def pair(cls) -> tuple["LocalBackend", "LocalBackend"]:
        a, b = cls(), cls()
        a._peer = b
        b._peer = a
        return a, b

    @property
    def peer_ready(self) -> bool:
        """``True`` iff the peer has already deposited a bit this round."""
        return self._inbox is not None

    def on_edge(self, our_bit: int, our_role: int) -> int:
        if self._peer is None:
            # Unpaired LocalBackend behaves like NullBackend.
            return 1
        # Deposit our bit for the peer.
        self._peer._inbox = our_bit & 1
        # Drain any bit the peer left for us.
        if self._inbox is None:
            return 1  # pull-up default; lockstep will eliminate this path
        bit, self._inbox = self._inbox, None
        return bit


class SerialCore:
    """Bit-accurate serial shift register.

    Duck-compatible with ``pyboy.core.serial.Serial``:

    * Attributes: ``SB``, ``SC``, ``transfer_enabled``,
      ``internal_clock``, ``_cycles_to_interrupt``, ``last_cycles``,
      ``clock``, ``clock_target``.
    * Methods: ``set_SB(value)``, ``set_SC(value)``, ``tick(cycles)``,
      ``save_state(f)``, ``load_state(f, state_version)``.

    ``tick`` returns ``True`` on the cycle a transfer completes (caller
    fires the serial interrupt), matching PyBoy's Motherboard contract.
    """

    # Matches save-state block tag written by the legacy Serial class;
    # bumped to a new tag once this core replaces the legacy class.
    STATE_VERSION: int = 1

    def __init__(self, backend: SerialBackend | None = None) -> None:
        self.backend: SerialBackend = backend if backend is not None else NullBackend()

        # FF01/FF02 register state.
        self.SB: int = 0xFF  # default pulls up; matches legacy serial's init
        self.SC: int = 0x00

        # Convenience flags PyBoy's mb.py may read directly.
        self.transfer_enabled: int = 0  # 0/1; mirrors SC bit 7
        self.internal_clock: int = 0    # 0/1; mirrors SC bit 0

        # PyBoy cycle accounting (same model as legacy Serial).
        self.last_cycles: int = 0
        self.clock: int = 0
        self.clock_target: int = MAX_CYCLES
        self._cycles_to_interrupt: int = MAX_CYCLES

        # Bit-accurate state.
        self._shift_register: int = 0xFF  # working byte; MSB is next-out bit
        self._bits_remaining: int = 0

    # --- register writes ------------------------------------------------

    def set_SB(self, value: int) -> None:
        """Write ``FF01``.

        Pan Docs: games load the outgoing byte here before arming
        ``SC``. We preserve it verbatim (legacy PyBoy forces ``0xFF``).
        """
        self.SB = value & 0xFF

    def set_SC(self, value: int) -> None:
        """Write ``FF02``.

        Arms a transfer when bit 7 is set. Bit 0 selects internal
        (master) vs external (slave) clock. Bit 1 (CGB fast clock) is
        stored but not acted on in milestone 1.
        """
        self.SC = value & 0xFF
        self.transfer_enabled = 1 if (self.SC & SC_TRANSFER_ENABLE) else 0
        self.internal_clock = 1 if (self.SC & SC_CLOCK_SOURCE) else 0

        if self.transfer_enabled:
            # Fresh transfer: load the shift register from SB. Subsequent
            # writes to SB mid-transfer do NOT disturb the in-flight
            # shift register (Pan Docs is ambiguous here; matching
            # hardware behavior is "the byte snapshotted at start").
            self._shift_register = self.SB
            self._bits_remaining = 8
            if self.internal_clock:
                self.clock_target = self.clock + CYCLES_PER_EDGE_DMG
            else:
                # Slave: never completes without external edges.
                self.clock_target = MAX_CYCLES
        else:
            self._bits_remaining = 0
            self.clock_target = MAX_CYCLES

        self._cycles_to_interrupt = self.clock_target - self.clock

    # --- core tick ------------------------------------------------------

    def tick(self, _cycles: int) -> bool:
        """Advance by ``_cycles - last_cycles`` CPU cycles.

        Returns ``True`` exactly on the cycle the current transfer
        completes and the serial interrupt should fire.
        """
        delta = _cycles - self.last_cycles
        if delta == 0:
            return False
        self.last_cycles = _cycles
        self.clock += delta

        interrupt = False
        # Only master mode ticks progress a transfer; slave mode waits
        # for apply_external_edge.
        if self.transfer_enabled and self.internal_clock:
            while self._bits_remaining > 0 and self.clock >= self.clock_target:
                if self._process_internal_edge():
                    interrupt = True

        self._cycles_to_interrupt = max(0, self.clock_target - self.clock)
        return interrupt

    # --- external clock (slave mode / coordinator) ----------------------

    def apply_external_edge(self, peer_bit: int) -> bool:
        """Advance one edge driven by an external clock.

        Intended for slave mode (or for a coordinator driving both
        cores in lockstep). ``peer_bit`` is the bit the peer is
        shifting to us this edge. Returns ``True`` if the transfer
        completes on this edge.

        Raises ``RuntimeError`` if no transfer is armed or if the core
        is currently in internal-clock mode (master).
        """
        if not self.transfer_enabled:
            raise RuntimeError(
                "apply_external_edge: no transfer armed (SC bit 7 clear)"
            )
        if self.internal_clock:
            raise RuntimeError(
                "apply_external_edge: core is internal-clock (master); "
                "use tick() instead"
            )
        return self._advance_one_edge(peer_bit)

    def peek_out_bit(self) -> int:
        """The bit that will be shifted out on the next edge (MSB of
        the working register). Does not advance state."""
        return (self._shift_register >> 7) & 1

    # --- internals ------------------------------------------------------

    def _process_internal_edge(self) -> bool:
        """Master-mode edge: fetch peer bit from backend, advance one."""
        out_bit = (self._shift_register >> 7) & 1
        peer_bit = self.backend.on_edge(out_bit, ROLE_INTERNAL) & 1
        completed = self._advance_one_edge(peer_bit)
        # Schedule next edge cycle (even if we just completed; the final
        # clock_target is then clamped below).
        self.clock_target += CYCLES_PER_EDGE_DMG
        if completed:
            self.clock_target = MAX_CYCLES
        return completed

    def _advance_one_edge(self, peer_bit: int) -> bool:
        """Shared edge logic: shift out MSB, shift in peer bit, handle
        completion. Returns True if the transfer just finished."""
        self._shift_register = ((self._shift_register << 1) | (peer_bit & 1)) & 0xFF
        self._bits_remaining -= 1
        if self._bits_remaining == 0:
            # Pan Docs: SB latches the received byte; SC bit 7 clears
            # (other bits preserved); serial interrupt requested.
            self.SB = self._shift_register
            self.SC &= (~SC_TRANSFER_ENABLE) & 0xFF
            self.transfer_enabled = 0
            self.clock_target = MAX_CYCLES
            self._cycles_to_interrupt = MAX_CYCLES
            return True
        return False

    # --- save/load (duck-compatible with legacy Serial) ------------------

    def save_state(self, f) -> None:
        f.write(self.SB)
        f.write(self.SC)
        f.write(self.transfer_enabled)
        f.write(self.internal_clock)
        f.write_64bit(self.last_cycles)
        f.write_64bit(self._cycles_to_interrupt)
        f.write_64bit(self.clock)
        f.write_64bit(self.clock_target)
        # Extensions for the bit-accurate core. Legacy PyBoy states
        # don't include these; load_state tolerates their absence.
        f.write(self._shift_register)
        f.write(self._bits_remaining)

    def load_state(self, f, state_version: int) -> None:
        self.SB = f.read()
        self.SC = f.read()
        self.transfer_enabled = f.read()
        self.internal_clock = f.read()
        self.last_cycles = f.read_64bit()
        self._cycles_to_interrupt = f.read_64bit()
        self.clock = f.read_64bit()
        self.clock_target = f.read_64bit()
        # If the saved state was written by this core, recover bit state.
        try:
            self._shift_register = f.read()
            self._bits_remaining = f.read()
        except Exception:
            # Legacy state: synthesize conservative bit state (not in
            # transfer). Matches what the legacy core would have
            # represented anyway.
            self._shift_register = self.SB
            self._bits_remaining = 0


__all__ = [
    "CYCLES_PER_BYTE_DMG",
    "CYCLES_PER_EDGE_DMG",
    "IF_SERIAL",
    "LocalBackend",
    "MAX_CYCLES",
    "NullBackend",
    "ROLE_EXTERNAL",
    "ROLE_INTERNAL",
    "SC_CLOCK_SOURCE",
    "SC_CLOCK_SPEED",
    "SC_TRANSFER_ENABLE",
    "SerialBackend",
    "SerialCore",
]
