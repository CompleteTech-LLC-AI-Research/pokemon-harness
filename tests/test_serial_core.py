"""Tier-1 register-level tests for the bit-accurate :class:`SerialCore`.

These correspond to the "Register-level (ROM-free)" tier in the design
doc (``docs/pyboy_serial_overhaul_design.md``). They drive ``SB``/``SC``
directly and assert Pan Docs semantics: shift-register behavior, master
internal-clock completion timing, slave external-clock waiting, serial
interrupt on completion, disconnect pull-up, and round-trip across two
locally-bridged cores.
"""

from __future__ import annotations

import pytest

from pokered_harness.link.serial_core import (
    CYCLES_PER_BYTE_DMG,
    CYCLES_PER_EDGE_DMG,
    LocalBackend,
    MAX_CYCLES,
    NullBackend,
    SerialCore,
)


# ---------------------------------------------------------------------------
# Defaults & arming behavior
# ---------------------------------------------------------------------------


def test_defaults_match_legacy_pyboy_serial():
    s = SerialCore()
    assert s.SB == 0xFF
    assert s.SC == 0x00
    assert s.transfer_enabled == 0
    assert s.internal_clock == 0
    assert s.clock == 0
    assert s.clock_target == MAX_CYCLES


@pytest.mark.parametrize("cgb_mode", [False, True])
def test_normal_serial_uses_literal_t_cycle_deadlines(cgb_mode):
    """4,194,304 T/s / 8192 bit/s = 512 T/bit, independently of exports."""
    edges = []

    class RecordingBackend:
        def on_edge(self, bit, role):
            edges.append((bit, role))
            return 1

    serial = SerialCore(cgb_mode, backend=RecordingBackend())
    serial.set_SB(0x00)
    serial.set_SC(0x81)  # Normal clock on both DMG and CGB; no fast-clock request.
    assert serial.clock_target == 512
    assert serial.tick(511) is False
    assert edges == []
    assert serial.tick(512) is False
    assert len(edges) == 1
    assert serial.tick(4095) is False
    assert len(edges) == 7
    assert serial.SC & 0x80
    assert serial.tick(4096) is True
    assert len(edges) == 8
    assert serial.SB == 0xFF
    assert serial.SC & 0x80 == 0
    assert serial.tick(4096) is False
    assert serial.tick(8192) is False
    assert len(edges) == 8


def test_normal_serial_constants_use_t_cycles_not_machine_cycles():
    from pyboy.core.serial import CYCLES_8192HZ

    assert CYCLES_PER_EDGE_DMG == 512
    assert CYCLES_PER_BYTE_DMG == 4096
    assert CYCLES_8192HZ == 512


@pytest.mark.parametrize("cgb_mode", [False, True])
def test_normal_serial_restart_has_fresh_literal_edge_phase(cgb_mode):
    """A restart schedules its first bit relative to that SC write's clock."""
    edges = []

    class RecordingBackend:
        def on_edge(self, bit, role):
            edges.append(bit)
            return 0

    serial = SerialCore(cgb_mode, backend=RecordingBackend())
    serial.tick(100)
    serial.set_SB(0x80)
    serial.set_SC(0x81)
    assert serial.clock_target == 612
    assert serial.tick(611) is False
    assert edges == []
    assert serial.tick(612) is False
    assert edges == [1]
    serial.tick(700)
    serial.set_SB(0x40)
    serial.set_SC(0x81)
    assert serial.clock_target == 1212
    assert serial.tick(1211) is False
    assert edges == [1]
    assert serial.tick(1212) is False
    assert edges == [1, 0]
    assert serial.tick(4795) is False
    assert serial.SC & 0x80
    assert serial.tick(4796) is True
    assert edges[1:] == [0, 1, 0, 0, 0, 0, 0, 0]
    assert serial.tick(5308) is False


def test_normal_serial_cancel_does_not_emit_pending_edge_or_irq():
    edges = []

    class RecordingBackend:
        def on_edge(self, bit, role):
            edges.append(bit)
            return 1

    serial = SerialCore(False, backend=RecordingBackend())
    serial.set_SC(0x81)
    serial.tick(511)
    serial.set_SC(0x01)
    assert serial.tick(4096) is False
    assert edges == []
    assert serial.transfer_enabled == 0
    serial.set_SC(0x81)
    assert serial.clock_target == 4608
    assert serial.tick(4607) is False
    assert edges == []
    assert serial.tick(4608) is False
    assert len(edges) == 1
    assert serial.tick(8192) is True


