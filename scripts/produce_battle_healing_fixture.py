"""Produce the ordinary-battle healing fixture used by the #90.3 ROM test.

The #90 medicine workstream needs an *ordinary battle* whose bag already holds a
medicine item, and no retained milestone provides one (no Yellow milestone is
in a battle at all, and none holds a Potion).  This producer drives one for real
from a pinned milestone with real button input:

1. reach Pewter Mart -- Yellow/Blue start inside Pewter Gym and leave it first,
   Red's milestone starts in Pewter City and enters the mart directly;
2. buy a POTION at the counter (the settle signal is the money delta, because
   the bag already holds Brock's TM and a truthiness check would pass without a
   purchase);
3. walk to Route 2 north and step into grass until the ROM's own encounter check
   fires.  Yellow's ``engine/battle/wild_encounters.asm`` reads the player's
   *bottom-left* half-block tile, ``hlcoord 8, 9`` -- ``wTileMap[9][8]`` -- and
   compares it with ``wGrassTile``; Red/Blue read the bottom-right tile
   ``hlcoord 9, 9`` instead, so the two games sample different cells;
4. save the state at the first command-menu boundary of that wild battle.

The retained Red and Blue ``after_brock`` milestones are at full HP, unlike
Yellow's damaged ``brock_badge``, so those captures pass ``--burn-move`` to burn
a turn with a non-damaging move and let the wild mon damage the active mon
before the save; Yellow needs no burn because it is already damaged.

The contract is bounded and fail-closed, in the style of
``scripts/produce_battle_scenario.py``: the ROM and symbol files must be pinned
in ``VERSIONS.md``, the source milestone must match a supplied SHA-1, an existing
output is never overwritten, and nothing is written unless every step was
observed.  The wild encounter is RNG-driven, so the resulting bytes are pinned by
hash in ``release-evidence/battle-healing-fixtures.json`` and verified by the
test; this script is a capture tool, not a claim of acceptance.

Usage:
    PYTHONPATH=src POKERED_PRET_ROOT=<pret/pokeyellow checkout> \\
    python -u scripts/produce_battle_healing_fixture.py \\
        --game yellow \\
        --source <milestone.state> --source-sha1 <sha1> \\
        --rom <rom root>/yellow/pokemon-yellow.gbc \\
        --sym <rom root>/yellow/pokemon-yellow.sym \\
        --out <fixture root>/yellow/battle_healing.state

``--rom``/``--sym`` take real paths to the assets pinned in ``VERSIONS.md``;
``POKERED_PRET_ROOT`` is inherited by the ``scripts/path_from_tiles.py``
subprocess the driver shells out to for navigation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

from pokered_harness.config import load_versions
from pokered_harness.session import Session

# Map ids are shared across Gen 1 (Kanto maps keep their ids).
PEWTER_CITY = 0x02
PEWTER_GYM = 0x36
PEWTER_MART = 0x38
ROUTE_2 = 0x0D

POTION = 0x14
POTION_PRICE = 300

# The encounter check reads the player's own half-block of the 20x18
# ``wTileMap`` view.  Yellow's ``engine/battle/wild_encounters.asm`` reads the
# *bottom-left* tile ``hlcoord 8, 9`` (row 9, column 8); Red/Blue read the
# *bottom-right* tile ``hlcoord 9, 9`` (row 9, column 9).  Sampling the wrong
# cell tests an adjacent tile and misreports the ROM's own predicate, so the
# cell is a per-game parameter of :data:`GAMES` rather than one constant.


class GameSpec(NamedTuple):
    """Per-game parameters the driver must not get from the Yellow default."""

    #: ``wTileMap`` row/column the game's own wild-encounter predicate reads.
    encounter_row: int
    encounter_col: int
    #: Where the pinned milestone starts: ``"gym"`` inside Pewter Gym (the
    #: producer leaves the gym first), ``"city"`` already in Pewter City.
    entry: str


GAMES: dict[str, GameSpec] = {
    # ``hlcoord 8, 9`` -> the bottom-left tile of the player's half-block.
    "yellow": GameSpec(encounter_row=9, encounter_col=8, entry="gym"),
    # ``hlcoord 9, 9`` -> the bottom-right tile; see #90.3 scoping notes.
    "blue": GameSpec(encounter_row=9, encounter_col=9, entry="gym"),
    "red": GameSpec(encounter_row=9, encounter_col=9, entry="city"),
}

# The counter clerk occupies (1,5); the walkable tile the player stands on to
# face them is (2,5).  ``scripts/path_from_tiles.py`` force-marks the goal cell
# walkable, so asking it for (1,5) returns a plan whose last step is blocked and
# which therefore ends at (2,5) -- the arrival is checked against the stand tile
# and confirmed by the BUY/SELL/QUIT list opening.
CLERK_STAND = "2,5"

# The ROM's own battle-menu fingerprint (engine/battle/core.asm:DisplayBattleMenu):
# a 2x2 menu whose left column is {FIGHT=0, ITEM=1} keyed with PAD_RIGHT|PAD_A,
# and whose right column is keyed with PAD_LEFT|PAD_A.  Down/up toggle inside a
# column, so wMaxMenuItem is 1 while the command menu is up.
BATTLE_MENU_MAX_ITEM = 1
BATTLE_MENU_LEFT_COLUMN = 0x11
BATTLE_MENU_RIGHT_COLUMN = 0x12
BAG_MENU_WATCHED_KEYS = 0x07

DIRECTIONS = {"u": "up", "d": "down", "l": "left", "r": "right"}
TILEMAP_WIDTH = 20
TILEMAP_HEIGHT = 18

# Recorded from real runs, in the style of scripts/yellow_to_brock.py.  The
# pathfinder is still consulted first; these are the verified fallbacks used
# when it refuses (it returns non-zero indoors, which is not a walk failure).
FALLBACK_PATHS = {
    "gym-door": "ddddddddddd",
    "mart-door": "lllllluuruuurrrrrrrddddrddrrrr",
    "clerk": "luul",
    "mart-exit": "ddr",
    "south-edge": "lllddddddddddddldddd",
}


class CaptureRefused(RuntimeError):
    """Raised when a request is refused before the emulator is touched."""


def sha1_of(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pinned(rom: Path, sym: Path) -> tuple[str, str]:
    """Resolve the ROM/SYM pins declared in ``VERSIONS.md`` or refuse."""
    pins = load_versions(_REPO / "VERSIONS.md")
    rom_sha1 = pins.sha1_for_path(rom)
    sym_sha1 = pins.symbol_sha1_for_path(sym)
    if rom_sha1 is None:
        raise CaptureRefused(f"ROM is not pinned in VERSIONS.md: {rom}")
    if sym_sha1 is None:
        raise CaptureRefused(f"symbol file is not pinned in VERSIONS.md: {sym}")
    return rom_sha1, sym_sha1


class Driver:
    """Minimal real-button driver over the merged public state readers."""

    def __init__(
        self,
        session: Session,
        *,
        rom: Path | None = None,
        sym: Path | None = None,
        rom_sha1: str | None = None,
        game: GameSpec | None = None,
    ) -> None:
        self.session = session
        self.memory = session._pyboy.memory
        self.symbols = session.symbols
        # The pathfinder is a separate process that loads the ROM itself, so it
        # needs the same pinned inputs the driver was handed.
        self.rom = rom
        self.sym = sym
        self.rom_sha1 = rom_sha1
        self.game = game or GAMES["yellow"]
        self.encounter_row = self.game.encounter_row
        self.encounter_col = self.game.encounter_col

    # -- observations --------------------------------------------------
    def state(self):
        return self.session.read_game_state()

    def where(self) -> tuple[int, int, int]:
        overworld = self.state().overworld
        return overworld.map_id, overworld.x, overworld.y

    def menu(self) -> tuple[int, int]:
        menu = self.state().menu
        return menu.current_item, menu.max_item

    def bag_stacks(self) -> list[tuple[int, int]]:
        bag = self.state().bag
        return [(s.item_id, s.quantity) for s in (bag.stacks if bag else ())]

    def money(self) -> int:
        return self.state().progress.money

    def hp(self) -> list[tuple[int, int]]:
        mons = self.state().party.mons or ()
        return [(m.hp, m.max_hp) for m in mons]

    def byte(self, name: str) -> int | None:
        try:
            return int(self.symbols.read_u8(self.memory, name))
        except (KeyError, LookupError):
            return None

    def rows(self) -> list[str]:
        base = self.symbols.get("wTileMap").addr
        out = []
        for row in range(TILEMAP_HEIGHT):
            tiles = [
                int(self.memory[base + row * TILEMAP_WIDTH + col]) for col in range(TILEMAP_WIDTH)
            ]
            out.append("".join(_glyph(t) for t in tiles))
        return out

    def grass_tiles(self) -> tuple[int, set[tuple[int, int]]]:
        """Grass cells of the drawn tile map, keyed by the ROM's own tile id."""
        tile = self.byte("wGrassTile") or 0
        base = self.symbols.get("wTileMap").addr
        cells = {
            (row, col)
            for row in range(TILEMAP_HEIGHT)
            for col in range(TILEMAP_WIDTH)
            if int(self.memory[base + row * TILEMAP_WIDTH + col]) == tile
        }
        return tile, cells

    def in_battle(self) -> bool:
        return bool(self.state().battle.raw_is_in_battle)

    def battle_menu_up(self) -> bool:
        """The ROM's own command-menu fingerprint, not a stale cursor byte."""
        if not self.in_battle():
            return False
        return self.menu()[1] == BATTLE_MENU_MAX_ITEM and self.byte("wMenuWatchedKeys") in (
            BATTLE_MENU_LEFT_COLUMN,
            BATTLE_MENU_RIGHT_COLUMN,
        )

    # -- actions -------------------------------------------------------
    def press(self, key: str, step: int = 24) -> None:
        self.session.press(key, duration=6)
        self.session.step(step, render=True)

    def idle(self, ticks: int) -> None:
        self.session.step(ticks, render=True)

    def step_dir(self, direction: str, attempts: int = 3) -> tuple[int, int, int]:
        before = self.where()
        for _ in range(attempts):
            self.press(DIRECTIONS[direction])
            if self.where() != before:
                return self.where()
        return before

    def dismiss(self, tries: int = 3) -> None:
        for _ in range(tries):
            if not self.byte("wTextBoxID"):
                return
            self.press("b", step=48)

    # -- navigation ----------------------------------------------------
    def pathfind(self, goal: str) -> str | None:
        scratch = self.session_path()
        state_path = scratch / "current.state"
        out_path = scratch / "path.txt"
        state_path.write_bytes(self.session.save_state())
        out_path.unlink(missing_ok=True)
        env = dict(os.environ)
        env.setdefault("PYTHONPATH", str(_REPO / "src"))
        if self.rom is not None:
            env["POKERED_ROM_PATH"] = str(self.rom)
        if self.sym is not None:
            env["POKERED_SYM_PATH"] = str(self.sym)
        if self.rom_sha1:
            env["POKERED_ROM_SHA1"] = self.rom_sha1
        result = subprocess.run(
            [
                sys.executable,
                "-u",
                str(_REPO / "scripts" / "path_from_tiles.py"),
                "--state",
                str(state_path),
                "--goal-xy",
                goal,
                "--save-path-to",
                str(out_path),
            ],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
            cwd=_REPO,
            env=env,
        )
        if result.returncode != 0 or not out_path.exists():
            return None
        return out_path.read_text().strip()

    def session_path(self) -> Path:
        path = Path(os.environ.get("TMPDIR", ".")) / "produce-battle-healing"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def walk(self, path: str, label: str) -> None:
        for index, direction in enumerate(path):
            self.step_dir(direction)
            if index % 10 == 9 or index == len(path) - 1:
                map_id, x, y = self.where()
                print(
                    f"    [{label}] {index + 1}/{len(path)} -> map=0x{map_id:02x} ({x},{y})",
                    flush=True,
                )

    def goto(self, goal: str, label: str, tries: int = 4) -> bool:
        """Arrive at ``goal`` or refuse: every attempt is verified."""
        want_x, want_y = (int(v) for v in goal.split(","))
        for attempt in range(tries):
            self.dismiss()
            self.idle(30)
            if self.where()[1:] == (want_x, want_y):
                return True
            path = self.pathfind(goal) or FALLBACK_PATHS.get(label)
            if path:
                self.walk(path, label)
            if self.where()[1:] == (want_x, want_y):
                print(f"    [{label}] reached {goal} on attempt {attempt + 1}", flush=True)
                return True
            map_id, x, y = self.where()
            print(
                f"    [{label}] drift: wanted {goal}, at ({x},{y}) on map 0x{map_id:02x}",
                flush=True,
            )
        return False

    def step_until_map(self, direction: str, wanted: int, budget: int = 14) -> bool:
        for _ in range(budget):
            before = self.where()
            self.step_dir(direction)
            if self.where() == before:
                self.dismiss()
                self.step_dir(direction)
            if self.where()[0] == wanted:
                return True
        return False


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


