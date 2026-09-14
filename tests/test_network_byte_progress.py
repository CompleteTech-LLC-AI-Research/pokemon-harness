"""A ROM mailbox must survive successive bytes from a TCP clock peer."""

from __future__ import annotations

import queue
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from pyboy.core.serial import CYCLES_PER_BYTE_DMG, Serial

from pokered_harness.link.network_backend import (
    _FRAME,
    _OP_EDGE_REQ,
    _OP_EDGE_RESP,
    NetworkBackend,
    NetworkBackendError,
    _InboundEdge,
)
from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
from pokered_harness.link.serial_coordinator import SerialOperationGate
from tests.test_network_cpu_owner import _ObservedEdgeQueue, _wait_for_edge_request
from tests.test_serial_backend_boundary import emulator as _emulator_fixture  # noqa: F401

pytestmark = [pytest.mark.unit, pytest.mark.timing_sensitive]

_MAILBOX = 0xC100
_READY = 0xC101
_COUNT = 0xC102
_OUTPUT = 0xC110


def _receiver_program():
    """Author a receiver which rearms in its IRQ before consuming its mailbox."""
    code = [
        0xF3,  # DI
        0x31, 0xFE, 0xFF,  # LD SP,$FFFE
        0xAF, 0xE0, 0x0F,  # IF = 0
        0x3E, 0x08, 0xE0, 0xFF,  # IE = serial only
        # A running timer provides normal, short motherboard scheduling
        # boundaries. Timer interrupts remain masked; no CPU stepping hook
        # or execution-governor override is installed by this regression.
        0x3E, 0xFF, 0xE0, 0x06, 0xE0, 0x05,  # TMA = TIMA = $FF
        0x3E, 0x05, 0xE0, 0x07,  # TAC: enabled, 16 T-cycle period
        0x21, 0x10, 0xC1,  # output pointer
        0x3E, 0x3C, 0xE0, 0x01,  # outgoing byte
        0x3E, 0x80, 0xE0, 0x02,  # external clock, transfer armed
        0xFB,  # EI
    ]
    labels = {}
    branches = []

    def jump(opcode, target):
        code.extend((opcode, 0))
        branches.append((len(code) - 1, target))

    labels["wait"] = len(code)
    code.extend((0xFA, 0x01, 0xC1, 0xB7))  # read ready flag; AND A
    jump(0x20, "ready")  # JR NZ
    code.append(0x76)  # HALT until serial IRQ
    jump(0x18, "wait")
    labels["ready"] = len(code)
    code.extend((0xAF, 0xEA, 0x01, 0xC1))  # clear ready before reading mailbox
    code.extend((0x06, 0x20))  # bounded ROM work between notification and read
    labels["delay"] = len(code)
    code.append(0x05)  # DEC B
    jump(0x20, "delay")
    code.extend((
        0xFA, 0x00, 0xC1,  # LD A,[mailbox]
        0x22,  # LD [HL+],A
        0xFA, 0x02, 0xC1, 0x3C, 0xEA, 0x02, 0xC1,  # increment count
        0xFE, 0x02,  # CP 2
    ))
    jump(0x20, "wait")
    labels["done"] = len(code)
    code.append(0x76)
    jump(0x18, "done")
    for operand, target in branches:
        displacement = labels[target] - operand - 1
        assert -128 <= displacement <= 127
        code[operand] = displacement & 0xFF
    return code


class _ResponseHandoffQueue(queue.Queue):
    """Give the clock peer its next request before this owner resumes CPU work."""

    def __init__(self, backend, edge_ready, *, expected_requests=16):
        super().__init__(maxsize=256)
        self.backend = backend
        self.edge_ready = edge_ready
        self.expected_requests = expected_requests
        self.responses = 0

    def put_nowait(self, request):
        super().put_nowait(request)
        if request is None:
            return
        self.responses += 1
        if self.responses < self.expected_requests:
            # This controls host scheduling only. The real response worker,
            # socket reader, native serial edges, and ROM remain responsible
            # for all transport and emulator state changes.
            _wait_for_edge_request(self.backend, self.edge_ready)


