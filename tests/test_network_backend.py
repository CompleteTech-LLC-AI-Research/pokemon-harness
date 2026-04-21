"""Tests for :class:`NetworkBackend` (milestone 8).

Proves the TCP transport exchanges bits correctly; tests here don't
exercise the end-to-end two-process-two-PyBoy integration (that's a
separate tier with its own fixture complexity). The transport surface
itself is small enough to unit-test directly.
"""

from __future__ import annotations

import struct
import threading
import time

import pytest

from pokered_harness.link.network_backend import (
    NetworkBackend,
    NetworkBackendError,
)


# ---------------------------------------------------------------------------
# Pair construction + basic wire contract
# ---------------------------------------------------------------------------


def test_pair_constructs_without_raising():
    a, b = NetworkBackend.pair()
    try:
        assert a is not b
    finally:
        a.close()
        b.close()


def test_paired_backends_exchange_a_single_bit():
    """Spin a tiny peer thread that echoes our bit back inverted; send
    a request through ``on_edge`` and assert we got the inverted bit."""
    a, b = NetworkBackend.pair()

    def echo_inverse():
        # Read one frame, respond with inverted payload.
        frame = b._recv_exactly(2)
        _opcode, payload = struct.unpack(">BB", frame)
        reply = struct.pack(">BB", 0x10, (~payload) & 1)
        b._sock.sendall(reply)

    peer = threading.Thread(target=echo_inverse, daemon=True)
    peer.start()
    try:
        reply = a.on_edge(our_bit=1, our_role=1)
        assert reply == 0  # inverted of 1
    finally:
        peer.join(timeout=2.0)
        a.close()
        b.close()


def test_paired_backends_exchange_full_byte_bit_by_bit():
    """MSB-first send 0xAA; peer replies with 0x55. After 8 edges each
    side should have shifted in the other's bits in order."""
    a, b = NetworkBackend.pair()

    a_bits = [1, 0, 1, 0, 1, 0, 1, 0]  # 0xAA
    b_bits = [0, 1, 0, 1, 0, 1, 0, 1]  # 0x55
    received_by_a: list[int] = []
    received_by_b: list[int] = []

    def run_peer():
        """Peer acts like a manual coordinator: reads our bit, sends
        its bit, records what it received."""
        for bit in b_bits:
            frame = b._recv_exactly(2)
            _op, payload = struct.unpack(">BB", frame)
            received_by_b.append(payload)
            b._sock.sendall(struct.pack(">BB", 0x10, bit))

    peer = threading.Thread(target=run_peer, daemon=True)
    peer.start()
    try:
        for bit in a_bits:
            received_by_a.append(a.on_edge(our_bit=bit, our_role=1))
    finally:
        peer.join(timeout=5.0)
        a.close()
        b.close()

    assert received_by_a == b_bits
    assert received_by_b == a_bits


# ---------------------------------------------------------------------------
# Socket lifecycle
# ---------------------------------------------------------------------------


def test_close_is_idempotent():
    a, b = NetworkBackend.pair()
    a.close()
    a.close()  # no error
    b.close()


def test_peer_hangup_mid_edge_raises():
    """When the peer closes the socket, the next on_edge raises
    :class:`NetworkBackendError` instead of hanging."""
    a, b = NetworkBackend.pair()
    b.close()
    with pytest.raises(NetworkBackendError):
        a.on_edge(our_bit=1, our_role=1)
    a.close()


def test_peer_sends_wrong_opcode_raises():
    a, b = NetworkBackend.pair()

    def bad_peer():
        frame = b._recv_exactly(2)
        # Reply with wrong opcode.
        b._sock.sendall(struct.pack(">BB", 0xFF, 0))

    t = threading.Thread(target=bad_peer, daemon=True)
    t.start()
    try:
        with pytest.raises(NetworkBackendError, match="unexpected opcode"):
            a.on_edge(our_bit=0, our_role=1)
    finally:
        t.join(timeout=2.0)
        a.close()
        b.close()