@pytest.mark.parametrize("cgb_mode", [False, True])
@pytest.mark.parametrize("control", [0x00, 0x80])
def test_large_absolute_clock_has_no_internal_deadline_when_not_master(cgb_mode, control):
    base = 1 << 40  # Longer-running fixtures already exceed the 2**31 sentinel.
    edges = []

    class RecordingBackend:
        def on_edge(self, bit, role):
            edges.append(bit)
            return 1

    serial = SerialCore(cgb_mode, backend=RecordingBackend())
    assert serial.tick(base) is False
    serial.set_SC(control)
    assert serial._cycles_to_interrupt == MAX_CYCLES
    for elapsed in (base + 1, base + 4096, base + 8192):
        assert serial.tick(elapsed) is False
        assert serial._cycles_to_interrupt == MAX_CYCLES
    assert edges == []
    assert bool(serial.transfer_enabled) == bool(control & 0x80)


@pytest.mark.parametrize("cgb_mode", [False, True])
def test_large_absolute_clock_master_completion_removes_internal_deadline(cgb_mode):
    base = 1 << 40
    serial = SerialCore(cgb_mode)
    serial.tick(base)
    serial.set_SB(0x00)
    serial.set_SC(0x81)
    assert serial._cycles_to_interrupt == 512
    assert serial.tick(base + 4095) is False
    assert serial._cycles_to_interrupt == 1
    assert serial.tick(base + 4096) is True
    assert serial._cycles_to_interrupt == MAX_CYCLES
    assert serial.tick(base + 8192) is False
    assert serial._cycles_to_interrupt == MAX_CYCLES


@pytest.mark.parametrize("cgb_mode", [False, True])
def test_large_absolute_clock_external_completion_has_no_later_irq(cgb_mode):
    base = 1 << 40
    serial = SerialCore(cgb_mode)
    serial.tick(base)
    serial.set_SC(0x80)
    for bit in range(8):
        assert serial.apply_external_edge(1) is (bit == 7)
    assert serial._cycles_to_interrupt == MAX_CYCLES
    assert serial.tick(base + 4096) is False
    assert serial._cycles_to_interrupt == MAX_CYCLES


def test_large_absolute_clock_cancel_removes_internal_deadline():
    base = 1 << 40
    serial = SerialCore(False)
    serial.tick(base)
    serial.set_SC(0x81)
    serial.tick(base + 100)
    serial.set_SC(0x01)
    assert serial._cycles_to_interrupt == MAX_CYCLES
    assert serial.tick(base + 4096) is False
    assert serial._cycles_to_interrupt == MAX_CYCLES


@pytest.mark.parametrize("control", [0x00, 0x80])
def test_large_absolute_clock_load_repairs_cached_nonmaster_deadline(control):
    base = 1 << 40
    original = SerialCore(False)
    original.tick(base)
    original.set_SB(0x02)
    original.set_SC(control)
    # Prior saves can cache zero after comparing absolute clock to MAX_CYCLES.
    original._cycles_to_interrupt = 0
    stream = _FakeStream()
    original.save_state(stream)
    restored = SerialCore(False)
    restored.load_state(stream, SerialCore.STATE_VERSION)
    assert restored._cycles_to_interrupt == MAX_CYCLES
    assert restored.clock == base
    assert restored.SB == original.SB and restored.SC == original.SC
    assert restored._bits_remaining == original._bits_remaining
    assert restored.tick(base) is False
    assert restored._cycles_to_interrupt == MAX_CYCLES


def test_set_SB_preserves_byte_unlike_legacy():
    """Pan Docs says SB holds the outgoing byte. Legacy PyBoy overwrote
    with 0xFF; the bit-accurate core must preserve."""
    s = SerialCore()
    s.set_SB(0xAA)
    assert s.SB == 0xAA


