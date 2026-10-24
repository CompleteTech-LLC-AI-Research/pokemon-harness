"""Reference/lifecycle tests for ``NetworkBackend.detach_local_core``."""

from __future__ import annotations

import gc
import threading
import time
import weakref

import pytest

from pokered_harness.link.network_backend import NetworkBackend, NetworkBackendError
from pokered_harness.link.serial_coordinator import SerialOperationGate

pytestmark = pytest.mark.timing_sensitive


class _BlockingCore:
    transfer_enabled = 1
    internal_clock = 0
    SB = 0
    SC = 0x80

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def peek_out_bit(self) -> int:
        return 0

    def apply_external_edge(self, _peer_bit: int) -> bool:
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=2.0)
        return False


class _CompletingCore:
    transfer_enabled = 1
    internal_clock = 0
    SB = 0
    SC = 0x80

    def __init__(self) -> None:
        self.calls = 0

    def peek_out_bit(self) -> int:
        return 0

    def apply_external_edge(self, _peer_bit: int) -> bool:
        self.calls += 1
        return True


def test_detach_local_core_clears_all_emulator_owned_references() -> None:
    backend, peer = NetworkBackend.pair()
    core = _BlockingCore()
    callback = lambda core=core: core
    provider = lambda core=core: {"pc": id(core)}
    core_ref = weakref.ref(core)
    backend.set_serial_transcript_context_provider(provider)
    backend.start_receiver(local_core=core, irq_callback=callback)

    try:
        assert backend.detach_local_core(timeout_s=0.2) is True
        assert backend._local_core is None
        assert backend._irq_callback is None
        assert backend._serial_transcript_context_provider is None
        assert backend._local_core_detached is True
        del callback, provider, core
        gc.collect()
        assert core_ref() is None
    finally:
        backend.stop()
        peer.stop()


def test_detach_local_core_timeout_preserves_attachment_and_can_retry() -> None:
    master, slave = NetworkBackend.pair()
    core = _BlockingCore()
    master.start_receiver(local_core=None)
    slave.start_receiver(local_core=core)
    edge_errors: list[BaseException] = []

    def send_edge() -> None:
        try:
            master.on_edge(our_bit=1, our_role=1)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            edge_errors.append(exc)

    sender = threading.Thread(target=send_edge, daemon=True)
    sender.start()
    assert core.started.wait(timeout=1.0)
    started = time.monotonic()
    try:
        assert slave.detach_local_core(timeout_s=0.05) is False
        assert time.monotonic() - started < 0.5
        assert slave._local_core is core
        assert slave._irq_callback is None
        assert slave._local_core_detached is False

        core.release.set()
        sender.join(timeout=1.0)
        assert not sender.is_alive()
        assert edge_errors == []
        assert slave.detach_local_core(timeout_s=0.5) is True
        assert slave._local_core is None
        # Detaching must preserve the worker-mode bit used by stop() to
        # signal the response-only worker; clearing it would strand a daemon
        # thread waiting on the completed-edge queue.
        assert slave.stop(timeout_s=1.0) is True
    finally:
        core.release.set()
        sender.join(timeout=1.0)
        master.stop()
        slave.stop()


