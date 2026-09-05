"""Tests for :class:`NetworkBackend` (milestone 8).

Proves the TCP transport exchanges bits correctly. Two layers of
coverage:

1. Raw protocol (``EDGE_REQ`` / ``EDGE_RESP``) via hand-rolled peer
   threads — asserts wire-level correctness and error paths.
2. End-to-end pairs of :class:`SerialCore` instances driven via the
   backend's reader threads — asserts NetworkBackend can substitute
   for :class:`CoordinatedBackend` without the peer being in the same
   process.
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

_OP_EDGE_REQ = 0x10
_OP_EDGE_RESP = 0x11
_OP_SYNC = 0x20
_OP_EXCHANGE = 0x30


def test_external_service_does_not_hold_dispatch_while_waiting_for_serial_gate():
    """Tick A must still dispatch after external service B waits for its gate."""
    a, b = NetworkBackend.pair()
    waiting = threading.Event()
    errors = []
    results = []

    class ObservedGate(SerialOperationGate):
        def __enter__(self):
            if threading.current_thread() is worker:
                waiting.set()
            return super().__enter__()

    gate = ObservedGate()
    b._serial_gate = gate
    b._dispatch_to_owner = True
    b._local_core = _CompletingSlaveCore()
    # Both queued edges are real owner-dispatch requests. No receiver is
    # needed: keeping completed responses queued makes accounting deterministic.
    first = network_module._InboundEdge(1)
    second = network_module._InboundEdge(0)
    b._edge_queue.put_nowait(first)
    b._edge_queue.put_nowait(second)
    b._edge_pending = 2

    def external_service():
        try:
            results.append(b.service_pending_edges(max_edges=1))
        except BaseException as exc:  # noqa: BLE001 - propagate worker failures below
            errors.append(exc)

    worker = threading.Thread(target=external_service, daemon=True)
    try:
        with gate:
            worker.start()
            assert waiting.wait(timeout=1.0)
            # B has reached gate admission. With the old
            # ordering it already holds dispatch, creating the exact inversion.
            # Probe nonblocking so a regression fails without wedging pytest.
            acquired = b._owner_dispatch_lock.acquire(blocking=False)
            assert acquired, "external service holds dispatch while waiting for serial gate"
            b._owner_dispatch_lock.release()
            assert b.service_pending_edges(max_edges=1) == 1
            # A waiter must not reserve the first edge before owning the gate:
            # the tick owner must apply that first edge, preserving wire FIFO.
            assert b._completed_edge_queue.get_nowait() is first
            assert b._local_core.SB == 1
            b._local_core.transfer_enabled = 1
        worker.join(timeout=1.0)
        assert not worker.is_alive()
        assert errors == []
        assert results == [1]
        assert b._completed_edge_queue.get_nowait() is second
        assert b._local_core.SB == 0
        assert b._stats["owner_edge_applied"] == 2
    finally:
        worker.join(timeout=1.0)
        a.stop()
        b.stop()


@pytest.mark.parametrize("lock_name", ["_edge_call_lock", "_edge_response_lock"])
@pytest.mark.parametrize("acquired", [False, True], ids=["timeout", "scheduler-overshoot"])
def test_edge_admission_expiry_preserves_active_response(monkeypatch, lock_name, acquired):
    """An unadmitted caller cannot close or consume the active transaction."""
    a, b = NetworkBackend.pair()
    now = [100.0]
    waits = []
    releases = []

    class ExpiringLock:
        def acquire(self, *, timeout):
            waits.append(timeout)
            # Model scheduler delay through the absolute deadline, including
            # a lock that becomes available just as its caller resumes.
            now[0] = 110.0 if acquired else round(now[0] + timeout, 12)
            return acquired

        def release(self):
            releases.append(True)

    def forbidden_close(*args, **kwargs):
        pytest.fail("admission timeout called terminal cleanup")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(network_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
            patch.setattr(a, lock_name, ExpiringLock())
            patch.setattr(a, "_mark_closed", forbidden_close)
            a._edge_inflight = True
            a._resp_queue.put_nowait(1)
            before = dict(a._stats)
            with pytest.raises(NetworkBackendError, match="admission timed out"):
                a.on_edge(our_bit=0, our_role=1)
            assert waits
            assert all(0 < timeout <= network_module._SEND_POLL_SECONDS for timeout in waits)
            if acquired:
                assert waits == [network_module._SEND_POLL_SECONDS]
            else:
                assert len(waits) == round(10.0 / network_module._SEND_POLL_SECONDS)
                assert sum(waits) == pytest.approx(10.0)
            assert releases == ([True] if acquired else [])
            assert a.connected
            assert not a._closed_event.is_set()
            assert a._reader_exc is None
            assert a._edge_inflight is True
            assert a._resp_queue.get_nowait() == 1
            assert a._resp_queue.empty()
            assert a._stats == before
            with pytest.raises(BlockingIOError):
                b._sock.recv(1)
            # The successfully acquired outer lock must also be released
            # when response-lock admission expires.
            if lock_name == "_edge_response_lock":
                assert a._edge_call_lock.acquire(blocking=False)
                a._edge_call_lock.release()
    finally:
        a.stop()
        b.stop()


def test_edge_admission_and_write_waits_reduce_original_response_budget(monkeypatch):
    """Every phase spends the original ten seconds, including both admissions."""
    a, b = NetworkBackend.pair()
    now = [100.0]
    waits = []

    class SpendingLock:
        def __init__(self, name, elapsed):
            self.name = name
            self.elapsed = elapsed

        def acquire(self, *, timeout):
            waits.append((self.name, timeout))
            # Successful acquisition may resume late after a scheduler stall;
            # subsequent phases must still use the original absolute deadline.
            now[0] += self.elapsed
            return True

        def release(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def send(frame, *, timeout, operation):
        assert frame == struct.pack(">BB", _OP_EDGE_REQ, 1)
        assert operation == "EDGE_REQ"
        waits.append(("send", timeout))
        now[0] += 0.003

    def receive(q, *, timeout, timeout_message):
        assert q is a._resp_queue
        assert "10s" in timeout_message
        waits.append(("response", timeout))
        return 0

    try:
        with monkeypatch.context() as patch:
            patch.setattr(network_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
            patch.setattr(a, "_edge_call_lock", SpendingLock("call", 9.98))
            patch.setattr(a, "_edge_response_lock", SpendingLock("admission-response", 0.01))
            patch.setattr(a, "_write_lock", SpendingLock("write", 0.004))
            patch.setattr(a, "_send_frame", send)
            patch.setattr(a, "_queue_get", receive)
            assert a.on_edge(our_bit=1, our_role=1) == 0
            assert [name for name, _ in waits] == [
                "call", "admission-response", "write", "send", "response"
            ]
            assert [timeout for _, timeout in waits] == pytest.approx(
                [network_module._SEND_POLL_SECONDS, 0.02, 0.01, 0.006, 0.003]
            )
            assert a._edge_inflight is False
            assert a._stats["edge_req_sent"] == 1
            assert a._stats["edge_resp_received"] == 1
    finally:
        a.stop()
        b.stop()


@pytest.mark.parametrize("acquired", [False, True], ids=["waiting", "just-acquired"])
def test_edge_admission_observes_close_without_waiting_full_budget(monkeypatch, acquired):
    a, b = NetworkBackend.pair()
    waits = []
    releases = []

    class ClosingLock:
        def acquire(self, *, timeout):
            waits.append(timeout)
            assert len(waits) == 1, "admission retried after terminal close"
            a._closed = True
            a._closed_event.set()
            return acquired

        def release(self):
            releases.append(True)

    def forbidden_close(*args, **kwargs):
        pytest.fail("unadmitted caller attempted terminal cleanup")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(network_module, "time", SimpleNamespace(monotonic=lambda: 100.0))
            patch.setattr(a, "_edge_call_lock", ClosingLock())
            patch.setattr(a, "_mark_closed", forbidden_close)
            with pytest.raises(NetworkBackendError, match="closed"):
                a.on_edge(our_bit=1, our_role=1)
            assert waits == [network_module._SEND_POLL_SECONDS]
            assert releases == ([True] if acquired else [])
            assert a._edge_inflight is False
            assert a._stats["edge_req_sent"] == 0
            with pytest.raises(BlockingIOError):
                b._sock.recv(1)
    finally:
        # This test models the terminal flag transition without closing the
        # socket, so release that owned descriptor explicitly.
        a._sock.close()
        b.stop()


# ---------------------------------------------------------------------------
# Pair construction + basic wire contract
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Raw wire protocol: REQ / RESP round-trip
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Backend drives a full SerialCore byte-exchange via two reader threads
# ---------------------------------------------------------------------------


def test_two_serialcores_exchange_byte_via_network_backend():
    """Canonical proof: two :class:`SerialCore` instances, each paired
    with its own :class:`NetworkBackend`, exchange a full byte over
    a socketpair. Master-mode edges on one side are handled by the
    peer's reader thread driving its local ``SerialCore`` — exactly
    the shape a two-process two-PyBoy setup needs.
    """
    from pokered_harness.link.serial_core import (
        CYCLES_PER_BYTE_DMG,
        SerialCore,
    )

    ba, bb = NetworkBackend.pair()

    master = SerialCore(backend=ba)
    slave = SerialCore()  # no outgoing backend; driven by bb's reader

    master.set_SB(0xAA)
    slave.set_SB(0x55)
    master.set_SC(0x81)  # internal clock
    slave.set_SC(0x80)  # external clock

    # Record slave-side IRQ fires.
    slave_irqs: list[int] = []

    def slave_irq():
        slave_irqs.append(1)

    ba.start_receiver(local_core=master)  # not strictly needed; keeps
    # reader idle for master side
    bb.start_receiver(local_core=slave, irq_callback=slave_irq)

    try:
        irq = master.tick(CYCLES_PER_BYTE_DMG)
        assert irq is True, "master didn't complete transfer"

        # Give bb's reader a moment to finish its final edge send.
        time.sleep(0.1)
        assert master.SB == 0x55, f"master got 0x{master.SB:02x}"
        assert slave.SB == 0xAA, f"slave got 0x{slave.SB:02x}"
        assert slave_irqs == [1], "slave IRQ callback should fire once"
    finally:
        ba.stop()
        bb.stop()


def test_multiple_bytes_exchange():
    """Two cores exchange three bytes in a row via the reader-thread
    model. Proves the backend works across multiple transfers without
    resetting."""
    from pokered_harness.link.serial_core import (
        CYCLES_PER_BYTE_DMG,
        SerialCore,
    )

    ba, bb = NetworkBackend.pair()
    master = SerialCore(backend=ba)
    slave = SerialCore()

    ba.start_receiver(local_core=master)
    bb.start_receiver(local_core=slave)

    try:
        for master_byte, slave_byte in [(0x01, 0xFE), (0xAA, 0x55), (0x42, 0x24)]:
            master.set_SB(master_byte)
            slave.set_SB(slave_byte)
            master.set_SC(0x81)
            slave.set_SC(0x80)
            master.tick(master.last_cycles + CYCLES_PER_BYTE_DMG)
            time.sleep(0.05)
            assert master.SB == slave_byte, (
                f"master expected 0x{slave_byte:02x}, got 0x{master.SB:02x}"
            )
            assert slave.SB == master_byte, (
                f"slave expected 0x{master_byte:02x}, got 0x{slave.SB:02x}"
            )
    finally:
        ba.stop()
        bb.stop()


# ---------------------------------------------------------------------------
# Real TCP listen/connect
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    try:
        return s.getsockname()[1]
    finally:
        s.close()


def test_sync_with_peer_rendezvous():
    """Both sides call sync_with_peer(id=N); each call returns only
    after the peer's matching SYNC arrives. Proves the barrier
    primitive for subprocess-level trade phase boundaries."""
    a, b = NetworkBackend.pair()
    a.start_receiver(local_core=None)
    b.start_receiver(local_core=None)

    a_done = threading.Event()
    b_done = threading.Event()

    def side(backend, done):
        backend.sync_with_peer(sync_id=7, timeout=5.0)
        done.set()

    ta = threading.Thread(target=side, args=(a, a_done), daemon=True)
    tb = threading.Thread(target=side, args=(b, b_done), daemon=True)
    ta.start()
    tb.start()
    try:
        assert a_done.wait(timeout=6.0), "side A never returned from sync"
        assert b_done.wait(timeout=6.0), "side B never returned from sync"
    finally:
        ta.join(timeout=1.0)
        tb.join(timeout=1.0)
        a.stop()
        b.stop()


def test_announce_sync_can_be_polled_without_blocking():
    """A peer can advertise a SYNC point and the other side can poll it."""
    a, b = NetworkBackend.pair()
    a.start_receiver(local_core=None)
    b.start_receiver(local_core=None)
    try:
        a.announce_sync(sync_id=9)
        deadline = time.monotonic() + 2.0
        hit = False
        while time.monotonic() < deadline and not hit:
            hit = b.poll_peer_sync(sync_id=9)
            time.sleep(0.01)
        assert hit is True
        assert b.poll_peer_sync(sync_id=9) is False
        assert a.debug_snapshot()["sync_sent"] == 1
        assert b.debug_snapshot()["sync_received"] == 1
        assert b.debug_snapshot()["sync_poll_hits"] == 1
    finally:
        a.stop()
        b.stop()


def test_sync_with_peer_times_out_on_silent_peer():
    """If the peer never sends SYNC, sync_with_peer raises
    :class:`NetworkBackendError` after the timeout."""
    a, b = NetworkBackend.pair()
    a.start_receiver(local_core=None)
    # Peer's reader not started; B never sends its SYNC back.
    try:
        with pytest.raises(NetworkBackendError, match="no peer OP_SYNC"):
            a.sync_with_peer(sync_id=1, timeout=1.0)
    finally:
        a.stop()
        b.stop()


def test_sync_with_peer_can_be_cancelled_and_closes_transport():
    """A cancelled barrier cannot be reused with a stale SYNC marker."""
    a, b = NetworkBackend.pair()
    a.start_receiver(local_core=None)
    b.start_receiver(local_core=None)
    cancel = threading.Event()
    cancel.set()
    try:
        with pytest.raises(NetworkBackendError, match="cancelled"):
            a.sync_with_peer(sync_id=2, timeout=5.0, cancel_event=cancel)
        assert not a.connected
    finally:
        a.stop()
        b.stop()


def test_wire_idle_wait_can_be_cancelled_without_touching_emulator():
    """Cancellation is observable even when no transport work is pending."""
    a, b = NetworkBackend.pair()
    a.start_receiver(local_core=None)
    b.start_receiver(local_core=None)
    cancel = threading.Event()
    cancel.set()
    try:
        with pytest.raises(NetworkBackendError, match="cancelled"):
            a.wait_for_wire_idle(timeout=5.0, cancel_event=cancel)
        assert a.connected
    finally:
        a.stop()
        b.stop()


def test_keepalive_fallback_is_visible_in_debug_snapshot():
    """An unarmed slave responds with keep-alive and records that fact."""
    a, b = NetworkBackend.pair()
    a.start_receiver(local_core=None)
    b.start_receiver(local_core=None)
    try:
        reply = a.on_edge(our_bit=1, our_role=1)
        assert reply == 1
        deadline = time.monotonic() + 1.0
        snap = b.debug_snapshot()
        while time.monotonic() < deadline and snap["edge_resp_sent"] == 0:
            time.sleep(0.01)
            snap = b.debug_snapshot()
        assert snap["edge_req_received"] == 1
        assert snap["edge_resp_sent"] == 1
        assert snap["slave_rearm_waits"] == 1
        assert snap["keepalive_bits_sent"] == 1
        assert snap["keepalive_bytes_started"] == 1
        assert snap["last_keepalive_state"] == {"core_present": False}
    finally:
        a.stop()
        b.stop()


class _CompletingSlaveCore:
    def __init__(self) -> None:
        self.transfer_enabled = 1
        self.internal_clock = 0
        self.SB = 0
        self.SC = 0x80

    def peek_out_bit(self) -> int:
        return 0

    def apply_external_edge(self, peer_bit: int) -> bool:
        self.transfer_enabled = 0
        self.SB = peer_bit & 1
        return True


class _BlockingSlaveCore:
    """Pause edge application so a transport-idle timeout is observable."""

    def __init__(self) -> None:
        self.transfer_enabled = 1
        self.internal_clock = 0
        self.SB = 0
        self.SC = 0x80
        self.edge_started = threading.Event()
        self.release_edge = threading.Event()

    def peek_out_bit(self) -> int:
        return 0

    def apply_external_edge(self, _peer_bit: int) -> bool:
        self.edge_started.set()
        if not self.release_edge.wait(timeout=2.0):
            raise RuntimeError("test edge release timed out")
        return False


def test_owner_dispatch_close_waits_for_inflight_native_edge():
    """Terminal close cannot race an owner-thread native edge application."""
    a, b = NetworkBackend.pair()

    class _CloseRaceCore(_BlockingSlaveCore):
        def __init__(self) -> None:
            super().__init__()
            self.backend: NetworkBackend | None = None
            self.closed_during_apply: bool | None = None

        def apply_external_edge(self, peer_bit: int) -> bool:
            self.edge_started.set()
            if not self.release_edge.wait(timeout=2.0):
                raise RuntimeError("test edge release timed out")
            assert self.backend is not None
            self.closed_during_apply = self.backend._closed
            self.SB = peer_bit & 1
            return False

    core = _CloseRaceCore()
    core.backend = b
    a.start_receiver(local_core=None)
    b.start_receiver(
        local_core=core,
        serial_gate=SerialOperationGate(),
        dispatch_to_owner=True,
    )
    sender_errors: list[Exception] = []
    dispatch_errors: list[Exception] = []
    dispatch_done = threading.Event()
    stop_started = threading.Event()
    stop_done = threading.Event()
    stop_results: list[bool] = []

    def send_edge() -> None:
        try:
            a.on_edge(our_bit=1, our_role=1)
        except Exception as exc:  # noqa: BLE001 - close may win the response race
            sender_errors.append(exc)

    def dispatch_edge() -> None:
        try:
            assert b.service_pending_edges(max_edges=1) == 1
        except Exception as exc:  # noqa: BLE001 - surface worker failures below
            dispatch_errors.append(exc)
        finally:
            dispatch_done.set()

    def stop_backend() -> None:
        stop_started.set()
        stop_results.append(b.stop(timeout_s=1.0))
        stop_done.set()

    sender = threading.Thread(target=send_edge, daemon=True)
    dispatcher = threading.Thread(target=dispatch_edge, daemon=True)
    stopper = threading.Thread(target=stop_backend, daemon=True)
    try:
        sender.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and b.debug_snapshot()["pending_edge_requests"] == 0:
            time.sleep(0.005)
        assert b.debug_snapshot()["pending_edge_requests"] == 1

        dispatcher.start()
        assert core.edge_started.wait(timeout=1.0)
        stopper.start()
        assert stop_started.wait(timeout=1.0)
        # stop() is serialized behind the owner operation and therefore must
        # remain pending until the native edge returns.
        time.sleep(0.02)
        assert not stop_done.is_set()

        core.release_edge.set()
        assert dispatch_done.wait(timeout=1.0)
        assert stop_done.wait(timeout=1.0)
        dispatcher.join(timeout=1.0)
        stopper.join(timeout=1.0)
        sender.join(timeout=1.0)
        assert not dispatcher.is_alive()
        assert not stopper.is_alive()
        assert not sender.is_alive()
        assert dispatch_errors == []
        assert stop_results == [True]
        assert core.closed_during_apply is False
        assert b.debug_snapshot()["closed"] is True
    finally:
        core.release_edge.set()
        sender.join(timeout=1.0)
        dispatcher.join(timeout=1.0)
        stopper.join(timeout=1.0)
        a.stop()
        b.stop()


def test_stop_releases_owner_queued_edge_accounting():
    """Closing a queued owner-dispatch backend cannot strand pending work."""
    a, b = NetworkBackend.pair()
    core = _CompletingSlaveCore()
    core.transfer_enabled = 0
    a.start_receiver(local_core=None)
    b.start_receiver(
        local_core=core,
        serial_gate=SerialOperationGate(),
        dispatch_to_owner=True,
    )
    sender_errors: list[Exception] = []

    def send_edge() -> None:
        try:
            a.on_edge(our_bit=1, our_role=1)
        except Exception as exc:  # noqa: BLE001 - shutdown is expected here
            sender_errors.append(exc)

    sender = threading.Thread(target=send_edge, daemon=True)
    sender.start()
    try:
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and b.debug_snapshot()["pending_edge_requests"] == 0:
            time.sleep(0.005)
        assert b.debug_snapshot()["pending_edge_requests"] == 1

        assert b.stop(timeout_s=0.2) is True
        assert b.debug_snapshot()["pending_edge_requests"] == 0
        sender.join(timeout=1.0)
        assert not sender.is_alive()
        assert sender_errors and isinstance(sender_errors[0], NetworkBackendError)
        assert core.SB == 0
    finally:
        a.stop()
        b.stop()


def test_wait_for_wire_idle_is_bounded_while_edge_worker_is_busy():
    """The phase barrier must expose a blocked edge instead of hanging."""
    a, b = NetworkBackend.pair()
    core = _BlockingSlaveCore()
    a.start_receiver(local_core=None)
    b.start_receiver(local_core=core)
    result: list[int] = []

    def send_edge() -> None:
        result.append(a.on_edge(our_bit=1, our_role=1))

    worker = threading.Thread(target=send_edge, daemon=True)
    worker.start()
    try:
        assert core.edge_started.wait(timeout=1.0)
        with pytest.raises(NetworkBackendError, match="wire did not become idle"):
            b.wait_for_wire_idle(timeout=0.01)
        assert b.debug_snapshot()["pending_edge_requests"] == 1

        core.release_edge.set()
        worker.join(timeout=1.0)
        assert not worker.is_alive()
        assert result == [0]
        b.wait_for_wire_idle(timeout=1.0)
        assert b.debug_snapshot()["pending_edge_requests"] == 0
    finally:
        core.release_edge.set()
        a.stop()
        b.stop()


def test_wait_for_wire_idle_can_progress_owner_queued_edge():
    """An owner callback drains inbound work without a transport deadlock."""
    a, b = NetworkBackend.pair()
    core = _CompletingSlaveCore()
    core.transfer_enabled = 0
    core.SC = 0
    a.start_receiver(local_core=None)
    b.start_receiver(
        local_core=core,
        serial_gate=SerialOperationGate(),
        dispatch_to_owner=True,
    )
    result: list[int] = []
    sender = threading.Thread(
        target=lambda: result.append(a.on_edge(our_bit=1, our_role=1)),
        daemon=True,
    )
    callback_calls = 0

    def progress() -> None:
        nonlocal callback_calls
        callback_calls += 1
        core.transfer_enabled = 1
        core.SC = 0x80
        b.service_pending_edges(max_edges=1)

    try:
        sender.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and b.debug_snapshot()["pending_edge_requests"] == 0:
            time.sleep(0.005)
        b.wait_for_wire_idle(
            timeout=1.0,
            progress_callback=progress,
            stable_checks=2,
        )
        sender.join(timeout=1.0)
        assert not sender.is_alive()
        assert result == [0]
        assert callback_calls > 0
        assert b.debug_snapshot()["pending_edge_requests"] == 0
    finally:
        a.stop()
        b.stop()


def test_owner_dispatch_uses_no_data_during_clock_role_transition():
    """A transient internal-clock edge gets a connected/no-data response."""
    a, b = NetworkBackend.pair()
    core = _CompletingSlaveCore()
    core.transfer_enabled = 1
    core.internal_clock = 1
    core.SC = 0x81
    a.start_receiver(local_core=None)
    b.start_receiver(
        local_core=core,
        serial_gate=SerialOperationGate(),
        dispatch_to_owner=True,
    )
    result: list[int] = []
    sender = threading.Thread(
        target=lambda: result.append(a.on_edge(our_bit=1, our_role=1)),
        daemon=True,
    )
    try:
        sender.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and b.debug_snapshot()["pending_edge_requests"] == 0:
            time.sleep(0.005)
        assert b.service_pending_edges(max_edges=1) == 1
        assert b.debug_snapshot()["pending_edge_requests"] == 1
        assert b.connected

        sender.join(timeout=1.0)
        assert not sender.is_alive()
        assert result == [1]
        assert core.SB == 0
        snapshot = b.debug_snapshot()
        assert snapshot["keepalive_bits_sent"] == 1
        assert snapshot["owner_edge_applied"] == 1
    finally:
        a.stop()
        b.stop()


def test_owner_real_edge_resets_keepalive_byte_boundary():
    """A real owner edge restarts the next transient keep-alive byte."""
    a, b = NetworkBackend.pair()
    core = _CompletingSlaveCore()
    core.internal_clock = 1
    a.start_receiver(local_core=None)
    b.start_receiver(
        local_core=core,
        serial_gate=SerialOperationGate(),
        dispatch_to_owner=True,
    )

    def request_and_service() -> int:
        result: list[int] = []
        sender = threading.Thread(
            target=lambda: result.append(a.on_edge(our_bit=1, our_role=1)),
            daemon=True,
        )
        sender.start()
        deadline = time.monotonic() + 1.0
        while (
            time.monotonic() < deadline
            and b.debug_snapshot()["pending_edge_requests"] == 0
        ):
            time.sleep(0.005)
        assert b.debug_snapshot()["pending_edge_requests"] == 1
        assert b.service_pending_edges(max_edges=1) == 1
        sender.join(timeout=1.0)
        assert not sender.is_alive()
        return result[0]

    try:
        assert request_and_service() == 1

        # A genuine slave edge must clear the keep-alive stream state.
        core.internal_clock = 0
        core.transfer_enabled = 1
        assert request_and_service() == 0

        # Seven subsequent transient responses are the first seven bits of
        # 0xFE. Without the reset above, the seventh response would be 0.
        core.internal_clock = 1
        core.transfer_enabled = 1
        assert [request_and_service() for _ in range(7)] == [1] * 7
    finally:
        a.stop()
        b.stop()


def test_wait_for_wire_idle_can_accept_orderly_peer_close():
    """A completed application barrier may be followed by peer teardown."""
    a, b = NetworkBackend.pair()
    a.start_receiver(local_core=None)
    b.start_receiver(local_core=None)
    try:
        a.stop()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and b.connected:
            time.sleep(0.01)
        b.wait_for_wire_idle(timeout=1.0, allow_peer_close=True)
        assert b._reader_exc is None
    finally:
        b.stop()


def test_post_byte_fallback_is_visible_in_debug_snapshot():
    """A keep-alive immediately after a completed byte is tagged separately."""
    a, b = NetworkBackend.pair()
    core = _CompletingSlaveCore()
    a.start_receiver(local_core=None)
    b.start_receiver(local_core=core)
    try:
        first_reply = a.on_edge(our_bit=1, our_role=1)
        assert first_reply == 0
        second_reply = a.on_edge(our_bit=0, our_role=1)
        assert second_reply == 1
        deadline = time.monotonic() + 1.0
        snap = b.debug_snapshot()
        while time.monotonic() < deadline and snap["keepalive_after_post_byte_waits"] == 0:
            time.sleep(0.01)
            snap = b.debug_snapshot()
        assert snap["slave_post_byte_rearm_waits"] >= 1
        assert snap["keepalive_after_post_byte_waits"] >= 1
        assert snap["last_slave_byte_complete_at"] is not None
    finally:
        a.stop()
        b.stop()


class _LateRearmingSlaveCore:
    def __init__(self) -> None:
        self.transfer_enabled = 1
        self.internal_clock = 0
        self.SB = 0
        self.SC = 0x80
        self._next_out_bit = 0
        self._byte_index = 0

    def peek_out_bit(self) -> int:
        return self._next_out_bit

    def apply_external_edge(self, peer_bit: int) -> bool:
        self.SB = peer_bit & 1
        if self._byte_index == 0:
            self.transfer_enabled = 0
            self._byte_index += 1
            return True
        self._byte_index += 1
        return False

    def rearm_after(self, delay_s: float, *, next_out_bit: int) -> None:
        def _rearm() -> None:
            time.sleep(delay_s)
            self._next_out_bit = next_out_bit & 1
            self.transfer_enabled = 1

        threading.Thread(target=_rearm, daemon=True).start()


class _DisarmingSlaveCore:
    """Become unarmed between the worker's readiness checks."""

    def __init__(self) -> None:
        self._transfer_reads = 0
        self.internal_clock = 0
        self.SB = 0
        self.SC = 0x80

    @property
    def transfer_enabled(self) -> int:
        self._transfer_reads += 1
        return 1 if self._transfer_reads == 1 else 0


