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
* ``step_interleaved`` interleaves the two emulators in bounded cycle chunks
  and re-evaluates the internal/external clock order. This is the required
  path for serial-heavy ROM routines; ``step`` remains the public frame
  convenience method for ordinary UI work. A bounded local re-arm callback
  reduces transient slave gaps, while a genuinely disconnected peer still
  receives the documented keep-alive behavior.

This class is the natural extension point for the network backend
(milestone 8): :meth:`listen` / :meth:`connect` classmethods would
pair a single local core with a TCP-backed remote endpoint instead
of a second local core.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pokered_harness.link.network_backend import NetworkBackend
from pokered_harness.link.serial_coordinator import LockstepCoordinator
from pokered_harness.link.serial_core import NullBackend, SerialCore


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


class PyBoyLinkSession:
    """Pairs up to two PyBoy instances under a bit-accurate serial link."""

    #: Max attached instances. Gen I Pokémon is strictly 2-player.
    MAX_ATTACHED: int = 2
    # PyBoy's LCD uses 70224 CPU cycles for a normal DMG frame. This is only
    # a fallback for test doubles or older integrations which do not expose
    # the LCD's next-frame cycle hint; real PyBoy instances use that hint.
    _DEFAULT_LCD_FRAME_CYCLES: int = 70224
    # Keep an individual singlestepped chunk bounded even if a caller
    # supplies an unusually large ``chunk_cycles`` value.
    _MAX_SINGLE_STEP_TICKS: int = 4096
    _HRAM_SERIAL_CONNECTION_STATUS: int = 0xFFAA
    _STATUS_EXTERNAL: int = 0x01
    _STATUS_INTERNAL: int = 0x02

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
        self._network_is_internal_clock = network_is_internal_clock
        self._local_rom_version = local_rom_version
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

        if self._network_backend is not None:
            # Network-mode: hook the local core up to the TCP backend
            # and fire the slave-IRQ via this pyboy's CPU flag register
            # when peer-driven edges complete our transfer.
            core.backend = self._network_backend
            self._seed_network_role_status(pyboy)
            self._network_backend.start_receiver(
                local_core=core,
                irq_callback=self._make_serial_irq_raiser(pyboy),
            )
        elif len(self._cores) == 2:
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
                on_a_peer_unarmed=self._make_peer_progressor(self._pyboys[1]),
                on_b_peer_unarmed=self._make_peer_progressor(self._pyboys[0]),
            )

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
            lcd = getattr(mb, "lcd", None)
            # ``_interleave_one_frame`` normally stops a side as soon as
            # it reaches its LCD boundary. The other side can still be in
            # its current frame and hit a serial edge during that small
            # window. Allow this bounded re-arm callback to advance the
            # already-finished side a few instructions into its next
            # frame; the next outer frame setup re-establishes the normal
            # boundary discipline.
            if getattr(lcd, "frame_done", False):
                lcd.frame_done = False
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

    def _seed_network_role_status(self, pyboy: _PyBoyLike) -> None:
        """Best-effort role seed for two-process sessions.

        The old remote endpoint wrote ``hSerialConnectionStatus`` as part
        of its clock-role handshake. The TCP-backed SerialCore path still
        benefits from the same role hint so the game enters the Cable Club
        flow with consistent listener/master vs connector/slave state.
        """
        if self._network_is_internal_clock is None:
            return
        memory = getattr(pyboy, "memory", None)
        if memory is None:
            return
        status = (
            self._STATUS_INTERNAL
            if self._network_is_internal_clock
            else self._STATUS_EXTERNAL
        )
        try:
            memory[self._HRAM_SERIAL_CONNECTION_STATUS] = status
        except Exception:
            pass

    def detach(self, pyboy: _PyBoyLike) -> None:
        """Restore ``pyboy.mb.serial.backend`` and (if paired) tear
        down the coordinator. No-op if ``pyboy`` isn't attached."""
        if pyboy not in self._pyboys:
            return
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
        self._cores.pop(idx)
        self._prev_backends.pop(idx)
        self._prev_serials.pop(idx)

    def detach_all(self) -> None:
        """Detach every attached PyBoy in reverse order."""
        for pyboy in list(reversed(self._pyboys)):
            self.detach(pyboy)

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

    def step(self, frames: int = 1, render: bool = False) -> None:
        """Advance both attached PyBoy instances by ``frames`` frames.

        Interleaves one frame at a time on each side. Sufficient for
        phases where the game isn't in a tight serial-sync loop —
        preamble handshakes, dialog advancement, overworld movement.
        For the tight nibble-exchange loop in
        ``Serial_SyncAndExchangeNybble`` use :meth:`step_interleaved`
        instead; per-frame granularity is too coarse there because
        each side can complete ~17 full-byte serial transfers within
        a single frame while the peer is frozen.

        Raises ``RuntimeError`` if fewer than 2 instances are attached.
        """
        if len(self._pyboys) != self.MAX_ATTACHED:
            raise RuntimeError(
                f"step() requires {self.MAX_ATTACHED} attached instances, "
                f"have {len(self._pyboys)}"
            )
        effective_render = render or self._view
        for _ in range(frames):
            for pyboy in self._serial_step_order(*self._pyboys):
                pyboy.tick(1, effective_render)

    def step_interleaved(
        self,
        frames: int = 1,
        *,
        chunk_cycles: int = 256,
        render: bool | None = None,
    ) -> None:
        """Advance both PyBoys by ``frames`` frames with sub-frame
        interleaving for tight serial-sync phases.

        Instead of ticking one whole frame on each side, this alternates
        ~``chunk_cycles`` CPU cycles per side. That keeps the two CPUs
        close enough that a Pokémon serial-sync loop — which oscillates
        a side between SC=0x80 (slave) and SC=0x81 (master) several
        times per byte — sees its peer in the matching role. Per-frame
        interleaving (``step``) is too coarse for this because each
        frame fits ~17 full-byte transfers, so one side can burn its
        whole sync-loop iteration while the peer is frozen.

        Implementation detail: uses PyBoy's ``breakpoint_singlestep``
        mode to force ``mb.tick`` to return after every CPU instruction,
        then batches instructions into chunks. This mode is only
        available on the non-Cython PyBoy build.
        """
        if len(self._pyboys) != self.MAX_ATTACHED:
            raise RuntimeError(
                f"step_interleaved() requires {self.MAX_ATTACHED} "
                f"attached instances, have {len(self._pyboys)}"
            )
        if not isinstance(chunk_cycles, int) or isinstance(chunk_cycles, bool):
            raise ValueError("chunk_cycles must be a positive integer")
        if chunk_cycles <= 0:
            raise ValueError("chunk_cycles must be a positive integer")
        # ``mb.tick`` returns after one CPU instruction in singlestep mode,
        # but instruction lengths vary.  Pass the actual cycle budget to the
        # frame driver so the two sides stay close in emulated time rather
        # than in an empirical number of instructions.
        effective_view = self._view if render is None else bool(render) or self._view
        a, b = self._pyboys[0], self._pyboys[1]
        for _ in range(frames):
            self._interleave_one_frame(a, b, chunk_cycles, view=effective_view)

    @staticmethod
    def _interleave_one_frame(a, b, chunk_cycles: int, *, view: bool = False) -> None:
        """Drive ``a`` and ``b`` through one frame each, interleaved.

        When ``view`` is True the LCD renderer stays on and each PyBoy's
        ``_post_tick`` (via ``tick(0, True, False)``) is invoked after the
        frame so the SDL2 window flips and pumps events.
        """
        # Per-frame setup mirrors what pyboy._tick does.
        for p in (a, b):
            p._handle_events(p.events)
            p.mb.lcd.frame_done = False
            p.mb.lcd.disable_renderer = not view
            p.mb.sound.disable_sampling = True
            p.mb.sound.clear_buffer()

        try:
            a_cycles = PyBoyLinkSession._cpu_cycles(a)
            b_cycles = PyBoyLinkSession._cpu_cycles(b)
            if a_cycles is not None and b_cycles is not None:
                PyBoyLinkSession._advance_to_shared_cycle_horizon(
                    a,
                    b,
                    a_cycles,
                    b_cycles,
                    chunk_cycles,
                )
            else:
                # Keep lightweight legacy doubles usable. They do not have
                # a common emulated-time counter, so their only safe frame
                # boundary is the LCD flag; the outer bound prevents a
                # broken double from turning this loop into an infinite one.
                PyBoyLinkSession._advance_to_lcd_boundaries(
                    a, b, chunk_cycles
                )
        finally:
            for p in (a, b):
                p.mb.breakpoint_singlestep = 0

        for p in (a, b):
            p.frame_count += 1
            p._post_handle_events()
            if view:
                # Drive PyBoy's _post_tick (plugin manager post_tick +
                # frame_limiter) so the SDL2 window flips its backbuffer
                # and pumps events. tick(0) skips the inner _tick loop
                # but still reaches _post_tick.
                p.tick(0, True, False)

    @staticmethod
    def _cpu_cycles(pyboy: object) -> int | None:
        """Return a PyBoy CPU's absolute cycle counter when available."""
        cpu = getattr(getattr(pyboy, "mb", None), "cpu", None)
        cycles = getattr(cpu, "cycles", None)
        if cycles is None:
            return None
        try:
            return int(cycles)
        except (TypeError, ValueError, OverflowError):
            return None

    @classmethod
    def _next_lcd_frame_target(cls, pyboy: object, start_cycles: int) -> int:
        """Return the absolute CPU cycle at the next LCD frame boundary.

        ``_cycles_to_frame`` is maintained by PyBoy in CPU-cycle units,
        including the CGB speed shift. Reading it avoids assuming that both
        emulators entered this scheduler frame at the same LCD phase.
        """
        lcd = getattr(getattr(pyboy, "mb", None), "lcd", None)
        remaining = getattr(lcd, "_cycles_to_frame", None)
        try:
            remaining_cycles = int(remaining)
        except (TypeError, ValueError, OverflowError):
            remaining_cycles = cls._DEFAULT_LCD_FRAME_CYCLES
        return start_cycles + max(1, remaining_cycles)

    @classmethod
    def _advance_to_shared_cycle_horizon(
        cls,
        a: object,
        b: object,
        a_start: int,
        b_start: int,
        chunk_cycles: int,
    ) -> None:
        """Advance both sides through one common relative CPU-cycle horizon.

        A local ``lcd.frame_done`` is a one-shot notification, not a safe
        point at which to freeze one emulator. The peer may still be in the
        preceding LCD phase, and ROM code which samples ``rLY`` can then
        observe an impossible phase relationship. Both sides therefore run
        toward the later of their two next LCD boundaries. If one reaches
        its local boundary first, ``_step_single_step_chunk`` clears that
        notification before continuing; it is never used as the shared stop
        condition.
        """
        a_frame_target = cls._next_lcd_frame_target(a, a_start)
        b_frame_target = cls._next_lcd_frame_target(b, b_start)
        # CPU counters are absolute to each emulator's own lifetime and may
        # differ substantially after independently captured save states.
        # Share the relative interval to the later LCD boundary instead of
        # forcing the lower absolute counter to catch up billions of cycles.
        horizon = max(
            a_frame_target - a_start,
            b_frame_target - b_start,
        )
        a_target = a_start + horizon
        b_target = b_start + horizon
        chunk = max(4, chunk_cycles)
        # Four times the nominal chunk count leaves room for variable-length
        # instructions and a transient breakpoint return. It is an absolute
        # bound on scheduler rounds, not a wall-clock wait.
        max_rounds = max(1, ((horizon + chunk - 1) // chunk) * 4 + 4)
        # An instruction may overshoot the target, but must not run an
        # unbounded amount beyond it. The per-chunk tick cap below and this
        # absolute cycle limit make a stuck/broken PyBoy fail closed.
        # A singlestepped motherboard can cross the target by more than one
        # nominal instruction when an interrupt/HDMA boundary is serviced in
        # the same tick. Keep the guard finite, but allow a bounded multiple
        # of the caller's chunk so a legitimate CGB/DMG phase boundary does
        # not become a false scheduler failure.
        max_cycle_overshoot = max(32, chunk * 8)
        max_cycles_a = a_target + max_cycle_overshoot
        max_cycles_b = b_target + max_cycle_overshoot
        reached_a = reached_b = False
        rounds = 0

        while not (reached_a and reached_b):
            if rounds >= max_rounds:
                current_a = cls._cpu_cycles(a)
                current_b = cls._cpu_cycles(b)
                raise TimeoutError(
                    "PyBoy pair did not reach the shared LCD cycle horizon "
                    f"within {max_rounds} rounds "
                    f"(target_delta={horizon}, a={current_a}, b={current_b})"
                )
            rounds += 1

            # Let the slave reach its SC=0x80 arm point before the
            # internal-clock side can emit an edge. Re-evaluate this order
            # after every chunk because Pokémon swaps roles between serial
            # transfers.
            for pyboy in cls._serial_step_order(a, b):
                current = cls._cpu_cycles(pyboy)
                if current is None:
                    raise TimeoutError(
                        "PyBoy CPU cycle counter disappeared during "
                        "interleaved stepping"
                    )
                target = a_target if pyboy is a else b_target
                max_cycles = max_cycles_a if pyboy is a else max_cycles_b
                if current > max_cycles:
                    raise TimeoutError(
                        "PyBoy pair exceeded the bounded LCD cycle horizon "
                        f"(target_delta={horizon}, limit={max_cycles}, "
                        f"current={current})"
                    )
                if current >= target:
                    if pyboy is a:
                        reached_a = True
                    else:
                        reached_b = True
                    continue

                budget = min(chunk, target - current)
                cls._step_single_step_chunk(
                    pyboy,
                    budget,
                    stop_on_frame=False,
                )
                current = cls._cpu_cycles(pyboy)
                if current is None:
                    raise TimeoutError(
                        "PyBoy CPU cycle counter disappeared during "
                        "interleaved stepping"
                    )
                if current > max_cycles:
                    raise TimeoutError(
                        "PyBoy pair exceeded the bounded LCD cycle horizon "
                        f"(target_delta={horizon}, limit={max_cycles}, "
                        f"current={current})"
                    )
                if pyboy is a:
                    reached_a = current >= target
                else:
                    reached_b = current >= target

    @classmethod
    def _advance_to_lcd_boundaries(
        cls, a: object, b: object, chunk_cycles: int
    ) -> None:
        """Bounded fallback for test doubles without CPU cycle counters."""
        chunk = max(4, chunk_cycles)
        max_rounds = max(
            1,
            ((cls._DEFAULT_LCD_FRAME_CYCLES + chunk - 1) // chunk) * 4 + 4,
        )
        a_done = b_done = False
        rounds = 0
        while not (a_done and b_done):
            if rounds >= max_rounds:
                raise TimeoutError(
                    "PyBoy pair did not reach both LCD frame boundaries "
                    f"within {max_rounds} rounds"
                )
            rounds += 1
            for pyboy in cls._serial_step_order(a, b):
                if pyboy is a and a_done:
                    continue
                if pyboy is b and b_done:
                    continue
                done = cls._step_single_step_chunk(
                    pyboy,
                    chunk,
                    stop_on_frame=True,
                )
                if pyboy is a:
                    a_done = done
                else:
                    b_done = done

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
