"""Tests for :class:`NetworkBackend` (milestone 8).

Proves the TCP transport exchanges bits correctly. Two layers of
coverage:

1. Raw protocol (``EDGE_REQ`` / ``EDGE_RESP``) via hand-rolled peer
   threads — asserts wire-level correctness and error paths.
2. End-to-end pairs of :class:`SerialCore` instances driven via the
   backend's reader threads — asserts NetworkBackend can substitute
   for :class:`CoordinatedBackend` without the peer being in the same
   process.
"""

from __future__ import annotations

import socket as _socket
import struct
import threading
import time
from contextlib import contextmanager

import pytest

import pokered_harness.link.network_backend as network_backend_module
from pokered_harness.link.network_backend import (
    NetworkBackend,
    NetworkBackendError,
)


_OP_EDGE_REQ = 0x10
_OP_EDGE_RESP = 0x11


@contextmanager
def _receiver_owner(backend, *, progress=None, allow_closed=False):
    """Standalone core tests explicitly run the receiver's execution owner."""
    ready, finish = threading.Event(), threading.Event()
    errors = []
    def receive():
        try:
            with backend.owner_scope():
                ready.set()
                while not finish.is_set():
                    backend.owner_poll()
                    if progress is not None:
                        progress()
                    finish.wait(.0005)
        except BaseException as error:
            errors.append(error)
    owner = threading.Thread(target=receive, name="test-receiver-owner", daemon=True)
    owner.start()
    try:
        assert ready.wait(2)
        yield
    finally:
        finish.set()
        owner.join(2)
        assert not owner.is_alive()
        assert errors == [] or (allow_closed and backend._closed and
                               all(isinstance(error, NetworkBackendError) for error in errors))


# ---------------------------------------------------------------------------
# Pair construction + basic wire contract
# ---------------------------------------------------------------------------


def test_pair_constructs_without_raising():
    a, b = NetworkBackend.pair()
    try:
        assert a is not b
    finally:
        a.stop()
        b.stop()


def test_close_is_idempotent():
    a, b = NetworkBackend.pair()
    a.close()
    a.close()  # no error
    b.close()


def test_versioned_handshake_reports_each_peer_rom():
    a_sock, b_sock = _socket.socketpair()
    a = NetworkBackend(a_sock, local_rom_version="red")
    b = NetworkBackend(b_sock, local_rom_version="blue")
    a.start_receiver(local_core=None)
    b.start_receiver(local_core=None)
    try:
        assert a.wait_for_hello(timeout=1.0) == "blue"
        assert b.wait_for_hello(timeout=1.0) == "red"
    finally:
        a.stop()
        b.stop()


def test_stop_wakes_blocked_edge_waiter():
    a, b = NetworkBackend.pair()
    a.start_receiver(local_core=None)
    result: list[Exception] = []

    def blocked_edge() -> None:
        try:
            a.on_edge(our_bit=1, our_role=1)
        except Exception as exc:  # noqa: BLE001
            result.append(exc)

    worker = threading.Thread(target=blocked_edge, daemon=True)
    worker.start()
    time.sleep(0.05)
    started = time.monotonic()
    a.stop()
    worker.join(timeout=1.0)
    elapsed = time.monotonic() - started
    try:
        assert not worker.is_alive()
        assert elapsed < 0.5
        assert result and isinstance(result[0], NetworkBackendError)
    finally:
        b.stop()


# ---------------------------------------------------------------------------
# Raw wire protocol: REQ / RESP round-trip
# ---------------------------------------------------------------------------


def test_on_edge_sends_REQ_and_waits_for_RESP():
    """Master-mode ``on_edge`` sends an ``EDGE_REQ`` and blocks on a
    matching ``EDGE_RESP`` from the peer. We simulate the peer's
    reader thread inline."""
    a, b = NetworkBackend.pair()

    def fake_peer_reader():
        # Read our REQ, respond with inverted bit.
        frame = b._recv_exactly(2)
        opcode, payload = struct.unpack(">BB", frame)
        assert opcode == _OP_EDGE_REQ, f"expected REQ, got 0x{opcode:02x}"
        b._sock.sendall(struct.pack(">BB", _OP_EDGE_RESP, (~payload) & 1))

    peer = threading.Thread(target=fake_peer_reader, daemon=True)
    peer.start()

    # Start our reader so the incoming RESP goes onto the queue.
    a.start_receiver(local_core=None, irq_callback=None)
    try:
        reply = a.on_edge(our_bit=1, our_role=1)
        assert reply == 0  # inverted of 1
    finally:
        peer.join(timeout=2.0)
        a.stop()
        b.stop()


