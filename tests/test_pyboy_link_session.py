"""Tests for :class:`PyBoyLinkSession` (milestone 4).

Uses a lightweight fake PyBoy that exposes just ``mb.serial`` and a
``tick`` method — enough to exercise attach/detach plumbing and the
end-to-end byte exchange without loading a real ROM.
"""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from pokered_harness.link import pyboy_link_session as pyboy_link_session_module
from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
from pokered_harness.link.network_backend import NetworkBackend, NetworkBackendError
from pokered_harness.link.serial_core import (
    CYCLES_PER_BYTE_DMG,
    NullBackend,
    SerialCore,
)
from pokered_harness.link.serial_coordinator import CoordinatedBackend
from pokered_harness.ownership import owner_for


class _FakeMB:
    """Explicit four-cycle instruction clock; one fake frame is one byte period."""

    def __init__(self, serial, owner):
        self.serial = serial
        self.owner = owner
        self.cgb_mode = False
        self.lcd = SimpleNamespace(frame_done=False, disable_renderer=True)
        self.sound = SimpleNamespace(disable_sampling=True, clear_buffer=lambda: None)
        self.breakpoint_singlestep = 0
        self.breakpoint_singlestep_latch = 0

    def tick(self):
        self.owner._cycles += 4
        self.serial.tick(self.owner._cycles)
        self.lcd.frame_done = self.owner._cycles - self.owner._frame_start >= CYCLES_PER_BYTE_DMG
        return True

    def breakpoint_reinject(self):
        pass

    def breakpoint_reached(self):
        return (-1, -1, -1)


class _FakePyBoy:
    """Minimal PyBoy-like object for session tests.

    ``tick(n, render)`` advances the installed serial core by
    ``n * CYCLES_PER_BYTE_DMG`` CPU cycles (pretending each "frame" is
    one full-byte transfer period). That lets tests drive serial
    edges without running a real CPU.
    """

    def __init__(self, serial=None):
        serial = serial or _LegacySerialStub()
        self._cycles = serial.last_cycles
        self._frame_start = self._cycles
        self.mb = _FakeMB(serial, self)
        self.events = []
        self.frame_count = 0

    def _handle_events(self, events):
        self._frame_start = self._cycles

    def _post_handle_events(self):
        pass

    def _handle_hooks(self):
        pass

    def tick(self, count: int = 1, render: bool = False, sound: bool = False) -> bool:
        self._cycles += count * CYCLES_PER_BYTE_DMG
        ser = self.mb.serial
        if hasattr(ser, "tick"):
            ser.tick(self._cycles)
        return True


class _RecordingMemory:
    def __init__(self):
        self.writes = []

    def __setitem__(self, address, value):
        self.writes.append((address, value))


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


class _BackendSetterCountingCore:
    """Valid owner-claim surface with observable backend assignment."""

    backend_failed = False

    def __init__(self):
        self.last_cycles = 0
        self.clock = 0
        self._backend = NullBackend()
        self.backend_writes = []
        self._owner_callback = None
        self._owner_token = None

    @property
    def backend(self):
        return self._backend

    @backend.setter
    def backend(self, value):
        self.backend_writes.append(value)
        self._backend = value

    def set_owner_pump(self, callback, poll=False):
        if self._owner_token is not None:
            raise RuntimeError("serial owner pump is exclusively claimed")
        self._owner_callback = callback

    def claim_owner_pump(self, callback, poll=False):
        if self._owner_callback is not None or self._owner_token is not None:
            raise RuntimeError("serial owner pump already installed or claimed")
        self._owner_token = object()
        self._owner_callback = callback
        return self._owner_token

    def release_owner_pump(self, token):
        if token is not self._owner_token:
            raise RuntimeError("invalid serial owner pump claim token")
        self._owner_token = None
        self._owner_callback = None


class _ProviderSentinel:
    """Weak-referenceable provider used to verify owner release."""

    pass


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


def test_attach_rolls_back_local_coordinator_failure(monkeypatch):
    first_legacy = _LegacySerialStub()
    second_legacy = _LegacySerialStub()
    first = _FakePyBoy(serial=first_legacy)
    second = _FakePyBoy(serial=second_legacy)
    link = PyBoyLinkSession.local()
    link.attach(first)

    def fail_coordinator(*_args, **_kwargs):
        raise RuntimeError("synthetic coordinator failure")

    monkeypatch.setattr(
        pyboy_link_session_module,
        "LockstepCoordinator",
        fail_coordinator,
    )

    with pytest.raises(RuntimeError, match="synthetic coordinator"):
        link.attach(second)

    assert link.attached == (first,)
    assert second.mb.serial is second_legacy
    assert link.coordinator is None


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


def test_network_step_advances_single_attached_pyboy():
    backend, peer = NetworkBackend.pair()
    pyboy = _FakePyBoy(serial=SerialCore())
    link = PyBoyLinkSession(network_backend=backend)

    try:
        link.attach(pyboy)
        link.step(frames=3)

        assert pyboy._cycles == 3 * CYCLES_PER_BYTE_DMG
    finally:
        link.close()
        peer.stop()