def test_successive_tcp_bytes_preserve_the_rom_mailbox(
    _emulator_fixture,  # noqa: F811 - shared original-ROM fixture
):
    pyboy = _emulator_fixture
    program = _receiver_program()
    pyboy.memory[0, 0x150 : 0x150 + len(program)] = program
    handler = [
        0xF5,  # preserve A/F
        0xF0, 0x01, 0xEA, 0x00, 0xC1,  # SB -> mailbox
        0x3E, 0x3C, 0xE0, 0x01,  # next outgoing byte
        0x3E, 0x80, 0xE0, 0x02,  # rearm before notification/consumption
        0x3E, 0x01, 0xEA, 0x01, 0xC1,  # ready = 1
        0xF1, 0xD9,  # restore A/F; RETI
    ]
    pyboy.memory[0, 0x58 : 0x58 + len(handler)] = handler
    pyboy.memory[_MAILBOX : _OUTPUT + 2] = [0] * (_OUTPUT + 2 - _MAILBOX)
    pyboy.tick(1, render=False, sound=False)
    assert pyboy.mb.serial.transfer_enabled
    assert not pyboy.mb.serial.internal_clock

    peer, backend = NetworkBackend.pair()
    edge_ready = threading.Event()
    backend._edge_queue = _ObservedEdgeQueue(edge_ready, maxsize=256)
    handoffs = _ResponseHandoffQueue(backend, edge_ready)
    backend._completed_edge_queue = handoffs
    peer.start_receiver(local_core=None)
    provider = PyBoyLinkSession(network_backend=backend)
    clock_core = Serial(backend=peer)
    errors = []
    responses = []

    def send():
        try:
            for number, value in enumerate((0x2D, 0x16), 1):
                clock_core.set_SB(value)
                clock_core.set_SC(0x81)
                clock_core.tick(number * CYCLES_PER_BYTE_DMG)
                clock_core.check_error()
                responses.append(int(clock_core.SB))
        except BaseException as exc:  # noqa: BLE001 - reported after bounded join
            errors.append(exc)

    sender = threading.Thread(target=send, daemon=True)
    try:
        provider.attach(pyboy)
        sender.start()
        _wait_for_edge_request(backend, edge_ready)
        for _ in range(8):
            provider.step(1)
            if pyboy.memory[_COUNT] == 2:
                break
        sender.join(timeout=1.0)
        assert not sender.is_alive()
        assert errors == []
        assert responses == [0x3C, 0x3C]
        assert handoffs.responses == 16
        assert backend.debug_snapshot()["edge_req_received"] == 16
        assert backend.debug_snapshot()["pending_edge_requests"] == 0
        assert list(pyboy.memory[_OUTPUT : _OUTPUT + 2]) == [0x2D, 0x16]
        assert pyboy.memory[_COUNT] == 2
    finally:
        backend.stop(timeout_s=1.0)
        peer.stop(timeout_s=1.0)
        if sender.ident is not None:
            sender.join(timeout=1.0)
            assert not sender.is_alive()
        provider.detach_all()