def test_on_edge_with_peer_hangup_raises():
    """If the peer closes the socket before responding, ``on_edge``
    times out and raises :class:`NetworkBackendError`."""
    a, b = NetworkBackend.pair()
    a.start_receiver(local_core=None)
    b.stop()  # close peer
    # With the peer gone, our reader hits EOF and marks closed. on_edge's
    # send will fail; if it succeeds, the wait for RESP times out.
    with pytest.raises(NetworkBackendError):
        # Retry a few times in case send succeeds before reader notices.
        for _ in range(3):
            a.on_edge(our_bit=1, our_role=1)
    a.stop()


def test_edge_timeout_closes_transport_and_rejects_reuse(monkeypatch):
    """An admitted edge timeout must not leave an ambiguous socket alive."""
    monkeypatch.setattr(network_backend_module, "_EDGE_CALL_TIMEOUT_SECONDS", 0.05)
    a, b = NetworkBackend.pair()
    # B deliberately has no receiver, so A's request cannot get a response.
    a.start_receiver(local_core=None)
    try:
        with pytest.raises(NetworkBackendError, match="no EDGE_RESP"):
            a.on_edge(our_bit=1, our_role=1)
        assert a.connected is False
        started = time.monotonic()
        with pytest.raises(NetworkBackendError, match="backend closed"):
            a.on_edge(our_bit=0, our_role=1)
        assert time.monotonic() - started < 0.2
    finally:
        a.stop()
        b.stop()


@pytest.mark.parametrize("response_count", [1, 2])
def test_unsolicited_edge_response_fails_closed(response_count):
    """A late/duplicate response cannot be retained for a future edge."""
    a, b = NetworkBackend.pair()
    a.start_receiver(local_core=None)
    try:
        b._sock.sendall(struct.pack(">BB", _OP_EDGE_RESP, 1) * response_count)
        assert a._closed_event.wait(timeout=1.0)
        assert a.connected is False
        assert isinstance(a._reader_exc, NetworkBackendError)
        assert "duplicate" in str(a._reader_exc)
        with pytest.raises(NetworkBackendError, match="backend closed"):
            a.on_edge(0, 1)
        assert a.debug_snapshot()["edge_req_sent"] == 0
    finally:
        a.stop()
        b.stop()


def test_unknown_opcode_is_ignored():
    """The reader tolerates unknown opcodes (drops them) rather than
    hard-erroring. This keeps real-ROM runs robust to spurious noise.
    """
    a, b = NetworkBackend.pair()
    a.start_receiver(local_core=None)
    # Peer sends garbage opcode then a valid control frame. An unsolicited
    # response is an error independently of unknown-opcode compatibility.
    b._sock.sendall(struct.pack(">BB", 0xFF, 0))
    b._sock.sendall(struct.pack(">BB", 0x20, 1))

    try:
        reply = a._sync_queue(1).get(timeout=2.0)
        assert reply == 1
    finally:
        a.stop()
        b.stop()


# ---------------------------------------------------------------------------
# Backend drives a full SerialCore byte-exchange via two reader threads
# ---------------------------------------------------------------------------


def test_two_serialcores_exchange_byte_via_network_backend():
    """Canonical proof: two :class:`SerialCore` instances, each paired
    with its own :class:`NetworkBackend`, exchange a full byte over
    a socketpair. Master-mode edges on one side are handled by the
    peer's reader thread driving its local ``SerialCore`` — exactly
    the shape a two-process two-PyBoy setup needs.
    """
    from pokered_harness.link.serial_core import (
        CYCLES_PER_BYTE_DMG,
        SerialCore,
    )

    ba, bb = NetworkBackend.pair()

    master = SerialCore(backend=ba)
    slave = SerialCore()  # no outgoing backend; driven by bb's reader

    master.set_SB(0xAA)
    slave.set_SB(0x55)
    master.set_SC(0x81)  # internal clock
    slave.set_SC(0x80)   # external clock

    # Record slave-side IRQ fires.
    slave_irqs: list[int] = []

    def slave_irq():
        slave_irqs.append(1)

    ba.start_receiver(local_core=master)  # not strictly needed; keeps
                                           # reader idle for master side
    bb.start_receiver(local_core=slave, irq_callback=slave_irq)

    try:
        with ba.owner_scope(), _receiver_owner(bb):
            irq = master.tick(CYCLES_PER_BYTE_DMG)
        assert irq is True, "master didn't complete transfer"

        # Give bb's reader a moment to finish its final edge send.
        time.sleep(0.1)
        assert master.SB == 0x55, f"master got 0x{master.SB:02x}"
        assert slave.SB == 0xAA, f"slave got 0x{slave.SB:02x}"
        assert slave_irqs == [1], "slave IRQ callback should fire once"
    finally:
        ba.stop()
        bb.stop()