def test_owner_dispatch_detach_waits_for_gate_admitted_core_operation() -> None:
    master, slave = NetworkBackend.pair()
    core = _BlockingCore()
    slave_gate = SerialOperationGate()
    master.start_receiver(local_core=None)
    slave.start_receiver(
        local_core=core,
        serial_gate=slave_gate,
        dispatch_to_owner=True,
    )
    edge_errors: list[BaseException] = []
    service_results: list[int] = []
    service_errors: list[BaseException] = []

    def send_edge() -> None:
        try:
            master.on_edge(our_bit=1, our_role=1)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            edge_errors.append(exc)

    def service_edge() -> None:
        try:
            service_results.append(slave.service_pending_edges(max_edges=1))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            service_errors.append(exc)

    sender = threading.Thread(target=send_edge, daemon=True)
    service = threading.Thread(target=service_edge, daemon=True)
    sender.start()
    try:
        deadline = time.monotonic() + 1.0
        while slave.debug_snapshot()["pending_edge_requests"] == 0:
            assert time.monotonic() < deadline, "owner dispatch never received EDGE_REQ"
            time.sleep(0.001)
        service.start()
        assert core.started.wait(timeout=1.0)

        assert slave.detach_local_core(timeout_s=0.05) is False
        assert slave._local_core is core
        assert slave._local_core_detached is False

        core.release.set()
        service.join(timeout=1.0)
        sender.join(timeout=1.0)
        assert not service.is_alive()
        assert not sender.is_alive()
        assert service_errors == []
        assert service_results == [1]
        assert edge_errors == []
        assert slave.detach_local_core(timeout_s=0.5) is True
        assert slave._local_core is None
    finally:
        core.release.set()
        service.join(timeout=1.0)
        sender.join(timeout=1.0)
        master.stop()
        slave.stop()


def test_stop_deadline_does_not_wait_unbounded_for_owner_dispatch() -> None:
    """Terminal socket cancellation remains bounded behind native owner work."""
    master, slave = NetworkBackend.pair()
    core = _BlockingCore()
    master.start_receiver(local_core=None)
    slave.start_receiver(
        local_core=core,
        serial_gate=SerialOperationGate(),
        dispatch_to_owner=True,
    )
    sender_errors: list[BaseException] = []
    service_results: list[int] = []
    service_errors: list[BaseException] = []

    def send_edge() -> None:
        try:
            master.on_edge(our_bit=1, our_role=1)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            sender_errors.append(exc)

    def service_edge() -> None:
        try:
            service_results.append(slave.service_pending_edges(max_edges=1))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            service_errors.append(exc)

    sender = threading.Thread(target=send_edge, daemon=True)
    service = threading.Thread(target=service_edge, daemon=True)
    sender.start()
    try:
        deadline = time.monotonic() + 1.0
        while slave.debug_snapshot()["pending_edge_requests"] == 0:
            assert time.monotonic() < deadline, "owner dispatch never received EDGE_REQ"
            time.sleep(0.001)
        service.start()
        assert core.started.wait(timeout=1.0)

        started = time.monotonic()
        assert slave.stop(timeout_s=0.05) is False
        assert time.monotonic() - started < 0.5
        assert slave.connected is False

        core.release.set()
        service.join(timeout=1.0)
        sender.join(timeout=1.0)
        assert not service.is_alive()
        assert not sender.is_alive()
        assert service_errors == []
        assert sender_errors and isinstance(sender_errors[0], NetworkBackendError)
        assert service_results == [0]
        assert slave.stop(timeout_s=0.5) is True
    finally:
        core.release.set()
        service.join(timeout=1.0)
        sender.join(timeout=1.0)
        master.stop()
        slave.stop()


def test_stop_deadline_does_not_wait_unbounded_for_lifecycle_lock() -> None:
    """A concurrent detach/rebind cannot make a bounded stop block forever."""
    backend, peer = NetworkBackend.pair()
    assert backend._receiver_start_lock.acquire(timeout=0.1)
    started = time.monotonic()
    try:
        assert backend.stop(timeout_s=0.05) is False
        assert time.monotonic() - started < 0.5
    finally:
        backend._receiver_start_lock.release()
        backend.stop(timeout_s=0.5)
        peer.stop(timeout_s=0.5)


def test_zero_timeout_idle_stop_is_successful_and_idempotent() -> None:
    """A free owner lock still permits immediate terminal publication."""
    backend, peer = NetworkBackend.pair()
    try:
        assert backend.stop(timeout_s=0) is True
        assert backend.stop(timeout_s=0) is True
    finally:
        peer.stop(timeout_s=0)


