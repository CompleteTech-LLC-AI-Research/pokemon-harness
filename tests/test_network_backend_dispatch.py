"""Frame dispatch, edge admission and versioned response handling (#128).

Split from ``tests/test_network_backend.py`` for #128 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module.
"""

from __future__ import annotations

import socket as _socket
import struct
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from pokered_harness.link import network_backend as network_module
from pokered_harness.link.network_backend import (
    NetworkBackend,
    NetworkBackendError,
)
from pokered_harness.link.serial_coordinator import SerialOperationGate
from tests._network_backend_support import (
    _OP_EDGE_REQ,
    _CompletingSlaveCore,
)


def _finish_network_test_cleanup(
    *,
    primary: BaseException | None,
    backends: tuple[tuple[str, NetworkBackend], ...] = (),
    threads: tuple[tuple[str, threading.Thread], ...] = (),
) -> None:
    """Attempt every test resource cleanup and preserve a primary failure."""
    cleanup_errors: list[tuple[str, BaseException]] = []
    for label, backend in backends:
        try:
            if backend.stop(timeout_s=1.0) is False:
                raise AssertionError(f"{label} backend workers did not stop")
        except BaseException as exc:  # noqa: BLE001 - report cleanup failures below
            cleanup_errors.append((label, exc))
    for label, thread in threads:
        # A failure before ``start`` leaves ``ident`` unset; joining such a
        # thread raises RuntimeError and hides the actual test/cleanup result.
        if thread.ident is None:
            continue
        try:
            thread.join(timeout=1.0)
            if thread.is_alive():
                raise AssertionError(f"{label} thread remained alive")
        except BaseException as exc:  # noqa: BLE001 - report cleanup failures below
            cleanup_errors.append((label, exc))
    if not cleanup_errors:
        return
    details = "; ".join(f"{label}: {error!r}" for label, error in cleanup_errors)
    if primary is not None:
        primary.add_note(f"test cleanup failed: {details}")
        return
    raise AssertionError(f"test cleanup failed: {details}") from cleanup_errors[0][1]


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


def test_deferred_edge_queue_refill_fails_closed_without_blocking(monkeypatch):
    """A malicious refill cannot block a deferred owner's bounded requeue."""
    a, b = NetworkBackend.pair()
    b._dispatch_to_owner = True
    b._local_core = _CompletingSlaveCore()
    b._edge_queue.put_nowait(network_module._InboundEdge(1))
    b._edge_pending = 1

    def refill_before_deferring(_request):
        for _ in range(b._edge_queue.maxsize):
            b._edge_queue.put_nowait(network_module._InboundEdge(0))
        return False

    monkeypatch.setattr(b, "_apply_owner_edge_if_ready", refill_before_deferring)
    try:
        with pytest.raises(NetworkBackendError, match="queue is full while deferring"):
            b.service_pending_edges(max_edges=1)
        assert not b.connected
        assert b._edge_pending == 0
        assert b._stats["owner_edge_applied"] == 0
        assert b._completed_edge_queue.empty() or b._completed_edge_queue.get_nowait() is None
    finally:
        a.stop()
        b.stop()


def test_frame_done_is_retained_while_response_accounting_drains(monkeypatch):
    """A received DONE survives a later transport-worker pending decrement."""
    leader, follower = NetworkBackend.pair()
    monkeypatch.setattr(network_module, "_EDGE_RESPONSE_TIMEOUT_SECONDS", 0.2)
    follower._frame_done_queue.put_nowait(None)
    follower._edge_pending = 1
    polls = 0

    def drain_response():
        nonlocal polls
        polls += 1
        if polls == 2:
            assert follower._frame_done_queue.empty()
            follower._decrement_edge_pending()

    try:
        follower.finish_frame_turn(leader=False, progress_callback=drain_response)
        assert polls == 2
        assert follower._edge_pending == 0
        assert follower.debug_snapshot()["frame_acks_sent"] == 1
        assert follower.connected
    finally:
        leader.stop()
        follower.stop()


@pytest.mark.parametrize("done_received,pending", [(False, 0), (True, 1)])
def test_follower_frame_completion_requires_done_and_drained_responses(
    monkeypatch, done_received, pending
):
    leader, follower = NetworkBackend.pair()
    monkeypatch.setattr(network_module, "_EDGE_RESPONSE_TIMEOUT_SECONDS", 0.03)
    if done_received:
        follower._frame_done_queue.put_nowait(None)
    follower._edge_pending = pending
    try:
        with pytest.raises(NetworkBackendError, match="no FRAME_DONE"):
            follower.finish_frame_turn(leader=False, progress_callback=lambda: None)
        assert follower.debug_snapshot()["frame_acks_sent"] == 0
        assert not follower.connected
    finally:
        leader.stop()
        follower.stop()


