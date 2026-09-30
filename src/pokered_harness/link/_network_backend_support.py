"""Private constants, wire types and socket helpers for :mod:`network_backend`.

Extracted verbatim from ``network_backend.py`` (#126): the protocol opcodes,
struct layouts and timeouts, the exception and inbound-edge queue types, the
argument validators, and the loopback socket helpers.  Calls to the
monkeypatch-patched facade globals resolve through ``_entry`` so attribute
patches on the loaded ``network_backend`` module stay visible.
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
from dataclasses import dataclass

import pokered_harness.link.network_backend as _entry

# Opcodes:
#   EDGE_REQ  = master → slave: "here's my outgoing bit"
#   EDGE_RESP = slave → master: "here's my outgoing bit, bit just applied"
#   SYNC      = "I've reached checkpoint <id>; waiting for peer's SYNC <id>"
#   FRAME_TICK = leader → follower: begin one emulation frame
#   FRAME_DONE = leader → follower: leader frame and its serial work ended
#   FRAME_ACK  = follower → leader: owner-side work for that frame is done
#
# Using distinct opcodes lets a reader thread on each side demux
# incoming frames without knowing the current role — peers can
# freely swap master/slave between transfers (which Pokemon's
# handshake does during nibble-sync). SYNC is a separate out-of-band
# rendezvous primitive that higher-level code (e.g. the subprocess
# trade driver) uses at phase boundaries.
_OP_EDGE_REQ: int = 0x10
_OP_EDGE_RESP: int = 0x11
_OP_EDGE_REQ_ID: int = 0x12
_OP_EDGE_RESP_ID: int = 0x13
_OP_SYNC: int = 0x20
_OP_EXCHANGE: int = 0x30
_OP_FRAME_TICK: int = 0x40
_OP_FRAME_ACK: int = 0x41
_OP_FRAME_DONE: int = 0x42
_OP_HELLO: int = 0x01

_PROTOCOL_VERSION: int = 2
_ROM_VERSION_CODES: dict[str, int] = {"red": 1, "blue": 2, "yellow": 3}
_ROM_VERSION_NAMES: dict[int, str] = {value: key for key, value in _ROM_VERSION_CODES.items()}

_FRAME = struct.Struct(">BB")  # opcode, payload (1-byte id for SYNC)
_EDGE_ID_FRAME = struct.Struct(">BBI")  # opcode, bit, non-zero request id
_LEN = struct.Struct(">H")
_EDGE_RESPONSE_TIMEOUT_SECONDS = 10.0
_DEFAULT_SEND_TIMEOUT_SECONDS = 10.0
_DEFAULT_ACCEPT_TIMEOUT_SECONDS = 30.0
_SEND_POLL_SECONDS = 0.05
_CONTROL_QUEUE_MAXSIZE = 256
_FRAME_READ_TIMEOUT_SECONDS = 10.0
_READ_POLL_SECONDS = 0.05
_REARM_WAIT_SECONDS = 0.100
_POST_BYTE_REARM_GRACE_SECONDS = 1.500
_ACTIVE_EXCHANGE_GRACE_SECONDS = 1.000
_ACTIVE_EXCHANGE_EDGE_THRESHOLD = 64
_ACTIVE_EXCHANGE_REARM_WAIT_SECONDS = 5.0
_MAX_SERIAL_TRANSCRIPT_ENTRIES = 4096
_EDGE_ID_MAX = 0xFFFFFFFF
_EDGE_RESPONSE_HISTORY_MAX = 256


class NetworkBackendError(RuntimeError):
    """Wraps socket errors + protocol errors from :class:`NetworkBackend`."""


@dataclass
class _InboundEdge:
    """An EDGE_REQ awaiting execution by the emulator owner."""

    peer_bit: int
    response_bit: int | None = None
    completed: bool = False
    deferred: bool = False
    error: BaseException | None = None
    # ``None`` denotes the historical two-byte frame. Keep this after the
    # original fields so positional construction retains its meaning.
    edge_id: int | None = None
    # Only byte responses held for owner progress need a transport receipt.
    # It is set after the response worker settles wire/pending accounting.
    response_finished: threading.Event | None = None


class _InboundEdgeQueue(queue.Queue):
    """Retry a deferred owner request before later wire requests.

    ``Queue`` calls ``_put`` while holding its mutex, including capacity
    checking and reader notification. The owner still dequeues before native
    application, so a reentrant service cannot apply the same edge twice.
    """

    def _put(self, item) -> None:
        if isinstance(item, _InboundEdge) and item.deferred:
            self.queue.appendleft(item)
        else:
            super()._put(item)


def _validate_id(value: int, name: str) -> int:
    """Validate a one-byte protocol identifier without bool coercion."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer in 0..255")
    if not 0 <= value <= 0xFF:
        raise ValueError(f"{name} must fit in uint8, got {value}")
    return value