@pytest.mark.parametrize("network_is_internal_clock", [False, True])
def test_network_attach_detach_never_writes_role_status(network_is_internal_clock):
    backend, peer = NetworkBackend.pair()
    pyboy = _FakePyBoy(serial=SerialCore())
    pyboy.memory = _RecordingMemory()
    link = PyBoyLinkSession(
        network_backend=backend,
        network_is_internal_clock=network_is_internal_clock,
    )

    try:
        link.attach(pyboy)
        assert pyboy.memory.writes == []

        link.detach(pyboy)
        assert pyboy.memory.writes == []
        link.close()
        assert pyboy.memory.writes == []
    finally:
        link.close()
        peer.stop()


def test_network_attach_rejects_stale_provider_before_backend_or_tick_mutation():
    calls = []
    backend = SimpleNamespace(
        start_receiver=lambda **kwargs: calls.append(("start_receiver", kwargs)),
        stop=lambda: calls.append(("stop", {})),
    )
    core = SerialCore()
    pyboy = _FakePyBoy(serial=core)
    ticks = []
    pyboy.tick = lambda *args, **kwargs: ticks.append((args, kwargs))
    link = PyBoyLinkSession(network_backend=backend)
    prior_backend = core.backend

    try:
        with pytest.raises(NetworkBackendError, match="owner scope"):
            link.attach(pyboy)
        assert link.attached == ()
        assert core.backend is prior_backend
        assert [name for name, _ in calls if name == "start_receiver"] == []
        assert ticks == []
    finally:
        link.close()


@pytest.mark.parametrize("has_scope", [False, True])
def test_network_attach_rejects_legacy_before_promotion_or_backend_write(monkeypatch, has_scope):
    calls = []
    backend = SimpleNamespace(
        start_receiver=lambda **kwargs: calls.append(("start_receiver", kwargs)),
        stop=lambda: calls.append(("stop", {})),
    )
    if has_scope:
        backend.owner_scope = lambda: None
    legacy = _LegacySerialStub()
    pyboy = _FakePyBoy(serial=legacy)
    link = PyBoyLinkSession(network_backend=backend)
    owner = owner_for(pyboy)
    promoted = []
    promote = link._promote_legacy_serial

    def track_promotion(serial):
        promoted.append(serial)
        return promote(serial)

    monkeypatch.setattr(link, "_promote_legacy_serial", track_promotion)

    try:
        message = "exclusive owner pump" if has_scope else "owner scope"
        with pytest.raises(NetworkBackendError, match=message):
            link.attach(pyboy)

        assert link.attached == ()
        assert pyboy.mb.serial is legacy
        assert promoted == []
        assert not hasattr(legacy, "backend")
        assert [name for name, _ in calls if name == "start_receiver"] == []

        replacement = _ProviderSentinel()
        owner.claim_provider(replacement)
        owner.release_provider(replacement)
    finally:
        link.close()


def test_network_attach_rejection_does_not_write_valid_core_backend(monkeypatch):
    calls = []
    backend = SimpleNamespace(
        start_receiver=lambda **kwargs: calls.append(("start_receiver", kwargs)),
        stop=lambda: calls.append(("stop", {})),
    )
    core = _BackendSetterCountingCore()
    pyboy = _FakePyBoy(serial=core)
    ticks = []
    pyboy.tick = lambda *args, **kwargs: ticks.append((args, kwargs))
    link = PyBoyLinkSession(network_backend=backend)
    owner = owner_for(pyboy)
    prior_backend = core.backend

    try:
        with pytest.raises(NetworkBackendError, match="owner scope"):
            link.attach(pyboy)

        assert link.attached == ()
        assert core.backend is prior_backend
        assert core.backend_writes == []
        assert [name for name, _ in calls if name == "start_receiver"] == []
        assert ticks == []

        replacement = _ProviderSentinel()
        owner.claim_provider(replacement)
        owner.release_provider(replacement)
    finally:
        link.close()


def test_network_detach_all_stops_backend_workers():
    backend, peer = NetworkBackend.pair()
    pyboy = _FakePyBoy(serial=SerialCore())
    link = PyBoyLinkSession(network_backend=backend)

    try:
        link.attach(pyboy)
        assert backend._reader is not None and backend._reader.is_alive()
        assert backend._edge_worker is not None and backend._edge_worker.is_alive()
        reader, edge_worker = backend._reader, backend._edge_worker

        link.detach_all()

        assert not backend.connected
        assert not reader.is_alive()
        assert not edge_worker.is_alive()
        assert backend._reader is None and backend._edge_worker is None
        assert backend._local_core is None and backend._irq_callback is None
        assert link.attached == ()
    finally:
        link.close()
        peer.stop()


def test_attach_rolls_back_network_setup_failure(monkeypatch):
    backend, peer = NetworkBackend.pair()
    core = SerialCore()
    pyboy = _FakePyBoy(serial=core)
    prior_backend = core.backend
    link = PyBoyLinkSession(network_backend=backend)

    def fail_receiver(*_args, **_kwargs):
        raise RuntimeError("synthetic receiver failure")

    monkeypatch.setattr(backend, "start_receiver", fail_receiver)
    try:
        with pytest.raises(RuntimeError, match="synthetic receiver"):
            link.attach(pyboy)

        assert link.attached == ()
        assert pyboy.mb.serial is core
        assert core.backend is prior_backend
        assert not backend.connected
    finally:
        link.close()
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
