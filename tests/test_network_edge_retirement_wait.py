"""Closed/retirement wait regressions for the network edge owner.

Split from ``tests/test_network_cpu_owner.py`` (#122).
"""

from __future__ import annotations

import sys
import threading

import pytest

from pokered_harness.link.network_backend import (
    _EDGE_ID_FRAME,
    _EDGE_RESPONSE_HISTORY_MAX,
    _FRAME,
    _OP_EDGE_REQ_ID,
    _OP_EDGE_RESP,
    _OP_EDGE_RESP_ID,
    NetworkBackend,
    _InboundEdge,
)
from pokered_harness.link.serial_coordinator import SerialOperationGate
from tests._network_edge_retirement_support import (
    _BOUNDED_WAIT,
    _EDGE_ADMISSION_EVENT,
    _EDGE_RETIREMENT_WITNESS_EVICTING_ENTRIES,
    _EDGE_WRITE_EVENT,
    _admit_identified_edge_request,
    _admit_legacy_edge_request,
    _close_pair,
    _edge_admission_witness,
    _EdgeAdmissionWitness,
    _enable_retirement_witness,
    _gated_closed_discard_transition,
    _inbound_retirement_ledger,
    _record_closed_retirement_evidence,
    _responses_written,
    _run_transition_while_wait_is_outstanding,
    _teardown_transport,
    _transcript_records,
    _wait_for_edge_requests_retired,
    _wait_for_pending_zero,
    _write_retire_and_close_holding_the_condition,
)

pytestmark = [pytest.mark.unit, pytest.mark.timing_sensitive]


def test_retirement_wait_rejects_transport_closed_before_the_wait() -> None:
    """A transport closed up front cannot certify retired edge work."""
    _peer, backend = NetworkBackend.pair()
    try:
        backend._mark_closed_uncoordinated()
        with pytest.raises(AssertionError, match="already closed"):
            _wait_for_edge_requests_retired(backend, timeout=0.5)
    finally:
        backend._mark_closed_uncoordinated()


@pytest.mark.parametrize("clean_close", [True, False], ids=["clean-close", "error-close"])
def test_retirement_wait_rejects_close_that_abandons_admitted_work(clean_close: bool) -> None:
    """A close during the wait drops admitted work; that is not retirement.

    Both closes are rejected. A clean close publishes ``_closed`` before it
    samples the counters it records, and the response worker skips its write
    once the transport is terminal, so a clean close can present with pending
    zero and no reader error while no response ever reached the wire.
    """
    _peer, backend = NetworkBackend.pair()
    _enable_retirement_witness(backend)
    try:
        with backend._edge_pending_condition:
            backend._edge_pending = 1
        assert _responses_written(backend) == 0
        outcome, elapsed = _run_transition_while_wait_is_outstanding(
            backend,
            lambda: backend._mark_closed_uncoordinated(
                None if clean_close else RuntimeError("transport failed")
            ),
        )
        assert isinstance(outcome, AssertionError), outcome
        assert "closure and not retirement" in str(outcome)
        assert backend._closed
        # No response was ever written, so zero pending work here is closure.
        assert _responses_written(backend) == 0
        assert elapsed < 5.0, f"rejection was not bounded ({elapsed:.3f}s)"
    finally:
        backend._mark_closed_uncoordinated()


def test_retirement_wait_accepts_a_close_after_a_completed_retirement(monkeypatch) -> None:
    """A close that lands after the response was written is still retirement.

    The wait must not reject every close.  A real peer request is admitted by
    the real reader, so its id is claimed and the per-admission record can later
    show it answered.  The transition holds the pending condition across the
    identified write, the retirement notification, and the completed close, so
    the zero cannot be observed before the transport is terminal, and the
    recorded decision proves the closed-transport branch produced the acceptance
    instead of an incidental open-transport return.
    """
    decisions: list[tuple[bool, object]] = []
    _record_closed_retirement_evidence(monkeypatch, decisions)
    peer, backend = NetworkBackend.pair()
    from tests.test_network_owner_execution import _Core

    threads: list[threading.Thread] = []
    _enable_retirement_witness(backend)
    backend.start_receiver(_Core(), serial_gate=SerialOperationGate(), dispatch_to_owner=True)
    try:
        _admit_identified_edge_request(peer, backend)
        assert _responses_written(backend) == 0
        outcome, elapsed = _run_transition_while_wait_is_outstanding(
            backend,
            lambda: _write_retire_and_close_holding_the_condition(backend, edge_id=1),
            threads=threads,
        )
        assert outcome is None, outcome
        assert backend._closed
        assert decisions == [(True, 0)], decisions
        assert _responses_written(backend) == 1
        assert elapsed < 5.0, f"acceptance was not bounded ({elapsed:.3f}s)"
        expected = _EDGE_ID_FRAME.pack(_OP_EDGE_RESP_ID, 1, 1)
        peer._sock.settimeout(2.0)
        assert peer._sock.recv(len(expected)) == expected
        assert peer._sock.recv(1) == b"", "the closed transport did not reach the peer as EOF"
    finally:
        _teardown_transport(peer, backend, *threads)