def test_set_SC_arms_master_transfer():
    s = SerialCore()
    s.set_SB(0xAA)
    s.set_SC(0x81)  # transfer_enable | internal_clock
    assert s.transfer_enabled == 1
    assert s.internal_clock == 1
    # Next edge scheduled one edge period from now.
    assert s.clock_target == CYCLES_PER_EDGE_DMG


def test_set_SC_arms_slave_transfer_with_no_timebase():
    s = SerialCore()
    s.set_SB(0xAA)
    s.set_SC(0x80)  # transfer_enable, external clock
    assert s.transfer_enabled == 1
    assert s.internal_clock == 0
    # Slave never schedules its own edges.
    assert s.clock_target == MAX_CYCLES


# ---------------------------------------------------------------------------
# Master mode with NullBackend (disconnected cable)
# ---------------------------------------------------------------------------


def test_master_with_null_backend_reads_0xff_after_full_transfer():
    """Pan Docs: disconnected master's RX is pulled high; received byte is 0xFF.
    Regardless of what the master wrote to SB."""
    s = SerialCore(backend=NullBackend())
    s.set_SB(0xAA)
    s.set_SC(0x81)

    # Advance past the end of the transfer in one go.
    irq = s.tick(CYCLES_PER_BYTE_DMG)
    assert irq is True
    assert s.SB == 0xFF


def test_master_completion_clears_only_SC_bit_7():
    """Pan Docs: on completion SC bit 7 auto-clears; other bits preserved.
    Legacy PyBoy used ``SC &= 0x80`` which was inverted."""
    s = SerialCore(backend=NullBackend())
    s.set_SB(0xAA)
    s.set_SC(0x81)  # bits 7 and 0 set
    s.tick(CYCLES_PER_BYTE_DMG)
    # Bit 7 cleared, bit 0 still set (internal clock preserved).
    assert s.SC & 0x80 == 0
    assert s.SC & 0x01 == 1
    assert s.transfer_enabled == 0


def test_master_transfer_does_not_complete_early():
    """Transfer must not finish before 8 * edge_period cycles."""
    s = SerialCore(backend=NullBackend())
    s.set_SB(0xAA)
    s.set_SC(0x81)

    # One cycle before completion: still in transfer.
    irq = s.tick(CYCLES_PER_BYTE_DMG - 1)
    assert irq is False
    assert s.transfer_enabled == 1
    assert s.SC & 0x80 != 0

    # Cross the threshold: completes.
    irq = s.tick(CYCLES_PER_BYTE_DMG)
    assert irq is True
    assert s.transfer_enabled == 0


def test_master_edge_by_edge_progresses_one_bit_per_period():
    s = SerialCore(backend=NullBackend())
    s.set_SB(0xAA)
    s.set_SC(0x81)

    # After each 512-T-cycle bit event, one more bit has been shifted.
    for edge in range(1, 8):
        cycles = CYCLES_PER_EDGE_DMG * edge
        irq = s.tick(cycles)
        # Last edge (8) triggers IRQ; earlier edges do not.
        assert irq is False, f"unexpected early IRQ at edge {edge}"
        assert s.transfer_enabled == 1

    irq = s.tick(CYCLES_PER_BYTE_DMG)
    assert irq is True


def test_tick_returns_false_for_zero_delta():
    """PyBoy calls tick() with absolute cycles. Re-calling with the same
    value is a no-op (no edges advanced, no IRQ)."""
    s = SerialCore(backend=NullBackend())
    s.set_SB(0xAA)
    s.set_SC(0x81)
    s.tick(CYCLES_PER_EDGE_DMG)  # advance 1 edge
    assert s.tick(CYCLES_PER_EDGE_DMG) is False  # same cycle, no progress


# ---------------------------------------------------------------------------
# Slave mode
# ---------------------------------------------------------------------------


def test_slave_mode_never_completes_without_external_edges():
    """Pan Docs: slave with no clock waits indefinitely; software must
    time out. Pokémon does exactly this in Serial_ExchangeBytes."""
    s = SerialCore(backend=NullBackend())
    s.set_SB(0xAA)
    s.set_SC(0x80)

    # Even an enormous number of cycles doesn't complete the transfer.
    for elapsed in (CYCLES_PER_BYTE_DMG, 100 * CYCLES_PER_BYTE_DMG, 10_000_000):
        irq = s.tick(elapsed)
        assert irq is False
        assert s.transfer_enabled == 1
        assert s.SC & 0x80 != 0


