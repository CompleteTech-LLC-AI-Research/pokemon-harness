"""Timed session acceptance with authored cartridges and real CPU retirement.

ScriptedChannel is a CONTROL DOUBLE, not wire/protocol acceptance. PyBoy,
Motherboard, CPU and Serial remain real in its tests. No commercial assets,
fake CPU clocks or replacement retirement providers are used. Native execution
must be selected by the main gate; source results do not qualify native code.
"""

import importlib
import importlib.util
import logging
import socket
import threading
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from pokered_harness.link.timed_wire import (
    Cancelled,
    ChannelClosed,
    DeadlineExceeded,
    EdgeRequest,
    EdgeResponse,
    EmissionComplete,
    Fence,
    FenceAck,
    Progress,
    Sync,
    TimedWireChannel,
)


class ScriptedChannel:
    """Explicit transport control double; never pretends to validate wire frames."""

    def __init__(self, messages=(), *, revision=2):
        self._sock, self._peer = socket.socketpair()
        self.epoch = b"timed-test-epoch"
        self.revision = revision
        # HELLO is not a committed-time anchor. Every executing control peer
        # explicitly sends zero progress before its application messages.
        self.messages = deque((Progress(0), *messages))
        self.sent = []
        self.send_deadlines = []
        self.closed = False
        self.error = None
        self.on_send = None
        self.on_poll = None
        self.polls = 0

    def handshake(self, *, deadline, cancel_event=None):
        self._check(cancel_event)
        if time.monotonic() >= deadline:
            raise TimeoutError("control handshake deadline")

    def _check(self, cancel_event=None):
        if self.closed:
            raise ChannelClosed("control channel closed")
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled("control cancellation")

    def send(self, message, *, deadline, cancel_event=None):
        self._check(cancel_event)
        if time.monotonic() >= deadline:
            raise TimeoutError("control send deadline")
        self.send_deadlines.append(deadline)
        if self.on_send is not None:
            self.on_send(message)
        self.sent.append(message)

    def poll(self):
        self._check()
        self.polls += 1
        if self.on_poll is not None:
            self.on_poll()
        return self.messages.popleft() if self.messages else None

    def receive(self, *, deadline, cancel_event=None):
        while time.monotonic() < deadline:
            self._check(cancel_event)
            message = self.poll()
            if message is not None:
                return message
            if cancel_event is not None:
                cancel_event.wait(min(0.001, max(0, deadline - time.monotonic())))
            else:
                threading.Event().wait(0.001)
        raise TimeoutError("control receive deadline")

    def close(self):
        self.closed = True
        self._sock.close()
        self._peer.close()


