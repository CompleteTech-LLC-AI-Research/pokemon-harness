"""Exclusive native pump admission and network lifecycle boundaries."""

from __future__ import annotations

import threading

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
    slave.start_receiver(local_core=core, irq_callback=lambda: (_ for _ in ()).throw(ValueError("fault")))
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
