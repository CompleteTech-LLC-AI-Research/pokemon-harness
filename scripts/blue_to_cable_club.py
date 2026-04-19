"""Blue-only: resume from after_brock.state and drive Pewter City → Route 3
→ Mt. Moon → Route 4 → Cerulean City → Pokémon Center → Cable Club (2F),
producing ``tests/fixtures/link/blue/cable_club.state`` for the link-cable
trade integration test.

STATUS (2026-04-19): **not yet producing a valid Cable Club state.**
The scaffold runs end-to-end without crashing but the leg navigators are
buggy — after Option-B boost and gym exit the player sits at Pewter
City (17, 20) and ``bump_walk(right, down)`` doesn't find a path through
Pewter's east edge to the Route 3 gate house. The Mt. Moon legs are
untested guesses. Each leg needs to be developed and verified
interactively; the full Pewter → Cable Club navigation is a
multi-session task comparable to ``run_to_brock.py`` (700 lines).

**Root-cause note from the first attempt**: the state file
``walkthrough_blue/milestones/after_brock.state`` was produced against
the color-patched ``pokemon-blue-color.gb`` ROM, not stock
``pokemon-blue.gb``. Loading it into the stock ROM succeeds silently but
the very next ``tick()`` corrupts WRAM into a repeating 0x00/0x39
pattern. The default ROM path/SHA in ``main()`` is therefore pinned to
the color-patched Blue.

Mirrors the idiom of ``blue_forest_to_brock.py``:

* Loads the upstream milestone (``walkthrough_blue/milestones/after_brock.state``).
* Applies an Option-B RAM boost to party slot 0 so Route 3 / Mt. Moon
  trainer battles and wild encounters resolve via pure A-mash + first-move
  one-shots — we are NOT validating battle AI here, we are validating the
  overworld-navigation → cable-club-attendant pipeline.
* Chains per-leg milestones under ``walkthrough_blue/milestones/`` and
  skips legs whose milestone already exists, so reruns are cheap.
* Final milestone gets copied into ``tests/fixtures/link/blue/`` as the
  tracked fixture for ``test_link_trade_roundtrip[blue-blue]``.

Map-id note
-----------

The Blue ``.sym`` file exposes map-*object* labels (``CeruleanPokecenter2F``)
but not the numeric map-id constants from pret's ``constants/map_constants.asm``.
We log ``wCurMap`` at every leg boundary so the observed ids become ground
truth; the values baked into :data:`MAP_IDS` are the ones empirically seen
on Blue in prior runs plus the pret constants file (referenced in the
plan).  When the script breaks because a map-id guess was wrong, the
printout at the top of the failing leg tells you what the real id is.

Run (from the worktree root)::

    PYTHONPATH=src \\
        POKERED_ROM_PATH=C:/Users/timot/Documents/projects/pokemon/rom/blue/pokemon-blue.gb \\
        POKERED_SYM_PATH=C:/Users/timot/Documents/projects/pokemon/rom/blue/pokemon-blue.sym \\
        POKERED_ROM_SHA1=e1deed63080bc24cad5fba18ecb3184f905d16d4 \\
        python -u scripts/blue_to_cable_club.py
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from pokered_harness.session import Session
from pokered_harness.mcp_server import register_default_hooks
from pokered_harness.state.party import (
    _OFFSET_HP,
    _OFFSET_LEVEL,
    _OFFSET_MAX_HP,
    _OFFSET_MOVES,
    _OFFSET_PP,
)

import run_to_brock as rtb
from bfs_route import clear_battle


# --- Map id table --------------------------------------------------------

# These are the pret/pokered numeric map-constants (see
# ``constants/map_constants.asm``). Blue uses the same layout as Red.
MAP_IDS = {
    "PEWTER_CITY": 0x02,
    "CERULEAN_CITY": 0x03,
    "ROUTE_3": 0x0E,
    "ROUTE_4": 0x0F,
    "MT_MOON_1F": 0x3B,
    "MT_MOON_B1F": 0x3C,
    "MT_MOON_B2F": 0x3D,
    # 0x3A is Mt. Moon Pokémon Center on Route 4; keep in mind when we
    # stumble into it during exit.
    "MT_MOON_POKECENTER": 0x3A,
    "CERULEAN_POKECENTER": 0x40,
    "TRADE_CENTER": 0xEF,  # Cable Club battle side — CLUB is the trade side
    "COLOSSEUM": 0xF0,
    # Cable Club maps are warp-driven from the PC 2F stairs. Blue/Red
    # Cable Club "2F" is actually a special map; the attendant sits at
    # (0, 4) / (4, 4) depending on side.
}


# --- Option-B boost ------------------------------------------------------


def boost_party_lead(session: Session) -> None:
    """Lift party slot 0 to L50/255-ATK with Tackle + Vine Whip so every
    post-Brock trainer / wild on Route 3 and Mt. Moon one-shots on the
    first FIGHT → first-move A-press, with surplus HP so we don't need
    the heal loop.

    This is the exact recipe from ``blue_forest_to_brock.boost_bulbasaur``
    (see that docstring for the reasoning). We re-apply it post-Brock
    because the post-gym state may have consumed PP / HP during the
    Jr. Trainer + Brock fight.
    """
    mem = session._pyboy.memory
    base = session.symbols.addr_of("wPartyMons")

    def put_be16(off: int, val: int) -> None:
        mem[base + off] = (val >> 8) & 0xFF
        mem[base + off + 1] = val & 0xFF

    mem[base + _OFFSET_LEVEL] = 50
    put_be16(_OFFSET_HP, 200)
    put_be16(_OFFSET_MAX_HP, 200)
    put_be16(36, 255)   # Attack
    put_be16(38, 255)   # Defense
    put_be16(40, 120)   # Speed
    put_be16(42, 255)   # Special

    # Experience — 150000 covers L50 medium-slow with headroom.
    xp = 150_000
    mem[base + 14] = (xp >> 16) & 0xFF
    mem[base + 15] = (xp >> 8) & 0xFF
    mem[base + 16] = xp & 0xFF

    # Tackle, Growl, Leech Seed, Vine Whip.
    for i, m in enumerate((33, 45, 73, 22)):
        mem[base + _OFFSET_MOVES + i] = m
    for i, pp in enumerate((35, 40, 10, 10)):
        mem[base + _OFFSET_PP + i] = pp

    gs = session.read_game_state()
    m = gs.party.mons[0]
    print(f"  boosted: L{m.level} HP{m.hp}/{m.max_hp} moves={m.moves}", flush=True)


# --- Overworld helpers ---------------------------------------------------


def settle(session: Session, ticks: int = 60) -> None:
    session.step(ticks, render=True)


def mash_a(drv: "rtb.Driver", n: int) -> None:
    """Press A ``n`` times, resolving any battle that fires mid-sequence."""
    for _ in range(n):
        if drv.gs().battle.active:
            drv.resolve_battle(max_turns=40)
            continue
        drv.press("a")


def unstick_from_brock_dialog(drv: "rtb.Driver", max_a: int = 80) -> None:
    """``after_brock.state`` may have been captured mid-badge-award dialog
    (screen-flash / "You got the Boulder Badge!" box / joy-locked). Mash
    A until we're back on the overworld of Pewter Gym or Pewter City and
    joy is unlocked."""
    for i in range(max_a):
        gs = drv.gs()
        if not drv.joy_locked() and not gs.battle.active and gs.overworld.map_id in (
            MAP_IDS["PEWTER_CITY"], 0x36  # PEWTER_GYM
        ):
            return
        drv.press("a")
    print(f"  WARN: still stuck after {max_a} A-presses — map="
          f"0x{drv.gs().overworld.map_id:02x} joy_locked={drv.joy_locked()}",
          flush=True)


def walk_until_map(drv: "rtb.Driver", directions: list[str], *,
                   stop_map_ids: tuple[int, ...] = (),
                   max_battles: int = 20) -> None:
    """Press each direction in ``directions``, short-circuiting on a map
    transition into any of ``stop_map_ids`` and resolving battles as they
    fire. Used for hardcoded-waypoint segments."""
    battles = 0
    for d in directions:
        gs = drv.gs()
        if gs.overworld.map_id in stop_map_ids:
            return
        if gs.battle.active:
            drv.resolve_battle(max_turns=40)
            battles += 1
            if battles > max_battles:
                print("  WARN: too many battles, aborting leg", flush=True)
                return
            continue
        if drv.joy_locked():
            for _ in range(5):
                drv.press("a")
                if not drv.joy_locked():
                    break
            continue
        drv.press(d)


def bump_walk(drv: "rtb.Driver", primary: str, alt: str,
              max_steps: int = 100,
              stop_map_ids: tuple[int, ...] = ()) -> None:
    """Walk in ``primary`` direction; when blocked, nudge in ``alt`` then
    retry. Cheap way to get across Route 3 / Route 4 since they are mostly
    straight lines with the occasional tree or trainer to detour around."""
    for _ in range(max_steps):
        gs = drv.gs()
        if gs.overworld.map_id in stop_map_ids:
            return
        if gs.battle.active:
            drv.resolve_battle(max_turns=40)
            continue
        if drv.joy_locked():
            drv.press("a")
            continue
        before = (gs.overworld.x, gs.overworld.y)
        drv.press(primary)
        if (drv.gs().overworld.x, drv.gs().overworld.y) == before:
            drv.press(alt)
            drv.press(primary)


# --- Milestone skeleton --------------------------------------------------


MILESTONE_ORDER = [
    "route3_entry",
    "route3_clear",
    "mt_moon_1f",
    "mt_moon_b1f",
    "mt_moon_exit",
    "cerulean_entry",
    "cerulean_pc",
    "cable_club",
]


def log_state(label: str, session: Session) -> None:
    gs = session.read_game_state()
    m = gs.party.mons[0] if gs.party.mons else None
    print(
        f"[{label}] map=0x{gs.overworld.map_id:02x} "
        f"xy=({gs.overworld.x},{gs.overworld.y}) "
        f"badges=0x{gs.progress.badges_raw:02x} "
        f"battle={gs.battle.active} "
        f"lead={'L%d HP%d/%d' % (m.level, m.hp, m.max_hp) if m else 'none'}",
        flush=True,
    )


# --- Legs ----------------------------------------------------------------


def leg_route3_entry(drv: "rtb.Driver") -> None:
    """Pewter City east exit → Route 3 (map 0x0e).

    Pewter City gym warp lands us near (16, 17). Post-Brock the player is
    inside the gym; we need to EXIT the gym first (DOWN from Brock takes
    us to the gym-door tile (4, 7) → warp to Pewter at (16, 17)), then
    walk east to the Route 3 edge at roughly (33, 18) and through the
    east gate.
    """
    # Exit gym if still inside.
    for _ in range(40):
        gs = drv.gs()
        if gs.overworld.map_id == MAP_IDS["PEWTER_CITY"]:
            break
        if gs.battle.active:
            drv.resolve_battle(max_turns=40)
            continue
        if drv.joy_locked():
            drv.press("a")
            continue
        drv.press("down")

    # Walk east along Pewter's main street. Pewter is ~31 tiles wide; the
    # east edge / Route 3 warp is around x=33 (off-screen edge of the
    # map), so we bump-walk east and let the ledge drop us onto Route 3.
    bump_walk(drv, "right", "down", max_steps=80,
              stop_map_ids=(MAP_IDS["ROUTE_3"],))


def leg_route3_clear(drv: "rtb.Driver") -> None:
    """Route 3 east to Mt. Moon entrance.

    Route 3 is 70 tiles east-west, mostly straight. Wild Pidgey/Spearow
    might fire; boosted lead one-shots them. Trainers have sight lines
    but the boosted lead also one-shots trainer mons. Bump-walk east
    until map transitions to Mt. Moon 1F (0x3b).
    """
    bump_walk(drv, "right", "up", max_steps=200,
              stop_map_ids=(MAP_IDS["MT_MOON_1F"],
                            MAP_IDS["MT_MOON_POKECENTER"]))


def leg_mt_moon_1f(drv: "rtb.Driver") -> None:
    """Mt. Moon 1F → ladder to B1F.

    Mt. Moon 1F ladder down to B1F is at roughly (5, 5) per pret
    ``data/maps/objects/MtMoon1F.asm``. Entry warp lands at around
    (5, 15). Walk UP+LEFT to reach it.

    There are trainer sight-lines; boosted lead resolves them via
    ``drv.resolve_battle``.
    """
    settle(drv.s, 60)
    # Waypoint sketch — UP a lot, LEFT to dodge trainers, more UP.
    path = (["up"] * 6 + ["left"] * 3 + ["up"] * 6)
    walk_until_map(drv, path, stop_map_ids=(MAP_IDS["MT_MOON_B1F"],))
    # If still on 1F, brute-force towards (5, 5).
    for _ in range(80):
        gs = drv.gs()
        if gs.overworld.map_id == MAP_IDS["MT_MOON_B1F"]:
            return
        if gs.battle.active:
            drv.resolve_battle(max_turns=40)
            continue
        if drv.joy_locked():
            drv.press("a")
            continue
        tx, ty = 5, 5
        if gs.overworld.y > ty:
            drv.press("up")
        elif gs.overworld.x > tx:
            drv.press("left")
        elif gs.overworld.x < tx:
            drv.press("right")
        else:
            drv.press("a")  # step onto ladder


def leg_mt_moon_b1f(drv: "rtb.Driver") -> None:
    """Mt. Moon B1F → ladder to Route 4 exit.

    The Mt. Moon B1F exit ladder (back up to 1F near the Route 4 side) is
    roughly at (25, 15) per pret ``data/maps/objects/MtMoonB1F.asm``.
    Then one more ladder up to 1F east side → warp down to Route 4 west
    edge.

    This is the trickiest leg. We use a rough waypoint chain; if it
    deadlocks, the script prints the current coord so you can patch in
    the real waypoints from the .asm.
    """
    settle(drv.s, 60)
    path = (["down"] * 5 + ["right"] * 10 + ["down"] * 5 + ["right"] * 5)
    walk_until_map(drv, path,
                   stop_map_ids=(MAP_IDS["MT_MOON_1F"], MAP_IDS["ROUTE_4"]))
    # Brute-force towards (25, 15) on B1F.
    for _ in range(120):
        gs = drv.gs()
        if gs.overworld.map_id != MAP_IDS["MT_MOON_B1F"]:
            return
        if gs.battle.active:
            drv.resolve_battle(max_turns=40)
            continue
        if drv.joy_locked():
            drv.press("a")
            continue
        tx, ty = 25, 15
        if gs.overworld.x < tx:
            drv.press("right")
        elif gs.overworld.y < ty:
            drv.press("down")
        elif gs.overworld.y > ty:
            drv.press("up")
        else:
            drv.press("a")


def leg_mt_moon_exit(drv: "rtb.Driver") -> None:
    """Back onto Mt. Moon 1F (east side) → ladder or doorway to Route 4.

    On some runs this leg is skipped because B1F ladder goes straight to
    Route 4. We just keep bump-walking toward east/down until map id
    matches Route 4 (0x0f) or Cerulean (0x03).
    """
    for _ in range(200):
        gs = drv.gs()
        if gs.overworld.map_id == MAP_IDS["ROUTE_4"]:
            return
        if gs.battle.active:
            drv.resolve_battle(max_turns=40)
            continue
        if drv.joy_locked():
            drv.press("a")
            continue
        # Inside Mt. Moon: bias east + down.
        before = (gs.overworld.x, gs.overworld.y)
        drv.press("down")
        if (drv.gs().overworld.x, drv.gs().overworld.y) == before:
            drv.press("right")


def leg_cerulean_entry(drv: "rtb.Driver") -> None:
    """Route 4 east → Cerulean City (map 0x03)."""
    bump_walk(drv, "right", "up", max_steps=80,
              stop_map_ids=(MAP_IDS["CERULEAN_CITY"],))


def leg_cerulean_pc(drv: "rtb.Driver") -> None:
    """Cerulean City → Pokémon Center interior (map 0x40).

    Cerulean PC warp is in the southwest corner. From the Route 4 entry
    (east side) we walk west then south to the PC door.
    """
    settle(drv.s, 60)
    bump_walk(drv, "left", "down", max_steps=40,
              stop_map_ids=(MAP_IDS["CERULEAN_POKECENTER"],))
    # If we missed, try south then west.
    for _ in range(60):
        gs = drv.gs()
        if gs.overworld.map_id == MAP_IDS["CERULEAN_POKECENTER"]:
            return
        if gs.battle.active:
            drv.resolve_battle(max_turns=40)
            continue
        if drv.joy_locked():
            drv.press("a")
            continue
        before = (gs.overworld.x, gs.overworld.y)
        drv.press("down")
        if (drv.gs().overworld.x, drv.gs().overworld.y) == before:
            drv.press("left")


def leg_cable_club(drv: "rtb.Driver") -> None:
    """Inside Cerulean PC → Cable Club 2F attendant.

    Stairs to 2F are on the east side of the ground floor. Walk up, go
    upstairs, then approach the trade attendant (usually around (4, 2)).
    Once standing in front of the attendant, we're done — do NOT press A,
    because the trade-UI walker is a follow-up task.
    """
    settle(drv.s, 60)
    # Walk up then right to reach the stairs.
    for _ in range(10):
        drv.press("up")
    for _ in range(6):
        drv.press("right")
    # The stairs warp is interactive — stand on the tile and ascend.
    # Cable Club map id differs by Blue vs Red. We treat any map
    # transition out of CERULEAN_POKECENTER as success.
    prev_map = drv.gs().overworld.map_id
    for _ in range(30):
        gs = drv.gs()
        if gs.overworld.map_id != prev_map and \
                gs.overworld.map_id != MAP_IDS["CERULEAN_POKECENTER"]:
            break
        drv.press("up")
    settle(drv.s, 60)
    # Approach the trade attendant (top-left desk). Walk UP+LEFT a few
    # tiles to face the NPC; do NOT press A, the fixture state is "just
    # before talking".
    for _ in range(4):
        drv.press("up")
    for _ in range(3):
        drv.press("left")
    for _ in range(2):
        drv.press("up")


LEG_FNS = {
    "route3_entry":   leg_route3_entry,
    "route3_clear":   leg_route3_clear,
    "mt_moon_1f":     leg_mt_moon_1f,
    "mt_moon_b1f":    leg_mt_moon_b1f,
    "mt_moon_exit":   leg_mt_moon_exit,
    "cerulean_entry": leg_cerulean_entry,
    "cerulean_pc":    leg_cerulean_pc,
    "cable_club":     leg_cable_club,
}


# --- Main ----------------------------------------------------------------


def main() -> int:
    # IMPORTANT: `after_brock.state` was produced against the color-patched
    # Blue ROM (pokeblue_color_vanilla.ips applied). Loading it into stock
    # Blue appears to succeed but the very next tick() corrupts WRAM into
    # the repeating 0x00/0x39 pattern — root-caused 2026-04-19. Default to
    # the patched ROM so reruns work out of the box.
    rom = os.environ.get(
        "POKERED_ROM_PATH",
        "C:/Users/timot/Documents/projects/pokemon/rom/blue/pokemon-blue-color.gb",
    )
    sym = os.environ.get(
        "POKERED_SYM_PATH",
        "C:/Users/timot/Documents/projects/pokemon/rom/blue/pokemon-blue.sym",
    )
    sha1 = os.environ.get(
        "POKERED_ROM_SHA1", "5f4b05725a860e04077045462176d3e2771c5022"
    )

    # Milestones live in the main repo under walkthrough_blue/milestones/
    # (gitignored there). The worktree only persists the final fixture.
    main_repo = Path("C:/Users/timot/Documents/projects/pokemon")
    milestones = main_repo / "walkthrough_blue" / "milestones"
    milestones.mkdir(parents=True, exist_ok=True)
    fixture_dir = Path("tests/fixtures/link/blue")
    fixture_dir.mkdir(parents=True, exist_ok=True)

    session = Session.from_files(rom, sym, expected_rom_sha1=sha1)
    register_default_hooks(session)

    # Find the latest existing milestone and resume from it. If none,
    # start from after_brock.state.
    resume_from: Path | None = None
    for name in reversed(MILESTONE_ORDER):
        p = milestones / f"{name}.state"
        if p.exists():
            resume_from = p
            print(f"resuming from existing milestone: {name}", flush=True)
            break
    if resume_from is None:
        resume_from = milestones / "after_brock.state"
        if not resume_from.exists():
            raise SystemExit(f"missing starting state {resume_from}")
        print("starting from after_brock.state", flush=True)

    session.load_state(resume_from.read_bytes())
    settle(session, 60)
    clear_battle(session)  # in case state was captured mid-battle
    log_state("loaded", session)

    # Only the very first run (coming from after_brock) needs the badge
    # dialog unstick + Option-B boost. If we're resuming from a later
    # milestone, skip those.
    drv = rtb.Driver(session)
    if resume_from.name == "after_brock.state":
        print("\n=== unstick brock-badge dialog ===", flush=True)
        unstick_from_brock_dialog(drv)
        log_state("unstuck", session)
        print("\n=== Option-B boost party lead ===", flush=True)
        boost_party_lead(session)

    # Find which leg to start at (first milestone not yet produced).
    start_idx = 0
    for i, name in enumerate(MILESTONE_ORDER):
        if (milestones / f"{name}.state").exists():
            start_idx = i + 1

    for name in MILESTONE_ORDER[start_idx:]:
        print(f"\n=== leg: {name} ===", flush=True)
        LEG_FNS[name](drv)
        session.step(60, render=True)
        log_state(name, session)
        out = milestones / f"{name}.state"
        out.write_bytes(session.save_state())
        print(f"  saved {out}", flush=True)

    # Copy the final milestone into the tracked fixture path.
    final = milestones / "cable_club.state"
    if final.exists():
        fixture = fixture_dir / "cable_club.state"
        shutil.copyfile(final, fixture)
        print(f"\ncopied {final} -> {fixture}", flush=True)
        gs = session.read_game_state()
        print(f"FINAL: map=0x{gs.overworld.map_id:02x} "
              f"xy=({gs.overworld.x},{gs.overworld.y})", flush=True)
    else:
        print("\nNO cable_club.state produced — script halted early",
              flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
