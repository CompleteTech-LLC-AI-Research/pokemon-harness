"""Keepalive fallback, post-byte rearm and loopback exchange (#128).

Split from ``tests/test_network_backend.py`` for #128 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module. The shared ``_post_byte_rearm`` fixture stays with the two
tests that use it.
"""

from __future__ import annotations

import socket as _socket
import threading
import time
from types import SimpleNamespace

import pytest

from pokered_harness.link import network_backend as network_module
from pokered_harness.link.network_backend import NetworkBackend


def _free_port() -> int:
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    try:
        return s.getsockname()[1]
    finally:
        s.close()


def test_post_byte_fallback_is_visible_in_debug_snapshot(_post_byte_rearm):
    """A CPU-starved slave stays unarmed through grace and sends keep-alive."""
    schedule = _post_byte_rearm
    # Model the peer getting no CPU through the actual grace deadline. This
    # remains a valid fallback case even though caller timing was misleading.
    schedule.now = schedule.backend._post_byte_rearm_until + 0.001
    assert schedule.core.transfer_enabled == 0
    schedule.release.set()
    assert schedule.finish() == 1
    snap = schedule.backend.debug_snapshot()
    assert snap["slave_post_byte_rearm_waits"] >= 1
    assert snap["slave_post_byte_rearm_successes"] == 0
    assert snap["keepalive_after_post_byte_waits"] == 1
    assert snap["keepalive_bits_sent"] == 1
    assert snap["last_slave_byte_complete_at"] is not None
    assert schedule.core._byte_index == 1  # fallback did not apply a real edge

    # Once the starved peer runs again, the next request must use real data.
    schedule.core.rearm(next_out_bit=0)
    assert schedule.master.on_edge(our_bit=0, our_role=1) == 0
    schedule.backend.wait_for_wire_idle(timeout=1.0)
    assert schedule.core._byte_index == 2
    assert schedule.backend.debug_snapshot()["keepalive_bits_sent"] == 1


class _LateRearmingSlaveCore:
    def __init__(self) -> None:
        self.transfer_enabled = 1
        self.internal_clock = 0
        self.SB = 0
        self.SC = 0x80
        self._next_out_bit = 0
        self._byte_index = 0

    def peek_out_bit(self) -> int:
        return self._next_out_bit

    def apply_external_edge(self, peer_bit: int) -> bool:
        self.SB = peer_bit & 1
        if self._byte_index == 0:
            self.transfer_enabled = 0
            self._byte_index += 1
            return True
        self._byte_index += 1
        return False

    def rearm(self, *, next_out_bit: int) -> None:
        self._next_out_bit = next_out_bit & 1
        self.transfer_enabled = 1


class _RearmSchedule:
    """Freeze only the slave worker's clock at an acknowledged rearm poll."""

    def __init__(self, master, backend, core):
        self.master = master
        self.backend = backend
        self.core = core
        self.now = time.monotonic()
        self.wait_entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.replies = []
        self.errors = []

    def monotonic(self):
        if threading.current_thread() is self.backend._edge_worker:
            return self.now
        return time.monotonic()

    def request(self):
        try:
            self.replies.append(self.master.on_edge(our_bit=0, our_role=1))
        except BaseException as exc:  # noqa: BLE001 - surface worker failures
            self.errors.append(exc)
        finally:
            self.finished.set()

    def finish(self):
        assert self.finished.wait(timeout=2.0), "second edge did not finish"
        assert self.errors == []
        assert len(self.replies) == 1
        # _edge_pending is decremented/notified in the worker's finally block,
        # after response accounting and IRQ callbacks, not just socket delivery.
        self.backend.wait_for_wire_idle(timeout=1.0)
        return self.replies[0]


@pytest.fixture
def _post_byte_rearm(monkeypatch):
    a, b = NetworkBackend.pair()
    core = _LateRearmingSlaveCore()
    schedule = _RearmSchedule(a, b, core)
    sender = threading.Thread(target=schedule.request, daemon=True)
    original_wait = b._closed_event.wait

    def rearm_poll(timeout=None):
        if threading.current_thread() is b._edge_worker:
            # This is the actual unarmed rearm-loop poll, after its deadline
            # and phase have been chosen. Never hold the serial operation gate.
            assert timeout is not None and 0 < timeout <= 0.0005
            schedule.wait_entered.set()
            assert schedule.release.wait(timeout=2.0), "rearm poll was not released"
            return original_wait(timeout=0)
        return original_wait(timeout=timeout)

    with monkeypatch.context() as patch:
        # Do not patch the process-wide time module: Event/Condition watchdogs
        # and every thread except this slave worker retain real monotonic time.
        patch.setattr(network_module, "time", SimpleNamespace(monotonic=schedule.monotonic))
        patch.setattr(b._closed_event, "wait", rearm_poll)
        try:
            a.start_receiver(local_core=None)
            b.start_receiver(local_core=core)
            assert a.on_edge(our_bit=1, our_role=1) == 0
            b.wait_for_wire_idle(timeout=1.0)
            assert core.transfer_enabled == 0
            sender.start()
            assert schedule.wait_entered.wait(timeout=2.0), "slave never entered rearm wait"
            assert not schedule.finished.is_set()
            assert b.debug_snapshot()["slave_post_byte_rearm_waits"] == 1
            yield schedule
        finally:
            # Restore real time before shutdown, including on a failed barrier,
            # so neither transport cleanup nor a join can inherit a frozen clock.
            patch.undo()
            schedule.release.set()
            try:
                a.stop()
            finally:
                b.stop()
                if sender.ident is not None:
                    sender.join(timeout=2.0)
                    assert not sender.is_alive()
                for backend in (a, b):
                    for worker in (backend._reader, backend._edge_worker):
                        assert worker is None or not worker.is_alive()


