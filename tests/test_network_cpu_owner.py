"""Asset-free CPU/HALT coverage for the network owner-dispatch path.

These tests use a tiny ROM program authored below rather than a Pokémon ROM.
The program arms the native serial port, enters ``HALT`` with serial interrupts
enabled, and records progress from the serial interrupt vector.  The PyBoy
endpoint is attached through :class:`PyBoyLinkSession`; all emulator state
observations happen on the thread that owns ``provider.step``.
"""

from __future__ import annotations

import linecache
import queue
import socket
import sys
import threading
import time

import pytest
from pyboy.core.serial import CYCLES_PER_BYTE_DMG, Serial

from pokered_harness.link.network_backend import (
    _EDGE_ID_FRAME,
    _FRAME,
    _OP_EDGE_REQ_ID,
    _OP_EDGE_RESP,
    _OP_EDGE_RESP_ID,
    NetworkBackend,
    _InboundEdge,
)
from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
from pokered_harness.link.serial_coordinator import SerialOperationGate

# Reuse the repository's original 32 KiB, asset-free PyBoy fixture.  The
# program and interrupt vector are replaced in cartridge memory below.
from tests.test_serial_backend_boundary import emulator as _emulator_fixture  # noqa: F401

pytestmark = [pytest.mark.unit, pytest.mark.timing_sensitive]


_PROGRAM_START = 0x0150
_SERIAL_VECTOR = 0x0058
_HALT_PC = 0x0167
_HALT_MARKER = 0xFFAB
_IRQ_MARKER = 0xFFAC

# Generous, still-bounded ceiling for the coordination waits and joins these
# regressions use.  It only elapses on failure; on the passing path every wait
# is satisfied almost immediately, so a loaded host cannot turn a genuine
# rejection into a spurious timeout-driven failure.
_BOUNDED_WAIT = 30.0


class _ObservedEdgeQueue(queue.Queue):
    """Queue probe used only to signal the owner when the reader enqueues."""

    def __init__(self, ready: threading.Event, *, maxsize: int) -> None:
        super().__init__(maxsize=maxsize)
        self._ready = ready

    def put_nowait(self, item) -> None:
        super().put_nowait(item)
        self._ready.set()


def _program(*, internal_clock: bool) -> list[int]:
    """Return a tiny serial/interrupt program for the synthetic cartridge."""
    incoming = 0xA5 if internal_clock else 0x3C
    clock_control = 0x81 if internal_clock else 0x80
    return [
        0x31,
        0xFE,
        0xFF,  # LD SP,$FFFE
        0x3E,
        0x08,
        0xE0,
        0xFF,  # IE = serial interrupt
        0xAF,
        0xE0,
        0x0F,  # IF = 0
        0x3E,
        incoming,
        0xE0,
        0x01,  # SB = payload
        0x3E,
        clock_control,
        0xE0,
        0x02,  # SC = transfer enable + selected clock
        0x3E,
        0xA1,
        0xE0,
        0xAB,  # marker: armed and about to halt
        0xFB,  # EI
        0x76,  # HALT until the serial IRQ
        0x3E,
        0xA2,
        0xE0,
        0xAB,  # marker: HALT resumed after the IRQ
        0x18,
        0xFE,  # JR $016C
    ]


def _install_program(pyboy, *, internal_clock: bool) -> None:
    """Install the authored program/vector without any ROM-derived input."""
    program = _program(internal_clock=internal_clock)
    pyboy.memory[0, _PROGRAM_START : _PROGRAM_START + len(program)] = program
    # LD A,$B2; LDH [$FFAC],A; RETI; NOP.  The marker proves that the
    # hardware serial interrupt woke the CPU from HALT before the main ROM
    # path wrote its post-HALT marker.
    pyboy.memory[0, _SERIAL_VECTOR : _SERIAL_VECTOR + 6] = [
        0x3E,
        0xB2,
        0xE0,
        0xAC,
        0xD9,
        0x00,
    ]
    pyboy.memory[_HALT_MARKER] = 0
    pyboy.memory[_IRQ_MARKER] = 0
    pyboy.memory[0xFF0F] = 0
    pyboy.memory[0xFFFF] = 0
    pyboy.register_file.SP = 0xFFFE
    pyboy.register_file.PC = _PROGRAM_START


