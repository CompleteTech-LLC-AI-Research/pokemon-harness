"""Network attachment and serial-backend wiring for :class:`PyBoyLinkSession`.

Extracted verbatim from ``pyboy_link_session.py`` (#136)."""

from __future__ import annotations

from functools import wraps
from operator import index

from pokered_harness.link._pyboy_link_session_support import (
    _accepts_timeout,
    _call_with_timeout,
    _PyBoyLike,
    _serialized_local_operation,
)
from pokered_harness.link.network_backend import NetworkBackend
from pokered_harness.link.serial_coordinator import (
    LockstepCoordinator,
)
from pokered_harness.link.serial_core import (
    NullBackend,
    SerialCore,
)
from pokered_harness.ownership import owner_for


class _PyBoyLinkNetworkMixin:
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
                    set_context_provider(self._make_serial_completion_context_provider(pyboy))
                self._network_backend.start_receiver(
                    local_core=core,
                    irq_callback=self._make_serial_irq_raiser(pyboy),
                    serial_gate=self._serial_gate,
                    dispatch_to_owner=True,
                    defer_byte_responses=(
                        callable(getattr(pyboy, "_tick", None)) and hasattr(pyboy, "frame_count")
                    ),
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
                    stopped = _call_with_timeout(self._network_backend.stop, 2.0)
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
                    attached_core.backend.validate_session_operation = (
                        self.validate_session_operation
                    )
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
            # doubles usable. Bundled source and native PyBoy expose ``_tick``; the
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

                def advance_owner_frame():
                    token = backend.begin_owner_frame()
                    frame_before = getattr(pyboy, "frame_count", None)
                    result = original_tick(*args, **kwargs)
                    if (
                        frame_before is not None
                        and pyboy.frame_count > frame_before
                        and not bool(getattr(pyboy, "quitting", False))
                    ):
                        backend.finish_owner_frame(token)
                    return result

                def progress_owner() -> None:
                    pending_before = int(backend.debug_snapshot().get("pending_edge_requests", 0))
                    applied = backend.service_pending_edges(max_edges=1)
                    core = getattr(backend, "_local_core", None)
                    byte_completed = (
                        applied > 0
                        and owner_attribute == "_tick"
                        and core is not None
                        and not bool(getattr(core, "internal_clock", 0))
                        and not bool(getattr(core, "transfer_enabled", 0))
                    )
                    # A held final-bit response also needs progress when no
                    # ninth request exists. Its peer remains blocked until
                    # normal owner execution has had a complete frame.
                    if (
                        backend.begin_owner_frame() is not None
                        or byte_completed
                        or (
                            applied == 0
                            and pending_before > 0
                            and core is not None
                            and (
                                not bool(getattr(core, "transfer_enabled", 0))
                                or bool(getattr(core, "internal_clock", 0))
                            )
                        )
                    ):
                        advance_owner_frame()

                try:
                    if getattr(backend, "_dispatch_to_owner", False):
                        backend.service_pending_edges()
                    result = advance_owner_frame()
                except BaseException:
                    if frame_barrier:
                        backend.abort_frame_turn(leader=bool(pacing_leader))
                    else:
                        backend._mark_closed(
                            RuntimeError("emulator frame aborted with pending serial work")
                        )
                    raise
                finally:
                    if getattr(backend, "_dispatch_to_owner", False):
                        backend.service_pending_edges()
                if backend.begin_owner_frame() is not None:
                    try:
                        # A byte may complete inside the requested frame or
                        # in the post-frame pump. A subsequent ordinary frame
                        # is required even for callers without frame barriers.
                        # This is one bounded continuation, not a queue-drain
                        # loop which can execute indefinitely.
                        advance_owner_frame()
                    except BaseException:
                        if frame_barrier:
                            backend.abort_frame_turn(leader=bool(pacing_leader))
                        else:
                            backend._mark_closed(RuntimeError("emulator byte continuation aborted"))
                        raise
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
        owner_pump = _PyBoyLinkNetworkMixin._make_network_owner_pump(backend)
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

    def network_frame_barrier(self) -> bool:
        """Return this endpoint's current network frame-barrier flag.

        Read-only companion to :meth:`set_network_frame_barrier` so a caller
        can confirm the flag it just set on this endpoint without reaching
        into private state.  The flag is session pacing metadata only: the ROM
        still owns every serial register and hardware role change.
        """
        with self._lifecycle_lock:
            return self._network_frame_barrier
