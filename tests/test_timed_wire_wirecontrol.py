"""Wire-control v2/v3 revisions, capabilities and fence contracts (#151).

Split from ``tests/test_timed_wire.py`` for #151 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module.
"""

from __future__ import annotations

import struct
import threading
import time
from contextlib import contextmanager

import pytest

from tests._timed_wire_support import (
    BOUND,
    EPOCH,
    HEADER,
    Job,
    channels,
    deadline,
    exchange,
    gated_channels,
    sockets,
    wait_state,
    wire,
)


@pytest.mark.parametrize(
    "message,tag,body",
    [
        (wire.Hello(), 1, b"\x00" * 4),
        (wire.Progress(9), 2, struct.pack(">Q", 9)),
        (wire.EdgeRequest(1, 2, 3, 1), 3, struct.pack(">QQQB", 1, 2, 3, 1)),
        (wire.EmissionComplete(7, 1), 4, struct.pack(">QQ", 7, 1)),
        (wire.EdgeResponse(1, 4, 0), 5, struct.pack(">QQB", 1, 4, 0)),
    ],
)
def test_wirecontrol_v2_bytes_and_trailing_revision_default(message, tag, body):
    implicit = wire.Frame(EPOCH, 17, message)
    explicit = wire.Frame(EPOCH, 17, message, 2)
    expected = HEADER.pack(b"PKTW", 2, tag, len(body), EPOCH, 17) + body
    assert implicit.revision == 2
    assert implicit == explicit
    assert wire.encode_frame(implicit) == wire.encode_frame(explicit) == expected
    assert wire.decode_frame(expected) == implicit


@pytest.mark.parametrize("revision", [2, 3])
def test_wirecontrol_channel_revision_readonly_and_alias(kind, revision):
    assert wire.Channel is wire.TimedWireChannel
    with channels(kind, revision=revision) as (left, right):
        assert left.revision == right.revision == revision
        with pytest.raises(AttributeError):
            left.revision = 3 if revision == 2 else 2
        exchange(left, right, wire.Progress(0))
    with sockets(kind) as (a, _):
        default = wire.Channel(a, epoch=EPOCH)
        try:
            assert default.revision == 2
        finally:
            default.close()


@pytest.mark.parametrize("revision", [True, False, 2.0, 3.0, "3", None, 0, 1, 4, 256])
def test_wirecontrol_strict_revision(revision):
    with pytest.raises(wire.ProtocolError):
        wire.encode_frame(wire.Frame(EPOCH, 1, wire.Hello(), revision))
    with sockets("socketpair") as (a, _), pytest.raises(ValueError):
        wire.TimedWireChannel(a, epoch=EPOCH, revision=revision)


@pytest.mark.parametrize("revision,caps", [(2, 1), (3, 0), (3, 2), (3, 2**32 - 1)])
def test_wirecontrol_capabilities_codec_rejects_mismatch(revision, caps):
    with pytest.raises(wire.ProtocolError):
        wire.encode_frame(wire.Frame(EPOCH, 1, wire.Hello(caps), revision))
    data = HEADER.pack(b"PKTW", revision, 1, 4, EPOCH, 1) + struct.pack(">I", caps)
    with pytest.raises(wire.ProtocolError):
        wire.decode_frame(data)


@pytest.mark.parametrize("caps", [True, False, 1.0, "1", None, -1, 2**32])
def test_wirecontrol_strict_v3_capability_type(caps):
    with pytest.raises(wire.ProtocolError):
        wire.encode_frame(wire.Frame(EPOCH, 1, wire.Hello(caps), 3))


@pytest.mark.parametrize("local,peer,caps", [(3, 3, 0), (3, 3, 2), (3, 2, 0), (2, 3, 1), (3, 4, 1)])
def test_wirecontrol_handshake_rejects_caps_and_revision_without_downgrade(kind, local, peer, caps):
    with sockets(kind) as (a, b):
        channel = wire.TimedWireChannel(a, epoch=EPOCH, revision=local)
        try:
            b.sendall(HEADER.pack(b"PKTW", peer, 1, 4, EPOCH, 1) + struct.pack(">I", caps))
            with pytest.raises(wire.ProtocolError):
                channel.handshake(deadline=deadline())
            assert channel.closed and channel.revision == local
        finally:
            channel.close()