class _DisarmingSlaveCore:
    """Become unarmed between the worker's readiness checks."""

    def __init__(self) -> None:
        self._transfer_reads = 0
        self.internal_clock = 0
        self.SB = 0
        self.SC = 0x80

    @property
    def transfer_enabled(self) -> int:
        self._transfer_reads += 1
        return 1 if self._transfer_reads == 1 else 0


def test_rearm_race_falls_back_to_keepalive_without_worker_crash():
    """A transfer ending during dispatch must not reference an unbound phase."""
    a, b = NetworkBackend.pair()
    core = _DisarmingSlaveCore()
    a.start_receiver(local_core=None)
    b.start_receiver(local_core=core)
    try:
        assert a.on_edge(our_bit=1, our_role=1) == 1
        b.wait_for_wire_idle(timeout=1.0)
        assert b.connected
        snapshot = b.debug_snapshot()
        assert snapshot["keepalive_bits_sent"] == 1
        assert snapshot["edge_resp_sent"] == 1
    finally:
        a.stop()
        b.stop()


def test_post_byte_rearm_grace_accepts_late_real_byte_without_keepalive(_post_byte_rearm):
    """A slave that rearms after the default wait but within the post-byte
    grace window should send its real next byte, not 0xFE keep-alive."""
    schedule = _post_byte_rearm
    # Entry is already acknowledged. Unlike timing the caller after starting
    # a rearm thread, this interval belongs to the worker's actual wait.
    started = schedule.now
    schedule.now += 0.150
    assert schedule.now - started > network_module._REARM_WAIT_SECONDS
    assert schedule.now < schedule.backend._post_byte_rearm_until
    assert not schedule.finished.is_set()
    schedule.core.rearm(next_out_bit=0)
    schedule.release.set()
    assert schedule.finish() == 0

    snap = schedule.backend.debug_snapshot()
    assert snap["slave_post_byte_rearm_waits"] >= 1
    assert snap["slave_post_byte_rearm_successes"] >= 1
    assert snap["keepalive_after_post_byte_waits"] == 0
    assert snap["keepalive_bits_sent"] == 0
    assert schedule.core._byte_index == 2


def test_listen_and_connect_over_loopback_exchange_byte():
    """Full loopback: one thread listens, one connects, each gets a
    :class:`NetworkBackend`, and they exchange a byte."""
    from pokered_harness.link.serial_core import (
        CYCLES_PER_BYTE_DMG,
        SerialCore,
    )

    port = _free_port()
    server_holder: dict = {}
    ready = threading.Event()
    release_owner = threading.Event()

    # Pre-bind the listener in main thread so we can reliably connect
    # once the server thread calls accept().
    listener = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    listener.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(1)

    def server():
        conn, _ = listener.accept()
        conn.setsockopt(_socket.IPPROTO_TCP, _socket.TCP_NODELAY, 1)
        backend = NetworkBackend(conn)
        server_holder["backend"] = backend
        core = SerialCore()
        core.set_SB(0x33)
        core.set_SC(0x80)
        backend.start_receiver(local_core=core)
        server_holder["core"] = core
        ready.set()
        # Keep the real owner-thread stall: the receiver must exchange the
        # armed byte while its setup/owner thread is not progressing.
        release_owner.wait(timeout=10.0)

    t = threading.Thread(target=server, daemon=True)
    t.start()

    client_backend = NetworkBackend.connect("127.0.0.1", port)
    assert ready.wait(timeout=2.0), "server didn't finish setup"
    client_core = SerialCore(backend=client_backend)
    client_core.set_SB(0xCC)
    client_core.set_SC(0x81)
    client_backend.start_receiver(local_core=client_core)
    try:
        client_core.tick(CYCLES_PER_BYTE_DMG)
        server_holder["backend"].wait_for_wire_idle(timeout=1.0)
        assert client_core.SB == 0x33
        assert server_holder["core"].SB == 0xCC
    finally:
        release_owner.set()
        client_backend.stop()
        t.join(timeout=2.0)
        if "backend" in server_holder:
            server_holder["backend"].stop()
        try:
            listener.close()
        except OSError:
            pass
        assert not t.is_alive()


def _wait_bound(port: int, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            s.settimeout(0.05)
            s.connect(("127.0.0.1", port))
            s.close()
            return True
        except (TimeoutError, OSError):
            time.sleep(0.05)
    return False
