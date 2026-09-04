"""Remote serial-link transport for two-agent paired play.

The in-process :class:`LinkTransport` in ``transport.py`` is a byte-queue
used by the single-process :class:`LinkPair`. That model doesn't fit two
independent MCP processes (one PyBoy + one session per process) each
wanting to trade/battle with the other over a cable. This module is the
remote counterpart — a socket-level peer-to-peer RPC channel that the
:class:`SerialBridge` can call into when its hooks fire.

Wire protocol
-------------

All frames are length-prefixed::

    +--------+------------------+
    | 4 B    | payload          |
    | be u32 |                  |
    +--------+------------------+

The payload's first byte is an opcode:

``0x01 HELLO``
    Body: ``rom_version`` as a length-prefixed UTF-8 string. Sent by
    the peer that calls :meth:`TcpSerialLink.connect`; the listener
    replies with its own HELLO. Both peers learn the peer's ROM
    version (``"red"`` / ``"blue"`` / ``"yellow"``) which
    :class:`SerialBridge` uses for cross-version symbol address
    translation. HELLO must be the first frame received on a connection.

``0x10 EXCHANGE``
    Body: ``kind`` (non-empty length-prefixed ASCII) + ``data``
    (length-prefixed bytes). The two peers are expected to both send an
    EXCHANGE with matching ``kind`` values; each side's
    :meth:`SerialLink.exchange` call returns the peer's data bytes.

``0xFE BYE``
    Peer gracefully disconnects.

Matching is FIFO within a given ``kind`` — important because the game
may issue ``Serial_ExchangeBytes`` multiple times during one trade and
we can't rely on strict one-at-a-time pairing if either peer runs a
few frames ahead. Each endpoint permits only one locally active exchange
for a given ``kind``; this prevents two local callers from racing for the
same unidentifiable FIFO response. The v1 frame has no request identifier,
so a timeout or cancellation after a request is sent closes the channel;
independent late-response recovery requires a versioned wire protocol.

Threading model
---------------

:class:`TcpSerialLink` spins a background reader thread that
deserializes inbound frames and routes ``EXCHANGE`` payloads into
per-kind :class:`queue.Queue` instances. The main thread (where the
emulator and :class:`SerialBridge` hooks run) calls
:meth:`SerialLink.exchange`, which synchronously writes an outbound
frame and blocks on the per-kind queue for the peer's matching frame.

All socket writes are serialized behind a single ``_write_lock`` so the
main thread and the reader thread (which only reads) never collide on
the socket.

Timeouts
--------

:meth:`SerialLink.exchange` accepts a ``timeout_ms`` argument. On
timeout the call raises :class:`SerialLinkTimeout` and the bridge can
decide whether to treat that as a "cable disconnected" hardware error
(which pret's ``Serial_*`` code handles gracefully) or to abort.
"""

from __future__ import annotations

import errno
import math
import queue
import select
import socket
import struct
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Protocol

from pokered_harness.link.network_backend import (
    _is_loopback_sockaddr,
    _validate_connected_socket_loopback,
    validate_loopback_host,
)

# --- opcodes ---------------------------------------------------------------

OP_HELLO: int = 0x01
OP_EXCHANGE: int = 0x10
OP_BYE: int = 0xFE


# A peer can announce a maximum one-megabyte frame, but EXCHANGE payloads are
# limited to the protocol's two-byte length prefix.  Keep the number of
# queued frames and the number of queued payload bytes bounded independently
# so a peer cannot exhaust memory by sending valid frames that nobody awaits.
_MAX_FRAME_SIZE = 1 << 20
_MAX_INBOUND_KINDS = 256
_MAX_INBOUND_FRAMES_PER_KIND = 32
_MAX_INBOUND_FRAMES = 256
_MAX_INBOUND_BYTES = 4 * 1024 * 1024
_DEFAULT_ACCEPT_TIMEOUT_SECONDS = 30.0
_HELLO_TIMEOUT_SECONDS = 5.0

# Socket I/O is non-blocking so closing a link cannot strand a writer behind a
# full kernel buffer.  These short polls also let cancellation/close state be
# observed without changing the public exchange timeout semantics.
_FRAME_READ_TIMEOUT_SECONDS = 5.0
_IO_POLL_S = 0.05
_WRITE_TIMEOUT_S = 1.0
_READER_JOIN_TIMEOUT_S = 1.0


# --- exceptions ------------------------------------------------------------


class SerialLinkError(RuntimeError):
    """Base class for all serial-link errors."""


class SerialLinkClosed(SerialLinkError):
    """Peer disconnected or the link was closed before the operation completed."""


class SerialLinkTimeout(SerialLinkError):
    """:meth:`SerialLink.exchange` blocked past the configured deadline."""


