"""Fixed EC/Progress contracts; scripted I/O and local sockets, no emulator.

The scripted fixture suppresses only the reader and supplies an established
Hello state. It executes the production admission, codec, write and termination
paths. Real socketpair cases separately exercise handshake and receive framing.
"""

import importlib.util
import queue
import socket
import sys
import threading
import time
from collections import deque
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "src/pokered_harness/link/timed_wire.py"
SPEC = importlib.util.spec_from_file_location("_tested_timed_wire_batch", SOURCE)
wire = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = wire
SPEC.loader.exec_module(wire)
EPOCH = bytes(range(16))
BOUND = 2.0


class Job:
    def __init__(self, call):
        self.results = queue.Queue()

        def run():
            try:
                self.results.put((True, call()))
            except BaseException as exc:  # noqa: BLE001
                # Carry pytest outcomes and worker failures back to the caller.
                self.results.put((False, exc))

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def result(self):
        self.thread.join(BOUND)
        assert not self.thread.is_alive(), "batch worker exceeded join bound"
        ok, value = self.results.get_nowait()
        if not ok:
            raise value
        return value


class ScriptSocket:
    def __init__(self):
        self.actions = deque()
        self.offered = []
        self.accepted = bytearray()
        self.closed = False

    def getpeername(self):
        return ("127.0.0.1", 1)

    def setblocking(self, value):
        assert value is False

    def send(self, data):
        data = bytes(data)
        self.offered.append(data)
        action = self.actions.popleft() if self.actions else len(data)
        if isinstance(action, BaseException):
            raise action
        count = action(data) if callable(action) else action
        assert 0 <= count <= len(data), "invalid scripted write size"
        self.accepted.extend(data[:count])
        return count

    def shutdown(self, how):
        assert how == socket.SHUT_RDWR

    def close(self):
        self.closed = True


class NoReader:
    def __init__(self, **kwargs):
        pass

    def start(self):
        pass

    def join(self, timeout):
        pass


@pytest.fixture(params=[2, 3])
def revision(request):
    return request.param