def test_slave_mode_apply_external_edge_progresses():
    """Slave receives 0x55 when the peer clocks 0,1,0,1,0,1,0,1."""
    s = SerialCore(backend=NullBackend())
    s.set_SB(0xAA)
    s.set_SC(0x80)

    peer_bits = [0, 1, 0, 1, 0, 1, 0, 1]
    for i, bit in enumerate(peer_bits):
        completed = s.apply_external_edge(bit)
        assert completed is (i == 7)

    assert s.SB == 0x55
    assert s.SC & 0x80 == 0
    assert s.transfer_enabled == 0


def test_apply_external_edge_requires_armed_transfer():
    s = SerialCore()
    with pytest.raises(RuntimeError, match="no transfer armed"):
        s.apply_external_edge(1)


def test_apply_external_edge_rejects_master_mode():
    s = SerialCore()
    s.set_SB(0x00)
    s.set_SC(0x81)  # master
    with pytest.raises(RuntimeError, match="internal-clock"):
        s.apply_external_edge(0)


# ---------------------------------------------------------------------------
# Shift semantics: verify the exact bit pattern flowing through the register
# ---------------------------------------------------------------------------


def test_slave_receives_arbitrary_byte():
    # Send 0b10110010 (=0xB2) bit-by-bit, MSB first.
    s = SerialCore()
    s.set_SB(0x00)
    s.set_SC(0x80)
    for bit in (1, 0, 1, 1, 0, 0, 1, 0):
        s.apply_external_edge(bit)
    assert s.SB == 0xB2


def test_master_shifts_out_MSB_first():
    """Capture each bit the master shifts out via a custom backend.

    Send 0b11010011 (=0xD3); peer bits all 0 so the received byte ends
    at 0x00.
    """
    captured: list[int] = []

    class RecordingBackend:
        def on_edge(self, our_bit, our_role):
            captured.append(our_bit)
            return 0

    s = SerialCore(backend=RecordingBackend())
    s.set_SB(0xD3)
    s.set_SC(0x81)
    s.tick(CYCLES_PER_BYTE_DMG)

    assert captured == [1, 1, 0, 1, 0, 0, 1, 1]  # 0xD3 MSB-first
    assert s.SB == 0x00


# ---------------------------------------------------------------------------
# LocalBackend: two cores bridged bit-at-a-time (lockstep-driven)
# ---------------------------------------------------------------------------


def test_atomic_edge_swap_exchanges_bytes():
    """Model the atomic edge-swap a :class:`LockstepCoordinator` will
    perform: peek both cores' out-bits, then apply each to the other.
    After 8 edges each side holds the other's starting byte.

    This is the minimal correctness check for the bit-accurate shift
    machinery independent of the backend API.
    """
    a = SerialCore()
    b = SerialCore()

    a.set_SB(0xAA)
    b.set_SB(0x55)
    # Both armed as slave; the coordinator (this test) supplies edges.
    a.set_SC(0x80)
    b.set_SC(0x80)

    for _ in range(8):
        a_out = a.peek_out_bit()
        b_out = b.peek_out_bit()
        a.apply_external_edge(b_out)
        b.apply_external_edge(a_out)

    assert a.SB == 0x55
    assert b.SB == 0xAA
    assert a.transfer_enabled == 0
    assert b.transfer_enabled == 0


def test_local_backend_pair_deposits_bit_for_peer():
    """LocalBackend.on_edge deposits our bit into the peer's inbox.

    This is the mechanism the eventual coordinator will rely on: at
    each edge the coordinator ensures both sides have deposited before
    either reads. The low-level test here pre-stages B's deposit so
    that when A's backend fires, A drains a real bit rather than the
    pull-up fallback.
    """
    ba, bb = LocalBackend.pair()

    # Pre-stage: B has already "deposited" a 0 into A's inbox.
    ba._inbox = 0
    assert ba.peer_ready

    # Now A edges with our_bit=1: A deposits 1 into B's inbox and
    # drains the pre-staged 0.
    peer = ba.on_edge(our_bit=1, our_role=1)
    assert peer == 0
    assert bb.peer_ready  # A's 1 is now waiting for B

    peer = bb.on_edge(our_bit=0, our_role=0)
    assert peer == 1  # drained A's deposit


