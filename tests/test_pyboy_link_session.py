"""Tests for :class:`PyBoyLinkSession` (milestone 4).

Uses a lightweight fake PyBoy that exposes just ``mb.serial`` and a
``tick`` method — enough to exercise attach/detach plumbing and the
end-to-end byte exchange without loading a real ROM.
"""

from __future__ import annotations

import socket as _socket
import threading
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
    """Explicit four-cycle instruction clock for the local scheduler."""

    def __init__(self, serial, owner):
        self.serial = serial
        self.owner = owner
        # The production scheduler requires runtime speed metadata even for
        # fixed-speed DMG doubles.  With no CGB transition, serial.clock is
        # mapped into the physical half-T-cycle domain by the owner.
        self.cgb_mode = False
        self.lcd = SimpleNamespace(frame_done=False, disable_renderer=True)
        self.sound = SimpleNamespace(
            disable_sampling=True,
            clear_buffer=lambda: None,
        )
        self.breakpoint_singlestep = 0
        self.breakpoint_singlestep_latch = 0

    def tick(self):
        self.owner._cycles += 4
        self.serial.tick(self.owner._cycles)
        self.lcd.frame_done = (
            self.owner._cycles - self.owner._frame_start >= CYCLES_PER_BYTE_DMG
        )
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


class _StatefulLegacySerialStub(_LegacySerialStub):
    """Legacy serial double with an active, partially shifted transfer."""

    def __init__(self):
        super().__init__()
        self.transfer_enabled = 1
        self.internal_clock = 1
        self.double_speed = 1
        self.cpu_speed_shift = 1
        self._shift_register = 0x5A
        self._bits_remaining = 3
        self._cycles_to_interrupt = 77
        self.clock_target = 1234


class _BackendSerialStub:
    """Serial double whose backend setter can fail during pair attach."""

    def __init__(self, *, fail_assignment: bool = False):
        self.last_cycles = 0
        self.clock = 0
        self._backend = object()
        self._fail_assignment = fail_assignment

    @property
    def backend(self):
        return self._backend

    @backend.setter
    def backend(self, value):
        if self._fail_assignment:
            raise RuntimeError("injected second-core assignment failure")
        self._backend = value


def _versioned_backend_pair(
    local_version: str = "red", peer_version: str = "blue"
):
    """Create a socket pair whose HELLO frames are available to attach()."""
    local_sock, peer_sock = _socket.socketpair()
    return (
        NetworkBackend(local_sock, local_rom_version=local_version),
        NetworkBackend(peer_sock, local_rom_version=peer_version),
    )


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


def test_listen_can_be_cancelled_before_a_peer_connects():
    cancel = threading.Event()
    result: list[BaseException] = []

    def listen() -> None:
        try:
            PyBoyLinkSession.listen(
                0,
                local_rom_version="red",
                accept_timeout_s=5.0,
                cancel_event=cancel,
            )
        except BaseException as exc:  # noqa: BLE001
            result.append(exc)

    worker = threading.Thread(target=listen, daemon=True)
    worker.start()
    # Port 0 is valid for binding; cancellation should be observed by the
    # bounded accept loop without requiring a connector.
    cancel.set()
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert result
    assert "cancelled" in str(result[0]).lower()


@pytest.mark.parametrize("network", [False, True])
def test_attach_rejects_stopped_endpoint_without_claiming_provider(network):
    from pokered_harness.ownership import EmulatorOwnershipError, owner_for

    endpoint = _FakePyBoy()
    endpoint.stopped = True
    owner = owner_for(endpoint)
    link = PyBoyLinkSession.local()
    if network:
        # Admission must fail before a network backend can start workers.
        link._network_backend = object()
    with pytest.raises(EmulatorOwnershipError, match="closed"):
        link.attach(endpoint)
    assert link._pyboys == []
    assert owner._provider is None


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


def test_attach_preserves_inflight_legacy_serial_state():
    """Promotion must preserve an already-active native serial transfer."""
    legacy = _StatefulLegacySerialStub()
    legacy.SB = 0xC3
    legacy.SC = 0x83
    legacy.last_cycles = 900
    legacy.clock = 1000
    a = _FakePyBoy(serial=legacy)

    link = PyBoyLinkSession.local()
    core = link.attach(a)

    assert core.SB == legacy.SB
    assert core.SC == legacy.SC
    for attr in (
        "transfer_enabled",
        "internal_clock",
        "double_speed",
        "cpu_speed_shift",
        "_shift_register",
        "_bits_remaining",
        "_cycles_to_interrupt",
        "clock_target",
    ):
        assert getattr(core, attr) == getattr(legacy, attr)
    assert core.last_cycles == legacy.last_cycles
    assert core.clock == legacy.clock
    link.detach_all()


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


