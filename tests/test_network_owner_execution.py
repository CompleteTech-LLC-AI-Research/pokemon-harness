"""Owner-thread execution contracts for the network serial backend."""

from __future__ import annotations

import queue
import sys
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


def _send_and_service(
    master,
    slave,
    *,
    service_thread=None,
    qsize_observed=None,
    allow_probe=None,
    sender_ref=None,
):
    result: list[object] = []

    def send() -> None:
        try:
            result.append(master.on_edge(1, 1))
        except BaseException as exc:  # noqa: BLE001 - asserted by caller
            result.append(exc)

    sender = threading.Thread(target=send, daemon=True)
    if sender_ref is not None:
        sender_ref.append(sender)
    sender_started = False
    try:
        sender.start()
        sender_started = True
        deadline = time.monotonic() + 1.0
        _wait_for_owner_queue(
            slave,
            deadline=deadline,
            qsize_observed=qsize_observed,
            allow_probe=allow_probe,
        )
        if service_thread is None:
            assert slave.service_pending_edges(max_edges=1) == 1
        else:
            service_thread.start()
            service_thread.join(timeout=1.0)
            assert not service_thread.is_alive()
        sender.join(timeout=1.0)
        assert not sender.is_alive()
        return result
    finally:
        # A failure while the helper is waiting for the deterministic queue
        # probe must release that test gate before transport teardown.  This
        # keeps an assertion in the caller from leaving the helper thread
        # parked until its full deadline.
        if allow_probe is not None:
            allow_probe.set()
        if sender_started and sender.is_alive():
            # A wait/service assertion can leave the nested sender blocked in
            # on_edge. Close both ends before the bounded join so cleanup
            # wakes that sender promptly. Preserve the active assertion or
            # transport error if either stop fails, but record every cleanup
            # failure and an unresolved nested thread instead of silently
            # leaking it.
            primary_error = sys.exc_info()[1]
            stop_failures: list[tuple[str, BaseException]] = []
            for operation, endpoint in (
                ("master.stop", master),
                ("slave.stop", slave),
            ):
                try:
                    stopped = endpoint.stop(timeout_s=1.0)
                    if stopped is False:
                        stop_failures.append(
                            (
                                operation,
                                RuntimeError(
                                    f"{operation} returned False before nested sender join"
                                ),
                            )
                        )
                except BaseException as exc:  # noqa: BLE001 - teardown is best effort
                    stop_failures.append((operation, exc))
            sender.join(timeout=1.0)
            sender_alive = sender.is_alive()

            if primary_error is not None:
                for operation, error in stop_failures:
                    primary_error.add_note(
                        f"nested cleanup {operation} failed: "
                        f"{type(error).__name__}: {error}"
                    )
            elif stop_failures:
                # This branch is defensive: the sender should only still be
                # alive while unwinding an active assertion, but cleanup
                # failures must remain observable even if that invariant is
                # changed by a future helper refactor.
                raise BaseExceptionGroup(
                    "nested sender cleanup failed",
                    [error for _operation, error in stop_failures],
                )

            if sender_alive:
                leak_note = (
                    "nested sender remained alive after bounded cleanup join; "
                    "transport cleanup did not prove quiescence"
                )
                if primary_error is not None:
                    primary_error.add_note(leak_note)
                for _operation, error in stop_failures:
                    error.add_note(leak_note)


def _wait_for_owner_queue(
    slave,
    *,
    count=1,
    deadline,
    qsize_observed=None,
    allow_probe=None,
):
    """Wait until admitted-work accounting and the owner queue agree.

    ``pending_edge_requests`` is published before the reader's queue put, so
    it is not by itself a dispatch-ready signal.  The owner queue has no
    competing consumer in these owner-dispatch tests; once its size reaches
    ``count``, service_pending_edges can deterministically dequeue the work.
    """
    while True:
        snapshot = slave.debug_snapshot()
        pending = snapshot["pending_edge_requests"]
        queue_size = slave._edge_queue.qsize()
        if pending >= count and queue_size >= count:
            return
        if qsize_observed is not None and pending >= count and queue_size < count:
            qsize_observed.set()
            if allow_probe is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not allow_probe.wait(timeout=remaining):
                    raise AssertionError("owner queue probe was not released")
        assert time.monotonic() < deadline, "owner dispatch did not receive queued EDGE_REQ"
        time.sleep(0.001)


