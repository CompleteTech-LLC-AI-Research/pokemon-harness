"""``PyBoyLinkSession`` — public surface for linking PyBoy instances.

Milestone 4 of the design doc (:doc:`docs/pyboy_serial_overhaul_design.md`).
Given one or two PyBoy instances, this wires a
:class:`~pokered_harness.link.serial_coordinator.CoordinatedBackend` (or
:class:`~pokered_harness.link.network_backend.NetworkBackend`) onto the
``backend`` attribute of each ``pyboy.mb.serial`` so the already-installed
bit-accurate :class:`pyboy.core.serial.Serial` instance exchanges bits
with its peer. No class swap is performed — the existing PyBoy Serial
instance is reused.

Usage
-----

::

    from pyboy import PyBoy
    from pokered_harness.link import PyBoyLinkSession

    a = PyBoy("red.gb", window="null")
    b = PyBoy("blue.gb", window="null")

    link = PyBoyLinkSession.local()
    link.attach(a)
    link.attach(b)

    # In-game code on both sides now sees a bit-accurate serial bridge.
    # Drive both emulators through the interleaved scheduler so serial-heavy
    # ROM routines stay near-aligned:
    while not done:
        link.step(frames=1)

    link.detach_all()

Design notes
------------

* ``attach`` sets ``pyboy.mb.serial.backend`` rather than replacing the
  whole ``mb.serial`` object. The bit-accurate shift-register logic
  lives in PyBoy's native Serial class (upstream fork); the harness
  only chooses which peer (null / local / network) provides the bits.
* Once *both* sides attach, a :class:`LockstepCoordinator` is
  created, wiring each core's backend to the other. Until then, the
  first side's core keeps its default :class:`NullBackend` and behaves
  like a disconnected cable — matching the "not yet connected" phase
  in Pokémon's own link-cable code.
* ``detach`` restores each serial's original ``backend`` (normally
  :class:`NullBackend`); PyBoy returns to disconnected-cable behavior.
* Both local stepping methods use one persistent physical-time instruction
  scheduler. LCD markers never freeze one side while its peer continues.
  Paired loads and independent managed Session stepping require detachment;
  network stepping retains its separate bounded frame-turn protocol.

This class is the natural extension point for the network backend
(milestone 8): :meth:`listen` / :meth:`connect` classmethods would
pair a single local core with a TCP-backed remote endpoint instead
of a second local core.
"""

from __future__ import annotations

import inspect
import threading
import time
from contextlib import contextmanager
from functools import wraps
from operator import index
from typing import Protocol, runtime_checkable

from pokered_harness.link.network_backend import NetworkBackend
from pokered_harness.link.serial_coordinator import (
    LockstepCoordinator,
    SerialOperationGate,
)
from pokered_harness.link.serial_core import (
    NullBackend,
    SerialCore,
)
from pokered_harness.ownership import EmulatorOwnershipError, owner_for, owner_group


@runtime_checkable
class _PyBoyLike(Protocol):
    """Minimal duck-type the session needs from a PyBoy instance.

    Real :class:`pyboy.PyBoy` satisfies this. A test fake need only
    expose a ``mb`` with a ``serial`` attribute (carrying a
    settable ``backend``) and a ``tick`` method.
    """

    mb: object

    def tick(self, count: int = 1, render: bool = True, sound: bool = False) -> bool: ...