def test_retirement_wait_closed_acceptance_requires_wire_evidence(monkeypatch) -> None:
    """The closed-transport branch has to be load-bearing, not decorative.

    Same real-request write/retire/close ordering as the acceptance regression,
    with the evidence predicate replaced by one that never consents.  If the
    acceptance test still passed, it would be leaving through a different branch
    and would not be a guard for accepting a completed close at all.
    """
    module = sys.modules[_wait_for_edge_requests_retired.__module__]
    monkeypatch.setattr(
        module,
        "_closed_transport_retirement_is_evidenced",
        lambda backend, *, pending_at_close, witness_at_entry: False,
    )
    peer, backend = NetworkBackend.pair()
    from tests.test_network_owner_execution import _Core

    threads: list[threading.Thread] = []
    _enable_retirement_witness(backend)
    backend.start_receiver(_Core(), serial_gate=SerialOperationGate(), dispatch_to_owner=True)
    try:
        _admit_identified_edge_request(peer, backend)
        outcome, _elapsed = _run_transition_while_wait_is_outstanding(
            backend,
            lambda: _write_retire_and_close_holding_the_condition(backend, edge_id=1),
            threads=threads,
        )
        assert isinstance(outcome, AssertionError), outcome
        assert "closure and not retirement" in str(outcome)
        assert _responses_written(backend) == 1
    finally:
        _teardown_transport(peer, backend, *threads)


def test_retirement_wait_reports_missing_evidence_when_the_publication_boundary_expires(
    monkeypatch,
) -> None:
    """An expired publication boundary is missing evidence, not proof of a leak.

    The transcript here really does hold this admission and its completed write:
    the response reached the peer and the backend retired the id.  Only the
    boundary that settles those producers is made to expire, which is the shape
    a caller sees when a producer is held past ``_BOUNDED_WAIT``.  The wait must
    still refuse - it may not certify retirement it could not observe settling -
    but it has to say which of the two it is, and it must not consult the
    evidence predicate at all, because the transcript it would read is exactly
    the copy that is not yet known to be complete.
    """
    module = sys.modules[_wait_for_edge_requests_retired.__module__]
    decisions: list[tuple[bool, object]] = []
    _record_closed_retirement_evidence(monkeypatch, decisions)
    boundary_entered = threading.Event()

    def expired_boundary(backend: NetworkBackend, *, timeout: float) -> bool:
        del backend, timeout
        boundary_entered.set()
        return False

    monkeypatch.setattr(module, "_await_wire_record_publication", expired_boundary)
    peer, backend = NetworkBackend.pair()
    from tests.test_network_owner_execution import _Core

    threads: list[threading.Thread] = []
    _enable_retirement_witness(backend)
    backend.start_receiver(_Core(), serial_gate=SerialOperationGate(), dispatch_to_owner=True)
    try:
        _admit_identified_edge_request(peer, backend)
        outcome, elapsed = _run_transition_while_wait_is_outstanding(
            backend,
            lambda: _write_retire_and_close_holding_the_condition(backend, edge_id=1),
            threads=threads,
        )
        assert boundary_entered.is_set(), "the wait never entered its publication boundary"
        assert isinstance(outcome, AssertionError), outcome
        assert "closure and not retirement" in str(outcome)
        assert "transcript_publication=unsettled" in str(outcome), str(outcome)
        assert "missing evidence" in str(outcome), str(outcome)
        assert decisions == [], (
            "the evidence predicate was consulted while the transcript was still "
            f"unsettled: {decisions}"
        )
        records = _transcript_records(backend)
        assert any(
            record.get("event") == _EDGE_ADMISSION_EVENT and record.get("edge_id") == 1
            for record in records
        ), "the admitted request was not on the transcript"
        assert any(
            record.get("event") == _EDGE_WRITE_EVENT
            and record.get("edge_id") == 1
            and record.get("outcome") == "success"
            for record in records
        ), "the completed write was not on the transcript"
        assert _responses_written(backend) == 1
        assert elapsed < 5.0, f"the refusal was not bounded ({elapsed:.3f}s)"
    finally:
        _teardown_transport(peer, backend, *threads)


