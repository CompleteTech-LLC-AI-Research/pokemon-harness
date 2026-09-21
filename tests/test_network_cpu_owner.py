"""Asset-free CPU/HALT coverage for the network owner-dispatch path.

These tests use a tiny ROM program authored below rather than a Pokémon ROM.
The program arms the native serial port, enters ``HALT`` with serial interrupts
enabled, and records progress from the serial interrupt vector.  The PyBoy
endpoint is attached through :class:`PyBoyLinkSession`; all emulator state
observations happen on the thread that owns ``provider.step``.
"""

from __future__ import annotations

import queue
import threading
import time

import pytest
from pyboy.core.serial import CYCLES_PER_BYTE_DMG, Serial

from pokered_harness.link.network_backend import NetworkBackend
from pokered_harness.link.pyboy_link_session import PyBoyLinkSession

# Reuse the repository's original 32 KiB, asset-free PyBoy fixture.  The
# program and interrupt vector are replaced in cartridge memory below.
from tests.test_serial_backend_boundary import emulator as _emulator_fixture  # noqa: F401

pytestmark = [pytest.mark.unit, pytest.mark.timing_sensitive]


_PROGRAM_START = 0x0150
_SERIAL_VECTOR = 0x0058
_HALT_PC = 0x0167
_HALT_MARKER = 0xFFAB
_IRQ_MARKER = 0xFFAC


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


def _retirement_diagnostics(backend: NetworkBackend) -> dict[str, object]:
    """Collect failure diagnostics without acquiring any transport lock.

    A terminal transition takes ``_close_lock`` and then
    ``_edge_pending_condition``, and ``debug_snapshot`` wants them in the same
    order, so a failure path that already holds either lock can invert the order
    against a concurrent close.  These reads are plain attribute reads on
    counters that are only ever assigned, which is enough for a diagnostic.
    """
    pre_close = backend._pre_close_snapshot
    pending_at_close = None
    reader_error = None
    if isinstance(pre_close, dict):
        pending_at_close = pre_close.get("pending_edge_requests")
        reader_error = pre_close.get("reader_error")
    return {
        "pending_edge_requests": backend._edge_pending,
        "closed": backend._closed,
        "pending_at_close": pending_at_close,
        "reader_error": reader_error,
    }


def _wait_for_edge_requests_retired(backend: NetworkBackend, *, timeout: float = 10.0) -> None:
    """Wait until every admitted edge response has reached the wire.

    ``pending_edge_requests`` counts an ``EDGE_REQ`` "from enqueue until their
    response has been written", and in owner-dispatch mode that write happens
    on the response worker, not on the owner thread.  The counter is therefore
    not zero the instant an owner frame returns, so sampling it immediately
    measures thread scheduling rather than backend state.  Wait for the
    documented retirement, notified by ``_decrement_edge_pending``, and let the
    caller assert the invariant.

    A terminal transport transition also drives the counter to zero while
    abandoning admitted work (``_mark_closed_common``), so zero alone does not
    prove a response reached the wire.  This wait therefore refuses to accept a
    close as retirement: it rejects a transport that is already closed when the
    wait begins, and one that closes while the wait is outstanding if the
    terminal transition recorded unretired work or a reader failure.
    """
    if backend._closed:
        raise AssertionError(
            "network backend was already closed before the admitted edge response "
            f"wait began; diagnostics={_retirement_diagnostics(backend)}"
        )
    deadline = time.monotonic() + timeout
    timed_out = False
    with backend._edge_pending_condition:
        while backend._edge_pending != 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            backend._edge_pending_condition.wait(timeout=remaining)
    # Both failure paths below run with no transport lock held and use only
    # lock-free diagnostics, so a concurrent close can always finish and the
    # failure stays bounded instead of deadlocking on the inverted lock order.
    if timed_out:
        raise AssertionError(
            "admitted edge response was not written within "
            f"{timeout:g}s; diagnostics={_retirement_diagnostics(backend)}"
        )
    if backend._closed:
        diagnostics = _retirement_diagnostics(backend)
        if diagnostics["pending_at_close"] or diagnostics["reader_error"] is not None:
            raise AssertionError(
                "network backend closed while the admitted edge response was "
                "outstanding, so zero pending work is closure and not retirement; "
                f"diagnostics={diagnostics}"
            )


def test_retirement_wait_rejects_transport_closed_before_the_wait() -> None:
    """A transport closed up front cannot certify retired edge work."""
    _peer, backend = NetworkBackend.pair()
    try:
        backend._mark_closed_uncoordinated()
        with pytest.raises(AssertionError, match="already closed"):
            _wait_for_edge_requests_retired(backend, timeout=0.5)
    finally:
        backend._mark_closed_uncoordinated()


def test_retirement_wait_rejects_close_that_abandons_admitted_work() -> None:
    """A close during the wait drops admitted work; that is not retirement."""
    _peer, backend = NetworkBackend.pair()
    try:
        with backend._edge_pending_condition:
            backend._edge_pending = 1
        outcomes: list[BaseException | None] = []

        def waiter() -> None:
            try:
                _wait_for_edge_requests_retired(backend, timeout=5.0)
            except BaseException as exc:  # noqa: BLE001 - asserted below
                outcomes.append(exc)
            else:
                outcomes.append(None)

        waiter_thread = threading.Thread(target=waiter, name="retirement-wait", daemon=True)
        waiter_thread.start()
        # The wait releases the pending condition while it blocks, so a real
        # terminal transition can complete inside the wait window.
        time.sleep(0.05)
        closer = threading.Thread(
            target=backend._mark_closed_uncoordinated,
            args=(RuntimeError("transport failed"),),
            name="retirement-wait-error-close",
            daemon=True,
        )
        closer.start()
        waiter_thread.join(timeout=10.0)
        closer.join(timeout=5.0)
        assert not waiter_thread.is_alive()
        assert not closer.is_alive()
        assert len(outcomes) == 1 and isinstance(outcomes[0], AssertionError), outcomes
        assert "closure and not retirement" in str(outcomes[0])
        assert backend._closed
    finally:
        backend._mark_closed_uncoordinated()


def test_retirement_wait_failure_path_stays_bounded_while_close_lock_is_held() -> None:
    """Timeout diagnostics must not wait on the close lock."""
    _peer, backend = NetworkBackend.pair()
    try:
        with backend._edge_pending_condition:
            backend._edge_pending = 1
        assert backend._close_lock.acquire(timeout=1.0)
        try:
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
            backend._close_lock.release()
    finally:
        backend._mark_closed_uncoordinated()


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
