"""Exclusive native pump admission and network lifecycle boundaries."""

from __future__ import annotations

import threading
import time

import pytest
from pyboy.core.serial import Serial, SerialBackendError

from pokered_harness.link.network_backend import NetworkBackend
from pokered_harness.link.serial_coordinator import SerialOperationGate

pytestmark = [pytest.mark.unit, pytest.mark.timing_sensitive]


def armed() -> Serial:
    core = Serial(False)
    core.set_SB(0xA5)
    core.set_SC(0x81)
    return core


def test_unclaimed_setter_keeps_replacement_semantics() -> None:
    core, old, new = armed(), [], []
    core.set_owner_pump(old.append)
    core.set_owner_pump(new.append)
    core.tick(512)
    assert not old and len(new) == 1
    core.set_owner_pump(None)
    core.tick(1024)
    assert len(new) == 1


def test_claim_rejects_replacement_and_wrong_release_without_mutation() -> None:
    core, seen = armed(), []
    token = core.claim_owner_pump(seen.append, poll=True)
    for callback in (None, lambda event: None):
        with pytest.raises(RuntimeError, match="exclusively claimed"):
            core.set_owner_pump(callback)
    for bad in (None, object()):
        with pytest.raises(RuntimeError, match="invalid.*token"):
            core.release_owner_pump(bad)
    errors: list[str] = []

    def wrong_thread() -> None:
        try:
            core.release_owner_pump(token)
        except RuntimeError as exc:
            errors.append(str(exc))

    worker = threading.Thread(target=wrong_thread)
    worker.start()
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert errors == ["serial owner pump release off owner thread"]
    core.tick(512)
    assert len(seen) == 1 and core.owner_poll_enabled
    core.release_owner_pump(token)
    assert not core.owner_poll_enabled
    core.set_owner_pump(None)


def test_release_after_fault_preserves_first_cause() -> None:
    core = armed()
    cause = ValueError("first cable fault")

    def fail(event):
        raise cause

    token = core.claim_owner_pump(fail, poll=True)
    core.tick(512)
    core.release_owner_pump(token)
    assert core.backend_failed and not core.owner_poll_enabled
    with pytest.raises(SerialBackendError) as caught:
        core.check_error()
    assert caught.value.__cause__ is cause


def test_callback_cannot_release_or_replace_active_binding() -> None:
    core, rejected = armed(), []

    def callback(event):
        for action in (
            lambda: core.release_owner_pump(token),
            lambda: core.set_owner_pump(None),
            lambda: core.claim_owner_pump(callback),
        ):
            with pytest.raises(RuntimeError, match="recursive CPU execution"):
                action()
            rejected.append(True)

    token = core.claim_owner_pump(callback)
    core.tick(512)
    core.check_error()
    assert rejected == [True] * 3
    core.release_owner_pump(token)


def test_concurrent_claims_have_exactly_one_winner() -> None:
    core = Serial(False)
    start, attempted = threading.Barrier(2), threading.Barrier(2)
    won, lost, errors = [], [], []

    def compete() -> None:
        token = None
        try:
            start.wait(timeout=2.0)
            try:
                token = core.claim_owner_pump(lambda event: None)
                won.append(threading.get_ident())
            except RuntimeError as exc:
                lost.append(str(exc))
            attempted.wait(timeout=2.0)
            if token is not None:
                core.release_owner_pump(token)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    workers = [threading.Thread(target=compete) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=3.0)
        assert not worker.is_alive()
    assert not errors and len(won) == len(lost) == 1
    assert "already installed or claimed" in lost[0]


def test_network_owner_dispatch_requires_shared_gate_and_has_no_native_pump_claim() -> None:
    core = armed()
    backend, peer = NetworkBackend.pair()
    try:
        with pytest.raises(ValueError, match="serial_gate"):
            backend.start_receiver(core, dispatch_to_owner=True)
        assert backend._reader is None and backend._edge_worker is None

        backend.start_receiver(core, serial_gate=SerialOperationGate(), dispatch_to_owner=True)
        assert backend._dispatch_to_owner is True
        assert backend._local_core is core
    finally:
        backend.stop(timeout_s=1.0)
        peer.stop(timeout_s=1.0)


def test_network_coreless_receiver_is_transport_only() -> None:
    backend, peer = NetworkBackend.pair()
    try:
        backend.start_receiver(local_core=None)
        assert backend._local_core is None
        assert backend._dispatch_to_owner is False
        assert backend.wait_for_wire_idle(timeout=0.1) is None
    finally:
        backend.stop(timeout_s=1.0)
        peer.stop(timeout_s=1.0)


def test_network_callback_failure_isolated_after_one_native_application() -> None:
    class Core:
        transfer_enabled = 1
        internal_clock = 0
        calls = 0

        def peek_out_bit(self):
            return 0

        def apply_external_edge(self, _bit):
            self.calls += 1
            return True

    core = Core()
    master, slave = NetworkBackend.pair()
    master.start_receiver(local_core=None)
    slave.start_receiver(
        local_core=core, irq_callback=lambda: (_ for _ in ()).throw(ValueError("fault"))
    )
    result: list[object] = []

    def send() -> None:
        try:
            result.append(master.on_edge(1, 1))
        except BaseException as exc:  # noqa: BLE001
            result.append(exc)

    sender = threading.Thread(target=send, daemon=True)
    sender.start()
    try:
        sender.join(timeout=1.0)
        assert not sender.is_alive()
        assert core.calls == 1
        # The compatibility worker isolates callback faults so that a local
        # serial completion is not turned into a wire-level replay. The
        # outcome is retained in diagnostics while the response still
        # releases the peer's one admitted edge.
        assert result == [0]
        assert slave.connected
        assert slave.debug_snapshot()["irq_callback_errors"] == 1
    finally:
        sender.join(timeout=1.0)
        master.stop(timeout_s=1.0)
        slave.stop(timeout_s=1.0)


