"""Bounded admission, backpressure and capacity-release contracts (#151).

Split from ``tests/test_timed_wire.py`` for #151 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module.
"""

from __future__ import annotations

import threading
import time

import pytest

from tests._timed_wire_support import (
    BOUND,
    EPOCH,
    Job,
    ReturnGateSocket,
    channels,
    deadline,
    exchange,
    gated_channels,
    sockets,
    wait_state,
    wire,
)


def test_fast_response_before_writer_return_and_before_request_consumption(kind):
    with gated_channels(kind) as (left, right, gate):
        request = wire.EdgeRequest(1, 100, 100, 1)
        sender = Job(lambda: left.send(request, deadline=deadline()))
        assert gate.delivered.wait(BOUND)
        wait_state(right, lambda: 1 in right._incoming.pending)
        # The handler has not consumed the request, yet correlation is ready.
        right.send(wire.EdgeResponse(1, 0, 0), deadline=deadline())
        assert left.receive(deadline=deadline()) == wire.EdgeResponse(1, 0, 0)
        assert sender.thread.is_alive()
        assert right.receive(deadline=deadline()) == request
        gate.release.set()
        assert sender.result() is None
        assert not left.closed and not right.closed


@pytest.mark.parametrize("action", ["cancel", "expire", "close"])
def test_waiting_writer_admission_is_bounded_and_does_not_skip_sequence(kind, action):
    with gated_channels(kind) as (left, right, gate):
        first = Job(lambda: left.send(wire.Progress(0), deadline=deadline()))
        assert gate.delivered.wait(BOUND)
        assert right.receive(deadline=deadline()) == wire.Progress(0)
        cancellation = threading.Event()
        started = threading.Event()

        def waiting():
            started.set()
            return left.send(
                wire.Progress(1), deadline=time.monotonic() + 0.15, cancel_event=cancellation
            )

        second = Job(waiting)
        assert started.wait(BOUND)
        if action == "cancel":
            cancellation.set()
            expected = wire.Cancelled
        elif action == "expire":
            expected = wire.DeadlineExceeded
        else:
            left.close()
            expected = wire.ChannelClosed
        with pytest.raises(expected):
            second.result()
        gate.release.set()
        if action == "close":
            with pytest.raises(wire.ChannelClosed):
                first.result()
        else:
            assert first.result() is None
            before = left._outgoing.sequence
            after = exchange(left, right, wire.Progress(1))
            assert after == before + 1


def test_bounded_outstanding_requests_release_capacity_on_response(kind):
    with channels(kind, inbound_capacity=1) as (left, right):
        exchange(left, right, wire.EdgeRequest(1, 0, 0, 0))
        with pytest.raises(wire.ProtocolError):
            left.send(wire.EdgeRequest(2, 1, 1, 0), deadline=deadline())
        exchange(right, left, wire.EdgeResponse(1, 0, 1))
        exchange(left, right, wire.EdgeRequest(2, 1, 1, 0))


@pytest.mark.parametrize("order", ["sender_returns_first", "peer_closes_first"])
def test_bounded_incoming_queue_fails_closed(kind, order):
    class OverflowGateSocket(ReturnGateSocket):
        def send(self, data, *args):
            count = super().send(data, *args)
            # Releasing the gate must lead to the final open check, not a
            # second send iteration after a partial write.
            assert count == len(data)
            return count

        def shutdown(self, how):
            # EOF must not release the writer before the test observes it.
            return self.sock.shutdown(how)

    with sockets(kind) as (a, b):
        gate = OverflowGateSocket(a)
        left = wire.TimedWireChannel(gate, epoch=EPOCH, inbound_capacity=1)
        right = wire.TimedWireChannel(b, epoch=EPOCH, inbound_capacity=1)
        sender = None
        try:
            hello = Job(lambda: left.handshake(deadline=deadline()))
            right.handshake(deadline=deadline())
            hello.result()
            left.send(wire.Progress(0), deadline=deadline())
            wait_state(right, lambda: len(right._queue) == 1)
            with right._condition:
                assert right._capacity == len(right._queue) == 1
                assert right._queue[0].message == wire.Progress(0)
                assert right._incoming.sequence == 2
                assert right._incoming.settled == 0
                assert not right.closed
                if order == "sender_returns_first":
                    # The reader cannot process overflow until send returns.
                    assert left.send(wire.Progress(1), deadline=deadline()) is None
                    assert not right.closed
                    assert len(right._queue) == 1
            if order == "peer_closes_first":
                gate.armed = True
                sender = Job(lambda: left.send(wire.Progress(1), deadline=deadline()))
                assert gate.delivered.wait(BOUND)
                wait_state(right, lambda: right.closed)
                wait_state(left, lambda: left.closed)
                assert type(left.error) is wire.ChannelClosed
                assert str(left.error) == "peer closed connection"
                assert sender.thread.is_alive()
                assert not gate.release.is_set()
                gate.release.set()
                with pytest.raises(wire.ChannelClosed, match="^peer closed connection$") as caught:
                    sender.result()
                assert caught.value is left.error
            wait_state(right, lambda: right.closed)
            error = right.error
            assert type(error) is wire.ProtocolError
            assert str(error) == "inbound queue capacity exceeded"
            with right._condition:
                assert right._capacity == 1
                assert not right._queue
                assert right._incoming.sequence == 2
                assert right._incoming.settled == 0
            with pytest.raises(
                wire.ProtocolError, match="^inbound queue capacity exceeded$"
            ) as caught:
                right.receive(deadline=deadline())
            assert caught.value is error
            assert right.error is error
        finally:
            gate.release.set()
            left.close()
            right.close()
            if sender is not None:
                sender.thread.join(BOUND + 1)
                assert not sender.thread.is_alive(), "wire worker exceeded outer guard"
