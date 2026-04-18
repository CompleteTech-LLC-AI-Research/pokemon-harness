"""Top-level per-tick game-state aggregate.

Composes the per-system parsers into a single snapshot the session layer
can hand to agents.
"""

from __future__ import annotations

from dataclasses import dataclass

from pokered_harness.state.bag import Bag, parse_bag
from pokered_harness.state.battle import BattleState, parse_battle
from pokered_harness.state.menu import MenuState, parse_menu
from pokered_harness.state.overworld import OverworldState, parse_overworld
from pokered_harness.state.party import Party, parse_party
from pokered_harness.state.progress import ProgressState, parse_progress
from pokered_harness.state.text import TextState, parse_text
from pokered_harness.symbols.loader import MemoryLike, SymbolTable


@dataclass(frozen=True, slots=True)
class GameState:
    overworld: OverworldState
    menu: MenuState
    text: TextState
    battle: BattleState
    party: Party
    bag: Bag
    progress: ProgressState


def parse_game_state(memory: MemoryLike, symbols: SymbolTable) -> GameState:
    return GameState(
        overworld=parse_overworld(memory, symbols),
        menu=parse_menu(memory, symbols),
        text=parse_text(memory, symbols),
        battle=parse_battle(memory, symbols),
        party=parse_party(memory, symbols),
        bag=parse_bag(memory, symbols),
        progress=parse_progress(memory, symbols),
    )
