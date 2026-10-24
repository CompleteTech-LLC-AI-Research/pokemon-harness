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
from pyboy.core.serial import SerialBackendError

from pokered_harness.link.serial_core import (
    CYCLES_PER_BYTE_DMG,
    CYCLES_PER_EDGE_CGB_FAST,
    CYCLES_PER_EDGE_DMG,
    MAX_CYCLES,
    LocalBackend,
    NullBackend,
    SerialCore,
)

# ---------------------------------------------------------------------------
# Defaults & arming behavior
# ---------------------------------------------------------------------------


def test_latch_backend_error_preserves_first_cause_and_rejects_nonexception():
    serial = SerialCore()
    with pytest.raises(TypeError, match="BaseException"):
        serial.latch_backend_error("not an exception")
    assert serial.backend_failed is False

    first = ValueError("first owner failure")
    serial.latch_backend_error(first)

    assert serial.backend_failed is True
    with pytest.raises(SerialBackendError) as raised:
        serial.check_error()
    assert raised.value.__cause__ is first

    second = RuntimeError("later transport failure")
    serial.latch_backend_error(second)
    with pytest.raises(SerialBackendError) as repeated:
        serial.check_error()
    assert repeated.value.__cause__ is first


def test_latched_backend_error_fail_closes_serial_operations_and_allows_release():
    serial = SerialCore()
    serial.set_SB(0xA5)
    serial.set_SC(0x81)
    before = (
        serial.SB,
        serial.SC,
        serial._shift_register,
        serial._bits_remaining,
        serial.transfer_enabled,
        serial.clock,
        serial.last_cycles,
        serial.clock_target,
    )
    token = serial.claim_owner_pump(lambda _event: None, poll=True)
    serial.latch_backend_error(ValueError("owner failure"))

    assert serial.tick(serial.clock_target + CYCLES_PER_EDGE_DMG) is False
    assert (
        serial.SB,
        serial.SC,
        serial._shift_register,
        serial._bits_remaining,
        serial.transfer_enabled,
        serial.clock,
        serial.last_cycles,
        serial.clock_target,
    ) == before
    serial.set_SB(0)
    serial.set_SC(0)
    assert (
        serial.SB,
        serial.SC,
        serial._shift_register,
        serial._bits_remaining,
        serial.transfer_enabled,
        serial.clock,
        serial.last_cycles,
        serial.clock_target,
    ) == before
    for operation in (
        lambda: serial.apply_external_edge(1),
        lambda: serial.save_state(None),
        lambda: serial.load_state(None, SerialCore.STATE_VERSION),
        serial.check_error,
    ):
        with pytest.raises(SerialBackendError):
            operation()

    serial.release_owner_pump(token)
    assert serial.owner_poll_enabled is False
    assert serial.backend_failed is True


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
    """CGB double speed keeps normal serial edges at 512 CPU cycles."""
    s = SerialCore(cgb_mode=True, backend=NullBackend())
    s.cpu_speed_shift = 1
    s.set_SB(0xAA)
    s.set_SC(0x81)

    # The normal serial clock and the double-speed CPU use the same
    # oscillator-cycle time domain, so the edge period remains 512.
    assert s.clock_target == CYCLES_PER_EDGE_DMG
    assert s.tick(CYCLES_PER_EDGE_DMG - 1) is False
    assert s.transfer_enabled == 1
    assert s.tick(CYCLES_PER_EDGE_DMG) is False
    assert s.transfer_enabled == 1
    assert s.tick(CYCLES_PER_BYTE_DMG - 1) is False
    assert s.transfer_enabled == 1
    assert s.tick(CYCLES_PER_BYTE_DMG) is True
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
    """CGB SC=0x83 uses 16 CPU cycles per edge at either CPU speed."""
    s = SerialCore(cgb_mode=True, backend=NullBackend())
    s.cpu_speed_shift = cpu_speed_shift
    s.set_SB(0xAA)
    s.set_SC(0x83)

    fast_edge_cycles = CYCLES_PER_EDGE_CGB_FAST
    fast_byte_cycles = 8 * fast_edge_cycles
    assert fast_edge_cycles == 16
    assert fast_byte_cycles == 128
    # At cpu_speed_shift=1 both the CPU and fast serial clocks double, so
    # the oscillator-cycle period stays 16; using 8 would double the rate
    # twice.
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

    initial_edge_cycles = (
        CYCLES_PER_EDGE_CGB_FAST
        if initial_sc & 0x02
        else CYCLES_PER_EDGE_DMG
    )
    s.tick(initial_edge_cycles)
    shift_before = s._shift_register
    bits_before = s._bits_remaining
    deadline_before = s.clock_target

    s.set_SC(next_sc)

    assert s._shift_register == shift_before
    assert s._bits_remaining == bits_before
    next_edge_cycles = (
        CYCLES_PER_EDGE_CGB_FAST
        if next_sc & 0x02
        else CYCLES_PER_EDGE_DMG
    )
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

    # After each 512-cycle edge, one more bit has been shifted.
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
    assert s._cycles_to_interrupt == MAX_CYCLES


