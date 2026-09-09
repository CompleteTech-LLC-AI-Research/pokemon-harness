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
* Both local step methods use a persistent elapsed-physical-time instruction
  owner from second attachment onward, including CGB CPU speed transitions.
  Attached state loads and independent managed Session stepping are rejected;
  detach before recovery. No ROM or input phase selects a different scheduler.

This class is the natural extension point for the network backend
(milestone 8): :meth:`listen` / :meth:`connect` classmethods would
pair a single local core with a TCP-backed remote endpoint instead
of a second local core.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
import time
import threading
from functools import wraps
from contextlib import nullcontext

from pokered_harness.link.network_backend import NetworkBackend, NetworkBackendError
from pokered_harness.link.serial_coordinator import LockstepCoordinator
from pokered_harness.link.serial_core import NullBackend, SerialCore
from pokered_harness.ownership import EmulatorOwnershipError, owner_for, owner_group


@runtime_checkable
class _PyBoyLike(Protocol):
    """Minimal duck-type the session needs from a PyBoy instance.

    Real :class:`pyboy.PyBoy` satisfies this. A test fake need only
    expose a ``mb`` with a ``serial`` attribute (carrying a
    settable ``backend``) and a ``tick`` method.
    """

    mb: object

    def tick(self, count: int = 1, render: bool = True, sound: bool = False) -> bool:
        ...


class PairedSessionOperationError(RuntimeError):
    code = "paired_session_operation"


def _serialized_local_operation(function):
    @wraps(function)
    def call(self, *args, **kwargs):
        if self._network_backend is not None:
            return function(self, *args, **kwargs)
        # Snapshot membership without taking the provider lock first. Every
        # native access uses canonical emulator owners BEFORE provider state.
        members = tuple(self._pyboys)
        endpoints = members
        if function.__name__ == "attach":
            endpoint = args[0] if args else kwargs["pyboy"]
            endpoints += (endpoint,)
        cleanup = function.__name__ in {"detach", "detach_all"}
        with owner_group((owner_for(endpoint) for endpoint in endpoints), allow_closed=cleanup):
            with self._operation_lock:
                if len(members) != len(self._pyboys) or any(
                    before is not after for before, after in zip(members, self._pyboys)
                ):
                    raise EmulatorOwnershipError("provider membership changed; retry the operation")
                return function(self, *args, **kwargs)
    return call


