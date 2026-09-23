"""Reciprocal-admission edge-retirement regressions.

Split from ``tests/test_network_cpu_owner.py`` (#122).
"""

from __future__ import annotations

import socket
import sys
import threading
import time

import pytest

from pokered_harness.link.network_backend import (
    _EDGE_ID_FRAME,
    _FRAME,
    _OP_EDGE_REQ,
    _OP_EDGE_REQ_ID,
    _OP_EDGE_RESP,
    _OP_EDGE_RESP_ID,
    NetworkBackend,
    _InboundEdge,
)
from pokered_harness.link.serial_coordinator import SerialOperationGate
from tests._network_edge_retirement_support import (
    _BOUNDED_WAIT,
    _EDGE_RECIPROCAL_EVENT,
    _EDGE_WRITE_EVENT,
    _abandon_test_transport,
    _admit_identified_edge_request,
    _edge_admission_witness,
    _enable_retirement_witness,
    _inbound_retirement_ledger,
    _read_master_edge_request,
    _record_closed_retirement_evidence,
    _responses_written,
    _run_transition_while_wait_is_outstanding,
    _start_master_edge,
    _teardown_transport,
    _transcript_records,
    _wait_for_edge_requests_retired,
    _wait_for_pending_zero,
)

pytestmark = [pytest.mark.unit, pytest.mark.timing_sensitive]


def test_retirement_witness_counts_a_reciprocal_admission_and_its_write(monkeypatch) -> None:
    """A reciprocal request is an admission with its own write obligation.

    When this endpoint's master edge is in flight, the reader answers an
    incoming peer request reciprocally on its own thread.  That path records
    ``reciprocal_master_edge`` before it attempts the write and never raises
    ``_edge_pending``, so the request appears in no pending count, in no close
    snapshot, and in no per-id ledger entry that a failed send would leave
    unretired - ``_send_edge_response`` publishes the response and drops the id
    before ``_send_frame``.  Only the reciprocal request's own
    ``edge_resp_send`` record with ``outcome="success"`` shows the response
    reached the wire, so the witness carries it as an admission and the close
    can decide from it.  This pins both real record shapes, the accounting a
    completed reciprocal edge produces, and the acceptance that must survive.
    """
    decisions: list[tuple[bool, object]] = []
    _record_closed_retirement_evidence(monkeypatch, decisions)
    peer, backend = NetworkBackend.pair()
    from tests.test_network_owner_execution import _Core

    threads: list[threading.Thread] = []
    _enable_retirement_witness(backend)
    backend.start_receiver(_Core(), serial_gate=SerialOperationGate(), dispatch_to_owner=True)
    try:
        peer._sock.settimeout(_BOUNDED_WAIT)
        completed = _EDGE_ID_FRAME.pack(_OP_EDGE_RESP_ID, 0, 1)
        _admit_identified_edge_request(peer, backend)
        assert backend.service_pending_edges(max_edges=1) == 1
        _wait_for_pending_zero(backend)
        assert peer._sock.recv(len(completed)) == completed

        master = _start_master_edge(backend, threads)
        _read_master_edge_request(peer)
        # The reciprocal response carries this endpoint's own master bit, which
        # is the ``our_bit`` the in-flight ``on_edge`` published.
        reciprocal = _EDGE_ID_FRAME.pack(_OP_EDGE_RESP_ID, 1, 3)
        peer._sock.sendall(_EDGE_ID_FRAME.pack(_OP_EDGE_REQ_ID, 1, 3))
        assert peer._sock.recv(len(reciprocal)) == reciprocal, (
            "the reciprocal request was not answered on the wire"
        )
        peer._sock.sendall(_FRAME.pack(_OP_EDGE_RESP, 0))
        master_thread = threads[-1]
        master_thread.join(timeout=_BOUNDED_WAIT)
        assert not master_thread.is_alive(), "the master exchange never finished"
        assert master == [None], master

        records = _transcript_records(backend)
        assert any(
            record.get("event") == _EDGE_RECIPROCAL_EVENT and record.get("edge_id") == 3
            for record in records
        ), "the reader did not record the reciprocal admission with its id"
        assert any(
            record.get("event") == _EDGE_WRITE_EVENT
            and record.get("edge_id") == 3
            and record.get("outcome") == "success"
            for record in records
        ), "the reciprocal response did not record a successful write"
        witness = _edge_admission_witness(backend)
        assert witness.complete, witness
        assert witness.admitted == frozenset({1, 3})
        assert witness.answered == frozenset({1, 3})
        assert witness.outstanding == frozenset()
        assert witness.unidentified_admissions == 0
        assert witness.witnessed_highest == 3
        assert _responses_written(backend) == 2, "the reciprocal response was not counted"

        outcome, elapsed = _run_transition_while_wait_is_outstanding(
            backend,
            backend._mark_closed_uncoordinated,
            resume_when="deadline = time.monotonic() + timeout",
            threads=threads,
        )
        assert outcome is None, outcome
        assert backend._closed
        assert decisions == [(True, 0)], decisions
        assert elapsed < _BOUNDED_WAIT, f"acceptance was not bounded ({elapsed:.3f}s)"
        assert peer._sock.recv(1) == b"", "the closed transport did not reach the peer as EOF"
    finally:
        monkeypatch.undo()
        _teardown_transport(peer, backend, *threads)