def test_retirement_wait_accepts_a_retirement_observed_on_an_open_transport() -> None:
    """Retirement already established while open must survive a later close.

    This is the schedule where the response is written before the wait begins
    and the worker's release arrives while the transport is still open, so the
    wait accepts on the open-transport observation without needing any counter
    growth measured during the wait.
    """
    peer, backend = NetworkBackend.pair()
    try:
        with backend._edge_pending_condition:
            backend._edge_pending = 1
        backend._send_edge_response(_InboundEdge(peer_bit=0, response_bit=1))
        assert _responses_written(backend) == 1

        def release_while_open() -> None:
            with backend._edge_pending_condition:
                backend._decrement_edge_pending()

        outcome, _elapsed = _run_transition_while_wait_is_outstanding(backend, release_while_open)
        assert outcome is None, outcome
        assert not backend._closed, "the wait was supposed to accept a still-open transport"
        expected = _FRAME.pack(_OP_EDGE_RESP, 1)
        peer._sock.settimeout(2.0)
        assert peer._sock.recv(len(expected)) == expected
    finally:
        _close_pair(peer, backend)


def test_retirement_wait_rejects_a_close_that_abandons_work_behind_a_written_response() -> None:
    """A written response must not discharge a different admitted request.

    A real ``EDGE_RESP`` reaches the peer and grows the shared counter while an
    admitted request stays outstanding, then the close - not the worker -
    supplies the zero almost immediately.  The same schedule also covers the
    held real decrement: whether the written response belonged to another
    request or to this one whose release was withheld, the close found admitted
    work outstanding, so counter growth is not retirement.
    """
    peer, backend = NetworkBackend.pair()
    _enable_retirement_witness(backend)
    try:
        with backend._edge_pending_condition:
            backend._edge_pending = 1

        def write_for_other_request_and_close() -> None:
            with backend._edge_pending_condition:
                backend._send_edge_response(_InboundEdge(peer_bit=0, response_bit=1))
                backend._mark_closed_uncoordinated()

        outcome, elapsed = _run_transition_while_wait_is_outstanding(
            backend, write_for_other_request_and_close
        )
        assert isinstance(outcome, AssertionError), outcome
        assert "closure and not retirement" in str(outcome)
        assert _responses_written(backend) == 1, "the decoy response was never written"
        assert backend._pre_close_snapshot["pending_edge_requests"] == 1
        assert elapsed < 5.0, f"rejection was not bounded ({elapsed:.3f}s)"
        expected = _FRAME.pack(_OP_EDGE_RESP, 1)
        peer._sock.settimeout(2.0)
        assert peer._sock.recv(len(expected)) == expected
    finally:
        _close_pair(peer, backend)


def test_retirement_wait_rejects_a_close_that_finds_zero_after_an_unwritten_discard(
    monkeypatch,
) -> None:
    """A close whose snapshot reads zero is not retirement when nothing was written.

    A real peer request is admitted by the real reader and stays on the owner
    queue, so its id is claimed with no recorded response.  A clean close
    publishes ``_closed`` and then the owner's real closed branch releases that
    request without answering it, so the close samples ``pending_edge_requests``
    zero with no reader error and no response ever written.  That is exactly the
    state a snapshot-only rule accepts; the wait must still reject it, and the
    peer must observe EOF with no response bytes.

    The discard runs from the transition thread rather than from inside a hold
    on the pending condition, and the closer is gated after it publishes
    ``_closed`` but before it wakes the reader or samples its snapshot.  The
    ordering is therefore deterministic and no participant ever holds the
    pending condition while it needs the owner-dispatch lock.
    """
    peer, backend = NetworkBackend.pair()
    from tests.test_network_owner_execution import _Core

    threads: list[threading.Thread] = []
    _enable_retirement_witness(backend)
    backend.start_receiver(_Core(), serial_gate=SerialOperationGate(), dispatch_to_owner=True)
    try:
        _admit_identified_edge_request(peer, backend)
        assert _inbound_retirement_ledger(backend) == (1, frozenset(), frozenset({1}))
        discard_without_writing_and_close, closer_outcomes = _gated_closed_discard_transition(
            backend, monkeypatch, threads
        )

        outcome, elapsed = _run_transition_while_wait_is_outstanding(
            backend,
            discard_without_writing_and_close,
            resume_when="deadline = time.monotonic() + timeout",
            threads=threads,
        )
        assert isinstance(outcome, AssertionError), outcome
        assert "closure and not retirement" in str(outcome)
        assert closer_outcomes == [None], closer_outcomes
        assert _responses_written(backend) == 0
        assert backend._pre_close_snapshot["pending_edge_requests"] == 0
        assert backend._pre_close_snapshot.get("reader_error") is None
        assert _inbound_retirement_ledger(backend) == (1, frozenset(), frozenset({1}))
        assert elapsed < 5.0, f"rejection was not bounded ({elapsed:.3f}s)"
        peer._sock.settimeout(2.0)
        assert peer._sock.recv(1) == b"", "the abandoned request was answered after all"
    finally:
        # Restore the real ``_closed_event.set`` before teardown; otherwise the
        # gated publish still installed here would stall ``_close_pair`` when a
        # failing assertion skipped the transition that releases the gate.
        monkeypatch.undo()
        _teardown_transport(peer, backend, *threads)


