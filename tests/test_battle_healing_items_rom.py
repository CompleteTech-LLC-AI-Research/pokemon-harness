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
import json
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

FIXTURE_VERSION = "yellow"
FIXTURE_NAME = "battle_healing.state"
FIXTURE_ID = "yellow-ordinary-battle-healing"
EVIDENCE_PATH = PROJECT_ROOT / "release-evidence" / "battle-healing-fixtures.json"
PRODUCER_PATH = "scripts/produce_battle_healing_fixture.py"
ROM_PIN_PATH = "rom/yellow/pokemon-yellow.gbc"
SYM_PIN_PATH = "rom/yellow/pokemon-yellow.sym"

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
