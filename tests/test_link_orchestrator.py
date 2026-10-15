"""Smoke tests for LockstepOrchestrator.

Uses :class:`InProcessSerialLink` + ``FakePyBoy`` so we don't need a
real ROM to verify the lockstep-stepping mechanics (worker threads,
go/done barrier, press injection).
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from pokered_harness.events import EventBus
from pokered_harness.link.remote import RemoteLinkEndpoint
from pokered_harness.link.serial_link import InProcessSerialLink
from pokered_harness.session import Session
from pokered_harness.symbols.loader import load_sym_text
from tests import _link_integration_remote_support as remote_support
from tests._link_integration_remote_support import _SessionRunner
from tests._link_orchestrator import LockstepOrchestrator
from tests.conftest import DictMemory
from tests.fakes import FakePyBoy

_BLUE_SYM = """\
00:22fa Serial_TryEstablishingExternallyClockedConnection
00:216f Serial_ExchangeBytes
00:22c3 Serial_ExchangeNybble
00:2247 Serial_ExchangeLinkMenuSelection
00:ffaa hSerialConnectionStatus
00:ffac hSerialSendData
00:ffad hSerialReceiveData
00:cc42 wLinkMenuSelectionSendBuffer
00:cc3d wLinkMenuSelectionReceiveBuffer
00:cc42 wSerialExchangeNybbleSendData
00:cc3e wSerialExchangeNybbleReceiveData
00:d141 wSerialRandomNumberListBlock
00:d173 wSerialPlayerDataBlock
"""


def _make_session():
    pb = FakePyBoy(DictMemory())
    sym = load_sym_text(_BLUE_SYM)
    return Session(pyboy=pb, symbols=sym, event_bus=EventBus()), pb


def test_orchestrator_steps_both_sessions_in_lockstep():
    sa, pa = _make_session()
    sb, pb = _make_session()
    la, lb = InProcessSerialLink.pair("blue", "blue")
    ea = RemoteLinkEndpoint.as_listener(sa, la)
    eb = RemoteLinkEndpoint.as_connector(sb, lb)
    ea.install()
    eb.install()

    ork = LockstepOrchestrator(sa, ea, sb, eb)
    ork.start()
    try:
        ork.step(10)
    finally:
        ork.stop()
    # Each FakePyBoy records its tick calls. With chunk=1 per-frame
    # serial_tick pattern, 10 frames = 10 entries.
    assert len(pa.tick_calls) == 10, pa.tick_calls
    assert len(pb.tick_calls) == 10, pb.tick_calls


def test_orchestrator_press_both_lands_on_same_frame():
    sa, pa = _make_session()
    sb, pb = _make_session()
    la, lb = InProcessSerialLink.pair("blue", "blue")
    ea = RemoteLinkEndpoint.as_listener(sa, la)
    eb = RemoteLinkEndpoint.as_connector(sb, lb)
    ea.install()
    eb.install()

    ork = LockstepOrchestrator(sa, ea, sb, eb)
    ork.start()
    try:
        ork.step(3)
        ork.press_both("a", duration=6)
        ork.step(1)  # press applies at top of this chunk
        ork.step(5)
    finally:
        ork.stop()
    # Both PyBoys saw the "a" press during the 4th frame (after step(3)).
    # FakePyBoy's button_calls record all presses in order.
    assert pa.button_calls == [("a", 6)]
    assert pb.button_calls == [("a", 6)]


def test_orchestrator_press_a_and_press_b_independently():
    sa, pa = _make_session()
    sb, pb = _make_session()
    la, lb = InProcessSerialLink.pair("blue", "blue")
    ea = RemoteLinkEndpoint.as_listener(sa, la)
    eb = RemoteLinkEndpoint.as_connector(sb, lb)
    ea.install()
    eb.install()

    ork = LockstepOrchestrator(sa, ea, sb, eb)
    ork.start()
    try:
        ork.press_a("left", duration=6)
        ork.press_b("right", duration=6)
        ork.step(1)
    finally:
        ork.stop()
    assert pa.button_calls == [("left", 6)]
    assert pb.button_calls == [("right", 6)]


def test_orchestrator_surfaces_worker_exception():
    sa, pa = _make_session()
    sb, _pb = _make_session()
    la, lb = InProcessSerialLink.pair("blue", "blue")
    ea = RemoteLinkEndpoint.as_listener(sa, la)
    eb = RemoteLinkEndpoint.as_connector(sb, lb)
    ea.install()
    eb.install()

    ork = LockstepOrchestrator(sa, ea, sb, eb)
    ork.start()
    try:
        # Sabotage side A's session to make tick raise
        def boom(*_a, **_k):
            raise RuntimeError("sabotage")

        pa.tick = boom  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="sabotage"):
            ork.step(1)
    finally:
        ork.stop()


def test_orchestrator_synchronous_nybble_exchange():
    """Two concurrent workers + hook firing on both sides during the
    same step() call exchange a byte through the serial link — proving
    the lockstep barrier doesn't deadlock during a blocking RPC."""
    sa, pa = _make_session()
    sb, pbb = _make_session()
    la, lb = InProcessSerialLink.pair("blue", "blue")
    ea = RemoteLinkEndpoint.as_listener(sa, la)
    eb = RemoteLinkEndpoint.as_connector(sb, lb)
    ea.install()
    eb.install()

    # Arrange distinguishable byte in each side's nybble send buffer.
    send_addr = sa.symbols.addr_of("wSerialExchangeNybbleSendData")
    recv_addr = sa.symbols.addr_of("wSerialExchangeNybbleReceiveData")
    pa.memory[send_addr] = 0x61
    pbb.memory[send_addr] = 0x67

    ork = LockstepOrchestrator(sa, ea, sb, eb)
    ork.start()
    try:
        # Fire the nybble-exchange hook on both sides simultaneously.
        # Both workers execute during the same step() call; the hooks
        # block on link.exchange and pair up via the InProcessSerialLink
        # queue.
        def _fire_both():
            t = threading.Thread(target=lambda: pa.fire(0x00, 0x22C3), daemon=True)
            t.start()
            pbb.fire(0x00, 0x22C3)
            t.join(timeout=2.0)

        # FakePyBoy.fire is synchronous and runs the callback right
        # there. We don't need an orchestrator step for this — the
        # hooks fire immediately. But verify ork is usable around it.
        ork.step(1)
        _fire_both()
        ork.step(1)
    finally:
        ork.stop()
    assert pa.memory[recv_addr] == 0x67
    assert pbb.memory[recv_addr] == 0x61