def test_detach_can_freeze_while_owner_dispatcher_waits_for_serial_gate() -> None:
    """A queued dispatcher must not make a gate-held detach time out."""
    master, slave = NetworkBackend.pair()
    core = _BlockingCore()
    entered_gate = threading.Event()
    service_errors: list[BaseException] = []
    service_results: list[int] = []
    service_thread: threading.Thread | None = None

    class ObservedGate(SerialOperationGate):
        def __enter__(self):
            if threading.current_thread() is service_thread:
                entered_gate.set()
            return super().__enter__()

    gate = ObservedGate()
    slave.start_receiver(local_core=core, serial_gate=gate, dispatch_to_owner=True)

    def service_edge() -> None:
        try:
            service_results.append(slave.service_pending_edges(max_edges=1))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            service_errors.append(exc)

    service_thread = threading.Thread(target=service_edge, daemon=True)
    try:
        with gate:
            service_thread.start()
            assert entered_gate.wait(timeout=1.0)
            assert slave.detach_local_core(timeout_s=0.2) is True
            assert slave._local_core is None
        service_thread.join(timeout=1.0)
        assert not service_thread.is_alive()
        assert service_results == []
        assert len(service_errors) == 1
        assert isinstance(service_errors[0], NetworkBackendError)
        assert "detached" in str(service_errors[0])
        assert core.calls == 0
    finally:
        service_thread.join(timeout=1.0)
        master.stop()
        slave.stop()


def test_detach_can_rebind_live_receiver_without_reconnecting() -> None:
    """A quiescent detached transport can publish one replacement core."""
    backend, peer = NetworkBackend.pair()
    first_core = _BlockingCore()
    second_core = _CompletingCore()
    first_callback = lambda: None
    second_callback = lambda: None
    first_provider = lambda: {"endpoint": "first"}
    second_provider = lambda: {"endpoint": "second"}
    backend.set_serial_transcript_context_provider(first_provider)
    backend.start_receiver(local_core=first_core, irq_callback=first_callback)

    try:
        assert backend.detach_local_core(timeout_s=0.2) is True
        assert backend.connected is True
        backend.set_serial_transcript_context_provider(second_provider)
        backend.start_receiver(local_core=second_core, irq_callback=second_callback)
        assert backend._local_core is second_core
        assert backend._irq_callback is second_callback
        assert backend._serial_transcript_context_provider is second_provider
        assert backend._local_core_detached is False
        assert backend.detach_local_core(timeout_s=0.2) is True
    finally:
        backend.stop()
        peer.stop()


def test_failed_rebind_cleanup_drops_staged_provider_reference() -> None:
    """Idempotent detach must release context staged for a failed rebind."""
    backend, peer = NetworkBackend.pair()
    first_core = _BlockingCore()
    backend.start_receiver(local_core=first_core, dispatch_to_owner=False)
    second_core = _CompletingCore()
    second_ref = weakref.ref(second_core)
    second_provider = lambda core=second_core: {"endpoint": id(core)}

    try:
        assert backend.detach_local_core(timeout_s=0.2) is True
        backend.set_serial_transcript_context_provider(second_provider)
        with pytest.raises(ValueError, match="dispatch mode"):
            backend.start_receiver(
                local_core=second_core,
                serial_gate=SerialOperationGate(),
                dispatch_to_owner=True,
            )
        assert backend._serial_transcript_context_provider is second_provider
        assert backend.detach_local_core(timeout_s=0.2) is True
        assert backend._serial_transcript_context_provider is None
        del second_provider, second_core
        gc.collect()
        assert second_ref() is None
    finally:
        backend.stop()
        peer.stop()


