"""Retirement-evidence helpers for the network edge-response owner.

Split out of ``tests/test_network_cpu_owner.py`` to satisfy the tracked-file
size limit (#122).  These helpers drive ``_wait_for_edge_requests_retired`` and
the admission/retirement witnesses that the edge-retirement regressions in
``test_network_edge_retirement_wait.py`` and
``test_network_edge_retirement_reciprocal.py`` rely on.
"""

from __future__ import annotations

import linecache
import socket
import sys
import threading
import time
from typing import NamedTuple

from pokered_harness.link.network_backend import (
    _EDGE_ID_FRAME,
    _FRAME,
    _MAX_SERIAL_TRANSCRIPT_ENTRIES,
    _OP_EDGE_REQ,
    _OP_EDGE_REQ_ID,
    NetworkBackend,
    _InboundEdge,
)

# Generous, still-bounded ceiling for the coordination waits and joins these
# regressions use.  It only elapses on failure; on the passing path every wait
# is satisfied almost immediately, so a loaded host cannot turn a genuine
# rejection into a spurious timeout-driven failure.
_BOUNDED_WAIT = 30.0

# The closed-transport decision reads the backend's own opt-in serial
# transcript, which is the only connection-scoped record that contains every
# admission - including the legacy, id-less ``OP_EDGE_REQ`` format that leaves
# no entry in the per-id ledger.  Capacity is the module ceiling so a
# regression that admits many sequential requests cannot evict the evidence it
# is judged on; ``_EDGE_RETIREMENT_WITNESS_EVICTING_ENTRIES`` is deliberately
# small and is used only to show what the helper does when the evidence really
# is missing.
_EDGE_RETIREMENT_WITNESS_ENTRIES = _MAX_SERIAL_TRANSCRIPT_ENTRIES
_EDGE_RETIREMENT_WITNESS_EVICTING_ENTRIES = 4

# The reader records exactly one ``edge_req_received`` transcript event per
# admission, at the single site that raises ``_edge_pending``.  A successful
# socket write records ``edge_resp_send`` with ``outcome="success"``; the
# closed, errored, and duplicate-replay paths are distinguished by that field.
# ``_claim_inbound_edge_id`` is the only raiser of the backend's inbound
# high-water mark, and each claim then records either ``edge_req_received``
# (queued for the owner) or ``reciprocal_master_edge`` (answered on the reader
# thread against this owner's master edge), so the witness ranges over both when
# it cross-checks its transcript against that mark.  A reciprocal record is an
# admission with its own write obligation, not merely a high-water datum, so the
# witness counts an identified reciprocal id as admitted and an id-less one as
# unnamed; only that shape makes a failed reciprocal write detectable.
_EDGE_ADMISSION_EVENT = "edge_req_received"
_EDGE_WRITE_EVENT = "edge_resp_send"
_EDGE_RECIPROCAL_EVENT = "reciprocal_master_edge"


def _responses_written(backend: NetworkBackend) -> int:
    """Count the ``EDGE_RESP`` frames this backend actually wrote to the wire.

    ``edge_resp_sent`` is incremented only after ``_send_frame`` returns, and
    ``_send_edge_response`` deliberately skips the increment on its
    "not sent, closed" path, so this counter is evidence that an ``EDGE_RESP``
    reached the socket.  It is aggregate evidence and is never sufficient on
    its own: a replay, or a response belonging to a different admitted request,
    grows it too.  It is now used only for diagnostics and for an explicit
    "bytes reached the peer" assertion; :func:`_closed_transport_retirement_is_evidenced`
    decides on per-admission retirement records instead.
    """
    return int(backend._stats["edge_resp_sent"])


def _inbound_retirement_ledger(
    backend: NetworkBackend,
) -> tuple[int, frozenset[int], frozenset[int]]:
    """Read the backend's own per-admission retirement ledger.

    ``network_backend`` keeps three connection-scoped, monotonically growing
    facts about identified (``OP_EDGE_REQ_ID``) admissions: the highest id it
    ever admitted, the ids it recorded a completed response for, and the ids it
    claimed but has not answered.  A response is recorded inside the write guard
    immediately before ``_send_frame`` runs, so an id in that set is a real
    write path rather than an aggregate count:

    * a duplicate replay of an already-retired id returns the recorded response
      and records nothing new, so a replay cannot manufacture a retirement
      record;
    * a request the owner's closed branch discards is released through
      ``_decrement_edge_pending`` without ever being recorded, so it stays
      claimed and never appears in the recorded set.

    ``_edge_id_lock`` is a leaf lock: nothing holds it while taking another
    transport lock, so reading the ledger here (including while the caller holds
    ``_edge_pending_condition``, as the retirement wait does) cannot invert the
    close path's lock order.
    """
    with backend._edge_id_lock:
        return (
            int(backend._highest_inbound_edge_id),
            frozenset(backend._inbound_edge_responses),
            frozenset(backend._inbound_edge_ids_pending),
        )