class SerialLinkProtocolError(SerialLinkError):
    """Received a malformed frame or an unexpected opcode."""


SUPPORTED_ROM_VERSIONS = ("red", "blue", "yellow")
_SUPPORTED_ROM_VERSIONS = frozenset(SUPPORTED_ROM_VERSIONS)


def validate_rom_version(version: str) -> str:
    if not isinstance(version, str):
        raise TypeError(f"ROM version must be a string, got {type(version).__name__}")
    normalized = version.strip().lower()
    if normalized not in _SUPPORTED_ROM_VERSIONS:
        raise ValueError(
            f"unsupported ROM version {version!r}; expected one of {list(SUPPORTED_ROM_VERSIONS)}"
        )
    return normalized


def _validate_exchange_kind(kind: str) -> str:
    if not isinstance(kind, str):
        raise TypeError(f"exchange kind must be a string, got {type(kind).__name__}")
    if not kind:
        raise ValueError("exchange kind must not be empty")
    try:
        encoded = kind.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("exchange kind must contain ASCII characters only") from exc
    if len(encoded) > 0xFF:
        raise ValueError(f"exchange kind is too long for the wire protocol: {len(encoded)} bytes")
    return kind


def _coerce_exchange_payload(payload: bytes | bytearray | memoryview) -> bytes:
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError(f"exchange payload must be bytes-like, got {type(payload).__name__}")
    return bytes(payload)


def _validate_timeout_ms(timeout_ms: int) -> int:
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
        raise TypeError("timeout_ms must be a positive integer")
    if timeout_ms <= 0:
        raise ValueError("timeout_ms must be a positive integer")
    return timeout_ms


def _validate_timeout_seconds(timeout_s: float, name: str) -> float:
    if isinstance(timeout_s, bool):
        raise TypeError(f"{name} must be finite and positive")
    try:
        value = float(timeout_s)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite and positive") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


def _validate_cancel_event(cancel_event: threading.Event | None) -> threading.Event | None:
    if cancel_event is None:
        return None
    if not callable(getattr(cancel_event, "is_set", None)) or not callable(
        getattr(cancel_event, "wait", None)
    ):
        raise TypeError("cancel_event must provide is_set() and wait()")
    return cancel_event


def _raise_if_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise SerialLinkClosed("exchange cancelled")


def _acquire_exchange_slot(
    lock: threading.Lock,
    *,
    kind: str,
    deadline: float,
    timeout_ms: int,
    cancel_event: threading.Event | None,
) -> None:
    """Acquire the one-in-flight slot without hiding cancellation/deadlines."""
    while True:
        _raise_if_cancelled(cancel_event)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SerialLinkTimeout(
                f"exchange slot for kind={kind!r} unavailable within {timeout_ms}ms"
            )
        if lock.acquire(timeout=min(_IO_POLL_S, remaining)):
            return


def _validate_port(port: int, *, allow_zero: bool = False) -> int:
    if isinstance(port, bool) or not isinstance(port, int):
        raise TypeError("port must be an integer")
    lower = 0 if allow_zero else 1
    if not lower <= port <= 0xFFFF:
        if allow_zero:
            raise ValueError("port must be in the range 0..65535")
        raise ValueError("port must be in the range 1..65535")
    return port


# --- Protocol --------------------------------------------------------------


class SerialLink(Protocol):
    """Abstraction over the peer serial channel.

    The :class:`SerialBridge` talks to this instead of reading peer
    memory directly. Concrete implementations: :class:`TcpSerialLink`
    for two-process setups; an in-process shim can implement the same
    surface for unit tests without spinning sockets.
    """

    @property
    def connected(self) -> bool: ...

    @property
    def peer_rom_version(self) -> str:
        """``"red"`` / ``"blue"`` / ``"yellow"`` as the peer announced
        in its HELLO. Raises :class:`SerialLinkError` if HELLO didn't
        complete."""
        ...

    def exchange(
        self,
        kind: str,
        my_bytes: bytes,
        *,
        timeout_ms: int = 5000,
        cancel_event: threading.Event | None = None,
    ) -> bytes:
        """Send ``my_bytes`` tagged with ``kind`` and block until peer
        sends a matching EXCHANGE for the same ``kind``. Returns peer's
        bytes.

        FIFO within kind: two rapid back-to-back exchanges of the same
        kind pair up in order. ``cancel_event`` is optional and is polled
        while waiting. If cancellation happens after this call sends its
        request, the connection is closed because v1 has no request id with
        which a later response could be safely correlated.
        """
        ...

    def close(self) -> None: ...


# --- wire helpers ----------------------------------------------------------


