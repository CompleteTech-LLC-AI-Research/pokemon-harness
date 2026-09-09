"""Real serial/CPU ownership checks; no cartridge data or gameplay RAM shortcuts."""

from contextlib import nullcontext
import threading
import time

import pytest
from pyboy.core.serial import Serial, SerialBackendError

from pokered_harness.link import network_backend as network_module
from pokered_harness.link.network_backend import NetworkBackend, NetworkBackendError
from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
from pokered_harness.session import Session
from pokered_harness.symbols.loader import SymbolTable
from tests.test_serial_backend_boundary import emulator as emulator

pytestmark = [pytest.mark.unit, pytest.mark.timing_sensitive]


def _thread(function, name):
    errors = []
    def run():
        try:
            function()
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=run, name=name, daemon=True)
    worker.start()
    return worker, errors


def _join(worker):
    worker.join(3)
    assert not worker.is_alive(), worker.name


def _stop(*backends):
    for backend in backends:
        backend.stop()
    for backend in backends:
        for worker in (backend._reader, backend._edge_worker):
            assert worker is None or not worker.is_alive()


class _OwnerClaimTestDouble:
    """Explicit owner binding for a non-native mailbox test double."""

    def __init__(self):
        self.owner_poll_enabled = False
        self._owner_callback = None
        self._owner_token = None

    def set_owner_pump(self, callback, poll=False):
        if self._owner_token is not None:
            raise RuntimeError("serial owner pump is exclusively claimed")
        self._owner_callback = callback
        self.owner_poll_enabled = callback is not None and poll

    def claim_owner_pump(self, callback, poll=False):
        if self._owner_callback is not None or self._owner_token is not None:
            raise RuntimeError("serial owner pump already installed or claimed")
        self._owner_token = object()
        self._owner_callback = callback
        self.owner_poll_enabled = poll
        return self._owner_token

    def release_owner_pump(self, token):
        if token is not self._owner_token:
            raise RuntimeError("invalid serial owner pump claim token")
        self._owner_token = None
        self._owner_callback = None
        self.owner_poll_enabled = False


def test_real_core_incoming_sample_shift_and_irq_use_declared_owner(monkeypatch):
    """Same invariant runs before/after: old worker mode fails thread IDs."""
    monkeypatch.setattr(network_module, "_EDGE_CALL_TIMEOUT_SECONDS", 1.0)
    sender, receiver = Serial(False), Serial(False)
    sender.set_SB(0xA5)
    receiver.set_SB(0x3C)
    sender.set_SC(0x81)
    receiver.set_SC(0x80)
    mutations, owner_ids = [], []
    ready, done, quit_owner = threading.Event(), threading.Event(), threading.Event()

    class ObservedReceiver:
        def __getattr__(self, name):
            return getattr(receiver, name)
        def peek_out_bit(self):
            mutations.append(("sample", threading.get_ident()))
            return receiver.peek_out_bit()
        def apply_external_edge(self, bit):
            mutations.append(("apply", threading.get_ident()))
            return receiver.apply_external_edge(bit)

    def irq():
        mutations.append(("irq", threading.get_ident()))
        done.set()

    a, b = NetworkBackend.pair()
    sender.backend = a
    a.start_receiver(local_core=None)
    b.start_receiver(local_core=ObservedReceiver(), irq_callback=irq)

    def receiver_owner():
        owner_ids.append(threading.get_ident())
        # Compatibility is test-only, to retain an identical fail-before
        # invariant against the old backend, not a production worker fallback.
        scope = getattr(b, "owner_scope", nullcontext)
        with scope():
            ready.set()
            while not quit_owner.is_set():
                poll = getattr(b, "owner_poll", None)
                if poll:
                    poll()
                quit_owner.wait(.0005)

    worker, errors = _thread(receiver_owner, "declared-receiver-owner")
    try:
        assert ready.wait(2)
        assert sender.tick(4096) is True
        sender.check_error()
        assert done.wait(1)
        assert sender.SB == 0x3C and receiver.SB == 0xA5
        assert [kind for kind, _ in mutations] == ["sample", "apply"] * 8 + ["irq"]
    finally:
        quit_owner.set()
        _join(worker)
        _stop(a, b)
    assert errors == []
    assert all(thread == owner_ids[0] for _, thread in mutations), (owner_ids, mutations)