def _validate_positive_int(value: int, name: str) -> int:
    """Validate a public count argument before doing any work."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be a positive integer")
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


class PairedSessionOperationError(RuntimeError):
    code = "paired_session_operation"


@contextmanager
def _bounded_provider_lock(lock, deadline):
    if deadline is None:
        with lock:
            yield
        return
    if not lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise TimeoutError("provider lock did not become available before cleanup deadline")
    try:
        yield
    finally:
        lock.release()


def _call_with_timeout(function, timeout_s: float):
    """Call a lifecycle hook only when it accepts a bounded deadline.

    Network teardown is a safety boundary: a no-argument ``stop`` or
    ``detach_local_core`` cannot prove that cleanup will return.  The native
    NetworkBackend exposes ``timeout_s`` on both hooks, and adapters must
    implement that same bounded contract before an emulator is attached.
    """
    if not _accepts_timeout(function):
        raise TypeError("network lifecycle hook must expose timeout_s")
    return function(timeout_s=timeout_s)


def _accepts_timeout(function) -> bool:
    try:
        # Bind the exact call shape: a positional-only timeout parameter or
        # another required argument is not a usable bounded lifecycle hook.
        inspect.signature(function).bind(timeout_s=0.0)
    except (TypeError, ValueError):
        return False
    return True


def _serialized_local_operation(function):
    @wraps(function)
    def call(self, *args, **kwargs):
        # Snapshot membership without taking the provider lock first. Every
        # native access uses canonical emulator owners BEFORE provider state.
        members = tuple(self._pyboys)
        endpoints = members
        if function.__name__ == "attach":
            endpoint = args[0] if args else kwargs["pyboy"]
            endpoints += (endpoint,)
        cleanup = function.__name__ in {"detach", "detach_all"}
        previous_deadline = getattr(self._cleanup_state, "deadline", None)
        deadline = previous_deadline
        if cleanup and deadline is None:
            deadline = time.monotonic() + 2.0
        network_generation = (
            self._network_cancellation_generation()
            if self._network_backend is not None
            else None
        )
        previous_network_cancelled = getattr(
            self._cleanup_state, "network_cancelled", False
        )
        try:
            if cleanup:
                self._cleanup_state.deadline = deadline
                if self._network_backend is not None:
                    # Validate before publishing transport cancellation.  If
                    # an adapter was replaced or lost its bounded detach
                    # hook, teardown must leave both transport and emulator
                    # references untouched for a later repair/retry.
                    self._require_network_lifecycle_contract()
                if (
                    self._network_backend is not None
                    and not previous_network_cancelled
                ):
                    # Transport cancellation is deliberately outside the
                    # emulator owner group.  A raw network tick can be
                    # blocked in native serial code while holding that
                    # owner; stopping the transport is the wake-up that lets
                    # it unwind before backend/core restoration.
                    self._cancel_network_transport(deadline)
                    self._cleanup_state.network_cancelled = True
            with owner_group(
                (owner_for(endpoint) for endpoint in endpoints),
                allow_closed=cleanup,
                timeout=None if deadline is None else max(0.0, deadline - time.monotonic()),
            ), _bounded_provider_lock(self._operation_lock, deadline):
                if len(members) != len(self._pyboys) or any(
                    before is not after for before, after in zip(members, self._pyboys)
                ):
                    raise EmulatorOwnershipError("provider membership changed; retry the operation")
                result = function(self, *args, **kwargs)
                if (
                    network_generation is not None
                    and function.__name__ in {"step", "step_interleaved"}
                    and network_generation
                    != self._network_cancellation_generation()
                ):
                    raise EmulatorOwnershipError(
                        "network emulator operation was cancelled by cleanup"
                    )
                return result
        finally:
            if cleanup:
                self._cleanup_state.deadline = previous_deadline
                self._cleanup_state.network_cancelled = previous_network_cancelled
    return call


class PyBoyLinkSession:
    """Pairs up to two PyBoy instances under a bit-accurate serial link."""

    #: Max attached instances. Gen I Pokémon is strictly 2-player.
    MAX_ATTACHED: int = 2
    PHYSICAL_QUANTUM = 70_224 * 2
    MAX_FRAME_INSTRUCTIONS = 200_000
    MAX_FRAME_SECONDS = 30.0
    MAX_STALLED_INSTRUCTIONS = 32
    # PyBoy's LCD uses 70224 CPU cycles for a normal DMG frame. This is only
    # a fallback for test doubles or older integrations which do not expose
    # the LCD's next-frame cycle hint; real PyBoy instances use that hint.
    _DEFAULT_LCD_FRAME_CYCLES: int = 70224
    # Keep an individual singlestepped chunk bounded even if a caller
    # supplies an unusually large ``chunk_cycles`` value.
    _MAX_SINGLE_STEP_TICKS: int = 4096
    # A versioned network peer must be identified before native serial
    # startup. This is deliberately finite so attach cannot retain an
    # emulator or reader thread forever when the remote endpoint disappears.
    _NETWORK_HELLO_TIMEOUT_SECONDS: float = 10.0

    def __init__(
        self,
        network_backend: NetworkBackend | None = None,
        *,
        network_is_internal_clock: bool | None = None,
        local_rom_version: str | None = None,
        view: bool = False,
    ) -> None:
        self._pyboys: list[object] = []
        # Tracks each attached PyBoy's serial instance (``pyboy.mb.serial``).
        # Kept under the historical ``_cores`` name so callers relying on
        # :attr:`cores` keep working.
        self._cores: list[object] = []
        # Per-attach backend we installed; saved so detach() can restore
        # whatever was on ``mb.serial.backend`` before we touched it.
        self._prev_backends: list[object] = []
        # When attaching to a legacy/no-backend serial object, we promote
        # it to a SerialCore and keep the original here so detach() can put
        # the motherboard back exactly as it was.
        self._prev_serials: list[object | None] = []
        self._coord: LockstepCoordinator | None = None
        self._network_backend: NetworkBackend | None = network_backend
        # The gate covers one complete PyBoy frame and owner-side native
        # serial dispatch. The per-frame boundary is installed below so a
        # multi-frame public tick cannot starve a queued peer edge.
        self._serial_gate = SerialOperationGate()
        # Attach/detach and local stepping mutate the same core/backend graph.
        # Serialize those lifecycle transitions so a concurrent caller cannot
        # observe or drive a half-paired session.
        self._lifecycle_lock = threading.RLock()
        self._operation_lock = self._lifecycle_lock
        self._cleanup_state = threading.local()
        # Cleanup must be able to publish a cancellation while a network
        # frame owns the provider/lifecycle lock.  This tiny independent
        # generation lock carries that signal without waiting on emulator
        # ownership; the active operation checks it when its native call
        # returns and fails closed before publishing a successful result.
        self._network_cancel_lock = threading.Lock()
        self._network_cancel_generation = 0
        self._network_tick_active = False
        self._owners = []
        self._step_active = False
        self._peer_progress_active = False
        self._clear_local_epoch()
        self._original_ticks: dict[int, tuple[str, object]] = {}
        # The native serial core may already have an owner-dispatch callback
        # installed by another integration. Keep the exact callback and
        # enabled state, plus our replacement callback, so detach restores
        # only state still owned by this session.
        self._previous_owner_dispatch: list[tuple[object | None, object, object] | None] = []
        self._network_is_internal_clock = network_is_internal_clock
        self._network_frame_barrier = False
        self._local_rom_version = local_rom_version
        # When True, per-frame stepping keeps the LCD renderer on and calls
        # each PyBoy's _post_tick (via pyboy.tick(0, True, False)) so the
        # SDL2 window actually flips and pumps events. Without this the
        # visible windows stay blank because _interleave_one_frame otherwise
        # drives mb.tick directly and skips the plugin manager.
        self._view: bool = view

    # --- construction --------------------------------------------------

    @classmethod
    def local(cls, *, view: bool = False) -> PyBoyLinkSession:
        """Create a local two-instance session.

        Both PyBoys attach into the same process; they're paired via
        a :class:`LockstepCoordinator`.
        """
        return cls(view=view)

    @classmethod
    def listen(
        cls,
        port: int,
        *,
        host: str = "127.0.0.1",
        local_rom_version: str | None = None,
        accept_timeout_s: float = 10.0,
        cancel_event: threading.Event | None = None,
    ) -> PyBoyLinkSession:
        """Bind ``(host, port)``, accept one peer, return the session.

        Single-instance mode: the session holds a :class:`NetworkBackend`;
        :meth:`attach` wires that backend onto the local PyBoy's
        ``mb.serial.backend``. The peer process is expected to have
        used :meth:`connect` and to be driving its own PyBoy.

        Blocks until a peer connects, the bounded accept deadline expires, or
        ``cancel_event`` is set. The bounded default prevents a forgotten
        listener from retaining a thread and socket forever.
        """
        backend, _listener = NetworkBackend.listen(
            port,
            host=host,
            local_rom_version=local_rom_version,
            accept_timeout_s=accept_timeout_s,
            cancel_event=cancel_event,
        )
        # Close the listener — we only accept one connection.
        try:
            _listener.close()
        except OSError:
            pass
        return cls(
            network_backend=backend,
            network_is_internal_clock=True,
            local_rom_version=local_rom_version,
        )

    @classmethod
    def connect(
        cls,
        host: str,
        port: int,
        *,
        timeout_s: float = 10.0,
        local_rom_version: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> PyBoyLinkSession:
        """Connect to a peer running :meth:`listen` on ``(host, port)``.

        Returns a single-instance network-mode session; call
        :meth:`attach` to wire the session's NetworkBackend onto the
        local PyBoy's ``mb.serial.backend``.
        """
        backend = NetworkBackend.connect(
            host,
            port,
            timeout_s=timeout_s,
            local_rom_version=local_rom_version,
            cancel_event=cancel_event,
        )
        return cls(
            network_backend=backend,
            network_is_internal_clock=False,
            local_rom_version=local_rom_version,
        )

    # --- attach / detach -----------------------------------------------

    @_serialized_local_operation
    def attach(self, pyboy: _PyBoyLike) -> object:
        """Wire a backend onto ``pyboy.mb.serial``.

        The operation is serialized with other attach, detach, and stepping
        calls so callers cannot observe a half-paired session.

        Returns the PyBoy ``Serial`` instance so callers can inspect
        its register state directly.

        Local mode: the second attachment pairs both instances via a
        :class:`LockstepCoordinator`; the coordinator installs a
        :class:`CoordinatedBackend` on each serial's ``backend``
        attribute.

        Network mode: only one instance attaches per session. The
        serial's ``backend`` is set to the :class:`NetworkBackend`;
        the reader thread starts so peer-driven (slave-mode) edges
        advance the local serial and fire the CPU's serial IRQ. When both
        the session and backend carry ROM labels, attach waits for the
        bounded HELLO handshake and records the deterministic network clock
        role as session metadata before returning to the caller. Attach does
        not write the native serial registers; the ROM remains the owner of
        that state.

        Raises ``RuntimeError`` if ``pyboy`` is already attached, the
        session is at :attr:`MAX_ATTACHED`, or a network-mode session
        already has its one attachment.
        """
        with self._lifecycle_lock:
            return self._attach_locked(pyboy)

    def _attach_locked(self, pyboy: _PyBoyLike) -> object:
        if pyboy in self._pyboys:
            raise RuntimeError(f"already attached: {pyboy!r}")
        maximum = 1 if self._network_backend is not None else self.MAX_ATTACHED
        if len(self._pyboys) >= maximum:
            raise RuntimeError(f"session is full ({maximum} instances max)")
        owner = owner_for(pyboy)
        owner.ensure_open()
        owner.claim_provider(self)
        try:
            # Validate the complete bounded lifecycle contract only after the
            # metadata-only provider claim.  A competing provider must still
            # receive the ownership error before an unrelated adapter-shape
            # error, and a failed validation releases this temporary claim
            # without touching the native serial graph.
            if self._network_backend is not None:
                self._require_network_lifecycle_contract()
            result = self._attach_claimed(pyboy)
        except BaseException as error:
            if pyboy in self._pyboys:
                try:
                    self._detach_locked(pyboy)
                except BaseException as cleanup_error:
                    error.add_note(f"attachment cleanup also failed: {cleanup_error!r}")
                    # Keep the owner alive if failed restoration still retains
                    # an endpoint. A later detach must have the same identity.
                    self._owners.append(owner)
                    raise error from cleanup_error
            owner.release_provider(self)
            raise
        self._owners.append(owner)
        return result

    def _require_network_lifecycle_contract(self) -> None:
        """Reject an unbounded network adapter before touching an emulator."""
        backend = self._network_backend
        if backend is None:
            return
        for name in ("stop", "detach_local_core"):
            hook = getattr(backend, name, None)
            if not callable(hook) or not _accepts_timeout(hook):
                raise TypeError(
                    "network backend requires a bounded timeout_s "
                    f"{name}() lifecycle hook before attach"
                )

    def _attach_claimed(self, pyboy: _PyBoyLike) -> object:
        # Caller holds the lifecycle lock.
        if pyboy in self._pyboys:
            raise RuntimeError(f"already attached: {pyboy!r}")
        max_attached = 1 if self._network_backend is not None else self.MAX_ATTACHED
        if len(self._pyboys) >= max_attached:
            raise RuntimeError(f"session is full ({max_attached} instances max)")

        mb = pyboy.mb
        core = mb.serial  # prefer reusing the existing PyBoy Serial instance
        # Save whatever backend the serial currently has so detach()
        # can restore it. On a freshly constructed PyBoy this is
        # ``NullBackend``; mid-game attaches preserve whatever was set.
        prev_backend = getattr(core, "backend", None)
        prev_serial = None
        if not hasattr(core, "backend"):
            prev_serial = core
            core = self._promote_legacy_serial(core)
            mb.serial = core
            prev_backend = getattr(core, "backend", None)

        self._pyboys.append(pyboy)
        self._cores.append(core)
        self._prev_backends.append(prev_backend)
        self._prev_serials.append(prev_serial)
        self._previous_owner_dispatch.append(None)

        if self._network_backend is not None:
            # Network-mode: hook the local core up to the TCP backend
            # and fire the slave-IRQ via this pyboy's CPU flag register
            # when peer-driven edges complete our transfer. The initial
            # native clock role is retained as session metadata and is
            # negotiated after versioned HELLO; the ROM owns all native
            # serial registers, its connection-status byte, and role changes.
            # Attaching a transport must not seed or rewrite the cartridge's
            # idle or in-flight serial state.
            core.backend = self._network_backend
            owner_dispatch_state: tuple[object | None, object, object] | None = None
            try:
                set_context_provider = getattr(
                    self._network_backend,
                    "set_serial_transcript_context_provider",
                    None,
                )
                if callable(set_context_provider):
                    set_context_provider(
                        self._make_serial_completion_context_provider(pyboy)
                    )
                self._network_backend.start_receiver(
                    local_core=core,
                    irq_callback=self._make_serial_irq_raiser(pyboy),
                    serial_gate=self._serial_gate,
                    dispatch_to_owner=True,
                )
                if (
                    self._local_rom_version is not None
                    and self._network_backend.local_rom_version is not None
                ):
                    # HELLO is emitted by NetworkBackend construction, so
                    # both connected endpoints can identify their ROMs before
                    # any emulator tick. Role negotiation records the
                    # deterministic pacing role without writing FF01/FF02 or
                    # otherwise changing the ROM's current serial state.
                    peer_version = self._network_backend.wait_for_hello(
                        timeout=self._NETWORK_HELLO_TIMEOUT_SECONDS
                    )
                    if peer_version is None:
                        raise RuntimeError("versioned network backend did not report peer ROM")
                    self.negotiate_network_clock_role(peer_version)
                self._install_network_tick_owner(pyboy)
                with self._serial_gate:
                    owner_dispatch_state = self._enable_network_owner_pump(
                        core, self._network_backend
                    )
                self._previous_owner_dispatch[-1] = owner_dispatch_state
            except BaseException as attach_error:
                # Cancel transport work, but retain the attachment records.
                # The outer transactional rollback uses _detach_locked to
                # drain core users and restore native callbacks/backends.
                # Dropping these records here would hide a failed cleanup
                # and retain emulator closures in the external backend.
                try:
                    stopped = _call_with_timeout(
                        self._network_backend.stop, 2.0
                    )
                    if stopped is False:
                        attach_error.add_note("network workers remain active after attach failure")
                except BaseException as stop_error:  # noqa: BLE001 - retain attach and teardown failures
                    attach_error.add_note(f"network cancellation also failed: {stop_error!r}")
                raise
        elif len(self._cores) == 2:
            # Local-mode pair: wire the in-process coordinator with
            # IRQ callbacks pointing at each motherboard's CPU. The
            # coordinator installs CoordinatedBackend on each core's
            # ``backend`` attribute.
            try:
                self._begin_local_epoch()
                self._coord = LockstepCoordinator(
                    self._cores[0],
                    self._cores[1],
                    on_a_transfer_complete=self._make_serial_irq_raiser(self._pyboys[0]),
                    on_b_transfer_complete=self._make_serial_irq_raiser(self._pyboys[1]),
                    on_a_peer_unarmed=self._make_owned_peer_progressor(self._pyboys[1]),
                    on_b_peer_unarmed=self._make_owned_peer_progressor(self._pyboys[0]),
                )
                for attached_core in self._cores:
                    attached_core.backend.validate_session_operation = self.validate_session_operation
            except BaseException as exc:
                self._clear_local_epoch()
                # Coordinator.attach() can fail after the new bookkeeping
                # entries have been appended (for example, a native Serial
                # rejects its backend assignment). Restore the second
                # motherboard and remove all bookkeeping entries transactionally.
                try:
                    if prev_serial is not None:
                        mb.serial = prev_serial
                    else:
                        core.backend = prev_backend if prev_backend is not None else NullBackend()
                except BaseException as rollback_error:  # noqa: BLE001
                    exc.add_note(f"local attach rollback also failed: {rollback_error!r}")
                finally:
                    self._pyboys.pop()
                    self._cores.pop()
                    self._prev_backends.pop()
                    self._prev_serials.pop()
                    self._previous_owner_dispatch.pop()
                raise

        return core

    @staticmethod
    def _promote_legacy_serial(serial: object) -> object:
        """Best-effort compatibility path for pre-backend serial objects.

        Older tests and partial PyBoy integrations may still expose a
        serial object without a runtime-settable ``backend`` attribute.
        Promote that object to a ``SerialCore`` while preserving the
        visible register state we can observe from Python.
        """
        if SerialCore is None:
            raise RuntimeError("SerialCore is unavailable; can't promote legacy serial")
        core = SerialCore(getattr(serial, "cgb_mode", False))
        # Set the absolute counters before arming SC. Older PyBoy serial
        # objects may already be in the middle of a transfer; arming against
        # a fresh core clock would otherwise schedule the next edge in the
        # past and lose the transfer's timing state.
        for attr in ("last_cycles", "clock"):
            if hasattr(serial, attr):
                setattr(core, attr, getattr(serial, attr))
        if hasattr(serial, "SB"):
            core.set_SB(serial.SB)
        if hasattr(serial, "SC"):
            raw_sc = serial.SC
            core.set_SC(raw_sc)
            core.SC = raw_sc
        # Preserve the serial state fields exposed by the native PyBoy
        # implementation when promoting a legacy object. The register-only
        # fallback above remains compatible with older integrations which do
        # not expose these in-flight fields.
        for attr in (
            "transfer_enabled",
            "internal_clock",
            "double_speed",
            "cpu_speed_shift",
            "_shift_register",
            "_bits_remaining",
            "_cycles_to_interrupt",
            "clock_target",
        ):
            if hasattr(serial, attr):
                setattr(core, attr, getattr(serial, attr))
        return core

    @staticmethod
    def _make_serial_irq_raiser(pyboy):
        """Return a zero-arg closure that raises INTR_SERIAL on
        ``pyboy``'s CPU. Looked up lazily per call so nothing breaks
        if the CPU object gets reconstructed (unusual but possible
        after load_state).

        Defensive: if the attached object is a test fake without a
        ``mb.cpu.set_interruptflag``, the callback no-ops. Real PyBoy
        always has both.
        """
        INTR_SERIAL = 0x08  # IF bit 3, same constant PyBoy uses

        def _raise():
            cpu = getattr(getattr(pyboy, "mb", None), "cpu", None)
            if cpu is not None and hasattr(cpu, "set_interruptflag"):
                cpu.set_interruptflag(INTR_SERIAL)

        return _raise

    @staticmethod
    def _make_serial_completion_context_provider(pyboy):
        """Return a read-only byte-completion diagnostic snapshot.

        ``hSerialIgnoringInitialData`` is anchored at HRAM ``$FFAB`` in the
        audited Red/Blue and Yellow cartridge sources. The provider is called
        by ``NetworkBackend`` only after an external-clock byte completes and
        only when its opt-in transcript is enabled; it neither steps the
        emulator nor changes any serial or input state.
        """

        def _snapshot() -> dict[str, int]:
            result: dict[str, int] = {}
            cpu = getattr(getattr(pyboy, "mb", None), "cpu", None)
            pc = getattr(cpu, "pc", None)
            if pc is None:
                # PyBoy's Cython and source runtimes expose the program
                # counter under different spellings. Keep the legacy alias
                # for fakes and the source runtime.
                pc = getattr(cpu, "PC", None)
            if not isinstance(pc, bool):
                try:
                    result["cpu_pc"] = index(pc)
                except TypeError:
                    pass
            memory = getattr(pyboy, "memory", None)
            if memory is not None:
                try:
                    ignored_initial = int(memory[0xFFAB])
                except (AttributeError, IndexError, KeyError, TypeError, ValueError):
                    pass
                else:
                    result["h_serial_ignoring_initial_data"] = ignored_initial
            return result

        return _snapshot

    def _install_network_tick_owner(self, pyboy: _PyBoyLike) -> None:
        """Make each normal PyBoy frame an owner-side serial boundary.

        The public ``PyBoy.tick(count)`` can execute many frames in one
        call. Wrapping that outer method would leave an owner-queued edge
        waiting for the whole multi-frame call. Wrapping ``_tick`` instead
        services the queue between frames while holding the gate across the
        complete motherboard/ROM serial operation.
        """
        backend = self._network_backend
        if backend is None:
            raise RuntimeError("network tick ownership requires a backend")
        key = id(pyboy)
        if key in self._original_ticks:
            raise RuntimeError("PyBoy tick owner is already installed")
        owner_attribute = "_tick"
        original_tick = getattr(pyboy, owner_attribute, None)
        if not callable(original_tick):
            # Keep lightweight integrations and older PyBoy-like test
            # doubles usable. Real source PyBoy exposes ``_tick``; the
            # public method fallback cannot split multi-frame calls but is
            # still correctly serialized for a one-frame caller.
            owner_attribute = "tick"
            original_tick = getattr(pyboy, owner_attribute, None)
        if not callable(original_tick):
            raise TypeError("network link requires a callable PyBoy._tick or PyBoy.tick")

        @wraps(original_tick)
        def run_owned_frame(*args, **kwargs):
            with owner_for(pyboy).access(), self._serial_gate:
                # This boolean is transport pacing metadata only. It is not
                # an observation or assignment of the cartridge's hardware
                # serial clock source; the ROM owns that state.
                pacing_leader = self._network_is_internal_clock
                frame_barrier = self._network_frame_barrier and pacing_leader is not None
                if frame_barrier:
                    backend.begin_frame_turn(leader=bool(pacing_leader))

                def progress_owner() -> None:
                    pending_before = int(backend.debug_snapshot().get("pending_edge_requests", 0))
                    applied = backend.service_pending_edges(max_edges=1)
                    # A deferred request means the ROM's serial IRQ has not
                    # re-armed yet. Advance only this owner thread until that
                    # native re-arm is observable; do not run speculative
                    # frames while no edge is admitted. This callback is
                    # supplied for either pacing role: hardware SC ownership
                    # can legitimately be opposite the frame metadata.
                    if (
                        applied == 0
                        and pending_before > 0
                        and getattr(backend, "_local_core", None) is not None
                        and not bool(getattr(backend._local_core, "transfer_enabled", 0))
                    ):
                        original_tick(*args, **kwargs)

                try:
                    if getattr(backend, "_dispatch_to_owner", False):
                        backend.service_pending_edges()
                    result = original_tick(*args, **kwargs)
                except BaseException:
                    if frame_barrier:
                        backend.abort_frame_turn(leader=bool(pacing_leader))
                    raise
                finally:
                    if getattr(backend, "_dispatch_to_owner", False):
                        backend.service_pending_edges()
                if frame_barrier:
                    try:
                        backend.finish_frame_turn(
                            leader=bool(pacing_leader),
                            progress_callback=progress_owner,
                        )
                    except BaseException:
                        backend.abort_frame_turn(leader=bool(pacing_leader))
                        raise
                return result

        @wraps(original_tick)
        def owned_frame(*args, **kwargs):
            with owner_for(pyboy).access(), self._lifecycle_lock:
                owner_for(pyboy).ensure_open()
                if self._network_tick_active:
                    raise RuntimeError("recursive network emulator execution")
                self._network_tick_active = True
                try:
                    return run_owned_frame(*args, **kwargs)
                finally:
                    self._network_tick_active = False

        try:
            setattr(pyboy, owner_attribute, owned_frame)
        except (AttributeError, TypeError) as exc:
            raise TypeError(
                f"network link requires an instance-writable PyBoy.{owner_attribute} "
                "for serialized serial ownership"
            ) from exc
        self._original_ticks[key] = (owner_attribute, original_tick)

    @staticmethod
    def _make_network_owner_pump(backend: NetworkBackend):
        """Return a noexcept-safe owner-thread serial queue pump.

        ``Serial.tick`` is declared ``noexcept`` for PyBoy's Cython
        motherboard path. Any backend failure therefore must be converted
        into the backend's normal fail-closed state instead of escaping from
        the callback as an unraisable Cython exception.
        """

        # The backend and its inbound queue are fixed for the lifetime of an
        # attached owner pump.  Capture the queue once so the hot callback
        # does not perform an attribute lookup on every native boundary.
        edge_queue = getattr(backend, "_edge_queue", None)

        def _pump() -> None:
            try:
                # Native Serial invokes this callback at every safe owner
                # boundary, including the overwhelmingly common no-work
                # case.  ``empty()`` is only an admission hint: a reader may
                # enqueue immediately after this check, but that request
                # remains queued for the next native boundary (and the
                # unconditional post-frame owner service).  Do not dequeue
                # here, so the existing gate/lifecycle/error handling stays
                # exclusively in ``service_pending_edges``.
                if edge_queue is not None and edge_queue.empty():
                    return
                backend.service_pending_edges()
            except BaseException as exc:  # noqa: BLE001 - fail the link closed
                backend._mark_closed(exc)

        return _pump

    @staticmethod
    def _enable_network_owner_pump(
        core: object, backend: NetworkBackend
    ) -> tuple[object | None, object, object] | None:
        """Install the serial-tick owner pump when the native core supports it."""
        if not hasattr(core, "owner_dispatch_callback") or not hasattr(
            core, "owner_dispatch_enabled"
        ):
            # Lightweight legacy doubles do not expose the optional native
            # pump. The per-frame owner wrapper remains the safe fallback for
            # those integrations; the bundled patched PyBoy core always has
            # the fields and therefore gets the higher-throughput path.
            return None
        previous_callback = core.owner_dispatch_callback
        previous_enabled = core.owner_dispatch_enabled
        owner_pump = PyBoyLinkSession._make_network_owner_pump(backend)
        try:
            core.owner_dispatch_callback = owner_pump
            core.owner_dispatch_enabled = True
        except BaseException as exc:
            # If a native setter rejects the replacement, make the partial
            # install transactional before propagating the original error.
            try:
                core.owner_dispatch_enabled = previous_enabled
                core.owner_dispatch_callback = previous_callback
            except BaseException as rollback_error:  # noqa: BLE001
                exc.add_note(f"owner-dispatch callback rollback also failed: {rollback_error!r}")
            raise
        return previous_callback, previous_enabled, owner_pump

    @staticmethod
    def _disable_network_owner_pump(
        core: object,
        state: tuple[object | None, object, object] | None,
    ) -> None:
        """Restore a session-owned owner pump before detaching a core.

        If another integration replaced our callback while the session was
        attached, leave that replacement untouched rather than clobbering a
        newer owner during teardown.
        """
        if state is None or not hasattr(core, "owner_dispatch_callback"):
            return
        previous_callback, previous_enabled, installed_callback = state
        if core.owner_dispatch_callback is not installed_callback:
            return
        core.owner_dispatch_enabled = previous_enabled
        core.owner_dispatch_callback = previous_callback

    def _restore_network_tick_owner(self, pyboy: _PyBoyLike) -> None:
        """Restore a PyBoy tick method installed by :meth:`attach`."""
        original = self._original_ticks.pop(id(pyboy), None)
        if original is None:
            return
        owner_attribute, original_tick = original
        with self._serial_gate:
            setattr(pyboy, owner_attribute, original_tick)

    @staticmethod
    def _make_peer_progressor(pyboy):
        """Return a bounded one-instruction progress callback.

        A TCP peer continues running while the local master waits for an
        edge response. A same-process pair has no second OS thread, so a
        master edge can otherwise observe the peer between its serial IRQ
        handler and the next SB/SC arm. The coordinator calls this callback
        only on that narrow path; it advances the peer's motherboard one
        instruction and mirrors the hook bookkeeping used by
        :meth:`_interleave_one_frame`.

        Test doubles that do not expose a motherboard tick simply return
        ``False`` and retain the historical pull-up behavior.
        """

        def _progress() -> bool:
            mb = getattr(pyboy, "mb", None)
            tick = getattr(mb, "tick", None)
            if not callable(tick):
                return False
            old_singlestep = getattr(mb, "breakpoint_singlestep", 0)
            try:
                mb.breakpoint_singlestep = 1
                if tick():
                    mb.breakpoint_reinject()
                    bp = mb.breakpoint_reached()
                    if bp != (-1, -1, -1):
                        bank, addr, _ = bp
                        mb.breakpoint_remove(bank, addr)
                        mb.breakpoint_singlestep_latch = 0
                        handle_hooks = getattr(pyboy, "_handle_hooks", None)
                        if callable(handle_hooks):
                            handle_hooks()
                return True
            finally:
                mb.breakpoint_singlestep = old_singlestep

        return _progress

    def _initialize_network_clock_role(self, core: object) -> None:
        """Retain a deprecated compatibility hook without mutation.

        Network role selection is transport/session metadata. The cartridge
        must own FF01/FF02 and any in-flight transfer, so attach and public
        negotiation intentionally do not initialize native serial registers.
        Older integrations may still reach this private hook, so it remains a
        no-op rather than reintroducing a host-side serial write.
        """
        del core

    @_serialized_local_operation
    def negotiate_network_clock_role(self, peer_rom_version: str) -> bool | None:
        """Choose the session's startup clock-role metadata after HELLO.

        The TCP listener/connector role is a transport concern, not a
        Pokémon hardware rule. Color Red/Blue and Yellow have different ROM
        polling paths, so cross-family peers use the non-Yellow endpoint as
        the deterministic pacing leader. For differing Red/Blue versions,
        Red is the pacing leader regardless of TCP direction. Same-family
        pairs retain their caller-provided pacing orientation; this metadata
        does not elect the cartridge's native serial clock or imply Blue-to-
        Blue trade acceptance.

        The selected boolean is session metadata used for deterministic
        network pacing. This method never writes native FF01/FF02 serial
        registers; the ROM owns those registers, ``hSerialConnectionStatus``,
        and all serial role changes. The ROM's eventual hardware role may
        differ from this pacing metadata.

        Returns the selected role, or ``None`` when this is not a network
        session or the session was created without a default role.
        """
        if self._network_backend is None or self._network_is_internal_clock is None:
            return None
        if self._network_tick_active:
            raise RuntimeError("cannot negotiate a clock role during network execution")
        if not isinstance(peer_rom_version, str):
            raise TypeError("peer_rom_version must be a string")
        peer = peer_rom_version.strip().lower()
        local = self._local_rom_version
        if local is None:
            return bool(self._network_is_internal_clock)
        local = local.strip().lower()
        supported = {"red", "blue", "yellow"}
        if local not in supported or peer not in supported:
            raise ValueError(
                "network clock negotiation requires ROM versions in "
                f"{sorted(supported)}, got local={local!r}, peer={peer!r}"
            )

        selected_internal = bool(self._network_is_internal_clock)
        cross_family = (local == "yellow") != (peer == "yellow")
        if cross_family:
            # Keep the non-Yellow endpoint as the deterministic pacing leader.
            # This does not direct either cartridge's native clock source;
            # each ROM remains responsible for its own serial handshake.
            selected_internal = local != "yellow"
        elif local in {"red", "blue"} and peer in {"red", "blue"} and local != peer:
            # Keep Red as the pacing leader regardless of TCP direction when
            # the two ROM versions differ. The ROM still owns
            # hSerialConnectionStatus and all hardware role changes.
            selected_internal = local == "red"

        if selected_internal != self._network_is_internal_clock:
            self._network_is_internal_clock = selected_internal
        # Identical Color Red/Blue families can use the bounded frame barrier
        # immediately. Yellow's input-sensitive preamble needs native edge
        # transport until both peers reach a ROM-owned boundary; the
        # coordinator can then call set_network_frame_barrier(True). Cross-
        # family pairs likewise remain on native edge transport because their
        # polling windows differ.
        self._network_frame_barrier = not cross_family and not (
            local == "yellow" and peer == "yellow"
        )
        return selected_internal

    def set_network_frame_barrier(self, enabled: bool) -> None:
        """Enable or disable frame pacing at an agreed ROM boundary.

        Pacing-role negotiation chooses which endpoint leads the network frame
        barrier; it does not choose the native serial owner or hardware clock
        source. Negotiation cannot safely guess when a cartridge has finished
        its input-sensitive preamble. Coordinators may therefore switch the
        bounded frame barrier at a mutually agreed boundary after both peers
        have reached the same ROM-owned phase. Callers must make the same
        change on both endpoints while no frame is in flight.
        """
        if type(enabled) is not bool:
            raise TypeError("enabled must be a bool")
        with self._lifecycle_lock:
            self._network_frame_barrier = enabled

    @_serialized_local_operation
    def detach(self, pyboy: _PyBoyLike) -> None:
        """Restore ``pyboy.mb.serial.backend`` and (if paired) tear
        down the coordinator. No-op if ``pyboy`` isn't attached."""
        with self._lifecycle_lock:
            self._detach_locked(pyboy)

    def _detach_locked(self, pyboy: _PyBoyLike) -> None:
        # Caller holds the lifecycle lock.
        if pyboy not in self._pyboys:
            return
        if self._step_active or self._network_tick_active:
            raise RuntimeError("cannot detach during emulator execution")
        if self._network_backend is not None:
            # Re-check in case an adapter was monkeypatched after attach;
            # never release the core/IRQ records without proving bounded
            # detach capability first.
            self._require_network_lifecycle_contract()
        self._clear_local_epoch()
        # Tearing down the coordinator first ensures neither remaining
        # core keeps a stale CoordinatedBackend pointing at the
        # detached peer.
        if self._coord is not None:
            self._coord.detach(timeout_s=self._remaining_cleanup_time())
            self._coord = None
        idx = self._pyboys.index(pyboy)
        prev_backend = self._prev_backends[idx]
        prev_serial = self._prev_serials[idx]
        previous_owner_dispatch = self._previous_owner_dispatch[idx]
        core = self._cores[idx]
        if self._network_backend is not None:
            detach_local_core = getattr(
                self._network_backend, "detach_local_core", None
            )
            if not _call_with_timeout(
                detach_local_core, self._remaining_cleanup_time()
            ):
                raise RuntimeError(
                    "network core operations did not drain before cleanup deadline"
                )
            # Wait for an in-progress owner tick before restoring the serial
            # backend. The response worker never touches the core, so after
            # this point no background thread retains an emulator reference.
            with self._serial_gate:
                self._disable_network_owner_pump(core, previous_owner_dispatch)
            self._restore_network_tick_owner(pyboy)
        if prev_serial is not None:
            pyboy.mb.serial = prev_serial
        else:
            # Restore whatever backend the serial had before we touched it
            # (NullBackend by default on a fresh PyBoy).
            try:
                core.backend = prev_backend if prev_backend is not None else NullBackend()
            except AttributeError:
                # If PyBoy's Serial doesn't expose a settable ``backend``
                # yet (partial Agent-A merge), there's nothing to restore.
                pass
        self._pyboys.pop(idx)
        self._cores.pop(idx)
        self._prev_backends.pop(idx)
        self._prev_serials.pop(idx)
        self._previous_owner_dispatch.pop(idx)
        owner = self._owners.pop(idx) if idx < len(self._owners) else owner_for(pyboy)
        owner.release_provider(self)

    @_serialized_local_operation
    def detach_all(self) -> None:
        """Detach every attached PyBoy and close the session transport.

        A network backend is a session-level resource, rather than a
        per-PyBoy attachment.  Stop it after the attachments have been
        restored so its reader and edge-worker threads cannot retain the
        detached serial core.  Keep this in ``detach_all`` (the terminal
        session cleanup path) so ``detach`` retains its existing behavior of
        only restoring one PyBoy's serial backend.
        """
        with self._lifecycle_lock:
            errors: list[BaseException] = []
            for pyboy in list(reversed(self._pyboys)):
                try:
                    # Call the public method so integrations which wrap
                    # detach() still observe every cleanup attempt. The
                    # RLock makes this re-entrant for the normal path.
                    self.detach(pyboy)
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)

            if self._network_backend is not None:
                stop_error: BaseException | None = None
                try:
                    stopped = _call_with_timeout(
                        self._network_backend.stop,
                        self._remaining_cleanup_time(),
                    )
                    if stopped is False:
                        stop_error = RuntimeError(
                            "network backend workers did not stop before cleanup deadline"
                        )
                except BaseException as exc:  # noqa: BLE001
                    stop_error = exc
                if stop_error is not None:
                    if errors:
                        errors[0].add_note(f"network backend cleanup also failed: {stop_error!r}")
                    else:
                        errors.append(stop_error)

            if errors:
                first = errors[0]
                for extra in errors[1:]:
                    first.add_note(f"additional detach cleanup failure: {extra!r}")
                raise first

    def _remaining_cleanup_time(self) -> float:
        deadline = getattr(self._cleanup_state, "deadline", None)
        return 2.0 if deadline is None else max(0.0, deadline - time.monotonic())

    def _network_cancellation_generation(self) -> int:
        with self._network_cancel_lock:
            return self._network_cancel_generation

    def _cancel_network_transport(self, deadline: float | None) -> None:
        """Wake an active network owner before taking emulator locks.

        ``detach_all`` is terminal for the transport, so closing the socket
        is the only reliable way to interrupt a peer wait or a native serial
        edge.  The generation is published first so a just-returned raw
        frame cannot be reported as a successful operation after cleanup has
        begun.  Every attached network backend must expose a bounded
        ``stop(timeout_s=...)`` hook.
        """
        with self._network_cancel_lock:
            self._network_cancel_generation += 1
        backend = self._network_backend
        stop = getattr(backend, "stop", None)
        if not callable(stop):
            return
        timeout = 2.0 if deadline is None else max(0.0, deadline - time.monotonic())
        _call_with_timeout(stop, timeout)

    # --- accessors -----------------------------------------------------

    @property
    def attached(self) -> tuple[object, ...]:
        return tuple(self._pyboys)

    @property
    def cores(self) -> tuple[object, ...]:
        """Tuple of each attached PyBoy's ``mb.serial`` instance.

        Historically these were harness-owned ``SerialCore`` objects;
        post the PyBoy fork merge they're the native
        :class:`pyboy.core.serial.Serial` instances themselves (which
        duck-type identically — ``SB``, ``SC``, ``transfer_enabled``,
        ``apply_external_edge``, ``peek_out_bit``, ``backend``)."""
        return tuple(self._cores)

    @property
    def coordinator(self) -> LockstepCoordinator | None:
        """``None`` until both sides attach."""
        return self._coord

    @property
    def paired(self) -> bool:
        return self._coord is not None

    # --- step ----------------------------------------------------------

    def _clear_local_epoch(self) -> None:
        self._epoch_origins = self._epoch_expected = None
        self._epoch_mbs = self._epoch_serials = None
        self._physical_origins = self._physical_expected = self._physical_now = None
        self._physical_generations = None
        self._scheduler_fault = None
        self._active_lcd_markers = None
        self._active_physical_targets = None
        self._active_instruction_endpoint = None
        self._last_lcd_markers = (0, 0)
        self._scheduler_quantum_count = 0

    def validate_session_operation(self, operation: str) -> None:
        """Optional managed-Session hook; independent attached loads cannot resume a pair."""
        if self.paired and operation in {"step", "run_until_event", "load_state"}:
            raise PairedSessionOperationError(
                f"{operation} is unsupported while locally paired; use the pair owner or detach first"
            )

    @staticmethod
    def _read_physical_clock(p) -> tuple[int, int]:
        if not hasattr(p.mb, "cgb_mode"):
            raise RuntimeError("local scheduler requires runtime speed metadata")
        getter = getattr(p.mb, "get_physical_clock", None)
        if callable(getter):
            value = getter()
        elif p.mb.cgb_mode:
            raise RuntimeError("CGB local scheduler requires runtime physical clock support")
        else:
            # Fixed-speed DMG runtimes and legacy asset-free shells need no rate
            # transition metadata. This never treats KEY1 as the actual CPU speed.
            value = (0, int(p.mb.serial.clock) * 2)
        if (not isinstance(value, tuple) or len(value) != 2
                or any(isinstance(part, bool) or not isinstance(part, int)
                       or not 0 <= part <= (1 << 64) - 1 for part in value)):
            raise RuntimeError("invalid runtime physical clock metadata")
        return value

    def _begin_local_epoch(self) -> None:
        for p in self._pyboys:
            self._read_physical_clock(p)
            if not callable(getattr(p.mb, "tick", None)):
                raise RuntimeError("local scheduler requires instruction stepping")  # noqa: TRY004 - runtime capability contract
        self._epoch_mbs = tuple(p.mb for p in self._pyboys)
        self._epoch_serials = tuple(p.mb.serial for p in self._pyboys)
        self._epoch_origins = tuple(int(s.clock) for s in self._epoch_serials)
        self._epoch_expected = self._epoch_origins
        physical = tuple(self._read_physical_clock(p) for p in self._pyboys)
        self._physical_generations = tuple(value[0] for value in physical)
        self._physical_origins = tuple(value[1] for value in physical)
        self._physical_expected = self._physical_now = self._physical_origins
        self._scheduler_fault = None

    def _latch_fault(self, reason: str) -> None:
        if self._scheduler_fault is None:
            self._scheduler_fault = reason

    def _raise_fault(self) -> None:
        if self._scheduler_fault is not None:
            raise RuntimeError(
                "local scheduler fault; detach/recover before continuing: "
                + self._scheduler_fault
            )

    def _check_epoch(self, *, boundary: bool = False) -> tuple[int, int]:
        self._raise_fault()
        if self._epoch_origins is None or len(self._pyboys) != 2:
            raise RuntimeError("local stepping requires 2 attached runtime endpoints")
        # The local owner is a fixed two-endpoint pair for the duration of an
        # epoch. Keep the checks explicit and in endpoint order: this is the
        # hot path, but the live motherboard/serial identities still have to
        # be re-read on every observation so replacement is quarantined.
        p0, p1 = self._pyboys
        if p0.mb is not self._epoch_mbs[0] or p0.mb.serial is not self._epoch_serials[0]:
            self._latch_fault("attached motherboard or serial identity changed")
            self._raise_fault()
        if p1.mb is not self._epoch_mbs[1] or p1.mb.serial is not self._epoch_serials[1]:
            self._latch_fault("attached motherboard or serial identity changed")
            self._raise_fault()
        try:
            physical0 = self._read_physical_clock(p0)
            physical1 = self._read_physical_clock(p1)
        except Exception as error:  # noqa: BLE001 - latch any failed runtime clock observation
            self._latch_fault(str(error))
            self._raise_fault()
        if (physical0[0], physical1[0]) != self._physical_generations:
            self._latch_fault("physical clock load epoch changed; attached loads are unsupported")
            self._raise_fault()
        now0, now1 = physical0[1], physical1[1]
        previous0, previous1 = self._physical_now
        if now0 < previous0 or now1 < previous1:
            self._latch_fault("physical clock moved backwards")
            self._raise_fault()
        now = (now0, now1)
        if boundary and now != self._physical_expected:
            self._latch_fault("physical clock changed outside the local scheduler")
            self._raise_fault()
        self._physical_now = now
        serial0, serial1 = self._epoch_serials
        clocks = (int(serial0.clock), int(serial1.clock))
        if boundary and clocks != self._epoch_expected:
            self._latch_fault("clock changed outside the local scheduler; attached loads are unsupported")
            self._raise_fault()
        return clocks

    def _make_owned_peer_progressor(self, p):
        progress = self._make_peer_progressor(p)

        def owned_progress() -> bool:
            # Never throw a Python guard through a native serial callback.
            # The normal owner raises the latched failure after mb.tick returns.
            previous_progress = self._peer_progress_active
            try:
                if self._scheduler_fault is not None:
                    return False
                if not self._step_active or self._peer_progress_active:
                    self._latch_fault("unowned or recursive peer progress")
                    return False
                # A native callback is allowed to run only inside the active
                # physical quantum.  Outside that scope there is no bounded
                # owner horizon against which a one-instruction rearm can be
                # judged, so report the line idle without touching the peer.
                if self._active_physical_targets is None:
                    return False
                self._check_epoch()
                try:
                    index = self._pyboys.index(p)
                    owner_index = self._pyboys.index(self._active_instruction_endpoint)
                except ValueError:
                    self._latch_fault("peer endpoint is no longer attached")
                    return False
                if index == owner_index:
                    self._latch_fault("peer progress selected the active owner")
                    return False
                peer_elapsed = self._physical_now[index] - self._physical_origins[index]
                owner_elapsed = (
                    self._physical_now[owner_index]
                    - self._physical_origins[owner_index]
                )
                if peer_elapsed >= owner_elapsed:
                    # Never advance a peer beyond the active owner's
                    # post-sync physical frontier from a native callback.
                    # The next scheduler selection owns any remaining work.
                    return False
                if self._physical_now[index] >= self._active_physical_targets[index]:
                    # Do not let a native serial callback start the next
                    # public quantum. The owner scheduler will service that
                    # endpoint on the next request.
                    return False
                before = int(p.mb.serial.clock)
                self._peer_progress_active = True
                result = progress()
                # A bounded peer instruction may itself cross an LCD
                # boundary. Consume that notification before returning to the
                # native serial callback; never leave a display marker as a
                # hidden CPU barrier.
                self._consume_lcd_marker(p)
                if int(p.mb.serial.clock) < before:
                    self._latch_fault("peer clock moved backwards")
                self._check_epoch()
                return bool(result) and self._scheduler_fault is None
            except BaseException as error:  # noqa: BLE001 - native callback must latch every failure
                self._latch_fault("peer progress failed: " + repr(error))
                return False
            finally:
                self._peer_progress_active = previous_progress

        return owned_progress

    def _consume_lcd_marker(self, p: object) -> bool:
        """Consume one endpoint LCD marker without advancing its CPU.

        ``frame_done`` is a presentation boundary emitted by PyBoy's LCD,
        not a serial-safe stop condition.  The active local quantum records
        the marker separately and clears the one-shot flag so instruction
        ownership remains governed by the common physical frontier.
        """
        lcd = getattr(getattr(p, "mb", None), "lcd", None)
        if lcd is None or not bool(getattr(lcd, "frame_done", False)):
            return False
        lcd.frame_done = False
        if self._active_lcd_markers is not None:
            try:
                index = self._pyboys.index(p)
            except ValueError:
                self._latch_fault("LCD marker endpoint is no longer attached")
            else:
                self._active_lcd_markers[index] += 1
        return True

    @staticmethod
    def _positive_count(value, name: str) -> None:
        _validate_positive_int(value, name)

    @_serialized_local_operation
    def step(self, frames: int = 1, render: bool = False) -> None:
        """Advance a pair with one persistent, all-phase instruction scheduler.

        Runtime physical time supports normal and double CPU speed.
        Use this owner exclusively while paired;
        detach before loading states or independently advancing either endpoint.
        Legacy DMG runtimes without load epochs cannot detect same-clock loads.
        """
        self._positive_count(frames, "frames")
        effective_render = render or self._view
        if self._network_backend is not None:
            members = tuple(self._pyboys)
            if len(members) != 1:
                raise RuntimeError("network step() requires one attached instance")
            endpoint = members[0]
            owner = owner_for(endpoint)
            cancellation_generation = self._network_cancellation_generation()
            # Cancellation deliberately bypasses ownership: stop() must wake a
            # blocked tick. Snapshot the endpoint, and reject a detached member
            # before starting another frame; receiver mutation is not covered.
            with owner.access():
                for _ in range(frames):
                    if (
                        cancellation_generation
                        != self._network_cancellation_generation()
                    ):
                        raise EmulatorOwnershipError(
                            "network emulator operation was cancelled by cleanup"
                        )
                    owner.ensure_open()
                    current = tuple(self._pyboys)
                    if len(current) != 1 or current[0] is not endpoint:
                        raise EmulatorOwnershipError("network endpoint detached during step")
                    # The main network _tick wrapper retains frame turns,
                    # owner dispatch, cancellation, and transcript handling.
                    endpoint.tick(1, effective_render)
            return
        self._run_local_frames(frames, view=effective_render)

    @_serialized_local_operation
    def step_interleaved(
        self, frames: int = 1, *, chunk_cycles: int = 256,
        render: bool | None = None,
    ) -> None:
        """Alias the same all-phase scheduler; chunks are at most one instruction.

        chunk_cycles remains a validated compatibility hint, not an empirical
        instruction-count conversion. Every instruction reselects by real clock.
        """
        self._positive_count(frames, "frames")
        self._positive_count(chunk_cycles, "chunk_cycles")
        view = self._view if render is None else bool(render) or self._view
        if self._network_backend is not None:
            self.step(frames, render=view)
            return
        self._run_local_frames(frames, view=view)

    def _run_local_frames(self, frames: int, *, view: bool) -> None:
        if self._step_active:
            raise RuntimeError("local scheduler is already executing")
        self._check_epoch(boundary=True)
        self._step_active = True
        try:
            for _ in range(frames):
                self._interleave_one_frame(*self._pyboys, 1, view=view)
            self._epoch_expected = self._check_epoch()
            self._physical_expected = self._physical_now
        except BaseException as error:
            self._latch_fault("owner execution failed: " + repr(error))
            raise
        finally:
            self._step_active = False

    @staticmethod
    def _instruction(p) -> None:
        p.mb.breakpoint_singlestep = 1
        if p.mb.tick():
            p.mb.breakpoint_reinject()
            bp = p.mb.breakpoint_reached()
            if bp != (-1, -1, -1):
                bank, addr, _ = bp
                p.mb.breakpoint_remove(bank, addr)
                p.mb.breakpoint_singlestep_latch = 0
                p._handle_hooks()

    def _interleave_one_frame(self, a, b, ticks_per_chunk: int, *, view=False) -> None:
        pair = (a, b)
        if not self._step_active or tuple(self._pyboys) != pair:
            raise RuntimeError("frame stepping must use the attached local owner")
        previous_stepping = [p.mb.breakpoint_singlestep for p in pair]
        frame_started = time.monotonic()
        iterations = stalled = 0
        lcd_markers = [0, 0]
        quantum = int(self.PHYSICAL_QUANTUM)
        if quantum <= 0:
            raise ValueError("PHYSICAL_QUANTUM must be positive")
        # Each public frame advances one fixed quantum from the persistent
        # epoch origin.  Using the current clock directly would make a
        # faster endpoint's instruction overrun redefine the next frame's
        # horizon and would let cumulative phase drift grow without bound.
        quantum_index = self._scheduler_quantum_count + 1
        physical_targets = tuple(
            origin + quantum_index * quantum for origin in self._physical_origins
        )
        self._active_lcd_markers = lcd_markers
        self._active_physical_targets = physical_targets
        try:
            for p in pair:
                p._handle_events(p.events)
                p.mb.lcd.frame_done = False
                p.mb.lcd.disable_renderer = not view
                p.mb.sound.disable_sampling = True
                p.mb.sound.clear_buffer()
            clocks = self._check_epoch()
            while True:
                self._raise_fault()
                # LCD completion is a marker, not a stop flag. Consume both
                # endpoints before selecting the next owner; this is what
                # permits an endpoint whose LCD was reset early to continue
                # toward the same physical-time frontier as its peer.
                self._consume_lcd_marker(a)
                self._consume_lcd_marker(b)
                reached_a = self._physical_now[0] >= physical_targets[0]
                reached_b = self._physical_now[1] >= physical_targets[1]
                if reached_a and reached_b:
                    if time.monotonic() - frame_started > self.MAX_FRAME_SECONDS:
                        raise RuntimeError("local frame wall deadline exhausted")
                    break
                if iterations >= self.MAX_FRAME_INSTRUCTIONS:
                    raise RuntimeError("local frame instruction budget exhausted")
                if iterations % 1024 == 0 and time.monotonic() - frame_started > self.MAX_FRAME_SECONDS:
                    raise RuntimeError("local frame wall deadline exhausted")
                # Always select the endpoint with the smaller elapsed
                # physical time. A side that has already emitted one or more
                # display markers remains eligible until the common horizon;
                # no side is frozen at frame_done.
                elapsed_a = self._physical_now[0] - self._physical_origins[0]
                elapsed_b = self._physical_now[1] - self._physical_origins[1]
                if reached_a:
                    selected = b
                elif reached_b or elapsed_a < elapsed_b:
                    selected = a
                elif elapsed_b < elapsed_a:
                    selected = b
                else:
                    # Resolve an equal-time tie by serial role without
                    # requiring endpoints to be hashable or value-comparable.
                    selected = self._serial_step_order(a, b)[0]
                before = clocks
                previous_instruction_endpoint = self._active_instruction_endpoint
                self._active_instruction_endpoint = selected
                try:
                    self._instruction(selected)
                finally:
                    self._active_instruction_endpoint = previous_instruction_endpoint
                clocks = self._check_epoch()
                if any(after < prior for after, prior in zip(clocks, before)):
                    raise RuntimeError("local clock moved backwards during instruction")
                stalled = stalled + 1 if clocks == before else 0
                if stalled >= self.MAX_STALLED_INSTRUCTIONS:
                    raise RuntimeError("instruction stepping made no clock progress")
                # Clear a marker generated by this instruction before another
                # endpoint can drive a serial edge.  Peer progressors perform
                # the same bounded cleanup for nested instructions.
                self._consume_lcd_marker(selected)
                iterations += 1
            self._last_lcd_markers = tuple(lcd_markers)
            self._scheduler_quantum_count += 1
            for p in pair:
                # Preserve the historical one-shot presentation signal at
                # the public step boundary.  This compatibility signal is not
                # included in ``_last_lcd_markers``; the next quantum clears
                # it before any CPU/serial scheduling decision.
                p.mb.lcd.frame_done = True
                p.frame_count += 1
                p._post_handle_events()
                if view:
                    p.tick(0, True, False)
        finally:
            self._active_lcd_markers = None
            self._active_physical_targets = None
            for p, prior in zip(pair, previous_stepping):
                p.mb.breakpoint_singlestep = prior

    @staticmethod
    def _step_single_step_chunk(
        p: object,
        cycle_budget: int,
        *,
        stop_on_frame: bool = True,
    ) -> bool:
        """Advance ``p`` up to ``cycle_budget`` CPU cycles.

        Single-stepped instructions have variable lengths, so a fixed
        instruction count creates role-dependent timing skew. Real PyBoy
        exposes the CPU cycle counter; the instruction-count fallback keeps
        lightweight legacy test doubles usable. When ``stop_on_frame`` is
        false, a local LCD frame notification is consumed and stepping
        continues toward the caller's shared cycle horizon.
        """
        cpu = getattr(p.mb, "cpu", None)
        start_cycles = getattr(cpu, "cycles", None)
        fallback_ticks = max(1, cycle_budget // 7)
        max_ticks = min(
            max(fallback_ticks, cycle_budget * 4),
            PyBoyLinkSession._MAX_SINGLE_STEP_TICKS,
        )
        ticks = 0
        while ticks < max_ticks:
            lcd = getattr(p.mb, "lcd", None)
            if stop_on_frame and getattr(lcd, "frame_done", False):
                return True
            if not stop_on_frame and getattr(lcd, "frame_done", False):
                # PyBoy's motherboard stops immediately while this flag is
                # set. Consume the one-shot notification only when the
                # caller explicitly asked us to cross that boundary.
                lcd.frame_done = False
            # Re-arm singlestep every iteration so mb.tick returns after a
            # single CPU instruction — breakpoint handling below may clear
            # it.
            p.mb.breakpoint_singlestep = 1
            if p.mb.tick():
                # Breakpoint/singlestep return. Mirror pyboy._tick's
                # hook-firing logic (best-effort — skips plugin manager,
                # which isn't load-bearing for tests).
                p.mb.breakpoint_reinject()
                bp = p.mb.breakpoint_reached()
                if bp != (-1, -1, -1):
                    bank, addr, _ = bp
                    p.mb.breakpoint_remove(bank, addr)
                    p.mb.breakpoint_singlestep_latch = 0
                    p._handle_hooks()
            ticks += 1
            if start_cycles is not None:
                current_cycles = getattr(cpu, "cycles", start_cycles)
                if int(current_cycles) - int(start_cycles) >= cycle_budget:
                    break
            elif ticks >= fallback_ticks:
                break
        return bool(getattr(lcd, "frame_done", False))

    @staticmethod
    def _serial_step_order(a: object, b: object) -> tuple[object, object]:
        """Return a role-aware cooperative stepping order.

        The serial master generates the clock, but it is the slave that
        must first execute the ROM instructions which arm SC. Advancing
        the slave before the master prevents a local scheduler race from
        turning a valid first edge into a disconnected-cable pull-up.
        When the two roles are not distinguishable (idle, both armed as
        masters, or test doubles without serial metadata), preserve the
        historical ``(a, b)`` order.
        """
        serial_a = getattr(getattr(a, "mb", None), "serial", None)
        serial_b = getattr(getattr(b, "mb", None), "serial", None)
        a_internal = bool(getattr(serial_a, "internal_clock", False))
        b_internal = bool(getattr(serial_b, "internal_clock", False))
        if a_internal and not b_internal:
            return b, a
        if b_internal and not a_internal:
            return a, b
        return a, b


__all__ = [
    "PyBoyLinkSession",
]