def _pending_edge_requests_at_close(backend: NetworkBackend) -> object:
    """Read the close snapshot's admitted-work count without taking a lock.

    ``None`` means the terminal transition had not published its snapshot at
    the moment the caller looked.  That is reported as missing evidence and
    never read as a zero.
    """
    pre_close = backend._pre_close_snapshot
    if isinstance(pre_close, dict):
        return pre_close.get("pending_edge_requests")
    return None


def _await_wire_record_publication(
    backend: NetworkBackend, *, timeout: float = _BOUNDED_WAIT
) -> bool:
    """Bound the producers that can still publish a transcript record.

    ``_mark_closed_common`` publishes ``_closed`` without joining the reader or
    the response workers, and ``_send_reciprocal_master_response`` answers an
    incoming request on the reader thread without ever touching
    ``_edge_pending``, so observing zero admitted work under
    ``_edge_pending_condition`` does not exclude that producer.  Reading the
    serial transcript while it was still in flight certified an unnamed
    reciprocal request whose response never reached the wire: the copy the
    decision was taken from simply did not contain the admission yet.

    Both the reader and both response workers hold
    ``_edge_wire_completion_lock`` across their transcript publication and the
    write it reports, so a bounded join of the reader followed by a bounded
    acquire of that lock - taken by the caller with no transport lock held - is
    a completion boundary for those records.  The join also closes the window
    before the lock is taken, where the reader has already deserialised a frame
    but has not yet entered its guarded section.  A timeout is reported as
    missing evidence and is never read as a settled transcript.
    """
    reader = backend._reader
    if reader is not None:
        reader.join(timeout=timeout)
        if reader.is_alive():
            return False
    completion_lock = backend._edge_wire_completion_lock
    if not completion_lock.acquire(timeout=timeout):
        return False
    completion_lock.release()
    return True


class _EdgeAdmissionWitness(NamedTuple):
    """Connection-scoped admission/answer state read from the serial transcript.

    ``admitted`` and ``answered`` hold identified request ids: the ids the
    reader admitted - including a request it answered reciprocally on its own
    thread - and the ids a successful ``EDGE_RESP`` write was recorded for.
    ``unidentified_admissions`` counts admissions that carry no id, which is
    both the historical id-less ``OP_EDGE_REQ`` format and the id-less
    reciprocal request, because neither can be matched to an individual write.
    ``dropped`` is the number of records the bounded ring evicted,
    ``witnessed_highest`` is the highest id any admission or reciprocal record
    named, and ``complete`` records whether this witness accounts for every
    admission the backend ever made.
    """

    enabled: bool
    complete: bool
    dropped: int
    admitted: frozenset[int]
    answered: frozenset[int]
    unidentified_admissions: int
    witnessed_highest: int

    @property
    def outstanding(self) -> frozenset[int]:
        """Identified ids that were admitted with no recorded successful write."""
        return self.admitted - self.answered