def test_rearm_race_falls_back_to_keepalive_without_worker_crash():
    """A transfer ending during dispatch must not reference an unbound phase."""
    a, b = NetworkBackend.pair()
    core = _DisarmingSlaveCore()
    a.start_receiver(local_core=None)
    b.start_receiver(local_core=core)
    try:
        assert a.on_edge(our_bit=1, our_role=1) == 1
        assert b.connected
        snapshot = b.debug_snapshot()
        assert snapshot["keepalive_bits_sent"] == 1
        assert snapshot["edge_resp_sent"] == 1
    finally:
        a.stop()
        b.stop()


def test_post_byte_rearm_grace_accepts_late_real_byte_without_keepalive():
    """A slave that rearms after the default wait but within the post-byte
    grace window should send its real next byte, not 0xFE keep-alive."""
    a, b = NetworkBackend.pair()
    core = _LateRearmingSlaveCore()
    a.start_receiver(local_core=None)
    b.start_receiver(local_core=core)
    try:
        first_reply = a.on_edge(our_bit=1, our_role=1)
        assert first_reply == 0

        core.rearm_after(0.150, next_out_bit=0)
        started = time.monotonic()
        second_reply = a.on_edge(our_bit=0, our_role=1)
        elapsed = time.monotonic() - started

        assert second_reply == 0
        assert elapsed >= 0.140

        snap = b.debug_snapshot()
        assert snap["slave_post_byte_rearm_waits"] >= 1
        assert snap["slave_post_byte_rearm_successes"] >= 1
        assert snap["keepalive_after_post_byte_waits"] == 0
        assert snap["keepalive_bits_sent"] == 0
    finally:
        a.stop()
        b.stop()


