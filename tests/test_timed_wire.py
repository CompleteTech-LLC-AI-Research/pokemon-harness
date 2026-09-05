"""Isolated v2 wire contracts: real local sockets, no emulator imports or ROMs."""

import importlib.util
import queue
import socket
import struct
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

# Loading the leaf avoids link.__init__ and its runtime-facing public exports.
SOURCE = Path(__file__).resolve().parents[1] / "src/pokered_harness/link/timed_wire.py"
SPEC = importlib.util.spec_from_file_location("_tested_timed_wire", SOURCE)
wire = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = wire
SPEC.loader.exec_module(wire)
EPOCH = bytes(range(16))
HEADER = struct.Struct(">4sBBH16sQ")
BOUND = 2.0


def deadline():
    return time.monotonic() + BOUND


class Job:
    """Capture worker failures and bound every join, including failed tests."""

    def __init__(self, call):
        self.results = queue.Queue()

        def run():
            try:
                self.results.put((True, call()))
            except BaseException as exc:  # noqa: BLE001
                # Propagate pytest outcomes and worker failures to the test thread.
                self.results.put((False, exc))

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def result(self):
        self.thread.join(BOUND + 1)
        assert not self.thread.is_alive(), "wire worker exceeded outer guard"
        ok, value = self.results.get_nowait()
        if not ok:
            raise value
        return value


@contextmanager
def sockets(kind):
    if kind == "socketpair":
        left, right = socket.socketpair()
    else:
        with socket.socket() as listener:
            listener.settimeout(BOUND)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            left = socket.create_connection(listener.getsockname(), timeout=BOUND)
            right, _ = listener.accept()
    try:
        yield left, right
    finally:
        left.close()
        right.close()


@pytest.fixture(params=["socketpair", "tcp"])
def kind(request):
    return request.param


@contextmanager
def channels(kind, **options):
    with sockets(kind) as (a, b):
        left = wire.TimedWireChannel(a, epoch=EPOCH, **options)
        right = wire.TimedWireChannel(b, epoch=EPOCH, **options)
        try:
            first = Job(lambda: left.handshake(deadline=deadline()))
            right.handshake(deadline=deadline())
            first.result()
            yield left, right
        finally:
            left.close()
            right.close()


def exchange(left, right, message):
    sent = left.send(message, deadline=deadline())
    got = right.receive(deadline=deadline())
    assert sent is None
    assert got == message
    return left._outgoing.sequence


@pytest.mark.parametrize(
    "message",
    [
        wire.Hello(),
        wire.Progress(0),
        wire.Progress(2**64 - 1),
        wire.EdgeRequest(1, 0, 0, 0),
        wire.EdgeRequest(2**64 - 1, 2**64 - 1, 2**64 - 1, 1),
        wire.EmissionComplete(0, 0),
        wire.EdgeResponse(1, 0, 1),
    ],
)
def test_codec_roundtrip(message):
    frame = wire.Frame(EPOCH, 1, message)
    assert wire.decode_frame(wire.encode_frame(frame)) == frame


@pytest.mark.parametrize("value", [-1, 2**64, True, False, 1.5, "1", None])
@pytest.mark.parametrize(
    "factory",
    [
        lambda n: wire.Frame(EPOCH, n, wire.Hello()),
        lambda n: wire.Frame(EPOCH, 1, wire.Progress(n)),
        lambda n: wire.Frame(EPOCH, 1, wire.EdgeRequest(n, 0, 0, 0)),
        lambda n: wire.Frame(EPOCH, 1, wire.EdgeRequest(1, n, 0, 0)),
        lambda n: wire.Frame(EPOCH, 1, wire.EdgeRequest(1, 0, n, 0)),
        lambda n: wire.Frame(EPOCH, 1, wire.EmissionComplete(n, 0)),
        lambda n: wire.Frame(EPOCH, 1, wire.EmissionComplete(0, n)),
        lambda n: wire.Frame(EPOCH, 1, wire.EdgeResponse(n, 0, 0)),
        lambda n: wire.Frame(EPOCH, 1, wire.EdgeResponse(1, n, 0)),
    ],
)
def test_codec_strict_uint64(factory, value):
    with pytest.raises(wire.ProtocolError):
        wire.encode_frame(factory(value))


@pytest.mark.parametrize("bit", [-1, 2, True, False, 0.0, "0"])
@pytest.mark.parametrize("factory", [wire.EdgeRequest, wire.EdgeResponse])
def test_codec_strict_bit(factory, bit):
    with pytest.raises(wire.ProtocolError):
        message = factory(1, 0, 0, bit) if factory is wire.EdgeRequest else factory(1, 0, bit)
        wire.encode_frame(wire.Frame(EPOCH, 1, message))