def test_frame_barrier_round_trip_keeps_turns_and_acknowledgements_bounded():
    """The negotiated owner-frame control path completes without edge traffic."""
    leader, follower = NetworkBackend.pair()
    leader.start_receiver(local_core=None)
    follower.start_receiver(local_core=None)
    errors: list[BaseException] = []

    def run_leader() -> None:
        try:
            for _ in range(3):
                leader.begin_frame_turn(leader=True)
                leader.finish_frame_turn(leader=True)
        except BaseException as exc:  # noqa: BLE001 - assert worker failures below
            errors.append(exc)

    def run_follower() -> None:
        try:
            for _ in range(3):
                follower.begin_frame_turn(leader=False)
                follower.finish_frame_turn(leader=False, progress_callback=lambda: None)
        except BaseException as exc:  # noqa: BLE001 - assert worker failures below
            errors.append(exc)

    leader_thread = threading.Thread(target=run_leader, daemon=True)
    follower_thread = threading.Thread(target=run_follower, daemon=True)
    try:
        leader_thread.start()
        follower_thread.start()
        leader_thread.join(timeout=2.0)
        follower_thread.join(timeout=2.0)
        assert not leader_thread.is_alive()
        assert not follower_thread.is_alive()
        assert errors == []
        leader_stats = leader.debug_snapshot()
        follower_stats = follower.debug_snapshot()
        assert leader_stats["frame_ticks_sent"] == 3
        assert leader_stats["frame_dones_sent"] == 3
        assert leader_stats["frame_acks_received"] == 3
        assert follower_stats["frame_ticks_received"] == 3
        assert follower_stats["frame_dones_received"] == 3
        assert follower_stats["frame_acks_sent"] == 3
        assert leader_stats["edge_req_sent"] == 0
        assert follower_stats["edge_req_sent"] == 0
    finally:
        leader.stop()
        follower.stop()


def test_leader_frame_ack_wait_pumps_owner_edge_after_frame_done(monkeypatch):
    """A reversed native clock role cannot strand a post-FRAME_DONE edge.

    The frame leader is the native external-clock side here, while the peer
    acts as the native master and starts its edge only after FRAME_DONE is on
    the wire. The leader must keep its owner-dispatch path alive while it
    waits for FRAME_ACK: the peer cannot send that ACK until this edge gets
    applied and answered.
    """
    leader, follower = NetworkBackend.pair()
    monkeypatch.setattr(network_module, "_EDGE_RESPONSE_TIMEOUT_SECONDS", 0.2)
    leader_core = _CompletingSlaveCore()
    frame_done_sent = threading.Event()
    follower_result: list[int] = []
    follower_errors: list[BaseException] = []
    original_send_frame = leader._send_frame

    def observe_frame_done(frame, *, timeout, operation, cancel_event=None):
        result = original_send_frame(
            frame,
            timeout=timeout,
            operation=operation,
            cancel_event=cancel_event,
        )
        if frame[0] == network_module._OP_FRAME_DONE:
            frame_done_sent.set()
        return result

    def run_follower() -> None:
        try:
            follower.begin_frame_turn(leader=False)
            assert frame_done_sent.wait(timeout=1.0)
            # This is the native master's first EDGE_REQ. It deliberately
            # begins after the leader has sent FRAME_DONE, reproducing the
            # role-metadata/native-register inversion seen in production.
            follower_result.append(follower.on_edge(our_bit=1, our_role=1))
            follower.finish_frame_turn(leader=False, progress_callback=lambda: None)
        except BaseException as exc:  # noqa: BLE001 - assert worker failures below
            follower_errors.append(exc)

    monkeypatch.setattr(leader, "_send_frame", observe_frame_done)
    follower_thread = threading.Thread(target=run_follower, daemon=True)
    try:
        leader.start_receiver(
            local_core=leader_core,
            serial_gate=SerialOperationGate(),
            dispatch_to_owner=True,
        )
        follower.start_receiver(local_core=None)
        follower_thread.start()
        leader.begin_frame_turn(leader=True)
        leader.finish_frame_turn(leader=True)
        follower_thread.join(timeout=1.0)
        assert not follower_thread.is_alive()
        assert follower_errors == []
        assert follower_result == [0]
        assert leader_core.SB == 1
        assert leader_core.transfer_enabled == 0
        snapshot = leader.debug_snapshot()
        assert snapshot["owner_edge_applied"] == 1
        assert snapshot["frame_acks_received"] == 1
    finally:
        _finish_network_test_cleanup(
            primary=sys.exc_info()[1],
            backends=(("leader", leader), ("follower", follower)),
            threads=(("follower", follower_thread),),
        )


