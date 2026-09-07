"""Tests for the peer-to-peer serial-link transport."""

from __future__ import annotations

import socket
import threading
import time

import pytest

from pokered_harness.link.serial_link import (
    InProcessSerialLink,
    SerialLinkClosed,
    SerialLinkError,
    SerialLinkProtocolError,
    SerialLinkTimeout,
    TcpSerialLink,
)


# --- helpers ---------------------------------------------------------------


def _free_port() -> int:
    """Grab an unused local TCP port by binding to 0 and reading back."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _make_tcp_pair(
    a_rom: str = "blue", b_rom: str = "blue"
) -> tuple[TcpSerialLink, TcpSerialLink]:
    """Spin up a listener + connector on localhost and return both ends."""
    port = _free_port()
    holder: dict[str, TcpSerialLink] = {}

    def _listen() -> None:
        holder["server"] = TcpSerialLink.listen(port, a_rom)

    t = threading.Thread(target=_listen, daemon=True)
    t.start()
    # Give the listener a moment to bind before we connect.
    for _ in range(50):
        time.sleep(0.01)
        if t.is_alive() is False or "server" in holder:
            break
    client = TcpSerialLink.connect("127.0.0.1", port, b_rom)
    t.join(timeout=2.0)
    server = holder["server"]
    return server, client


# --- InProcessSerialLink ---------------------------------------------------


def test_in_process_exchange_round_trip():
    a, b = InProcessSerialLink.pair("blue", "yellow")

    def peer_work():
        reply = b.exchange("exchange_bytes/wSerialPlayerDataBlock", b"\x11\x22\x33")
        assert reply == b"\xAA\xBB\xCC"

    t = threading.Thread(target=peer_work, daemon=True)
    t.start()
    got = a.exchange(
        "exchange_bytes/wSerialPlayerDataBlock", b"\xAA\xBB\xCC", timeout_ms=2000
    )
    assert got == b"\x11\x22\x33"
    t.join(timeout=2.0)


def test_in_process_rom_versions_swap():
    a, b = InProcessSerialLink.pair("red", "yellow")
    assert a.peer_rom_version == "yellow"
    assert b.peer_rom_version == "red"


def test_in_process_fifo_within_kind():
    """Two back-to-back exchanges of the same kind pair up in order."""
    a, b = InProcessSerialLink.pair("blue", "blue")
    results_b: list[bytes] = []

    def peer_work():
        results_b.append(b.exchange("nybble", b"\x10"))
        results_b.append(b.exchange("nybble", b"\x20"))

    t = threading.Thread(target=peer_work, daemon=True)
    t.start()
    r1 = a.exchange("nybble", b"\x01", timeout_ms=2000)
    r2 = a.exchange("nybble", b"\x02", timeout_ms=2000)
    t.join(timeout=2.0)
    assert r1 == b"\x10"
    assert r2 == b"\x20"
    assert results_b == [b"\x01", b"\x02"]


def test_in_process_different_kinds_dont_cross():
    """Different ``kind`` queues are independent: a ``foo`` exchange on
    one peer will never deliver into the other's ``bar`` queue, even
    if the ``bar`` caller arrives first.

    Note on ordering: if both peers are making sequential exchanges
    (one at a time, blocking), they must call matching kinds in the
    same order — otherwise they deadlock, each waiting on a queue the
    other hasn't produced into yet. The test fires ``foo`` on A before
    B posts its matching ``foo``, but A's post itself unblocks B's
    ``foo`` receive — so FIFO-within-kind still holds."""
    a, b = InProcessSerialLink.pair("blue", "blue")

    def peer_work():
        assert b.exchange("foo", b"\xAA") == b"\x01"
        assert b.exchange("bar", b"\xBB") == b"\x02"

    t = threading.Thread(target=peer_work, daemon=True)
    t.start()
    # Match peer's order (foo then bar).
    got_foo = a.exchange("foo", b"\x01", timeout_ms=2000)
    got_bar = a.exchange("bar", b"\x02", timeout_ms=2000)
    t.join(timeout=2.0)
    assert got_foo == b"\xAA"
    assert got_bar == b"\xBB"


def test_in_process_timeout_when_peer_silent():
    a, _b = InProcessSerialLink.pair("blue", "blue")
    with pytest.raises(SerialLinkTimeout):
        a.exchange("orphan", b"\x00", timeout_ms=50)


def test_in_process_closed_after_close():
    a, b = InProcessSerialLink.pair("blue", "blue")
    a.close()
    assert not a.connected
    with pytest.raises(SerialLinkClosed):
        a.exchange("nope", b"\x00", timeout_ms=100)
    # Peer side is independent and still considered "connected" until
    # its own close().
    assert b.connected


# --- TcpSerialLink ---------------------------------------------------------


def test_tcp_exchange_round_trip():
    server, client = _make_tcp_pair(a_rom="blue", b_rom="yellow")
    try:
        # Cross-version HELLO negotiated both ways.
        assert server.peer_rom_version == "yellow"
        assert client.peer_rom_version == "blue"

        results_server: list[bytes] = []

        def server_work():
            results_server.append(
                server.exchange("exchange_bytes/wSerialPlayerDataBlock", b"\x11" * 5)
            )

        t = threading.Thread(target=server_work, daemon=True)
        t.start()
        got = client.exchange(
            "exchange_bytes/wSerialPlayerDataBlock", b"\xAA" * 5, timeout_ms=2000
        )
        t.join(timeout=2.0)
        assert got == b"\x11" * 5
        assert results_server == [b"\xAA" * 5]
    finally:
        server.close()
        client.close()


def test_tcp_larger_payload():
    """~1 KiB payload — party data blocks are in this range."""
    server, client = _make_tcp_pair()
    try:
        s_payload = bytes(range(256)) * 4  # 1024 bytes, varied
        c_payload = bytes(reversed(range(256))) * 4

        def server_work():
            assert server.exchange("blob", s_payload) == c_payload

        t = threading.Thread(target=server_work, daemon=True)
        t.start()
        got = client.exchange("blob", c_payload, timeout_ms=3000)
        t.join(timeout=3.0)
        assert got == s_payload
    finally:
        server.close()
        client.close()


def test_tcp_timeout_when_peer_silent():
    server, client = _make_tcp_pair()
    try:
        with pytest.raises(SerialLinkTimeout):
            client.exchange("never", b"\x00", timeout_ms=50)
    finally:
        server.close()
        client.close()


def test_tcp_peer_close_causes_closed_error():
    server, client = _make_tcp_pair()
    server.close()
    # Give the client's reader thread a moment to notice the close.
    for _ in range(50):
        time.sleep(0.01)
        if not client.connected:
            break
    assert not client.connected
    with pytest.raises((SerialLinkClosed, SerialLinkError)):
        client.exchange("x", b"\x00", timeout_ms=500)
    client.close()


def test_tcp_close_wakes_inflight_exchange():
    """Closing a peer must release an exchange waiter before its deadline."""
    server, client = _make_tcp_pair()
    outcome = []

    def exchange_worker():
        try:
            client.exchange("inflight", b"x", timeout_ms=5000)
        except Exception as exc:  # noqa: BLE001
            outcome.append(exc)

    worker = threading.Thread(target=exchange_worker)
    worker.start()
    time.sleep(0.05)
    server.close()
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], SerialLinkClosed)
    client.close()


def test_tcp_rejects_malformed_frame():
    """Hand-craft a bogus frame and confirm the reader thread raises."""
    server, client = _make_tcp_pair()
    try:
        # Write directly to the socket to bypass _send_frame framing.
        # Zero-length frame is explicitly rejected.
        with client._write_lock:
            client._sock.sendall(b"\x00\x00\x00\x00")
        # Peer's reader thread should flag a protocol error and close.
        for _ in range(50):
            time.sleep(0.01)
            if server._reader_exc is not None:
                break
        assert isinstance(server._reader_exc, SerialLinkProtocolError)
    finally:
        server.close()
        client.close()
