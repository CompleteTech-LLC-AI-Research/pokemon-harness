"""Sync rendezvous, wire-idle accounting and owner-dispatch ordering (#128).

Split from ``tests/test_network_backend.py`` for #128 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module.
"""

from __future__ import annotations

import threading
import time

import pytest

from pokered_harness.link.network_backend import (
    NetworkBackend,
    NetworkBackendError,
)
from pokered_harness.link.serial_coordinator import SerialOperationGate
from tests._network_backend_support import (
    _CompletingSlaveCore,
)


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


def test_owner_completion_and_irq_precede_blocked_edge_response(monkeypatch):
    """An eighth external edge is locally complete before TCP can reply.

    A TCP response is flow control for the peer's clock edge, not part of
    the local Game Boy's shift-register completion.  Keep its write blocked
    until after the owner has applied the edge, then prove SB/SC-equivalent
    core state and the owner IRQ are already observable.
    """
    master, slave = NetworkBackend.pair()
    core = _CompletingSlaveCore()
    irq_calls: list[str] = []
    response_write_started = threading.Event()
    release_response_write = threading.Event()
    sender_result: list[int] = []
    sender_errors: list[BaseException] = []
    original_send_frame = slave._send_frame

    def blocked_response_write(frame, *, timeout, operation, cancel_event=None):
        if operation == "EDGE_RESP":
            response_write_started.set()
            assert release_response_write.wait(timeout=1.0), "test response release timed out"
        return original_send_frame(
            frame,
            timeout=timeout,
            operation=operation,
            cancel_event=cancel_event,
        )

    def send_master_edge() -> None:
        try:
            sender_result.append(master.on_edge(our_bit=1, our_role=1))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            sender_errors.append(exc)

    monkeypatch.setattr(slave, "_send_frame", blocked_response_write)
    master.start_receiver(local_core=None)
    slave.start_receiver(
        local_core=core,
        irq_callback=lambda: irq_calls.append("serial"),
        serial_gate=SerialOperationGate(),
        dispatch_to_owner=True,
    )
    sender = threading.Thread(target=send_master_edge, daemon=True)
    try:
        sender.start()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and slave.debug_snapshot()["pending_edge_requests"] == 0:
            time.sleep(0.005)
        assert slave.debug_snapshot()["pending_edge_requests"] == 1

        assert slave.service_pending_edges(max_edges=1) == 1
        assert response_write_started.wait(timeout=1.0)
        # The response thread is stuck in sendall, but the completed eighth
        # edge has already latched data, disarmed the slave, and requested
        # the owner-side serial IRQ.
        assert core.SB == 1
        assert core.transfer_enabled == 0
        assert irq_calls == ["serial"]
        assert slave.debug_snapshot()["irq_callbacks"] == 1
        assert sender.is_alive(), "master unexpectedly received a blocked response"

        release_response_write.set()
        sender.join(timeout=1.0)
        assert not sender.is_alive()
        assert sender_errors == []
        assert sender_result == [0]
        slave.wait_for_wire_idle(timeout=1.0)
    finally:
        release_response_write.set()
        sender.join(timeout=1.0)
        master.stop()
        slave.stop()


