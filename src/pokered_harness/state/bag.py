"""Bag inventory parser.

The bag is a count-prefixed list of ``(item_id, quantity)`` pairs capped
at 20 stacks, followed by a ``$FF`` terminator byte (``wBagItemsEnd``).
Key items and HMs are stored inline as ordinary item IDs — there is no
separate key-item list in Gen 1. PC-stored items (``wNumBoxItems`` /
``wBoxItems``) follow the same layout but live in SRAM and aren't parsed
here yet.
"""

from __future__ import annotations

from dataclasses import dataclass

from pokered_harness.symbols.loader import MemoryLike, SymbolTable

MAX_BAG_STACKS = 20
_ITEM_STACK_SIZE = 2


@dataclass(frozen=True, slots=True)
class BagStack:
    item_id: int
    quantity: int


@dataclass(frozen=True, slots=True)
class Bag:
    count: int
    stacks: tuple[BagStack, ...]

    def has_item(self, item_id: int) -> bool:
        return any(s.item_id == item_id for s in self.stacks)

    def quantity_of(self, item_id: int) -> int:
        return sum(s.quantity for s in self.stacks if s.item_id == item_id)


def parse_bag(memory: MemoryLike, symbols: SymbolTable) -> Bag:
    count = symbols.read_u8(memory, "wNumBagItems")
    count = min(count, MAX_BAG_STACKS)
    if count == 0:
        return Bag(count=0, stacks=())

    base = symbols.addr_of("wBagItems")
    stacks = tuple(
        BagStack(
            item_id=int(memory[base + i * _ITEM_STACK_SIZE]) & 0xFF,
            quantity=int(memory[base + i * _ITEM_STACK_SIZE + 1]) & 0xFF,
        )
        for i in range(count)
    )
    return Bag(count=count, stacks=stacks)
