"""Focused tests for serialized remote-serial ownership."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from pokered_harness.link.network_backend import (
    NetworkBackend,
    NetworkBackendError,
)
from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
from pokered_harness.link.serial_coordinator import SerialOperationGate


def _wait_for(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


class _RecordingOwnerCore:
    """Small serial-core double that records only edge-native operations."""

    def __init__(self, *, armed: bool, completes: bool = True) -> None:
        self.backend = None
        self.transfer_enabled = int(armed)
        self.internal_clock = 0
        self.SB = 0
        self.SC = 0x80 if armed else 0
        self.completes = completes
        self.edge_events: list[tuple[str, int, int | None]] = []

    def set_SB(self, value: int) -> None:
        self.SB = value & 0xFF

    def set_SC(self, value: int) -> None:
        self.SC = value & 0xFF
        self.transfer_enabled = int(bool(self.SC & 0x80))
        self.internal_clock = int(bool(self.SC & 0x01))

    def peek_out_bit(self) -> int:
        self.edge_events.append(("peek", threading.get_ident(), None))
        return 0

    def apply_external_edge(self, peer_bit: int) -> bool:
        self.edge_events.append(("apply", threading.get_ident(), peer_bit & 1))
        self.SB = peer_bit & 1
        if self.completes:
            self.transfer_enabled = 0
            self.SC &= ~0x80
            return True
        return False


def _start_owner_pair(core, irq_callback=None):
    master, owner = NetworkBackend.pair()
    gate = SerialOperationGate()
    master.start_receiver(local_core=None)
    owner.start_receiver(
        local_core=core,
        irq_callback=irq_callback,
        serial_gate=gate,
        dispatch_to_owner=True,
    )
    return master, owner, gate


def test_dispatch_to_owner_runs_native_core_and_irq_on_owner_thread_only():
    core = _RecordingOwnerCore(armed=True, completes=True)
    irq_threads: list[int] = []
    master, owner, _gate = _start_owner_pair(
        core, irq_callback=lambda: irq_threads.append(threading.get_ident())
    )
    sender_result: list[object] = []

    def send_edge() -> None:
        try:
            sender_result.append(master.on_edge(our_bit=1, our_role=1))
        except BaseException as exc:  # noqa: BLE001 - reported below
            sender_result.append(exc)

    sender = threading.Thread(target=send_edge, name="test-edge-sender")
    sender.start()
    owner_thread_id: list[int] = []
    service_result: list[int] = []

    try:
        assert _wait_for(
            lambda: owner.debug_snapshot()["pending_edge_requests"] == 1
        )
        assert core.edge_events == []

        def service_once() -> None:
            owner_thread_id.append(threading.get_ident())
            service_result.append(owner.service_pending_edges(max_edges=1))

        service_thread = threading.Thread(
            target=service_once, name="test-emulator-owner"
        )
        service_thread.start()
        service_thread.join(timeout=1.0)
        sender.join(timeout=1.0)

        assert not service_thread.is_alive()
        assert not sender.is_alive()
        assert service_result == [1]
        assert sender_result == [0]
        assert owner_thread_id
        owner_id = owner_thread_id[0]
        assert [event[1] for event in core.edge_events] == [owner_id, owner_id]
        assert irq_threads == [owner_id]
        assert owner._reader is not None and owner._reader.ident != owner_id
        assert owner._edge_worker is not None
        assert owner._edge_worker.ident != owner_id
        assert owner._edge_worker.name.endswith("owner-response-worker")

        snapshot = owner.debug_snapshot()
        assert snapshot["owner_edge_applied"] == 1
        assert snapshot["keepalive_bits_sent"] == 0
    finally:
        owner.stop(timeout_s=1.0)
        master.stop(timeout_s=1.0)
        sender.join(timeout=1.0)


def test_dispatch_to_owner_defers_unarmed_edge_until_owner_rearms():
    core = _RecordingOwnerCore(armed=False, completes=True)
    master, owner, gate = _start_owner_pair(core)
    sender_result: list[object] = []

    def send_edge() -> None:
        try:
            sender_result.append(master.on_edge(our_bit=1, our_role=1))
        except BaseException as exc:  # noqa: BLE001 - reported below
            sender_result.append(exc)

    sender = threading.Thread(target=send_edge, name="test-deferred-edge-sender")
    sender.start()
    first_service_done = threading.Event()
    permit_rearm = threading.Event()
    owner_thread_id: list[int] = []
    service_results: list[int] = []

    def service_and_rearm() -> None:
        owner_thread_id.append(threading.get_ident())
        service_results.append(owner.service_pending_edges(max_edges=1))
        first_service_done.set()
        assert permit_rearm.wait(timeout=1.0)
        with gate:
            core.SC = 0x80
            core.transfer_enabled = 1
            core.internal_clock = 0
        service_results.append(owner.service_pending_edges(max_edges=1))

    service_thread = threading.Thread(
        target=service_and_rearm, name="test-deferred-emulator-owner"
    )

    try:
        assert _wait_for(
            lambda: owner.debug_snapshot()["pending_edge_requests"] == 1
        )
        service_thread.start()
        assert first_service_done.wait(timeout=1.0)

        assert service_results == [0]
        assert core.edge_events == []
        snapshot = owner.debug_snapshot()
        assert snapshot["pending_edge_requests"] == 1
        assert snapshot["owner_edge_deferred"] == 1
        assert snapshot["keepalive_bits_sent"] == 0
        assert sender.is_alive()

        permit_rearm.set()
        service_thread.join(timeout=1.0)
        sender.join(timeout=1.0)

        assert not service_thread.is_alive()
        assert not sender.is_alive()
        assert service_results == [0, 1]
        assert sender_result == [0]
        assert owner_thread_id
        assert {event[1] for event in core.edge_events} == {owner_thread_id[0]}
        assert owner.debug_snapshot()["keepalive_bits_sent"] == 0
    finally:
        permit_rearm.set()
        owner.stop(timeout_s=1.0)
        master.stop(timeout_s=1.0)
        service_thread.join(timeout=1.0)
        sender.join(timeout=1.0)


def test_dispatch_to_owner_stop_cleans_response_worker_and_wakes_master():
    core = _RecordingOwnerCore(armed=True, completes=True)
    master, owner, _gate = _start_owner_pair(core)
    sender_result: list[object] = []

    def send_edge() -> None:
        try:
            sender_result.append(master.on_edge(our_bit=1, our_role=1))
        except BaseException as exc:  # noqa: BLE001 - expected on stop
            sender_result.append(exc)

    sender = threading.Thread(target=send_edge, name="test-stop-edge-sender")
    sender.start()

    try:
        assert _wait_for(
            lambda: owner.debug_snapshot()["pending_edge_requests"] == 1
        )
        started = time.monotonic()
        assert owner.stop(timeout_s=1.0)
        assert time.monotonic() - started < 0.5
        sender.join(timeout=1.0)

        assert not sender.is_alive()
        assert sender_result
        assert isinstance(sender_result[0], NetworkBackendError)
        assert not owner.connected
        assert owner._reader is not None and not owner._reader.is_alive()
        assert owner._edge_worker is not None and not owner._edge_worker.is_alive()
        assert core.edge_events == []
    finally:
        master.stop(timeout_s=1.0)
        owner.stop(timeout_s=1.0)
        sender.join(timeout=1.0)


class _TickProbePyBoy:
    def __init__(self, serial) -> None:
        self.mb = SimpleNamespace(
            serial=serial,
            cpu=SimpleNamespace(set_interruptflag=lambda _flag: None),
        )
        self.tick_threads: list[int] = []

    def _tick(self, render=True, sound=True):
        del render, sound
        self.tick_threads.append(threading.get_ident())
        return True

    def tick(self, count=1, render=True, sound=True):
        for _ in range(count):
            if not self._tick(render, sound):
                return False
        return True


def test_network_attach_wraps_source_pyboy_tick_and_services_owner_queue():
    core = _RecordingOwnerCore(armed=True, completes=True)
    backend, peer = NetworkBackend.pair()
    peer.start_receiver(local_core=None)
    pyboy = _TickProbePyBoy(core)
    original_tick = pyboy._tick
    link = PyBoyLinkSession(
        network_backend=backend,
        network_is_internal_clock=False,
    )
    sender_result: list[object] = []

    def send_edge() -> None:
        try:
            sender_result.append(peer.on_edge(our_bit=1, our_role=1))
        except BaseException as exc:  # noqa: BLE001 - reported below
            sender_result.append(exc)

    sender = threading.Thread(target=send_edge, name="test-tick-edge-sender")

    try:
        link.attach(pyboy)
        assert pyboy._tick != original_tick
        assert callable(getattr(pyboy._tick, "__wrapped__", None))

        sender.start()
        assert _wait_for(
            lambda: backend.debug_snapshot()["pending_edge_requests"] == 1
        )
        assert core.edge_events == []
        assert pyboy.tick(1, render=False) is True
        sender.join(timeout=1.0)

        assert not sender.is_alive()
        assert sender_result == [0]
        assert len(pyboy.tick_threads) == 1
        assert {event[1] for event in core.edge_events} == {
            pyboy.tick_threads[0]
        }
    finally:
        link.detach_all()
        peer.stop(timeout_s=1.0)
        sender.join(timeout=1.0)

    assert pyboy._tick == original_tick


def test_pyboy_tick_accepts_instance_assignment():
    from pyboy import PyBoy

    instance = PyBoy.__new__(PyBoy)
    # Keep the class destructor from treating this method-only probe as a
    # partially initialized emulator.
    instance.initialized = False
    instance.stopped = True
    original_tick = instance.tick
    sentinel = object()

    def replacement(*_args, **_kwargs):
        return sentinel

    instance.tick = replacement
    assert instance.tick() is sentinel
    assert callable(original_tick)
