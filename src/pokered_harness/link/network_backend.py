"""TCP-backed :class:`SerialBackend` for two-process PyBoy linking.

Milestone 8 of the design doc (:doc:`docs/pyboy_serial_overhaul_design.md`).
Where :class:`CoordinatedBackend` exchanges bits through direct access
to a peer :class:`SerialCore` in the same process, :class:`NetworkBackend`
exchanges bits through a TCP socket so two independent Python processes
(potentially on different machines) can drive linked PyBoys.

Protocol
--------

Byte-oriented, request/response, framed::

    struct EdgeFrame {
        uint8_t opcode;   // 0x10 EDGE_REQ  (master -> slave)
                          // 0x11 EDGE_RESP (slave -> master)
        uint8_t payload;  // bit value: 0 or 1
    };

Either side can be master at any moment. The master's ``on_edge`` sends
an ``EDGE_REQ`` and waits for the peer's ``EDGE_RESP``; meanwhile a
background reader thread on both sides demuxes incoming frames. In owner
dispatch mode, when a reader sees ``EDGE_REQ`` (peer is master, we are
slave) it queues the request for the emulator owner to advance our local
:class:`SerialCore` via ``apply_external_edge`` and respond with our
outgoing bit. When it sees ``EDGE_RESP`` (peer is slave responding
to our master request) it hands the bit to the blocking ``on_edge``
waiter via a response queue.

Slave IRQ
---------

When the owner-side dispatch path completes the 8th edge on the local slave
core, it fires the optional ``irq_callback`` so halted code wakes up
(Pokemon's ``halt; wait for serial IRQ`` idiom). Wire this to
``pyboy.mb.cpu.set_interruptflag(INTR_SERIAL)`` on attach.

Threading model
---------------

``on_edge`` blocks for one socket round-trip. The caller is expected
to be PyBoy's main tick thread. The reader thread runs in the
background and uses a response queue to hand back RESP payloads. In
production owner-dispatch mode, REQ frames are handed to the emulator
owner and a response-only worker sends the completed response; no network
thread touches emulator state. A single write-lock ensures response sends
do not collide with ``on_edge``'s REQ-sends on the same socket. The legacy
direct-worker mode remains available for low-level callers that explicitly
do not attach a PyBoy instance.
"""

from __future__ import annotations

import errno
import ipaddress
import json
import math
import queue
import select
import socket
import struct
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from pokered_harness.link.serial_coordinator import SerialOperationGate

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
    deadline = time.monotonic() + timeout_s
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
                remaining = deadline - time.monotonic()
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