def test_retirement_wait_rejects_a_close_that_discards_a_request_admitted_during_the_wait(
    monkeypatch,
) -> None:
    """A request admitted while the wait is parked is not certified by the close.

    Same real admission, real owner queue, and real closed discard as the
    entry-scope regression above, except that the request is admitted after the
    wait has already sampled its entry witness, so nothing about it can be
    covered by that snapshot.  The close still publishes
    ``pending_edge_requests`` zero with no reader error and no write, so the
    wait-scope clause is what keeps this schedule a rejection.
    """
    peer, backend = NetworkBackend.pair()
    from tests.test_network_owner_execution import _Core

    threads: list[threading.Thread] = []
    _enable_retirement_witness(backend)
    backend.start_receiver(_Core(), serial_gate=SerialOperationGate(), dispatch_to_owner=True)
    try:
        discard_without_writing_and_close, closer_outcomes = _gated_closed_discard_transition(
            backend, monkeypatch, threads
        )

        def admit_then_discard_and_close() -> None:
            _admit_identified_edge_request(peer, backend)
            discard_without_writing_and_close()

        outcome, elapsed = _run_transition_while_wait_is_outstanding(
            backend,
            admit_then_discard_and_close,
            resume_when="deadline = time.monotonic() + timeout",
            threads=threads,
        )
        assert isinstance(outcome, AssertionError), outcome
        assert "closure and not retirement" in str(outcome)
        assert closer_outcomes == [None], closer_outcomes
        assert backend._pre_close_snapshot["pending_edge_requests"] == 0
        assert _inbound_retirement_ledger(backend) == (1, frozenset(), frozenset({1}))
        assert _responses_written(backend) == 0
        # The admission is visible to the witness and no write ever followed it.
        assert _edge_admission_witness(backend).outstanding == frozenset({1})
        assert elapsed < 5.0, f"rejection was not bounded ({elapsed:.3f}s)"
        assert peer._sock.recv(1) == b"", "the abandoned request was answered after all"
    finally:
        monkeypatch.undo()
        _teardown_transport(peer, backend, *threads)


def test_retirement_wait_rejects_a_replay_decoy_over_a_closed_discard(monkeypatch) -> None:
    """A duplicate replay must not certify a different request the close dropped.

    A first identified request is admitted, answered by the owner path, and its
    response is read off the wire, so the backend's per-admission record shows
    it completed.  A second identified request is then admitted and left on the
    owner queue.  The wait enters and parks.  While it is parked the peer replays
    the first request: the reader answers the duplicate from the bounded replay
    window, which grows the aggregate response counter without answering
    anything new.  The close then publishes terminal state and the owner's real
    closed branch releases the second request without answering it, so the close
    samples ``pending_edge_requests`` zero.

    A rule that accepted "zero at close plus counter growth" certifies the
    dropped second request on the strength of the first request's duplicate,
    which is exactly the acceptance path this regression pins.
    """
    peer, backend = NetworkBackend.pair()
    from tests.test_network_owner_execution import _Core

    threads: list[threading.Thread] = []
    _enable_retirement_witness(backend)
    backend.start_receiver(_Core(), serial_gate=SerialOperationGate(), dispatch_to_owner=True)
    try:
        responded = _EDGE_ID_FRAME.pack(_OP_EDGE_RESP_ID, 0, 1)
        peer._sock.settimeout(_BOUNDED_WAIT)
        _admit_identified_edge_request(peer, backend)
        assert backend.service_pending_edges(max_edges=1) == 1
        _wait_for_edge_requests_retired(backend, timeout=5.0)
        assert _responses_written(backend) == 1
        assert peer._sock.recv(len(responded)) == responded

        _admit_identified_edge_request(peer, backend, edge_id=2)
        assert _inbound_retirement_ledger(backend) == (2, frozenset({1}), frozenset({2}))
        replay_close_and_discard, closer_outcomes = _gated_closed_discard_transition(
            backend, monkeypatch, threads
        )

        def replay_then_discard() -> None:
            peer._sock.sendall(_EDGE_ID_FRAME.pack(_OP_EDGE_REQ_ID, 1, 1))
            assert peer._sock.recv(len(responded)) == responded, "the duplicate was not answered"
            replay_close_and_discard()
            # ``edge_resp_sent`` is incremented only after ``_send_frame`` returns,
            # so it trails the bytes the peer just read; sample it once the closed
            # transition has synchronized instead of racing the reader's increment.
            assert _responses_written(backend) == 2, "the duplicate did not grow the shared counter"

        outcome, elapsed = _run_transition_while_wait_is_outstanding(
            backend,
            replay_then_discard,
            resume_when="deadline = time.monotonic() + timeout",
            threads=threads,
        )
        assert isinstance(outcome, AssertionError), outcome
        assert "closure and not retirement" in str(outcome)
        assert closer_outcomes == [None], closer_outcomes
        assert backend._pre_close_snapshot["pending_edge_requests"] == 0
        # The duplicate grew the aggregate counter while the second request was
        # never answered, so the per-admission record is the only fact that
        # separates this closure from a completed retirement.
        assert _responses_written(backend) == 2
        assert _inbound_retirement_ledger(backend) == (2, frozenset({1}), frozenset({2}))
        assert elapsed < 5.0, f"rejection was not bounded ({elapsed:.3f}s)"
        assert peer._sock.recv(1) == b"", "the discarded request was answered after all"
    finally:
        # See the discard regression above: drop the gated ``set`` before any
        # teardown close so a reported failure cannot hang in ``_close_pair``.
        monkeypatch.undo()
        _teardown_transport(peer, backend, *threads)