@pytest.mark.parametrize("sc", [0x00, 0x01, 0x80])
def test_unscheduled_hint_stays_max_across_large_cycle_boundaries(sc):
    s = SerialCore()
    s.set_SC(sc)
    for cycles in (1, (1 << 31) - 1, (1 << 31) + 1, (1 << 32) + 1):
        assert s.tick(cycles) is False
        assert s.clock == cycles
        assert s.last_cycles == cycles
        assert s._cycles_to_interrupt == MAX_CYCLES
        assert s.tick(cycles) is False  # repeated timestamp
        assert s._cycles_to_interrupt == MAX_CYCLES
        s.set_SC(sc)
        assert s._cycles_to_interrupt == MAX_CYCLES
        assert s._bits_remaining == (8 if sc & 0x80 else 0)


@pytest.mark.parametrize("cycles", [(1 << 31) + 17, (1 << 32) + 17])
@pytest.mark.parametrize("sc", [0x00, 0x80])
@pytest.mark.parametrize("stale_hint", [0, 23])
def test_zero_delta_tick_repairs_unscheduled_hint(cycles, sc, stale_hint):
    s = SerialCore()
    s.tick(cycles)
    s.set_SC(sc)
    s._cycles_to_interrupt = stale_hint
    assert s.tick(cycles) is False
    assert s._cycles_to_interrupt == MAX_CYCLES
    assert s.clock == cycles
    assert s._bits_remaining == (8 if sc & 0x80 else 0)


@pytest.mark.parametrize("cycles", [(1 << 31) + 17, (1 << 32) + 17])
def test_external_partial_rearm_abort_and_completion_keep_max_hint(cycles):
    s = SerialCore()
    s.tick(cycles)
    s.set_SB(0xA5)
    s.set_SC(0x80)
    assert s._cycles_to_interrupt == MAX_CYCLES
    for bit in (1, 0, 1):
        assert s.apply_external_edge(bit) is False
        assert s._cycles_to_interrupt == MAX_CYCLES
    partial = s._shift_register
    assert s._bits_remaining == 5
    s.set_SB(0x3C)
    s.set_SC(0x80)
    assert s._shift_register == partial
    assert s._bits_remaining == 5
    assert s._cycles_to_interrupt == MAX_CYCLES
    assert s.tick(cycles + 4096) is False
    assert s._shift_register == partial
    assert s._bits_remaining == 5
    assert s._cycles_to_interrupt == MAX_CYCLES

    s.set_SC(0x00)
    assert s.transfer_enabled == 0
    assert s._bits_remaining == 0
    assert s._cycles_to_interrupt == MAX_CYCLES
    with pytest.raises(RuntimeError, match="no transfer armed"):
        s.apply_external_edge(1)
    s.set_SC(0x80)
    assert s._shift_register == 0x3C
    assert s._bits_remaining == 8
    assert s._cycles_to_interrupt == MAX_CYCLES
    for index, bit in enumerate((1, 0, 1, 1, 0, 0, 1, 0)):
        assert s.apply_external_edge(bit) is (index == 7)
        assert s._cycles_to_interrupt == MAX_CYCLES
    assert s.SB == 0xB2
    assert s.SC & 0x80 == 0
    assert s.transfer_enabled == 0
    assert s.tick(cycles + 4097) is False
    assert s._cycles_to_interrupt == MAX_CYCLES


