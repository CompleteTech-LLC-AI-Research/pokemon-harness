"""Real-ROM acceptance for the ordinary-battle medicine fixture (#90.3).

No retained Yellow milestone is in a battle and none of them holds a Potion, so
``scripts/produce_battle_healing_fixture.py`` drives one from the pinned
``brock_badge`` milestone with real button input: it buys a Potion at Pewter
Mart, steps into Route 2 north's grass until the ROM's own encounter check fires
(Yellow's ``engine/battle/wild_encounters.asm`` reads the player's bottom-left
tile with ``hlcoord 8, 9``, i.e. ``wTileMap[9][8]``, and compares it with
``wGrassTile``; Red/Blue read the bottom-right tile ``hlcoord 9, 9`` instead),
and captures the wild battle at its command-menu boundary.

This module loads those bytes and plays ITEM -> POTION -> target, then checks the
observed inventory, HP, and turn accounting against the shared ROM-free contract
in ``tests/_battle_item_evidence.py``.  Every navigational decision comes from
the ROM's own menu geometry (``engine/battle/core.asm:DisplayBattleMenu``): the
command menu is a 2x2 grid whose left column is {FIGHT, ITEM} keyed with
``PAD_RIGHT|PAD_A``, the in-battle bag list is ``wTextBoxID`` 13 with
``wMenuWatchedKeys`` 7, and the target prompt is ``wTextBoxID`` 1 with
``wMenuWatchedKeys`` 3.  ``wCurrentMenuItem`` alone is never treated as proof
that a menu is up, and the Potion row is selected from the *drawn* list because
bag order is not fixed: Brock's TM34 sits above it, and selecting the wrong row
only produces "This isn't the time to use that!".

The ROM, symbol, and fixture inputs are operator-managed and absent from an
asset-free checkout, so the fixture-backed tests skip there.  Run them with all
three pinned::

    PYTHONPATH=vendor/pyboy-src:src:. \
    POKERED_ROM_ROOT=<rom root> \
    POKERED_FIXTURE_ROOT=<fixture root> \
    pytest tests/test_battle_healing_items_rom.py
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import pytest

from pokered_harness.config import load_versions
from pokered_harness.session import Session
from pokered_harness.state import GameState
from scripts import produce_battle_healing_fixture as producer
from tests._battle_item_evidence import (
    BAG_TERMINATOR,
    EVENT_APPLICATION,
    EVENT_COMMAND_SELECTION,
    EVENT_ITEM_SELECTION,
    EVENT_NEXT_COMMAND,
    EVENT_OPPONENT_ACTION,
    POTION_HEAL,
    REASON_APPLIED,
    SUCCESSFUL_ITEM_CONTINUATION,
    ActionTimeline,
    ActiveBattleSnapshot,
    BagSnapshot,
    BagStack,
    ItemUseSnapshot,
    MedicineRule,
    PartyMonSnapshot,
    account_timeline,
    assert_continuation_idempotent,
    assert_last_unit_compaction,
    assert_medicine_application,
    assert_successful_item_turn,
    capped_heal,
    validate_snapshot,
)
from tests._rom_assets import PROJECT_ROOT, fixture_path, rom_path, sym_path
from tests._tier_config import KNOWN_TEST_MODULES

FIXTURE_VERSION = "yellow"
FIXTURE_NAME = "battle_healing.state"
FIXTURE_ID = "yellow-ordinary-battle-healing"
EVIDENCE_PATH = PROJECT_ROOT / "release-evidence" / "battle-healing-fixtures.json"
PRODUCER_PATH = "scripts/produce_battle_healing_fixture.py"
ROM_PIN_PATH = "rom/yellow/pokemon-yellow.gbc"
SYM_PIN_PATH = "rom/yellow/pokemon-yellow.sym"

# The dual-runtime registration bundle for this leaf's runtime half.  Nothing in
# it is trusted: the function below re-derives every claim it makes from the
# files themselves.  It is a committed record, so an overclaim or a leaked
# machine path must fail here rather than be discovered at review time.
QUALIFICATION_BUNDLE = (
    PROJECT_ROOT / "release-evidence" / "feature-qualification" / "issue90-medicine-yellow-2d87676"
)
ACCEPTANCE_NODE_ID = (
    "tests/test_battle_healing_items_rom.py"
    "::test_potion_heals_the_active_mon_from_the_battle_item_menu"
)
BUNDLE_TIERS = ("source", "cython")

# The committed file set of the bundle, as repository-relative POSIX paths.  An
# allowlist rather than a forbidden-name list: a stray save state or symbol file
# added next to the record must fail, not merely be ignored.
BUNDLE_FILES = frozenset(
    {
        "README.md",
        "results.txt",
        "runtime-identity.json",
        "junit/source-focused.xml",
        "junit/cython-focused.xml",
        "logs/source-focused.log",
        "logs/cython-focused.log",
    }
)

# Absolute-path fragments that must never reach a committed record.  The same
# tuple shape guards the build files in ``tests/test_runtime_packaging.py``; the
# ``/opt``, ``/srv``, ``/media`` and ``/run`` roots are included because they are
# ordinary places for operator data to live, not because this host uses them.
BUNDLE_FORBIDDEN_FRAGMENTS = (
    "/mnt/",
    "/home/",
    "/Users/",
    "C:\\Users\\",
    "C:/Users/",
    "/usr/",
    "/tmp/",
    "/var/",
    "/root/",
    "/etc/",
    "/opt/",
    "/srv/",
    "/media/",
    "/run/",
)

# The fragment list only names prefixes that are known in advance; these
# patterns are the general net.  They are applied to text with URLs and
# bracketed placeholder roots neutralized first, so a placeholder-rooted path
# such as ``[source-venv]/lib/python3.11/...`` and a documentation URL are not
# hits, while a bare absolute path is - including single-component forms
# (``/opt/private.log``), forms with spaces inside a component
# (``/opt/Private Data/evidence.log``), Windows drive and UNC forms, and
# ``file://`` URLs.
BUNDLE_URL_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s\"'<>\]\)]*")
BUNDLE_PLACEHOLDER_ROOT_PATTERN = re.compile(r"\[[a-z][a-z0-9\-]*\]")
BUNDLE_UNIX_ABSOLUTE_PATTERN = re.compile(
    # Each component has to carry a word character, so a relative remainder such
    # as ``yellow}/...`` is not read as an absolute path, while a single-component
    # form (``/opt``), a spaced component (``/opt/Private Data``) and a path glued
    # to a preceding colon or bracket are all still hits.  Components stop at
    # markup characters, so an XML self-closing tag is not read as a path.
    r"(?<![\w\]\)<>\}])/(?=[A-Za-z0-9_.~])"
    r"(?:[^/\n<>\"\']*\w[^/\n<>\"\']*/)+[^/\n<>\"\']*\w[^/\n<>\"\']*"
)
BUNDLE_WINDOWS_ABSOLUTE_PATTERN = re.compile(r"(?<![\w])[A-Za-z]:[\\/](?:[^\\/\n]+[\\/])*[^\\/\n]*")
BUNDLE_UNC_PATTERN = re.compile(r"(?<![\w:/])//[A-Za-z0-9_.\-]+[\\/]")
BUNDLE_UNC_BACKSLASH_PATTERN = re.compile(r"(?<![\w:\\])\\\\[A-Za-z0-9_.\-]+\\\\")

# A PyBoy loader warning whose payload is not already the counted redaction
# marker.  Symbol-table input must never be committed, so this must not match
# anything in the bundle.
BUNDLE_SYMBOL_PAYLOAD_PATTERN = re.compile(r"Skipping \.sym line: (?!<redacted:)\S")

# pytest's terminal summary line, parsed rather than substring-searched: the
# whole line has to be the summary, so ``125 passed`` cannot satisfy a claim of
# ``25 passed`` and an extra count cannot hide inside a longer number.
BUNDLE_TERMINAL_LINE_PATTERN = re.compile(
    r"^(?P<parts>(?:\d+ (?:failed|passed|skipped|deselected|xfailed|xpassed"
    r"|warnings?|errors?|error))(?:, (?:\d+ (?:failed|passed|skipped|deselected"
    r"|xfailed|xpassed|warnings?|errors?|error)))*) in \d+\.\d+s$",
    re.MULTILINE,
)
BUNDLE_TERMINAL_PART_PATTERN = re.compile(r"(\d+) (\w+)")
BUNDLE_TERMINAL_KINDS = {
    "warning": "warnings",
    "warnings": "warnings",
    "error": "errors",
    "errors": "errors",
}

# One tier's row in ``results.txt``.  ``summary`` is captured whole so it can be
# compared with the log's terminal line, and the counters are captured separately
# so they can be compared with the executed node ids.
BUNDLE_RESULTS_ROW_PATTERN = re.compile(
    r"^(?P<status>PASS|FAIL)  (?P<tier>source|cython)  (?P<summary>.*?)  "
    r"tests=(?P<tests>\d+) failures=(?P<failures>\d+) "
    r"errors=(?P<errors>\d+) skipped=(?P<skipped>\d+)",
    re.MULTILINE,
)

# Potion (``0x14``) restores a fixed 20 HP; see ``_battle_item_evidence``.
POTION = 0x14
POTION_RULE = MedicineRule(item_id=POTION, fixed_heal=POTION_HEAL)

# ROM menu fingerprints, from ``engine/battle/core.asm:DisplayBattleMenu``.
BATTLE_MENU_MAX_ITEM = 1
BATTLE_MENU_LEFT_COLUMN = 0x11  # PAD_RIGHT | PAD_A
BATTLE_MENU_RIGHT_COLUMN = 0x12  # PAD_LEFT | PAD_A
BAG_MENU_WATCHED_KEYS = 0x07
TARGET_MENU_WATCHED_KEYS = 0x03
BAG_LIST_TEXTBOX = 13
TARGET_TEXTBOX = 1
BATTLE_MENU_FIGHT = 0
BATTLE_MENU_ITEM = 1

# ``wEnemySelectedMove`` is zero until the ROM has the opponent choose a move.
ENEMY_MOVE_NONE = 0
# The move the opponent used in the captured turn, observed on screen as
# "used LEER" and in ``wEnemySelectedMove``.  Pinned as regression evidence for
# this fixture; the load-bearing check is that the observation exists at all and
# lands after the application.
CAPTURED_OPPONENT_MOVE = 43

# ``wTileMap`` is the 20x18 text/graphics buffer; the item list starts at row 4
# and names are drawn two rows apart (name, then quantity).
TILEMAP_WIDTH = 20
TILEMAP_HEIGHT = 18
BAG_LIST_FIRST_ITEM_ROW = 4
BAG_LIST_ROW_STRIDE = 2

# The captured fixture: a freshly-bought Potion heals the damaged active mon.
CAPTURED_HP = 6
CAPTURED_MAX_HP = 102
CAPTURED_HEALED_HP = 26


def _glyph(tile: int) -> str:
    """Decode the text glyphs of ``wTileMap``; graphics tiles are opaque."""
    if tile == 0x7F:
        return " "
    if 0x80 <= tile <= 0x99:
        return chr(ord("A") + tile - 0x80)
    if 0xA0 <= tile <= 0xB9:
        return chr(ord("a") + tile - 0xA0)
    if 0xF6 <= tile <= 0xFF:
        return chr(ord("0") + tile - 0xF6)
    return "_"


def _rows(session: Session) -> list[str]:
    """The drawn screen, as printed text rows."""
    base = session.symbols.addr_of("wTileMap")
    memory = session._pyboy.memory
    return [
        "".join(
            _glyph(int(memory[base + row * TILEMAP_WIDTH + column]))
            for column in range(TILEMAP_WIDTH)
        )
        for row in range(TILEMAP_HEIGHT)
    ]


def _byte(session: Session, name: str) -> int | None:
    symbol = session.symbols.get(name)
    if symbol is None:
        return None
    return int(session._pyboy.memory[symbol.addr]) & 0xFF


def _enemy_move(session: Session) -> int:
    """``wEnemySelectedMove``; zero until the ROM has the opponent choose."""
    value = session.read_game_state().battle.enemy_selected_move
    return ENEMY_MOVE_NONE if value is None else int(value) & 0xFF


def _turn_consumed(session: Session) -> bool:
    """``wActionResultOrTookTurn``: the ROM's own record that the action landed.

    Read rather than assumed, so a mutation that stops reporting the flag makes
    the turn assertions fail instead of passing on a supplied constant.
    """
    value = session.read_game_state().battle.action_result_or_took_turn
    return bool(value)


def _enemy_move_announced(session: Session) -> bool:
    """The ROM drew the opponent's move announcement ("used <MOVE>")."""
    text = " ".join(_rows(session))
    return "Enemy" in text and "used" in text


