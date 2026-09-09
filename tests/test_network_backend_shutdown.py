"""Deterministic lifecycle tests for :class:`NetworkBackend`.

These tests deliberately inspect the backend's owned lifecycle fields.  The
transport is allowed to close immediately, but the core/callback graph must
not be discarded until both background workers have stopped.
"""

from __future__ import annotations

import gc
import threading
import time
import weakref

import pytest

import pokered_harness.link.network_backend as network_backend_module
from pokered_harness.link.network_backend import NetworkBackend, NetworkBackendError


class _Owner:
    """Small owner object suitable for ``NetworkBackend.bind_owner``."""

    def access(self):
        return self._lock

    def __init__(self):
        self._lock = threading.RLock()


class _Core:
    """Owner-claim test double that can participate in a reference cycle."""

    transfer_enabled = False
    internal_clock = False

    def __init__(self):
        self.backend = None
        self._callback = None
        self._token = None

    def set_owner_pump(self, callback, poll=False):
        if self._token is not None:
            raise RuntimeError("owner pump is claimed")
        self._callback = callback

    def claim_owner_pump(self, callback, poll=False):
        if self._callback is not None or self._token is not None:
            raise RuntimeError("owner pump is already installed")
        self._token = object()
        self._callback = callback
        return self._token

    def release_owner_pump(self, token):
        if token is not self._token:
            raise RuntimeError("invalid owner token")
        self._token = None
        self._callback = None


def _callback_for(core):
    # Keep a real Python callback -> core edge.  This is the edge that made a
    # stopped backend retain an attached emulator in the original bug.
    def callback():
        return core

    return callback


def _assert_committed_cleanup(backend):
    assert backend._receiver_starting is False
    assert backend._active_owner_scopes == 0
    assert backend._active_owner_executions == 0
    assert backend._reader is None
    assert backend._edge_worker is None
    assert backend._local_core is None
    assert backend._irq_callback is None
    assert backend._emulator_owner is None
    assert backend._pending_edge is None
    assert backend._waiting_request is None


def test_stop_commits_owned_cleanup_after_both_workers_exit():
    backend, peer = NetworkBackend.pair()
    core = _Core()
    owner = _Owner()
    callback = _callback_for(core)
    core.backend = backend
    backend.bind_owner(owner)
    backend.start_receiver(core, callback)
    reader, edge_worker = backend._reader, backend._edge_worker
    assert reader is not None and edge_worker is not None

    try:
        backend.stop()
        assert not reader.is_alive()
        assert not edge_worker.is_alive()
        _assert_committed_cleanup(backend)
    finally:
        backend.stop()
        peer.stop()


def test_stop_is_idempotent_after_owned_cleanup_commit():
    backend, peer = NetworkBackend.pair()
    backend.start_receiver(None)
    try:
        backend.stop()
        backend.stop()
        _assert_committed_cleanup(backend)
        assert backend.connected is False
    finally:
        backend.stop()
        peer.stop()


def test_stop_keeps_terminal_receiver_lifecycle():
    backend, peer = NetworkBackend.pair()
    backend.start_receiver(None)
    try:
        backend.stop()
        with pytest.raises(NetworkBackendError, match="backend closed"):
            backend.start_receiver(None)
    finally:
        backend.stop()
        peer.stop()


