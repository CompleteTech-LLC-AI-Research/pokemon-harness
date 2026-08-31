"""Tests for :class:`PyBoyLinkSession` (milestone 4).

Uses a lightweight fake PyBoy that exposes just ``mb.serial`` and a
``tick`` method — enough to exercise attach/detach plumbing and the
end-to-end byte exchange without loading a real ROM.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pokered_harness.link.network_backend import NetworkBackend
from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
from pokered_harness.link.serial_coordinator import CoordinatedBackend
from pokered_harness.link.serial_core import (
    CYCLES_PER_BYTE_DMG,
    SerialCore,
)


class _FakeMB:
    """Stand-in for ``pyboy.mb`` — just needs a swappable ``serial``."""

    def __init__(self, serial):
        self.serial = serial


class _FakePyBoy:
    """Minimal PyBoy-like object for session tests.

    ``tick(n, render)`` advances the installed serial core by
    ``n * CYCLES_PER_BYTE_DMG`` CPU cycles (pretending each "frame" is
    one full-byte transfer period). That lets tests drive serial
    edges without running a real CPU.
    """

    def __init__(self, serial=None):
        self.mb = _FakeMB(serial or _LegacySerialStub())
        self._cycles = 0

    def tick(self, count: int = 1, render: bool = False, sound: bool = False) -> bool:
        self._cycles += count * CYCLES_PER_BYTE_DMG
        ser = self.mb.serial
        if hasattr(ser, "tick"):
            ser.tick(self._cycles)
        return True


class _LegacySerialStub:
    """Looks like PyBoy's legacy Serial for attach() to copy state from."""

    def __init__(self):
        self.SB = 0xFF
        self.SC = 0x00
        self.last_cycles = 0
        self.clock = 0

    def tick(self, _cycles):  # pragma: no cover - never exercised
        return False

    def set_SB(self, value):
        self.SB = value & 0xFF

    def set_SC(self, value):
        self.SC = value & 0xFF


# ---------------------------------------------------------------------------
# attach / detach
# ---------------------------------------------------------------------------


def test_attach_installs_serial_core():
    a = _FakePyBoy()
    link = PyBoyLinkSession.local()
    core = link.attach(a)

    assert isinstance(core, SerialCore)
    assert a.mb.serial is core
    assert link.attached == (a,)
    assert link.paired is False  # only one side; no coordinator yet


def test_attach_preserves_register_state_from_legacy_serial():
    """Mid-game attach must not visibly disturb SB/SC."""
    legacy = _LegacySerialStub()
    legacy.SB = 0x42
    legacy.SC = 0x03
    legacy.last_cycles = 1000
    a = _FakePyBoy(serial=legacy)

    link = PyBoyLinkSession.local()
    core = link.attach(a)

    assert core.SB == 0x42
    assert core.SC == 0x03
    assert core.last_cycles == 1000


def test_second_attach_wires_coordinator():
    a = _FakePyBoy()
    b = _FakePyBoy()
    link = PyBoyLinkSession.local()
    link.attach(a)
    link.attach(b)

    assert link.paired is True
    assert link.coordinator is not None
    assert isinstance(a.mb.serial.backend, CoordinatedBackend)
    assert isinstance(b.mb.serial.backend, CoordinatedBackend)


def test_attach_same_pyboy_twice_raises():
    a = _FakePyBoy()
    link = PyBoyLinkSession.local()
    link.attach(a)
    with pytest.raises(RuntimeError, match="already attached"):
        link.attach(a)


def test_attach_overflow_raises():
    link = PyBoyLinkSession.local()
    link.attach(_FakePyBoy())
    link.attach(_FakePyBoy())
    with pytest.raises(RuntimeError, match="session is full"):
        link.attach(_FakePyBoy())


def test_detach_restores_original_serial():
    legacy_a = _LegacySerialStub()
    a = _FakePyBoy(serial=legacy_a)
    link = PyBoyLinkSession.local()
    link.attach(a)
    assert a.mb.serial is not legacy_a

    link.detach(a)
    assert a.mb.serial is legacy_a
    assert link.attached == ()