def _menu(session: Session) -> tuple[int, int]:
    state = session.read_game_state()
    return state.menu.current_item, state.menu.max_item


def _press(session: Session, button: str, ticks: int = 45) -> None:
    session.press(button, duration=6)
    session.step(ticks, render=True)


def _command_menu_up(session: Session) -> bool:
    """The command menu is the ROM's 2x2 template, not a stale cursor byte."""
    if not session.read_game_state().battle.raw_is_in_battle:
        return False
    return _menu(session)[1] == BATTLE_MENU_MAX_ITEM and _byte(session, "wMenuWatchedKeys") in (
        BATTLE_MENU_LEFT_COLUMN,
        BATTLE_MENU_RIGHT_COLUMN,
    )


def _wait_for(session: Session, predicate, *, budget: int = 14, ticks: int = 45) -> bool:
    for _ in range(budget):
        if predicate(session):
            return True
        session.step(ticks, render=True)
    return predicate(session)


def _party_snapshot(state: GameState) -> tuple[PartyMonSnapshot, ...]:
    mons = state.party.mons or ()
    return tuple(
        PartyMonSnapshot(
            slot=mon.slot,
            species=mon.species,
            level=mon.level,
            hp=mon.hp,
            max_hp=mon.max_hp,
            status=mon.status.raw,
            moves=mon.moves,
            pp=mon.pp,
        )
        for mon in mons
    )