def _edge_admission_witness(backend: NetworkBackend) -> _EdgeAdmissionWitness:
    """Derive per-admission retirement state from the backend's own transcript.

    The serial transcript is the only connection-scoped record that covers every
    admission, including the id-less ``OP_EDGE_REQ`` format that leaves no entry
    in the per-id ledger.  ``_record_serial_event`` appends under
    ``_serial_transcript_lock``, which is a leaf lock: no caller holds it while
    acquiring another transport lock, so reading here - including while the
    caller holds ``_edge_pending_condition``, as the retirement wait does -
    cannot invert the close path's lock order.  ``snapshot_stats`` copies the
    ring, so the returned facts are a consistent point-in-time sample and the
    live deque is never mutated by this read.

    ``complete`` is the evidence-sufficiency check, not a guess:

    * the ring must be enabled (a disabled transcript records nothing);
    * ``dropped`` must be zero, so no record was evicted and a missing
      admission cannot be read as one that never happened;
    * the highest id this witness saw must match the backend's own
      ``_highest_inbound_edge_id``, so a transcript enabled after this
      connection already admitted identified work is refused rather than
      silently under-reporting admissions.  ``_claim_inbound_edge_id`` is the
      only raiser of that mark, and every claim then records either
      ``edge_req_received`` (queued behind ``_edge_pending``) or
      ``reciprocal_master_edge`` (answered on the reader thread against this
      owner's master edge), so the cross-check ranges over both and does not
      mistake a reciprocally answered request for a transcript that started
      late.  A transcript that under-reports is refused here rather than read as
      "nothing was admitted"; the narrower case of a claim that recorded nothing
      at all is refused by the conservation clause below, because such a claim
      keeps its id in the claimed-but-unanswered set;
    * a reciprocal record is an admission in its own right, not only a
      high-water datum.  The reader answers that request on its own thread,
      records ``reciprocal_master_edge`` before it attempts the write, and never
      raises ``_edge_pending``, so the write is invisible to the pending
      counter, to the close snapshot, and to the per-id ledger - which
      publishes the response before ``_send_frame`` and so counts a failed send
      as retired.  Only that request's own ``edge_resp_send`` record with
      ``outcome="success"`` shows its response reached the wire, so an
      identified reciprocal request joins ``admitted`` and an id-less one joins
      the unnamed count.  Ranging over the record for completeness while
      declining it an obligation is what let a failed reciprocal write be
      certified as retired;
    * every id the backend still counts as claimed-and-unanswered must appear in
      this witness's admitted set.  Both cross-checks read the per-id ledger
      rather than the bounded replay window, because an id-less admission leaves
      no per-id entry and a reciprocally answered one is retired by the reader
      without ever reaching the owner, so only ``_highest_inbound_edge_id`` and
      the claimed-id set are authoritative for what this connection still owes a
      response.

    A transcript write failure is recorded as ``outcome="error"`` and a
    transport that closed before the write as ``outcome="not_sent_closed"``, so
    neither is counted as a completed response.
    """
    info = backend.snapshot_stats()["serial_transcript"]
    enabled = bool(info["enabled"])
    dropped = int(info["dropped"])
    records = info["records"] if enabled else []
    admitted: set[int] = set()
    answered: set[int] = set()
    witnessed_highest = 0
    unidentified = 0
    for record in records:
        event = record.get("event")
        # Both transcript admission sites are accounted identically on purpose.
        # A request queued behind ``_edge_pending`` (``edge_req_received``) and a
        # request the reader answers itself on its own thread
        # (``reciprocal_master_edge``) each owe a successful write, and each
        # carries ``edge_id=None`` when the peer used the id-less format.
        if event in (_EDGE_ADMISSION_EVENT, _EDGE_RECIPROCAL_EVENT):
            edge_id = record.get("edge_id")
            if edge_id is None:
                unidentified += 1
            else:
                admitted.add(int(edge_id))
                witnessed_highest = max(witnessed_highest, int(edge_id))
        elif event == _EDGE_WRITE_EVENT and record.get("outcome") == "success":
            edge_id = record.get("edge_id")
            if edge_id is not None:
                answered.add(int(edge_id))
    highest_edge_id, _retired, unretired = _inbound_retirement_ledger(backend)
    complete = (
        enabled and dropped == 0 and unretired <= admitted and witnessed_highest == highest_edge_id
    )
    return _EdgeAdmissionWitness(
        enabled=enabled,
        complete=complete,
        dropped=dropped,
        admitted=frozenset(admitted),
        answered=frozenset(answered),
        unidentified_admissions=unidentified,
        witnessed_highest=witnessed_highest,
    )


def _witness_summary(witness: _EdgeAdmissionWitness) -> dict[str, object]:
    """Compact, bounded description of one witness for failure diagnostics."""
    return {
        "enabled": witness.enabled,
        "complete": witness.complete,
        "dropped": witness.dropped,
        "identified_admissions": len(witness.admitted),
        "identified_answered": len(witness.answered),
        "identified_outstanding": sorted(witness.outstanding),
        "unidentified_admissions": witness.unidentified_admissions,
        "witnessed_highest": witness.witnessed_highest,
    }


def _retirement_diagnostics(backend: NetworkBackend) -> dict[str, object]:
    """Collect failure diagnostics without acquiring any transport lock.

    A terminal transition takes ``_close_lock`` and then
    ``_edge_pending_condition``, and ``debug_snapshot`` wants them in the same
    order, so a failure path that already holds either lock can invert the order
    against a concurrent close.  These reads are plain attribute reads on
    counters that are only ever assigned, which is enough for a diagnostic.
    The admission witness reads the transcript ring under its own leaf lock so a
    rejection can name which piece of evidence was missing.
    """
    pre_close = backend._pre_close_snapshot
    reader_error = None
    if isinstance(pre_close, dict):
        reader_error = pre_close.get("reader_error")
    highest_edge_id, retired_ids, unretired_ids = _inbound_retirement_ledger(backend)
    return {
        "pending_edge_requests": backend._edge_pending,
        "closed": backend._closed,
        "pending_at_close": _pending_edge_requests_at_close(backend),
        "reader_error": reader_error,
        "responses_written": _responses_written(backend),
        "highest_inbound_edge_id": highest_edge_id,
        "retired_inbound_edge_ids": sorted(retired_ids),
        "unretired_inbound_edge_ids": sorted(unretired_ids),
        "admission_witness": _witness_summary(_edge_admission_witness(backend)),
    }


