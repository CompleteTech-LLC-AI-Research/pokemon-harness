from __future__ import annotations

import pytest

from pokered_harness.state.progress import (
    ALL_BADGES,
    Badge,
    parse_progress,
    read_event_flag,
)
from pokered_harness.symbols.loader import load_sym_text

_OPTIONAL_PROGRESS_SYMBOLS = (
    ("wPlayerID", 0xC010, "trainer_id"),
    ("wPlayerMoney", 0xC020, "money"),
    ("wPlayTimeHours", 0xC030, "play_time_hours"),
    ("wPlayTimeMaxed", 0xC031, "play_time_maxed"),
    ("wPlayTimeMinutes", 0xC032, "play_time_minutes"),
    ("wPlayTimeSeconds", 0xC033, "play_time_seconds"),
)


def _relocated_progress_symbols(*, without: str | None = None):
    lines = ["00:C001 wObtainedBadges"]
    lines.extend(
        f"00:{addr:04X} {name}"
        for name, addr, _field in _OPTIONAL_PROGRESS_SYMBOLS
        if name != without
    )
    return load_sym_text("\n".join(lines))


def _write_relocated_progress(mem):
    mem[0xC001] = 1 << Badge.THUNDER.value
    mem[0xC010] = 0x12
    mem[0xC011] = 0x34
    mem[0xC020] = 0x12
    mem[0xC021] = 0x34
    mem[0xC022] = 0x56
    mem[0xC030] = 7
    mem[0xC031] = 1
    mem[0xC032] = 42
    mem[0xC033] = 58


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


def test_progress_reads_every_field_from_its_symbol(mem):
    sym = _relocated_progress_symbols()
    _write_relocated_progress(mem)

    state = parse_progress(mem, sym)

    assert state.badges_raw == 1 << Badge.THUNDER.value
    assert state.trainer_id == 0x1234
    assert state.money == 123456
    assert state.play_time_hours == 7
    assert state.play_time_maxed is True
    assert state.play_time_minutes == 42
    assert state.play_time_seconds == 58


@pytest.mark.parametrize(
    ("missing_symbol", "field"),
    [
        (name, field)
        for name, _addr, field in _OPTIONAL_PROGRESS_SYMBOLS
        if name != "wPlayTimeMaxed"
    ],
)
def test_missing_optional_progress_symbol_is_unknown(mem, missing_symbol, field):
    sym = _relocated_progress_symbols(without=missing_symbol)
    _write_relocated_progress(mem)

    state = parse_progress(mem, sym)

    assert getattr(state, field) is None
    # Other symbol-backed values remain observable when only one optional
    # label is absent; absence must not invalidate the whole snapshot.
    assert state.badges_raw == 1 << Badge.THUNDER.value
    assert state.trainer_id == (None if field == "trainer_id" else 0x1234)
    assert state.money == (None if field == "money" else 123456)
    assert state.play_time_hours == (
        None if field == "play_time_hours" else 7
    )
    assert state.play_time_minutes == (
        None if field == "play_time_minutes" else 42
    )
    assert state.play_time_seconds == (
        None if field == "play_time_seconds" else 58
    )
    assert state.play_time_maxed is True


def test_present_zero_progress_values_are_known(mem):
    sym = _relocated_progress_symbols()

    state = parse_progress(mem, sym)

    assert state.trainer_id == 0
    assert state.money == 0
    assert state.play_time_hours == 0
    assert state.play_time_minutes == 0
    assert state.play_time_seconds == 0
    assert state.play_time_maxed is False


def test_progress_requires_badges_symbol(mem):
    sym = load_sym_text("00:C010 wPlayerID\n")

    with pytest.raises(KeyError, match="wObtainedBadges"):
        parse_progress(mem, sym)


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


def test_event_flag_reads_from_symbol_declared_base(mem):
    sym = load_sym_text("00:C100 wEventFlags\n")
    mem[0xC100 + 1] = 1 << 3

    assert read_event_flag(mem, sym, 11) is True


def test_event_flag_negative_bit_rejected(mem, symbols):
    with pytest.raises(ValueError):
        read_event_flag(mem, symbols, -1)


def test_event_flag_requires_symbol_base(mem):
    with pytest.raises(KeyError, match="wEventFlags"):
        read_event_flag(mem, load_sym_text(""), 0)


def test_progress_optional_symbols_absent(mem):
    sym = load_sym_text("00:D356 wObtainedBadges\n")
    state = parse_progress(mem, sym)
    assert state.badges_raw == 0
    assert state.trainer_id is None
    assert state.money is None
    assert state.play_time_hours is None
    assert state.play_time_minutes is None
    assert state.play_time_seconds is None
    # Keep the existing bool API: without this optional label, False is a
    # compatibility default, not evidence that the flag was observed.
    assert state.play_time_maxed is False
