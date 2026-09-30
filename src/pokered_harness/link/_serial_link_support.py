"""Internal building blocks for the peer-to-peer serial link (#149).

Split from ``pokered_harness.link.serial_link`` with no behavior change.
These are the wire framing helpers, protocol constants, exceptions and
argument validators the concrete links share. ``serial_link`` re-exports
every name here so its public surface is unchanged; this module is private
to the package and is never imported by external callers.
"""

from __future__ import annotations

import errno
import math
import select
import socket
import struct
import threading
import time
from dataclasses import dataclass

from pokered_harness.link.network_backend import (
    _is_loopback_sockaddr,
    validate_loopback_host,
)

# A peer can announce a maximum one-megabyte frame, but EXCHANGE payloads are
# limited to the protocol's two-byte length prefix.  Keep the number of
# queued frames and the number of queued payload bytes bounded independently
# so a peer cannot exhaust memory by sending valid frames that nobody awaits.
_MAX_FRAME_SIZE = 1 << 20
_MAX_EXCHANGE_PAYLOAD_SIZE = 0xFFFF
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
    encoded = bytes(payload)
    if len(encoded) > _MAX_EXCHANGE_PAYLOAD_SIZE:
        raise ValueError(
            f"exchange payload is too long for the wire protocol: {len(encoded)} bytes"
        )
    return encoded


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
    if len(encoded) > _MAX_EXCHANGE_PAYLOAD_SIZE:
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


# --- HELLO handshake -------------------------------------------------------


@dataclass
class _Hello:
    rom_version: str
