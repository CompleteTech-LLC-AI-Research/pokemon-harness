"""Opt-in, transport-independent scheduling; not a hardware-accuracy claim.

Construction does not activate any emulator or production path. Each instance
owns one epoch, anchored at local raw CPU clock and explicit peer progress zero.
Budgets are FULL hardware cycles; all ``half_cycle(s)`` fields are integers in
half hardware cycles. Normal CPU ticks contribute two halves, double-speed
ticks one: retaining halves exactly preserves odd double-speed carry.

The caller serializes emulator execution, executes at most a reserved bound,
then commits actual CPU clock and instruction count before changing speed.
An instruction that cannot fit must not execute. Reset/load/reconnect requires
a new instance and an independently agreed epoch, never rebasing this one.

The configurable quantum is experimental: 128 full cycles remains the default.
Q256 is a provisional native-adapter choice, not evidence of native readiness.
Permits are execution bounds, not committed progress. An in-flight native
``on_edge`` callback before post-execution commit remains the adapter's
responsibility; this module does not synchronize that callback or integrate it.

Transport authentication and completeness are CALLER responsibilities. An
inclusive watermark attests that its contiguous received edge prefix includes
ALL edges at or before its time. This class checks epoch and receipt sequence,
not network origin. Edges may arrive late within an explicit full-cycle bound;
watermark contradictions always terminate independently of that bound. Observed
execution is never rolled back in terminal diagnostics. Delivered metadata must
be acknowledged before further permits; acknowledgement is a caller assertion,
not evidence that this module applied an edge to hardware or an emulator.

Request IDs increase lexicographically (use fixed-width numeric strings).
Only the latest progress/edge receipt is replayable; older sequence numbers
fail closed. These contracts bound duplicate history. Queued edges and payload
sizes are bounded as well; a transport must provide backpressure upstream.
"""

from __future__ import annotations

import heapq
import math
import time
from dataclasses import dataclass
from itertools import count
from threading import TIMEOUT_MAX, Condition
from typing import NoReturn

QUANTUM_CYCLES = 128
MAX_PENDING_EDGES = 1024
MAX_EDGE_PAYLOAD_BYTES = 4096
_DELIVERY_TOKENS = count(1)


class EmulatedTimeError(ValueError):
    """Invalid input or contract violation; the coordinator is terminal."""


class CoordinatorClosed(EmulatedTimeError):
    """Operation attempted after cancellation, closure, or contract failure."""


@dataclass(frozen=True)
class Permit:
    token: int
    cpu_cycles: int
    half_cycles: int
    instruction_cap: int


@dataclass(frozen=True)
class TimedEdge:
    epoch: str
    sequence: int
    at_half_cycle: int
    payload: bytes


@dataclass(frozen=True)
class EdgeDelivery(TimedEdge):
    delivered_half_cycle: int
    lateness_half_cycles: int
    batch_token: int


@dataclass(frozen=True)
class TimeSnapshot:
    local_half_cycles: int
    peer_half_cycles: int
    debt_half_cycles: int
    remaining_episode_half_cycles: int
    remaining_instructions: int
    pending_permit: bool
    closed: bool
    cancelled: bool
    epoch: str
    request_id: str | None
    active_episode: bool
    raw_cpu_clock: int
    terminal_reason: str | None
    pending_delivery: bool
    observed_raw_cpu_clock: int


