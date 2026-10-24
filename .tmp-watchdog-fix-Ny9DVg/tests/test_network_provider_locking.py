"""Deterministic provider/lifecycle locking regressions for network links."""

from __future__ import annotations

import threading
import time

import pytest

from pokered_harness.link.network_backend import NetworkBackend, NetworkBackendError
from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
from pokered_harness.link.serial_coordinator import SerialOperationGate
from pokered_harness.ownership import owner_for
from tests.test_pyboy_link_session import _FakePyBoy

pytestmark = pytest.mark.timing_sensitive


class _CompletingCore:
    transfer_enabled = 1
    internal_clock = 0
    SB = 0
    SC = 0x80

    def __init__(self) -> None:
        self.calls = 0

    def peek_out_bit(self) -> int:
        return 0

    def apply_external_edge(self, peer_bit: int) -> bool:
        self.calls += 1
        self.SB = peer_bit & 1
        self.transfer_enabled = 0
        return True


class _CallbackPyBoy(_FakePyBoy):
    """Fake endpoint whose original network tick can re-enter the link."""

    def __init__(self) -> None:
        super().__init__()
        self.on_tick = None

    def tick(self, count: int = 1, render: bool = False, sound: bool = False) -> bool:
        if self.on_tick is not None:
            self.on_tick()
        # The network role initializer arms the native serial transfer. This
        # scope/lifecycle regression deliberately keeps the original frame
        # body inert so it cannot block on an unstarted peer socket.
        self._cycles += count
        return True


def test_network_callback_set_frame_barrier_and_detach_use_canonical_scopes() -> None:
    """A callback may set frame pacing while teardown waits on the same owner."""
    backend, peer = NetworkBackend.pair()
    pyboy = _CallbackPyBoy()
    link = PyBoyLinkSession(
        network_backend=backend,
        network_is_internal_clock=True,
    )
    callback_entered = threading.Event()
    callback_release = threading.Event()
    detach_attempted = threading.Event()
    tick_errors: list[BaseException] = []
    detach_errors: list[BaseException] = []
    observed: list[tuple[int, bool, bool, bool]] = []

    def callback() -> None:
        owner = owner_for(pyboy)
        observed.append(
            (
                int(getattr(owner.execution, "depth", 0)),
                bool(link._network_tick_active),
                bool(link._lifecycle_lock._is_owned()),
                bool(link._serial_gate._lock._is_owned()),
            )
        )
        # This is the original callback/re-entry path: it must use the same
        # canonical owner -> lifecycle -> serial-gate ordering as a normal
        # network frame, and must not deadlock against a concurrent detach.
        link.set_network_frame_barrier(True)
        callback_entered.set()
        assert callback_release.wait(timeout=1.0)

    pyboy.on_tick = callback

    def tick() -> None:
        try:
            pyboy.tick(1)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            tick_errors.append(exc)

    def detach() -> None:
        try:
            link.detach_all()
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            detach_errors.append(exc)

    tick_thread = threading.Thread(target=tick, name="network-callback-tick")
    detach_thread: threading.Thread | None = None
    link.attach(pyboy)
    owner = owner_for(pyboy)
    original_owner_lock = owner.lock

    class ObservedOwnerLock:
        def acquire(self, *args, **kwargs):
            if threading.current_thread() is not tick_thread:
                detach_attempted.set()
            return original_owner_lock.acquire(*args, **kwargs)

        def release(self):
            return original_owner_lock.release()

        def __enter__(self):
            self.acquire()
            return self

        def __exit__(self, *exc_info):
            self.release()
            return False

    owner.lock = ObservedOwnerLock()
    try:
        tick_thread.start()
        assert callback_entered.wait(timeout=1.0)

        detach_thread = threading.Thread(target=detach, name="network-callback-detach")
        detach_thread.start()
        # The callback still owns the canonical emulator scope, so detach may
        # wait, but it must not mutate the provider/member graph early.
        assert detach_attempted.wait(timeout=1.0)
        assert link.attached == (pyboy,)

        callback_release.set()
        tick_thread.join(timeout=1.0)
        detach_thread.join(timeout=1.0)
        assert not tick_thread.is_alive()
        assert not detach_thread.is_alive()
        assert tick_errors == []
        assert detach_errors == []
        assert observed == [(2, True, True, True)]
        assert link._network_frame_barrier is True
        assert link.attached == ()
        assert backend._local_core is None
    finally:
        callback_release.set()
        tick_thread.join(timeout=1.0)
        if detach_thread is not None:
            detach_thread.join(timeout=1.0)
        owner.lock = original_owner_lock
        if link.attached:
            link.detach_all()
        backend.stop(timeout_s=1.0)
        peer.stop(timeout_s=1.0)