def test_second_attach_failure_rolls_back_session_bookkeeping():
    """A failed coordinator install must leave only the first side attached."""
    serial_a = _BackendSerialStub()
    serial_b = _BackendSerialStub(fail_assignment=True)
    original_backend_a = serial_a.backend
    original_backend_b = serial_b.backend
    a = _FakePyBoy(serial=serial_a)
    b = _FakePyBoy(serial=serial_b)
    link = PyBoyLinkSession.local()
    link.attach(a)

    with pytest.raises(RuntimeError, match="injected second-core assignment"):
        link.attach(b)

    assert link.attached == (a,)
    assert link.cores == (serial_a,)
    assert link.paired is False
    assert serial_a.backend is original_backend_a
    assert serial_b.backend is original_backend_b
    link.detach_all()


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
        assert backend._local_core is None
        assert backend._irq_callback is None
        assert backend._serial_transcript_context_provider is None
    finally:
        # The peer is not owned by this session; clean up the test fixture
        # explicitly just as a direct NetworkBackend caller must.
        backend.stop()
        peer.stop()


def test_network_attach_failure_releases_emulator_references(monkeypatch):
    backend, peer = NetworkBackend.pair()
    endpoint = _FakePyBoy()
    original_serial = endpoint.mb.serial
    link = PyBoyLinkSession(network_backend=backend)

    def fail_install(_endpoint):
        raise RuntimeError("injected tick owner installation failure")

    monkeypatch.setattr(link, "_install_network_tick_owner", fail_install)
    try:
        with pytest.raises(RuntimeError, match="injected tick owner"):
            link.attach(endpoint)
        assert link.attached == ()
        assert endpoint.mb.serial is original_serial
        assert backend._local_core is None
        assert backend._irq_callback is None
        assert backend._serial_transcript_context_provider is None
        assert backend._reader is not None and not backend._reader.is_alive()
        assert backend._edge_worker is not None and not backend._edge_worker.is_alive()
    finally:
        link.detach_all()
        peer.stop()


@pytest.mark.parametrize("hook_name", ["stop", "detach_local_core"])
@pytest.mark.parametrize("bad_signature", ["positional_only", "extra_required"])
def test_network_attach_rejects_uncallable_timeout_signature(hook_name, bad_signature):
    backend, peer = NetworkBackend.pair()
    endpoint = _FakePyBoy(serial=SerialCore())
    link = PyBoyLinkSession(network_backend=backend)
    original_hook = getattr(backend, hook_name)
    previous_backend = endpoint.mb.serial.backend
    calls = []

    def positional_only(timeout_s, /):
        calls.append(timeout_s)
        return True

    def extra_required(required, *, timeout_s):
        calls.append((required, timeout_s))
        return True

    replacement = positional_only if bad_signature == "positional_only" else extra_required
    setattr(backend, hook_name, replacement)
    try:
        with pytest.raises(TypeError, match=hook_name):
            link.attach(endpoint)
        assert calls == []
        assert link.attached == ()
        assert endpoint.mb.serial.backend is previous_backend
        assert backend._local_core is None
        assert backend._irq_callback is None
        assert backend._serial_transcript_context_provider is None
        assert backend.connected
    finally:
        setattr(backend, hook_name, original_hook)
        try:
            link.detach_all()
        finally:
            peer.stop()


def test_network_detach_missing_bounded_hook_preserves_attachment_references():
    """A missing timeout-aware detach hook must fail before mutating state."""
    backend, peer = NetworkBackend.pair()
    endpoint = _FakePyBoy()
    link = PyBoyLinkSession(network_backend=backend)

    try:
        link.attach(endpoint)
        local_core = backend._local_core
        irq_callback = backend._irq_callback
        context_provider = backend._serial_transcript_context_provider
        original_detach = backend.detach_local_core
        backend.detach_local_core = None

        with pytest.raises(TypeError, match="detach_local_core"):
            link.detach(endpoint)

        assert link.attached == (endpoint,)
        assert backend.connected
        assert backend._local_core is local_core
        assert backend._irq_callback is irq_callback
        assert backend._serial_transcript_context_provider is context_provider
    finally:
        # Restore the adapter contract before terminal cleanup; the failed
        # detach intentionally left every reference in place for retry.
        if "original_detach" in locals():
            backend.detach_local_core = original_detach
        try:
            link.detach_all()
        finally:
            peer.stop()


def test_serial_completion_context_provider_reads_cpu_and_hram():
    """The diagnostic snapshot identifies the ROM's serial receive phase."""
    pyboy = _FakePyBoy()
    pyboy.mb.cpu = SimpleNamespace(PC=0x1E68)
    pyboy.memory = {0xFFAB: 1}

    snapshot = PyBoyLinkSession._make_serial_completion_context_provider(pyboy)

    assert snapshot() == {
        "cpu_pc": 0x1E68,
        "h_serial_ignoring_initial_data": 1,
    }