@pytest.mark.parametrize("cycles", [(1 << 31) + 17, (1 << 32) + 17])
@pytest.mark.parametrize("sc, period", [(0x81, 512), (0x83, 16)])
@pytest.mark.parametrize("cpu_speed_shift", [0, 1])
def test_internal_cadence_at_large_clocks(cycles, sc, period, cpu_speed_shift):
    s = SerialCore(cgb_mode=True, backend=NullBackend())
    s.cpu_speed_shift = cpu_speed_shift
    s.tick(cycles)
    s.set_SB(0)
    s.set_SC(sc)
    for edge in range(1, 9):
        deadline = cycles + edge * period
        assert s.clock_target == deadline
        assert s._cycles_to_interrupt == period
        assert s.tick(deadline - 1) is False
        assert s._bits_remaining == 9 - edge
        assert s._cycles_to_interrupt == 1
        assert s.tick(deadline) is (edge == 8)
        assert s._bits_remaining == 8 - edge
    assert s.SB == 0xFF
    assert s.transfer_enabled == 0
    assert s._cycles_to_interrupt == MAX_CYCLES
    assert s.tick(deadline + 1) is False
    assert s._cycles_to_interrupt == MAX_CYCLES


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


def test_loading_pre_timing_extension_retimes_in_flight_transfer():
    """Known 128-cycle provenance explicitly opts into timing migration."""
    original = SerialCore(cgb_mode=False, backend=NullBackend())
    original.set_SB(0x42)
    original.set_SC(0x81)
    original.tick(64)  # old scheduler: halfway to the first 128-cycle edge

    stream = _FakeStream()
    original.save_state(stream)
    # Drop the new marker/version, retaining the pre-correction extension.
    stream._buf = stream._buf[:10]
    # Simulate the old scheduler's first-edge deadline (the current stream
    # was produced by the corrected implementation above).
    stream._buf[7] = ("u64", 128)

    restored = SerialCore(cgb_mode=False, backend=NullBackend())
    restored.load_state(stream, SerialCore.STATE_VERSION, legacy_timing=128)

    assert restored.transfer_enabled == 1
    assert restored._bits_remaining == 8
    assert restored.clock == 64
    # 64 old cycles remaining represents 256 cycles in the corrected domain.
    assert restored.clock_target == 320
    assert restored._cycles_to_interrupt == 256