class _FakeRemoteSession:
    def __init__(self) -> None:
        self.tick = 0
        self.button_calls: list[tuple[str, int]] = []
        self.button_ticks: list[int] = []
        self.press_threads: list[int | None] = []

    def current_tick(self) -> int:
        return self.tick

    def press(self, button: str, *, duration: int) -> None:
        self.button_calls.append((button, duration))
        self.button_ticks.append(self.tick)
        self.press_threads.append(threading.get_ident())


class _FakeRemoteEndpoint:
    clock_status_byte = 1

    def __init__(
        self,
        session: _FakeRemoteSession,
        *,
        block_first_step: bool = False,
        error: Exception | None = None,
    ) -> None:
        self.session = session
        self.block_first_step = block_first_step
        self.error = error
        self.entered_step = threading.Event()
        self.release_step = threading.Event()

    def step(self, chunk: int) -> None:
        if self.block_first_step:
            self.block_first_step = False
            self.entered_step.set()
            if not self.release_step.wait(timeout=5.0):
                raise TimeoutError("fake blocked step was not released")
        if self.error is not None:
            raise self.error
        self.session.tick += chunk


class _FakeTradeSession(_FakeRemoteSession):
    def __init__(self) -> None:
        super().__init__()
        self.overworld = SimpleNamespace(map_id=0x40, x=3, y=4)
        self.map_observed = threading.Event()

    def read_game_state(self):
        if self.overworld.map_id == 0xEF:
            self.map_observed.set()
        return SimpleNamespace(overworld=self.overworld)


