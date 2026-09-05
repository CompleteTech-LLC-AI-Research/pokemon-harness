"""Deterministic public-MB contract tests; no emulator or ROM is loaded."""

from inspect import signature
from types import SimpleNamespace

import pytest

from pokered_harness.link.emulated_time import EmulatedTimeCoordinator, EmulatedTimeError, Permit
from pokered_harness.link.execution_adapter import ExecutionGovernorAdapter


class FakeBoard:
    """Only public native fields; hidden flag models setter refusal."""

    __slots__ = (
        "cpu",
        "double_speed",
        "speed_transition_count",
        "speed_transition_clock",
        "speed_transition_double_speed",
        "execution_before",
        "execution_after",
        "__pair_active",
        "instructions",
        "grants",
    )

    def __init__(self, double_speed=False):
        self.cpu = SimpleNamespace(cycles=0, halted=False)
        self.double_speed = double_speed
        self.speed_transition_count = 0
        self.speed_transition_clock = 0
        self.speed_transition_double_speed = double_speed
        self.execution_before = self.execution_after = None
        self.__pair_active = False
        self.instructions = 0
        self.grants = []

    def set_execution_governor(self, before, after):
        if self.__pair_active:
            raise RuntimeError("active callback pair")
        assert (before is None and after is None) or (callable(before) and callable(after))
        self.execution_before, self.execution_after = before, after

    def start(self, kind="cpu", required=24):
        self.__pair_active = True
        try:
            grant = self.execution_before(self.cpu.cycles, self.double_speed, kind, required)
            assert type(grant) is int and required <= grant <= 2**63 - 1
            self.grants.append(grant)
        except BaseException:
            self.__pair_active = False
            raise

    def finish(self, delta=4, instructions=1, kind="cpu", required=24, boundary=None):
        start, speed = self.cpu.cycles, self.double_speed
        self.cpu.cycles += delta
        self.instructions += instructions
        if boundary is not None:
            self.speed_transition_count += 1
            self.speed_transition_clock = start + boundary
            self.double_speed = not speed
            self.speed_transition_double_speed = self.double_speed
        report = (
            start,
            self.cpu.cycles,
            speed,
            self.double_speed,
            kind,
            required,
            int(boundary is not None),
            self.speed_transition_clock,
            self.speed_transition_double_speed,
        )
        try:
            self.execution_after(*report)
        finally:
            self.__pair_active = False
        return report


def setup(*, speed=False, quantum=256, wait=None, attempts=3):
    board = FakeBoard(speed)
    coordinator = EmulatedTimeCoordinator(
        epoch="adapter",
        raw_cpu_clock=0,
        double_speed=speed,
        rearm_budget=1024,
        max_edge_lateness=0,
        quantum_cycles=quantum,
    )
    coordinator.record_peer_progress(epoch="adapter", sequence=1, committed_half_cycles=0)

    def unexpected_wait(timeout):
        pytest.fail(f"unexpected wait: {timeout}")

    adapter = ExecutionGovernorAdapter(
        coordinator,
        instruction_counter=lambda: board.instructions,
        wait_for_progress=wait or unexpected_wait,
        wait_timeout=1.0,
        max_wait_attempts=attempts,
    )
    return board, coordinator, adapter


def test_constructor_detached_and_public_callback_arity():
    board, c, adapter = setup()
    assert board.execution_before is board.execution_after is None
    assert not c.snapshot().pending_permit
    assert len(signature(adapter.before).parameters) == 4
    assert len(signature(adapter.after).parameters) == 9
    adapter.attach(board)
    before, after = board.execution_before, board.execution_after
    board.start()
    board.finish()
    assert board.execution_before is before and board.execution_after is after


def test_instruction_counter_is_required():
    _, c, _ = setup()
    with pytest.raises(TypeError):
        ExecutionGovernorAdapter(c, wait_for_progress=lambda timeout: None)


@pytest.mark.parametrize(
    "kind,delta,instructions", [("cpu", 4, 1), ("hdma", 206, 0), ("cpu", 4, 0)]
)
def test_cpu_hdma_and_halt_charge_actual(kind, delta, instructions):
    board, c, adapter = setup()
    adapter.attach(board)
    c.begin_episode("0001", cycle_budget=512, instruction_cap=10)
    required = 206 if kind == "hdma" else 24
    board.cpu.halted = kind == "cpu" and instructions == 0
    board.start(kind, required)
    board.finish(delta, instructions, kind, required)
    s = c.snapshot()
    assert (s.raw_cpu_clock, s.local_half_cycles) == (delta, delta * 2)
    assert s.remaining_instructions == 10 - instructions
    assert not s.pending_permit and not s.closed


