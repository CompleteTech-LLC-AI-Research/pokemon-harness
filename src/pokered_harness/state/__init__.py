"""Per-system state parsers.

Each submodule exposes a frozen dataclass describing one slice of runtime
game state, plus a ``parse_*`` function that reads it from a MemoryLike
backing store through a :class:`SymbolTable`.

Parsers are pure: they never write to memory, they never touch PyBoy
directly, and they return ``None`` for optional fields whose backing
symbol is absent from the loaded ``.sym`` (so the harness degrades
gracefully across minor pokered revisions).
"""

from pokered_harness.state.bag import Bag, BagStack, parse_bag
from pokered_harness.state.base import (
    Direction,
    StatusCondition,
    parse_direction,
    parse_status,
)
from pokered_harness.state.battle import (
    BattleKind,
    BattleState,
    BattleType,
    parse_battle,
)
from pokered_harness.state.game_state import GameState, parse_game_state
from pokered_harness.state.menu import MenuState, parse_menu
from pokered_harness.state.overworld import OverworldState, parse_overworld
from pokered_harness.state.party import Party, PartyMon, parse_party
from pokered_harness.state.progress import (
    ALL_BADGES,
    Badge,
    ProgressState,
    parse_progress,
    read_event_flag,
)
from pokered_harness.state.text import TextState, parse_text

__all__ = [
    "ALL_BADGES",
    "Badge",
    "Bag",
    "BagStack",
    "BattleKind",
    "BattleState",
    "BattleType",
    "Direction",
    "GameState",
    "MenuState",
    "OverworldState",
    "Party",
    "PartyMon",
    "ProgressState",
    "StatusCondition",
    "TextState",
    "parse_bag",
    "parse_battle",
    "parse_direction",
    "parse_game_state",
    "parse_menu",
    "parse_overworld",
    "parse_party",
    "parse_progress",
    "parse_status",
    "parse_text",
    "read_event_flag",
]