@pytest.mark.parametrize(
    "edge_id",
    [3, None],
    ids=["identified-reciprocal", "unnamed-reciprocal"],
)
@pytest.mark.parametrize(
    "transport_end",
    ["write-half-shutdown", "clean-close"],
    ids=["failed-send", "close-cancelled"],
)
def test_retirement_wait_rejects_a_reciprocal_response_that_never_reached_the_wire(
    monkeypatch, edge_id: int | None, transport_end: str
) -> None:
    """An unanswered reciprocal request must not be certified as retired.

    A first identified request completes through the real owner path and its
    response is read off the wire, so an unrelated admission is already
    answered.  The real master exchange is then started, and the peer sends a
    further request that the reader answers reciprocally.  That response is
    gated immediately before the socket write and the write is then really
    prevented - either by shutting down this endpoint's write half or by
    completing a clean close while it is gated.  ``_send_edge_response`` has
    already published the response and dropped the id from the pending ledger
    by then, and the reciprocal path never raised ``_edge_pending``, so the
    close records zero outstanding work and every aggregate fact looks healthy.
    The transcript is the only place the failure is visible, which is why an
    identified reciprocal id must join ``admitted`` and an id-less one must
    join the unnamed count instead of only advancing the high-water mark.
    """
    decisions: list[tuple[bool, object]] = []
    _record_closed_retirement_evidence(monkeypatch, decisions)
    peer, backend = NetworkBackend.pair()
    from tests.test_network_owner_execution import _Core

    threads: list[threading.Thread] = []
    _enable_retirement_witness(backend)
    backend.start_receiver(_Core(), serial_gate=SerialOperationGate(), dispatch_to_owner=True)
    try:
        peer._sock.settimeout(_BOUNDED_WAIT)
        completed = _EDGE_ID_FRAME.pack(_OP_EDGE_RESP_ID, 0, 1)
        _admit_identified_edge_request(peer, backend)
        assert backend.service_pending_edges(max_edges=1) == 1
        _wait_for_pending_zero(backend)
        assert peer._sock.recv(len(completed)) == completed
        wire: list[bytes] = []

        def fail_the_reciprocal_write_and_close() -> None:
            at_send = threading.Event()
            release_send = threading.Event()
            real_send = backend._send_frame

            def gated_send(frame: bytes, **kwargs) -> None:
                if kwargs.get("operation") == "EDGE_RESP":
                    at_send.set()
                    assert release_send.wait(timeout=_BOUNDED_WAIT), (
                        "the gated reciprocal write was never released"
                    )
                real_send(frame, **kwargs)

            monkeypatch.setattr(backend, "_send_frame", gated_send)
            master = _start_master_edge(backend, threads)
            wire.append(_read_master_edge_request(peer))
            if edge_id is None:
                request = _FRAME.pack(_OP_EDGE_REQ, 1)
            else:
                request = _EDGE_ID_FRAME.pack(_OP_EDGE_REQ_ID, 1, edge_id)
            peer._sock.sendall(request)
            assert at_send.wait(timeout=_BOUNDED_WAIT), (
                "the reader never attempted the reciprocal response"
            )
            if transport_end == "write-half-shutdown":
                backend._sock.shutdown(socket.SHUT_WR)
            else:
                backend._mark_closed_uncoordinated()
            release_send.set()
            master_thread = threads[-1]
            master_thread.join(timeout=_BOUNDED_WAIT)
            assert not master_thread.is_alive(), "the master exchange never failed out"
            assert isinstance(master[0], BaseException), master
            backend._reader.join(timeout=_BOUNDED_WAIT)
            assert not backend._reader.is_alive(), (
                "the failed reciprocal write did not end the reader"
            )
            assert backend._closed, "the failed reciprocal write did not close the transport"

        outcome, elapsed = _run_transition_while_wait_is_outstanding(
            backend,
            fail_the_reciprocal_write_and_close,
            resume_when="deadline = time.monotonic() + timeout",
            threads=threads,
        )
        assert isinstance(outcome, AssertionError), outcome
        assert "closure and not retirement" in str(outcome)
        assert decisions == [(True, 0)], decisions
        assert backend._pre_close_snapshot["pending_edge_requests"] == 0
        assert _responses_written(backend) == 1, "the failed write grew the success counter"
        records = _transcript_records(backend)
        assert any(
            record.get("event") == _EDGE_RECIPROCAL_EVENT and record.get("edge_id") == edge_id
            for record in records
        ), "the reader did not record the reciprocal admission with its id"
        assert any(
            record.get("event") == _EDGE_WRITE_EVENT
            and record.get("edge_id") == edge_id
            and record.get("outcome") != "success"
            for record in records
        ), "the unanswerable reciprocal write was recorded as a success"
        witness = _edge_admission_witness(backend)
        assert witness.answered == frozenset({1}), witness
        if edge_id is None:
            assert witness.admitted == frozenset({1}), witness
            assert witness.unidentified_admissions == 1, witness
        else:
            assert witness.admitted == frozenset({1, 3}), witness
            assert witness.outstanding == frozenset({3}), witness
        assert b"".join(wire) == _FRAME.pack(_OP_EDGE_REQ, 1), (
            "the peer saw bytes other than the master's own outbound request"
        )
        assert peer._sock.recv(1) == b"", "the unanswerable reciprocal response reached the peer"
        assert elapsed < 5.0, f"rejection was not bounded ({elapsed:.3f}s)"
    finally:
        monkeypatch.undo()
        _teardown_transport(peer, backend, *threads)