def leave_gym_and_reach_mart(driver: Driver) -> None:
    if not driver.goto("4,13", "gym-door"):
        raise CaptureRefused("could not reach the Pewter Gym exit")
    if not driver.step_until_map("d", PEWTER_CITY):
        raise CaptureRefused("leaving the gym did not return to Pewter City")
    reach_mart_from_city(driver)


def reach_mart_from_city(driver: Driver) -> None:
    """Walk to the Pewter Mart door from anywhere in Pewter City and warp in.

    Yellow's ``brock_badge`` and Blue's ``after_brock`` milestones start inside
    Pewter Gym, so they reach the mart through :func:`leave_gym_and_reach_mart`.
    Red's ``after_brock`` milestone starts in Pewter City itself, so it enters
    the mart directly and must not be driven at the gym-door waypoint.
    """
    if not driver.goto("23,19", "mart-door"):
        raise CaptureRefused("could not reach the Pewter Mart door")
    for _ in range(4):
        driver.step_dir("u")
        if driver.where()[0] == PEWTER_MART:
            return
    raise CaptureRefused("the Pewter Mart door did not warp indoors")


def buy_potion(driver: Driver) -> dict:
    """Buy one POTION; the settle signal is the money/bag delta."""
    for _ in range(12):
        if not any(row.strip("_") for row in driver.rows()):
            break
        driver.press("b", step=48)
    driver.idle(45)
    if not driver.goto(CLERK_STAND, "clerk"):
        raise CaptureRefused("could not stand at the mart counter")
    driver.press("left")
    for _ in range(4):
        driver.press("a", step=48)
    if driver.byte("wTextBoxID") != 13:
        raise CaptureRefused("the BUY/SELL/QUIT list never opened")
    list_row = {
        "textbox": driver.byte("wTextBoxID"),
        "max_item": driver.menu()[1],
        "watched_keys": driver.byte("wMenuWatchedKeys"),
    }
    money_before = driver.money()
    bag_before = driver.bag_stacks()
    driver.press("down")
    driver.press("a", step=48)
    for _ in range(8):
        driver.press("a", step=60)
        if driver.money() != money_before and any(i == POTION for i, _ in driver.bag_stacks()):
            break
    else:
        raise CaptureRefused("the purchase never settled")
    money_after = driver.money()
    bag_after = driver.bag_stacks()
    if money_after != money_before - POTION_PRICE:
        raise CaptureRefused(f"unexpected price: {money_before} -> {money_after}")
    if not any(i == POTION for i, _ in bag_after):
        raise CaptureRefused("no POTION stack after the purchase")
    for _ in range(12):
        if not any(row.strip("_") for row in driver.rows()):
            break
        driver.press("b", step=60)
    return {
        "shop_list": list_row,
        "money_before": money_before,
        "money_after": money_after,
        "bag_before": bag_before,
        "bag_after": bag_after,
        "price": money_before - money_after,
    }


