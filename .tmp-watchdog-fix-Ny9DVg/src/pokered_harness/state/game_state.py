"""Top-level per-tick game-state aggregate.

Composes the per-system parsers into a single snapshot the session layer
can hand to agents.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TypeVar

from pokered_harness.state.bag import Bag, parse_bag
from pokered_harness.state.battle import BattleKind, BattleState, parse_battle
from pokered_harness.state.menu import MenuState, parse_menu
from pokered_harness.state.overworld import OverworldState, parse_overworld
from pokered_harness.state.party import Party, parse_party
from pokered_harness.state.progress import ProgressState, parse_progress
from pokered_harness.state.text import TextState, parse_text
from pokered_harness.symbols.loader import MemoryLike, SymbolTable


class StateStatus(str, Enum):
    """Confidence of the aggregate snapshot's decoded state.

    ``VALID`` means every field tracked by this aggregate has a backing
    symbol and a recognized raw representation. ``PARTIAL`` means at least
    one field is unavailable or undecodable while another field is usable.
    ``UNKNOWN`` means none of the component fields could be decoded. The
    distinction is deliberately explicit: a missing symbol must never be
    represented by a guessed zero or ``False`` value.
    """

    VALID = "valid"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class StateValidity:
    """Symbol and value provenance for a :class:`GameState` snapshot.

    ``missing_symbols`` contains the exact labels that were unavailable in
    the loaded ``.sym`` table. ``unknown_fields`` contains dotted aggregate
    field paths whose values must not be used as authoritative state.
    ``invalid_fields`` is reserved for a symbol-backed value that is outside
    the parser's known encoding (for example, an unknown battle kind).

    The old component dataclasses remain unchanged for complete symbol
    tables. This metadata is additive so callers can keep using the legacy
    fields while opting into an evidence-aware contract.
    """

    status: StateStatus = StateStatus.UNKNOWN
    missing_symbols: tuple[str, ...] = ()
    unknown_fields: tuple[str, ...] = ()
    invalid_fields: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        """Whether the aggregate is complete and value-decodable."""
        return self.status is StateStatus.VALID

    @property
    def is_valid(self) -> bool:
        """Alias for :attr:`valid` for callers that prefer predicate names."""
        return self.valid

    @property
    def unknown(self) -> bool:
        """Whether any exposed field is unavailable or semantically unknown."""
        return bool(self.unknown_fields or self.invalid_fields)

    @property
    def is_unknown(self) -> bool:
        """Alias for :attr:`unknown`. ``PARTIAL`` snapshots can be unknown."""
        return self.unknown

    @property
    def completely_unknown(self) -> bool:
        """Whether no component field could be decoded at all."""
        return self.status is StateStatus.UNKNOWN


# These are the fields directly represented by the public component
# dataclasses, followed by their derived predicates. Keeping this registry in
# the aggregate layer lets us report provenance without changing every child
# parser's public shape.
_FIELD_SOURCES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("overworld.map_id", ("wCurMap",)),
    ("overworld.x", ("wXCoord",)),
    ("overworld.y", ("wYCoord",)),
    ("overworld.walk_counter", ("wWalkCounter",)),
    (
        "overworld.direction",
        ("wSpritePlayerStateData1FacingDirection", "wSpriteStateData1"),
    ),
    ("overworld.map_script_flags", ("wCurrentMapScriptFlags",)),
    ("overworld.current_map_script", ("wCurMapScript",)),
    ("overworld.status_flags5_raw", ("wStatusFlags5",)),
    ("overworld.is_standing", ("wWalkCounter",)),
    ("overworld.scripted_movement_active", ("wStatusFlags5",)),
    ("menu.current_item", ("wCurrentMenuItem",)),
    ("menu.max_item", ("wMaxMenuItem",)),
    ("menu.watched_keys", ("wMenuWatchedKeys",)),
    ("menu.list_scroll_offset", ("wListScrollOffset",)),
    ("menu.saved_party_pc", ("wPartyAndBillsPCSavedMenuItem",)),
    ("menu.saved_bag", ("wBagSavedMenuItem",)),
    ("menu.saved_battle_start", ("wBattleAndStartSavedMenuItem",)),
    ("menu.cursor_valid", ("wListScrollOffset",)),
    ("menu.cursor_at_top", ("wCurrentMenuItem", "wListScrollOffset")),
    ("text.text_dest_addr", ("wTextDest",)),
    ("text.text_dest_valid", ("wTextDest",)),
    (
        "text.suppress_prompt_wait",
        ("wDoNotWaitForButtonPressAfterDisplayingText",),
    ),
    (
        "text.suppress_prompt_wait_valid",
        ("wDoNotWaitForButtonPressAfterDisplayingText",),
    ),
    ("text.dest_in_wram_tilemap", ("wTileMap",)),
    ("text.dest_in_vram_tilemap", ("wTextDest",)),
    ("battle.kind", ("wIsInBattle",)),
    ("battle.raw_is_in_battle", ("wIsInBattle",)),
    ("battle.battle_type", ("wBattleType",)),
    ("battle.engaged_trainer_class", ("wEngagedTrainerClass",)),
    ("battle.engaged_trainer_set", ("wEngagedTrainerSet",)),
    ("battle.player_mon_slot", ("wPlayerMonNumber",)),
    ("battle.move_menu_type", ("wMoveMenuType",)),
    ("battle.player_selected_move", ("wPlayerSelectedMove",)),
    ("battle.enemy_selected_move", ("wEnemySelectedMove",)),
    ("battle.action_result_or_took_turn", ("wActionResultOrTookBattleTurn",)),
    ("battle.active", ("wIsInBattle",)),
    ("battle.is_trainer_battle", ("wIsInBattle",)),
    ("battle.is_wild_battle", ("wIsInBattle",)),
    ("battle.turn_already_consumed", ("wActionResultOrTookBattleTurn",)),
    ("battle_active", ("wIsInBattle",)),
    ("party.count", ("wPartyCount",)),
    ("party.mons", ("wPartyMons",)),
    ("bag.count", ("wNumBagItems",)),
    ("bag.stacks", ("wBagItems",)),
    ("bag.valid", ("wNumBagItems",)),
    ("progress.badges_raw", ("wObtainedBadges",)),
    ("progress.trainer_id", ("wPlayerID",)),
    ("progress.money", ("wPlayerMoney",)),
    ("progress.play_time_hours", ("wPlayTimeHours",)),
    ("progress.play_time_minutes", ("wPlayTimeMinutes",)),
    ("progress.play_time_seconds", ("wPlayTimeSeconds",)),
    ("progress.play_time_maxed", ("wPlayTimeMaxed",)),
    ("progress.play_time_maxed_raw", ("wPlayTimeMaxed",)),
)

_REQUIRED_SYMBOLS: dict[str, tuple[str, ...]] = {
    "overworld": ("wCurMap", "wXCoord", "wYCoord", "wWalkCounter"),
    "menu": ("wCurrentMenuItem", "wMaxMenuItem"),
    "text": (),
    "battle": ("wIsInBattle",),
    "progress": ("wObtainedBadges",),
}

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class GameState:
    """One aggregate snapshot plus explicit symbol/value confidence.

    The seven original fields are retained in their original order and keep
    their original concrete values whenever the corresponding symbols are
    available. A component is ``None`` only when its required symbol-backed
    state cannot be decoded; consult :attr:`validity` before consuming a
    partial snapshot.
    """

    overworld: OverworldState | None
    menu: MenuState | None
    text: TextState | None
    battle: BattleState | None
    party: Party | None
    bag: Bag | None
    progress: ProgressState | None
    validity: StateValidity = field(default_factory=StateValidity)

    @property
    def is_valid(self) -> bool:
        """Whether every aggregate field is symbol-backed and decodable."""
        return self.validity.is_valid

    @property
    def is_unknown(self) -> bool:
        """Whether this snapshot contains unavailable or unknown state."""
        return self.validity.is_unknown

    @property
    def battle_active(self) -> bool | None:
        """Return known battle activity, or ``None`` when it is unknown.

        The legacy ``battle.active`` property is intentionally preserved for
        compatibility and reflects its historical raw-byte behavior. This
        aggregate predicate is fail-closed: an absent ``wIsInBattle`` symbol
        or an unrecognized raw kind cannot be promoted to an active battle.
        """
        if self.battle is None:
            return None
        if any(
            field_name in self.validity.unknown_fields
            or field_name in self.validity.invalid_fields
            for field_name in ("battle_active", "battle.kind")
        ):
            return None
        if self.battle.kind is None:
            return None
        return self.battle.kind is not BattleKind.NONE

    @property
    def battle_active_known(self) -> bool:
        """Whether :attr:`battle_active` is a boolean rather than unknown."""
        return self.battle_active is not None


def parse_game_state(memory: MemoryLike, symbols: SymbolTable) -> GameState:
    """Parse all available state without inventing values for missing data.

    Complete Red/Blue/Yellow symbol tables retain the original aggregate
    behavior. With a partial table, optional fields remain represented by the
    child parser's legacy ``None`` values and a component whose required
    symbols are absent becomes ``None`` with exact provenance in
    :attr:`GameState.validity`.
    """
    overworld = _parse_if_symbols(
        parse_overworld, memory, symbols, _REQUIRED_SYMBOLS["overworld"]
    )
    menu = _parse_if_symbols(parse_menu, memory, symbols, _REQUIRED_SYMBOLS["menu"])
    text = _parse_if_symbols(parse_text, memory, symbols, _REQUIRED_SYMBOLS["text"])
    battle = _parse_if_symbols(
        parse_battle, memory, symbols, _REQUIRED_SYMBOLS["battle"]
    )
    party = _parse_if_symbols(
        parse_party, memory, symbols, _party_required_symbols(memory, symbols)
    )
    bag = _parse_if_symbols(
        parse_bag, memory, symbols, _bag_required_symbols(memory, symbols)
    )
    progress = _parse_if_symbols(
        parse_progress, memory, symbols, _REQUIRED_SYMBOLS["progress"]
    )

    components = {
        "overworld": overworld,
        "menu": menu,
        "text": text,
        "battle": battle,
        "party": party,
        "bag": bag,
        "progress": progress,
    }
    return GameState(
        overworld=overworld,
        menu=menu,
        text=text,
        battle=battle,
        party=party,
        bag=bag,
        progress=progress,
        validity=_build_validity(memory, symbols, components),
    )


def _parse_if_symbols(
    parser: Callable[[MemoryLike, SymbolTable], _T],
    memory: MemoryLike,
    symbols: SymbolTable,
    required: tuple[str, ...],
) -> _T | None:
    if any(name not in symbols for name in required):
        return None
    return parser(memory, symbols)


def _party_required_symbols(memory: MemoryLike, symbols: SymbolTable) -> tuple[str, ...]:
    if "wPartyCount" not in symbols:
        return ("wPartyCount", "wPartyMons")
    count = symbols.read_u8(memory, "wPartyCount")
    return ("wPartyCount",) if count == 0 else ("wPartyCount", "wPartyMons")


def _bag_required_symbols(memory: MemoryLike, symbols: SymbolTable) -> tuple[str, ...]:
    if "wNumBagItems" not in symbols:
        return ("wNumBagItems", "wBagItems")
    count = symbols.read_u8(memory, "wNumBagItems")
    return ("wNumBagItems",) if count == 0 else ("wNumBagItems", "wBagItems")


def _build_validity(
    memory: MemoryLike,
    symbols: SymbolTable,
    components: dict[str, object | None],
) -> StateValidity:
    unknown: set[str] = set()
    invalid: set[str] = set()
    missing: set[str] = set()

    for path, source_names in _FIELD_SOURCES:
        component_name = _component_for_path(path)
        component = components.get(component_name)

        if component is None:
            unknown.add(path)
            if not any(name in symbols for name in source_names):
                missing.update(source_names)
            continue

        if _field_not_applicable(path, component):
            continue

        available = any(name in symbols for name in source_names)
        if not available:
            unknown.add(path)
            missing.update(source_names)

    _mark_invalid_values(memory, symbols, components, unknown, invalid)

    all_field_names = {path for path, _source_names in _FIELD_SOURCES}
    known_field_count = len(all_field_names - unknown - invalid)
    if known_field_count == 0:
        status = StateStatus.UNKNOWN
    elif unknown or invalid:
        status = StateStatus.PARTIAL
    else:
        status = StateStatus.VALID

    return StateValidity(
        status=status,
        missing_symbols=tuple(sorted(missing)),
        unknown_fields=tuple(sorted(unknown)),
        invalid_fields=tuple(sorted(invalid)),
    )


def _component_for_path(path: str) -> str:
    if path == "battle_active":
        return "battle"
    return path.split(".", 1)[0]


def _field_not_applicable(path: str, component: object) -> bool:
    if path == "party.mons" and isinstance(component, Party):
        return component.count == 0
    if path == "bag.stacks" and isinstance(component, Bag):
        return component.count == 0
    return False


def _mark_invalid_values(
    memory: MemoryLike,
    symbols: SymbolTable,
    components: dict[str, object | None],
    unknown: set[str],
    invalid: set[str],
) -> None:
    text = components["text"]
    if isinstance(text, TextState):
        if text.text_dest_valid is False:
            text_invalid = {
                "text.text_dest_addr",
                "text.text_dest_valid",
                "text.dest_in_wram_tilemap",
                "text.dest_in_vram_tilemap",
            }
            invalid.update(text_invalid)
            unknown.update(text_invalid)
        if text.suppress_prompt_wait_valid is False:
            text_invalid = {
                "text.suppress_prompt_wait",
                "text.suppress_prompt_wait_valid",
            }
            invalid.update(text_invalid)
            unknown.update(text_invalid)
        if (
            text.dest_in_wram_tilemap is None
            and "text.dest_in_wram_tilemap" not in unknown
        ):
            unknown.add("text.dest_in_wram_tilemap")

    menu = components["menu"]
    if isinstance(menu, MenuState):
        if menu.cursor_valid is False:
            menu_invalid = {"menu.cursor_valid", "menu.cursor_at_top"}
            invalid.update(menu_invalid)
            unknown.update(menu_invalid)
        elif menu.cursor_valid is None:
            unknown.update({"menu.cursor_valid", "menu.cursor_at_top"})

    overworld = components["overworld"]
    if (
        isinstance(overworld, OverworldState)
        and overworld.direction is None
        and "overworld.direction" not in unknown
    ):
        invalid.add("overworld.direction")

    battle = components["battle"]
    if isinstance(battle, BattleState):
        if battle.kind is None and "battle.kind" not in unknown:
            battle_invalid = {
                "battle.kind",
                "battle.active",
                "battle.is_trainer_battle",
                "battle.is_wild_battle",
                "battle_active",
            }
            invalid.update(battle_invalid)
            unknown.update(battle_invalid)
        if (
            battle.battle_type is None
            and "battle.battle_type" not in unknown
        ):
            invalid.add("battle.battle_type")

    bag = components["bag"]
    if isinstance(bag, Bag) and bag.valid is not True:
        invalid.add("bag.valid")
        unknown.add("bag.valid")

    party = components["party"]
    if isinstance(party, Party) and any(
        mon.status.is_unknown for mon in party.mons
    ):
        invalid.add("party.mons")
        unknown.add("party.mons")

    if "wPartyCount" in symbols:
        raw_count = symbols.read_u8(memory, "wPartyCount")
        if raw_count > 6:
            invalid.update({"party.count", "party.mons"})
            unknown.update({"party.count", "party.mons"})

    if "wNumBagItems" in symbols:
        raw_count = symbols.read_u8(memory, "wNumBagItems")
        if raw_count > 20:
            invalid.update({"bag.count", "bag.stacks"})
            unknown.update({"bag.count", "bag.stacks"})

    progress = components["progress"]
    if isinstance(progress, ProgressState):
        if (
            "wPlayTimeMinutes" in symbols
            and symbols.read_u8(memory, "wPlayTimeMinutes") >= 60
        ):
            invalid.add("progress.play_time_minutes")
            unknown.add("progress.play_time_minutes")
        if (
            "wPlayTimeSeconds" in symbols
            and symbols.read_u8(memory, "wPlayTimeSeconds") >= 60
        ):
            invalid.add("progress.play_time_seconds")
            unknown.add("progress.play_time_seconds")
        if "wPlayerMoney" in symbols and _money_has_invalid_bcd(memory, symbols):
            invalid.add("progress.money")
            unknown.add("progress.money")


def _money_has_invalid_bcd(memory: MemoryLike, symbols: SymbolTable) -> bool:
    base = symbols.addr_of("wPlayerMoney")
    for offset in range(3):
        raw = int(memory[base + offset]) & 0xFF
        if (raw >> 4) > 9 or (raw & 0x0F) > 9:
            return True
    return False
