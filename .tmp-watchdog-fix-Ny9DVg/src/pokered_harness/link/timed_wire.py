"""Opt-in timed-wire v2/v3 codec and socket channel; no emulator integration.

Epochs identify a session, not an authenticated peer. Deadlines are absolute
``time.monotonic()`` values. Hello is handled internally by ``handshake``;
``receive`` returns application messages. Invalid local messages do not change
state. Once a send is admitted, any failure closes the channel (its delivery
is uncertain). Timeout/cancellation before admission or while receiving is
nonterminal. Terminal errors are retained in ``error`` and raised to waiters.
"""

from __future__ import annotations

import math
import select
import socket
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import TypeAlias


class WireError(Exception):
    """Base channel/codec error."""


class ProtocolError(WireError, ValueError):
    """Malformed frame or invalid protocol transition."""


class ChannelClosed(WireError):
    """Local close, EOF, or socket failure."""


class DeadlineExceeded(WireError, TimeoutError):
    """An absolute deadline expired."""


class Cancelled(WireError):
    """The caller's cancellation event was set."""


@dataclass(frozen=True)
class Hello:
    capabilities: int = 0


@dataclass(frozen=True)
class Progress:
    settled_half_cycles: int


@dataclass(frozen=True)
class EdgeRequest:
    edge_id: int
    scheduled_half_cycle: int
    observed_half_cycle: int
    bit: int


@dataclass(frozen=True)
class EmissionComplete:
    through_half_cycle: int
    last_edge_id: int


@dataclass(frozen=True)
class EdgeResponse:
    edge_id: int
    delivered_half_cycle: int
    bit: int


@dataclass(frozen=True)
class Sync:
    marker_id: int


@dataclass(frozen=True)
class Fence:
    fence_id: int
    through_edge_id: int


@dataclass(frozen=True)
class FenceAck:
    fence_id: int
    through_edge_id: int


Message: TypeAlias = (
    Hello | Progress | EdgeRequest | EmissionComplete | EdgeResponse | Sync | Fence | FenceAck
)


@dataclass(frozen=True)
class Frame:
    epoch: bytes
    sequence: int
    message: Message
    revision: int = 2


HEADER = struct.Struct(">4sBBH16sQ")
MAX_BODY = 25
PARTIAL_READ_TIMEOUT = 10.0
_POLL = 0.05
_LAYOUTS = {
    Hello: (1, struct.Struct(">I")),
    Progress: (2, struct.Struct(">Q")),
    EdgeRequest: (3, struct.Struct(">QQQB")),
    EmissionComplete: (4, struct.Struct(">QQ")),
    EdgeResponse: (5, struct.Struct(">QQB")),
    Sync: (6, struct.Struct(">B")),
    Fence: (7, struct.Struct(">QQ")),
    FenceAck: (8, struct.Struct(">QQ")),
}
_TYPES = {tag: (cls, layout) for cls, (tag, layout) in _LAYOUTS.items()}


def _uint(value: int, bits: int = 64) -> None:
    if type(value) is not int or not 0 <= value < 1 << bits:
        raise ProtocolError(f"expected uint{bits}")


def _epoch(epoch: bytes) -> None:
    if type(epoch) is not bytes or len(epoch) != 16:
        raise ProtocolError("epoch must be exactly 16 bytes")


def _revision(revision: int) -> None:
    if type(revision) is not int or revision not in (2, 3):
        raise ProtocolError("revision must be 2 or 3")


def _payload(message: Message, revision: int = 2) -> tuple[int, bytes]:
    _revision(revision)
    entry = _LAYOUTS.get(type(message))
    if entry is None:
        raise ProtocolError("unknown message type")
    tag, layout = entry
    if revision == 2 and tag >= 6:
        raise ProtocolError("control messages require revision 3")
    values = tuple(getattr(message, name) for name in message.__dataclass_fields__)
    for index, value in enumerate(values):
        bits = (
            32
            if tag == 1
            else 8
            if tag == 6
            else 1
            if tag in (3, 5) and index == len(values) - 1
            else 64
        )
        _uint(value, bits)
    if isinstance(message, Hello) and message.capabilities != (1 if revision == 3 else 0):
        raise ProtocolError("Hello capabilities must be zero for v2 or one for v3")
    if (
        isinstance(message, EdgeRequest)
        and message.scheduled_half_cycle > message.observed_half_cycle
    ):
        raise ProtocolError("scheduled edge exceeds observation")
    return tag, layout.pack(*values)


def encode_frame(frame: Frame) -> bytes:
    if type(frame) is not Frame:
        raise ProtocolError("expected Frame")
    _epoch(frame.epoch)
    _uint(frame.sequence)
    if frame.sequence == 0:
        raise ProtocolError("sequence starts at one")
    tag, body = _payload(frame.message, frame.revision)
    return HEADER.pack(b"PKTW", frame.revision, tag, len(body), frame.epoch, frame.sequence) + body