def test_no_owner_means_no_mutation_and_bounded_pending_expiry(monkeypatch):
    monkeypatch.setattr(network_module, "_EDGE_CALL_TIMEOUT_SECONDS", .08)
    core = Serial()
    core.set_SB(0x3C)
    core.set_SC(0x80)
    irq = []
    a, b = NetworkBackend.pair()
    a.start_receiver(None)
    b.start_receiver(core, lambda: irq.append(True))
    try:
        with pytest.raises(NetworkBackendError):
            a.on_edge(1, 1)
        assert core._bits_remaining == 8 and core._shift_register == 0x3C
        assert irq == []
        assert b._closed_event.wait(.5)
        with b.owner_scope(), pytest.raises(NetworkBackendError):
            b.owner_poll()
        assert core._bits_remaining == 8
    finally:
        _stop(a, b)


def test_cancel_pending_request_does_not_require_owner_lock(monkeypatch):
    monkeypatch.setattr(network_module, "_EDGE_CALL_TIMEOUT_SECONDS", 1.0)
    core = Serial()
    core.set_SC(0x80)
    a, b = NetworkBackend.pair()
    a.start_receiver(None)
    b.start_receiver(core)
    held, release = threading.Event(), threading.Event()
    def hold_owner():
        with b.owner_scope():
            held.set()
            assert release.wait(2)
    owner, owner_errors = _thread(hold_owner, "held-owner")
    assert held.wait(1)
    sender, send_errors = _thread(lambda: a.on_edge(1, 1), "sender")
    try:
        deadline = time.monotonic() + 1
        while b._pending_edge is None and time.monotonic() < deadline:
            time.sleep(.001)
        assert b._pending_edge is not None
        b.stop()
        _join(sender)
        assert len(send_errors) == 1 and isinstance(send_errors[0], NetworkBackendError)
        assert core._bits_remaining == 8
    finally:
        release.set()
        _join(owner)
        _stop(a, b)
    assert owner_errors == []


def test_owner_scope_can_migrate_between_threads_without_stale_native_binding():
    core = Serial()
    a, b = NetworkBackend.pair()
    b.start_receiver(core)
    observations = []
    barrier = threading.Barrier(2)
    first_done = threading.Event()
    def first():
        with b.owner_scope():
            observations.append((threading.get_ident(), core.owner_poll_enabled))
        first_done.set()
        barrier.wait(2)
    def second():
        assert first_done.wait(2)
        with b.owner_scope():
            observations.append((threading.get_ident(), core.owner_poll_enabled))
        barrier.wait(2)
    one, errors1 = _thread(first, "first-owner")
    two, errors2 = _thread(second, "second-owner")
    try:
        _join(one)
        _join(two)
        assert errors1 == errors2 == []
        assert len({tid for tid, _ in observations}) == 2
        assert all(enabled for _, enabled in observations)
        assert not core.owner_poll_enabled
    finally:
        _stop(a, b)