# ---------------------------------------------------------------------------
# Real TCP listen/connect
# ---------------------------------------------------------------------------


def _free_port() -> int:
    import socket as _s
    s = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    try:
        return s.getsockname()[1]
    finally:
        s.close()


def test_network_backend_drives_serialcore_byte_exchange():
    """Two :class:`SerialCore` instances paired via :class:`NetworkBackend`
    (socketpair transport) exchange a full byte correctly.

    This is the proof that :class:`NetworkBackend` is a valid drop-in
    for :class:`CoordinatedBackend`: master-mode edges fire the backend,
    which round-trips one bit over TCP per edge, and both sides end up
    with the peer's byte in SB.

    The slave side is driven manually via ``apply_external_edge`` in
    this test (same pattern as the in-process coordinator's inline
    exchange), so we don't need a second independent PyBoy to prove
    the transport. A full two-process two-PyBoy trade over TCP is a
    follow-up milestone.
    """
    from pokered_harness.link.serial_core import (
        CYCLES_PER_BYTE_DMG,
        SerialCore,
    )

    ba, bb = NetworkBackend.pair()

    master = SerialCore(backend=ba)
    slave = SerialCore()  # no backend — driven by the other side's on_edge

    master.set_SB(0xAA)
    slave.set_SB(0x55)
    master.set_SC(0x81)  # internal clock
    slave.set_SC(0x80)   # external clock

    # Slave-side thread: read edges from the TCP socket and drive the
    # slave's SerialCore. In a two-process setup this would be the
    # peer process's main tick loop; here we do it inline in a thread.
    done = threading.Event()
    errors: list[BaseException] = []

    def slave_reader():
        try:
            for _ in range(8):
                frame = bb._recv_exactly(2)
                _op, master_bit = struct.unpack(">BB", frame)
                slave_out = slave.peek_out_bit()
                slave.apply_external_edge(master_bit & 1)
                bb._sock.sendall(struct.pack(">BB", 0x10, slave_out))
        except Exception as exc:
            errors.append(exc)
        finally:
            done.set()

    t = threading.Thread(target=slave_reader, daemon=True)
    t.start()

    try:
        # Master ticks — each edge fires ba.on_edge, which sends the
        # bit over TCP and reads the reply. After 8 edges, transfer
        # completes.
        irq = master.tick(CYCLES_PER_BYTE_DMG)
        assert irq is True, "master transfer didn't complete"
        assert done.wait(timeout=5.0), "slave reader thread hung"
        assert not errors, f"slave reader errored: {errors[0]}"
        assert master.SB == 0x55, f"master got 0x{master.SB:02x}, expected 0x55"
        assert slave.SB == 0xAA, f"slave got 0x{slave.SB:02x}, expected 0xAA"
    finally:
        ba.close()
        bb.close()


def test_listen_and_connect_over_loopback():
    """Spin up a listener in a thread, connect from the main thread,
    exchange a bit, confirm both sides saw the other's bit."""
    port = _free_port()
    server_holder: dict = {}

    def server():
        backend, listener = NetworkBackend.listen(port)
        server_holder["backend"] = backend
        server_holder["listener"] = listener
        # Echo inverted.
        frame = backend._recv_exactly(2)
        _op, payload = struct.unpack(">BB", frame)
        backend._sock.sendall(struct.pack(">BB", 0x10, (~payload) & 1))

    t = threading.Thread(target=server, daemon=True)
    t.start()
    # Give the listener a tick to bind. A small sleep is adequate here;
    # a proper ready-signal would be overkill for a localhost test.
    time.sleep(0.1)

    client = NetworkBackend.connect("127.0.0.1", port)
    try:
        reply = client.on_edge(our_bit=1, our_role=1)
        assert reply == 0
    finally:
        client.close()
        t.join(timeout=2.0)
        if "backend" in server_holder:
            server_holder["backend"].close()
        if "listener" in server_holder:
            server_holder["listener"].close()
