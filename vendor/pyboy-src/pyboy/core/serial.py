#
# License: See LICENSE.md file
# GitHub: https://github.com/Baekalfen/PyBoy
#
"""Bit-accurate serial shift-register core.

The :class:`Serial` class below is a drop-in replacement for PyBoy's
upstream 8192 Hz countdown serial. It models FF01/FF02 bit-by-bit so a
coordinator / network backend can drive edges symmetrically (master-
side via ``tick`` + backend, slave-side via :meth:`apply_external_edge`).

Motherboard contract:

* ``__init__(cgb_mode, *, backend=None)`` — default backend is
  :class:`NullBackend`, which makes the cable appear disconnected
  (hardware pulls peer bits high -> received byte is 0xFF).
* ``tick(cycles)`` returns ``True`` on the cycle the current transfer
  completes (mb.py fires INTR_SERIAL).
* ``set_SB(value)`` / ``set_SC(value)`` follow Pan Docs semantics.
* ``save_state`` / ``load_state`` remain duck-compatible with the
  legacy layout and tolerate old saves.
"""

# Module-level ints below (CYCLES_PER_EDGE_DMG etc.) are Python-visible;
# cdef method bodies inline the numeric literals so they remain nogil-safe.
import pyboy
import cython

from pyboy.utils import MAX_CYCLES

logger = pyboy.logging.get_logger(__name__)

# --- Constants ----------------------------------------------------------

# One shift edge (rising or falling) every 128 CPU cycles gives the
# classic 8192 Hz DMG serial clock. Eight edges = one byte = 1024 Hz of
# byte-rate throughput. CGB fast-clock (SC bit 1) is not modelled yet.
CYCLES_PER_EDGE_DMG = 128
CYCLES_PER_BYTE_DMG = 8 * CYCLES_PER_EDGE_DMG

# Kept for back-compat with any external consumer that imported the
# legacy name.
CYCLES_8192HZ = 128

# FF02 bits.
SC_TRANSFER_ENABLE = 0x80
SC_CLOCK_SPEED = 0x02   # CGB only; stored but not acted on (milestone 9)
SC_CLOCK_SOURCE = 0x01  # 1 = internal (master), 0 = external (slave)

# INTR_SERIAL bitmask in IF.
IF_SERIAL = 0x08

# Roles passed to SerialBackend.on_edge().
ROLE_INTERNAL = 1  # master (drives the clock)
ROLE_EXTERNAL = 0  # slave

# Local marker for the serial save-block layout. The global
# pyboy.utils.STATE_VERSION is bumped by upstream releases; this tracks
# whether we've written the bit-accurate extension fields.
SERIAL_STATE_VERSION = 1


# --- Backends -----------------------------------------------------------

from typing import Protocol


