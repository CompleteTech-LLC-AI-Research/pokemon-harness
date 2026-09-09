"""Public ``NetworkBackend`` backpressure and cancellation checks.

The existing deadline tests exercise ``_send_frame`` directly.  These tests
keep the public ``on_edge`` operation in the loop instead: one proves a
positive partial write still completes a real EDGE_REQ/EDGE_RESP exchange,
and the others bound the public master and response-worker paths when a
socket remains unwritable.
"""

from __future__ import annotations

import struct
import threading
import time

import pytest

from pokered_harness.link import network_backend as module
from pokered_harness.link.network_backend import NetworkBackend, NetworkBackendError
from pokered_harness.link.serial_coordinator import SerialOperationGate

pytestmark = pytest.mark.timing_sensitive


_EDGE_REQ = 0x10
_EDGE_RESP = 0x11


class _SocketProxy:
    def __init__(self, sock):
        self.sock = sock

    def __getattr__(self, name):
        return getattr(self.sock, name)


class _PartialPositiveSocket(_SocketProxy):
    """Make the first public EDGE_REQ write partial, then transiently busy."""

    def __init__(self, sock):
        super().__init__(sock)
        self.calls = 0

    def send(self, data):
        self.calls += 1
        if self.calls == 1:
            return self.sock.send(data[:1])
        if self.calls == 2:
            raise BlockingIOError()
        return self.sock.send(data)


class _BudgetedEdgeSocket(_SocketProxy):
    """Spend two write-poll intervals before one successful public send."""

    def __init__(self, sock, attempted: threading.Event, completed_at: list[float]):
        super().__init__(sock)
        self.attempted = attempted
        self.completed_at = completed_at
        self.calls = 0

    def send(self, data):
        self.calls += 1
        if self.calls <= 2:
            self.attempted.set()
            raise BlockingIOError()
        sent = self.sock.send(data)
        if sent:
            self.completed_at.append(time.monotonic())
        return sent


class _ResponseGateSocket(_SocketProxy):
    """Apply a deterministic EAGAIN gate only to an EDGE_RESP frame."""

    def __init__(
        self,
        sock,
        attempted: threading.Event,
        release: threading.Event,
    ):
        super().__init__(sock)
        self.attempted = attempted
        self.release = release

    def send(self, data):
        if data and bytes(data)[0] == _EDGE_RESP and not self.release.is_set():
            self.attempted.set()
            raise BlockingIOError()
        return self.sock.send(data)


class _CompletingSlave:
    """Minimal native-Serial-shaped core for the compatibility worker."""

    transfer_enabled = 1
    internal_clock = 0
    SB = 0
    SC = 0x80

    def peek_out_bit(self) -> int:
        return 0

    def apply_external_edge(self, peer_bit: int) -> bool:
        self.SB = peer_bit & 1
        self.SC = 0
        self.transfer_enabled = 0
        return True