class EmulatedTimeCoordinator:
    """Single-owner execution permits with concurrent peer/transport updates.

    ``rearm_budget`` is required, in full cycles (zero disables rearm).
    ``quantum_cycles`` is experimental, positive, and measured in full cycles.
    Validation failures on an existing instance close it and wake all waiters.
    A blocked reserve returns None; it never sleeps. ``_condition`` is the
    standard Condition used by waits, allowing tests to observe the wait seam.
    """

    def __init__(
        self,
        *,
        epoch: str,
        raw_cpu_clock: int,
        rearm_budget: int,
        max_edge_lateness: int,
        double_speed: bool = False,
        quantum_cycles: int = QUANTUM_CYCLES,
    ) -> None:
        self._condition = Condition()
        self._closed = False
        self._cancelled = False
        self._terminal_reason: str | None = None
        with self._condition:
            self._name(epoch, "epoch")
            self._integer(raw_cpu_clock, "raw_cpu_clock")
            self._integer(rearm_budget, "rearm_budget")
            self._integer(max_edge_lateness, "max_edge_lateness")
            self._integer(quantum_cycles, "quantum_cycles", 1)
            self._boolean(double_speed)
        self._epoch = epoch
        self._raw = raw_cpu_clock
        self._observed_raw = raw_cpu_clock
        self._rate = 1 if double_speed else 2
        self._rearm = rearm_budget * 2
        self._quantum = quantum_cycles * 2
        self._max_lateness = max_edge_lateness * 2
        self._local = self._peer = 0
        self._progress_sequence = 0
        self._pending: Permit | None = None
        self._token = 0
        self._episode: tuple[str, int, int] | None = None
        self._episode_open = False
        self._episode_end = 0
        self._instructions = 0
        self._edge_sequence = 0
        self._last_edge: TimedEdge | None = None
        self._edges: list[tuple[int, int, TimedEdge]] = []
        self._watermark = -1
        self._watermark_sequence = 0
        self._pending_delivery: int | None = None

    def _fail(self, message: str) -> NoReturn:
        if self._terminal_reason is None:
            self._terminal_reason = message
        self._closed = True
        self._condition.notify_all()
        raise EmulatedTimeError(message)

    def _open(self) -> None:
        if self._closed:
            raise CoordinatorClosed(self._terminal_reason or "coordinator closed")

    def _integer(self, value: int, name: str, minimum: int = 0) -> None:
        if type(value) is not int or value < minimum:
            self._fail(f"{name} must be an integer >= {minimum}")

    def _name(self, value: str, name: str) -> None:
        if type(value) is not str or not value or len(value) > 256:
            self._fail(f"{name} must be a nonempty string of at most 256 characters")

    def _boolean(self, value: bool) -> None:
        if type(value) is not bool:
            self._fail("double_speed must be bool")

    def _check_epoch(self, epoch: str) -> None:
        self._name(epoch, "epoch")
        if epoch != self._epoch:
            self._fail("epoch mismatch; construct a new coordinator")

    def _debt(self) -> int:
        return max(0, self._local - self._peer - self._quantum)

    def _active_episode(self) -> bool:
        return self._episode_open and self._episode_end > self._local and self._instructions > 0

    def snapshot(self) -> TimeSnapshot:
        """Read immutable accounting, including after terminal failure."""
        with self._condition:
            return TimeSnapshot(
                self._local,
                self._peer,
                self._debt(),
                max(0, self._episode_end - self._local),
                self._instructions,
                self._pending is not None,
                self._closed,
                self._cancelled,
                self._epoch,
                self._episode[0] if self._episode else None,
                self._episode_open,
                self._raw,
                self._terminal_reason,
                self._pending_delivery is not None,
                self._observed_raw,
            )

    def begin_episode(self, request_id: str, *, cycle_budget: int, instruction_cap: int) -> None:
        """Fix an episode's start allowance; retries cannot replenish it.

        The local ceiling is min(P+Q+R, start+cycle_budget). A new request
        requires exhausted or explicitly finished allowance AND zero debt.
        Finishing never changes debt; only committed peer progress repays it.
        An identical retired request remains a no-op and never reopens.
        """
        with self._condition:
            self._open()
            self._name(request_id, "request_id")
            self._integer(cycle_budget, "cycle_budget", 1)
            self._integer(instruction_cap, "instruction_cap", 1)
            if cycle_budget * 2 > self._rearm:
                self._fail("episode exceeds explicit rearm_budget")
            episode = (request_id, cycle_budget, instruction_cap)
            if self._episode is not None:
                if episode == self._episode:
                    return
                if request_id <= self._episode[0]:
                    self._fail("request IDs must increase; conflicting replay")
            if self._pending or self._debt() or self._active_episode():
                self._fail("cannot replace pending, indebted, or active episode")
            self._episode = episode
            self._episode_open = True
            self._episode_end = self._local + cycle_budget * 2
            self._instructions = instruction_cap
            self._condition.notify_all()

    def finish_episode(self, request_id: str) -> None:
        """Retire allowance and resume ordinary credit without changing debt.

        No permit may be outstanding. Matching retries are idempotent; the
        retained request ID and parameters prevent replay from restoring credit.
        A finished snapshot retains request_id but reports active_episode=False.
        """
        with self._condition:
            self._open()
            self._name(request_id, "request_id")
            if self._episode is None or request_id != self._episode[0]:
                self._fail("finish requires the current request ID")
            if self._pending is not None:
                self._fail("finish requires no outstanding permit")
            self._episode_open = False
            self._episode_end = self._local
            self._instructions = 0
            self._condition.notify_all()

    def _reserve(self, max_cpu_cycles: int) -> Permit | None:
        if (
            self._pending is not None
            or self._pending_delivery is not None
            or not self._progress_sequence
        ):
            return None
        ceiling = self._peer + self._quantum
        active = self._episode_open
        if active:
            if not self._instructions:
                return None
            ceiling = min(ceiling + self._rearm, self._episode_end)
        if self._edges:
            scheduled = self._edges[0][0]
            distance = scheduled - self._local
            if distance <= 0:
                return None
            # Allow a bounded instruction reservation through the lateness
            # deadline, without extending ordinary or episode credit.
            boundary = self._local + ((distance + self._rate - 1) // self._rate) * self._rate
            if boundary - scheduled > self._max_lateness:
                self._fail("future edge boundary cannot fit speed and lateness bound")
            ceiling = min(ceiling, scheduled + self._max_lateness)
        cycles = min(max_cpu_cycles, max(0, ceiling - self._local) // self._rate)
        if not cycles:
            return None
        self._token += 1
        self._pending = Permit(
            self._token, cycles, cycles * self._rate, self._instructions if active else cycles
        )
        return self._pending

    def reserve(self, max_cpu_cycles: int) -> Permit | None:
        with self._condition:
            self._open()
            self._integer(max_cpu_cycles, "max_cpu_cycles", 1)
            return self._reserve(max_cpu_cycles)

    def commit(self, permit: Permit, *, raw_cpu_clock: int, instructions: int) -> TimeSnapshot:
        """Commit actual positive progress, possibly smaller than the permit.

        Actual instructions may be zero (for example HALT or DMA progress),
        but CPU clock delta must be positive and consumes the cycle allowance.
        Permit identity is instance-bound, not merely equal dataclass fields.
        With a valid outstanding permit, actual positive elapsed time is recorded
        BEFORE checking overruns, instruction validity, and edge lateness. Thus a
        terminal snapshot may exceed scheduling invariants: it reports observed
        execution, not permission. This also applies if transport failure or close
        occurred during execution. A rollback cannot yield normalized elapsed.
        """
        with self._condition:
            if permit is not self._pending or self._pending is None:
                self._open()
                self._fail("invalid or already committed permit")
            self._integer(raw_cpu_clock, "raw_cpu_clock")
            self._observed_raw = raw_cpu_clock
            delta = raw_cpu_clock - self._raw
            if delta <= 0:
                self._pending = None
                self._fail("CPU clock rollback or zero progress; elapsed cannot advance")
            if self._episode_open and type(instructions) is int and instructions >= 0:
                self._instructions = max(0, self._instructions - instructions)
            self._local += delta * self._rate
            self._raw = raw_cpu_clock
            self._pending = None
            self._condition.notify_all()
            self._open()
            self._integer(instructions, "instructions")
            if delta > permit.cpu_cycles:
                self._fail("physical permit overrun; actual elapsed recorded")
            if instructions > permit.instruction_cap:
                self._fail("instruction cap exceeded; actual elapsed recorded")
            if self._edges and self._local - self._edges[0][0] > self._max_lateness:
                self._fail("edge lateness exceeds bound; actual elapsed recorded")
            return self.snapshot()

    def set_speed(self, *, raw_cpu_clock: int, double_speed: bool) -> None:
        """Switch only after committing all old-rate execution at this clock."""
        with self._condition:
            self._open()
            self._integer(raw_cpu_clock, "raw_cpu_clock")
            self._boolean(double_speed)
            if self._pending is not None or raw_cpu_clock != self._raw:
                self._fail("speed change requires committed old-rate clock and no permit")
            self._rate = 1 if double_speed else 2
            self._condition.notify_all()

    def record_peer_progress(
        self, *, epoch: str, sequence: int, committed_half_cycles: int
    ) -> bool:
        """Accept a contiguous committed-progress sequence, anchored at zero."""
        with self._condition:
            self._open()
            self._check_epoch(epoch)
            self._integer(sequence, "sequence", 1)
            self._integer(committed_half_cycles, "committed_half_cycles")
            if sequence == self._progress_sequence:
                if committed_half_cycles == self._peer:
                    return False
                self._fail("conflicting progress replay")
            if sequence != self._progress_sequence + 1:
                self._fail("noncontiguous progress sequence")
            if not self._progress_sequence and committed_half_cycles != 0:
                self._fail("first peer progress must anchor epoch at zero")
            if committed_half_cycles < self._peer:
                self._fail("peer progress rollback")
            self._progress_sequence = sequence
            self._peer = committed_half_cycles
            self._condition.notify_all()
            return True

    def receive_edge(
        self, *, epoch: str, sequence: int, at_half_cycle: int, payload: bytes
    ) -> bool:
        with self._condition:
            self._open()
            self._check_epoch(epoch)
            self._integer(sequence, "sequence", 1)
            self._integer(at_half_cycle, "at_half_cycle")
            if type(payload) is not bytes or len(payload) > MAX_EDGE_PAYLOAD_BYTES:
                self._fail("payload must be bytes within MAX_EDGE_PAYLOAD_BYTES")
            edge = TimedEdge(epoch, sequence, at_half_cycle, payload)
            if sequence == self._edge_sequence:
                if edge == self._last_edge:
                    return False
                self._fail("conflicting edge replay")
            if sequence != self._edge_sequence + 1:
                self._fail("noncontiguous edge receipt sequence")
            if at_half_cycle <= self._watermark:
                self._fail("edge contradicts inclusive completeness watermark")
            if self._local - at_half_cycle > self._max_lateness:
                self._fail("edge lateness exceeds bound")
            if len(self._edges) >= MAX_PENDING_EDGES:
                self._fail("edge backlog exhausted")
            self._edge_sequence = sequence
            self._last_edge = edge
            heapq.heappush(self._edges, (at_half_cycle, sequence, edge))
            self._condition.notify_all()
            return True

    def advance_watermark(self, *, epoch: str, sequence: int, through_half_cycle: int) -> None:
        """Trust an inclusive completeness attestation for the received prefix.

        Sequence zero is the explicit empty prefix. A missing receipt, time
        regression, or sequence regression fails closed; a bare time cannot
        establish completeness. The caller must authenticate the attestation.
        """
        with self._condition:
            self._open()
            self._check_epoch(epoch)
            self._integer(sequence, "sequence")
            self._integer(through_half_cycle, "through_half_cycle")
            if sequence != self._edge_sequence or sequence < self._watermark_sequence:
                self._fail("watermark requires complete contiguous receipt prefix")
            if through_half_cycle < self._watermark:
                self._fail("watermark rollback")
            self._watermark = through_half_cycle
            self._watermark_sequence = sequence
            self._condition.notify_all()

    def pop_ready_edges(self) -> tuple[EdgeDelivery, ...]:
        """Release ordered metadata at current committed time, never while executing.

        A nonempty batch blocks further execution until acknowledge_delivery.
        Neither release nor acknowledgement proves native edge application.
        """
        with self._condition:
            self._open()
            if self._pending is not None or self._pending_delivery is not None:
                return ()
            ready = []
            through = min(self._local, self._watermark)
            batch_token = next(_DELIVERY_TOKENS)
            while self._edges and self._edges[0][0] <= through:
                edge = heapq.heappop(self._edges)[2]
                lateness = self._local - edge.at_half_cycle
                if lateness > self._max_lateness:
                    self._fail("delivery lateness exceeds bound")
                ready.append(
                    EdgeDelivery(
                        edge.epoch,
                        edge.sequence,
                        edge.at_half_cycle,
                        edge.payload,
                        self._local,
                        lateness,
                        batch_token,
                    )
                )
            if ready:
                self._pending_delivery = batch_token
                self._condition.notify_all()
            return tuple(ready)

    def acknowledge_delivery(self, batch_token: int) -> None:
        """Caller asserts application; tokens are unique across in-process instances.

        Tokens reject wrong-instance and retired batches, not authenticate callers.
        """
        with self._condition:
            self._open()
            self._integer(batch_token, "batch_token", 1)
            if batch_token != self._pending_delivery:
                self._fail("invalid or already acknowledged delivery batch")
            self._pending_delivery = None
            self._condition.notify_all()

    def wait_for_permit(self, max_cpu_cycles: int, *, deadline: float) -> Permit | None:
        """Wait on the condition until a grant or an absolute monotonic deadline.

        Expiry returns None without mutation. Cancel/close wake and raise
        CoordinatorClosed. No wall-clock sleeping or polling supplies credit.
        """
        with self._condition:
            self._open()
            self._integer(max_cpu_cycles, "max_cpu_cycles", 1)
            if type(deadline) not in (int, float):
                self._fail("deadline must be finite monotonic seconds")
            try:
                finite = math.isfinite(deadline)
            except OverflowError:
                finite = False
            if not finite or deadline < 0:
                self._fail("deadline must be finite nonnegative monotonic seconds")
            while True:
                self._open()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                permit = self._reserve(max_cpu_cycles)
                if permit is not None:
                    return permit
                self._condition.wait(min(remaining, TIMEOUT_MAX))

    def cancel(self) -> None:
        with self._condition:
            self._cancelled = True
            self._closed = True
            if self._terminal_reason is None:
                self._terminal_reason = "coordinator cancelled"
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            if self._terminal_reason is None:
                self._terminal_reason = "coordinator closed"
            self._condition.notify_all()

    def reset(self, *, epoch: str, raw_cpu_clock: int) -> None:
        """Fail terminally: create a new instance after any clock/epoch reset."""
        with self._condition:
            self._open()
            self._fail("reset forbidden; construct a new coordinator for a new epoch")
