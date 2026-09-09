"""Owner-thread timed cable integration through the public PyBoy tick path.

The wire is loopback-only, not authenticated. Wall time bounds waiting; only
native CPU timestamps and retired instructions supply emulated progress.
Each session and channel epoch is single-use. Call close on the attaching
thread to restore the emulator; other threads may cancel and wake its waits.
"""

from __future__ import annotations

import ipaddress
import math
import socket
import threading
import time
from collections import deque
from contextlib import contextmanager

try:
    from pyboy.core.serial import SerialBackendError as _SerialBackendError
except ImportError:
    # Older external PyBoy forks do not expose the latched backend wrapper.
    # The source/runtime contract remains usable without recognizing it.
    _SERIAL_BACKEND_ERRORS: tuple[type[Exception], ...] = ()
else:
    _SERIAL_BACKEND_ERRORS = (_SerialBackendError,)

from .emulated_time import CoordinatorClosed, EmulatedTimeCoordinator, EmulatedTimeError
from .execution_adapter import ExecutionGovernorAdapter
from .timed_wire import (
    Cancelled,
    ChannelClosed,
    DeadlineExceeded,
    EdgeRequest,
    EdgeResponse,
    EmissionComplete,
    Fence,
    FenceAck,
    Progress,
    ProtocolError,
    Sync,
)


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