def _recv_exact(sock, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise AssertionError("peer closed before complete EDGE_REQ")
        data.extend(chunk)
    return bytes(data)


def _join(thread: threading.Thread, *, timeout: float = 1.0) -> None:
    thread.join(timeout=timeout)
    assert not thread.is_alive(), f"thread survived bounded test cleanup: {thread.name}"


def _stop(*backends: NetworkBackend) -> None:
    for backend in backends:
        backend.stop(timeout_s=1.0)
    for backend in backends:
        for worker in (backend._reader, backend._edge_worker):
            if isinstance(worker, threading.Thread):
                _join(worker)


def _wait_for_pending(backend: NetworkBackend, *, timeout: float = 1.0) -> None:
    """Wait for the reader's edge admission notification without polling."""
    deadline = time.monotonic() + timeout
    with backend._edge_pending_condition:
        while backend._edge_pending == 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("reader did not admit an EDGE_REQ")
            backend._edge_pending_condition.wait(timeout=remaining)


def _install_response_gate_select(monkeypatch, writer: _ResponseGateSocket) -> None:
    real_select = module.select.select

    def gated_select(readable, writable, exceptional, timeout):
        if any(sock is writer for sock in writable) and not writer.release.is_set():
            # A real EAGAIN would make select wait for writability. The event
            # gives stop() an immediate wake-up while retaining the same
            # bounded poll/deadline behavior.
            writer.release.wait(timeout=timeout)
            if writer.release.is_set():
                return [], writable, []
            return [], [], []
        return real_select(readable, writable, exceptional, timeout)

    monkeypatch.setattr(module.select, "select", gated_select)


def test_public_on_edge_completes_after_positive_partial_write():
    """A real public EDGE_REQ survives a positive partial write and EAGAIN."""
    master, peer = NetworkBackend.pair()
    peer._sock.setblocking(True)
    writer = _PartialPositiveSocket(master._sock)
    master._sock = writer
    peer_frame: list[bytes] = []
    peer_errors: list[BaseException] = []
    peer_done = threading.Event()

    def peer_round_trip() -> None:
        try:
            frame = _recv_exact(peer._sock, 2)
            peer_frame.append(frame)
            opcode, payload = struct.unpack(">BB", frame)
            assert opcode == _EDGE_REQ
            peer._sock.sendall(bytes((_EDGE_RESP, (~payload) & 1)))
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            peer_errors.append(exc)
        finally:
            peer_done.set()

    peer_thread = threading.Thread(target=peer_round_trip, name="public-edge-peer")
    master.start_receiver(local_core=None)
    peer_thread.start()
    try:
        assert master.on_edge(our_bit=1, our_role=1) == 0
        assert peer_done.wait(timeout=1.0)
        assert peer_errors == []
        assert peer_frame == [bytes((_EDGE_REQ, 1))]
        assert writer.calls >= 3
        assert master.debug_snapshot()["edge_req_sent"] == 1
        assert master.debug_snapshot()["edge_resp_received"] == 1
    finally:
        _stop(master, peer)
        _join(peer_thread)


def test_public_on_edge_backpressure_uses_one_bounded_send_response_budget(
    monkeypatch,
):
    """A gated public send leaves only the remaining budget for its response."""
    master, peer = NetworkBackend.pair()
    master.start_receiver(local_core=None)
    peer._sock.setblocking(True)
    attempted = threading.Event()
    send_completed_at: list[float] = []
    writer = _BudgetedEdgeSocket(master._sock, attempted, send_completed_at)
    master._sock = writer
    select_started = threading.Event()
    request_received = threading.Event()
    gate_calls = 0
    done_at: list[float] = []
    result: list[BaseException | int] = []
    peer_observer_errors: list[BaseException] = []
    real_select = module.select.select

    def gated_select(readable, writable, exceptional, timeout):
        nonlocal gate_calls
        if any(sock is writer for sock in writable):
            gate_calls += 1
            select_started.set()
            # Let two normal poll intervals spend part of the public on_edge
            # deadline, then report writability so the request can complete.
            threading.Event().wait(timeout)
            if gate_calls >= 2:
                return [], writable, []
            return [], [], []
        return real_select(readable, writable, exceptional, timeout)

    def peer_observer() -> None:
        try:
            _recv_exact(peer._sock, 2)
            request_received.set()
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            peer_observer_errors.append(exc)

    def invoke() -> None:
        try:
            result.append(master.on_edge(our_bit=1, our_role=1))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            result.append(exc)
        finally:
            done_at.append(time.monotonic())

    monkeypatch.setattr(module, "_EDGE_RESPONSE_TIMEOUT_SECONDS", 0.16)
    monkeypatch.setattr(module.select, "select", gated_select)
    peer_thread = threading.Thread(target=peer_observer, name="public-edge-observer")
    worker = threading.Thread(target=invoke, name="public-edge-request")
    started = time.monotonic()
    peer_thread.start()
    worker.start()
    try:
        assert attempted.wait(timeout=1.0)
        assert select_started.wait(timeout=1.0)
        assert request_received.wait(timeout=1.0)
        assert peer_observer_errors == []
        # The positive send occurs after two bounded poll intervals.
        assert writer.calls >= 3
        assert worker.join(timeout=1.0) is None
        assert not worker.is_alive()
        assert result and isinstance(result[0], NetworkBackendError)
        assert done_at and send_completed_at
        assert done_at[0] - send_completed_at[0] < 0.13
        assert done_at[0] - started < 0.45
        assert not master.connected
    finally:
        _stop(master, peer)
        _join(worker)
        _join(peer_thread)


def _start_gated_response_pair():
    master, slave = NetworkBackend.pair()
    master.start_receiver(local_core=None)
    core = _CompletingSlave()
    response_attempted = threading.Event()
    release_response = threading.Event()
    writer = _ResponseGateSocket(slave._sock, response_attempted, release_response)
    slave._sock = writer
    irq_fired = threading.Event()
    slave.start_receiver(
        local_core=core,
        irq_callback=irq_fired.set,
        serial_gate=SerialOperationGate(),
        dispatch_to_owner=True,
    )
    return master, slave, core, response_attempted, release_response, irq_fired, writer


def test_public_edge_response_worker_backpressure_fails_closed_on_deadline(
    monkeypatch,
):
    """A gated public EDGE_RESP worker reports its own bounded deadline."""
    (
        master,
        slave,
        core,
        response_attempted,
        release_response,
        irq_fired,
        _writer,
    ) = _start_gated_response_pair()
    result: list[BaseException | int] = []
    done = threading.Event()

    _install_response_gate_select(monkeypatch, _writer)

    def invoke() -> None:
        try:
            result.append(master.on_edge(our_bit=1, our_role=1))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            result.append(exc)
        finally:
            done.set()

    # The master captures this long budget before the owner dispatches the
    # request. The shorter value below belongs only to the response worker.
    monkeypatch.setattr(module, "_EDGE_RESPONSE_TIMEOUT_SECONDS", 0.60)
    sender = threading.Thread(target=invoke, name="public-edge-response-sender")
    started = time.monotonic()
    sender.start()
    try:
        _wait_for_pending(slave)
        monkeypatch.setattr(module, "_EDGE_RESPONSE_TIMEOUT_SECONDS", 0.16)
        assert slave.service_pending_edges() == 1
        assert irq_fired.wait(timeout=1.0)
        assert response_attempted.wait(timeout=1.0)
        assert slave._closed_event.wait(timeout=1.0)
        assert isinstance(slave._reader_exc, NetworkBackendError)
        assert "EDGE_RESP send timed out" in str(slave._reader_exc)
        assert done.wait(timeout=1.0)
        assert result and isinstance(result[0], NetworkBackendError)
        assert core.transfer_enabled == 0
        assert slave.debug_snapshot()["edge_resp_sent"] == 0
        assert time.monotonic() - started < 0.7
        assert not master.connected
        assert not slave.connected
    finally:
        release_response.set()
        _stop(master, slave)
        _join(sender)


def test_public_on_edge_stop_wakes_waiter_while_response_worker_is_gated(monkeypatch):
    """Stopping the public master wakes its waiter without waiting 10 seconds."""
    (
        master,
        slave,
        _core,
        response_attempted,
        release_response,
        _irq_fired,
        writer,
    ) = _start_gated_response_pair()
    result: list[BaseException | int] = []
    done = threading.Event()

    def invoke() -> None:
        try:
            result.append(master.on_edge(our_bit=1, our_role=1))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            result.append(exc)
        finally:
            done.set()

    _install_response_gate_select(monkeypatch, writer)
    sender = threading.Thread(target=invoke, name="public-edge-stop-sender")
    sender.start()
    try:
        _wait_for_pending(slave)
        assert slave.service_pending_edges() == 1
        assert response_attempted.wait(timeout=1.0)
        started = time.monotonic()
        assert master.stop(timeout_s=0.3) is True
        assert done.wait(timeout=0.4)
        assert time.monotonic() - started < 0.4
        assert result and isinstance(result[0], NetworkBackendError)
    finally:
        release_response.set()
        _stop(master, slave)
        _join(sender)