@pytest.mark.parametrize("raw", [False, True])
def test_real_cpu_external_halt_receives_via_managed_or_raw_owner(emulator, raw):
    p = emulator
    # Authored ROM instructions enable serial IRQ, arm external receive, HALT.
    program = [0x3E, 8, 0xE0, 0xFF, 0x3E, 0x3C, 0xE0, 1,
               0x3E, 0x80, 0xE0, 2, 0x76, 0x18, 0xFD]
    p.memory[0, 0x150:0x150 + len(program)] = program
    session = Session(pyboy=p, symbols=SymbolTable([]))
    a, b = NetworkBackend.pair()
    a.start_receiver(None)
    provider = PyBoyLinkSession(network_backend=b)
    provider.attach(p)  # No listener/connector role seed configured.
    ready, complete, quit_owner = threading.Event(), threading.Event(), threading.Event()
    original_poll = b.owner_poll
    halt_cycle = None
    def observe_halt(event=None):
        nonlocal halt_cycle
        if event is not None and event[1] == 4 and p.register_file.PC == 0x15C:
            if halt_cycle is None:
                halt_cycle = event[2]
            elif event[2] > halt_cycle:
                # HALT keeps PC at its opcode; a subsequent outer poll with
                # advanced cycles proves the actual instruction executed.
                ready.set()
        original_poll(event)
    b.owner_poll = observe_halt
    irq_threads, owner_ids, irq_values = [], [], []
    original_irq = b._irq_callback
    def irq():
        irq_threads.append(threading.get_ident())
        original_irq()
        irq_values.append(p.memory[0xFF0F])
        complete.set()
    b._irq_callback = irq
    def receive():
        owner_ids.append(threading.get_ident())
        while not quit_owner.is_set():
            (provider.step if raw else session.step)(1)
    owner, errors = _thread(receive, "cpu-owner")
    sender = Serial(backend=a)
    sender.set_SB(0xA5)
    sender.set_SC(0x81)
    try:
        assert ready.wait(2)
        assert sender.tick(4096)
        sender.check_error()
        assert complete.wait(2)
    finally:
        quit_owner.set()
        owner.join(2)
        # Failure cleanup is bounded even if a broken frame cannot return.
        if owner.is_alive():
            b.stop()
        _join(owner)
        provider.detach_all()
        _stop(a, b)
    assert errors == []
    assert p.frame_count > 0
    assert irq_threads == owner_ids
    assert sender.SB == 0x3C and p.mb.serial.SB == 0xA5
    assert irq_values[0] & 8
    session.close()


def test_optional_outer_poll_preserves_default_event_stream_and_blocks_recursion(emulator):
    p = emulator
    events = []
    p.mb.serial.set_owner_pump(events.append)
    p.mb.breakpoint_singlestep = True
    p.mb.tick()
    assert events == []
    def recurse(event):
        events.append(event)
        p.mb.tick()
    p.mb.serial.set_owner_pump(recurse, poll=True)
    with pytest.raises(SerialBackendError) as raised:
        p.mb.tick()
    assert len(events) == 1 and events[0][1] == 4
    assert "recursive CPU" in str(raised.value.__cause__)
    assert not p.mb.serial.owner_pump_active


def test_irq_failure_latches_first_cause_and_never_acknowledges_or_replays(emulator):
    p = emulator
    session = Session(pyboy=p, symbols=SymbolTable([]))
    a, b = NetworkBackend.pair()
    a.start_receiver(None)
    provider = PyBoyLinkSession(network_backend=b)
    provider.attach(p)
    core = p.mb.serial
    cause = ValueError("completion IRQ failed")
    with session.locked():
        core.set_SB(0)
        core.set_SC(0x80)
        for bit in (1, 0, 1, 0, 0, 1, 0):
            assert not core.apply_external_edge(bit)
    def irq():
        raise cause
    b._irq_callback = irq
    sender, send_errors = _thread(lambda: a.on_edge(1, 1), "fault-edge-sender")
    try:
        deadline = time.monotonic() + 1
        while b._pending_edge is None and time.monotonic() < deadline:
            time.sleep(.001)
        assert b._pending_edge is not None
        with pytest.raises(SerialBackendError) as raised:
            with session.locked():
                p.memory[0xFF01]
        assert raised.value.__cause__ is cause
        assert core.SB == 0xA5 and core._bits_remaining == 0
        _join(sender)
        assert len(send_errors) == 1 and isinstance(send_errors[0], NetworkBackendError)
        assert b.debug_snapshot()["edge_resp_sent"] == 0
        with pytest.raises(SerialBackendError) as repeated:
            core.check_error()
        assert repeated.value.__cause__ is cause
        assert core.SB == 0xA5
    finally:
        provider.detach_all()
        _stop(a, b)
        session.close()