def test_simultaneous_owner_masters_exchange_sampled_bits_without_core_access(monkeypatch):
    """Reciprocal master edges are answered without queueing core work."""
    left, right = NetworkBackend.pair()
    monkeypatch.setattr(network_module, "_EDGE_RESPONSE_TIMEOUT_SECONDS", 0.5)
    start = threading.Barrier(2)
    send_barrier = threading.Barrier(2)
    results: list[tuple[str, int]] = []
    errors: list[BaseException] = []

    for backend in (left, right):
        original_send_frame = backend._send_frame

        def synchronized_send(
            frame,
            *,
            timeout,
            operation,
            cancel_event=None,
            _original=original_send_frame,
        ):
            if operation == "EDGE_REQ":
                send_barrier.wait(timeout=1.0)
            return _original(
                frame,
                timeout=timeout,
                operation=operation,
                cancel_event=cancel_event,
            )

        backend._send_frame = synchronized_send

    def clock(backend: NetworkBackend, label: str, bit: int) -> None:
        try:
            start.wait(timeout=1.0)
            results.append((label, backend.on_edge(our_bit=bit, our_role=1)))
        except BaseException as exc:  # noqa: BLE001 - asserted by test owner
            errors.append(exc)

    left_thread = threading.Thread(target=clock, args=(left, "left", 0), daemon=True)
    right_thread = threading.Thread(target=clock, args=(right, "right", 1), daemon=True)
    try:
        left.start_receiver(
            local_core=_CompletingSlaveCore(),
            serial_gate=SerialOperationGate(),
            dispatch_to_owner=True,
        )
        right.start_receiver(
            local_core=_CompletingSlaveCore(),
            serial_gate=SerialOperationGate(),
            dispatch_to_owner=True,
        )
        left_thread.start()
        right_thread.start()
        left_thread.join(timeout=2.0)
        right_thread.join(timeout=2.0)
        assert not left_thread.is_alive()
        assert not right_thread.is_alive()
        assert errors == []
        assert sorted(results) == [("left", 1), ("right", 0)]
        for backend in (left, right):
            snapshot = backend.debug_snapshot()
            assert snapshot["reciprocal_master_edges"] == 1
            assert snapshot["owner_edge_applied"] == 0
            assert snapshot["pending_edge_requests"] == 0
    finally:
        _finish_network_test_cleanup(
            primary=sys.exc_info()[1],
            backends=(("left", left), ("right", right)),
            threads=(("left master", left_thread), ("right master", right_thread)),
        )


@pytest.mark.parametrize("identified", [False, True])
def test_owner_master_answers_peer_edge_queued_before_local_clock(monkeypatch, identified):
    """A slightly earlier peer clock must not strand both owner threads."""
    sockets = _socket.socketpair()
    left = NetworkBackend(sockets[0], local_rom_version="red" if identified else None)
    right = NetworkBackend(sockets[1], local_rom_version="blue" if identified else None)
    monkeypatch.setattr(network_module, "_EDGE_RESPONSE_TIMEOUT_SECONDS", 1.0)
    results = []
    errors = []

    def clock():
        try:
            results.append(left.on_edge(our_bit=0, our_role=1))
        except BaseException as exc:  # noqa: BLE001 - assert every worker failure below
            errors.append(exc)

    worker = threading.Thread(target=clock, daemon=True)
    original_response = right._send_edge_response

    def respond_after_local_response(request):
        deadline = time.monotonic() + 0.5
        while not right._edge_response_seen:
            assert time.monotonic() < deadline
            time.sleep(0.001)
        return original_response(request)

    monkeypatch.setattr(right, "_send_edge_response", respond_after_local_response)
    try:
        for backend in (left, right):
            backend.start_receiver(
                local_core=_CompletingSlaveCore(),
                serial_gate=SerialOperationGate(),
                dispatch_to_owner=True,
            )
        if identified:
            assert left.wait_for_hello(timeout=1.0) == "blue"
            assert right.wait_for_hello(timeout=1.0) == "red"
        right._local_core.internal_clock = 1
        right._local_core.transfer_enabled = 1
        worker.start()
        deadline = time.monotonic() + 0.5
        while right.debug_snapshot()["pending_edge_requests"] != 1:
            assert time.monotonic() < deadline
            time.sleep(0.001)
        assert right.service_pending_edges(max_edges=1) == 0
        assert right.on_edge(our_bit=1, our_role=1) == 0
        worker.join(timeout=1.0)
        assert not worker.is_alive()
        assert errors == []
        assert results == [1]
        for backend in (left, right):
            snapshot = backend.debug_snapshot()
            assert snapshot["pending_edge_requests"] == 0
            assert snapshot["reciprocal_master_edges"] == 1
            assert snapshot["owner_edge_applied"] == 0
            assert snapshot["edge_req_received"] == 1
            assert snapshot["edge_resp_sent"] == 1
    finally:
        _finish_network_test_cleanup(
            primary=sys.exc_info()[1],
            backends=(("left", left), ("right", right)),
            threads=(("earlier master", worker),),
        )


