"""On-edge exchange, frame IO and wire-idle helpers. Extracted verbatim from ``network_backend.py`` (#126)."""

from __future__ import annotations

import json
import math
import queue
import select
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pokered_harness.link.network_backend as _entry
from pokered_harness.link._network_backend_support import (
    _ACTIVE_EXCHANGE_EDGE_THRESHOLD,
    _ACTIVE_EXCHANGE_GRACE_SECONDS,
    _ACTIVE_EXCHANGE_REARM_WAIT_SECONDS,
    _EDGE_ID_FRAME,
    _FRAME,
    _OP_EDGE_REQ,
    _OP_EDGE_REQ_ID,
    _OP_EDGE_RESP,
    _OP_EDGE_RESP_ID,
    _POST_BYTE_REARM_GRACE_SECONDS,
    _READ_POLL_SECONDS,
    _REARM_WAIT_SECONDS,
    NetworkBackendError,
    _InboundEdge,
    _require_timeout,
)


class _NetworkBackendIOMixin:
    """On-edge exchange, frame IO and wire-idle helpers."""

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
        deadline = _entry.time.monotonic() + _entry._EDGE_RESPONSE_TIMEOUT_SECONDS
        # Admission failure must not close or mutate another caller's exchange.
        with self._edge_admission_guard(self._edge_call_lock, deadline):
            # The ROM can change clock roles during the continuation which
            # owns a held slave-byte response. Complete that earlier wire
            # event before publishing a new local master edge. Otherwise
            # the peer could classify our new request as a collision with
            # its unfinished last bit and respond from the wrong byte.
            self._finish_held_response_before_master_edge(deadline)
            stale_error: NetworkBackendError | None = None
            with self._edge_admission_guard(self._edge_response_lock, deadline):
                if self._closed:
                    raise NetworkBackendError("backend closed")
                if self._edge_inflight:
                    raise NetworkBackendError("concurrent EDGE_REQ is not supported")
                if not self._resp_queue.empty():
                    stale_error = NetworkBackendError("stale EDGE_RESP before EDGE_REQ")
                else:
                    edge_id = self._allocate_edge_id() if self._edge_ids_enabled() else None
                    self._edge_inflight = True
                    self._edge_inflight_id = edge_id
                    self._edge_response_seen = False
                    self._reciprocal_master_bit = our_bit & 1
                    self._reciprocal_master_response_sent = False

            if stale_error is not None:
                self._mark_closed(stale_error)
                raise stale_error

            edge_id = self._edge_inflight_id
            if edge_id is None:
                frame_out = _FRAME.pack(_OP_EDGE_REQ, our_bit & 1)
            else:
                frame_out = _EDGE_ID_FRAME.pack(_OP_EDGE_REQ_ID, our_bit & 1, edge_id)
            self._stats["edge_req_sent"] = int(self._stats["edge_req_sent"]) + 1
            if edge_id is not None:
                self._stats["edge_id_req_sent"] = int(self._stats["edge_id_req_sent"]) + 1
            self._record_serial_event(
                "edge_req_sent",
                direction="local_to_peer",
                edge_bit=our_bit & 1,
                edge_id=edge_id,
            )
            # Reuse the admission deadline rather than granting send and
            # receive fresh budgets. In legacy mode a timeout remains
            # terminal because the two-byte response has no id; in the
            # versioned mode a late response is rejected by its id.
            queued_collision = None
            try:
                queued_collision = self._reserve_queued_master_collision()
                with self._write_guard(
                    deadline=deadline,
                    operation="EDGE_REQ",
                ) as write_deadline:
                    self._send_frame(
                        frame_out,
                        timeout=max(0.0, write_deadline - _entry.time.monotonic()),
                        operation="EDGE_REQ",
                    )
                if queued_collision is not None:
                    self._stats["edge_req_received"] = int(self._stats["edge_req_received"]) + 1
                    self._stats["reciprocal_master_edges"] = int(self._stats["reciprocal_master_edges"]) + 1
                    self._record_serial_event(
                        "reciprocal_master_edge",
                        direction="peer_to_local",
                        edge_bit=queued_collision.peer_bit,
                        response_bit=queued_collision.response_bit,
                        edge_id=queued_collision.edge_id,
                    )
                    with self._edge_wire_completion_lock:
                        self._send_edge_response(queued_collision)
                        self._decrement_edge_pending()
                        queued_collision = None
                bit = self._queue_get(
                    self._resp_queue,
                    timeout=max(0.0, deadline - _entry.time.monotonic()),
                    timeout_message=(
                        f"no EDGE_RESP from peer within {_entry._EDGE_RESPONSE_TIMEOUT_SECONDS:g}s"
                    ),
                )
                self._stats["edge_resp_received"] = int(self._stats["edge_resp_received"]) + 1
                with self._edge_response_lock:
                    self._edge_response_seen = True
                self._record_serial_event(
                    "edge_resp_consumed",
                    direction="peer_to_local",
                    edge_bit=bit,
                    edge_id=edge_id,
                )
                return bit
            except OSError as exc:
                error = NetworkBackendError(f"failed to send EDGE_REQ: {exc}")
                self._mark_closed(error)
                raise error from exc
            except NetworkBackendError as exc:
                # A timed-out edge cannot be retried on this connection. In
                # legacy mode a delayed response has no id; in versioned
                # mode the peer may still have an outstanding request, and
                # closing is still the safest response to a failed waiter.
                if not self._closed_event.is_set():
                    self._mark_closed(exc)
                raise
            finally:
                if queued_collision is not None:
                    self._decrement_edge_pending()
                with self._edge_response_lock:
                    self._edge_inflight = False
                    self._edge_inflight_id = None
                    self._edge_response_seen = False
                    self._reciprocal_master_bit = None
                    self._reciprocal_master_response_sent = False
                self._notify_transport_change()

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
                timeout=_entry._EDGE_RESPONSE_TIMEOUT_SECONDS,
                operation="EDGE_RESP",
            ) as write_deadline:
                if self._closed:
                    self._record_serial_event(
                        "edge_resp_send",
                        direction="local_to_peer",
                        edge_bit=request.response_bit,
                        byte_complete=request.completed,
                        edge_id=request.edge_id,
                        outcome="not_sent_closed",
                    )
                    return
                if request.edge_id is None:
                    frame = _FRAME.pack(_OP_EDGE_RESP, request.response_bit)
                else:
                    frame = _EDGE_ID_FRAME.pack(
                        _OP_EDGE_RESP_ID,
                        request.response_bit,
                        request.edge_id,
                    )
                # Publish the completed response before releasing the write
                # lock.  The peer may send a duplicate as soon as it has
                # received the first response; recording only after the
                # socket write leaves a race where the reader still sees the
                # request id as pending and closes the connection.
                self._remember_inbound_edge_response(
                    request.edge_id, request.response_bit
                )
                self._send_frame(
                    frame,
                    timeout=max(0.0, write_deadline - _entry.time.monotonic()),
                    operation="EDGE_RESP",
                )
                self._stats["edge_resp_sent"] = int(self._stats["edge_resp_sent"]) + 1
                if request.edge_id is not None:
                    self._stats["edge_id_resp_sent"] = int(
                        self._stats["edge_id_resp_sent"]
                    ) + 1
        except (OSError, NetworkBackendError) as exc:
            self._record_serial_event(
                "edge_resp_send",
                direction="local_to_peer",
                edge_bit=request.response_bit,
                byte_complete=request.completed,
                edge_id=request.edge_id,
                outcome="error",
                error_type=type(exc).__name__,
            )
            raise
        self._record_serial_event(
            "edge_resp_send",
            direction="local_to_peer",
            edge_bit=request.response_bit,
            byte_complete=request.completed,
            edge_id=request.edge_id,
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
            self._notify_transport_change()

    def _notify_transport_change(self) -> None:
        """Signal completed state updates without calling owner code."""
        # The condition's default RLock also permits pending-count/close
        # publishers to signal while already holding it. Frame/queue waiters
        # release it before owner callbacks, queue access, or network writes.
        with self._edge_pending_condition:
            self._transport_generation += 1
            self._edge_pending_condition.notify_all()

    def _wait_for_transport_change(self, generation: int, *, timeout: float) -> None:
        """Do not sleep through a change since the owner's last work check."""
        with self._edge_pending_condition:
            if generation == self._transport_generation and not self._closed_event.is_set():
                self._edge_pending_condition.wait(timeout=timeout)

    def _handle_edge_req(self, request: _InboundEdge | int) -> None:
        """Run one compatibility-worker edge under core admission."""
        # A legacy receiver with no local core intentionally emits the
        # historical keep-alive stream. It still enters the admission barrier
        # so detach cannot race the worker's phase bookkeeping.
        if isinstance(request, _InboundEdge):
            peer_bit = request.peer_bit
            edge_id = request.edge_id
        else:
            # Keep the private helper tolerant of old test doubles which
            # called it with the raw peer bit.
            peer_bit = request
            edge_id = None
        with self._local_core_access(allow_none=True):
            self._handle_edge_req_impl(peer_bit, edge_id=edge_id)

    def _handle_edge_req_impl(self, peer_bit: int, *, edge_id: int | None = None) -> None:
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
        phase_now = _entry.time.monotonic()
        active_exchange = phase_now < self._active_exchange_until
        post_byte_rearm = phase_now < self._post_byte_rearm_until

        if not armed():
            self._stats["slave_rearm_waits"] = int(self._stats["slave_rearm_waits"]) + 1
            now = _entry.time.monotonic()
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
            deadline = _entry.time.monotonic() + _REARM_WAIT_SECONDS
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
                    _entry.time.monotonic() + _ACTIVE_EXCHANGE_REARM_WAIT_SECONDS,
                )
            while _entry.time.monotonic() < deadline and not self._closed_event.is_set():
                if armed():
                    self._stats["slave_rearm_successes"] = (
                        int(self._stats["slave_rearm_successes"]) + 1
                    )
                    if post_byte_rearm:
                        self._stats["slave_post_byte_rearm_successes"] = (
                            int(self._stats["slave_post_byte_rearm_successes"]) + 1
                        )
                        self._post_byte_rearm_until = 0.0
                    self._stats["last_slave_rearm_at"] = _entry.time.monotonic()
                    break
                remaining = deadline - _entry.time.monotonic()
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
                    self._active_exchange_until = _entry.time.monotonic() + _ACTIVE_EXCHANGE_GRACE_SECONDS
                our_bit = core.peek_out_bit()
                completed = core.apply_external_edge(peer_bit)
                completion_context = self._serial_completion_context() if completed else None
                if completed:
                    self._stats["last_slave_byte_complete_at"] = _entry.time.monotonic()
                    self._post_byte_rearm_until = _entry.time.monotonic() + _POST_BYTE_REARM_GRACE_SECONDS
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
                timeout=_entry._EDGE_RESPONSE_TIMEOUT_SECONDS,
                operation="EDGE_RESP",
            ) as write_deadline:
                if not self._closed:
                    if edge_id is None:
                        frame = _FRAME.pack(_OP_EDGE_RESP, our_bit & 1)
                    else:
                        frame = _EDGE_ID_FRAME.pack(
                            _OP_EDGE_RESP_ID,
                            our_bit & 1,
                            edge_id,
                        )
                    # Publish the completed response while the write lock is
                    # held.  A versioned peer may send a duplicate as soon as
                    # it receives the first response; recording only after
                    # this send lets the reader race with the replay and
                    # misclassify the duplicate as a second edge.
                    self._remember_inbound_edge_response(edge_id, our_bit & 1)
                    self._send_frame(
                        frame,
                        timeout=max(0.0, write_deadline - _entry.time.monotonic()),
                        operation="EDGE_RESP",
                    )
                    self._stats["edge_resp_sent"] = int(self._stats["edge_resp_sent"]) + 1
                    if edge_id is not None:
                        self._stats["edge_id_resp_sent"] = int(
                            self._stats["edge_id_resp_sent"]
                        ) + 1
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
            edge_id=edge_id,
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
                remaining = partial_deadline - _entry.time.monotonic()
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
                partial_deadline = _entry.time.monotonic() + _entry._FRAME_READ_TIMEOUT_SECONDS
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
            deadline = _entry.time.monotonic() + timeout
        else:
            if deadline is None:
                raise ValueError("deadline must be finite")
            if not math.isfinite(deadline):
                raise ValueError("deadline must be finite")
            timeout = max(0.0, deadline - _entry.time.monotonic())
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise NetworkBackendError(f"{operation}: cancelled")
            if self._closed or self._closed_event.is_set():
                raise NetworkBackendError(f"{operation}: backend closed")
            remaining = deadline - _entry.time.monotonic()
            if remaining <= 0:
                raise NetworkBackendError(f"{operation} write lock timed out after {timeout:g}s")
            if self._write_lock.acquire(timeout=min(_entry._SEND_POLL_SECONDS, remaining)):
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
        deadline = _entry.time.monotonic() + timeout
        while offset < len(view):
            if cancel_event is not None and cancel_event.is_set():
                raise NetworkBackendError(f"{operation}: cancelled")
            if self._closed or self._closed_event.is_set():
                raise NetworkBackendError(f"{operation}: backend closed")
            remaining = deadline - _entry.time.monotonic()
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
                    min(_entry._SEND_POLL_SECONDS, remaining),
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
        deadline = _entry.time.monotonic() + max(0.0, timeout)
        while True:
            with self._edge_pending_condition:
                generation = self._transport_generation
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
            remaining = deadline - _entry.time.monotonic()
            if remaining <= 0:
                error = NetworkBackendError(timeout_message)
                self._mark_closed(error)
                raise error
            if progress_callback is not None:
                # Do not invoke owner code while holding a queue/condition
                # lock. The callback is bounded by the caller's owner
                # contract; the surrounding wait loop still applies the
                # original absolute transport deadline.
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
                remaining = deadline - _entry.time.monotonic()
                if remaining <= 0:
                    error = NetworkBackendError(timeout_message)
                    self._mark_closed(error)
                    raise error
            try:
                return q.get_nowait()
            except queue.Empty:
                self._wait_for_transport_change(
                    generation, timeout=min(_entry._SEND_POLL_SECONDS, remaining)
                )

    def _run_progress_callback(self, callback: Callable[[], object]) -> None:
        """Run owner progress and convert callback failures to link errors."""
        try:
            callback()
        except NetworkBackendError as exc:
            if not self._closed_event.is_set():
                self._mark_closed(exc)
            raise
        except BaseException as exc:
            error = NetworkBackendError(f"owner progress callback failed: {exc}")
            self._mark_closed(error)
            raise error from exc