def test_detach_waits_for_blocking_completion_context_provider() -> None:
    """Provider closures cannot outlive a successful local-core detach."""
    master, slave = NetworkBackend.pair()
    core = _CompletingCore()
    provider_started = threading.Event()
    provider_release = threading.Event()
    provider_errors: list[BaseException] = []
    service_errors: list[BaseException] = []
    service_results: list[int] = []

    def provider() -> dict[str, object]:
        provider_started.set()
        provider_release.wait(timeout=2.0)
        return {"pc": 0x1234}

    slave.enable_serial_transcript(max_entries=8)
    slave.set_serial_transcript_context_provider(provider)
    master.start_receiver(local_core=None)
    slave.start_receiver(
        local_core=core,
        serial_gate=SerialOperationGate(),
        dispatch_to_owner=True,
    )

    def send_edge() -> None:
        try:
            master.on_edge(our_bit=1, our_role=1)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            provider_errors.append(exc)

    def service_edge() -> None:
        try:
            service_results.append(slave.service_pending_edges(max_edges=1))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            service_errors.append(exc)

    sender = threading.Thread(target=send_edge, daemon=True)
    service = threading.Thread(target=service_edge, daemon=True)
    sender.start()
    try:
        deadline = time.monotonic() + 1.0
        while slave.debug_snapshot()["pending_edge_requests"] == 0:
            assert time.monotonic() < deadline, "owner dispatch never received EDGE_REQ"
            time.sleep(0.001)
        service.start()
        assert provider_started.wait(timeout=1.0)

        assert slave.detach_local_core(timeout_s=0.05) is False
        assert slave._local_core is core
        assert slave._serial_transcript_context_provider is provider

        provider_release.set()
        service.join(timeout=1.0)
        sender.join(timeout=1.0)
        assert not service.is_alive()
        assert not sender.is_alive()
        assert service_errors == []
        assert provider_errors == []
        assert service_results == [1]
        assert core.calls == 1
        assert slave.detach_local_core(timeout_s=0.5) is True
        assert slave._local_core is None
        assert slave._serial_transcript_context_provider is None
    finally:
        provider_release.set()
        service.join(timeout=1.0)
        sender.join(timeout=1.0)
        master.stop()
        slave.stop()


def test_detach_waits_for_blocking_irq_callback() -> None:
    """The IRQ closure is also quiesced before bindings are cleared."""
    master, slave = NetworkBackend.pair()
    core = _CompletingCore()
    irq_started = threading.Event()
    irq_release = threading.Event()
    service_errors: list[BaseException] = []
    service_results: list[int] = []
    edge_errors: list[BaseException] = []

    def irq_callback() -> None:
        irq_started.set()
        irq_release.wait(timeout=2.0)

    slave.start_receiver(
        local_core=core,
        irq_callback=irq_callback,
        serial_gate=SerialOperationGate(),
        dispatch_to_owner=True,
    )
    master.start_receiver(local_core=None)

    def send_edge() -> None:
        try:
            master.on_edge(our_bit=1, our_role=1)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            edge_errors.append(exc)

    def service_edge() -> None:
        try:
            service_results.append(slave.service_pending_edges(max_edges=1))
        except BaseException as exc:  # noqa: BLE001 - asserted below
            service_errors.append(exc)

    sender = threading.Thread(target=send_edge, daemon=True)
    service = threading.Thread(target=service_edge, daemon=True)
    sender.start()
    try:
        deadline = time.monotonic() + 1.0
        while slave.debug_snapshot()["pending_edge_requests"] == 0:
            assert time.monotonic() < deadline, "owner dispatch never received EDGE_REQ"
            time.sleep(0.001)
        service.start()
        assert irq_started.wait(timeout=1.0)

        assert slave.detach_local_core(timeout_s=0.05) is False
        assert slave._local_core is core
        assert slave._irq_callback is irq_callback

        irq_release.set()
        service.join(timeout=1.0)
        sender.join(timeout=1.0)
        assert not service.is_alive()
        assert not sender.is_alive()
        assert service_errors == []
        assert edge_errors == []
        assert service_results == [1]
        assert slave.detach_local_core(timeout_s=0.5) is True
        assert slave._local_core is None
        assert slave._irq_callback is None
    finally:
        irq_release.set()
        service.join(timeout=1.0)
        sender.join(timeout=1.0)
        master.stop()
        slave.stop()