def test_network_dispatch_preserves_existing_native_callback(monkeypatch) -> None:
    core = Serial(False)
    core.set_SB(0xA5)
    core.set_SC(0x80)
    callback_threads: list[int] = []

    def callback() -> None:
        callback_threads.append(threading.get_ident())

    core.owner_dispatch_callback = callback
    gate = SerialOperationGate()
    master, backend = NetworkBackend.pair()
    master.start_receiver(local_core=None)
    backend.start_receiver(
        local_core=core,
        irq_callback=callback,
        serial_gate=gate,
        dispatch_to_owner=True,
    )

    def send_and_service() -> int:
        result: list[object] = []

        def send() -> None:
            try:
                result.append(master.on_edge(1, 1))
            except BaseException as exc:  # noqa: BLE001 - assert outside thread
                result.append(exc)

        sender = threading.Thread(target=send, daemon=True)
        sender.start()
        deadline = time.monotonic() + 1.0
        while backend._edge_queue.empty():
            assert time.monotonic() < deadline, "owner request was not queued"
            time.sleep(0.001)
        assert backend.service_pending_edges(max_edges=1) == 1
        sender.join(timeout=1.0)
        assert not sender.is_alive()
        assert len(result) == 1 and type(result[0]) is int
        return result[0]

    gate_held = threading.Event()
    release_gate = threading.Event()
    sender_results: list[object] = []
    owner_results: list[int] = []
    owner_errors: list[BaseException] = []

    def hold_gate() -> None:
        with gate:
            gate_held.set()
            assert release_gate.wait(timeout=2.0)

    def send_final_edge() -> None:
        try:
            sender_results.append(master.on_edge(1, 1))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            sender_results.append(exc)

    def service_final_edge() -> None:
        try:
            owner_results.append(backend.service_pending_edges(max_edges=1))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            owner_errors.append(exc)

    holder = threading.Thread(target=hold_gate, daemon=True)
    sender = threading.Thread(target=send_final_edge, daemon=True)
    service = threading.Thread(target=service_final_edge, daemon=True)
    try:
        assert backend._local_core is core
        assert backend._irq_callback is callback
        assert core.owner_dispatch_callback is callback
        for _ in range(7):
            send_and_service()
            assert callback_threads == []

        holder.start()
        assert gate_held.wait(timeout=1.0)
        sender.start()
        deadline = time.monotonic() + 1.0
        while backend._edge_queue.empty():
            assert time.monotonic() < deadline, "final owner request was not queued"
            time.sleep(0.001)
        service.start()
        time.sleep(0.01)
        assert callback_threads == []
        assert core.owner_dispatch_callback is callback

        release_gate.set()
        service.join(timeout=1.0)
        sender.join(timeout=1.0)
        holder.join(timeout=1.0)
        assert not service.is_alive() and not sender.is_alive() and not holder.is_alive()
        assert owner_errors == []
        assert owner_results == [1]
        assert len(sender_results) == 1 and type(sender_results[0]) is int
        assert callback_threads == [service.ident]
    finally:
        release_gate.set()
        if holder.ident is not None:
            holder.join(timeout=1.0)
        if service.ident is not None:
            service.join(timeout=1.0)
        master.stop(timeout_s=1.0)
        backend.stop(timeout_s=1.0)
    assert core.owner_dispatch_callback is callback


@pytest.mark.parametrize(
    "shape",
    (
        pytest.param("setter-only-without-gate", id="setter-only-without-gate"),
        pytest.param("attached-core-without-claim", id="attached-core-without-claim"),
    ),
)
def test_network_dispatch_core_shape_admission_uses_shared_gate_not_claim_api(
    shape, monkeypatch
) -> None:
    backend, peer = NetworkBackend.pair()

    class AttachedCore:
        transfer_enabled = 1
        internal_clock = 0
        SB = 0
        SC = 0x80

        def peek_out_bit(self) -> int:
            return 0

        def apply_external_edge(self, _bit: int) -> bool:
            return False

    try:
        if shape == "setter-only-without-gate":

            class SetterOnly:
                def __init__(self) -> None:
                    self.setter_calls = 0

                def set_owner_pump(self, _callback) -> None:
                    self.setter_calls += 1

            core = SetterOnly()
            with pytest.raises(ValueError, match="serial_gate"):
                backend.start_receiver(core, dispatch_to_owner=True)
            assert core.setter_calls == 0
            assert backend._local_core is None
            assert backend._reader is None and backend._edge_worker is None
        else:
            core = AttachedCore()
            gate = SerialOperationGate()
            backend.start_receiver(core, serial_gate=gate, dispatch_to_owner=True)
            assert not hasattr(core, "claim_owner_pump")
            assert backend._serial_gate is gate
            assert backend._local_core is core
            assert backend._dispatch_to_owner is True
            assert backend.stop(timeout_s=1.0) is True
    finally:
        backend.stop(timeout_s=1.0)
        peer.stop(timeout_s=1.0)
