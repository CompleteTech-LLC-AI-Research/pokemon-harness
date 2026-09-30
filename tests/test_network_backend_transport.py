"""Transport lifecycle, handshake, frame I/O and on-edge error paths (#128).

Split from ``tests/test_network_backend.py`` for #128 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module. ``_OP_*`` constants come from the shared support module.
"""

from __future__ import annotations

import socket as _socket
import struct
import threading
import time
from types import SimpleNamespace

import pytest

from pokered_harness.link import network_backend as network_module
from pokered_harness.link.network_backend import (
    NetworkBackend,
    NetworkBackendError,
    validate_loopback_host,
)
from pokered_harness.link.serial_coordinator import SerialOperationGate
from tests._network_backend_support import (
    _OP_EDGE_REQ,
    _OP_EDGE_RESP,
    _OP_EXCHANGE,
    _OP_SYNC,
)


def test_pair_constructs_without_raising():
    a, b = NetworkBackend.pair()
    try:
        assert a is not b
    finally:
        a.stop()
        b.stop()


def test_close_is_idempotent():
    a, b = NetworkBackend.pair()
    a.close()
    a.close()  # no error
    b.close()


def test_network_backend_rejects_non_loopback_hosts():
    """The unauthenticated wire protocol cannot be exposed remotely."""
    assert validate_loopback_host("127.0.0.1") == "127.0.0.1"
    assert validate_loopback_host("localhost") == "localhost"
    assert validate_loopback_host("[::1]") == "::1"
    with pytest.raises(ValueError, match="localhost-only"):
        validate_loopback_host("0.0.0.0")
    with pytest.raises(ValueError, match="localhost-only"):
        NetworkBackend.connect("192.0.2.1", 1)


def test_network_backend_rejects_unbounded_accept():
    with pytest.raises(ValueError, match="finite and positive"):
        NetworkBackend.listen(0, accept_timeout_s=None)


def test_localhost_resolution_must_remain_loopback(monkeypatch):
    def unsafe_resolution(*_args, **_kwargs):
        return [
            (
                _socket.AF_INET,
                _socket.SOCK_STREAM,
                0,
                "",
                ("192.0.2.1", 0),
            )
        ]

    monkeypatch.setattr(_socket, "getaddrinfo", unsafe_resolution)
    with pytest.raises(ValueError, match="resolved unsafely"):
        validate_loopback_host("localhost")


def test_versioned_handshake_reports_each_peer_rom():
    a_sock, b_sock = _socket.socketpair()
    a = NetworkBackend(a_sock, local_rom_version="red")
    b = NetworkBackend(b_sock, local_rom_version="blue")
    a.start_receiver(local_core=None)
    b.start_receiver(local_core=None)
    try:
        assert a.wait_for_hello(timeout=1.0) == "blue"
        assert b.wait_for_hello(timeout=1.0) == "red"
    finally:
        a.stop()
        b.stop()


def test_versioned_handshake_rejects_unexpected_peer_rom():
    a_sock, b_sock = _socket.socketpair()
    a = NetworkBackend(a_sock, local_rom_version="red")
    b = NetworkBackend(b_sock, local_rom_version="blue")
    a.start_receiver(local_core=None)
    b.start_receiver(local_core=None)
    try:
        with pytest.raises(NetworkBackendError, match="does not match"):
            a.wait_for_hello(
                timeout=1.0,
                expected_peer_rom_version="yellow",
            )
        assert not a.connected
    finally:
        a.stop()
        b.stop()


def test_versioned_handshake_can_be_cancelled_and_closes_transport():
    """A cancelled HELLO wait cannot leave an unauthenticated link alive."""
    a_sock, b_sock = _socket.socketpair()
    a = NetworkBackend(a_sock, local_rom_version="red")
    b = NetworkBackend(b_sock)
    a.start_receiver(local_core=None)
    cancel = threading.Event()
    cancel.set()
    try:
        with pytest.raises(NetworkBackendError, match="cancelled"):
            a.wait_for_hello(timeout=5.0, cancel_event=cancel)
        assert not a.connected
    finally:
        a.stop()
        b.stop()


