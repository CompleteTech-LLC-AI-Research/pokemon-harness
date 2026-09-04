"""Bag inventory parser.

The bag has a symbol-backed stack count followed by ``(item_id, quantity)``
pairs.  The list is capped at 20 stacks and has a single ``$FF`` item-ID
terminator in the 21st slot.  The upstream Red, Blue, and Yellow WRAM source
allocates that terminator byte as part of ``wBagItems``; it does not expose a
separate ``wBagItemsEnd`` symbol.  The parser therefore bounds the scan from
the loaded ``wBagItems`` address rather than inventing an end address.

Key items and HMs are stored inline as ordinary item IDs — there is no
separate key-item list in Gen 1. PC-stored items (``wNumBoxItems`` /
``wBoxItems``) follow the same layout but live in SRAM and aren't parsed
here yet.
"""

from __future__ import annotations

from dataclasses import dataclass

from pokered_harness.symbols.loader import MemoryLike, SymbolTable

MAX_BAG_STACKS = 20
MAX_ITEM_QUANTITY = 99
NO_ITEM = 0x00
BAG_TERMINATOR = 0xFF
_ITEM_STACK_SIZE = 2


@dataclass(frozen=True, slots=True)
class BagStack:
    item_id: int
    quantity: int

    @property
    def item_id_valid(self) -> bool:
        """Whether this byte can represent an item rather than a sentinel."""
        return NO_ITEM < self.item_id < BAG_TERMINATOR

    @property
    def quantity_valid(self) -> bool:
        """Whether the quantity satisfies Gen-1's per-stack invariant."""
        return 1 <= self.quantity <= MAX_ITEM_QUANTITY

    @property
    def valid(self) -> bool:
        """Whether both raw stack fields describe an actual stack."""
        return self.item_id_valid and self.quantity_valid

    @property
    def is_valid(self) -> bool:
        """Alias for :attr:`valid` for consistency with state helpers."""
        return self.valid


@dataclass(frozen=True, slots=True)
class Bag:
    count: int
    stacks: tuple[BagStack, ...]
    # ``valid`` is tri-state: True is a checked bag, False is contradictory or
    # malformed RAM, and None means the required observation was unavailable.
    # The first two fields intentionally retain their historical shape so
    # existing callers can continue to inspect raw bytes.
    valid: bool | None = True
    raw_count: int | None = None
    terminator_index: int | None = None
    count_valid: bool | None = True
    terminator_valid: bool | None = True

    @property
    def is_valid(self) -> bool:
        return self.valid is True

    @property
    def is_unknown(self) -> bool:
        return self.valid is None

    @property
    def terminator_found(self) -> bool | None:
        """Whether the bounded item-list scan found ``$FF``."""
        if self.valid is None and self.terminator_index is None:
            return None
        return self.terminator_index is not None

    def has_item(self, item_id: int) -> bool | None:
        matching = [s for s in self.stacks if s.item_id == item_id]
        if self.is_unknown:
            return None
        if not matching:
            return False
        if any(not stack.valid for stack in matching):
            return None
        return True

    def quantity_of(self, item_id: int) -> int | None:
        matching = [s for s in self.stacks if s.item_id == item_id]
        if self.is_unknown:
            return None
        if any(not stack.valid for stack in matching):
            return None
        return sum(s.quantity for s in matching)


def parse_bag(memory: MemoryLike, symbols: SymbolTable) -> Bag:
    raw_count = symbols.read_u8(memory, "wNumBagItems")
    count = min(raw_count, MAX_BAG_STACKS)

    # A zero count is sufficient to report a known empty bag.  This is useful
    # for symbol files whose revision omits the item-list label, and avoids a
    # guessed canonical address.  A non-empty bag still requires the base.
    if raw_count == 0 and "wBagItems" not in symbols:
        return Bag(
            count=0,
            stacks=(),
            valid=True,
            raw_count=0,
            count_valid=True,
            terminator_valid=None,
        )

    base = symbols.addr_of("wBagItems")
    terminator_index = _find_terminator(memory, base)
    stack_count = count if terminator_index is None else terminator_index
    stacks = _read_stacks(memory, base, stack_count)
    count_valid = raw_count <= MAX_BAG_STACKS
    terminator_valid = (
        terminator_index is not None and terminator_index == raw_count
    )
    valid = count_valid and terminator_valid and all(stack.valid for stack in stacks)
    return Bag(
        count=count,
        stacks=stacks,
        valid=valid,
        raw_count=raw_count,
        terminator_index=terminator_index,
        count_valid=count_valid,
        terminator_valid=terminator_valid,
    )


def _find_terminator(memory: MemoryLike, base: int) -> int | None:
    """Return the first sentinel slot without reading past bag storage."""
    for index in range(MAX_BAG_STACKS + 1):
        item_id = int(memory[base + index * _ITEM_STACK_SIZE]) & 0xFF
        if item_id == BAG_TERMINATOR:
            return index
    return None


def _read_stacks(
    memory: MemoryLike,
    base: int,
    count: int,
) -> tuple[BagStack, ...]:
    return tuple(
        BagStack(
            item_id=int(memory[base + i * _ITEM_STACK_SIZE]) & 0xFF,
            quantity=int(memory[base + i * _ITEM_STACK_SIZE + 1]) & 0xFF,
        )
        for i in range(count)
    )