class SerialBackend(Protocol):
    """Peer side of the cable.

    Called by :class:`Serial` on each master-mode edge to fetch the
    peer's outgoing bit. Slave-mode edges bypass the backend and are
    driven via :meth:`Serial.apply_external_edge` by the coordinator /
    peer.
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


# --- Serial core --------------------------------------------------------


class Serial:
    """Bit-accurate FF01/FF02 shift-register.

    Cdef-compiled via ``serial.pxd``. Method signatures (``tick``,
    ``set_SB``, ``set_SC``, ``save_state``, ``load_state``) match the
    legacy Serial class so ``mb.py`` is unchanged. The extra
    :meth:`apply_external_edge` entry point is ``cpdef`` so Python
    callers (the coordinator) can drive slave-mode edges.
    """

    # Class-level version tag for the harness save/load tests.
    # pyboy.utils.STATE_VERSION still governs full-motherboard saves;
    # this one tracks the bit-accurate-serial sub-layout.
    STATE_VERSION = SERIAL_STATE_VERSION

    def __init__(self, cgb_mode=False, *, backend=None):
        self.cgb_mode = bool(cgb_mode)
        self.SB = 0xFF  # disconnected-cable pull-up default
        # Legacy PyBoy and the harness unit tests both expect a raw
        # post-boot SC of 0x00. Real hardware's "unused bits read as 1"
        # mask is applied in ``set_SC`` when the game writes FF02, not
        # at construction — so ``Serial().SC`` is 0 until the ROM
        # touches it.
        self.SC = 0x00

        self.transfer_enabled = 0
        self.internal_clock = 0
        self.double_speed = 0
        # CPU cycles are twice as fast while the CGB is in double-speed
        # mode, but the normal serial clock remains in its hardware domain.
        # Motherboard.switch_speed keeps this separate from SC bit 1, which
        # selects the CGB fast-serial mode.
        self.cpu_speed_shift = 0
        self._cycles_to_interrupt = MAX_CYCLES
        self.last_cycles = 0
        self.clock = 0
        self.clock_target = MAX_CYCLES

        # Bit-accurate state.
        self._shift_register = 0xFF  # working byte; MSB is next-out bit
        self._bits_remaining = 0

        self.backend = backend if backend is not None else NullBackend()

    # --- register writes ------------------------------------------------

    def set_SB(self, value):
        """Write FF01.

        Games load the outgoing byte here before arming SC. Legacy
        PyBoy forced 0xFF (disconnected-cable model); we preserve the
        byte so the shift register can ship it.
        """
        self.SB = value & 0xFF
        # The mid-transfer write behaviour of real hardware is "the
        # byte was already snapshotted into the shift register when
        # SC bit 7 was set", so we do NOT touch _shift_register here.
        # If bit 7 is not armed yet, keep the shift register in sync
        # so that a subsequent SC write picks up the latest SB.
        if not self.transfer_enabled:
            self._shift_register = self.SB

    def set_SC(self, value):
        """Write FF02.

        Bit 7 arms a transfer. Bit 0 picks internal (master) vs
        external (slave) clock. Bit 1 (CGB fast clock) is stored but
        not acted on in this milestone. Unused hardware bits read as 1.
        """
        if self.cgb_mode:
            self.SC = (value & 0xFF) | 0b01111100
        else:
            self.SC = (value & 0xFF) | 0b01111110

        self.transfer_enabled = 1 if (self.SC & 0x80) else 0
        self.internal_clock = 1 if (self.SC & 0x01) else 0
        self.double_speed = 1 if (self.SC & 0x02) else 0

        if self.transfer_enabled:
            # Fresh transfer: snapshot SB into the shift register.
            self._shift_register = self.SB
            self._bits_remaining = 8
            if self.internal_clock:
                # Master: schedule first edge.
                self.clock_target = self.clock + (128 << self.cpu_speed_shift)
            else:
                # Slave: no internal clock, waits for apply_external_edge.
                # Literal to stay nogil-safe (cpdef void ... nogil can't
                # touch Python-module globals like MAX_CYCLES).
                self.clock_target = (1 << 31)
        else:
            self._bits_remaining = 0
            self.clock_target = (1 << 31)

        self._cycles_to_interrupt = self.clock_target - self.clock

    # --- tick (master / idle) -------------------------------------------

    def tick(self, _cycles):
        """Advance by ``_cycles - last_cycles`` CPU cycles.

        Returns ``True`` on the cycle the current transfer completes
        (caller fires the serial interrupt). Only master-mode ticks
        progress a transfer; slave mode waits for
        :meth:`apply_external_edge`.
        """
        delta = _cycles - self.last_cycles
        if delta == 0:
            return False
        self.last_cycles = _cycles
        self.clock += delta

        interrupt = False
        if self.transfer_enabled and self.internal_clock:
            # Process every edge whose deadline has passed. In practice
            # mb.py ticks far more often than 128, so
            # this loop runs at most a handful of times per call.
            # Any backend call requires the GIL; reacquire it for the
            # rare cycles that actually cross an edge so steady-state
            # ticks stay in the nogil fast path.
            if self._bits_remaining > 0 and self.clock >= self.clock_target:
                with cython.gil:
                    while self._bits_remaining > 0 and self.clock >= self.clock_target:
                        out_bit = (self._shift_register >> 7) & 1
                        peer_bit = self.backend.on_edge(out_bit, 1) & 1
                        self._shift_register = ((self._shift_register << 1) | peer_bit) & 0xFF
                        self._bits_remaining -= 1
                        if self._bits_remaining == 0:
                            # Transfer complete: SB latches received byte,
                            # SC bit 7 clears (read-only bits preserved),
                            # serial interrupt requested.
                            self.SB = self._shift_register
                            if self.cgb_mode:
                                self.SC = (self.SC & ~0x80 & 0xFF) | 0b01111100
                            else:
                                self.SC = (self.SC & ~0x80 & 0xFF) | 0b01111110
                            self.transfer_enabled = 0
                            self.clock_target = (1 << 31)
                            interrupt = True
                            break
                        else:
                            self.clock_target = self.clock_target + (
                                128 << self.cpu_speed_shift
                            )

        if self.clock_target > self.clock:
            self._cycles_to_interrupt = self.clock_target - self.clock
        else:
            self._cycles_to_interrupt = 0
        return interrupt

    # --- external clock (slave mode / coordinator) ----------------------

    def apply_external_edge(self, peer_bit):
        """Advance one edge driven by an external clock.

        Intended for slave mode (or for a coordinator driving both
        cores in lockstep). ``peer_bit`` is the bit the peer shifts to
        us this edge. Returns ``True`` iff this edge completed the
        byte (8th shift), ``False`` otherwise. Callers that need our
        outgoing bit should call :meth:`peek_out_bit` immediately
        before :meth:`apply_external_edge`.

        Raises ``RuntimeError`` if no transfer is armed or if we are
        in internal-clock mode (master).
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
        return self._advance_one_edge(peer_bit & 1)

    def peek_out_bit(self):
        """The bit that would shift out on the next edge (MSB of the
        working register). Does not advance state."""
        return (self._shift_register >> 7) & 1

    # --- internals ------------------------------------------------------

    def _advance_one_edge(self, peer_bit):
        """Shared edge logic: shift out MSB, shift in peer bit, handle
        completion. Returns True if the transfer just finished."""
        self._shift_register = ((self._shift_register << 1) | (peer_bit & 1)) & 0xFF
        self._bits_remaining -= 1
        if self._bits_remaining == 0:
            # Pan Docs: SB latches the received byte; SC bit 7 clears
            # (other bits preserved); serial interrupt requested.
            self.SB = self._shift_register
            if self.cgb_mode:
                self.SC = (self.SC & ~0x80 & 0xFF) | 0b01111100
            else:
                self.SC = (self.SC & ~0x80 & 0xFF) | 0b01111110
            self.transfer_enabled = 0
            self.clock_target = (1 << 31)
            self._cycles_to_interrupt = (1 << 31)
            return True
        return False

    # --- save/load ------------------------------------------------------

    def save_state(self, f):
        f.write(self.SB)
        f.write(self.SC)
        f.write(self.transfer_enabled)
        f.write(self.internal_clock)
        f.write_64bit(self.last_cycles)
        f.write_64bit(self._cycles_to_interrupt)
        f.write_64bit(self.clock)
        f.write_64bit(self.clock_target)
        # Bit-accurate extension. Legacy states don't include these;
        # load_state tolerates their absence.
        f.write(self._shift_register)
        f.write(self._bits_remaining)

    def load_state(self, f, state_version):
        self.SB = f.read()
        self.SC = f.read()
        self.transfer_enabled = f.read()
        self.internal_clock = f.read()
        self.last_cycles = f.read_64bit()
        self._cycles_to_interrupt = f.read_64bit()
        self.clock = f.read_64bit()
        self.clock_target = f.read_64bit()
        # Attempt to restore extended fields. Older states (and
        # upstream PyBoy saves) don't have them, so recover
        # conservatively: assume no in-flight transfer.
        try:
            self._shift_register = f.read()
            self._bits_remaining = f.read()
        except Exception:
            self._shift_register = self.SB
            self._bits_remaining = 0


__all__ = [
    "CYCLES_8192HZ",
    "CYCLES_PER_BYTE_DMG",
    "CYCLES_PER_EDGE_DMG",
    "IF_SERIAL",
    "LocalBackend",
    "NullBackend",
    "ROLE_EXTERNAL",
    "ROLE_INTERNAL",
    "SC_CLOCK_SOURCE",
    "SC_CLOCK_SPEED",
    "SC_TRANSFER_ENABLE",
    "SERIAL_STATE_VERSION",
    "Serial",
    "SerialBackend",
]