def test_frame_send_deadline_does_not_block_on_silent_peer():
    a_sock, b_sock = _socket.socketpair()
    a_sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_SNDBUF, 4096)
    backend = NetworkBackend(a_sock)
    try:
        started = time.monotonic()
        with pytest.raises(NetworkBackendError, match="send timed out"):
            backend._send_frame(
                b"x" * (1024 * 1024),
                timeout=0.05,
                operation="TEST",
            )
        assert time.monotonic() - started < 0.5
    finally:
        backend.stop()
        b_sock.close()


@pytest.mark.parametrize("public_operation", [False, True], ids=["frame-helper", "edge"])
def test_zero_byte_send_fails_without_retry_or_poll(monkeypatch, public_operation):
    a, b = NetworkBackend.pair()
    calls = []

    class ClosedWriter:
        def send(self, data):
            calls.append(bytes(data))
            return 0

        def __getattr__(self, name):
            return getattr(real_socket, name)

    def forbidden_poll(*_args):
        pytest.fail("zero-byte send must not poll a closed stream")

    real_socket = a._sock
    try:
        with monkeypatch.context() as patch:
            patch.setattr(a, "_sock", ClosedWriter())
            patch.setattr(network_module.select, "select", forbidden_poll)
            with pytest.raises(NetworkBackendError, match="socket closed while sending"):
                if public_operation:
                    a.on_edge(our_bit=1, our_role=1)
                else:
                    a._send_frame(b"test", timeout=1.0, operation="TEST")
            assert len(calls) == 1
            if public_operation:
                assert not a.connected
                assert a._closed_event.is_set()
    finally:
        a.stop()
        b.stop()


@pytest.mark.parametrize("interrupt_poll", [False, True])
def test_would_block_send_polls_then_resumes_partial_write(monkeypatch, interrupt_poll):
    a, b = NetworkBackend.pair()
    calls = []
    polls = []

    class BackpressuredWriter:
        def send(self, data):
            calls.append(bytes(data))
            if len(calls) == 1:
                raise BlockingIOError()
            return 1 if len(calls) == 2 else len(data)

    def ready_to_write(readable, writable, exceptional, timeout):
        polls.append(timeout)
        if interrupt_poll and len(polls) == 1:
            raise InterruptedError()
        return [], writable, []

    try:
        with monkeypatch.context() as patch:
            patch.setattr(a, "_sock", BackpressuredWriter())
            patch.setattr(network_module.select, "select", ready_to_write)
            a._send_frame(b"abc", timeout=1.0, operation="TEST")
            assert calls == [b"abc", b"abc", b"bc"]
            assert len(polls) == 1
            assert 0 < polls[0] <= network_module._SEND_POLL_SECONDS
            assert a.connected
    finally:
        a.stop()
        b.stop()


