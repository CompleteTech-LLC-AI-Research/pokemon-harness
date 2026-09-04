"""Text / dialogue state parser.

There is no RAM-backed "text is active" bit in pokered. ``wTextDest`` is a
2-byte cursor written by the text engine, but the game does not read it back
as a latch. It normally points into the symbol-backed ``wTileMap`` WRAM
buffer, and it may remain there after text has finished. Its value is useful
as a raw diagnostic only; execution hooks such as ``PrintText`` are the
authoritative way to observe text rendering.

``wDoNotWaitForButtonPressAfterDisplayingText`` has a narrower meaning: when
present, a non-zero byte tells ``DisplayTextID`` to skip its post-display
button wait. It does not report whether text is currently visible. Missing or
unreadable optional symbols are represented by explicit validity metadata;
the legacy boolean fields retain their old fallback values for callers that
have not migrated yet.
"""

from __future__ import annotations

from dataclasses import dataclass

from pokered_harness.symbols.loader import MemoryLike, SymbolTable

# GB VRAM tile maps live in $9800-$9FFF. ``wTextDest`` was historically (and
# incorrectly) treated as if it pointed somewhere in that range. Keep these
# bounds for the compatibility property below; the real text output buffer is
# the symbol-backed wTileMap in WRAM.
_VRAM_TILEMAP_LO = 0x9800
_VRAM_TILEMAP_HI = 0x9FFF
_WRAM_TILEMAP_BYTES = 20 * 18


@dataclass(frozen=True, slots=True)
class TextState:
    text_dest_addr: int | None
    suppress_prompt_wait: bool
    # ``None`` means the symbol was absent, ``True`` means the value was read
    # from a valid CPU-space symbol, and ``False`` means the symbol existed
    # but could not be used (for example, it was in a ROM bank or the memory
    # read failed).
    text_dest_valid: bool | None = None
    suppress_prompt_wait_valid: bool | None = None
    # This is deliberately tri-state. Without a wTileMap symbol there is no
    # portable, symbol-backed range against which to classify wTextDest.
    dest_in_wram_tilemap: bool | None = None

    @property
    def suppress_prompt_wait_observed(self) -> bool | None:
        """Return the suppression flag only when it was actually observed.

        ``suppress_prompt_wait`` remains a bool for compatibility, so callers
        must use this property (or ``suppress_prompt_wait_valid``) when they
        need to distinguish an absent/invalid symbol from a verified zero.
        """
        if self.suppress_prompt_wait_valid is not True:
            return None
        return self.suppress_prompt_wait

    @property
    def text_active(self) -> None:
        """Text activity is unknown from this RAM-only snapshot.

        ``wTextDest`` is write-only bookkeeping rather than an activity latch.
        Use the symbol-backed execution event stream for authoritative text
        activity; returning ``None`` here prevents a stale cursor from being
        promoted to a guessed boolean.
        """
        return None

    @property
    def dest_in_tilemap(self) -> bool | None:
        """Alias for the symbol-derived WRAM output-buffer classification."""
        return self.dest_in_wram_tilemap

    @property
    def dest_in_vram_tilemap(self) -> bool:
        """Legacy raw-address check for the old VRAM interpretation.

        This property is retained for existing callers and is *not* a text
        activity signal. In pokered, text output is written to ``wTileMap`` in
        WRAM; use :attr:`dest_in_wram_tilemap` for the symbol-backed location
        check. A missing or invalid destination remains conservatively false.
        """
        if self.text_dest_valid is False or self.text_dest_addr is None:
            return False
        return _VRAM_TILEMAP_LO <= self.text_dest_addr <= _VRAM_TILEMAP_HI


def parse_text(memory: MemoryLike, symbols: SymbolTable) -> TextState:
    text_dest_addr, text_dest_valid = _read_u16(memory, symbols, "wTextDest")
    suppress_raw, suppress_valid = _read_u8(
        memory, symbols, "wDoNotWaitForButtonPressAfterDisplayingText"
    )

    # Preserve the historical bool fallback for callers using the old field.
    # The *_valid field and observed property carry the truthful value.
    suppress = bool(suppress_raw) if suppress_valid is True else False

    dest_in_wram_tilemap: bool | None = None
    tilemap = symbols.get("wTileMap")
    if (
        text_dest_valid is True
        and text_dest_addr is not None
        and tilemap is not None
        and tilemap.bank == 0
    ):
        tilemap_end = tilemap.addr + _WRAM_TILEMAP_BYTES
        if tilemap_end <= 0x10000:
            dest_in_wram_tilemap = tilemap.addr <= text_dest_addr < tilemap_end

    return TextState(
        text_dest_addr=text_dest_addr,
        suppress_prompt_wait=suppress,
        text_dest_valid=text_dest_valid,
        suppress_prompt_wait_valid=suppress_valid,
        dest_in_wram_tilemap=dest_in_wram_tilemap,
    )


def _read_u8(
    memory: MemoryLike, symbols: SymbolTable, name: str
) -> tuple[int | None, bool | None]:
    symbol = symbols.get(name)
    if symbol is None:
        return None, None
    if symbol.bank != 0:
        return None, False
    try:
        return symbols.read_u8(memory, name), True
    except (IndexError, KeyError, TypeError, ValueError, OverflowError):
        return None, False


def _read_u16(
    memory: MemoryLike, symbols: SymbolTable, name: str
) -> tuple[int | None, bool | None]:
    symbol = symbols.get(name)
    if symbol is None:
        return None, None
    if symbol.bank != 0:
        return None, False
    try:
        return symbols.read_u16_le(memory, name), True
    except (IndexError, KeyError, TypeError, ValueError, OverflowError):
        return None, False