@pytest.mark.parametrize("leader", [False, True], ids=["follower", "leader"])
@pytest.mark.parametrize("publication", ["while_waiting", "before_wait"])
def test_frame_owner_wakes_for_published_edge_without_polling(monkeypatch, leader, publication):
    """Force publication both during a wait and after an empty owner probe."""
    # Make polling unable to satisfy the test. Events establish the exact
    # interleaving; bounded waits only detect failure and keep cleanup finite.
    monkeypatch.setattr(network_module, "_SEND_POLL_SECONDS", 5.0)
    core = _Core()
    peer, owner = _start_owner_pair(core)
    put_entered = threading.Event()
    release_put = threading.Event()
    published = threading.Event()
    probed = threading.Event()
    release_probe = threading.Event()
    waiting = threading.Event()
    applied = threading.Event()
    cleanup = threading.Event()
    owner_result: list[object] = []
    edge_result: list[object] = []
    original_put = owner._edge_queue.put_nowait
    original_wait = owner._edge_pending_condition.wait
    original_ack_get = owner._frame_ack_queue.get

    def paused_put(item):
        put_entered.set()
        assert release_put.wait(timeout=2.0)
        original_put(item)
        published.set()

    def observed_wait(timeout=None):
        waiting.set()
        return original_wait(timeout=timeout)

    def observed_ack_get(block=True, timeout=None):
        if block:
            waiting.set()
        return original_ack_get(block=block, timeout=timeout)

    def progress():
        if cleanup.is_set():
            raise NetworkBackendError("test cleanup")
        count = owner.service_pending_edges(max_edges=1)
        if count:
            applied.set()
        if not probed.is_set():
            probed.set()
            if publication == "before_wait":
                assert release_probe.wait(timeout=2.0)

    def finish():
        try:
            owner.finish_frame_turn(leader=leader, progress_callback=progress)
            owner_result.append(None)
        except BaseException as exc:  # noqa: BLE001 - asserted after join
            owner_result.append(exc)

    def send():
        try:
            edge_result.append(peer.on_edge(1, 1))
        except BaseException as exc:  # noqa: BLE001 - asserted after join
            edge_result.append(exc)

    monkeypatch.setattr(owner._edge_queue, "put_nowait", paused_put)
    monkeypatch.setattr(owner._edge_pending_condition, "wait", observed_wait)
    monkeypatch.setattr(owner._frame_ack_queue, "get", observed_ack_get)
    finisher = threading.Thread(target=finish, daemon=True)
    sender = threading.Thread(target=send, daemon=True)
    started: list[threading.Thread] = []
    try:
        pacing_leader, follower = (owner, peer) if leader else (peer, owner)
        pacing_leader.begin_frame_turn(leader=True)
        follower.begin_frame_turn(leader=False)
        if publication == "while_waiting":
            sender.start()
            started.append(sender)
            assert put_entered.wait(timeout=1.0)
            assert owner.debug_snapshot()["pending_edge_requests"] == 1
            assert owner._edge_queue.empty()
            finisher.start()
            started.append(finisher)
            assert waiting.wait(timeout=1.0)
            release_put.set()
        else:
            finisher.start()
            started.append(finisher)
            assert probed.wait(timeout=1.0)
            sender.start()
            started.append(sender)
            assert put_entered.wait(timeout=1.0)
            release_put.set()
            assert published.wait(timeout=1.0)
            release_probe.set()

        assert published.wait(timeout=1.0)
        assert applied.wait(timeout=1.0), "available owner work required a polling timeout"
        sender.join(timeout=1.0)
        assert not sender.is_alive()
        assert edge_result == [0]
        peer.finish_frame_turn(leader=not leader, progress_callback=lambda: None)
        finisher.join(timeout=1.0)
        assert not finisher.is_alive()
        assert owner_result == [None]
        assert core.calls == 1
        assert core.call_threads == [finisher.ident, finisher.ident]
        assert owner.debug_snapshot()["pending_edge_requests"] == 0
    finally:
        cleanup.set()
        release_put.set()
        release_probe.set()
        owner.stop(timeout_s=1.0)
        peer.stop(timeout_s=1.0)
        # Release the baseline's queue-only waits even on assertion failure.
        for response_queue, value in (
            (owner._frame_ack_queue, None),
            (peer._resp_queue, 0),
        ):
            try:
                response_queue.put_nowait(value)
            except queue.Full:
                pass
        for thread in started:
            thread.join(timeout=1.0)
            assert not thread.is_alive(), f"test leaked {thread.name}"


