"""Text / dialogue state parser.

No single RAM byte reports "text is active" in pokered. Two observable
signals help:

1. ``wTextDest`` — a 2-byte CPU pointer to where the text engine is
   currently writing. When text is not being rendered, the engine tends
   to leave this at zero or pointing into non-VRAM RAM; a non-trivial
   VRAM-range value is a strong "text is rendering" cue.
2. Tile-layer signature — pokered's overworld docs note that tile IDs
   above ``$60`` represent text boxes and menus, and the player sprite
   is hidden while such tiles are on screen. That check needs the
   tilemap and is composed in by the session layer, not here.

This parser handles (1). Tile-signature fusion happens at the
:class:`GameState` level once the session wires the tilemap in.
"""

from __future__ import annotations

from dataclasses import dataclass

from pokered_harness.symbols.loader import MemoryLike, SymbolTable

# GB VRAM tile maps live in $9800-$9FFF; text is written into VRAM via
# wTextDest pointing somewhere in that range while rendering.
_VRAM_TILEMAP_LO = 0x9800
_VRAM_TILEMAP_HI = 0x9FFF


@dataclass(frozen=True, slots=True)
class TextState:
    text_dest_addr: int | None
    suppress_prompt_wait: bool

    @property
    def dest_in_vram_tilemap(self) -> bool:
        """True iff ``wTextDest`` points into a VRAM tilemap range.

        Used as a fast RAM-only heuristic for "a text/menu box is being
        written right now." Combine with the tile-signature check at
        the GameState level for higher-confidence classification.
        """
        if self.text_dest_addr is None:
            return False
        return _VRAM_TILEMAP_LO <= self.text_dest_addr <= _VRAM_TILEMAP_HI


def parse_text(memory: MemoryLike, symbols: SymbolTable) -> TextState:
    text_dest_addr: int | None = None
    if "wTextDest" in symbols:
        # CPU-space pointer — little-endian per Game Boy convention.
        text_dest_addr = symbols.read_u16_le(memory, "wTextDest")

    suppress = False
    if "wDoNotWaitForButtonPressAfterDisplayingText" in symbols:
        suppress = symbols.read_u8(memory, "wDoNotWaitForButtonPressAfterDisplayingText") != 0

    return TextState(text_dest_addr=text_dest_addr, suppress_prompt_wait=suppress)