def _closed_transport_retirement_is_evidenced(
    backend: NetworkBackend,
    *,
    pending_at_close: object,
    witness_at_entry: _EdgeAdmissionWitness,
) -> bool:
    """Decide whether a closed transport really retired the admitted work.

    Called only after ``_edge_pending`` was observed at zero under
    ``_edge_pending_condition`` while ``_closed`` was already published.
    ``_mark_closed_common`` publishes ``_closed`` before it samples its
    snapshot and zeroes the admitted-work count under that same condition, so
    a terminal transport supplies a zero of its own.  None of the facts a
    closed transport makes cheap separates retirement from closure:

    * The close snapshot alone is not enough.  An owner that has already seen
      the published ``_closed`` runs the real closed branch of
      ``_service_pending_edges_locked``, which calls ``_decrement_edge_pending``
      and drops the request unanswered.  That decrement lands before the close
      samples its snapshot, so the snapshot can read zero with an admitted
      request never answered.
    * The aggregate ``edge_resp_sent`` growth alone is not enough either.  A
      duplicate replay of an already-answered id writes ``EDGE_RESP`` and grows
      that counter without answering anything new, so it can supply the growth
      for a request the close discarded.
    * The backend's per-id replay window is not a completion ledger either.
      ``_inbound_edge_responses`` is a bounded duplicate-replay cache that
      evicts its oldest entry once 256 responses are recorded
      (``_EDGE_RESPONSE_HISTORY_MAX``), so an id missing from it is not
      evidence that the request was never answered.  ``_highest_inbound_edge_id``
      also accepts any id above the high-water mark without requiring
      consecutive ids, so ranging over it invents admissions for ids the peer
      never sent.

    A per-admission record is required instead, and the only connection-scoped
    record that covers every admission is the backend's own opt-in serial
    transcript.  The reader appends exactly one ``edge_req_received`` record at
    the single site that raises ``_edge_pending``, carrying that request's
    ``edge_id`` - ``None`` for the historical id-less ``OP_EDGE_REQ`` - and a
    response appends ``edge_resp_send`` with ``outcome="success"`` only after
    ``_send_frame`` returned.  :func:`_edge_admission_witness` turns those into
    the connection's admitted and answered id sets plus the count of unnamed
    admissions.  A request the reader answers reciprocally on its own thread is
    an admission too: that path records ``reciprocal_master_edge`` before it
    tries the write and never raises ``_edge_pending``, so its own
    ``edge_resp_send`` record is the only evidence that the response reached the
    wire, and the witness registers it as an admission for exactly that reason.

    The decision requires all of:

    * ``pending_edge_requests`` in the close snapshot must be zero, so the close
      itself discarded nothing; a release after terminal publication must not be
      able to certify this wait.
    * the witness must be complete: enabled, nothing evicted, and accounting for
      every identified id the backend ever admitted.  This wait covers the
      connection's own lifetime, so an unusable transcript is reported as
      missing evidence and never read as an absence of admissions.  Long-lived
      callers outside this file do not reach this branch, which is why the
      completeness requirement is stated here rather than assumed.
    * no unnamed admission may exist anywhere in the connection.  An id-less
      request - the historical ``OP_EDGE_REQ`` or the id-less reciprocal request
      the reader answers itself - cannot be matched to a write, so a closed
      transport that saw one is refused rather than guessed at; a historical
      identified answer must not authenticate a later unanswered unnamed
      discard.
    * every identified admission that was outstanding when the wait began must
      have a recorded successful write now, which is this wait's own contract.
    * every identified admission that became outstanding during the wait must
      have one too, so a request admitted while the wait was parked cannot be
      certified by the close that discarded it.  Together these two clauses say
      that no identified admission is left unanswered, and each is isolated by
      its own regression: an entry-scope request is dropped by the unwritten
      discard, and a wait-scope request is dropped by the same schedule with its
      admission moved after the entry sample.
    * at least one identified request must have been admitted, so a zero that
      never covered admitted work certifies nothing.

    The caller is responsible for reading this predicate only after
    :func:`_await_wire_record_publication` has bounded the reader and the
    response workers: the witness is a snapshot, so a producer that is still
    mid-publication would be invisible to every clause here.

    Kept as its own function so a regression can install a predicate that never
    consents and show this acceptance path is load-bearing rather than
    incidental.
    """
    if pending_at_close != 0:
        return False
    witness = _edge_admission_witness(backend)
    if not witness.complete:
        return False
    if witness.unidentified_admissions:
        return False
    if witness_at_entry.outstanding - witness.answered:
        return False
    if witness.outstanding - witness_at_entry.outstanding:
        return False
    return bool(witness.admitted)


