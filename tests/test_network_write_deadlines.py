"""Real socket and lock backpressure must fit one edge wall deadline."""

import select
import socket
import threading
import time

import pytest

from pokered_harness.link import network_backend as module
from pokered_harness.link.network_backend import NetworkBackend, NetworkBackendError

pytestmark = pytest.mark.timing_sensitive


def _call_edge(backend):
    done = threading.Event()
    errors = []

    def run():
        try:
            backend.on_edge(1, 1)
        except NetworkBackendError as exc:
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
    deadline = time.monotonic() + 3
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


@pytest.mark.parametrize("contention", ["write", "edge", "tcp"])
def test_edge_deadline_covers_all_admission_and_socket_backpressure(monkeypatch, contention):
    monkeypatch.setattr(module, "_EDGE_CALL_TIMEOUT_SECONDS", 0.12)
    if contention == "tcp":
        sender, peer = _backpressured_tcp()
        backend = NetworkBackend(sender)
    else:
        backend, other = NetworkBackend.pair()
        peer = other._sock
    lock = None
    if contention in {"write", "edge"}:
        lock = backend._write_lock if contention == "write" else backend._edge_call_lock
        lock.acquire()
    started = time.monotonic()
    done, errors, thread = _call_edge(backend)
    try:
        returned_in_budget = done.wait(0.45)
        elapsed = time.monotonic() - started
        closed_before_cleanup = not backend.connected
    finally:
        backend.stop()
        if lock is not None:
            lock.release()
        thread.join(1)
        peer.close()
    assert not thread.is_alive()
    assert returned_in_budget, f"edge exceeded deadline under {contention} contention"
    assert elapsed < 0.45
    assert errors
    assert closed_before_cleanup


@pytest.mark.parametrize("lock_name", ["_edge_call_lock", "_write_lock"])
def test_edge_admission_and_response_share_one_budget(monkeypatch, lock_name):
    monkeypatch.setattr(module, "_EDGE_CALL_TIMEOUT_SECONDS", 0.3)
    backend, peer = NetworkBackend.pair()
    lock = getattr(backend, lock_name)
    lock.acquire()
    released = False
    started = time.monotonic()
    done, errors, thread = _call_edge(backend)
    try:
        time.sleep(0.2)
        lock.release()
        released = True
        assert done.wait(0.22), "response wait restarted the admission deadline"
        assert time.monotonic() - started < 0.43
        assert errors and not backend.connected
    finally:
        backend.stop()
        if not released:
            lock.release()
        peer.stop()
        thread.join(1)
        assert not thread.is_alive()