def _validate_optional_timeout(value: float | None, name: str) -> float | None:
    """Validate an optional finite, non-negative timeout in seconds."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError(f"{name} must be a finite, non-negative number")
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite, non-negative number") from exc
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{name} must be a finite, non-negative number")
    return normalized


def _require_timeout(value: float | None, name: str) -> float:
    """Validate a required finite timeout without relying on ``assert``."""
    normalized = _validate_optional_timeout(value, name)
    if normalized is None:
        raise ValueError(f"{name} must be finite and non-negative")
    return normalized


def _coerce_payload(value: bytes | bytearray | memoryview, name: str) -> bytes:
    """Validate and copy a wire payload before framing it."""
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError(f"{name} must be bytes-like, got {type(value).__name__}")
    return bytes(value)


def validate_loopback_host(host: str) -> str:
    """Return a normalized loopback host or reject unsafe destinations.

    The wire protocol has no authentication or encryption.  Keeping the
    transport itself loopback-only prevents a direct library caller from
    accidentally exposing an unauthenticated link service on a LAN.  A
    caller that needs cross-host operation must provide a separately secured
    transport rather than bypassing this guard.
    """
    if not isinstance(host, str):
        raise TypeError("host must be a string")
    normalized = host.strip()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    if normalized.lower() == "localhost":
        # Do not trust a hosts-file or resolver override for an
        # unauthenticated transport. Every address returned for localhost
        # must still be loopback before it is accepted.
        try:
            addresses = socket.getaddrinfo(normalized, None, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise ValueError("NetworkBackend is localhost-only; localhost did not resolve") from exc
        if not addresses or any(
            not _is_loopback_sockaddr(address[4] if len(address) > 4 else None)
            for address in addresses
        ):
            raise ValueError("NetworkBackend is localhost-only; localhost resolved unsafely")
        return "localhost"
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError as exc:
        raise ValueError(
            "NetworkBackend is localhost-only; use 127.0.0.1, localhost, or ::1"
        ) from exc
    if not address.is_loopback:
        raise ValueError("NetworkBackend is localhost-only; use 127.0.0.1, localhost, or ::1")
    return normalized


def _is_loopback_sockaddr(sockaddr: object) -> bool:
    """Return whether a resolver result is an IP loopback address."""
    if not isinstance(sockaddr, tuple) or not sockaddr:
        return False
    host = sockaddr[0]
    if not isinstance(host, str):
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_connected_socket_loopback(sock: socket.socket) -> None:
    """Reject raw TCP sockets whose peer is not on the local host.

    The factory methods validate their destination or bind address, but the
    low-level constructors also accept an already-connected socket.  Without
    this check a caller could bypass the localhost-only boundary by handing a
    cross-host TCP socket directly to a backend.  AF_UNIX socket pairs remain
    available for in-process tests; they are not TCP transports.
    """
    family = getattr(sock, "family", None)
    unix_family = getattr(socket, "AF_UNIX", None)
    if unix_family is not None and family == unix_family:
        return
    if family not in (socket.AF_INET, socket.AF_INET6):
        raise ValueError("NetworkBackend requires a connected loopback TCP socket")
    try:
        peer = sock.getpeername()
    except OSError as exc:
        raise ValueError("NetworkBackend requires a connected loopback TCP socket") from exc
    if not isinstance(peer, tuple) or not peer or not isinstance(peer[0], str):
        raise ValueError("NetworkBackend requires a connected loopback TCP socket")
    try:
        validate_loopback_host(peer[0])
    except (TypeError, ValueError) as exc:
        raise ValueError("NetworkBackend is localhost-only; raw TCP peer is not loopback") from exc


def _connect_socket(
    host: str,
    port: int,
    timeout_s: float,
    cancel_event: threading.Event | None = None,
) -> socket.socket:
    """Connect with short cancellation polling instead of blocking forever."""
    if cancel_event is not None and cancel_event.is_set():
        raise NetworkBackendError("connection cancelled")
    normalized_host = validate_loopback_host(host)
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("timeout_s must be finite and positive")
    deadline = _entry.time.monotonic() + timeout_s
    try:
        addresses = socket.getaddrinfo(normalized_host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise NetworkBackendError(f"unable to resolve {normalized_host!r}: {exc}") from exc
    if not addresses or any(
        not _is_loopback_sockaddr(address[4] if len(address) > 4 else None) for address in addresses
    ):
        raise NetworkBackendError(
            "NetworkBackend is localhost-only; resolver returned an unsafe address"
        )
    last_error: OSError | None = None
    for family, socktype, proto, _canonname, sockaddr in addresses:
        if cancel_event is not None and cancel_event.is_set():
            raise NetworkBackendError("connection cancelled")
        sock = socket.socket(family, socktype, proto)
        connected = False
        try:
            sock.setblocking(False)
            result = sock.connect_ex(sockaddr)
            if result == 0:
                connected = True
                sock.setblocking(True)
                return sock
            if result not in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY):
                last_error = OSError(result, errno.errorcode.get(result, "connect failed"))
                continue
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise NetworkBackendError("connection cancelled")
                remaining = deadline - _entry.time.monotonic()
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
                    connected = True
                    sock.setblocking(True)
                    return sock
                last_error = OSError(error, errno.errorcode.get(error, "connect failed"))
                break
        except NetworkBackendError:
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
    raise NetworkBackendError(f"unable to connect to {normalized_host}:{port}")


def _socket_family(host: str) -> int:
    return socket.AF_INET6 if ":" in host else socket.AF_INET


def _validate_rom_version(version: str) -> str:
    normalized = version.strip().lower()
    if normalized not in _ROM_VERSION_CODES:
        raise ValueError(
            f"unsupported ROM version {version!r}; expected one of {sorted(_ROM_VERSION_CODES)}"
        )
    return normalized
