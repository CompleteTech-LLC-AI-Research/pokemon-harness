"""Pending externally clocked state survives deadline-cache repair."""

from io import BytesIO

import pytest
from pyboy.utils import IntIOWrapper

from pokered_harness.link.serial_core import MAX_CYCLES, SerialCore
from tests.test_serial_core import _FakeStream


@pytest.mark.parametrize("cgb_mode", [False, True])
def test_partial_external_state_restores_remaining_bits_at_large_clock(cgb_mode):
    base = 1 << 40
    original = SerialCore(cgb_mode)
    original.tick(base)
    original.set_SB(0x96)
    original.set_SC(0x80)
    received_bits = (1, 0, 1, 0, 0, 1, 0, 1)
    for bit in received_bits[:3]:
        assert original.apply_external_edge(bit) is False
    assert original._bits_remaining == 5
    original._cycles_to_interrupt = 0  # Cached value from the old scheduler.
    stream = _FakeStream()
    original.save_state(stream)

    restored = SerialCore(cgb_mode)
    restored.load_state(stream, SerialCore.STATE_VERSION)
    assert restored.clock == base
    assert restored._cycles_to_interrupt == MAX_CYCLES
    assert restored._bits_remaining == 5
    assert restored._shift_register == original._shift_register
    assert restored.SB == 0x96
    assert restored.SC == original.SC
    assert restored.tick(base + 8192) is False
    assert restored._bits_remaining == 5
    for index, bit in enumerate(received_bits[3:]):
        assert restored.apply_external_edge(bit) is (index == 4)
    assert restored.SB == 0xA5
    assert restored.SC & 0x80 == 0
    assert restored._bits_remaining == 0
    assert restored._cycles_to_interrupt == MAX_CYCLES
    assert restored.tick(base + 16384) is False
    assert restored._cycles_to_interrupt == MAX_CYCLES


def _byte_roundtrip(original):
    buffer = BytesIO()
    stream = IntIOWrapper(buffer)
    original.save_state(stream)
    stream.seek(0)
    restored = SerialCore(original.cgb_mode)
    restored.load_state(stream, SerialCore.STATE_VERSION)
    assert stream.tell() == len(buffer.getvalue())
    return restored, buffer.getvalue()


@pytest.mark.parametrize("cgb_mode", [False, True])
def test_old_active_master_preserves_next_deadline_then_uses_normal_period(cgb_mode):
    original = SerialCore(cgb_mode)
    original.set_SB(0x96)
    original.set_SC(0x81)
    original.tick(3 * 512)
    assert original._bits_remaining == 5
    # Synthesize an old 128-T-cycle save at its fourth-bit boundary.
    original.clock = original.last_cycles = 384
    original.clock_target = 512
    original._cycles_to_interrupt = 128
    restored, _ = _byte_roundtrip(original)
    assert restored.clock_target == 512
    assert restored._cycles_to_interrupt == 128
    assert restored._shift_register == original._shift_register
    assert restored.tick(511) is False
    assert restored._bits_remaining == 5
    assert restored.tick(512) is False
    assert restored._bits_remaining == 4
    assert restored.clock_target == 1024
    for deadline, remaining in ((1024, 3), (1536, 2), (2048, 1)):
        assert restored.tick(deadline - 1) is False
        assert restored._bits_remaining == remaining + 1
        assert restored.tick(deadline) is False
        assert restored._bits_remaining == remaining
    assert restored.tick(2559) is False
    assert restored.tick(2560) is True
    assert restored.SB == 0xFF
    assert restored.SC & 0x80 == 0
    assert restored.tick(2560) is False
    assert restored.tick(8192) is False
    assert restored._cycles_to_interrupt == MAX_CYCLES


@pytest.mark.parametrize("cgb_mode", [False, True])
@pytest.mark.parametrize("control", [0x00, 0x80, 0x01])
def test_old_negative_cached_wait_bytes_repaired(cgb_mode, control):
    original = SerialCore(cgb_mode)
    original.tick(1 << 40)
    original.set_SB(0x69)
    original.set_SC(control)
    stale_wait = MAX_CYCLES - original.clock
    # Old source saves can encode a negative wait modulo 2**64. Native
    # write_64bit rejects negatives, so construct those historical bytes
    # explicitly rather than requiring native serialization of bad state.
    _, valid_encoded = _byte_roundtrip(original)
    encoded = (
        valid_encoded[:12]
        + (stale_wait % (1 << 64)).to_bytes(8, "little")
        + valid_encoded[20:]
    )
    restored = SerialCore(cgb_mode)
    restored.load_state(IntIOWrapper(BytesIO(encoded)), SerialCore.STATE_VERSION)
    # Four u8 fields, then last_cycles u64, then cached wait u64.
    assert int.from_bytes(encoded[12:20], "little") == stale_wait % (1 << 64)
    assert restored._cycles_to_interrupt == MAX_CYCLES
    assert restored.clock_target == MAX_CYCLES
    assert restored.clock == original.clock
    assert restored.last_cycles == original.last_cycles
    assert restored.SC == original.SC
    assert restored.SB == original.SB
    assert restored._shift_register == original._shift_register
    assert restored._bits_remaining == original._bits_remaining
    assert restored.tick(original.clock) is False
    assert restored.tick(original.clock + 4096) is False
    assert restored._cycles_to_interrupt == MAX_CYCLES


@pytest.mark.parametrize("cgb_mode", [False, True])
def test_canceled_internal_selection_roundtrips_without_restarting(cgb_mode):
    original = SerialCore(cgb_mode)
    original.tick(1 << 40)
    original.set_SB(0x42)
    original.set_SC(0x81)
    original.tick((1 << 40) + 512)
    original.set_SC(0x01)
    assert original.internal_clock and not original.transfer_enabled
    restored, _ = _byte_roundtrip(original)
    assert restored.internal_clock and not restored.transfer_enabled
    assert restored._bits_remaining == 0
    assert restored.SC == original.SC
    assert restored.SB == original.SB
    assert restored._shift_register == original._shift_register
    assert restored._cycles_to_interrupt == MAX_CYCLES
    assert restored.tick(original.clock + 4096) is False
    assert restored._cycles_to_interrupt == MAX_CYCLES
