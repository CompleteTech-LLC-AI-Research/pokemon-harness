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
def gated_channels(kind):
    with sockets(kind) as (a, b):
        gate = ReturnGateSocket(a)
        left, right = (
            wire.TimedWireChannel(gate, epoch=EPOCH),
            wire.TimedWireChannel(b, epoch=EPOCH),
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