@contextmanager
def _held_byte(pyboy=None):
    """Complete one genuine TCP byte without advancing the receiver's CPU."""
    peer, backend = NetworkBackend.pair()
    edge_ready = threading.Event()
    backend._edge_queue = _ObservedEdgeQueue(edge_ready, maxsize=256)
    peer.start_receiver(local_core=None)
    core = Serial() if pyboy is None else pyboy.mb.serial
    core.set_SB(0x3C)
    core.set_SC(0x80)
    provider = None
    if pyboy is None:
        backend.start_receiver(
            core,
            serial_gate=SerialOperationGate(),
            dispatch_to_owner=True,
            defer_byte_responses=True,
        )
    else:
        provider = PyBoyLinkSession(network_backend=backend)
        provider.attach(pyboy)
    clock_core = Serial(backend=peer)
    errors, responses = [], []

    def send():
        try:
            clock_core.set_SB(0x2D)
            clock_core.set_SC(0x81)
            clock_core.tick(CYCLES_PER_BYTE_DMG)
            clock_core.check_error()
            responses.append(int(clock_core.SB))
        except BaseException as exc:  # noqa: BLE001 - asserted on the owner
            errors.append(exc)

    sender = threading.Thread(target=send, daemon=True)
    try:
        sender.start()
        for _ in range(8):
            _wait_for_edge_request(backend, edge_ready)
            assert backend.service_pending_edges(max_edges=1) == 1
        assert core.SB == 0x2D
        assert not core.transfer_enabled
        assert backend.debug_snapshot()["held_owner_byte_response"] is True
        assert backend.debug_snapshot()["pending_edge_requests"] == 1
        assert sender.is_alive()
        assert responses == []
        yield SimpleNamespace(
            backend=backend,
            core=core,
            sender=sender,
            errors=errors,
            responses=responses,
            provider=provider,
        )
    finally:
        assert backend.stop(timeout_s=1.0)
        assert peer.stop(timeout_s=1.0)
        if sender.ident is not None:
            sender.join(timeout=1.0)
            assert not sender.is_alive()
        if provider is not None:
            provider.detach_all()


def test_rearming_alone_does_not_release_held_byte():
    with _held_byte() as case:
        token = case.backend.begin_owner_frame()
        case.core.set_SC(0x80)
        # No subsequent owner frame has completed. An absent/older token
        # cannot turn SC rearming into proof of CPU continuation.
        case.backend.finish_owner_frame(None)
        assert case.backend.debug_snapshot()["held_owner_byte_response"] is True
        assert case.backend.debug_snapshot()["edge_resp_sent"] == 7
        assert case.responses == []
        case.backend.finish_owner_frame(token)
        case.sender.join(timeout=1.0)
        assert not case.sender.is_alive()
        assert case.errors == []
        assert case.responses == [0x3C]
        assert case.backend.debug_snapshot()["pending_edge_requests"] == 0


def test_released_response_is_flushed_before_new_master_edge():
    """A completed frame can release the held byte before it is on the wire.

    The held reference is cleared the moment the owner frame completes, but the
    response worker may not have written the EDGE_RESP yet. A subsequent local
    master edge must still wait for that released response, or the peer sees a
    new EDGE_REQ before the prior EDGE_RESP and answers from the wrong bit.
    """
    peer, backend = NetworkBackend.pair()
    backend._edge_queue = _ObservedEdgeQueue(threading.Event(), maxsize=256)
    core = Serial()
    try:
        peer.start_receiver(local_core=None)
        backend.start_receiver(
            core,
            serial_gate=SerialOperationGate(),
            dispatch_to_owner=True,
            defer_byte_responses=True,
        )
        token = _InboundEdge(
            peer_bit=0,
            response_bit=1,
            completed=True,
            response_finished=threading.Event(),
        )
        with backend._close_lock:
            backend._held_owner_byte_response = token
        backend._edge_pending = 1

        worker_entered = threading.Event()
        allow_send = threading.Event()
        sent: list[int] = []
        original_send = backend._send_edge_response

        def gated_send(request):
            worker_entered.set()
            assert allow_send.wait(2.0)
            sent.append(request.response_bit)

        backend._send_edge_response = gated_send
        try:
            # Release the byte exactly as a completed owner frame does. The
            # response is now queued but the worker is held before it writes.
            backend.finish_owner_frame(token)
            assert worker_entered.wait(2.0)
            assert backend._held_owner_byte_response is None
            assert backend._owner_response_pending == 1

            flushed = threading.Event()

            def flush_before_master_edge():
                backend._finish_held_response_before_master_edge(time.monotonic() + 5.0)
                flushed.set()

            flusher = threading.Thread(target=flush_before_master_edge, daemon=True)
            flusher.start()
            # The defect returned immediately here, letting the new edge pass
            # the still-unsent response. The fix must block on the receipt.
            assert not flushed.wait(0.3)
            assert sent == []
            allow_send.set()
            flusher.join(timeout=2.0)
            assert flushed.is_set()
            assert sent == [1]
            assert backend._owner_response_pending == 0
        finally:
            backend._send_edge_response = original_send
    finally:
        assert backend.stop(timeout_s=1.0)
        assert peer.stop(timeout_s=1.0)