def leave_mart(driver: Driver) -> None:
    if not driver.goto("3,7", "mart-exit"):
        raise CaptureRefused("could not reach the mart exit tile")
    for _ in range(12):
        if driver.where()[0] != PEWTER_MART:
            return
        driver.dismiss()
        driver.press("down", step=48)
    raise CaptureRefused("the mart door did not warp back to Pewter City")


def walk_into_grass(driver: Driver, battle_budget: int = 160) -> dict:
    """Step into grass until the ROM's encounter check fires.

    Movement alternates toward the nearest grass tile and every step is recorded
    with the ROM's own predicate: the game's own half-block tile (Yellow reads
    ``hlcoord 8, 9``, Red/Blue read ``hlcoord 9, 9``) equals ``wGrassTile``.
    The same cell is used as the movement aim point, so the walked path is the
    one the recorded predicate describes.
    """
    check_row = driver.encounter_row
    check_col = driver.encounter_col
    steps_on_grass = 0
    total = 0
    for _ in range(battle_budget):
        driver.dismiss()
        tile, cells = driver.grass_tiles()
        on_grass = (check_row, check_col) in cells
        steps_on_grass += int(on_grass)
        total += 1
        if driver.in_battle():
            return {
                "steps": total,
                "steps_on_grass": steps_on_grass,
                "grass_tile": tile,
                "grass_rate": driver.byte("wGrassRate"),
                "map_id": driver.where()[0],
                "encounter_cell": [check_row, check_col],
            }
        if not cells:
            before = driver.where()
            for direction in ("l", "d", "r", "u"):
                driver.step_dir(direction)
                if driver.where() != before:
                    break
            continue

        def key(cell: tuple[int, int]) -> tuple[int, int]:
            row, col = cell
            aligned = 0 if (row - check_row) % 2 == 0 and (col - check_col) % 2 == 0 else 1
            return (aligned, abs(row - check_row) + abs(col - check_col))

        row, col = min(cells, key=key)
        vertical = ["d" if row > check_row else "u"]
        horizontal = ["r" if col > check_col else "l"]
        order = (
            (vertical + horizontal)
            if abs(row - check_row) >= abs(col - check_col)
            else (horizontal + vertical)
        )
        before = driver.where()
        for direction in order + ["l", "r", "u", "d"]:
            driver.step_dir(direction)
            if driver.where() != before:
                break
    raise CaptureRefused("no wild encounter during the grass walk")


