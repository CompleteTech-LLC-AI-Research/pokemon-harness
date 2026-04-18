from __future__ import annotations

from pokered_harness.state.base import parse_status


def test_status_healthy_zero():
    s = parse_status(0)
    assert s.is_healthy is True
    assert s.asleep is False
    assert s.sleep_turns == 0
    assert not any([s.poisoned, s.burned, s.frozen, s.paralyzed])


def test_status_sleep_counter_masks_low_three_bits():
    s = parse_status(0b0000_0101)  # sleep counter = 5
    assert s.sleep_turns == 5
    assert s.asleep is True
    assert s.is_healthy is False


def test_status_poisoned_bit():
    s = parse_status(1 << 3)
    assert s.poisoned is True
    assert s.burned is False


def test_status_all_conditions_simultaneously_impossible_but_decodes():
    # Non-volatile status bits are mutually exclusive in game logic, but
    # the decoder must still be a pure bit decomposition.
    raw = (1 << 3) | (1 << 4) | (1 << 5) | (1 << 6)
    s = parse_status(raw)
    assert s.poisoned and s.burned and s.frozen and s.paralyzed
    assert s.sleep_turns == 0


def test_status_ignores_high_bit():
    # Bit 7 is unused; the decoder should not treat it as a condition.
    s = parse_status(0b1000_0000)
    assert s.raw == 0x80
    assert not any([s.asleep, s.poisoned, s.burned, s.frozen, s.paralyzed])


def test_status_clamps_input_to_byte():
    s = parse_status(0x1_FF)
    assert s.raw == 0xFF