def _wait_for_edge_requests_retired(backend: NetworkBackend, *, timeout: float = 10.0) -> None:
    """Wait until every admitted edge response has reached the wire.

    ``pending_edge_requests`` counts an ``EDGE_REQ`` "from enqueue until their
    response has been written", and in owner-dispatch mode that write happens
    on the response worker, not on the owner thread.  The counter is therefore
    not zero the instant an owner frame returns, so sampling it immediately
    measures thread scheduling rather than backend state.  Wait for the
    documented retirement, notified by ``_decrement_edge_pending``, and let the
    caller assert the invariant.

    Zero pending work is necessary but not sufficient.  A terminal transport
    transition drives the counter to zero while abandoning admitted work, and
    ``_mark_closed_common`` publishes ``_closed`` before it samples the state it
    records, so neither "pending zero" nor "no reader error" separates
    retirement from closure on its own.  Two schedules are accepted instead,
    and both anchor the release to the transport being open when it happened:

    * The zero is observed under the condition while ``_closed`` is still
      false.  The close publishes ``_closed`` before it takes this condition to
      zero the counter, so on an open transport only
      ``_decrement_edge_pending`` can zero it: that release is the retirement
      notification, and a close arriving afterwards must not invalidate it.
    * The zero is observed with a published ``_closed`` and
      :func:`_closed_transport_retirement_is_evidenced` shows that the close
      found nothing outstanding and that the backend's own serial transcript
      records a completed write for every identified request it admitted,
      including any it had already claimed when this wait began.  A published
      close does not join the reader or the response workers, so the witness is
      read only after :func:`_await_wire_record_publication` bounds those
      producers outside this condition; a transcript copy taken while a
      reciprocal admission was still in flight certified a request whose
      response never reached the wire.

    Every other exit is reported as a closure with lock-free diagnostics, and a
    transport that is already closed when the wait begins is refused outright.
    When the publication boundary expires, the refusal says so - the transcript
    could not be settled, which is missing evidence rather than a demonstration
    that the response was never written.
    The admission witness is sampled before that refusal, so an admission
    already claimed when this wait is entered is still part of the entry
    snapshot the closed-transport decision compares against.  The transcript it
    reads is opt-in and is enabled by the closed-transport regressions below
    before ``start_receiver``; a caller that never reaches the closed branch
    pays nothing for it, and a closed branch reached without one is refused as
    missing evidence rather than guessed at.

    Known limitation, unchanged from the original assertions: a release that
    runs one of the discard paths while the transport is still open is
    indistinguishable from a completed response through the open-transport
    observation itself, because those paths call ``_decrement_edge_pending``
    without writing.  That is why the closed-transport branch may not lean on an
    aggregate count and why an unidentified admission cannot be certified after
    a close: closure is still rejected, and a request that has not produced a
    response keeps its id claimed, so the leak this wait was added to catch
    cannot hide behind a close.
    """
    witness_at_entry = _edge_admission_witness(backend)
    if backend._closed:
        raise AssertionError(
            "network backend was already closed before the admitted edge response "
            f"wait began; diagnostics={_retirement_diagnostics(backend)}"
        )
    deadline = time.monotonic() + timeout
    timed_out = False
    retired_while_open = False
    closed_at_zero = False
    with backend._edge_pending_condition:
        while True:
            if backend._edge_pending == 0:
                if not backend._closed:
                    retired_while_open = True
                else:
                    closed_at_zero = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            backend._edge_pending_condition.wait(timeout=remaining)
    # Every failure path below runs with no transport lock held and uses only
    # lock-free diagnostics, so a concurrent close can always finish and the
    # failure stays bounded instead of deadlocking on the inverted lock order.
    if timed_out:
        raise AssertionError(
            "admitted edge response was not written within "
            f"{timeout:g}s; diagnostics={_retirement_diagnostics(backend)}"
        )
    if retired_while_open:
        return
    # A zero seen with a published close is the close's own zero, not yet a
    # settled transcript.  The reciprocal handler answers on the reader thread
    # and never touches ``_edge_pending``, so this condition could not exclude
    # it: taking the witness straight from the loop certified an unnamed
    # reciprocal admission whose record was still in flight.  Wait - outside
    # the condition, because the producers themselves need it - for the reader
    # and the response workers to finish publishing, and only then read the
    # evidence.
    publication_settled = closed_at_zero and _await_wire_record_publication(
        backend, timeout=_BOUNDED_WAIT
    )
    if publication_settled and _closed_transport_retirement_is_evidenced(
        backend,
        pending_at_close=_pending_edge_requests_at_close(backend),
        witness_at_entry=witness_at_entry,
    ):
        return
    if not closed_at_zero:
        publication = "not evaluated: the wait did not observe a zero with a close"
    elif publication_settled:
        publication = "settled"
    else:
        publication = (
            "unsettled: a transcript producer was still publishing when the "
            f"{_BOUNDED_WAIT:g}s publication boundary expired, so this refusal is missing "
            "evidence - it does not show the response was never written"
        )
    raise AssertionError(
        "network backend closed while the admitted edge response was outstanding, "
        "so zero pending work is closure and not retirement; "
        f"transcript_publication={publication}; "
        f"diagnostics={_retirement_diagnostics(backend)}"
    )