class TimedLinkSession:
    """One native emulator owner and one already-connected timed channel.

    Budgets are normal-speed cycles; wire timestamps are half cycles. Progress
    is coalesced at one quarter of Q in half-cycle units (Q/4 CPU cycles at
    normal speed), and forced at credit waits, edges and public tick returns.
    Arbitrary user hooks must return: Python callbacks cannot be preempted.
    """

    def __init__(
        self,
        channel,
        *,
        rearm_budget,
        rearm_instruction_cap,
        max_edge_lateness,
        quantum_cycles=256,
        operation_timeout=1.0,
        max_wait_attempts=16,
        inbound_capacity=64,
        cancel_event=None,
    ):
        for name, value, minimum in (
            ("rearm_budget", rearm_budget, 0),
            ("rearm_instruction_cap", rearm_instruction_cap, 1),
            ("max_edge_lateness", max_edge_lateness, 0),
            ("quantum_cycles", quantum_cycles, 1),
            ("max_wait_attempts", max_wait_attempts, 1),
            ("inbound_capacity", inbound_capacity, 1),
        ):
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        if (
            type(operation_timeout) not in (int, float)
            or not math.isfinite(operation_timeout)
            or operation_timeout <= 0
        ):
            raise ValueError("operation_timeout must be finite and positive")
        if type(channel.epoch) is not bytes or len(channel.epoch) != 16:
            raise ValueError("channel epoch must be 16 bytes")
        if cancel_event is not None and not isinstance(cancel_event, threading.Event):
            raise TypeError("cancel_event must be a threading.Event or None")
        self.channel = channel
        self._epoch = channel.epoch.hex()
        self._rearm = rearm_budget
        self._instruction_cap = rearm_instruction_cap
        self._lateness = max_edge_lateness * 2
        self._quantum = quantum_cycles
        self._threshold = max(1, quantum_cycles // 2)
        self._timeout = float(operation_timeout)
        self._attempts = max_wait_attempts
        self._capacity = inbound_capacity
        self._cancel = threading.Event()
        self._cancel_view = _CancellationView(self._cancel, cancel_event)
        self._terminal = threading.Event()
        self._owner = None
        self._used = False
        self._active = False
        self._attaching = False
        self._in_edge = False
        self._pumping = False
        self._pump_deadline = None
        self._pyboy = self._board = self._core = None
        self._adapter = self._coordinator = None
        self._original_tick = None
        self._previous_backend = None
        self._previous_dispatch = None
        self._terminal_error = None
        self._ingress = deque()
        self._edges = deque()
        self._edge_deadlines = {}
        self._delivery = deque()
        self._batch_token = None
        self._ingress_edge = self._admitted_edge = self._out_edge = 0
        self._peer_progress_sequence = 0
        self._ingress_watermark = -1
        self._ingress_prefix = 0
        self._coordinator_watermark = -1
        self._settled = 0
        self._sent_progress = self._sent_watermark = -1
        self._sent_prefix = 0
        self._waiting_edge = None
        self._response = None
        self._applied = 0
        self._held_deadline = None
        self._held_start = self._held_counter = None
        self._held_earliest = None
        self._episode_id = None
        self._episode_number = 0
        self._segments = []
        self._transition_count = 0
        self._mapped_raw = 0
        self._control_active = False
        self._control_deadline = None
        self._sync_markers = deque()
        self._inbound_fences = deque()
        self._outbound_fences = {}
        self._last_inbound_fence = self._last_outbound_fence = 0
        self._completed_prefix = 0
        self._completion_gaps = set()
        self._bootstrap_writes = []

    @staticmethod
    def _loopback(channel):
        sock = channel._sock
        if sock.family == getattr(socket, "AF_UNIX", object()):
            return
        if sock.family not in (socket.AF_INET, socket.AF_INET6):
            raise ValueError("timed session requires loopback TCP or a local socketpair")
        if sock.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
            raise ValueError("timed IP channels require TCP stream sockets")
        for endpoint in (sock.getsockname(), sock.getpeername()):
            address = ipaddress.ip_address(endpoint[0].split("%", 1)[0])
            address = getattr(address, "ipv4_mapped", None) or address
            if not address.is_loopback:
                raise ValueError("timed TCP requires both endpoints on loopback")

    def _check(self):
        if self._cancel_view.is_set():
            # The wire retains its first error; cancellation must not hide a
            # protocol violation already detected by its reader.
            if isinstance(self.channel.error, ProtocolError):
                raise self.channel.error
            raise Cancelled("timed session cancelled")
        if self._terminal.is_set():
            raise ChannelClosed("timed session is terminal")
        if self.channel.closed:
            raise self.channel.error or ChannelClosed("timed channel closed")
        now = time.monotonic()
        if self._control_deadline is not None and now >= self._control_deadline:
            raise DeadlineExceeded("control deadline expired")
        if self._pump_deadline is not None and now >= self._pump_deadline:
            raise DeadlineExceeded("owner pump deadline expired")

    def _require_owner(self):
        if threading.get_ident() != self._owner:
            raise RuntimeError("timed emulator mutation requires the attaching thread")

    def _require_execution(self):
        self._require_owner()
        self._check()
        if not self._active or self._in_edge or self._pumping or self._control_active:
            raise RuntimeError("recursive or unowned timed execution")
        self._verify_registration()

    def _verify_registration(self):
        if (
            self._board.serial is not self._core
            or self._core.backend is not self
            or self._core.owner_dispatch_callback is not self._previous_dispatch[0]
            or self._core.owner_dispatch_enabled != self._previous_dispatch[1]
            or not self._adapter._registered()
        ):
            raise RuntimeError("timed session registration was replaced")

    def attach(self, pyboy, *, deadline, startup_internal_clock=None):
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError("deadline must be finite absolute monotonic seconds")
        if startup_internal_clock is not None and type(startup_internal_clock) is not bool:
            raise TypeError("startup_internal_clock must be bool or None")
        if self._used:
            raise RuntimeError("timed session epoch cannot be reused")
        self._used = True
        self._owner = threading.get_ident()
        self._attaching = True
        try:
            self._check()
            self._loopback(self.channel)
            board, core = pyboy.mb, pyboy.mb.serial
            if (
                board.execution_before is not None
                or board.execution_after is not None
                or core.owner_dispatch_enabled
                or core.owner_dispatch_callback is not None
            ):
                raise RuntimeError("attach requires idle serial and no callback ownership")
            if core.transfer_enabled and (
                startup_internal_clock is None or core.internal_clock or core._bits_remaining != 8
            ):
                raise RuntimeError("startup requires idle or a fresh external eight-bit transfer")
            # Do not mistake a serial clock offset from a save for CPU time.
            if core.last_cycles != board.cpu.cycles:
                raise RuntimeError("attach requires serial.last_cycles == cpu.cycles")
            counter = board.cpu.retired_instructions
            if type(counter) is not int or counter < 0 or not callable(pyboy.tick):
                raise TypeError("native retired_instructions and public PyBoy.tick required")
            from .serial_core import NullBackend

            if not isinstance(core.backend, NullBackend):
                # Ownership conflicts are lifecycle errors, not invalid argument types.
                raise RuntimeError("serial backend already belongs to another link")  # noqa: TRY004
            self._pyboy, self._board, self._core = pyboy, board, core
            self._original_tick = pyboy.tick
            self._previous_backend = core.backend
            self._previous_dispatch = (core.owner_dispatch_callback, core.owner_dispatch_enabled)
            attach_state = (
                board.cpu.cycles,
                counter,
                board.double_speed,
                board.speed_transition_count,
                board.speed_transition_clock,
                board.speed_transition_double_speed,
                core.last_cycles,
                core.SB,
                core.SC,
                core._bits_remaining,
                core.transfer_enabled,
                core.internal_clock,
                core.clock,
                core.clock_target,
            )
            self._segments = [(board.cpu.cycles, 0, bool(board.double_speed))]
            self._mapped_raw = board.cpu.cycles
            self._transition_count = board.speed_transition_count
            self._coordinator = EmulatedTimeCoordinator(
                epoch=self._epoch,
                raw_cpu_clock=board.cpu.cycles,
                double_speed=bool(board.double_speed),
                rearm_budget=self._rearm,
                max_edge_lateness=self._lateness // 2,
                quantum_cycles=self._quantum,
                enforce_completeness=True,
            )
            self._adapter = _TimedAdapter(self)
            self.channel.handshake(deadline=deadline, cancel_event=self._cancel_view)
            self._check()
            if (
                pyboy.mb is not board
                or board.serial is not core
                or core.backend is not self._previous_backend
                or core.owner_dispatch_callback is not self._previous_dispatch[0]
                or core.owner_dispatch_enabled != self._previous_dispatch[1]
                or attach_state
                != (
                    board.cpu.cycles,
                    board.cpu.retired_instructions,
                    board.double_speed,
                    board.speed_transition_count,
                    board.speed_transition_clock,
                    board.speed_transition_double_speed,
                    core.last_cycles,
                    core.SB,
                    core.SC,
                    core._bits_remaining,
                    core.transfer_enabled,
                    core.internal_clock,
                    core.clock,
                    core.clock_target,
                )
            ):
                raise RuntimeError("emulator ownership or state changed during handshake")
            self._adapter.attach(board)
            core.backend = self
            if startup_internal_clock is not None:
                self._bootstrap(startup_internal_clock, deadline)
            self._send(Progress(0), deadline)
            self._sent_progress = 0
        except BaseException as exc:
            failure = self._normalize_failure(exc)
            self._fail(failure)
            self._cleanup(preserve=failure)
            if failure is not exc:
                raise failure from exc
            raise
        finally:
            self._attaching = False

    def _bootstrap(self, internal, deadline):
        """Exactly two or three native register writes; no emulated execution.

        Once a write starts, failure retains actual register state and retires
        this epoch. This explicitly restarts a fresh external byte.
        """
        core = self._core
        control = (core.SC & 2) | 0x80 | int(internal)
        writes = [("SB", 1 if internal else 2), ("SC", control)]
        if core.transfer_enabled:
            writes.insert(0, ("SC", core.SC & ~0x80))
        before = (self._board.cpu.cycles, self._board.cpu.retired_instructions)
        for register, value in writes:
            self._check()
            if time.monotonic() >= deadline:
                raise DeadlineExceeded("startup register-write deadline expired")
            self._verify_registration()
            # These native setters cannot execute CPU instructions. Avoid IO
            # accessors which also call Serial.tick while a byte is armed.
            (core.set_SB if register == "SB" else core.set_SC)(value)
            self._bootstrap_writes.append((register, value))
            if before != (self._board.cpu.cycles, self._board.cpu.retired_instructions):
                raise EmulatedTimeError("startup unexpectedly executed CPU work")

    def _deadline(self):
        deadline = time.monotonic() + self._timeout
        if self._control_deadline is not None:
            deadline = min(deadline, self._control_deadline)
        if self._pump_deadline is not None:
            deadline = min(deadline, self._pump_deadline)
        return min(deadline, self._held_deadline) if self._held_deadline is not None else deadline

    def _send(self, message, deadline=None):
        self._check()
        if deadline is not None and self._control_deadline is not None:
            deadline = min(deadline, self._control_deadline)
        if deadline is not None and self._held_deadline is not None:
            deadline = min(deadline, self._held_deadline)
        if deadline is not None and self._pump_deadline is not None:
            deadline = min(deadline, self._pump_deadline)
        self.channel.send(
            message,
            deadline=self._deadline() if deadline is None else deadline,
            cancel_event=self._cancel_view,
        )

    def _observe_map(self):
        board = self._board
        raw = board.cpu.cycles
        count = board.speed_transition_count
        delta = count - self._transition_count
        if raw < self._mapped_raw or delta not in (0, 1):
            raise EmulatedTimeError("unmapped native execution or multiple STOP transitions")
        if delta:
            clock = board.speed_transition_clock
            speed = bool(board.speed_transition_double_speed)
            if not self._mapped_raw <= clock <= raw or speed == self._segments[-1][2]:
                raise EmulatedTimeError("contradictory native STOP transition")
            half = self._map_clock(clock)
            if len(self._segments) >= 4096:
                raise EmulatedTimeError("STOP interval history capacity exhausted")
            self._segments.append((clock, half, speed))
            self._transition_count = count
        if bool(board.double_speed) != self._segments[-1][2]:
            raise EmulatedTimeError("native rate changed without STOP metadata")
        self._mapped_raw = raw

    def _map_clock(self, raw):
        if type(raw) is not int or not self._segments or raw < self._segments[0][0]:
            raise EmulatedTimeError("serial timestamp precedes epoch")
        for start, half, speed in reversed(self._segments):
            if raw >= start:
                return half + (raw - start) * (1 if speed else 2)
        raise EmulatedTimeError("serial timestamp has no exact rate interval")

    def _stage(self, message):
        if isinstance(message, EdgeResponse):
            if message.edge_id != self._waiting_edge or self._response is not None:
                raise ProtocolError("unexpected or duplicate edge response")
            self._response = message
            return
        if not isinstance(
            message, (Progress, EdgeRequest, EmissionComplete, Sync, Fence, FenceAck)
        ):
            raise ProtocolError("unexpected timed session message")
        if (
            isinstance(message, (Sync, Fence, FenceAck))
            and getattr(self.channel, "revision", 2) != 3
        ):
            raise ProtocolError("owner controls require wire revision 3")
        if (
            len(self._ingress) + len(self._edges) + len(self._delivery) + len(self._completion_gaps)
            >= self._capacity
        ):
            raise ProtocolError("session ingress capacity exhausted")
        if isinstance(message, EdgeRequest):
            self._edge_deadlines.setdefault(message.edge_id, time.monotonic() + self._timeout)
        self._ingress.append(message)

    def _poll(self):
        # A flood cannot turn a pre-step boundary into an unbounded drain.
        for _ in range(self._capacity):
            self._check()
            if self._pump_deadline is not None and time.monotonic() >= self._pump_deadline:
                raise DeadlineExceeded("owner pump deadline expired")
            try:
                message = self.channel.poll()
            except ChannelClosed as exc:
                raise self._normalize_failure(exc)
            if message is None:
                break
            self._stage(message)

    def _consume(self):
        for _ in range(min(len(self._ingress), self._capacity)):
            self._check()
            message = self._ingress.popleft()
            if isinstance(message, Progress):
                self._peer_progress_sequence += 1
                self._coordinator.record_peer_progress(
                    epoch=self._epoch,
                    sequence=self._peer_progress_sequence,
                    committed_half_cycles=message.settled_half_cycles,
                )
            elif isinstance(message, EdgeRequest):
                if (
                    message.edge_id != self._ingress_edge + 1
                    or message.scheduled_half_cycle <= self._ingress_watermark
                ):
                    raise ProtocolError("edge contradicts ingress prefix")
                self._ingress_edge = message.edge_id
                self._coordinator.receive_edge(
                    epoch=self._epoch,
                    sequence=message.edge_id,
                    at_half_cycle=message.scheduled_half_cycle,
                    payload=bytes([message.bit]),
                )
                self._admitted_edge = message.edge_id
                self._edges.append(message)
            elif isinstance(message, EmissionComplete):
                if (
                    message.last_edge_id != self._ingress_edge
                    or message.through_half_cycle < self._ingress_watermark
                ):
                    raise ProtocolError("invalid ingress emission prefix")
                self._ingress_watermark = message.through_half_cycle
                self._ingress_prefix = message.last_edge_id
                self._coordinator.advance_watermark(
                    epoch=self._epoch,
                    sequence=self._admitted_edge,
                    through_half_cycle=message.through_half_cycle,
                )
                self._coordinator_watermark = message.through_half_cycle
            elif isinstance(message, Sync):
                if len(self._sync_markers) >= self._capacity:
                    raise ProtocolError("peer sync marker capacity exhausted")
                self._sync_markers.append(message.marker_id)
            elif isinstance(message, Fence):
                if (
                    message.fence_id != self._last_inbound_fence + 1
                    or message.through_edge_id != self._ingress_edge
                    or len(self._inbound_fences) >= self._capacity
                ):
                    raise ProtocolError("invalid or excessive inbound fence")
                self._last_inbound_fence = message.fence_id
                self._inbound_fences.append(message)
            else:
                pending = self._outbound_fences.get(message.fence_id)
                if pending is None or pending != (message.through_edge_id, False):
                    raise ProtocolError("unexpected or conflicting fence acknowledgement")
                self._outbound_fences[message.fence_id] = (message.through_edge_id, True)

    def _publish(self, *, force=False):
        if self._in_edge:
            raise RuntimeError("cannot settle inside native serial callback")
        local = self._coordinator.snapshot().local_half_cycles
        if self._core.last_cycles != self._board.cpu.cycles:
            raise EmulatedTimeError("peripherals have not reached the CPU endpoint")
        self._settled = local
        progress_due = local > self._sent_progress and (
            force or local - self._sent_progress >= self._threshold
        )
        # Publish the proven emission prefix before granting peer CPU credit.
        if progress_due:
            self._check()
            self.channel.send_complete_progress(
                EmissionComplete(local, self._out_edge),
                Progress(local),
                deadline=self._deadline(),
                cancel_event=self._cancel_view,
            )
            self._sent_watermark, self._sent_prefix = local, self._out_edge
            self._sent_progress = local
        elif local > self._sent_watermark and (
            force or local - self._sent_watermark >= self._threshold
        ):
            self._send(EmissionComplete(local, self._out_edge))
            self._sent_watermark, self._sent_prefix = local, self._out_edge

    def _start_hold(self, scheduled, deadline):
        if self._held_deadline is None:
            self._held_deadline = deadline
            self._held_start = self._coordinator.snapshot().local_half_cycles
            self._held_counter = self._board.cpu.retired_instructions
            self._held_earliest = scheduled

    def _check_hold(self):
        if self._held_deadline is None:
            return
        snap = self._coordinator.snapshot()
        if (
            time.monotonic() >= self._held_deadline
            or snap.local_half_cycles - self._held_earliest > self._lateness
        ):
            raise DeadlineExceeded("held edge deadline or lateness exhausted")

    def _begin_rearm(self):
        self._check_hold()
        if self._episode_id is not None:
            if not self._coordinator.snapshot().active_episode:
                raise DeadlineExceeded("held rearm allowance exhausted")
            return
        snap = self._coordinator.snapshot()
        cycles = min(
            self._rearm - (snap.local_half_cycles - self._held_start + 1) // 2,
            (self._held_earliest + self._lateness - snap.local_half_cycles) // 2,
        )
        instructions = self._instruction_cap - (
            self._board.cpu.retired_instructions - self._held_counter
        )
        if cycles <= 0 or instructions <= 0:
            raise DeadlineExceeded("held rearm allowance exhausted")
        if snap.debt_half_cycles:
            return  # Peer progress, not a new episode, must repay existing debt.
        self._episode_number += 1
        request_id = f"{self._episode_number:020d}"
        self._coordinator.begin_delivery_rearm(
            self._batch_token,
            request_id,
            cycle_budget=cycles,
            instruction_cap=instructions,
        )
        self._episode_id = request_id

    def _finish_episode(self):
        if self._episode_id is not None:
            self._coordinator.finish_episode(self._episode_id)
            self._episode_id = None

    def _deliver(self, *, rearm):
        self._check_hold()
        core = self._core
        if not self._delivery:
            self._delivery.extend(self._coordinator.pop_ready_edges())
            if self._delivery:
                self._batch_token = self._delivery[0].batch_token
                ids = {edge.sequence for edge in self._delivery}
                self._edges = deque(edge for edge in self._edges if edge.edge_id not in ids)
                self._start_hold(
                    min(edge.at_half_cycle for edge in self._delivery),
                    min(self._edge_deadlines[edge.sequence] for edge in self._delivery),
                )
        for _ in range(self._capacity):
            if not self._delivery:
                break
            self._check()
            self._check_hold()
            if core.internal_clock:
                raise ProtocolError("incoming clock conflicts with local internal clock")
            if not core.transfer_enabled:
                if rearm:
                    self._begin_rearm()
                return
            edge = self._delivery[0]
            bit = core.peek_out_bit()
            completed = core.apply_external_edge(edge.payload[0])
            self._applied += 1
            # If send fails, the native shift is real and must never be retried.
            self._send(
                EdgeResponse(edge.sequence, self._coordinator.snapshot().local_half_cycles, bit)
            )
            if completed:
                self._board.cpu.set_interruptflag(0x08)
            self._complete_edge(edge.sequence)
            self._delivery.popleft()
            del self._edge_deadlines[edge.sequence]
        if self._batch_token is not None and not self._delivery:
            self._coordinator.acknowledge_delivery(self._batch_token)
            self._batch_token = None
            self._episode_id = None
            self._held_deadline = self._held_start = self._held_counter = self._held_earliest = None

    def _complete_edge(self, sequence):
        if sequence <= self._completed_prefix or sequence in self._completion_gaps:
            raise ProtocolError("edge completion repeated")
        self._completion_gaps.add(sequence)
        while self._completed_prefix + 1 in self._completion_gaps:
            self._completed_prefix += 1
            self._completion_gaps.remove(self._completed_prefix)
        if len(self._completion_gaps) > self._capacity:
            raise ProtocolError("completed edge prefix gap capacity exhausted")

    def _ack_fences(self):
        for _ in range(self._capacity):
            if not self._inbound_fences:
                break
            fence = self._inbound_fences[0]
            if fence.through_edge_id > self._completed_prefix:
                break
            self._send(FenceAck(fence.fence_id, fence.through_edge_id))
            self._inbound_fences.popleft()

    def _safe_pump(self, *, rearm=True, force=False, deadline=None):
        self._require_owner()
        self._check()
        if self._in_edge or self._pumping or self._coordinator.snapshot().pending_permit:
            raise RuntimeError("unsafe recursive timed pump")
        self._pumping = True
        self._pump_deadline = time.monotonic() + self._timeout
        if deadline is not None:
            self._pump_deadline = min(self._pump_deadline, deadline)
        try:
            snap = self._coordinator.snapshot()
            if (
                self._board.cpu.cycles != snap.raw_cpu_clock
                or self._board.cpu.retired_instructions != self._adapter._last_counter
            ):
                raise EmulatedTimeError("execution occurred outside the accounted interval")
            self._poll()
            self._consume()
            if self._edge_deadlines and time.monotonic() >= min(self._edge_deadlines.values()):
                raise DeadlineExceeded("incoming edge fixed deadline expired")
            self._deliver(rearm=rearm)
            self._ack_fences()
            self._publish(force=force)
            self._check()
        finally:
            self._pumping = False
            self._pump_deadline = None

    def _wait_for_progress(self, remaining):
        deadline = min(time.monotonic() + remaining, self._deadline())
        before = self._coordinator.snapshot()
        before_watermark = self._coordinator_watermark
        self._safe_pump(force=True, deadline=deadline)
        after = self._coordinator.snapshot()
        if (
            after.peer_half_cycles != before.peer_half_cycles
            or self._coordinator_watermark > before_watermark
            or after.request_id != before.request_id
            or after.pending_delivery != before.pending_delivery
        ):
            return
        deadline = min(deadline, self._deadline())
        self._stage(self.channel.receive(deadline=deadline, cancel_event=self._cancel_view))
        self._safe_pump(force=True, deadline=deadline)

    @contextmanager
    def _control_operation(self, deadline=None):
        try:
            self._require_owner()
            self._check()
            if (
                self._active
                or self._attaching
                or self._in_edge
                or self._pumping
                or self._control_active
                or self._board is None
            ):
                raise RuntimeError("control operations require an idle owner boundary")
            if getattr(self.channel, "revision", 2) != 3:
                raise ProtocolError("owner controls require wire revision 3")
            if deadline is not None:
                if type(deadline) not in (int, float) or not math.isfinite(deadline):
                    raise ValueError("deadline must be finite absolute monotonic seconds")
                if time.monotonic() >= deadline:
                    raise DeadlineExceeded("control deadline expired")
            self._verify_registration()
        except BaseException as exc:
            failure = self._normalize_failure(exc)
            self._fail(failure)
            # A rejected idle-owner call has no outer operation to detach it.
            # Foreign or recursive callers must leave cleanup to that owner.
            if threading.get_ident() == self._owner and not (
                self._active
                or self._attaching
                or self._in_edge
                or self._pumping
                or self._control_active
            ):
                self._cleanup(preserve=failure)
            if failure is not exc:
                raise failure from exc
            raise
        self._control_active = True
        self._control_deadline = deadline
        try:
            yield
            self._check()
            if deadline is not None and time.monotonic() >= deadline:
                raise DeadlineExceeded("control deadline expired")
        except BaseException as exc:
            failure = self._normalize_failure(exc)
            self._fail(failure)
            # The yielded operation has unwound. Reentrant close while it
            # was active could signal termination, but could not detach it.
            self._control_active = False
            self._cleanup(preserve=failure)
            if failure is not exc:
                raise failure from exc
            raise
        finally:
            self._control_active = False
            self._control_deadline = None

    def service_controls(self, *, deadline):
        """Bounded nonwaiting owner pump; never execute CPU or begin rearm.

        An empty transport poll returns immediately. The facade may call again
        under the same absolute deadline; absence of messages is not wire idle.
        """
        with self._control_operation(deadline):
            self._safe_pump(rearm=False, force=True, deadline=deadline)
            self._check()
            if time.monotonic() >= deadline:
                raise DeadlineExceeded("control deadline expired")
            return self.controls_snapshot()

    def announce_sync(self, marker, *, deadline):
        with self._control_operation(deadline):
            if type(marker) is not int or not 0 <= marker <= 255:
                raise ValueError("sync marker must be uint8")
            self._send(Sync(marker), deadline)

    def poll_peer_sync(self, marker):
        """Consume one owner-processed matching marker, without reading wire."""
        with self._control_operation():
            if type(marker) is not int or not 0 <= marker <= 255:
                raise ValueError("sync marker must be uint8")
            try:
                self._sync_markers.remove(marker)
            except ValueError:
                return False
            return True

    def start_fence(self, *, deadline):
        with self._control_operation(deadline):
            self._safe_pump(rearm=False, force=True, deadline=deadline)
            if len(self._outbound_fences) >= self._capacity:
                raise ProtocolError("outbound fence capacity exhausted")
            fence_id = self._last_outbound_fence + 1
            self._outbound_fences[fence_id] = (self._out_edge, False)
            self._send(Fence(fence_id, self._out_edge), deadline)
            self._last_outbound_fence = fence_id
            return fence_id

    def poll_fence(self, fence_id):
        """Consume a correlated acknowledgement; it proves only its prefix."""
        with self._control_operation():
            if type(fence_id) is not int or fence_id < 1:
                raise ValueError("fence ID must be a positive integer")
            pending = self._outbound_fences.get(fence_id)
            if pending is None or not pending[1]:
                return False
            del self._outbound_fences[fence_id]
            return True

    def controls_snapshot(self):
        """Owner-only local observations, never a permanent wire-idle assertion."""
        self._require_owner()
        pending_delivery = bool(self._delivery or self._edges or self._batch_token is not None)
        pending_response = self._waiting_edge is not None or self._in_edge
        return {
            "pending_ingress": len(self._ingress),
            "pending_delivery": pending_delivery,
            "pending_response": pending_response,
            "pending_inbound_fences": len(self._inbound_fences),
            "pending_outbound_fences": len(self._outbound_fences),
            "pending_sync_markers": len(self._sync_markers),
            "completed_edge_prefix": self._completed_prefix,
            "completion_gaps": len(self._completion_gaps),
            "bootstrap_writes": len(self._bootstrap_writes),
            "quiescent": not (
                self._ingress
                or pending_delivery
                or pending_response
                or self._inbound_fences
                or self._completion_gaps
            ),
        }

    def on_edge(self, our_bit, our_role):
        self._require_owner()
        self._check()
        if not self._active or self._in_edge or self._pumping or our_role != 1:
            raise RuntimeError("recursive or unowned serial edge")
        self._in_edge = True
        try:
            self._observe_map()
            observed_raw = self._core.last_cycles
            scheduled_raw = observed_raw - (self._core.clock - self._core.clock_target)
            if not scheduled_raw <= observed_raw <= self._board.cpu.cycles:
                raise EmulatedTimeError("invalid native serial timestamp ordering")
            scheduled = self._map_clock(scheduled_raw)
            observed = self._map_clock(observed_raw)
            if scheduled <= self._sent_watermark:
                raise ProtocolError("native edge contradicts emitted completeness prefix")
            deadline = self._deadline()
            self._out_edge += 1
            self._waiting_edge = self._out_edge
            self._response = None
            self._send(EdgeRequest(self._out_edge, scheduled, observed, our_bit), deadline)
            self._send(EmissionComplete(scheduled, self._out_edge), deadline)
            self._sent_watermark, self._sent_prefix = scheduled, self._out_edge
            # Only the last proven safe settlement may be advertised here.
            if self._settled > self._sent_progress:
                self._send(Progress(self._settled), deadline)
                self._sent_progress = self._settled
            for _ in range(self._attempts * self._capacity):
                if self._response is not None:
                    response = self._response
                    if (
                        response.delivered_half_cycle < scheduled
                        or response.delivered_half_cycle - scheduled > self._lateness
                    ):
                        raise ProtocolError("peer edge delivery outside lateness bound")
                    return response.bit
                self._stage(self.channel.receive(deadline=deadline, cancel_event=self._cancel_view))
            raise DeadlineExceeded("edge response message budget exhausted")
        finally:
            self._waiting_edge = self._response = None
            self._in_edge = False

    def tick(self, count=1, render=True, sound=True):
        try:
            self._require_owner()
        except BaseException as exc:
            self._fail(exc)
            raise
        if self._board is None:
            self._check()
        if self._active or self._attaching or self._control_active or self._board is None:
            exc = RuntimeError("recursive tick or unattached timed session")
            if self._active or self._control_active:
                self._fail(exc)
            raise exc
        self._active = True
        try:
            self._check()
            self._verify_registration()
            result = self._original_tick(count, render, sound)
            self._check()
            self._verify_registration()
            if self._coordinator.snapshot().pending_permit:
                raise EmulatedTimeError("public tick returned with uncommitted execution")
            self._safe_pump(rearm=False, force=True)
            return result
        except BaseException as exc:
            failure = self._normalize_failure(exc)
            self._fail(failure)
            self._active = False
            self._cleanup(preserve=failure)
            if failure is not exc:
                raise failure from exc
            raise
        finally:
            self._active = False

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


__all__ = ["TimedLinkSession"]