@pytest.mark.parametrize(
    "edge_id",
    [3, None],
    ids=["identified-reciprocal", "unnamed-reciprocal"],
)
def test_retirement_wait_rejects_a_reciprocal_admission_published_after_the_close(
    monkeypatch, edge_id: int | None
) -> None:
    """A transcript copy taken before the reader published is not evidence.

    The reader answers an incoming request reciprocally on its own thread and
    appends ``reciprocal_master_edge`` inside its wire-completion section, but
    it never touches ``_edge_pending``, so the pending condition this wait sits
    on cannot exclude it and a real clean close can publish while that record is
    still in flight.  This test holds the reader immediately before its
    admission record, completes the close, and asserts that the wait does not
    decide from the copy it could have taken then: it has to stay inside its
    publication boundary until the admission and the real failed send are on the
    transcript, and then refuse.  Deciding from the stale copy certified an
    unnamed reciprocal request whose response never reached the wire, which is
    exactly what the reader is gated on here.
    """
    decisions: list[tuple[bool, object]] = []
    _record_closed_retirement_evidence(monkeypatch, decisions)
    peer, backend = NetworkBackend.pair()
    from tests.test_network_owner_execution import _Core

    threads: list[threading.Thread] = []
    _enable_retirement_witness(backend)
    backend.start_receiver(_Core(), serial_gate=SerialOperationGate(), dispatch_to_owner=True)
    try:
        peer._sock.settimeout(_BOUNDED_WAIT)
        completed = _EDGE_ID_FRAME.pack(_OP_EDGE_RESP_ID, 0, 1)
        _admit_identified_edge_request(peer, backend)
        assert backend.service_pending_edges(max_edges=1) == 1
        _wait_for_pending_zero(backend)
        assert peer._sock.recv(len(completed)) == completed

        record_started = threading.Event()
        release_record = threading.Event()
        real_record = backend._record_serial_event

        def gated_record(event: str, **fields: object) -> None:
            if event == _EDGE_RECIPROCAL_EVENT:
                record_started.set()
                assert release_record.wait(timeout=_BOUNDED_WAIT), (
                    "the gated reciprocal admission record was never released"
                )
            real_record(event, **fields)

        monkeypatch.setattr(backend, "_record_serial_event", gated_record)

        module = sys.modules[_wait_for_edge_requests_retired.__module__]
        real_boundary = module._await_wire_record_publication
        boundary_entered = threading.Event()

        def signalling_boundary(transport: NetworkBackend, *, timeout: float) -> bool:
            boundary_entered.set()
            return real_boundary(transport, timeout=timeout)

        monkeypatch.setattr(module, "_await_wire_record_publication", signalling_boundary)

        def release_once_the_waiter_is_parked_on_the_record() -> None:
            if boundary_entered.wait(timeout=_BOUNDED_WAIT):
                release_record.set()

        watcher = threading.Thread(
            target=release_once_the_waiter_is_parked_on_the_record,
            name="reciprocal-record-releaser",
            daemon=True,
        )
        threads.append(watcher)
        watcher.start()

        wire: list[bytes] = []
        master_holder: list[list[BaseException | None]] = []

        def close_while_the_record_is_gated() -> None:
            master_holder.append(_start_master_edge(backend, threads))
            wire.append(_read_master_edge_request(peer))
            if edge_id is None:
                request = _FRAME.pack(_OP_EDGE_REQ, 1)
            else:
                request = _EDGE_ID_FRAME.pack(_OP_EDGE_REQ_ID, 1, edge_id)
            peer._sock.sendall(request)
            assert record_started.wait(timeout=_BOUNDED_WAIT), (
                "the reader never attempted the reciprocal admission record"
            )
            # The reader has consumed the request and holds its wire-completion
            # section, but the admission record is not on the transcript yet, so
            # the close finds no admitted work and cannot see the request.
            backend._mark_closed_uncoordinated()
            assert backend._closed
            assert backend._pre_close_snapshot["pending_edge_requests"] == 0

        outcome, elapsed = _run_transition_while_wait_is_outstanding(
            backend,
            close_while_the_record_is_gated,
            resume_when="deadline = time.monotonic() + timeout",
            threads=threads,
        )
        assert isinstance(outcome, AssertionError), outcome
        assert "closure and not retirement" in str(outcome)
        assert decisions == [(True, 0)], decisions
        records = _transcript_records(backend)
        assert any(
            record.get("event") == _EDGE_RECIPROCAL_EVENT and record.get("edge_id") == edge_id
            for record in records
        ), "the admitted reciprocal request never reached the transcript"
        witness = _edge_admission_witness(backend)
        assert witness.answered == frozenset({1}), witness
        if edge_id is None:
            assert witness.admitted == frozenset({1}), witness
            assert witness.unidentified_admissions == 1, witness
        else:
            assert witness.admitted == frozenset({1, 3}), witness
            assert witness.outstanding == frozenset({3}), witness
        assert _responses_written(backend) == 1, "the failed reciprocal write reached the wire"
        assert b"".join(wire) == _FRAME.pack(_OP_EDGE_REQ, 1), (
            "the peer saw bytes other than the master's own outbound request"
        )
        assert peer._sock.recv(1) == b"", "an unanswered reciprocal response reached the peer"
        assert elapsed < 5.0, f"rejection was not bounded ({elapsed:.3f}s)"
        master_thread = next(
            thread for thread in threads if thread.name == "reciprocal-master-edge"
        )
        master_thread.join(timeout=_BOUNDED_WAIT)
        assert not master_thread.is_alive(), "the master exchange never failed out"
        master_outcomes = master_holder[0]
        assert master_outcomes and isinstance(master_outcomes[0], BaseException), master_outcomes
    finally:
        monkeypatch.undo()
        _teardown_transport(peer, backend, *threads)