def test_versioned_simultaneous_masters_match_identified_responses(monkeypatch):
    """HELLO peers use ids when both master edges cross on the wire."""
    left_sock, right_sock = _socket.socketpair()
    left = NetworkBackend(left_sock, local_rom_version="red")
    right = NetworkBackend(right_sock, local_rom_version="blue")
    monkeypatch.setattr(network_module, "_EDGE_RESPONSE_TIMEOUT_SECONDS", 0.5)
    send_barrier = threading.Barrier(2)
    results: list[tuple[str, int]] = []
    errors: list[BaseException] = []

    for backend in (left, right):
        original_send_frame = backend._send_frame

        def synchronized_send(
            frame,
            *,
            timeout,
            operation,
            cancel_event=None,
            _original=original_send_frame,
        ):
            if operation == "EDGE_REQ":
                send_barrier.wait(timeout=1.0)
            return _original(
                frame,
                timeout=timeout,
                operation=operation,
                cancel_event=cancel_event,
            )

        backend._send_frame = synchronized_send

    def clock(backend: NetworkBackend, label: str, bit: int) -> None:
        try:
            results.append((label, backend.on_edge(our_bit=bit, our_role=1)))
        except BaseException as exc:  # noqa: BLE001 - assert worker failures below
            errors.append(exc)

    left_thread = threading.Thread(target=clock, args=(left, "left", 0), daemon=True)
    right_thread = threading.Thread(target=clock, args=(right, "right", 1), daemon=True)
    try:
        left.start_receiver(local_core=None)
        right.start_receiver(local_core=None)
        assert left.wait_for_hello(timeout=1.0) == "blue"
        assert right.wait_for_hello(timeout=1.0) == "red"
        left_thread.start()
        right_thread.start()
        left_thread.join(timeout=2.0)
        right_thread.join(timeout=2.0)
        assert not left_thread.is_alive()
        assert not right_thread.is_alive()
        assert errors == []
        assert sorted(results) == [("left", 1), ("right", 0)]
        for backend in (left, right):
            snapshot = backend.debug_snapshot()
            assert snapshot["edge_id_req_sent"] == 1
            assert snapshot["edge_id_req_received"] == 1
            assert snapshot["edge_id_resp_sent"] == 1
            assert snapshot["edge_id_resp_received"] == 1
            assert snapshot["reciprocal_master_edges"] == 1
            assert snapshot["pending_edge_requests"] == 0
    finally:
        _finish_network_test_cleanup(
            primary=sys.exc_info()[1],
            backends=(("left", left), ("right", right)),
            threads=(("left master", left_thread), ("right master", right_thread)),
        )


