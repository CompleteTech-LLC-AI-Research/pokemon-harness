"""Yellow post-Brock: Pewter Gym → Cerulean PokéCenter.

Resumes from a Yellow Boulder Badge state (e.g.
``walkthrough_yellow_honest15/milestones/brock_badge.state``) and
walks the canonical post-Brock route:

    Pewter Gym → Pewter PC heal → Route 3 → Mt. Moon (1F → B1F → B2F →
    1F east exit) → Route 4 → Cerulean City → Cerulean PC

Each leg saves a milestone state under ``--outdir/milestones/`` so
later iterations can resume mid-pipeline. Uses ``path_from_tiles.py``
A* for in-map navigation; map-warp transitions are handled by stepping
the relevant direction at the warp tile until the map id changes.

Usage::

    PYTHONIOENCODING=utf-8 \\
    POKERED_ROM_PATH=rom/yellow/pokemon-yellow.gbc \\
    POKERED_SYM_PATH=rom/yellow/pokemon-yellow.sym \\
    POKERED_ROM_SHA1=cc7d03262ebfaf2f06772c1a480c7d9d5f4a38e1 \\
    python scripts/to_cerulean.py \\
        --start walkthrough_yellow_honest15/milestones/brock_badge.state \\
        --outdir walkthrough_yellow_to_cerulean
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pokered_harness.session import Session
from pokered_harness.mcp_server import register_default_hooks

import run_to_brock as rtb
import full_to_brock as ftb


# Map IDs (Yellow / Red / Blue all share these — pokered constants).
M_PEWTER_CITY = 0x02
M_CERULEAN_CITY = 0x03
M_ROUTE_3 = 0x0e
M_ROUTE_4 = 0x0f
M_PEWTER_GYM = 0x36
M_PEWTER_POKECENTER = 0x3A
M_CERULEAN_POKECENTER = 0x40
M_MT_MOON_1F = 0x3B
M_MT_MOON_B1F = 0x3C
M_MT_MOON_B2F = 0x3D


def _save(session: Session, outdir: Path, name: str) -> Path:
    p = outdir / "milestones" / f"{name}.state"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(session.save_state())
    print(f"  saved milestone: {name}", flush=True)
    return p


def _gs_summary(session: Session) -> str:
    gs = session.read_game_state()
    parts = [
        f"map=0x{gs.overworld.map_id:02x}",
        f"xy=({gs.overworld.x},{gs.overworld.y})",
        f"badges=0x{gs.progress.badges_raw:02x}",
    ]
    if gs.party.mons:
        m = gs.party.mons[0]
        parts.append(f"L{m.level} hp={m.hp}/{m.max_hp}")
    return " ".join(parts)


def _pathfind_walk(drv: rtb.Driver, session: Session, outdir: Path,
                   goal_xy: str, label: str,
                   rom: str, sym: str, sha1: str,
                   stop_map_ids=()) -> str:
    """Save state, run A* to ``goal_xy``, walk the path. Returns the
    walk_path result code (``done``/``stop``/``stalled``/etc.)."""
    seed = outdir / f"_{label}.state"
    seed.write_bytes(session.save_state())
    out_txt = outdir / f"_{label}.txt"
    try:
        path = ftb.run_pathfinder(seed, goal_xy, out_txt, rom, sym, sha1)
    except RuntimeError as e:
        print(f"  [{label}] pathfind failed: {e}", flush=True)
        return "pathfail"
    print(f"  [{label}] A* {len(path)} steps -> walking", flush=True)
    return ftb.walk_path(drv, path, label=label, stop_map_ids=stop_map_ids)


# --- Phase 1: exit gym + heal at Pewter PC --------------------------------

def exit_gym(drv: rtb.Driver, session: Session, outdir: Path,
             rom: str, sym: str, sha1: str) -> bool:
    """A* from current gym xy down to the south warp tile, then step
    onto it to trigger the warp out. Pewter Gym south warp is at
    (4, 13)/(5, 13); A* picks whichever is reachable."""
    res = _pathfind_walk(drv, session, outdir, "4,13", "exit_gym",
                         rom, sym, sha1, stop_map_ids=(M_PEWTER_CITY,))
    print(f"  exit_gym walk: {res}", flush=True)
    # Step DOWN through the warp if not already out
    for _ in range(6):
        if drv.gs().overworld.map_id == M_PEWTER_CITY:
            break
        drv.press("down")
    session.step(60, render=True)
    return drv.gs().overworld.map_id == M_PEWTER_CITY


def heal_at_pewter_pc(drv: rtb.Driver, session: Session, outdir: Path,
                      rom: str, sym: str, sha1: str) -> bool:
    """A* to Pewter PC entrance, talk to nurse, exit south."""
    # Pewter PC entry warp is around (13, 26) — door tile that warps to
    # PEWTER_POKECENTER. We path to the tile DIRECTLY in front of it
    # (13, 27) then step UP onto the warp.
    res = _pathfind_walk(drv, session, outdir, "13,27", "to_pewter_pc",
                         rom, sym, sha1)
    print(f"  to_pewter_pc result: {res}", flush=True)
    # Cross into PC
    for _ in range(6):
        if drv.gs().overworld.map_id == M_PEWTER_POKECENTER:
            break
        drv.press("up")
    session.step(60, render=True)
    if drv.gs().overworld.map_id != M_PEWTER_POKECENTER:
        print(f"  WARN failed entry to Pewter PC "
              f"(map=0x{drv.gs().overworld.map_id:02x})", flush=True)
        return False
    # Walk up to nurse and talk
    for _ in range(6):
        drv.press("up")
    drv.press("a")
    for _ in range(30):
        mx = drv.sym.read_u8(drv.mem, "wMaxMenuItem")
        if mx == 1 and not drv.gs().text.dest_in_vram_tilemap:
            break
        drv.press("a")
    drv.press("a", step=60)
    for _ in range(60):
        try:
            m = drv.gs().party.mons[0]
            if m.hp == m.max_hp:
                break
        except IndexError:
            pass
        drv.press("a")
    for _ in range(15):
        drv.press("b", step=60)
    # Exit south
    for _ in range(8):
        if drv.gs().overworld.map_id == M_PEWTER_CITY:
            break
        drv.press("down")
    session.step(60, render=True)
    return drv.gs().overworld.map_id == M_PEWTER_CITY


# --- Phase 2: cross Pewter east -> Route 3 --------------------------------

def walk_to_route3(drv: rtb.Driver, session: Session, outdir: Path,
                   rom: str, sym: str, sha1: str) -> bool:
    """A* east across Pewter to the Route 3 entry tile.

    Route 3 entry from Pewter is the east warp around (33, 19) -
    crossing it lands the player on Route 3 at (0, 4).
    """
    ftb._activate_repel(drv)
    # Pewter east-to-Route-3 warp is around (35, 19) — pathfind to the
    # tile JUST west of the warp then step RIGHT through.
    res = _pathfind_walk(drv, session, outdir, "35,19", "pewter_east",
                         rom, sym, sha1, stop_map_ids=(M_ROUTE_3,))
    print(f"  pewter_east result: {res}", flush=True)
    # Cross the east warp
    for _ in range(8):
        if drv.gs().overworld.map_id == M_ROUTE_3:
            break
        drv.press("right")
    session.step(60, render=True)
    return drv.gs().overworld.map_id == M_ROUTE_3


# --- Phase 3: cross Route 3 east -> Mt. Moon entry -----------------------

def _recover_to_route3(drv: rtb.Driver, session: Session, outdir: Path,
                       rom: str, sym: str, sha1: str) -> bool:
    """After a blackout that dumped us into Pewter (or its PC), walk
    south out of the PC if needed, settle the engine, then A* east
    back into Route 3."""
    # Settle longer than usual — blackout warp transitions leave the
    # tileset / WRAM in a transient state that breaks save_state
    # snapshots used by the pathfinder subprocess.
    session.step(180, render=True)
    # Brute-mash A: blackout-warp can land us face-to-face with the
    # Pewter PC bench-guy NPC, who fires multi-page dialog
    # ("Any POKEMON that takes part in battle, however short, earns
    #  EXP! ..."). Yellow doesn't surface this through
    # ``text.dest_in_vram_tilemap`` or ``joy_locked``, so the standard
    # "stop on no text" guard exits early. Detection: try a SOUTH step
    # after each A — if it moves us, dialog cleared; if not, more A.
    cleared = False
    for _ in range(80):
        gs = drv.gs()
        if gs.battle.active:
            drv.resolve_battle()
            continue
        before = (drv.gs().overworld.x, drv.gs().overworld.y)
        drv.press("a")
        # Probe: try to step DOWN. If movement happens, dialog is gone.
        drv.press("down")
        if (drv.gs().overworld.x, drv.gs().overworld.y) != before:
            cleared = True
            break
    if not cleared:
        print("  recover: dialog mash didn't free us; bailing", flush=True)
    session.step(60, render=True)
    gs = drv.gs()
    if gs.overworld.map_id == M_PEWTER_POKECENTER:
        for _ in range(8):
            if drv.gs().overworld.map_id == M_PEWTER_CITY:
                break
            drv.press("down")
        session.step(60, render=True)
    if drv.gs().overworld.map_id != M_PEWTER_CITY:
        print(f"  recover: not on Pewter (map=0x"
              f"{drv.gs().overworld.map_id:02x}); giving up", flush=True)
        return False
    # If blackout left us in front of a building entrance, take a
    # cardinal step (south usually opens up the city) to clear any
    # lingering screen-edge transition.
    for _ in range(4):
        before = (drv.gs().overworld.x, drv.gs().overworld.y)
        drv.press("up")
        if drv.gs().battle.active:
            drv.resolve_battle()
        if (drv.gs().overworld.x, drv.gs().overworld.y) != before:
            break
    session.step(60, render=True)
    ftb._activate_repel(drv)
    for retry in range(3):
        res = _pathfind_walk(drv, session, outdir, "35,19",
                             f"recover_east_r{retry}",
                             rom, sym, sha1, stop_map_ids=(M_ROUTE_3,))
        print(f"  recover_east_r{retry}: {res}", flush=True)
        if res in ("done", "stop"):
            break
        session.step(180, render=True)
    for _ in range(8):
        if drv.gs().overworld.map_id == M_ROUTE_3:
            break
        drv.press("right")
    session.step(60, render=True)
    return drv.gs().overworld.map_id == M_ROUTE_3


def _greedy_east(drv: rtb.Driver, session: Session,
                 target_x: int, target_map_id: int | None = None,
                 max_steps: int = 400) -> str:
    """Walk east greedily, handling obstacles by trying alternate
    directions. Returns ``"reached"`` if x>=target_x, ``"map"`` if
    target_map_id matched, ``"blackout"`` if we landed on Pewter,
    ``"stuck"`` if no progress for many tries.

    Key mechanic: when a trainer's sight-line catches us, the game
    sets wJoyIgnore and auto-walks the trainer toward our tile. This
    takes several seconds and our direction/A presses are ignored.
    We detect joy_locked and idle the emulator instead — letting the
    trainer-approach animation complete naturally. Once battle fires
    we resolve it via drv.resolve_battle (which delegates to the
    grind battle-turn logic with Double Kick + ThunderShock).
    """
    no_progress = 0
    last_x = drv.gs().overworld.x
    for _ in range(max_steps):
        gs = drv.gs()
        if gs.battle.active:
            drv.resolve_battle()
            continue
        if (gs.party.mons and gs.party.mons[0].hp == 0
                and gs.overworld.map_id != M_ROUTE_3):
            return "blackout"
        if gs.overworld.map_id in (M_PEWTER_CITY, M_PEWTER_POKECENTER):
            return "blackout"
        if target_map_id is not None and gs.overworld.map_id == target_map_id:
            return "map"
        if gs.overworld.x >= target_x and gs.overworld.map_id == M_ROUTE_3:
            return "reached"
        # Trainer-approach detection: wJoyIgnore locks all input while
        # the trainer's sprite walks toward us. Just idle the emulator
        # so that auto-walk completes and a battle eventually fires.
        if drv.joy_locked():
            session.step(120, render=True)
            continue
        # Try directions in priority: east, then alternate UD to dodge
        # sprite blockers, then push through dialog with A.
        before = (gs.overworld.x, gs.overworld.y)
        for d in ("right", "up", "right", "down", "right",
                  "down", "right", "up"):
            drv.press(d)
            if drv.gs().battle.active:
                drv.resolve_battle()
                break
            if drv.joy_locked():
                session.step(120, render=True)
                break
            after = (drv.gs().overworld.x, drv.gs().overworld.y)
            if after != before:
                break
        # If pressing didn't move us, mash A in case of dialog
        after = (drv.gs().overworld.x, drv.gs().overworld.y)
        if after == before and not drv.joy_locked():
            for _ in range(8):
                drv.press("a")
                if drv.gs().battle.active:
                    drv.resolve_battle()
                    break
                if (drv.gs().overworld.x, drv.gs().overworld.y) != before:
                    break
        # Track east progress
        cur_x = drv.gs().overworld.x
        if cur_x > last_x:
            no_progress = 0
            last_x = cur_x
        else:
            no_progress += 1
            if no_progress >= 25:
                return "stuck"
    return "stuck"


def cross_route3(drv: rtb.Driver, session: Session, outdir: Path,
                 rom: str, sym: str, sha1: str,
                 max_blackout_recoveries: int = 5) -> bool:
    """A* east across Route 3 to the Mt. Moon 1F entrance.

    Route 3 east edge warps to MT_MOON_1F (0x3B). Trainer sight-lines
    along the corridor block direct A* paths (defeated trainers stay
    as sprite blockers in the seed state until we actually fight them).
    Solution: iterative chunk pathfinding — A* to a sequence of
    waypoints east, walking each chunk and resolving any trainer
    battles that fire en route.

    Blackout recovery: Pikachu at L16 with Double Kick can take down
    individual trainers but multiple back-to-back fights drain HP.
    On blackout we land in Pewter PC fully healed; walk back through
    Pewter east into Route 3 and continue from where we left off (the
    defeated trainers' sprite blockers are gone, so each cycle covers
    new ground).
    """
    ftb._activate_repel(drv)
    blackouts = 0
    waypoints = [
        ("30,11", "r3_wp1"),
        ("60,11", "r3_wp2"),
        ("100,11", "r3_wp3"),
        ("139,4", "r3_wp4"),
    ]
    while drv.gs().overworld.map_id != M_MT_MOON_1F:
        cur_map = drv.gs().overworld.map_id
        if cur_map in (M_PEWTER_CITY, M_PEWTER_POKECENTER):
            if blackouts >= max_blackout_recoveries:
                print(f"  too many blackouts ({blackouts}); bailing",
                      flush=True)
                return False
            blackouts += 1
            print(f"  blackout #{blackouts} -> recovering to Route 3",
                  flush=True)
            if not _recover_to_route3(drv, session, outdir, rom, sym, sha1):
                return False
            ftb._activate_repel(drv)
            continue
        if cur_map != M_ROUTE_3:
            print(f"  unexpected map=0x{cur_map:02x}; bailing", flush=True)
            return False
        # Try A* waypoints first, then greedy_east as fallback.
        ftb._activate_repel(drv)
        start_x = drv.gs().overworld.x
        a_star_progressed = False
        for goal, label in waypoints:
            goal_x = int(goal.split(",")[0])
            if drv.gs().overworld.x >= goal_x:
                continue
            if drv.gs().overworld.map_id != M_ROUTE_3:
                break
            res = _pathfind_walk(drv, session, outdir, goal,
                                 f"{label}_b{blackouts}",
                                 rom, sym, sha1,
                                 stop_map_ids=(M_MT_MOON_1F,))
            print(f"  {label}_b{blackouts}: {res} -> "
                  f"{_gs_summary(session)}", flush=True)
            if res in ("done", "stop", "map"):
                a_star_progressed = True
                continue
            # pathfail or stalled: fall through to greedy_east
            break
        if drv.gs().overworld.map_id != M_ROUTE_3:
            continue
        # Greedy fallback from current position.
        res = _greedy_east(drv, session, target_x=139,
                           target_map_id=M_MT_MOON_1F, max_steps=400)
        print(f"  greedy east: {res} -> {_gs_summary(session)}",
              flush=True)
        if res == "blackout":
            continue  # outer loop handles recovery
        if res == "stuck":
            # Force a blackout to escape the sprite-trap pocket: turn
            # Repel off and bounce in-place until a wild fires; in the
            # battle, mash A and don't switch when Pikachu faints. The
            # blackout teleport drops us at Pewter PC fully healed and
            # the outer recovery loop walks us back into Route 3 from
            # (0, 11). Defeated trainers stay defeated across the
            # cycle, so each iteration covers new ground.
            print(f"  stuck at {_gs_summary(session)}; forcing blackout",
                  flush=True)
            try:
                base = drv.sym.addr_of("wRepelRemainingSteps")
                drv.mem[base] = 0
            except Exception:
                pass
            forced = False
            for _ in range(80):
                gs = drv.gs()
                if gs.overworld.map_id != M_ROUTE_3:
                    forced = True
                    break
                if gs.battle.active:
                    drv.resolve_battle()
                    if (gs.party.mons
                            and gs.party.mons[0].hp == 0):
                        for _ in range(120):
                            g = drv.gs()
                            if g.overworld.map_id != M_ROUTE_3:
                                forced = True
                                break
                            drv.press("a")
                        break
                    continue
                # Try directions to step in grass
                for d in ("up", "down", "right", "left"):
                    drv.press(d)
                    if drv.gs().battle.active:
                        break
            if not forced:
                print("  force-blackout failed; bailing", flush=True)
                return False
            continue
        # Greedy east-walker returned "reached" or "map" — fall through
        # to the warp-cross step below.
        if drv.gs().overworld.map_id == M_MT_MOON_1F:
            break
    # Cross the east warp
    for _ in range(8):
        if drv.gs().overworld.map_id == M_MT_MOON_1F:
            break
        drv.press("right")
    session.step(60, render=True)
    return drv.gs().overworld.map_id == M_MT_MOON_1F


# --- Main -----------------------------------------------------------------

PHASES = [
    "exit_gym",
    "pewter_pc",
    "route3_entry",
    "mt_moon_entry",
]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True,
                   help="Path to a .state to resume from (e.g. brock_badge.state)")
    p.add_argument("--outdir", default="walkthrough_yellow_to_cerulean")
    p.add_argument("--stop-after", default=PHASES[-1], choices=PHASES)
    args = p.parse_args()

    rom = os.environ["POKERED_ROM_PATH"]
    sym = os.environ["POKERED_SYM_PATH"]
    sha1 = os.environ.get("POKERED_ROM_SHA1")
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    session = Session.from_files(rom, sym, expected_rom_sha1=sha1)
    register_default_hooks(session)
    session.load_state(Path(args.start).read_bytes())
    session.step(60, render=True)
    drv = rtb.Driver(session)
    print(f"start: {_gs_summary(session)}", flush=True)

    print("\n=== phase: exit_gym ===", flush=True)
    if not exit_gym(drv, session, outdir, rom, sym, sha1):
        print(f"  FAIL exit_gym: {_gs_summary(session)}", flush=True)
        return 1
    print(f"  out: {_gs_summary(session)}", flush=True)
    _save(session, outdir, "exit_gym")
    if args.stop_after == "exit_gym":
        return 0

    print("\n=== phase: pewter_pc ===", flush=True)
    if not heal_at_pewter_pc(drv, session, outdir, rom, sym, sha1):
        print(f"  FAIL pewter_pc: {_gs_summary(session)}", flush=True)
        return 1
    print(f"  healed: {_gs_summary(session)}", flush=True)
    _save(session, outdir, "pewter_pc")
    if args.stop_after == "pewter_pc":
        return 0

    print("\n=== phase: route3_entry ===", flush=True)
    if not walk_to_route3(drv, session, outdir, rom, sym, sha1):
        print(f"  FAIL route3_entry: {_gs_summary(session)}", flush=True)
        return 1
    print(f"  on route3: {_gs_summary(session)}", flush=True)
    _save(session, outdir, "route3_entry")
    if args.stop_after == "route3_entry":
        return 0

    print("\n=== phase: mt_moon_entry ===", flush=True)
    if not cross_route3(drv, session, outdir, rom, sym, sha1):
        print(f"  FAIL mt_moon_entry: {_gs_summary(session)}", flush=True)
        return 1
    print(f"  in mt_moon: {_gs_summary(session)}", flush=True)
    _save(session, outdir, "mt_moon_entry")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
