"""Overworld state parser.

Reads the free-movement / scripted-movement slice of runtime state. All
symbols below come from ``pret/pokered`` raw ``ram/wram.asm`` (not the
generated Mintlify wiki).
"""

from __future__ import annotations

from dataclasses import dataclass

from pokered_harness.state.base import Direction, parse_direction
from pokered_harness.symbols.loader import MemoryLike, SymbolTable

# ``BIT_SCRIPTED_MOVEMENT_STATE`` in
# ``pret/pokered/constants/ram_constants.asm`` and the matching
# ``pret/pokeyellow`` source.  The flag is the high bit of wStatusFlags5.
# Keep this source-derived value centralized so every callsite agrees.
BIT_SCRIPTED_MOVEMENT_STATE = 7


@dataclass(frozen=True, slots=True)
class OverworldState:
    map_id: int
    x: int
    y: int
    walk_counter: int
    direction: Direction | None
    map_script_flags: int | None
    current_map_script: int | None
    status_flags5_raw: int | None

    @property
    def is_standing(self) -> bool:
        return self.walk_counter == 0

    @property
    def scripted_movement_active(self) -> bool:
        if self.status_flags5_raw is None:
            return False
        return bool((self.status_flags5_raw >> BIT_SCRIPTED_MOVEMENT_STATE) & 1)


def parse_overworld(memory: MemoryLike, symbols: SymbolTable) -> OverworldState:
    return OverworldState(
        map_id=symbols.read_u8(memory, "wCurMap"),
        x=symbols.read_u8(memory, "wXCoord"),
        y=symbols.read_u8(memory, "wYCoord"),
        walk_counter=symbols.read_u8(memory, "wWalkCounter"),
        direction=_read_direction(memory, symbols),
        map_script_flags=_optional_u8(memory, symbols, "wCurrentMapScriptFlags"),
        current_map_script=_optional_u8(memory, symbols, "wCurMapScript"),
        status_flags5_raw=_optional_u8(memory, symbols, "wStatusFlags5"),
    )


def _optional_u8(memory: MemoryLike, symbols: SymbolTable, name: str) -> int | None:
    return symbols.read_u8(memory, name) if name in symbols else None


def _read_direction(memory: MemoryLike, symbols: SymbolTable) -> Direction | None:
    # Pokered exposes the per-sprite facing direction through struct-macro
    # expansion. The player is sprite 0, so the label is usually
    # ``wSpritePlayerStateData1FacingDirection``. If that exact label isn't
    # in the .sym, fall back to ``wSpriteStateData1`` + offset 9.
    if "wSpritePlayerStateData1FacingDirection" in symbols:
        return parse_direction(
            symbols.read_u8(memory, "wSpritePlayerStateData1FacingDirection")
        )
    base = symbols.get("wSpriteStateData1")
    if base is None:
        return None
    raw = memory[base.addr + 0x09]
    return parse_direction(int(raw) & 0xFF)