def test_zero_breakpoint_discards_verified_unconsumed_permit():
    board, c, adapter = setup()
    adapter.attach(board)
    before = c.snapshot()
    board.start()
    board.finish(0, 0)
    assert c.snapshot() == before
    board.start()
    board.finish()
    assert c.snapshot().local_half_cycles == 8


@pytest.mark.parametrize("speed", [False, True])
@pytest.mark.parametrize("boundary", [0, 1, 3, 4])
def test_stop_splits_both_speed_directions_at_boundaries(speed, boundary):
    board, c, adapter = setup(speed=speed)
    adapter.attach(board)
    board.start()
    board.finish(boundary=boundary)
    old_rate, new_rate = (1, 2) if speed else (2, 1)
    assert c.snapshot().local_half_cycles == boundary * old_rate + (4 - boundary) * new_rate
    assert c.snapshot().double_speed is not speed
    board.start()
    board.finish()
    assert c.snapshot().local_half_cycles == boundary * old_rate + (8 - boundary) * new_rate


def test_double_speed_requests_conservative_twice_required(monkeypatch):
    board, c, adapter = setup(speed=True)
    requests = []
    reserve = c.reserve

    def record(required):
        requests.append(required)
        return reserve(required)

    monkeypatch.setattr(c, "reserve", record)
    adapter.attach(board)
    board.start()
    board.finish()
    assert requests == [48]
    assert c.snapshot().local_half_cycles == 4


@pytest.mark.parametrize("cpu,halves", [(23, 100), (48, 47)])
def test_both_permit_dimensions_must_fit_before_grant(monkeypatch, cpu, halves):
    waits = []
    board, c, adapter = setup(speed=True, wait=lambda timeout: waits.append(timeout))
    reserve = c.reserve

    def constrained(required):
        p = reserve(required)
        if p is not None:
            # Inject an identity-valid coordinator result to isolate adapter validation.
            p = Permit(p.token, cpu, halves, p.instruction_cap)
            c._pending = p
        return p

    monkeypatch.setattr(c, "reserve", constrained)
    adapter.attach(board)
    with pytest.raises(TimeoutError):
        board.start()
    assert not board.grants and board.instructions == 0
    assert not c.snapshot().pending_permit
    assert len(waits) <= 3


def test_partial_permit_discarded_before_callback_then_peer_unblocks():
    calls = []

    def wait(timeout):
        calls.append(timeout)
        assert not c.snapshot().pending_permit
        assert not board.grants
        c.record_peer_progress(epoch="adapter", sequence=2, committed_half_cycles=100)

    board, c, adapter = setup(quantum=12, wait=wait)
    adapter.attach(board)
    board.start()
    board.finish()
    assert len(calls) == 1 and 0 < calls[0] <= 1.0
    assert len(board.grants) == 1


@pytest.mark.parametrize(
    "action", ["exhaust", "raise", "cancel", "clock", "speed", "counter", "callbacks"]
)
def test_wait_failure_never_executes_or_leaks_permit(action):
    calls = []

    def wait(timeout):
        calls.append(timeout)
        assert not c.snapshot().pending_permit
        if action == "raise":
            raise RuntimeError("callback failed")
        if action == "cancel":
            c.cancel()
        if action == "clock":
            board.cpu.cycles += 1
        if action == "speed":
            board.double_speed = True
        if action == "counter":
            board.instructions += 1
        if action == "callbacks":
            board.execution_after = lambda *args: None

    board, c, adapter = setup(quantum=1, wait=wait)
    adapter.attach(board)
    error = (
        TimeoutError
        if action == "exhaust"
        else RuntimeError
        if action == "raise"
        else EmulatedTimeError
    )
    with pytest.raises(error):
        board.start()
    assert not board.grants
    assert 1 <= len(calls) <= 3
    assert not c.snapshot().pending_permit
    assert c.snapshot().local_half_cycles == 0


def test_detach_closes_epoch_and_prevents_all_reattachment():
    board, c, adapter = setup()
    adapter.attach(board)
    adapter.detach()
    assert board.execution_before is board.execution_after is None
    assert c.snapshot().closed
    with pytest.raises(EmulatedTimeError):
        adapter.attach(board)
    with pytest.raises(EmulatedTimeError):
        fresh = ExecutionGovernorAdapter(
            c,
            instruction_counter=lambda: board.instructions,
            wait_for_progress=lambda t: None,
        )
        fresh.attach(board)


def test_active_pair_detach_rejected_preserves_report():
    board, c, adapter = setup()
    adapter.attach(board)
    before, after = board.execution_before, board.execution_after
    board.start()
    with pytest.raises(EmulatedTimeError):
        adapter.detach()
    assert board.execution_before is before and board.execution_after is after
    assert c.snapshot().pending_permit and not c.snapshot().closed
    board.finish()
    assert c.snapshot().local_half_cycles == 8
    adapter.detach()


