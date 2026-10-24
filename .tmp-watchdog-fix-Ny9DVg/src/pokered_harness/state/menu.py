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
    # These are required raw ROM bytes.  The pinned Red/Blue/Yellow symbol
    # files all provide them; a missing symbol fails closed in parse_menu
    # rather than fabricating a zero-valued cursor.
    current_item: int
    max_item: int
    watched_keys: int | None
    list_scroll_offset: int | None
    saved_party_pc: int | None
    saved_bag: int | None
    saved_battle_start: int | None

    @property
    def cursor_valid(self) -> bool | None:
        """Whether the observed cursor fields are mutually consistent.

        ``wCurrentMenuItem`` is relative to the visible list and
        ``wMaxMenuItem`` is the bottom-item id.  When the scroll symbol is
        available, the current item plus the scroll offset must not pass that
        bottom id.  ``None`` means that the optional scroll field prevents a
        complete check.  Missing required symbols are reported by
        ``parse_menu`` as a ``KeyError``.

        This is only a structural check.  The ROM keeps ``wMaxMenuItem``
        after a menu closes, so these fields cannot establish that a menu is
        currently active.
        """
        if self.current_item > self.max_item:
            return False
        if self.list_scroll_offset is None:
            return None
        return self.current_item + self.list_scroll_offset <= self.max_item

    @property
    def cursor_at_top(self) -> bool | None:
        """Whether the cursor is at the first item in the full list.

        A missing scroll symbol makes this fact unknown.  If the raw cursor
        bytes are inconsistent, retain the legacy boolean comparison and
        expose the invalidity through :attr:`cursor_valid`; callers that need
        a semantically valid menu must check that property first.  In
        particular, an absent optional symbol must not be silently treated as
        a zero-valued RAM byte.
        """
        if self.list_scroll_offset is None:
            return None
        return self.current_item == 0 and self.list_scroll_offset == 0


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
