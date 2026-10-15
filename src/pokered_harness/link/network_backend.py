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
reader sees ``EDGE_REQ`` (peer is master, we are slave) it queues an
immutable bit for the emulator owner. That owner samples and applies the
edge before responding. When it sees ``EDGE_RESP`` (peer is slave responding
to our master request) it hands the bit to the blocking ``on_edge``
waiter via a response queue.

Slave IRQ
---------

When the owner completes the 8th edge on the local slave
core, it fires the optional ``irq_callback`` so halted code wakes up
(Pokemon's ``halt; wait for serial IRQ`` idiom). Wire this to
``pyboy.mb.cpu.set_interruptflag(INTR_SERIAL)`` on attach.

Threading model
---------------

``on_edge`` blocks for one socket round-trip. The caller is expected
to be PyBoy's main tick thread. The reader thread runs in the
background and uses a response queue to hand back RESP payloads;
REQ frames wait in a bounded mailbox; a transport-only watchdog bounds
pending requests even if no owner runs. Owner scopes bind the native
precommit pump plus optional CPU/HALT polls to the current execution thread.
Rearm occurs through ordinary owner execution, never worker-thread emulation.
A single write-lock serializes outbound frames. Explicit core-less transport
compatibility can produce placeholder bits, but an attached core never does.
Version-1 framing has no timestamp/epoch frontier or physical dual-clock
arbitration; owner confinement alone is not authentic remote gameplay proof.
"""

from __future__ import annotations

import queue
import select
import socket
import struct
import threading
import time
import weakref
from contextlib import contextmanager
from typing import Any, Callable, Optional


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
_REARM_WAIT_SECONDS = 0.100
_POST_BYTE_REARM_GRACE_SECONDS = 1.500
_ACTIVE_EXCHANGE_GRACE_SECONDS = 1.000
_ACTIVE_EXCHANGE_EDGE_THRESHOLD = 64
_ACTIVE_EXCHANGE_REARM_WAIT_SECONDS = 5.0
_EDGE_CALL_TIMEOUT_SECONDS = 10.0
_STOP_JOIN_TIMEOUT_SECONDS = 2.0


class NetworkBackendError(RuntimeError):
    """Wraps socket errors + protocol errors from :class:`NetworkBackend`."""


def _require_exclusive_owner_claims(core: object) -> None:
    """Reject attached cores that cannot enforce serialized owner admission."""
    required = ("set_owner_pump", "claim_owner_pump", "release_owner_pump")
    if any(not callable(getattr(core, name, None)) for name in required):
        raise NetworkBackendError("serial core lacks exclusive owner pump claims")


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
        self._write_lock = threading.Lock()
        # Pokemon's serial protocol has no request identifier.  Admit only
        # one master edge at a time; otherwise two callers can consume each
        # other's responses and leave both emulators permanently shifted.
        self._edge_call_lock = threading.Lock()
        self._response_lock = threading.Lock()
        self._response_pending = False
        self._close_lock = threading.Lock()
        # Responses from peer (when we're master) land here.
        # A second response without a waiter is a protocol violation.  A
        # one-item queue lets the reader detect that condition instead of
        # retaining stale bytes for a later request.
        self._resp_queue: queue.Queue[int] = queue.Queue(maxsize=1)
        # Keep edge application ordered, but do not let a slow slave
        # re-arm wait block the reader from consuming control frames such
        # as SYNC or HELLO. The master sends one EDGE_REQ at a time, so a
        # bounded queue is sufficient and makes overload fail closed.
        self._edge_queue: queue.Queue[int | None] = queue.Queue(maxsize=256)
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
        self._local_core: Optional[object] = None
        self._irq_callback: Optional[Callable[[], None]] = None
        self._reader: Optional[threading.Thread] = None
        self._edge_worker: Optional[threading.Thread] = None
        self._incoming_lock = threading.Lock()
        self._pending_edge: tuple[int, float] | None = None
        self._waiting_request: tuple[int, float] | None = None
        self._owner_state = threading.local()
        self._standalone_owner = threading.RLock()
        self._emulator_owner = None
        # Shutdown must never take the emulator-owner lock.  These counters
        # let cleanup defer the owned-reference commit while a foreground
        # owner scope or an owner_poll call is still using the core.
        self._owner_activity = threading.Condition()
        self._active_owner_scopes = 0
        self._active_owner_executions = 0
        # ``start_receiver`` publishes its thread objects before calling
        # ``Thread.start``.  Keep teardown from mistaking that short
        # activation window for a quiescent receiver.
        self._receiver_starting = False
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
            "last_keepalive_state": None,
            "last_slave_byte_complete_at": None,
            "last_slave_rearm_at": None,
        }
        self._active_exchange_until: float = 0.0
        self._consecutive_armed_edges: int = 0
        self._post_byte_rearm_until: float = 0.0

        # The backend owns this connected socket. Configure once, before any
        # receiver starts; never race reader IO by changing shared timeouts.
        self._sock.setblocking(False)
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
    ) -> tuple["NetworkBackend", socket.socket]:
        """Bind to ``(host, port)`` and block until a peer connects.

        Returns ``(backend, listener_sock)``; the caller keeps the
        listener socket to close later if needed. A fresh accepted
        socket is wrapped by the backend.
        """
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, port))
        listener.listen(backlog)
        conn, _ = listener.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return cls(conn, local_rom_version=local_rom_version), listener

    @classmethod
    def connect(
        cls,
        host: str,
        port: int,
        *,
        timeout_s: float = 10.0,
        local_rom_version: str | None = None,
    ) -> "NetworkBackend":
        """Open an outbound connection to a ``listen``-ing peer."""
        sock = socket.create_connection((host, port), timeout=timeout_s)
        sock.settimeout(None)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return cls(sock, local_rom_version=local_rom_version)

    @classmethod
    def pair(cls) -> tuple["NetworkBackend", "NetworkBackend"]:
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
        """Return the peer's authenticated protocol ROM label.

        The label is available only when both sides opted into the
        versioned handshake. Legacy in-process test pairs intentionally have
        no label and must use :meth:`wait_for_hello` only when configured.
        """
        if self._peer_rom_version is None:
            raise NetworkBackendError("peer HELLO has not completed")
        return self._peer_rom_version

    def wait_for_hello(self, timeout: float = 10.0) -> str | None:
        """Wait for the optional versioned handshake.

        Unversioned ``NetworkBackend.pair()`` users get ``None`` so the
        historical ROM-free backend tests remain valid. Production TCP
        callers pass ``local_rom_version`` and therefore fail closed if the
        peer does not present a compatible protocol/ROM identity.
        """
        if self._local_rom_version is None:
            return None
        if not self._hello_received.wait(timeout=max(0.0, timeout)):
            exc = NetworkBackendError(
                f"peer HELLO not received within {timeout:g}s"
            )
            self._close_transport(exc)
            raise exc
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
        return self.peer_rom_version

    # --- slave-side receiver ------------------------------------------

    def start_receiver(
        self,
        local_core: object,
        irq_callback: Optional[Callable[[], None]] = None,
    ) -> None:
        """Start transport reader/watchdog threads, not an emulator worker.

        When an incoming ``EDGE_REQ`` arrives (the peer is acting as
        master), queue the bit for ``owner_poll`` inside ``owner_scope``.
        The owner samples ``peek_out_bit`` and applies the incoming bit.
        If ``apply_external_edge`` returns True
        (8th-edge completion) and ``irq_callback`` is provided, the
        callback fires so halted slave code wakes.

        ``EDGE_RESP`` frames (responses to our own master-side
        ``on_edge`` requests) are put on the response queue for the
        blocking ``on_edge`` call to pick up.
        """
        if local_core is not None:
            _require_exclusive_owner_claims(local_core)
        with self._owner_activity:
            if self._closed:
                raise NetworkBackendError("backend closed")
            if self._reader is not None or self._receiver_starting:
                raise RuntimeError("receiver already started")
            self._receiver_starting = True
            self._local_core = local_core
            self._irq_callback = irq_callback
            edge_worker = threading.Thread(
                target=self._edge_worker_loop,
                name="NetworkBackend.edge-worker",
                daemon=True,
            )
            reader = threading.Thread(
                target=self._reader_loop, name="NetworkBackend.reader", daemon=True
            )
            self._edge_worker = edge_worker
            self._reader = reader

        try:
            # Do not hold the lifecycle condition across either start call:
            # tests and platform thread setup may block, while stop() must
            # remain free to close the transport and use its own deadline.
            edge_worker.start()
            reader.start()
        except BaseException as exc:
            self._close_transport(exc)
            raise
        finally:
            with self._owner_activity:
                self._receiver_starting = False
                self._owner_activity.notify_all()
                self._finalize_owned_state_locked()

    def bind_owner(self, owner) -> None:
        """Associate the already-claimed emulator owner before activation."""
        with self._owner_activity:
            if self._reader is not None or self._receiver_starting:
                raise NetworkBackendError("cannot replace owner after receiver activation")
            self._emulator_owner = weakref.ref(owner)

    def _owner_scope_enter(self) -> None:
        with self._owner_activity:
            self._active_owner_scopes += 1

    def _owner_scope_exit(self) -> None:
        with self._owner_activity:
            if self._active_owner_scopes <= 0:
                # This is internal lifecycle state; fail closed rather than
                # allowing a counter corruption to trigger early cleanup.
                self._active_owner_scopes = 1
                self._owner_activity.notify_all()
                return
            self._active_owner_scopes -= 1
            self._owner_activity.notify_all()
            self._finalize_owned_state_locked()

    def _owner_execution_enter(self) -> None:
        with self._owner_activity:
            self._active_owner_executions += 1

    def _owner_execution_exit(self) -> None:
        with self._owner_activity:
            if self._active_owner_executions <= 0:
                self._active_owner_executions = 1
                self._owner_activity.notify_all()
                return
            self._active_owner_executions -= 1
            self._owner_activity.notify_all()
            self._finalize_owned_state_locked()

    def _workers_quiescent_locked(self) -> bool:
        current = threading.current_thread()
        for worker in (self._edge_worker, self._reader):
            if worker is None:
                continue
            if not isinstance(worker, threading.Thread):
                return False
            if worker is current or worker.is_alive():
                return False
        return True

    def _finalize_owned_state_locked(self) -> bool:
        """Commit owned-reference cleanup; caller holds ``_owner_activity``."""
        if not self._closed:
            return False
        if self._receiver_starting:
            return False
        if self._active_owner_scopes or self._active_owner_executions:
            return False
        if not self._workers_quiescent_locked():
            return False

        # This is one all-or-nothing commit point.  If a worker or foreground
        # owner is still active, keep the complete graph for a later retry or
        # for the owner-scope exit callback to finish safely.
        self._reader = None
        self._edge_worker = None
        self._local_core = None
        self._irq_callback = None
        self._emulator_owner = None
        with self._incoming_lock:
            self._pending_edge = None
            self._waiting_request = None
        return True

    @contextmanager
    def owner_scope(self):
        """Bind the native pump to this serialized execution scope's thread.

        No worker enters this scope. Rebinding between calls is safe because
        the emulator owner excludes previous execution, including callbacks.
        Cancellation only closes transport and never waits for this lock.
        """
        depth = getattr(self._owner_state, "depth", 0)
        outer = not depth
        if outer:
            self._owner_scope_enter()
        try:
            owner = self._emulator_owner() if self._emulator_owner is not None else None
            access = owner.access() if owner is not None else self._standalone_owner
            with access:
                core = self._local_core
                if outer and core is not None:
                    _require_exclusive_owner_claims(core)
                self._owner_state.depth = depth + 1
                claim_token = None
                try:
                    if outer and core is not None and not getattr(core, "backend_failed", False):
                        claim = getattr(core, "claim_owner_pump")
                        claim_token = claim(self.owner_poll, poll=True)
                    yield
                finally:
                    self._owner_state.depth = depth
                    # Token release unbinds even after failure without clearing
                    # the original latched cause or permitting an unsafe retry.
                    if claim_token is not None:
                        core.release_owner_pump(claim_token)
        finally:
            if outer:
                self._owner_scope_exit()

    def owner_poll(self, event=None) -> None:
        """Service an incoming edge on the owner, without executing CPU code.

        An unarmed request stays pending while ordinary owner execution can
        reach the ROM's rearm instructions. No synthetic payload substitutes
        for a real core. This protocol still has no timestamp/frontier grant.
        """
        if not getattr(self._owner_state, "depth", 0):
            raise NetworkBackendError("incoming serial work requires an owner scope")
        try:
            if getattr(self._owner_state, "servicing", False):
                raise NetworkBackendError("recursive incoming edge service")
            self._owner_execution_enter()
            try:
                self._owner_poll_impl(event)
            finally:
                self._owner_execution_exit()
        except BaseException as exc:
            self._close_transport(exc)
            raise

    def _owner_poll_impl(self, event=None) -> None:
        """Implementation split keeps execution-depth cleanup outermost."""
        try:
            self._remaining(time.monotonic() + _EDGE_CALL_TIMEOUT_SECONDS)
            with self._incoming_lock:
                pending = self._pending_edge
            if pending is None:
                return
            peer_bit, received_at = pending
            if time.monotonic() >= received_at + _EDGE_CALL_TIMEOUT_SECONDS:
                raise NetworkBackendError("incoming EDGE_REQ owner deadline exhausted")
            core = self._local_core
            if core is None or not core.transfer_enabled or core.internal_clock:
                if self._waiting_request != pending:
                    self._waiting_request = pending
                    self._stats["slave_rearm_waits"] = int(self._stats["slave_rearm_waits"]) + 1
                    if self._stats["last_slave_byte_complete_at"] is not None:
                        self._stats["slave_post_byte_rearm_waits"] = int(self._stats["slave_post_byte_rearm_waits"]) + 1
                return
            if hasattr(core, "check_error"):
                core.check_error()
            if self._waiting_request == pending:
                self._stats["slave_rearm_successes"] = int(self._stats["slave_rearm_successes"]) + 1
                if self._stats["last_slave_byte_complete_at"] is not None:
                    self._stats["slave_post_byte_rearm_successes"] = int(self._stats["slave_post_byte_rearm_successes"]) + 1
                self._waiting_request = None
            self._owner_state.servicing = True
            try:
                our_bit = core.peek_out_bit()
                completed = core.apply_external_edge(peer_bit)
                self._stats["slave_armed_edges"] = int(self._stats["slave_armed_edges"]) + 1
                if completed:
                    self._stats["last_slave_byte_complete_at"] = time.monotonic()
                if completed and self._irq_callback is not None:
                    self._stats["irq_callbacks"] = int(self._stats["irq_callbacks"]) + 1
                    self._irq_callback()
                # Both the shift and completion IRQ precede acknowledgement.
                # A failure after either is terminal; never replay that edge.
                # Clearing before send admits the next legitimate request.
                with self._incoming_lock:
                    self._pending_edge = None
                self._send_frame(
                    _FRAME.pack(_OP_EDGE_RESP, our_bit & 1),
                    deadline=received_at + _EDGE_CALL_TIMEOUT_SECONDS,
                )
                self._stats["edge_resp_sent"] = int(self._stats["edge_resp_sent"]) + 1
            finally:
                self._owner_state.servicing = False
        except BaseException as exc:
            self._close_transport(exc)
            raise

    # --- SerialBackend.on_edge (master path) --------------------------

    def on_edge(self, our_bit: int, our_role: int) -> int:
        """Master-mode: send ``our_bit`` as EDGE_REQ, wait for EDGE_RESP.

        Blocks until the peer's owner responds. Timeout raises
        :class:`NetworkBackendError`. If the reader on our own side
        isn't running yet, incoming responses will be lost — callers
        must call :meth:`start_receiver` before master-mode transfers.
        """
        # Use a bounded admission wait as well as a bounded response wait.
        # If an earlier request is still in flight, do not enqueue another
        # wire request: there is no sequence number with which to correlate
        # the two responses.
        deadline = time.monotonic() + _EDGE_CALL_TIMEOUT_SECONDS
        if self._local_core is not None and not getattr(self._owner_state, "depth", 0):
            raise NetworkBackendError("serial stepping requires an owner scope")
        acquired = False
        try:
            self._acquire_until(self._edge_call_lock, deadline)
            acquired = True
            frame_out = _FRAME.pack(_OP_EDGE_REQ, our_bit & 1)
            try:
                def expect_response():
                    with self._response_lock:
                        self._response_pending = True
                self._send_frame(frame_out, deadline=deadline, before_send=expect_response)
                self._stats["edge_req_sent"] = int(self._stats["edge_req_sent"]) + 1
            except OSError as exc:
                self._close_transport(exc)
                raise NetworkBackendError(
                    f"failed to send EDGE_REQ: {exc}"
                ) from exc
            try:
                bit = self._queue_get(
                    self._resp_queue,
                    timeout=_EDGE_CALL_TIMEOUT_SECONDS,
                    deadline=deadline,
                    timeout_message=(
                        "no EDGE_RESP from peer within "
                        f"{_EDGE_CALL_TIMEOUT_SECONDS:g}s"
                    ),
                )
            except NetworkBackendError as exc:
                # A peer may have applied this edge even if its response was
                # lost.  Reusing the socket would let that late response
                # satisfy a future edge, so teardown is the only safe
                # recovery for this unnumbered protocol.
                self._close_transport(exc)
                raise
            self._stats["edge_resp_received"] = (
                int(self._stats["edge_resp_received"]) + 1
            )
            return bit
        except NetworkBackendError as exc:
            self._close_transport(exc)
            raise
        finally:
            if acquired:
                with self._response_lock:
                    self._response_pending = False
                self._edge_call_lock.release()

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
        This response budget starts after the separately bounded frame write.
        """
        q = self._sync_queue(sync_id)
        self._send_sync(sync_id)
        try:
            self._queue_get(
                q,
                timeout=timeout,
                timeout_message=f"no peer OP_SYNC({sync_id}) within {timeout}s",
            )
        except NetworkBackendError as exc:
            self._close_transport(exc)
            raise

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
        ``timeout`` remains the response budget after the separately bounded
        frame write; it is not an end-to-end exchange deadline.
        """
        if not 0 <= kind_id <= 255:
            raise ValueError(f"kind_id must fit in uint8, got {kind_id}")
        if len(payload) > 0xFFFF:
            raise ValueError(f"payload too large: {len(payload)} bytes")
        q = self._exchange_queue(kind_id)
        frame = _FRAME.pack(_OP_EXCHANGE, kind_id) + _LEN.pack(len(payload)) + payload
        self._stats["exchange_sent"] = int(self._stats["exchange_sent"]) + 1
        try:
            self._send_frame(frame)
        except OSError as exc:
            self._close_transport(exc)
            raise NetworkBackendError(
                f"failed to send OP_EXCHANGE({kind_id}): {exc}"
            ) from exc
        try:
            return self._queue_get(
                q,
                timeout=timeout,
                timeout_message=f"no peer OP_EXCHANGE({kind_id}) within {timeout}s",
            )
        except NetworkBackendError as exc:
            self._close_transport(exc)
            raise

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
        return snap

    def _sync_queue(self, sync_id: int) -> queue.Queue[int]:
        if not 0 <= sync_id <= 255:
            raise ValueError(f"sync_id must fit in uint8, got {sync_id}")
        # Make sure we have a queue ready before we send, so the
        # reader thread can deposit an incoming SYNC even if we
        # haven't started waiting yet.
        with self._sync_lock:
            return self._sync_queues.setdefault(sync_id, queue.Queue())

    def _exchange_queue(self, kind_id: int) -> queue.Queue[bytes]:
        with self._exchange_lock:
            return self._exchange_queues.setdefault(kind_id, queue.Queue())

    def _send_sync(self, sync_id: int) -> None:
        self._stats["sync_sent"] = int(self._stats["sync_sent"]) + 1
        try:
            self._send_frame(_FRAME.pack(_OP_SYNC, sync_id))
        except OSError as exc:
            self._close_transport(exc)
            raise NetworkBackendError(
                f"failed to send OP_SYNC({sync_id}): {exc}"
            ) from exc

    def _send_hello(self, rom_version: str) -> None:
        payload = (_PROTOCOL_VERSION << 4) | _ROM_VERSION_CODES[rom_version]
        try:
            self._send_frame(_FRAME.pack(_OP_HELLO, payload))
        except OSError as exc:
            self._close_transport(exc)
            raise NetworkBackendError(f"failed to send HELLO: {exc}") from exc

    # --- lifecycle ----------------------------------------------------

    def close(self) -> None:
        self.stop()

    def stop(self) -> None:
        """Close the transport and reclaim attached execution state.

        The socket close is deliberately cancellation-safe: it wakes the
        reader, the transport-only edge worker, and any blocked edge waiter
        without taking the emulator-owner lock.  The references that can
        retain a PyBoy (``_local_core`` and ``_irq_callback``) are released
        only after *both* background threads and all foreground owner scopes
        have stopped.  If a thread or owner scope does not quiesce within the
        bounded shutdown budget, all lifecycle references remain available
        for diagnosis and a later ``stop()`` (or scope exit) can complete the
        cleanup.
        """
        self._close_transport()
        workers = (self._edge_worker, self._reader)
        deadline = time.monotonic() + _STOP_JOIN_TIMEOUT_SECONDS
        shutdown_complete = True
        current = threading.current_thread()
        for worker in workers:
            if worker is None:
                continue
            if not isinstance(worker, threading.Thread):
                # These fields are internal lifecycle state.  Treat an
                # unexpected value as live rather than dropping a reference
                # that may still own application state.
                shutdown_complete = False
                continue
            if worker is current:
                # A worker cannot join itself.  Preserve every owned
                # reference until a later caller can finish the shutdown.
                shutdown_complete = False
                continue
            if not worker.is_alive():
                # ``Thread.join`` raises for a never-started thread.  Such a
                # value is already quiescent, so treat it as stopped without
                # calling join.
                continue
            worker.join(timeout=max(0.0, deadline - time.monotonic()))
            if worker.is_alive():
                shutdown_complete = False

        if shutdown_complete:
            # Do this as one commit point.  If a foreground owner scope or
            # owner_poll call is still active, its exit path will retry this
            # commit after releasing the native owner claim.  No owner lock
            # is acquired here, so cancellation remains bounded.
            with self._owner_activity:
                self._finalize_owned_state_locked()

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
                    if self._local_core is not None:
                        with self._incoming_lock:
                            if self._pending_edge is not None:
                                raise NetworkBackendError("duplicate pending EDGE_REQ")
                            self._pending_edge = (payload & 1, time.monotonic())
                        self._stats["edge_req_received"] = int(self._stats["edge_req_received"]) + 1
                    else:
                        try:
                            self._edge_queue.put_nowait(payload & 1)
                        except queue.Full as exc:
                            raise NetworkBackendError("incoming EDGE_REQ queue is full") from exc
                elif opcode == _OP_EDGE_RESP:
                    with self._response_lock:
                        if not self._response_pending:
                            raise NetworkBackendError("unsolicited or duplicate EDGE_RESP")
                        self._response_pending = False
                        try:
                            self._resp_queue.put_nowait(payload & 1)
                        except queue.Full as exc:
                            raise NetworkBackendError(
                                "unsolicited or duplicate EDGE_RESP"
                            ) from exc
                elif opcode == _OP_SYNC:
                    self._stats["sync_received"] = int(self._stats["sync_received"]) + 1
                    with self._sync_lock:
                        q = self._sync_queues.setdefault(payload, queue.Queue())
                    q.put(payload)
                elif opcode == _OP_EXCHANGE:
                    raw_len = self._recv_exactly(2)
                    (length,) = _LEN.unpack(raw_len)
                    data = self._recv_exactly(length)
                    self._stats["exchange_received"] = (
                        int(self._stats["exchange_received"]) + 1
                    )
                    with self._exchange_lock:
                        q = self._exchange_queues.setdefault(payload, queue.Queue())
                    q.put(data)
                else:
                    # Unknown opcode — drop. A strict implementation
                    # would raise and tear down; we log-and-continue
                    # to keep the trade robust to transient noise.
                    self._stats["unknown_opcode_count"] = (
                        int(self._stats["unknown_opcode_count"]) + 1
                    )
                    continue
        except NetworkBackendError as exc:
            # Peer closed or malformed frame. Surface via closed flag and
            # close the socket so every pending waiter wakes immediately.
            self._close_transport(exc)
        except OSError as exc:
            self._close_transport(exc)
        except Exception as exc:  # noqa: BLE001
            # Core/backend failures must reach blocked callers through the
            # same bounded error path as socket failures.  A bare reader
            # thread exception otherwise leaves the emulator waiting until a
            # long exchange timeout expires.
            self._close_transport(exc)

    def _edge_worker_loop(self) -> None:
        """Watch pending deadlines; never sample or mutate an attached core."""
        if self._local_core is not None:
            while not self._closed_event.wait(0.01):
                with self._incoming_lock:
                    pending = self._pending_edge
                if pending is not None and time.monotonic() >= pending[1] + _EDGE_CALL_TIMEOUT_SECONDS:
                    self._close_transport(NetworkBackendError("incoming EDGE_REQ owner deadline exhausted"))
                    return
            return
        # Explicit core-less transport compatibility has no emulation to own.
        while not self._closed:
            try:
                peer_bit = self._edge_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if peer_bit is None or self._closed:
                return
            try:
                self._handle_edge_req(peer_bit)
            except Exception as exc:  # noqa: BLE001
                self._close_transport(exc)
                return

    def _signal_edge_worker_stop(self) -> None:
        try:
            self._edge_queue.put_nowait(None)
        except queue.Full:
            pass

    def _close_transport(self, exc: Exception | None = None) -> None:
        """Fail closed and wake every transport waiter.

        EDGE frames are intentionally unnumbered for compatibility with the
        bundled PyBoy fork.  Once a request or response is lost, the peer may
        have advanced its serial core already; keeping the socket alive would
        make a later request consume an ambiguous late response.  Closing is
        therefore the bounded, deterministic recovery policy.
        """
        with self._close_lock:
            if exc is not None and self._reader_exc is None:
                self._reader_exc = exc
            self._closed = True
            self._closed_event.set()
            self._hello_received.set()
            self._signal_edge_worker_stop()
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass

    def _handle_edge_req(self, peer_bit: int) -> None:
        """Core-less compatibility only; workers never inspect native state."""
        if self._local_core is not None:
            raise NetworkBackendError("worker cannot access an attached serial core")
        self._stats["edge_req_received"] = int(self._stats["edge_req_received"]) + 1
        self._stats["slave_rearm_waits"] = int(self._stats["slave_rearm_waits"]) + 1
        self._stats["slave_idle_rearm_waits"] = int(self._stats["slave_idle_rearm_waits"]) + 1
        if self._closed_event.wait(_REARM_WAIT_SECONDS):
            return
        if self._keepalive_bit_idx == 0:
            self._stats["keepalive_bytes_started"] = int(self._stats["keepalive_bytes_started"]) + 1
        self._stats["keepalive_bits_sent"] = int(self._stats["keepalive_bits_sent"]) + 1
        self._stats["last_keepalive_state"] = {"core_present": False}
        bit = 0 if self._keepalive_bit_idx == 7 else 1
        self._keepalive_bit_idx = (self._keepalive_bit_idx + 1) & 7
        try:
            self._send_frame(_FRAME.pack(_OP_EDGE_RESP, bit))
            self._stats["edge_resp_sent"] = int(self._stats["edge_resp_sent"]) + 1
        except (OSError, NetworkBackendError) as exc:
            self._close_transport(exc)

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

    def _remaining(self, deadline: float | None) -> float:
        if self._closed_event.is_set():
            raise NetworkBackendError("backend closed")
        if deadline is None:
            return 0.05
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise NetworkBackendError("network IO deadline exceeded")
        return min(0.05, remaining)

    def _acquire_until(self, lock, deadline: float) -> None:
        while True:
            if lock.acquire(timeout=self._remaining(deadline)):
                return

    def _wait_socket(self, *, writing: bool, deadline: float | None) -> None:
        while True:
            wait = self._remaining(deadline)
            try:
                readable, writable, _ = select.select(
                    [] if writing else [self._sock],
                    [self._sock] if writing else [], [], wait,
                )
            except InterruptedError:
                continue
            except (OSError, ValueError) as exc:
                raise NetworkBackendError(f"socket readiness failed: {exc}") from exc
            if readable or writable:
                return

    def _send_frame(self, frame: bytes, *, deadline=None, before_send=None) -> None:
        """Serialize partial writes with cancellation and one absolute deadline.

        Control writes without a caller deadline use the edge IO bound. Public
        SYNC/exchange response timeouts still start after the frame is sent.
        """
        if deadline is None:
            deadline = time.monotonic() + _EDGE_CALL_TIMEOUT_SECONDS
        acquired = False
        try:
            self._acquire_until(self._write_lock, deadline)
            acquired = True
            self._remaining(deadline)
            if before_send is not None:
                before_send()
            view = memoryview(frame)
            while view:
                self._remaining(deadline)
                try:
                    sent = self._sock.send(view)
                except InterruptedError:
                    continue
                except BlockingIOError:
                    self._wait_socket(writing=True, deadline=deadline)
                    continue
                if sent == 0:
                    raise NetworkBackendError("peer closed socket during send")
                view = view[sent:]
        except (OSError, NetworkBackendError) as exc:
            self._close_transport(exc)
            raise
        finally:
            if acquired:
                self._write_lock.release()

    def _recv_exactly(self, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            self._remaining(None)
            try:
                chunk = self._sock.recv(n - len(buf))
            except InterruptedError:
                continue
            except BlockingIOError:
                self._wait_socket(writing=False, deadline=None)
                continue
            if not chunk:
                raise NetworkBackendError("peer closed socket mid-frame")
            buf.extend(chunk)
        return bytes(buf)

    def _queue_get(
        self,
        q: queue.Queue,
        *,
        timeout: float,
        timeout_message: str,
        deadline: float | None = None,
    ) -> Any:
        """Get a response while allowing :meth:`stop` to wake the waiter."""
        if deadline is None:
            deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if q is self._resp_queue and self._local_core is not None:
                self.owner_poll()
            if self._closed_event.is_set():
                if self._reader_exc is not None:
                    raise NetworkBackendError(
                        f"backend reader stopped: {self._reader_exc}"
                    ) from self._reader_exc
                raise NetworkBackendError("backend closed")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NetworkBackendError(timeout_message)
            try:
                value = q.get(timeout=min(0.01 if self._local_core is not None else 0.25, remaining))
            except queue.Empty:
                continue
            # A response dequeued concurrently with a transport failure is
            # not trustworthy: the peer may have applied a different frame
            # before the failure became visible locally.
            if self._closed_event.is_set():
                if self._reader_exc is not None:
                    raise NetworkBackendError(
                        f"backend reader stopped: {self._reader_exc}"
                    ) from self._reader_exc
                raise NetworkBackendError("backend closed")
            return value


__all__ = [
    "NetworkBackend",
    "NetworkBackendError",
]