def test_serial_completion_context_provider_prefers_lowercase_indexable_pc():
    """A supported lowercase PC alias wins over the legacy uppercase alias."""

    class _IndexOnly:
        def __index__(self):
            return 0x2A7C

    pyboy = _FakePyBoy()
    pyboy.mb.cpu = SimpleNamespace(pc=_IndexOnly(), PC=0x1E68)

    snapshot = PyBoyLinkSession._make_serial_completion_context_provider(pyboy)

    assert snapshot() == {"cpu_pc": 0x2A7C}


def test_serial_completion_context_provider_omits_boolean_pc():
    """A bool must not be misreported as an integer program counter."""
    pyboy = _FakePyBoy()
    pyboy.mb.cpu = SimpleNamespace(pc=True, PC=0x1E68)

    snapshot = PyBoyLinkSession._make_serial_completion_context_provider(pyboy)

    assert snapshot() == {}


def test_serial_completion_context_provider_tolerates_missing_emulation_internals():
    """Diagnostics must not disrupt lightweight integrations or teardown."""
    snapshot = PyBoyLinkSession._make_serial_completion_context_provider(
        SimpleNamespace(mb=SimpleNamespace())
    )

    assert snapshot() == {}


@pytest.mark.parametrize("is_internal_clock", [True, False])
def test_network_attach_does_not_seed_game_role_status(is_internal_clock):
    """Native attach leaves ROM-owned serial role state untouched.

    ``hSerialConnectionStatus`` is populated by the ROM's serial interrupt
    handler after the native handshake. Native attach may configure the
    serial registers for the requested wire role, but it must never prefill
    this ROM-owned HRAM byte for either network role.
    """
    backend, peer = _versioned_backend_pair("red", "red")
    serial = SerialCore()
    pyboy = _FakePyBoy(serial=serial)
    pyboy.memory = {0xFFAA: 0xFF}
    link = PyBoyLinkSession(
        network_backend=backend,
        network_is_internal_clock=is_internal_clock,
        local_rom_version="red",
    )

    try:
        link.attach(pyboy)
        assert pyboy.memory[0xFFAA] == 0xFF
        assert pyboy.mb.serial.backend is backend
        assert serial.transfer_enabled == 1
        assert serial.internal_clock == int(is_internal_clock)
    finally:
        link.detach_all()
        peer.stop()


@pytest.mark.parametrize(
    ("is_internal_clock", "expected_sb", "expected_sc_source"),
    [(True, 0x01, 1), (False, 0x02, 0)],
)
def test_network_attach_arms_native_role_handshake(
    is_internal_clock, expected_sb, expected_sc_source
):
    """Configure FF01/FF02 without touching the ROM's role-status HRAM."""
    backend, peer = _versioned_backend_pair("red", "red")
    serial = SerialCore()
    serial.set_SB(0x02)
    serial.set_SC(0x80)
    pyboy = _FakePyBoy(serial=serial)
    pyboy.memory = {0xFFAA: 0xFF}
    link = PyBoyLinkSession(
        network_backend=backend,
        network_is_internal_clock=is_internal_clock,
        local_rom_version="red",
    )

    try:
        link.attach(pyboy)
        assert serial.SB == expected_sb
        assert serial.transfer_enabled == 1
        assert serial.internal_clock == expected_sc_source
        assert serial.SC & 0x80
        assert serial.SC & 0x01 == expected_sc_source
        assert pyboy.memory[0xFFAA] == 0xFF
    finally:
        link.detach_all()
        peer.stop()


@pytest.mark.parametrize(
    (
        "local_version",
        "peer_version",
        "default_internal",
        "expected_internal",
        "expected_frame_barrier",
    ),
    [
        ("yellow", "red", True, False, False),
        ("red", "yellow", False, True, False),
        ("yellow", "blue", True, False, False),
        ("blue", "yellow", False, True, False),
        ("yellow", "yellow", True, True, False),
        ("red", "red", True, True, True),
        ("red", "red", False, False, True),
        ("red", "blue", False, True, True),
        ("blue", "red", True, False, True),
        ("blue", "blue", True, True, True),
        ("blue", "blue", False, False, True),
    ],
)
def test_network_clock_negotiation_selects_compatible_native_role(
    local_version,
    peer_version,
    default_internal,
    expected_internal,
    expected_frame_barrier,
):
    """Startup role selection only changes native registers."""
    backend, peer = _versioned_backend_pair(local_version, peer_version)
    serial = SerialCore()
    pyboy = _FakePyBoy(serial=serial)
    pyboy.memory = {0xFFAA: 0xFF}
    link = PyBoyLinkSession(
        network_backend=backend,
        network_is_internal_clock=default_internal,
        local_rom_version=local_version,
    )

    try:
        link.attach(pyboy)
        selected = link.negotiate_network_clock_role(peer_version)
        assert selected is expected_internal
        assert link._network_frame_barrier is expected_frame_barrier
        assert serial.internal_clock == int(expected_internal)
        assert serial.SB == (0x01 if expected_internal else 0x02)
        assert pyboy.memory[0xFFAA] == 0xFF
    finally:
        link.detach_all()
        peer.stop()