def test_detach_tears_down_coordinator():
    a = _FakePyBoy()
    b = _FakePyBoy()
    link = PyBoyLinkSession.local()
    link.attach(a)
    link.attach(b)
    assert link.paired

    link.detach(a)
    assert link.coordinator is None
    # Remaining attached core's backend should no longer be a
    # CoordinatedBackend pointing at a detached peer.
    assert not isinstance(b.mb.serial.backend, CoordinatedBackend)


def test_detach_unknown_pyboy_is_noop():
    link = PyBoyLinkSession.local()
    link.detach(_FakePyBoy())  # never attached; should not raise


def test_detach_all_restores_every_instance():
    legacy_a = _LegacySerialStub()
    legacy_b = _LegacySerialStub()
    a = _FakePyBoy(serial=legacy_a)
    b = _FakePyBoy(serial=legacy_b)
    link = PyBoyLinkSession.local()
    link.attach(a)
    link.attach(b)

    link.detach_all()

    assert a.mb.serial is legacy_a
    assert b.mb.serial is legacy_b
    assert link.attached == ()


def test_detach_all_stops_session_network_backend_workers():
    """The session's terminal cleanup must close both network workers."""
    backend, peer = NetworkBackend.pair()
    pyboy = _FakePyBoy()
    link = PyBoyLinkSession(network_backend=backend)

    try:
        link.attach(pyboy)
        assert backend._reader is not None and backend._reader.is_alive()
        assert backend._edge_worker is not None and backend._edge_worker.is_alive()

        link.detach_all()

        assert not backend.connected
        assert backend._reader is not None and not backend._reader.is_alive()
        assert backend._edge_worker is not None and not backend._edge_worker.is_alive()
        assert link.attached == ()
    finally:
        # The peer is not owned by this session; clean up the test fixture
        # explicitly just as a direct NetworkBackend caller must.
        backend.stop()
        peer.stop()


def test_detach_all_stops_network_backend_when_detach_raises(monkeypatch):
    """Transport shutdown must be unconditional when detaching fails."""
    backend, peer = NetworkBackend.pair()
    link = PyBoyLinkSession(network_backend=backend)
    link.attach(_FakePyBoy())

    def fail_detach(_pyboy):
        raise RuntimeError("synthetic serial restoration failure")

    monkeypatch.setattr(link, "detach", fail_detach)
    try:
        with pytest.raises(RuntimeError, match="synthetic serial restoration"):
            link.detach_all()

        assert not backend.connected
        assert backend._reader is not None and not backend._reader.is_alive()
        assert backend._edge_worker is not None and not backend._edge_worker.is_alive()
    finally:
        backend.stop()
        peer.stop()


# ---------------------------------------------------------------------------
# step(): drives both sides and triggers the coordinated exchange
# ---------------------------------------------------------------------------


def test_step_requires_both_sides_attached():
    link = PyBoyLinkSession.local()
    link.attach(_FakePyBoy())
    with pytest.raises(RuntimeError, match="requires 2 attached"):
        link.step()


def test_step_exchanges_a_full_byte_end_to_end():
    """Integration sanity: arm a byte exchange on the two fake PyBoys,
    call link.step(), and assert both sides received the peer's byte."""
    a = _FakePyBoy()
    b = _FakePyBoy()
    link = PyBoyLinkSession.local()
    core_a = link.attach(a)
    core_b = link.attach(b)

    core_a.set_SB(0xAA)
    core_b.set_SB(0x55)
    core_a.set_SC(0x81)  # master
    core_b.set_SC(0x80)  # slave

    # One "frame" on the fake PyBoy = CYCLES_PER_BYTE_DMG cycles, i.e.
    # a full 8-edge transfer in master-mode.
    link.step(frames=1)

    assert core_a.SB == 0x55
    assert core_b.SB == 0xAA
    assert core_a.transfer_enabled == 0
    assert core_b.transfer_enabled == 0


def test_interleaved_chunk_uses_cpu_cycles_for_variable_length_instructions():
    """A chunk ends on emulated time, not an instruction-count estimate."""

    class _ChunkMB:
        def __init__(self):
            self.cpu = SimpleNamespace(cycles=100)
            self.lcd = SimpleNamespace(frame_done=False)
            self.breakpoint_singlestep = 0

        def tick(self):
            # The second instruction is deliberately longer than the
            # historical ~7-cycle estimate.
            self.cpu.cycles += (4, 20)[self.cpu.cycles != 100]
            return False

    pyboy = SimpleNamespace(mb=_ChunkMB())

    assert PyBoyLinkSession._step_single_step_chunk(pyboy, 24) is False
    assert pyboy.mb.cpu.cycles == 124