def test_retirement_witness_records_identified_and_unidentified_admissions() -> None:
    """Pin the two transcript facts every closed-transport decision rests on.

    The reader appends exactly one ``edge_req_received`` record at the single
    site that raises ``_edge_pending``, carrying that request's ``edge_id`` and
    ``None`` for the historical id-less ``OP_EDGE_REQ``; a response appends
    ``edge_resp_send`` with ``outcome="success"`` only after ``_send_frame``
    returned.  If either record ever changed shape, every decision below would
    be reading a different fact than the one it documents, so the witness's own
    inputs are asserted here against a real reader, a real owner dispatch, and
    real bytes on a real socket pair.
    """
    peer, backend = NetworkBackend.pair()
    from tests.test_network_owner_execution import _Core

    _enable_retirement_witness(backend)
    backend.start_receiver(_Core(), serial_gate=SerialOperationGate(), dispatch_to_owner=True)
    try:
        empty = _edge_admission_witness(backend)
        assert empty == _EdgeAdmissionWitness(
            enabled=True,
            complete=True,
            dropped=0,
            admitted=frozenset(),
            answered=frozenset(),
            unidentified_admissions=0,
            witnessed_highest=0,
        ), empty

        _admit_legacy_edge_request(peer, backend)
        legacy = _edge_admission_witness(backend)
        assert legacy.complete
        assert legacy.unidentified_admissions == 1
        assert legacy.admitted == frozenset()
        assert legacy.outstanding == frozenset()

        # The legacy request is still on the owner queue, so this second
        # admission is only observed once the queue holds both requests.
        _admit_identified_edge_request(peer, backend, edge_id=7, queued=2, expected_pending=2)
        identified = _edge_admission_witness(backend)
        assert identified.complete
        assert identified.admitted == frozenset({7})
        assert identified.answered == frozenset()
        assert identified.outstanding == frozenset({7})
        assert identified.unidentified_admissions == 1

        assert backend.service_pending_edges(max_edges=2) == 2
        _wait_for_pending_zero(backend)
        answered = _edge_admission_witness(backend)
        assert answered.complete
        assert answered.answered == frozenset({7})
        assert answered.outstanding == frozenset()
        # The id-less admission stays visible after it was answered: it still
        # cannot be matched to that answer, which is why any unnamed admission
        # refuses closed acceptance rather than being counted as retired.
        assert answered.unidentified_admissions == 1
    finally:
        _close_pair(peer, backend)