def test_network_attach_selects_cross_family_role_before_owner_ticks():
    """HELLO must select Yellow external before a native tick can begin."""
    backend, peer = _versioned_backend_pair("yellow", "red")
    serial = SerialCore()
    pyboy = _FakePyBoy(serial=serial)
    link = PyBoyLinkSession(
        network_backend=backend,
        network_is_internal_clock=True,
        local_rom_version="yellow",
    )

    try:
        link.attach(pyboy)
        assert pyboy._cycles == 0
        assert serial.SB == 0x02
        assert serial.internal_clock == 0
        assert serial.transfer_enabled == 1
    finally:
        link.detach_all()
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


def test_detach_all_attempts_remaining_attachments_after_failure(monkeypatch):
    """One restoration failure must not strand the other attached core."""
    a = _FakePyBoy()
    b = _FakePyBoy()
    link = PyBoyLinkSession.local()
    link.attach(a)
    link.attach(b)
    calls = []
    real_detach = link.detach

    def fail_first(pyboy):
        calls.append(pyboy)
        if pyboy is b:
            raise RuntimeError("synthetic first detach failure")
        real_detach(pyboy)

    monkeypatch.setattr(link, "detach", fail_first)
    with pytest.raises(RuntimeError, match="synthetic first detach failure"):
        link.detach_all()

    assert calls == [b, a]
    assert link.attached == (b,)
    monkeypatch.setattr(link, "detach", real_detach)
    link.detach_all()


# ---------------------------------------------------------------------------
# step(): drives both sides and triggers the coordinated exchange
# ---------------------------------------------------------------------------


def test_step_requires_both_sides_attached():
    link = PyBoyLinkSession.local()
    link.attach(_FakePyBoy())
    with pytest.raises(RuntimeError, match="requires 2 attached"):
        link.step()


@pytest.mark.parametrize(
    ("method_name", "frames", "exception"),
    [
        ("step", 0, ValueError),
        ("step", -1, ValueError),
        ("step", True, TypeError),
        ("step_interleaved", 0, ValueError),
        ("step_interleaved", -1, ValueError),
        ("step_interleaved", True, TypeError),
    ],
)
def test_step_rejects_non_positive_or_boolean_frame_counts(
    method_name, frames, exception
):
    link = PyBoyLinkSession.local()
    link.attach(_FakePyBoy())
    link.attach(_FakePyBoy())

    with pytest.raises(exception, match="frames must be a positive integer"):
        getattr(link, method_name)(frames=frames)

    link.detach_all()


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
        self.cgb_mode = bool(speed_shift)
        self.speed_shift = speed_shift
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
        self.serial = SerialCore()
        self.serial.last_cycles = start_cycles
        self.serial.clock = start_cycles
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
        self.serial.tick(self.cpu.cycles)
        return False

    def get_physical_clock(self):
        # PyBoy's scheduler clock uses half-normal-speed T-cycles: normal
        # speed consumes two units per CPU cycle, while CGB double speed
        # consumes one.  The tests use small physical quanta below so the
        # scheduler behavior remains observable without a large instruction
        # budget.
        rate = 1 if self.speed_shift else 2
        return (0, self.cpu.cycles * rate)


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
    link = PyBoyLinkSession.local()
    link.PHYSICAL_QUANTUM = 64
    link.attach(a)
    link.attach(b)

    link.step()

    assert a.mb.cpu.cycles - a_start == b.mb.cpu.cycles - b_start == 32
    assert a.mb.ticks_after_boundary > 0
    assert a.frame_count == b.frame_count == 1
    link.detach_all()


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
    link = PyBoyLinkSession.local()
    link.PHYSICAL_QUANTUM = 40
    link.attach(a)
    link.attach(b)

    link.step()

    assert a.mb.cpu.cycles - a_start == 40
    assert b.mb.cpu.cycles - b_start == 20
    assert a.frame_count == b.frame_count == 1
    link.detach_all()


def test_interleaved_frame_timeout_is_bounded_and_clears_singlestep():
    a = _FrameBoundaryPyBoy(8, stalled=True)
    b = _FrameBoundaryPyBoy(8, stalled=True)
    link = PyBoyLinkSession.local()
    link.PHYSICAL_QUANTUM = 16
    link.attach(a)
    link.attach(b)

    with pytest.raises(RuntimeError, match="instruction stepping made no clock progress"):
        link.step()

    assert a.mb.breakpoint_singlestep == b.mb.breakpoint_singlestep == 0
    link.detach_all()
