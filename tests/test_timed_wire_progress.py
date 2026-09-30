"""Edge progress, watermark, correlation and frame-fragmentation contracts (#151).

Split from ``tests/test_timed_wire.py`` for #151 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from tests._timed_wire_support import (
    BOUND,
    EPOCH,
    HEADER,
    FragmentSocket,
    Job,
    channels,
    deadline,
    exchange,
    sockets,
    wire,
)


def test_initial_zero_edge_then_equal_completeness_and_independent_progress(kind):
    with channels(kind) as (left, right):
        exchange(left, right, wire.EdgeRequest(1, 0, 0, 1))
        exchange(left, right, wire.EmissionComplete(0, 1))
        exchange(left, right, wire.EmissionComplete(0, 1))
        exchange(left, right, wire.Progress(0))
        exchange(left, right, wire.Progress(100))
        exchange(left, right, wire.EmissionComplete(1, 1))
        exchange(right, left, wire.EdgeResponse(1, 0, 0))


@pytest.mark.parametrize(
    "bad",
    [
        wire.EdgeRequest(1, 0, 0, 1),
        wire.EmissionComplete(0, 1),
    ],
)
def test_watermark_zero_rejects_late_zero_edge_and_inexact_prefix(kind, bad):
    with channels(kind) as (left, right):
        exchange(left, right, wire.EmissionComplete(0, 0))
        with pytest.raises(wire.WireError):
            left.send(bad, deadline=deadline())


@pytest.mark.parametrize("edge_id", [0, 2])
def test_request_ids_must_start_one_and_be_contiguous(kind, edge_id):
    with channels(kind) as (left, _), pytest.raises(wire.WireError):
        left.send(wire.EdgeRequest(edge_id, 0, 0, 1), deadline=deadline())


def test_response_correlation_ignores_independent_clock_domains(kind):
    with channels(kind) as (left, right):
        exchange(left, right, wire.EdgeRequest(1, 100, 120, 1))
        exchange(right, left, wire.EdgeResponse(1, 1, 0))
        with pytest.raises(wire.WireError):
            right.send(wire.EdgeResponse(1, 999, 0), deadline=deadline())


def test_bidirectional_concurrent_requests_and_responses(kind):
    with channels(kind) as (left, right):
        barrier = threading.Barrier(2, timeout=BOUND)

        def participant(local, peer_bit):
            barrier.wait()
            local.send(wire.EdgeRequest(1, 0, 0, peer_bit), deadline=deadline())
            got = local.receive(deadline=deadline())
            assert isinstance(got, wire.EdgeRequest)
            local.send(wire.EdgeResponse(got.edge_id, 7, 1 - peer_bit), deadline=deadline())
            response = local.receive(deadline=deadline())
            assert response == wire.EdgeResponse(1, 7, peer_bit)

        jobs = [Job(lambda: participant(left, 0)), Job(lambda: participant(right, 1))]
        for job in jobs:
            job.result()


def test_response_can_arrive_before_request_sender_consumes_application_queue(kind):
    with channels(kind) as (left, right):
        exchange(right, left, wire.Progress(0))
        left.send(wire.EdgeRequest(1, 0, 0, 1), deadline=deadline())
        assert right.receive(deadline=deadline()) == wire.EdgeRequest(1, 0, 0, 1)
        right.send(wire.EdgeResponse(1, 0, 0), deadline=deadline())
        # A following request in the same direction must not destroy correlation.
        right.send(wire.EdgeRequest(1, 0, 0, 1), deadline=deadline())
        assert left.receive(deadline=deadline()) == wire.EdgeResponse(1, 0, 0)
        assert left.receive(deadline=deadline()) == wire.EdgeRequest(1, 0, 0, 1)


def test_fragmented_header_and_body(kind):
    with sockets(kind) as (a, b):
        wrapped = FragmentSocket(a)
        left, right = (
            wire.TimedWireChannel(wrapped, epoch=EPOCH),
            wire.TimedWireChannel(b, epoch=EPOCH),
        )
        try:
            job = Job(lambda: left.handshake(deadline=deadline()))
            right.handshake(deadline=deadline())
            job.result()
            exchange(left, right, wire.Progress(0))
            exchange(right, left, wire.Progress(0))
            assert wrapped.read_entered.is_set() and wrapped.write_entered.is_set()
        finally:
            left.close()
            right.close()


@pytest.mark.parametrize("cut", [1, HEADER.size - 1, HEADER.size + 1])
def test_partial_eof_wakes_handshake(kind, cut):
    with sockets(kind) as (a, b):
        channel = wire.TimedWireChannel(a, epoch=EPOCH)
        try:
            b.sendall(wire.encode_frame(wire.Frame(EPOCH, 1, wire.Hello()))[:cut])
            b.shutdown(socket.SHUT_WR)
            with pytest.raises(wire.WireError):
                channel.handshake(deadline=deadline())
            assert channel.closed
        finally:
            channel.close()


def test_close_wakes_receive(kind):
    with channels(kind) as (left, _):
        started = threading.Event()

        def receive():
            started.set()
            return left.receive(deadline=deadline())

        job = Job(receive)
        assert started.wait(BOUND)
        left.close()
        with pytest.raises(wire.ChannelClosed):
            job.result()


def test_pre_admission_cancel_preserves_sequence_and_channel(kind):
    with channels(kind) as (left, right):
        before = exchange(left, right, wire.Progress(0))
        cancelled = threading.Event()
        cancelled.set()
        with pytest.raises(wire.Cancelled):
            left.send(wire.Progress(1), deadline=deadline(), cancel_event=cancelled)
        assert not left.closed
        after = exchange(left, right, wire.Progress(1))
        assert after == before + 1


def test_expired_admission_deadline_does_not_send(kind):
    with channels(kind) as (left, right):
        before = exchange(left, right, wire.Progress(0))
        with pytest.raises(wire.DeadlineExceeded):
            left.send(wire.Progress(1), deadline=time.monotonic() - 1)
        assert not left.closed
        after = exchange(left, right, wire.Progress(1))
        assert after == before + 1