def test_retirement_wait_accepts_a_reciprocal_response_whose_success_record_publishes_after_the_close(
    monkeypatch,
) -> None:
    """A real reciprocal write is retirement even when its record lags the close.

    The reciprocal response for id 3 really reaches the peer, so the write is
    complete and the success counter has already grown.  Only the transcript
    record that reports it is held back, and the clean close lands in that
    window with zero admitted work.  A witness read then sees an admission with
    no answer and refuses a request that was really written, so the publication
    boundary has to settle that record before the decision is taken.  The wire
    is asserted byte for byte, and the successful completion this test accepts
    is the same exchange the round-6 repair's failure regressions tear down.
    """
    decisions: list[tuple[bool, object]] = []
    _record_closed_retirement_evidence(monkeypatch, decisions)
    peer, backend = NetworkBackend.pair()
    from tests.test_network_owner_execution import _Core

    threads: list[threading.Thread] = []
    _enable_retirement_witness(backend)
    backend.start_receiver(_Core(), serial_gate=SerialOperationGate(), dispatch_to_owner=True)
    try:
        peer._sock.settimeout(_BOUNDED_WAIT)
        completed = _EDGE_ID_FRAME.pack(_OP_EDGE_RESP_ID, 0, 1)
        _admit_identified_edge_request(peer, backend)
        assert backend.service_pending_edges(max_edges=1) == 1
        _wait_for_pending_zero(backend)
        assert peer._sock.recv(len(completed)) == completed

        success_recorded = threading.Event()
        release_record = threading.Event()
        real_record = backend._record_serial_event

        def gated_record(event: str, **fields: object) -> None:
            if (
                event == _EDGE_WRITE_EVENT
                and fields.get("edge_id") == 3
                and fields.get("outcome") == "success"
            ):
                success_recorded.set()
                assert release_record.wait(timeout=_BOUNDED_WAIT), (
                    "the gated successful write record was never released"
                )
            real_record(event, **fields)

        monkeypatch.setattr(backend, "_record_serial_event", gated_record)

        module = sys.modules[_wait_for_edge_requests_retired.__module__]
        real_boundary = module._await_wire_record_publication
        boundary_entered = threading.Event()

        def signalling_boundary(transport: NetworkBackend, *, timeout: float) -> bool:
            boundary_entered.set()
            return real_boundary(transport, timeout=timeout)

        monkeypatch.setattr(module, "_await_wire_record_publication", signalling_boundary)

        def release_once_the_waiter_is_parked_on_the_record() -> None:
            if boundary_entered.wait(timeout=_BOUNDED_WAIT):
                release_record.set()

        watcher = threading.Thread(
            target=release_once_the_waiter_is_parked_on_the_record,
            name="reciprocal-success-releaser",
            daemon=True,
        )
        threads.append(watcher)
        watcher.start()

        wire: list[bytes] = []
        reciprocal = _EDGE_ID_FRAME.pack(_OP_EDGE_RESP_ID, 1, 3)

        def close_after_the_response_reached_the_wire() -> None:
            _start_master_edge(backend, threads)
            wire.append(_read_master_edge_request(peer))
            peer._sock.sendall(_EDGE_ID_FRAME.pack(_OP_EDGE_REQ_ID, 1, 3))
            received = b""
            while len(received) < len(reciprocal):
                part = peer._sock.recv(len(reciprocal) - len(received))
                assert part, (received.hex(), "the reciprocal response never reached the peer")
                received += part
            assert received == reciprocal, received
            wire.append(received)
            assert success_recorded.wait(timeout=_BOUNDED_WAIT), (
                "the reader never recorded the reciprocal write"
            )
            assert _responses_written(backend) == 2, "the reciprocal response was not written"
            backend._mark_closed_uncoordinated()
            assert backend._closed
            assert backend._pre_close_snapshot["pending_edge_requests"] == 0

        outcome, elapsed = _run_transition_while_wait_is_outstanding(
            backend,
            close_after_the_response_reached_the_wire,
            resume_when="deadline = time.monotonic() + timeout",
            threads=threads,
        )
        assert outcome is None, outcome
        assert backend._closed
        assert decisions == [(True, 0)], decisions
        witness = _edge_admission_witness(backend)
        assert witness.admitted == frozenset({1, 3}), witness
        assert witness.answered == frozenset({1, 3}), witness
        assert witness.outstanding == frozenset(), witness
        assert b"".join(wire) == _FRAME.pack(_OP_EDGE_REQ, 1) + reciprocal, (
            "the peer saw bytes other than the master request and the reciprocal response"
        )
        assert peer._sock.recv(1) == b"", "the closed transport did not reach the peer as EOF"
        assert elapsed < 5.0, f"acceptance was not bounded ({elapsed:.3f}s)"
    finally:
        monkeypatch.undo()
        _teardown_transport(peer, backend, *threads)


