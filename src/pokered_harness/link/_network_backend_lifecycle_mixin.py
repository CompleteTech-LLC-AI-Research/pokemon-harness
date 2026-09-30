"""Connection lifecycle: construction, handshake, teardown. Extracted verbatim from ``network_backend.py`` (#126)."""

from __future__ import annotations

import queue
import socket
import threading
from collections import deque
from collections.abc import Callable

import pokered_harness.link.network_backend as _entry
from pokered_harness.link._network_backend_support import (
    NetworkBackendError,
    _InboundEdge,
    _InboundEdgeQueue,
    _require_timeout,
    _validate_connected_socket_loopback,
    _validate_rom_version,
)
from pokered_harness.link.serial_coordinator import SerialOperationGate


class _NetworkBackendLifecycleMixin:
    """Connection lifecycle: construction, handshake, teardown."""

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
        # Versioned peers add an id to each edge so a late response cannot be
        # matched to a later edge.  The old two-byte mode retains its strict
        # single-slot response queue and fail-closed behavior.
        self._edge_call_lock = threading.Lock()
        self._edge_response_lock = threading.Lock()
        # Serialize an inbound EDGE_REQ with completion of the response for
        # the preceding owner/worker request.  The peer is allowed to send
        # its next edge as soon as it receives that response; keeping the
        # pending-count decrement in this same critical section prevents the
        # reader from admitting the next request while the worker's finally
        # block is still catching up.
        self._edge_wire_completion_lock = threading.Lock()
        self._edge_inflight = False
        self._edge_inflight_id: int | None = None
        self._edge_response_seen = False
        self._next_edge_id = 1
        # While the emulator owner is blocked in its own master edge, a peer
        # may legitimately present the reciprocal master edge. The sampled
        # output bit is sufficient to answer that wire event; the reader must
        # not inspect or mutate the serial core to do so.
        self._reciprocal_master_bit: int | None = None
        # Only the first unique request which crosses a local master edge is
        # answered from that sampled bit. Further requests are queued for the
        # owner; request ids make this bounded arbitration safe even when a
        # peer immediately starts its next edge after receiving the response.
        self._reciprocal_master_response_sent = False
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
        self._defer_owner_byte_responses = False
        self._held_owner_byte_response: _InboundEdge | None = None
        # Deferred owner byte responses are counted from release until the
        # response worker has written the frame. A completed owner frame can
        # clear the held reference before the worker transmits it, so a new
        # local master edge must wait on this count rather than only on the
        # held reference, or it can overtake the prior EDGE_RESP on the wire.
        self._owner_response_condition = threading.Condition()
        self._owner_response_pending = 0
        # Counts EDGE_REQ frames from enqueue until their response has been
        # written.  A phase barrier can therefore wait for the wire work
        # already admitted by the reader without mistaking an armed-but-idle
        # ROM SC register for an in-flight transfer.
        self._edge_pending_condition = threading.Condition()
        self._edge_pending = 0
        # Queue publication, control frames, response completion, and close
        # share a wakeup generation. Owners snapshot it before checking work
        # outside this lock, then wait only if nothing changed in between.
        self._transport_generation = 0
        # Live HELLO peers use monotonically increasing request ids. Keep a
        # bounded replay window for late duplicate requests and a high-water
        # mark so an evicted old id can never be mistaken for a new edge.
        self._edge_id_lock = threading.Lock()
        self._inbound_edge_ids_pending: set[int] = set()
        self._inbound_edge_responses: dict[int, int] = {}
        self._inbound_edge_response_order: deque[int] = deque()
        self._highest_inbound_edge_id = 0
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
            "reciprocal_master_edges": 0,
            "edge_id_req_sent": 0,
            "edge_id_req_received": 0,
            "edge_id_resp_sent": 0,
            "edge_id_resp_received": 0,
            "edge_id_duplicates_replayed": 0,
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
        deadline = _entry.time.monotonic() + timeout
        while not self._hello_received.is_set():
            if cancel_event is not None and cancel_event.is_set():
                error = NetworkBackendError("HELLO wait cancelled")
                self._mark_closed(error)
                raise error
            remaining = deadline - _entry.time.monotonic()
            if remaining <= 0:
                error = NetworkBackendError(f"peer HELLO not received within {timeout:g}s")
                self._mark_closed(error)
                raise error
            self._hello_received.wait(timeout=min(_entry._SEND_POLL_SECONDS, remaining))
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
        defer_byte_responses: bool = False,
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

        ``defer_byte_responses`` additionally retains the final-bit response
        until the owner completes a subsequent ordinary emulator frame. The
        native SB/SC/IF event still occurs immediately. This is conservative
        transport pacing for an existing ROM receive mailbox, not a shared
        emulated clock or a claim that arbitrary ROM code consumed the byte.
        Call :meth:`begin_owner_frame` and :meth:`finish_owner_frame` around
        actual frame execution when enabling it. If that execution changes
        the ROM to master mode, its next native master edge first flushes
        the held response and waits for its wire accounting to settle.

        ``EDGE_RESP`` frames (responses to our own master-side
        ``on_edge`` requests) are put on the response queue for the
        blocking ``on_edge`` call to pick up.

        """
        if type(defer_byte_responses) is not bool:
            raise TypeError("defer_byte_responses must be a bool")
        if defer_byte_responses and not dispatch_to_owner:
            raise ValueError("defer_byte_responses requires dispatch_to_owner=True")
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
                        if self._held_owner_byte_response is not None:
                            raise NetworkBackendError(
                                "cannot rebind while an owner byte response is held"
                            )
                        self._local_core = local_core
                        self._irq_callback = irq_callback
                        self._defer_owner_byte_responses = defer_byte_responses
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
            self._defer_owner_byte_responses = defer_byte_responses
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
        remaining = max(0.0, deadline - _entry.time.monotonic())
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
                pre_close["held_owner_byte_response"] = self._held_owner_byte_response is not None
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
                self._held_owner_byte_response = None
                self._notify_transport_change()
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
        deadline = _entry.time.monotonic() + timeout_s

        # Serialize this transition with start_receiver publication.  The
        # lock is acquired with the same total deadline as the quiescence
        # wait, making a concurrent receiver setup bounded and retryable.
        remaining = max(0.0, deadline - _entry.time.monotonic())
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
                    self._defer_owner_byte_responses = False
                    return True
                self._local_core_detaching = True
                while self._local_core_users:
                    remaining = deadline - _entry.time.monotonic()
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
                self._defer_owner_byte_responses = False
                self._local_core_detached = True
                self._local_core_detaching = False
                self._local_core_condition.notify_all()
            if self._held_owner_byte_response is not None:
                # The native byte committed, but its peer has not observed
                # the final response. Retiring the core cannot transfer this
                # unfinished exchange to a newly attached emulator.
                self._mark_closed(
                    NetworkBackendError("local core detached with a held byte response"),
                    deadline=deadline,
                )
            return True
        finally:
            self._receiver_start_lock.release()

    def stop(self, *, timeout_s: float = 2.0) -> bool:
        timeout_s = _require_timeout(timeout_s, "timeout_s")
        deadline = _entry.time.monotonic() + timeout_s
        # Serialize lifecycle teardown with receiver setup so stop cannot
        # observe a half-published worker pair and return before the newly
        # started threads are signalled and joined.
        remaining = max(0.0, deadline - _entry.time.monotonic())
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
            edge_worker.join(timeout=max(0.0, deadline - _entry.time.monotonic()))

        if isinstance(reader, threading.Thread) and reader is not threading.current_thread():
            reader.join(timeout=max(0.0, deadline - _entry.time.monotonic()))
        workers_stopped = not any(
            isinstance(worker, threading.Thread)
            and worker is not threading.current_thread()
            and worker.is_alive()
            for worker in (edge_worker, reader)
        )
        return closed_coordinated and workers_stopped