@pytest.mark.parametrize("epoch", [b"", b"x" * 15, b"x" * 17, "x" * 16])
def test_codec_epoch_length_and_type(epoch):
    with pytest.raises(wire.ProtocolError):
        wire.encode_frame(wire.Frame(epoch, 1, wire.Hello()))


@pytest.mark.parametrize("field,value", [(0, b"NOPE"), (1, 1), (1, 3), (2, 255), (3, 65535)])
def test_codec_rejects_unknown_or_malformed_header(field, value):
    encoded = wire.encode_frame(wire.Frame(EPOCH, 1, wire.Hello()))
    fields = list(HEADER.unpack(encoded[: HEADER.size]))
    fields[field] = value
    with pytest.raises(wire.ProtocolError):
        wire.decode_frame(HEADER.pack(*fields) + encoded[HEADER.size :])


def test_codec_requires_exact_frame_length():
    encoded = wire.encode_frame(wire.Frame(EPOCH, 1, wire.Progress(0)))
    for bad in (encoded[:-1], encoded + b"x", encoded[: HEADER.size - 1]):
        with pytest.raises(wire.ProtocolError):
            wire.decode_frame(bad)


def test_handshake_idempotent_and_application_delivery(kind):
    with channels(kind) as (left, right):
        left.handshake(deadline=deadline())
        right.handshake(deadline=deadline())
        exchange(left, right, wire.Progress(0))
        exchange(right, left, wire.Progress(0))
        assert not left.closed and not right.closed


def test_send_requires_handshake(kind):
    with sockets(kind) as (a, _):
        channel = wire.TimedWireChannel(a, epoch=EPOCH)
        try:
            with pytest.raises(wire.WireError):
                channel.send(wire.Progress(0), deadline=deadline())
        finally:
            channel.close()


@pytest.mark.parametrize("peer_epoch,caps", [(b"z" * 16, 0), (EPOCH, 1)])
def test_handshake_rejects_epoch_and_capabilities(kind, peer_epoch, caps):
    with sockets(kind) as (a, b):
        channel = wire.TimedWireChannel(a, epoch=EPOCH)
        try:
            encoded = wire.encode_frame(wire.Frame(peer_epoch, 1, wire.Hello()))
            if caps:
                encoded = encoded[:-1] + bytes([caps])
            b.sendall(encoded)
            with pytest.raises(wire.WireError):
                channel.handshake(deadline=deadline())
            assert channel.closed
        finally:
            channel.close()


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


class FragmentSocket:
    """Force partial I/O while retaining real socket readiness and EOF."""

    def __init__(self, sock):
        self.sock = sock
        self.read_entered = threading.Event()
        self.write_entered = threading.Event()

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def recv(self, size, *args):
        self.read_entered.set()
        return self.sock.recv(min(size, 1), *args)

    def send(self, data, *args):
        self.write_entered.set()
        return self.sock.send(data[:1], *args)


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


def wait_state(channel, predicate):
    with channel._condition:
        assert channel._condition.wait_for(predicate, BOUND), "reader did not reach barrier"


class ReturnGateSocket(FragmentSocket):
    """Put bytes on the wire, then hold the writer before send returns."""

    def __init__(self, sock):
        super().__init__(sock)
        self.armed = False
        self.delivered = threading.Event()
        self.release = threading.Event()

    def send(self, data, *args):
        count = self.sock.send(data, *args)
        if self.armed:
            self.delivered.set()
            assert self.release.wait(BOUND), "test did not release writer"
        return count

    def shutdown(self, how):
        self.release.set()
        return self.sock.shutdown(how)


@contextmanager
def gated_channels(kind, **options):
    with sockets(kind) as (a, b):
        gate = ReturnGateSocket(a)
        left, right = (
            wire.TimedWireChannel(gate, epoch=EPOCH, **options),
            wire.TimedWireChannel(b, epoch=EPOCH, **options),
        )
        try:
            hello = Job(lambda: left.handshake(deadline=deadline()))
            right.handshake(deadline=deadline())
            hello.result()
            gate.armed = True
            yield left, right, gate
        finally:
            gate.release.set()
            left.close()
            right.close()


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


def test_bounded_incoming_queue_fails_closed(kind):
    with channels(kind, inbound_capacity=1) as (left, right):
        left.send(wire.Progress(0), deadline=deadline())
        wait_state(right, lambda: len(right._queue) == 1)
        left.send(wire.Progress(1), deadline=deadline())
        wait_state(right, lambda: right.closed)
        assert isinstance(right.error, wire.ProtocolError)
        with pytest.raises(wire.ProtocolError):
            right.receive(deadline=deadline())


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