@pytest.mark.parametrize("sc, period", [(0x81, 512), (0x83, 16)])
def test_untagged_hardware_cadence_state_preserves_deadline_without_provenance(sc, period):
    original = SerialCore(cgb_mode=True)
    original.set_SB(0x42)
    original.set_SC(sc)
    original.tick(3 * period + period // 2)
    stream = _FakeStream()
    original.save_state(stream)
    stream._buf = stream._buf[:10]

    restored = SerialCore(cgb_mode=True)
    restored.load_state(stream, SerialCore.STATE_VERSION)

    assert restored.clock_target == original.clock_target
    assert restored._cycles_to_interrupt == original._cycles_to_interrupt
    assert restored._bits_remaining == original._bits_remaining
    assert restored._shift_register == original._shift_register


def test_loading_current_timing_extension_preserves_deadline():
    original = SerialCore(cgb_mode=True, backend=NullBackend())
    original.set_SB(0x42)
    original.set_SC(0x83)
    original.tick(3 * CYCLES_PER_EDGE_CGB_FAST)

    stream = _FakeStream()
    original.save_state(stream)

    restored = SerialCore(cgb_mode=True, backend=NullBackend())
    restored.load_state(stream, SerialCore.STATE_VERSION)

    assert restored.clock_target == original.clock_target
    assert restored._bits_remaining == original._bits_remaining


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


@pytest.mark.parametrize("cycles", [(1 << 31) + 17, (1 << 32) + 17])
@pytest.mark.parametrize("field_count", [8, 10, 12], ids=["legacy", "unmarked", "current"])
@pytest.mark.parametrize("sc", [0x00, 0x01, 0x80])
@pytest.mark.parametrize("saved_hint", [0, 23])
def test_loading_unscheduled_large_clock_state_repairs_hint(
    cycles, field_count, sc, saved_hint
):
    original = SerialCore()
    original.tick(cycles)
    original.set_SB(0xA5)
    original.set_SC(sc)
    if sc & 0x80:
        original.apply_external_edge(1)
        original.apply_external_edge(0)
    stream = _FakeStream()
    original.save_state(stream)
    stream._buf = stream._buf[:field_count]
    # Older writers cached zero or a finite countdown for an unscheduled
    # core. Loading must recover the scheduling invariant in either case.
    stream._buf[5] = ("u64", saved_hint)

    restored = SerialCore()
    restored.load_state(stream, SerialCore.STATE_VERSION)
    assert restored.clock == cycles
    assert restored.last_cycles == cycles
    assert restored._cycles_to_interrupt == MAX_CYCLES
    assert restored.tick(cycles) is False
    assert restored._cycles_to_interrupt == MAX_CYCLES
    assert restored.tick(cycles + 1) is False
    assert restored._cycles_to_interrupt == MAX_CYCLES
    if field_count == 8:
        assert restored.transfer_enabled == 0
        assert restored.SC & 0x80 == 0
        assert restored._bits_remaining == 0
        assert restored._shift_register == restored.SB
    elif sc & 0x80:
        assert restored._bits_remaining == 6
        assert restored._shift_register == original._shift_register
        for index, bit in enumerate((1, 1, 0, 0, 1, 0)):
            assert restored.apply_external_edge(bit) is (index == 5)
            assert restored._cycles_to_interrupt == MAX_CYCLES
        assert restored.SB == 0xB2
    else:
        assert restored.transfer_enabled == 0


@pytest.mark.parametrize("cycles", [(1 << 31) + 17, (1 << 32) + 17])
@pytest.mark.parametrize("legacy", [False, True], ids=["current", "unmarked"])
@pytest.mark.parametrize("sc, period", [(0x81, 512), (0x83, 16)])
@pytest.mark.parametrize("cpu_speed_shift", [0, 1])
def test_restored_internal_large_clock_transfer_keeps_cadence(
    cycles, legacy, sc, period, cpu_speed_shift
):
    original = SerialCore(cgb_mode=True)
    original.cpu_speed_shift = cpu_speed_shift
    original.tick(cycles)
    original.set_SB(0)
    original.set_SC(sc)
    original.tick(cycles + 3 * period + period // 2)
    stream = _FakeStream()
    original.save_state(stream)
    if legacy:
        stream._buf = stream._buf[:10]
        old_period = 4 if sc == 0x83 else 128 << cpu_speed_shift
        stream._buf[5] = ("u64", old_period // 2)
        stream._buf[7] = ("u64", original.clock + old_period // 2)

    restored = SerialCore(cgb_mode=True)
    # CPU speed belongs to the motherboard, not the serial state stream.
    restored.cpu_speed_shift = cpu_speed_shift
    restored.load_state(
        stream, SerialCore.STATE_VERSION, legacy_timing=128 if legacy else None
    )
    assert restored.clock == original.clock
    assert restored.last_cycles == original.last_cycles
    assert restored._bits_remaining == 5
    assert restored._shift_register == 7
    assert restored.clock_target == original.clock + period // 2
    assert restored._cycles_to_interrupt == period // 2
    for edge in range(5):
        deadline = original.clock + period // 2 + edge * period
        assert restored.clock_target == deadline
        assert restored.tick(deadline - 1) is False
        assert restored._bits_remaining == 5 - edge
        assert restored._cycles_to_interrupt == 1
        assert restored.tick(deadline) is (edge == 4)
        assert restored._bits_remaining == 4 - edge
        assert restored._cycles_to_interrupt == (MAX_CYCLES if edge == 4 else period)
    assert restored.SB == 0xFF
    assert restored.transfer_enabled == 0
    assert restored.tick(deadline + 1) is False
    assert restored._cycles_to_interrupt == MAX_CYCLES
