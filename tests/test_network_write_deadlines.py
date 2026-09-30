"""Deadline and partial-write coverage for :class:`NetworkBackend`."""

from __future__ import annotations

import select
import socket
import threading
import time

import pytest

from pokered_harness.link import network_backend as module
from pokered_harness.link.network_backend import NetworkBackend, NetworkBackendError

pytestmark = pytest.mark.timing_sensitive


def _call_edge(backend: NetworkBackend):
    done = threading.Event()
    errors: list[BaseException] = []

    def run() -> None:
        try:
            backend.on_edge(1, 1)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            errors.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return done, errors, thread


def _backpressured_tcp():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    sender = socket.create_connection(listener.getsockname())
    peer, _ = listener.accept()
    listener.close()
    sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    peer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sender.setblocking(False)
    deadline = time.monotonic() + 3.0
    try:
        while time.monotonic() < deadline:
            try:
                sender.send(b"x" * 65536)
            except BlockingIOError:
                if not select.select([], [sender], [], 0.1)[1]:
                    sender.setblocking(True)
                    return sender, peer
        raise AssertionError("TCP test failed to establish backpressure")
    except BaseException:
        sender.close()
        peer.close()
        raise


@pytest.mark.parametrize("contention", ["write", "edge"])
def test_edge_deadline_covers_admission_and_write_lock(monkeypatch, contention):
    monkeypatch.setattr(module, "_EDGE_RESPONSE_TIMEOUT_SECONDS", 0.12)
    backend, peer = NetworkBackend.pair()
    lock = backend._write_lock if contention == "write" else backend._edge_call_lock
    lock.acquire()
    started = time.monotonic()
    done, errors, thread = _call_edge(backend)
    try:
        assert done.wait(0.45)
        assert time.monotonic() - started < 0.45
        assert errors
        if contention == "write":
            assert not backend.connected
    finally:
        lock.release()
        backend.stop(timeout_s=1.0)
        peer.stop(timeout_s=1.0)
        thread.join(timeout=1.0)
        assert not thread.is_alive()


def test_edge_admission_and_response_share_one_total_budget(monkeypatch):
    monkeypatch.setattr(module, "_EDGE_RESPONSE_TIMEOUT_SECONDS", 0.30)
    backend, peer = NetworkBackend.pair()
    backend._edge_call_lock.acquire()
    started = time.monotonic()
    done, errors, thread = _call_edge(backend)
    try:
        time.sleep(0.20)
        backend._edge_call_lock.release()
        assert done.wait(0.22)
        assert time.monotonic() - started < 0.43
        assert errors and not backend.connected
    finally:
        if backend._edge_call_lock.locked():
            backend._edge_call_lock.release()
        backend.stop(timeout_s=1.0)
        peer.stop(timeout_s=1.0)
        thread.join(timeout=1.0)


@pytest.mark.parametrize("operation", ["hello", "sync", "exchange"])
def test_control_writes_fail_closed_under_tcp_backpressure(monkeypatch, operation):
    monkeypatch.setattr(module, "_DEFAULT_SEND_TIMEOUT_SECONDS", 0.10)
    sender, peer_socket = _backpressured_tcp()
    backend: NetworkBackend | None = None
    started = time.monotonic()
    try:
        if operation == "hello":
            with pytest.raises(NetworkBackendError, match="timed out|deadline|closed"):
                NetworkBackend(sender, local_rom_version="red")
        else:
            backend = NetworkBackend(sender)
            with pytest.raises(NetworkBackendError, match="timed out|deadline|closed"):
                if operation == "sync":
                    backend.announce_sync(3)
                else:
                    backend.exchange_block(2, b"hello", timeout=0.2)
        assert time.monotonic() - started < 0.6
        assert sender.fileno() == -1
    finally:
        if backend is not None:
            backend.stop(timeout_s=1.0)
        sender.close()
        peer_socket.close()


