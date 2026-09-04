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
    translation.

``0x10 EXCHANGE``
    Body: ``kind`` (length-prefixed ASCII) + ``data`` (length-prefixed
    bytes). The two peers are expected to both send an EXCHANGE with
    matching ``kind`` values; each side's :meth:`SerialLink.exchange`
    call returns the peer's data bytes.

``0xFE BYE``
    Peer gracefully disconnects.

Matching is FIFO within a given ``kind`` — important because the game
may issue ``Serial_ExchangeBytes`` multiple times during one trade and
we can't rely on strict one-at-a-time pairing if either peer runs a
few frames ahead.

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
import ipaddress
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


# --- opcodes ---------------------------------------------------------------

OP_HELLO: int = 0x01
OP_EXCHANGE: int = 0x10
OP_BYE: int = 0xFE


# The largest frame accepted by the original wire protocol. EXCHANGE's
# two-byte payload length keeps valid frames below this ceiling, but retaining
# the existing frame limit makes malformed inputs fail closed before parsing.
_MAX_FRAME_SIZE = 1 << 20
_MAX_INBOUND_KINDS = 256
_MAX_INBOUND_FRAMES_PER_KIND = 32
_MAX_INBOUND_FRAMES = 256
_MAX_INBOUND_BYTES = 4 * 1024 * 1024

_DEFAULT_ACCEPT_TIMEOUT_SECONDS = 30.0
_HELLO_TIMEOUT_SECONDS = 5.0

# A connection may remain idle indefinitely. Once a frame starts, however,
# all of its bytes must arrive by one absolute monotonic deadline. The short
# poll also lets close() interrupt the reader without changing the wire
# protocol.
_FRAME_READ_TIMEOUT_SECONDS = 5.0
_IO_POLL_S = 0.05
_WRITE_TIMEOUT_S = 1.0
_READER_JOIN_TIMEOUT_S = 1.0

SUPPORTED_ROM_VERSIONS = ("red", "blue", "yellow")
_SUPPORTED_ROM_VERSIONS = frozenset(SUPPORTED_ROM_VERSIONS)


# --- exceptions ------------------------------------------------------------


class SerialLinkError(RuntimeError):
    """Base class for all serial-link errors."""


class SerialLinkClosed(SerialLinkError):
    """Peer disconnected or the link was closed before the operation completed."""


class SerialLinkTimeout(SerialLinkError):
    """:meth:`SerialLink.exchange` blocked past the configured deadline."""


class SerialLinkProtocolError(SerialLinkError):
    """Received a malformed frame or an unexpected opcode."""


def validate_rom_version(version: str) -> str:
    """Normalize and validate the ROM identity used by the HELLO frame."""
    if not isinstance(version, str):
        raise TypeError(f"ROM version must be a string, got {type(version).__name__}")
    normalized = version.strip().lower()
    if normalized not in _SUPPORTED_ROM_VERSIONS:
        raise ValueError(
            f"unsupported ROM version {version!r}; expected one of {list(SUPPORTED_ROM_VERSIONS)}"
        )
    return normalized


def _validate_exchange_kind(kind: str) -> str:
    """Validate the ASCII string represented by the one-byte wire length."""
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


def _coerce_exchange_payload(
    payload: bytes | bytearray | memoryview,
) -> bytes:
    """Convert supported bytes-like payloads without widening wire limits."""
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


def _validate_port(port: int, *, allow_zero: bool = False) -> int:
    if isinstance(port, bool) or not isinstance(port, int):
        raise TypeError("port must be an integer")
    lower = 0 if allow_zero else 1
    if not lower <= port <= 0xFFFF:
        if allow_zero:
            raise ValueError("port must be in the range 0..65535")
        raise ValueError("port must be in the range 1..65535")
    return port


