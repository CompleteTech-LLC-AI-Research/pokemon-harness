from __future__ import annotations

import pytest

from pokered_harness.state.text import parse_text
from pokered_harness.symbols.loader import load_sym_text


def test_text_dest_in_vram_tilemap_detected(mem, symbols):
    # Text box writing into BG tilemap at $9C00 (second tilemap region).
    mem.write_word_le(symbols.addr_of("wTextDest"), 0x9C00)
    state = parse_text(mem, symbols)
    assert state.text_dest_addr == 0x9C00
    assert state.dest_in_vram_tilemap is True


@pytest.mark.parametrize(
    "dest_addr",
    (0x0001, 0x7FFF, 0x8000, 0x97FF, 0xA000, 0xC000, 0xFFFF),
    ids=lambda dest_addr: f"0x{dest_addr:04X}",
)
def test_text_dest_non_tilemap_value_is_known_but_not_active(mem, symbols, dest_addr):
    mem.write_word_le(symbols.addr_of("wTextDest"), dest_addr)
    state = parse_text(mem, symbols)
    # Preserve the raw symbol-backed value so callers can distinguish a
    # known non-text destination from a missing symbol (unknown state).
    assert state.text_dest_addr == dest_addr
    assert state.dest_in_vram_tilemap is False


def test_text_dest_zero_is_inactive(mem, symbols):
    # A present symbol whose bytes are zero is a known inactive value, not an
    # unknown value caused by a missing symbol.
    state = parse_text(mem, symbols)
    assert state.text_dest_addr == 0
    assert state.dest_in_vram_tilemap is False


def test_text_dest_at_tilemap_boundary(mem, symbols):
    text_dest_addr = symbols.addr_of("wTextDest")
    mem.write_word_le(text_dest_addr, 0x9800)  # low bound inclusive
    assert parse_text(mem, symbols).dest_in_vram_tilemap is True
    mem.write_word_le(text_dest_addr, 0x9FFF)  # high bound inclusive
    assert parse_text(mem, symbols).dest_in_vram_tilemap is True
    mem.write_word_le(text_dest_addr, 0xA000)  # just outside
    assert parse_text(mem, symbols).dest_in_vram_tilemap is False


def test_suppress_prompt_wait_flag(mem, symbols):
    mem[symbols.addr_of("wDoNotWaitForButtonPressAfterDisplayingText")] = 1
    state = parse_text(mem, symbols)
    assert state.suppress_prompt_wait is True


def test_text_parser_reads_the_supplied_symbol_addresses(mem):
    # Per-version .sym files are authoritative; neither parser field may
    # silently fall back to the fixture's canonical addresses.
    sym = load_sym_text(
        "00:C100 wTextDest\n"
        "00:C102 wDoNotWaitForButtonPressAfterDisplayingText\n"
    )
    mem.write_word_le(0xC100, 0x9C00)
    mem[0xC102] = 1
    mem.write_word_le(0xCC3A, 0xC000)
    mem[0xCC3C] = 0

    state = parse_text(mem, sym)

    assert state.text_dest_addr == 0x9C00
    assert state.dest_in_vram_tilemap is True
    assert state.suppress_prompt_wait is True


def test_missing_text_dest_is_unknown_not_a_guessed_value(mem):
    sym = load_sym_text(
        "00:CC3C wDoNotWaitForButtonPressAfterDisplayingText\n"
    )
    # Even if the canonical address happens to contain an active-looking
    # pointer, no wTextDest symbol means that destination state is unknown.
    mem.write_word_le(0xCC3A, 0x9C00)
    mem[0xCC3C] = 1

    state = parse_text(mem, sym)

    assert state.text_dest_addr is None
    assert state.dest_in_vram_tilemap is False
    assert state.suppress_prompt_wait is True


def test_missing_wait_flag_keeps_legacy_false_without_reading_guess(mem):
    sym = load_sym_text("00:CC3A wTextDest\n")
    mem.write_word_le(0xCC3A, 0x9C00)
    # The flag's canonical address is deliberately nonzero, but its symbol is
    # absent. Preserve the public bool default without reading that address.
    mem[0xCC3C] = 1

    state = parse_text(mem, sym)

    assert state.text_dest_addr == 0x9C00
    assert state.dest_in_vram_tilemap is True
    assert state.suppress_prompt_wait is False


def test_text_parser_tolerates_missing_symbols(mem):
    sym = load_sym_text("")  # completely empty
    state = parse_text(mem, sym)
    assert state.text_dest_addr is None
    assert state.suppress_prompt_wait is False
    assert state.dest_in_vram_tilemap is False
