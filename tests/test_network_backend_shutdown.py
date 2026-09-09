"""Bounded lifecycle and attachment cleanup for :class:`NetworkBackend`.

The transport and the emulator endpoint have deliberately separate lifecycle
boundaries. ``stop`` closes the wire and joins its workers; callers that want
to release an attached emulator while retaining the connection use
``detach_local_core`` first.
"""

from __future__ import annotations

import gc
import threading
import time
import weakref

import pytest

from pokered_harness.link.network_backend import NetworkBackend, NetworkBackendError
from pokered_harness.link.serial_coordinator import SerialOperationGate

pytestmark = pytest.mark.timing_sensitive


class _Core:
    transfer_enabled = 1
    internal_clock = 0
    SB = 0
    SC = 0x80

    def __init__(self) -> None:
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def peek_out_bit(self) -> int:
        return 0

    def apply_external_edge(self, _peer_bit: int) -> bool:
        self.calls += 1
        self.started.set()
        return False


class _BlockingCore(_Core):
    def apply_external_edge(self, _peer_bit: int) -> bool:
        self.calls += 1
        self.started.set()
        self.release.wait(timeout=2.0)
        return False


def _workers_stopped(backend: NetworkBackend) -> None:
    assert backend._reader is None or not backend._reader.is_alive()
    assert backend._edge_worker is None or not backend._edge_worker.is_alive()


def test_stop_is_bounded_idempotent_and_terminal() -> None:
    backend, peer = NetworkBackend.pair()
    backend.start_receiver(local_core=None)
    try:
        assert backend.stop(timeout_s=1.0) is True
        assert backend.stop(timeout_s=0) is True
        assert backend.connected is False
        _workers_stopped(backend)
        with pytest.raises(NetworkBackendError, match="backend closed"):
            backend.start_receiver(local_core=None)
    finally:
        backend.stop(timeout_s=1.0)
        peer.stop(timeout_s=1.0)


def test_successful_detach_releases_only_emulator_owned_references() -> None:
    backend, peer = NetworkBackend.pair()
    core = _Core()
    callback = lambda core=core: core
    provider = lambda core=core: {"core": id(core)}
    core_ref = weakref.ref(core)
    backend.enable_serial_transcript(max_entries=8)
    backend.set_serial_transcript_context_provider(provider)
    backend.start_receiver(local_core=core, irq_callback=callback)
    reader, worker = backend._reader, backend._edge_worker
    try:
        assert backend.detach_local_core(timeout_s=0.5) is True
        assert backend.connected is True
        assert backend._local_core is None
        assert backend._irq_callback is None
        assert backend._serial_transcript_context_provider is None
        assert backend._local_core_detached is True
        assert reader is not None and worker is not None
        assert reader.is_alive() and worker.is_alive()
        del callback, provider, core
        gc.collect()
        assert core_ref() is None
    finally:
        backend.stop(timeout_s=1.0)
        peer.stop(timeout_s=1.0)


def test_detach_timeout_preserves_attachment_until_owner_quiesces() -> None:
    master, slave = NetworkBackend.pair()
    core = _BlockingCore()
    gate = SerialOperationGate()
    master.start_receiver(local_core=None)
    slave.start_receiver(local_core=core, serial_gate=gate, dispatch_to_owner=True)
    sender_errors: list[BaseException] = []
    service_errors: list[BaseException] = []

    def send() -> None:
        try:
            master.on_edge(1, 1)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            sender_errors.append(exc)

    def service() -> None:
        try:
            assert slave.service_pending_edges(max_edges=1) == 1
        except BaseException as exc:  # noqa: BLE001 - asserted below
            service_errors.append(exc)

    sender = threading.Thread(target=send, daemon=True)
    dispatcher = threading.Thread(target=service, daemon=True)
    sender.start()
    try:
        deadline = time.monotonic() + 1.0
        while slave.debug_snapshot()["pending_edge_requests"] == 0:
            assert time.monotonic() < deadline
            time.sleep(0.001)
        dispatcher.start()
        assert core.started.wait(timeout=1.0)
        assert slave.detach_local_core(timeout_s=0.03) is False
        assert slave._local_core is core
        assert slave._local_core_detached is False

        core.release.set()
        dispatcher.join(timeout=1.0)
        sender.join(timeout=1.0)
        assert not dispatcher.is_alive() and not sender.is_alive()
        assert service_errors == [] and sender_errors == []
        assert slave.detach_local_core(timeout_s=0.5) is True
        assert slave._local_core is None
    finally:
        core.release.set()
        dispatcher.join(timeout=1.0)
        sender.join(timeout=1.0)
        master.stop(timeout_s=1.0)
        slave.stop(timeout_s=1.0)


def test_stop_timeout_keeps_worker_reference_until_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker that cannot join keeps its object graph available for retry."""
    backend, peer = NetworkBackend.pair()
    release = threading.Event()
    worker = threading.Thread(target=release.wait, daemon=True)
    worker.start()
    backend._reader = worker
    monkeypatch.setattr(backend, "_mark_closed", lambda *, deadline=None, error=None: True)
    try:
        assert backend.stop(timeout_s=0.01) is False
        assert backend._reader is worker
        release.set()
        worker.join(timeout=1.0)
        assert backend.stop(timeout_s=1.0) is True
    finally:
        release.set()
        worker.join(timeout=1.0)
        backend.stop(timeout_s=1.0)
        peer.stop(timeout_s=1.0)


def test_detached_receiver_can_rebind_without_reconnecting() -> None:
    backend, peer = NetworkBackend.pair()
    first, second = _Core(), _Core()
    gate = SerialOperationGate()
    backend.start_receiver(local_core=first, serial_gate=gate, dispatch_to_owner=True)
    try:
        assert backend.detach_local_core(timeout_s=0.5) is True
        backend.start_receiver(local_core=second, serial_gate=gate, dispatch_to_owner=True)
        assert backend._local_core is second
        assert backend._local_core_detached is False
        assert backend.detach_local_core(timeout_s=0.5) is True
    finally:
        backend.stop(timeout_s=1.0)
        peer.stop(timeout_s=1.0)