class _FakeTradeEndpoint(_FakeRemoteEndpoint):
    def __init__(
        self,
        session: _FakeTradeSession,
        rng_done: threading.Event,
        *,
        block_first_step: bool = False,
        pause_second_step: bool = False,
    ) -> None:
        super().__init__(session, block_first_step=block_first_step)
        self.rng_done = rng_done
        self.pause_second_step = pause_second_step
        self.second_step_entered = threading.Event()
        self.release_second_step = threading.Event()
        self.step_count = 0
        self.schedule = None
        self.matched_return = False

    def step(self, chunk: int) -> None:
        self.step_count += 1
        super().step(chunk)
        if self.session.tick >= 8:
            self.session.overworld.map_id = 0xEF
        if self.schedule is not None and self.schedule.trade_tick is not None:
            self.matched_return = True
            self.rng_done.set()
        if self.pause_second_step and self.step_count == 2:
            self.second_step_entered.set()
            if not self.release_second_step.wait(timeout=2.0):
                raise TimeoutError("test did not release the second fake step")


def _fake_trade_schedule(session, stop_inputs, input_lock, rng_done, role="listener"):
    return remote_support._RemoteTradeInputSchedule(
        session, role, stop_inputs, input_lock, rng_done
    )


def test_remote_trade_schedule_uses_phase_cadence_and_rng_cap():
    session = _FakeTradeSession()
    stop_inputs = threading.Event()
    input_lock = threading.Lock()
    rng_done = {"listener": threading.Event(), "connector": threading.Event()}
    schedule = _fake_trade_schedule(session, stop_inputs, input_lock, rng_done)

    schedule.before_step()
    session.tick = 59
    schedule.before_step()
    assert session.button_calls == []

    session.tick = 60
    schedule.before_step()
    assert session.button_calls == [("a", 4)]
    assert session.button_ticks == [60]
    session.tick = 67
    schedule.before_step()
    assert len(session.button_calls) == 1

    session.overworld.map_id = 0xEF
    session.tick = 68
    schedule.before_step()
    assert schedule.trade_tick == 68
    session.tick = 307
    schedule.before_step()
    assert schedule.movement_cycles == 0

    for tick, x in ((308, 3), (332, 6), (356, 3), (380, 6)):
        session.tick = tick
        session.overworld.x = x
        schedule.before_step()
    assert schedule.movement_cycles == 4
    assert session.button_calls[1:5] == [
        ("right", 8),
        ("left", 8),
        ("right", 8),
        ("left", 8),
    ]
    assert session.button_ticks[1:5] == [308, 332, 356, 380]

    session.tick = 403
    schedule.before_step()
    assert schedule.rng_presses == 0
    for press_index in range(200):
        session.tick = 404 + press_index * 12
        schedule.before_step()
    assert schedule.rng_presses == 200
    assert session.button_ticks[5:] == [404 + index * 12 for index in range(200)]

    session.tick = 2804
    schedule.before_step()
    assert schedule.failure == "matching RNG return not reached within 200 A presses"
    assert stop_inputs.is_set()
    assert schedule.rng_presses == 200


def test_remote_trade_schedule_caps_menu_attempts_at_120():
    session = _FakeTradeSession()
    stop_inputs = threading.Event()
    input_lock = threading.Lock()
    rng_done = {"listener": threading.Event(), "connector": threading.Event()}
    schedule = _fake_trade_schedule(session, stop_inputs, input_lock, rng_done)

    schedule.before_step()
    for press_index in range(120):
        session.tick = 60 + press_index * 8
        schedule.before_step()

    assert schedule.menu_presses == 120
    assert session.button_calls == [("a", 4)] * 120
    assert session.button_ticks == [60 + index * 8 for index in range(120)]

    session.tick = 1020
    schedule.before_step()
    assert schedule.failure == "Trade Center not reached within 120 A presses"
    assert stop_inputs.is_set()
    assert schedule.menu_presses == 120