def _snapshot(pyboy) -> dict[str, int | bool]:
    """Read one complete owner-thread-only emulator observation."""
    serial = pyboy.mb.serial
    pc = int(pyboy.register_file.PC)
    return {
        # ``CPU.halted`` is a source-runtime-only Python field; the bundled
        # Cython CPU keeps it cdef-private.  The authored ROM leaves PC at the
        # HALT opcode, so this portable observation has the same meaning on
        # both runtimes without inspecting private native state.
        "pc": pc,
        "halted": pc == _HALT_PC,
        "sb": int(serial.SB),
        "sc": int(serial.SC),
        "transfer_enabled": bool(serial.transfer_enabled),
        "internal_clock": bool(serial.internal_clock),
        "if": int(pyboy.memory[0xFF0F]),
        "halt_marker": int(pyboy.memory[_HALT_MARKER]),
        "irq_marker": int(pyboy.memory[_IRQ_MARKER]),
        "frame": int(pyboy.frame_count),
    }


def _wait_for_edge_request(
    backend: NetworkBackend, ready: threading.Event, *, timeout: float = 3.0
) -> None:
    """Wait until the owner can observe an admitted peer edge.

    The reader increments pending work immediately before publishing the queue
    item.  Checking both values avoids losing a one-shot queue probe during a
    scheduler handoff, while still requiring a real admitted ``EDGE_REQ``.
    """
    deadline = time.monotonic() + timeout
    while True:
        if backend.debug_snapshot()["pending_edge_requests"] > 0 and not backend._edge_queue.empty():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                "network reader did not queue EDGE_REQ; "
                f"snapshot={backend.debug_snapshot()}"
            )
        if not ready.wait(timeout=remaining):
            raise AssertionError(
                "network reader did not queue EDGE_REQ; "
                f"snapshot={backend.debug_snapshot()}"
            )
        ready.clear()
        if backend._closed:
            raise AssertionError("network backend closed before EDGE_REQ")


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


