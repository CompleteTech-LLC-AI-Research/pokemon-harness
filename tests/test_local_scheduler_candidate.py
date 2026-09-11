"""Adversarial instruction-owner tests, without ROM assets."""
import threading
from types import SimpleNamespace

import pytest

from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
from pokered_harness.link.serial_core import SerialCore


class Endpoint:
    def __init__(self, name, trace, *, origin=0, cycles=4, instructions=4, cgb=False):
        self.name, self.trace = name, trace
        self.cycles, self.instructions = cycles, instructions
        self.memory = {0xFF4D: 0}
        self.events = []
        self.frame_count = 0
        self.setup = self.finalize = self.rendered = self.hooks = 0
        self.on_instruction = None
        self.progress = 0
        serial = SerialCore(cgb)
        serial.clock = origin
        self.mb = SimpleNamespace(
            serial=serial, cgb_mode=cgb,
            lcd=SimpleNamespace(frame_done=False, disable_renderer=True),
            sound=SimpleNamespace(disable_sampling=False, clear_buffer=lambda: None),
            breakpoint_singlestep=7, breakpoint_singlestep_latch=0,
            tick=self.instruction, breakpoint_reinject=lambda: None,
            breakpoint_reached=lambda: (-1, -1, -1),
        )

    def instruction(self):
        self.trace.append((self.name, self.mb.serial.clock))
        self.mb.serial.clock += self.cycles
        self.progress += 1
        if self.progress >= self.instructions:
            self.mb.lcd.frame_done = True
        if self.on_instruction:
            self.on_instruction()
        return True

    def _handle_events(self, events):
        self.setup += 1
        self.progress = 0

    def _post_handle_events(self):
        self.finalize += 1

    def _handle_hooks(self):
        self.hooks += 1

    def tick(self, frames, *args):
        assert frames == 0, "paired owner must never whole-frame tick independently"
        self.rendered += 1


def pair(**kwargs):
    trace = []
    a = Endpoint("a", trace, **kwargs)
    b = Endpoint("b", trace, **kwargs)
    link = PyBoyLinkSession.local()
    link.attach(a)
    link.attach(b)
    # The production quantum is one normal LCD frame in physical units;
    # these tiny instruction fakes use a reduced deterministic horizon.
    link.PHYSICAL_QUANTUM = 16
    return link, a, b, trace


def test_persistent_origins_and_both_public_methods():
    trace = []
    a = Endpoint("a", trace, origin=1000, cycles=12)
    b = Endpoint("b", trace, origin=10, cycles=4)
    link = PyBoyLinkSession.local()
    link.attach(a)
    link.attach(b)
    origins = link._epoch_origins
    link.step()
    assert [name for name, _ in trace[:4]] == ["a", "b", "b", "b"]
    link.step_interleaved(2, chunk_cycles=10000, render=True)
    assert link._epoch_origins == origins == (1000, 10)
    assert a.frame_count == b.frame_count == 3
    assert a.setup == a.finalize == b.setup == b.finalize == 3
    assert a.rendered == b.rendered == 2
    assert a.mb.breakpoint_singlestep == b.mb.breakpoint_singlestep == 7


@pytest.mark.parametrize("change", ["clock", "serial", "motherboard"])
def test_boundary_identity_and_external_advance_fault_requires_detach(change):
    link, a, _b, _ = pair()
    if change == "clock":
        a.mb.serial.clock += 4
    elif change == "serial":
        a.mb.serial = SerialCore()
    else:
        a.mb = SimpleNamespace(**vars(a.mb))
    with pytest.raises(RuntimeError, match="fault"):
        link.step()
    with pytest.raises(RuntimeError, match="detach/recover"):
        link.step()
    link.detach_all()
    assert link._epoch_origins is None


@pytest.mark.parametrize("key1", [1, 128, 129])
def test_cgb_without_physical_clock_at_second_attach_rolls_back(key1):
    trace = []
    a, b = Endpoint("a", trace, cgb=True), Endpoint("b", trace, cgb=True)
    link = PyBoyLinkSession.local()
    link.attach(a)
    prior = a.mb.serial.backend
    b.memory[0xFF4D] = key1
    with pytest.raises(RuntimeError, match="physical clock support"):
        link.attach(b)
    assert link.attached == (a,) and not link.paired
    assert a.mb.serial.backend is prior


