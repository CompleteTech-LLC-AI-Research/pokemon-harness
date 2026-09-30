"""Local stepping and frame-interleave methods for :class:`PyBoyLinkSession`.

Extracted verbatim from ``pyboy_link_session.py`` (#136)."""

from __future__ import annotations

import time

from pokered_harness.link._pyboy_link_session_support import (
    _serialized_local_operation,
)
from pokered_harness.ownership import EmulatorOwnershipError, owner_for


class _PyBoyLinkSteppingMixin:
    # Bounds an individual single-stepped chunk below.
    _MAX_SINGLE_STEP_TICKS: int = 4096

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
                    if cancellation_generation != self._network_cancellation_generation():
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
        self,
        frames: int = 1,
        *,
        chunk_cycles: int = 256,
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
            pair = tuple(self._pyboys)
            for _ in range(frames):
                self._interleave_one_frame(*pair, 1, view=view)
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
            chunked_runtime = self._is_chunked_runtime(pair)
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
                if (
                    iterations % 1024 == 0
                    and time.monotonic() - frame_started > self.MAX_FRAME_SECONDS
                ):
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
                    if chunked_runtime:
                        selected_index = 0 if selected is a else 1
                        remaining_physical = (
                            physical_targets[selected_index] - self._physical_now[selected_index]
                        )
                        # Physical time advances at one unit per CPU cycle in
                        # CGB double-speed mode and two units otherwise.  A
                        # transition can occur inside the chunk, so use the
                        # current rate and retain a bounded overshoot.
                        rate = 1 if bool(getattr(selected.mb, "double_speed", False)) else 2
                        cycle_budget = max(
                            1,
                            min(
                                self._REAL_SCHEDULER_CHUNK_CYCLES,
                                remaining_physical // rate,
                            ),
                        )
                        self._step_single_step_chunk(
                            selected,
                            cycle_budget,
                            stop_on_frame=False,
                        )
                    else:
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
                # ``_step_single_step_chunk`` bounds its own instruction
                # count.  Charge the conservative four-cycle minimum here so
                # the outer frame budget remains meaningful for real
                # runtimes; the exact instruction path charges one each time.
                iterations += max(1, cycle_budget // 4) if chunked_runtime else 1
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
    def _is_chunked_runtime(pair: tuple[object, object]) -> bool:
        """Return whether both endpoints are bundled PyBoy runtimes.

        The chunked scheduler is deliberately limited to real PyBoy objects.
        Contract tests use small fakes whose instruction order and validation
        failures are observable; retaining the exact path for those objects
        keeps the scheduler's adversarial guarantees unchanged.
        """

        for endpoint in pair:
            motherboard = getattr(endpoint, "mb", None)
            if motherboard is None:
                return False
            module = type(motherboard).__module__
            if not module.startswith("pyboy"):
                return False
            if not callable(getattr(motherboard, "tick", None)):
                return False
            if not hasattr(motherboard, "cpu"):
                return False
        return True

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
            _PyBoyLinkSteppingMixin._MAX_SINGLE_STEP_TICKS,
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