def test_next_edge_waits_for_response_accounting_to_finish(monkeypatch):
    """A peer's next edge cannot overtake the prior pending decrement."""
    master, slave = NetworkBackend.pair()
    core = _CompletingSlaveCore()
    # This test exercises response-accounting ordering after an admitted
    # external-clock edge. An armed internal-clock core now deliberately
    # defers a peer edge until its own scheduled clock samples the wire.
    core.internal_clock = 0
    response_decrement_entered = threading.Event()
    response_decrement_done = threading.Event()
    release_response_decrement = threading.Event()
    # Set when the *reader* thread reaches the inbound-EDGE_REQ admission
    # critical section. This is the deterministic synchronization point: the
    # reader must be provably blocked entering the gate while the previous
    # response completion is still pending, rather than relying on a timed
    # observation window that can pass before the next request even arrives.
    reader_attempted_admission = threading.Event()
    original_decrement = slave._decrement_edge_pending
    decrement_calls = 0
    decrement_lock = threading.Lock()

    def delayed_decrement() -> None:
        nonlocal decrement_calls
        with decrement_lock:
            decrement_calls += 1
            call_number = decrement_calls
        if call_number == 1:
            response_decrement_entered.set()
            # Never assert inside this daemon worker: a load-induced timeout
            # would fail the test through a dead worker instead of a clear
            # assertion, and the release below is driven by the main thread.
            release_response_decrement.wait(timeout=30.0)
        original_decrement()
        if call_number == 1:
            response_decrement_done.set()

    monkeypatch.setattr(slave, "_decrement_edge_pending", delayed_decrement)

    real_wire_lock = slave._edge_wire_completion_lock

    class _ObservedWireLock:
        """Wire completion lock that signals the reader's admission attempt."""

        def __enter__(self):
            if threading.current_thread().name == "NetworkBackend.reader":
                reader_attempted_admission.set()
            real_wire_lock.acquire()
            return self

        def __exit__(self, *_exc):
            real_wire_lock.release()
            return False

    monkeypatch.setattr(slave, "_edge_wire_completion_lock", _ObservedWireLock())
    master.start_receiver(local_core=None)
    slave.start_receiver(
        local_core=core,
        serial_gate=SerialOperationGate(),
        dispatch_to_owner=True,
    )
    first_reply: list[int] = []
    first_errors: list[BaseException] = []

    def append_edge_result(reply: list[int], errors: list[BaseException]) -> None:
        try:
            reply.append(master.on_edge(our_bit=1, our_role=1))
        except BaseException as exc:  # noqa: BLE001 - assert worker failures below
            errors.append(exc)

    first_sender = threading.Thread(
        target=lambda: append_edge_result(first_reply, first_errors),
        daemon=True,
    )
    second_reply: list[int] = []
    second_errors: list[BaseException] = []
    second_sender = threading.Thread(
        target=lambda: append_edge_result(second_reply, second_errors),
        daemon=True,
    )
    try:
        first_sender.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and slave.debug_snapshot()["pending_edge_requests"] == 0:
            time.sleep(0.005)
        assert slave.debug_snapshot()["pending_edge_requests"] == 1
        assert slave.service_pending_edges(max_edges=1) == 1
        assert response_decrement_entered.wait(timeout=10.0)
        first_sender.join(timeout=10.0)
        assert not first_sender.is_alive()
        assert first_errors == []
        assert first_reply == [0]

        # The first response is already on the wire, so the peer immediately
        # starts its next edge. The worker is deliberately paused after that
        # write. The reader must reach the admission gate and block there
        # instead of exposing two admitted requests; wait for that attempt
        # before asserting the pending count is still one.
        reader_attempted_admission.clear()
        second_sender.start()
        assert reader_attempted_admission.wait(
            timeout=10.0
        ), "reader never attempted inbound EDGE_REQ admission"
        assert slave.debug_snapshot()["pending_edge_requests"] == 1

        release_response_decrement.set()
        assert response_decrement_done.wait(timeout=10.0)
        # The first completed external edge disarms this test double, as
        # authentic hardware does. Model the ROM's intervening re-arm before
        # admitting the next peer clock.
        core.transfer_enabled = 1
        deadline = time.monotonic() + 10.0
        applied = 0
        while time.monotonic() < deadline and applied == 0:
            applied = slave.service_pending_edges(max_edges=1)
            if applied == 0:
                time.sleep(0.005)
        assert applied == 1
        second_sender.join(timeout=10.0)
        assert not second_sender.is_alive()
        assert second_errors == []
        assert second_reply == [0]
        slave.wait_for_wire_idle(timeout=10.0)
        assert slave.debug_snapshot()["pending_edge_requests"] == 0
    finally:
        release_response_decrement.set()
        if first_sender.ident is not None:
            first_sender.join(timeout=10.0)
        if second_sender.ident is not None:
            second_sender.join(timeout=10.0)
        master.stop()
        slave.stop()


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
    core.transfer_enabled = 0
    core.internal_clock = 1
    core.SC = 0x01
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
    core.transfer_enabled = 0
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
        applied = 0
        while time.monotonic() < deadline and applied == 0:
            applied = b.service_pending_edges(max_edges=1)
            if applied == 0:
                time.sleep(0.005)
        assert applied == 1
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
        core.transfer_enabled = 0
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