class NetworkBackend:
    """Bit-level SerialBackend over a TCP socket.

    Symmetric: both peers instantiate a ``NetworkBackend`` over the
    same socket. Call :meth:`start_receiver` on each side, supplying
    the local :class:`SerialCore` and an IRQ callback, to enable
    slave-side behavior (incoming EDGE_REQ drives our local core).
    Call :meth:`stop` to tear down.
    """

    def __init__(self, sock: socket.socket, *, local_rom_version: str | None = None) -> None:
        _validate_connected_socket_loopback(sock)
        self._sock = sock
        # Keep both directions non-blocking.  The reader uses short
        # select-polling below, while writers use the same bounded polling
        # primitive.  This makes a saturated peer unable to wedge either the
        # emulator thread or lifecycle teardown in ``sendall``/``recv``.
        try:
            self._sock.setblocking(False)
        except OSError:
            try:
                self._sock.close()
            except OSError:
                pass
            raise
        self._write_lock = threading.Lock()
        # A Game Boy serial core has one outstanding master edge at a time.
        # EDGE_RESP has no request id, so a late/duplicate response cannot
        # safely be matched to a later edge.
        self._edge_call_lock = threading.Lock()
        self._edge_response_lock = threading.Lock()
        self._edge_inflight = False
        # Responses from peer (when we're master) land here.
        self._resp_queue: queue.Queue[int] = queue.Queue(maxsize=1)
        # Negotiated frame barrier queues. They are enabled by the session
        # only after the versioned network clock-role metadata is selected;
        # the session never seeds the native serial registers.
        self._frame_tick_queue: queue.Queue[None] = queue.Queue(maxsize=1)
        self._frame_done_queue: queue.Queue[None] = queue.Queue(maxsize=1)
        self._frame_ack_queue: queue.Queue[None] = queue.Queue(maxsize=1)
        self._frame_turn_lock = threading.Lock()
        self._leader_frame_inflight = False
        # Keep edge application ordered, but do not let a slow slave
        # re-arm wait block the reader from consuming control frames such
        # as SYNC or HELLO. The master sends one EDGE_REQ at a time, so a
        # bounded queue is sufficient and makes overload fail closed. In
        # owner-dispatch mode this queue contains requests which only the
        # emulator owner may execute; the network threads never touch the
        # native serial object.
        self._edge_queue: queue.Queue[_InboundEdge | None] = _InboundEdgeQueue(maxsize=256)
        self._completed_edge_queue: queue.Queue[_InboundEdge | None] = queue.Queue(maxsize=256)
        # Counts EDGE_REQ frames from enqueue until their response has been
        # written.  A phase barrier can therefore wait for the wire work
        # already admitted by the reader without mistaking an armed-but-idle
        # ROM SC register for an in-flight transfer.
        self._edge_pending_condition = threading.Condition()
        self._edge_pending = 0
        # Peer SYNC events, indexed by sync-id. queue.Queue per id
        # lets multiple pending syncs coexist (unusual but defensively
        # modeled).
        self._sync_queues: dict[int, queue.Queue[int]] = {}
        # At most one unconsumed marker is allowed for a sync id. The wire
        # format has no request id, so accepting two pending markers would let
        # a duplicate satisfy a later barrier. Once a marker is consumed, the
        # id may be reused for the next phase.
        self._sync_pending: set[int] = set()
        self._exchange_queues: dict[int, queue.Queue[bytes]] = {}
        # poll_peer_sync and the reader both update the per-id pending marker;
        # a re-entrant lock keeps the small queue helper safe when called from
        # either path while preserving one ordering point for duplicate checks.
        self._sync_lock = threading.RLock()
        self._exchange_lock = threading.Lock()
        # Serialise owner-side edge admission with the terminal transition.
        # The owner may be between its closed check and native edge apply
        # while a reader or lifecycle caller is closing the transport. A
        # re-entrant lock lets the owner fail-closed path call _mark_closed()
        # without self-deadlocking; _mark_closed acquires this lock before
        # _close_lock, which is the only lock-order rule callers need follow.
        self._owner_dispatch_lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._closed = False
        self._closed_event = threading.Event()
        self._reader_exc: Exception | None = None
        # Kept once, at the terminal transition, so cleanup/status callers
        # can inspect wire work that is deliberately cleared from the live
        # accounting below.
        self._pre_close_snapshot: dict[str, object] | None = None
        self._local_rom_version = (
            _validate_rom_version(local_rom_version) if local_rom_version is not None else None
        )
        self._peer_rom_version: str | None = None
        self._hello_received = threading.Event()
        # Slave-mode config — set by start_receiver.
        self._local_core: object | None = None
        self._irq_callback: Callable[[], None] | None = None
        # Every path which can touch ``_local_core`` enters this admission
        # barrier before reading the reference.  Detach uses it as a small
        # quiescence barrier: once ``_local_core_detaching`` is published,
        # no new worker/owner operation can acquire a core reference, while
        # an operation already admitted is allowed to finish within the
        # caller's bounded deadline.  This is deliberately separate from
        # ``_serial_gate`` because that gate is an externally-owned RLock and
        # cannot be acquired with a timeout.
        self._local_core_condition = threading.Condition()
        self._local_core_users = 0
        self._local_core_detaching = False
        self._local_core_detached = False
        self._local_core_access_state = threading.local()
        # Optional owner-thread context sampled only for enabled transcript
        # byte completions.  It is deliberately observational: a provider
        # failure is recorded in the transcript and never changes transport
        # or emulator behavior.
        self._serial_transcript_context_provider: Callable[[], object] | None = None
        self._serial_transcript_context_max_bytes = 1024
        self._serial_gate = SerialOperationGate()
        self._dispatch_to_owner = False
        self._reader: threading.Thread | None = None
        self._edge_worker: threading.Thread | None = None
        self._receiver_start_lock = threading.Lock()
        # Keep-alive "fake slave" bit index. When our local core is
        # idle but the peer is still master-clocking (common during
        # trade/battle sequences where one side's CPU finishes a phase
        # slightly ahead of the peer), stream the bits of
        # SERIAL_NO_DATA_BYTE (0xFE, binary 1111 1110, MSB first) back
        # to the peer instead of pull-up 0xFF. Pokemon's protocol
        # treats 0xFE as "connected, no data" but 0xFF as "cable
        # yanked"; the keep-alive byte keeps the peer progressing
        # through its still-running exchange until our side catches up
        # and re-arms its slave core.
        self._keepalive_bit_idx: int = 0
        self._stats: dict[str, object] = {
            "edge_req_sent": 0,
            "edge_req_received": 0,
            "edge_resp_sent": 0,
            "edge_resp_received": 0,
            "frame_ticks_sent": 0,
            "frame_ticks_received": 0,
            "frame_dones_sent": 0,
            "frame_dones_received": 0,
            "frame_acks_sent": 0,
            "frame_acks_received": 0,
            "sync_sent": 0,
            "sync_received": 0,
            "sync_poll_hits": 0,
            "exchange_sent": 0,
            "exchange_received": 0,
            "unknown_opcode_count": 0,
            "slave_rearm_waits": 0,
            "slave_rearm_successes": 0,
            "slave_idle_rearm_waits": 0,
            "slave_active_rearm_waits": 0,
            "slave_post_byte_rearm_waits": 0,
            "slave_post_byte_rearm_successes": 0,
            "keepalive_after_post_byte_waits": 0,
            "slave_armed_edges": 0,
            "keepalive_bits_sent": 0,
            "keepalive_bytes_started": 0,
            "irq_callbacks": 0,
            "irq_callback_errors": 0,
            "last_irq_callback_error": None,
            "last_keepalive_state": None,
            "last_slave_byte_complete_at": None,
            "last_slave_rearm_at": None,
            "owner_edge_deferred": 0,
            "owner_edge_applied": 0,
            "owner_edge_errors": 0,
        }
        self._active_exchange_until: float = 0.0
        self._consecutive_armed_edges: int = 0
        self._post_byte_rearm_until: float = 0.0
        # The edge path is timing-sensitive, so detailed records are opt-in.
        # Once enabled, this fixed-size ring is the only transcript storage:
        # a busy or broken peer can never cause unbounded diagnostic memory.
        self._serial_transcript_lock = threading.Lock()
        self._serial_transcript: deque[dict[str, object]] | None = None
        self._serial_transcript_capacity = 0
        self._serial_transcript_dropped = 0
        self._serial_transcript_sequence = 0

        if self._local_rom_version is not None:
            self._send_hello(self._local_rom_version)

    @classmethod
    def listen(
        cls,
        port: int,
        *,
        host: str = "127.0.0.1",
        backlog: int = 1,
        local_rom_version: str | None = None,
        accept_timeout_s: float = _DEFAULT_ACCEPT_TIMEOUT_SECONDS,
        cancel_event: threading.Event | None = None,
    ) -> tuple[NetworkBackend, socket.socket]:
        """Bind to ``(host, port)`` and wait until a peer connects.

        Returns ``(backend, listener_sock)``; the caller keeps the
        listener socket to close later if needed. A fresh accepted
        socket is wrapped by the backend. ``accept_timeout_s`` and
        ``cancel_event`` make the accept cancellable for lifecycle owners.
        The accept deadline is always finite; cancellation may end it sooner.
        """
        normalized_host = validate_loopback_host(host)
        accept_timeout_s = _validate_optional_timeout(accept_timeout_s, "accept_timeout_s")
        if accept_timeout_s is None:
            raise ValueError("accept_timeout_s must be finite and positive")
        family = _socket_family(normalized_host)
        listener = socket.socket(family, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        bind_address = (
            (normalized_host, port, 0, 0) if family == socket.AF_INET6 else (normalized_host, port)
        )
        conn: socket.socket | None = None
        try:
            listener.bind(bind_address)
            listener.listen(backlog)
            deadline = time.monotonic() + accept_timeout_s
            while conn is None:
                if cancel_event is not None and cancel_event.is_set():
                    raise NetworkBackendError("listener accept cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NetworkBackendError(
                        f"listener accept timed out after {accept_timeout_s:g}s"
                    )
                listener.settimeout(min(0.25, remaining))
                try:
                    conn, _ = listener.accept()
                except TimeoutError:
                    continue
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            backend = cls(conn, local_rom_version=local_rom_version)
        except BaseException:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass
            try:
                listener.close()
            except OSError:
                pass
            raise
        return backend, listener

    @classmethod
    def connect(
        cls,
        host: str,
        port: int,
        *,
        timeout_s: float = 10.0,
        local_rom_version: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> NetworkBackend:
        """Open an outbound connection to a ``listen``-ing peer."""
        normalized_host = validate_loopback_host(host)
        sock = _connect_socket(normalized_host, port, timeout_s, cancel_event)
        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return cls(sock, local_rom_version=local_rom_version)

    @classmethod
    def pair(cls) -> tuple[NetworkBackend, NetworkBackend]:
        """Build a :func:`socket.socketpair` pair of backends — useful
        for in-process testing without real TCP."""
        a, b = socket.socketpair()
        return cls(a), cls(b)

    @property
    def connected(self) -> bool:
        """Whether the socket has not been closed by either side."""
        return not self._closed

    @property
    def local_rom_version(self) -> str | None:
        """Return the local HELLO ROM label, if versioned transport is on."""
        return self._local_rom_version

    @property
    def peer_rom_version(self) -> str:
        """Return the peer's versioned protocol ROM label.

        The label is available only when both sides opted into the
        versioned handshake. Legacy in-process test pairs intentionally have
        no label and must use :meth:`wait_for_hello` only when configured.
        """
        if self._peer_rom_version is None:
            raise NetworkBackendError("peer HELLO has not completed")
        return self._peer_rom_version

    def wait_for_hello(
        self,
        timeout: float = 10.0,
        *,
        expected_peer_rom_version: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> str | None:
        """Wait for the optional versioned handshake.

        Unversioned ``NetworkBackend.pair()`` users get ``None`` so the
        historical ROM-free backend tests remain valid. Production TCP
        callers pass ``local_rom_version`` and therefore fail closed if the
        peer does not present a compatible protocol/ROM identity. Callers that
        know the expected peer label can pass it explicitly to reject a
        mismatched announcement. ``cancel_event`` is polled while waiting;
        cancellation closes this transport because the handshake has no
        request id and cannot safely be resumed.
        """
        timeout = _require_timeout(timeout, "timeout")
        expected = (
            _validate_rom_version(expected_peer_rom_version)
            if expected_peer_rom_version is not None
            else None
        )
        if self._local_rom_version is None:
            if cancel_event is not None and cancel_event.is_set():
                raise NetworkBackendError("HELLO wait cancelled")
            if expected is not None:
                raise NetworkBackendError(
                    "cannot validate peer ROM without a local versioned HELLO"
                )
            return None
        deadline = time.monotonic() + timeout
        while not self._hello_received.is_set():
            if cancel_event is not None and cancel_event.is_set():
                error = NetworkBackendError("HELLO wait cancelled")
                self._mark_closed(error)
                raise error
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                error = NetworkBackendError(f"peer HELLO not received within {timeout:g}s")
                self._mark_closed(error)
                raise error
            self._hello_received.wait(timeout=min(_SEND_POLL_SECONDS, remaining))
        if self._closed and self._peer_rom_version is None:
            if self._reader_exc is not None:
                raise NetworkBackendError(
                    f"peer HELLO failed: {self._reader_exc}"
                ) from self._reader_exc
            raise NetworkBackendError("backend closed before peer HELLO")
        if self._reader_exc is not None and self._peer_rom_version is None:
            raise NetworkBackendError(
                f"peer HELLO failed: {self._reader_exc}"
            ) from self._reader_exc
        peer_version = self.peer_rom_version
        if expected is not None and peer_version != expected:
            error = NetworkBackendError(
                f"peer ROM version {peer_version!r} does not match expected {expected!r}"
            )
            self._mark_closed(error)
            raise error
        return peer_version

    # --- slave-side receiver ------------------------------------------

    def start_receiver(
        self,
        local_core: object,
        irq_callback: Callable[[], None] | None = None,
        *,
        serial_gate: SerialOperationGate | None = None,
        dispatch_to_owner: bool = False,
    ) -> None:
        """Start the reader thread.

        When an incoming ``EDGE_REQ`` arrives (the peer is acting as
        master), the request is either handled by the compatibility
        worker or, when ``dispatch_to_owner`` is true, queued for
        :meth:`service_pending_edges`. The latter is the production
        PyBoy path: the caller that owns the emulator tick applies the
        bit to ``local_core`` via ``apply_external_edge``, reads
        ``peek_out_bit()``, and invokes ``irq_callback`` while holding
        ``serial_gate``. No network thread touches emulator state.

        ``dispatch_to_owner=True`` requires a shared ``serial_gate``. The
        PyBoy tick wrapper and this owner-side dispatch must use the same
        gate so a native ``Serial.tick`` cannot overlap an external edge.
        If ``apply_external_edge`` returns True (8th-edge completion) and
        ``irq_callback`` is provided, the callback fires on the owner
        thread.

        ``EDGE_RESP`` frames (responses to our own master-side
        ``on_edge`` requests) are put on the response queue for the
        blocking ``on_edge`` call to pick up.

        """
        with self._receiver_start_lock:
            if self._closed:
                raise NetworkBackendError("backend closed")
            if self._reader is not None:
                # A successful detach intentionally leaves the transport and
                # its reader/response workers alive so a session can attach a
                # fresh emulator endpoint without reconnecting. Rebinding is
                # allowed only for the same worker mode, with live workers and
                # no queued edge work that could belong to the old core.
                with self._local_core_condition:
                    if not self._local_core_detached:
                        raise RuntimeError("receiver already started")
                    if self._local_core_detaching:
                        raise NetworkBackendError("local serial core detach is still in progress")
                    edge_worker = self._edge_worker
                    reader = self._reader
                    if not all(
                        isinstance(worker, threading.Thread) and worker.is_alive()
                        for worker in (edge_worker, reader)
                    ):
                        raise NetworkBackendError(
                            "receiver workers have stopped; reconnect before rebinding"
                        )
                    if dispatch_to_owner != self._dispatch_to_owner:
                        raise ValueError("receiver dispatch mode cannot change while rebinding")
                    if serial_gate is not None and serial_gate is not self._serial_gate:
                        raise ValueError("receiver serial gate cannot change while rebinding")
                    with self._edge_pending_condition:
                        if self._edge_pending or not self._edge_queue.empty():
                            raise NetworkBackendError(
                                "cannot rebind while edge work is pending; reconnect instead"
                            )
                        if self._dispatch_to_owner and not self._completed_edge_queue.empty():
                            raise NetworkBackendError(
                                "cannot rebind while edge responses are pending; reconnect instead"
                            )
                        self._local_core = local_core
                        self._irq_callback = irq_callback
                        self._local_core_detached = False
                        self._local_core_condition.notify_all()
                return
            if dispatch_to_owner and serial_gate is None:
                raise ValueError("dispatch_to_owner=True requires a shared serial_gate")
            with self._local_core_condition:
                if self._local_core_detached:
                    raise NetworkBackendError("local serial core has been detached")
                self._local_core = local_core
                self._irq_callback = irq_callback
            if serial_gate is not None:
                self._serial_gate = serial_gate
            self._dispatch_to_owner = dispatch_to_owner
            worker_target = (
                self._owner_response_worker_loop if dispatch_to_owner else self._edge_worker_loop
            )
            self._edge_worker = threading.Thread(
                target=worker_target,
                name=(
                    "NetworkBackend.owner-response-worker"
                    if dispatch_to_owner
                    else "NetworkBackend.edge-worker"
                ),
                daemon=True,
            )
            self._reader = threading.Thread(
                target=self._reader_loop, name="NetworkBackend.reader", daemon=True
            )
            self._edge_worker.start()
            self._reader.start()

    # --- SerialBackend.on_edge (master path) --------------------------

    def on_edge(self, our_bit: int, our_role: int) -> int:
        """Master-mode: send ``our_bit`` as EDGE_REQ, wait for EDGE_RESP.

        Blocks until the peer's reader thread responds. Timeout raises
        :class:`NetworkBackendError`. If the reader on our own side
        isn't running yet, incoming responses will be lost — callers
        must call :meth:`start_receiver` before master-mode transfers.

        Admission, send, and response waits share one deadline. Terminal
        cleanup still waits for an admitted owner operation to finish, so
        this is not an end-to-end bound on error return or shutdown.
        """
        del our_role  # the wire role is carried by the ROM's SC register
        deadline = time.monotonic() + _EDGE_RESPONSE_TIMEOUT_SECONDS
        # Admission failure must not close or mutate another caller's exchange.
        with self._edge_admission_guard(self._edge_call_lock, deadline):
            stale_error: NetworkBackendError | None = None
            with self._edge_admission_guard(self._edge_response_lock, deadline):
                if self._closed:
                    raise NetworkBackendError("backend closed")
                if self._edge_inflight:
                    raise NetworkBackendError("concurrent EDGE_REQ is not supported")
                if not self._resp_queue.empty():
                    stale_error = NetworkBackendError("stale EDGE_RESP before EDGE_REQ")
                else:
                    self._edge_inflight = True

            if stale_error is not None:
                self._mark_closed(stale_error)
                raise stale_error

            frame_out = _FRAME.pack(_OP_EDGE_REQ, our_bit & 1)
            self._stats["edge_req_sent"] = int(self._stats["edge_req_sent"]) + 1
            self._record_serial_event(
                "edge_req_sent",
                direction="local_to_peer",
                edge_bit=our_bit & 1,
            )
            # EDGE_RESP has no request id, so a late response cannot be
            # retried or matched to a later transfer. Reuse the admission
            # deadline rather than granting send and receive fresh budgets.
            try:
                with self._write_guard(
                    deadline=deadline,
                    operation="EDGE_REQ",
                ) as write_deadline:
                    self._send_frame(
                        frame_out,
                        timeout=max(0.0, write_deadline - time.monotonic()),
                        operation="EDGE_REQ",
                    )
                bit = self._queue_get(
                    self._resp_queue,
                    timeout=max(0.0, deadline - time.monotonic()),
                    timeout_message=(
                        f"no EDGE_RESP from peer within {_EDGE_RESPONSE_TIMEOUT_SECONDS:g}s"
                    ),
                )
                self._stats["edge_resp_received"] = int(self._stats["edge_resp_received"]) + 1
                self._record_serial_event(
                    "edge_resp_consumed",
                    direction="peer_to_local",
                    edge_bit=bit,
                )
                return bit
            except OSError as exc:
                error = NetworkBackendError(f"failed to send EDGE_REQ: {exc}")
                self._mark_closed(error)
                raise error from exc
            except NetworkBackendError as exc:
                # Without request ids, a timed-out edge cannot be safely
                # retried: a delayed peer response would otherwise become
                # the response for a future edge. Terminate the transport
                # and require a fresh connection instead.
                if not self._closed_event.is_set():
                    self._mark_closed(exc)
                raise
            finally:
                with self._edge_response_lock:
                    self._edge_inflight = False
                with self._edge_pending_condition:
                    self._edge_pending_condition.notify_all()

    @contextmanager
    def _edge_admission_guard(self, lock: Any, deadline: float) -> Iterator[None]:
        """Acquire before publishing an edge, without terminal side effects."""
        while True:
            if self._closed_event.is_set() or self._closed:
                raise NetworkBackendError("backend closed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NetworkBackendError("EDGE_REQ admission timed out")
            if lock.acquire(timeout=min(_SEND_POLL_SECONDS, remaining)):
                break
        try:
            if self._closed_event.is_set() or self._closed:
                raise NetworkBackendError("backend closed")
            # Acquisition may finish at the deadline after scheduler delay.
            if time.monotonic() >= deadline:
                raise NetworkBackendError("EDGE_REQ admission timed out")
            yield
        finally:
            lock.release()

    # --- negotiated frame barrier -----------------------------------

    def begin_frame_turn(
        self,
        *,
        leader: bool,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Start one bounded owner-frame turn after clock negotiation.

        Cancellation is terminal for this transport.  The frame marker has
        no request id, so a cancelled turn cannot safely be resumed on the
        same connection.
        """
        if not isinstance(leader, bool):
            raise TypeError("leader must be a bool")
        if leader:
            with self._frame_turn_lock:
                if self._leader_frame_inflight:
                    raise NetworkBackendError("previous FRAME_TICK is still in flight")
                self._leader_frame_inflight = True
            try:
                with self._write_guard(
                    timeout=_EDGE_RESPONSE_TIMEOUT_SECONDS,
                    operation="FRAME_TICK",
                    cancel_event=cancel_event,
                ) as write_deadline:
                    self._send_frame(
                        _FRAME.pack(_OP_FRAME_TICK, 0),
                        timeout=max(0.0, write_deadline - time.monotonic()),
                        operation="FRAME_TICK",
                        cancel_event=cancel_event,
                    )
                self._stats["frame_ticks_sent"] = int(self._stats["frame_ticks_sent"]) + 1
            except (OSError, NetworkBackendError) as exc:
                self._clear_leader_frame_inflight()
                error = (
                    exc
                    if isinstance(exc, NetworkBackendError)
                    else NetworkBackendError(f"failed to send FRAME_TICK: {exc}")
                )
                self._mark_closed(error)
                raise error from exc
            return
        try:
            self._queue_get(
                self._frame_tick_queue,
                timeout=_EDGE_RESPONSE_TIMEOUT_SECONDS,
                timeout_message=(
                    f"no FRAME_TICK from peer within {_EDGE_RESPONSE_TIMEOUT_SECONDS:g}s"
                ),
                cancel_event=cancel_event,
            )
        except NetworkBackendError as exc:
            if not self._closed_event.is_set():
                self._mark_closed(exc)
            raise

    def finish_frame_turn(
        self,
        *,
        leader: bool,
        progress_callback: Callable[[], object] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Finish a frame, servicing deferred owner edges before ACK.

        The ACK wait has one absolute transport deadline.  If an owner
        ``progress_callback`` is supplied, it is invoked between short queue
        polls so peer-driven edges can be applied even when the native clock
        role differs from the negotiated pacing role.  Cancellation is
        terminal for the same reason as :meth:`begin_frame_turn`.
        """
        if not isinstance(leader, bool):
            raise TypeError("leader must be a bool")
        if leader:
            try:
                with self._write_guard(
                    timeout=_EDGE_RESPONSE_TIMEOUT_SECONDS,
                    operation="FRAME_DONE",
                    cancel_event=cancel_event,
                ) as write_deadline:
                    self._send_frame(
                        _FRAME.pack(_OP_FRAME_DONE, 0),
                        timeout=max(0.0, write_deadline - time.monotonic()),
                        operation="FRAME_DONE",
                        cancel_event=cancel_event,
                    )
                self._stats["frame_dones_sent"] = int(self._stats["frame_dones_sent"]) + 1
                # In the normal PyBoy owner path the leader calls this method
                # while it still owns the serial gate.  A peer may have
                # started its native-clock transfer only after FRAME_DONE
                # was sent, though.  In that case its EDGE_REQ is queued for
                # this backend's owner and the peer cannot produce FRAME_ACK
                # until we apply that edge.  Keep the transport wait bounded,
                # but give owner-dispatch work a chance to run between polls.
                if progress_callback is None and self._dispatch_to_owner:
                    progress_callback = lambda: self.service_pending_edges(max_edges=1)
                self._queue_get(
                    self._frame_ack_queue,
                    timeout=_EDGE_RESPONSE_TIMEOUT_SECONDS,
                    timeout_message=(
                        f"no FRAME_ACK from peer within {_EDGE_RESPONSE_TIMEOUT_SECONDS:g}s"
                    ),
                    cancel_event=cancel_event,
                    progress_callback=progress_callback,
                )
            except NetworkBackendError as exc:
                if not self._closed_event.is_set():
                    self._mark_closed(exc)
                raise
            finally:
                self._clear_leader_frame_inflight()
            return
        if progress_callback is None:
            raise TypeError("follower frame completion requires progress_callback")
        deadline = time.monotonic() + _EDGE_RESPONSE_TIMEOUT_SECONDS
        try:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise NetworkBackendError("frame completion cancelled")
                self._run_progress_callback(progress_callback)
                if cancel_event is not None and cancel_event.is_set():
                    raise NetworkBackendError("frame completion cancelled")
                try:
                    self._frame_done_queue.get_nowait()
                except queue.Empty:
                    pass
                else:
                    with self._edge_pending_condition:
                        if self._edge_pending == 0:
                            break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NetworkBackendError(
                        f"no FRAME_DONE from peer within {_EDGE_RESPONSE_TIMEOUT_SECONDS:g}s"
                    )
                with self._edge_pending_condition:
                    self._edge_pending_condition.wait(
                        timeout=min(_SEND_POLL_SECONDS, remaining)
                    )
        except NetworkBackendError as exc:
            if not self._closed_event.is_set():
                self._mark_closed(exc)
            raise
        try:
            with self._write_guard(
                timeout=_EDGE_RESPONSE_TIMEOUT_SECONDS,
                operation="FRAME_ACK",
                cancel_event=cancel_event,
            ) as write_deadline:
                self._send_frame(
                    _FRAME.pack(_OP_FRAME_ACK, 0),
                    timeout=max(0.0, write_deadline - time.monotonic()),
                    operation="FRAME_ACK",
                    cancel_event=cancel_event,
                )
            self._stats["frame_acks_sent"] = int(self._stats["frame_acks_sent"]) + 1
        except (OSError, NetworkBackendError) as exc:
            error = (
                exc
                if isinstance(exc, NetworkBackendError)
                else NetworkBackendError(f"failed to send FRAME_ACK: {exc}")
            )
            self._mark_closed(error)
            raise error from exc

    def abort_frame_turn(self, *, leader: bool) -> None:
        if not isinstance(leader, bool):
            raise TypeError("leader must be a bool")
        if leader:
            self._clear_leader_frame_inflight()
        self._mark_closed(NetworkBackendError("emulator frame aborted during frame barrier"))

    def _clear_leader_frame_inflight(self) -> None:
        with self._frame_turn_lock:
            self._leader_frame_inflight = False

    # --- out-of-band rendezvous ---------------------------------------

    def sync_with_peer(
        self,
        sync_id: int = 0,
        *,
        timeout: float = 120.0,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Cross-peer rendezvous barrier.

        Send an ``OP_SYNC`` frame with ``sync_id`` (0-255), then block
        until the peer's matching ``OP_SYNC`` arrives. Both peers must
        call ``sync_with_peer`` with the same ``sync_id`` to proceed.
        Used by higher-level drivers (e.g. the subprocess trade flow)
        to converge both sides at phase boundaries.

        Raises :class:`NetworkBackendError` if the peer's SYNC doesn't
        arrive within ``timeout`` seconds. The timeout covers both the send
        and receive phases as one total deadline. Cancellation closes the
        transport after the marker is sent so a stale barrier cannot be
        mistaken for a later one.
        """
        sync_id = _validate_id(sync_id, "sync_id")
        timeout = _require_timeout(timeout, "timeout")
        q = self._sync_queue(sync_id)
        deadline = time.monotonic() + timeout
        try:
            self._send_sync_with_timeout(
                sync_id,
                timeout=max(0.0, deadline - time.monotonic()),
                cancel_event=cancel_event,
            )
            self._queue_get(
                q,
                timeout=max(0.0, deadline - time.monotonic()),
                timeout_message=f"no peer OP_SYNC({sync_id}) within {timeout}s",
                cancel_event=cancel_event,
            )
            with self._sync_lock:
                self._sync_pending.discard(sync_id)
        except NetworkBackendError:
            # A cancelled or timed-out SYNC has already put a control marker
            # on the wire. Without request ids, retaining the connection
            # would let that stale marker satisfy a later barrier.
            if not self._closed:
                self._mark_closed()
            raise

    def announce_sync(
        self,
        sync_id: int = 0,
        *,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Send an ``OP_SYNC`` marker without waiting for the peer.

        Higher-level drivers can use this to advertise that the local
        process reached a state boundary while continuing to step its
        CPU until the peer catches up.
        """
        self._sync_queue(sync_id)
        self._send_sync(sync_id, cancel_event=cancel_event)

    def poll_peer_sync(self, sync_id: int = 0) -> bool:
        """Return True if a peer ``OP_SYNC`` for ``sync_id`` is pending.

        Consumes one queued peer SYNC if present; otherwise returns
        False immediately.
        """
        sync_id = _validate_id(sync_id, "sync_id")
        with self._sync_lock:
            q = self._sync_queue(sync_id)
            try:
                q.get_nowait()
            except queue.Empty:
                return False
            self._sync_pending.discard(sync_id)
        self._stats["sync_poll_hits"] = int(self._stats["sync_poll_hits"]) + 1
        return True

    def exchange_block(
        self,
        kind_id: int,
        payload: bytes,
        *,
        timeout: float = 120.0,
        cancel_event: threading.Event | None = None,
    ) -> bytes:
        """Exchange an arbitrary serial data block with the peer.

        This is an out-of-band helper for high-level ROM routines such as
        ``Serial_ExchangeBytes`` where bit-level transfer is prohibitively
        slow or can desynchronize after thousands of serial edges. FIFO is
        preserved per ``kind_id``. The timeout covers both directions as one
        total deadline; cancellation closes the transport after a frame may
        have been admitted.
        """
        kind_id = _validate_id(kind_id, "kind_id")
        payload = _coerce_payload(payload, "payload")
        if len(payload) > 0xFFFF:
            raise ValueError(f"payload too large: {len(payload)} bytes")
        timeout = _require_timeout(timeout, "timeout")
        q = self._exchange_queue(kind_id)
        frame = _FRAME.pack(_OP_EXCHANGE, kind_id) + _LEN.pack(len(payload)) + payload
        self._stats["exchange_sent"] = int(self._stats["exchange_sent"]) + 1
        deadline = time.monotonic() + timeout
        try:
            with self._write_guard(
                deadline=deadline,
                operation=f"OP_EXCHANGE({kind_id})",
                cancel_event=cancel_event,
            ) as write_deadline:
                self._send_frame(
                    frame,
                    timeout=max(0.0, write_deadline - time.monotonic()),
                    operation=f"OP_EXCHANGE({kind_id})",
                    cancel_event=cancel_event,
                )
        except (OSError, NetworkBackendError) as exc:
            error = (
                exc
                if isinstance(exc, NetworkBackendError)
                else NetworkBackendError(f"failed to send OP_EXCHANGE({kind_id}): {exc}")
            )
            self._mark_closed(error)
            raise error from exc
        return self._queue_get(
            q,
            timeout=max(0.0, deadline - time.monotonic()),
            timeout_message=f"no peer OP_EXCHANGE({kind_id}) within {timeout}s",
            cancel_event=cancel_event,
        )

    def debug_snapshot(self) -> dict[str, object]:
        """Return a shallow, JSON-serializable diagnostic snapshot.

        Once closed, ``pre_close_snapshot`` retains the counters and pending
        edge state observed at the terminal transition. The live
        ``pending_edge_requests`` field continues to report the post-close
        value, which is zero by contract.
        """
        snap = dict(self._stats)
        last_keepalive = snap.get("last_keepalive_state")
        if isinstance(last_keepalive, dict):
            snap["last_keepalive_state"] = dict(last_keepalive)
        with self._close_lock:
            closed = self._closed
            pre_close = self._pre_close_snapshot
        snap["closed"] = closed
        snap["reader_started"] = self._reader is not None
        now = time.monotonic()
        snap["active_exchange"] = now < self._active_exchange_until
        snap["post_byte_rearm_grace"] = now < self._post_byte_rearm_until
        snap["consecutive_armed_edges"] = self._consecutive_armed_edges
        with self._edge_pending_condition:
            snap["pending_edge_requests"] = self._edge_pending
        if pre_close is not None:
            pre_close_copy = dict(pre_close)
            last_keepalive = pre_close_copy.get("last_keepalive_state")
            if isinstance(last_keepalive, dict):
                pre_close_copy["last_keepalive_state"] = dict(last_keepalive)
            reader_error = pre_close_copy.get("reader_error")
            if isinstance(reader_error, dict):
                pre_close_copy["reader_error"] = dict(reader_error)
            snap["pre_close_snapshot"] = pre_close_copy
        snap.update(self.snapshot_stats())
        return snap

    def enable_serial_transcript(self, *, max_entries: int = 256) -> None:
        """Enable a bounded local serial edge/byte diagnostic transcript.

        Records describe only observations available in this process: wire
        direction and bits, best-effort local core state, slave byte
        completion, and IRQ callback result. They never change serial
        scheduling or infer peer-core state. The transcript starts empty on
        every enable so callers can bracket one diagnostic attempt.
        """
        if not 1 <= max_entries <= _MAX_SERIAL_TRANSCRIPT_ENTRIES:
            raise ValueError(
                "max_entries must be between 1 and "
                f"{_MAX_SERIAL_TRANSCRIPT_ENTRIES}, got {max_entries}"
            )
        with self._serial_transcript_lock:
            self._serial_transcript = deque(maxlen=max_entries)
            self._serial_transcript_capacity = max_entries
            self._serial_transcript_dropped = 0
            self._serial_transcript_sequence = 0

    def set_serial_transcript_context_provider(
        self, provider: Callable[[], object] | None, *, max_bytes: int = 1024
    ) -> None:
        """Set an optional bounded completion-context diagnostic provider.

        The provider is called only when a transcript is enabled and a slave
        byte completes. Its return value is copied through a compact JSON
        representation; errors are reported as data and cannot affect wire
        operation.
        """
        if provider is not None and not callable(provider):
            raise TypeError("provider must be callable or None")
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not 1 <= max_bytes <= 4096
        ):
            raise ValueError("max_bytes must be between 1 and 4096")
        with self._local_core_condition:
            if self._local_core_detaching:
                raise NetworkBackendError("local serial core detach is still in progress")
            # A provider can be staged between a successful detach and a
            # guarded same-transport start_receiver rebinding. It does not
            # become reachable until a new local core is published.
            self._serial_transcript_context_provider = provider
            self._serial_transcript_context_max_bytes = max_bytes

    @contextmanager
    def _local_core_access(self, *, allow_none: bool = False) -> Iterator[object | None]:
        """Admit one operation that may access the attached local core.

        The returned reference remains valid until the context exits.  The
        detach path waits for this count to reach zero before clearing any
        core-related callback/reference, so no worker can continue using a
        detached emulator after cleanup reports success.
        """
        depth = int(getattr(self._local_core_access_state, "depth", 0))
        with self._local_core_condition:
            # A nested callback is part of the already-admitted owner/worker
            # operation. Detach may publish its freeze flag while that
            # operation is running; rejecting the nested admission would
            # turn a safe quiescence wait into a spurious core failure.
            if depth == 0 and (self._local_core_detaching or self._local_core_detached):
                raise NetworkBackendError("local serial core has been detached")
            core = self._local_core
            if core is None and not allow_none:
                raise NetworkBackendError("local serial core is not configured")
            self._local_core_users += 1
            self._local_core_access_state.depth = depth + 1
        try:
            yield core
        finally:
            with self._local_core_condition:
                self._local_core_users -= 1
                self._local_core_users = max(0, self._local_core_users)
                self._local_core_condition.notify_all()
                if depth:
                    self._local_core_access_state.depth = depth
                else:
                    del self._local_core_access_state.depth

    def disable_serial_transcript(self) -> None:
        """Disable and discard serial transcript records immediately."""
        with self._serial_transcript_lock:
            self._serial_transcript = None
            self._serial_transcript_capacity = 0
            self._serial_transcript_dropped = 0
            self._serial_transcript_sequence = 0

    def snapshot_stats(self) -> dict[str, object]:
        """Return a copy of bounded serial transcript diagnostics.

        Sequence order is local backend event order, not emulator-cycle
        atomicity: owner and transport threads can observe a core between
        ticks. Returned records are copied so callers cannot mutate the live
        ring.
        """
        with self._serial_transcript_lock:
            transcript = self._serial_transcript
            if transcript is None:
                return {
                    "serial_transcript": {
                        "enabled": False,
                        "capacity": 0,
                        "dropped": 0,
                        "records": [],
                    }
                }
            records: list[dict[str, object]] = []
            for record in transcript:
                copied = dict(record)
                for key in ("local_state_before", "local_state_after"):
                    value = copied.get(key)
                    if isinstance(value, dict):
                        copied[key] = dict(value)
                completion_context = copied.get("completion_context")
                if isinstance(completion_context, dict):
                    copied["completion_context"] = json.loads(json.dumps(completion_context))
                records.append(copied)
            return {
                "serial_transcript": {
                    "enabled": True,
                    "capacity": self._serial_transcript_capacity,
                    "dropped": self._serial_transcript_dropped,
                    "records": records,
                }
            }

    def _record_serial_event(self, event: str, **fields: object) -> None:
        """Append one local diagnostic record when the opt-in ring is live."""
        if self._serial_transcript is None:
            return
        record: dict[str, object] = {
            "sequence": 0,
            "monotonic_s": time.monotonic(),
            "event": event,
            **fields,
        }
        with self._serial_transcript_lock:
            transcript = self._serial_transcript
            if transcript is None:
                return
            if len(transcript) == transcript.maxlen:
                self._serial_transcript_dropped += 1
            self._serial_transcript_sequence += 1
            record["sequence"] = self._serial_transcript_sequence
            transcript.append(record)

    def wait_for_wire_idle(
        self,
        timeout: float = 10.0,
        *,
        allow_peer_close: bool = False,
        progress_callback: Callable[[], None] | None = None,
        stable_checks: int = 1,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Wait until all already-received edge work has drained.

        This is a transport barrier, not a ROM-state barrier.  Pokémon can
        leave ``SC`` bit 7 armed while waiting for the next peer clock, so
        requiring the emulated register to look idle would deadlock a valid
        LinkMenu rendezvous.  The barrier instead waits for queued and
        currently handled ``EDGE_REQ`` frames, plus any outstanding local
        response, to finish within a bounded deadline.  ``allow_peer_close``
        is intended only for a caller that has already completed an
        application-level close marker and therefore expects the peer to
        tear down immediately.  When ``progress_callback`` is supplied, it
        is called outside the transport condition lock between bounded
        checks.  An emulator owner can use this to service queued inbound
        edges while the caller is waiting for wire idle; without it, a caller
        that owns the emulator thread can wait on work that only that same
        thread is allowed to apply. ``stable_checks`` requires that many
        consecutive idle observations separated by a bounded poll interval,
        allowing a caller to establish a quiet window around a ROM phase
        boundary rather than trusting a single gap between serial edges.
        Cancellation leaves this transport open because no wire marker was
        sent and the wait can be safely retried.
        """
        timeout = _require_timeout(timeout, "timeout")
        if isinstance(stable_checks, bool) or not isinstance(stable_checks, int):
            raise TypeError("stable_checks must be a positive integer")
        if stable_checks <= 0:
            raise ValueError("stable_checks must be a positive integer")
        deadline = time.monotonic() + timeout
        idle_checks = 0
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise NetworkBackendError("wire-idle wait cancelled")
            idle_observed = False
            with self._edge_pending_condition:
                with self._edge_response_lock:
                    edge_inflight = self._edge_inflight
                    response_pending = not self._resp_queue.empty()
                if self._edge_pending == 0 and not edge_inflight and not response_pending:
                    if self._closed:
                        if allow_peer_close and self._reader_exc is None:
                            return
                        if self._reader_exc is not None:
                            raise NetworkBackendError(
                                f"backend closed while waiting for wire idle: {self._reader_exc}"
                            ) from self._reader_exc
                        raise NetworkBackendError("backend closed while waiting for wire idle")
                    idle_observed = True
                    idle_checks += 1
                    if idle_checks >= stable_checks:
                        return
                else:
                    idle_checks = 0
                if self._closed:
                    if self._reader_exc is not None:
                        raise NetworkBackendError(
                            f"backend closed while waiting for wire idle: {self._reader_exc}"
                        ) from self._reader_exc
                    raise NetworkBackendError("backend closed while waiting for wire idle")
                if cancel_event is not None and cancel_event.is_set():
                    raise NetworkBackendError("wire-idle wait cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NetworkBackendError(
                        "wire did not become idle within "
                        f"{timeout:g}s (pending_edge_requests={self._edge_pending}, "
                        f"edge_inflight={edge_inflight}, "
                        f"response_pending={response_pending})"
                    )
                wait_time = min(0.05, remaining)
                if progress_callback is None:
                    self._edge_pending_condition.wait(timeout=wait_time)
                    continue
            # Never invoke arbitrary emulator-owner code while holding the
            # transport condition lock. The callback is deliberately outside
            # the lock so its owner-side serial dispatch can notify the
            # reader/response worker and let the next loop observe progress.
            progress_callback()
            if idle_observed and stable_checks > 1:
                # A sequence of immediate checks is not a quiet window: a
                # peer can enqueue its next EDGE_REQ between two Python
                # observations. Keep the owner callback outside the lock,
                # then wait for one bounded poll interval before counting the
                # next observation. Reader/worker notifications wake this
                # wait as soon as edge work appears or drains.
                with self._edge_pending_condition:
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        self._edge_pending_condition.wait(
                            timeout=min(_SEND_POLL_SECONDS, remaining)
                        )

    def _sync_queue(self, sync_id: int) -> queue.Queue[int]:
        sync_id = _validate_id(sync_id, "sync_id")
        # Make sure we have a queue ready before we send, so the
        # reader thread can deposit an incoming SYNC even if we
        # haven't started waiting yet.
        with self._sync_lock:
            return self._sync_queues.setdefault(
                sync_id, queue.Queue(maxsize=_CONTROL_QUEUE_MAXSIZE)
            )

    def _exchange_queue(self, kind_id: int) -> queue.Queue[bytes]:
        kind_id = _validate_id(kind_id, "kind_id")
        with self._exchange_lock:
            return self._exchange_queues.setdefault(
                kind_id, queue.Queue(maxsize=_CONTROL_QUEUE_MAXSIZE)
            )

    def _send_sync(
        self,
        sync_id: int,
        *,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self._send_sync_with_timeout(
            sync_id,
            timeout=_DEFAULT_SEND_TIMEOUT_SECONDS,
            cancel_event=cancel_event,
        )

    def _send_sync_with_timeout(
        self,
        sync_id: int,
        *,
        timeout: float,
        cancel_event: threading.Event | None = None,
    ) -> None:
        sync_id = _validate_id(sync_id, "sync_id")
        self._stats["sync_sent"] = int(self._stats["sync_sent"]) + 1
        try:
            with self._write_guard(
                timeout=timeout,
                operation=f"OP_SYNC({sync_id})",
                cancel_event=cancel_event,
            ) as write_deadline:
                self._send_frame(
                    _FRAME.pack(_OP_SYNC, sync_id),
                    timeout=max(0.0, write_deadline - time.monotonic()),
                    operation=f"OP_SYNC({sync_id})",
                    cancel_event=cancel_event,
                )
        except (OSError, NetworkBackendError) as exc:
            error = (
                exc
                if isinstance(exc, NetworkBackendError)
                else NetworkBackendError(f"failed to send OP_SYNC({sync_id}): {exc}")
            )
            self._mark_closed(error)
            raise error from exc

    def _send_hello(self, rom_version: str) -> None:
        payload = (_PROTOCOL_VERSION << 4) | _ROM_VERSION_CODES[rom_version]
        try:
            with self._write_guard(
                timeout=_DEFAULT_SEND_TIMEOUT_SECONDS,
                operation="HELLO",
            ) as write_deadline:
                self._send_frame(
                    _FRAME.pack(_OP_HELLO, payload),
                    timeout=max(0.0, write_deadline - time.monotonic()),
                    operation="HELLO",
                )
        except (OSError, NetworkBackendError) as exc:
            error = (
                exc
                if isinstance(exc, NetworkBackendError)
                else NetworkBackendError(f"failed to send HELLO: {exc}")
            )
            self._mark_closed(error)
            raise error from exc

    # --- lifecycle ----------------------------------------------------

    def _mark_closed(
        self,
        error: Exception | None = None,
        *,
        deadline: float | None = None,
    ) -> bool:
        """Publish terminal transport state without exceeding ``deadline``.

        Normal protocol failures still serialize terminal publication with
        owner dispatch.  Lifecycle teardown has a finite deadline, however:
        an owner may be inside native serial code while ``stop()`` needs to
        close the socket and wake its waiters.  If the owner lock cannot be
        acquired by that deadline, the close state is published through the
        close lock without claiming that owner quiescence completed.  The
        caller receives ``False`` and may retry after the owner operation
        releases the lock.
        """
        if deadline is None:
            with self._owner_dispatch_lock:
                self._mark_closed_locked(error)
            return True
        remaining = max(0.0, deadline - time.monotonic())
        if self._owner_dispatch_lock.acquire(timeout=remaining):
            try:
                self._mark_closed_locked(error)
            finally:
                self._owner_dispatch_lock.release()
            return True
        self._mark_closed_uncoordinated(error)
        return False

    def _mark_closed_locked(self, error: Exception | None = None) -> None:
        """Fail closed and wake all waiters after a transport error.

        ``on_edge`` can discover a stale response or a peer timeout while the
        reader thread is still blocked in ``recv``. Closing the socket here
        makes that state terminal and lets both the reader and edge worker
        unwind; a later call to :meth:`stop` remains idempotent.
        """
        self._mark_closed_common(error)

    def _mark_closed_uncoordinated(self, error: Exception | None = None) -> None:
        """Publish terminal transport state without waiting for owner work."""
        self._mark_closed_common(error)

    def _mark_closed_common(self, error: Exception | None = None) -> None:
        """Close shared transport state; caller may or may not own dispatch."""
        with self._close_lock:
            if self._closed:
                return
            if error is not None:
                self._reader_exc = error
            self._closed = True
            self._closed_event.set()
            self._hello_received.set()
            # Requests which have not produced a response cannot complete
            # after the transport is terminal. Clear the admitted-work
            # accounting now; worker finally blocks use the saturating helper
            # below so a racing worker cannot make it negative. Capture the
            # live values first so cleanup/status callers retain the wire
            # state that caused or accompanied closure.
            with self._edge_pending_condition, self._edge_response_lock:
                edge_inflight = self._edge_inflight
                response_pending = not self._resp_queue.empty()
                pre_close = dict(self._stats)
                pre_close["pending_edge_requests"] = self._edge_pending
                pre_close["edge_inflight"] = edge_inflight
                pre_close["response_pending"] = response_pending
                if self._reader_exc is None:
                    pre_close["reader_error"] = None
                else:
                    pre_close["reader_error"] = {
                        "type": type(self._reader_exc).__name__,
                        "message": str(self._reader_exc),
                    }
                last_keepalive = pre_close.get("last_keepalive_state")
                if isinstance(last_keepalive, dict):
                    pre_close["last_keepalive_state"] = dict(last_keepalive)
                self._pre_close_snapshot = pre_close
                self._edge_pending = 0
                self._edge_pending_condition.notify_all()
            self._signal_edge_worker_stop()
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass

    def close(self, *, timeout_s: float = 2.0) -> bool:
        return self.stop(timeout_s=timeout_s)

    def detach_local_core(self, *, timeout_s: float = 2.0) -> bool:
        """Detach emulator-owned references after bounded quiescence.

        This is the lifecycle boundary used when a session restores a PyBoy
        serial backend while the network transport itself may remain alive
        until a later terminal cleanup.  It clears the local serial core,
        IRQ callback, and transcript-context provider only after all admitted
        worker/owner operations have returned.  A timeout returns ``False``
        and leaves every reference intact, so the caller can release its
        emulator-side operation and retry without losing ownership state.

        The transport is intentionally not stopped here. Once successful,
        incoming edges are rejected until a guarded ``start_receiver``
        rebinding publishes a new core; a peer edge during that detached
        window closes the transport to avoid cross-session ambiguity. The
        owning session should stop the backend when its transport lifecycle
        ends if it does not rebind.
        """
        timeout_s = _require_timeout(timeout_s, "timeout_s")
        deadline = time.monotonic() + timeout_s

        # Serialize this transition with start_receiver publication.  The
        # lock is acquired with the same total deadline as the quiescence
        # wait, making a concurrent receiver setup bounded and retryable.
        remaining = max(0.0, deadline - time.monotonic())
        if not self._receiver_start_lock.acquire(timeout=remaining):
            return False
        try:
            with self._local_core_condition:
                if self._local_core_detached:
                    # A caller may stage transcript context while detached
                    # before attempting a guarded same-transport rebind.
                    # If that rebind fails, cleanup is intentionally
                    # idempotent; it must still discard every staged
                    # emulator-owned closure rather than retaining the old
                    # session through a second detach call.
                    self._local_core = None
                    self._irq_callback = None
                    self._serial_transcript_context_provider = None
                    self._serial_transcript_context_max_bytes = 1024
                    return True
                self._local_core_detaching = True
                while self._local_core_users:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._local_core_detaching = False
                        self._local_core_condition.notify_all()
                        return False
                    self._local_core_condition.wait(timeout=remaining)

                # No admitted operation can retain any of these references
                # past this point.  Keep the transport's reader/response
                # worker objects alive, but make their next core admission
                # fail closed without exposing the detached emulator.
                self._local_core = None
                self._irq_callback = None
                self._serial_transcript_context_provider = None
                self._serial_transcript_context_max_bytes = 1024
                self._local_core_detached = True
                self._local_core_detaching = False
                self._local_core_condition.notify_all()
                return True
        finally:
            self._receiver_start_lock.release()

    def stop(self, *, timeout_s: float = 2.0) -> bool:
        timeout_s = _require_timeout(timeout_s, "timeout_s")
        deadline = time.monotonic() + timeout_s
        # Serialize lifecycle teardown with receiver setup so stop cannot
        # observe a half-published worker pair and return before the newly
        # started threads are signalled and joined.
        remaining = max(0.0, deadline - time.monotonic())
        if not self._receiver_start_lock.acquire(timeout=remaining):
            # A concurrent bounded detach/rebind owns this lock.  Do not
            # turn stop's finite timeout into an unbounded wait for that
            # lifecycle operation; the caller can retry after it releases
            # the lock, just as it retries after an admitted owner edge.
            return False
        try:
            # Wake the mode-specific edge worker and close the socket before
            # joining either thread. In owner-dispatch mode queued requests
            # are terminal emulator work and must not be applied after close;
            # _mark_closed clears their pending accounting.
            closed_coordinated = self._mark_closed(deadline=deadline)
            edge_worker = self._edge_worker
            reader = self._reader
        finally:
            self._receiver_start_lock.release()
        if (
            isinstance(edge_worker, threading.Thread)
            and edge_worker is not threading.current_thread()
        ):
            edge_worker.join(timeout=max(0.0, deadline - time.monotonic()))

        if isinstance(reader, threading.Thread) and reader is not threading.current_thread():
            reader.join(timeout=max(0.0, deadline - time.monotonic()))
        workers_stopped = not any(
            isinstance(worker, threading.Thread)
            and worker is not threading.current_thread()
            and worker.is_alive()
            for worker in (edge_worker, reader)
        )
        return closed_coordinated and workers_stopped

    # --- internals ----------------------------------------------------

    def _reader_loop(self) -> None:
        try:
            while not self._closed:
                frame = self._recv_exactly(2)
                opcode, payload = _FRAME.unpack(frame)
                if opcode == _OP_HELLO:
                    protocol = payload >> 4
                    version_code = payload & 0x0F
                    if protocol != _PROTOCOL_VERSION:
                        raise NetworkBackendError(
                            f"unsupported NetworkBackend protocol {protocol}; "
                            f"expected {_PROTOCOL_VERSION}"
                        )
                    peer_version = _ROM_VERSION_NAMES.get(version_code)
                    if peer_version is None:
                        raise NetworkBackendError(
                            f"unsupported peer ROM version code {version_code}"
                        )
                    if self._peer_rom_version is not None:
                        raise NetworkBackendError("duplicate peer HELLO")
                    self._peer_rom_version = peer_version
                    self._hello_received.set()
                elif opcode == _OP_EDGE_REQ:
                    if payload > 1:
                        raise NetworkBackendError(f"invalid EDGE_REQ bit payload {payload}")
                    # A detached transport has no emulator owner to service
                    # an incoming edge. Fail closed rather than queueing work
                    # that could be mistaken for the next session after a
                    # same-transport rebind.
                    with self._local_core_condition:
                        if self._local_core_detaching or self._local_core_detached:
                            raise NetworkBackendError(
                                "received EDGE_REQ while local serial core is detached"
                            )
                        with self._edge_pending_condition:
                            if self._closed:
                                continue
                            self._edge_pending += 1
                            self._edge_pending_condition.notify_all()
                    request = _InboundEdge(payload & 1)
                    self._record_serial_event(
                        "edge_req_received",
                        direction="peer_to_local",
                        edge_bit=payload & 1,
                    )
                    try:
                        self._edge_queue.put_nowait(request)
                    except queue.Full as exc:
                        with self._edge_pending_condition:
                            # _mark_closed() can clear the admitted-work
                            # count while this queue operation is racing
                            # teardown.  Keep the live diagnostic invariant
                            # non-negative on the queue-full path as well as
                            # in worker finalizers.
                            if self._edge_pending > 0:
                                self._edge_pending -= 1
                            else:
                                self._edge_pending = 0
                            self._edge_pending_condition.notify_all()
                        raise NetworkBackendError("incoming EDGE_REQ queue is full") from exc
                elif opcode == _OP_EDGE_RESP:
                    if payload > 1:
                        raise NetworkBackendError(f"invalid EDGE_RESP bit payload {payload}")
                    with self._edge_response_lock:
                        if not self._edge_inflight:
                            raise NetworkBackendError("unsolicited EDGE_RESP")
                        try:
                            self._resp_queue.put_nowait(payload & 1)
                        except queue.Full as exc:
                            raise NetworkBackendError("duplicate or unsolicited EDGE_RESP") from exc
                    self._record_serial_event(
                        "edge_resp_received",
                        direction="peer_to_local",
                        edge_bit=payload & 1,
                    )
                elif opcode == _OP_FRAME_TICK:
                    if payload != 0:
                        raise NetworkBackendError("invalid FRAME_TICK payload")
                    try:
                        self._frame_tick_queue.put_nowait(None)
                    except queue.Full as exc:
                        raise NetworkBackendError("duplicate FRAME_TICK") from exc
                    self._stats["frame_ticks_received"] = int(
                        self._stats["frame_ticks_received"]
                    ) + 1
                    with self._edge_pending_condition:
                        self._edge_pending_condition.notify_all()
                elif opcode == _OP_FRAME_DONE:
                    if payload != 0:
                        raise NetworkBackendError("invalid FRAME_DONE payload")
                    try:
                        self._frame_done_queue.put_nowait(None)
                    except queue.Full as exc:
                        raise NetworkBackendError("duplicate FRAME_DONE") from exc
                    self._stats["frame_dones_received"] = int(
                        self._stats["frame_dones_received"]
                    ) + 1
                    with self._edge_pending_condition:
                        self._edge_pending_condition.notify_all()
                elif opcode == _OP_FRAME_ACK:
                    if payload != 0:
                        raise NetworkBackendError("invalid FRAME_ACK payload")
                    with self._frame_turn_lock:
                        if not self._leader_frame_inflight:
                            raise NetworkBackendError("unsolicited FRAME_ACK")
                    try:
                        self._frame_ack_queue.put_nowait(None)
                    except queue.Full as exc:
                        raise NetworkBackendError("duplicate FRAME_ACK") from exc
                    self._stats["frame_acks_received"] = int(
                        self._stats["frame_acks_received"]
                    ) + 1
                elif opcode == _OP_SYNC:
                    with self._sync_lock:
                        if payload in self._sync_pending:
                            raise NetworkBackendError(f"duplicate pending OP_SYNC({payload})")
                        q = self._sync_queues.setdefault(
                            payload, queue.Queue(maxsize=_CONTROL_QUEUE_MAXSIZE)
                        )
                        try:
                            q.put_nowait(payload)
                        except queue.Full as exc:
                            raise NetworkBackendError(f"OP_SYNC({payload}) queue is full") from exc
                        self._sync_pending.add(payload)
                    self._stats["sync_received"] = int(self._stats["sync_received"]) + 1
                elif opcode == _OP_EXCHANGE:
                    raw_len = self._recv_exactly(2)
                    (length,) = _LEN.unpack(raw_len)
                    data = self._recv_exactly(length)
                    self._stats["exchange_received"] = int(self._stats["exchange_received"]) + 1
                    with self._exchange_lock:
                        q = self._exchange_queues.setdefault(
                            payload, queue.Queue(maxsize=_CONTROL_QUEUE_MAXSIZE)
                        )
                    try:
                        q.put_nowait(data)
                    except queue.Full as exc:
                        raise NetworkBackendError(f"OP_EXCHANGE({payload}) queue is full") from exc
                else:
                    raise NetworkBackendError(f"unknown NetworkBackend opcode 0x{opcode:02x}")
        except NetworkBackendError as exc:
            # EOF exactly on a frame boundary is an orderly peer shutdown.
            # Keep it distinguishable from a truncated frame so a lifecycle
            # barrier can accept the former without masking protocol damage.
            if str(exc) == "peer closed socket":
                self._mark_closed()
            else:
                self._mark_closed(exc)
        except OSError as exc:
            self._mark_closed(exc)
        except Exception as exc:  # noqa: BLE001
            # Core/backend failures must reach blocked callers through the
            # same bounded error path as socket failures.  A bare reader
            # thread exception otherwise leaves the emulator waiting until a
            # long exchange timeout expires.
            self._mark_closed(exc)

    def _edge_worker_loop(self) -> None:
        """Compatibility worker for direct low-level backend callers.

        PyBoy sessions use :meth:`service_pending_edges` instead. Keeping
        this worker preserves the historical ``start_receiver`` behavior for
        callers that provide a standalone serial-core double; all operations
        in this path are still serialized by ``_serial_gate``.
        """
        while not self._closed:
            try:
                request = self._edge_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if request is None:
                return
            try:
                if self._closed:
                    return
                self._handle_edge_req(request.peer_bit)
            except Exception as exc:  # noqa: BLE001
                self._mark_closed(exc)
                return
            finally:
                self._decrement_edge_pending()

    def _owner_response_worker_loop(self) -> None:
        """Send responses produced by the emulator-owner dispatch path.

        This thread is deliberately transport-only. It never reads or
        writes the local serial core, CPU, or emulator memory; the owner
        thread has already completed those operations before a request is
        placed on ``_completed_edge_queue``.
        """
        while not self._closed:
            try:
                request = self._completed_edge_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if request is None:
                return
            try:
                if not self._closed:
                    self._send_edge_response(request)
            except Exception as exc:  # noqa: BLE001
                self._mark_closed(exc)
                return
            finally:
                self._decrement_edge_pending()

    def service_pending_edges(self, *, max_edges: int | None = None) -> int:
        """Apply queued peer edges on the caller's emulator-owner thread.

        The method is only valid after ``start_receiver(...,
        dispatch_to_owner=True)``. It can wait for the serial gate and
        lifecycle lock, but does not wait for slave rearming: an
        unarmed slave request is put back on the queue and the caller returns
        to its PyBoy tick so the ROM can execute its normal SB/SC re-arm
        sequence. A later owner boundary retries the same request. If the
        owner never services it, the master's existing bounded EDGE_RESP
        deadline fails closed; no synthetic bit is emitted.

        ``max_edges`` bounds work per owner boundary. ``None`` drains all
        requests that are currently ready. The return value is the number of
        requests applied, not the number merely observed or deferred.
        """
        if not self._dispatch_to_owner:
            raise NetworkBackendError("service_pending_edges requires dispatch_to_owner=True")
        if max_edges is not None:
            if isinstance(max_edges, bool) or not isinstance(max_edges, int):
                raise TypeError("max_edges must be a positive integer or None")
            if max_edges <= 0:
                raise ValueError("max_edges must be a positive integer or None")

        # Wait for the externally-owned serial gate before admitting a core
        # operation.  A lifecycle detach is called under that same gate by
        # the owner, so a dispatcher queued behind it must not keep the core
        # admission count non-zero and force detach to time out. Once the
        # gate is held, admission and native application are one critical
        # section; a dispatcher which wakes after a successful detach fails
        # closed before it can read the cleared reference.
        # Serialize dequeue as well as application: a competing dispatcher
        # must not take an older request and then wait while the tick owner
        # applies a later one. This matches the tick wrapper's lock order.
        with self._serial_gate, self._local_core_access():
            return self._service_pending_edges_locked(max_edges=max_edges)

    def _service_pending_edges_locked(self, *, max_edges: int | None) -> int:
        """Drain ready requests while the caller holds the serial gate."""
        applied = 0
        while max_edges is None or applied < max_edges:
            try:
                request = self._edge_queue.get_nowait()
            except queue.Empty:
                break
            if request is None:
                # The stop sentinel belongs to the response worker in owner
                # mode. Treating it as terminal here makes a late owner poll
                # harmless without altering the pending count.
                break
            # Admission and native application share the lifecycle lock with
            # _mark_closed(). If close wins, this request is discarded; if
            # owner admission wins, close waits until the native operation
            # has returned before publishing the terminal state.
            # The caller already holds the serial gate. Close never acquires
            # that gate, and owner dispatch is held only for this request.
            with self._owner_dispatch_lock:
                if self._closed:
                    self._decrement_edge_pending()
                    break
                try:
                    ready = self._apply_owner_edge_if_ready(request)
                except BaseException as exc:
                    request.error = exc
                    self._stats["owner_edge_errors"] = int(self._stats["owner_edge_errors"]) + 1
                    # The eighth edge may already have committed native SB/SC
                    # before its IRQ callback fails. Preserve that prefix,
                    # but latch the failure so later native ticks/MMIO/state
                    # calls cannot silently continue with a lost interrupt.
                    try:
                        latch_error = getattr(self._local_core, "latch_backend_error", None)
                        if callable(latch_error):
                            latch_error(exc)
                    except BaseException as latch_error:  # noqa: BLE001
                        exc.add_note(f"serial failure latching also failed: {latch_error!r}")
                    error = NetworkBackendError(
                        f"owner failed to apply incoming EDGE_REQ: {exc}"
                    )
                    self._mark_closed(error)
                    self._decrement_edge_pending()
                    raise error from exc
                if self._closed:
                    # A deadline-aware stop may publish terminal state while
                    # this owner operation was inside native code. Do not
                    # enqueue a response for a worker that has already been
                    # cancelled; release the admitted edge accounting here.
                    self._decrement_edge_pending()
                    break
                if not ready:
                    # There is at most one in-flight master edge per peer, but
                    # preserving FIFO here also makes malformed/busy callers
                    # deterministic. The queue restores a deferred request at
                    # its head. A malformed peer may refill the freed slot
                    # meanwhile; fail closed instead of blocking or losing it.
                    request.deferred = True
                    self._stats["owner_edge_deferred"] = int(self._stats["owner_edge_deferred"]) + 1
                    try:
                        self._edge_queue.put_nowait(request)
                    except queue.Full as exc:
                        error = NetworkBackendError(
                            "incoming EDGE_REQ queue is full while deferring owner request"
                        )
                        self._mark_closed(error)
                        self._decrement_edge_pending()
                        raise error from exc
                    break
                self._stats["owner_edge_applied"] = int(self._stats["owner_edge_applied"]) + 1
                try:
                    self._completed_edge_queue.put_nowait(request)
                except queue.Full as exc:
                    self._mark_closed(
                        NetworkBackendError("completed EDGE_REQ response queue is full")
                    )
                    self._decrement_edge_pending()
                    raise NetworkBackendError("completed EDGE_REQ response queue is full") from exc
                applied += 1
        return applied

    def _apply_owner_edge_if_ready(self, request: _InboundEdge) -> bool:
        """Apply one edge atomically if the owner core is armed.

        The readiness check and the native calls share one critical section;
        a concurrent lifecycle or emulator operation therefore cannot change
        the serial role between the check and ``apply_external_edge``.
        """
        # ``service_pending_edges`` owns the admission barrier for this
        # dispatch. Keeping this helper single-admission is important: a
        # detach may publish its freeze flag while the outer call is still
        # active, and a nested admission would reject the already-admitted
        # operation instead of allowing it to quiesce.
        core = self._local_core
        if core is None:
            raise NetworkBackendError("owner dispatch requires a local serial core")
        with self._serial_gate:
            transfer_enabled = bool(getattr(core, "transfer_enabled", 0))
            internal_clock = bool(getattr(core, "internal_clock", 0))
            if internal_clock:
                # The ROM can switch clock source while an EDGE_REQ from
                # the previous role is already queued. The existing
                # serial protocol uses the connected/no-data byte for
                # this brief transition; emit its next bit without
                # touching the native core rather than closing the link
                # or waiting for a role that cannot service the request
                # while it is internal-clock.
                self._apply_owner_keepalive(request, core)
                return True
            if not transfer_enabled:
                self._record_serial_event(
                    "owner_edge_deferred",
                    direction="peer_to_local",
                    edge_bit=request.peer_bit & 1,
                    reason="local_core_unarmed",
                    local_state_before=(
                        self._core_state_snapshot(core)
                        if self._serial_transcript is not None
                        else None
                    ),
                )
                return False
            self._apply_owner_edge(request, core)
            return True

    def _apply_owner_keepalive(self, request: _InboundEdge, core: object) -> None:
        """Prepare one no-data response for a transient internal-clock edge."""
        state_before = (
            self._core_state_snapshot(core) if self._serial_transcript is not None else None
        )
        self._stats["edge_req_received"] = int(self._stats["edge_req_received"]) + 1
        now = time.monotonic()
        if now < self._active_exchange_until:
            raise NetworkBackendError(
                "received EDGE_REQ while local serial core is internal-clock "
                "during an active exchange"
            )
        if self._keepalive_bit_idx == 0:
            self._stats["keepalive_bytes_started"] = int(self._stats["keepalive_bytes_started"]) + 1
        self._stats["keepalive_bits_sent"] = int(self._stats["keepalive_bits_sent"]) + 1
        self._stats["last_keepalive_state"] = self._core_state_snapshot(core)
        request.response_bit = 0 if self._keepalive_bit_idx == 7 else 1
        request.completed = False
        self._keepalive_bit_idx = (self._keepalive_bit_idx + 1) & 7
        self._record_serial_event(
            "owner_keepalive_applied",
            direction="peer_to_local",
            edge_bit=request.peer_bit & 1,
            response_bit=request.response_bit,
            byte_complete=False,
            local_state_before=state_before,
            local_state_after=(
                self._core_state_snapshot(core) if state_before is not None else None
            ),
        )

    def _apply_owner_edge(self, request: _InboundEdge, core: object) -> None:
        """Perform one authentic external edge under the shared gate."""
        if not bool(getattr(core, "transfer_enabled", 0)):
            raise NetworkBackendError("serial core became unarmed before EDGE_REQ dispatch")
        if bool(getattr(core, "internal_clock", 0)):
            raise NetworkBackendError("serial core became internal-clock before EDGE_REQ dispatch")
        state_before = (
            self._core_state_snapshot(core) if self._serial_transcript is not None else None
        )
        our_bit = int(core.peek_out_bit()) & 1
        completed = bool(core.apply_external_edge(request.peer_bit & 1))
        request.response_bit = our_bit
        request.completed = completed
        completion_context = self._serial_completion_context() if completed else None
        # A real external edge ends any transient 0xFE keep-alive stream.
        # The next internal-clock transition must restart at the first bit of
        # SERIAL_NO_DATA_BYTE rather than continuing from the old byte index.
        self._keepalive_bit_idx = 0
        self._stats["edge_req_received"] = int(self._stats["edge_req_received"]) + 1
        self._stats["slave_armed_edges"] = int(self._stats["slave_armed_edges"]) + 1
        self._record_serial_event(
            "owner_edge_applied",
            direction="peer_to_local",
            edge_bit=request.peer_bit & 1,
            response_bit=our_bit,
            byte_complete=completed,
            completion_context=completion_context,
            local_state_before=state_before,
            local_state_after=(
                self._core_state_snapshot(core) if state_before is not None else None
            ),
        )
        if completed:
            self._stats["last_slave_byte_complete_at"] = time.monotonic()
            self._notify_completed_slave_irq(isolate_callback_errors=False)

    def _notify_completed_slave_irq(self, *, isolate_callback_errors: bool) -> None:
        """Run the completion callback under local-core admission."""
        with self._local_core_access(allow_none=True):
            self._notify_completed_slave_irq_impl(
                isolate_callback_errors=isolate_callback_errors
            )

    def _notify_completed_slave_irq_impl(self, *, isolate_callback_errors: bool) -> None:
        """Expose the serial IRQ caused by an already-completed eighth edge.

        Hardware completion (SB latch plus SC bit-7 clear) and the IRQ are
        consequences of the same external clock edge. The transport response
        may release the remote master later, but must never delay this local
        event. Owner dispatch propagates a callback error because it runs on
        the emulator owner; the legacy worker preserves its historical
        callback-error isolation.
        """
        if self._irq_callback is None:
            self._record_serial_event(
                "irq_callback",
                direction="local",
                byte_complete=True,
                outcome="not_configured",
            )
            return
        self._stats["irq_callbacks"] = int(self._stats["irq_callbacks"]) + 1
        try:
            self._irq_callback()
        except Exception as exc:
            if isolate_callback_errors:
                self._stats["irq_callback_errors"] = int(
                    self._stats["irq_callback_errors"]
                ) + 1
                self._stats["last_irq_callback_error"] = type(exc).__name__
            self._record_serial_event(
                "irq_callback",
                direction="local",
                byte_complete=True,
                outcome="error",
                error_type=type(exc).__name__,
            )
            if not isolate_callback_errors:
                raise
        else:
            self._record_serial_event(
                "irq_callback",
                direction="local",
                byte_complete=True,
                outcome="success",
            )

    def _serial_completion_context(self) -> dict[str, object] | None:
        """Capture completion context while retaining the local-core lease."""
        with self._local_core_access(allow_none=True):
            return self._serial_completion_context_impl()

    def _serial_completion_context_impl(self) -> dict[str, object] | None:
        """Capture a bounded, JSON-safe owner diagnostic without side effects."""
        if self._serial_transcript is None:
            return None
        provider = self._serial_transcript_context_provider
        if provider is None:
            return None
        try:
            raw = provider()
        except BaseException as exc:  # noqa: BLE001 - diagnostics must not break a link.
            return {"status": "provider_error", "error_type": type(exc).__name__}
        if not isinstance(raw, dict):
            return {"status": "invalid_result"}
        try:
            copied = json.loads(json.dumps(raw, sort_keys=True, separators=(",", ":")))
        except (TypeError, ValueError, OverflowError):
            return {"status": "invalid_result"}
        value: dict[str, object] = {}
        truncated = False
        for key in sorted(copied):
            candidate = dict(value)
            candidate[key] = copied[key]
            encoded = json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode("utf-8")
            if len(encoded) <= self._serial_transcript_context_max_bytes:
                value = candidate
            else:
                truncated = True
        return {"status": "ok", "truncated": truncated, "value": value}

    def _send_edge_response(self, request: _InboundEdge) -> None:
        """Send an owner-produced response without touching emulator state."""
        if request.error is not None:
            raise NetworkBackendError(
                f"incoming EDGE_REQ failed: {request.error}"
            ) from request.error
        if request.response_bit not in (0, 1):
            raise NetworkBackendError("owner produced an invalid EDGE_RESP bit")
        try:
            with self._write_guard(
                timeout=_EDGE_RESPONSE_TIMEOUT_SECONDS,
                operation="EDGE_RESP",
            ) as write_deadline:
                if self._closed:
                    self._record_serial_event(
                        "edge_resp_send",
                        direction="local_to_peer",
                        edge_bit=request.response_bit,
                        byte_complete=request.completed,
                        outcome="not_sent_closed",
                    )
                    return
                self._send_frame(
                    _FRAME.pack(_OP_EDGE_RESP, request.response_bit),
                    timeout=max(0.0, write_deadline - time.monotonic()),
                    operation="EDGE_RESP",
                )
                self._stats["edge_resp_sent"] = int(self._stats["edge_resp_sent"]) + 1
        except (OSError, NetworkBackendError) as exc:
            self._record_serial_event(
                "edge_resp_send",
                direction="local_to_peer",
                edge_bit=request.response_bit,
                byte_complete=request.completed,
                outcome="error",
                error_type=type(exc).__name__,
            )
            raise
        self._record_serial_event(
            "edge_resp_send",
            direction="local_to_peer",
            edge_bit=request.response_bit,
            byte_complete=request.completed,
            outcome="success",
        )

    def _signal_edge_worker_stop(self) -> None:
        target_queue = self._completed_edge_queue if self._dispatch_to_owner else self._edge_queue
        try:
            target_queue.put_nowait(None)
        except queue.Full:
            pass

    def _decrement_edge_pending(self) -> None:
        """Release one admitted edge without allowing close races to underflow."""
        with self._edge_pending_condition:
            if self._edge_pending > 0:
                self._edge_pending -= 1
            else:
                self._edge_pending = 0
            self._edge_pending_condition.notify_all()

    def _handle_edge_req(self, peer_bit: int) -> None:
        """Run one compatibility-worker edge under core admission."""
        # A legacy receiver with no local core intentionally emits the
        # historical keep-alive stream. It still enters the admission barrier
        # so detach cannot race the worker's phase bookkeeping.
        with self._local_core_access(allow_none=True):
            self._handle_edge_req_impl(peer_bit)

    def _handle_edge_req_impl(self, peer_bit: int) -> None:
        """Peer is master, we are slave. Apply edge to local_core,
        respond with our bit.

        When the local core transiently isn't armed (e.g. the ROM's
        serial IRQ handler is in the middle of reading SB and
        re-arming the slave with a new SB+SC, which takes a handful
        of CPU cycles), briefly poll for re-arm rather than
        immediately responding with keep-alive bytes. Keep-alive
        (0xFE) is a valid "no data" signal but it's not the right
        response mid-stream inside a fixed-length block exchange
        like ``Serial_ExchangeBytes`` — the peer would receive 0xFE
        in place of real payload data and desync.

        If the re-arm wait expires without the local core becoming
        armed, fall back to the keep-alive stream.
        """
        # The public wrapper holds ``_local_core_access`` for this entire
        # method, including the bounded re-arm wait and serial gate section.
        # Detach therefore cannot clear the reference while this worker is
        # between readiness checks and native calls.
        core = self._local_core
        self._stats["edge_req_received"] = int(self._stats["edge_req_received"]) + 1

        def armed() -> bool:
            return (
                core is not None
                and getattr(core, "transfer_enabled", 0)
                and not getattr(core, "internal_clock", 0)
            )

        # Capture these phase flags before the readiness check.  The core can
        # legitimately finish a transfer between the first ``armed()`` call
        # and the final check under ``_serial_gate``; the fallback path still
        # needs a defined phase classification in that race.
        phase_now = time.monotonic()
        active_exchange = phase_now < self._active_exchange_until
        post_byte_rearm = phase_now < self._post_byte_rearm_until

        if not armed():
            self._stats["slave_rearm_waits"] = int(self._stats["slave_rearm_waits"]) + 1
            now = time.monotonic()
            active_exchange = now < self._active_exchange_until
            post_byte_rearm = now < self._post_byte_rearm_until
            if post_byte_rearm:
                self._stats["slave_post_byte_rearm_waits"] = (
                    int(self._stats["slave_post_byte_rearm_waits"]) + 1
                )
            if active_exchange:
                self._stats["slave_active_rearm_waits"] = (
                    int(self._stats["slave_active_rearm_waits"]) + 1
                )
            else:
                self._stats["slave_idle_rearm_waits"] = (
                    int(self._stats["slave_idle_rearm_waits"]) + 1
                )
            # Spin-wait with short sleeps. Deadline sized to cover
            # the slowest realistic re-arm window in pokered (serial
            # IRQ handler → SB/SC re-arm ≲ 100 CPU cycles of game
            # code, but in non-Cython Python-threaded subprocesses
            # the host-wall-clock gap between the peer's REQ and our
            # ROM rearming can stretch to 50+ ms when the peer's
            # subprocess is getting more CPU share). Longer than the
            # deadline and we fall back to keep-alive rather than
            # stalling the peer's on_edge forever.
            deadline = time.monotonic() + _REARM_WAIT_SECONDS
            if post_byte_rearm:
                # A completed byte can be followed by a short IRQ-handler
                # re-arm gap. Preserve the existing bounded grace behavior
                # for that transition; unlike an established long exchange,
                # an idle/post-byte miss may still use keep-alive.
                deadline = max(deadline, self._post_byte_rearm_until)
            if active_exchange:
                # A recent run of real slave edges means the peer is in a
                # fixed-length serial exchange. Returning SERIAL_NO_DATA in
                # that window is not a harmless idle signal: it becomes
                # payload and shifts every subsequent byte out of phase.
                # Give the peer's main emulator thread a bounded, generous
                # re-arm window instead. If it still cannot re-arm, the
                # worker fails the connection below so the master receives a
                # deterministic protocol error rather than corrupted data.
                deadline = max(
                    deadline,
                    time.monotonic() + _ACTIVE_EXCHANGE_REARM_WAIT_SECONDS,
                )
            while time.monotonic() < deadline and not self._closed_event.is_set():
                if armed():
                    self._stats["slave_rearm_successes"] = (
                        int(self._stats["slave_rearm_successes"]) + 1
                    )
                    if post_byte_rearm:
                        self._stats["slave_post_byte_rearm_successes"] = (
                            int(self._stats["slave_post_byte_rearm_successes"]) + 1
                        )
                        self._post_byte_rearm_until = 0.0
                    self._stats["last_slave_rearm_at"] = time.monotonic()
                    break
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    # Event.wait is both a short polling interval and an
                    # immediate cancellation point for stop()/peer EOF.
                    self._closed_event.wait(timeout=min(0.0005, remaining))

        with self._serial_gate:
            if armed():
                state_before = (
                    self._core_state_snapshot(core) if self._serial_transcript is not None else None
                )
                slave_armed_edges = int(self._stats["slave_armed_edges"]) + 1
                self._stats["slave_armed_edges"] = slave_armed_edges
                self._consecutive_armed_edges += 1
                if slave_armed_edges >= _ACTIVE_EXCHANGE_EDGE_THRESHOLD:
                    self._active_exchange_until = time.monotonic() + _ACTIVE_EXCHANGE_GRACE_SECONDS
                our_bit = core.peek_out_bit()
                completed = core.apply_external_edge(peer_bit)
                completion_context = self._serial_completion_context() if completed else None
                if completed:
                    self._stats["last_slave_byte_complete_at"] = time.monotonic()
                    self._post_byte_rearm_until = time.monotonic() + _POST_BYTE_REARM_GRACE_SECONDS
                # Reset keep-alive counter so the next idle stretch starts
                # fresh at the top of a 0xFE byte boundary rather than
                # mid-byte.
                self._keepalive_bit_idx = 0
                self._record_serial_event(
                    "worker_edge_applied",
                    direction="peer_to_local",
                    edge_bit=peer_bit & 1,
                    response_bit=our_bit & 1,
                    byte_complete=completed,
                    completion_context=completion_context,
                    local_state_before=state_before,
                    local_state_after=(
                        self._core_state_snapshot(core) if state_before is not None else None
                    ),
                )
            else:
                if active_exchange:
                    raise NetworkBackendError("slave did not re-arm during active serial exchange")
                self._consecutive_armed_edges = 0
                if post_byte_rearm:
                    self._stats["keepalive_after_post_byte_waits"] = (
                        int(self._stats["keepalive_after_post_byte_waits"]) + 1
                    )
                # Still not armed after the re-arm wait — stream the bits
                # of SERIAL_NO_DATA_BYTE (0xFE) MSB-first. Wraps every 8
                # edges so successive idle bytes all come out as 0xFE.
                # First 7 bits are 1, last is 0.
                if self._keepalive_bit_idx == 0:
                    self._stats["keepalive_bytes_started"] = (
                        int(self._stats["keepalive_bytes_started"]) + 1
                    )
                self._stats["keepalive_bits_sent"] = int(self._stats["keepalive_bits_sent"]) + 1
                keepalive_state = (
                    self._core_state_snapshot(core) if self._serial_transcript is not None else None
                )
                self._stats["last_keepalive_state"] = self._core_state_snapshot(core)
                our_bit = 0 if self._keepalive_bit_idx == 7 else 1
                self._keepalive_bit_idx = (self._keepalive_bit_idx + 1) & 7
                completed = False
                self._record_serial_event(
                    "worker_keepalive_applied",
                    direction="peer_to_local",
                    edge_bit=peer_bit & 1,
                    response_bit=our_bit,
                    byte_complete=False,
                    local_state_before=keepalive_state,
                    local_state_after=(
                        self._core_state_snapshot(core) if keepalive_state is not None else None
                    ),
                )
        # The completed edge is local hardware state; EDGE_RESP only releases
        # the remote master and can block or fail independently.
        if completed:
            self._notify_completed_slave_irq(isolate_callback_errors=True)
        try:
            with self._write_guard(
                timeout=_EDGE_RESPONSE_TIMEOUT_SECONDS,
                operation="EDGE_RESP",
            ) as write_deadline:
                if not self._closed:
                    self._send_frame(
                        _FRAME.pack(_OP_EDGE_RESP, our_bit & 1),
                        timeout=max(0.0, write_deadline - time.monotonic()),
                        operation="EDGE_RESP",
                    )
                    self._stats["edge_resp_sent"] = int(self._stats["edge_resp_sent"]) + 1
        except (OSError, NetworkBackendError) as exc:
            self._record_serial_event(
                "edge_resp_send",
                direction="local_to_peer",
                edge_bit=our_bit & 1,
                byte_complete=completed,
                outcome="error",
                error_type=type(exc).__name__,
            )
            self._mark_closed()
            return
        self._record_serial_event(
            "edge_resp_send",
            direction="local_to_peer",
            edge_bit=our_bit & 1,
            byte_complete=completed,
            outcome="success" if not self._closed else "not_sent_closed",
        )

    @staticmethod
    def _core_state_snapshot(core: object | None) -> dict[str, object]:
        if core is None:
            return {"core_present": False}
        snap: dict[str, object] = {
            "core_present": True,
            "type": type(core).__name__,
        }
        for attr in (
            "transfer_enabled",
            "internal_clock",
            "SB",
            "SC",
            "clock",
            "last_cycles",
        ):
            if hasattr(core, attr):
                value = getattr(core, attr)
                if isinstance(value, bool):
                    value = int(value)
                snap[attr] = value
        return snap

    def _recv_exactly(self, n: int) -> bytes:
        """Read one frame fragment with a bounded partial-frame deadline.

        An idle link is valid, so the first byte has no protocol timeout.
        Once a fragment arrives, the remaining bytes must arrive within a
        bounded interval. Short select polls let ``stop()`` interrupt a
        reader waiting for either the first byte or the remainder.
        """
        if n < 0:
            raise ValueError(f"read length must be non-negative, got {n}")
        buf = bytearray()
        partial_deadline: float | None = None
        while len(buf) < n:
            if self._closed_event.is_set():
                raise NetworkBackendError("backend closed")
            if partial_deadline is None:
                wait_timeout = _READ_POLL_SECONDS
            else:
                remaining = partial_deadline - time.monotonic()
                if remaining <= 0:
                    raise NetworkBackendError(
                        "peer frame timed out while waiting for remaining bytes"
                    )
                wait_timeout = min(_READ_POLL_SECONDS, remaining)
            try:
                readable, _writable, exceptional = select.select(
                    [self._sock], [], [self._sock], wait_timeout
                )
            except InterruptedError:
                continue
            except (OSError, ValueError) as exc:
                if self._closed_event.is_set():
                    raise NetworkBackendError("backend closed") from exc
                raise NetworkBackendError(
                    f"failed to poll socket while reading frame: {exc}"
                ) from exc
            if exceptional and not readable:
                raise NetworkBackendError("socket became exceptional while reading frame")
            if not readable:
                continue
            try:
                chunk = self._sock.recv(n - len(buf))
            except (TimeoutError, BlockingIOError, InterruptedError):
                continue
            if not chunk:
                if buf:
                    raise NetworkBackendError("peer closed socket mid-frame")
                raise NetworkBackendError("peer closed socket")
            buf.extend(chunk)
            if partial_deadline is None:
                partial_deadline = time.monotonic() + _FRAME_READ_TIMEOUT_SECONDS
        return bytes(buf)

    @contextmanager
    def _write_guard(
        self,
        *,
        timeout: float | None = None,
        deadline: float | None = None,
        operation: str,
        cancel_event: threading.Event | None = None,
    ) -> Iterator[float]:
        """Acquire the shared writer lock without extending a deadline."""
        if (timeout is None) == (deadline is None):
            raise ValueError("provide exactly one of timeout or deadline")
        if timeout is not None:
            timeout = _require_timeout(timeout, "timeout")
            deadline = time.monotonic() + timeout
        else:
            if deadline is None:
                raise ValueError("deadline must be finite")
            if not math.isfinite(deadline):
                raise ValueError("deadline must be finite")
            timeout = max(0.0, deadline - time.monotonic())
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise NetworkBackendError(f"{operation}: cancelled")
            if self._closed or self._closed_event.is_set():
                raise NetworkBackendError(f"{operation}: backend closed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NetworkBackendError(f"{operation} write lock timed out after {timeout:g}s")
            if self._write_lock.acquire(timeout=min(_SEND_POLL_SECONDS, remaining)):
                break
        try:
            yield deadline
        finally:
            self._write_lock.release()

    def _send_frame(
        self,
        frame: bytes,
        *,
        timeout: float,
        operation: str,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Send one framed message with a cancellation-aware deadline.

        ``socket.sendall`` is deliberately avoided: it can block in the
        caller for an unbounded period when a peer stops reading.  The socket
        is non-blocking, so each partial write is followed by a short
        writability poll and a closed-event check.
        """
        timeout = _require_timeout(timeout, "timeout")
        view = memoryview(frame)
        offset = 0
        deadline = time.monotonic() + timeout
        while offset < len(view):
            if cancel_event is not None and cancel_event.is_set():
                raise NetworkBackendError(f"{operation}: cancelled")
            if self._closed or self._closed_event.is_set():
                raise NetworkBackendError(f"{operation}: backend closed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NetworkBackendError(f"{operation} send timed out after {timeout:g}s")
            try:
                sent = self._sock.send(view[offset:])
            except BlockingIOError:
                pass
            except InterruptedError:
                continue
            except OSError as exc:
                raise NetworkBackendError(f"failed to send {operation}: {exc}") from exc
            else:
                # A successful zero-byte write is terminal, unlike EAGAIN:
                # polling cannot make progress on a closed stream.
                if sent == 0:
                    raise NetworkBackendError(f"socket closed while sending {operation}")
                offset += sent
                continue
            try:
                _readable, writable, exceptional = select.select(
                    [],
                    [self._sock],
                    [self._sock],
                    min(_SEND_POLL_SECONDS, remaining),
                )
            except InterruptedError:
                continue
            except (OSError, ValueError) as exc:
                if self._closed or self._closed_event.is_set():
                    raise NetworkBackendError(f"{operation}: backend closed") from exc
                raise NetworkBackendError(f"failed to poll socket for {operation}: {exc}") from exc
            if exceptional and not writable:
                raise NetworkBackendError(f"socket became exceptional while sending {operation}")

    def _queue_get(
        self,
        q: queue.Queue,
        *,
        timeout: float,
        timeout_message: str,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[], object] | None = None,
    ) -> Any:
        """Get a response while allowing stop and owner progress to wake it."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if cancel_event is not None and cancel_event.is_set():
                error = NetworkBackendError("operation cancelled")
                self._mark_closed(error)
                raise error
            if self._closed_event.is_set():
                if self._reader_exc is not None:
                    raise NetworkBackendError(
                        f"backend reader stopped: {self._reader_exc}"
                    ) from self._reader_exc
                raise NetworkBackendError("backend closed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                error = NetworkBackendError(timeout_message)
                self._mark_closed(error)
                raise error
            if progress_callback is not None:
                # Do not invoke owner code while holding a queue/condition
                # lock.  The callback is deliberately bounded by the
                # caller's owner contract; the surrounding polling loop
                # still applies the original absolute transport deadline.
                self._run_progress_callback(progress_callback)
                if cancel_event is not None and cancel_event.is_set():
                    error = NetworkBackendError("operation cancelled")
                    self._mark_closed(error)
                    raise error
                if self._closed_event.is_set():
                    if self._reader_exc is not None:
                        raise NetworkBackendError(
                            f"backend reader stopped: {self._reader_exc}"
                        ) from self._reader_exc
                    raise NetworkBackendError("backend closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    error = NetworkBackendError(timeout_message)
                    self._mark_closed(error)
                    raise error
            try:
                return q.get(timeout=min(_SEND_POLL_SECONDS, remaining))
            except queue.Empty:
                continue

    def _run_progress_callback(self, callback: Callable[[], object]) -> None:
        """Run owner progress and convert callback failures to link errors."""
        try:
            callback()
        except NetworkBackendError:
            raise
        except BaseException as exc:
            error = NetworkBackendError(f"owner progress callback failed: {exc}")
            self._mark_closed(error)
            raise error from exc


__all__ = [
    "NetworkBackend",
    "NetworkBackendError",
]
