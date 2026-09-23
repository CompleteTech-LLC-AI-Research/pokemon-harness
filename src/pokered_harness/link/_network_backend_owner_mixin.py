"""Reader loop and emulator-owner edge dispatch. Extracted verbatim from ``network_backend.py`` (#126)."""

from __future__ import annotations

import queue
import struct
import threading

import pokered_harness.link.network_backend as _entry
from pokered_harness.link._network_backend_support import (
    _CONTROL_QUEUE_MAXSIZE,
    _FRAME,
    _LEN,
    _OP_EDGE_REQ,
    _OP_EDGE_REQ_ID,
    _OP_EDGE_RESP,
    _OP_EDGE_RESP_ID,
    _OP_EXCHANGE,
    _OP_FRAME_ACK,
    _OP_FRAME_DONE,
    _OP_FRAME_TICK,
    _OP_HELLO,
    _OP_SYNC,
    _PROTOCOL_VERSION,
    _ROM_VERSION_NAMES,
    NetworkBackendError,
    _InboundEdge,
)


class _NetworkBackendOwnerMixin:
    """Reader loop and emulator-owner edge dispatch."""

    # --- internals ----------------------------------------------------

    def _reader_loop(self) -> None:
        try:
            while not self._closed:
                frame = self._recv_exactly(2)
                opcode, payload = _FRAME.unpack(frame)
                edge_id: int | None = None
                if opcode in (_OP_EDGE_REQ_ID, _OP_EDGE_RESP_ID):
                    raw_edge_id = self._recv_exactly(4)
                    (edge_id,) = struct.unpack(">I", raw_edge_id)
                    if edge_id == 0:
                        raise NetworkBackendError("invalid identified edge id 0")
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
                elif opcode in (_OP_EDGE_REQ, _OP_EDGE_REQ_ID):
                    # Keep the reader behind the response worker's wire
                    # completion point.  Otherwise a peer can receive an
                    # EDGE_RESP, immediately send its next EDGE_REQ, and
                    # have this branch increment _edge_pending before the
                    # previous worker finally block decrements it.
                    with self._edge_wire_completion_lock:
                        if payload > 1:
                            raise NetworkBackendError(
                                f"invalid EDGE_REQ bit payload {payload}"
                            )
                        if edge_id is not None:
                            replay_bit = self._claim_inbound_edge_id(edge_id)
                            if replay_bit is not None:
                                self._stats["edge_id_duplicates_replayed"] = int(
                                    self._stats["edge_id_duplicates_replayed"]
                                ) + 1
                                self._record_serial_event(
                                    "edge_req_duplicate_replayed",
                                    direction="peer_to_local",
                                    edge_bit=payload & 1,
                                    edge_id=edge_id,
                                    response_bit=replay_bit,
                                )
                                self._send_edge_response(
                                    _InboundEdge(
                                        peer_bit=payload & 1,
                                        response_bit=replay_bit,
                                        edge_id=edge_id,
                                    )
                                )
                                continue
                            self._stats["edge_id_req_received"] = int(
                                self._stats["edge_id_req_received"]
                            ) + 1
                        if self._send_reciprocal_master_response(
                            payload & 1,
                            edge_id=edge_id,
                        ):
                            continue
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
                        request = _InboundEdge(peer_bit=payload & 1, edge_id=edge_id)
                        self._record_serial_event(
                            "edge_req_received",
                            direction="peer_to_local",
                            edge_bit=payload & 1,
                            edge_id=edge_id,
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
                                self._notify_transport_change()
                            self._release_inbound_edge_id(edge_id)
                            raise NetworkBackendError(
                                "incoming EDGE_REQ queue is full"
                            ) from exc
                        # Pending accounting is admission, not queue readiness.
                        # Wake owners only after the request is dispatchable.
                        self._notify_transport_change()
                elif opcode in (_OP_EDGE_RESP, _OP_EDGE_RESP_ID):
                    if payload > 1:
                        raise NetworkBackendError(f"invalid EDGE_RESP bit payload {payload}")
                    with self._edge_response_lock:
                        if not self._edge_inflight:
                            raise NetworkBackendError("unsolicited EDGE_RESP")
                        expected_edge_id = self._edge_inflight_id
                        if expected_edge_id != edge_id:
                            if expected_edge_id is None:
                                raise NetworkBackendError(
                                    "unexpected identified EDGE_RESP for legacy EDGE_REQ"
                                )
                            raise NetworkBackendError(
                                f"EDGE_RESP id {edge_id} does not match in-flight id "
                                f"{expected_edge_id}"
                            )
                        try:
                            self._resp_queue.put_nowait(payload & 1)
                        except queue.Full as exc:
                            raise NetworkBackendError("duplicate or unsolicited EDGE_RESP") from exc
                        self._edge_response_seen = True
                    if edge_id is not None:
                        self._stats["edge_id_resp_received"] = int(
                            self._stats["edge_id_resp_received"]
                        ) + 1
                    self._record_serial_event(
                        "edge_resp_received",
                        direction="peer_to_local",
                        edge_bit=payload & 1,
                        edge_id=edge_id,
                    )
                    self._notify_transport_change()
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
                    self._notify_transport_change()
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
                    self._notify_transport_change()
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
                    self._notify_transport_change()
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
                    self._notify_transport_change()
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
                    self._notify_transport_change()
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
            with self._edge_wire_completion_lock:
                try:
                    if self._closed:
                        return
                    self._handle_edge_req(request)
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
            with self._edge_wire_completion_lock:
                try:
                    if not self._closed:
                        self._send_edge_response(request)
                except Exception as exc:  # noqa: BLE001
                    self._mark_closed(exc)
                    return
                finally:
                    self._decrement_edge_pending()
                    if request.response_finished is not None:
                        request.response_finished.set()
                        self._settle_owner_response()

    def _reserve_queued_master_collision(self) -> _InboundEdge | None:
        """Answer a peer master edge admitted before this owner's edge.

        The reader handles requests arriving during ``on_edge`` directly.
        A request queued just before entry needs the same sampled-bit path:
        the owner cannot return to its normal pump until its response arrives.
        Reserve before sending our request; its response may arrive before
        we answer this queued edge. The caller sends our request first.
        """
        if not self._dispatch_to_owner:
            return None
        with self._serial_gate, self._edge_wire_completion_lock:
            try:
                request = self._edge_queue.get_nowait()
            except queue.Empty:
                return None
            if request is None:
                self._edge_queue.put_nowait(None)
                return None
            with self._edge_response_lock:
                if self._reciprocal_master_response_sent:
                    request.deferred = True
                    self._edge_queue.put_nowait(request)
                    return None
                self._reciprocal_master_response_sent = True
                request.response_bit = self._reciprocal_master_bit
                return request

    def _send_reciprocal_master_response(
        self,
        peer_bit: int,
        *,
        edge_id: int | None = None,
    ) -> bool:
        """Answer a peer edge that collides with this owner's master edge.

        The reader may run while the owner is blocked in ``on_edge``. In that
        narrow interval the sampled local output bit is immutable and is the
        only serial value needed for the reciprocal wire response. This path
        deliberately does not read the local core or invoke callbacks; both
        emulators retain ownership of their own master edge. Identified
        requests are admitted once per local edge; legacy requests use the
        same crossed-edge guard and keep their historical two-byte response
        format.
        """
        with self._edge_response_lock:
            # Once our own response has arrived, the local master edge is
            # complete even if its caller has not yet cleared the admission
            # flag. A request arriving in that small gap is the peer's next
            # edge and must go through the owner. Identified peers also get
            # one reciprocal response per local edge; duplicates are replayed
            # by the reader before reaching this path.
            if not self._edge_inflight or self._edge_response_seen or (
                edge_id is not None and self._reciprocal_master_response_sent
            ):
                return False
            bit = self._reciprocal_master_bit
            if edge_id is not None:
                self._reciprocal_master_response_sent = True
        if bit is None:
            return False
        request = _InboundEdge(
            peer_bit=peer_bit,
            response_bit=bit,
            completed=False,
            edge_id=edge_id,
        )
        self._stats["edge_req_received"] = int(self._stats["edge_req_received"]) + 1
        self._stats["reciprocal_master_edges"] = (
            int(self._stats["reciprocal_master_edges"]) + 1
        )
        self._record_serial_event(
            "reciprocal_master_edge",
            direction="peer_to_local",
            edge_bit=peer_bit,
            response_bit=bit,
            edge_id=edge_id,
        )
        self._send_edge_response(request)
        return True

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
            if self._held_owner_byte_response is not None:
                break
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
                if request.completed and self._defer_owner_byte_responses:
                    request.response_finished = threading.Event()
                    # Deadline-aware close can proceed without dispatch
                    # quiescence. Serialize this small publication with its
                    # terminal state so it cannot leave a held response after
                    # close has cleared the pending accounting.
                    with self._close_lock:
                        if not self._closed:
                            self._held_owner_byte_response = request
                    applied += 1
                    break
                self._publish_owner_response(request)
                applied += 1
        return applied

    def _publish_owner_response(self, request: _InboundEdge) -> None:
        """Queue a completed edge for transport-only response transmission."""
        if self._closed:
            self._decrement_edge_pending()
            if request.response_finished is not None:
                # A deferred response counted at release is abandoned here
                # because a deadline-aware close won the race. Retire the
                # receipt so outstanding-work accounting stays truthful.
                request.response_finished.set()
                self._settle_owner_response()
            return
        try:
            self._completed_edge_queue.put_nowait(request)
        except queue.Full as exc:
            self._mark_closed(
                NetworkBackendError("completed EDGE_REQ response queue is full")
            )
            self._decrement_edge_pending()
            raise NetworkBackendError("completed EDGE_REQ response queue is full") from exc

    def begin_owner_frame(self) -> _InboundEdge | None:
        """Snapshot a held byte before actual ordinary owner-frame execution.

        A byte completed inside that frame is intentionally ineligible for
        release by this token. Its interrupt may have occurred at the very
        end of the frame, before the CPU had time to consume its mailbox.
        """
        with self._serial_gate, self._local_core_access(), self._owner_dispatch_lock:
            return self._held_owner_byte_response

    def finish_owner_frame(self, token: _InboundEdge | None) -> None:
        """Release the byte held before a successfully completed owner frame."""
        if token is None:
            return
        with self._serial_gate, self._local_core_access(), self._owner_dispatch_lock:
            self._release_owner_byte_response(token)

    def _begin_owner_response(self) -> None:
        """Count one released response that still owes a wire transmission."""
        with self._owner_response_condition:
            self._owner_response_pending += 1
            self._owner_response_condition.notify_all()

    def _settle_owner_response(self) -> None:
        """Retire one response after its frame has been written or abandoned."""
        with self._owner_response_condition:
            if self._owner_response_pending > 0:
                self._owner_response_pending -= 1
            self._owner_response_condition.notify_all()

    def _release_owner_byte_response(self, token: _InboundEdge) -> None:
        """Release one token while the owner retains dispatch admission."""
        with self._close_lock:
            if self._closed or self._held_owner_byte_response is not token:
                return
            self._held_owner_byte_response = None
            self._begin_owner_response()
        try:
            self._publish_owner_response(token)
        except BaseException:
            # The response was counted but never queued; retire the receipt so
            # a waiter learns about the failure through close, not by hanging.
            self._settle_owner_response()
            raise

    def _finish_held_response_before_master_edge(self, deadline: float) -> None:
        if self._held_owner_byte_response is None and self._owner_response_pending == 0:
            # Transport-only peers have no local serial core; never touch the
            # core-access gate when there is nothing outstanding.
            return
        with self._serial_gate, self._local_core_access(), self._owner_dispatch_lock:
            request = self._held_owner_byte_response
            if request is not None:
                self._release_owner_byte_response(request)
        # A completed owner frame can release the held byte before the response
        # worker writes it. Wait for every outstanding deferred response, not
        # just the currently held reference, so a new EDGE_REQ cannot precede
        # the prior EDGE_RESP on the wire.
        timed_out = False
        while True:
            if self._closed_event.is_set():
                raise NetworkBackendError("backend closed while completing prior byte response")
            with self._owner_response_condition:
                if self._owner_response_pending == 0:
                    break
                remaining = deadline - _entry.time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                self._owner_response_condition.wait(min(_entry._SEND_POLL_SECONDS, remaining))
        if timed_out:
            error = NetworkBackendError("prior byte response exceeded EDGE_REQ deadline")
            self._mark_closed(error)
            raise error
        if self._closed_event.is_set():
            raise NetworkBackendError("backend closed while completing prior byte response")

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
                if transfer_enabled:
                    # A scheduled local master edge owns a real output bit.
                    # Keep this peer request queued until on_edge samples
                    # that bit; emitting no-data here corrupts role election.
                    return False
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
        now = _entry.time.monotonic()
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
            self._stats["last_slave_byte_complete_at"] = _entry.time.monotonic()
            self._notify_completed_slave_irq(isolate_callback_errors=False)
