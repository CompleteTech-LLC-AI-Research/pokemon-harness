"""ROM-free contract tests for the ordinary-battle medicine fixture producer.

These tests never instantiate PyBoy and never load a ROM, symbol file, or state.
They pin the parts of ``scripts/produce_battle_healing_fixture.py`` that a real
emulator run would otherwise be the only thing to exercise:

* the per-game ``GAMES`` table - the ``wTileMap`` cell each game's own
  ``engine/battle/wild_encounters.asm`` predicate reads, and where the pinned
  milestone starts;
* the ``--game`` CLI surface, including the renamed ``--version`` flag; and
* the two fail-closed decisions taken on the capture path, the full-HP refusal
  and the fresh-battle save boundary.

They exist because the module that consumes the producer's output,
``tests/test_battle_healing_items_rom.py``, needs operator-managed ROM and
fixture assets and therefore *skips* in an asset-free checkout.  Without a
committed control here, reverting the producer's own subject matter - the
Red/Blue encounter column, the entry route, the save-boundary gate, or the
full-HP refusal - leaves the committed suite green.  The sibling producer
``scripts/produce_battle_scenario.py`` carries the equivalent ROM-free contract
module in the ``tests/test_battle_scenario_*.py`` suite.
"""

from __future__ import annotations

from collections import deque
from typing import Any

import pytest

from scripts import produce_battle_healing_fixture as producer

#: The flags every invocation must supply; values are never read without a
#: live session, so they only have to parse.
REQUIRED_ARGV = [
    "--source",
    "milestone.state",
    "--source-sha1",
    "0" * 40,
    "--rom",
    "pokemon-blue-color.gb",
    "--sym",
    "pokemon-blue.sym",
    "--out",
    "out.state",
]


def _argv(*extra: str) -> list[str]:
    return [*REQUIRED_ARGV, *extra]


def test_games_table_declares_exactly_the_shipped_games() -> None:
    """A new game must be a reviewed addition, not an incidental side effect."""
    assert set(producer.GAMES) == {"yellow", "blue", "red"}


@pytest.mark.parametrize(
    ("game", "row", "col"),
    [("yellow", 9, 8), ("blue", 9, 9), ("red", 9, 9)],
)
def test_encounter_cell_is_per_game(game: str, row: int, col: int) -> None:
    """The cell the ROM's own predicate reads is a per-game parameter."""
    spec = producer.GAMES[game]
    assert (spec.encounter_row, spec.encounter_col) == (row, col)


def test_yellow_does_not_share_the_red_blue_encounter_column() -> None:
    """Negative control for the bug this table exists to prevent.

    Yellow's ``hlcoord 8, 9`` is the bottom-*left* half-block tile and Red/Blue's
    ``hlcoord 9, 9`` is the bottom-*right* one.  If a single column were shared by
    all three, sampling it would test an adjacent tile and misreport the ROM.
    """
    assert producer.GAMES["yellow"].encounter_col == 8
    assert producer.GAMES["blue"].encounter_col == 9
    assert producer.GAMES["red"].encounter_col == 9
    assert producer.GAMES["yellow"].encounter_col != producer.GAMES["red"].encounter_col


@pytest.mark.parametrize(
    ("game", "entry"),
    [("yellow", "gym"), ("blue", "gym"), ("red", "city")],
)
def test_entry_route_is_per_game(game: str, entry: str) -> None:
    """A milestone already in the city must not be driven at the gym-door waypoint."""
    assert producer.GAMES[game].entry == entry


def test_game_flag_defaults_to_yellow() -> None:
    assert producer.parse_args(_argv()).game == "yellow"


@pytest.mark.parametrize("game", ["yellow", "blue", "red"])
def test_game_flag_accepts_every_declared_game(game: str) -> None:
    assert producer.parse_args(_argv("--game", game)).game == game


def test_game_flag_rejects_an_undeclared_game() -> None:
    with pytest.raises(SystemExit):
        producer.parse_args(_argv("--game", "green"))


def test_the_old_version_flag_is_gone() -> None:
    """``--version`` was renamed to ``--game``; the old spelling must not parse.

    ``scripts/produce_cable_club_fixture.py`` genuinely uses ``--version``, so a
    silent aliasing here would invite the wrong flag in a copy-pasted command.
    """
    with pytest.raises(SystemExit):
        producer.parse_args(_argv("--version", "yellow"))


@pytest.mark.parametrize(("hp", "max_hp"), [(102, 102), (1, 1)])
def test_full_hp_milestone_without_a_burn_move_is_refused(hp: int, max_hp: int) -> None:
    """A fixture saved here would hold a Potion with nothing to heal."""
    with pytest.raises(producer.CaptureRefused, match="at full HP and no --burn-move"):
        producer.require_damage_source(hp, max_hp, None)


def test_full_hp_milestone_with_a_burn_move_is_accepted() -> None:
    assert producer.require_damage_source(102, 102, 2) is None


@pytest.mark.parametrize(
    ("hp", "max_hp", "burn_move"),
    [(150, 152, None), (150, 152, 3), (1, 2, None)],
)
def test_an_already_damaged_milestone_needs_no_burn_move(
    hp: int, max_hp: int, burn_move: int | None
) -> None:
    assert producer.require_damage_source(hp, max_hp, burn_move) is None


def test_an_absent_active_mon_is_not_treated_as_already_damaged() -> None:
    """Unknown HP is not evidence of damage; the refusal must still fire."""
    with pytest.raises(producer.CaptureRefused, match="at full HP and no --burn-move"):
        producer.require_damage_source(None, None, None)


