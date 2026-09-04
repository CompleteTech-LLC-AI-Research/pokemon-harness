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
    MAX_CYCLES,
    LocalBackend,
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


def test_rewriting_active_same_role_SC_does_not_restart_transfer():
    """A repeated external-clock arm preserves the in-flight byte.

    The Pokémon overworld connection probe writes ``SC=$80`` repeatedly
    while waiting for the peer's clock.  Those writes must not reset the
    serial shift count or discard bits already exchanged.
    """
    s = SerialCore()
    s.set_SB(0xA5)
    s.set_SC(0x80)
    s.apply_external_edge(1)
    s.apply_external_edge(0)
    shift_before = s._shift_register
    bits_before = s._bits_remaining

    s.set_SB(0x5A)  # Mid-transfer SB writes do not replace the snapshot.
    s.set_SC(0x80)

    assert s._shift_register == shift_before
    assert s._bits_remaining == bits_before
    assert s.transfer_enabled == 1
    assert s.internal_clock == 0

    for _ in range(bits_before):
        completed = s.apply_external_edge(0)
    assert completed is True
    assert s.transfer_enabled == 0


def test_switching_clock_source_while_armed_starts_fresh_transfer():
    """A role change is a new native transfer, not a continuation."""
    s = SerialCore()
    s.set_SB(0xA5)
    s.set_SC(0x80)
    s.apply_external_edge(1)
    assert s._bits_remaining == 7

    s.set_SC(0x81)

    assert s.internal_clock == 1
    assert s._bits_remaining == 8
    assert s._shift_register == 0xA5
    assert s.clock_target == s.clock + CYCLES_PER_EDGE_DMG


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


def test_cgb_double_speed_keeps_existing_normal_serial_edge_rate():
    """Preserve the existing normal-rate raw-cycle scheduling model."""
    s = SerialCore(cgb_mode=True, backend=NullBackend())
    s.cpu_speed_shift = 1
    s.set_SB(0xAA)
    s.set_SC(0x81)

    # The existing normal-rate path keeps the serial clock in its hardware
    # time domain, so a double-speed CPU gets twice the raw-cycle deadline.
    # The CGB fast clock is different: its hardware rate also doubles.
    assert s.tick((CYCLES_PER_EDGE_DMG * 2) - 1) is False
    assert s.transfer_enabled == 1
    assert s.tick(CYCLES_PER_EDGE_DMG * 2) is False
    assert s.transfer_enabled == 1
    assert s.tick(CYCLES_PER_BYTE_DMG * 2) is True
    assert s.transfer_enabled == 0


def test_cgb_normal_serial_rate_matches_dmg_rate():
    """CGB SC bit 1 clear keeps the normal 8192 Hz serial cadence."""
    s = SerialCore(cgb_mode=True, backend=NullBackend())
    s.set_SB(0xAA)
    s.set_SC(0x81)

    assert s.clock_target == CYCLES_PER_EDGE_DMG
    assert s.tick(CYCLES_PER_BYTE_DMG - 1) is False
    assert s.transfer_enabled == 1
    assert s.tick(CYCLES_PER_BYTE_DMG) is True
    assert s.transfer_enabled == 0


@pytest.mark.parametrize("cpu_speed_shift", [0, 1])
def test_cgb_fast_serial_rate_is_32_times_normal(cpu_speed_shift):
    """CGB SC=0x83 uses four raw cycles per edge at either CPU speed."""
    s = SerialCore(cgb_mode=True, backend=NullBackend())
    s.cpu_speed_shift = cpu_speed_shift
    s.set_SB(0xAA)
    s.set_SC(0x83)

    fast_edge_cycles = CYCLES_PER_EDGE_DMG // 32
    fast_byte_cycles = CYCLES_PER_BYTE_DMG // 32
    assert fast_edge_cycles == 4
    assert fast_byte_cycles == 32
    # At cpu_speed_shift=1 both the CPU and fast serial clocks double, so
    # the raw-cycle period stays 4; using 2 would double the rate twice.
    assert s.clock_target == fast_edge_cycles

    assert s.tick(fast_byte_cycles - 1) is False
    assert s.transfer_enabled == 1
    assert s.tick(fast_byte_cycles) is True
    assert s.transfer_enabled == 0


