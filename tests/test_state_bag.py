from __future__ import annotations

from pokered_harness.state.bag import MAX_BAG_STACKS, parse_bag


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