def _is_loopback_sockaddr(sockaddr: object) -> bool:
    if not isinstance(sockaddr, tuple) or not sockaddr:
        return False
    host = sockaddr[0]
    if not isinstance(host, str):
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_loopback_host(host: str) -> str:
    """Reject destinations outside the transport's existing loopback scope."""
    if not isinstance(host, str):
        raise TypeError("host must be a string")
    normalized = host.strip()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    if normalized.lower() == "localhost":
        try:
            addresses = socket.getaddrinfo(normalized, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError("TcpSerialLink is localhost-only; localhost did not resolve") from exc
        if not addresses or any(
            not _is_loopback_sockaddr(address[4] if len(address) > 4 else None)
            for address in addresses
        ):
            raise ValueError("TcpSerialLink is localhost-only; localhost resolved unsafely")
        return "localhost"
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError as exc:
        raise ValueError(
            "TcpSerialLink is localhost-only; use 127.0.0.1, localhost, or ::1"
        ) from exc
    if not address.is_loopback:
        raise ValueError("TcpSerialLink is localhost-only; use 127.0.0.1, localhost, or ::1")
    return normalized


def _validate_connected_socket_loopback(sock: socket.socket) -> None:
    """Prevent raw TCP construction from bypassing the loopback boundary."""
    family = getattr(sock, "family", None)
    unix_family = getattr(socket, "AF_UNIX", None)
    if unix_family is not None and family == unix_family:
        return
    if family not in (socket.AF_INET, socket.AF_INET6):
        raise ValueError("TcpSerialLink requires a connected loopback TCP socket")
    try:
        peer = sock.getpeername()
    except OSError as exc:
        raise ValueError("TcpSerialLink requires a connected loopback TCP socket") from exc
    if not isinstance(peer, tuple) or not peer or not isinstance(peer[0], str):
        raise ValueError("TcpSerialLink requires a connected loopback TCP socket")
    try:
        _validate_loopback_host(peer[0])
    except (TypeError, ValueError) as exc:
        raise ValueError("TcpSerialLink is localhost-only; raw TCP peer is not loopback") from exc


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

    def exchange(self, kind: str, my_bytes: bytes, *, timeout_ms: int = 5000) -> bytes:
        """Send ``my_bytes`` tagged with ``kind`` and block until peer
        sends a matching EXCHANGE for the same ``kind``. Returns peer's
        bytes.

        FIFO within kind: two rapid back-to-back exchanges of the same
        kind pair up in order.
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
    encoded = _coerce_exchange_payload(b)
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
    """Read exactly ``n`` bytes with an optional absolute deadline.

    The initial byte of a new frame is read without a deadline so an idle
    connection remains valid. The caller then supplies one deadline for the
    rest of that frame; it is never reset for each partial ``recv``.
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
        except (BlockingIOError, InterruptedError, socket.timeout):
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
    """Read one frame, bounding a started frame by one absolute deadline."""
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
    if offset < 0 or offset >= len(data):
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
    return s, end


def _read_lp_bytes(data: memoryview, offset: int) -> tuple[bytes, int]:
    if offset < 0 or offset + 2 > len(data):
        raise SerialLinkProtocolError("length-prefixed bytes are missing")
    (length,) = struct.unpack(">H", data[offset : offset + 2])
    offset += 2
    end = offset + length
    if end > len(data):
        raise SerialLinkProtocolError("length-prefixed bytes are truncated")
    return bytes(data[offset:end]), end


def _connect_socket(
    host: str,
    port: int,
    timeout_s: float,
    cancel_event: threading.Event | None = None,
) -> socket.socket:
    """Connect with one absolute deadline and short cancellation polls."""
    if cancel_event is not None and cancel_event.is_set():
        raise SerialLinkClosed("connection cancelled")
    normalized_host = _validate_loopback_host(host)
    timeout_s = _validate_timeout_seconds(timeout_s, "timeout_s")
    port = _validate_port(port)
    deadline = time.monotonic() + timeout_s
    try:
        addresses = socket.getaddrinfo(normalized_host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SerialLinkError(f"unable to resolve {normalized_host!r}: {exc}") from exc
    if not addresses or any(
        not _is_loopback_sockaddr(address[4] if len(address) > 4 else None) for address in addresses
    ):
        raise ValueError("TcpSerialLink is localhost-only; resolver returned an unsafe address")

    last_error: OSError | None = None
    for family, socktype, proto, _canonname, sockaddr in addresses:
        if cancel_event is not None and cancel_event.is_set():
            raise SerialLinkClosed("connection cancelled")
        sock = socket.socket(family, socktype, proto)
        connected = False
        try:
            sock.setblocking(False)
            result = sock.connect_ex(sockaddr)
            if result == 0:
                connected = True
                return sock
            if result not in (
                errno.EINPROGRESS,
                errno.EWOULDBLOCK,
                errno.EALREADY,
                errno.EINTR,
            ):
                last_error = OSError(result, errno.errorcode.get(result, "connect failed"))
                continue
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise SerialLinkClosed("connection cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    last_error = TimeoutError(f"connection to {normalized_host}:{port} timed out")
                    break
                try:
                    _readable, writable, exceptional = select.select(
                        [], [sock], [sock], min(_IO_POLL_S, remaining)
                    )
                except (OSError, ValueError) as exc:
                    last_error = exc
                    break
                if not writable and not exceptional:
                    continue
                error = sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if error == 0:
                    connected = True
                    return sock
                last_error = OSError(error, errno.errorcode.get(error, "connect failed"))
                break
        except (SerialLinkClosed, KeyboardInterrupt):
            raise
        except OSError as exc:
            last_error = exc
        finally:
            if not connected:
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
        local_rom_version = validate_rom_version(local_rom_version)
        self._sock = sock
        self._local_rom_version = local_rom_version
        self._peer_rom_version: str | None = None
        self._closed = False
        self._closed_event = threading.Event()
        self._close_lock = threading.Lock()
        self._state_lock = threading.Lock()

        self._write_lock = threading.Lock()
        # Per-kind queues are bounded so valid but unsolicited EXCHANGE
        # frames cannot become an unbounded memory sink.
        self._inbound: dict[str, queue.Queue[bytes]] = {}
        self._inbound_lock = threading.Lock()
        self._inbound_frame_count = 0
        self._inbound_byte_count = 0
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

        # Send our HELLO exactly once. The reader thread on the other side
        # captures the peer's HELLO into ``_peer_rom_version``.
        try:
            self._send_frame(bytes([OP_HELLO]) + _pack_lp_str(local_rom_version))
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
    ) -> "TcpSerialLink":
        """Open an outbound connection to a peer that is
        :meth:`listen`-ing on ``(host, port)``."""
        sock = _connect_socket(host, port, timeout_s, cancel_event)
        try:
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
    ) -> "TcpSerialLink":
        """Bind and wait for one loopback peer with a finite deadline."""
        host = _validate_loopback_host(host)
        accept_timeout_s = _validate_timeout_seconds(accept_timeout_s, "accept_timeout_s")
        port = _validate_port(port, allow_zero=True)
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        listener = socket.socket(family, socket.SOCK_STREAM)
        conn: socket.socket | None = None
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            bind_address = (host, port, 0, 0) if family == socket.AF_INET6 else (host, port)
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
                listener.settimeout(min(_IO_POLL_S, remaining))
                try:
                    conn, _peer_addr = listener.accept()
                except (socket.timeout, TimeoutError):
                    continue
        finally:
            listener.close()
        if conn is None:
            raise SerialLinkClosed("listener closed before accepting a peer")
        try:
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

    def exchange(self, kind: str, my_bytes: bytes, *, timeout_ms: int = 5000) -> bytes:
        timeout_ms = _validate_timeout_ms(timeout_ms)
        kind = _validate_exchange_kind(kind)
        payload = _coerce_exchange_payload(my_bytes)
        if self._is_closed():
            raise SerialLinkClosed("link is closed")
        self._raise_if_reader_failed()

        deadline = time.monotonic() + timeout_ms / 1000.0
        self._wait_for_hello(max(0.0, deadline - time.monotonic()))
        frame = bytes([OP_EXCHANGE]) + _pack_lp_str(kind) + _pack_lp_bytes(payload)

        with self._inbound_lock:
            q = self._get_inbound_queue_locked(kind)
        self._send_frame(frame, deadline=deadline)

        while True:
            if self._closed_event.is_set():
                self._raise_if_reader_failed()
                raise SerialLinkClosed("link is closed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._raise_if_reader_failed()
                error = SerialLinkTimeout(
                    f"no peer EXCHANGE for kind={kind!r} within {timeout_ms}ms"
                )
                # EXCHANGE has no request identifier. A delayed response
                # cannot safely be matched after a timeout, so this session
                # is terminal and must not be reused.
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
            self._closed_event.wait(timeout=min(_IO_POLL_S, remaining))

    def close(self) -> None:
        with self._close_lock:
            if not self._is_closed():
                # Hold the write lock through the state transition so a
                # concurrent exchange cannot write after the BYE frame.
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
        with self._state_lock:
            hello_received = self._peer_rom_version is not None
        if opcode != OP_HELLO and not hello_received:
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

    def _send_frame(self, payload: bytes, *, deadline: float | None = None) -> None:
        with self._write_lock:
            self._send_frame_locked(payload, deadline=deadline)

    def _send_frame_locked(self, payload: bytes, *, deadline: float | None = None) -> None:
        payload = bytes(payload)
        if not payload:
            raise ValueError("frame payload must not be empty")
        if len(payload) > _MAX_FRAME_SIZE:
            raise ValueError(f"frame too large: {len(payload)}")
        frame = struct.pack(">I", len(payload)) + payload
        write_deadline = time.monotonic() + _WRITE_TIMEOUT_S
        if deadline is not None:
            write_deadline = min(write_deadline, deadline)
        if self._is_closed():
            raise SerialLinkClosed("link is closed")
        offset = 0
        while offset < len(frame):
            if self._is_closed():
                raise SerialLinkClosed("link is closed")
            try:
                sent = self._sock.send(frame[offset:])
            except (BlockingIOError, InterruptedError, socket.timeout):
                remaining = write_deadline - time.monotonic()
                if remaining <= 0:
                    self._mark_closed()
                    raise SerialLinkTimeout("socket write timed out")
                try:
                    _readable, writable, exceptional = select.select(
                        [], [self._sock], [self._sock], min(_IO_POLL_S, remaining)
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

    def _wait_for_hello(self, timeout_s: float) -> str:
        if not self._hello_received.wait(timeout=max(0.0, timeout_s)):
            self._raise_if_reader_failed()
            error = SerialLinkTimeout("peer did not send HELLO before the deadline")
            self._mark_closed(error)
            self._join_reader()
            raise error
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
    ) -> None:
        self._local_rom = validate_rom_version(local_rom_version)
        self._peer_rom = validate_rom_version(peer_rom_version)
        self._out = outgoing
        self._in = incoming
        self._out_lock, self._in_lock = locks
        self._closed = False

    @classmethod
    def pair(
        cls, a_rom_version: str, b_rom_version: str
    ) -> tuple["InProcessSerialLink", "InProcessSerialLink"]:
        a_to_b: dict[str, queue.Queue[bytes]] = defaultdict(queue.Queue)
        b_to_a: dict[str, queue.Queue[bytes]] = defaultdict(queue.Queue)
        a_lock = threading.Lock()
        b_lock = threading.Lock()
        a = cls(a_rom_version, b_rom_version, a_to_b, b_to_a, (a_lock, b_lock))
        b = cls(b_rom_version, a_rom_version, b_to_a, a_to_b, (b_lock, a_lock))
        return a, b

    @property
    def connected(self) -> bool:
        return not self._closed

    @property
    def peer_rom_version(self) -> str:
        return self._peer_rom

    def exchange(self, kind: str, my_bytes: bytes, *, timeout_ms: int = 5000) -> bytes:
        timeout_ms = _validate_timeout_ms(timeout_ms)
        kind = _validate_exchange_kind(kind)
        payload = _coerce_exchange_payload(my_bytes)
        if self._closed:
            raise SerialLinkClosed("link is closed")
        with self._out_lock:
            self._out[kind].put(payload)
        with self._in_lock:
            q = self._in[kind]
        try:
            return q.get(timeout=timeout_ms / 1000.0)
        except queue.Empty as exc:
            raise SerialLinkTimeout(
                f"no peer EXCHANGE for kind={kind!r} within {timeout_ms}ms"
            ) from exc

    def close(self) -> None:
        self._closed = True


__all__ = [
    "InProcessSerialLink",
    "SerialLink",
    "SerialLinkClosed",
    "SerialLinkError",
    "SerialLinkProtocolError",
    "SerialLinkTimeout",
    "TcpSerialLink",
    "OP_BYE",
    "OP_EXCHANGE",
    "OP_HELLO",
    "SUPPORTED_ROM_VERSIONS",
    "validate_rom_version",
]
