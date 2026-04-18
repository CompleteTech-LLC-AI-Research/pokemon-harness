"""Menu state parser.

Generic menu handling in pokered lives in ``home/window.asm``:
``HandleMenuInput`` updates ``wCurrentMenuItem``, respects ``wMaxMenuItem``,
and checks ``wMenuWatchedKeys``. Per-context saved cursor vars (bag,
party/Bill's PC, start/battle) persist cursor position across submenu
re-entry so "where was I last in the bag" survives a close/reopen cycle.
"""

from __future__ import annotations

from dataclasses import dataclass

from pokered_harness.symbols.loader import MemoryLike, SymbolTable


@dataclass(frozen=True, slots=True)
class MenuState:
    current_item: int
    max_item: int
    watched_keys: int | None
    list_scroll_offset: int | None
    saved_party_pc: int | None
    saved_bag: int | None
    saved_battle_start: int | None

    @property
    def cursor_at_top(self) -> bool:
        return self.current_item == 0 and (self.list_scroll_offset or 0) == 0


def parse_menu(memory: MemoryLike, symbols: SymbolTable) -> MenuState:
    return MenuState(
        current_item=symbols.read_u8(memory, "wCurrentMenuItem"),
        max_item=symbols.read_u8(memory, "wMaxMenuItem"),
        watched_keys=_opt(memory, symbols, "wMenuWatchedKeys"),
        list_scroll_offset=_opt(memory, symbols, "wListScrollOffset"),
        saved_party_pc=_opt(memory, symbols, "wPartyAndBillsPCSavedMenuItem"),
        saved_bag=_opt(memory, symbols, "wBagSavedMenuItem"),
        saved_battle_start=_opt(memory, symbols, "wBattleAndStartSavedMenuItem"),
    )


def _opt(memory: MemoryLike, symbols: SymbolTable, name: str) -> int | None:
    return symbols.read_u8(memory, name) if name in symbols else None
