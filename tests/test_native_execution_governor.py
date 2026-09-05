"""ROM-free tests of the governor's Python source seam, NOT native proof.

Load mb.py explicitly even when installed PyBoy modules are extensions: fake
devices cannot populate typed native Motherboard fields. CPU instruction costs
and STOP events below are controlled inputs, not opcode or hardware validation.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def source_mb():
    path = Path(__file__).resolve().parents[1] / "vendor/pyboy-src/pyboy/core/mb.py"
    spec = importlib.util.spec_from_file_location("pyboy.core._governor_source_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert Path(module.__file__).resolve() == path.resolve()
    return module


class Device:
    def __init__(self, events, name):
        self.events = events
        self.name = name
        self._cycles_to_interrupt = 80
        self._cycles_to_frame = 80
        self.frame_done = False
        self._STAT = SimpleNamespace(_mode=0)
        self.speed_shift = 0
        self.cpu_speed_shift = 0
        self.transfer_active = False
        self.renderer = SimpleNamespace(load_state=lambda *args: None, clear_cache=lambda: None)

    def tick(self, clock):
        self.events.append((self.name, clock))
        if self.name == "lcd":
            self.frame_done = True
        return 0

    def dispatch_owner(self):
        self.events.append(("dispatch",))

    def cycles_to_mode0(self):
        return 80

    def load_state(self, *args):
        pass


class FakeCPU:
    def __init__(self, mb, events):
        self.mb = mb
        self.events = events
        self.cycles = 100
        self.PC = 0x150
        self.halted = False
        self.interrupt_queued = False
        self.interrupt_master_enable = False
        self.interrupts_flag_register = 0
        self.interrupts_enabled_register = 0
        self.actual = 4
        self.action = None

    def tick(self, target):
        self.events.append(("cpu", target))
        if self.action is not None:
            self.action()
        else:
            self.cycles += self.actual
            if not self.halted:
                self.PC += 1

    def set_interruptflag(self, flag):
        self.interrupts_flag_register |= flag

    def load_state(self, *args):
        pass


@pytest.fixture
def board(source_mb, monkeypatch):
    """Exercise real source initialization using only constructor fakes."""
    events = []
    devices = {name: Device(events, name) for name in ("timer", "serial", "lcd", "sound")}
    for name, constructor in (
        ("timer", "Timer"),
        ("serial", "Serial"),
        ("lcd", "LCD"),
        ("sound", "Sound"),
    ):
        device = devices[name]
        monkeypatch.setattr(
            source_mb,
            name,
            SimpleNamespace(**{constructor: lambda *args, _device=device, **kwargs: _device}),
        )
    for name, constructor in (("ram", "RAM"), ("interaction", "Interaction")):
        monkeypatch.setattr(
            source_mb,
            name,
            SimpleNamespace(**{constructor: lambda *args, **kwargs: Device(events, "state")}),
        )
    cart = Device(events, "cartridge")
    cart.cgb = True
    monkeypatch.setattr(source_mb, "cartridge", SimpleNamespace(load_cartridge=lambda *args: cart))
    monkeypatch.setattr(
        source_mb, "bootrom", SimpleNamespace(BootROM=lambda *args: SimpleNamespace(cgb=True))
    )
    monkeypatch.setattr(source_mb, "cpu", SimpleNamespace(CPU=lambda mb: FakeCPU(mb, events)))
    mb = source_mb.Motherboard(None, None, None, None, None, None, 0, False, 0, True)
    mb.hdma = Device(events, "hdma")
    mb.events = events
    return mb


def install(board, grant=None):
    reports = []

    def before(*args):
        board.events.append(("before", *args))
        return args[-1] if grant is None else grant

    def after(*args):
        board.events.append(("after", *args))
        reports.append(args)

    board.set_execution_governor(before, after)
    return reports


def test_default_off_preserves_batch_target_and_transition_defaults(board):
    assert board._execution_governor_enabled is False
    assert board._execution_governor_active is False
    assert (
        board.speed_transition_count,
        board.speed_transition_clock,
        board.speed_transition_double_speed,
    ) == (0, 0, False)
    board.tick()
    assert board.events[0] == ("cpu", 80)
    assert not any(event[0] in ("before", "after") for event in board.events)


@pytest.mark.parametrize(
    "before,after",
    [(None, lambda *a: None), (lambda *a: 24, None), (1, 2), (False, False), (object(), object())],
)
def test_registration_requires_two_callables_or_two_none(board, source_mb, before, after):
    with pytest.raises(source_mb.PyBoyInvalidOperationException):
        board.set_execution_governor(before, after)
    assert not board._execution_governor_enabled
    assert board.cpu.cycles == 100


def test_detach_restores_unbounded_batch(board):
    install(board)
    board.set_execution_governor(None, None)
    assert not board._execution_governor_enabled
    board.tick()
    assert board.events[0] == ("cpu", 80)


@pytest.mark.parametrize(
    "actual,halted",
    [(4, False), (24, False), (20, False), (4, True)],
    ids=["instruction4", "instruction24", "interrupt20", "halt4"],
)
@pytest.mark.parametrize("grant", [24, 1000, 9223372036854775807])
def test_cpu_single_step_and_actual_report_before_dispatch(board, actual, halted, grant):
    board.cpu.actual = actual
    board.cpu.halted = halted
    if actual == 20:
        board.cpu.interrupt_master_enable = True
        board.cpu.interrupts_flag_register = board.cpu.interrupts_enabled_register = 1
    reports = install(board, grant)
    board.tick()
    assert board.events[:3] == [
        ("before", 100, False, "cpu", 24),
        ("cpu", 4),
        ("after", *reports[0]),
    ]
    assert reports[0][:7] == (100, 100 + actual, False, False, "cpu", 24, 0)
    assert board.events[3] == ("dispatch",)
    assert board.key1 == 0


class IntSubclass(int):
    pass


@pytest.mark.parametrize(
    "grant",
    [True, False, None, 24.0, "24", [], object(), IntSubclass(24), -1, 0, 23, 9223372036854775808],
)
def test_invalid_or_undergrant_does_not_execute(board, source_mb, grant):
    reports = install(board, grant)
    # None is an invalid callback result; install's None means default grant.
    if grant is None:
        board.set_execution_governor(lambda *args: None, lambda *args: reports.append(args))
    before = vars(board.cpu).copy()
    with pytest.raises(source_mb.PyBoyInvalidOperationException):
        board.tick()
    assert vars(board.cpu) == before
    assert reports == []
    assert not any(e[0] in ("cpu", "dispatch") for e in board.events)
    assert not board._execution_governor_active


def test_hdma_reserves_and_executes_whole_206_cycles(board):
    board.hdma.transfer_active = True
    board.hdma.tick = lambda mb: mb.events.append(("hdma", mb.cpu.cycles)) or 206
    reports = install(board)
    board.tick()
    assert board.events[:3] == [
        ("before", 100, False, "hdma", 206),
        ("hdma", 100),
        ("after", *reports[0]),
    ]
    assert reports[0][:7] == (100, 306, False, False, "hdma", 206, 0)
    assert board.events[3] == ("dispatch",)


def test_hdma_undergrant_never_calls_transfer(board, source_mb):
    board.hdma.transfer_active = True
    reports = install(board, 205)
    with pytest.raises(source_mb.PyBoyInvalidOperationException):
        board.tick()
    assert board.cpu.cycles == 100
    assert board.events == [("before", 100, False, "hdma", 206)]
    assert reports == []


def test_overrun_reports_actual_and_retains_clock_before_rejecting(board, source_mb):
    board.cpu.actual = 28
    reports = install(board)
    with pytest.raises(source_mb.PyBoyInvalidOperationException):
        board.tick()
    assert reports[0][:7] == (100, 128, False, False, "cpu", 24, 0)
    assert board.cpu.cycles == 128
    assert not any(e[0] == "dispatch" for e in board.events)
    assert not board._execution_governor_active


def test_zero_cycle_breakpoint_still_reports(board):
    board.cpu.actual = 0
    board.breakpoint_singlestep = True
    reports = install(board)
    assert board.tick() is True
    assert reports[0][:7] == (100, 100, False, False, "cpu", 24, 0)
    assert board.events[3] == ("dispatch",)


@pytest.mark.parametrize("old_speed", [False, True])
def test_stop_transition_and_midinstruction_observer(board, old_speed):
    board.double_speed = old_speed
    board.key1 = 0x81 if old_speed else 1
    observations = []

    def stop():
        board.cpu.cycles += 8
        board.switch_speed()
        observations.append(
            (
                board.cpu.cycles,
                board.speed_transition_clock,
                board.speed_transition_double_speed,
                board.speed_transition_count,
            )
        )
        board.cpu.cycles += 4

    board.cpu.action = stop
    reports = install(board)
    board.tick()
    assert observations == [(108, 108, not old_speed, 1)]
    assert reports == [(100, 112, old_speed, not old_speed, "cpu", 24, 1, 108, not old_speed)]
    start, end, speed0, speed1, _, _, _, transition, _ = reports[0]
    elapsed = (transition - start) / (2 if speed0 else 1) + (end - transition) / (
        2 if speed1 else 1
    )
    assert elapsed == (10 if not old_speed else 8)
    assert board.key1 == (0 if old_speed else 0x80)
    assert board.events[0] == ("before", 100, old_speed, "cpu", 24)
    assert board.events.index(("after", *reports[0])) < board.events.index(("dispatch",))


def test_switch_without_key1_request_does_not_record_transition(board):
    board.switch_speed()
    assert (
        board.key1,
        board.double_speed,
        board.speed_transition_count,
        board.speed_transition_clock,
        board.speed_transition_double_speed,
    ) == (0, False, 0, 0, False)
    assert board.events == []


@pytest.mark.parametrize("stage", ["before", "after"])
def test_callback_exception_propagates_and_unlocks_pair(board, stage):
    error = RuntimeError(stage)

    def fail(*args):
        raise error

    board.set_execution_governor(
        fail if stage == "before" else lambda *args: 24,
        fail if stage == "after" else lambda *args: None,
    )
    with pytest.raises(RuntimeError) as caught:
        board.tick()
    assert caught.value is error
    assert board.cpu.cycles == (100 if stage == "before" else 104)
    assert not board._execution_governor_active
    assert not any(e[0] == "dispatch" for e in board.events)
    board.set_execution_governor(None, None)


@pytest.mark.parametrize("stage", ["before", "after"])
@pytest.mark.parametrize("operation", ["tick", "register", "detach", "load"])
def test_active_pair_rejects_reentry(board, source_mb, stage, operation):
    calls = []

    def callback(*args):
        actions = {
            "tick": board.tick,
            "register": lambda: board.set_execution_governor(lambda *a: 24, lambda *a: None),
            "detach": lambda: board.set_execution_governor(None, None),
            "load": lambda: board.load_state(None),
        }
        with pytest.raises(source_mb.PyBoyInvalidOperationException):
            actions[operation]()
        calls.append(stage)
        return 24

    board.set_execution_governor(
        callback if stage == "before" else lambda *args: 24,
        callback if stage == "after" else lambda *args: None,
    )
    board.tick()
    assert calls == [stage]
    assert board.cpu.cycles == 104
    assert not board._execution_governor_active


def test_execution_exception_reports_best_effort_and_preserves_original(board):
    original = RuntimeError("CPU execution failed")
    reports = []

    def execute():
        board.cpu.cycles += 4
        raise original

    def after(*args):
        reports.append(args)
        raise ValueError("report failed")

    board.cpu.action = execute
    board.set_execution_governor(lambda *args: 24, after)
    with pytest.raises(RuntimeError) as caught:
        board.tick()
    assert caught.value is original
    assert reports[0][:7] == (100, 104, False, False, "cpu", 24, 0)
    assert board.cpu.cycles == 104
    assert not board._execution_governor_active
    assert not any(e[0] == "dispatch" for e in board.events)


def test_configured_load_rejected_before_stream_read_or_state_mutation(board, source_mb):
    install(board)
    state = (board.cpu.cycles, board.key1, board.double_speed, board.speed_transition_count)
    reads = []
    stream = SimpleNamespace(read=lambda: reads.append(True))
    with pytest.raises(source_mb.PyBoyInvalidOperationException):
        board.load_state(stream)
    assert reads == []
    assert (board.cpu.cycles, board.key1, board.double_speed, board.speed_transition_count) == state
    board.set_execution_governor(None, None)
    values = iter([source_mb.STATE_VERSION, 1, 0, 0, 0, 1, 0])
    stream = SimpleNamespace(read=lambda: next(values), flush=lambda: None)
    board.load_state(stream)
    assert not board._execution_governor_enabled


@pytest.mark.parametrize("raw", [-1, 9223372036854775807 - 23, 9223372036854775807])
def test_unsafe_raw_boundary_rejects_before_reservation(board, source_mb, raw):
    board.cpu.cycles = raw
    reports = install(board)
    with pytest.raises(source_mb.PyBoyInvalidOperationException):
        board.tick()
    assert board.cpu.cycles == raw
    assert board.events == []
    assert reports == []


def test_last_safe_raw_boundary_reports_boxed_difference(board):
    board.cpu.cycles = 9223372036854775807 - 24
    board.cpu.actual = 24
    reports = install(board)
    board.tick()
    assert reports[0][1] == 9223372036854775807
    assert reports[0][1] - reports[0][0] == 24


@pytest.mark.parametrize(
    "case",
    [
        "count_wrap",
        "two_transitions",
        "speed_without_count",
        "transition_before",
        "transition_after",
        "transition_speed_mismatch",
        "zero_with_transition",
    ],
)
def test_inconsistent_actual_metadata_reports_then_rejects(board, source_mb, case):
    """Count wrap is injected; Python integers do not prove native uint64 behavior."""
    if case == "count_wrap":
        board.speed_transition_count = (1 << 64) - 1

    def execute():
        board.cpu.cycles += 4
        if case == "count_wrap":
            board.speed_transition_count = 0
        elif case == "two_transitions":
            board.key1 = 1
            board.switch_speed()
            board.key1 |= 1
            board.switch_speed()
        elif case == "speed_without_count":
            board.double_speed = True
        else:
            board.key1 = 1
            board.switch_speed()
            if case == "transition_before":
                board.speed_transition_clock = 99
            elif case == "transition_after":
                board.speed_transition_clock = 105
            elif case == "transition_speed_mismatch":
                board.speed_transition_double_speed = False
            elif case == "zero_with_transition":
                board.cpu.cycles = 100
                board.speed_transition_clock = 100

    board.cpu.action = execute
    reports = install(board)
    with pytest.raises(source_mb.PyBoyInvalidOperationException):
        board.tick()
    assert len(reports) == 1
    assert reports[0][1] == board.cpu.cycles
    if case == "count_wrap":
        assert reports[0][6] == -((1 << 64) - 1)
    assert not any(e[0] == "dispatch" for e in board.events)
    assert not board._execution_governor_active


@pytest.mark.parametrize("stage", ["before", "after"])
def test_callback_clock_mutation_rejected_without_dispatch(board, source_mb, stage):
    reports = []

    def mutate(*args):
        board.cpu.cycles += 1
        reports.append(args)
        return 24

    board.set_execution_governor(
        mutate if stage == "before" else lambda *a: 24,
        mutate if stage == "after" else lambda *a: None,
    )
    with pytest.raises(source_mb.PyBoyInvalidOperationException):
        board.tick()
    assert len(reports) == 1
    assert board.cpu.cycles == (101 if stage == "before" else 105)
    assert not any(e[0] == "dispatch" for e in board.events)
    assert any(e[0] == "cpu" for e in board.events) == (stage == "after")


def test_disabled_direct_step_rejected(board, source_mb):
    with pytest.raises(source_mb.PyBoyInvalidOperationException):
        board._execution_step()
    assert board.cpu.cycles == 100
    assert board.events == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("double_speed", True),
        ("speed_transition_count", 1),
        ("speed_transition_clock", 101),
        ("speed_transition_double_speed", True),
        ("execution_before", lambda *a: 24),
        ("execution_after", lambda *a: None),
        ("_execution_governor_enabled", False),
    ],
)
def test_after_source_field_mutation_rejected_before_dispatch(board, source_mb, field, value):
    """Direct field writes inject invalid source state, not native readonly writes."""
    reports = []

    def after(*args):
        reports.append(args)
        setattr(board, field, value)

    board.set_execution_governor(lambda *a: 24, after)
    with pytest.raises(source_mb.PyBoyInvalidOperationException):
        board.tick()
    assert reports[0][:7] == (100, 104, False, False, "cpu", 24, 0)
    assert not any(e[0] == "dispatch" for e in board.events)
    assert not board._execution_governor_active