def test_versioned_duplicate_edge_request_replays_without_core_application():
    """A late identified REQ gets its prior response without a second edge."""
    backend_sock, peer_sock = _socket.socketpair()
    backend = NetworkBackend(backend_sock, local_rom_version="red")
    peer_sock.settimeout(1.0)

    def recv_exact(size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = peer_sock.recv(remaining)
            if not chunk:
                raise AssertionError("peer socket closed while reading test response")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    try:
        # Drain the backend's constructor HELLO and complete the peer HELLO
        # manually so the backend's reader is the only protocol consumer.
        assert recv_exact(2)[0] == network_module._OP_HELLO
        peer_sock.sendall(network_module._FRAME.pack(network_module._OP_HELLO, 0x22))
        backend.start_receiver(local_core=None)
        assert backend.wait_for_hello(timeout=1.0) == "blue"

        request = network_module._EDGE_ID_FRAME.pack(
            network_module._OP_EDGE_REQ_ID,
            1,
            7,
        )
        peer_sock.sendall(request)
        first = recv_exact(network_module._EDGE_ID_FRAME.size)
        assert network_module._EDGE_ID_FRAME.unpack(first) == (
            network_module._OP_EDGE_RESP_ID,
            1,
            7,
        )
        peer_sock.sendall(request)
        replay = recv_exact(network_module._EDGE_ID_FRAME.size)
        assert network_module._EDGE_ID_FRAME.unpack(replay) == (
            network_module._OP_EDGE_RESP_ID,
            1,
            7,
        )
        assert backend.debug_snapshot()["edge_id_duplicates_replayed"] == 1
        assert backend.debug_snapshot()["owner_edge_applied"] == 0
    finally:
        try:
            _finish_network_test_cleanup(
                primary=sys.exc_info()[1],
                backends=(("backend", backend),),
            )
        finally:
            peer_sock.close()


def test_versioned_late_response_id_fails_closed(monkeypatch):
    """A response for an earlier identified edge cannot satisfy this edge."""
    backend_sock, peer_sock = _socket.socketpair()
    backend = NetworkBackend(backend_sock, local_rom_version="red")
    peer_sock.settimeout(1.0)
    monkeypatch.setattr(network_module, "_EDGE_RESPONSE_TIMEOUT_SECONDS", 0.5)
    result: list[BaseException] = []

    try:
        # Complete the optional handshake with a hand-rolled peer.
        assert peer_sock.recv(2)[0] == network_module._OP_HELLO
        peer_sock.sendall(network_module._FRAME.pack(network_module._OP_HELLO, 0x22))
        backend.start_receiver(local_core=None)
        assert backend.wait_for_hello(timeout=1.0) == "blue"

        def run_edge() -> None:
            try:
                backend.on_edge(our_bit=1, our_role=1)
            except BaseException as exc:  # noqa: BLE001 - assert worker failure below
                result.append(exc)

        worker = threading.Thread(target=run_edge, daemon=True)
        worker.start()
        raw_request = peer_sock.recv(network_module._EDGE_ID_FRAME.size)
        opcode, bit, edge_id = network_module._EDGE_ID_FRAME.unpack(raw_request)
        assert opcode == network_module._OP_EDGE_REQ_ID
        assert bit == 1
        peer_sock.sendall(
            network_module._EDGE_ID_FRAME.pack(
                network_module._OP_EDGE_RESP_ID,
                0,
                edge_id + 1,
            )
        )
        worker.join(timeout=1.0)
        assert not worker.is_alive()
        assert result and isinstance(result[0], NetworkBackendError)
        assert backend._reader_exc is not None
        assert "does not match in-flight id" in str(backend._reader_exc)
        assert not backend.connected
    finally:
        try:
            _finish_network_test_cleanup(
                primary=sys.exc_info()[1],
                backends=(("backend", backend),),
                threads=(("identified edge", locals().get("worker")),)
                if "worker" in locals()
                else (),
            )
        finally:
            peer_sock.close()


def test_leader_frame_ack_wait_can_be_cancelled_with_bounded_cleanup():
    """A cancelled ACK wait closes and wakes without a long frame timeout."""
    leader, follower = NetworkBackend.pair()
    frame_done_sent = threading.Event()
    cancel = threading.Event()
    result: list[BaseException] = []
    original_send_frame = leader._send_frame

    def observe_frame_done(frame, *, timeout, operation, cancel_event=None):
        result_frame = original_send_frame(
            frame,
            timeout=timeout,
            operation=operation,
            cancel_event=cancel_event,
        )
        if frame[0] == network_module._OP_FRAME_DONE:
            frame_done_sent.set()
        return result_frame

    def wait_for_ack() -> None:
        try:
            leader.finish_frame_turn(leader=True, cancel_event=cancel)
        except BaseException as exc:  # noqa: BLE001 - assert cancellation below
            result.append(exc)

    # Do not let the test's cancellation race the FRAME_DONE send itself.
    leader._send_frame = observe_frame_done
    worker = threading.Thread(target=wait_for_ack, daemon=True)
    try:
        leader.start_receiver(local_core=None)
        follower.start_receiver(local_core=None)
        leader.begin_frame_turn(leader=True)
        worker.start()
        assert frame_done_sent.wait(timeout=1.0)
        started = time.monotonic()
        cancel.set()
        worker.join(timeout=1.0)
        assert not worker.is_alive()
        assert result and isinstance(result[0], NetworkBackendError)
        assert "cancelled" in str(result[0]).lower()
        assert time.monotonic() - started < 0.5
        assert not leader.connected
    finally:
        cancel.set()
        _finish_network_test_cleanup(
            primary=sys.exc_info()[1],
            backends=(("leader", leader), ("follower", follower)),
            threads=(("ACK waiter", worker),),
        )


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
                "call",
                "admission-response",
                "write",
                "send",
                "response",
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