@pytest.mark.parametrize("operation", ["hello", "sync", "exchange", "response"])
def test_every_control_write_is_bounded_under_tcp_backpressure(monkeypatch, operation):
    monkeypatch.setattr(module, "_EDGE_CALL_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(module, "_REARM_WAIT_SECONDS", 0)
    sender, peer = _backpressured_tcp()
    backend = None
    started = time.monotonic()
    try:
        if operation == "hello":
            with pytest.raises(NetworkBackendError, match="deadline"):
                NetworkBackend(sender, local_rom_version="red")
        else:
            backend = NetworkBackend(sender)
            if operation == "response":
                backend._handle_edge_req(1)
                assert not backend.connected
            else:
                with pytest.raises(NetworkBackendError, match="deadline"):
                    if operation == "sync":
                        backend.announce_sync(3)
                    else:
                        backend.exchange_block(2, b"hello", timeout=2)
        assert time.monotonic() - started < 0.45
        assert sender.fileno() == -1
    finally:
        if backend is not None:
            backend.stop()
        sender.close()
        peer.close()


class SocketProxy:
    def __init__(self, sock):
        self.sock = sock

    def __getattr__(self, name):
        return getattr(self.sock, name)


def test_partial_interrupted_writes_preserve_complete_frame_order(monkeypatch):
    backend, peer = NetworkBackend.pair()
    real_select = module.select.select
    waits = []

    def interrupted_select(*args):
        waits.append(1)
        if len(waits) == 1:
            raise InterruptedError()
        return real_select(*args)

    class PartialSocket(SocketProxy):
        calls = 0

        def send(self, data):
            self.calls += 1
            if self.calls == 1:
                raise InterruptedError()
            if self.calls == 2:
                raise BlockingIOError()
            time.sleep(0.0001)
            return self.sock.send(data[:1])

    monkeypatch.setattr(module.select, "select", interrupted_select)
    backend._sock = PartialSocket(backend._sock)
    frames = (b"\x30\x07\x00\x64" + b"a" * 100, b"\x30\x08\x00\x64" + b"b" * 100)
    errors = []
    received = []
    receive_done = threading.Event()

    def send(frame):
        try:
            backend._send_frame(frame)
        except Exception as exc:  # noqa: BLE001 - preserve worker failures for main-thread assertion
            errors.append(exc)

    def receive():
        try:
            received.append(peer._recv_exactly(sum(map(len, frames))))
        except Exception as exc:  # noqa: BLE001 - preserve worker failures for main-thread assertion
            errors.append(exc)
        finally:
            receive_done.set()

    threads = [threading.Thread(target=send, args=(frame,), daemon=True) for frame in frames]
    threads.append(threading.Thread(target=receive, daemon=True))
    try:
        for thread in threads:
            thread.start()
        assert receive_done.wait(2), "frame receiver did not finish within its test bound"
        for thread in threads:
            thread.join(1)
            assert not thread.is_alive()
        assert not errors, errors
        assert received[0] in (frames[0] + frames[1], frames[1] + frames[0])
        assert len(waits) >= 2
    finally:
        backend.stop()
        peer.stop()
        for thread in threads:
            thread.join(1)
            assert not thread.is_alive()


def test_zero_send_closes_transport_without_retry():
    backend, peer = NetworkBackend.pair()

    class ZeroSocket(SocketProxy):
        calls = 0

        def send(self, data):
            self.calls += 1
            return 0

    proxy = ZeroSocket(backend._sock)
    backend._sock = proxy
    try:
        with pytest.raises(NetworkBackendError, match="closed socket during send"):
            backend._send_frame(b"\x01\x01")
        assert proxy.calls == 1
        assert not backend.connected
    finally:
        backend.stop()
        peer.stop()


def test_nonblocking_reader_preserves_fragmented_frames_and_eof():
    backend, peer = NetworkBackend.pair()

    class InterruptedReceive(SocketProxy):
        calls = 0

        def recv(self, count):
            self.calls += 1
            if self.calls == 1:
                raise InterruptedError()
            return self.sock.recv(min(count, 1))

    backend._sock = InterruptedReceive(backend._sock)
    try:
        peer._send_frame(b"\x01\x01")
        assert backend._recv_exactly(2) == b"\x01\x01"
        peer._send_frame(b"\x01")
        peer.stop()
        with pytest.raises(NetworkBackendError, match="mid-frame"):
            backend._recv_exactly(2)
    finally:
        backend.stop()
        peer.stop()


@pytest.mark.parametrize("contention", ["write", "tcp"])
def test_stop_cancels_admission_and_backpressured_send(monkeypatch, contention):
    monkeypatch.setattr(module, "_EDGE_CALL_TIMEOUT_SECONDS", 5)
    if contention == "tcp":
        sender, peer = _backpressured_tcp()
        backend = NetworkBackend(sender)
    else:
        backend, other = NetworkBackend.pair()
        peer = other._sock
        backend._write_lock.acquire()
    done, errors, thread = _call_edge(backend)
    try:
        time.sleep(0.05)
        started = time.monotonic()
        backend.stop()
        assert done.wait(0.4)
        assert time.monotonic() - started < 0.4
        assert errors
    finally:
        if contention == "write":
            backend._write_lock.release()
        backend.stop()
        peer.close()
        thread.join(1)
        assert not thread.is_alive()