def test_stop_timeout_preserves_all_owned_refs_until_retry(monkeypatch):
    backend, peer = NetworkBackend.pair()
    core = _Core()
    owner = _Owner()
    callback = _callback_for(core)
    core.backend = backend
    backend.bind_owner(owner)
    # A live thread models a worker that did not observe cancellation.  The
    # second thread has completed, proving that cleanup is an all-or-nothing
    # commit rather than a per-thread reference drop.
    release = threading.Event()
    live_worker = threading.Thread(target=release.wait, daemon=True)
    live_worker.start()
    completed_worker = threading.Thread(target=lambda: None, daemon=True)
    completed_worker.start()
    completed_worker.join()
    backend._reader = live_worker
    backend._edge_worker = completed_worker
    backend._local_core = core
    backend._irq_callback = callback

    monkeypatch.setattr(network_backend_module, "_STOP_JOIN_TIMEOUT_SECONDS", 0.01)
    try:
        backend.stop()
        assert live_worker.is_alive()
        assert backend._reader is live_worker
        assert backend._edge_worker is completed_worker
        assert backend._local_core is core
        assert backend._irq_callback is callback
        assert backend._emulator_owner is not None

        release.set()
        live_worker.join(timeout=1)
        assert not live_worker.is_alive()
        backend.stop()
        _assert_committed_cleanup(backend)
    finally:
        release.set()
        live_worker.join(timeout=1)
        backend.stop()
        peer.stop()


def test_stop_during_receiver_activation_preserves_startup_refs(monkeypatch):
    """A blocked first ``Thread.start`` cannot race cleanup into ``None``."""
    backend, peer = NetworkBackend.pair()
    core = _Core()
    callback = _callback_for(core)
    entered = threading.Event()
    release = threading.Event()
    start_errors = []
    original_start = threading.Thread.start

    def blocking_start(thread):
        if thread.name == "NetworkBackend.edge-worker":
            entered.set()
            assert release.wait(2)
        return original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", blocking_start)
    monkeypatch.setattr(network_backend_module, "_STOP_JOIN_TIMEOUT_SECONDS", 0.05)

    def activate():
        try:
            backend.start_receiver(core, callback)
        except BaseException as exc:  # noqa: BLE001
            start_errors.append(exc)

    starter = threading.Thread(target=activate, name="receiver-starter")
    starter.start()
    try:
        assert entered.wait(1)
        started = time.monotonic()
        backend.stop()
        assert time.monotonic() - started < 0.5
        assert backend._receiver_starting is True
        assert backend._edge_worker is not None
        assert backend._reader is not None
        assert backend._local_core is core
        assert backend._irq_callback is callback

        release.set()
        starter.join(2)
        assert not starter.is_alive()
        assert start_errors == []
        backend.stop()
        _assert_committed_cleanup(backend)
    finally:
        release.set()
        starter.join(2)
        backend.stop()
        peer.stop()


def test_owner_scope_releases_core_claim_before_stop_cleanup():
    backend, peer = NetworkBackend.pair()
    core = _Core()
    backend.start_receiver(core)
    try:
        with backend.owner_scope():
            assert core._callback is not None
            assert core._token is not None
        assert core._callback is None
        assert core._token is None
        backend.stop()
        _assert_committed_cleanup(backend)
    finally:
        backend.stop()
        peer.stop()


def test_owned_core_callback_cycle_becomes_collectable_after_stop():
    backend, peer = NetworkBackend.pair()
    core = _Core()
    core.backend = backend
    callback = _callback_for(core)
    backend.start_receiver(core, callback)
    core_ref = weakref.ref(core)
    try:
        backend.stop()
        _assert_committed_cleanup(backend)
        del callback
        del core
        for _ in range(3):
            gc.collect()
            if core_ref() is None:
                break
            time.sleep(0.01)
        assert core_ref() is None
    finally:
        backend.stop()
        peer.stop()


def test_current_worker_timeout_preserves_refs(monkeypatch):
    """A worker cannot join itself; its refs remain for a later caller."""
    backend, peer = NetworkBackend.pair()
    core = _Core()
    backend._local_core = core
    backend._irq_callback = _callback_for(core)
    done = threading.Event()

    def stop_from_worker():
        backend._reader = threading.current_thread()
        backend.stop()
        done.set()

    worker = threading.Thread(target=stop_from_worker, daemon=True)
    backend._edge_worker = worker
    worker.start()
    try:
        assert done.wait(1)
        assert backend._reader is worker
        assert backend._local_core is core
        # The current worker has now returned, so a separate stop can commit.
        backend.stop()
        _assert_committed_cleanup(backend)
    finally:
        backend.stop()
        peer.stop()