@pytest.mark.parametrize(
    "name,args,tag,body",
    [
        ("Hello", (1,), 1, struct.pack(">I", 1)),
        ("Sync", (0,), 6, b"\x00"),
        ("Sync", (255,), 6, b"\xff"),
        ("Fence", (1, 0), 7, struct.pack(">QQ", 1, 0)),
        ("Fence", (2**64 - 1, 2**64 - 1), 7, b"\xff" * 16),
        ("FenceAck", (1, 0), 8, struct.pack(">QQ", 1, 0)),
        ("FenceAck", (2**64 - 1, 2**64 - 1), 8, b"\xff" * 16),
    ],
)
def test_wirecontrol_v3_exact_bytes(name, args, tag, body):
    message = getattr(wire, name)(*args)
    frame = wire.Frame(EPOCH, 9, message, 3)
    expected = HEADER.pack(b"PKTW", 3, tag, len(body), EPOCH, 9) + body
    assert wire.encode_frame(frame) == expected
    assert wire.decode_frame(expected) == frame
    if name != "Hello":
        with pytest.raises(wire.ProtocolError):
            wire.encode_frame(wire.Frame(EPOCH, 9, message))
        with pytest.raises(wire.ProtocolError):
            wire.decode_frame(expected[:4] + b"\x02" + expected[5:])
    for malformed in (expected[:-1], expected + b"x"):
        with pytest.raises(wire.ProtocolError):
            wire.decode_frame(malformed)


@pytest.mark.parametrize("value", [-1, 256, True, False, 0.0, "0", None])
def test_wirecontrol_sync_strict_uint8(value):
    with pytest.raises(wire.ProtocolError):
        wire.encode_frame(wire.Frame(EPOCH, 1, wire.Sync(value), 3))


@pytest.mark.parametrize("name", ["Fence", "FenceAck"])
@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("value", [-1, 2**64, True, False, 1.0, "1", None])
def test_wirecontrol_fence_strict_uint64(name, index, value):
    args = [1, 0]
    args[index] = value
    with pytest.raises(wire.ProtocolError):
        wire.encode_frame(wire.Frame(EPOCH, 1, getattr(wire, name)(*args), 3))


def test_wirecontrol_sync_does_not_advance_edge_or_fence_prefix(kind):
    with channels(kind, revision=3) as (left, right):
        for marker in (0, 255, 0):
            exchange(left, right, wire.Sync(marker))
        exchange(left, right, wire.Fence(1, 0))
        exchange(right, left, wire.FenceAck(1, 0))
        exchange(left, right, wire.EdgeRequest(1, 0, 0, 1))


@pytest.mark.parametrize("prefix", [0, 2])
def test_wirecontrol_fence_requires_exact_current_prefix_on_send(kind, prefix):
    with channels(kind, revision=3) as (left, right):
        exchange(left, right, wire.EdgeRequest(1, 0, 0, 1))
        sequence = left._outgoing.sequence
        with pytest.raises(wire.ProtocolError):
            left.send(wire.Fence(1, prefix), deadline=deadline())
        assert left._outgoing.sequence == sequence and not left.closed
        exchange(left, right, wire.Fence(1, 1))


def test_wirecontrol_ack_matches_saved_prefix_after_both_directions_advance(kind):
    with channels(kind, revision=3) as (left, right):
        exchange(left, right, wire.EdgeRequest(1, 0, 0, 1))
        exchange(left, right, wire.Fence(1, 1))
        exchange(left, right, wire.EdgeRequest(2, 1, 1, 0))
        for i in range(1, 4):
            exchange(right, left, wire.EdgeRequest(i, i, i, 0))
        for bad in (wire.FenceAck(1, 2), wire.FenceAck(1, 3), wire.FenceAck(2, 1)):
            with pytest.raises(wire.ProtocolError):
                right.send(bad, deadline=deadline())
        exchange(right, left, wire.FenceAck(1, 1))
        with pytest.raises(wire.ProtocolError):
            right.send(wire.FenceAck(1, 1), deadline=deadline())
        exchange(left, right, wire.Fence(2, 2))
        exchange(right, left, wire.FenceAck(2, 2))


@pytest.mark.parametrize("fence_id", [0, 2, 2**64 - 1])
def test_wirecontrol_fence_ids_start_one_and_invalid_send_is_nonmutating(kind, fence_id):
    with channels(kind, revision=3) as (left, right):
        with pytest.raises(wire.ProtocolError):
            left.send(wire.Fence(fence_id, 0), deadline=deadline())
        exchange(left, right, wire.Fence(1, 0))
        exchange(right, left, wire.FenceAck(1, 0))
        with pytest.raises(wire.ProtocolError):
            left.send(wire.Fence(1, 0), deadline=deadline())
        exchange(left, right, wire.Fence(2, 0))