def test_closed_abandonment_retires_released_response_accounting():
    """A close that wins the publication race must retire the counted receipt."""
    peer, backend = NetworkBackend.pair()
    try:
        token = _InboundEdge(
            peer_bit=0,
            response_bit=1,
            completed=True,
            response_finished=threading.Event(),
        )
        backend._held_owner_byte_response = token
        backend._edge_pending = 1
        # Count the released response, then let a deadline-aware close publish
        # terminal state before the response worker can queue it.
        backend._begin_owner_response()
        assert backend._owner_response_pending == 1
        backend._mark_closed(NetworkBackendError("close won the publication race"))
        backend._publish_owner_response(token)
        assert backend._owner_response_pending == 0
        assert token.response_finished.is_set()
    finally:
        assert backend.stop(timeout_s=1.0)
        assert peer.stop(timeout_s=1.0)


@pytest.mark.parametrize("operation", ["stop", "detach_local_core"])
def test_retiring_receiver_cancels_held_byte_without_acknowledging_it(operation):
    with _held_byte() as case:
        token = case.backend.begin_owner_frame()
        assert getattr(case.backend, operation)(timeout_s=1.0)
        case.sender.join(timeout=1.0)
        assert not case.sender.is_alive()
        assert len(case.errors) == 1
        assert case.responses == []
        snapshot = case.backend.debug_snapshot()
        assert snapshot["closed"] is True
        assert snapshot["held_owner_byte_response"] is False
        assert snapshot["pending_edge_requests"] == 0
        assert snapshot["edge_resp_sent"] == 7
        assert snapshot["pre_close_snapshot"]["held_owner_byte_response"] is True
        assert case.core.SB == 0x2D  # already-committed hardware is preserved
        if operation == "stop":
            case.backend.finish_owner_frame(token)
        else:
            with pytest.raises(NetworkBackendError, match="detached"):
                case.backend.finish_owner_frame(token)
            assert case.backend._local_core is None
            assert case.backend._irq_callback is None
        assert case.backend.debug_snapshot()["edge_resp_sent"] == 7


def test_paused_frame_does_not_acknowledge_held_byte(_emulator_fixture):  # noqa: F811
    pyboy = _emulator_fixture
    with _held_byte(pyboy) as case:
        frame = int(pyboy.frame_count)
        pyboy.paused = True
        case.provider.step(1)
        assert int(pyboy.frame_count) == frame
        assert case.backend.debug_snapshot()["held_owner_byte_response"] is True
        assert case.responses == []
        pyboy.paused = False
        case.provider.step(1)
        case.sender.join(timeout=1.0)
        assert not case.sender.is_alive()
        assert case.errors == []
        assert case.responses == [0x3C]


def test_failed_owner_frame_cancels_held_byte(_emulator_fixture, monkeypatch):  # noqa: F811
    pyboy = _emulator_fixture

    def fail_frame(*args, **kwargs):
        raise RuntimeError("ordinary frame failed")

    monkeypatch.setattr(pyboy, "_tick", fail_frame)
    with _held_byte(pyboy) as case:
        with pytest.raises(RuntimeError, match="ordinary frame failed"):
            case.provider.step(1)
        case.sender.join(timeout=1.0)
        assert not case.sender.is_alive()
        assert len(case.errors) == 1
        assert case.responses == []
        assert case.provider._network_tick_active is False
        snapshot = case.backend.debug_snapshot()
        assert snapshot["closed"] is True
        assert snapshot["held_owner_byte_response"] is False
        assert snapshot["pending_edge_requests"] == 0
        assert snapshot["edge_resp_sent"] == 7


