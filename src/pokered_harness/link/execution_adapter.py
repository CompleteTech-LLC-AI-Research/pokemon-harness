"""Explicit, single-epoch bridge between motherboard hooks and time permits.

The owner must serialize attach, execution, and detach. Both injected callbacks
must be observational with respect to this motherboard. The progress callback
must return within its supplied remaining seconds; arbitrary blocking Python
cannot be preempted here. There is no default wait, transport, or activation.
The instruction counter must count actual instructions, including native ones;
neither PC changes nor clock deltas are used to guess an instruction count.
A real native counter provider remains incomplete and is not supplied here.
Fake-board/unit evidence does not establish native runtime integration.

The motherboard after hook carries no execution-exception flag. It can report
valid actuals even when execution then propagates an error. The owner must
catch propagated motherboard errors, close the coordinator, and explicitly
detach after the callback pair has unwound, before resuming or loading state.

Deadline or attempt exhaustion raises TimeoutError. Injected callback exceptions
propagate unchanged after best-effort accounting cleanup. Metadata and contract
violations raise EmulatedTimeError. All callback failures close the coordinator
and retain installed hooks, so failure never silently enables ungoverned work.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

from .emulated_time import EmulatedTimeCoordinator, EmulatedTimeError, SpeedTransition


class ExecutionGovernorAdapter:
    """One explicit attachment; successful detach permanently closes its epoch."""

    def __init__(
        self,
        coordinator: EmulatedTimeCoordinator,
        *,
        instruction_counter: Callable[[], int],
        wait_for_progress: Callable[[float], None],
        wait_timeout: float = 1.0,
        max_wait_attempts: int = 16,
    ) -> None:
        if not callable(instruction_counter) or not callable(wait_for_progress):
            raise TypeError("instruction_counter and wait_for_progress must be callable")
        if (
            type(wait_timeout) not in (int, float)
            or not math.isfinite(wait_timeout)
            or wait_timeout <= 0
        ):
            raise ValueError("wait_timeout must be finite and positive")
        if type(max_wait_attempts) is not int or max_wait_attempts < 1:
            raise ValueError("max_wait_attempts must be a positive integer")
        self.coordinator = coordinator
        self._counter = instruction_counter
        self._wait = wait_for_progress
        self._timeout = float(wait_timeout)
        self._attempts = max_wait_attempts
        self._board = None
        self._retired = False
        self._depth = 0
        self._permit = None
        self._start = None
        self._last_counter = None
        self._start_counter = None
        self._kind = None
        self._required = None
        # Bound method lookup creates a new object; retain registration identity.
        self._before_callback = self.before
        self._after_callback = self.after

    @staticmethod
    def _integer(value) -> bool:
        return type(value) is int and value >= 0

    def _fail(self, message: str) -> None:
        self.coordinator.close()
        raise EmulatedTimeError(message)

    def _read_counter(self) -> int:
        value = self._counter()
        if not self._integer(value) or (
            self._last_counter is not None and value < self._last_counter
        ):
            raise EmulatedTimeError("instruction counter must be a monotonic nonnegative int")
        self._last_counter = value
        return value

    def _observe(self, *, strict=True):
        board = self._board
        raw, speed = board.cpu.cycles, board.double_speed
        if not self._integer(raw) or (strict and type(speed) is not bool):
            raise EmulatedTimeError("invalid motherboard clock or rate")
        return (
            raw,
            speed,
            board.speed_transition_count,
            board.speed_transition_clock,
            board.speed_transition_double_speed,
        )

    def _registered(self) -> bool:
        return (
            self._board is not None
            and self._board.execution_before is self._before_callback
            and self._board.execution_after is self._after_callback
        )

    def attach(self, motherboard) -> None:
        """Validate a fresh coordinator and board before installing either hook."""
        if self._retired or self._board is not None or self._depth:
            raise EmulatedTimeError("adapter is attached or retired")
        snapshot = self.coordinator.snapshot()
        if (
            snapshot.closed
            or snapshot.cancelled
            or snapshot.local_half_cycles != 0
            or snapshot.peer_half_cycles != 0
            or snapshot.debt_half_cycles != 0
            or snapshot.pending_permit
            or snapshot.pending_delivery
            or snapshot.observed_raw_cpu_clock != snapshot.raw_cpu_clock
        ):
            raise EmulatedTimeError("attachment requires a fresh open coordinator epoch")
        if motherboard.execution_before is not None or motherboard.execution_after is not None:
            raise EmulatedTimeError("motherboard already has execution callbacks")
        self._board = motherboard
        try:
            observed = self._observe()
            if observed[:2] != (snapshot.raw_cpu_clock, snapshot.double_speed):
                raise EmulatedTimeError("motherboard clock/rate does not match coordinator")
            if (
                not self._integer(observed[2])
                or not self._integer(observed[3])
                or type(observed[4]) is not bool
            ):
                raise EmulatedTimeError("invalid motherboard transition metadata")
            self._read_counter()
            if self._observe() != observed:
                raise EmulatedTimeError("instruction counter mutated motherboard")
            motherboard.set_execution_governor(self._before_callback, self._after_callback)
        except BaseException:
            self._board = None
            self.coordinator.close()
            raise

    def detach(self) -> None:
        """Remove hooks only outside a pair; setter rejection preserves state."""
        if self._depth or self._permit is not None:
            raise EmulatedTimeError("cannot detach during an execution callback pair")
        if self._board is not None:
            if not self._registered():
                self._fail("execution callback registration changed")
            # Native active flags are private. The setter is the final authority,
            # including the gap after our after hook but before the pair returns.
            self._board.set_execution_governor(None, None)
        self._board = None
        self._retired = True
        self.coordinator.close()

    def _transition(self, observed, count, clock, speed):
        start_raw, start_speed = self._start[:2]
        end_raw, end_speed = observed[:2]
        valid_types = (
            self._integer(count)
            and self._integer(clock)
            and type(speed) is bool
            and type(end_speed) is bool
        )
        if valid_types and count == 0 and start_speed == end_speed:
            return None
        if (
            valid_types
            and count == 1
            and start_raw <= clock <= end_raw
            and speed == end_speed
            and speed != start_speed
        ):
            return SpeedTransition(clock, speed)
        # Public commit records the observed endpoint, clears the permit and
        # diagnoses an unknown normalized interval. Never edit coordinator state.
        return SpeedTransition(-1, False)

    def _record(self, observed, instructions, transition) -> None:
        permit = self._permit
        try:
            if (
                observed[0] == self._start[0]
                and observed[1] == self._start[1]
                and instructions == 0
                and transition is None
            ):
                self.coordinator.discard_unconsumed_permit(
                    permit,
                    raw_cpu_clock=observed[0],
                    instructions=instructions,
                    double_speed=observed[1],
                )
            else:
                self.coordinator.commit(
                    permit,
                    raw_cpu_clock=observed[0],
                    instructions=instructions,
                    speed_transition=transition,
                )
        finally:
            if not self.coordinator.snapshot().pending_permit:
                self._permit = None

    def _recover(self) -> None:
        """Preserve available actuals on a failing injected callback."""
        self.coordinator.close()
        if self._permit is None:
            return
        try:
            try:
                instructions = self._read_counter() - self._start_counter
            except BaseException:
                instructions = -1  # Invalid count, never a guessed native count.
            observed = self._observe(strict=False)
            count = (
                observed[2] - self._start[2]
                if self._integer(observed[2]) and self._integer(self._start[2])
                else -1
            )
            transition = self._transition(observed, count, observed[3], observed[4])
            self._record(observed, instructions, transition)
        except BaseException:
            # Preserve the primary callback exception. Coordinator diagnostics
            # retain any endpoint/elapsed interval that could be established.
            pass

    def before(self, raw_cpu_clock, double_speed, kind, required_cpu_cycles) -> int:
        """Reserve worst-case rate credit for one CPU instruction or HDMA step."""
        if self._depth or self._permit is not None:
            self._fail("recursive or unmatched execution before callback")
        self._depth += 1
        try:
            if not self._registered() or self._retired:
                self._fail("adapter is not registered")
            if (
                not self._integer(raw_cpu_clock)
                or type(double_speed) is not bool
                or type(kind) is not str
                or kind not in ("cpu", "hdma")
                or type(required_cpu_cycles) is not int
                or required_cpu_cycles != (24 if kind == "cpu" else 206)
                or raw_cpu_clock > 9223372036854775807 - required_cpu_cycles
            ):
                self._fail("invalid execution before metadata")
            self._start = self._observe()
            snapshot = self.coordinator.snapshot()
            if (
                self._start[:2] != (raw_cpu_clock, double_speed)
                or (snapshot.raw_cpu_clock, snapshot.double_speed) != self._start[:2]
                or not self._integer(self._start[2])
                or not self._integer(self._start[3])
                or type(self._start[4]) is not bool
            ):
                self._fail("execution start contradicts motherboard or coordinator")
            previous_counter = self._last_counter
            self._start_counter = self._read_counter()
            if previous_counter != self._start_counter:
                self._fail("instructions executed outside a governed interval")
            self._kind, self._required = kind, required_cpu_cycles
            deadline = time.monotonic() + self._timeout
            attempts = 0
            while True:
                if self._observe() != self._start or self._read_counter() != self._start_counter:
                    self._fail("execution occurred while awaiting a permit")
                if not self._registered():
                    self._fail("execution callbacks changed while awaiting a permit")
                current = self.coordinator.snapshot()
                if (
                    current.epoch != snapshot.epoch
                    or current.local_half_cycles != snapshot.local_half_cycles
                    or (current.raw_cpu_clock, current.double_speed) != self._start[:2]
                    or current.observed_raw_cpu_clock != snapshot.observed_raw_cpu_clock
                    or current.pending_permit
                ):
                    self._fail("coordinator timeline changed while awaiting a permit")
                if attempts and time.monotonic() >= deadline:
                    raise TimeoutError("execution permit deadline expired")
                self._permit = self.coordinator.reserve(
                    required_cpu_cycles * (2 if double_speed else 1)
                )
                if self._permit is not None:
                    if (
                        self._observe() != self._start
                        or self._read_counter() != self._start_counter
                    ):
                        self._fail("execution occurred while reserving a permit")
                    if (
                        self._permit.cpu_cycles >= required_cpu_cycles
                        and self._permit.half_cycles >= required_cpu_cycles * 2
                    ):
                        return required_cpu_cycles
                    self._record(self._observe(), self._read_counter() - self._start_counter, None)
                remaining = deadline - time.monotonic()
                if remaining <= 0 or attempts >= self._attempts:
                    raise TimeoutError("execution permit wait exhausted")
                attempts += 1
                self._wait(remaining)
        except BaseException:
            self._recover()
            raise
        finally:
            self._depth -= 1

    def after(
        self,
        start_raw,
        end_raw,
        start_double_speed,
        end_double_speed,
        kind,
        required_cpu_cycles,
        transition_count,
        transition_clock,
        transition_double_speed,
    ) -> None:
        """Record actuals first, including terminal and contradictory reports."""
        if self._depth or self._permit is None:
            self._fail("recursive or unmatched execution after callback")
        self._depth += 1
        try:
            initial_observed = self._observe(strict=False)
            counter_error = None
            try:
                instructions = self._read_counter() - self._start_counter
            except BaseException as exc:
                counter_error = exc
                instructions = -1
            observed = self._observe(strict=False)
            actual_count = observed[2] - self._start[2] if self._integer(observed[2]) else -1
            transition = self._transition(
                observed,
                transition_count,
                transition_clock,
                transition_double_speed,
            )
            actual_transition = self._transition(
                observed,
                actual_count,
                observed[3],
                observed[4],
            )
            if (
                transition != actual_transition
                or type(transition_count) is not int
                or transition_count != actual_count
                or (type(end_double_speed) is bool and end_double_speed != observed[1])
            ):
                transition = SpeedTransition(-1, False)
            contradiction = (
                not self._integer(start_raw)
                or not self._integer(end_raw)
                or type(start_double_speed) is not bool
                or type(end_double_speed) is not bool
                or (start_raw, start_double_speed) != self._start[:2]
                or (end_raw, end_double_speed) != observed[:2]
                or type(kind) is not str
                or kind != self._kind
                or type(required_cpu_cycles) is not int
                or required_cpu_cycles != self._required
                or not self._registered()
                or initial_observed != observed
                or (
                    transition_count == 1
                    and (transition_clock != observed[3] or transition_double_speed != observed[4])
                )
            )
            delta = observed[0] - self._start[0]
            try:
                self._record(observed, instructions, transition)
            except BaseException:
                if counter_error is not None:
                    raise counter_error
                raise
            if counter_error is not None:
                raise counter_error
            if contradiction:
                self._fail("execution metadata contradicts observed motherboard interval")
            if delta > self._required:
                self._fail("execution exceeded required CPU bound; actual elapsed recorded")
            if instructions > (1 if self._kind == "cpu" else 0):
                self._fail("execution exceeded instruction bound; actual elapsed recorded")
        except BaseException:
            self._recover()
            raise
        finally:
            self._depth -= 1