def wait_for_command_menu(driver: Driver, budget: int = 14) -> bool:
    for _ in range(budget):
        driver.idle(40)
        if driver.battle_menu_up():
            return True
        driver.press("a", step=60)
    return False


def fight_move(driver: Driver, index: int) -> None:
    """With the command menu open on FIGHT, execute the move at ``index``."""
    driver.press("a", step=48)  # open the FIGHT move list (cursor starts at 0)
    for _ in range(index):
        driver.press("down", step=30)
    driver.press("a", step=60)  # execute the chosen move


def burn_turns_until_damaged(driver: Driver, *, burn_move: int, budget: int = 6) -> dict:
    """Take damage from the current wild battle, and report how.

    Yellow's ``brock_badge`` milestone is already damaged, so the caller does not
    reach here for it.  The retained Red and Blue ``after_brock`` milestones are
    at full HP, so a battle captured at the first command menu would leave a
    Potion with nothing to heal.  Each burn executes ``burn_move`` - which the
    caller picks to be non-damaging, so the wild mon survives long enough to take
    its own turn - and then waits for the command menu to return and reads the
    ROM's reported HP.  The turn is only accepted once the ROM itself shows a
    lower HP.  This battle is *throwaway*: the caller leaves it and re-encounters
    so the saved fixture is a fresh battle whose opponent has not yet chosen a
    move, which is what the acceptance driver's ordering observation requires.
    """
    active = driver.state().party.active_mon
    if active is None:
        raise CaptureRefused("the active mon is unavailable")
    if active.hp < active.max_hp:
        return {"burns": 0, "hp": [active.hp, active.max_hp], "already_damaged": True}
    for burn in range(1, budget + 1):
        fight_move(driver, burn_move)
        if not wait_for_command_menu(driver):
            raise CaptureRefused("the wild battle ended before the active mon was damaged")
        mon = driver.state().party.active_mon
        if mon is not None and mon.hp < mon.max_hp:
            return {"burns": burn, "hp": [mon.hp, mon.max_hp], "already_damaged": False}
    raise CaptureRefused("the active mon was never damaged by the wild battle")