@pytest.mark.parametrize(
    "close_owns_the_condition",
    [True, False],
    ids=["close-holds-condition", "close-before-observation"],
)
def test_retirement_wait_accepts_a_close_that_beats_the_waiter_to_the_condition(
    monkeypatch, close_owns_the_condition: bool
) -> None:
    """A genuine retirement observed only after the close must still be accepted.

    The identified response is written before the wait begins, so nothing grows
    the aggregate counter during the wait, and the worker's release arrives while
    the transport is still open.  The close then completes before the waiter can
    observe the zero: in one schedule the closer holds the pending condition
    across the release and the close, so the notified waiter loses the
    reacquisition; in the other the waiter is held before it acquires the
    condition at all.  Either way the transport presents as closed with pending
    zero and a counter that never moved during the wait, which a rule keyed on
    counter growth rejects even though the admitted request really was answered.
    """
    decisions: list[tuple[bool, object]] = []
    _record_closed_retirement_evidence(monkeypatch, decisions)
    peer, backend = NetworkBackend.pair()
    from tests.test_network_owner_execution import _Core

    threads: list[threading.Thread] = []
    _enable_retirement_witness(backend)
    backend.start_receiver(_Core(), serial_gate=SerialOperationGate(), dispatch_to_owner=True)
    try:
        responded = _EDGE_ID_FRAME.pack(_OP_EDGE_RESP_ID, 1, 1)
        peer._sock.settimeout(_BOUNDED_WAIT)
        _admit_identified_edge_request(peer, backend)
        backend._send_edge_response(_InboundEdge(peer_bit=0, response_bit=1, edge_id=1))
        assert _responses_written(backend) == 1
        assert peer._sock.recv(len(responded)) == responded
        ledger_at_entry = _inbound_retirement_ledger(backend)
        assert ledger_at_entry == (1, frozenset({1}), frozenset()), ledger_at_entry

        def release_and_close() -> None:
            if not close_owns_the_condition:
                backend._decrement_edge_pending()
                backend._mark_closed_uncoordinated()
                return
            with backend._edge_pending_condition:
                backend._decrement_edge_pending()
                backend._mark_closed_uncoordinated()

        outcome, elapsed = _run_transition_while_wait_is_outstanding(
            backend,
            release_and_close,
            resume_when=None
            if close_owns_the_condition
            else "deadline = time.monotonic() + timeout",
            threads=threads,
        )
        assert outcome is None, outcome
        assert backend._closed
        assert decisions == [(True, 0)], decisions
        assert backend._pre_close_snapshot["pending_edge_requests"] == 0
        assert _responses_written(backend) == 1, "no response was written during the wait"
        assert _inbound_retirement_ledger(backend) == ledger_at_entry
        assert elapsed < 5.0, f"acceptance was not bounded ({elapsed:.3f}s)"
        assert peer._sock.recv(1) == b"", "the closed transport did not reach the peer as EOF"
    finally:
        _teardown_transport(peer, backend, *threads)


