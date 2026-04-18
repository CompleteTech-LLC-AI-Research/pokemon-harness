from __future__ import annotations

from pokered_harness.state.text import parse_text
from pokered_harness.symbols.loader import load_sym_text


def test_text_dest_in_vram_tilemap_detected(mem, symbols):
    # Text box writing into BG tilemap at $9C00 (second tilemap region).
    mem.write_word_le(0xCC3A, 0x9C00)
    state = parse_text(mem, symbols)
    assert state.text_dest_addr == 0x9C00
    assert state.dest_in_vram_tilemap is True


def test_text_dest_outside_vram_tilemap(mem, symbols):
    mem.write_word_le(0xCC3A, 0xC000)  # WRAM, not a tilemap
    state = parse_text(mem, symbols)
    assert state.text_dest_addr == 0xC000
    assert state.dest_in_vram_tilemap is False


def test_text_dest_zero_is_inactive(mem, symbols):
    # wTextDest defaults to 0 in our dict; treat as inactive.
    state = parse_text(mem, symbols)
    assert state.text_dest_addr == 0
    assert state.dest_in_vram_tilemap is False


def test_text_dest_at_tilemap_boundary(mem, symbols):
    mem.write_word_le(0xCC3A, 0x9800)  # low bound inclusive
    assert parse_text(mem, symbols).dest_in_vram_tilemap is True
    mem.write_word_le(0xCC3A, 0x9FFF)  # high bound inclusive
    assert parse_text(mem, symbols).dest_in_vram_tilemap is True
    mem.write_word_le(0xCC3A, 0xA000)  # just outside
    assert parse_text(mem, symbols).dest_in_vram_tilemap is False


def test_suppress_prompt_wait_flag(mem, symbols):
    mem[0xCC3C] = 1  # wDoNotWaitForButtonPressAfterDisplayingText
    state = parse_text(mem, symbols)
    assert state.suppress_prompt_wait is True


def test_text_parser_tolerates_missing_symbols(mem):
    sym = load_sym_text("")  # completely empty
    state = parse_text(mem, sym)
    assert state.text_dest_addr is None
    assert state.suppress_prompt_wait is False
    assert state.dest_in_vram_tilemap is False