def test_remote_trade_driver_progresses_peer_while_one_runner_is_blocked():
    blocked_session = _FakeTradeSession()
    peer_session = _FakeTradeSession()
    rng_done = {"listener": threading.Event(), "connector": threading.Event()}
    blocked_endpoint = _FakeTradeEndpoint(
        blocked_session, rng_done["listener"], block_first_step=True
    )
    peer_endpoint = _FakeTradeEndpoint(peer_session, rng_done["connector"], pause_second_step=True)
    runner_a = _SessionRunner(blocked_session, blocked_endpoint)
    runner_b = _SessionRunner(peer_session, peer_endpoint)
    stop_inputs = threading.Event()
    input_lock = threading.Lock()
    schedule_a = _fake_trade_schedule(
        blocked_session, stop_inputs, input_lock, rng_done, "listener"
    )
    schedule_b = _fake_trade_schedule(peer_session, stop_inputs, input_lock, rng_done, "connector")
    blocked_endpoint.schedule = schedule_a
    peer_endpoint.schedule = schedule_b
    driver_errors: list[Exception] = []

    def drive() -> None:
        try:
            remote_support._drive_remote_trade_to_rng(
                (runner_a, runner_b),
                (schedule_a, schedule_b),
                rng_done,
                stop_inputs,
                input_lock,
                no_progress_timeout_s=2.0,
                outer_timeout_s=5.0,
            )
        except Exception as exc:  # noqa: BLE001 - surface coordinator failure below
            driver_errors.append(exc)

    driver = threading.Thread(target=drive)
    driver.start()
    try:
        assert blocked_endpoint.entered_step.wait(timeout=1.0)
        assert peer_endpoint.second_step_entered.wait(timeout=1.0)
        assert not blocked_endpoint.release_step.is_set()

        blocked_endpoint.release_step.set()
        peer_endpoint.release_second_step.set()
        assert blocked_session.map_observed.wait(timeout=1.0)
        assert peer_session.map_observed.wait(timeout=1.0)
        driver.join(timeout=2.0)
        assert not driver.is_alive(), "trade coordinator did not finish"
        assert driver_errors == []
        assert schedule_a.trade_tick is not None
        assert schedule_b.trade_tick is not None
        assert all(event.is_set() for event in rng_done.values())
        assert blocked_endpoint.matched_return
        assert peer_endpoint.matched_return
    finally:
        blocked_endpoint.release_step.set()
        peer_endpoint.release_second_step.set()
        with input_lock:
            stop_inputs.set()
            for event in rng_done.values():
                event.set()
        driver.join(timeout=2.0)
        for runner in (runner_a, runner_b):
            if runner._thread.ident is not None:
                runner.stop(timeout=1.0)
    assert not driver.is_alive(), "trade coordinator thread leaked"
    assert not runner_a._thread.is_alive()
    assert not runner_b._thread.is_alive()


def test_remote_trade_schedule_skips_press_after_synchronized_cancellation():
    session = _FakeTradeSession()
    stop_inputs = threading.Event()
    input_lock = threading.Lock()
    rng_done = {"listener": threading.Event(), "connector": threading.Event()}
    schedule = _fake_trade_schedule(session, stop_inputs, input_lock, rng_done)
    schedule.before_step()
    session.tick = 60
    read_state = threading.Event()

    def read_game_state():
        read_state.set()
        return SimpleNamespace(overworld=session.overworld)

    session.read_game_state = read_game_state
    thread_errors: list[Exception] = []

    def run_before_step() -> None:
        try:
            schedule.before_step()
        except Exception as exc:  # noqa: BLE001 - assert owner callback failures below
            thread_errors.append(exc)

    with input_lock:
        owner = threading.Thread(target=run_before_step)
        owner.start()
        assert read_state.wait(timeout=1.0)
        stop_inputs.set()
    owner.join(timeout=1.0)

    assert not owner.is_alive(), "owner callback did not finish"
    assert thread_errors == []
    assert session.button_calls == []
    assert schedule.menu_presses == 0


