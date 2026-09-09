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
import operator
import threading
from typing import NamedTuple

from pyboy.utils import MAX_CYCLES

logger = pyboy.logging.get_logger(__name__)

# --- Constants ----------------------------------------------------------

# PyBoy passes CPU T-cycles (a NOP costs 4), not machine cycles. The
# normal serial clock shifts one bit every 512 T-cycles:
# 4,194,304 T-cycles/s / 8192 bits/s = 512 T-cycles/bit, 4096 per byte.
# These are bit-transfer events, not both electrical clock transitions.
CYCLES_PER_EDGE_DMG = 512
CYCLES_PER_EDGE_CGB_FAST = 16
CYCLES_PER_BYTE_DMG = 8 * CYCLES_PER_EDGE_DMG

# Kept for back-compat with any external consumer that imported the
# legacy name.
CYCLES_8192HZ = 512

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

# The extension originally stored the shift register and bit count without
# recording the serial timing domain. Keep that ten-field layout readable,
# but tag new saves so the corrected hardware cadence is unambiguous. Untagged
# 128-T saves can be retimed only when the caller supplies provenance.
SERIAL_STATE_FORMAT_MAGIC = 0xA5
SERIAL_STATE_FORMAT_VERSION = 2


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


class SerialBackendError(RuntimeError):
    """An edge failed; the local emulator is quarantined until replaced."""


class OwnerBoundaryPre(NamedTuple):
    """Immutable metadata for a pre-commit owner boundary.

    ``event`` is the unchanged 12-field tuple delivered to the existing
    owner-pump callback.  The separate metadata object keeps that callback
    backwards compatible while giving a new observer named raw/physical
    times and the parent token for nested catch-up edges.
    """

    boundary_seq: int
    kind: int
    observed_cycles: int
    effective_cycles: int
    physical_epoch: int | None
    effective_physical_units: int | None
    parent_boundary_seq: int | None
    event: tuple[object, ...]


class OwnerBoundarySnapshot(NamedTuple):
    """Complete serial state captured at a post-commit boundary."""

    generation: int
    sb: int
    sc: int
    shift_register: int
    bits_remaining: int
    transfer_enabled: bool
    clock: int
    clock_target: int


class OwnerBoundaryPost(NamedTuple):
    """Immutable owner-thread post-commit record keyed by ``boundary_seq``."""

    boundary_seq: int
    kind: int
    observed_cycles: int
    effective_cycles: int
    physical_epoch: int | None
    effective_physical_units: int | None
    parent_boundary_seq: int | None
    committed: bool
    snapshot: OwnerBoundarySnapshot


