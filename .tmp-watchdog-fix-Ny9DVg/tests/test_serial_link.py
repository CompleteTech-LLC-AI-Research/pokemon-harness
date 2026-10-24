"""Tests for the peer-to-peer serial-link transport."""

from __future__ import annotations

import math
import select
import socket
import struct
import threading
import time

import pytest

from pokered_harness.link import serial_link as serial_link_module
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


def _send_raw(sock: socket.socket, data: bytes) -> None:
    """Send raw bytes through a non-blocking test socket."""
    view = memoryview(data)
    deadline = time.monotonic() + 1.0
    while view:
        try:
            sent = sock.send(view)
        except (BlockingIOError, InterruptedError):
            remaining = deadline - time.monotonic()
            assert remaining > 0, "test socket did not become writable"
            select.select([], [sock], [sock], min(0.05, remaining))
            continue
        assert sent > 0, "test socket made no send progress"
        view = view[sent:]


def _send_wire_frame(sock: socket.socket, body: bytes) -> None:
    """Send one complete length-prefixed frame through a raw test socket."""
    _send_raw(sock, struct.pack(">I", len(body)) + body)


def _wait_for_reader_stop(link: TcpSerialLink, timeout_s: float = 1.0) -> None:
    """Wait for a link reader to finish without allowing an unbounded test."""
    deadline = time.monotonic() + timeout_s
    while link._reader.is_alive() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert not link._reader.is_alive(), "serial-link reader did not stop before deadline"


def _make_tcp_pair(a_rom: str = "blue", b_rom: str = "blue") -> tuple[TcpSerialLink, TcpSerialLink]:
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
        assert reply == b"\xaa\xbb\xcc"

    t = threading.Thread(target=peer_work, daemon=True)
    t.start()
    got = a.exchange("exchange_bytes/wSerialPlayerDataBlock", b"\xaa\xbb\xcc", timeout_ms=2000)
    assert got == b"\x11\x22\x33"
    t.join(timeout=2.0)


def test_in_process_rejects_payload_above_wire_limit():
    a, b = InProcessSerialLink.pair("blue", "yellow")
    try:
        with pytest.raises(ValueError, match="exchange payload is too long"):
            a.exchange("oversized", b"x" * (0xFFFF + 1), timeout_ms=50)
        assert a.connected
        assert b.connected
    finally:
        a.close()
        b.close()


def test_in_process_inbound_budget_fails_closed_and_drains_queues(monkeypatch):
    monkeypatch.setattr(serial_link_module, "_MAX_INBOUND_BYTES", 3)
    a, b = InProcessSerialLink.pair("blue", "yellow")
    results: list[Exception] = []

    def send(kind: str) -> None:
        try:
            b.exchange(kind, b"xx", timeout_ms=1000)
        except SerialLinkError as exc:  # the first sender is woken by the second
            results.append(exc)

    threads = [threading.Thread(target=send, args=(kind,), daemon=True) for kind in ("one", "two")]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=1.0)
        assert all(not thread.is_alive() for thread in threads)
        assert not a.connected
        assert not b.connected
        assert any(isinstance(exc, SerialLinkProtocolError) for exc in results)
        assert a._inbound_budget.frame_count == 0
        assert a._inbound_budget.byte_count == 0
        assert all(q.empty() for q in a._in.values())
    finally:
        a.close()
        b.close()


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