@pytest.mark.parametrize("field,value", [(0, 1), (2, 1), (4, "other"), (5, 25)])
def test_wrong_report_metadata_retains_known_actual(field, value):
    board, c, adapter = setup()
    adapter.attach(board)
    board.start()
    board.cpu.cycles = 4
    board.instructions = 1
    report = [0, 4, False, False, "cpu", 24, 0, 0, False]
    report[field] = value
    with pytest.raises(EmulatedTimeError):
        adapter.after(*report)
    assert c.snapshot().closed
    assert c.snapshot().observed_raw_cpu_clock == 4
    assert c.snapshot().raw_cpu_clock == 4
    assert c.snapshot().local_half_cycles == 8


@pytest.mark.parametrize(
    "count,clock,end,transition",
    [
        (2, 2, True, True),
        (1, 5, True, True),
        (1, 2, True, False),
        (0, 0, True, False),
        (True, 2, True, True),
    ],
)
def test_malformed_speed_interval_retains_endpoint_not_guessed_normalization(
    count, clock, end, transition
):
    board, c, adapter = setup()
    adapter.attach(board)
    board.start()
    board.cpu.cycles = 4
    board.instructions = 1
    board.double_speed = end
    with pytest.raises(EmulatedTimeError):
        adapter.after(0, 4, False, end, "cpu", 24, count, clock, transition)
    s = c.snapshot()
    assert s.closed and not s.pending_permit
    assert s.observed_raw_cpu_clock == 4
    assert s.local_half_cycles == 0 and s.raw_cpu_clock == 0


def test_known_overrun_retained_even_inside_conservative_permit():
    board, c, adapter = setup(speed=True)
    adapter.attach(board)
    board.start()
    with pytest.raises(EmulatedTimeError):
        board.finish(25)
    s = c.snapshot()
    assert s.closed and not s.pending_permit
    assert (s.observed_raw_cpu_clock, s.raw_cpu_clock, s.local_half_cycles) == (25, 25, 25)


def test_duplicate_report_does_not_double_charge():
    board, c, adapter = setup()
    adapter.attach(board)
    board.start()
    report = board.finish()
    with pytest.raises(EmulatedTimeError):
        adapter.after(*report)
    assert c.snapshot().local_half_cycles == 8


@pytest.mark.parametrize("counter", [-1, True, 1.5, "1", 2])
def test_malformed_or_overshooting_instruction_count_retains_actual(counter):
    board, c, adapter = setup()
    adapter.attach(board)
    c.begin_episode("0001", cycle_budget=512, instruction_cap=1)
    board.start()
    board.cpu.cycles = 4
    board.instructions = counter
    with pytest.raises(EmulatedTimeError):
        adapter.after(0, 4, False, False, "cpu", 24, 0, 0, False)
    s = c.snapshot()
    assert s.closed and not s.pending_permit
    assert (s.observed_raw_cpu_clock, s.local_half_cycles) == (4, 8)