class PyBoyLinkSession:
    """Pairs up to two PyBoy instances under a bit-accurate serial link."""

    #: Max attached instances. Gen I Pokémon is strictly 2-player.
    MAX_ATTACHED: int = 2
    # A normal DMG LCD frame is 70,224 CPU T-cycles.  The runtime physical
    # clock is measured in half-normal-speed T units, so one public scheduler
    # quantum is 140,448 units for both normal- and double-speed endpoints.
    # LCD markers are sampled inside this quantum and never define its stop.
    PHYSICAL_QUANTUM = 70_224 * 2
    MAX_FRAME_INSTRUCTIONS = 200_000
    MAX_FRAME_SECONDS = 30.0
    MAX_STALLED_INSTRUCTIONS = 32

    def __init__(
        self,
        network_backend: NetworkBackend | None = None,
        *,
        network_is_internal_clock: bool | None = None,
        local_rom_version: str | None = None,
        view: bool = False,
    ) -> None:
        self._pyboys: list[object] = []
        self._owners = []
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
        # Retain the constructor keyword for compatibility only. Transport
        # roles must never seed game RAM or choose the ROM's serial clock.
        self._local_rom_version = local_rom_version
        # Local time begins when the second endpoint attaches, not at a ROM
        # phase or public step boundary. Attached loads require detach first.
        self._epoch_origins: tuple[int, int] | None = None
        self._epoch_expected: tuple[int, int] | None = None
        self._epoch_mbs: tuple[object, object] | None = None
        self._epoch_serials: tuple[object, object] | None = None
        self._physical_origins = self._physical_expected = self._physical_now = None
        self._physical_generations = None
        self._scheduler_fault: str | None = None
        self._step_active = False
        self._peer_progress_active = False
        self._active_instruction_endpoint: object | None = None
        # Display completion is a per-endpoint notification.  It is kept
        # separate from the public scheduler quantum so an LCD phase reset
        # cannot freeze that endpoint's CPU/serial state.
        self._active_lcd_markers: list[int] | None = None
        self._active_physical_targets: tuple[int, int] | None = None
        self._last_lcd_markers: tuple[int, int] = (0, 0)
        self._scheduler_quantum_count = 0
        self._operation_lock = threading.RLock()
        # When True, per-frame stepping keeps the LCD renderer on and calls
        # each PyBoy's _post_tick (via pyboy.tick(0, True, False)) so the
        # SDL2 window actually flips and pumps events. Without this the
        # visible windows stay blank because _interleave_one_frame otherwise
        # drives mb.tick directly and skips the plugin manager.
        self._view: bool = view

    # --- construction --------------------------------------------------

    @classmethod
    def local(cls, *, view: bool = False) -> "PyBoyLinkSession":
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
    ) -> "PyBoyLinkSession":
        """Bind ``(host, port)``, accept one peer, return the session.

        Single-instance mode: the session holds a :class:`NetworkBackend`;
        :meth:`attach` wires that backend onto the local PyBoy's
        ``mb.serial.backend``. The peer process is expected to have
        used :meth:`connect` and to be driving its own PyBoy.

        Blocks until a peer connects.
        """
        backend, _listener = NetworkBackend.listen(
            port, host=host, local_rom_version=local_rom_version
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
    ) -> "PyBoyLinkSession":
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

        Returns the PyBoy ``Serial`` instance so callers can inspect
        its register state directly.

        Local mode: the second attachment pairs both instances via a
        :class:`LockstepCoordinator`; the coordinator installs a
        :class:`CoordinatedBackend` on each serial's ``backend``
        attribute.

        Network mode: only one instance attaches per session. The
        serial's ``backend`` is set to the :class:`NetworkBackend`;
        the reader thread starts so peer-driven (slave-mode) edges
        advance the local serial and fire the CPU's serial IRQ.

        Raises ``RuntimeError`` if ``pyboy`` is already attached, the
        session is at :attr:`MAX_ATTACHED`, or a network-mode session
        already has its one attachment.
        """
        if pyboy in self._pyboys:
            raise RuntimeError(f"already attached: {pyboy!r}")
        max_attached = 1 if self._network_backend is not None else self.MAX_ATTACHED
        if len(self._pyboys) >= max_attached:
            raise RuntimeError(
                f"session is full ({max_attached} instances max)"
            )

        mb = getattr(pyboy, "mb", None)
        if mb is None or not hasattr(mb, "serial"):
            raise ValueError("pyboy must expose mb.serial")

        owner = owner_for(pyboy)
        owner.ensure_open()

        # Attach is transactional. Promotion, backend installation, reader
        # startup, and local coordinator construction can all fail; none of
        # those partial mutations should escape to the caller.
        core = mb.serial  # prefer reusing the existing PyBoy Serial
        prev_backend = getattr(core, "backend", None)
        prev_serial = None
        prior_backends = [
            (existing_core, getattr(existing_core, "backend", None))
            for existing_core in self._cores
        ]
        appended = False
        network_attempted = self._network_backend is not None
        coord_before = self._coord
        claimed = False

        try:
            owner.claim_provider(self)
            claimed = True
            if self._network_backend is not None:
                self._validate_network_admission(self._network_backend, core)
            if not hasattr(core, "backend"):
                prev_serial = core
                core = self._promote_legacy_serial(core)
                mb.serial = core
                prev_backend = getattr(core, "backend", None)

            self._pyboys.append(pyboy)
            self._owners.append(owner)
            self._cores.append(core)
            self._prev_backends.append(prev_backend)
            self._prev_serials.append(prev_serial)
            appended = True

            if self._network_backend is not None:
                # Network-mode: hook the local core up to the TCP backend
                # and fire the slave-IRQ via this pyboy's CPU flag register
                # when peer-driven edges complete our transfer.
                core.backend = self._network_backend
                bind_owner = getattr(self._network_backend, "bind_owner", None)
                if callable(bind_owner):
                    bind_owner(owner)
                self._network_backend.start_receiver(
                    local_core=core,
                    irq_callback=self._make_serial_irq_raiser(pyboy),
                )
            elif len(self._cores) == 2:
                self._begin_local_epoch()
                # Local-mode pair: wire the in-process coordinator with
                # IRQ callbacks pointing at each motherboard's CPU. The
                # coordinator installs CoordinatedBackend on each core's
                # ``backend`` attribute.
                self._coord = LockstepCoordinator(
                    self._cores[0],
                    self._cores[1],
                    on_a_transfer_complete=self._make_serial_irq_raiser(
                        self._pyboys[0]
                    ),
                    on_b_transfer_complete=self._make_serial_irq_raiser(
                        self._pyboys[1]
                    ),
                    on_a_peer_unarmed=self._make_owned_peer_progressor(self._pyboys[1]),
                    on_b_peer_unarmed=self._make_owned_peer_progressor(self._pyboys[0]),
                )
                for attached_core in self._cores:
                    attached_core.backend.validate_session_operation = self.validate_session_operation

            return core
        except BaseException:
            if not claimed:
                # A competing provider still owns this emulator. Do not
                # perform rollback writes or stop an unrelated transport.
                raise
            if self._network_backend is None:
                self._clear_local_epoch()
            # Stop a network backend even when start_receiver or a serial
            # setter fails. This prevents a reader/worker thread from
            # retaining a promoted core after a rejected attach.
            if network_attempted and self._network_backend is not None:
                try:
                    self._network_backend.stop()
                except BaseException:
                    pass

            # A coordinator may have installed one side before failing. If
            # it is available, detach it; the explicit backend snapshots
            # below also cover constructors that fail before returning one.
            if self._coord is not None and self._coord is not coord_before:
                try:
                    self._coord.detach()
                except BaseException:
                    pass
                self._coord = coord_before

            for existing_core, existing_backend in prior_backends:
                try:
                    existing_core.backend = existing_backend
                except BaseException:
                    pass

            if appended:
                idx = self._pyboys.index(pyboy)
                self._pyboys.pop(idx)
                self._owners.pop(idx)
                self._cores.pop(idx)
                self._prev_backends.pop(idx)
                self._prev_serials.pop(idx)

            try:
                if prev_serial is not None:
                    mb.serial = prev_serial
                elif appended:
                    core.backend = prev_backend
            except BaseException:
                pass
            # Keep the original exception and traceback visible to callers.
            if claimed:
                owner.release_provider(self)
            raise

    @staticmethod
    def _validate_network_admission(backend: object, core: object) -> None:
        """Reject stale network providers before touching an emulator core."""
        if not callable(getattr(backend, "owner_scope", None)):
            raise NetworkBackendError("network backend lacks owner scope")
        if not callable(getattr(backend, "start_receiver", None)):
            raise NetworkBackendError("network backend lacks receiver startup")
        required = ("set_owner_pump", "claim_owner_pump", "release_owner_pump")
        if any(not callable(getattr(core, name, None)) for name in required):
            raise NetworkBackendError("serial core lacks exclusive owner pump claims")

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
        if hasattr(serial, "SB"):
            core.set_SB(getattr(serial, "SB"))
        if hasattr(serial, "SC"):
            raw_sc = getattr(serial, "SC")
            core.set_SC(raw_sc)
            core.SC = raw_sc
        for attr in ("last_cycles", "clock"):
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

    @_serialized_local_operation
    def detach(self, pyboy: _PyBoyLike) -> None:
        """Restore ``pyboy.mb.serial.backend`` and (if paired) tear
        down the coordinator. No-op if ``pyboy`` isn't attached."""
        if pyboy not in self._pyboys:
            return
        if self._step_active:
            raise RuntimeError("cannot detach during local scheduler execution")
        self._clear_local_epoch()
        # Tearing down the coordinator first ensures neither remaining
        # core keeps a stale CoordinatedBackend pointing at the
        # detached peer.
        if self._coord is not None:
            self._coord.detach()
            self._coord = None
        idx = self._pyboys.index(pyboy)
        prev_backend = self._prev_backends[idx]
        prev_serial = self._prev_serials[idx]
        core = self._cores[idx]
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
        owner = self._owners.pop(idx)
        self._cores.pop(idx)
        self._prev_backends.pop(idx)
        self._prev_serials.pop(idx)
        owner.release_provider(self)

    @_serialized_local_operation
    def detach_all(self) -> None:
        """Detach every attached PyBoy and close the session transport.

        A network backend is a session-level resource, rather than a
        per-PyBoy attachment. Stop it after the serial backends have been
        restored so its reader and edge-worker threads cannot retain a
        detached core. This is the terminal cleanup path; ``detach`` alone
        retains its existing behavior of restoring one PyBoy.
        """
        try:
            for pyboy in list(reversed(self._pyboys)):
                self.detach(pyboy)
        finally:
            if self._network_backend is not None:
                self._network_backend.stop()

    def close(self) -> None:
        """Close this link and release any attached transport resources."""
        self.detach_all()

    def __enter__(self) -> "PyBoyLinkSession":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

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
                raise RuntimeError("local scheduler requires instruction stepping")
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
        for i, p in enumerate(self._pyboys):
            if p.mb is not self._epoch_mbs[i] or p.mb.serial is not self._epoch_serials[i]:
                self._latch_fault("attached motherboard or serial identity changed")
                self._raise_fault()
        try:
            physical = tuple(self._read_physical_clock(p) for p in self._pyboys)
        except Exception as error:
            self._latch_fault(str(error))
            self._raise_fault()
        if tuple(value[0] for value in physical) != self._physical_generations:
            self._latch_fault("physical clock load epoch changed; attached loads are unsupported")
            self._raise_fault()
        now = tuple(value[1] for value in physical)
        if any(after < before for after, before in zip(now, self._physical_now)):
            self._latch_fault("physical clock moved backwards")
            self._raise_fault()
        if boundary and now != self._physical_expected:
            self._latch_fault("physical clock changed outside the local scheduler")
            self._raise_fault()
        self._physical_now = now
        clocks = tuple(int(s.clock) for s in self._epoch_serials)
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
            except BaseException as error:
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
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(name + " must be a positive integer")

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
            # Cancellation deliberately bypasses ownership: stop() must wake a
            # blocked tick. Snapshot the endpoint, and reject a detached member
            # before starting another frame; receiver mutation is not covered.
            with owner.access():
                scope = getattr(self._network_backend, "owner_scope", None)
                with scope() if callable(scope) else nullcontext():
                    for _ in range(frames):
                        owner.ensure_open()
                        current = tuple(self._pyboys)
                        if len(current) != 1 or current[0] is not endpoint:
                            raise EmulatorOwnershipError("network endpoint detached during step")
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
