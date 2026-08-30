"""Tests for :class:`PyBoyLinkSession` (milestone 4).

Uses a lightweight fake PyBoy that exposes just ``mb.serial`` and a
``tick`` method — enough to exercise attach/detach plumbing and the
end-to-end byte exchange without loading a real ROM.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
from pokered_harness.link.serial_core import (
    CYCLES_PER_BYTE_DMG,
    NullBackend,
    SerialCore,
)
from pokered_harness.link.serial_coordinator import CoordinatedBackend


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