def test_late_progress_callback_cannot_grant_after_deadline(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("pokered_harness.link.execution_adapter.time.monotonic", lambda: now[0])
    calls = []

    def wait(timeout):
        calls.append(timeout)
        c.record_peer_progress(epoch="adapter", sequence=2, committed_half_cycles=100)
        now[0] = 1.0

    board, c, adapter = setup(quantum=1, wait=wait)
    adapter.attach(board)
    with pytest.raises(TimeoutError):
        board.start()
    assert calls == [1.0]
    assert not board.grants and board.instructions == 0
    assert not c.snapshot().pending_permit


def test_attempt_bound_with_frozen_clock_preserves_debt(monkeypatch):
    monkeypatch.setattr("pokered_harness.link.execution_adapter.time.monotonic", lambda: 0.0)
    waits = []
    board, c, adapter = setup(quantum=1, wait=lambda t: waits.append(t), attempts=3)
    adapter.attach(board)
    c.begin_episode("0001", cycle_budget=24, instruction_cap=1)
    board.start()
    board.finish(24)
    debt = c.snapshot().debt_half_cycles
    assert debt == 46
    with pytest.raises(TimeoutError):
        board.start()
    assert waits == [1.0, 1.0, 1.0]
    assert len(board.grants) == 1 and board.instructions == 1
    assert c.snapshot().debt_half_cycles == debt
    assert not c.snapshot().pending_permit


def test_counter_callback_raises_after_execution_retains_known_actual():
    board, c, _ = setup()

    def counter():
        if board.cpu.cycles:
            raise RuntimeError("counter failed after execution")
        return 0

    adapter = ExecutionGovernorAdapter(
        c, instruction_counter=counter, wait_for_progress=lambda t: None
    )
    adapter.attach(board)
    board.start()
    with pytest.raises(RuntimeError, match="counter failed after execution"):
        board.finish()
    s = c.snapshot()
    assert s.closed and not s.pending_permit
    assert (s.raw_cpu_clock, s.observed_raw_cpu_clock, s.local_half_cycles) == (4, 4, 8)


def test_foreign_registration_is_not_removed_by_detach():
    board, c, adapter = setup()
    adapter.attach(board)
    before, after = lambda *args: 24, lambda *args: None
    board.set_execution_governor(before, after)
    with pytest.raises(EmulatedTimeError):
        adapter.detach()
    assert board.execution_before is before and board.execution_after is after
    assert c.snapshot().closed


def test_setter_refusal_after_report_preserves_adapter_registration():
    board, c, adapter = setup()
    adapter.attach(board)
    before, after = board.execution_before, board.execution_after
    board.start()
    board.cpu.cycles = 4
    board.instructions = 1
    adapter.after(0, 4, False, False, "cpu", 24, 0, 0, False)
    with pytest.raises(RuntimeError, match="active callback pair"):
        adapter.detach()
    assert board.execution_before is before and board.execution_after is after
    assert not c.snapshot().closed and not c.snapshot().pending_permit
    assert c.snapshot().local_half_cycles == 8


def test_report_without_before_fails_without_progress():
    board, c, adapter = setup()
    adapter.attach(board)
    with pytest.raises(EmulatedTimeError):
        adapter.after(0, 4, False, False, "cpu", 24, 0, 0, False)
    assert c.snapshot().closed and c.snapshot().local_half_cycles == 0


@pytest.mark.parametrize("instructions,boundary", [(1, None), (0, 0)])
def test_zero_clock_cannot_discard_executed_instruction_or_speed_change(instructions, boundary):
    board, c, adapter = setup()
    adapter.attach(board)
    board.start()
    with pytest.raises(EmulatedTimeError):
        board.finish(0, instructions, boundary=boundary)
    assert c.snapshot().closed and not c.snapshot().pending_permit
    assert c.snapshot().local_half_cycles == 0


def test_hdma_cannot_report_executed_instruction():
    board, c, adapter = setup()
    adapter.attach(board)
    board.start("hdma", 206)
    with pytest.raises(EmulatedTimeError):
        board.finish(206, 1, "hdma", 206)
    assert c.snapshot().closed
    assert c.snapshot().raw_cpu_clock == 206
    assert c.snapshot().local_half_cycles == 412


def test_after_counter_mutation_retains_latest_observed_endpoint():
    board, c, _ = setup()

    def counter():
        if board.cpu.cycles == 4:
            board.cpu.cycles = 6
        return board.instructions

    adapter = ExecutionGovernorAdapter(
        c, instruction_counter=counter, wait_for_progress=lambda t: None
    )
    adapter.attach(board)
    board.start()
    with pytest.raises(EmulatedTimeError):
        board.finish()
    s = c.snapshot()
    assert s.closed and not s.pending_permit
    assert (s.observed_raw_cpu_clock, s.raw_cpu_clock, s.local_half_cycles) == (6, 6, 12)


def test_invalid_live_speed_retains_raw_without_guessing_normalized_time():
    board, c, adapter = setup()
    adapter.attach(board)
    board.start()
    board.cpu.cycles = 4
    board.instructions = 1
    board.double_speed = 1
    with pytest.raises(EmulatedTimeError):
        adapter.after(0, 4, False, True, "cpu", 24, 0, 0, False)
    s = c.snapshot()
    assert s.closed and not s.pending_permit
    assert (s.observed_raw_cpu_clock, s.raw_cpu_clock, s.local_half_cycles) == (4, 0, 0)


def test_false_report_endpoint_retains_actual_board_clock():
    board, c, adapter = setup()
    adapter.attach(board)
    board.start()
    board.cpu.cycles = 4
    board.instructions = 1
    with pytest.raises(EmulatedTimeError):
        adapter.after(0, 12, False, False, "cpu", 24, 0, 0, False)
    s = c.snapshot()
    assert s.closed and not s.pending_permit
    assert (s.observed_raw_cpu_clock, s.raw_cpu_clock, s.local_half_cycles) == (4, 4, 8)


def test_wait_coordinator_rate_mutation_rejected_before_cpu_grant():
    def wait(timeout):
        assert not c.snapshot().pending_permit
        c.set_speed(raw_cpu_clock=0, double_speed=False)
        c.record_peer_progress(epoch="adapter", sequence=2, committed_half_cycles=100)

    board, c, adapter = setup(speed=True, quantum=1, wait=wait)
    adapter.attach(board)
    with pytest.raises(EmulatedTimeError):
        board.start()
    assert not board.grants and board.instructions == 0
    assert board.double_speed is True
    assert c.snapshot().closed and not c.snapshot().pending_permit
    assert c.snapshot().local_half_cycles == 0