def test_tick_callback_cannot_detach_its_own_active_provider() -> None:
    backend, peer = NetworkBackend.pair()
    endpoint = _CallbackPyBoy()
    link = PyBoyLinkSession(network_backend=backend)
    rejected: list[RuntimeError] = []

    def callback() -> None:
        with pytest.raises(RuntimeError, match="during emulator execution") as error:
            link.detach(endpoint)
        rejected.append(error.value)
        assert link.attached == (endpoint,)
        assert endpoint.mb.serial.backend is backend
        assert backend._local_core is endpoint.mb.serial

    endpoint.on_tick = callback
    try:
        link.attach(endpoint)
        endpoint.tick()
        assert len(rejected) == 1
        assert not link._network_tick_active
        link.detach_all()
        assert link.attached == ()
        assert backend._local_core is None
    finally:
        link.detach_all()
        peer.stop()


def test_provider_replacement_is_rejected_while_completion_callback_detaches() -> None:
    """Detach freezes the old provider until an admitted callback returns."""
    master, slave = NetworkBackend.pair()
    core = _CompletingCore()
    provider_started = threading.Event()
    provider_release = threading.Event()
    service_errors: list[BaseException] = []
    sender_errors: list[BaseException] = []
    service_results: list[int] = []

    def old_provider() -> dict[str, int]:
        provider_started.set()
        assert provider_release.wait(timeout=1.0)
        return {"pc": 0x1234}

    def new_provider() -> dict[str, int]:
        return {"pc": 0x5678}

    slave.enable_serial_transcript(max_entries=4)
    slave.set_serial_transcript_context_provider(old_provider)
    master.start_receiver(local_core=None)
    slave.start_receiver(
        local_core=core,
        serial_gate=SerialOperationGate(),
        dispatch_to_owner=True,
    )

    def send_edge() -> None:
        try:
            master.on_edge(our_bit=1, our_role=1)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            sender_errors.append(exc)

    def service_edge() -> None:
        try:
            service_results.append(slave.service_pending_edges(max_edges=1))
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            service_errors.append(exc)

    detach_results: list[bool] = []

    def detach_core() -> None:
        detach_results.append(slave.detach_local_core(timeout_s=0.05))

    sender = threading.Thread(target=send_edge, name="provider-lock-sender")
    service = threading.Thread(target=service_edge, name="provider-lock-owner")
    detach_thread: threading.Thread | None = None
    try:
        sender.start()
        deadline = time.monotonic() + 1.0
        while slave.debug_snapshot()["pending_edge_requests"] == 0:
            assert time.monotonic() < deadline, "owner dispatch never received EDGE_REQ"
            time.sleep(0.001)
        service.start()
        assert provider_started.wait(timeout=1.0)

        detach_thread = threading.Thread(target=detach_core, name="provider-lock-detach")
        detach_thread.start()
        deadline = time.monotonic() + 1.0
        while not slave._local_core_detaching:
            assert time.monotonic() < deadline, "detach never published its freeze state"
            time.sleep(0.001)

        with pytest.raises(NetworkBackendError, match="detach is still in progress"):
            slave.set_serial_transcript_context_provider(new_provider)
        assert slave._serial_transcript_context_provider is old_provider
        assert slave._local_core is core

        detach_thread.join(timeout=1.0)
        assert not detach_thread.is_alive()
        assert detach_results == [False]
        provider_release.set()
        service.join(timeout=1.0)
        sender.join(timeout=1.0)
        assert not service.is_alive()
        assert not sender.is_alive()
        assert service_errors == []
        assert sender_errors == []
        assert service_results == [1]
        assert slave.detach_local_core(timeout_s=0.5) is True
        assert slave._local_core is None
        assert slave._serial_transcript_context_provider is None
    finally:
        provider_release.set()
        service.join(timeout=1.0)
        sender.join(timeout=1.0)
        if detach_thread is not None:
            detach_thread.join(timeout=1.0)
        master.stop(timeout_s=1.0)
        slave.stop(timeout_s=1.0)


def test_network_detach_all_is_bounded_when_canonical_owner_is_busy() -> None:
    """A busy emulator owner blocks cleanup without mutating the provider."""
    backend, peer = NetworkBackend.pair()
    pyboy = _FakePyBoy()
    link = PyBoyLinkSession(network_backend=backend)
    link.attach(pyboy)
    owner = owner_for(pyboy)
    original_lock = owner.lock

    class BusyOwnerLock:
        def __init__(self) -> None:
            self.timeouts: list[float] = []

        def acquire(self, *, timeout: float) -> bool:
            self.timeouts.append(timeout)
            return False

        def release(self) -> None:
            raise AssertionError("a failed owner acquisition must not release the lock")

    busy_lock = BusyOwnerLock()
    owner.lock = busy_lock
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="ownership lock acquisition timed out"):
            link.detach_all()
        assert time.monotonic() - started < 0.5
        assert busy_lock.timeouts and 0.0 < busy_lock.timeouts[0] <= 2.0
        assert link.attached == (pyboy,)
        assert link._network_backend is backend
        assert backend._local_core is link._cores[0]
        assert owner._provider is not None and owner._provider() is link
    finally:
        owner.lock = original_lock
        if link.attached:
            link.detach_all()
        backend.stop(timeout_s=1.0)
        peer.stop(timeout_s=1.0)