def _bag_snapshot(state: GameState) -> BagSnapshot:
    bag = state.bag
    return BagSnapshot(
        stacks=tuple(BagStack(item_id=s.item_id, quantity=s.quantity) for s in bag.stacks),
        terminator=BAG_TERMINATOR,
        raw_count=bag.raw_count,
        terminator_position=bag.terminator_index,
        valid=bag.valid,
    )


def _snapshot(session: Session, *, turn_consumed: bool) -> ItemUseSnapshot:
    """The selected-item observation, keyed by the ROM's active party slot."""
    state = session.read_game_state()
    slot = state.party.active_slot
    assert slot is not None, "the active party slot is unavailable"
    active = state.party.active_mon
    assert active is not None, "the active battle record is unavailable"
    snapshot = ItemUseSnapshot(
        item_id=POTION,
        target_slot=slot,
        bag=_bag_snapshot(state),
        party=_party_snapshot(state),
        active=ActiveBattleSnapshot(
            player_slot=slot,
            species=active.species,
            level=active.level,
            hp=active.hp,
            max_hp=active.max_hp,
            status=active.status.raw,
            moves=active.moves,
            pp=active.pp,
            # Observed continuation, not an assumption: the item action spends
            # the turn, so the player's next command boundary only returns after
            # the opponent has acted.
            turn_consumed=turn_consumed,
        ),
    )
    validate_snapshot(snapshot)
    return snapshot


def _bag_cursor_for(session: Session, label: str) -> int:
    """Cursor index of the drawn bag row showing ``label``.

    The list is read from the screen rather than assumed from bag order: the
    cursor index is the row's position in the drawn list, and the caller
    re-verifies the label after moving there.
    """
    rows = _rows(session)
    max_item = _menu(session)[1]
    for row, text in enumerate(rows):
        offset = row - BAG_LIST_FIRST_ITEM_ROW
        if offset < 0 or offset % BAG_LIST_ROW_STRIDE or label not in text:
            continue
        index = offset // BAG_LIST_ROW_STRIDE
        if index <= max_item:
            return index
    raise AssertionError(f"the drawn bag list does not show {label!r}: {rows[3:11]}")


@dataclass(frozen=True, slots=True)
class _ItemUse:
    """One observed ITEM -> POTION -> target turn."""

    slot: int
    before: ItemUseSnapshot
    after: ItemUseSnapshot
    continued: ItemUseSnapshot
    boundary: ItemUseSnapshot
    opponent_action: _OpponentAction
    money_before: int
    money_after: int
    timeline: ActionTimeline


@dataclass(frozen=True, slots=True)
class _OpponentAction:
    """The opponent's action, as observed in the ROM's state and on screen.

    ``after_application`` records that the observation was taken after the
    application settled (``wEnemySelectedMove`` was still zero at that point),
    and ``announced`` records that the ROM was seen drawing its move
    announcement while the action resolved.  Both are required before the
    timeline may claim the opponent acted, so the action count is derived from
    evidence instead of being supplied.
    """

    move_id: int
    after_application: bool
    announced: bool


def _item_turn_timeline(
    *,
    before: ItemUseSnapshot,
    after: ItemUseSnapshot,
    boundary: ItemUseSnapshot,
    opponent_action: _OpponentAction | None,
) -> ActionTimeline:
    """Assemble the turn timeline from observations, or refuse to.

    This is the falsifiable core of the acceptance test: it raises when the
    opponent's action was not observed, was not observed after the application,
    or was not announced by the ROM.  It is deliberately pure so the negative
    control can exercise it without an emulator.
    """
    if opponent_action is None:
        raise AssertionError("no opponent action was observed before the boundary returned")
    if not opponent_action.after_application:
        raise AssertionError("the opponent action was not observed after the application")
    if not opponent_action.announced:
        raise AssertionError("the ROM never announced the opponent's move")

    timeline = ActionTimeline()
    timeline.record(EVENT_COMMAND_SELECTION, consumes_action=True)
    timeline.record(EVENT_ITEM_SELECTION, item_id=POTION, target_slot=before.target_slot)
    timeline.record(
        EVENT_APPLICATION,
        item_id=POTION,
        target_slot=before.target_slot,
        consumed=1,
        hp_delta=after.mon(after.target_slot).hp - before.mon(before.target_slot).hp,
        reason=REASON_APPLIED,
    )
    timeline.record(
        EVENT_OPPONENT_ACTION,
        hp_delta=boundary.mon(boundary.target_slot).hp - after.mon(after.target_slot).hp,
    )
    timeline.record(EVENT_NEXT_COMMAND)
    return timeline


