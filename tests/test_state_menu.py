from __future__ import annotations

from pokered_harness.state.menu import parse_menu
from pokered_harness.symbols.loader import load_sym_text


def test_menu_parses_basic_cursor(mem, symbols):
    mem[0xCC26] = 2  # wCurrentMenuItem
    mem[0xCC28] = 5  # wMaxMenuItem
    state = parse_menu(mem, symbols)
    assert state.current_item == 2
    assert state.max_item == 5
    assert state.cursor_at_top is False


def test_menu_cursor_at_top(mem, symbols):
    # current=0, scroll=0 — topmost entry of the list.
    state = parse_menu(mem, symbols)
    assert state.cursor_at_top is True


def test_menu_cursor_at_top_false_when_scrolled(mem, symbols):
    mem[0xCC36] = 3  # wListScrollOffset — list has been scrolled down
    state = parse_menu(mem, symbols)
    assert state.current_item == 0
    assert state.list_scroll_offset == 3
    assert state.cursor_at_top is False


def test_menu_saved_submenu_cursors(mem, symbols):
    mem[0xCC2B] = 4  # saved_party_pc
    mem[0xCC2C] = 7  # saved_bag
    mem[0xCC2D] = 2  # saved_battle_start
    state = parse_menu(mem, symbols)
    assert state.saved_party_pc == 4
    assert state.saved_bag == 7
    assert state.saved_battle_start == 2


def test_menu_missing_optional_symbols(mem):
    # Only the two required symbols — everything else should be None.
    sym = load_sym_text(
        """
        00:CC26 wCurrentMenuItem
        00:CC28 wMaxMenuItem
        """
    )
    state = parse_menu(mem, sym)
    assert state.watched_keys is None
    assert state.list_scroll_offset is None
    assert state.saved_party_pc is None
    assert state.saved_bag is None
    assert state.saved_battle_start is None