def _header(data: bytes) -> tuple[type, struct.Struct, bytes, int, int]:
    magic, version, tag, length, epoch, sequence = HEADER.unpack(data)
    if magic != b"PKTW" or version not in (2, 3) or tag not in _TYPES:
        raise ProtocolError("invalid magic, version, or message type")
    if version == 2 and tag >= 6:
        raise ProtocolError("control messages require revision 3")
    cls, layout = _TYPES[tag]
    if length > MAX_BODY or length != layout.size or sequence == 0:
        raise ProtocolError("invalid payload length or sequence")
    return cls, layout, epoch, sequence, version


def decode_frame(data: bytes) -> Frame:
    if type(data) is not bytes or len(data) < HEADER.size:
        raise ProtocolError("expected complete frame bytes")
    cls, layout, epoch, sequence, revision = _header(data[: HEADER.size])
    if len(data) != HEADER.size + layout.size:
        raise ProtocolError("frame length mismatch")
    message = cls(*layout.unpack(data[HEADER.size :]))
    _payload(message, revision)
    return Frame(epoch, sequence, message, revision)


@dataclass
class _Direction:
    sequence: int = 0
    hello: bool = False
    last_edge_id: int = 0
    pending: set[int] = field(default_factory=set)
    last_fence_id: int = 0
    pending_fences: dict[int, int] = field(default_factory=dict)
    settled: int = 0
    watermark: int | None = None


def _deadline(deadline: float) -> None:
    try:
        finite = type(deadline) in (int, float) and math.isfinite(float(deadline))
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError("deadline must be a finite absolute monotonic time")


def _remaining(deadline: float, cancel_event: threading.Event | None) -> float:
    if cancel_event is not None and cancel_event.is_set():
        raise Cancelled("operation cancelled")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DeadlineExceeded("operation deadline expired")
    return min(remaining, _POLL)


