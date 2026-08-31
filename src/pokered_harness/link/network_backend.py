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
background reader thread on both sides demuxes incoming frames. When a
reader sees ``EDGE_REQ`` (peer is master, we are slave) it advances our
local :class:`SerialCore` via ``apply_external_edge`` and responds with
our outgoing bit. When it sees ``EDGE_RESP`` (peer is slave responding
to our master request) it hands the bit to the blocking ``on_edge``
waiter via a response queue.

Slave IRQ
---------

When the reader-thread path completes the 8th edge on the local slave
core, it fires the optional ``irq_callback`` so halted code wakes up
(Pokemon's ``halt; wait for serial IRQ`` idiom). Wire this to
``pyboy.mb.cpu.set_interruptflag(INTR_SERIAL)`` on attach.

Threading model
---------------

``on_edge`` blocks for one socket round-trip. The caller is expected
to be PyBoy's main tick thread. The reader thread runs in the
background and uses a response queue to hand back RESP payloads;
REQ frames are handed to an ordered edge worker so slow slave re-arm
waits cannot starve control frames. A single write-lock ensures the
reader/worker RESP-sends don't collide with ``on_edge``'s REQ-sends on the
same socket.
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
from collections.abc import Callable
from typing import Any

# Opcodes:
#   EDGE_REQ  = master → slave: "here's my outgoing bit"
#   EDGE_RESP = slave → master: "here's my outgoing bit, bit just applied"
#   SYNC      = "I've reached checkpoint <id>; waiting for peer's SYNC <id>"
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
_OP_HELLO: int = 0x01

_PROTOCOL_VERSION: int = 1
_ROM_VERSION_CODES: dict[str, int] = {"red": 1, "blue": 2, "yellow": 3}
_ROM_VERSION_NAMES: dict[int, str] = {
    value: key for key, value in _ROM_VERSION_CODES.items()
}

_FRAME = struct.Struct(">BB")  # opcode, payload (1-byte id for SYNC)
_LEN = struct.Struct(">H")
_EDGE_RESPONSE_TIMEOUT_SECONDS = 10.0
_DEFAULT_SEND_TIMEOUT_SECONDS = 10.0
_DEFAULT_ACCEPT_TIMEOUT_SECONDS = 30.0
_SEND_POLL_SECONDS = 0.05
_CONTROL_QUEUE_MAXSIZE = 256
_REARM_WAIT_SECONDS = 0.100
_POST_BYTE_REARM_GRACE_SECONDS = 1.500
_ACTIVE_EXCHANGE_GRACE_SECONDS = 1.000
_ACTIVE_EXCHANGE_EDGE_THRESHOLD = 64
_ACTIVE_EXCHANGE_REARM_WAIT_SECONDS = 5.0


class NetworkBackendError(RuntimeError):
    """Wraps socket errors + protocol errors from :class:`NetworkBackend`."""


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
        raise ValueError(
            f"{name} must be a finite, non-negative number"
        ) from exc
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{name} must be a finite, non-negative number")
    return normalized


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
            addresses = socket.getaddrinfo(
                normalized, None, type=socket.SOCK_STREAM
            )
        except OSError as exc:
            raise ValueError(
                "NetworkBackend is localhost-only; localhost did not resolve"
            ) from exc
        if not addresses or any(
            not ipaddress.ip_address(address[4][0]).is_loopback
            for address in addresses
        ):
            raise ValueError(
                "NetworkBackend is localhost-only; localhost resolved unsafely"
            )
        return "localhost"
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError as exc:
        raise ValueError(
            "NetworkBackend is localhost-only; use 127.0.0.1, localhost, or ::1"
        ) from exc
    if not address.is_loopback:
        raise ValueError(
            "NetworkBackend is localhost-only; use 127.0.0.1, localhost, or ::1"
        )
    return normalized


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
        addresses = socket.getaddrinfo(
            normalized_host, port, type=socket.SOCK_STREAM
        )
    except OSError as exc:
        raise NetworkBackendError(
            f"unable to resolve {normalized_host!r}: {exc}"
        ) from exc
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
                last_error = OSError(
                    result, errno.errorcode.get(result, "connect failed")
                )
                continue
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise NetworkBackendError("connection cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    last_error = TimeoutError(
                        f"connection to {normalized_host}:{port} timed out"
                    )
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
                last_error = OSError(
                    error, errno.errorcode.get(error, "connect failed")
                )
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
            f"unsupported ROM version {version!r}; expected one of "
            f"{sorted(_ROM_VERSION_CODES)}"
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
        # Keep edge application ordered, but do not let a slow slave
        # re-arm wait block the reader from consuming control frames such
        # as SYNC or HELLO. The master sends one EDGE_REQ at a time, so a
        # bounded queue is sufficient and makes overload fail closed.
        self._edge_queue: queue.Queue[int | None] = queue.Queue(maxsize=256)
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
        self._exchange_queues: dict[int, queue.Queue[bytes]] = {}
        self._sync_lock = threading.Lock()
        self._exchange_lock = threading.Lock()
        self._closed = False
        self._closed_event = threading.Event()
        self._reader_exc: Exception | None = None
        self._local_rom_version = (
            _validate_rom_version(local_rom_version)
            if local_rom_version is not None
            else None
        )
        self._peer_rom_version: str | None = None
        self._hello_received = threading.Event()
        # Slave-mode config — set by start_receiver.
        self._local_core: object | None = None
        self._irq_callback: Callable[[], None] | None = None
        self._reader: threading.Thread | None = None
        self._edge_worker: threading.Thread | None = None
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
        }
        self._active_exchange_until: float = 0.0
        self._consecutive_armed_edges: int = 0
        self._post_byte_rearm_until: float = 0.0

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
        accept_timeout_s: float | None = _DEFAULT_ACCEPT_TIMEOUT_SECONDS,
        cancel_event: threading.Event | None = None,
    ) -> tuple[NetworkBackend, socket.socket]:
        """Bind to ``(host, port)`` and wait until a peer connects.

        Returns ``(backend, listener_sock)``; the caller keeps the
        listener socket to close later if needed. A fresh accepted
        socket is wrapped by the backend. ``accept_timeout_s`` and
        ``cancel_event`` make the accept cancellable for lifecycle owners.
        The default accept deadline is finite; explicitly passing ``None``
        without cancellation retains the legacy unbounded behavior for
        diagnostic use only.
        """
        normalized_host = validate_loopback_host(host)
        accept_timeout_s = _validate_optional_timeout(
            accept_timeout_s, "accept_timeout_s"
        )
        family = _socket_family(normalized_host)
        listener = socket.socket(family, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        bind_address = (
            (normalized_host, port, 0, 0)
            if family == socket.AF_INET6
            else (normalized_host, port)
        )
        conn: socket.socket | None = None
        try:
            listener.bind(bind_address)
            listener.listen(backlog)
            if accept_timeout_s is None and cancel_event is None:
                conn, _ = listener.accept()
            else:
                deadline = (
                    None
                    if accept_timeout_s is None
                    else time.monotonic() + accept_timeout_s
                )
                while conn is None:
                    if cancel_event is not None and cancel_event.is_set():
                        raise NetworkBackendError("listener accept cancelled")
                    remaining = (
                        None
                        if deadline is None
                        else deadline - time.monotonic()
                    )
                    if remaining is not None and remaining <= 0:
                        raise NetworkBackendError(
                            f"listener accept timed out after {accept_timeout_s:g}s"
                        )
                    wait_s = 0.25 if remaining is None else min(0.25, remaining)
                    listener.settimeout(wait_s)
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
    ) -> str | None:
        """Wait for the optional versioned handshake.

        Unversioned ``NetworkBackend.pair()`` users get ``None`` so the
        historical ROM-free backend tests remain valid. Production TCP
        callers pass ``local_rom_version`` and therefore fail closed if the
        peer does not present a compatible protocol/ROM identity. Callers that
        know the expected peer label can pass it explicitly to reject a
        mismatched announcement.
        """
        expected = (
            _validate_rom_version(expected_peer_rom_version)
            if expected_peer_rom_version is not None
            else None
        )
        if self._local_rom_version is None:
            if expected is not None:
                raise NetworkBackendError(
                    "cannot validate peer ROM without a local versioned HELLO"
                )
            return None
        if not self._hello_received.wait(timeout=max(0.0, timeout)):
            raise NetworkBackendError(
                f"peer HELLO not received within {timeout:g}s"
            )
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
                f"peer ROM version {peer_version!r} does not match "
                f"expected {expected!r}"
            )
            self._mark_closed(error)
            raise error
        return peer_version

    # --- slave-side receiver ------------------------------------------

    def start_receiver(
        self,
        local_core: object,
        irq_callback: Callable[[], None] | None = None,
    ) -> None:
        """Start the reader thread.

        When an incoming ``EDGE_REQ`` arrives (the peer is acting as
        master), apply the bit to ``local_core`` via
        ``apply_external_edge`` and respond with our
        ``peek_out_bit()``. If ``apply_external_edge`` returns True
        (8th-edge completion) and ``irq_callback`` is provided, the
        callback fires so halted slave code wakes.

        ``EDGE_RESP`` frames (responses to our own master-side
        ``on_edge`` requests) are put on the response queue for the
        blocking ``on_edge`` call to pick up.
        """
        if self._reader is not None:
            raise RuntimeError("receiver already started")
        self._local_core = local_core
        self._irq_callback = irq_callback
        self._edge_worker = threading.Thread(
            target=self._edge_worker_loop,
            name="NetworkBackend.edge-worker",
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
        """
        del our_role  # the wire role is carried by the ROM's SC register
        with self._edge_call_lock:
            with self._edge_response_lock:
                if self._closed:
                    raise NetworkBackendError("backend closed")
                if self._edge_inflight:
                    raise NetworkBackendError("concurrent EDGE_REQ is not supported")
                if not self._resp_queue.empty():
                    error = NetworkBackendError("stale EDGE_RESP before EDGE_REQ")
                    self._mark_closed(error)
                    raise error
                self._edge_inflight = True

            frame_out = _FRAME.pack(_OP_EDGE_REQ, our_bit & 1)
            self._stats["edge_req_sent"] = int(self._stats["edge_req_sent"]) + 1
            try:
                with self._write_lock:
                    if self._closed:
                        raise NetworkBackendError("backend closed")
                    self._send_frame(
                        frame_out,
                        timeout=_EDGE_RESPONSE_TIMEOUT_SECONDS,
                        operation="EDGE_REQ",
                    )
                bit = self._queue_get(
                    self._resp_queue,
                    timeout=_EDGE_RESPONSE_TIMEOUT_SECONDS,
                    timeout_message=(
                        "no EDGE_RESP from peer within "
                        f"{_EDGE_RESPONSE_TIMEOUT_SECONDS:g}s"
                    ),
                )
                self._stats["edge_resp_received"] = (
                    int(self._stats["edge_resp_received"]) + 1
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

    # --- out-of-band rendezvous ---------------------------------------

    def sync_with_peer(self, sync_id: int = 0, *, timeout: float = 120.0) -> None:
        """Cross-peer rendezvous barrier.

        Send an ``OP_SYNC`` frame with ``sync_id`` (0-255), then block
        until the peer's matching ``OP_SYNC`` arrives. Both peers must
        call ``sync_with_peer`` with the same ``sync_id`` to proceed.
        Used by higher-level drivers (e.g. the subprocess trade flow)
        to converge both sides at phase boundaries.

        Raises :class:`NetworkBackendError` if the peer's SYNC doesn't
        arrive within ``timeout`` seconds.
        """
        timeout = _validate_optional_timeout(timeout, "timeout")
        assert timeout is not None
        q = self._sync_queue(sync_id)
        self._send_sync_with_timeout(sync_id, timeout=timeout)
        self._queue_get(
            q,
            timeout=timeout,
            timeout_message=f"no peer OP_SYNC({sync_id}) within {timeout}s",
        )

    def announce_sync(self, sync_id: int = 0) -> None:
        """Send an ``OP_SYNC`` marker without waiting for the peer.

        Higher-level drivers can use this to advertise that the local
        process reached a state boundary while continuing to step its
        CPU until the peer catches up.
        """
        self._sync_queue(sync_id)
        self._send_sync(sync_id)

    def poll_peer_sync(self, sync_id: int = 0) -> bool:
        """Return True if a peer ``OP_SYNC`` for ``sync_id`` is pending.

        Consumes one queued peer SYNC if present; otherwise returns
        False immediately.
        """
        q = self._sync_queue(sync_id)
        try:
            q.get_nowait()
        except queue.Empty:
            return False
        self._stats["sync_poll_hits"] = int(self._stats["sync_poll_hits"]) + 1
        return True

    def exchange_block(
        self, kind_id: int, payload: bytes, *, timeout: float = 120.0
    ) -> bytes:
        """Exchange an arbitrary serial data block with the peer.

        This is an out-of-band helper for high-level ROM routines such as
        ``Serial_ExchangeBytes`` where bit-level transfer is prohibitively
        slow or can desynchronize after thousands of serial edges. FIFO is
        preserved per ``kind_id``.
        """
        if not 0 <= kind_id <= 255:
            raise ValueError(f"kind_id must fit in uint8, got {kind_id}")
        if len(payload) > 0xFFFF:
            raise ValueError(f"payload too large: {len(payload)} bytes")
        timeout = _validate_optional_timeout(timeout, "timeout")
        assert timeout is not None
        q = self._exchange_queue(kind_id)
        frame = _FRAME.pack(_OP_EXCHANGE, kind_id) + _LEN.pack(len(payload)) + payload
        self._stats["exchange_sent"] = int(self._stats["exchange_sent"]) + 1
        try:
            with self._write_lock:
                if self._closed:
                    raise NetworkBackendError("backend closed")
                self._send_frame(
                    frame,
                    timeout=timeout,
                    operation=f"OP_EXCHANGE({kind_id})",
                )
        except (OSError, NetworkBackendError) as exc:
            error = (
                exc
                if isinstance(exc, NetworkBackendError)
                else NetworkBackendError(
                    f"failed to send OP_EXCHANGE({kind_id}): {exc}"
                )
            )
            self._mark_closed(error)
            raise error from exc
        return self._queue_get(
            q,
            timeout=timeout,
            timeout_message=f"no peer OP_EXCHANGE({kind_id}) within {timeout}s",
        )

    def debug_snapshot(self) -> dict[str, object]:
        """Return a shallow, JSON-serializable diagnostic snapshot."""
        snap = dict(self._stats)
        last_keepalive = snap.get("last_keepalive_state")
        if isinstance(last_keepalive, dict):
            snap["last_keepalive_state"] = dict(last_keepalive)
        snap["closed"] = self._closed
        snap["reader_started"] = self._reader is not None
        now = time.monotonic()
        snap["active_exchange"] = now < self._active_exchange_until
        snap["post_byte_rearm_grace"] = now < self._post_byte_rearm_until
        snap["consecutive_armed_edges"] = self._consecutive_armed_edges
        with self._edge_pending_condition:
            snap["pending_edge_requests"] = self._edge_pending
        return snap

    def wait_for_wire_idle(
        self, timeout: float = 10.0, *, allow_peer_close: bool = False
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
        tear down immediately.
        """
        timeout = _validate_optional_timeout(timeout, "timeout")
        assert timeout is not None
        deadline = time.monotonic() + timeout
        with self._edge_pending_condition:
            while True:
                with self._edge_response_lock:
                    edge_inflight = self._edge_inflight
                    response_pending = not self._resp_queue.empty()
                if self._edge_pending == 0 and not edge_inflight and not response_pending:
                    if self._closed:
                        if allow_peer_close and self._reader_exc is None:
                            return
                        if self._reader_exc is not None:
                            raise NetworkBackendError(
                                f"backend closed while waiting for wire idle: "
                                f"{self._reader_exc}"
                            ) from self._reader_exc
                        raise NetworkBackendError("backend closed while waiting for wire idle")
                    return
                if self._closed:
                    if self._reader_exc is not None:
                        raise NetworkBackendError(
                            f"backend closed while waiting for wire idle: "
                            f"{self._reader_exc}"
                        ) from self._reader_exc
                    raise NetworkBackendError("backend closed while waiting for wire idle")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NetworkBackendError(
                        "wire did not become idle within "
                        f"{timeout:g}s (pending_edge_requests={self._edge_pending}, "
                        f"edge_inflight={edge_inflight}, "
                        f"response_pending={response_pending})"
                    )
                self._edge_pending_condition.wait(timeout=min(0.05, remaining))

    def _sync_queue(self, sync_id: int) -> queue.Queue[int]:
        if not 0 <= sync_id <= 255:
            raise ValueError(f"sync_id must fit in uint8, got {sync_id}")
        # Make sure we have a queue ready before we send, so the
        # reader thread can deposit an incoming SYNC even if we
        # haven't started waiting yet.
        with self._sync_lock:
            return self._sync_queues.setdefault(
                sync_id, queue.Queue(maxsize=_CONTROL_QUEUE_MAXSIZE)
            )

    def _exchange_queue(self, kind_id: int) -> queue.Queue[bytes]:
        with self._exchange_lock:
            return self._exchange_queues.setdefault(
                kind_id, queue.Queue(maxsize=_CONTROL_QUEUE_MAXSIZE)
            )

    def _send_sync(self, sync_id: int) -> None:
        self._send_sync_with_timeout(sync_id, timeout=_DEFAULT_SEND_TIMEOUT_SECONDS)

    def _send_sync_with_timeout(self, sync_id: int, *, timeout: float) -> None:
        self._stats["sync_sent"] = int(self._stats["sync_sent"]) + 1
        try:
            with self._write_lock:
                if self._closed:
                    raise NetworkBackendError("backend closed")
                self._send_frame(
                    _FRAME.pack(_OP_SYNC, sync_id),
                    timeout=timeout,
                    operation=f"OP_SYNC({sync_id})",
                )
        except (OSError, NetworkBackendError) as exc:
            error = (
                exc
                if isinstance(exc, NetworkBackendError)
                else NetworkBackendError(
                    f"failed to send OP_SYNC({sync_id}): {exc}"
                )
            )
            self._mark_closed(error)
            raise error from exc

    def _send_hello(self, rom_version: str) -> None:
        payload = (_PROTOCOL_VERSION << 4) | _ROM_VERSION_CODES[rom_version]
        try:
            with self._write_lock:
                if self._closed:
                    raise NetworkBackendError("backend closed")
                self._send_frame(
                    _FRAME.pack(_OP_HELLO, payload),
                    timeout=_DEFAULT_SEND_TIMEOUT_SECONDS,
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

    def _mark_closed(self, error: Exception | None = None) -> None:
        """Fail closed and wake all waiters after a transport error.

        ``on_edge`` can discover a stale response or a peer timeout while the
        reader thread is still blocked in ``recv``. Closing the socket here
        makes that state terminal and lets both the reader and edge worker
        unwind; a later call to :meth:`stop` remains idempotent.
        """
        if error is not None and self._reader_exc is None:
            self._reader_exc = error
        self._closed = True
        self._closed_event.set()
        self._hello_received.set()
        with self._edge_pending_condition:
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

    def stop(self, *, timeout_s: float = 2.0) -> bool:
        timeout_s = _validate_optional_timeout(timeout_s, "timeout_s")
        assert timeout_s is not None
        deadline = time.monotonic() + timeout_s
        self._closed = True
        self._closed_event.set()
        self._hello_received.set()
        with self._edge_pending_condition:
            self._edge_pending_condition.notify_all()
        try:
            self._edge_queue.put_nowait(None)
        except queue.Full:
            # The closed flag is authoritative; the worker will observe it
            # on its next bounded queue wait even if the sentinel cannot be
            # inserted during an overload condition.
            pass
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        edge_worker = self._edge_worker
        if (
            isinstance(edge_worker, threading.Thread)
            and edge_worker is not threading.current_thread()
        ):
            edge_worker.join(timeout=max(0.0, deadline - time.monotonic()))

        reader = self._reader
        if (
            isinstance(reader, threading.Thread)
            and reader is not threading.current_thread()
        ):
            reader.join(timeout=max(0.0, deadline - time.monotonic()))
        return not any(
            isinstance(worker, threading.Thread)
            and worker is not threading.current_thread()
            and worker.is_alive()
            for worker in (edge_worker, reader)
        )

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
                        raise NetworkBackendError(
                            f"invalid EDGE_REQ bit payload {payload}"
                        )
                    with self._edge_pending_condition:
                        self._edge_pending += 1
                    try:
                        self._edge_queue.put_nowait(payload & 1)
                    except queue.Full as exc:
                        with self._edge_pending_condition:
                            self._edge_pending -= 1
                            self._edge_pending_condition.notify_all()
                        raise NetworkBackendError(
                            "incoming EDGE_REQ queue is full"
                        ) from exc
                elif opcode == _OP_EDGE_RESP:
                    if payload > 1:
                        raise NetworkBackendError(
                            f"invalid EDGE_RESP bit payload {payload}"
                        )
                    with self._edge_response_lock:
                        try:
                            self._resp_queue.put_nowait(payload & 1)
                        except queue.Full as exc:
                            raise NetworkBackendError(
                                "duplicate or unsolicited EDGE_RESP"
                            ) from exc
                elif opcode == _OP_SYNC:
                    self._stats["sync_received"] = int(self._stats["sync_received"]) + 1
                    with self._sync_lock:
                        q = self._sync_queues.setdefault(
                            payload, queue.Queue(maxsize=_CONTROL_QUEUE_MAXSIZE)
                        )
                    try:
                        q.put_nowait(payload)
                    except queue.Full as exc:
                        raise NetworkBackendError(
                            f"OP_SYNC({payload}) queue is full"
                        ) from exc
                elif opcode == _OP_EXCHANGE:
                    raw_len = self._recv_exactly(2)
                    (length,) = _LEN.unpack(raw_len)
                    data = self._recv_exactly(length)
                    self._stats["exchange_received"] = (
                        int(self._stats["exchange_received"]) + 1
                    )
                    with self._exchange_lock:
                        q = self._exchange_queues.setdefault(
                            payload, queue.Queue(maxsize=_CONTROL_QUEUE_MAXSIZE)
                        )
                    try:
                        q.put_nowait(data)
                    except queue.Full as exc:
                        raise NetworkBackendError(
                            f"OP_EXCHANGE({payload}) queue is full"
                        ) from exc
                else:
                    raise NetworkBackendError(
                        f"unknown NetworkBackend opcode 0x{opcode:02x}"
                    )
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
        """Apply incoming edges in wire order without blocking the reader."""
        while not self._closed:
            try:
                peer_bit = self._edge_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if peer_bit is None:
                return
            try:
                if self._closed:
                    return
                self._handle_edge_req(peer_bit)
            except Exception as exc:  # noqa: BLE001
                self._mark_closed(exc)
                return
            finally:
                with self._edge_pending_condition:
                    self._edge_pending -= 1
                    self._edge_pending_condition.notify_all()

    def _signal_edge_worker_stop(self) -> None:
        try:
            self._edge_queue.put_nowait(None)
        except queue.Full:
            pass

    def _handle_edge_req(self, peer_bit: int) -> None:
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
        core = self._local_core
        self._stats["edge_req_received"] = int(self._stats["edge_req_received"]) + 1
        def armed() -> bool:
            return (
                core is not None
                and getattr(core, "transfer_enabled", 0)
                and not getattr(core, "internal_clock", 0)
            )

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
            while time.monotonic() < deadline and not self._closed:
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
                time.sleep(0.0005)

        if armed():
            slave_armed_edges = int(self._stats["slave_armed_edges"]) + 1
            self._stats["slave_armed_edges"] = slave_armed_edges
            self._consecutive_armed_edges += 1
            if slave_armed_edges >= _ACTIVE_EXCHANGE_EDGE_THRESHOLD:
                self._active_exchange_until = (
                    time.monotonic() + _ACTIVE_EXCHANGE_GRACE_SECONDS
                )
            our_bit = core.peek_out_bit()
            completed = core.apply_external_edge(peer_bit)
            if completed:
                self._stats["last_slave_byte_complete_at"] = time.monotonic()
                self._post_byte_rearm_until = (
                    time.monotonic() + _POST_BYTE_REARM_GRACE_SECONDS
                )
            # Reset keep-alive counter so the next idle stretch starts
            # fresh at the top of a 0xFE byte boundary rather than
            # mid-byte.
            self._keepalive_bit_idx = 0
        else:
            if active_exchange:
                raise NetworkBackendError(
                    "slave did not re-arm during active serial exchange"
                )
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
            self._stats["keepalive_bits_sent"] = (
                int(self._stats["keepalive_bits_sent"]) + 1
            )
            self._stats["last_keepalive_state"] = self._core_state_snapshot(core)
            our_bit = 0 if self._keepalive_bit_idx == 7 else 1
            self._keepalive_bit_idx = (self._keepalive_bit_idx + 1) & 7
            completed = False
        try:
            with self._write_lock:
                if not self._closed:
                    self._send_frame(
                        _FRAME.pack(_OP_EDGE_RESP, our_bit & 1),
                        timeout=_EDGE_RESPONSE_TIMEOUT_SECONDS,
                        operation="EDGE_RESP",
                    )
                    self._stats["edge_resp_sent"] = (
                        int(self._stats["edge_resp_sent"]) + 1
                    )
        except (OSError, NetworkBackendError):
            self._mark_closed()
            return
        if completed and self._irq_callback is not None:
            try:
                self._stats["irq_callbacks"] = int(self._stats["irq_callbacks"]) + 1
                self._irq_callback()
            except Exception as exc:  # noqa: BLE001 - isolate optional callback
                # IRQ callback errors shouldn't kill the reader thread. Keep a
                # compact diagnostic so callers can inspect failures through
                # debug_snapshot() without changing transport behavior.
                self._stats["irq_callback_errors"] = (
                    int(self._stats["irq_callback_errors"]) + 1
                )
                self._stats["last_irq_callback_error"] = type(exc).__name__

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
        buf = bytearray()
        while len(buf) < n:
            if self._closed or self._closed_event.is_set():
                raise NetworkBackendError("backend closed")
            try:
                chunk = self._sock.recv(n - len(buf))
            except BlockingIOError:
                try:
                    select.select(
                        [self._sock], [], [], _SEND_POLL_SECONDS
                    )
                except (OSError, ValueError) as exc:
                    if self._closed or self._closed_event.is_set():
                        raise NetworkBackendError("backend closed") from exc
                    raise
                continue
            except InterruptedError:
                continue
            if not chunk:
                if buf:
                    raise NetworkBackendError("peer closed socket mid-frame")
                raise NetworkBackendError("peer closed socket")
            buf.extend(chunk)
        return bytes(buf)

    def _send_frame(self, frame: bytes, *, timeout: float, operation: str) -> None:
        """Send one framed message with a cancellation-aware deadline.

        ``socket.sendall`` is deliberately avoided: it can block in the
        caller for an unbounded period when a peer stops reading.  The socket
        is non-blocking, so each partial write is followed by a short
        writability poll and a closed-event check.
        """
        timeout = _validate_optional_timeout(timeout, "timeout")
        assert timeout is not None
        view = memoryview(frame)
        offset = 0
        deadline = time.monotonic() + timeout
        while offset < len(view):
            if self._closed or self._closed_event.is_set():
                raise NetworkBackendError(f"{operation}: backend closed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NetworkBackendError(
                    f"{operation} send timed out after {timeout:g}s"
                )
            try:
                sent = self._sock.send(view[offset:])
            except BlockingIOError:
                sent = 0
            except InterruptedError:
                continue
            except OSError as exc:
                raise NetworkBackendError(
                    f"failed to send {operation}: {exc}"
                ) from exc
            if sent > 0:
                offset += sent
                continue
            try:
                _readable, writable, exceptional = select.select(
                    [], [self._sock], [self._sock],
                    min(_SEND_POLL_SECONDS, remaining),
                )
            except (OSError, ValueError) as exc:
                if self._closed or self._closed_event.is_set():
                    raise NetworkBackendError(
                        f"{operation}: backend closed"
                    ) from exc
                raise NetworkBackendError(
                    f"failed to poll socket for {operation}: {exc}"
                ) from exc
            if exceptional and not writable:
                raise NetworkBackendError(
                    f"socket became exceptional while sending {operation}"
                )

    def _queue_get(
        self,
        q: queue.Queue,
        *,
        timeout: float,
        timeout_message: str,
    ) -> Any:
        """Get a response while allowing :meth:`stop` to wake the waiter."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
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
                return q.get(timeout=min(0.25, remaining))
            except queue.Empty:
                continue


__all__ = [
    "NetworkBackend",
    "NetworkBackendError",
]
