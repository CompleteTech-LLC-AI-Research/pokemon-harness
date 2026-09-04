from __future__ import annotations

import pytest

from pokered_harness.state.menu import parse_menu
from pokered_harness.symbols.loader import load_sym_text


def test_menu_parses_basic_cursor(mem, symbols):
    mem[0xCC26] = 2  # wCurrentMenuItem
    mem[0xCC28] = 5  # wMaxMenuItem
    state = parse_menu(mem, symbols)
    assert state.current_item == 2
    assert state.max_item == 5
    assert state.cursor_valid is True
    assert state.cursor_at_top is False


def test_menu_reads_symbol_addresses_not_fixed_offsets(mem):
    # A generated Red/Blue/Yellow .sym is the address authority. Keep these
    # addresses deliberately away from the conventional CCxx locations so a
    # parser that guesses the layout cannot satisfy this contract.
    sym = load_sym_text(
        """
        00:8000 wCurrentMenuItem
        00:8001 wMaxMenuItem
        00:8002 wMenuWatchedKeys
        00:8003 wListScrollOffset
        00:8004 wPartyAndBillsPCSavedMenuItem
        00:8005 wBagSavedMenuItem
        00:8006 wBattleAndStartSavedMenuItem
        """
    )
    mem[0x8000] = 1
    mem[0x8001] = 3
    mem[0x8002] = 0xA5
    mem[0x8003] = 2
    mem[0x8004] = 4
    mem[0x8005] = 5
    mem[0x8006] = 6

    state = parse_menu(mem, sym)

    assert state.current_item == 1
    assert state.max_item == 3
    assert state.watched_keys == 0xA5
    assert state.list_scroll_offset == 2
    assert state.saved_party_pc == 4
    assert state.saved_bag == 5
    assert state.saved_battle_start == 6


def test_menu_cursor_at_top(mem, symbols):
    # current=0, scroll=0 — topmost entry of the list.
    state = parse_menu(mem, symbols)
    assert state.cursor_valid is True
    assert state.cursor_at_top is True


def test_menu_cursor_at_top_false_when_scrolled(mem, symbols):
    mem[0xCC28] = 3  # wMaxMenuItem — keep the full-list position valid
    mem[0xCC36] = 3  # wListScrollOffset — list has been scrolled down
    state = parse_menu(mem, symbols)
    assert state.current_item == 0
    assert state.list_scroll_offset == 3
    assert state.cursor_valid is True
    assert state.cursor_at_top is False


def test_menu_saved_submenu_cursors(mem, symbols):
    mem[0xCC2B] = 4  # saved_party_pc
    mem[0xCC2C] = 7  # saved_bag
    mem[0xCC2D] = 2  # saved_battle_start
    state = parse_menu(mem, symbols)
    assert state.saved_party_pc == 4
    assert state.saved_bag == 7
    assert state.saved_battle_start == 2


def test_menu_zero_is_known_when_optional_symbols_exist(mem, symbols):
    # A present symbol whose ROM-owned byte is zero is known zero. Unknown
    # means the symbol is unavailable, not that its observed value is zero.
    state = parse_menu(mem, symbols)

    assert state.watched_keys == 0
    assert state.list_scroll_offset == 0
    assert state.saved_party_pc == 0
    assert state.saved_bag == 0
    assert state.saved_battle_start == 0
    assert state.cursor_valid is True
    assert state.cursor_at_top is True


def test_menu_preserves_inconsistent_cursor_without_clamping(mem, symbols):
    # The parser has no menu-specific context with which to repair a stale or
    # partially initialized snapshot. Preserve the raw bytes and let callers
    # decide whether current_item <= max_item is a valid menu observation.
    mem[0xCC26] = 0xFF
    mem[0xCC28] = 2
    state = parse_menu(mem, symbols)

    assert state.current_item == 0xFF
    assert state.max_item == 2
    assert state.cursor_valid is False
    assert state.cursor_at_top is False


@pytest.mark.parametrize("missing_symbol", ("wCurrentMenuItem", "wMaxMenuItem"))
def test_menu_missing_required_symbol_fails_closed(mem, missing_symbol):
    addresses = {
        "wCurrentMenuItem": 0x8100,
        "wMaxMenuItem": 0x8101,
    }
    sym = load_sym_text(
        "\n".join(
            f"00:{address:04X} {name}"
            for name, address in addresses.items()
            if name != missing_symbol
        )
    )

    # A missing symbol is not equivalent to a readable zero-valued byte.
    with pytest.raises(KeyError, match=missing_symbol):
        parse_menu(mem, sym)


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
    assert state.cursor_at_top is None