def test_multiple_bytes_exchange():
    """Two cores exchange three bytes in a row via the reader-thread
    model. Proves the backend works across multiple transfers without
    resetting."""
    from pokered_harness.link.serial_core import (
        CYCLES_PER_BYTE_DMG,
        SerialCore,
    )

    ba, bb = NetworkBackend.pair()
    master = SerialCore(backend=ba)
    slave = SerialCore()

    ba.start_receiver(local_core=master)
    bb.start_receiver(local_core=slave)

    try:
        for master_byte, slave_byte in [(0x01, 0xFE), (0xAA, 0x55), (0x42, 0x24)]:
            master.set_SB(master_byte)
            slave.set_SB(slave_byte)
            master.set_SC(0x81)
            slave.set_SC(0x80)
            with ba.owner_scope(), _receiver_owner(bb):
                master.tick(master.last_cycles + CYCLES_PER_BYTE_DMG)
            time.sleep(0.05)
            assert master.SB == slave_byte, (
                f"master expected 0x{slave_byte:02x}, got 0x{master.SB:02x}"
            )
            assert slave.SB == master_byte, (
                f"slave expected 0x{master_byte:02x}, got 0x{slave.SB:02x}"
            )
    finally:
        ba.stop()
        bb.stop()


# ---------------------------------------------------------------------------
# Real TCP listen/connect
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    try:
        return s.getsockname()[1]
    finally:
        s.close()


def test_sync_with_peer_rendezvous():
    """Both sides call sync_with_peer(id=N); each call returns only
    after the peer's matching SYNC arrives. Proves the barrier
    primitive for subprocess-level trade phase boundaries."""
    a, b = NetworkBackend.pair()
    a.start_receiver(local_core=None)
    b.start_receiver(local_core=None)

    a_done = threading.Event()
    b_done = threading.Event()

    def side(backend, done):
        backend.sync_with_peer(sync_id=7, timeout=5.0)
        done.set()

    ta = threading.Thread(target=side, args=(a, a_done), daemon=True)
    tb = threading.Thread(target=side, args=(b, b_done), daemon=True)
    ta.start()
    tb.start()
    try:
        assert a_done.wait(timeout=6.0), "side A never returned from sync"
        assert b_done.wait(timeout=6.0), "side B never returned from sync"
    finally:
        ta.join(timeout=1.0)
        tb.join(timeout=1.0)
        a.stop()
        b.stop()


def test_slave_response_send_failure_closes_socket_and_wakes_peer(monkeypatch):
    a, b = NetworkBackend.pair()
    original_socket = a._sock

    class FailingResponseSocket:
        def send(self, frame):
            raise OSError("response send failed")

        def shutdown(self, how):
            original_socket.shutdown(how)

        def close(self):
            original_socket.close()

    monkeypatch.setattr(a, "_sock", FailingResponseSocket())
    monkeypatch.setattr(network_backend_module, "_REARM_WAIT_SECONDS", 0.0)
    b._sock.settimeout(1.0)
    try:
        a._handle_edge_req(1)
        assert not a.connected
        assert original_socket.fileno() == -1
        assert b._sock.recv(1) == b""
        assert str(a._reader_exc) == "response send failed"
    finally:
        a.stop()
        b.stop()