@pytest.mark.parametrize("selected_move", [1, 0x2D, 255, None])
def test_save_boundary_refuses_a_queued_opponent_move(selected_move: int | None) -> None:
    """A state saved one turn in is not a fresh ordinary-battle fixture.

    ``None`` is included deliberately: ``Driver.byte`` returns ``None`` when the
    symbol cannot be read, and that must fail closed rather than be mistaken for
    ``wEnemySelectedMove == 0``.
    """
    with pytest.raises(producer.CaptureRefused, match="already has the opponent's move queued"):
        producer.require_fresh_battle(selected_move)


def test_save_boundary_accepts_a_fresh_battle() -> None:
    assert producer.require_fresh_battle(0) is None


class _FakeMon:
    def __init__(self, hp: int, max_hp: int) -> None:
        self.hp = hp
        self.max_hp = max_hp


class _FakeDriver:
    """Just enough of the producer's ``Driver`` to run ``resolve_capture``.

    No emulator, no ROM, no state file.  ``in_battle`` is scripted from a queue
    because ``resolve_capture``'s throwaway path calls it a known number of
    times: once to see the battle menu, once to enter the flee loop, and once to
    leave it.
    """

    def __init__(
        self,
        *,
        hp: int,
        max_hp: int,
        selected_move: int | None,
        in_battle: tuple[bool, ...] = (),
        take_damage_on_first_press: bool = False,
    ) -> None:
        self.mon = _FakeMon(hp, max_hp)
        self.selected_move = selected_move
        self.presses: list[str] = []
        self._in_battle = deque(in_battle)
        self._damage_pending = take_damage_on_first_press

    def state(self) -> Any:
        return type("_State", (), {"party": type("_Party", (), {"active_mon": self.mon})})()

    def byte(self, name: str) -> int | None:
        if name == "wEnemySelectedMove":
            return self.selected_move
        if name == "wMenuWatchedKeys":
            return producer.BATTLE_MENU_LEFT_COLUMN
        return None

    def menu(self) -> tuple[int, int]:
        return (0, producer.BATTLE_MENU_MAX_ITEM)

    def idle(self, ticks: int) -> None:
        return None

    def battle_menu_up(self) -> bool:
        # Delegate to the real fingerprint rather than restating it here, so the
        # fake cannot drift from the menu geometry the producer relies on.
        return producer.Driver.battle_menu_up(self)

    def in_battle(self) -> bool:
        return self._in_battle.popleft() if self._in_battle else True

    def press(self, key: str, step: int = 24) -> None:
        self.presses.append(key)
        if self._damage_pending:
            self.mon.hp = self.mon.max_hp - 5
            self._damage_pending = False


def _re_encounter(record: list[str]) -> Any:
    def capture_battle() -> tuple[dict, dict]:
        record.append("re-encountered")
        return {"steps": 2}, {"kind": "WILD_BATTLE"}

    return capture_battle


def test_resolve_capture_accepts_an_already_damaged_ordinary_battle() -> None:
    driver = _FakeDriver(hp=150, max_hp=152, selected_move=0)
    signals = producer.resolve_capture(
        driver,
        burn_move=None,
        first_encounter={"steps": 1},
        first_battle={"kind": "WILD_BATTLE"},
        capture_battle=_re_encounter([]),
    )
    assert signals["damage"] == {"burns": 0, "hp": [150, 152], "already_damaged": True}
    assert signals["encounter"] == {"steps": 1}
    assert "throwaway_battle" not in signals
    assert driver.presses == []


def test_resolve_capture_refuses_full_hp_without_a_burn_move() -> None:
    """The refusal is reached through the real call path, not just the helper."""
    driver = _FakeDriver(hp=102, max_hp=102, selected_move=0)
    with pytest.raises(producer.CaptureRefused, match="at full HP and no --burn-move"):
        producer.resolve_capture(
            driver,
            burn_move=None,
            first_encounter={},
            first_battle={},
            capture_battle=_re_encounter([]),
        )
    assert driver.presses == []


def test_resolve_capture_refuses_a_stale_battle_after_damage() -> None:
    """A queued opponent move is refused even though the damage step succeeded."""
    driver = _FakeDriver(hp=150, max_hp=152, selected_move=0x2D)
    with pytest.raises(producer.CaptureRefused, match="already has the opponent's move queued"):
        producer.resolve_capture(
            driver,
            burn_move=None,
            first_encounter={},
            first_battle={},
            capture_battle=_re_encounter([]),
        )


def test_resolve_capture_burns_in_a_throwaway_battle_and_re_encounters() -> None:
    """The full-HP path burns a turn, discards that battle, and re-encounters."""
    record: list[str] = []
    driver = _FakeDriver(
        hp=102,
        max_hp=102,
        selected_move=0,
        in_battle=(True, True, False),
        take_damage_on_first_press=True,
    )
    signals = producer.resolve_capture(
        driver,
        burn_move=2,
        first_encounter={"steps": 1},
        first_battle={"kind": "WILD_BATTLE"},
        capture_battle=_re_encounter(record),
    )
    assert record == ["re-encountered"]
    assert signals["damage"] == {"burns": 1, "hp": [97, 102], "already_damaged": False}
    assert signals["throwaway_battle"] == {
        "encounter": {"steps": 1},
        "battle": {"kind": "WILD_BATTLE"},
    }
    assert signals["encounter"] == {"steps": 2}
    assert signals["enemy_selected_move"] == 0
    assert driver.presses[0] == "a"
