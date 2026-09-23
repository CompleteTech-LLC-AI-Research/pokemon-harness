"""Private helpers for :mod:`pokered_harness.link.timed_link_session` (issue #156).

Holds the cancellation-adapter pair and the lifecycle/failure-teardown mixin
extracted from the 1053-line ``timed_link_session`` module so that the
canonical module stays under the 1000-line split target.  Nothing here is part
of the public API; ``timed_link_session`` remains the import path and the
``TimedLinkSession`` composition point.
"""

from __future__ import annotations

import time

try:
    from pyboy.core.serial import SerialBackendError as _SerialBackendError
except ImportError:
    # Older external PyBoy forks do not expose the latched backend wrapper.
    # The source/runtime contract remains usable without recognizing it.
    _SERIAL_BACKEND_ERRORS: tuple[type[Exception], ...] = ()
else:
    _SERIAL_BACKEND_ERRORS = (_SerialBackendError,)

from .emulated_time import CoordinatorClosed
from .execution_adapter import ExecutionGovernorAdapter
from .timed_wire import Cancelled, ChannelClosed, DeadlineExceeded, ProtocolError


class _CancellationView:
    """Observe two thread-safe events without changing either or spawning a thread."""

    def __init__(self, internal, external):
        self._internal, self._external = internal, external

    def is_set(self):
        return self._internal.is_set() or (self._external is not None and self._external.is_set())

    def wait(self, timeout=None):
        # Event-compatible bounded waiting for transport control doubles too.
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while not self.is_set():
            remaining = 0.01 if deadline is None else deadline - time.monotonic()
            if remaining <= 0:
                return self.is_set()
            self._internal.wait(min(0.01, remaining))
        return True


class _TimedAdapter(ExecutionGovernorAdapter):
    def __init__(self, session):
        self.session = session
        super().__init__(
            session._coordinator,
            instruction_counter=lambda: session._board.cpu.retired_instructions,
            wait_for_progress=session._wait_for_progress,
            wait_timeout=session._timeout,
            max_wait_attempts=session._attempts,
        )

    def before(self, raw_cpu_clock, double_speed, kind, required_cpu_cycles):
        try:
            self.session._require_execution()
            self.session._safe_pump()
            return super().before(raw_cpu_clock, double_speed, kind, required_cpu_cycles)
        except BaseException as exc:
            # Direct PyBoy.tick has no outer session guard. Close its epoch
            # before propagating, but never detach inside this native callback.
            failure = self.session._normalize_failure(exc)
            self.session._fail(failure)
            if failure is not exc:
                raise failure from exc
            raise

    def after(self, *args):
        # CPU actuals are not peripheral settlement, even on success.
        super().after(*args)
        self.session._observe_map()


class _TimedLinkSessionLifecycle:
    """Lifecycle and failure teardown extracted from TimedLinkSession (#156)."""

    def _normalize_failure(self, exc):
        """Expose cancellation consistently across wire/governor close races.

        Do this before recording failure or cleanup: both close the channel.
        The native serial core may quarantine a typed cancellation by wrapping
        it in ``SerialBackendError``.  Recognize only that exact wrapper and a
        typed cause; unrelated native/caller failures keep their original
        exception object.  Never clear the core's terminal latch here.
        """
        native_cause = None
        if _SERIAL_BACKEND_ERRORS and isinstance(exc, _SERIAL_BACKEND_ERRORS):
            candidate = exc.__cause__
            if isinstance(
                candidate, (Cancelled, ChannelClosed, DeadlineExceeded, CoordinatorClosed)
            ):
                native_cause = candidate
        effective = native_cause if native_cause is not None else exc
        if self._cancel_view.is_set() and isinstance(
            effective, (Cancelled, ChannelClosed, DeadlineExceeded, CoordinatorClosed)
        ):
            first_wire_error = self.channel.error
            if isinstance(first_wire_error, ProtocolError):
                return first_wire_error
            if isinstance(effective, Cancelled) and native_cause is None:
                return effective
            if isinstance(effective, Cancelled):
                # Do not reuse the cause: raising it from its wrapper would
                # create a cyclic exception chain (cause -> wrapper -> cause).
                return Cancelled("timed session cancelled")
            return Cancelled("timed session cancelled")
        return exc

    def _fail(self, exc):
        if self._terminal_error is None:
            self._terminal_error = f"{type(exc).__name__}: {exc}"
        self._terminal.set()
        if self._coordinator is not None:
            if self._cancel_view.is_set():
                self._coordinator.cancel()
            else:
                self._coordinator.close()
        try:
            self.channel.close()
        # Cleanup must preserve the primary failure, including cancellation.
        except BaseException as cleanup_error:  # noqa: BLE001
            exc.add_note(f"channel cleanup failed: {cleanup_error}")

    def _cleanup(self, *, preserve=None):
        self._require_owner()
        if self._active or self._in_edge or self._pumping or self._control_active:
            raise RuntimeError("cannot detach during native execution")
        try:
            # Preflight the whole attachment before changing any owned field.
            # Otherwise a foreign backend could be left running ungoverned.
            if self._core is not None:
                if self._board.serial is not self._core:
                    raise RuntimeError("refusing cleanup of a replaced serial core")
                if (
                    self._core.backend is not self
                    and self._core.backend is not self._previous_backend
                ):
                    raise RuntimeError("refusing to overwrite a foreign serial backend")
                if (
                    self._core.owner_dispatch_callback is not self._previous_dispatch[0]
                    or self._core.owner_dispatch_enabled != self._previous_dispatch[1]
                ):
                    raise RuntimeError("refusing to overwrite foreign serial callbacks")
            if (
                self._adapter is not None
                and self._adapter._board is not None
                and not self._adapter._registered()
            ):
                raise RuntimeError("refusing to detach foreign execution callbacks")
            if self._adapter is not None and self._adapter._board is not None:
                # Account actuals from failures before/after the CPU report.
                self._adapter._recover()
                self._adapter.detach()
            if self._core is not None:
                if self._core.backend is self:
                    self._core.backend = self._previous_backend
                elif self._core.backend is not self._previous_backend:
                    raise RuntimeError("refusing to overwrite a foreign serial backend")
                if (
                    self._core.owner_dispatch_callback is not self._previous_dispatch[0]
                    or self._core.owner_dispatch_enabled != self._previous_dispatch[1]
                ):
                    raise RuntimeError("refusing to overwrite foreign serial callbacks")
            self._board = self._core = self._pyboy = None
        except BaseException as exc:
            if preserve is None:
                raise
            preserve.add_note(f"timed owner cleanup remains pending: {exc}")

    def cancel(self):
        self._cancel.set()
        self._terminal.set()
        if self._coordinator is not None:
            self._coordinator.cancel()
        self.channel.close()

    def close(self):
        self._terminal.set()
        if self._coordinator is not None:
            self._coordinator.close()
        self.channel.close()
        if self._owner is None:
            self._used = True
            return
        self._require_owner()
        self._cleanup()

    def snapshot(self):
        """Read locked accounting from any thread, including terminal outcomes.

        This never reads native emulator state or asserts wire quiescence.
        """
        if self._coordinator is None:
            raise RuntimeError("session has no attached accounting epoch")
        return self._coordinator.snapshot()