def test_in_process_same_kind_exchanges_are_single_flight():
    """Local callers cannot race for an unidentifiable v1 response."""
    a, b = InProcessSerialLink.pair("blue", "blue")
    results: dict[str, object] = {}
    threads: list[threading.Thread] = []
    try:

        def call_first() -> None:
            try:
                results["first"] = a.exchange("same", b"first", timeout_ms=2000)
            except Exception as exc:  # noqa: BLE001 - assert the worker outcome below
                results["first"] = exc

        def call_second() -> None:
            try:
                results["second"] = a.exchange("same", b"second", timeout_ms=2000)
            except Exception as exc:  # noqa: BLE001 - assert the worker outcome below
                results["second"] = exc

        first = threading.Thread(target=call_first, daemon=True)
        first.start()
        threads.append(first)
        deadline = time.monotonic() + 1.0
        while a._out["same"].qsize() < 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert a._out["same"].qsize() == 1

        second_started = threading.Event()

        def call_second_started() -> None:
            second_started.set()
            call_second()

        second = threading.Thread(target=call_second_started, daemon=True)
        second.start()
        threads.append(second)
        assert second_started.wait(timeout=1.0)
        time.sleep(0.1)
        assert a._out["same"].qsize() == 1

        peer_results: list[bytes] = []

        def peer_work() -> None:
            peer_results.append(b.exchange("same", b"peer-1", timeout_ms=2000))
            peer_results.append(b.exchange("same", b"peer-2", timeout_ms=2000))

        peer = threading.Thread(target=peer_work, daemon=True)
        peer.start()
        threads.append(peer)
        for thread in threads:
            thread.join(timeout=2.0)

        assert all(not thread.is_alive() for thread in threads)
        assert results == {"first": b"peer-1", "second": b"peer-2"}
        assert peer_results == [b"first", b"second"]
    finally:
        a.close()
        b.close()
        for thread in threads:
            thread.join(timeout=1.0)


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
        assert b.exchange("foo", b"\xaa") == b"\x01"
        assert b.exchange("bar", b"\xbb") == b"\x02"

    t = threading.Thread(target=peer_work, daemon=True)
    t.start()
    # Match peer's order (foo then bar).
    got_foo = a.exchange("foo", b"\x01", timeout_ms=2000)
    got_bar = a.exchange("bar", b"\x02", timeout_ms=2000)
    t.join(timeout=2.0)
    assert got_foo == b"\xaa"
    assert got_bar == b"\xbb"


def test_in_process_timeout_when_peer_silent():
    a, b = InProcessSerialLink.pair("blue", "blue")
    with pytest.raises(SerialLinkTimeout):
        a.exchange("orphan", b"\x00", timeout_ms=50)
    assert not a.connected
    assert not b.connected
    with pytest.raises(SerialLinkClosed):
        b.exchange("orphan", b"\x00", timeout_ms=50)


def test_in_process_closed_after_close():
    a, b = InProcessSerialLink.pair("blue", "blue")
    a.close()
    assert not a.connected
    with pytest.raises(SerialLinkClosed):
        a.exchange("nope", b"\x00", timeout_ms=100)
    # Closing one endpoint disconnects the shared in-process cable, matching
    # the EOF behavior observed by the peer of a TCP link.
    assert not b.connected


def test_in_process_peer_close_wakes_exchange_waiter_promptly():
    a, b = InProcessSerialLink.pair("blue", "blue")
    started = threading.Event()
    result: list[Exception] = []

    def blocked_exchange() -> None:
        started.set()
        try:
            a.exchange("blocked", b"x", timeout_ms=5000)
        except Exception as exc:  # noqa: BLE001
            result.append(exc)

    worker = threading.Thread(target=blocked_exchange, daemon=True)
    worker.start()
    assert started.wait(timeout=1.0)
    b.close()
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert result and isinstance(result[0], SerialLinkClosed)


def test_in_process_exchange_cancelled_before_send_is_reusable():
    a, b = InProcessSerialLink.pair("blue", "blue")
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(SerialLinkClosed, match="cancelled"):
        a.exchange("cancel", b"local", timeout_ms=500, cancel_event=cancel)

    assert a.connected
    assert b.connected
    cancel.clear()
    peer_result: list[bytes] = []

    def peer_work() -> None:
        peer_result.append(b.exchange("cancel", b"peer", timeout_ms=1000))

    worker = threading.Thread(target=peer_work, daemon=True)
    worker.start()
    try:
        assert a.exchange("cancel", b"local", timeout_ms=1000, cancel_event=cancel) == b"peer"
        worker.join(timeout=1.0)
        assert not worker.is_alive()
        assert peer_result == [b"local"]
    finally:
        a.close()
        b.close()