def _play_potion_turn(session: Session) -> _ItemUse:
    """Drive ITEM -> POTION -> target and observe the whole turn."""
    assert _wait_for(session, _command_menu_up), "the battle command menu never appeared"
    assert session.read_game_state().battle.is_wild_battle, "the fixture is not a wild battle"
    assert _byte(session, "wMenuWatchedKeys") == BATTLE_MENU_LEFT_COLUMN

    before = _snapshot(session, turn_consumed=False)
    money_before = session.read_game_state().progress.money

    # FIGHT is cursor 0 and ITEM is cursor 1 in the left column; down toggles
    # inside the column, so the ROM stays on the left-column key mask.
    for _ in range(8):
        if _menu(session)[0] == BATTLE_MENU_ITEM:
            break
        _press(session, "down", 48)
    assert _menu(session)[0] == BATTLE_MENU_ITEM, f"battle cursor is {_menu(session)}"
    _press(session, "a", 90)

    def bag_list_up(s: Session) -> bool:
        return (
            _byte(s, "wTextBoxID") == BAG_LIST_TEXTBOX
            and _byte(s, "wMenuWatchedKeys") == BAG_MENU_WATCHED_KEYS
        )

    assert _wait_for(session, bag_list_up), "the in-battle bag list never opened"

    row = _bag_cursor_for(session, "POTION")
    for _ in range(8):
        if _menu(session)[0] == row:
            break
        _press(session, "down", 45)
    assert _menu(session)[0] == row, f"bag cursor is {_menu(session)}, wanted row {row}"
    drawn = _rows(session)[BAG_LIST_FIRST_ITEM_ROW + BAG_LIST_ROW_STRIDE * row]
    assert "POTION" in drawn, f"cursor row does not show POTION: {drawn!r}"
    _press(session, "a", 90)

    def target_prompt_up(s: Session) -> bool:
        return (
            _byte(s, "wTextBoxID") == TARGET_TEXTBOX
            and _byte(s, "wMenuWatchedKeys") == TARGET_MENU_WATCHED_KEYS
        )

    assert _wait_for(session, target_prompt_up), "the item target prompt never appeared"
    hp_before = before.mon(before.target_slot).hp
    # ``wEnemySelectedMove`` is zero until the ROM has the opponent choose, so
    # a non-zero reading taken before the application would mean the ordering
    # claimed below is wrong.
    assert _enemy_move(session) == ENEMY_MOVE_NONE, "the opponent had already chosen a move"
    _press(session, "a", 60)
    for _ in range(8):
        if session.read_game_state().party.active_mon.hp != hp_before:
            break
        session.step(45, render=True)
    after = _snapshot(session, turn_consumed=_turn_consumed(session))
    assert after.mon(after.target_slot).hp != hp_before, "the Potion was never applied"
    # The application settled while the opponent had still not chosen: the
    # observed order really is application-then-opponent, not an assumption.
    assert _enemy_move(session) == ENEMY_MOVE_NONE, (
        "the opponent acted before the application settled"
    )
    assert after.active.turn_consumed, "the ROM did not record the item as taking the turn"

    # The application message waits for a button press; advancing it must not
    # consume a second unit.
    _press(session, "a", 60)
    continued = _snapshot(session, turn_consumed=_turn_consumed(session))
    money_after = session.read_game_state().progress.money

    # The opponent's action is *observed* rather than asserted.  The checks above
    # prove the opponent had still not chosen a move while the Potion was being
    # applied, so this loop -- which starts after the application -- can only see
    # a choice the opponent made afterwards.  ``wEnemySelectedMove`` is not reset
    # for the rest of the battle, so the first non-zero reading is kept and the
    # announcement is latched: a later poll may already show the returned command
    # menu, and losing an observation that did happen would be wrong.
    first_move: int | None = None
    announced = False
    assert _enemy_move(session) == ENEMY_MOVE_NONE, (
        "the opponent had already chosen when the application settled"
    )
    for _ in range(20):
        if _command_menu_up(session):
            break
        _press(session, "a", 45)
        move = _enemy_move(session)
        if move == ENEMY_MOVE_NONE:
            continue
        if first_move is None:
            first_move = move
        announced = announced or _enemy_move_announced(session)
    opponent_action = (
        None
        if first_move is None
        else _OpponentAction(move_id=first_move, after_application=True, announced=announced)
    )
    assert _command_menu_up(session), "the battle never returned to the player's command menu"
    assert opponent_action is not None, "the opponent's action was never observed"
    boundary = _snapshot(session, turn_consumed=_turn_consumed(session))
    assert session.read_game_state().battle.raw_is_in_battle, "the battle ended mid-turn"

    timeline = _item_turn_timeline(
        before=before,
        after=after,
        boundary=boundary,
        opponent_action=opponent_action,
    )

    return _ItemUse(
        slot=before.target_slot,
        before=before,
        after=after,
        continued=continued,
        boundary=boundary,
        opponent_action=opponent_action,
        money_before=money_before,
        money_after=money_after,
        timeline=timeline,
    )


def _load_evidence() -> dict:
    return json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))


def _evidence_fixture() -> dict:
    fixtures = [item for item in _load_evidence()["fixtures"] if item["id"] == FIXTURE_ID]
    assert len(fixtures) == 1, f"expected one {FIXTURE_ID!r} entry, found {len(fixtures)}"
    return fixtures[0]


def _open_session() -> Session:
    rom = rom_path(FIXTURE_VERSION)
    symbols = sym_path(FIXTURE_VERSION)
    missing = [str(path) for path in (rom, symbols) if not path.is_file()]
    if missing:
        pytest.skip("Missing BYO ROM/SYM assets: " + ", ".join(missing))

    pins = load_versions(PROJECT_ROOT / "VERSIONS.md")
    rom_sha1 = pins.sha1_for_path(ROM_PIN_PATH)
    symbol_sha1 = pins.symbol_sha1_for_path(SYM_PIN_PATH)
    assert rom_sha1 is not None, "Missing canonical Yellow ROM SHA-1 pin"
    assert symbol_sha1 is not None, "Missing canonical Yellow SYM SHA-1 pin"
    return Session.from_files(
        rom,
        symbols,
        expected_rom_sha1=rom_sha1,
        expected_symbol_sha1=symbol_sha1,
        expected_pyboy_version=pins.pyboy_version,
        expected_pyboy_revision=pins.pyboy_revision,
    )