def _abandon_test_transport(backend: NetworkBackend) -> None:
    """Release a transport whose inspector can no longer be joined.

    Only used by teardown when a regression is being demonstrated and the
    pending condition is still owned by a thread that cannot finish; reaching
    ``_mark_closed_uncoordinated`` would block on that condition, so the socket
    is released directly and the runner stays bounded.
    """
    try:
        backend._sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        backend._sock.close()
    except OSError:
        pass


def _close_pair(peer: NetworkBackend, backend: NetworkBackend) -> None:
    """Terminate both ends of a real transport pair, bounded and idempotent."""
    backend._mark_closed_uncoordinated()
    assert backend.stop(timeout_s=2)
    assert peer.stop(timeout_s=2)


def _teardown_transport(
    peer: NetworkBackend, backend: NetworkBackend, *threads: threading.Thread
) -> None:
    """Close a transport pair without waiting on a thread that may hold its locks.

    A regressed helper can leave a participant owning the close lock, and both
    ``_close_pair`` and ``stop()`` acquire it without a timeout.  When a
    participant is still alive after a bounded join, the sockets are released
    directly instead, so the runner reports the failed assertion rather than
    hanging in cleanup.
    """
    for thread in threads:
        thread.join(timeout=5.0)
    if any(thread.is_alive() for thread in threads):
        _abandon_test_transport(backend)
        _abandon_test_transport(peer)
        return
    _close_pair(peer, backend)


def _admit_identified_edge_request(
    peer: NetworkBackend,
    backend: NetworkBackend,
    *,
    edge_id: int = 1,
    queued: int = 1,
    expected_pending: int = 1,
) -> None:
    """Send one identified ``EDGE_REQ`` through the real reader and wait for it.

    The reader increments the admitted-work count before it publishes the
    request on the owner queue and then notifies this condition, so observing a
    queue of ``queued`` published requests under the condition observes genuine
    admission rather than a value assigned by the test.
    """
    peer._sock.sendall(_EDGE_ID_FRAME.pack(_OP_EDGE_REQ_ID, 1, edge_id))
    _await_admitted_edge(backend, queued=queued, expected_pending=expected_pending)


def _admit_legacy_edge_request(
    peer: NetworkBackend,
    backend: NetworkBackend,
    *,
    queued: int = 1,
    expected_pending: int = 1,
) -> None:
    """Send one id-less ``OP_EDGE_REQ`` through the real reader and wait for it.

    Same real admission path as :func:`_admit_identified_edge_request`, in the
    historical two-byte format whose request carries no id.  The transport
    still counts it as admitted work, so it must be part of the closed
    transport's admission witness even though it has no per-id record.
    """
    peer._sock.sendall(_FRAME.pack(_OP_EDGE_REQ, 1))
    _await_admitted_edge(backend, queued=queued, expected_pending=expected_pending)


def _await_admitted_edge(backend: NetworkBackend, *, queued: int, expected_pending: int) -> None:
    """Wait under the condition until the reader published ``queued`` requests."""
    deadline = time.monotonic() + _BOUNDED_WAIT
    with backend._edge_pending_condition:
        while backend._edge_queue.qsize() < queued:
            remaining = deadline - time.monotonic()
            assert remaining > 0, "the network reader never published an admitted EDGE_REQ"
            backend._edge_pending_condition.wait(timeout=remaining)
        assert backend._edge_pending == expected_pending, backend._edge_pending


def _wait_for_pending_zero(backend: NetworkBackend) -> None:
    """Wait until every admitted request's response write retired."""
    deadline = time.monotonic() + _BOUNDED_WAIT
    with backend._edge_pending_condition:
        while backend._edge_pending != 0:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"admitted work never retired ({backend._edge_pending})"
            backend._edge_pending_condition.wait(timeout=remaining)


def _enable_retirement_witness(
    backend: NetworkBackend, *, max_entries: int = _EDGE_RETIREMENT_WITNESS_ENTRIES
) -> None:
    """Enable the serial transcript the closed-transport decision reads.

    Must be called before ``start_receiver`` so the first admission is already
    inside the ring.  The default capacity is the module ceiling, so a
    regression that admits hundreds of sequential requests cannot evict the
    evidence it is judged on.  ``enable_serial_transcript`` deliberately keeps
    the produced facts local to this process and changes no serial scheduling.
    """
    backend.enable_serial_transcript(max_entries=max_entries)


