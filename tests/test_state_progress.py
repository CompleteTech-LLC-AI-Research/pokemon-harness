from __future__ import annotations

import pytest

from pokered_harness.state.progress import (
    ALL_BADGES,
    Badge,
    parse_progress,
    read_event_flag,
)
from pokered_harness.symbols.loader import load_sym_text


def test_no_badges_default(mem, symbols):
    state = parse_progress(mem, symbols)
    assert state.badges_raw == 0
    assert state.badges == ()
    assert state.badge_count == 0


def test_single_badge(mem, symbols):
    mem[0xD356] = 1 << Badge.BOULDER.value
    state = parse_progress(mem, symbols)
    assert state.has_badge(Badge.BOULDER) is True
    assert state.has_badge(Badge.CASCADE) is False
    assert state.badges == (Badge.BOULDER,)
    assert state.badge_count == 1


def test_all_badges(mem, symbols):
    mem[0xD356] = 0xFF
    state = parse_progress(mem, symbols)
    assert state.badges == ALL_BADGES
    assert state.badge_count == 8


def test_trainer_id_big_endian(mem, symbols):
    # TID = 12345 → 0x3039 → hi=0x30, lo=0x39
    mem[0xD359] = 0x30
    mem[0xD35A] = 0x39
    state = parse_progress(mem, symbols)
    assert state.trainer_id == 12345


def test_money_bcd_decoding(mem, symbols):
    # $123,456 → bytes [0x12, 0x34, 0x56]
    mem[0xD347] = 0x12
    mem[0xD348] = 0x34
    mem[0xD349] = 0x56
    state = parse_progress(mem, symbols)
    assert state.money == 123456


def test_money_zero(mem, symbols):
    state = parse_progress(mem, symbols)
    assert state.money == 0


def test_play_time_fields(mem, symbols):
    mem[0xDA40] = 2  # hours
    mem[0xDA41] = 0  # maxed flag
    mem[0xDA42] = 30  # minutes
    mem[0xDA43] = 15  # seconds
    state = parse_progress(mem, symbols)
    assert state.play_time_hours == 2
    assert state.play_time_minutes == 30
    assert state.play_time_seconds == 15
    assert state.play_time_maxed is False


def test_play_time_maxed(mem, symbols):
    mem[0xDA41] = 1
    state = parse_progress(mem, symbols)
    assert state.play_time_maxed is True


def test_event_flag_read_by_bit_index(mem, symbols):
    # base $D747, bit index 11 → byte offset 1, bit 3
    mem[0xD747 + 1] = 1 << 3
    assert read_event_flag(mem, symbols, 11) is True
    assert read_event_flag(mem, symbols, 10) is False
    assert read_event_flag(mem, symbols, 12) is False


def test_event_flag_first_and_last_byte_boundaries(mem, symbols):
    mem[0xD747] = 0b0000_0001
    assert read_event_flag(mem, symbols, 0) is True
    assert read_event_flag(mem, symbols, 7) is False
    mem[0xD747] = 0b1000_0000
    assert read_event_flag(mem, symbols, 7) is True


def test_event_flag_negative_bit_rejected(mem, symbols):
    with pytest.raises(ValueError):
        read_event_flag(mem, symbols, -1)


def test_progress_optional_symbols_absent(mem):
    sym = load_sym_text("00:D356 wObtainedBadges\n")
    state = parse_progress(mem, sym)
    assert state.badges_raw == 0
    assert state.trainer_id is None
    assert state.money is None
    assert state.play_time_hours is None
    assert state.play_time_maxed is False