def test_interrupted_write_poll_does_not_reset_send_deadline(monkeypatch):
    a, b = NetworkBackend.pair()
    now = [100.0]
    sends = []

    class BackpressuredWriter:
        def send(self, data):
            sends.append(bytes(data))
            raise BlockingIOError()

    def interrupted_poll(*_args):
        now[0] += 1.0
        raise InterruptedError()

    try:
        with monkeypatch.context() as patch:
            patch.setattr(a, "_sock", BackpressuredWriter())
            patch.setattr(network_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
            patch.setattr(network_module.select, "select", interrupted_poll)
            with pytest.raises(NetworkBackendError, match="send timed out after 1s"):
                a._send_frame(b"test", timeout=1.0, operation="TEST")
            assert sends == [b"test"]
    finally:
        a.stop()
        b.stop()


@pytest.mark.parametrize("expire_deadline", [False, True], ids=["retry", "deadline"])
def test_interrupted_read_poll_preserves_partial_frame_and_deadline(monkeypatch, expire_deadline):
    a, b = NetworkBackend.pair()
    now = [100.0]
    calls = []
    original_select = network_module.select.select
    b._sock.send(b"a")

    def interrupted_poll(readable, writable, exceptional, timeout):
        calls.append(timeout)
        if len(calls) == 2:
            if expire_deadline:
                now[0] += 1.0
            else:
                b._sock.send(b"bc")
            raise InterruptedError()
        return original_select(readable, writable, exceptional, timeout)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(network_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
            patch.setattr(network_module, "_FRAME_READ_TIMEOUT_SECONDS", 1.0)
            patch.setattr(network_module.select, "select", interrupted_poll)
            if expire_deadline:
                with pytest.raises(NetworkBackendError, match="peer frame timed out"):
                    a._recv_exactly(3)
                assert len(calls) == 2
            else:
                assert a._recv_exactly(3) == b"abc"
                assert len(calls) == 3
    finally:
        a.stop()
        b.stop()


def test_stop_wakes_blocked_edge_waiter():
    a, b = NetworkBackend.pair()
    a.start_receiver(local_core=None)
    result: list[Exception] = []

    def blocked_edge() -> None:
        try:
            a.on_edge(our_bit=1, our_role=1)
        except Exception as exc:  # noqa: BLE001
            result.append(exc)

    worker = threading.Thread(target=blocked_edge, daemon=True)
    worker.start()
    time.sleep(0.05)
    started = time.monotonic()
    a.stop()
    worker.join(timeout=1.0)
    elapsed = time.monotonic() - started
    try:
        assert not worker.is_alive()
        assert elapsed < 0.5
        assert result and isinstance(result[0], NetworkBackendError)
    finally:
        b.stop()


def test_stop_applies_one_total_deadline_to_workers():
    a, b = NetworkBackend.pair()
    started = threading.Event()
    release = threading.Event()

    class StubbornCore:
        transfer_enabled = 1
        internal_clock = 0
        SB = 0
        SC = 0x80

        def peek_out_bit(self) -> int:
            return 0

        def apply_external_edge(self, _peer_bit: int) -> bool:
            started.set()
            release.wait(timeout=2.0)
            return False

    a.start_receiver(local_core=None)
    b.start_receiver(local_core=StubbornCore())
    edge_done = threading.Event()

    def send_edge() -> None:
        try:
            a.on_edge(our_bit=1, our_role=1)
        except NetworkBackendError:
            pass
        finally:
            edge_done.set()

    sender = threading.Thread(target=send_edge, daemon=True)
    sender.start()
    assert started.wait(timeout=1.0)

    started_stop = time.monotonic()
    stopped = b.stop(timeout_s=0.05)
    elapsed = time.monotonic() - started_stop
    assert stopped is False
    assert elapsed < 0.5

    release.set()
    sender.join(timeout=1.0)
    assert edge_done.wait(timeout=1.0)
    # A bounded stop may return False while user-owned emulator work is
    # still running, but callers must be able to retry and complete teardown
    # after that work releases.  Keep this lifecycle guarantee explicit so a
    # daemon worker cannot silently survive the test.
    assert b.stop(timeout_s=1.0) is True
    assert b._edge_worker is not None and not b._edge_worker.is_alive()
    a.stop()


def test_on_edge_sends_REQ_and_waits_for_RESP():
    """Master-mode ``on_edge`` sends an ``EDGE_REQ`` and blocks on a
    matching ``EDGE_RESP`` from the peer. We simulate the peer's
    reader thread inline."""
    a, b = NetworkBackend.pair()

    def fake_peer_reader():
        # Read our REQ, respond with inverted bit.
        frame = b._recv_exactly(2)
        opcode, payload = struct.unpack(">BB", frame)
        assert opcode == _OP_EDGE_REQ, f"expected REQ, got 0x{opcode:02x}"
        b._sock.sendall(struct.pack(">BB", _OP_EDGE_RESP, (~payload) & 1))

    peer = threading.Thread(target=fake_peer_reader, daemon=True)
    peer.start()

    # Start our reader so the incoming RESP goes onto the queue.
    a.start_receiver(local_core=None, irq_callback=None)
    try:
        reply = a.on_edge(our_bit=1, our_role=1)
        assert reply == 0  # inverted of 1
    finally:
        peer.join(timeout=2.0)
        a.stop()
        b.stop()


def test_on_edge_uses_one_total_deadline_for_send_and_response(monkeypatch):
    """A slow send cannot double the advertised owner-blocking limit."""
    a, b = NetworkBackend.pair()
    monkeypatch.setattr(
        "pokered_harness.link.network_backend._EDGE_RESPONSE_TIMEOUT_SECONDS",
        0.05,
    )
    observed_response_timeouts: list[float] = []

    def slow_send(_frame, *, timeout, operation, cancel_event=None):
        del timeout, operation, cancel_event
        time.sleep(0.02)

    def no_response(_queue, *, timeout, timeout_message, cancel_event=None):
        del timeout_message, cancel_event
        observed_response_timeouts.append(timeout)
        raise NetworkBackendError("test response timeout")

    monkeypatch.setattr(a, "_send_frame", slow_send)
    monkeypatch.setattr(a, "_queue_get", no_response)
    try:
        with pytest.raises(NetworkBackendError, match="test response timeout"):
            a.on_edge(our_bit=1, our_role=1)
        assert observed_response_timeouts
        assert observed_response_timeouts[0] < 0.045
    finally:
        a.stop()
        b.stop()


def test_stale_edge_response_fails_closed_without_wedging_on_edge():
    """A queued stale response closes the backend and returns promptly."""
    a, b = NetworkBackend.pair()
    a._resp_queue.put_nowait(1)
    finished = threading.Event()
    result: list[BaseException] = []

    def invoke_on_edge() -> None:
        try:
            a.on_edge(our_bit=1, our_role=1)
        except BaseException as exc:  # noqa: BLE001
            result.append(exc)
        finally:
            finished.set()

    worker = threading.Thread(target=invoke_on_edge, daemon=True)
    worker.start()
    try:
        assert finished.wait(timeout=1.0), "stale EDGE_RESP wedged on_edge"
        worker.join(timeout=1.0)
        assert not worker.is_alive()
        assert result and isinstance(result[0], NetworkBackendError)
        assert str(result[0]) == "stale EDGE_RESP before EDGE_REQ"
        assert not a.connected
    finally:
        if worker.is_alive():
            # The pre-fix implementation deadlocks while holding the lock;
            # do not call stop() on that path during failure cleanup.
            a._sock.close()
        else:
            a.stop()
        b.stop()


def test_on_edge_with_peer_hangup_raises():
    """If the peer closes the socket before responding, ``on_edge``
    times out and raises :class:`NetworkBackendError`."""
    a, b = NetworkBackend.pair()
    a.start_receiver(local_core=None)
    b.stop()  # close peer
    # With the peer gone, our reader hits EOF and marks closed. on_edge's
    # send will fail; if it succeeds, the wait for RESP times out.
    with pytest.raises(NetworkBackendError):
        # Retry a few times in case send succeeds before reader notices.
        for _ in range(3):
            a.on_edge(our_bit=1, our_role=1)
    a.stop()


def test_unknown_opcode_fails_closed_and_stops_reader():
    """Unknown wire operations are protocol errors, not ignorable noise."""
    a, b = NetworkBackend.pair()
    a.start_receiver(local_core=None)
    b._sock.sendall(struct.pack(">BB", 0xFF, 0))
    try:
        for _ in range(100):
            if a._reader_exc is not None:
                break
            time.sleep(0.01)
        assert isinstance(a._reader_exc, NetworkBackendError)
        assert not a.connected
    finally:
        a.stop()
        b.stop()


def test_unsolicited_edge_responses_fail_closed_without_wedging_shutdown():
    """A response without an in-flight request is a protocol error."""
    a, b = NetworkBackend.pair()
    a.start_receiver(local_core=None)
    b._sock.sendall(struct.pack(">BB", _OP_EDGE_RESP, 1) + struct.pack(">BB", _OP_EDGE_RESP, 0))
    try:
        for _ in range(100):
            if a._reader_exc is not None:
                break
            time.sleep(0.01)
        assert isinstance(a._reader_exc, NetworkBackendError)
        assert "unsolicited EDGE_RESP" in str(a._reader_exc)
        started = time.monotonic()
        a.stop()
        assert time.monotonic() - started < 0.5
        assert a._reader is not None and not a._reader.is_alive()
    finally:
        b.stop()


def test_duplicate_pending_sync_fails_closed():
    """The id-only SYNC frame cannot safely carry two outstanding markers."""
    a, b = NetworkBackend.pair()
    a.start_receiver(local_core=None)
    b.start_receiver(local_core=None)
    try:
        a.announce_sync(sync_id=42)
        a.announce_sync(sync_id=42)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and b._reader_exc is None:
            time.sleep(0.005)
        assert isinstance(b._reader_exc, NetworkBackendError)
        assert "duplicate pending OP_SYNC" in str(b._reader_exc)
        assert not b.connected
    finally:
        a.stop()
        b.stop()


def test_sync_id_can_be_reused_after_marker_is_consumed():
    """Duplicate protection must not make a connection single-use per id."""
    a, b = NetworkBackend.pair()
    a.start_receiver(local_core=None)
    b.start_receiver(local_core=None)
    try:
        for _ in range(2):
            a.announce_sync(sync_id=42)
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                if b.poll_peer_sync(sync_id=42):
                    break
                time.sleep(0.005)
            else:
                pytest.fail("peer SYNC marker was not delivered")
        assert b.connected
    finally:
        a.stop()
        b.stop()


def test_partial_frame_eof_fails_closed():
    """EOF after a partial control frame is protocol damage, not clean close."""
    a, b = NetworkBackend.pair()
    a.start_receiver(local_core=None)
    b._sock.sendall(struct.pack(">B", _OP_SYNC))
    b.stop()
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and a._reader_exc is None:
            time.sleep(0.005)
        assert isinstance(a._reader_exc, NetworkBackendError)
        assert "mid-frame" in str(a._reader_exc)
        assert not a.connected
    finally:
        a.stop()


def test_exchange_queue_backpressure_fails_closed():
    """A peer flooding one exchange kind cannot grow an unbounded queue."""
    a, b = NetworkBackend.pair()
    a.start_receiver(local_core=None)
    frame = struct.pack(">BBH", _OP_EXCHANGE, 7, 0)
    b._sock.sendall(frame * 257)
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and a._reader_exc is None:
            time.sleep(0.005)
        assert isinstance(a._reader_exc, NetworkBackendError)
        assert "queue is full" in str(a._reader_exc)
        assert not a.connected
    finally:
        a.stop()
        b.stop()


def test_edge_pending_stays_nonnegative_when_queue_full_races_close(monkeypatch):
    """Queue rejection after close must not underflow live edge accounting."""
    a, b = NetworkBackend.pair()
    a.start_receiver(
        local_core=None,
        serial_gate=SerialOperationGate(),
        dispatch_to_owner=True,
    )
    for _ in range(a._edge_queue.maxsize):
        a._edge_queue.put_nowait(object())

    put_entered = threading.Event()
    release_put = threading.Event()
    original_put_nowait = a._edge_queue.put_nowait

    def delayed_put(item):
        put_entered.set()
        assert release_put.wait(timeout=1.0)
        return original_put_nowait(item)

    monkeypatch.setattr(a._edge_queue, "put_nowait", delayed_put)
    closer = threading.Thread(target=a.stop, daemon=True)
    try:
        b._sock.sendall(struct.pack(">BB", _OP_EDGE_REQ, 1))
        assert put_entered.wait(timeout=1.0)
        closer.start()
        assert a._closed_event.wait(timeout=1.0)
        release_put.set()
        closer.join(timeout=1.0)
        assert not closer.is_alive()
        assert a.debug_snapshot()["pending_edge_requests"] == 0
    finally:
        release_put.set()
        if closer.is_alive():
            closer.join(timeout=1.0)
        a.stop()
        b.stop()


def test_cancelled_network_connect_returns_promptly():
    cancel = threading.Event()
    cancel.set()
    started = time.monotonic()
    with pytest.raises(NetworkBackendError, match="cancelled"):
        NetworkBackend.connect("127.0.0.1", 1, timeout_s=30.0, cancel_event=cancel)
    assert time.monotonic() - started < 1.0