@pytest.mark.parametrize(
    "legacy_before_entry",
    [True, False],
    ids=["legacy-before-entry", "legacy-during-the-wait"],
)
def test_retirement_wait_rejects_a_closed_legacy_discard_that_borrows_an_old_answer(
    monkeypatch, legacy_before_entry: bool
) -> None:
    """An unnamed admission must not be certified by a historical answer.

    A real identified request is admitted, answered by the owner path, and read
    off the wire.  A second, id-less request is then admitted and left
    unanswered, and the close's real closed branch discards it.  The close
    records ``pending_edge_requests == 0``, the aggregate response count never
    moves, and the per-id ledger shows nothing outstanding - the id-less request
    never had an entry - so a rule keyed on "some identified id existed" accepts
    a request that never received a response.

    The witness refuses both orderings, because an admission with no id cannot
    be matched to a write at all.  The second ordering admits the unnamed
    request after the wait has already sampled its entry witness, so the refusal
    cannot come from a stale entry snapshot.
    """
    decisions: list[tuple[bool, object]] = []
    _record_closed_retirement_evidence(monkeypatch, decisions)
    peer, backend = NetworkBackend.pair()
    from tests.test_network_owner_execution import _Core

    threads: list[threading.Thread] = []
    _enable_retirement_witness(backend)
    backend.start_receiver(_Core(), serial_gate=SerialOperationGate(), dispatch_to_owner=True)
    try:
        responded = _EDGE_ID_FRAME.pack(_OP_EDGE_RESP_ID, 0, 1)
        peer._sock.settimeout(_BOUNDED_WAIT)
        _admit_identified_edge_request(peer, backend)
        assert backend.service_pending_edges(max_edges=1) == 1
        _wait_for_pending_zero(backend)
        assert peer._sock.recv(len(responded)) == responded
        if legacy_before_entry:
            _admit_legacy_edge_request(peer, backend)

        discard_without_writing_and_close, closer_outcomes = _gated_closed_discard_transition(
            backend, monkeypatch, threads
        )

        def discard_unnamed_request_and_close() -> None:
            if not legacy_before_entry:
                _admit_legacy_edge_request(peer, backend)
            discard_without_writing_and_close()

        outcome, elapsed = _run_transition_while_wait_is_outstanding(
            backend,
            discard_unnamed_request_and_close,
            resume_when="deadline = time.monotonic() + timeout",
            threads=threads,
        )
        assert isinstance(outcome, AssertionError), outcome
        assert "closure and not retirement" in str(outcome)
        assert closer_outcomes == [None], closer_outcomes
        assert decisions == [(True, 0)], decisions
        assert backend._pre_close_snapshot["pending_edge_requests"] == 0
        # The identified request really was answered, so nothing in the per-id
        # ledger is outstanding: only the admission witness can refuse this.
        assert _inbound_retirement_ledger(backend) == (1, frozenset({1}), frozenset())
        assert _responses_written(backend) == 1
        assert "'unidentified_admissions': 1" in str(outcome), str(outcome)
        assert elapsed < 5.0, f"rejection was not bounded ({elapsed:.3f}s)"
        assert peer._sock.recv(1) == b"", "the unnamed request was answered after all"
    finally:
        # Restore the real ``_closed_event.set`` before teardown; see the
        # unwritten-discard regression for why the gate must be dropped here.
        monkeypatch.undo()
        _teardown_transport(peer, backend, *threads)


def test_retirement_wait_accepts_a_clean_close_after_the_replay_window_evicts_a_retired_id(
    monkeypatch,
) -> None:
    """The bounded replay window is not a completion ledger.

    ``_inbound_edge_responses`` keeps only the most recent 256 completed
    responses, so the oldest retired id disappears from it while the connection
    itself is entirely healthy.  Every one of these requests is admitted by the
    real reader, dispatched to the real owner, written to the real socket, and
    retired while the transport is still open, and the close then finds nothing
    outstanding.  A rule that required a cached response for each id in its
    entry ledger rejects this legitimate retirement; the admission witness
    instead decides from per-admission records that outlive the replay window.
    """
    decisions: list[tuple[bool, object]] = []
    _record_closed_retirement_evidence(monkeypatch, decisions)
    peer, backend = NetworkBackend.pair()
    from tests.test_network_owner_execution import _Core

    threads: list[threading.Thread] = []
    retired = _EDGE_RESPONSE_HISTORY_MAX + 1
    _enable_retirement_witness(backend)
    backend.start_receiver(_Core(), serial_gate=SerialOperationGate(), dispatch_to_owner=True)
    try:
        peer._sock.settimeout(_BOUNDED_WAIT)
        _admit_identified_edge_request(peer, backend, edge_id=1)
        received = bytearray()

        def retire_every_request_and_close() -> None:
            for edge_id in range(1, retired + 1):
                if edge_id > 1:
                    _admit_identified_edge_request(peer, backend, edge_id=edge_id)
                assert backend.service_pending_edges(max_edges=1) == 1
                _wait_for_pending_zero(backend)
                frame = _EDGE_ID_FRAME.pack(_OP_EDGE_RESP_ID, 0, edge_id)
                received.extend(peer._sock.recv(len(frame)))
            backend._mark_closed_uncoordinated()

        outcome, elapsed = _run_transition_while_wait_is_outstanding(
            backend,
            retire_every_request_and_close,
            resume_when="deadline = time.monotonic() + timeout",
            threads=threads,
        )
        assert outcome is None, outcome
        assert backend._closed
        assert decisions == [(True, 0)], decisions
        assert elapsed < _BOUNDED_WAIT, f"acceptance was not bounded ({elapsed:.3f}s)"
        expected = b"".join(
            _EDGE_ID_FRAME.pack(_OP_EDGE_RESP_ID, 0, edge_id) for edge_id in range(1, retired + 1)
        )
        assert bytes(received) == expected, "not every retired response reached the peer"
        assert peer._sock.recv(1) == b"", "the closed transport did not reach the peer as EOF"
        # The regression only means something if the replay window really
        # evicted the oldest id, which is the fact the pre-round-6 rule keyed on.
        _highest, replayed, unretired = _inbound_retirement_ledger(backend)
        assert 1 not in replayed, "the bounded replay window did not evict the oldest id"
        assert unretired == frozenset()
        witness = _edge_admission_witness(backend)
        assert witness.complete and not witness.unidentified_admissions, witness
        assert witness.admitted == frozenset(range(1, retired + 1))
        assert witness.answered == witness.admitted
    finally:
        _teardown_transport(peer, backend, *threads)


