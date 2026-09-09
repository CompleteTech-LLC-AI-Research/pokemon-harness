"""Owner-thread execution contracts for the network serial backend."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from pyboy.core.serial import Serial, SerialBackendError

from pokered_harness.link import network_backend as network_module
from pokered_harness.link.network_backend import (
    _FRAME,
    _OP_EDGE_REQ,
    NetworkBackend,
    NetworkBackendError,
)
from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
from pokered_harness.link.serial_coordinator import SerialOperationGate

pytestmark = [pytest.mark.unit, pytest.mark.timing_sensitive]


class _Core:
    transfer_enabled = 1
    internal_clock = 0
    SB = 0
    SC = 0x80

    def __init__(self, *, complete: bool = False) -> None:
        self.complete = complete
        self.calls = 0
        self.call_threads: list[int] = []
        self.applied_bits: list[int] = []

    def peek_out_bit(self) -> int:
        self.call_threads.append(threading.get_ident())
        return 0

    def apply_external_edge(self, bit: int) -> bool:
        self.call_threads.append(threading.get_ident())
        self.applied_bits.append(bit & 1)
        self.calls += 1
        return self.complete


def _start_owner_pair(core, *, irq=None):
    master, slave = NetworkBackend.pair()
    master.start_receiver(local_core=None)
    slave.start_receiver(
        local_core=core,
        irq_callback=irq,
        serial_gate=SerialOperationGate(),
        dispatch_to_owner=True,
    )
    return master, slave


def _send_and_service(master, slave, *, service_thread=None):
    result: list[object] = []

    def send() -> None:
        try:
            result.append(master.on_edge(1, 1))
        except BaseException as exc:  # noqa: BLE001 - asserted by caller
            result.append(exc)

    sender = threading.Thread(target=send, daemon=True)
    sender.start()
    deadline = time.monotonic() + 1.0
    while slave.debug_snapshot()["pending_edge_requests"] == 0:
        assert time.monotonic() < deadline, "owner dispatch did not receive EDGE_REQ"
        time.sleep(0.001)
    if service_thread is None:
        assert slave.service_pending_edges(max_edges=1) == 1
    else:
        service_thread.start()
        service_thread.join(timeout=1.0)
        assert not service_thread.is_alive()
    sender.join(timeout=1.0)
    assert not sender.is_alive()
    return result


def test_incoming_edge_and_completion_irq_run_on_owner_thread() -> None:
    core = Serial(False)
    core.set_SB(0x3C)
    core.set_SC(0x80)
    irq_threads: list[int] = []
    master, slave = _start_owner_pair(core, irq=lambda: irq_threads.append(threading.get_ident()))
    owner_thread = threading.get_ident()
    try:
        expected = [0, 0, 1, 1, 1, 1, 0, 0]
        for _ in range(8):
            result = _send_and_service(master, slave)
            assert result == [expected[_]]
        assert core.SB == 0xFF
        assert core._bits_remaining == 0
        assert irq_threads == [owner_thread]
        assert core.backend_failed is False
        assert slave.debug_snapshot()["owner_edge_applied"] == 8
    finally:
        master.stop(timeout_s=1.0)
        slave.stop(timeout_s=1.0)


def test_without_owner_service_no_native_mutation_and_bounded_expiry(monkeypatch) -> None:
    monkeypatch.setattr(network_module, "_EDGE_RESPONSE_TIMEOUT_SECONDS", 0.08)
    core = Serial(False)
    core.set_SB(0x3C)
    core.set_SC(0x80)
    master, slave = _start_owner_pair(core)
    try:
        with pytest.raises(NetworkBackendError, match="within|closed|timed out"):
            master.on_edge(1, 1)
        assert core._bits_remaining == 8
        assert core._shift_register == 0x3C
        assert slave.debug_snapshot()["owner_edge_applied"] == 0
        assert master.connected is False
    finally:
        master.stop(timeout_s=1.0)
        slave.stop(timeout_s=1.0)


def test_owner_dispatch_can_move_between_owner_threads() -> None:
    core = _Core()
    master, slave = _start_owner_pair(core)
    service_threads: list[int] = []
    try:
        for _ in range(2):
            service_started = threading.Event()
            service_error: list[BaseException] = []

            def service(service_started=service_started, service_error=service_error) -> None:
                service_threads.append(threading.get_ident())
                service_started.set()
                try:
                    assert slave.service_pending_edges(max_edges=1) == 1
                except BaseException as exc:  # noqa: BLE001
                    service_error.append(exc)

            dispatcher = threading.Thread(target=service, daemon=True)
            # Ensure the request is present before the dispatcher is run, then
            # execute the dispatch itself on the explicitly chosen thread.
            result = _send_and_service(master, slave, service_thread=dispatcher)
            assert result == [0]
            assert service_error == []
            assert service_started.is_set()
        assert len(service_threads) == 2
        # Python may recycle a short-lived thread's identifier immediately;
        # the core observations below are the ownership invariant.
        assert set(core.call_threads) == set(service_threads)
    finally:
        master.stop(timeout_s=1.0)
        slave.stop(timeout_s=1.0)


def test_owner_dispatch_reentrant_empty_service_does_not_duplicate_edge() -> None:
    callback_calls: list[int] = []
    holder: dict[str, NetworkBackend] = {}

    class ReentrantCore(_Core):
        def apply_external_edge(self, bit: int) -> bool:
            result = super().apply_external_edge(bit)
            callback_calls.append(holder["slave"].service_pending_edges())
            return result

    core = ReentrantCore()
    master, slave = _start_owner_pair(core)
    holder["slave"] = slave
    try:
        assert _send_and_service(master, slave) == [0]
        assert core.calls == 1
        assert callback_calls == [0]
        assert slave.debug_snapshot()["owner_edge_applied"] == 1
    finally:
        master.stop(timeout_s=1.0)
        slave.stop(timeout_s=1.0)


def test_owner_dispatch_internal_clock_uses_keepalive_without_native_mutation() -> None:
    core = _Core()
    core.transfer_enabled = 0
    core.internal_clock = 1
    master, slave = _start_owner_pair(core)
    try:
        assert _send_and_service(master, slave) == [1]
        assert core.calls == 0
        snapshot = slave.debug_snapshot()
        assert snapshot["keepalive_bits_sent"] == 1
        assert snapshot["owner_edge_applied"] == 1
    finally:
        master.stop(timeout_s=1.0)
        slave.stop(timeout_s=1.0)


@pytest.mark.parametrize("error_type", [ValueError, KeyboardInterrupt])
def test_owner_dispatch_irq_failure_closes_transport_without_replaying_native_edge(error_type) -> None:
    """An owner callback failure is terminal and the edge is applied once.

    ``Serial`` itself latches this failure when it is the callback owner.  A
    lightweight core double has no such native latch, so this test asserts
    only the backend boundary: no response is emitted and no replay occurs.
    """
    core = _Core(complete=True)
    cause = error_type("completion IRQ failed")
    master, slave = _start_owner_pair(core, irq=lambda: (_ for _ in ()).throw(cause))
    result: list[object] = []

    def send() -> None:
        try:
            result.append(master.on_edge(1, 1))
        except BaseException as exc:  # noqa: BLE001
            result.append(exc)

    sender = threading.Thread(target=send, daemon=True)
    sender.start()
    try:
        deadline = time.monotonic() + 1.0
        while slave.debug_snapshot()["pending_edge_requests"] == 0:
            assert time.monotonic() < deadline
            time.sleep(0.001)
        with pytest.raises(NetworkBackendError) as owner_error:
            slave.service_pending_edges(max_edges=1)
        assert owner_error.value.__cause__ is cause
        sender.join(timeout=1.0)
        assert not sender.is_alive()
        assert len(result) == 1 and isinstance(result[0], NetworkBackendError)
        assert core.calls == 1
        assert slave.connected is False
        assert slave.debug_snapshot()["edge_resp_sent"] == 0
        assert slave.service_pending_edges(max_edges=1) == 0
        assert core.calls == 1
    finally:
        sender.join(timeout=1.0)
        master.stop(timeout_s=1.0)
        slave.stop(timeout_s=1.0)


def test_public_network_step_surfaces_owner_irq_failure() -> None:
    """A native owner callback fault must not make a later tick look successful."""

    class _Motherboard:
        def __init__(self) -> None:
            self.serial = Serial(False)
            self.cpu = SimpleNamespace(set_interruptflag=lambda _flag: None)

    class _Endpoint:
        def __init__(self) -> None:
            self.mb = _Motherboard()

        def tick(self, count=1, render=False, sound=False):
            del render, sound
            for _ in range(count):
                self.mb.serial.tick(self.mb.serial.clock + 512)
            return True

    master, slave = NetworkBackend.pair()
    master.start_receiver(local_core=None)
    endpoint = _Endpoint()
    endpoint.mb.serial.set_SB(0x3C)
    endpoint.mb.serial.set_SC(0x80)
    link = PyBoyLinkSession(network_backend=slave)
    try:
        link.attach(endpoint)
        for index in range(8):
            result: list[object] = []

            def send(result=result) -> None:
                try:
                    result.append(master.on_edge(1, 1))
                except BaseException as exc:  # noqa: BLE001
                    result.append(exc)

            sender = threading.Thread(target=send, daemon=True)
            sender.start()
            deadline = time.monotonic() + 1.0
            while slave.debug_snapshot()["pending_edge_requests"] == 0:
                assert time.monotonic() < deadline
                time.sleep(0.001)
            if index == 7:
                slave._irq_callback = lambda: (_ for _ in ()).throw(ValueError("owner IRQ"))
            if index < 7:
                link.step(1)
                sender.join(timeout=1.0)
                assert result and not isinstance(result[0], BaseException)
            else:
                with pytest.raises((NetworkBackendError, SerialBackendError)):
                    link.step(1)
                sender.join(timeout=1.0)
                assert endpoint.mb.serial.backend_failed
                with pytest.raises(SerialBackendError):
                    endpoint.mb.serial.check_error()
            assert not sender.is_alive()
    finally:
        link.detach_all()
        master.stop(timeout_s=1.0)
        slave.stop(timeout_s=1.0)


@pytest.mark.parametrize("deferrals", [1, 2, 3])
def test_malformed_multiple_pending_edges_preserve_fifo_after_rearm(deferrals) -> None:
    """Document FIFO ordering if a peer violates one-edge-at-a-time admission."""
    class ArmedCore(_Core):
        transfer_enabled = 0

    core = ArmedCore()
    master, slave = _start_owner_pair(core)
    try:
        master._sock.setblocking(True)
        master._sock.sendall(_FRAME.pack(_OP_EDGE_REQ, 1) + _FRAME.pack(_OP_EDGE_REQ, 0))
        deadline = time.monotonic() + 1.0
        while slave.debug_snapshot()["pending_edge_requests"] < 2:
            assert time.monotonic() < deadline
            time.sleep(0.001)
        for _ in range(deferrals):
            assert slave.service_pending_edges(max_edges=1) == 0
        assert core.applied_bits == []
        core.transfer_enabled = 1
        assert slave.service_pending_edges(max_edges=1) == 1
        assert core.applied_bits == [1]
    finally:
        master.stop(timeout_s=1.0)
        slave.stop(timeout_s=1.0)