class _OwnerBoundaryToken(NamedTuple):
    boundary_seq: int
    kind: int
    observed_cycles: int
    effective_cycles: int
    address: int
    value: int
    parent_boundary_seq: int | None


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
        # Retained for compatibility with older motherboard integrations and
        # legacy-save migration. New hardware-cadence deadlines are measured
        # in the raw CPU-cycle domain and do not use this shift.
        self.cpu_speed_shift = 0
        self._cycles_to_interrupt = MAX_CYCLES
        self.last_cycles = 0
        self.clock = 0
        self.clock_target = MAX_CYCLES

        # Bit-accurate state.
        self._shift_register = 0xFF  # working byte; MSB is next-out bit
        self._bits_remaining = 0

        self.backend = backend if backend is not None else NullBackend()
        # Legacy owner-dispatch compatibility.  These fields intentionally
        # remain independent from the newer claimed owner-pump callbacks:
        # callers may migrate transactionally by saving/restoring them while
        # retaining the b443 owner-boundary API.
        self.owner_dispatch_callback = None
        self.owner_dispatch_enabled = False
        # Native/noexcept callers cannot propagate Python backend exceptions.
        # Preserve the first error for an exception-capable owner boundary.
        # A failed edge may already have affected the peer: never retry it.
        self.backend_failed = False
        self._backend_error = None
        self._owner_pump = None
        self._pump_thread = None
        self._owner_pump_claim = None
        self._pump_binding_lock = threading.Lock()
        self.owner_pump_active = False
        self.owner_poll_enabled = False
        self._boundary_seq = 0
        self.transfer_generation = 0
        # Optional owner-phase completion channel.  The legacy owner pump is
        # intentionally kept separate: its callback still receives the exact
        # 12-field pre tuple and remains the only control point when these
        # callbacks are disabled.
        self._owner_pre_metadata = None
        self._owner_post = None
        self._owner_time_mapper = None
        self._owner_callback_thread = None
        self._owner_boundary_pending = {}

    def set_owner_boundary_callbacks(self, pre_metadata=None, post=None):
        """Install optional owner-thread pre-metadata and post callbacks.

        ``pre_metadata`` receives :class:`OwnerBoundaryPre` after a stable
        boundary sequence has been allocated and before the operation mutates
        serial state; real mutating boundaries also allocate a completion
        token.  ``post`` receives :class:`OwnerBoundaryPost` after each real
        edge or MMIO operation commits.  Kind-4 owner-poll observations invoke
        the pre callback but have no post-commit operation or post token.  Both
        callbacks run synchronously on the serial owner thread.  Existing
        ``owner_pump`` users and its 12-field tuple are unchanged when both
        callbacks are ``None``.

        A post callback failure latches the first error in the same quarantine
        as a backend failure.  Native state is deliberately not rolled back.
        """
        with self._pump_binding_lock:
            self.check_execution_allowed()
            if pre_metadata is not None and not callable(pre_metadata):
                raise TypeError("owner pre-metadata callback must be callable or None")
            if post is not None and not callable(post):
                raise TypeError("owner post callback must be callable or None")
            current_thread = threading.get_ident()
            owner_thread = self._pump_thread
            if owner_thread is None:
                owner_thread = self._owner_callback_thread
            if owner_thread is not None and owner_thread != current_thread:
                raise RuntimeError("owner callbacks must be changed on the owner thread")
            callbacks_changed = (
                pre_metadata is not self._owner_pre_metadata
                or post is not self._owner_post
            )
            if callbacks_changed and self._owner_pump_claim is not None:
                raise RuntimeError("owner callbacks are exclusively claimed")
            if callbacks_changed and self._owner_boundary_pending:
                raise RuntimeError("cannot replace owner callbacks with pending boundaries")
            if pre_metadata is not None or post is not None:
                self.check_error()
                if owner_thread is None:
                    owner_thread = current_thread
                    self._owner_callback_thread = owner_thread
            elif owner_thread is None and callbacks_changed:
                # Keep a disable operation owner-bound when it follows an
                # unbound no-op; future registration must not silently move
                # the owner to an arbitrary thread.
                self._owner_callback_thread = current_thread
            self._owner_pre_metadata = pre_metadata
            self._owner_post = post
            self._clear_owner_binding_if_idle()

    def _clear_owner_binding_if_idle(self):
        """Drop callback-thread affinity after the owner state is fully idle.

        A mapper may persist for the lifetime of a motherboard, but it does
        not establish ownership.  Explicit callbacks and pumps do.  Once the
        last callbacks are disabled and there is no active pump, claim, or
        pending completion, a later worker must be able to claim the idle
        serial core.  Callers hold ``_pump_binding_lock``.
        """
        if (
            self._owner_pump is None
            and self._pump_thread is None
            and self._owner_pump_claim is None
            and self._owner_pre_metadata is None
            and self._owner_post is None
            and not self._owner_boundary_pending
        ):
            self._owner_callback_thread = None

    def set_owner_time_mapper(self, mapper=None):
        """Set the optional raw-serial-to-physical-time owner mapper.

        The mapper is called as ``mapper(kind, observed_cycles,
        effective_cycles)`` from the owner thread and returns
        ``(physical_epoch, effective_physical_units)``.  The motherboard
        installs this mapper; standalone ``Serial`` users may omit it, in
        which case the metadata fields are ``None`` rather than guessed.
        """
        if mapper is not None and not callable(mapper):
            raise TypeError("owner time mapper must be callable or None")
        with self._pump_binding_lock:
            self.check_execution_allowed()
            current_thread = threading.get_ident()
            owner_thread = self._pump_thread
            if owner_thread is None:
                owner_thread = self._owner_callback_thread
            if owner_thread is not None and owner_thread != current_thread:
                raise RuntimeError("owner time mapper must be changed on the owner thread")
            mapper_changed = mapper is not self._owner_time_mapper
            if mapper_changed and self._owner_pump_claim is not None:
                raise RuntimeError("owner time mapper is exclusively claimed")
            if mapper_changed and self._owner_boundary_pending:
                raise RuntimeError("cannot replace owner time mapper with pending boundaries")
            if mapper is not None:
                self.check_error()
                # A motherboard installs its mapper during construction, before
                # the runtime owner thread is known.  Mapper registration alone
                # must therefore remain unbound.  Once an explicit callback or
                # pump has established an owner, ``owner_thread`` above is
                # non-None and the normal thread-affinity check still applies.
            self._owner_time_mapper = mapper

    def set_owner_pump(self, callback, poll=False):
        """Install an owner-thread, stack-preserving precommit pump.

        Callback return authorizes continuation; waits happen inside callback,
        not by throwing. External-edge application is an owner-only primitive
        usable inside the pump and does not recursively invoke this hook.
        This is not an arbitrary-cycle CPU grant or hardware bus-phase API.
        Optional poll=True adds kind-4 boundaries before CPU/HALT slices;
        existing users receive only the original precommit event stream.
        """
        with self._pump_binding_lock:
            self.check_execution_allowed()
            self.check_error()
            if self._owner_pump_claim is not None:
                raise RuntimeError("serial owner pump is exclusively claimed")
            if callback is not None and not callable(callback):
                raise TypeError("owner pump must be callable or None")
            if callback is not None and self._owner_callback_thread not in (None, threading.get_ident()):
                raise RuntimeError("owner pump and owner callbacks must share a thread")
            self._owner_pump = callback
            self._pump_thread = threading.get_ident() if callback is not None else None
            if callback is not None and (
                self._owner_pre_metadata is not None or self._owner_post is not None
            ):
                self._owner_callback_thread = self._pump_thread
            self.owner_poll_enabled = callback is not None and poll
            self._clear_owner_binding_if_idle()

    def claim_owner_pump(self, callback, poll=False):
        """Exclusively bind an unused pump; return an opaque release token."""
        with self._pump_binding_lock:
            self.check_execution_allowed()
            self.check_error()
            if not callable(callback):
                raise TypeError("claimed owner pump must be callable")
            if self._owner_pump is not None or self._owner_pump_claim is not None:
                raise RuntimeError("serial owner pump already installed or claimed")
            if self._owner_callback_thread not in (None, threading.get_ident()):
                raise RuntimeError("owner pump and owner callbacks must share a thread")
            token = object()
            self._owner_pump = callback
            self._pump_thread = threading.get_ident()
            self._owner_callback_thread = self._pump_thread
            self.owner_poll_enabled = poll
            self._owner_pump_claim = token
            return token

    def release_owner_pump(self, token):
        """Release this thread's claim, including after a latched cable fault."""
        with self._pump_binding_lock:
            self.check_execution_allowed()
            if token is None or token is not self._owner_pump_claim:
                raise RuntimeError("invalid serial owner pump claim token")
            if threading.get_ident() != self._pump_thread:
                raise RuntimeError("serial owner pump release off owner thread")
            self._owner_pump = None
            self._pump_thread = None
            self.owner_poll_enabled = False
            self._owner_pump_claim = None
            # The motherboard's time mapper is installed before a runtime
            # owner exists and intentionally does not establish affinity.
            # Release the binding only once every explicit owner resource is
            # idle; pending post tokens and callbacks still retain affinity.
            self._clear_owner_binding_if_idle()

    def dispatch_owner(self):
        """Run the legacy queued-edge callback at a safe CPU boundary.

        Motherboard invokes this only after a CPU instruction batch has
        returned, never from serial MMIO or ``tick`` itself.  Keeping the
        callback separate from the owner-pump path preserves the published
        main-tree integration while b443 callers use claimed pumps.
        """
        self.check_execution_allowed()
        if (
            self.owner_dispatch_enabled
            and self.transfer_enabled
            and not self.internal_clock
        ):
            callback = self.owner_dispatch_callback
            if callback is not None:
                callback()

    def check_execution_allowed(self):
        if self.owner_pump_active:
            raise RuntimeError("recursive CPU execution during serial owner pump")

    def owner_boundary(self, kind, cycles, address, value):
        # Called below noexcept CPU/MMIO frames: never throw through those.
        # kind 1=MMIO read, 2=MMIO write, 3=internal-edge pre-sample,
        # 4=owner-poll observation.  Kind 4 has no post-commit token: it
        # observes the boundary before a CPU/HALT slice and performs no
        # serial mutation of its own.
        if self.backend_failed:
            return False
        with cython.gil:
            token = None
            # Initialize locals before the exception-capable native path.  The
            # values are assigned again while holding the binding lock, but
            # Cython otherwise leaves the C temporaries potentially undefined
            # on a callback/pump setup failure.
            boundary_seq = 0
            effective_cycles = cycles
            parent_boundary_seq = None
            try:
                with self._pump_binding_lock:
                    if (
                        self._owner_pump is None
                        and self._owner_pre_metadata is None
                        and self._owner_post is None
                    ):
                        return True
                    if self.owner_pump_active:
                        raise RuntimeError("recursive serial owner boundary")
                    owner_thread = self._pump_thread
                    if owner_thread is None:
                        owner_thread = self._owner_callback_thread
                    if owner_thread is None:
                        owner_thread = threading.get_ident()
                        self._owner_callback_thread = owner_thread
                    if threading.get_ident() != owner_thread:
                        raise RuntimeError("serial boundary executed off owner thread")
                    callback = self._owner_pump
                    pre_metadata = self._owner_pre_metadata
                    mapper = self._owner_time_mapper
                    self.owner_pump_active = True
                    if self._boundary_seq >= 0xFFFFFFFFFFFFFFFF:
                        raise OverflowError("serial owner boundary sequence exhausted")
                    self._boundary_seq += 1
                    boundary_seq = self._boundary_seq
                    effective_cycles = self.clock_target if kind == 3 else cycles
                    parent_boundary_seq = None
                    # Owner polls are observations rather than mutations.
                    # They still deliver pre metadata and the legacy pump
                    # event, but there is no later mutation site that could
                    # complete a post token.  Never retain an unfinishable
                    # kind-4 token in the pending-boundary table.
                    needs_token = (
                        kind != 4
                        and (pre_metadata is not None or self._owner_post is not None)
                    )
                    if needs_token and kind == 3:
                        for previous in reversed(tuple(self._owner_boundary_pending.values())):
                            if previous.kind in (1, 2):
                                parent_boundary_seq = previous.boundary_seq
                                break
                    if needs_token:
                        token = _OwnerBoundaryToken(
                            boundary_seq,
                            kind,
                            cycles,
                            effective_cycles,
                            address,
                            value,
                            parent_boundary_seq,
                        )
                # Immutable observation, not a cached bit to commit afterward.
                event = (boundary_seq, kind, cycles, self.clock_target,
                         address, value, self.transfer_generation,
                         self.SB, self.SC, self._shift_register,
                         self._bits_remaining, bool(self.transfer_enabled))
                if token is not None:
                    with self._pump_binding_lock:
                        self._owner_boundary_pending[boundary_seq] = token
                if pre_metadata is not None:
                    physical_epoch = None
                    effective_physical_units = None
                    if mapper is not None:
                        physical_epoch, effective_physical_units = mapper(
                            kind, cycles, effective_cycles
                        )
                    pre_metadata(
                        OwnerBoundaryPre(
                            boundary_seq,
                            kind,
                            cycles,
                            effective_cycles,
                            physical_epoch,
                            effective_physical_units,
                            parent_boundary_seq,
                            event,
                        )
                    )
                if callback is not None:
                    callback(event)
                with self._pump_binding_lock:
                    self.owner_pump_active = False
            except BaseException as error:
                with self._pump_binding_lock:
                    if token is not None:
                        self._owner_boundary_pending.pop(token.boundary_seq, None)
                    self.owner_pump_active = False
                if not self.backend_failed:
                    self._backend_error = error
                    self.backend_failed = True
                return False
            return not self.backend_failed

    def owner_boundary_post(self, boundary_seq, committed=True):
        """Complete one owner token after its real mutation.

        The method is cdef/noexcept in the native build because it is called
        from ``Serial.tick`` and the motherboard's noexcept MMIO paths.  It
        therefore latches callback failures and returns ``False`` instead of
        throwing through the CPU frame.  A parent MMIO token cannot complete
        while one of its child catch-up edge tokens remains open.
        """
        if self.backend_failed:
            with cython.gil:
                with self._pump_binding_lock:
                    self._owner_boundary_pending.pop(boundary_seq, None)
            return False
        with cython.gil:
            if self._owner_pre_metadata is None and self._owner_post is None:
                return True
            token = None
            try:
                with self._pump_binding_lock:
                    if self.owner_pump_active:
                        raise RuntimeError("recursive serial owner boundary post")
                    token = self._owner_boundary_pending.get(boundary_seq)
                    if token is None:
                        raise RuntimeError("unknown or already completed owner boundary")
                    owner_thread = self._pump_thread
                    if owner_thread is None:
                        owner_thread = self._owner_callback_thread
                    if threading.get_ident() != owner_thread:
                        raise RuntimeError("owner boundary post executed off owner thread")
                    if token.kind in (1, 2):
                        if any(
                            pending.parent_boundary_seq == token.boundary_seq
                            for pending in self._owner_boundary_pending.values()
                        ):
                            raise RuntimeError("owner boundary post has open child tokens")
                    mapper = self._owner_time_mapper
                    callback = self._owner_post
                    self.owner_pump_active = True
                physical_epoch = None
                effective_physical_units = None
                if callback is not None and mapper is not None:
                    physical_epoch, effective_physical_units = mapper(
                        token.kind, token.observed_cycles, token.effective_cycles
                    )
                snapshot = OwnerBoundarySnapshot(
                    self.transfer_generation,
                    self.SB,
                    self.SC,
                    self._shift_register,
                    self._bits_remaining,
                    bool(self.transfer_enabled),
                    self.clock,
                    self.clock_target,
                )
                if callback is not None:
                    callback(
                        OwnerBoundaryPost(
                            token.boundary_seq,
                            token.kind,
                            token.observed_cycles,
                            token.effective_cycles,
                            physical_epoch,
                            effective_physical_units,
                            token.parent_boundary_seq,
                            bool(committed),
                            snapshot,
                        )
                    )
                with self._pump_binding_lock:
                    self._owner_boundary_pending.pop(token.boundary_seq, None)
                    self.owner_pump_active = False
            except BaseException as error:
                with self._pump_binding_lock:
                    if token is not None:
                        self._owner_boundary_pending.pop(token.boundary_seq, None)
                    self.owner_pump_active = False
                if not self.backend_failed:
                    self._backend_error = error
                    self.backend_failed = True
                return False
            return not self.backend_failed

    def owner_boundary_abort(self, boundary_seq):
        """Discard a token after a fatal mutation failure without a callback."""
        with cython.gil:
            with self._pump_binding_lock:
                self._owner_boundary_pending.pop(boundary_seq, None)
        return not self.backend_failed

    def _owner_pending_edge_deadlines(self):
        """Return effective raw deadlines that still require mapping."""
        with self._pump_binding_lock:
            deadlines = []
            for token in self._owner_boundary_pending.values():
                if token.kind == 3:
                    deadlines.append(token.effective_cycles)
            return tuple(deadlines)

    def check_error(self):
        """Raise a latched cable fault; detach does not make retry safe.

        Recovery requires a fresh standalone core, or a new emulator session
        when attached. Do not replace only an attached core: a failed CPU
        instruction and the peer may already have partial side effects.
        Loading state or replacing the backend cannot make continuation safe.
        """
        if self.backend_failed:
            raise SerialBackendError("serial backend failed; recreate session before resuming") from self._backend_error

    # --- register writes ------------------------------------------------

    def set_SB(self, value):
        """Write FF01.

        Games load the outgoing byte here before arming SC. Legacy
        PyBoy forced 0xFF (disconnected-cable model); we preserve the
        byte so the shift register can ship it.
        """
        if self.backend_failed:
            return
        self.SB = value & 0xFF
        # The mid-transfer write behaviour of real hardware is "the
        # byte was already snapshotted into the shift register when
        # SC bit 7 was set", so we do NOT touch _shift_register here.
        # If bit 7 is not armed yet, keep the shift register in sync
        # so that a subsequent SC write picks up the latest SB.
        if not self.transfer_enabled:
            self._shift_register = self.SB

    @cython.locals(
        was_transfer_enabled=cython.bint,
        was_internal_clock=cython.bint,
        was_double_speed=cython.bint,
        fresh_transfer=cython.bint,
    )
    def set_SC(self, value):
        """Write FF02.

        Bit 7 arms a transfer. Bit 0 picks internal (master) vs
        external (slave) clock. Bit 1 selects the CGB fast internal clock.
        Unused hardware bits read as 1.

        Rewriting an already-armed transfer with the same clock source
        updates the visible control register without restarting the byte.
        Pokémon's connection probe legitimately rewrites ``SC=$80`` while
        waiting for an external clock; restarting the shift register on
        each probe would prevent the eight hardware edges from completing.
        A newly armed transfer, or a write that changes the clock source,
        starts a fresh byte from ``SB``. Changing the speed of an active
        internal transfer preserves the in-flight byte and retimes its next
        edge from the current clock.
        """
        if self.backend_failed:
            return
        was_transfer_enabled = self.transfer_enabled
        was_internal_clock = self.internal_clock
        was_double_speed = self.double_speed
        self.transfer_generation += 1
        if self.cgb_mode:
            self.SC = (value & 0xFF) | 0b01111100
        else:
            self.SC = (value & 0xFF) | 0b01111110

        self.transfer_enabled = 1 if (self.SC & 0x80) else 0
        self.internal_clock = 1 if (self.SC & 0x01) else 0
        # On DMG, bit 1 reads as an unused bit set to one and must not expose
        # a CGB-only fast-clock mode to the motherboard.
        self.double_speed = 1 if self.cgb_mode and (self.SC & 0x02) else 0

        fresh_transfer = (
            not was_transfer_enabled
            or not self.transfer_enabled
            or was_internal_clock != self.internal_clock
            or self._bits_remaining == 0
        )

        if not self.transfer_enabled:
            self._bits_remaining = 0
            self.clock_target = (1 << 31)
        elif fresh_transfer:
            # Fresh transfer: snapshot SB into the shift register.
            self._shift_register = self.SB
            self._bits_remaining = 8
            if self.internal_clock:
                # Master: schedule first edge.
                self.clock_target = self.clock + (
                    16
                    if self.double_speed
                    else 512
                )
            else:
                # Slave: no internal clock, waits for apply_external_edge.
                # Literal to stay nogil-safe (cpdef void ... nogil can't
                # touch Python-module globals like MAX_CYCLES).
                self.clock_target = (1 << 31)
        elif self.internal_clock:
            # Same-role writes do not restart an active master transfer. A
            # speed change retimes its next edge from the current clock,
            # preserving the already-shifted bits.
            if was_double_speed != self.double_speed:
                self.clock_target = self.clock + (
                    16
                    if self.double_speed
                    else 512
                )
        else:
            # Same-role writes do not disturb an active slave transfer.
            # There is no local timebase to reschedule.
            pass

        if not self.transfer_enabled or not self.internal_clock:
            # No local deadline: MAX_CYCLES is a scheduling sentinel, not
            # an absolute clock target. Literal keeps this nogil-safe.
            self._cycles_to_interrupt = (1 << 31)
        elif self.clock_target > self.clock:
            self._cycles_to_interrupt = self.clock_target - self.clock
        else:
            self._cycles_to_interrupt = 0

    # --- tick (master / idle) -------------------------------------------

    @cython.locals(delta=cython.ulonglong)
    def tick(self, _cycles):
        """Advance by ``_cycles - last_cycles`` CPU cycles.

        Returns ``True`` on the cycle the current transfer completes
        (caller fires the serial interrupt). Only master-mode ticks
        progress a transfer; slave mode waits for
        :meth:`apply_external_edge`.
        """
        if self.backend_failed:
            return False
        if not (self.transfer_enabled and self.internal_clock):
            # Normalize even when no cycles elapsed (including old hints).
            self._cycles_to_interrupt = (1 << 31)
        delta = _cycles - self.last_cycles
        if delta == 0:
            return False
        self.last_cycles = _cycles
        self.clock += delta

        interrupt = False
        if self.transfer_enabled and self.internal_clock:
            # Process every edge whose deadline has passed. In practice
            # mb.py ticks far more often than 512 T-cycles, so
            # this loop runs at most a handful of times per call.
            # Any backend call requires the GIL; reacquire it for the
            # rare cycles that actually cross an edge so steady-state
            # ticks stay in the nogil fast path.
            if self._bits_remaining > 0 and self.clock >= self.clock_target:
                with cython.gil:
                    while self._bits_remaining > 0 and self.clock >= self.clock_target:
                        if not self.owner_boundary(3, _cycles, -1, -1):
                            return False
                        edge_boundary_seq = self._boundary_seq
                        # The owner may have serviced a queued cancellation.
                        # Resample current state, never the event snapshot.
                        if (
                            not self.transfer_enabled
                            or not self.internal_clock
                            or self._bits_remaining == 0
                            or self.clock < self.clock_target
                        ):
                            if not self.owner_boundary_post(edge_boundary_seq, False):
                                return False
                            break
                        out_bit = (self._shift_register >> 7) & 1
                        try:
                            peer_bit = operator.index(self.backend.on_edge(out_bit, 1)) & 1
                        except BaseException as error:
                            self.owner_boundary_abort(edge_boundary_seq)
                            if not self.backend_failed:
                                self._backend_error = error
                                self.backend_failed = True
                            return False
                        # A backend may have entered a nested native path.
                        # Never shift after that path has latched a fault.
                        if self.backend_failed:
                            self.owner_boundary_abort(edge_boundary_seq)
                            return False
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
                        else:
                            self.clock_target = self.clock_target + (
                                16
                                if self.double_speed
                                else 512
                            )
                        if not self.owner_boundary_post(edge_boundary_seq, True):
                            return False
                        if interrupt:
                            break

        if not (self.transfer_enabled and self.internal_clock):
            # Idle and externally clocked transfers cannot produce a
            # CPU-timed interrupt, even when clock has passed MAX_CYCLES.
            self._cycles_to_interrupt = (1 << 31)
        elif self.clock_target > self.clock:
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
        self.check_error()
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

    def _migrate_legacy_timing(self):
        """Retarget an untagged in-flight transfer to hardware cadence.

        Saves written before the assembly-informed timing correction contain
        the current deadline but no timing-domain marker. Their old periods
        were 128 DMG cycles (or 128 shifted by CGB CPU speed) and 4 CGB-fast
        cycles. Only an armed internal transfer needs migration; slave
        transfers have no local deadline.
        """
        if not (self.transfer_enabled and self.internal_clock and self._bits_remaining > 0):
            return

        old_period = (
            4
            if self.cgb_mode and self.double_speed
            else (128 << self.cpu_speed_shift)
        )
        new_period = CYCLES_PER_EDGE_CGB_FAST if self.double_speed else CYCLES_PER_EDGE_DMG
        remaining = self.clock_target - self.clock
        if remaining < 1:
            remaining = 1
        new_remaining = (remaining * new_period + old_period - 1) // old_period
        self.clock_target = self.clock + max(1, new_remaining)
        self._cycles_to_interrupt = self.clock_target - self.clock

    def save_state(self, f):
        self.check_error()
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
        f.write(SERIAL_STATE_FORMAT_MAGIC)
        f.write(SERIAL_STATE_FORMAT_VERSION)

    def load_state(self, f, state_version, legacy_timing=None):
        self.check_execution_allowed()
        self.check_error()
        # State loads rebase the owner's serial clock.  Any pre-boundary token
        # from the old state is no longer meaningful and must not be completed
        # against the restored bytes.
        self._owner_boundary_pending.clear()
        self.SB = f.read()
        self.SC = f.read()
        self.transfer_enabled = f.read()
        self.internal_clock = f.read()
        self.last_cycles = f.read_64bit()
        if self.transfer_enabled and self.internal_clock:
            self._cycles_to_interrupt = f.read_64bit()
        else:
            # Consume the old u64 cache before signed-field conversion:
            # source saves may encode an underflowed wait modulo 2**64.
            f.read_64bit()
            self._cycles_to_interrupt = (1 << 31)
        self.clock = f.read_64bit()
        self.clock_target = f.read_64bit()
        self.double_speed = 1 if self.cgb_mode and (self.SC & 0x02) else 0
        if not (self.transfer_enabled and self.internal_clock):
            # Old saves can contain a countdown to the idle clock sentinel.
            # Normalize the derived value without altering pending slave bits.
            self._cycles_to_interrupt = (1 << 31)
        # Attempt to restore extended fields. Older states (and upstream
        # PyBoy saves) don't have them, so recover conservatively: assume no
        # in-flight transfer. The prior harness format had the two extension
        # bytes but no timing marker; preserve its transfer and serialized
        # deadline unless the caller supplies explicit legacy provenance.
        try:
            self._shift_register = f.read()
            self._bits_remaining = f.read()
        except Exception:
            self._shift_register = self.SB
            self._bits_remaining = 0
            self.transfer_enabled = 0
            self.SC = self.SC & ~0x80 & 0xFF
            self.clock_target = (1 << 31)
            self._cycles_to_interrupt = (1 << 31)
            return 0
        if not (self.transfer_enabled and self.internal_clock):
            self.clock_target = (1 << 31)
            self._cycles_to_interrupt = (1 << 31)

        try:
            format_magic = f.read()
        except Exception:
            # The untagged ten-field extension was emitted by two distinct
            # runtime histories: 217e95e used 128 T-cycles per edge, while
            # b443ecd already used the corrected 512-T cadence.  Their
            # remaining deadlines overlap, so absence of a marker cannot
            # identify the producer. Preserve the serialized deadline by
            # default (the b443 compatibility contract); an older 128-T
            # producer must opt in explicitly at the load boundary.
            if legacy_timing in (None, 512, "512", "b443"):
                return 0
            if legacy_timing in (128, "128", "217e95e"):
                self._migrate_legacy_timing()
                return 0
            raise ValueError(
                "ambiguous untagged serial state; legacy_timing must be "
                "128, 512, or None"
            )

        if format_magic != SERIAL_STATE_FORMAT_MAGIC:
            raise ValueError(
                "unknown bit-accurate serial state format marker: "
                f"0x{format_magic:02x}"
            )
        format_version = f.read()
        if format_version != SERIAL_STATE_FORMAT_VERSION:
            raise ValueError(
                "unsupported bit-accurate serial state format version: "
                f"{format_version}"
            )
        return 0


SerialCore = Serial


__all__ = [
    "CYCLES_8192HZ",
    "CYCLES_PER_BYTE_DMG",
    "CYCLES_PER_EDGE_CGB_FAST",
    "CYCLES_PER_EDGE_DMG",
    "IF_SERIAL",
    "LocalBackend",
    "NullBackend",
    "OwnerBoundaryPost",
    "OwnerBoundaryPre",
    "OwnerBoundarySnapshot",
    "ROLE_EXTERNAL",
    "ROLE_INTERNAL",
    "SC_CLOCK_SOURCE",
    "SC_CLOCK_SPEED",
    "SC_TRANSFER_ENABLE",
    "SERIAL_STATE_VERSION",
    "SERIAL_STATE_FORMAT_MAGIC",
    "SERIAL_STATE_FORMAT_VERSION",
    "Serial",
    "SerialCore",
    "SerialBackend",
]