class _FrameBoundaryDouble:
    """Small motherboard double for the interleaved frame scheduler."""

    def __init__(
        self,
        frame_boundary: int,
        *,
        start_cycles: int = 0,
        stalled: bool = False,
        speed_shift: int = 0,
    ):
        self._frame_boundary = start_cycles + frame_boundary
        self._next_frame_boundary = self._frame_boundary
        self._stalled = stalled
        self.cpu = SimpleNamespace(cycles=start_cycles)
        self.lcd = SimpleNamespace(
            frame_done=False,
            _cycles_to_frame=frame_boundary,
            speed_shift=speed_shift,
        )
        self.sound = SimpleNamespace(
            disable_sampling=False,
            clear_buffer=lambda: None,
        )
        self.serial = SimpleNamespace(internal_clock=False)
        self.breakpoint_singlestep = 0
        self.ticks_after_boundary = 0

    def tick(self):
        if self._stalled:
            return False
        if self._next_frame_boundary > self._frame_boundary:
            self.ticks_after_boundary += 1
        self.cpu.cycles += 4
        if self.cpu.cycles >= self._next_frame_boundary:
            self.lcd.frame_done = True
            self._next_frame_boundary += 1000
        return False


class _FrameBoundaryPyBoy:
    def __init__(
        self,
        frame_boundary: int,
        *,
        start_cycles: int = 0,
        stalled: bool = False,
        speed_shift: int = 0,
    ):
        self.mb = _FrameBoundaryDouble(
            frame_boundary,
            start_cycles=start_cycles,
            stalled=stalled,
            speed_shift=speed_shift,
        )
        self.events = []
        self.frame_count = 0

    def _handle_events(self, _events):
        return None

    def _post_handle_events(self):
        return None


def test_interleaved_frame_crosses_early_lcd_boundary_to_shared_horizon():
    """An early LCD boundary must not freeze its peer's serial clock."""
    a_start = 1_000_000
    b_start = 20_000_000
    a = _FrameBoundaryPyBoy(20, start_cycles=a_start)
    b = _FrameBoundaryPyBoy(32, start_cycles=b_start)

    PyBoyLinkSession._interleave_one_frame(a, b, chunk_cycles=8)

    assert a.mb.cpu.cycles - a_start == b.mb.cpu.cycles - b_start == 32
    assert a.mb.ticks_after_boundary > 0
    assert a.frame_count == b.frame_count == 1


def test_interleaved_frame_normalizes_cgb_double_speed_cycles():
    """A CGB double-speed CPU must not consume two game frames.

    PyBoy's CPU cycle counter advances twice as quickly in CGB double-speed
    mode, while the LCD and the ROM's DelayFrame cadence remain in the
    normal hardware-time domain. The scheduler therefore scales the raw
    CPU budget per side before choosing its shared horizon.
    """
    a_start = 1_000_000
    b_start = 2_000_000
    a = _FrameBoundaryPyBoy(
        40, start_cycles=a_start, speed_shift=1
    )
    b = _FrameBoundaryPyBoy(
        20, start_cycles=b_start, speed_shift=0
    )

    PyBoyLinkSession._interleave_one_frame(a, b, chunk_cycles=8)

    assert a.mb.cpu.cycles - a_start == 40
    assert b.mb.cpu.cycles - b_start == 20
    assert a.frame_count == b.frame_count == 1


def test_interleaved_frame_timeout_is_bounded_and_clears_singlestep():
    a = _FrameBoundaryPyBoy(8, stalled=True)
    b = _FrameBoundaryPyBoy(8, stalled=True)

    with pytest.raises(TimeoutError, match="shared LCD cycle horizon"):
        PyBoyLinkSession._interleave_one_frame(a, b, chunk_cycles=4)

    assert a.mb.breakpoint_singlestep == b.mb.breakpoint_singlestep == 0