def _is_lower_hex(value: object, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def test_turn_evidence_is_required_rather_than_supplied() -> None:
    """The turn timeline must refuse an opponent action it did not observe.

    ROM-free negative control: it exercises the pure timeline builder and the
    shared validator, so it belongs in the unit tier.  It is not real-ROM
    acceptance evidence and makes no gameplay claim.
    """
    before: ItemUseSnapshot | None = None

    # Missing evidence, evidence that predates the application, and evidence the
    # ROM never announced are each refused before any observation is consumed.
    with pytest.raises(AssertionError, match="no opponent action was observed"):
        _item_turn_timeline(before=before, after=before, boundary=before, opponent_action=None)
    with pytest.raises(AssertionError, match="not observed after the application"):
        _item_turn_timeline(
            before=before,
            after=before,
            boundary=before,
            opponent_action=_OpponentAction(
                move_id=CAPTURED_OPPONENT_MOVE, after_application=False, announced=True
            ),
        )
    with pytest.raises(AssertionError, match="never announced"):
        _item_turn_timeline(
            before=before,
            after=before,
            boundary=before,
            opponent_action=_OpponentAction(
                move_id=CAPTURED_OPPONENT_MOVE, after_application=True, announced=False
            ),
        )

    # The shared validator already rejects a duplicated or reordered opponent
    # action, so the builder cannot smuggle one in unnoticed.
    def item_turn(*opponent_positions: int) -> ActionTimeline:
        timeline = ActionTimeline()
        timeline.record(EVENT_COMMAND_SELECTION, consumes_action=True)
        timeline.record(EVENT_ITEM_SELECTION, item_id=POTION, target_slot=0)
        for position in opponent_positions:
            if position == -1:
                timeline.record(EVENT_OPPONENT_ACTION)
        timeline.record(EVENT_APPLICATION, item_id=POTION, target_slot=0, consumed=1)
        for position in opponent_positions:
            if position == 1:
                timeline.record(EVENT_OPPONENT_ACTION)
        timeline.record(EVENT_NEXT_COMMAND)
        return timeline

    assert (
        account_timeline(item_turn(1), continuation=SUCCESSFUL_ITEM_CONTINUATION).opponent_actions
        == 1
    )
    with pytest.raises(AssertionError, match="opponent action"):
        account_timeline(item_turn(), continuation=SUCCESSFUL_ITEM_CONTINUATION)
    with pytest.raises(AssertionError, match="precedes the item application"):
        account_timeline(item_turn(-1), continuation=SUCCESSFUL_ITEM_CONTINUATION)
    with pytest.raises(AssertionError, match="opponent action"):
        account_timeline(item_turn(1, 1), continuation=SUCCESSFUL_ITEM_CONTINUATION)


def test_release_evidence_names_the_pinned_assets_and_producer() -> None:
    document = _load_evidence()
    assert document["manifest_id"] == "pokered-harness.battle-healing-fixtures"
    assert document["manifest_version"] == 1
    assert document["fixture_root"] == "tests/fixtures/link"

    fixture = _evidence_fixture()
    assert fixture["path"] == f"{FIXTURE_VERSION}/{FIXTURE_NAME}"
    assert fixture["version"] == FIXTURE_VERSION
    assert fixture["producer"] == PRODUCER_PATH
    assert (PROJECT_ROOT / PRODUCER_PATH).is_file()
    assert _is_lower_hex(fixture["sha1"], 40), "fixture SHA-1 is not lowercase hex"
    assert _is_lower_hex(fixture["sha256"], 64), "fixture SHA-256 is not lowercase hex"
    assert fixture["size_bytes"] > 0

    # The pinned inputs must agree with VERSIONS.md, not merely with each other.
    pins = load_versions(PROJECT_ROOT / "VERSIONS.md")
    assert fixture["expected_rom"] == {
        "path": ROM_PIN_PATH,
        "sha1": pins.sha1_for_path(ROM_PIN_PATH),
    }
    assert fixture["expected_symbols"] == {
        "path": SYM_PIN_PATH,
        "sha1": pins.symbol_sha1_for_path(SYM_PIN_PATH),
    }


def test_fixture_bytes_match_the_release_evidence() -> None:
    fixture_path_on_disk = fixture_path(FIXTURE_VERSION, FIXTURE_NAME)
    if not fixture_path_on_disk.is_file():
        pytest.skip(f"battle healing fixture is absent: {fixture_path_on_disk}")
    payload = fixture_path_on_disk.read_bytes()
    fixture = _evidence_fixture()
    assert len(payload) == fixture["size_bytes"], "fixture size disagrees with the evidence"
    assert hashlib.sha1(payload).hexdigest() == fixture["sha1"], "fixture SHA-1 mismatch"
    assert hashlib.sha256(payload).hexdigest() == fixture["sha256"], "fixture SHA-256 mismatch"


def test_producer_refuses_existing_output_and_unpinned_assets(tmp_path: Path) -> None:
    """The producer's fail-closed guards, exercised without an emulator."""
    rom = rom_path(FIXTURE_VERSION)
    symbols = sym_path(FIXTURE_VERSION)
    rom_sha1 = load_versions(PROJECT_ROOT / "VERSIONS.md").sha1_for_path(ROM_PIN_PATH)
    assert rom_sha1 is not None
    source = tmp_path / "milestone.state"
    source.write_bytes(b"temporary milestone bytes")
    source_sha1 = hashlib.sha1(source.read_bytes()).hexdigest()
    output = tmp_path / "out.state"
    arguments = [
        "--source",
        str(source),
        "--source-sha1",
        source_sha1,
        "--rom",
        str(rom),
        "--sym",
        str(symbols),
        "--out",
        str(output),
    ]

    # An existing output is refused before any input is inspected.
    output.write_bytes(b"existing")
    with pytest.raises(producer.CaptureRefused, match="refusing to overwrite"):
        producer.main(arguments)
    assert output.read_bytes() == b"existing"
    output.unlink()

    # A milestone that does not match its recorded SHA-1 is refused.
    with pytest.raises(producer.CaptureRefused, match="source SHA-1 mismatch"):
        producer.main([*arguments[:3], "0" * 40, *arguments[4:]])
    assert not output.exists()

    # Assets that VERSIONS.md does not pin can never be recorded as pinned.
    with pytest.raises(producer.CaptureRefused, match="not pinned in VERSIONS.md"):
        producer.pinned(tmp_path / "pokemon-yellow.gbc", tmp_path / "pokemon-yellow.sym")
    assert not output.exists()


def _bundle_junit(path: Path) -> dict:
    """Read one tier's JUnit XML, deriving outcomes from the testcase elements.

    The suite counters are claims about the run; the per-testcase elements are
    the evidence.  Every counter is recomputed from the children, so a
    ``<failure>`` added to a testcase cannot keep passing behind an unchanged
    ``failures="0"``, a dropped testcase cannot keep the old total, and a second
    suite cannot arrive unnoticed.
    """
    root = ET.parse(path).getroot()
    assert root.tag in {"testsuite", "testsuites"}, f"{path} is not a JUnit document"
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    assert len(suites) == 1, f"{path} carries {len(suites)} test suites; expected exactly one"
    suite = suites[0]
    assert not suite.findall("testsuite"), f"{path} nests an unexpected test suite"

    node_ids: list[str] = []
    outcomes: dict[str, str] = {}
    for case in suite.iter("testcase"):
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        assert classname and name, f"{path} carries a testcase without a class and a name"
        node_id = f"{classname.replace('.', '/')}.py::{name}"
        assert node_id not in outcomes, f"{path} lists {node_id} more than once"
        children = sorted(child.tag for child in case)
        assert set(children) <= {"failure", "error", "skipped"}, (
            f"{path}:{node_id} carries unexpected children {children}"
        )
        node_ids.append(node_id)
        outcomes[node_id] = children[0] if children else "passed"

    derived = {
        "tests": len(node_ids),
        "failures": list(outcomes.values()).count("failure"),
        "errors": list(outcomes.values()).count("error"),
        "skipped": list(outcomes.values()).count("skipped"),
    }
    for attribute, value in derived.items():
        assert int(suite.get(attribute, -1)) == value, (
            f"{path} reports {attribute}={suite.get(attribute)!r} but its testcases give {value}"
        )
    assert derived["tests"] > 0, f"{path} carries no testcases"
    return {"node_ids": node_ids, "outcomes": outcomes, **derived}


def _bundle_terminal_summary(log: str) -> dict:
    """Parse the single pytest terminal summary line out of a captured log.

    Parsing the whole line is the point: a substring search for ``N passed``
    also matches ``1N passed``, so a drifted count could satisfy it.
    """
    matches = list(BUNDLE_TERMINAL_LINE_PATTERN.finditer(log))
    assert len(matches) == 1, (
        f"expected exactly one pytest terminal summary line, found {len(matches)}"
    )
    line = matches[0].group(0)
    parts: dict[str, int] = {}
    for count, raw_kind in BUNDLE_TERMINAL_PART_PATTERN.findall(matches[0].group("parts")):
        kind = BUNDLE_TERMINAL_KINDS.get(raw_kind, raw_kind)
        assert kind not in parts, f"the terminal summary {line!r} repeats {kind}"
        parts[kind] = int(count)
    assert parts.get("passed"), f"the terminal summary {line!r} reports no passing tests"
    for kind in ("failed", "errors", "skipped", "xfailed", "error"):
        assert parts.get(kind, 0) == 0, f"the terminal summary {line!r} reports {kind}"
    return {"line": line, "passed": parts["passed"], "parts": parts}


def _bundle_results_row(results_text: str, tier: str) -> dict:
    """Parse one tier's row out of ``results.txt``."""
    rows = [
        match
        for match in BUNDLE_RESULTS_ROW_PATTERN.finditer(results_text)
        if match.group("tier") == tier
    ]
    assert len(rows) == 1, f"results.txt carries {len(rows)} rows for the {tier} tier"
    row = rows[0]
    assert row.group("status") == "PASS", f"the {tier} row is not a PASS row"
    return {
        "summary": row.group("summary"),
        "tests": int(row.group("tests")),
        "failures": int(row.group("failures")),
        "errors": int(row.group("errors")),
        "skipped": int(row.group("skipped")),
    }


def _bundle_absolute_path_leaks(contents: str) -> list[str]:
    """Absolute-path forms present in one committed file."""
    leaks = [fragment for fragment in BUNDLE_FORBIDDEN_FRAGMENTS if fragment in contents]
    if "file://" in contents:
        leaks.append("file://")
    # Documentation URLs are allowed, so they are neutralized before scanning.
    scrubbed = BUNDLE_URL_PATTERN.sub(" ", contents)
    # A bracketed placeholder stands in for a machine root.  Replacing it with a
    # word character leaves the remainder relative, so the scan does not fire on
    # the placeholder itself while still seeing a real absolute path that
    # follows it on the same line.
    scrubbed = BUNDLE_PLACEHOLDER_ROOT_PATTERN.sub("X", scrubbed)
    for pattern in (
        BUNDLE_UNIX_ABSOLUTE_PATTERN,
        BUNDLE_WINDOWS_ABSOLUTE_PATTERN,
        BUNDLE_UNC_PATTERN,
        BUNDLE_UNC_BACKSLASH_PATTERN,
    ):
        leaks.extend(match.group(0) for match in pattern.finditer(scrubbed))
    return sorted(set(leaks))


def test_runtime_registration_bundle_is_sanitized_and_consistent() -> None:
    """The committed dual-runtime record must be true, complete and clean.

    ROM-free: it reads only committed files.  It fails closed when the bundle is
    absent, disagrees with the harness pins, hides a missing tier, reports a
    non-terminal row, drops the acceptance node id, or carries an absolute local
    path, still carries symbol-table input, or gains a file the record does not
    describe.  A registration record that can drift from the run it describes is
    not evidence, so the drift is made to fail here.
    """
    assert callable(globals()[ACCEPTANCE_NODE_ID.split("::")[1]]), (
        "the registered acceptance node id does not name a function in this module"
    )

    present = {
        path.relative_to(QUALIFICATION_BUNDLE).as_posix()
        for path in QUALIFICATION_BUNDLE.rglob("*")
        if path.is_file()
    }
    assert present == set(BUNDLE_FILES), (
        "the bundle file set changed: "
        f"missing {sorted(set(BUNDLE_FILES) - present)}, "
        f"unexpected {sorted(present - set(BUNDLE_FILES))}"
    )

    identity = json.loads(
        (QUALIFICATION_BUNDLE / "runtime-identity.json").read_text(encoding="utf-8")
    )
    assert identity["issue"] == 90
    assert identity["leaf"] == "90.3"
    assert identity["acceptance_node_id"] == ACCEPTANCE_NODE_ID
    assert identity["worktree_clean_at_run"] is True

    # The record's own guardrail declaration has to keep saying that nothing was
    # weakened; a record that admits to a skip, an xfail, a widened bound or a
    # RAM edit is not a qualification record any more.
    guardrails = identity["guardrails"]
    weakened = sorted(key for key, value in guardrails.items() if isinstance(value, bool) and value)
    assert not weakened, f"the record declares a weakened guardrail: {weakened}"
    assert all(
        isinstance(value, str) and value
        for value in guardrails.values()
        if not isinstance(value, bool)
    ), "a guardrail is declared as an empty note"

    # The record names exactly one tested state, and it names it in full.
    for key in ("worktree_head", "worktree_tree"):
        assert _is_lower_hex(identity[key], 40), f"{key} is not a lowercase hex object id"

    pins = load_versions(PROJECT_ROOT / "VERSIONS.md")
    declared_revision = identity["vendored_revision_marker"]
    assert declared_revision == pins.pyboy_revision, (
        "the bundle's vendored revision marker disagrees with VERSIONS.md"
    )
    assert set(identity["tiers"]) == set(BUNDLE_TIERS), "a declared runtime is missing"

    for tier in BUNDLE_TIERS:
        tier_identity = identity["tiers"][tier]
        assert tier_identity["requested_tier"] == tier
        assert (
            tier_identity["python_version"] == identity["tiers"][BUNDLE_TIERS[0]]["python_version"]
        ), "the two tiers did not run the same interpreter version"
        assert tier_identity["pyboy_version"] == pins.pyboy_version
        assert tier_identity["pyboy_revision"] == pins.pyboy_revision
        assert tier_identity["revision_matches_vendored_pin"] is True

    # The dual-runtime claim rests on the imports each tier actually resolved:
    # source must come from the vendored tree with no compiled extension, and
    # cython must come from outside it with compiled extensions loaded.  The
    # declared-runtime flag is derived from those measured fields rather than
    # trusted, and the interpreter, import root, loader switch and module total
    # each have to agree with the tier being claimed, so swapping fields between
    # the two tiers cannot satisfy them all.
    source = identity["tiers"]["source"]
    cython = identity["tiers"]["cython"]
    for tier, tier_identity in (("source", source), ("cython", cython)):
        kinds = tier_identity["imported_by_kind"]
        assert kinds, f"the {tier} tier recorded no imported module kinds"
        assert all(isinstance(count, int) and count > 0 for count in kinds.values())
        assert tier_identity["imported_pyboy_modules"] == sum(kinds.values()), (
            f"the {tier} tier's module total disagrees with its imported kinds"
        )
        vendored_file = tier_identity["pyboy_file"].startswith("[worktree]/vendor/pyboy-src/")
        assert tier_identity["pyboy_imported_from_vendored_tree"] is vendored_file, (
            f"the {tier} tier's import root contradicts the module file it resolved"
        )
        loader = tier_identity["pyboy_no_cython"]
        declared = (
            vendored_file and not kinds.get(".so") and loader == "1"
            if tier == "source"
            else not vendored_file and bool(kinds.get(".so")) and loader is None
        )
        assert tier_identity["is_declared_runtime"] is declared, (
            f"the {tier} tier did not measure as its declared runtime"
        )

    assert source["pyboy_imported_from_vendored_tree"] is True
    assert not source["imported_by_kind"].get(".so"), "the source tier loaded compiled extensions"
    assert "[worktree]/vendor/pyboy-src" in source["pythonpath"]
    assert source["python_executable"].startswith("[source-venv]/")
    assert cython["pyboy_imported_from_vendored_tree"] is False, (
        "the cython tier fell back to the vendored source tree"
    )
    assert cython["imported_by_kind"].get(".so"), "the cython tier loaded no compiled extensions"
    assert "vendor/pyboy-src" not in cython["pythonpath"]
    assert cython["python_executable"].startswith("[native-venv]/")
    assert source["python_executable"] != cython["python_executable"]

    results = identity["results_by_tier"]
    assert set(results) == set(BUNDLE_TIERS)
    results_text = (QUALIFICATION_BUNDLE / "results.txt").read_text(encoding="utf-8")
    for prefix, expected in (
        ("tested head   : ", identity["worktree_head"]),
        ("tested tree   : ", identity["worktree_tree"]),
        ("acceptance    : ", ACCEPTANCE_NODE_ID),
        ("modules       : ", " ".join(identity["modules"])),
    ):
        assert results_text.count(f"{prefix}{expected}") == 1, (
            f"results.txt does not name {prefix.strip()} exactly once as the record does"
        )

    executed: dict[str, list[str]] = {}
    for tier in BUNDLE_TIERS:
        recorded = results[tier]
        assert recorded["failures"] == 0 and recorded["errors"] == 0, (
            f"the {tier} row is not terminal: {recorded}"
        )
        assert recorded["skipped"] == 0, f"the {tier} row skipped a required test"
        assert recorded["tests"] > 0
        assert len(set(recorded["node_ids"])) == len(recorded["node_ids"]), (
            f"the {tier} row lists a node id more than once"
        )
        assert recorded["tests"] == len(recorded["node_ids"]), (
            f"the {tier} row's test count and its node ids disagree"
        )
        assert ACCEPTANCE_NODE_ID in recorded["node_ids"], (
            f"the {tier} row does not contain the acceptance node id"
        )

        junit = _bundle_junit(QUALIFICATION_BUNDLE / "junit" / f"{tier}-focused.xml")
        assert junit["node_ids"] == recorded["node_ids"], (
            f"the {tier} JUnit XML and the recorded node ids disagree"
        )
        assert junit["tests"] == recorded["tests"]
        assert (junit["failures"], junit["errors"], junit["skipped"]) == (0, 0, 0), (
            f"the {tier} JUnit XML records a non-passing testcase"
        )
        assert set(junit["outcomes"].values()) == {"passed"}, (
            f"the {tier} JUnit XML records a non-passing outcome"
        )
        executed[tier] = junit["node_ids"]

        log = (QUALIFICATION_BUNDLE / "logs" / f"{tier}-focused.log").read_text(encoding="utf-8")
        terminal = _bundle_terminal_summary(log)
        assert terminal["passed"] == recorded["tests"] == len(junit["node_ids"]), (
            f"the {tier} terminal count, the record and the JUnit XML disagree"
        )
        assert terminal["line"] == recorded["summary"], (
            f"the {tier} terminal line and the recorded summary disagree"
        )
        assert log.count(f"PASSED {ACCEPTANCE_NODE_ID}") == 1, (
            f"the {tier} log does not report the acceptance node id exactly once as passed"
        )

        row = _bundle_results_row(results_text, tier)
        assert row["summary"] == recorded["summary"], (
            f"the {tier} results.txt row and the record disagree on the summary"
        )
        assert (row["tests"], row["failures"], row["errors"], row["skipped"]) == (
            recorded["tests"],
            recorded["failures"],
            recorded["errors"],
            recorded["skipped"],
        ), f"the {tier} results.txt row and the record disagree on the counts"

    # The two tiers declare one shared selection, so their node lists must agree.
    assert executed[BUNDLE_TIERS[0]] == executed[BUNDLE_TIERS[1]], (
        "the two tiers did not execute the same selection"
    )

    # The module list is derived from the same run as the node ids, so it has to
    # name exactly the modules those node ids live in.  A hand-edited list would
    # otherwise let the record advertise a selection the tiers never executed.
    executed_modules: list[str] = []
    for node_id in executed[BUNDLE_TIERS[0]]:
        module = node_id.partition("::")[0]
        if module not in executed_modules:
            executed_modules.append(module)
    assert identity["modules"] == executed_modules, (
        "the recorded module list does not match the modules the tiers executed: "
        f"recorded {identity['modules']}, executed {executed_modules}"
    )
    for module in identity["modules"]:
        assert re.fullmatch(r"tests/[A-Za-z0-9_]+\.py", module), (
            f"the recorded module {module!r} is not a test module path"
        )
        assert module.partition("tests/")[2] in KNOWN_TEST_MODULES, (
            f"the recorded module {module!r} is not a reviewed test module"
        )

    # A record whose node ids no longer name live tests describes a tree that no
    # longer exists, so it must stop being citable rather than linger.  Every
    # recorded tier is checked, not only the first one.
    for tier in BUNDLE_TIERS:
        for node_id in results[tier]["node_ids"]:
            module_path, _, test_name = node_id.partition("::")
            module = importlib.import_module(module_path[: -len(".py")].replace("/", "."))
            assert callable(getattr(module, test_name, None)), (
                f"{node_id} is registered in the {tier} row but does not exist in this tree"
            )

    # The record's inputs are the harness's own declared inputs, not merely
    # strings that agree with each other.
    assets = {entry["label"]: entry for entry in identity["assets"]}
    fixture_label = f"tests/fixtures/link/{FIXTURE_VERSION}/{FIXTURE_NAME}"
    assert set(assets) == {ROM_PIN_PATH, SYM_PIN_PATH, fixture_label}
    assert assets[ROM_PIN_PATH]["sha1"] == pins.sha1_for_path(ROM_PIN_PATH), (
        "the ROM digest in the bundle disagrees with VERSIONS.md"
    )
    assert assets[SYM_PIN_PATH]["sha1"] == pins.symbol_sha1_for_path(SYM_PIN_PATH), (
        "the symbol-table digest in the bundle disagrees with VERSIONS.md"
    )
    fixture = _evidence_fixture()
    assert assets[fixture_label]["sha1"] == fixture["sha1"]
    assert assets[fixture_label]["sha256"] == fixture["sha256"]
    assert assets[fixture_label]["size_bytes"] == fixture["size_bytes"]
    for label, entry in assets.items():
        assert _is_lower_hex(entry["sha1"], 40), f"{label} carries no lowercase SHA-1"
        assert _is_lower_hex(entry["sha256"], 64), f"{label} carries no lowercase SHA-256"
        assert entry["size_bytes"] > 0

    # The redaction is a counted claim about the committed bytes: the file the
    # guard just read must be the redacted file the record names, the marker must
    # appear exactly once with the recorded count, and the elided payloads must
    # remain auditable outside the repository.
    redactions = identity["redactions"]
    assert set(redactions["tiers"]) == set(BUNDLE_TIERS)
    for tier in BUNDLE_TIERS:
        record = redactions["tiers"][tier]
        assert record["file"] == f"logs/{tier}-focused.log"
        raw = (QUALIFICATION_BUNDLE / record["file"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == record["redacted_sha256"], (
            f"the committed {tier} log is not the redacted file the record names"
        )
        assert len(raw) == record["redacted_size_bytes"]
        text = raw.decode("utf-8")
        assert record["payload_lines_elided"] > 0, (
            f"the {tier} redaction elides no payloads, so it records nothing"
        )
        assert text.count(record["marker"]) == 1, (
            f"the committed {tier} log does not carry the recorded redaction marker exactly once"
        )
        assert f"<redacted: {record['payload_lines_elided']} private symbol payloads" in text, (
            f"the committed {tier} log marker does not carry the recorded elided count"
        )
        assert _is_lower_hex(record["private_original_sha256"], 64), (
            "the private original digest is not a lowercase SHA-256"
        )
        assert record["private_original_size_bytes"] > record["redacted_size_bytes"] > 0
        assert record["private_original_label"].startswith("private-game-states/"), (
            "the private originals must be retained outside the repository"
        )

    for relative in sorted(present):
        contents = (QUALIFICATION_BUNDLE / relative).read_text(encoding="utf-8", errors="replace")
        assert BUNDLE_SYMBOL_PAYLOAD_PATTERN.search(contents) is None, (
            f"{relative} carries unredacted symbol-table input"
        )
        leaked = _bundle_absolute_path_leaks(contents)
        assert not leaked, f"{relative} leaks {leaked}"


def test_potion_heals_the_active_mon_from_the_battle_item_menu() -> None:
    fixture = fixture_path(FIXTURE_VERSION, FIXTURE_NAME)
    if not fixture.is_file():
        pytest.skip(f"battle healing fixture is absent: {fixture}")

    session = _open_session()
    try:
        session.load_state(fixture.read_bytes())
        session.step(60, render=True)
        observed = _play_potion_turn(session)
    finally:
        session.close(save=False)

    before, after = observed.before, observed.after
    assert before.bag.quantity_of(POTION) == 1, "the fixture must hold exactly one Potion"
    assert observed.money_after == observed.money_before, "an in-battle item must not cost money"

    outcome = assert_medicine_application(before, after, POTION_RULE, expected_consumed=1)
    assert outcome.applied and outcome.reason == REASON_APPLIED
    assert outcome.hp == capped_heal(CAPTURED_HP, CAPTURED_MAX_HP, POTION_HEAL)
    assert (before.mon(observed.slot).hp, before.mon(observed.slot).max_hp) == (
        CAPTURED_HP,
        CAPTURED_MAX_HP,
    )
    assert after.mon(observed.slot).hp == CAPTURED_HEALED_HP
    assert_last_unit_compaction(before, after, POTION)
    assert_continuation_idempotent(before, after, observed.continued, POTION)

    # The opponent's action is evidence, not an input to the timeline: the ROM
    # must have chosen and announced a move after the application settled.
    assert observed.opponent_action.after_application, "opponent action ordering is unobserved"
    assert observed.opponent_action.announced, "the opponent's move was never announced"
    assert observed.opponent_action.move_id == CAPTURED_OPPONENT_MOVE, (
        f"observed opponent move {observed.opponent_action.move_id} "
        f"!= pinned {CAPTURED_OPPONENT_MOVE}"
    )

    account = assert_successful_item_turn(
        observed.timeline,
        item_id=POTION,
        target_slot=observed.slot,
        expected_hp_delta=POTION_HEAL,
    )
    assert account.applied and account.opponent_actions == 1
    # The heal survives the opponent's action and the returned command boundary.
    assert observed.boundary.mon(observed.slot).hp == CAPTURED_HEALED_HP