def test_retirement_wait_accepts_a_clean_close_after_a_nonconsecutive_id_gap(
    monkeypatch,
) -> None:
    """A high-water mark is not a contiguous range of admissions.

    ``_claim_inbound_edge_id`` accepts any id above the high-water mark, so a
    peer that abandons a request and recovers on the next id leaves a gap that
    this connection never admitted.  Both requests here are admitted, answered
    by the real owner, written to the peer, and retired, and the close finds
    nothing outstanding.  A rule that ranged over the high-water mark invents an
    admission for the missing id and rejects the retirement; the admission
    witness only ever counts ids the reader really admitted.
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
        _admit_identified_edge_request(peer, backend, edge_id=1)
        received = bytearray()

        def retire_one_three_and_close() -> None:
            for edge_id in (1, 3):
                if edge_id != 1:
                    _admit_identified_edge_request(peer, backend, edge_id=edge_id)
                assert backend.service_pending_edges(max_edges=1) == 1
                _wait_for_pending_zero(backend)
                frame = _EDGE_ID_FRAME.pack(_OP_EDGE_RESP_ID, 0, edge_id)
                received.extend(peer._sock.recv(len(frame)))
            backend._mark_closed_uncoordinated()

        outcome, elapsed = _run_transition_while_wait_is_outstanding(
            backend,
            retire_one_three_and_close,
            resume_when="deadline = time.monotonic() + timeout",
            threads=threads,
        )
        assert outcome is None, outcome
        assert backend._closed
        assert decisions == [(True, 0)], decisions
        assert elapsed < _BOUNDED_WAIT, f"acceptance was not bounded ({elapsed:.3f}s)"
        assert bytes(received) == (
            _EDGE_ID_FRAME.pack(_OP_EDGE_RESP_ID, 0, 1)
            + _EDGE_ID_FRAME.pack(_OP_EDGE_RESP_ID, 0, 3)
        ), "the nonconsecutive retirements did not reach the peer"
        assert peer._sock.recv(1) == b"", "the closed transport did not reach the peer as EOF"
        ledger = _inbound_retirement_ledger(backend)
        assert ledger == (3, frozenset({1, 3}), frozenset()), ledger
        witness = _edge_admission_witness(backend)
        assert witness.complete and not witness.unidentified_admissions, witness
        assert witness.admitted == frozenset({1, 3})
        assert witness.answered == frozenset({1, 3})
    finally:
        _teardown_transport(peer, backend, *threads)


def test_retirement_wait_reports_missing_witness_evidence_when_the_transcript_evicts(
    monkeypatch,
) -> None:
    """An evicted transcript is missing evidence, not an empty one.

    With a ring too small for the connection, the oldest admission record is
    evicted, so the witness can no longer show that every admitted request was
    answered.  The wait must refuse loudly and name the missing evidence, rather
    than read the shortened transcript as "nothing was ever admitted".  The
    recorded decision still comes from the closed-transport branch on a clean
    close with zero outstanding work, so this is the same schedule as the
    acceptance regressions apart from the ring capacity.
    """
    peer, backend = NetworkBackend.pair()
    from tests.test_network_owner_execution import _Core

    threads: list[threading.Thread] = []
    _enable_retirement_witness(backend, max_entries=_EDGE_RETIREMENT_WITNESS_EVICTING_ENTRIES)
    backend.start_receiver(_Core(), serial_gate=SerialOperationGate(), dispatch_to_owner=True)
    try:
        peer._sock.settimeout(_BOUNDED_WAIT)
        for edge_id in range(1, 6):
            _admit_identified_edge_request(peer, backend, edge_id=edge_id)
            assert backend.service_pending_edges(max_edges=1) == 1
            _wait_for_pending_zero(backend)
            frame = _EDGE_ID_FRAME.pack(_OP_EDGE_RESP_ID, 0, edge_id)
            assert peer._sock.recv(len(frame)) == frame
        witness = _edge_admission_witness(backend)
        assert witness.dropped > 0, "the bounded ring did not evict a record"
        assert not witness.complete, witness

        outcome, elapsed = _run_transition_while_wait_is_outstanding(
            backend,
            backend._mark_closed_uncoordinated,
            resume_when="deadline = time.monotonic() + timeout",
            threads=threads,
        )
        assert isinstance(outcome, AssertionError), outcome
        assert "closure and not retirement" in str(outcome)
        assert backend._pre_close_snapshot["pending_edge_requests"] == 0
        assert "'complete': False" in str(outcome), str(outcome)
        assert "'dropped': " in str(outcome), str(outcome)
        assert elapsed < 5.0, f"rejection was not bounded ({elapsed:.3f}s)"
    finally:
        _teardown_transport(peer, backend, *threads)


def test_retirement_wait_rejects_a_witness_enabled_after_the_admission(monkeypatch) -> None:
    """A transcript that starts after the admission is refused, not trusted.

    ``enable_serial_transcript`` starts an empty ring, so a caller that enables
    it after this connection already admitted identified work would see no
    admission for a request the backend really holds.  The schedule reaches the
    decision with a close that found **no** outstanding work: the owner's real
    closed branch discards the queued request before the close samples its
    snapshot, so the recorded count is zero, exactly as in the acceptance
    schedule.  That leaves the clauses that compare the transcript against the
    backend's own ledger as the only ones able to refuse it, which is what this
    regression is for.  A rule that trusts the transcript's visible ids accepts
    this close; the witness instead reports the admitted id as missing evidence,
    because it is still claimed-and-unanswered in ``_inbound_edge_ids_pending``
    and ``_highest_inbound_edge_id`` names an id the transcript never saw.
    """
    decisions: list[tuple[bool, object]] = []
    _record_closed_retirement_evidence(monkeypatch, decisions)
    peer, backend = NetworkBackend.pair()
    from tests.test_network_owner_execution import _Core

    threads: list[threading.Thread] = []
    backend.start_receiver(_Core(), serial_gate=SerialOperationGate(), dispatch_to_owner=True)
    try:
        _admit_identified_edge_request(peer, backend)
        assert _inbound_retirement_ledger(backend) == (1, frozenset(), frozenset({1}))
        _enable_retirement_witness(backend)
        witness = _edge_admission_witness(backend)
        assert witness.admitted == frozenset(), witness
        assert not witness.complete, witness

        discard_without_writing_and_close, closer_outcomes = _gated_closed_discard_transition(
            backend, monkeypatch, threads
        )
        outcome, elapsed = _run_transition_while_wait_is_outstanding(
            backend,
            discard_without_writing_and_close,
            resume_when="deadline = time.monotonic() + timeout",
            threads=threads,
        )
        assert isinstance(outcome, AssertionError), outcome
        assert "closure and not retirement" in str(outcome)
        assert closer_outcomes == [None], closer_outcomes
        # The predicate really was consulted on a zero of the close's own
        # making, so the refusal below cannot come from the pre-close count.
        assert decisions == [(True, 0)], decisions
        assert backend._pre_close_snapshot["pending_edge_requests"] == 0
        assert "'complete': False" in str(outcome), str(outcome)
        assert "'highest_inbound_edge_id': 1" in str(outcome), str(outcome)
        assert "'identified_admissions': 0" in str(outcome), str(outcome)
        assert "'unretired_inbound_edge_ids': [1]" in str(outcome), str(outcome)
        assert elapsed < 5.0, f"rejection was not bounded ({elapsed:.3f}s)"
        assert peer._sock.recv(1) == b"", "the discarded request was answered after all"
    finally:
        # Restore the real ``_closed_event.set`` before teardown; see the
        # unnamed-discard regression for why the gate must be dropped here.
        monkeypatch.undo()
        _teardown_transport(peer, backend, *threads)