def _retirement_diagnostics(backend: NetworkBackend) -> dict[str, object]:
    """Collect failure diagnostics without acquiring any transport lock.

    A terminal transition takes ``_close_lock`` and then
    ``_edge_pending_condition``, and ``debug_snapshot`` wants them in the same
    order, so a failure path that already holds either lock can invert the order
    against a concurrent close.  These reads are plain attribute reads on
    counters that are only ever assigned, which is enough for a diagnostic.
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
    }


def _closed_transport_retirement_is_evidenced(
    backend: NetworkBackend,
    *,
    pending_at_close: object,
    ledger_at_entry: tuple[int, frozenset[int], frozenset[int]],
) -> bool:
    """Decide whether a closed transport really retired the admitted work.

    Called only after ``_edge_pending`` was observed at zero under
    ``_edge_pending_condition`` while ``_closed`` was already published.
    ``_mark_closed_common`` publishes ``_closed`` before it samples its
    snapshot and zeroes the admitted-work count under that same condition, so
    a terminal transport supplies a zero of its own.  Neither "pending zero"
    nor an aggregate response count separates retirement from closure:

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

    A per-admission answer is required instead.  The backend records a completed
    response against the admitted request's own id, and a discard that wrote
    nothing leaves that id still claimed with no record:

    * ``pending_edge_requests`` in the close snapshot must be zero, so the close
      itself discarded nothing; a release after terminal publication must not be
      able to certify this wait.
    * every id the reader had claimed but not answered when the wait began must
      have a recorded response now, and the same holds for ids claimed while the
      wait was outstanding.
    * no id may still be claimed with no recorded response at decision time.
    * at least one identified request must have been admitted.  An unidentified
      ``OP_EDGE_REQ`` leaves no per-admission record at all, so a closed
      transport that admitted only unidentified work is refused rather than
      guessed at.

    Kept as its own function so a regression can install a predicate that never
    consents and show this acceptance path is load-bearing rather than
    incidental.
    """
    if pending_at_close != 0:
        return False
    highest_at_entry, _retired_at_entry, unretired_at_entry = ledger_at_entry
    highest_now, retired_now, unretired_now = _inbound_retirement_ledger(backend)
    if unretired_now:
        return False
    if unretired_at_entry - retired_now:
        return False
    if any(edge_id not in retired_now for edge_id in range(highest_at_entry + 1, highest_now + 1)):
        return False
    return highest_now > 0


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
      found nothing outstanding and that the backend's per-admission ledger
      records a completed response for every identified request it admitted,
      including any it had already claimed when this wait began.

    Every other exit is reported as a closure with lock-free diagnostics, and a
    transport that is already closed when the wait begins is refused outright.
    The ledger is sampled before that refusal, so an admission already claimed
    when this wait is entered is still part of the entry snapshot the
    closed-transport decision compares against.

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
    ledger_at_entry = _inbound_retirement_ledger(backend)
    if backend._closed:
        raise AssertionError(
            "network backend was already closed before the admitted edge response "
            f"wait began; diagnostics={_retirement_diagnostics(backend)}"
        )
    deadline = time.monotonic() + timeout
    timed_out = False
    retired_while_open = False
    with backend._edge_pending_condition:
        while True:
            if backend._edge_pending == 0:
                if not backend._closed:
                    retired_while_open = True
                else:
                    retired_while_open = _closed_transport_retirement_is_evidenced(
                        backend,
                        pending_at_close=_pending_edge_requests_at_close(backend),
                        ledger_at_entry=ledger_at_entry,
                    )
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
    raise AssertionError(
        "network backend closed while the admitted edge response was outstanding, "
        "so zero pending work is closure and not retirement; "
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


def test_retirement_wait_rejects_transport_closed_before_the_wait() -> None:
    """A transport closed up front cannot certify retired edge work."""
    _peer, backend = NetworkBackend.pair()
    try:
        backend._mark_closed_uncoordinated()
        with pytest.raises(AssertionError, match="already closed"):
            _wait_for_edge_requests_retired(backend, timeout=0.5)
    finally:
        backend._mark_closed_uncoordinated()


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
    deadline = time.monotonic() + _BOUNDED_WAIT
    with backend._edge_pending_condition:
        while backend._edge_queue.qsize() < queued:
            remaining = deadline - time.monotonic()
            assert remaining > 0, "the network reader never published an admitted EDGE_REQ"
            backend._edge_pending_condition.wait(timeout=remaining)
        assert backend._edge_pending == expected_pending, backend._edge_pending


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

    def recording(backend: NetworkBackend, *, pending_at_close, ledger_at_entry):
        decisions.append((backend._closed, pending_at_close))
        return real_evidence(
            backend, pending_at_close=pending_at_close, ledger_at_entry=ledger_at_entry
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


@pytest.mark.parametrize("clean_close", [True, False], ids=["clean-close", "error-close"])
def test_retirement_wait_rejects_close_that_abandons_admitted_work(clean_close: bool) -> None:
    """A close during the wait drops admitted work; that is not retirement.

    Both closes are rejected. A clean close publishes ``_closed`` before it
    samples the counters it records, and the response worker skips its write
    once the transport is terminal, so a clean close can present with pending
    zero and no reader error while no response ever reached the wire.
    """
    _peer, backend = NetworkBackend.pair()
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
        lambda backend, *, pending_at_close, ledger_at_entry: False,
    )
    peer, backend = NetworkBackend.pair()
    from tests.test_network_owner_execution import _Core

    threads: list[threading.Thread] = []
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


def test_byte_completed_at_frame_barrier_resumes_cpu_without_a_ninth_edge(
    _emulator_fixture,  # noqa: F811 - shared authored-ROM fixture
    monkeypatch,
):
    """The final external edge must leave room for its pending CPU interrupt."""
    pyboy = _emulator_fixture
    _install_program(pyboy, internal_clock=False)
    pyboy.tick(1, render=False, sound=False)
    assert _snapshot(pyboy)["halt_marker"] == 0xA1
    peer, backend = NetworkBackend.pair()
    edge_queued = threading.Event()
    backend._edge_queue = _ObservedEdgeQueue(edge_queued, maxsize=256)
    peer.start_receiver(local_core=None)
    provider = PyBoyLinkSession(network_backend=backend)
    sender_errors: list[BaseException] = []
    peer_serial = Serial(backend=peer)
    peer_serial.set_SB(0xA5)
    peer_serial.set_SC(0x81)

    def send_byte():
        try:
            peer_serial.tick(CYCLES_PER_BYTE_DMG)
            peer_serial.check_error()
        except BaseException as exc:  # noqa: BLE001 - collected after bounded join
            sender_errors.append(exc)

    sender = threading.Thread(target=send_byte, daemon=True)
    states = []

    def finish_frame(*, leader, progress_callback):
        assert leader is False
        assert _snapshot(pyboy)["halted"]
        sender.start()
        for _ in range(8):
            _wait_for_edge_request(backend, edge_queued)
            progress_callback()
            states.append(_snapshot(pyboy))
        sender.join(timeout=1.0)
        assert not sender.is_alive()
        assert sender_errors == []
        assert backend.debug_snapshot()["edge_req_received"] == 8
        # No ninth edge or outer Session.step is available to drive this IRQ.
        assert states[-1]["irq_marker"] == 0xB2
        assert states[-1]["halt_marker"] == 0xA2
        assert all(state["irq_marker"] == 0 for state in states[:-1])

    try:
        provider.attach(pyboy)
        provider._network_is_internal_clock = False
        provider._network_frame_barrier = True
        monkeypatch.setattr(backend, "begin_frame_turn", lambda **_kwargs: None)
        monkeypatch.setattr(backend, "finish_frame_turn", finish_frame)
        frame_before = pyboy.frame_count
        provider.step(1)
        assert pyboy.mb.serial.SB == 0xA5
        # One requested frame plus one bounded owner recovery frame.
        assert pyboy.frame_count - frame_before == 2
    finally:
        backend.stop(timeout_s=1.0)
        peer.stop(timeout_s=1.0)
        if sender.ident is not None:
            sender.join(timeout=1.0)
            assert not sender.is_alive()
        provider.detach_all()


def _run_cpu_case(pyboy, *, internal_clock: bool) -> dict[str, object]:
    """Run one real PyBoy endpoint against a synthetic native Serial peer."""
    _install_program(pyboy, internal_clock=internal_clock)

    left, right = NetworkBackend.pair()
    if internal_clock:
        # The authored PyBoy is the master.  The peer is an external-clock
        # native Serial driven by the compatibility receiver worker; it never
        # touches the PyBoy endpoint.
        peer_backend = right
        attached_backend = left
        peer = Serial(backend=peer_backend)
        peer.set_SB(0x3C)
        peer.set_SC(0x80)
        peer_backend.start_receiver(local_core=peer)
    else:
        # The authored PyBoy is the external-clock owner.  The peer is the
        # internal-clock native Serial, whose requests must be dispatched to
        # the PyBoyLinkSession owner thread.
        peer_backend = left
        attached_backend = right
        peer_backend.start_receiver(local_core=None)
        peer = Serial(backend=peer_backend)
        peer.set_SB(0xA5)
        peer.set_SC(0x81)

    provider = PyBoyLinkSession(network_backend=attached_backend)
    edge_queued = threading.Event()
    if not internal_clock:
        attached_backend._edge_queue = _ObservedEdgeQueue(
            edge_queued, maxsize=attached_backend._edge_queue.maxsize
        )
    provider.attach(pyboy)
    assert attached_backend._dispatch_to_owner is True
    assert pyboy.mb.serial.owner_dispatch_enabled is True

    owner_id: list[int] = []
    service_threads: list[int] = []
    states: list[dict[str, int | bool]] = []
    owner_errors: list[BaseException] = []
    owner_done = threading.Event()
    ready = threading.Event()
    start_transfer = threading.Event()

    original_service = attached_backend.service_pending_edges

    def observe_service(*args, **kwargs):
        service_threads.append(threading.get_ident())
        return original_service(*args, **kwargs)

    attached_backend.service_pending_edges = observe_service

    irq_threads: list[int] = []
    irq_if_values: list[int] = []
    if not internal_clock:
        original_irq = attached_backend._irq_callback
        assert callable(original_irq)

        def observe_irq() -> None:
            irq_threads.append(threading.get_ident())
            original_irq()
            # The callback runs after the native serial byte latch and before
            # the next owner CPU boundary, so IF must contain the serial bit.
            irq_if_values.append(int(pyboy.memory[0xFF0F]))

        attached_backend._irq_callback = observe_irq

    def owner() -> None:
        owner_id.append(threading.get_ident())
        try:
            if not internal_clock:
                for _ in range(256):
                    provider.step(1)
                    state = _snapshot(pyboy)
                    states.append(state)
                    if (
                        state["halted"]
                        and state["transfer_enabled"]
                        and not state["internal_clock"]
                        and state["halt_marker"] == 0xA1
                    ):
                        ready.set()
                        break
                else:
                    raise AssertionError("synthetic ROM did not reach HALT")

                # The test thread clocks the peer only after the owner has
                # observed the ROM's real HALT state.
                if not start_transfer.wait(timeout=3.0):
                    raise AssertionError("owner transfer handoff timed out")

            for _ in range(256):
                # The owner pump services each queued edge at the next normal
                # CPU boundary. After the eighth request, native PyBoy may
                # need one additional boundary to dispatch the serial IRQ and
                # leave HALT, and there is no ninth EDGE_REQ to wake a queue
                # waiter.
                if (
                    not internal_clock
                    and attached_backend.debug_snapshot()["edge_req_received"] < 8
                ):
                    _wait_for_edge_request(attached_backend, edge_queued)
                provider.step(1)
                state = _snapshot(pyboy)
                states.append(state)
                if state["halt_marker"] == 0xA2:
                    owner_done.set()
                    return
            raise AssertionError("synthetic serial IRQ did not resume HALT")
        except BaseException as exc:  # noqa: BLE001 - reported by coordinator
            owner_errors.append(exc)
        finally:
            owner_done.set()

    owner_thread = threading.Thread(target=owner, name="test-pyboy-owner")
    owner_thread.start()

    try:
        if not internal_clock:
            assert ready.wait(timeout=5.0), "synthetic ROM did not reach HALT"
            start_transfer.set()
            peer.tick(peer.last_cycles + CYCLES_PER_BYTE_DMG)
        assert owner_done.wait(timeout=8.0), "owner thread did not finish"
        owner_thread.join(timeout=2.0)
        assert not owner_thread.is_alive()
        assert owner_errors == []
        assert owner_id and service_threads
        assert set(service_threads) == {owner_id[0]}

        reader = attached_backend._reader
        edge_worker = attached_backend._edge_worker
        assert reader is not None and edge_worker is not None
        assert owner_id[0] not in {reader.ident, edge_worker.ident}

        final = states[-1]
        assert final["halt_marker"] == 0xA2
        assert final["irq_marker"] == 0xB2
        assert final["halted"] is False
        assert final["transfer_enabled"] is False
        assert final["frame"] > 0
        if not internal_clock:
            ready_state = next(
                state
                for state in states
                if state["halt_marker"] == 0xA1 and state["halted"]
            )
            assert ready_state["halted"] is True
            assert ready_state["transfer_enabled"] is True
            assert ready_state["internal_clock"] is False
            assert final["pc"] != ready_state["pc"]
            assert irq_threads == [owner_id[0]]
            assert irq_if_values and irq_if_values[0] & 0x08

        assert not peer.transfer_enabled
        expected_peer_byte = 0xA5 if internal_clock else 0x3C
        expected_local_byte = 0x3C if internal_clock else 0xA5
        assert peer.SB == expected_peer_byte
        assert final["sb"] == expected_local_byte

        snapshot = attached_backend.debug_snapshot()
        peer_snapshot = peer_backend.debug_snapshot()
        if internal_clock:
            deadline = time.monotonic() + 3.0
            while peer_snapshot["edge_resp_sent"] < 8 and time.monotonic() < deadline:
                time.sleep(0.005)
                peer_snapshot = peer_backend.debug_snapshot()
        assert snapshot["owner_edge_applied"] == (0 if internal_clock else 8)
        assert snapshot["edge_req_sent"] == (8 if internal_clock else 0)
        assert snapshot["edge_resp_received"] == (8 if internal_clock else 0)
        assert peer_snapshot["edge_req_received"] == (8 if internal_clock else 0)
        assert peer_snapshot["edge_resp_sent"] == (8 if internal_clock else 0)
        assert peer_snapshot["edge_req_sent"] == (0 if internal_clock else 8)
        assert peer_snapshot["edge_resp_received"] == (0 if internal_clock else 8)
    finally:
        # A failed owner assertion must still wake any admitted peer edge
        # before the session restores the native Serial backend.
        start_transfer.set()
        edge_queued.set()
        if not owner_done.is_set():
            attached_backend.stop(timeout_s=1.0)
            peer_backend.stop(timeout_s=1.0)
        if not owner_done.is_set():
            owner_done.wait(timeout=2.0)
        owner_thread.join(timeout=2.0)
        assert not owner_thread.is_alive(), "owner thread survived network teardown"
        try:
            provider.detach_all()
        finally:
            attached_backend.stop(timeout_s=1.0)
            peer_backend.stop(timeout_s=1.0)

    return {
        "owner_id": owner_id,
        "states": states,
        "irq_threads": irq_threads,
    }


def test_network_pyboy_external_clock_dispatch_wakes_halted_cpu(_emulator_fixture):  # noqa: F811
    """Peer-driven edges mutate native Serial only on the PyBoy owner."""
    _run_cpu_case(_emulator_fixture, internal_clock=False)


def test_network_pyboy_internal_clock_dispatch_completes_halted_cpu(_emulator_fixture):  # noqa: F811
    """The owner-thread PyBoy master completes a real HALT serial transfer."""
    _run_cpu_case(_emulator_fixture, internal_clock=True)


class _PumpBackend:
    """Small queue-backed double for the private native owner pump."""

    def __init__(
        self,
        edge_queue: queue.Queue,
        *,
        service_error: BaseException | None = None,
        closed: bool = False,
    ):
        self._edge_queue = edge_queue
        self._service_error = service_error
        self._closed = closed
        self.service_calls = 0
        self.serviced: list[object] = []
        self.closed_errors: list[BaseException] = []

    def service_pending_edges(self) -> int:
        self.service_calls += 1
        if self._service_error is not None:
            raise self._service_error
        try:
            self.serviced.append(self._edge_queue.get_nowait())
        except queue.Empty:
            return 0
        return 1

    def _mark_closed(self, error: BaseException) -> None:
        self._closed = True
        self.closed_errors.append(error)


class _ArrivesAfterEmpty(queue.Queue):
    """Inject one request after the optimistic empty observation."""

    def __init__(self, item: object):
        super().__init__()
        self._item = item
        self._injected = False

    def empty(self) -> bool:
        was_empty = super().empty()
        if was_empty and not self._injected:
            self._injected = True
            self.put_nowait(self._item)
        return was_empty


class _NoQueuePumpBackend:
    """Legacy-like backend that does not expose an inbound queue attribute."""

    def __init__(self):
        self.service_calls = 0
        self.closed_errors: list[BaseException] = []

    def service_pending_edges(self) -> int:
        self.service_calls += 1
        return 0

    def _mark_closed(self, error: BaseException) -> None:
        self.closed_errors.append(error)


class _BrokenEmptyQueue:
    """Queue probe double used to exercise the pump's fail-closed boundary."""

    def __init__(self, error: BaseException):
        self._error = error

    def empty(self) -> bool:
        raise self._error