def test_clock_role_change_sends_held_response_before_new_master_request(
    _emulator_fixture,  # noqa: F811
):
    pyboy = _emulator_fixture
    program = _receiver_program()
    pyboy.memory[0, 0x150 : 0x150 + len(program)] = program
    # The first serial IRQ changes to internal clock before notifying the
    # main loop. The second IRQ leaves the port idle after receiving 0x16.
    handler = [
        0xF5,
        0xF0, 0x01, 0xEA, 0x00, 0xC1,  # SB -> mailbox
        0xFA, 0x03, 0xC1, 0x3C, 0xEA, 0x03, 0xC1,  # increment IRQ count
        0xFE, 0x01, 0x20, 0x08,  # only the first IRQ changes clock role
        0x3E, 0xA5, 0xE0, 0x01,  # outgoing byte for local master
        0x3E, 0x81, 0xE0, 0x02,
        0x3E, 0x01, 0xEA, 0x01, 0xC1,  # notify main loop
        0xF1, 0xD9,
    ]
    pyboy.memory[0, 0x58 : 0x58 + len(handler)] = handler
    pyboy.memory[_MAILBOX : _OUTPUT + 2] = [0] * (_OUTPUT + 2 - _MAILBOX)
    pyboy.tick(1, render=False, sound=False)

    peer, backend = NetworkBackend.pair()
    peer._sock.settimeout(3.0)
    edge_ready = threading.Event()
    backend._edge_queue = _ObservedEdgeQueue(edge_ready, maxsize=256)
    backend._completed_edge_queue = _ResponseHandoffQueue(
        backend, edge_ready, expected_requests=8
    )
    provider = PyBoyLinkSession(network_backend=backend)
    errors, received = [], []

    def read_frame():
        data = bytearray()
        while len(data) != _FRAME.size:
            chunk = peer._sock.recv(_FRAME.size - len(data))
            assert chunk, "receiver closed before completing the role transition"
            data.extend(chunk)
        return _FRAME.unpack(data)

    def exchange():
        try:
            # A raw TCP peer makes the wire-order requirement explicit:
            # all eight responses must precede the new local master request.
            for bit in range(7, -1, -1):
                peer._sock.sendall(_FRAME.pack(_OP_EDGE_REQ, (0x2D >> bit) & 1))
                assert read_frame() == (_OP_EDGE_RESP, (0x3C >> bit) & 1)
            for bit in range(7, -1, -1):
                opcode, value = read_frame()
                assert opcode == _OP_EDGE_REQ
                received.append(value)
                peer._sock.sendall(_FRAME.pack(_OP_EDGE_RESP, (0x16 >> bit) & 1))
        except BaseException as exc:  # noqa: BLE001 - asserted after join
            errors.append(exc)

    sender = threading.Thread(target=exchange, daemon=True)
    try:
        provider.attach(pyboy)
        sender.start()
        _wait_for_edge_request(backend, edge_ready)
        for _ in range(8):
            provider.step(1)
            if pyboy.memory[_COUNT] == 2:
                break
        sender.join(timeout=1.0)
        assert not sender.is_alive()
        assert errors == []
        assert received == [(0xA5 >> bit) & 1 for bit in range(7, -1, -1)]
        assert list(pyboy.memory[_OUTPUT : _OUTPUT + 2]) == [0x2D, 0x16]
        assert pyboy.memory[_COUNT] == 2
        snapshot = backend.debug_snapshot()
        assert snapshot["edge_req_sent"] == snapshot["edge_resp_received"] == 8
        assert snapshot["edge_req_received"] == snapshot["edge_resp_sent"] == 8
        assert snapshot["reciprocal_master_edges"] == 0
        assert snapshot["pending_edge_requests"] == 0
        assert snapshot["held_owner_byte_response"] is False
    finally:
        assert backend.stop(timeout_s=1.0)
        assert peer.stop(timeout_s=1.0)
        if sender.ident is not None:
            sender.join(timeout=1.0)
            assert not sender.is_alive()
        provider.detach_all()