def test_owner_dispatch_waits_for_queue_put_after_pending_publication(monkeypatch):
    """Pending accounting alone must not let the owner helper dispatch early."""
    core = _Core()
    master, slave = _start_owner_pair(core)
    put_entered = threading.Event()
    release_put = threading.Event()
    qsize_observed = threading.Event()
    allow_probe = threading.Event()
    helper_done = threading.Event()
    outcome: list[object] = []
    original_put_nowait = slave._edge_queue.put_nowait

    def paused_put(item):
        put_entered.set()
        assert release_put.wait(timeout=1.0), "test queue gate was not released"
        return original_put_nowait(item)

    monkeypatch.setattr(slave._edge_queue, "put_nowait", paused_put)

    def run_helper() -> None:
        try:
            outcome.append(
                _send_and_service(
                    master,
                    slave,
                    qsize_observed=qsize_observed,
                    allow_probe=allow_probe,
                )
            )
        except BaseException as exc:  # noqa: BLE001 - asserted by caller
            outcome.append(exc)
        finally:
            helper_done.set()

    helper = threading.Thread(target=run_helper, daemon=True)
    helper_started = False
    try:
        helper.start()
        helper_started = True
        assert put_entered.wait(timeout=1.0)
        assert qsize_observed.wait(timeout=1.0)
        assert not helper_done.is_set()
        assert slave.debug_snapshot()["pending_edge_requests"] == 1
        assert slave._edge_queue.empty()

        allow_probe.set()
        release_put.set()
        helper.join(timeout=1.0)
        assert not helper.is_alive()
        assert outcome == [[0]]
        assert core.calls == 1
    finally:
        release_put.set()
        allow_probe.set()
        try:
            master.stop(timeout_s=1.0)
        finally:
            try:
                slave.stop(timeout_s=1.0)
            finally:
                if helper_started:
                    helper.join(timeout=1.0)
                    assert not helper.is_alive()


def test_nested_sender_cleanup_records_stop_faults_without_masking_primary(monkeypatch) -> None:
    """A faulted cleanup must release gates and leave no hidden sender thread."""
    sender_entered = threading.Event()
    release_sender = threading.Event()
    sender_done = threading.Event()
    allow_probe = threading.Event()
    primary = AssertionError("owner queue wait failed")
    sender_ref: list[threading.Thread] = []

    class _BlockedMaster:
        def on_edge(self, _our_bit, _our_role):
            sender_entered.set()
            try:
                assert release_sender.wait(timeout=5.0)
                return 0
            finally:
                sender_done.set()

        def stop(self, *, timeout_s):
            del timeout_s
            raise RuntimeError("master stop fault")

    class _BlockedSlave:
        def stop(self, *, timeout_s):
            del timeout_s
            raise RuntimeError("slave stop fault")

    master = _BlockedMaster()
    slave = _BlockedSlave()

    def fail_wait(*_args, **_kwargs):
        assert sender_entered.wait(timeout=1.0)
        raise primary

    # pytest owns restoration even if an assertion in this regression test
    # fails while the deliberately blocked sender is being released.
    monkeypatch.setitem(globals(), "_wait_for_owner_queue", fail_wait)
    try:
        with pytest.raises(AssertionError, match="owner queue wait failed") as raised:
            _send_and_service(
                master,
                slave,
                allow_probe=allow_probe,
                sender_ref=sender_ref,
            )
        assert raised.value is primary
        notes = "\n".join(raised.value.__notes__)
        assert "nested cleanup master.stop failed" in notes
        assert "nested cleanup slave.stop failed" in notes
        assert "nested sender remained alive after bounded cleanup join" in notes
        assert allow_probe.is_set()

        # The intentionally faulted stop hooks above cannot wake the blocked
        # sender. Release its final gate explicitly, then prove the helper's
        # daemon thread actually exits before this test returns.
        release_sender.set()
        assert sender_ref
        sender_ref[0].join(timeout=1.0)
        assert not sender_ref[0].is_alive()
        assert sender_done.wait(timeout=1.0)
    finally:
        allow_probe.set()
        release_sender.set()
        if sender_ref:
            sender_ref[0].join(timeout=1.0)
            assert not sender_ref[0].is_alive()
        assert sender_done.wait(timeout=1.0)


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
        _wait_for_owner_queue(slave, deadline=deadline)
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
            _wait_for_owner_queue(slave, deadline=deadline)
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
        _wait_for_owner_queue(slave, count=2, deadline=deadline)
        for _ in range(deferrals):
            assert slave.service_pending_edges(max_edges=1) == 0
        assert core.applied_bits == []
        core.transfer_enabled = 1
        assert slave.service_pending_edges(max_edges=1) == 1
        assert core.applied_bits == [1]
    finally:
        master.stop(timeout_s=1.0)
        slave.stop(timeout_s=1.0)