def test_unpaired_local_backend_behaves_like_null():
    """LocalBackend with no peer bound must not crash and must fall
    back to pull-up, matching NullBackend."""
    ba = LocalBackend()  # unpaired
    a = SerialCore(backend=ba)
    a.set_SB(0xAA)
    a.set_SC(0x81)
    a.tick(CYCLES_PER_BYTE_DMG)
    assert a.SB == 0xFF


# ---------------------------------------------------------------------------
# Disconnect / mid-transfer backend swap
# ---------------------------------------------------------------------------


def test_mid_transfer_backend_swap_fills_remaining_with_pullup():
    """Disconnect 3 edges in: first 3 bits from the original backend,
    remaining 5 from the new (NullBackend) are all 1s."""

    class AllZerosBackend:
        def on_edge(self, our_bit, our_role):
            return 0

    s = SerialCore(backend=AllZerosBackend())
    s.set_SB(0x00)
    s.set_SC(0x81)

    s.tick(3 * CYCLES_PER_EDGE_DMG)  # 3 edges with peer=0
    s.backend = NullBackend()         # "disconnect"
    s.tick(CYCLES_PER_BYTE_DMG)       # finish the remaining 5 edges

    # Shift register received: 0,0,0,1,1,1,1,1 (MSB-first) = 0b00011111 = 0x1F
    assert s.SB == 0x1F


# ---------------------------------------------------------------------------
# _cycles_to_interrupt: PyBoy-facing scheduling hint
# ---------------------------------------------------------------------------


def test_cycles_to_interrupt_tracks_next_edge_for_master():
    s = SerialCore(backend=NullBackend())
    s.set_SB(0xAA)
    s.set_SC(0x81)
    assert s._cycles_to_interrupt == CYCLES_PER_EDGE_DMG

    s.tick(50)
    assert s._cycles_to_interrupt == CYCLES_PER_EDGE_DMG - 50


def test_cycles_to_interrupt_is_max_for_idle_core():
    s = SerialCore()
    assert s._cycles_to_interrupt == MAX_CYCLES


def test_cycles_to_interrupt_is_max_for_slave():
    """Slave with no external clock has nothing to schedule."""
    s = SerialCore()
    s.set_SB(0xAA)
    s.set_SC(0x80)
    assert s._cycles_to_interrupt >= MAX_CYCLES - CYCLES_PER_BYTE_DMG


# ---------------------------------------------------------------------------
# Save/load round-trip (duck-compatible with PyBoy's state streams)
# ---------------------------------------------------------------------------


class _FakeStream:
    """Minimal stand-in for PyBoy's save-state f; stores a list of tokens."""

    def __init__(self):
        self._buf: list = []
        self._ptr = 0

    def write(self, v):
        self._buf.append(("u8", v & 0xFF))

    def write_64bit(self, v):
        self._buf.append(("u64", v))

    def read(self):
        tag, v = self._buf[self._ptr]
        assert tag == "u8", f"expected u8, got {tag}"
        self._ptr += 1
        return v

    def read_64bit(self):
        tag, v = self._buf[self._ptr]
        assert tag == "u64", f"expected u64, got {tag}"
        self._ptr += 1
        return v


def test_save_load_round_trip_preserves_all_state():
    original = SerialCore(backend=NullBackend())
    original.set_SB(0x42)
    original.set_SC(0x81)
    original.tick(3 * CYCLES_PER_EDGE_DMG)  # mid-transfer

    stream = _FakeStream()
    original.save_state(stream)

    restored = SerialCore(backend=NullBackend())
    restored.load_state(stream, SerialCore.STATE_VERSION)

    assert restored.SB == original.SB
    assert restored.SC == original.SC
    assert restored.transfer_enabled == original.transfer_enabled
    assert restored.internal_clock == original.internal_clock
    assert restored.clock == original.clock
    assert restored.clock_target == original.clock_target
    assert restored._shift_register == original._shift_register
    assert restored._bits_remaining == original._bits_remaining