def test_announce_sync_can_be_polled_without_blocking():
    """A peer can advertise a SYNC point and the other side can poll it."""
    a, b = NetworkBackend.pair()
    a.start_receiver(local_core=None)
    b.start_receiver(local_core=None)
    try:
        a.announce_sync(sync_id=9)
        deadline = time.time() + 2.0
        hit = False
        while time.time() < deadline and not hit:
            hit = b.poll_peer_sync(sync_id=9)
            time.sleep(0.01)
        assert hit is True
        assert b.poll_peer_sync(sync_id=9) is False
        assert a.debug_snapshot()["sync_sent"] == 1
        assert b.debug_snapshot()["sync_received"] == 1
        assert b.debug_snapshot()["sync_poll_hits"] == 1
    finally:
        a.stop()
        b.stop()


def test_sync_with_peer_times_out_on_silent_peer():
    """If the peer never sends SYNC, sync_with_peer raises
    :class:`NetworkBackendError` after the timeout."""
    a, b = NetworkBackend.pair()
    a.start_receiver(local_core=None)
    # Peer's reader not started; B never sends its SYNC back.
    try:
        with pytest.raises(NetworkBackendError, match="no peer OP_SYNC"):
            a.sync_with_peer(sync_id=1, timeout=1.0)
    finally:
        a.stop()
        b.stop()


def test_keepalive_fallback_is_visible_in_debug_snapshot():
    """An unarmed slave responds with keep-alive and records that fact."""
    a, b = NetworkBackend.pair()
    a.start_receiver(local_core=None)
    b.start_receiver(local_core=None)
    try:
        reply = a.on_edge(our_bit=1, our_role=1)
        assert reply == 1
        deadline = time.time() + 1.0
        snap = b.debug_snapshot()
        while time.time() < deadline and snap["edge_resp_sent"] == 0:
            time.sleep(0.01)
            snap = b.debug_snapshot()
        assert snap["edge_req_received"] == 1
        assert snap["edge_resp_sent"] == 1
        assert snap["slave_rearm_waits"] == 1
        assert snap["keepalive_bits_sent"] == 1
        assert snap["keepalive_bytes_started"] == 1
        assert snap["last_keepalive_state"] == {"core_present": False}
    finally:
        a.stop()
        b.stop()


class _OwnerClaimTestDouble:
    """Explicit test-only owner binding for non-native core behavior doubles."""

    def __init__(self) -> None:
        self.owner_poll_enabled = False
        self._owner_callback = None
        self._owner_token = None

    def set_owner_pump(self, callback, poll=False):
        if self._owner_token is not None:
            raise RuntimeError("serial owner pump is exclusively claimed")
        self._owner_callback = callback
        self.owner_poll_enabled = callback is not None and poll

    def claim_owner_pump(self, callback, poll=False):
        if self._owner_callback is not None or self._owner_token is not None:
            raise RuntimeError("serial owner pump already installed or claimed")
        self._owner_token = object()
        self._owner_callback = callback
        self.owner_poll_enabled = poll
        return self._owner_token

    def release_owner_pump(self, token):
        if token is not self._owner_token:
            raise RuntimeError("invalid serial owner pump claim token")
        self._owner_token = None
        self._owner_callback = None
        self.owner_poll_enabled = False


class _CompletingSlaveCore(_OwnerClaimTestDouble):
    def __init__(self) -> None:
        super().__init__()
        self.transfer_enabled = 1
        self.internal_clock = 0
        self.SB = 0
        self.SC = 0x80

    def peek_out_bit(self) -> int:
        return 0

    def apply_external_edge(self, peer_bit: int) -> bool:
        self.transfer_enabled = 0
        self.SB = peer_bit & 1
        return True


def test_post_byte_missing_rearm_fails_without_fabricating_payload(monkeypatch):
    """An attached core cannot synthesize a response while it is unarmed."""
    monkeypatch.setattr(network_backend_module, "_EDGE_CALL_TIMEOUT_SECONDS", .08)
    a, b = NetworkBackend.pair()
    core = _CompletingSlaveCore()
    a.start_receiver(local_core=None)
    b.start_receiver(local_core=core)
    try:
        with _receiver_owner(b, allow_closed=True):
            first_reply = a.on_edge(our_bit=1, our_role=1)
            assert first_reply == 0
            with pytest.raises(NetworkBackendError):
                a.on_edge(our_bit=0, our_role=1)
        snap = b.debug_snapshot()
        assert snap["slave_post_byte_rearm_waits"] >= 1
        assert snap["keepalive_after_post_byte_waits"] == 0
        assert snap["keepalive_bits_sent"] == 0
        assert snap["last_slave_byte_complete_at"] is not None
    finally:
        a.stop()
        b.stop()


