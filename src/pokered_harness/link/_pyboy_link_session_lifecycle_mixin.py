"""Detach, cleanup, epoch, and introspection methods for :class:`PyBoyLinkSession`.

Extracted verbatim from ``pyboy_link_session.py`` (#136)."""

from __future__ import annotations

import time

from pokered_harness.link._pyboy_link_session_support import (
    PairedSessionOperationError,
    _call_with_timeout,
    _PyBoyLike,
    _serialized_local_operation,
    _validate_positive_int,
)
from pokered_harness.link.serial_coordinator import (
    LockstepCoordinator,
)
from pokered_harness.link.serial_core import (
    NullBackend,
)
from pokered_harness.ownership import owner_for


class _PyBoyLinkLifecycleMixin:
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
            detach_local_core = getattr(self._network_backend, "detach_local_core", None)
            if not _call_with_timeout(detach_local_core, self._remaining_cleanup_time()):
                raise RuntimeError("network core operations did not drain before cleanup deadline")
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
        if (
            not isinstance(value, tuple)
            or len(value) != 2
            or any(
                isinstance(part, bool)
                or not isinstance(part, int)
                or not 0 <= part <= (1 << 64) - 1
                for part in value
            )
        ):
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
                "local scheduler fault; detach/recover before continuing: " + self._scheduler_fault
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
            self._latch_fault(
                "clock changed outside the local scheduler; attached loads are unsupported"
            )
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
                    self._physical_now[owner_index] - self._physical_origins[owner_index]
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