def _write_retire_and_close_holding_the_condition(
    backend: NetworkBackend, *, edge_id: int | None = None
) -> None:
    """Write the response, retire it, and complete the close as one hold.

    Holding the pending condition across the whole transition keeps the wait
    from observing the zero before the transport is already terminal, so the
    decision has to come from the closed-transport evidence instead of an
    incidental open-transport return.  ``edge_id`` writes the identified
    response for that admission, which is what the per-admission ledger records;
    ``None`` keeps the historical two-byte response.
    """
    with backend._edge_pending_condition:
        backend._send_edge_response(_InboundEdge(peer_bit=0, response_bit=1, edge_id=edge_id))
        backend._decrement_edge_pending()
        backend._mark_closed_uncoordinated()


def _record_closed_retirement_evidence(monkeypatch, decisions: list[tuple[bool, object]]) -> None:
    """Wrap the closed-transport evidence predicate and record every decision."""
    module = sys.modules[_wait_for_edge_requests_retired.__module__]
    real_evidence = module._closed_transport_retirement_is_evidenced

    def recording(backend: NetworkBackend, *, pending_at_close, witness_at_entry):
        decisions.append((backend._closed, pending_at_close))
        return real_evidence(
            backend, pending_at_close=pending_at_close, witness_at_entry=witness_at_entry
        )

    monkeypatch.setattr(module, "_closed_transport_retirement_is_evidenced", recording)


def _gated_closed_discard_transition(
    backend: NetworkBackend, monkeypatch, threads: list[threading.Thread]
) -> tuple[object, list[BaseException | None]]:
    """Build a transition that discards the owner queue before a gated close snapshots.

    ``_mark_closed_common`` publishes ``_closed`` before it wakes the reader and
    before it records ``pending_edge_requests``.  The gate installed here holds
    the closer inside that window, so the transition can run
    ``service_pending_edges`` - which takes the real already-terminal discard
    branch and answers nothing - before the close samples the snapshot the wait
    will read.  The reader is deliberately left asleep until after the discard:
    it takes the owner-dispatch lock before it waits for the close lock, so
    letting it wake first would block the discard behind the close instead of
    ordering the discard in front of the snapshot.
    """
    close_published = threading.Event()
    release_close = threading.Event()
    real_event_set = backend._closed_event.set
    closer_outcomes: list[BaseException | None] = []

    def gated_publish() -> None:
        close_published.set()
        assert release_close.wait(timeout=_BOUNDED_WAIT), "the gated close was never released"
        real_event_set()

    monkeypatch.setattr(backend._closed_event, "set", gated_publish)

    def transition() -> None:
        def close() -> None:
            try:
                backend._mark_closed_uncoordinated()
            except BaseException as exc:  # noqa: BLE001 - reported to the caller
                closer_outcomes.append(exc)
            else:
                closer_outcomes.append(None)

        closer = threading.Thread(target=close, name="retirement-wait-closer", daemon=True)
        threads.append(closer)
        closer.start()
        try:
            assert close_published.wait(timeout=_BOUNDED_WAIT), (
                "the close never published terminal state"
            )
            assert backend._closed and not backend._closed_event.is_set()
            assert backend.service_pending_edges(max_edges=1) == 0
            assert backend._edge_pending == 0
        finally:
            # Always release the gated closer, even when the discard ordering
            # assertion fails, so a reported failure cannot hang teardown.
            real_event_set()
            release_close.set()
        closer.join(timeout=_BOUNDED_WAIT)
        assert not closer.is_alive(), "the gated close did not complete"

    return transition, closer_outcomes


