"""Edge identifiers, frame turns, sync and diagnostics. Extracted verbatim from ``network_backend.py`` (#126)."""

from __future__ import annotations

import json
import queue
import threading
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pokered_harness.link.network_backend as _entry
from pokered_harness.link._network_backend_support import (
    _CONTROL_QUEUE_MAXSIZE,
    _EDGE_ID_MAX,
    _EDGE_RESPONSE_HISTORY_MAX,
    _FRAME,
    _LEN,
    _MAX_SERIAL_TRANSCRIPT_ENTRIES,
    _OP_EXCHANGE,
    _OP_FRAME_ACK,
    _OP_FRAME_DONE,
    _OP_FRAME_TICK,
    _OP_HELLO,
    _OP_SYNC,
    _PROTOCOL_VERSION,
    _ROM_VERSION_CODES,
    NetworkBackendError,
    _coerce_payload,
    _require_timeout,
    _validate_id,
)


class _NetworkBackendProtocolMixin:
    """Edge identifiers, frame turns, sync and diagnostics."""

    def _edge_ids_enabled(self) -> bool:
        """Whether this transport has negotiated the live edge-id format."""
        return self._local_rom_version is not None and self._peer_rom_version is not None

    def _allocate_edge_id(self) -> int:
        """Return the next connection-scoped id without wrapping."""
        edge_id = self._next_edge_id
        if edge_id > _EDGE_ID_MAX:
            raise NetworkBackendError("EDGE_REQ id space exhausted; reconnect")
        self._next_edge_id += 1
        return edge_id

    def _claim_inbound_edge_id(self, edge_id: int) -> int | None:
        """Claim a new id or return the response for a safe replay.

        ``None`` means that the id is new. A response bit means the request
        was already completed and can be replayed without touching the core.
        Pending duplicates and ids older than the high-water mark are
        protocol errors; retaining them as new work would apply a serial edge
        twice after the bounded replay window has evicted its response.
        """
        if not isinstance(edge_id, int) or isinstance(edge_id, bool):
            raise NetworkBackendError("invalid EDGE_REQ id")
        if not 1 <= edge_id <= _EDGE_ID_MAX:
            raise NetworkBackendError(f"invalid EDGE_REQ id {edge_id}")
        with self._edge_id_lock:
            response = self._inbound_edge_responses.get(edge_id)
            if response is not None:
                return response
            if edge_id in self._inbound_edge_ids_pending:
                raise NetworkBackendError(f"duplicate pending EDGE_REQ id {edge_id}")
            if edge_id <= self._highest_inbound_edge_id:
                raise NetworkBackendError(f"stale EDGE_REQ id {edge_id}")
            self._highest_inbound_edge_id = edge_id
            self._inbound_edge_ids_pending.add(edge_id)
        return None

    def _release_inbound_edge_id(self, edge_id: int | None) -> None:
        """Release a claimed id when queue admission fails."""
        if edge_id is None:
            return
        with self._edge_id_lock:
            self._inbound_edge_ids_pending.discard(edge_id)

    def _remember_inbound_edge_response(self, edge_id: int | None, bit: int) -> None:
        """Record a successful response for bounded duplicate replay."""
        if edge_id is None:
            return
        with self._edge_id_lock:
            self._inbound_edge_ids_pending.discard(edge_id)
            if edge_id in self._inbound_edge_responses:
                # A replay does not extend the replay window or duplicate
                # its eviction marker.
                return
            self._inbound_edge_responses[edge_id] = bit & 1
            self._inbound_edge_response_order.append(edge_id)
            while len(self._inbound_edge_response_order) > _EDGE_RESPONSE_HISTORY_MAX:
                expired = self._inbound_edge_response_order.popleft()
                self._inbound_edge_responses.pop(expired, None)

    @contextmanager
    def _edge_admission_guard(self, lock: Any, deadline: float) -> Iterator[None]:
        """Acquire before publishing an edge, without terminal side effects."""
        while True:
            if self._closed_event.is_set() or self._closed:
                raise NetworkBackendError("backend closed")
            remaining = deadline - _entry.time.monotonic()
            if remaining <= 0:
                raise NetworkBackendError("EDGE_REQ admission timed out")
            if lock.acquire(timeout=min(_entry._SEND_POLL_SECONDS, remaining)):
                break
        try:
            if self._closed_event.is_set() or self._closed:
                raise NetworkBackendError("backend closed")
            # Acquisition may finish at the deadline after scheduler delay.
            if _entry.time.monotonic() >= deadline:
                raise NetworkBackendError("EDGE_REQ admission timed out")
            yield
        finally:
            lock.release()

    def begin_frame_turn(
        self,
        *,
        leader: bool,
        cancel_event: threading.Event | None = None,
    ) -> None:
        """Start one bounded owner-frame turn after clock negotiation.

        Cancellation is terminal for this transport. The frame marker has no
        request id, so a cancelled turn cannot safely be resumed on the same
        connection.
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
                    timeout=_entry._EDGE_RESPONSE_TIMEOUT_SECONDS,
                    operation="FRAME_TICK",
                    cancel_event=cancel_event,
                ) as write_deadline:
                    self._send_frame(
                        _FRAME.pack(_OP_FRAME_TICK, 0),
                        timeout=max(0.0, write_deadline - _entry.time.monotonic()),
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
                timeout=_entry._EDGE_RESPONSE_TIMEOUT_SECONDS,
                timeout_message=(
                    f"no FRAME_TICK from peer within {_entry._EDGE_RESPONSE_TIMEOUT_SECONDS:g}s"
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

        The ACK wait has one absolute transport deadline. If an owner
        ``progress_callback`` is supplied, edge publication wakes it so
        peer-driven edges can be applied even when the native clock role
        differs from the negotiated pacing role. Bounded polling remains for
        cancellation and deferred work needing further CPU progress.
        Cancellation is terminal for the same reason as :meth:`begin_frame_turn`.
        """
        if not isinstance(leader, bool):
            raise TypeError("leader must be a bool")
        if leader:
            try:
                with self._write_guard(
                    timeout=_entry._EDGE_RESPONSE_TIMEOUT_SECONDS,
                    operation="FRAME_DONE",
                    cancel_event=cancel_event,
                ) as write_deadline:
                    self._send_frame(
                        _FRAME.pack(_OP_FRAME_DONE, 0),
                        timeout=max(0.0, write_deadline - _entry.time.monotonic()),
                        operation="FRAME_DONE",
                        cancel_event=cancel_event,
                    )
                self._stats["frame_dones_sent"] = int(self._stats["frame_dones_sent"]) + 1
                # A peer may begin a native-clock transfer only after
                # FRAME_DONE is sent. In owner-dispatch mode its EDGE_REQ is
                # queued for this backend's owner, and the peer cannot
                # produce FRAME_ACK until that edge is applied. Keep the
                # transport wait bounded, while giving owner dispatch work a
                # chance to run when inbound work becomes available.
                if progress_callback is None and self._dispatch_to_owner:
                    progress_callback = lambda: self.service_pending_edges(max_edges=1)
                self._queue_get(
                    self._frame_ack_queue,
                    timeout=_entry._EDGE_RESPONSE_TIMEOUT_SECONDS,
                    timeout_message=(
                        f"no FRAME_ACK from peer within {_entry._EDGE_RESPONSE_TIMEOUT_SECONDS:g}s"
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
        deadline = _entry.time.monotonic() + _entry._EDGE_RESPONSE_TIMEOUT_SECONDS
        frame_done_received = False
        try:
            while True:
                with self._edge_pending_condition:
                    generation = self._transport_generation
                if cancel_event is not None and cancel_event.is_set():
                    raise NetworkBackendError("frame completion cancelled")
                if self._closed_event.is_set():
                    raise NetworkBackendError("backend closed during frame completion")
                self._run_progress_callback(progress_callback)
                if cancel_event is not None and cancel_event.is_set():
                    raise NetworkBackendError("frame completion cancelled")
                if self._closed_event.is_set():
                    raise NetworkBackendError("backend closed during frame completion")
                if not frame_done_received:
                    try:
                        self._frame_done_queue.get_nowait()
                    except queue.Empty:
                        pass
                    else:
                        frame_done_received = True
                if frame_done_received:
                    with self._edge_pending_condition:
                        if self._edge_pending == 0:
                            break
                remaining = deadline - _entry.time.monotonic()
                if remaining <= 0:
                    raise NetworkBackendError(
                        f"no FRAME_DONE from peer within {_entry._EDGE_RESPONSE_TIMEOUT_SECONDS:g}s"
                    )
                self._wait_for_transport_change(
                    generation, timeout=min(_entry._SEND_POLL_SECONDS, remaining)
                )
        except NetworkBackendError as exc:
            if not self._closed_event.is_set():
                self._mark_closed(exc)
            raise
        try:
            with self._write_guard(
                timeout=_entry._EDGE_RESPONSE_TIMEOUT_SECONDS,
                operation="FRAME_ACK",
                cancel_event=cancel_event,
            ) as write_deadline:
                self._send_frame(
                    _FRAME.pack(_OP_FRAME_ACK, 0),
                    timeout=max(0.0, write_deadline - _entry.time.monotonic()),
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
        deadline = _entry.time.monotonic() + timeout
        try:
            self._send_sync_with_timeout(
                sync_id,
                timeout=max(0.0, deadline - _entry.time.monotonic()),
                cancel_event=cancel_event,
            )
            self._queue_get(
                q,
                timeout=max(0.0, deadline - _entry.time.monotonic()),
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
        deadline = _entry.time.monotonic() + timeout
        try:
            with self._write_guard(
                deadline=deadline,
                operation=f"OP_EXCHANGE({kind_id})",
                cancel_event=cancel_event,
            ) as write_deadline:
                self._send_frame(
                    frame,
                    timeout=max(0.0, write_deadline - _entry.time.monotonic()),
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
            timeout=max(0.0, deadline - _entry.time.monotonic()),
            timeout_message=f"no peer OP_EXCHANGE({kind_id}) within {timeout}s",
            cancel_event=cancel_event,
        )

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
        deadline = _entry.time.monotonic() + timeout
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
                remaining = deadline - _entry.time.monotonic()
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
                    remaining = deadline - _entry.time.monotonic()
                    if remaining > 0:
                        self._edge_pending_condition.wait(
                            timeout=min(_entry._SEND_POLL_SECONDS, remaining)
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
            snap["held_owner_byte_response"] = self._held_owner_byte_response is not None
        snap["closed"] = closed
        snap["reader_started"] = self._reader is not None
        now = _entry.time.monotonic()
        snap["active_exchange"] = now < self._active_exchange_until
        snap["post_byte_rearm_grace"] = now < self._post_byte_rearm_until
        snap["consecutive_armed_edges"] = self._consecutive_armed_edges
        with self._edge_pending_condition:
            snap["pending_edge_requests"] = self._edge_pending
        with self._owner_response_condition:
            snap["owner_response_pending"] = self._owner_response_pending
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
            "monotonic_s": _entry.time.monotonic(),
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
            timeout=_entry._DEFAULT_SEND_TIMEOUT_SECONDS,
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
                    timeout=max(0.0, write_deadline - _entry.time.monotonic()),
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
                timeout=_entry._DEFAULT_SEND_TIMEOUT_SECONDS,
                operation="HELLO",
            ) as write_deadline:
                self._send_frame(
                    _FRAME.pack(_OP_HELLO, payload),
                    timeout=max(0.0, write_deadline - _entry.time.monotonic()),
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
