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
from types import SimpleNamespace

import pytest
from pyboy.core.serial import Serial

from pokered_harness.link.network_backend import NetworkBackend, NetworkBackendError
from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
from pokered_harness.link.serial_coordinator import SerialOperationGate
from pokered_harness.ownership import owner_for

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
    core = _Core()
    callback = lambda: None
    release = threading.Event()
    worker = threading.Thread(target=release.wait, daemon=True)
    completed_worker = threading.Thread(target=lambda: None, daemon=True)
    completed_worker.start()
    completed_worker.join(timeout=1.0)
    worker.start()
    backend._reader = worker
    backend._edge_worker = completed_worker
    backend._local_core = core
    backend._irq_callback = callback
    monkeypatch.setattr(backend, "_mark_closed", lambda *, deadline=None, error=None: True)
    try:
        assert backend.stop(timeout_s=0.01) is False
        assert backend._reader is worker
        assert backend._edge_worker is completed_worker
        assert backend._local_core is core
        assert backend._irq_callback is callback
        release.set()
        worker.join(timeout=1.0)
        assert backend.stop(timeout_s=1.0) is True
        assert backend._reader is worker
        assert backend._edge_worker is completed_worker
        assert backend._local_core is core
        assert backend._irq_callback is callback
        assert backend.detach_local_core(timeout_s=0.5) is True
        assert backend._local_core is None
        assert backend._irq_callback is None
    finally:
        release.set()
        worker.join(timeout=1.0)
        monkeypatch.undo()
        backend.stop(timeout_s=1.0)
        peer.stop(timeout_s=1.0)


def test_native_serial_detach_releases_graph_after_bounded_cleanup() -> None:
    """Public session cleanup releases a real Serial graph after idle detach."""

    class Endpoint:
        def __init__(self) -> None:
            self.mb = SimpleNamespace(serial=Serial())

        def tick(self, *_args, **_kwargs) -> bool:
            return True

    backend, peer = NetworkBackend.pair()
    endpoint = Endpoint()
    endpoint_ref = weakref.ref(endpoint)
    core = endpoint.mb.serial
    previous_backend = core.backend
    previous_callback = core.owner_dispatch_callback
    previous_enabled = core.owner_dispatch_enabled
    owner = owner_for(endpoint)
    link = PyBoyLinkSession(network_backend=backend)

    try:
        assert link.attach(endpoint) is core
        assert owner._provider is not None and owner._provider() is link
        assert core.backend is backend
        assert core.owner_dispatch_callback is not previous_callback
        assert core.owner_dispatch_enabled is True
        assert backend._local_core is core
        assert backend._irq_callback is not None
        assert backend._serial_transcript_context_provider is not None
        assert backend.connected, f"{backend._reader_exc!r} {backend.debug_snapshot()!r}"
        reader, worker = backend._reader, backend._edge_worker
        assert reader is not None and reader.is_alive()
        assert worker is not None and worker.is_alive()

        # This direct backend transition is valid for an idle attachment only.
        # A held response remains owned by the transport and cannot be retired
        # by this graph-release assertion.
        assert backend._held_owner_byte_response is None
        assert backend.detach_local_core(timeout_s=0.5) is True
        assert backend._local_core is None
        assert backend._irq_callback is None
        assert backend._serial_transcript_context_provider is None
        assert backend._held_owner_byte_response is None
        assert backend.connected
        assert reader.is_alive() and worker.is_alive()
        assert core.backend is backend
        assert core.owner_dispatch_enabled is True
        assert owner._provider is not None and owner._provider() is link

        # The public wrapper performs bounded transport cancellation before
        # restoring the native serial fields and releasing provider membership.
        link.detach(endpoint)

        assert link.attached == ()
        assert owner._provider is None
        assert core.backend is previous_backend
        assert core.owner_dispatch_callback is previous_callback
        assert core.owner_dispatch_enabled is previous_enabled
        assert backend._local_core is None
        assert backend._irq_callback is None
        assert backend._serial_transcript_context_provider is None
        assert backend._held_owner_byte_response is None
        assert not backend.connected
        assert not reader.is_alive() and not worker.is_alive()

        del endpoint, core, owner
        gc.collect()
        assert endpoint_ref() is None
    finally:
        link.detach_all()
        peer.stop(timeout_s=1.0)


def test_stop_during_receiver_activation_preserves_startup_refs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, peer = NetworkBackend.pair()
    core = _Core()
    callback = lambda: None
    start_entered = threading.Event()
    release_start = threading.Event()
    start_errors: list[BaseException] = []
    original_start = threading.Thread.start

    def pause_receiver_worker(thread, *args, **kwargs):
        if thread.name == "NetworkBackend.edge-worker":
            start_entered.set()
            assert release_start.wait(timeout=1.0)
        return original_start(thread, *args, **kwargs)

    def start_receiver() -> None:
        try:
            backend.start_receiver(local_core=core, irq_callback=callback)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            start_errors.append(exc)

    monkeypatch.setattr(threading.Thread, "start", pause_receiver_worker)
    starter = threading.Thread(target=start_receiver, name="receiver-starter", daemon=True)
    try:
        starter.start()
        assert start_entered.wait(timeout=1.0)
        reader, worker = backend._reader, backend._edge_worker
        assert reader is not None and worker is not None
        assert backend._local_core is core
        assert backend._irq_callback is callback

        started = time.monotonic()
        assert backend.stop(timeout_s=0.03) is False
        assert time.monotonic() - started < 0.5
        assert backend._reader is reader
        assert backend._edge_worker is worker
        assert backend._local_core is core
        assert backend._irq_callback is callback

        release_start.set()
        starter.join(timeout=1.0)
        assert not starter.is_alive()
        assert start_errors == []
        assert backend.stop(timeout_s=0.5) is True
        assert backend._reader is reader and not reader.is_alive()
        assert backend._edge_worker is worker and not worker.is_alive()
        assert backend._local_core is core
        assert backend._irq_callback is callback
    finally:
        release_start.set()
        starter.join(timeout=1.0)
        backend.stop(timeout_s=1.0)
        peer.stop(timeout_s=1.0)


def test_worker_initiated_stop_is_bounded_and_retryable() -> None:
    backend, peer = NetworkBackend.pair()
    core = _Core()
    callback = lambda: None
    stopped = threading.Event()
    allow_exit = threading.Event()
    outcome: dict[str, object] = {}

    def stop_from_reader() -> None:
        reader = threading.current_thread()
        outcome["result"] = backend.stop(timeout_s=0.5)
        outcome["reader"] = backend._reader is reader
        outcome["reader_alive"] = reader.is_alive()
        outcome["core"] = backend._local_core is core
        outcome["callback"] = backend._irq_callback is callback
        stopped.set()
        allow_exit.wait(timeout=1.0)

    reader = threading.Thread(target=stop_from_reader, name="self-stop-reader", daemon=True)
    backend._reader = reader
    backend._local_core = core
    backend._irq_callback = callback
    try:
        reader.start()
        assert stopped.wait(timeout=1.0)
        assert outcome == {
            "result": True,
            "reader": True,
            "reader_alive": True,
            "core": True,
            "callback": True,
        }
        assert reader.is_alive()

        allow_exit.set()
        reader.join(timeout=1.0)
        assert not reader.is_alive()
        assert backend.stop(timeout_s=0.5) is True
        assert backend._reader is reader
        assert backend._local_core is core
        assert backend._irq_callback is callback
        assert backend.detach_local_core(timeout_s=0.5) is True
        assert backend._local_core is None
        assert backend._irq_callback is None
    finally:
        allow_exit.set()
        reader.join(timeout=1.0)
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