def _pack_lp_str(s: str) -> bytes:
    """Length-prefixed UTF-8 string (1-byte length, up to 255 bytes)."""
    if not isinstance(s, str):
        raise TypeError(f"length-prefixed value must be a string, got {type(s).__name__}")
    enc = s.encode("utf-8")
    if len(enc) > 0xFF:
        raise ValueError(f"string too long for length-prefix: {len(enc)}")
    return bytes([len(enc)]) + enc


def _pack_lp_bytes(b: bytes) -> bytes:
    """Length-prefixed byte blob (2-byte length, up to 65535 bytes)."""
    if not isinstance(b, (bytes, bytearray, memoryview)):
        raise TypeError(f"length-prefixed value must be bytes-like, got {type(b).__name__}")
    encoded = bytes(b)
    if len(encoded) > 0xFFFF:
        raise ValueError(f"blob too long for length-prefix: {len(encoded)}")
    return struct.pack(">H", len(encoded)) + encoded


def _read_exactly(
    sock: socket.socket,
    n: int,
    *,
    deadline: float | None = None,
    allow_clean_eof: bool = False,
) -> bytes:
    """Read exactly ``n`` bytes from a non-blocking socket.

    A clean EOF is only valid when no bytes of the next frame have arrived
    yet. EOF after a partial header or body is a malformed frame and must
    fail closed as a protocol error. When a deadline is supplied, it is an
    absolute deadline shared by every partial read of the frame.
    """
    if n < 0:
        raise ValueError("read length must be non-negative")
    buf = bytearray()
    while len(buf) < n:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SerialLinkProtocolError(
                    f"frame read deadline exceeded after {len(buf)}/{n} bytes"
                )
            poll_timeout = min(_IO_POLL_S, remaining)
        else:
            poll_timeout = _IO_POLL_S
        try:
            readable, _writable, exceptional = select.select([sock], [], [sock], poll_timeout)
        except (OSError, ValueError) as exc:
            raise SerialLinkClosed("socket closed while reading") from exc
        if not readable and not exceptional:
            continue
        try:
            chunk = sock.recv(n - len(buf))
        except (BlockingIOError, InterruptedError):
            continue
        except OSError as exc:
            raise SerialLinkClosed("socket closed while reading") from exc
        if not chunk:
            if not buf and allow_clean_eof:
                raise SerialLinkClosed("peer closed socket")
            raise SerialLinkProtocolError("peer closed socket mid-frame")
        buf.extend(chunk)
    return bytes(buf)


def _read_frame(sock: socket.socket) -> bytes:
    """Read one frame with one absolute deadline after its first byte."""
    first = _read_exactly(sock, 1, allow_clean_eof=True)
    deadline = time.monotonic() + _FRAME_READ_TIMEOUT_SECONDS
    header = first + _read_exactly(sock, 3, deadline=deadline)
    (size,) = struct.unpack(">I", header)
    if size == 0:
        raise SerialLinkProtocolError("zero-length frame")
    if size > _MAX_FRAME_SIZE:
        raise SerialLinkProtocolError(f"frame too large: {size}")
    return _read_exactly(sock, size, deadline=deadline)


def _read_lp_str(data: memoryview, offset: int) -> tuple[str, int]:
    if offset >= len(data):
        raise SerialLinkProtocolError("length-prefixed string is missing")
    length = data[offset]
    offset += 1
    end = offset + length
    if end > len(data):
        raise SerialLinkProtocolError("length-prefixed string is truncated")
    try:
        s = bytes(data[offset:end]).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SerialLinkProtocolError("length-prefixed string is not UTF-8") from exc
    return s, offset + length


def _read_lp_bytes(data: memoryview, offset: int) -> tuple[bytes, int]:
    if offset + 2 > len(data):
        raise SerialLinkProtocolError("length-prefixed bytes are missing")
    (length,) = struct.unpack(">H", data[offset : offset + 2])
    offset += 2
    end = offset + length
    if end > len(data):
        raise SerialLinkProtocolError("length-prefixed bytes are truncated")
    b = bytes(data[offset:end])
    return b, offset + length