def test_wirecontrol_owner_ack_before_dequeue_and_writer_return_no_autoack(kind):
    with gated_channels(kind, revision=3) as (left, right, gate):
        fence = wire.Fence(1, 0)
        sender = Job(lambda: left.send(fence, deadline=deadline()))
        assert gate.delivered.wait(BOUND)
        wait_state(right, lambda: len(right._queue) == 1)
        # A completed reader publication must not have emitted any ACK.
        with right._condition:
            assert right._outgoing.sequence == 1
        assert left.poll() is None
        right.send(wire.FenceAck(1, 0), deadline=deadline())
        assert left.receive(deadline=deadline()) == wire.FenceAck(1, 0)
        assert sender.thread.is_alive()
        assert right.receive(deadline=deadline()) == fence
        gate.release.set()
        assert sender.result() is None


def test_wirecontrol_bidirectional_concurrent_fences(kind):
    with channels(kind, revision=3) as (left, right):
        barrier = threading.Barrier(2, timeout=BOUND)

        def participant(channel):
            for fence_id in range(1, 9):
                barrier.wait()
                channel.send(wire.Fence(fence_id, 0), deadline=deadline())
                assert channel.receive(deadline=deadline()) == wire.Fence(fence_id, 0)
                channel.send(wire.FenceAck(fence_id, 0), deadline=deadline())
                assert channel.receive(deadline=deadline()) == wire.FenceAck(fence_id, 0)

        jobs = [Job(lambda: participant(left)), Job(lambda: participant(right))]
        for job in jobs:
            job.result()


def test_wirecontrol_pending_fences_bounded_independently_of_queue(kind):
    with channels(kind, revision=3, inbound_capacity=2) as (left, right):
        exchange(left, right, wire.Fence(1, 0))
        exchange(left, right, wire.Fence(2, 0))
        with pytest.raises(wire.ProtocolError):
            left.send(wire.Fence(3, 0), deadline=deadline())
        # ACK correlation is by stored ID, not the newest ID or queue position.
        exchange(right, left, wire.FenceAck(2, 0))
        exchange(left, right, wire.Fence(3, 0))
        exchange(right, left, wire.FenceAck(1, 0))
        exchange(right, left, wire.FenceAck(3, 0))
        exchange(left, right, wire.Fence(4, 0))


@contextmanager
def raw_v3_channel(kind, **options):
    with sockets(kind) as (a, b):
        channel = wire.TimedWireChannel(a, epoch=EPOCH, revision=3, **options)
        try:
            b.sendall(wire.encode_frame(wire.Frame(EPOCH, 1, wire.Hello(1), 3)))
            channel.handshake(deadline=deadline())
            yield channel, b
        finally:
            channel.close()


def test_wirecontrol_reader_finishes_fence_and_next_frame_without_autoack(kind):
    with raw_v3_channel(kind) as (channel, peer):
        peer.sendall(
            wire.encode_frame(wire.Frame(EPOCH, 2, wire.Fence(1, 0), 3))
            + wire.encode_frame(wire.Frame(EPOCH, 3, wire.Sync(255), 3))
        )
        # Reaching the next frame proves the entire fence reader iteration
        # finished, including any work after queue publication.
        wait_state(channel, lambda: channel._incoming.sequence == 3)
        with channel._condition:
            assert channel._outgoing.sequence == 1
            assert channel._incoming.pending_fences == {1: 0}
        channel.send(wire.FenceAck(1, 0), deadline=deadline())
        assert channel.receive(deadline=deadline()) == wire.Fence(1, 0)
        assert channel.receive(deadline=deadline()) == wire.Sync(255)


@pytest.mark.parametrize(
    "case",
    [
        "prefix_behind",
        "prefix_ahead",
        "gap",
        "duplicate",
        "unsolicited_ack",
        "wrong_ack",
        "duplicate_ack",
        "overflow",
        "revision",
    ],
)
def test_wirecontrol_raw_peer_invalid_transitions_fail_closed(kind, case):
    with raw_v3_channel(kind, inbound_capacity=1) as (channel, peer):
        sequence = 1

        def inject(message, revision=3):
            nonlocal sequence
            sequence += 1
            peer.sendall(wire.encode_frame(wire.Frame(EPOCH, sequence, message, revision)))

        if case.startswith("prefix"):
            inject(wire.EdgeRequest(1, 0, 0, 1))
            assert channel.receive(deadline=deadline()) == wire.EdgeRequest(1, 0, 0, 1)
            bad = wire.Fence(1, 0 if case == "prefix_behind" else 2)
        elif case in ("duplicate", "overflow"):
            inject(wire.Fence(1, 0))
            assert channel.receive(deadline=deadline()) == wire.Fence(1, 0)
            bad = wire.Fence(1 if case == "duplicate" else 2, 0)
        elif case in ("wrong_ack", "duplicate_ack"):
            channel.send(wire.Fence(1, 0), deadline=deadline())
            if case == "duplicate_ack":
                inject(wire.FenceAck(1, 0))
                assert channel.receive(deadline=deadline()) == wire.FenceAck(1, 0)
            bad = wire.FenceAck(1, 1 if case == "wrong_ack" else 0)
        elif case == "gap":
            bad = wire.Fence(2, 0)
        elif case == "revision":
            bad = wire.Progress(0)
        else:
            bad = wire.FenceAck(1, 0)
        inject(bad, revision=2 if case == "revision" else 3)
        with pytest.raises(wire.ProtocolError):
            channel.receive(deadline=deadline())
        assert channel.closed and isinstance(channel.error, wire.ProtocolError)