class TimedWireChannel:
    """Own a connected socket, switching it to nonblocking mode.

    A daemon reader performs only framing, protocol state, and bounded queue
    work. Both directional outstanding sets and the receive queue are bounded
    by inbound_capacity. No callbacks run on the reader. Close wakes all
    waiters and joins the reader for at most 0.2 seconds, never itself.
    Revision 3 requires Hello(1); there is no revision negotiation. Each
    directional pending-fence map is also bounded by inbound_capacity.
    The reader queues fences without acknowledging them: the owner must send
    FenceAck only after applying the identified prefix.
    """

    def __init__(
        self,
        sock: socket.socket,
        *,
        epoch: bytes,
        inbound_capacity: int = 64,
        revision: int = 2,
    ):
        _epoch(epoch)
        _revision(revision)
        if type(inbound_capacity) is not int or inbound_capacity < 1:
            raise ValueError("inbound_capacity must be a positive integer")
        sock.getpeername()
        sock.setblocking(False)
        self._sock = sock
        self._epoch = epoch
        self._revision = revision
        self._capacity = inbound_capacity
        self._condition = threading.Condition()
        self._send_lock = threading.Lock()
        self._closed = threading.Event()
        self._error: WireError | None = None
        self._incoming = _Direction()
        self._outgoing = _Direction()
        self._hello_sent = False
        self._queue: deque[Frame] = deque()
        self._reader = threading.Thread(
            target=self._read_loop, name="timed-wire-reader", daemon=True
        )
        self._reader.start()

    @property
    def epoch(self) -> bytes:
        return self._epoch

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    @property
    def error(self) -> WireError | None:
        with self._condition:
            return self._error

    def _check_open(self) -> None:
        if self._error is not None:
            raise self._error

    def _terminate(self, error: WireError) -> WireError:
        """Retain and return the first terminal reason, including close races."""
        with self._condition:
            if self._error is not None:
                return self._error
            self._error = error
            self._closed.set()
            self._queue.clear()
            self._incoming.pending.clear()
            self._outgoing.pending.clear()
            self._incoming.pending_fences.clear()
            self._outgoing.pending_fences.clear()
            self._condition.notify_all()
        # Never hold protocol state locks across socket operations.
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        return error

    def close(self) -> None:
        self._terminate(ChannelClosed("channel closed"))
        if threading.current_thread() is not self._reader:
            self._reader.join(0.2)

    def _accept(self, frame: Frame, direction: _Direction, opposite: _Direction) -> None:
        """Validate fully, then mutate; caller holds the condition lock."""
        message = frame.message
        if frame.revision != self.revision:
            raise ProtocolError("revision mismatch")
        if frame.epoch != self.epoch or frame.sequence != direction.sequence + 1:
            raise ProtocolError("epoch or sequence mismatch")
        if isinstance(message, Hello):
            if direction.hello or frame.sequence != 1:
                raise ProtocolError("Hello must be first and unique")
        elif not direction.hello or not opposite.hello or not self._hello_sent:
            raise ProtocolError("mutual Hello required")
        if isinstance(message, Progress) and message.settled_half_cycles < direction.settled:
            raise ProtocolError("settled progress regressed")
        if isinstance(message, EdgeRequest):
            if message.edge_id != direction.last_edge_id + 1:
                raise ProtocolError("edge IDs must be contiguous from one")
            if len(direction.pending) >= self._capacity:
                raise ProtocolError("outstanding edge capacity exceeded")
            if (
                direction.watermark is not None
                and message.scheduled_half_cycle <= direction.watermark
            ):
                raise ProtocolError("edge falls within completed emission prefix")
        if isinstance(message, EmissionComplete):
            if message.last_edge_id != direction.last_edge_id:
                raise ProtocolError("completion must identify exact request prefix")
            if direction.watermark is not None and message.through_half_cycle < direction.watermark:
                raise ProtocolError("emission watermark regressed")
        if isinstance(message, EdgeResponse) and message.edge_id not in opposite.pending:
            raise ProtocolError("response does not match an outstanding request")
        if isinstance(message, Fence):
            if message.fence_id != direction.last_fence_id + 1:
                raise ProtocolError("fence IDs must be contiguous from one")
            if message.through_edge_id != direction.last_edge_id:
                raise ProtocolError("fence must identify exact request prefix")
            if len(direction.pending_fences) >= self._capacity:
                raise ProtocolError("outstanding fence capacity exceeded")
        if (
            isinstance(message, FenceAck)
            and opposite.pending_fences.get(message.fence_id) != message.through_edge_id
        ):
            raise ProtocolError("ack does not match an outstanding fence prefix")

        direction.sequence = frame.sequence
        if isinstance(message, Hello):
            direction.hello = True
        elif isinstance(message, Progress):
            direction.settled = message.settled_half_cycles
        elif isinstance(message, EdgeRequest):
            direction.last_edge_id = message.edge_id
            direction.pending.add(message.edge_id)
        elif isinstance(message, EmissionComplete):
            direction.watermark = message.through_half_cycle
        elif isinstance(message, EdgeResponse):
            # delivered_half_cycle belongs to the responder's clock domain.
            opposite.pending.remove(message.edge_id)
        elif isinstance(message, Fence):
            direction.last_fence_id = message.fence_id
            direction.pending_fences[message.fence_id] = message.through_edge_id
        elif isinstance(message, FenceAck):
            # Compare the saved prefix, not the direction's possibly newer edge ID.
            del opposite.pending_fences[message.fence_id]

    def handshake(self, *, deadline: float, cancel_event: threading.Event | None = None) -> None:
        _deadline(deadline)
        self._send(Hello(1 if self.revision == 3 else 0), deadline, cancel_event, hello_once=True)
        with self._condition:
            while True:
                self._check_open()
                wait = _remaining(deadline, cancel_event)
                if self._incoming.hello and self._hello_sent:
                    return
                self._condition.wait(wait)

    def send(
        self, message: Message, *, deadline: float, cancel_event: threading.Event | None = None
    ) -> None:
        self._send(message, deadline, cancel_event)

    def send_complete_progress(
        self,
        complete: EmissionComplete,
        progress: Progress,
        *,
        deadline: float,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Admit a validated fixed pair and send EC then Progress without interleaving.

        Both frames publish the same frontier and share one absolute deadline
        and one send lock. Invalid
        local pairs leave state and bytes untouched; any failure after admission
        closes the channel because delivery may be partial and cannot be retried.
        """
        if type(complete) is not EmissionComplete or type(progress) is not Progress:
            raise ProtocolError("expected EmissionComplete then Progress")
        if complete.through_half_cycle != progress.settled_half_cycles:
            raise ProtocolError("completion and progress must publish the same frontier")
        self._send(progress, deadline, cancel_event, complete=complete)

    def _send(
        self,
        message: Message,
        deadline: float,
        cancel_event: threading.Event | None,
        *,
        hello_once: bool = False,
        complete: EmissionComplete | None = None,
    ) -> Frame:
        _deadline(deadline)
        if complete is not None:
            _payload(complete, self.revision)
        _payload(message, self.revision)
        while True:
            with self._condition:
                self._check_open()
            if self._send_lock.acquire(timeout=_remaining(deadline, cancel_event)):
                break
        admitted = False
        try:
            with self._condition:
                self._check_open()
                _remaining(deadline, cancel_event)
                if hello_once and self._outgoing.hello:
                    return Frame(self.epoch, 1, message, self.revision)
                if complete is None:
                    frame = Frame(self.epoch, self._outgoing.sequence + 1, message, self.revision)
                    data = encode_frame(frame)
                    self._accept(frame, self._outgoing, self._incoming)
                else:
                    # The fixed EC/Progress pair only changes outgoing scalar
                    # state. Copy bounded collections too so validation remains
                    # isolated from live state until both frames are accepted.
                    outgoing = replace(
                        self._outgoing,
                        pending=set(self._outgoing.pending),
                        pending_fences=dict(self._outgoing.pending_fences),
                    )
                    prefix = Frame(self.epoch, outgoing.sequence + 1, complete, self.revision)
                    frame = Frame(self.epoch, outgoing.sequence + 2, message, self.revision)
                    data = encode_frame(prefix) + encode_frame(frame)
                    self._accept(prefix, outgoing, self._incoming)
                    self._accept(frame, outgoing, self._incoming)
                    _remaining(deadline, cancel_event)
                    self._outgoing = outgoing
                admitted = True
                self._condition.notify_all()
            # Admission precedes the first byte: the reader can correlate a
            # response even if the peer responds before this send returns.
            offset = 0
            while offset < len(data):
                with self._condition:
                    self._check_open()
                wait = _remaining(deadline, cancel_event)
                _, writable, _ = select.select([], [self._sock], [], wait)
                if not writable:
                    continue
                _remaining(deadline, cancel_event)
                try:
                    count = self._sock.send(data[offset:])
                except (BlockingIOError, InterruptedError):
                    continue
                if count == 0:
                    raise ChannelClosed("socket send returned zero")
                offset += count
            _remaining(deadline, cancel_event)
            with self._condition:
                self._check_open()
                if isinstance(message, Hello):
                    self._hello_sent = True
                    self._condition.notify_all()
            return frame
        except (WireError, OSError, ValueError) as exc:
            error = exc if isinstance(exc, WireError) else ChannelClosed(str(exc))
            if admitted:
                # Reader termination can close the fd during select/send.
                # Surface its original reason instead of the resulting OS error.
                error = self._terminate(error)
            raise error from None
        finally:
            self._send_lock.release()

    def poll(self) -> Message | None:
        """Return the next queued message immediately, or None; raise on close."""
        with self._condition:
            self._check_open()
            return self._queue.popleft().message if self._queue else None

    def receive(self, *, deadline: float, cancel_event: threading.Event | None = None) -> Message:
        _deadline(deadline)
        with self._condition:
            while True:
                self._check_open()
                wait = _remaining(deadline, cancel_event)
                if self._queue:
                    return self._queue.popleft().message
                self._condition.wait(wait)

    def _read_loop(self) -> None:
        buffer = bytearray()
        target = HEADER.size
        deadline: float | None = None
        try:
            while not self.closed:
                wait = _POLL if deadline is None else _remaining(deadline, None)
                readable, _, _ = select.select([self._sock], [], [], wait)
                if not readable:
                    continue
                if deadline is not None:
                    _remaining(deadline, None)
                try:
                    chunk = self._sock.recv(target - len(buffer))
                except (BlockingIOError, InterruptedError):
                    continue
                if not chunk:
                    raise ChannelClosed("peer closed connection")
                if deadline is None:
                    deadline = time.monotonic() + PARTIAL_READ_TIMEOUT
                buffer.extend(chunk)
                _remaining(deadline, None)
                if len(buffer) != target:
                    continue
                if target == HEADER.size:
                    _, layout, epoch, _, revision = _header(bytes(buffer))
                    if revision != self.revision:
                        raise ProtocolError("revision mismatch")
                    if epoch != self.epoch:
                        raise ProtocolError("epoch mismatch")
                    target += layout.size
                    continue
                frame = decode_frame(bytes(buffer))
                with self._condition:
                    self._check_open()
                    # A peer may answer fully delivered Hello bytes before
                    # our send syscall returns and publishes _hello_sent.
                    # Wait only for an already admitted Hello, using this
                    # frame's original deadline and releasing the state lock.
                    while (
                        not isinstance(frame.message, Hello)
                        and self._outgoing.hello
                        and not self._hello_sent
                    ):
                        self._condition.wait(_remaining(deadline, None))
                        self._check_open()
                    _remaining(deadline, None)
                    if not isinstance(frame.message, Hello) and len(self._queue) >= self._capacity:
                        raise ProtocolError("inbound queue capacity exceeded")
                    self._accept(frame, self._incoming, self._outgoing)
                    if not isinstance(frame.message, Hello):
                        self._queue.append(frame)
                    self._condition.notify_all()
                buffer.clear()
                target = HEADER.size
                deadline = None
        except (WireError, OSError, ValueError) as exc:
            error = exc if isinstance(exc, WireError) else ChannelClosed(str(exc))
            self._terminate(error)


Channel = TimedWireChannel