def _connect_socket(
    host: str,
    port: int,
    timeout_s: float,
    cancel_event: threading.Event | None = None,
) -> socket.socket:
    """Connect without an uninterruptible blocking ``create_connection``."""
    if cancel_event is not None and cancel_event.is_set():
        raise SerialLinkClosed("connection cancelled")
    normalized_host = validate_loopback_host(host)
    timeout_s = _validate_timeout_seconds(timeout_s, "timeout_s")
    port = _validate_port(port)
    end_time = time.monotonic() + timeout_s
    last_error: OSError | None = None
    try:
        addresses = socket.getaddrinfo(normalized_host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SerialLinkError(f"unable to resolve {normalized_host!r}: {exc}") from exc
    if not addresses or any(
        not _is_loopback_sockaddr(address[4] if len(address) > 4 else None) for address in addresses
    ):
        raise SerialLinkError(
            "TcpSerialLink is localhost-only; resolver returned an unsafe address"
        )
    for family, socktype, proto, _canonname, sockaddr in addresses:
        if cancel_event is not None and cancel_event.is_set():
            raise SerialLinkClosed("connection cancelled")
        sock = socket.socket(family, socktype, proto)
        connected = False
        try:
            sock.setblocking(False)
            result = sock.connect_ex(sockaddr)
            if result == 0:
                sock.setblocking(True)
                connected = True
                return sock
            if result not in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
                last_error = OSError(result, errno.errorcode.get(result, "connect failed"))
                continue
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise SerialLinkClosed("connection cancelled")
                remaining = end_time - time.monotonic()
                if remaining <= 0:
                    last_error = TimeoutError(f"connection to {normalized_host}:{port} timed out")
                    break
                _readable, writable, exceptional = select.select(
                    [], [sock], [sock], min(0.05, remaining)
                )
                if not writable and not exceptional:
                    continue
                error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if error == 0:
                    sock.setblocking(True)
                    connected = True
                    return sock
                last_error = OSError(error, errno.errorcode.get(error, "connect failed"))
                break
        except (SerialLinkClosed, KeyboardInterrupt):
            raise
        except OSError as exc:
            last_error = exc
        finally:
            if sock.fileno() != -1 and not connected:
                try:
                    sock.close()
                except OSError:
                    pass
    if last_error is not None:
        raise last_error
    raise SerialLinkError(f"unable to connect to {normalized_host}:{port}")


# --- TCP implementation ----------------------------------------------------


@dataclass
class _Hello:
    rom_version: str


class TcpSerialLink:
    """A :class:`SerialLink` over a single TCP connection.

    Construct via the :meth:`connect` (client) or :meth:`listen` (server)
    classmethods — the raw ``__init__`` takes an already-connected
    socket and is primarily used by the two factory methods and by
    tests that want to pre-create a pair of in-memory socket ends.
    """

    def __init__(self, sock: socket.socket, local_rom_version: str) -> None:
        _validate_connected_socket_loopback(sock)
        self._sock = sock
        self._local_rom_version = validate_rom_version(local_rom_version)
        self._peer_rom_version: str | None = None
        self._closed = False
        self._closed_event = threading.Event()
        self._close_lock = threading.Lock()
        self._state_lock = threading.Lock()

        self._write_lock = threading.Lock()
        # Per-kind inbound queue; created lazily when first message of that
        # kind arrives or is awaited.  The queues and their aggregate byte
        # counts are bounded to keep a peer from turning EXCHANGE into an
        # unbounded memory sink.
        self._inbound: dict[str, queue.Queue[bytes]] = {}
        self._inbound_lock = threading.Lock()
        self._inbound_frame_count = 0
        self._inbound_byte_count = 0
        self._exchange_locks: dict[str, threading.Lock] = {}
        self._exchange_locks_lock = threading.Lock()
        self._reader_exc: Exception | None = None
        self._hello_received = threading.Event()

        try:
            self._sock.setblocking(False)
        except OSError:
            try:
                self._sock.close()
            except OSError:
                pass
            raise

        self._reader = threading.Thread(
            target=self._reader_loop, name="TcpSerialLink.reader", daemon=True
        )
        self._reader.start()

        # Send our HELLO. The reader thread on the other side will
        # capture the peer's HELLO into ``_peer_rom_version``.
        try:
            self._send_frame(bytes([OP_HELLO]) + _pack_lp_str(self._local_rom_version))
        except BaseException:
            self._mark_closed()
            self._join_reader()
            raise

    # --- construction --------------------------------------------------

    @classmethod
    def connect(
        cls,
        host: str,
        port: int,
        local_rom_version: str,
        *,
        timeout_s: float = 10.0,
        cancel_event: threading.Event | None = None,
    ) -> TcpSerialLink:
        """Open an outbound connection to a peer that is
        :meth:`listen`-ing on ``(host, port)``."""
        sock = _connect_socket(host, port, timeout_s, cancel_event)
        try:
            sock.settimeout(None)  # blocking reads in reader thread
            return cls(sock, local_rom_version)
        except BaseException:
            try:
                sock.close()
            except OSError:
                pass
            raise

    @classmethod
    def listen(
        cls,
        port: int,
        local_rom_version: str,
        *,
        host: str = "127.0.0.1",
        accept_timeout_s: float = _DEFAULT_ACCEPT_TIMEOUT_SECONDS,
        cancel_event: threading.Event | None = None,
        ready_event: threading.Event | None = None,
    ) -> TcpSerialLink:
        """Bind ``(host, port)`` and wait for one peer connection.

        The accept deadline is always finite. Lifecycle owners can provide
        ``accept_timeout_s`` and/or ``cancel_event`` to use a shorter or
        cancellable wait. ``ready_event`` is set after the listener is bound and accepting,
        which lets a connector start without a guessed sleep.
        """
        host = validate_loopback_host(host)
        accept_timeout_s = _validate_timeout_seconds(accept_timeout_s, "accept_timeout_s")
        port = _validate_port(port, allow_zero=True)
        listener = socket.socket(
            socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM
        )
        conn: socket.socket | None = None
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            bind_address = (host, port, 0, 0) if ":" in host else (host, port)
            listener.bind(bind_address)
            listener.listen(1)
            if ready_event is not None:
                ready_event.set()

            deadline = time.monotonic() + accept_timeout_s
            while conn is None:
                if cancel_event is not None and cancel_event.is_set():
                    raise SerialLinkClosed("listener accept cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SerialLinkTimeout(
                        f"listener accept timed out after {accept_timeout_s:g}s"
                    )
                listener.settimeout(min(0.25, remaining))
                try:
                    conn, _peer_addr = listener.accept()
                except TimeoutError:
                    continue
        finally:
            listener.close()
        if conn is None:
            raise SerialLinkClosed("listener closed before accepting a peer")
        try:
            conn.settimeout(None)
            return cls(conn, local_rom_version)
        except BaseException:
            try:
                conn.close()
            except OSError:
                pass
            raise

    # --- SerialLink surface -------------------------------------------

    @property
    def connected(self) -> bool:
        return not self._is_closed() and self._reader.is_alive()

    @property
    def peer_rom_version(self) -> str:
        return self._wait_for_hello(_HELLO_TIMEOUT_SECONDS)

    def exchange(
        self,
        kind: str,
        my_bytes: bytes,
        *,
        timeout_ms: int = 5000,
        cancel_event: threading.Event | None = None,
    ) -> bytes:
        timeout_ms = _validate_timeout_ms(timeout_ms)
        kind = _validate_exchange_kind(kind)
        payload = _coerce_exchange_payload(my_bytes)
        cancel_event = _validate_cancel_event(cancel_event)
        if self._is_closed():
            raise SerialLinkClosed("link is closed")
        self._raise_if_reader_failed()

        deadline = time.monotonic() + timeout_ms / 1000.0
        exchange_lock = self._get_exchange_lock(kind)
        _acquire_exchange_slot(
            exchange_lock,
            kind=kind,
            deadline=deadline,
            timeout_ms=timeout_ms,
            cancel_event=cancel_event,
        )
        try:
            _raise_if_cancelled(cancel_event)
            self._wait_for_hello(max(0.0, deadline - time.monotonic()), cancel_event=cancel_event)
            _raise_if_cancelled(cancel_event)
            frame = bytes([OP_EXCHANGE]) + _pack_lp_str(kind) + _pack_lp_bytes(payload)
            with self._inbound_lock:
                q = self._get_inbound_queue_locked(kind)
            self._send_frame(frame, deadline=deadline, cancel_event=cancel_event)

            while True:
                if self._closed_event.is_set():
                    self._raise_if_reader_failed()
                    raise SerialLinkClosed("link is closed")
                if cancel_event is not None and cancel_event.is_set():
                    error = SerialLinkClosed("exchange cancelled")
                    # A response that arrives after cancellation has no
                    # request id to identify it. Make the v1 channel
                    # terminal rather than allowing response poisoning.
                    self._mark_closed(error)
                    self._join_reader()
                    raise error
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._raise_if_reader_failed()
                    error = SerialLinkTimeout(
                        f"no peer EXCHANGE for kind={kind!r} within {timeout_ms}ms"
                    )
                    # EXCHANGE has no request id. Leaving the channel reusable
                    # after a timeout would let a delayed response satisfy a
                    # later call of the same kind, so timeout is terminal and a
                    # fresh connection is required.
                    self._mark_closed(error)
                    self._join_reader()
                    raise error
                with self._inbound_lock:
                    try:
                        payload = q.get_nowait()
                    except queue.Empty:
                        payload = None
                    else:
                        self._inbound_frame_count -= 1
                        self._inbound_byte_count -= len(payload)
                        return payload
                if cancel_event is None:
                    self._closed_event.wait(timeout=min(_IO_POLL_S, remaining))
                else:
                    cancel_event.wait(timeout=min(_IO_POLL_S, remaining))
        finally:
            exchange_lock.release()

    def close(self) -> None:
        with self._close_lock:
            if not self._is_closed():
                # Send BYE while the link is still open. This is best-effort
                # and ordered with the terminal state transition. Holding the
                # write lock through ``_mark_closed`` prevents a concurrent
                # exchange from writing after BYE but before closure.
                with self._write_lock:
                    try:
                        self._send_frame_locked(bytes([OP_BYE]))
                    except (OSError, SerialLinkError):
                        pass
                    finally:
                        self._mark_closed()
        self._join_reader()

    # --- reader thread --------------------------------------------------

    def _reader_loop(self) -> None:
        try:
            while not self._is_closed():
                body = _read_frame(self._sock)
                self._dispatch(memoryview(body))
        except SerialLinkClosed:
            # Clean peer close; no error, just mark closed.
            self._mark_closed()
        except Exception as exc:  # noqa: BLE001
            self._mark_closed(exc)
        finally:
            self._hello_received.set()  # unblock peer_rom_version waiters

    def _dispatch(self, body: memoryview) -> None:
        if not body:
            raise SerialLinkProtocolError("empty frame body")
        opcode = body[0]
        if opcode != OP_HELLO:
            with self._state_lock:
                hello_received = self._peer_rom_version is not None
            if not hello_received:
                raise SerialLinkProtocolError("HELLO must be the first frame received")
        if opcode == OP_HELLO:
            rom_version, offset = _read_lp_str(body, 1)
            if offset != len(body):
                raise SerialLinkProtocolError("HELLO has trailing bytes")
            try:
                rom_version = validate_rom_version(rom_version)
            except (TypeError, ValueError) as exc:
                raise SerialLinkProtocolError(f"invalid HELLO rom_version: {exc}") from exc
            with self._state_lock:
                if self._peer_rom_version is not None:
                    raise SerialLinkProtocolError("duplicate HELLO")
                self._peer_rom_version = rom_version
            self._hello_received.set()
        elif opcode == OP_EXCHANGE:
            kind, offset = _read_lp_str(body, 1)
            try:
                _validate_exchange_kind(kind)
            except (TypeError, ValueError) as exc:
                raise SerialLinkProtocolError(f"invalid EXCHANGE kind: {exc}") from exc
            payload, offset = _read_lp_bytes(body, offset)
            if offset != len(body):
                raise SerialLinkProtocolError("EXCHANGE has trailing bytes")
            with self._inbound_lock:
                if self._closed_event.is_set():
                    raise SerialLinkClosed("link is closed")
                q = self._get_inbound_queue_locked(kind)
                if self._inbound_frame_count >= _MAX_INBOUND_FRAMES:
                    raise SerialLinkProtocolError("inbound EXCHANGE frame limit exceeded")
                if self._inbound_byte_count + len(payload) > _MAX_INBOUND_BYTES:
                    raise SerialLinkProtocolError("inbound EXCHANGE byte limit exceeded")
                try:
                    q.put_nowait(payload)
                except queue.Full as exc:
                    raise SerialLinkProtocolError(
                        f"inbound EXCHANGE queue is full for kind={kind!r}"
                    ) from exc
                self._inbound_frame_count += 1
                self._inbound_byte_count += len(payload)
        elif opcode == OP_BYE:
            if len(body) != 1:
                raise SerialLinkProtocolError("BYE has a payload")
            self._mark_closed()
        else:
            raise SerialLinkProtocolError(f"unknown opcode 0x{opcode:02x}")

    # --- helpers --------------------------------------------------------

    def _is_closed(self) -> bool:
        with self._state_lock:
            return self._closed

    def _get_exchange_lock(self, kind: str) -> threading.Lock:
        with self._exchange_locks_lock:
            lock = self._exchange_locks.get(kind)
            if lock is not None:
                return lock
            if len(self._exchange_locks) >= _MAX_INBOUND_KINDS:
                raise SerialLinkProtocolError("exchange kind limit exceeded")
            lock = threading.Lock()
            self._exchange_locks[kind] = lock
            return lock

    def _get_inbound_queue_locked(self, kind: str) -> queue.Queue[bytes]:
        q = self._inbound.get(kind)
        if q is not None:
            return q
        if len(self._inbound) >= _MAX_INBOUND_KINDS:
            raise SerialLinkProtocolError("inbound EXCHANGE kind limit exceeded")
        q = queue.Queue(maxsize=_MAX_INBOUND_FRAMES_PER_KIND)
        self._inbound[kind] = q
        return q

    def _mark_closed(self, error: Exception | None = None) -> None:
        with self._state_lock:
            if self._closed:
                if error is not None and self._reader_exc is None:
                    self._reader_exc = error
                return
            if error is not None:
                self._reader_exc = error
            self._closed = True
            self._closed_event.set()
            self._hello_received.set()
        with self._inbound_lock:
            for q in self._inbound.values():
                while True:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break
            self._inbound_frame_count = 0
            self._inbound_byte_count = 0
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def _send_frame(
        self,
        payload: bytes,
        *,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        with self._write_lock:
            self._send_frame_locked(payload, deadline=deadline, cancel_event=cancel_event)

    def _send_frame_locked(
        self,
        payload: bytes,
        *,
        deadline: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        payload = bytes(payload)
        if not payload:
            raise ValueError("frame payload must not be empty")
        if len(payload) > _MAX_FRAME_SIZE:
            raise ValueError(f"frame too large: {len(payload)}")
        header = struct.pack(">I", len(payload))
        frame = header + payload
        write_deadline = time.monotonic() + _WRITE_TIMEOUT_S
        if deadline is not None:
            write_deadline = min(write_deadline, deadline)
        if self._is_closed():
            raise SerialLinkClosed("link is closed")
        _raise_if_cancelled(cancel_event)
        offset = 0
        while offset < len(frame):
            if self._is_closed():
                raise SerialLinkClosed("link is closed")
            if cancel_event is not None and cancel_event.is_set():
                self._mark_closed()
                raise SerialLinkClosed("exchange cancelled")
            try:
                sent = self._sock.send(frame[offset:])
            except (BlockingIOError, InterruptedError):
                remaining = write_deadline - time.monotonic()
                if remaining <= 0:
                    self._mark_closed()
                    raise SerialLinkTimeout("socket write timed out")
                try:
                    _readable, writable, exceptional = select.select(
                        [],
                        [self._sock],
                        [self._sock],
                        min(_IO_POLL_S, remaining),
                    )
                except (OSError, ValueError) as exc:
                    if self._is_closed():
                        raise SerialLinkClosed("link is closed") from exc
                    self._mark_closed()
                    raise SerialLinkClosed("socket closed while writing") from exc
                if not writable and not exceptional:
                    continue
                continue
            except OSError as exc:
                if not self._is_closed():
                    self._mark_closed()
                raise SerialLinkClosed("socket write failed") from exc
            if sent <= 0:
                self._mark_closed()
                raise SerialLinkClosed("socket write returned no progress")
            offset += sent

    def _wait_for_hello(
        self,
        timeout_s: float,
        *,
        cancel_event: threading.Event | None = None,
    ) -> str:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while not self._hello_received.is_set():
            _raise_if_cancelled(cancel_event)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._raise_if_reader_failed()
                error = SerialLinkTimeout("peer did not send HELLO before the deadline")
                # HELLO has no request id or retry epoch. A timed-out handshake
                # cannot safely be resumed on this socket.
                self._mark_closed(error)
                self._join_reader()
                raise error
            if cancel_event is None:
                self._hello_received.wait(timeout=min(_IO_POLL_S, remaining))
            else:
                cancel_event.wait(timeout=min(_IO_POLL_S, remaining))
        self._raise_if_reader_failed()
        if self._is_closed():
            raise SerialLinkClosed("link is closed before HELLO completed")
        with self._state_lock:
            peer_rom_version = self._peer_rom_version
        if peer_rom_version is None:
            raise SerialLinkProtocolError("peer HELLO arrived without rom_version")
        return peer_rom_version

    def _join_reader(self) -> None:
        if self._reader is threading.current_thread():
            return
        self._reader.join(timeout=_READER_JOIN_TIMEOUT_S)

    def _raise_if_reader_failed(self) -> None:
        with self._state_lock:
            reader_exc = self._reader_exc
        if reader_exc is not None:
            raise SerialLinkError(
                f"reader thread failed: {type(reader_exc).__name__}: {reader_exc}"
            ) from reader_exc


# --- in-process loopback (for unit tests) ---------------------------------


class _InProcessState:
    """Shared close notification for the two ends of an in-process cable."""

    def __init__(self) -> None:
        self.closed = threading.Event()


class InProcessSerialLink:
    """A :class:`SerialLink` for single-process tests.

    Pair of ``InProcessSerialLink`` instances exchange bytes through
    shared queues without any socket I/O. :meth:`pair` creates the
    pair and wires them together. Useful when testing the bridge
    semantics without the socket/threading complications of
    :class:`TcpSerialLink`.
    """

    def __init__(
        self,
        local_rom_version: str,
        peer_rom_version: str,
        outgoing: dict[str, queue.Queue[bytes]],
        incoming: dict[str, queue.Queue[bytes]],
        locks: tuple[threading.Lock, threading.Lock],
        state: _InProcessState | None = None,
    ) -> None:
        self._local_rom = validate_rom_version(local_rom_version)
        self._peer_rom = validate_rom_version(peer_rom_version)
        self._out = outgoing
        self._in = incoming
        self._out_lock, self._in_lock = locks
        self._state = state if state is not None else _InProcessState()
        self._closed = False
        self._exchange_locks: dict[str, threading.Lock] = {}
        self._exchange_locks_lock = threading.Lock()

    @classmethod
    def pair(
        cls, a_rom_version: str, b_rom_version: str
    ) -> tuple[InProcessSerialLink, InProcessSerialLink]:
        def _new_queue() -> queue.Queue[bytes]:
            return queue.Queue(maxsize=_MAX_INBOUND_FRAMES_PER_KIND)

        a_to_b: dict[str, queue.Queue[bytes]] = defaultdict(_new_queue)
        b_to_a: dict[str, queue.Queue[bytes]] = defaultdict(_new_queue)
        a_lock = threading.Lock()
        b_lock = threading.Lock()
        state = _InProcessState()
        a = cls(
            a_rom_version,
            b_rom_version,
            a_to_b,
            b_to_a,
            (a_lock, b_lock),
            state,
        )
        b = cls(
            b_rom_version,
            a_rom_version,
            b_to_a,
            a_to_b,
            (b_lock, a_lock),
            state,
        )
        return a, b

    @property
    def connected(self) -> bool:
        return not self._state.closed.is_set()

    @property
    def peer_rom_version(self) -> str:
        return self._peer_rom

    def exchange(
        self,
        kind: str,
        my_bytes: bytes,
        *,
        timeout_ms: int = 5000,
        cancel_event: threading.Event | None = None,
    ) -> bytes:
        timeout_ms = _validate_timeout_ms(timeout_ms)
        kind = _validate_exchange_kind(kind)
        payload = _coerce_exchange_payload(my_bytes)
        cancel_event = _validate_cancel_event(cancel_event)
        if self._state.closed.is_set():
            raise SerialLinkClosed("link is closed")
        deadline = time.monotonic() + timeout_ms / 1000.0
        exchange_lock = self._get_exchange_lock(kind)
        _acquire_exchange_slot(
            exchange_lock,
            kind=kind,
            deadline=deadline,
            timeout_ms=timeout_ms,
            cancel_event=cancel_event,
        )
        request_sent = False
        try:
            _raise_if_cancelled(cancel_event)
            with self._out_lock:
                if self._state.closed.is_set():
                    raise SerialLinkClosed("link is closed")
                try:
                    self._out[kind].put_nowait(payload)
                except queue.Full as exc:
                    self._state.closed.set()
                    raise SerialLinkProtocolError(
                        f"inbound EXCHANGE queue is full for kind={kind!r}"
                    ) from exc
            request_sent = True
            with self._in_lock:
                q = self._in[kind]
            while True:
                if self._state.closed.is_set():
                    raise SerialLinkClosed("link is closed")
                if cancel_event is not None and cancel_event.is_set():
                    self.close()
                    raise SerialLinkClosed("exchange cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SerialLinkTimeout(
                        f"no peer EXCHANGE for kind={kind!r} within {timeout_ms}ms"
                    )
                try:
                    return q.get_nowait()
                except queue.Empty:
                    if cancel_event is None:
                        self._state.closed.wait(timeout=min(_IO_POLL_S, remaining))
                    else:
                        cancel_event.wait(timeout=min(_IO_POLL_S, remaining))
        except SerialLinkTimeout:
            # EXCHANGE has no request identifier. A delayed response after a
            # timeout cannot safely be matched to a later call of this kind,
            # so the shared in-process stream becomes terminal too.
            if request_sent:
                self.close()
            raise
        finally:
            exchange_lock.release()

    def _get_exchange_lock(self, kind: str) -> threading.Lock:
        with self._exchange_locks_lock:
            lock = self._exchange_locks.get(kind)
            if lock is not None:
                return lock
            if len(self._exchange_locks) >= _MAX_INBOUND_KINDS:
                raise SerialLinkProtocolError("exchange kind limit exceeded")
            lock = threading.Lock()
            self._exchange_locks[kind] = lock
            return lock

    def close(self) -> None:
        self._closed = True
        self._state.closed.set()


__all__ = [
    "OP_BYE",
    "OP_EXCHANGE",
    "OP_HELLO",
    "SUPPORTED_ROM_VERSIONS",
    "InProcessSerialLink",
    "SerialLink",
    "SerialLinkClosed",
    "SerialLinkError",
    "SerialLinkProtocolError",
    "SerialLinkTimeout",
    "TcpSerialLink",
    "validate_rom_version",
]
