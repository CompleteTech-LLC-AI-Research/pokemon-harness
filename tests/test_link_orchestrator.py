"""Smoke tests for LockstepOrchestrator.

Uses :class:`InProcessSerialLink` + ``FakePyBoy`` so we don't need a
real ROM to verify the lockstep-stepping mechanics (worker threads,
go/done barrier, press injection).
"""

from __future__ import annotations

import threading

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