def test_dmg_ignores_cgb_fast_serial_bit():
    """DMG SC=0x83 retains the fixed normal serial timing and mode."""
    s = SerialCore(cgb_mode=False, backend=NullBackend())
    s.set_SB(0xAA)
    s.set_SC(0x83)

    assert s.double_speed == 0
    assert s.clock_target == CYCLES_PER_EDGE_DMG
    assert s.tick(CYCLES_PER_BYTE_DMG - 1) is False
    assert s.transfer_enabled == 1
    assert s.tick(CYCLES_PER_BYTE_DMG) is True
    assert s.transfer_enabled == 0


@pytest.mark.parametrize("initial_sc, next_sc", [(0x81, 0x83), (0x83, 0x81)])
def test_cgb_fast_serial_bit_change_preserves_armed_transfer(initial_sc, next_sc):
    """Changing only SC bit 1 does not restart an in-flight transfer."""
    s = SerialCore(cgb_mode=True, backend=NullBackend())
    s.set_SB(0xA5)
    s.set_SC(initial_sc)

    initial_edge_cycles = CYCLES_PER_EDGE_DMG // (32 if initial_sc & 0x02 else 1)
    s.tick(initial_edge_cycles)
    shift_before = s._shift_register
    bits_before = s._bits_remaining
    deadline_before = s.clock_target

    s.set_SC(next_sc)

    assert s._shift_register == shift_before
    assert s._bits_remaining == bits_before
    next_edge_cycles = CYCLES_PER_EDGE_DMG // (32 if next_sc & 0x02 else 1)
    assert s.clock_target != deadline_before
    assert s.clock_target == s.clock + next_edge_cycles
    assert s.transfer_enabled == 1
    assert s.double_speed == bool(next_sc & 0x02)

    # The remaining bits use the newly selected rate; the transfer was not
    # re-armed because the shift snapshot and remaining count were retained.
    assert s.tick(s.clock + next_edge_cycles * bits_before) is True
    assert s.transfer_enabled == 0


def test_master_edge_by_edge_progresses_one_bit_per_period():
    s = SerialCore(backend=NullBackend())
    s.set_SB(0xAA)
    s.set_SC(0x81)

    # After each 128-cycle edge, one more bit has been shifted.
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


def test_owner_dispatch_callback_runs_at_explicit_owner_boundary():
    """Remote owner dispatch stays outside the serial tick/register path."""
    s = SerialCore()
    calls: list[int] = []
    s.owner_dispatch_callback = lambda: calls.append(1)
    s.owner_dispatch_enabled = True
    s.set_SC(0x80)

    s.tick(1)
    assert calls == []

    s.dispatch_owner()

    assert calls == [1]


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


def test_set_SC_reports_fast_clock_only_for_cgb_serial():
    dmg = SerialCore(cgb_mode=False)
    dmg.set_SC(0x82)

    cgb = SerialCore(cgb_mode=True)
    cgb.set_SC(0x82)

    assert dmg.double_speed == 0
    assert cgb.double_speed == 1


def test_loading_legacy_state_drops_in_flight_transfer():
    original = SerialCore(cgb_mode=True)
    original.set_SB(0x42)
    original.set_SC(0x81)

    stream = _FakeStream()
    original.save_state(stream)
    stream._buf = stream._buf[:8]

    restored = SerialCore(cgb_mode=True)
    restored.load_state(stream, SerialCore.STATE_VERSION)

    assert restored.transfer_enabled == 0
    assert restored.SC & 0x80 == 0
    assert restored.internal_clock == 1
    assert restored._bits_remaining == 0
    assert restored._shift_register == restored.SB
    assert restored.clock_target == (1 << 31)
    assert restored._cycles_to_interrupt == (1 << 31)


def test_state_round_trip_restores_cgb_fast_clock_flag():
    original = SerialCore(cgb_mode=True)
    original.set_SB(0x42)
    original.set_SC(0x83)

    stream = _FakeStream()
    original.save_state(stream)

    restored = SerialCore(cgb_mode=True)
    restored.double_speed = 0
    restored.load_state(stream, SerialCore.STATE_VERSION)

    assert restored.double_speed == 1
