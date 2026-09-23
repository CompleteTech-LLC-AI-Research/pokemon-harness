"""Raw-peer replay, partial-frame deadlines and poll interoperability (#151).

Split from ``tests/test_timed_wire.py`` for #151 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from tests._timed_wire_support import (
    BOUND,
    EPOCH,
    FragmentSocket,
    Job,
    ReturnGateSocket,
    channels,
    deadline,
    exchange,
    sockets,
    wait_state,
    wire,
)


@pytest.mark.parametrize(
    "message,sequence",
    [
        (wire.Progress(0), 1),
        (wire.Progress(0), 3),
        (wire.EdgeRequest(2, 0, 0, 0), 2),
        (wire.EdgeResponse(1, 0, 0), 2),
    ],
)
def test_raw_peer_replay_gaps_and_uncorrelated_response(kind, message, sequence):
    with sockets(kind) as (a, b):
        channel = wire.TimedWireChannel(a, epoch=EPOCH)
        try:
            b.sendall(wire.encode_frame(wire.Frame(EPOCH, 1, wire.Hello())))
            channel.handshake(deadline=deadline())
            b.sendall(wire.encode_frame(wire.Frame(EPOCH, sequence, message)))
            with pytest.raises(wire.ProtocolError):
                channel.receive(deadline=deadline())
            assert channel.closed
        finally:
            channel.close()


def test_close_wakes_partial_frame_reader(kind):
    with sockets(kind) as (a, b):
        observed = FragmentSocket(a)
        channel = wire.TimedWireChannel(observed, epoch=EPOCH)
        try:
            b.sendall(b"P")
            assert observed.read_entered.wait(BOUND)
            channel.close()
            assert not channel._reader.is_alive()
            with pytest.raises(wire.ChannelClosed):
                channel.receive(deadline=deadline())
        finally:
            channel.close()


def test_partial_frame_has_bounded_deadline(kind, monkeypatch):
    monkeypatch.setattr(wire, "PARTIAL_READ_TIMEOUT", 0.1)
    with sockets(kind) as (a, b):
        channel = wire.TimedWireChannel(a, epoch=EPOCH)
        try:
            b.sendall(b"P")
            with pytest.raises(wire.DeadlineExceeded):
                channel.handshake(deadline=deadline())
            assert channel.closed
        finally:
            channel.close()


def test_peer_application_waits_for_local_hello_send_publication(kind):
    with sockets(kind) as (a, b):
        gate = ReturnGateSocket(a)
        gate.armed = True
        left = wire.TimedWireChannel(gate, epoch=EPOCH)
        right = wire.TimedWireChannel(b, epoch=EPOCH)
        try:
            shared_deadline = deadline()
            hello = Job(lambda: left.handshake(deadline=shared_deadline))
            assert gate.delivered.wait(BOUND)
            right.handshake(deadline=shared_deadline)
            right.send(wire.Progress(0), deadline=shared_deadline)
            # Observe the reader at its publication wait, not merely a sent byte.
            entered = threading.Event()
            original_wait = left._condition.wait

            def observed_wait(timeout=None):
                entered.set()
                return original_wait(timeout)

            with left._condition:
                left._condition.wait = observed_wait
                left._condition.notify_all()
            assert entered.wait(BOUND)
            assert not left.closed
            assert not left._hello_sent
            gate.release.set()
            hello.result()
            assert left.receive(deadline=shared_deadline) == wire.Progress(0)
        finally:
            gate.release.set()
            left.close()
            right.close()


def test_one_absolute_deadline_covers_admission_and_partial_write(kind, monkeypatch):
    with channels(kind) as (left, _):
        clock = [100.0]
        monkeypatch.setattr(wire, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        original_lock = left._send_lock
        original_socket = left._sock
        sent = []

        class AdmissionLock:
            def acquire(self, timeout):
                acquired = original_lock.acquire(timeout=timeout)
                if acquired:
                    clock[0] += 0.6
                return acquired

            def release(self):
                original_lock.release()

        class PartialWriter:
            def __getattr__(self, name):
                return getattr(original_socket, name)

            def send(self, data):
                count = original_socket.send(data[:1])
                sent.append(count)
                clock[0] += 0.6
                return count

        left._send_lock = AdmissionLock()
        left._sock = PartialWriter()
        with pytest.raises(wire.DeadlineExceeded):
            left.send(wire.Progress(0), deadline=101.0)
        assert sent == [1], "deadline was reset after admission or partial I/O"
        assert left.closed
        assert isinstance(left.error, wire.DeadlineExceeded)


def test_completeness_requires_exact_nonempty_prefix(kind):
    with channels(kind) as (left, right):
        exchange(left, right, wire.EdgeRequest(1, 10, 10, 0))
        for last_edge_id in (0, 2):
            with pytest.raises(wire.ProtocolError):
                left.send(wire.EmissionComplete(10, last_edge_id), deadline=deadline())
        exchange(left, right, wire.EmissionComplete(10, 1))
        exchange(left, right, wire.EmissionComplete(10, 1))


def test_malformed_body_from_peer_fails_closed(kind):
    with sockets(kind) as (a, b):
        channel = wire.TimedWireChannel(a, epoch=EPOCH)
        try:
            b.sendall(wire.encode_frame(wire.Frame(EPOCH, 1, wire.Hello())))
            channel.handshake(deadline=deadline())
            data = wire.encode_frame(wire.Frame(EPOCH, 2, wire.EdgeRequest(1, 0, 0, 1)))
            b.sendall(data[:-1] + b"\x02")
            with pytest.raises(wire.ProtocolError):
                channel.receive(deadline=deadline())
            assert channel.closed
        finally:
            channel.close()


def test_poll_empty_fifo_and_receive_interoperate(kind):
    with channels(kind) as (left, right):
        assert Job(right.poll).result() is None
        left.send(wire.Progress(0), deadline=deadline())
        left.send(wire.Progress(1), deadline=deadline())
        wait_state(right, lambda: len(right._queue) == 2)
        assert right.poll() == wire.Progress(0)
        assert right.receive(deadline=deadline()) == wire.Progress(1)
        assert Job(right.poll).result() is None


def test_poll_preserves_terminal_error_and_does_not_expose_queued_messages(kind):
    with channels(kind) as (left, right):
        left.send(wire.Progress(0), deadline=deadline())
        wait_state(right, lambda: len(right._queue) == 1)
        right.close()
        first_error = right.error
        for _ in range(2):
            with pytest.raises(wire.ChannelClosed) as caught:
                right.poll()
            assert caught.value is first_error