def test_key1_is_not_used_as_runtime_physical_time():
    # An old CGB runtime without the clock API is rejected even when KEY1
    # advertises normal speed; writable KEY1 cannot establish elapsed time.
    with pytest.raises(RuntimeError, match="physical clock support"):
        pair(cgb=True)


def test_reported_physical_clock_regression_detected_after_single_instruction():
    link, a, b, trace = pair()
    a.mb.get_physical_clock = lambda: (0, 0 if a.progress else 10)
    b.mb.get_physical_clock = lambda: (0, b.mb.serial.clock * 2)
    link.detach_all()
    link.attach(a)
    link.attach(b)
    with pytest.raises(RuntimeError, match="physical clock moved backwards"):
        link.step()
    assert len(trace) == 1
    assert a.frame_count == b.frame_count == 0
    assert a.mb.breakpoint_singlestep == b.mb.breakpoint_singlestep == 7


def test_epoch_check_reads_pair_in_stable_order(monkeypatch):
    link, _a, _b, _ = pair()
    calls = []

    def read_physical_clock(endpoint):
        calls.append(endpoint.name)
        return (0, endpoint.mb.serial.clock * 2)

    monkeypatch.setattr(link, "_read_physical_clock", read_physical_clock)

    assert link._check_epoch() == (0, 0)
    assert calls == ["a", "b"]
    assert link._physical_now == (0, 0)


def test_completed_frame_peer_rearm_is_line_idle_without_next_frame_tick():
    link, a, b, trace = pair(instructions=1)
    progress = link._make_owned_peer_progressor(a)
    results = []
    b.on_instruction = lambda: results.append(progress())
    link.step()
    # LCD markers no longer stop the endpoint at one instruction.  Both
    # sides reach the reduced common physical horizon (16 units) before the
    # public boundary, and the peer callback remains bounded at that frontier.
    assert results == [False, False]
    assert trace == [("a", 0), ("b", 0), ("a", 4), ("b", 4)]
    assert a.progress == b.progress == 2
    assert a.frame_count == b.frame_count == 1
    assert a.mb.lcd.frame_done is True
    assert b.mb.lcd.frame_done is True
    # The next public quantum continues both endpoints from the new physical
    # frontier; it does not re-run the first quantum's work.
    link.step()
    assert results == [False, False, False, False]
    assert trace == [
        ("a", 0), ("b", 0), ("a", 4), ("b", 4),
        ("a", 8), ("b", 8), ("a", 12), ("b", 12),
    ]
    assert a.frame_count == b.frame_count == 2
    assert a.setup == a.finalize == b.setup == b.finalize == 2


def test_completed_frame_peer_rearm_without_scheduler_owner_still_faults():
    link, a, _b, _ = pair(instructions=1)
    progress = link._make_owned_peer_progressor(a)
    a.mb.lcd.frame_done = True
    assert progress() is False
    with pytest.raises(RuntimeError, match="unowned or recursive peer progress"):
        link.step()


def test_completed_frame_peer_edge_is_pullup_without_peer_tick():
    link, a, b, _ = pair(instructions=1)
    a.mb.serial.set_SB(0x00)
    a.mb.serial.set_SC(0x81)
    b.mb.serial.set_SC(0x00)
    b.mb.lcd.frame_done = True
    calls = []
    original_tick = b.mb.tick
    def forbidden_tick():
        calls.append(None)
        return original_tick()
    b.mb.tick = forbidden_tick
    link._step_active = True
    try:
        reply = a.mb.serial.backend.on_edge(0, 1)
    finally:
        link._step_active = False
    assert reply == 1
    assert calls == []
    assert link._scheduler_fault is None


def test_rearm_progress_accounted_and_both_flags_reread():
    link, a, b, trace = pair(instructions=3)
    progress = link._make_owned_peer_progressor(b)
    called = []
    def advance_peer_once():
        if not called:
            called.append(progress())
    a.on_instruction = advance_peer_once
    link.step()
    assert called == [True]
    assert a.progress == b.progress == 2
    assert a.mb.serial.clock == b.mb.serial.clock == 8
    assert len(trace) == 4