def test_remote_menu_driver_progresses_peer_while_one_runner_is_blocked(monkeypatch):
    monkeypatch.setattr(remote_support, "_REMOTE_RUNNER_WINDOW_TIMEOUT", 2.0)
    blocked_session = _FakeRemoteSession()
    active_session = _FakeRemoteSession()
    blocked_endpoint = _FakeRemoteEndpoint(blocked_session, block_first_step=True)
    active_endpoint = _FakeRemoteEndpoint(active_session)
    blocked_runner = _SessionRunner(blocked_session, blocked_endpoint)
    active_runner = _SessionRunner(active_session, active_endpoint)
    started: list[_SessionRunner] = []
    try:
        blocked_runner.start()
        started.append(blocked_runner)
        assert blocked_endpoint.entered_step.wait(timeout=1.0)
        active_runner.start()
        started.append(active_runner)
        remote_support._drive_remote_to_link_menu(
            blocked_runner,
            active_runner,
            lambda: ("a", 4) in active_session.button_calls,
            attempts=5,
        )
        assert active_session.button_calls == [("up", 6)] * 3 + [("a", 4)]
        assert [
            b - a
            for a, b in zip(
                active_session.button_ticks,
                active_session.button_ticks[1:],
                strict=False,
            )
        ] == [20] * 3
        assert active_session.press_threads == [active_runner._thread.ident] * 4
        assert blocked_session.button_calls == []
        assert blocked_runner._thread.is_alive()
    finally:
        blocked_endpoint.release_step.set()
        for runner in started:
            runner.stop(timeout=1.0)
    assert blocked_session.button_calls == []


def test_remote_menu_schedule_skips_press_after_synchronized_cancellation():
    session = _FakeRemoteSession()
    stop_inputs = threading.Event()
    input_lock = threading.Lock()
    ready_checked = threading.Event()
    release_ready = threading.Event()
    thread_errors: list[Exception] = []

    def ready() -> bool:
        ready_checked.set()
        if not release_ready.wait(timeout=2.0):
            raise TimeoutError("test did not release the readiness check")
        return False

    schedule = remote_support._RemoteInputSchedule(
        session, ready, stop_inputs, input_lock, attempts=1
    )
    schedule.before_step()
    session.tick = 180

    def run_before_step() -> None:
        try:
            schedule.before_step()
        except Exception as exc:  # noqa: BLE001 - assert owner failures on the test thread
            thread_errors.append(exc)

    owner = threading.Thread(target=run_before_step)
    owner.start()
    try:
        assert ready_checked.wait(timeout=2.0)
        with input_lock:
            stop_inputs.set()
    finally:
        release_ready.set()
        owner.join(timeout=2.0)

    assert not owner.is_alive(), "owner callback did not finish"
    assert thread_errors == []
    assert session.button_calls == []
    assert schedule.done.is_set()


def test_remote_menu_driver_keeps_each_side_action_budget_bounded():
    session_a = _FakeRemoteSession()
    session_b = _FakeRemoteSession()
    runner_a = _SessionRunner(session_a, _FakeRemoteEndpoint(session_a))
    runner_b = _SessionRunner(session_b, _FakeRemoteEndpoint(session_b))
    started: list[_SessionRunner] = []
    try:
        runner_a.start()
        started.append(runner_a)
        runner_b.start()
        started.append(runner_b)
        remote_support._drive_remote_to_link_menu(runner_a, runner_b, lambda: False, attempts=2)
    finally:
        for runner in started:
            runner.stop(timeout=1.0)
    expected = [("up", 6)] * 3 + [("a", 4)] * 2
    assert session_a.button_calls == expected
    assert session_b.button_calls == expected
    assert [
        b - a for a, b in zip(session_a.button_ticks, session_a.button_ticks[1:], strict=False)
    ] == [20] * 4
    assert [
        b - a for a, b in zip(session_b.button_ticks, session_b.button_ticks[1:], strict=False)
    ] == [20] * 4
    assert session_a.press_threads == [runner_a._thread.ident] * len(expected)
    assert session_b.press_threads == [runner_b._thread.ident] * len(expected)


def test_remote_menu_driver_surfaces_owner_thread_error():
    session_a = _FakeRemoteSession()
    session_b = _FakeRemoteSession()
    runner_a = _SessionRunner(session_a, _FakeRemoteEndpoint(session_a))
    runner_b = _SessionRunner(
        session_b,
        _FakeRemoteEndpoint(session_b, error=RuntimeError("owner step failed")),
    )
    started: list[_SessionRunner] = []
    try:
        runner_a.start()
        started.append(runner_a)
        runner_b.start()
        started.append(runner_b)
        with pytest.raises(RuntimeError, match="owner step failed"):
            remote_support._drive_remote_to_link_menu(runner_a, runner_b, lambda: False, attempts=2)
    finally:
        for runner in started:
            runner.stop(timeout=1.0)