@pytest.fixture
def harness(monkeypatch, revision):
    sock = ScriptSocket()
    clock = [100.0]
    monkeypatch.setattr(wire, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(
        wire,
        "threading",
        SimpleNamespace(
            Thread=NoReader,
            Condition=threading.Condition,
            Lock=threading.Lock,
            Event=threading.Event,
            current_thread=threading.current_thread,
        ),
    )
    monkeypatch.setattr(
        wire, "select", SimpleNamespace(select=lambda r, w, x, timeout: ([], w, []))
    )
    channel = wire.TimedWireChannel(sock, epoch=EPOCH, revision=revision)
    hello = wire.Frame(EPOCH, 1, wire.Hello(1 if revision == 3 else 0), revision)
    with channel._condition:
        channel._accept(hello, channel._outgoing, channel._incoming)
        channel._accept(hello, channel._incoming, channel._outgoing)
        channel._hello_sent = True
    yield SimpleNamespace(channel=channel, sock=sock, clock=clock, revision=revision)
    channel.close()


def pair_bytes(revision, sequence=2, through=10):
    return b"".join(
        wire.encode_frame(wire.Frame(EPOCH, sequence + i, message, revision))
        for i, message in enumerate((wire.EmissionComplete(through, 0), wire.Progress(through)))
    )


def send_pair(channel, **kwargs):
    return channel.send_complete_progress(
        wire.EmissionComplete(10, 0), wire.Progress(10), deadline=101.0, **kwargs
    )


def snapshot(channel):
    return deepcopy((channel._outgoing, channel._incoming, channel._hello_sent))


def seed_outstanding(channel):
    """Nonempty bounded collections make accidental clears/aliasing observable."""
    for direction in (channel._outgoing, channel._incoming):
        direction.pending.update({7, 8})
        direction.pending_fences[4] = 8


def assert_unadmitted(h, before):
    assert snapshot(h.channel) == before
    assert not h.channel.closed and h.channel.error is None
    assert h.sock.offered == [] and h.sock.accepted == b""


def assert_terminal(channel, error):
    assert channel.closed and channel.error is error
    for operation in (
        channel.poll,
        lambda: channel.receive(deadline=101.0),
        lambda: send_pair(channel),
    ):
        with pytest.raises(type(error)) as caught:
            operation()
        assert caught.value is error


@pytest.mark.parametrize("bad", [-1, 2**64, True, 1.0, "10"])
def test_batch_invalid_second_payload_has_no_admission(harness, bad):
    h = harness
    before = snapshot(h.channel)
    with pytest.raises(wire.ProtocolError):
        h.channel.send_complete_progress(
            wire.EmissionComplete(10, 0), wire.Progress(bad), deadline=101.0
        )
    assert_unadmitted(h, before)


@pytest.mark.parametrize("invalid", ["progress_regression", "prefix_replay", "watermark_replay"])
def test_batch_invalid_transition_has_no_admission(harness, invalid):
    h = harness
    seed_outstanding(h.channel)
    if invalid == "progress_regression":
        h.channel._outgoing.settled = 11
    elif invalid == "prefix_replay":
        h.channel._outgoing.last_edge_id = 1
        h.channel._outgoing.pending.add(1)
    else:
        h.channel._outgoing.watermark = 11
    before = snapshot(h.channel)
    original = h.channel._outgoing
    with pytest.raises(wire.ProtocolError):
        send_pair(h.channel)
    assert_unadmitted(h, before)
    assert h.channel._outgoing is original


@pytest.mark.parametrize("sequence", [2**64 - 2, 2**64 - 1])
def test_batch_sequence_overflow_has_no_admission(harness, sequence):
    h = harness
    seed_outstanding(h.channel)
    h.channel._outgoing.sequence = sequence
    before = snapshot(h.channel)
    with pytest.raises(wire.ProtocolError):
        send_pair(h.channel)
    assert_unadmitted(h, before)


def test_batch_last_two_uint64_sequences_are_valid(harness):
    h = harness
    h.channel._outgoing.sequence = 2**64 - 3
    send_pair(h.channel)
    assert bytes(h.sock.accepted) == pair_bytes(h.revision, sequence=2**64 - 2)
    assert h.channel._outgoing.sequence == 2**64 - 1
    assert not h.channel.closed


@pytest.mark.parametrize("complete_at,progress_at", [(10, 20), (20, 10)])
def test_batch_frontier_mismatch_preserves_both_directions_and_usability(
    harness, complete_at, progress_at
):
    h = harness
    seed_outstanding(h.channel)
    before = snapshot(h.channel)
    with pytest.raises(wire.ProtocolError, match="must publish the same frontier"):
        h.channel.send_complete_progress(
            wire.EmissionComplete(complete_at, 0), wire.Progress(progress_at), deadline=101.0
        )
    assert_unadmitted(h, before)
    send_pair(h.channel)
    assert bytes(h.sock.accepted) == pair_bytes(h.revision)
    assert h.channel._outgoing.sequence == 3
    assert h.channel._incoming == before[1]


@pytest.mark.parametrize("position", ["first", "second", "reversed", "iterable", "subclass"])
def test_batch_rejects_nonexact_pair_types(harness, position):
    h = harness
    complete, progress = wire.EmissionComplete(10, 0), wire.Progress(10)
    if position == "first":
        complete = wire.Hello(1 if h.revision == 3 else 0)
    elif position == "second":
        progress = wire.Hello(1 if h.revision == 3 else 0)
    elif position == "reversed":
        complete, progress = progress, complete
    elif position == "iterable":
        complete = [complete, progress]
    else:

        class DerivedProgress(wire.Progress):
            pass

        progress = DerivedProgress(10)
    before = snapshot(h.channel)
    with pytest.raises(wire.ProtocolError):
        h.channel.send_complete_progress(complete, progress, deadline=101.0)
    assert_unadmitted(h, before)


def test_batch_bytes_sequence_epoch_and_repeat_preserve_wire_contract(harness):
    h = harness
    assert send_pair(h.channel) is None
    expected = pair_bytes(h.revision)
    assert len(expected) == 88
    assert h.sock.offered == [expected]
    assert bytes(h.sock.accepted) == expected
    for data, sequence, message in (
        (expected[:48], 2, wire.EmissionComplete(10, 0)),
        (expected[48:], 3, wire.Progress(10)),
    ):
        assert wire.decode_frame(data) == wire.Frame(EPOCH, sequence, message, h.revision)
    assert h.channel._outgoing.sequence == 3
    assert h.channel._outgoing.watermark == h.channel._outgoing.settled == 10
    # Equal watermarks/progress remain valid; repeated values are not wire replay.
    send_pair(h.channel)
    assert bytes(h.sock.accepted) == expected + pair_bytes(h.revision, sequence=4)
    assert h.channel._outgoing.sequence == 5


def test_batch_frontier_rule_does_not_restrict_individual_sends(harness):
    h = harness
    h.channel.send(wire.EmissionComplete(20, 0), deadline=101.0)
    h.channel.send(wire.Progress(10), deadline=101.0)
    assert h.channel._outgoing.watermark == 20
    assert h.channel._outgoing.settled == 10
    assert h.channel._outgoing.sequence == 3 and not h.channel.closed


@pytest.mark.parametrize("cut", [1, 31, 32, 47, 48, 49, 87])
def test_batch_short_writes_and_transient_errors_preserve_exact_suffix(harness, cut):
    h = harness
    expected = pair_bytes(h.revision)
    h.sock.actions.extend([cut, BlockingIOError(), InterruptedError(), 88 - cut])
    send_pair(h.channel)
    assert h.sock.offered == [expected, expected[cut:], expected[cut:], expected[cut:]]
    assert bytes(h.sock.accepted) == expected
    assert h.channel._outgoing.sequence == 3 and not h.channel.closed


@pytest.mark.parametrize("action", ["cancel", "expire"])
def test_batch_before_admission_preserves_channel(harness, action):
    h = harness
    event = threading.Event()
    if action == "cancel":
        event.set()
    else:
        h.clock[0] = 102.0
    before = snapshot(h.channel)
    error = wire.Cancelled if action == "cancel" else wire.DeadlineExceeded
    with pytest.raises(error):
        send_pair(h.channel, cancel_event=event)
    assert_unadmitted(h, before)


@pytest.mark.parametrize("action", ["cancel", "expire"])
def test_batch_validation_deadline_or_cancel_precedes_atomic_admission(
    harness, monkeypatch, action
):
    h = harness
    event = threading.Event()
    original_accept = h.channel._accept

    def accept(frame, direction, opposite):
        original_accept(frame, direction, opposite)
        if isinstance(frame.message, wire.Progress):
            event.set() if action == "cancel" else h.clock.__setitem__(0, 102.0)

    monkeypatch.setattr(h.channel, "_accept", accept)
    before = snapshot(h.channel)
    error = wire.Cancelled if action == "cancel" else wire.DeadlineExceeded
    with pytest.raises(error):
        send_pair(h.channel, cancel_event=event)
    assert_unadmitted(h, before)


@pytest.mark.parametrize("cut", [0, 1, 48, 49, 88])
@pytest.mark.parametrize("action", ["cancel", "expire"])
def test_batch_admitted_failure_including_final_byte_is_terminal(harness, monkeypatch, cut, action):
    h = harness
    event = threading.Event()

    def trigger():
        assert h.channel._outgoing.sequence == 3
        event.set() if action == "cancel" else h.clock.__setitem__(0, 102.0)

    def write(data):
        trigger()
        return cut

    def ready(r, w, x, timeout):
        trigger()
        return [], w, []

    if cut:
        h.sock.actions.append(write)
    else:
        monkeypatch.setattr(wire.select, "select", ready)
    error = wire.Cancelled if action == "cancel" else wire.DeadlineExceeded
    with pytest.raises(error) as caught:
        send_pair(h.channel, cancel_event=event)
    assert bytes(h.sock.accepted) == pair_bytes(h.revision)[:cut]
    assert_terminal(h.channel, caught.value)
    assert h.sock.closed


@pytest.mark.parametrize("fault", [0, OSError("write failed")])
def test_batch_zero_or_failed_write_is_terminal(harness, fault):
    h = harness
    h.sock.actions.extend([48, fault])
    with pytest.raises(wire.ChannelClosed) as caught:
        send_pair(h.channel)
    assert bytes(h.sock.accepted) == pair_bytes(h.revision)[:48]
    assert_terminal(h.channel, caught.value)


@pytest.mark.parametrize("stage", ["select", "send"])
@pytest.mark.parametrize("secondary", [OSError, ValueError, wire.Cancelled])
@pytest.mark.parametrize("cut", [0, 48])
def test_batch_writer_preserves_original_terminal_error_identity(
    harness, monkeypatch, stage, secondary, cut
):
    h = harness
    original = wire.ProtocolError("reader rejected peer sequence")

    def fail(*args):
        h.channel._terminate(original)
        raise secondary("secondary writer error")

    if cut:
        h.sock.actions.append(cut)
    if stage == "select":

        def ready(r, w, x, timeout):
            if len(h.sock.accepted) < cut:
                return [], w, []
            return fail()

        monkeypatch.setattr(wire.select, "select", ready)
    else:
        h.sock.actions.append(fail)
    with pytest.raises(wire.ProtocolError) as caught:
        send_pair(h.channel)
    assert caught.value is original
    assert bytes(h.sock.accepted) == pair_bytes(h.revision)[:cut]
    assert_terminal(h.channel, original)


@pytest.mark.parametrize("action", ["cancel", "expire"])
def test_batch_lock_contention_failure_is_nonterminal(harness, action):
    h = harness
    entered = threading.Event()
    release = threading.Event()
    cancel = threading.Event()
    original_lock = h.channel._send_lock

    class ContendedLock:
        def acquire(self, timeout):
            entered.set()
            assert release.wait(BOUND), "did not release admission barrier"
            return original_lock.acquire(timeout=timeout)

        def release(self):
            original_lock.release()

    h.channel._send_lock = ContendedLock()
    original_lock.acquire()
    before = snapshot(h.channel)
    worker = Job(lambda: send_pair(h.channel, cancel_event=cancel))
    try:
        assert entered.wait(BOUND)
        cancel.set() if action == "cancel" else h.clock.__setitem__(0, 102.0)
        release.set()
        error = wire.Cancelled if action == "cancel" else wire.DeadlineExceeded
        with pytest.raises(error):
            worker.result()
        assert_unadmitted(h, before)
    finally:
        release.set()
        original_lock.release()
        worker.thread.join(BOUND)
        assert not worker.thread.is_alive()
    h.clock[0] = 100.0
    send_pair(h.channel)
    assert h.channel._outgoing.sequence == 3


def test_batch_writer_barrier_excludes_interleaving(harness):
    h = harness
    boundary = threading.Event()
    release = threading.Event()
    contender_entered = threading.Event()
    original_lock = h.channel._send_lock
    calls = [0]

    class ObservedLock:
        def acquire(self, timeout):
            calls[0] += 1
            if calls[0] >= 2:
                contender_entered.set()
            return original_lock.acquire(timeout=timeout)

        def release(self):
            original_lock.release()

    def pause(data):
        boundary.set()
        assert release.wait(BOUND), "did not release EC boundary"
        return len(data)

    h.channel._send_lock = ObservedLock()
    h.sock.actions.extend([48, pause])
    first = Job(lambda: send_pair(h.channel))
    second = None
    try:
        assert boundary.wait(BOUND)
        second = Job(lambda: h.channel.send(wire.Progress(11), deadline=101.0))
        assert contender_entered.wait(BOUND)
        assert bytes(h.sock.accepted) == pair_bytes(h.revision)[:48]
        assert h.channel._outgoing.sequence == 3
        assert len(h.sock.offered) == 2
        release.set()
        assert first.result() is None
        assert second.result() is None
        tail = wire.encode_frame(wire.Frame(EPOCH, 4, wire.Progress(11), h.revision))
        assert bytes(h.sock.accepted) == pair_bytes(h.revision) + tail
    finally:
        release.set()
        for worker in (first, second):
            if worker is not None:
                worker.thread.join(BOUND)
                assert not worker.thread.is_alive()


def test_batch_same_absolute_deadline_covers_lock_and_short_write(harness):
    h = harness
    original_lock = h.channel._send_lock

    class CostlyLock:
        def acquire(self, timeout):
            acquired = original_lock.acquire(timeout=timeout)
            if acquired:
                h.clock[0] += 0.6
            return acquired

        def release(self):
            original_lock.release()

    def write(data):
        h.clock[0] += 0.6
        return 1

    h.channel._send_lock = CostlyLock()
    h.sock.actions.append(write)
    with pytest.raises(wire.DeadlineExceeded) as caught:
        send_pair(h.channel)
    assert bytes(h.sock.accepted) == pair_bytes(h.revision)[:1]
    assert len(h.sock.offered) == 1
    assert_terminal(h.channel, caught.value)


@contextmanager
def real_channels(revision, capacity=4):
    a, b = socket.socketpair()
    left = wire.TimedWireChannel(a, epoch=EPOCH, revision=revision, inbound_capacity=capacity)
    right = wire.TimedWireChannel(b, epoch=EPOCH, revision=revision, inbound_capacity=capacity)
    hello = Job(lambda: left.handshake(deadline=time.monotonic() + BOUND))
    try:
        right.handshake(deadline=time.monotonic() + BOUND)
        hello.result()
        yield left, right
    finally:
        left.close()
        right.close()
        hello.thread.join(BOUND)
        assert not hello.thread.is_alive()
        for channel in (left, right):
            channel._reader.join(BOUND)
            assert not channel._reader.is_alive()


def test_batch_real_receive_order_and_sequence(revision):
    with real_channels(revision) as (left, right):
        complete, progress = wire.EmissionComplete(10, 0), wire.Progress(10)
        left.send_complete_progress(complete, progress, deadline=time.monotonic() + BOUND)
        assert right.receive(deadline=time.monotonic() + BOUND) == complete
        assert right.receive(deadline=time.monotonic() + BOUND) == progress
        assert right._incoming.sequence == left._outgoing.sequence == 3


def test_batch_real_receiver_rejects_sequence_replay(revision):
    with real_channels(revision) as (left, right):
        left.send_complete_progress(
            wire.EmissionComplete(10, 0), wire.Progress(10), deadline=time.monotonic() + BOUND
        )
        assert right.receive(deadline=time.monotonic() + BOUND) == wire.EmissionComplete(10, 0)
        assert right.receive(deadline=time.monotonic() + BOUND) == wire.Progress(10)
        replay = pair_bytes(revision)
        assert left._sock.send(replay) == len(replay)
        with right._condition:
            assert right._condition.wait_for(lambda: right.closed, BOUND)
        assert isinstance(right.error, wire.ProtocolError)
        assert right._incoming.sequence == 3


@pytest.mark.parametrize("capacity", [1, 2])
def test_batch_real_receiver_preserves_queue_bound(revision, capacity):
    with real_channels(revision, capacity) as (left, right):
        try:
            left.send_complete_progress(
                wire.EmissionComplete(10, 0), wire.Progress(10), deadline=time.monotonic() + BOUND
            )
        except wire.ChannelClosed as error:
            # Overflow may close the peer before the sender's final open check.
            assert capacity == 1 and left.error is error
        with right._condition:
            assert right._condition.wait_for(lambda: right.closed or len(right._queue) == 2, BOUND)
        if capacity == 1:
            assert isinstance(right.error, wire.ProtocolError)
            assert str(right.error) == "inbound queue capacity exceeded"
            assert right._incoming.sequence == 2
            assert len(right._queue) == 0
        else:
            assert not right.closed
            assert len(right._queue) == capacity
            assert right.poll() == wire.EmissionComplete(10, 0)
            assert right.poll() == wire.Progress(10)
            assert right.poll() is None
