"""Shared helpers for the split timed-link session test modules.

Split from ``tests/test_timed_link_session.py`` for #134 with no behavior
change; the channel doubles, fixtures, source seam and context managers are
moved verbatim into one module that the split test modules import. The
``session_type``, ``game`` and ``source_lifecycle_game`` pytest fixtures are
re-exported through ``tests/conftest.py`` so the split modules stay
discoverable without import-only F401 shims.
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
    EdgeRequest,
    EmissionComplete,
    Progress,
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
        self.send_calls = []
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
        self.send_calls.append(("send", (message,), deadline, cancel_event))
        self._record_message(message, deadline=deadline, cancel_event=cancel_event)

    def send_complete_progress(self, complete, progress, *, deadline, cancel_event=None):
        """Explicit synthetic batch capability, not two calls to single send.

        This records the API shape and preserves per-message observation hooks;
        real framed batching is exercised separately by the paired channels.
        """
        assert type(complete) is EmissionComplete and type(progress) is Progress
        assert complete.through_half_cycle == progress.settled_half_cycles
        self.send_calls.append(("complete_progress", (complete, progress), deadline, cancel_event))
        self._record_message(complete, deadline=deadline, cancel_event=cancel_event)
        self._record_message(progress, deadline=deadline, cancel_event=cancel_event)

    def _record_message(self, message, *, deadline, cancel_event):
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