def test_retirement_wait_failure_path_stays_bounded_while_close_lock_is_held() -> None:
    """Timeout diagnostics must not wait on the close lock."""
    _peer, backend = NetworkBackend.pair()
    close_lock_held = False
    waiter_thread: threading.Thread | None = None
    try:
        with backend._edge_pending_condition:
            backend._edge_pending = 1
        assert backend._close_lock.acquire(timeout=1.0)
        close_lock_held = True
        outcomes: list[BaseException | None] = []

        def waiter() -> None:
            try:
                _wait_for_edge_requests_retired(backend, timeout=0.2)
            except BaseException as exc:  # noqa: BLE001 - asserted below
                outcomes.append(exc)
            else:
                outcomes.append(None)

        waiter_thread = threading.Thread(target=waiter, name="retirement-wait", daemon=True)
        started = time.monotonic()
        waiter_thread.start()
        waiter_thread.join(timeout=5.0)
        elapsed = time.monotonic() - started
        assert not waiter_thread.is_alive(), (
            "timeout diagnostics waited on the close lock a closer holds"
        )
        assert elapsed < 5.0, f"retirement wait was not bounded ({elapsed:.3f}s)"
        assert len(outcomes) == 1 and isinstance(outcomes[0], AssertionError), outcomes
        assert "was not written within" in str(outcomes[0])
    finally:
        if close_lock_held:
            backend._close_lock.release()
            close_lock_held = False
        # Teardown must stay bounded even when the helper has regressed. A
        # pre-fix helper is left holding the pending condition while it waits
        # for the close lock, so releasing the lock first gives it the chance to
        # finish; if it still cannot, the transport is released without taking
        # the pending condition that thread owns, so the runner reports the
        # failed assertion instead of hanging in cleanup.
        if waiter_thread is not None:
            waiter_thread.join(timeout=2.0)
        if waiter_thread is None or not waiter_thread.is_alive():
            backend._mark_closed_uncoordinated()
        else:
            _abandon_test_transport(backend)