def test_listen_and_connect_over_loopback_exchange_byte():
    """Full loopback: one thread listens, one connects, each gets a
    :class:`NetworkBackend`, and they exchange a byte."""
    from pokered_harness.link.serial_core import (
        CYCLES_PER_BYTE_DMG,
        SerialCore,
    )

    port = _free_port()
    server_holder: dict = {}
    ready = threading.Event()

    # Pre-bind the listener in main thread so we can reliably connect
    # once the server thread calls accept().
    listener = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    listener.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(1)

    def server():
        conn, _ = listener.accept()
        conn.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
        backend = NetworkBackend(conn)
        server_holder["backend"] = backend
        core = SerialCore()
        core.set_SB(0x33)
        core.set_SC(0x80)
        backend.start_receiver(local_core=core)
        server_holder["core"] = core
        ready.set()
        time.sleep(2.0)

    t = threading.Thread(target=server, daemon=True)
    t.start()

    client_backend = NetworkBackend.connect("127.0.0.1", port)
    assert ready.wait(timeout=2.0), "server didn't finish setup"
    client_core = SerialCore(backend=client_backend)
    client_core.set_SB(0xCC)
    client_core.set_SC(0x81)
    client_backend.start_receiver(local_core=client_core)
    try:
        client_core.tick(CYCLES_PER_BYTE_DMG)
        time.sleep(0.2)
        assert client_core.SB == 0x33
        assert server_holder["core"].SB == 0xCC
    finally:
        client_backend.stop()
        t.join(timeout=2.0)
        if "backend" in server_holder:
            server_holder["backend"].stop()
        try:
            listener.close()
        except OSError:
            pass


def _wait_bound(port: int, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            s.settimeout(0.05)
            s.connect(("127.0.0.1", port))
            s.close()
            return True
        except (TimeoutError, OSError):
            time.sleep(0.05)
    return False