class _SocketProxy:
    def __init__(self, sock):
        self.sock = sock

    def __getattr__(self, name):
        return getattr(self.sock, name)


def test_partial_interrupted_writes_preserve_complete_frame_order(monkeypatch):
    backend, peer = NetworkBackend.pair()

    class PartialSocket(_SocketProxy):
        calls = 0

        def send(self, data):
            self.calls += 1
            if self.calls == 1:
                raise InterruptedError()
            if self.calls == 2:
                raise BlockingIOError()
            return self.sock.send(data[:1])

    backend._sock = PartialSocket(backend._sock)
    frames = (b"a" * 104, b"b" * 104)
    errors: list[BaseException] = []
    received: list[bytes] = []
    receive_done = threading.Event()

    def send(frame: bytes) -> None:
        try:
            with backend._write_guard(timeout=1.0, operation="TEST") as deadline:
                backend._send_frame(
                    frame,
                    timeout=max(0.0, deadline - time.monotonic()),
                    operation="TEST",
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def receive() -> None:
        try:
            received.append(peer._recv_exactly(sum(map(len, frames))))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            receive_done.set()

    threads = [threading.Thread(target=send, args=(frame,), daemon=True) for frame in frames]
    threads.append(threading.Thread(target=receive, daemon=True))
    try:
        for thread in threads:
            thread.start()
        assert receive_done.wait(2.0)
        for thread in threads:
            thread.join(timeout=1.0)
            assert not thread.is_alive()
        assert errors == []
        assert received[0] in (frames[0] + frames[1], frames[1] + frames[0])
    finally:
        backend.stop(timeout_s=1.0)
        peer.stop(timeout_s=1.0)


def test_zero_send_closes_public_transport_without_retry():
    backend, peer = NetworkBackend.pair()

    class ZeroSocket(_SocketProxy):
        calls = 0

        def send(self, data):
            self.calls += 1
            return 0

    proxy = ZeroSocket(backend._sock)
    backend._sock = proxy
    try:
        with pytest.raises(NetworkBackendError, match="closed while sending"):
            backend.announce_sync(1)
        assert proxy.calls == 1
        assert backend.connected is False
    finally:
        backend.stop(timeout_s=1.0)
        peer.stop(timeout_s=1.0)


def test_nonblocking_reader_preserves_fragmented_frames_and_eof():
    backend, peer = NetworkBackend.pair()

    class InterruptedReceive(_SocketProxy):
        calls = 0

        def recv(self, count):
            self.calls += 1
            if self.calls == 1:
                raise InterruptedError()
            return self.sock.recv(min(count, 1))

    backend._sock = InterruptedReceive(backend._sock)
    try:
        peer._send_frame(b"\x01\x01", timeout=1.0, operation="TEST")
        assert backend._recv_exactly(2) == b"\x01\x01"
        peer._send_frame(b"\x01", timeout=1.0, operation="TEST")
        peer.stop(timeout_s=1.0)
        with pytest.raises(NetworkBackendError, match="mid-frame"):
            backend._recv_exactly(2)
    finally:
        backend.stop(timeout_s=1.0)
        peer.stop(timeout_s=1.0)


def test_stop_cancels_waiting_edge_without_using_full_edge_timeout(monkeypatch):
    monkeypatch.setattr(module, "_EDGE_RESPONSE_TIMEOUT_SECONDS", 5.0)
    backend, peer = NetworkBackend.pair()
    backend._write_lock.acquire()
    done, errors, thread = _call_edge(backend)
    try:
        time.sleep(0.05)
        started = time.monotonic()
        assert backend.stop(timeout_s=0.3) is True
        assert done.wait(0.4)
        assert time.monotonic() - started < 0.4
        assert errors
    finally:
        backend._write_lock.release()
        backend.stop(timeout_s=1.0)
        peer.stop(timeout_s=1.0)
        thread.join(timeout=1.0)
        assert not thread.is_alive()