def test_real_serial_core_graph_is_collectable_after_stop():
    """Exercise cleanup with the vendored source or compiled Serial type."""
    from pyboy.core.serial import Serial

    class SerialHolder:
        def __init__(self):
            self.serial = Serial()
            self.backend = None

        def __getattr__(self, name):
            return getattr(self.serial, name)

    backend, peer = NetworkBackend.pair()
    core = SerialHolder()
    core.backend = backend
    callback = _callback_for(core)
    backend.start_receiver(core, callback)
    core_ref = weakref.ref(core)
    try:
        backend.stop()
        _assert_committed_cleanup(backend)
        del callback
        del core
        for _ in range(3):
            gc.collect()
            if core_ref() is None:
                break
            time.sleep(0.01)
        assert core_ref() is None
    finally:
        backend.stop()
        peer.stop()


class _BlockingCore:
    transfer_enabled = True
    internal_clock = False

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.applied = threading.Event()
        self.claimed = False
        self.owner_callback = None

    def claim_owner_pump(self, callback, poll=False):
        assert not self.claimed
        self.claimed = True
        self.owner_callback = callback
        return object()

    def set_owner_pump(self, callback, poll=False):
        self.owner_callback = callback

    def release_owner_pump(self, token):
        self.claimed = False
        self.owner_callback = None

    def check_error(self):
        return None

    def peek_out_bit(self):
        self.entered.set()
        assert self.release.wait(2)
        return 1

    def apply_external_edge(self, bit):
        self.applied.set()
        return True


def test_stop_defers_cleanup_until_foreground_owner_quiesces():
    """A concurrent stop cannot discard a callback during an edge."""
    peer, receiver = NetworkBackend.pair()
    core = _BlockingCore()
    irq_calls = []
    owner_errors = []
    callback = lambda: irq_calls.append(threading.get_ident())
    owner_holder = _Owner()
    receiver.bind_owner(owner_holder)
    receiver.start_receiver(core, callback)
    sent_frames = []

    def record_response(frame, *, deadline=None, before_send=None):
        sent_frames.append(frame)

    # The transport is intentionally cancelled while the foreground core
    # call is blocked.  Recording the already-committed response keeps this
    # regression about lifecycle ownership rather than a socket race.
    receiver._send_frame = record_response
    with receiver._incoming_lock:
        receiver._pending_edge = (0, time.monotonic())

    def owner_run():
        try:
            with receiver.owner_scope():
                while not core.entered.is_set():
                    receiver.owner_poll()
        except BaseException as exc:  # noqa: BLE001
            owner_errors.append(exc)

    owner = threading.Thread(target=owner_run, name="foreground-owner")
    owner.start()
    try:
        assert core.entered.wait(3)

        started = time.monotonic()
        receiver.stop()
        assert time.monotonic() - started < 0.5
        # Both transport workers have stopped, but the foreground scope is
        # still executing core.peek_out_bit.  The owned graph must remain
        # intact.
        assert receiver._active_owner_scopes == 1
        assert receiver._active_owner_executions == 1
        assert receiver._local_core is core
        assert receiver._irq_callback is callback
        assert receiver._emulator_owner is not None
        assert owner.is_alive()

        core.release.set()
        owner.join(3)
        assert not owner.is_alive()
        assert owner_errors == []
        assert core.applied.is_set()
        assert len(irq_calls) == 1
        assert receiver._active_owner_scopes == 0
        assert receiver._active_owner_executions == 0
        assert sent_frames
        # owner_scope's exit path completes the deferred cleanup without an
        # unbounded owner-lock acquisition in stop().
        _assert_committed_cleanup(receiver)
    finally:
        core.release.set()
        owner.join(3)
        receiver.stop()
        peer.stop()
