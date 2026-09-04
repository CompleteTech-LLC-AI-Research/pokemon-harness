from __future__ import annotations

import pytest

from pokered_harness.state.bag import MAX_BAG_STACKS, parse_bag
from pokered_harness.symbols.loader import load_sym_text


def _write_stack(mem, base: int, index: int, item_id: int, qty: int) -> None:
    mem[base + index * 2] = item_id
    mem[base + index * 2 + 1] = qty


def test_empty_bag(mem, symbols):
    bag = parse_bag(mem, symbols)
    assert bag.count == 0
    assert bag.stacks == ()
    assert bag.has_item(0x04) is False  # Poké Ball
    assert bag.quantity_of(0x04) == 0


def test_single_stack(mem, symbols):
    mem[0xD31D] = 1
    _write_stack(mem, 0xD31E, 0, item_id=0x04, qty=5)  # 5x Poké Balls
    bag = parse_bag(mem, symbols)
    assert bag.count == 1
    assert bag.stacks[0].item_id == 0x04
    assert bag.stacks[0].quantity == 5
    assert bag.has_item(0x04)
    assert bag.quantity_of(0x04) == 5


def test_multiple_stacks_preserves_order(mem, symbols):
    mem[0xD31D] = 3
    _write_stack(mem, 0xD31E, 0, 0x04, 10)
    _write_stack(mem, 0xD31E, 1, 0x11, 2)  # Potion
    _write_stack(mem, 0xD31E, 2, 0x01, 1)  # Master Ball
    bag = parse_bag(mem, symbols)
    assert [s.item_id for s in bag.stacks] == [0x04, 0x11, 0x01]
    assert [s.quantity for s in bag.stacks] == [10, 2, 1]


def test_duplicate_item_across_stacks_sums_quantity(mem, symbols):
    mem[0xD31D] = 2
    _write_stack(mem, 0xD31E, 0, 0x04, 99)
    _write_stack(mem, 0xD31E, 1, 0x04, 50)
    bag = parse_bag(mem, symbols)
    assert bag.quantity_of(0x04) == 149


def test_count_clamped_to_maximum(mem, symbols):
    mem[0xD31D] = 200
    bag = parse_bag(mem, symbols)
    assert bag.count == MAX_BAG_STACKS
    assert len(bag.stacks) == MAX_BAG_STACKS


def test_bag_reads_loaded_symbol_addresses_not_canonical_defaults(mem):
    # A version-specific .sym may move WRAM fields.  A parser that silently
    # falls back to the familiar Red addresses would report the decoy data.
    sym = load_sym_text(
        """
        00:C100 wNumBagItems
        00:C200 wBagItems
        """
    )
    mem[0xC100] = 1
    _write_stack(mem, 0xC200, 0, item_id=0x04, qty=5)
    mem[0xD31D] = 0
    _write_stack(mem, 0xD31E, 0, item_id=0x11, qty=99)

    bag = parse_bag(mem, sym)

    assert bag.count == 1
    assert bag.stacks[0].item_id == 0x04
    assert bag.stacks[0].quantity == 5


def test_zero_count_is_known_empty_without_item_base_symbol(mem):
    # No item bytes are needed to establish an empty bag; this preserves the
    # useful zero-count behavior while keeping symbol resolution explicit.
    sym = load_sym_text("00:C100 wNumBagItems\n")
    mem[0xC100] = 0

    bag = parse_bag(mem, sym)

    assert bag.count == 0
    assert bag.stacks == ()
    assert bag.has_item(0x04) is False
    assert bag.quantity_of(0x04) == 0


def test_missing_count_symbol_is_an_explicit_unknown_not_empty(mem):
    sym = load_sym_text("00:C200 wBagItems\n")

    with pytest.raises(KeyError, match="wNumBagItems"):
        parse_bag(mem, sym)


def test_nonempty_bag_missing_item_base_symbol_does_not_guess_address(mem):
    sym = load_sym_text("00:C100 wNumBagItems\n")
    mem[0xC100] = 1
    _write_stack(mem, 0xD31E, 0, item_id=0x11, qty=99)

    with pytest.raises(KeyError, match="wBagItems"):
        parse_bag(mem, sym)