class NoFurtherEdgesChannel(ScriptedChannel):
    """Synthetic control peer: all requests are queued before each idle poll.

    Attest only the owner's last published interval, with the received prefix.
    This explicit no-further-edges script is not a second CPU or runtime proof.
    It never invents peer CPU progress or changes execution policy.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.received_edge = 0
        self.complete_through = -1

    def poll(self):
        message = super().poll()
        if isinstance(message, EdgeRequest):
            self.received_edge = message.edge_id
        elif isinstance(message, EmissionComplete):
            self.complete_through = message.through_half_cycle
        elif message is None:
            progress = next(
                (m.settled_half_cycles for m in reversed(self.sent) if isinstance(m, Progress)),
                0,
            )
            if progress > max(0, self.complete_through):
                self.complete_through = progress
                return EmissionComplete(progress, self.received_edge)
        return message


@pytest.fixture
def session_type():
    return importlib.import_module("pokered_harness.link.timed_link_session").TimedLinkSession


@contextmanager
def authored_game(tmp_path, name="authored-timed"):
    """Actual selected-runtime PyBoy, CPU, MB and Serial; authored zero cartridge."""
    from pyboy import PyBoy
    from pyboy.core import cpu, mb, serial

    cartridge = bytearray(0x8000)
    cartridge[0x134:0x139] = b"TIMED"
    cartridge[0x14D] = (-sum(cartridge[0x134:0x14D]) - 25) & 0xFF
    path = tmp_path / f"{name}.gb"
    path.write_bytes(cartridge)
    emulator = PyBoy(str(path), window="null", sound_emulated=False)
    try:
        assert type(emulator.mb) is mb.Motherboard
        assert type(emulator.mb.cpu) is cpu.CPU
        assert type(emulator.mb.serial) is serial.Serial
        assert emulator.mb.cpu.retired_instructions == 0
        emulator.set_emulation_speed(0)
        emulator.memory[0xFF50] = 1
        emulator.memory[0xFFFF] = emulator.memory[0xFF0F] = 0
        emulator.register_file.PC = 0xC000
        emulator.register_file.SP = 0xD000
        emulator.memory[0xC000:0xC002] = [0x18, 0xFE]  # Real 12-cycle JR loop.
        yield emulator
    finally:
        emulator.stop(save=False)


@pytest.fixture
def game(tmp_path):
    with authored_game(tmp_path) as emulator:
        yield emulator


@contextmanager
def attached(session_type, game, channel=None, **options):
    channel = channel if channel is not None else ScriptedChannel()
    settings = {
        "rearm_budget": 4096,
        "rearm_instruction_cap": 1024,
        "max_edge_lateness": 4096,
        "quantum_cycles": 100000,
        "operation_timeout": 0.2,
        "max_wait_attempts": 16,
        "inbound_capacity": 64,
    }
    settings.update(options)
    session = session_type(channel, **settings)
    try:
        session.attach(game, deadline=time.monotonic() + 1)
        yield session, channel
    finally:
        session.close()
        channel.close()


def test_public_tick_executes_real_instructions_and_preserves_result(session_type, game):
    public_tick = game.tick
    calls = []

    def observe(*args, **kwargs):
        result = public_tick(*args, **kwargs)
        calls.append(result)
        return result

    game.tick = observe
    start = game.mb.cpu.cycles
    retired = game.mb.cpu.retired_instructions
    frame = game.frame_count
    with attached(session_type, game, NoFurtherEdgesChannel()) as (session, channel):
        result = session.tick(1, render=False, sound=False)
        assert calls and type(result) is type(calls[-1]) and result == calls[-1]
        assert game.frame_count == frame + 1
        count = game.mb.cpu.retired_instructions - retired
        assert count > 0
        assert game.mb.cpu.cycles - start == count * 12
        assert any(isinstance(message, Progress) for message in channel.sent)
        assert any(isinstance(message, EmissionComplete) for message in channel.sent)


def test_public_zero_count_does_not_execute(session_type, game):
    before = (game.mb.cpu.cycles, game.mb.cpu.retired_instructions, game.frame_count)
    with attached(session_type, game) as (session, _):
        result = session.tick(0, render=False, sound=False)
        assert result == 0
        assert (game.mb.cpu.cycles, game.mb.cpu.retired_instructions, game.frame_count) == before


def test_control_armed_attach_is_transactional(session_type, game):
    original = game.mb.serial.backend
    game.memory[0xFF02] = 0x80
    channel = ScriptedChannel()
    session = session_type(channel, rearm_budget=64, rearm_instruction_cap=16, max_edge_lateness=64)
    try:
        with pytest.raises(RuntimeError, match="idle"):
            session.attach(game, deadline=time.monotonic() + 1)
        assert game.mb.serial.backend is original
        assert game.mb.execution_before is None
        assert game.mb.execution_after is None
    finally:
        session.close()
        channel.close()


def test_control_close_restores_owned_backend_and_callbacks(session_type, game):
    serial = game.mb.serial
    before = (serial.backend, serial.owner_dispatch_callback, serial.owner_dispatch_enabled)
    with attached(session_type, game) as (session, _):
        session.close()
        assert serial.backend is before[0]
        assert serial.owner_dispatch_callback is before[1]
        assert serial.owner_dispatch_enabled == before[2]
        assert game.mb.execution_before is None
        assert game.mb.execution_after is None
        with pytest.raises(RuntimeError, match="epoch cannot be reused"):
            session.attach(game, deadline=time.monotonic() + 1)


def test_control_eighth_response_precedes_irq_and_latches_real_byte(session_type, game):
    # Transfer armed by real LDH instruction after the idle-only attachment.
    game.memory[0xC000:0xC008] = [0x3E, 0x80, 0xE0, 0x02, 0x00, 0x18, 0xFE, 0x00]
    channel = NoFurtherEdgesChannel()
    owner = threading.get_ident()
    observations = []
    queued = False

    def inject():
        nonlocal queued
        if not queued and game.mb.serial.transfer_enabled:
            queued = True
            channel.messages.extend(
                EdgeRequest(i, 0, 0, (0xA5 >> (8 - i)) & 1) for i in range(1, 9)
            )
            channel.messages.append(EmissionComplete(0, 8))

    def observe(message):
        if isinstance(message, EdgeResponse):
            observations.append(
                (
                    message.edge_id,
                    threading.get_ident(),
                    game.memory[0xFF0F] & 8,
                    game.mb.serial.SB,
                    bool(game.mb.serial.transfer_enabled),
                )
            )

    channel.on_poll = inject
    channel.on_send = observe
    with attached(session_type, game, channel) as (session, _):
        session.tick(1, render=False, sound=False)
        assert [row[0] for row in observations] == list(range(1, 9))
        assert all(row[1] == owner and row[2] == 0 for row in observations)
        assert observations[-1][3:] == (0xA5, False)
        assert game.memory[0xFF0F] & 8


def test_control_failure_after_successful_public_execution_cleans_up(session_type, game):
    original_tick = game.tick
    backend = game.mb.serial.backend
    error = RuntimeError("public post-execution sentinel")

    def fail_after(*args, **kwargs):
        original_tick(*args, **kwargs)
        raise error

    game.tick = fail_after
    with attached(session_type, game, NoFurtherEdgesChannel()) as (session, channel):
        with pytest.raises(RuntimeError) as caught:
            session.tick(1, render=False, sound=False)
        assert caught.value is error
        assert game.mb.cpu.retired_instructions > 0
        assert channel.closed
        assert game.mb.execution_before is None
        assert game.mb.execution_after is None
        assert game.mb.serial.backend is backend


def test_control_cancel_before_tick_prevents_cpu_execution(session_type, game):
    with attached(session_type, game) as (session, _):
        before = (game.mb.cpu.cycles, game.mb.cpu.retired_instructions)
        session.cancel()
        start = time.monotonic()
        with pytest.raises(Cancelled):
            session.tick(1, render=False, sound=False)
        assert time.monotonic() - start < 1
        assert (game.mb.cpu.cycles, game.mb.cpu.retired_instructions) == before


def test_real_channel_handshake_attach_and_cleanup(session_type, game):
    left, right = socket.socketpair()
    channel = TimedWireChannel(left, epoch=b"timed-test-epoch")
    peer = TimedWireChannel(right, epoch=channel.epoch)
    failures = []

    def handshake():
        try:
            peer.handshake(deadline=time.monotonic() + 1)
        except BaseException as exc:  # noqa: BLE001 - Report every worker failure to the main assertion.
            failures.append(exc)

    worker = threading.Thread(target=handshake, daemon=True)
    worker.start()
    try:
        with attached(session_type, game, channel):
            worker.join(1)
            assert not worker.is_alive()
            assert not failures
    finally:
        peer.close()
        channel.close()
        worker.join(1)


@pytest.mark.parametrize(
    "exchange,lateness",
    [(False, 4096), (True, 4096), (False, 32)],
    ids=["idle", "delayed-byte", "asymmetric-L64-half"],
)
def test_real_pair_repeated_public_frames_bounded_wire_volume(
    session_type, tmp_path, exchange, lateness
):
    """Real wire, public PyBoy ticks and CPU counts; no fabricated peer credit."""
    left, right = socket.socketpair()
    channels = [
        TimedWireChannel(left, epoch=b"timed-test-epoch"),
        TimedWireChannel(right, epoch=b"timed-test-epoch"),
    ]
    sessions = []
    failures = []
    results = [None, None]
    counts = [0, 0]
    edge_requests = [[], []]
    edge_responses = [[], []]
    request_permits = []
    progress = [[], []]
    messages = [[], []]
    publications = [[], []]
    native_progress = []
    start = threading.Barrier(2)
    for i, channel in enumerate(channels):
        original = channel.send

        def observe(message, *, deadline, cancel_event=None, index=i, send=original):
            counts[index] += 1
            messages[index].append(message)
            if isinstance(message, Progress):
                progress[index].append(message.settled_half_cycles)
                if sessions[index]._in_edge:
                    assert message.settled_half_cycles == sessions[index]._settled
                    native_progress.append(message)
            if isinstance(message, EdgeRequest):
                edge_requests[index].append(message)
                request_permits.append(sessions[index].snapshot().pending_permit)
            if isinstance(message, EdgeResponse):
                edge_responses[index].append(message)
                assert sessions[index].snapshot().pending_delivery
            return send(message, deadline=deadline, cancel_event=cancel_event)

        channel.send = observe
        sessions.append(
            session_type(
                channel,
                rearm_budget=4096,
                rearm_instruction_cap=1024,
                max_edge_lateness=lateness,
                quantum_cycles=256,
                operation_timeout=5,
                max_wait_attempts=16,
                inbound_capacity=64,
            )
        )
        publish = sessions[i]._publish

        def observe_publish(*, force=False, index=i, original=publish):
            begin = len(messages[index])
            result = original(force=force)
            publications[index].append(messages[index][begin:])
            return result

        sessions[i]._publish = observe_publish

    def owner(index):
        try:
            with authored_game(tmp_path, f"pair-{index}") as emulator:
                if lateness == 32 and index == 1:
                    # Real asymmetric 4-cycle NOP / 12-cycle JR retirement.
                    emulator.memory[0xC000:0xC003] = [0x00, 0x18, 0xFD]
                if exchange:
                    # Slave arms after the first master's 512-cycle edge.
                    # NOPs are real retired instructions, not injected clocks.
                    code = [0x00] * 180 if index == 1 else []
                    code += [
                        0x3E,
                        (0xA5, 0x3C)[index],
                        0xE0,
                        0x01,
                        0x3E,
                        (0x81, 0x80)[index],
                        0xE0,
                        0x02,
                    ]
                    if index == 0:
                        # The first internal edge occurs in the unfinished LDH
                        # read at +512 clocks, exercising a pending permit.
                        # SC arms at the LDH write's +4 phase, leaving eight
                        # cycles in that instruction before these 125 NOPs.
                        code += [0x00] * 125 + [0xF0, 0x01]
                    code += [0x18, 0xFE]
                    emulator.memory[0xC000 : 0xC000 + len(code)] = code
                session = sessions[index]
                session.attach(emulator, deadline=time.monotonic() + 10)
                assert session._coordinator._enforce_completeness is True
                if lateness == 32:
                    assert session._lateness == 64 and session._threshold == 128
                start.wait(10)
                raw = emulator.mb.cpu.cycles
                retired = emulator.mb.cpu.retired_instructions
                frame = emulator.frame_count
                try:
                    for _ in range(3):
                        assert session.tick(1, render=False, sound=False) == 1
                    results[index] = (
                        emulator.frame_count - frame,
                        emulator.mb.cpu.cycles - raw,
                        emulator.mb.cpu.retired_instructions - retired,
                        session.snapshot(),
                        emulator.mb.serial.SB,
                        emulator.memory[0xFF0F] & 8,
                        bool(emulator.mb.serial.transfer_enabled),
                    )
                finally:
                    # Keep both owners alive until both have finished frame 3.
                    start.wait(10)
                    session.close()
        except BaseException as exc:  # noqa: BLE001 - Report every worker failure to the main assertion.
            failures.append(exc)
            start.abort()
            for session in sessions:
                session.cancel()

    workers = [threading.Thread(target=owner, args=(i,), daemon=True) for i in range(2)]
    for worker in workers:
        worker.start()
    try:
        for worker in workers:
            worker.join(30)
        assert not any(worker.is_alive() for worker in workers), "paired public ticks deadlocked"
        assert not failures, failures
        for index, result in enumerate(results):
            frames, cycles, retired, snapshot, sb, irq, armed = result
            assert frames == 3 and retired > 0
            if exchange:
                assert sb == (0x3C, 0xA5)[index]
                assert irq == 8 and not armed
            elif lateness != 32 or index == 0:
                assert cycles == retired * 12
            else:
                assert retired * 4 < cycles < retired * 12
            assert snapshot.local_half_cycles == cycles * 2
            # At least a fourfold reduction from two messages per instruction;
            # permits coalescing policy choices without accepting per-step spam.
            if lateness == 32:
                deltas = [b - a for a, b in zip(progress[index], progress[index][1:])]
                assert any(0 < delta < 128 for delta in deltas)
                assert snapshot.local_half_cycles <= sessions[index]._coordinator_watermark + 64
            else:
                assert 0 < counts[index] <= retired // 2 + 32
            # When a safe publication sends both independently coalesced
            # frontiers, its emission prefix must be sent first.
            both = []
            for batch in publications[index]:
                if any(isinstance(m, EmissionComplete) for m in batch) and any(
                    isinstance(m, Progress) for m in batch
                ):
                    assert len(batch) == 2
                    assert isinstance(batch[0], EmissionComplete)
                    assert isinstance(batch[1], Progress)
                    assert batch[0].through_half_cycle == batch[1].settled_half_cycles
                    both.append(batch)
            assert both
            watermark = -1
            last_edge = 0
            # Attachment sends only the truthful zero CPU anchor; it must not
            # certify edge zero absent before the first safe owner pump.
            assert messages[index][0] == Progress(0)
            for position, message in enumerate(messages[index][1:], start=1):
                if isinstance(message, EdgeRequest):
                    assert message.scheduled_half_cycle > watermark
                    last_edge = message.edge_id
                    assert messages[index][position + 1] == EmissionComplete(
                        message.scheduled_half_cycle, message.edge_id
                    )
                elif isinstance(message, EmissionComplete):
                    assert message.last_edge_id == last_edge
                    watermark = message.through_half_cycle
                elif isinstance(message, Progress):
                    assert message.settled_half_cycles <= watermark
        if exchange:
            assert native_progress
            assert [m.edge_id for m in edge_requests[0]] == list(range(1, 9))
            assert not edge_requests[1]
            assert [m.edge_id for m in edge_responses[1]] == list(range(1, 9))
            assert not edge_responses[0]
            assert request_permits[0] is True
            assert (
                edge_responses[1][0].delivered_half_cycle > edge_requests[0][0].scheduled_half_cycle
            )
    finally:
        for session in sessions:
            session.cancel()
        for channel in channels:
            channel.close()
        for worker in workers:
            worker.join(5)


def test_control_send_failure_after_eighth_apply_is_terminal_no_retry(session_type, game):
    game.memory[0xC000:0xC007] = [0x3E, 0x80, 0xE0, 0x02, 0x00, 0x18, 0xFE]
    channel = ScriptedChannel()
    injected = False
    attempts = []
    sentinel = RuntimeError("eighth response failed")

    def inject():
        nonlocal injected
        if not injected and game.mb.serial.transfer_enabled:
            injected = True
            channel.messages.extend(EdgeRequest(i, 0, 0, 1) for i in range(1, 9))
            channel.messages.append(EmissionComplete(0, 8))

    def fail(message):
        if isinstance(message, EdgeResponse):
            attempts.append(message.edge_id)
            if message.edge_id == 8:
                assert game.mb.serial.SB == 0xFF
                assert not game.mb.serial.transfer_enabled
                assert not game.memory[0xFF0F] & 8
                raise sentinel

    channel.on_poll, channel.on_send = inject, fail
    with attached(session_type, game, channel) as (session, _):
        with pytest.raises(RuntimeError) as caught:
            session.tick(1, render=False, sound=False)
        assert caught.value is sentinel
        assert attempts == list(range(1, 9))
        assert game.mb.serial.SB == 0xFF and not game.mb.serial.transfer_enabled
        assert not game.memory[0xFF0F] & 8
        assert session.snapshot().closed
        with pytest.raises(ChannelClosed):
            session.tick(1, render=False, sound=False)
        assert attempts == list(range(1, 9))


def test_control_held_edge_cancel_keeps_original_deadline(session_type, game):
    channel = ScriptedChannel([EdgeRequest(1, 0, 0, 1), EmissionComplete(0, 1)])
    with attached(session_type, game, channel, quantum_cycles=256) as (session, _):
        deadlines = []
        original = channel.poll

        def observe():
            if session._held_deadline is not None:
                deadlines.append(session._held_deadline)
                if len(deadlines) == 3:
                    session.cancel()
            return original()

        channel.poll = observe
        with pytest.raises(Cancelled):
            session.tick(1, render=False, sound=False)
        assert len(deadlines) >= 3
        assert len(set(deadlines)) == 1
        assert not any(isinstance(message, EdgeResponse) for message in channel.sent)
        assert session.snapshot().cancelled
        assert game.mb.cpu.retired_instructions <= 1024


def _source_module(relative, name):
    path = Path(__file__).resolve().parents[1] / "vendor/pyboy-src/pyboy" / relative
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.logger = logging.getLogger(name)
    return module


@pytest.fixture
def source_lifecycle_game(monkeypatch):
    """Source lifecycle seam: real CPU/opcodes/Serial; inert other devices.

    This is deliberately NOT full motherboard/hardware acceptance. Device
    doubles only delimit a frame; they never supply or modify CPU counters.
    It exercises the actual source public tick and _tick even in a native lane.
    """
    from tests.test_native_execution_governor import Device

    board_module = _source_module("core/mb.py", "pyboy.core._timed_test_mb")
    cpu_module = _source_module("core/cpu.py", "pyboy.core._timed_test_cpu")
    cpu_module.opcodes = _source_module("core/opcodes.py", "pyboy.core._timed_test_opcodes")
    serial_module = _source_module("core/serial.py", "pyboy.core._timed_test_serial")
    public_module = _source_module("pyboy.py", "pyboy._timed_test_public")
    events = []
    for name, constructor in (
        ("timer", "Timer"),
        ("lcd", "LCD"),
        ("sound", "Sound"),
        ("ram", "RAM"),
        ("interaction", "Interaction"),
    ):
        device = Device(events, name)
        if name == "sound":
            device.clear_buffer = lambda: None
        monkeypatch.setattr(
            board_module, name, SimpleNamespace(**{constructor: lambda *a, _d=device, **kw: _d})
        )
    cart = Device(events, "cartridge")
    cart.cgb = False
    board_module.cartridge = SimpleNamespace(load_cartridge=lambda *a: cart)
    board_module.bootrom = SimpleNamespace(BootROM=lambda *a: SimpleNamespace(cgb=False))
    board_module.cpu, board_module.serial = cpu_module, serial_module
    board = board_module.Motherboard(None, None, None, None, None, None, 0, False, 0, False)
    from pokered_harness.link.serial_core import NullBackend

    board.serial.backend = NullBackend()
    memory = bytearray(65536)
    memory[0xC000:0xC002] = bytes([0x18, 0xFE])
    board.getitem = lambda address: memory[address]
    board.setitem = lambda address, value: memory.__setitem__(address, value)
    board.cpu.PC, board.cpu.SP = 0xC000, 0xD000
    emulator = public_module.PyBoy.__new__(public_module.PyBoy)
    emulator.initialized = False  # No destructor/plugin resources were acquired.
    emulator.mb = board
    emulator._test_memory = memory
    emulator.events = []
    emulator.stopped = emulator.paused = emulator.quitting = False
    emulator.frame_count = 0
    emulator.avg_tick = emulator.avg_emu = 0
    emulator.gameshark = SimpleNamespace(tick=lambda: events.append(("gameshark",)))
    emulator._handle_events = lambda items: events.append(("events",))
    emulator._post_handle_events = lambda: events.append(("post-events",))
    emulator._post_tick = lambda: events.append(("post-tick",))
    yield emulator, events


def test_source_actual_public_lifecycle_without_rom(source_lifecycle_game):
    game, events = source_lifecycle_game
    result = game.tick(1, render=False, sound=False)
    assert result is True
    assert game.frame_count == 1
    # Ungoverned MB batches to the inert devices' 80-cycle boundary; JR
    # instructions are indivisible, so seven real retirements reach 84.
    assert game.mb.cpu.cycles == 84
    assert game.mb.cpu.retired_instructions == 7
    assert events[0:2] == [("events",), ("gameshark",)]
    assert events[-2:] == [("post-events",), ("post-tick",)]


def test_source_successful_after_then_device_error_owner_cleanup(
    session_type, source_lifecycle_game
):
    game, events = source_lifecycle_game
    sentinel = RuntimeError("device failed after actual instruction report")
    original_backend = game.mb.serial.backend
    with attached(session_type, game) as (session, channel):

        def fail(clock):
            snapshot = session.snapshot()
            assert snapshot.raw_cpu_clock == game.mb.cpu.cycles == 12
            assert game.mb.cpu.retired_instructions == 1
            assert not snapshot.pending_permit
            raise sentinel

        game.mb.timer.tick = fail
        with pytest.raises(RuntimeError) as caught:
            session.tick(1, render=False, sound=False)
        assert caught.value is sentinel
        assert session.snapshot().closed
        assert session.snapshot().raw_cpu_clock == 12
        assert game.mb.cpu.retired_instructions == 1
        assert game.frame_count == 0
        assert ("post-tick",) not in events
        assert channel.closed
        assert game.mb.serial.backend is original_backend
        assert game.mb.execution_before is None and game.mb.execution_after is None
        assert not any(
            isinstance(m, Progress) and m.settled_half_cycles >= 24 for m in channel.sent
        )


def test_source_recursive_public_tick_rejected_without_extra_retirement(
    session_type, source_lifecycle_game
):
    game, _ = source_lifecycle_game
    with attached(session_type, game) as (session, _):
        attempts = []

        def recurse(clock):
            attempts.append(game.mb.cpu.retired_instructions)
            session.tick(1, render=False, sound=False)

        game.mb.timer.tick = recurse
        with pytest.raises(RuntimeError, match="recursive"):
            session.tick(1, render=False, sound=False)
        assert attempts == [1]
        assert game.mb.cpu.retired_instructions == 1
        assert session.snapshot().closed
        assert game.mb.execution_before is None and game.mb.execution_after is None


def test_direct_public_tick_fails_closed_before_cpu(session_type, game):
    with attached(session_type, game) as (session, channel):
        before = (game.mb.cpu.cycles, game.mb.cpu.retired_instructions)
        with pytest.raises(RuntimeError, match="unowned"):
            game.tick(1, render=False, sound=False)
        assert (game.mb.cpu.cycles, game.mb.cpu.retired_instructions) == before
        assert session.snapshot().closed
        assert channel.closed
        assert game.mb.execution_before is not None
        with pytest.raises(ChannelClosed):
            session.tick(1, render=False, sound=False)
        assert (game.mb.cpu.cycles, game.mb.cpu.retired_instructions) == before


def test_source_held_rearm_cpu_keeps_pending_delivery(session_type, source_lifecycle_game):
    game, _ = source_lifecycle_game
    channel = ScriptedChannel([EdgeRequest(1, 0, 0, 1), EmissionComplete(0, 1)])
    with attached(session_type, game, channel) as (session, _):
        reports = []
        original = game.mb.timer.tick

        def observe(clock):
            snapshot = session.snapshot()
            reports.append(
                (
                    game.mb.cpu.retired_instructions,
                    snapshot.pending_delivery,
                    snapshot.active_episode,
                )
            )
            return original(clock)

        game.mb.timer.tick = observe
        session.tick(1, render=False, sound=False)
        assert reports == [(1, True, True)]
        assert session.snapshot().pending_delivery
        assert not any(isinstance(m, EdgeResponse) for m in channel.sent)


@pytest.mark.parametrize("nops", [126, 127], ids=["inside-IO", "outer-dispatch"])
def test_source_stop_edge_mapping_and_no_premature_settlement(
    session_type, source_lifecycle_game, nops
):
    game, _ = source_lifecycle_game
    board, core = game.mb, game.mb.serial
    board.cgb = True
    board.key1 = 1
    code = [0x00] * nops + [0x10, 0x00, 0xE0, 0x01, 0x18, 0xFE]
    game._test_memory[0xC000 : 0xC000 + len(code)] = bytes(code)
    memory_write = board.setitem

    def write(address, value):
        if address in (0xFF01, 0xFF02):
            return board.setitem_io_ports(address, value)
        return memory_write(address, value)

    board.setitem = write
    board.lcd.tick = lambda clock: setattr(board.lcd, "frame_done", clock >= 528) or 0
    channel = ScriptedChannel()
    observations = []
    with attached(session_type, game, channel) as (session, _):
        core.set_SB(0x80)
        core.set_SC(0x81)
        assert core.clock_target - core.clock == 512  # Preserve hardware period.

        def reply(message):
            if isinstance(message, EdgeRequest):
                observations.append(
                    (message, session.snapshot().pending_permit, board.cpu.retired_instructions)
                )
                # All completed emission prefixes precede this just-emitted edge.
                assert all(
                    not isinstance(prior, EmissionComplete)
                    or prior.through_half_cycle < message.scheduled_half_cycle
                    for prior in channel.sent
                )
                channel.messages.append(
                    EdgeResponse(message.edge_id, message.scheduled_half_cycle, 1)
                )
            elif isinstance(message, Progress) and session._in_edge:
                assert message.settled_half_cycles < 512 + nops * 4

        channel.on_send = reply
        session.tick(1, render=False, sound=False)
        assert len(observations) == 1
        message, pending, retired = observations[0]
        assert message.scheduled_half_cycle == 512 + nops * 4
        assert message.observed_half_cycle == message.scheduled_half_cycle
        assert pending is (nops == 126)
        assert retired == nops + 1  # STOP retired; unfinished LDH must not count.
        assert core.clock_target == 1024  # 512-cycle cadence survives STOP.


def test_source_cross_thread_close_does_not_detach_live_owner(session_type, source_lifecycle_game):
    game, _ = source_lifecycle_game
    with attached(session_type, game) as (session, _):
        errors = []

        def foreign_close():
            try:
                session.close()
            except RuntimeError as exc:
                errors.append(exc)

        def close_during_device(clock):
            worker = threading.Thread(target=foreign_close, daemon=True)
            worker.start()
            worker.join(2)
            assert not worker.is_alive()
            assert game.mb.execution_before is not None
            assert game.mb.serial.backend is session
            return 0

        game.mb.timer.tick = close_during_device
        with pytest.raises(ChannelClosed):
            session.tick(1, render=False, sound=False)
        assert all("attaching thread" in str(error) for error in errors)
        assert game.mb.cpu.retired_instructions == 1
        assert game.mb.execution_before is None


def test_source_primary_failure_survives_foreign_registration_cleanup(
    session_type, source_lifecycle_game
):
    game, _ = source_lifecycle_game
    channel = ScriptedChannel()
    session = session_type(channel, rearm_budget=64, rearm_instruction_cap=16, max_edge_lateness=64)
    session.attach(game, deadline=time.monotonic() + 1)
    sentinel = RuntimeError("primary device failure")
    foreign = object()

    def fail(clock):
        game.mb.serial.backend = foreign
        raise sentinel

    game.mb.timer.tick = fail
    try:
        with pytest.raises(RuntimeError) as caught:
            session.tick(1, render=False, sound=False)
        assert caught.value is sentinel
        assert game.mb.serial.backend is foreign
        assert game.mb.execution_before is not None
        assert session.snapshot().closed
    finally:
        # Test owns the injected foreign identity; restore only that injection
        # so owner cleanup can release its original registration afterwards.
        if game.mb.serial.backend is foreign:
            game.mb.serial.backend = session
        session.close()
        channel.close()


@contextmanager
def control_session(session_type, game, channel=None, *, startup=None, capacity=64):
    channel = channel if channel is not None else ScriptedChannel(revision=3)
    session = session_type(
        channel,
        rearm_budget=4096,
        rearm_instruction_cap=1024,
        max_edge_lateness=4096,
        quantum_cycles=100000,
        operation_timeout=1,
        inbound_capacity=capacity,
    )
    try:
        session.attach(game, deadline=time.monotonic() + 2, startup_internal_clock=startup)
        yield session, channel
    finally:
        session.close()
        channel.close()


@pytest.mark.parametrize("internal", [False, True])
@pytest.mark.parametrize("old_sb", [0x00, 0xA5, 0xFF])
def test_v3_explicit_startup_accepts_generic_fully_armed_external(
    session_type, game, internal, old_sb
):
    core = game.mb.serial
    core.set_SB(old_sb)
    core.set_SC(0x80)
    before = (game.mb.cpu.cycles, game.mb.cpu.retired_instructions)
    with control_session(session_type, game, startup=internal) as (session, _):
        expected_sb = 1 if internal else 2
        assert expected_sb == core.SB
        assert core.transfer_enabled and bool(core.internal_clock) is internal
        assert core._bits_remaining == 8
        assert (game.mb.cpu.cycles, game.mb.cpu.retired_instructions) == before
        writes = session.controls_snapshot()["bootstrap_writes"]
        assert writes == 3
        session.service_controls(deadline=time.monotonic() + 1)
        assert session.controls_snapshot()["bootstrap_writes"] == writes
        assert (game.mb.cpu.cycles, game.mb.cpu.retired_instructions) == before


@pytest.mark.parametrize("startup", [0, 1, "internal"])
def test_v3_startup_flag_rejects_non_boolean(session_type, game, startup):
    original = game.mb.serial.backend
    with pytest.raises(TypeError), control_session(session_type, game, startup=startup):
        pytest.fail("non-bool startup admitted")
    assert game.mb.serial.backend is original
    assert game.mb.execution_before is None
    assert game.mb.cpu.retired_instructions == 0


@pytest.mark.parametrize("armed_kind", ["partial-external", "active-internal"])
def test_v3_startup_rejects_inflight_transfer_transactionally(session_type, game, armed_kind):
    core = game.mb.serial
    core.set_SB(0xA5)
    core.set_SC(0x81 if armed_kind == "active-internal" else 0x80)
    if armed_kind == "partial-external":
        core.apply_external_edge(1)
    before = (core.SB, core.SC, core._bits_remaining, core.backend)
    with pytest.raises(RuntimeError), control_session(session_type, game, startup=True):
        pytest.fail("inflight startup admitted")
    assert (core.SB, core.SC, core._bits_remaining, core.backend) == before
    assert game.mb.cpu.retired_instructions == 0


def test_v3_sync_passive_consuming_and_never_bootstraps(session_type, game):
    channel = ScriptedChannel([Sync(7), Sync(9)], revision=3)
    core = game.mb.serial
    before = (game.mb.cpu.cycles, game.mb.cpu.retired_instructions, core.SB, core.SC)
    with control_session(session_type, game, channel) as (session, _):
        deadline = time.monotonic() + 1
        session.service_controls(deadline=deadline)
        assert session.poll_peer_sync(9) is True
        assert session.poll_peer_sync(9) is False
        assert session.poll_peer_sync(7) is True
        session.announce_sync(3, deadline=deadline)
        assert Sync(3) in channel.sent
        assert session.controls_snapshot()["bootstrap_writes"] == 0
        assert (game.mb.cpu.cycles, game.mb.cpu.retired_instructions, core.SB, core.SC) == before


def test_v3_fence_ack_waits_for_contiguous_applied_prefix(session_type, game):
    # IDs are receive order, not timestamp order: completing edge 2 alone
    # must not acknowledge a fence through 2 while edge 1 is still future.
    channel = NoFurtherEdgesChannel(
        [
            EdgeRequest(1, 100, 100, 1),
            EdgeRequest(2, 0, 0, 0),
            EmissionComplete(100, 2),
            Fence(1, 2),
        ],
        revision=3,
    )
    with control_session(session_type, game, channel) as (session, _):
        game.mb.serial.set_SB(0)
        game.mb.serial.set_SC(0x80)
        session.service_controls(deadline=time.monotonic() + 1)
        assert [m.edge_id for m in channel.sent if isinstance(m, EdgeResponse)] == [2]
        assert session.controls_snapshot()["completed_edge_prefix"] == 0
        assert not any(isinstance(m, FenceAck) for m in channel.sent)
        session.tick(1, render=False, sound=False)
        assert [m.edge_id for m in channel.sent if isinstance(m, EdgeResponse)] == [2, 1]
        assert [m for m in channel.sent if isinstance(m, FenceAck)] == [FenceAck(1, 2)]
        assert session.controls_snapshot()["completed_edge_prefix"] == 2


def test_v3_fence_ack_after_eighth_response_and_irq(session_type, game):
    channel = ScriptedChannel(
        [*(EdgeRequest(i, 0, 0, 1) for i in range(1, 9)), EmissionComplete(0, 8), Fence(1, 8)],
        revision=3,
    )
    with control_session(session_type, game, channel) as (session, _):
        game.mb.serial.set_SB(0)
        game.mb.serial.set_SC(0x80)
        observations = []

        def observe(message):
            if isinstance(message, (EdgeResponse, FenceAck)):
                observations.append((message, game.memory[0xFF0F] & 8))
            if isinstance(message, FenceAck):
                assert EdgeResponse(8, 0, 0) in channel.sent
                assert game.memory[0xFF0F] & 8
                assert not session.snapshot().pending_delivery

        channel.on_send = observe
        before = (game.mb.cpu.cycles, game.mb.cpu.retired_instructions)
        session.service_controls(deadline=time.monotonic() + 1)
        assert len(observations) == 9
        assert observations[-2][0].edge_id == 8 and observations[-2][1] == 0
        assert observations[-1] == (FenceAck(1, 8), 8)
        assert (game.mb.cpu.cycles, game.mb.cpu.retired_instructions) == before


def test_v3_on_edge_stages_controls_until_safe_owner_boundary(session_type, game):
    # Initial serial arm is explicit local test setup; remote Sync never arms.
    code = [0x00] * 127 + [0xF0, 0x01, 0x18, 0xFE]
    game.memory[0xC000 : 0xC000 + len(code)] = code
    channel = NoFurtherEdgesChannel(revision=3)
    with control_session(session_type, game, channel) as (session, _):
        core = game.mb.serial
        core.set_SB(0xA5)
        core.set_SC(0x81)
        in_edge = []

        def reply(message):
            if isinstance(message, EdgeRequest):
                in_edge.append(session.snapshot().pending_permit)
                if message.edge_id == 1:
                    channel.messages.extend([Sync(5), Fence(1, 0)])
                channel.messages.append(
                    EdgeResponse(message.edge_id, message.scheduled_half_cycle, 1)
                )
            elif isinstance(message, FenceAck):
                assert not session._in_edge
                assert not session.snapshot().pending_permit

        channel.on_send = reply
        session.tick(1, render=False, sound=False)
        assert in_edge[0] is True
        assert session._applied == 0
        assert session.poll_peer_sync(5) is True
        assert [m for m in channel.sent if isinstance(m, FenceAck)] == [FenceAck(1, 0)]


def test_v3_start_fence_forces_emission_and_ack_poll_consumes(session_type, game):
    with control_session(session_type, game) as (session, channel):
        before = (game.mb.cpu.cycles, game.mb.cpu.retired_instructions)
        deadline = time.monotonic() + 1
        sent_before = len(channel.send_deadlines)
        fence_id = session.start_fence(deadline=deadline)
        fence = Fence(fence_id, 0)
        assert fence in channel.sent
        position = channel.sent.index(fence)
        assert EmissionComplete(0, 0) in channel.sent[:position]
        assert session.poll_fence(fence_id) is False
        channel.messages.append(FenceAck(fence_id, 0))
        session.service_controls(deadline=deadline)
        assert session.poll_fence(fence_id) is True
        assert session.poll_fence(fence_id) is False
        assert all(value <= deadline for value in channel.send_deadlines[sent_before:])
        assert (game.mb.cpu.cycles, game.mb.cpu.retired_instructions) == before


@pytest.mark.parametrize("operation", ["service", "fence", "sync"])
def test_v3_expired_control_deadline_cannot_renew_or_execute(session_type, game, operation):
    original_backend = game.mb.serial.backend
    with control_session(session_type, game) as (session, channel):
        deadline = time.monotonic() - 1
        before = (game.mb.cpu.cycles, game.mb.cpu.retired_instructions)
        sent = list(channel.sent)
        with pytest.raises(DeadlineExceeded):
            if operation == "service":
                session.service_controls(deadline=deadline)
            elif operation == "fence":
                session.start_fence(deadline=deadline)
            else:
                session.announce_sync(1, deadline=deadline)
        assert channel.sent == sent
        assert (game.mb.cpu.cycles, game.mb.cpu.retired_instructions) == before
        assert channel.closed and session.snapshot().closed
        assert game.mb.serial.backend is original_backend
        assert game.mb.execution_before is game.mb.execution_after is None


def test_v3_passive_control_poll_is_bounded_under_continuous_input(session_type, game):
    channel = ScriptedChannel(revision=3)
    with control_session(session_type, game, channel) as (session, _):
        channel.messages.clear()
        channel.on_poll = lambda: channel.messages.append(Sync(3))
        polls = channel.polls
        before = (game.mb.cpu.cycles, game.mb.cpu.retired_instructions)
        session.service_controls(deadline=time.monotonic() + 1)
        assert 0 < channel.polls - polls <= 64
        assert (game.mb.cpu.cycles, game.mb.cpu.retired_instructions) == before


def test_v3_control_cancel_after_poll_never_sends_fence_ack(session_type, game):
    channel = ScriptedChannel([Fence(1, 0)], revision=3)
    with control_session(session_type, game, channel) as (session, _):
        before = (game.mb.cpu.cycles, game.mb.cpu.retired_instructions)
        channel.on_poll = session.cancel
        with pytest.raises(Cancelled):
            session.service_controls(deadline=time.monotonic() + 1)
        assert not any(isinstance(message, FenceAck) for message in channel.sent)
        assert session.snapshot().cancelled
        assert (game.mb.cpu.cycles, game.mb.cpu.retired_instructions) == before


def test_v3_foreign_controls_snapshot_rejects_without_mutation(session_type, game):
    with control_session(session_type, game) as (session, _):
        before = session.controls_snapshot()
        errors = []

        def foreign():
            try:
                session.controls_snapshot()
            except RuntimeError as exc:
                errors.append(exc)

        worker = threading.Thread(target=foreign, daemon=True)
        worker.start()
        worker.join(2)
        assert not worker.is_alive()
        assert len(errors) == 1
        assert session.controls_snapshot() == before
        assert not session.snapshot().closed
        assert game.mb.cpu.retired_instructions == 0


def test_v3_close_during_fence_send_detaches_only_after_control_unwinds(session_type, game):
    with control_session(session_type, game) as (session, channel):
        observations = []

        def close_during_send(message):
            if isinstance(message, Fence):
                try:
                    session.close()
                finally:
                    observations.append(
                        (game.mb.serial.backend is session, game.mb.execution_before is not None)
                    )

        channel.on_send = close_during_send
        with pytest.raises(RuntimeError):
            session.start_fence(deadline=time.monotonic() + 1)
        assert observations == [(True, True)]
        assert session.snapshot().closed
        assert game.mb.execution_before is None
        assert game.mb.serial.backend is not session
        assert game.mb.cpu.retired_instructions == 0


@pytest.mark.parametrize("boundary", ["credit-wait", "edge-wait", "zero-work-return"])
def test_external_cancel_at_real_execution_boundary(session_type, game, boundary):
    external = threading.Event()
    entered = threading.Event()
    worker_errors = []
    channel = ScriptedChannel(revision=3)
    backend = game.mb.serial.backend
    if boundary == "edge-wait":
        # SC arms at raw32; the first edge falls inside LDH at raw544.
        code = [0x3E, 0xA5, 0xE0, 0x01, 0x3E, 0x81, 0xE0, 0x02]
        code += [0x00] * 125 + [0xF0, 0x01, 0x18, 0xFE]
        game.memory[0xC000 : 0xC000 + len(code)] = code

    def cancel_from_peer():
        if not entered.wait(3):
            worker_errors.append("owner did not reach the requested boundary")
        external.set()

    worker = threading.Thread(target=cancel_from_peer, daemon=True)
    session = session_type(
        channel,
        rearm_budget=4096,
        rearm_instruction_cap=1024,
        max_edge_lateness=4096,
        quantum_cycles=24 if boundary == "credit-wait" else 100000,
        operation_timeout=5,
        cancel_event=external,
    )
    if boundary == "zero-work-return":
        public_tick = game.tick

        def cancel_after_actual_public_return(*args, **kwargs):
            result = public_tick(*args, **kwargs)
            entered.set()
            assert external.wait(3)
            return result

        game.tick = cancel_after_actual_public_return
    else:
        receive = channel.receive

        def observe_wait(*, deadline, cancel_event=None):
            assert game.mb.cpu.retired_instructions > 0
            assert session.snapshot().pending_permit is (boundary == "edge-wait")
            entered.set()
            return receive(deadline=deadline, cancel_event=cancel_event)

        channel.receive = observe_wait
    try:
        session.attach(game, deadline=time.monotonic() + 1)
        worker.start()
        with pytest.raises(Cancelled):
            session.tick(0 if boundary == "zero-work-return" else 1, render=False, sound=False)
        worker.join(3)
        assert not worker.is_alive() and not worker_errors
        assert session.snapshot().cancelled
        assert game.mb.execution_before is None
        assert game.mb.serial.backend is backend
        assert channel.closed
        if boundary == "zero-work-return":
            assert game.mb.cpu.retired_instructions == game.mb.cpu.cycles == 0
        elif boundary == "edge-wait":
            assert game.mb.cpu.retired_instructions == 129
            assert game.mb.cpu.cycles == 544
            assert session.snapshot().raw_cpu_clock == 544
        else:
            assert game.mb.cpu.retired_instructions == 1
            assert game.mb.cpu.cycles == 12
    finally:
        external.set()
        entered.set()
        session.close()
        channel.close()
        if worker.ident is not None:
            worker.join(3)


@contextmanager
def real_v3_channels():
    """Real framed socket transport; the peer supplies no invented CPU credit."""
    left, right = socket.socketpair()
    channel = TimedWireChannel(left, epoch=b"timed-test-epoch", revision=3)
    peer = TimedWireChannel(right, epoch=channel.epoch, revision=3)
    failures = []

    def handshake():
        try:
            peer.handshake(deadline=time.monotonic() + 2)
        except BaseException as exc:  # noqa: BLE001 - Preserve worker failures.
            failures.append(exc)

    worker = threading.Thread(target=handshake, daemon=True)
    worker.start()
    try:
        channel.handshake(deadline=time.monotonic() + 2)
        worker.join(2)
        assert not worker.is_alive()
        assert not failures
        yield channel, peer
    finally:
        channel.close()
        peer.close()
        worker.join(2)
        assert not worker.is_alive()
        assert not failures


@pytest.mark.parametrize("boundary", ["credit-wait", "edge-wait"])
def test_real_v3_external_cancel_interrupts_active_receive(session_type, game, boundary):
    external = threading.Event()
    entered = threading.Event()
    failures = []
    original_backend = game.mb.serial.backend
    if boundary == "edge-wait":
        # Authored instructions arm SC at raw32 and encounter the edge in LDH.
        code = [0x3E, 0xA5, 0xE0, 0x01, 0x3E, 0x81, 0xE0, 0x02]
        code += [0x00] * 125 + [0xF0, 0x01, 0x18, 0xFE]
        game.memory[0xC000 : 0xC000 + len(code)] = code
    with real_v3_channels() as (channel, peer):
        session = session_type(
            channel,
            rearm_budget=4096,
            rearm_instruction_cap=1024,
            max_edge_lateness=4096,
            quantum_cycles=24 if boundary == "credit-wait" else 100000,
            operation_timeout=1,
            cancel_event=external,
        )
        receive = channel.receive

        def observe_receive(*, deadline, cancel_event=None):
            if game.mb.cpu.retired_instructions:
                assert session._active
                assert session.snapshot().pending_permit is (boundary == "edge-wait")
                entered.set()
            return receive(deadline=deadline, cancel_event=cancel_event)

        channel.receive = observe_receive

        def cancel_wait():
            if not entered.wait(2):
                failures.append("owner never entered active receive")
            external.set()

        worker = threading.Thread(target=cancel_wait, daemon=True)
        try:
            session.attach(game, deadline=time.monotonic() + 2)
            # The peer has executed no CPU work. Zero is its truthful anchor.
            peer.send(Progress(0), deadline=time.monotonic() + 1)
            worker.start()
            with pytest.raises(Cancelled):
                session.tick(1, render=False, sound=False)
            worker.join(2)
            assert not worker.is_alive() and not failures
            assert entered.is_set()
            assert game.mb.cpu.retired_instructions == (1 if boundary == "credit-wait" else 129)
            assert game.mb.cpu.cycles == (12 if boundary == "credit-wait" else 544)
            assert session.snapshot().cancelled
            assert channel.closed
            assert game.mb.serial.backend is original_backend
            assert game.mb.execution_before is game.mb.execution_after is None
        finally:
            external.set()
            entered.set()
            session.close()
            if worker.ident is not None:
                worker.join(2)


@pytest.mark.parametrize("armed,fail_after", [(False, 1), (True, 1), (True, 2)])
def test_real_v3_startup_failure_preserves_native_writes(session_type, game, armed, fail_after):
    """Inject registration failure between real setters, never replace Serial."""
    core = game.mb.serial
    core.set_SB(0xA5)
    if armed:
        core.set_SC(0x80)
    original_backend = core.backend
    sentinel = RuntimeError("registration failure after actual startup write")
    with real_v3_channels() as (channel, _):
        session = session_type(
            channel,
            rearm_budget=4096,
            rearm_instruction_cap=1024,
            max_edge_lateness=4096,
        )
        verify = session._verify_registration
        observations = []

        def fail_between_setters():
            verify()
            observations.append((core.SB, core.SC, len(session._bootstrap_writes)))
            assert game.mb.execution_before is not None
            assert game.mb.execution_after is not None
            assert core.backend is session
            assert game.mb.cpu.cycles == game.mb.cpu.retired_instructions == 0
            if len(session._bootstrap_writes) == fail_after:
                raise sentinel

        session._verify_registration = fail_between_setters
        try:
            with pytest.raises(RuntimeError) as caught:
                session.attach(game, deadline=time.monotonic() + 2, startup_internal_clock=True)
            assert caught.value is sentinel
            assert len(session._bootstrap_writes) == fail_after
            assert observations[-1] == (core.SB, core.SC, fail_after)
            assert core.SB == (0xA5 if armed and fail_after == 1 else 1)
            assert not core.transfer_enabled
            assert game.mb.cpu.cycles == game.mb.cpu.retired_instructions == 0
            assert core.backend is original_backend
            assert game.mb.execution_before is game.mb.execution_after is None
            assert channel.closed and session.snapshot().closed
            with pytest.raises(RuntimeError, match="epoch cannot be reused"):
                session.attach(game, deadline=time.monotonic() + 1)
            assert len(session._bootstrap_writes) == fail_after
        finally:
            session.close()


def test_real_v3_poll_returning_after_deadline_never_applies_edge(session_type, game):
    original_backend = game.mb.serial.backend
    with (
        real_v3_channels() as (channel, peer),
        control_session(session_type, game, channel, startup=False) as (session, _),
    ):
        core = game.mb.serial
        before = (core.SB, core.SC, core._bits_remaining)
        deadline = time.monotonic() + 0.1
        peer.send(Progress(0), deadline=deadline)
        peer.send(EdgeRequest(1, 0, 0, 1), deadline=deadline)
        peer.send(EmissionComplete(0, 1), deadline=deadline)
        poll = channel.poll
        delayed = []

        def delayed_wire_poll():
            message = poll()
            if isinstance(message, EmissionComplete):
                # Hold a genuinely decoded wire result across the absolute
                # deadline. Event.wait uses real wall time, not a fake clock.
                sleeper = threading.Event()
                while time.monotonic() < deadline:
                    sleeper.wait(max(0, deadline - time.monotonic()))
                delayed.append(message)
            return message

        channel.poll = delayed_wire_poll
        with pytest.raises(DeadlineExceeded):
            while time.monotonic() < deadline:
                session.service_controls(deadline=deadline)
            session.service_controls(deadline=deadline)
        assert delayed == [EmissionComplete(0, 1)]
        assert (core.SB, core.SC, core._bits_remaining) == before
        assert session._applied == 0
        assert game.mb.cpu.cycles == game.mb.cpu.retired_instructions == 0
        assert session.snapshot().closed and channel.closed
        assert core.backend is original_backend
        assert game.mb.execution_before is game.mb.execution_after is None


@pytest.mark.parametrize("revision", [2, 3])
def test_timed_attach_always_enforces_completeness(session_type, game, revision):
    with attached(session_type, game, ScriptedChannel(revision=revision)) as (session, _):
        assert session._coordinator._enforce_completeness is True
        assert session._coordinator_watermark == -1
        assert game.mb.cpu.cycles == game.mb.cpu.retired_instructions == 0


def test_source_completeness_progress_due_forces_prefix_below_threshold(
    session_type, source_lifecycle_game
):
    """Synthetic publication bookkeeping at a genuinely retired CPU frontier.

    Only cached send positions are arranged; CPU/serial clocks and coordinator
    time stay untouched. The known no-edge script makes both prefixes valid.
    Actual paired edge/coalescing coverage remains in the real-wire test.
    """
    game, _ = source_lifecycle_game
    board = game.mb
    board.lcd.tick = lambda clock: setattr(board.lcd, "frame_done", clock >= 312) or 0
    channel = NoFurtherEdgesChannel(revision=3)
    # Synthetic peer explicitly reports this settled interval; not a runtime
    # liveness assertion or a future completeness watermark.
    channel.messages.append(Progress(476))
    with attached(session_type, game, channel, quantum_cycles=256) as (session, _):
        session.tick(1, render=False, sound=False)
        assert board.cpu.cycles == 312 and board.cpu.retired_instructions == 26
        assert board.serial.last_cycles == 312
        before = session.snapshot()
        assert before.local_half_cycles == 624
        session._sent_progress = 480
        session._sent_watermark = 512
        assert 624 - 480 >= session._threshold == 128
        assert 0 < 624 - 512 < session._threshold
        sent_before = len(channel.sent)
        session._publish()
        assert channel.sent[sent_before:] == [EmissionComplete(624, 0), Progress(624)]
        assert session.snapshot() == before
        assert board.cpu.cycles == 312 and board.cpu.retired_instructions == 26


def test_control_completeness_bootstrap_edge_zero_waits_for_prefix(session_type, game):
    """Synthetic edge-zero control script; actual Serial, no CPU clock writes."""
    channel = ScriptedChannel([EdgeRequest(1, 0, 0, 1)], revision=3)
    with attached(session_type, game, channel, max_edge_lateness=32) as (session, _):
        core = game.mb.serial
        core.set_SB(0)
        core.set_SC(0x80)
        before = (core.SB, core.SC, core._bits_remaining)
        session.service_controls(deadline=time.monotonic() + 1)
        assert session._coordinator_watermark == -1
        assert (core.SB, core.SC, core._bits_remaining) == before
        assert not any(isinstance(m, EdgeResponse) for m in channel.sent)
        channel.messages.append(EmissionComplete(0, 1))
        session.service_controls(deadline=time.monotonic() + 1)
        assert [m for m in channel.sent if isinstance(m, EdgeResponse)] == [EdgeResponse(1, 0, 0)]
        assert core._bits_remaining == 7
        assert session.controls_snapshot()["completed_edge_prefix"] == 1
        assert not game.memory[0xFF0F] & 8
        assert game.mb.cpu.cycles == game.mb.cpu.retired_instructions == 0


def test_control_completeness_watermark_only_pump_wakes_without_receive(session_type, game):
    """Synthetic passive prefix advance must be noticed by the initial pump."""
    channel = ScriptedChannel([Progress(476)], revision=3)
    with attached(session_type, game, channel, max_edge_lateness=32) as (session, _):
        session.service_controls(deadline=time.monotonic() + 1)
        before = session.snapshot()
        assert before.peer_half_cycles == 476
        assert session._coordinator_watermark == -1  # Progress is not completeness.
        channel.messages.append(EmissionComplete(476, 0))

        def unexpected_receive(**kwargs):
            pytest.fail("watermark-only initial pump must return without blocking receive")

        channel.receive = unexpected_receive
        session._wait_for_progress(0.2)
        after = session.snapshot()
        assert after.peer_half_cycles == before.peer_half_cycles
        assert session._coordinator_watermark == 476
        assert after.local_half_cycles == before.local_half_cycles == 0
        assert game.mb.cpu.cycles == game.mb.cpu.retired_instructions == 0


def test_source_completeness_withheld_edge_then_prefix_keeps_cpu_bound(
    session_type, source_lifecycle_game
):
    """Synthetic P/W476 and edge512 script; real JR and Serial execution.

    This controls receipt ordering, not peer runtime progress. P476 alone
    cannot certify W476, and receiving an edge alone cannot raise W+L540.
    """
    game, _ = source_lifecycle_game
    board, core = game.mb, game.mb.serial
    board.lcd.tick = lambda clock: setattr(board.lcd, "frame_done", clock >= 264) or 0
    channel = ScriptedChannel([Progress(476), EmissionComplete(476, 0)], revision=3)
    with attached(session_type, game, channel, quantum_cycles=256, max_edge_lateness=32) as (
        session,
        _,
    ):
        core.set_SB(0)
        core.set_SC(0x80)
        arrivals = deque([EdgeRequest(1, 512, 512, 1), EmissionComplete(512, 1)])
        waits = []

        def receive(*, deadline, cancel_event=None):
            channel._check(cancel_event)
            assert time.monotonic() < deadline
            assert board.cpu.cycles == 252
            assert board.cpu.retired_instructions == 21
            assert session.snapshot().local_half_cycles == 504
            assert session._coordinator_watermark == 476
            assert not session.snapshot().pending_permit
            assert core._bits_remaining == 8
            assert not any(isinstance(m, EdgeResponse) for m in channel.sent)
            assert arrivals, "CPU failed to resume after the complete prefix"
            message = arrivals.popleft()
            waits.append(message)
            return message

        channel.receive = receive
        session.tick(1, render=False, sound=False)
        assert waits == [EdgeRequest(1, 512, 512, 1), EmissionComplete(512, 1)]
        assert board.cpu.cycles == 264 and board.cpu.retired_instructions == 22
        assert session.snapshot().local_half_cycles == 528 <= 512 + 64
        assert session._coordinator_watermark == 512
        assert [m for m in channel.sent if isinstance(m, EdgeResponse)] == [EdgeResponse(1, 528, 0)]
        assert core._bits_remaining == 7
        assert not board.cpu.interrupts_flag_register & 8
        assert session.controls_snapshot()["completed_edge_prefix"] == 1


def test_source_completeness_stop_preserves_actual_speed_interval(
    session_type, source_lifecycle_game
):
    """Authored STOP, real CPU/Serial, synthetic frame boundary and EC0 peer."""
    game, _ = source_lifecycle_game
    board = game.mb
    board.cgb = True
    board.key1 = 1
    game._test_memory[0xC000:0xC004] = bytes([0x10, 0, 0x18, 0xFE])
    channel = ScriptedChannel([EmissionComplete(0, 0)], revision=3)
    with attached(session_type, game, channel, quantum_cycles=256, max_edge_lateness=32) as (
        session,
        _,
    ):
        session.tick(1, render=False, sound=False)
        assert board.cpu.retired_instructions == 1
        assert board.cpu.cycles == 4
        assert board.double_speed and board.speed_transition_count == 1
        boundary = board.speed_transition_clock
        assert session.snapshot().local_half_cycles == boundary * 2 + (4 - boundary)
        assert session.snapshot().raw_cpu_clock == 4
        assert session.snapshot().local_half_cycles <= 64
        assert not session.snapshot().pending_permit
        assert not any(isinstance(m, EdgeRequest) for m in channel.sent)


def test_source_completeness_hdma_412_explicit_nonprogress(session_type, source_lifecycle_game):
    """Real source HDMA selection, CPU and Serial; synthetic inert LCD HBlank.

    L64 cannot fit the atomic 206 CPU cycles / 412 half-cycles. A partial
    reservation must never execute DMA, retire CPU work, or advance clocks.
    """
    game, _ = source_lifecycle_game
    board = game.mb
    board.cgb_mode = True
    board_module = _source_module("core/mb.py", "pyboy.core._timed_hdma_mb")
    board.hdma = board_module.HDMA()
    board.hdma.hdma1 = 0xC0
    board.hdma.set_hdma5(0x80, board)
    assert board.hdma.transfer_active and board.lcd._STAT._mode == 0
    channel = ScriptedChannel([Progress(476), EmissionComplete(0, 0)], revision=3)
    original_backend = board.serial.backend
    with attached(session_type, game, channel, quantum_cycles=256, max_edge_lateness=32) as (
        session,
        _,
    ):
        reserve = session._coordinator.reserve
        permits = []

        def observe_reservation(required):
            assert required == 206
            permit = reserve(required)
            if permit is not None:
                permits.append((permit.cpu_cycles, permit.half_cycles))
            return permit

        session._coordinator.reserve = observe_reservation
        waits = []

        def cancel_unfittable_dma(*, deadline, cancel_event=None):
            assert time.monotonic() < deadline
            waits.append((board.cpu.cycles, board.cpu.retired_instructions))
            assert not session.snapshot().pending_permit
            session.cancel()
            channel._check(cancel_event)

        channel.receive = cancel_unfittable_dma
        with pytest.raises(Cancelled):
            session.tick(1, render=False, sound=False)
        assert waits == [(0, 0)]
        assert permits and all(p == (32, 64) for p in permits)
        assert board.cpu.cycles == board.cpu.retired_instructions == 0
        assert session.snapshot().local_half_cycles == session.snapshot().raw_cpu_clock == 0
        assert board.hdma.transfer_active and board.hdma.curr_src == 0xC000
        assert board.hdma.curr_dst == 0x8000
        assert game.frame_count == 0
        assert not any(isinstance(m, Progress) and m.settled_half_cycles for m in channel.sent)
        assert channel.closed and session.snapshot().cancelled
        assert board.serial.backend is original_backend
        assert board.execution_before is board.execution_after is None


def test_control_completeness_cancel_wait_preserves_real_cpu_frontier(session_type, game):
    channel = ScriptedChannel(revision=3)
    with attached(session_type, game, channel, quantum_cycles=256, max_edge_lateness=32) as (
        session,
        _,
    ):
        waits = []

        def cancel_wait(*, deadline, cancel_event=None):
            assert time.monotonic() < deadline
            waits.append((game.mb.cpu.cycles, game.mb.cpu.retired_instructions))
            assert session._coordinator_watermark == -1
            session.cancel()
            channel._check(cancel_event)

        channel.receive = cancel_wait
        with pytest.raises(Cancelled):
            session.tick(1, render=False, sound=False)
        assert waits == [(12, 1)]
        assert game.mb.cpu.cycles == 12 and game.mb.cpu.retired_instructions == 1
        assert session.snapshot().local_half_cycles == 24
        assert session.snapshot().cancelled and channel.closed
        assert game.mb.execution_before is game.mb.execution_after is None
