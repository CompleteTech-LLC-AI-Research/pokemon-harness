from __future__ import annotations

import pytest

from pokered_harness.state.base import parse_status


def test_status_healthy_zero():
    s = parse_status(0)
    assert s.is_healthy is True
    assert s.is_valid is True
    assert s.is_unknown is False
    assert s.unknown_bits == 0
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
    # the decoder must still be a pure bit decomposition.  ``is_valid`` here
    # means that every bit is part of the known byte schema; it does not
    # claim that this combination can be produced by normal gameplay.
    raw = (1 << 3) | (1 << 4) | (1 << 5) | (1 << 6)
    s = parse_status(raw)
    assert s.poisoned and s.burned and s.frozen and s.paralyzed
    assert s.sleep_turns == 0
    assert s.unknown_bits == 0
    assert s.is_valid is True
    assert s.is_unknown is False
    assert s.is_healthy is False


def test_status_reserved_bit_is_unknown_not_healthy():
    # Bit 7 is unused by the Gen-1 status schema.  It must not be guessed as
    # another condition or silently treated as a healthy status.
    s = parse_status(0b1000_0000)
    assert s.raw == 0x80
    assert s.unknown_bits == 0x80
    assert s.is_valid is False
    assert s.is_unknown is True
    assert s.is_healthy is False
    assert not any([s.asleep, s.poisoned, s.burned, s.frozen, s.paralyzed])


def test_status_clamps_input_to_byte_without_losing_unknown_semantics():
    # Keep the historical integer-normalization behavior for callers that
    # provide a wider value, while ensuring the normalized reserved bit is
    # still reported as unknown rather than healthy.
    s = parse_status(0x1_FF)
    assert s.raw == 0xFF
    assert s.unknown_bits == 0x80
    assert s.is_valid is False
    assert s.is_unknown is True
    assert s.is_healthy is False


@pytest.mark.parametrize("raw", range(0x100))
def test_status_decomposition_is_lossless_for_every_byte(raw):
    """Every byte has explicit known-condition and reserved-bit semantics."""
    s = parse_status(raw)

    assert s.raw == raw
    assert s.sleep_turns == raw & 0b0000_0111
    assert s.asleep is ((raw & 0b0000_0111) != 0)
    assert s.poisoned is bool(raw & (1 << 3))
    assert s.burned is bool(raw & (1 << 4))
    assert s.frozen is bool(raw & (1 << 5))
    assert s.paralyzed is bool(raw & (1 << 6))
    assert s.unknown_bits == raw & (1 << 7)
    assert s.is_valid is ((raw & (1 << 7)) == 0)
    assert s.is_unknown is ((raw & (1 << 7)) != 0)
    assert s.is_healthy is (raw == 0)