def test_in_process_exchange_cancelled_after_send_closes_shared_link():
    a, b = InProcessSerialLink.pair("blue", "blue")
    cancel = threading.Event()
    result: list[Exception] = []

    def blocked_exchange() -> None:
        try:
            a.exchange("cancel", b"local", timeout_ms=5000, cancel_event=cancel)
        except Exception as exc:  # noqa: BLE001 - assert cancellation type below
            result.append(exc)

    worker = threading.Thread(target=blocked_exchange, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 1.0
        while a._out["cancel"].qsize() < 1 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert a._out["cancel"].qsize() == 1
        cancel.set()
        worker.join(timeout=1.0)
        assert not worker.is_alive()
        assert result and isinstance(result[0], SerialLinkClosed)
        assert "cancelled" in str(result[0])
        assert not a.connected
        assert not b.connected
    finally:
        a.close()
        b.close()


def test_in_process_rejects_unsupported_rom_version():
    with pytest.raises(ValueError, match="unsupported ROM version"):
        InProcessSerialLink.pair("pokemon", "blue")


@pytest.mark.parametrize("kind", ["", "é"])
def test_in_process_rejects_non_wire_exchange_kind(kind: str):
    a, _b = InProcessSerialLink.pair("blue", "blue")
    with pytest.raises(ValueError, match="exchange kind"):
        a.exchange(kind, b"x", timeout_ms=50)


def test_exchange_rejects_non_positive_or_non_integer_deadline():
    a, _b = InProcessSerialLink.pair("blue", "blue")
    with pytest.raises(ValueError, match="timeout_ms"):
        a.exchange("x", b"x", timeout_ms=0)
    with pytest.raises(TypeError, match="timeout_ms"):
        a.exchange("x", b"x", timeout_ms=True)


# --- TcpSerialLink ---------------------------------------------------------


def test_tcp_listener_ready_event_and_bounded_accept():
    port = _free_port()
    ready = threading.Event()
    errors: list[BaseException] = []

    def listen_without_peer() -> None:
        try:
            TcpSerialLink.listen(
                port,
                "blue",
                accept_timeout_s=0.1,
                ready_event=ready,
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=listen_without_peer, daemon=True)
    thread.start()
    assert ready.wait(timeout=1.0)
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert errors and isinstance(errors[0], SerialLinkTimeout)


def test_tcp_listener_cancel_event_returns_promptly():
    port = _free_port()
    ready = threading.Event()
    cancel = threading.Event()
    errors: list[BaseException] = []

    def listen_until_cancelled() -> None:
        try:
            TcpSerialLink.listen(
                port,
                "blue",
                cancel_event=cancel,
                ready_event=ready,
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=listen_until_cancelled, daemon=True)
    thread.start()
    assert ready.wait(timeout=1.0)
    cancel.set()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert errors and isinstance(errors[0], SerialLinkClosed)


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
        got = client.exchange("exchange_bytes/wSerialPlayerDataBlock", b"\xaa" * 5, timeout_ms=2000)
        t.join(timeout=2.0)
        assert got == b"\x11" * 5
        assert results_server == [b"\xaa" * 5]
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


def test_tcp_exchange_timeout_is_terminal():
    server, client = _make_tcp_pair()
    try:
        started = time.monotonic()
        with pytest.raises(SerialLinkTimeout):
            client.exchange("stale", b"x", timeout_ms=75)
        assert time.monotonic() - started < 1.0
        assert not client.connected
        _wait_for_reader_stop(client)

        # EXCHANGE has no request identifier, so a timed-out channel cannot
        # safely accept a later response or be reused for the same kind.
        with pytest.raises(SerialLinkClosed):
            client.exchange("stale", b"y", timeout_ms=75)
    finally:
        server.close()
        client.close()


def test_tcp_exchange_cancelled_after_send_closes_channel():
    server, client = _make_tcp_pair()
    cancel = threading.Event()
    result: list[Exception] = []

    def blocked_exchange() -> None:
        try:
            client.exchange("cancel", b"local", timeout_ms=5000, cancel_event=cancel)
        except Exception as exc:  # noqa: BLE001 - assert cancellation type below
            result.append(exc)

    worker = threading.Thread(target=blocked_exchange, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 1.0
        while (
            server._inbound.get("cancel") is None or server._inbound["cancel"].qsize() < 1
        ) and time.monotonic() < deadline:
            time.sleep(0.005)
        assert server._inbound["cancel"].qsize() == 1
        cancel.set()
        worker.join(timeout=1.0)
        assert not worker.is_alive()
        assert result and isinstance(result[0], SerialLinkClosed)
        assert "cancelled" in str(result[0])

        deadline = time.monotonic() + 1.0
        while server.connected and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not server.connected
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


def test_tcp_peer_close_wakes_exchange_waiter_promptly():
    server, client = _make_tcp_pair()
    result: list[Exception] = []

    def blocked_exchange() -> None:
        try:
            client.exchange("blocked", b"x", timeout_ms=5000)
        except Exception as exc:  # noqa: BLE001
            result.append(exc)

    worker = threading.Thread(target=blocked_exchange, daemon=True)
    worker.start()
    time.sleep(0.05)
    server.close()
    worker.join(timeout=1.0)
    try:
        assert not worker.is_alive()
        assert result and isinstance(result[0], SerialLinkClosed)
    finally:
        client.close()


@pytest.mark.timing_sensitive
@pytest.mark.parametrize(
    "partial_frame",
    [
        pytest.param(b"\x00", id="partial-header"),
        pytest.param(struct.pack(">I", 5) + b"x", id="partial-body"),
    ],
)
def test_tcp_partial_frame_cannot_hold_exchange_past_deadline(partial_frame: bytes):
    """A stalled frame read must not outlive the public exchange deadline."""
    local, peer = socket.socketpair()
    link = TcpSerialLink(local, "blue")
    try:
        _send_wire_frame(
            peer,
            bytes([serial_link_module.OP_HELLO]) + serial_link_module._pack_lp_str("blue"),
        )
        assert link.peer_rom_version == "blue"

        # Leave the reader waiting for the rest of this frame. The exchange
        # deadline must still close the channel and return to its caller.
        _send_raw(peer, partial_frame)
        started = time.monotonic()
        with pytest.raises(SerialLinkTimeout):
            link.exchange("partial", b"x", timeout_ms=100)
        assert time.monotonic() - started < 1.0
        assert not link.connected
        _wait_for_reader_stop(link)
    finally:
        link.close()
        peer.close()


def test_tcp_clean_eof_is_not_reader_failure():
    server, client = _make_tcp_pair()
    try:
        assert server.peer_rom_version == "blue"
        client._sock.shutdown(socket.SHUT_WR)

        _wait_for_reader_stop(server)
        assert not server.connected
        assert server._reader_exc is None
        with pytest.raises(SerialLinkClosed):
            server.exchange("after-eof", b"x", timeout_ms=50)
    finally:
        server.close()
        client.close()


def test_tcp_bye_is_not_reader_failure():
    server, client = _make_tcp_pair()
    try:
        assert server.peer_rom_version == "blue"
        client.close()

        _wait_for_reader_stop(server)
        assert not server.connected
        assert server._reader_exc is None
        with pytest.raises(SerialLinkClosed):
            server.exchange("after-bye", b"x", timeout_ms=50)
    finally:
        server.close()
        client.close()


def test_tcp_inbound_exchange_queue_overflow_fails_closed():
    server, client = _make_tcp_pair()
    kind = "flood"
    body = (
        bytes([serial_link_module.OP_EXCHANGE])
        + serial_link_module._pack_lp_str(kind)
        + serial_link_module._pack_lp_bytes(b"x")
    )
    try:
        for _ in range(serial_link_module._MAX_INBOUND_FRAMES_PER_KIND):
            client._send_frame(body)

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            inbound = server._inbound.get(kind)
            if (
                inbound is not None
                and inbound.qsize() == serial_link_module._MAX_INBOUND_FRAMES_PER_KIND
            ):
                break
            time.sleep(0.005)
        inbound = server._inbound[kind]
        assert inbound.maxsize == serial_link_module._MAX_INBOUND_FRAMES_PER_KIND
        assert inbound.qsize() == serial_link_module._MAX_INBOUND_FRAMES_PER_KIND

        client._send_frame(body)
        deadline = time.monotonic() + 1.0
        while server._reader_exc is None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert isinstance(server._reader_exc, SerialLinkProtocolError)
        assert not server.connected
        assert server._inbound_frame_count == 0
        assert server._inbound_byte_count == 0
    finally:
        server.close()
        client.close()


def test_tcp_inbound_exchange_frame_budget_fails_closed_across_kinds():
    server, client = _make_tcp_pair()
    try:
        for index in range(serial_link_module._MAX_INBOUND_FRAMES + 1):
            kind = f"flood-{index}"
            body = (
                bytes([serial_link_module.OP_EXCHANGE])
                + serial_link_module._pack_lp_str(kind)
                + serial_link_module._pack_lp_bytes(b"x")
            )
            client._send_frame(body)

        deadline = time.monotonic() + 1.0
        while server._reader_exc is None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert isinstance(server._reader_exc, SerialLinkProtocolError)
        assert not server.connected
        assert len(server._inbound) <= serial_link_module._MAX_INBOUND_KINDS
        assert server._inbound_frame_count == 0
    finally:
        server.close()
        client.close()


def test_tcp_terminal_error_publishes_after_inbound_budget_cleanup():
    """Reader failure publication cannot expose stale inbound counters."""
    server, client = _make_tcp_pair()
    closer = threading.Thread(
        target=server._mark_closed,
        args=(SerialLinkProtocolError("forced terminal cleanup"),),
        daemon=True,
    )

    def body_for(index: int) -> memoryview:
        return memoryview(
            bytes([serial_link_module.OP_EXCHANGE])
            + serial_link_module._pack_lp_str(f"forced-{index}")
            + serial_link_module._pack_lp_bytes(b"x")
        )
    try:
        assert server.peer_rom_version == "blue"
        for index in range(serial_link_module._MAX_INBOUND_FRAMES):
            server._dispatch(body_for(index))
        assert server._inbound_frame_count == serial_link_module._MAX_INBOUND_FRAMES

        # Hold the queue lock after the terminal state is announced. Before
        # the fix this made _reader_exc visible while the frame/byte budgets
        # still contained all queued frames.
        assert server._inbound_lock.acquire(timeout=1.0)
        closer.start()
        assert server._closed_event.wait(timeout=1.0)
        assert server._reader_exc is None
        assert server._inbound_frame_count == serial_link_module._MAX_INBOUND_FRAMES
        server._inbound_lock.release()

        closer.join(timeout=1.0)
        assert not closer.is_alive()
        assert isinstance(server._reader_exc, SerialLinkProtocolError)
        assert server._inbound_frame_count == 0
        assert server._inbound_byte_count == 0
    finally:
        if server._inbound_lock.locked():
            server._inbound_lock.release()
        closer.join(timeout=1.0)
        server.close()
        client.close()


def test_tcp_close_sends_bye_before_shutdown():
    local, peer = socket.socketpair()
    link = TcpSerialLink(local, "blue")
    try:
        header = peer.recv(4)
        size = struct.unpack(">I", header)[0]
        assert peer.recv(size) == bytes([1, 4]) + b"blue"
        link.close()
        assert not link.connected
        assert not link._reader.is_alive()
        header = peer.recv(4)
        size = struct.unpack(">I", header)[0]
        assert peer.recv(size) == bytes([0xFE])
        link.close()
        assert not link.connected
    finally:
        link.close()
        peer.close()


def test_cancelled_tcp_connect_returns_promptly():
    cancel = threading.Event()
    cancel.set()
    started = time.monotonic()
    with pytest.raises(SerialLinkClosed, match="cancelled"):
        TcpSerialLink.connect("127.0.0.1", 1, "blue", timeout_s=30.0, cancel_event=cancel)
    assert time.monotonic() - started < 1.0


def test_tcp_serial_link_rejects_non_loopback_hosts():
    with pytest.raises(ValueError, match="localhost-only"):
        TcpSerialLink.connect("192.0.2.1", 1, "blue")
    with pytest.raises(ValueError, match="localhost-only"):
        TcpSerialLink.listen(_free_port(), "blue", host="0.0.0.0")


def test_tcp_serial_link_rejects_unbounded_accept():
    with pytest.raises(ValueError, match="finite and positive"):
        TcpSerialLink.listen(_free_port(), "blue", accept_timeout_s=None)


def test_tcp_truncated_frame_is_protocol_error_and_closes():
    server, client = _make_tcp_pair()
    try:
        # The header promises five body bytes, but only one arrives before
        # the peer half-closes its write side.
        _send_raw(client._sock, struct.pack(">I", 5) + b"x")
        client._sock.shutdown(socket.SHUT_WR)

        deadline = time.monotonic() + 1.0
        while server._reader_exc is None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert isinstance(server._reader_exc, SerialLinkProtocolError)
        assert not server.connected
    finally:
        server.close()
        client.close()


@pytest.mark.parametrize(
    ("body", "message"),
    [
        pytest.param(b"\x7f", "unknown opcode", id="unknown-opcode"),
        pytest.param(
            bytes([serial_link_module.OP_HELLO])
            + serial_link_module._pack_lp_str("blue")
            + b"\x00",
            "HELLO has trailing bytes",
            id="hello-trailing-bytes",
        ),
        pytest.param(
            bytes([serial_link_module.OP_EXCHANGE])
            + serial_link_module._pack_lp_str("kind")
            + b"\x00",
            "length-prefixed bytes",
            id="exchange-missing-length",
        ),
        pytest.param(
            bytes([serial_link_module.OP_HELLO, 1, 0xFF]),
            "not UTF-8",
            id="hello-invalid-utf8",
        ),
    ],
)
def test_tcp_rejects_malformed_frames(body: bytes, message: str):
    server, client = _make_tcp_pair()
    try:
        assert server.peer_rom_version == "blue"
        _send_wire_frame(client._sock, body)

        _wait_for_reader_stop(server)
        assert isinstance(server._reader_exc, SerialLinkProtocolError)
        assert message in str(server._reader_exc)
        assert not server.connected
    finally:
        server.close()
        client.close()


def test_tcp_rejects_oversized_frame_before_body():
    server, client = _make_tcp_pair()
    try:
        assert server.peer_rom_version == "blue"
        # Do not send the body: the bounded header must be rejected before the
        # reader waits for or allocates the advertised payload.
        _send_raw(
            client._sock,
            struct.pack(">I", serial_link_module._MAX_FRAME_SIZE + 1),
        )

        _wait_for_reader_stop(server)
        assert isinstance(server._reader_exc, SerialLinkProtocolError)
        assert "frame too large" in str(server._reader_exc)
        assert not server.connected
    finally:
        server.close()
        client.close()


def test_tcp_rejects_duplicate_hello():
    server, client = _make_tcp_pair()
    try:
        assert server.peer_rom_version == "blue"
        _send_wire_frame(
            client._sock,
            bytes([serial_link_module.OP_HELLO]) + serial_link_module._pack_lp_str("blue"),
        )

        _wait_for_reader_stop(server)
        assert isinstance(server._reader_exc, SerialLinkProtocolError)
        assert "duplicate HELLO" in str(server._reader_exc)
        assert not server.connected
    finally:
        server.close()
        client.close()


def test_tcp_rejects_malformed_frame():
    """Hand-craft a bogus frame and confirm the reader thread raises."""
    server, client = _make_tcp_pair()
    try:
        # Write directly to the socket to bypass _send_frame framing.
        # Zero-length frame is explicitly rejected.
        _send_raw(client._sock, b"\x00\x00\x00\x00")
        # Peer's reader thread should flag a protocol error and close.
        for _ in range(50):
            time.sleep(0.01)
            if server._reader_exc is not None:
                break
        assert isinstance(server._reader_exc, SerialLinkProtocolError)
    finally:
        server.close()
        client.close()


def test_tcp_rejects_exchange_before_peer_hello():
    local, peer = socket.socketpair()
    link = TcpSerialLink(local, "blue")
    body = (
        bytes([serial_link_module.OP_EXCHANGE])
        + serial_link_module._pack_lp_str("x")
        + serial_link_module._pack_lp_bytes(b"x")
    )
    try:
        _send_raw(peer, struct.pack(">I", len(body)) + body)
        deadline = time.monotonic() + 1.0
        while link._reader_exc is None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert isinstance(link._reader_exc, SerialLinkProtocolError)
        assert "HELLO" in str(link._reader_exc)
        assert not link.connected
    finally:
        link.close()
        peer.close()


def test_tcp_rejects_non_finite_connect_deadline():
    with pytest.raises(ValueError, match="finite and positive"):
        TcpSerialLink.connect("127.0.0.1", 1, "blue", timeout_s=math.inf)


def test_tcp_close_is_bounded_when_peer_stops_reading(monkeypatch):
    local, peer = socket.socketpair()
    local.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    link = TcpSerialLink(local, "blue")
    try:
        # Fill the outbound kernel buffer without involving the link writer;
        # close() must not block forever trying to send BYE afterwards.
        for _ in range(1024):
            try:
                link._sock.send(b"x" * 65536)
            except BlockingIOError:
                break
        else:
            pytest.fail("test socket never filled")

        monkeypatch.setattr(serial_link_module, "_WRITE_TIMEOUT_S", 0.05)
        started = time.monotonic()
        link.close()
        assert time.monotonic() - started < 1.0
        assert not link.connected
        assert not link._reader.is_alive()
    finally:
        link.close()
        peer.close()


def test_tcp_close_race_with_writer_finishes(monkeypatch):
    local, peer = socket.socketpair()
    local.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    link = TcpSerialLink(local, "blue")
    writer_done = threading.Event()

    def blocked_writer() -> None:
        try:
            link._send_frame(b"x" * serial_link_module._MAX_FRAME_SIZE)
        except (SerialLinkClosed, SerialLinkTimeout):
            pass
        finally:
            writer_done.set()

    try:
        monkeypatch.setattr(serial_link_module, "_WRITE_TIMEOUT_S", 0.05)
        writer = threading.Thread(target=blocked_writer, daemon=True)
        writer.start()
        time.sleep(0.01)
        started = time.monotonic()
        link.close()
        assert time.monotonic() - started < 1.0
        assert writer_done.wait(timeout=1.0)
        assert not writer.is_alive()
        assert not link._reader.is_alive()
    finally:
        link.close()
        peer.close()