def test_nonadvancing_clock_fault_and_instruction_budget():
    link, _a, _b, _ = pair(cycles=0, instructions=1000)
    link.MAX_STALLED_INSTRUCTIONS = 3
    with pytest.raises(RuntimeError, match="no clock progress"):
        link.step()
    link.detach_all()
    link, _a, _b, _ = pair(instructions=1000)
    link.MAX_FRAME_INSTRUCTIONS = 3
    with pytest.raises(RuntimeError, match="instruction budget"):
        link.step()


def test_owned_operation_hook_and_detach_recovery():
    link, a, b, _ = pair()
    hook = a.mb.serial.backend.validate_session_operation
    for operation in ("step", "load_state", "run_until_event"):
        with pytest.raises(RuntimeError) as error:
            hook(operation)
        assert error.value.code == "paired_session_operation"
    for operation in ("press", "save_state", "read", "close"):
        hook(operation)
    link.detach_all()
    assert not hasattr(a.mb.serial.backend, "validate_session_operation")
    link.attach(a)
    link.attach(b)
    link.step()


def test_peer_exception_is_deferred_until_native_tick_returns():
    link, a, b, _ = pair()
    progress = link._make_owned_peer_progressor(b)
    b.mb.tick = lambda: (_ for _ in ()).throw(ValueError("boom"))
    returned = []
    a.on_instruction = lambda: returned.append(progress())
    with pytest.raises(RuntimeError, match="peer progress failed"):
        link.step()
    assert returned == [False]


def test_callback_outside_owner_faults_without_advancing_peer():
    link, _a, b, trace = pair()
    assert link._make_owned_peer_progressor(b)() is False
    assert trace == []
    with pytest.raises(RuntimeError, match="unowned"):
        link.step()


def test_backwards_clock_faults_before_next_instruction():
    link, a, _b, trace = pair(origin=100)
    a.on_instruction = lambda: setattr(a.mb.serial, "clock", 99)
    with pytest.raises(RuntimeError, match="backwards"):
        link.step()
    assert len(trace) == 1


def test_wall_budget_fault_restores_stepping(monkeypatch):
    link, a, b, _ = pair()
    values = iter((0, 100))
    monkeypatch.setattr("pokered_harness.link.pyboy_link_session.time.monotonic", lambda: next(values))
    with pytest.raises(RuntimeError, match="wall deadline"):
        link.step()
    assert a.mb.breakpoint_singlestep == b.mb.breakpoint_singlestep == 7


@pytest.mark.parametrize("value,error_type", [(0, ValueError), (-1, ValueError), (True, TypeError), (1.5, TypeError)])
def test_invalid_frame_count(value, error_type):
    link, *_ = pair()
    with pytest.raises(error_type):
        link.step(value)


def test_public_boundary_validation_is_inside_operation_lock():
    link, a, b, _ = pair()
    entered = threading.Event()
    release = threading.Event()
    attempted = threading.Event()
    errors = []
    original = link._check_epoch
    first = True
    def check(*, boundary=False):
        nonlocal first
        if boundary and first:
            first = False
            entered.set()
            assert release.wait(3)
        return original(boundary=boundary)
    link._check_epoch = check
    def run(second=False):
        try:
            if second:
                attempted.set()
            link.step()
        except BaseException as error:  # noqa: BLE001 - capture thread failure
            errors.append(error)
    t1 = threading.Thread(target=run)
    t2 = threading.Thread(target=run, args=(True,))
    t1.start()
    try:
        assert entered.wait(3)
        assert not link._operation_lock.acquire(blocking=False)
        t2.start()
        assert attempted.wait(3)
        assert a.frame_count == b.frame_count == 0
    finally:
        release.set()
        t1.join(3)
        if t2.ident is not None:
            t2.join(3)
    assert not t1.is_alive() and not t2.is_alive()
    assert errors == []
    assert a.frame_count == b.frame_count == 2


def test_recursive_public_step_is_rejected_without_deadlock():
    link, a, _b, _ = pair()
    a.on_instruction = lambda: link.step()
    with pytest.raises(RuntimeError, match="already executing"):
        link.step()
    link.detach_all()
    assert not link.paired