def test_direct_recursive_mailbox_service_cannot_apply_pending_edge_twice():
    class Core(_OwnerClaimTestDouble):
        transfer_enabled = True
        internal_clock = False
        applications = 0
        def __init__(self):
            super().__init__()
        def peek_out_bit(self):
            return 0
        def apply_external_edge(self, bit):
            self.applications += 1
            return True
    core = Core()
    a, b = NetworkBackend.pair()
    a.start_receiver(None)
    b.start_receiver(core, lambda: b.owner_poll())
    sender, send_errors = _thread(lambda: a.on_edge(1, 1), "recursive-edge-sender")
    try:
        deadline = time.monotonic() + 1
        while b._pending_edge is None and time.monotonic() < deadline:
            time.sleep(.001)
        assert b._pending_edge is not None
        with b.owner_scope(), pytest.raises(NetworkBackendError, match="recursive incoming"):
            b.owner_poll()
        _join(sender)
        assert len(send_errors) == 1 and isinstance(send_errors[0], NetworkBackendError)
        assert core.applications == 1
        assert b.debug_snapshot()["edge_resp_sent"] == 0
    finally:
        _stop(a, b)


def test_duplicate_pending_request_fails_before_any_owner_mutation():
    core = Serial()
    core.set_SB(0x3C)
    core.set_SC(0x80)
    a, b = NetworkBackend.pair()
    b.start_receiver(core)
    try:
        a._send_frame(bytes((0x10, 1, 0x10, 0)))
        assert b._closed_event.wait(1)
        assert "duplicate pending" in str(b._reader_exc)
        assert core._bits_remaining == 8 and core._shift_register == 0x3C
        assert b.debug_snapshot()["edge_resp_sent"] == 0
    finally:
        _stop(a, b)


@pytest.mark.parametrize("raw", [False, True])
def test_real_cpu_internal_edges_resume_through_managed_or_raw_owner(emulator, raw):
    p = emulator
    program = [0x3E, 8, 0xE0, 0xFF, 0x3E, 0xA5, 0xE0, 1,
               0x3E, 0x81, 0xE0, 2, 0x76, 0x18, 0xFD]
    p.memory[0, 0x150:0x150 + len(program)] = program
    session = Session(pyboy=p, symbols=SymbolTable([]))
    a, b = NetworkBackend.pair()
    provider = PyBoyLinkSession(network_backend=a)
    provider.attach(p)
    receiver = Serial()
    receiver.set_SB(0x3C)
    receiver.set_SC(0x80)
    b.start_receiver(receiver)
    ready, finish = threading.Event(), threading.Event()
    def receive():
        with b.owner_scope():
            ready.set()
            while not finish.is_set():
                b.owner_poll()
                finish.wait(.0005)
    worker, errors = _thread(receive, "external-receiver-owner")
    events = []
    original_poll = a.owner_poll
    owner_id = threading.get_ident()
    def observe(event=None):
        if event is not None:
            events.append((event[1], threading.get_ident()))
        original_poll(event)
    a.owner_poll = observe
    try:
        assert ready.wait(2)
        (provider.step if raw else session.step)(1)
        assert p.frame_count == 1
        assert p.mb.serial.SB == 0x3C and receiver.SB == 0xA5
        assert p.memory[0xFF0F] & 8
        assert a.debug_snapshot()["edge_req_sent"] == 8
        assert b.debug_snapshot()["slave_armed_edges"] == 8
        assert sum(kind == 3 for kind, _ in events) == 8
        assert {kind for kind, _ in events} >= {2, 3, 4}
        assert all(thread == owner_id for _, thread in events)
    finally:
        finish.set()
        _join(worker)
        provider.detach_all()
        _stop(a, b)
        session.close()
    assert errors == []