class _LateRearmingSlaveCore(_OwnerClaimTestDouble):
    def __init__(self) -> None:
        super().__init__()
        self._transfer_enabled = 1
        self._unarmed_observed = threading.Event()
        self.internal_clock = 0
        self.SB = 0
        self.SC = 0x80
        self._next_out_bit = 0
        self._byte_index = 0

    @property
    def transfer_enabled(self) -> int:
        if not self._transfer_enabled:
            self._unarmed_observed.set()
        return self._transfer_enabled

    @transfer_enabled.setter
    def transfer_enabled(self, enabled: int) -> None:
        self._transfer_enabled = enabled

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

    def rearm_after(self, delay_s: float, *, next_out_bit: int) -> threading.Thread:
        def _rearm() -> None:
            # Begin the simulated IRQ delay only after the backend observes
            # the next request's unarmed core. A paused test thread must not
            # let this worker rearm before that request is even admitted.
            if not self._unarmed_observed.wait(timeout=2.0):
                return
            time.sleep(delay_s)
            self._next_out_bit = next_out_bit & 1
            self.transfer_enabled = 1

        worker = threading.Thread(target=_rearm, daemon=True)
        worker.start()
        return worker

    def owner_rearm_after(self, delay_s: float, *, next_out_bit: int):
        deadline = None
        def progress():
            nonlocal deadline
            if self._unarmed_observed.is_set():
                if deadline is None:
                    deadline = time.monotonic() + delay_s
                if time.monotonic() >= deadline:
                    self._next_out_bit = next_out_bit & 1
                    self.transfer_enabled = 1
        return progress


def test_post_byte_rearm_grace_accepts_late_real_byte_without_keepalive():
    """A slave that rearms after the default wait but within the post-byte
    grace window should send its real next byte, not 0xFE keep-alive."""
    a, b = NetworkBackend.pair()
    core = _LateRearmingSlaveCore()
    a.start_receiver(local_core=None)
    b.start_receiver(local_core=core)
    rearm_worker = None
    try:
        with _receiver_owner(b, progress=core.owner_rearm_after(.150, next_out_bit=0)):
            first_reply = a.on_edge(our_bit=1, our_role=1)
            assert first_reply == 0
            started = time.monotonic()
            second_reply = a.on_edge(our_bit=0, our_role=1)
            elapsed = time.monotonic() - started

        assert second_reply == 0
        assert elapsed >= 0.140

        snap = b.debug_snapshot()
        assert snap["slave_post_byte_rearm_waits"] >= 1
        assert snap["slave_post_byte_rearm_successes"] >= 1
        assert snap["keepalive_after_post_byte_waits"] == 0
        assert snap["keepalive_bits_sent"] == 0
    finally:
        a.stop()
        b.stop()
        if rearm_worker is not None:
            rearm_worker.join(timeout=2.0)
            assert not rearm_worker.is_alive(), "late-rearm worker did not stop"


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
        with backend.owner_scope():
            deadline = time.monotonic() + 2.0
            while core.transfer_enabled and time.monotonic() < deadline:
                backend.owner_poll()
                time.sleep(.0005)

    t = threading.Thread(target=server, daemon=True)
    t.start()

    client_backend = NetworkBackend.connect("127.0.0.1", port)
    assert ready.wait(timeout=2.0), "server didn't finish setup"
    client_core = SerialCore(backend=client_backend)
    client_core.set_SB(0xCC)
    client_core.set_SC(0x81)
    client_backend.start_receiver(local_core=client_core)
    try:
        with client_backend.owner_scope():
            client_core.tick(CYCLES_PER_BYTE_DMG)
        time.sleep(0.2)
        assert client_core.SB == 0x33
        assert server_holder["core"].SB == 0xCC
    finally:
        client_backend.stop()
        t.join(timeout=2.0)
        if "backend" in server_holder:
            server_holder["backend"].stop()
        try:
            listener.close()
        except OSError:
            pass


def _wait_bound(port: int, timeout: float = 2.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            s.settimeout(0.05)
            s.connect(("127.0.0.1", port))
            s.close()
            return True
        except (OSError, _socket.timeout):
            time.sleep(0.05)
    return False