@pytest.mark.parametrize("closed", [False, True])
def test_network_owner_pump_skips_empty_queue_and_closed_idle_backend(closed: bool) -> None:
    """Idle native boundaries do not enter the backend service path."""
    backend = _PumpBackend(queue.Queue(), closed=closed)
    pump = PyBoyLinkSession._make_network_owner_pump(backend)

    pump()

    assert backend.service_calls == 0
    assert backend.serviced == []
    assert backend.closed_errors == []
    assert backend._closed is closed


def test_network_owner_pump_late_queue_arrival_is_deferred_to_next_boundary() -> None:
    """The empty hint cannot consume or lose a request arriving concurrently."""
    backend = _PumpBackend(_ArrivesAfterEmpty("late-edge"))
    pump = PyBoyLinkSession._make_network_owner_pump(backend)

    # The queue inserts the request after returning the optimistic ``True``
    # result.  The first callback therefore skips service, while the next
    # callback must still observe and apply the retained request.
    pump()
    assert backend.service_calls == 0
    assert backend.serviced == []

    pump()
    assert backend.service_calls == 1
    assert backend.serviced == ["late-edge"]
    assert backend.closed_errors == []


def test_network_owner_pump_fail_closed_on_nonempty_service_error() -> None:
    """A queued request still uses the existing noexcept fail-closed path."""
    error = RuntimeError("owner service failed")
    edge_queue: queue.Queue[object] = queue.Queue()
    edge_queue.put_nowait("edge")
    backend = _PumpBackend(edge_queue, service_error=error)
    pump = PyBoyLinkSession._make_network_owner_pump(backend)

    pump()

    assert backend.service_calls == 1
    assert backend.closed_errors == [error]


def test_network_owner_pump_without_queue_uses_legacy_service_fallback() -> None:
    """Backends without the private queue attribute retain old behavior."""
    backend = _NoQueuePumpBackend()
    pump = PyBoyLinkSession._make_network_owner_pump(backend)

    pump()

    assert backend.service_calls == 1
    assert backend.closed_errors == []


def test_network_owner_pump_empty_probe_error_fails_closed() -> None:
    """A queue-probe failure cannot escape a native noexcept callback."""
    error = RuntimeError("queue probe failed")
    backend = _PumpBackend(_BrokenEmptyQueue(error))
    pump = PyBoyLinkSession._make_network_owner_pump(backend)

    pump()

    assert backend.service_calls == 0
    assert backend.closed_errors == [error]
    assert backend._closed is True