def _run_transition_while_wait_is_outstanding(
    backend: NetworkBackend,
    transition,
    *,
    wait_timeout: float = 5.0,
    resume_when: str | None = None,
    threads: list[threading.Thread] | None = None,
) -> tuple[BaseException | None, float]:
    """Run ``transition`` after the retirement wait is provably inside its wait.

    Returns the waiter's outcome and how long it took after the transition
    started.  The transition is released only once the real condition-wait path
    reports that this waiter has parked, so the ordering never depends on a
    sleep and the waiter cannot mistake the transition for its entry check.

    ``resume_when`` names a stripped source line inside
    :func:`_wait_for_edge_requests_retired` that the waiter executes exactly
    once, after it samples its entry ledger and after its already-closed check,
    but before it acquires the pending condition.  When it is given, the waiter
    is held there while the transition runs and is resumed once the transition
    has completed.  Holding the waiter at that point decides the
    close-versus-observation ordering deterministically while the waiter owns no
    transport lock at all, which is what keeps a regression from depending on a
    lock race.  Spawned threads are appended to ``threads`` so a caller can hand
    them to :func:`_teardown_transport` even when an assertion aborts this
    helper.
    """
    outcomes: list[BaseException | None] = []
    parked = threading.Event()
    paused = threading.Event()
    resume = threading.Event()
    waiter_holder: list[threading.Thread] = []
    condition = backend._edge_pending_condition
    original_wait = condition.wait

    def observed_wait(timeout: float | None = None):
        # Report only this waiter's own wait call; other transport waiters
        # share the same condition and must not start the transition early.
        if waiter_holder and threading.current_thread() is waiter_holder[0]:
            parked.set()
        return original_wait(timeout)

    def trace(frame, event, arg):
        if event == "line" and frame.f_code is _wait_for_edge_requests_retired.__code__:
            line = linecache.getline(frame.f_code.co_filename, frame.f_lineno).strip()
            if line == resume_when:
                paused.set()
                assert resume.wait(timeout=_BOUNDED_WAIT), (
                    "the paused retirement wait was never resumed"
                )
        return trace

    actor_outcomes: list[BaseException] = []

    def actor_run() -> None:
        try:
            transition()
        except BaseException as exc:  # noqa: BLE001 - surfaced to the caller
            actor_outcomes.append(exc)

    def waiter() -> None:
        sys.settrace(trace if resume_when is not None else None)
        try:
            _wait_for_edge_requests_retired(backend, timeout=wait_timeout)
        except BaseException as exc:  # noqa: BLE001 - returned for assertion
            outcomes.append(exc)
        else:
            outcomes.append(None)
        finally:
            sys.settrace(None)

    condition.wait = observed_wait  # type: ignore[method-assign]
    try:
        waiter_thread = threading.Thread(target=waiter, name="retirement-wait", daemon=True)
        waiter_holder.append(waiter_thread)
        if threads is not None:
            threads.append(waiter_thread)
        waiter_thread.start()
        if resume_when is None:
            assert parked.wait(timeout=_BOUNDED_WAIT), (
                "retirement wait never parked on the condition"
            )
        else:
            assert paused.wait(timeout=_BOUNDED_WAIT), (
                f"retirement wait never reached its pause line {resume_when!r}"
            )
        actor = threading.Thread(target=actor_run, name="retirement-wait-actor", daemon=True)
        if threads is not None:
            threads.append(actor)
        actor.start()
        actor.join(timeout=_BOUNDED_WAIT)
        assert not actor.is_alive(), "the transition did not finish"
        started = time.monotonic()
        if resume_when is not None:
            resume.set()
        waiter_thread.join(timeout=_BOUNDED_WAIT)
        elapsed = time.monotonic() - started
    finally:
        condition.wait = original_wait
        resume.set()
    assert not waiter_thread.is_alive(), "retirement wait did not observe the transition"
    assert not actor.is_alive(), "the transition did not complete"
    if actor_outcomes:
        raise actor_outcomes[0]
    assert len(outcomes) == 1, outcomes
    return outcomes[0], elapsed


def _start_master_edge(
    backend: NetworkBackend, threads: list[threading.Thread]
) -> list[BaseException | None]:
    """Run the real master exchange of ``on_edge`` on its own thread.

    ``on_edge`` sends this endpoint's own ``EDGE_REQ`` and then blocks for its
    ``EDGE_RESP``, so the reciprocal branch the reader takes for an incoming
    request needs a caller that is genuinely in flight.  The returned list
    receives the outcome, which lets a regression assert that a sabotaged write
    surfaced as an error instead of being swallowed on a detached thread.
    """
    outcomes: list[BaseException | None] = []

    def run() -> None:
        try:
            backend.on_edge(1, 1)
        except BaseException as exc:  # noqa: BLE001 - returned for assertion
            outcomes.append(exc)
        else:
            outcomes.append(None)

    thread = threading.Thread(target=run, name="reciprocal-master-edge", daemon=True)
    threads.append(thread)
    thread.start()
    return outcomes


def _read_master_edge_request(peer: NetworkBackend) -> bytes:
    """Consume the master's own outbound ``EDGE_REQ`` from the peer's socket.

    Edge ids are unnegotiated in these regressions (no ``HELLO`` exchange), so
    ``on_edge`` sends the historical two-byte request and the peer answers it
    with the two-byte response.  Reading it here is what puts the reader's
    reciprocal branch next in the peer's own byte order.  The consumed frame is
    returned so a caller can account for every byte the peer really saw.
    """
    frame = _FRAME.pack(_OP_EDGE_REQ, 1)
    received = b""
    while len(received) < len(frame):
        part = peer._sock.recv(len(frame) - len(received))
        assert part, (received.hex(), "the master never sent its outbound edge request")
        received += part
    assert received == frame, received
    return received


def _transcript_records(backend: NetworkBackend) -> list[dict[str, object]]:
    """Copy the serial transcript's records for shape assertions."""
    return list(backend.snapshot_stats()["serial_transcript"]["records"])