@pytest.mark.parametrize("action", ["cancel", "expire"])
def test_wirecontrol_pre_admission_and_receive_deadlines_preserve_fence(kind, action):
    with channels(kind, revision=3) as (left, right):
        cancel = threading.Event()
        if action == "cancel":
            cancel.set()
        limit = deadline() if action == "cancel" else time.monotonic() - 1
        error = wire.Cancelled if action == "cancel" else wire.DeadlineExceeded
        before = left._outgoing.sequence
        with pytest.raises(error):
            left.send(wire.Fence(1, 0), deadline=limit, cancel_event=cancel)
        assert not left.closed and left._outgoing.sequence == before
        left.send(wire.Fence(1, 0), deadline=deadline())
        wait_state(right, lambda: len(right._queue) == 1)
        with pytest.raises(error):
            right.receive(deadline=limit, cancel_event=cancel)
        assert not right.closed
        assert right.receive(deadline=deadline()) == wire.Fence(1, 0)
        exchange(right, left, wire.FenceAck(1, 0))


@pytest.mark.parametrize("name,args", [("Sync", (0,)), ("Fence", (1, 0)), ("FenceAck", (1, 0))])
def test_wirecontrol_default_channel_rejects_new_types_without_mutation(kind, name, args):
    with channels(kind) as (left, right):
        before = left._outgoing.sequence
        with pytest.raises(wire.ProtocolError):
            left.send(getattr(wire, name)(*args), deadline=deadline())
        assert not left.closed and left._outgoing.sequence == before
        exchange(left, right, wire.Progress(0))


def test_wirecontrol_two_owners_cannot_ack_same_fence_twice(kind):
    with channels(kind, revision=3) as (left, right):
        exchange(left, right, wire.Fence(1, 0))
        barrier = threading.Barrier(2, timeout=BOUND)

        def ack():
            barrier.wait()
            try:
                right.send(wire.FenceAck(1, 0), deadline=deadline())
            except wire.ProtocolError:
                return "rejected"
            return "sent"

        jobs = [Job(ack), Job(ack)]
        assert sorted(job.result() for job in jobs) == ["rejected", "sent"]
        assert left.receive(deadline=deadline()) == wire.FenceAck(1, 0)
        assert right._outgoing.sequence == 2
        assert not left.closed and not right.closed


@pytest.mark.parametrize("counter", ["sequence", "last_fence_id"])
def test_wirecontrol_uint64_counter_exhaustion_cannot_wrap(kind, counter):
    with channels(kind, revision=3) as (left, right):
        maximum = 2**64 - 1
        # Reach the otherwise impractically distant boundary without billions
        # of frames; normal acceptance and real socket I/O handle the last ID.
        with left._condition:
            setattr(left._outgoing, counter, maximum - 1)
        with right._condition:
            setattr(right._incoming, counter, maximum - 1)
        fence_id = maximum if counter == "last_fence_id" else 1
        exchange(left, right, wire.Fence(fence_id, 0))
        exchange(right, left, wire.FenceAck(fence_id, 0))
        for next_id in (0, fence_id + 1):
            with pytest.raises(wire.ProtocolError):
                left.send(wire.Fence(next_id, 0), deadline=deadline())
        assert getattr(left._outgoing, counter) == maximum
        assert not left.closed and not right.closed


def test_wirecontrol_close_clears_both_pending_maps_and_wakes_waiter(kind):
    with channels(kind, revision=3) as (left, right):
        exchange(left, right, wire.Fence(1, 0))
        exchange(right, left, wire.Fence(1, 0))
        with left._condition:
            assert left._incoming.pending_fences == {1: 0}
            assert left._outgoing.pending_fences == {1: 0}
        entered = threading.Event()

        def receive():
            entered.set()
            return left.receive(deadline=deadline())

        receiver = Job(receive)
        assert entered.wait(BOUND)
        left.close()
        with pytest.raises(wire.ChannelClosed):
            receiver.result()
        assert left._incoming.pending_fences == left._outgoing.pending_fences == {}
        assert not left._reader.is_alive()