def flee_battle(driver: Driver, budget: int = 8) -> None:
    """Leave the current wild battle through RUN, verified against ``wIsInBattle``.

    The command menu is a 2x2 template (FIGHT/ITEM above POKéMON/RUN), so RUN is
    reached with RIGHT then DOWN.  The driver is far above the Route 2 wild
    levels, so the escape is deterministic; every step is still observed rather
    than assumed.
    """
    for _ in range(budget):
        if not driver.in_battle():
            return
        driver.press("right", step=30)
        driver.press("down", step=30)
        driver.press("a", step=60)
        for _ in range(6):
            if not driver.in_battle():
                return
            driver.press("a", step=45)
    raise CaptureRefused("the wild battle could not be left through RUN")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, help="pinned milestone state to drive")
    parser.add_argument("--source-sha1", required=True, help="expected SHA-1 of --source")
    parser.add_argument("--rom", required=True)
    parser.add_argument("--sym", required=True)
    parser.add_argument("--out", required=True, help="fixture path; must not exist")
    parser.add_argument("--provenance-out", default=None)
    parser.add_argument(
        "--game",
        default="yellow",
        choices=sorted(GAMES),
        help="which game's encounter cell and entry route to drive (default yellow)",
    )
    parser.add_argument(
        "--burn-move",
        type=int,
        default=None,
        help=(
            "zero-based FIGHT move index used to burn a turn so the wild mon can "
            "damage a full-HP active mon before the capture; omit to require the "
            "milestone to already be damaged"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = Path(args.source)
    rom = Path(args.rom)
    sym = Path(args.sym)
    out = Path(args.out)
    provenance_out = (
        Path(args.provenance_out) if args.provenance_out else out.with_suffix(".provenance.json")
    )

    if out.exists():
        raise CaptureRefused(f"refusing to overwrite an existing fixture: {out}")
    if not source.exists():
        raise CaptureRefused(f"source milestone is missing: {source}")
    observed_source_sha1 = sha1_of(source)
    if observed_source_sha1.lower() != args.source_sha1.lower():
        raise CaptureRefused(f"source SHA-1 mismatch: {observed_source_sha1} != {args.source_sha1}")
    rom_sha1, sym_sha1 = pinned(rom, sym)
    game = GAMES[args.game]

    session = Session.from_files(
        rom,
        sym,
        expected_rom_sha1=rom_sha1,
        expected_symbol_sha1=sym_sha1,
    )
    signals: dict = {}
    try:
        session.load_state(source.read_bytes())
        session.step(60, render=True)
        driver = Driver(session, rom=rom, sym=sym, rom_sha1=rom_sha1, game=game)
        print(
            f"  source {source.name} sha1={observed_source_sha1} "
            f"map=0x{driver.where()[0]:02x} {driver.where()[1:]} money={driver.money()} "
            f"bag={driver.bag_stacks()} hp={driver.hp()}",
            flush=True,
        )

        if game.entry == "gym":
            leave_gym_and_reach_mart(driver)
        else:
            reach_mart_from_city(driver)
        signals["purchase"] = buy_potion(driver)
        leave_mart(driver)
        print(f"  purchased: {signals['purchase']}", flush=True)

        if not driver.goto("19,34", "south-edge"):
            raise CaptureRefused("could not reach Pewter's south edge")
        if not driver.step_until_map("d", ROUTE_2):
            raise CaptureRefused("the south warp did not reach Route 2")
        signals["route2_entry"] = {
            "map_id": driver.where()[0],
            "x": driver.where()[1],
            "y": driver.where()[2],
        }
        print(f"  route 2 at {driver.where()} bag={driver.bag_stacks()}", flush=True)

        def capture_battle() -> tuple[dict, dict]:
            encounter = walk_into_grass(driver)
            print(f"  encounter: {encounter}", flush=True)
            if not wait_for_command_menu(driver):
                raise CaptureRefused("the battle command menu never appeared")
            battle = driver.state().battle
            observed = {
                "kind": getattr(battle.kind, "name", None),
                "raw_is_in_battle": battle.raw_is_in_battle,
                "max_item": driver.menu()[1],
                "watched_keys": driver.byte("wMenuWatchedKeys"),
                "hp": driver.hp(),
                "bag": driver.bag_stacks(),
                "money": driver.money(),
            }
            print(f"  battle: {observed}", flush=True)
            if not battle.is_wild_battle:
                raise CaptureRefused("the captured battle is not an ordinary wild battle")
            if not any(i == POTION for i, _ in driver.bag_stacks()):
                raise CaptureRefused("the captured battle has no POTION in the bag")
            return encounter, observed

        first_encounter, first_battle = capture_battle()

        active = driver.state().party.active_mon
        if active is not None and active.hp < active.max_hp:
            signals["encounter"], signals["battle"] = first_encounter, first_battle
            signals["damage"] = {
                "burns": 0,
                "hp": [active.hp, active.max_hp],
                "already_damaged": True,
            }
        else:
            if args.burn_move is None:
                raise CaptureRefused("the milestone is at full HP and no --burn-move was given")
            signals["damage"] = burn_turns_until_damaged(driver, burn_move=args.burn_move)
            print(f"  damage: {signals['damage']}", flush=True)
            signals["throwaway_battle"] = {"encounter": first_encounter, "battle": first_battle}
            flee_battle(driver)
            print("  left the throwaway battle; re-encountering on the grass", flush=True)
            signals["encounter"], signals["battle"] = capture_battle()
        print(f"  damage: {signals['damage']}", flush=True)

        selected = driver.byte("wEnemySelectedMove")
        signals["enemy_selected_move"] = selected
        if selected != 0:
            raise CaptureRefused(
                "the saved battle already has the opponent's move queued: "
                f"wEnemySelectedMove={selected}"
            )

        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(session.save_state())
    finally:
        session.close(save=False)

    provenance = {
        "$schema": "pokered-harness.battle-healing-fixture-provenance",
        "game": args.game,
        "fixture": {
            "path": out.name,
            "size_bytes": out.stat().st_size,
            "sha1": sha1_of(out),
            "sha256": sha256_of(out),
        },
        "source": {"path": f"external {source.name}", "sha1": observed_source_sha1},
        "expected_rom": {"path": str(rom), "sha1": rom_sha1},
        "expected_symbols": {"path": str(sym), "sha1": sym_sha1},
        "producer": "scripts/produce_battle_healing_fixture.py",
        "runtime_identity": {
            "python": sys.version.split()[0],
            "pyboy_version": getattr(__import__("pyboy"), "__version__", "unknown"),
        },
        "observed_signals": signals,
        "reproducibility": (
            "the encounter is RNG-driven: the captured bytes are pinned by hash and "
            "verified by the test; re-running the producer from the same milestone "
            "and the same driver code reproduces them, but a changed input sequence "
            "yields a different (equally valid) fixture"
        ),
    }
    provenance_out.parent.mkdir(parents=True, exist_ok=True)
    provenance_out.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n")
    print(f"  wrote {out} sha1={provenance['fixture']['sha1']}", flush=True)
    print(f"  wrote {provenance_out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
