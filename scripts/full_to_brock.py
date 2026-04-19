"""End-to-end orchestrator: boot → Boulder Badge on the colorized ROM.

Stitches the pieces built by the specialist agents:
  run_to_brock.main()  — boot → Route 1 → Viridian → Route 2 entry (verified)
  grind_to_level(13)   — wild-battle grind on Route 2 to learn Vine Whip
  heal_at_viridian_pokecenter — restore HP before forest
  path_from_tiles A*   — forest navigation, Pewter navigation
  brock_gym.run_pewter_to_brock_badge — gym + trainers + Brock

Each phase saves a milestone state so it can be resumed. Run:

    python -u scripts/full_to_brock.py --outdir walkthrough_badge
"""

from __future__ import annotations

import argparse
import os
import sys
import subprocess
from pathlib import Path

# Local imports require scripts/ on sys.path
sys.path.insert(0, str(Path(__file__).parent))

from pokered_harness.session import Session
from pokered_harness.mcp_server import register_default_hooks

import walkthrough as wt
import run_to_brock as rtb
import level_up as lu
import brock_gym as bg
import grind


def run_pathfinder(state_path: Path, goal: str, out_path: Path,
                   rom: str, sym: str, sha1: str) -> str:
    """Invoke path_from_tiles.py and return the computed path string."""
    script = Path(__file__).parent / "path_from_tiles.py"
    env = dict(os.environ)
    env.update(
        POKERED_ROM_PATH=rom,
        POKERED_SYM_PATH=sym,
        POKERED_ROM_SHA1=sha1,
        PYTHONPATH=str(Path(__file__).parent.parent / "src"),
        PYTHONIOENCODING="utf-8",
    )
    kw = ["--state", str(state_path), "--save-path-to", str(out_path)]
    if goal.startswith("map:"):
        kw += ["--goal-map", goal[4:]]
    else:
        kw += ["--goal-xy", goal]
    r = subprocess.run(
        [sys.executable, "-u", str(script), *kw],
        env=env, capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"pathfinder failed: {r.stderr}")
    return out_path.read_text().strip()


DIR_CHAR = {"u": "up", "d": "down", "l": "left", "r": "right"}


def walk_path(drv, path: str, *, label: str, stop_map_ids=(),
              blackout_map_ids=(0x25, 0x26)) -> str:
    """Execute a direction-string path. Auto-resolves battles; aborts if
    we blackout (map warps to player's house). Returns reason: ``stop``,
    ``blackout``, ``fainted``, or ``done``.

    If a press doesn't move us, mashes A to clear any trainer dialog /
    "Hey, wait up!" text that fires when an NPC's sight line catches us
    — the battle only becomes active after the dialog is dismissed,
    and before then `joy_locked` is already 0 so callers can't tell a
    wall from a dialog by the standard flags.
    """
    start_map = drv.gs().overworld.map_id
    for i, c in enumerate(path, 1):
        gs = drv.gs()
        if gs.overworld.map_id in stop_map_ids:
            print(f"[{label}] step {i}: reached target map "
                  f"0x{gs.overworld.map_id:02x}", flush=True)
            return "stop"
        if (gs.overworld.map_id in blackout_map_ids
                and gs.overworld.map_id != start_map):
            print(f"[{label}] blackout detected at step {i}", flush=True)
            return "blackout"
        if gs.battle.active:
            drv.resolve_battle()
            if drv.gs().overworld.map_id in blackout_map_ids:
                print(f"[{label}] blackout after battle at step {i}",
                      flush=True)
                return "blackout"
        if drv.gs().party.mons and drv.gs().party.mons[0].hp == 0:
            print(f"[{label}] FAINTED at step {i}", flush=True)
            return "fainted"
        before = (drv.gs().overworld.x, drv.gs().overworld.y,
                  drv.gs().overworld.map_id)
        drv.press(DIR_CHAR[c])
        after = (drv.gs().overworld.x, drv.gs().overworld.y,
                 drv.gs().overworld.map_id)
        if after == before and not drv.gs().battle.active:
            # Stalled. Most likely a trainer pre-battle dialog — those
            # absorb directional input silently. Mash A until either a
            # battle kicks off or a few presses pass (after which it's
            # a real wall and we should give up on this step).
            for _ in range(6):
                drv.press("a")
                if drv.gs().battle.active:
                    drv.resolve_battle()
                    break
                nxt = (drv.gs().overworld.x, drv.gs().overworld.y,
                       drv.gs().overworld.map_id)
                if nxt != before:
                    break
    return "done"


def _activate_repel(drv, steps: int = 255) -> None:
    """RAM-poke wRepelRemainingSteps so wild encounters are suppressed
    for the next few hundred overworld steps. This exists because Gen 1
    Repel only blocks encounters with a level strictly below the lead
    mon — fine for Route 1 (Pidgey/Rattata L2-5) with a L5+ Bulbasaur,
    and a no-op for unaffected encounters. Saves us from grinding
    Bulbasaur up before Viridian Pokécenter exists as a heal option."""
    try:
        base = drv.sym.addr_of("wRepelRemainingSteps")
        drv.mem[base] = steps & 0xff
        print(f"  [repel] wRepelRemainingSteps = {steps}", flush=True)
    except Exception as e:
        print(f"  [repel] failed to set: {e}", flush=True)


def _pathfind_and_walk(drv, session, outdir, goal_xy: str, label: str,
                       rom: str, sym: str, sha1: str,
                       stop_map_ids=()) -> None:
    """Save state → run A* pathfinder → walk the returned direction string,
    resolving battles as they fire. Used by the viridian navigator's
    Route 1 fallback when the hand-coded zig-zag paths get desynced by
    wild battles and the simple "UP with L/R detour" finisher can't
    clear a multi-tile ledge."""
    tmp_state = outdir / f"_{label}.state"
    tmp_state.write_bytes(session.save_state())
    tmp_path = outdir / f"_{label}.txt"
    path = run_pathfinder(tmp_state, goal_xy, tmp_path, rom, sym, sha1)
    print(f"  [{label}] A* {len(path)} steps → walking", flush=True)
    walk_path(drv, path, label=label, stop_map_ids=stop_map_ids)


def navigate_to_viridian_with_retry(drv: "rtb.Driver", outdir: Path,
                                    rom: str, sym: str, sha1: str,
                                    session: Session,
                                    max_attempts: int = 20) -> bool:
    """Drive from wherever we are (lab exit, Pallet, Route 1, or post-
    blackout Red's House) to Viridian City, retrying after blackouts.

    Each blackout advances the emulator's tick counter and thus the wild-
    encounter RNG, so retries are *not* deterministically identical —
    eventually one threads the needle through Route 1 without KOing
    Bulbasaur.
    """
    M_PALLET, M_VIRIDIAN, M_ROUTE_1 = 0x00, 0x01, 0x0c
    M_REDS_1F, M_REDS_2F = 0x25, 0x26
    M_OAKS_LAB = 0x28

    for attempt in range(max_attempts):
        gs = drv.gs()
        print(f"  [viridian attempt {attempt+1}/{max_attempts}] "
              f"map=0x{gs.overworld.map_id:02x} "
              f"xy=({gs.overworld.x},{gs.overworld.y})", flush=True)

        if gs.overworld.map_id == M_VIRIDIAN:
            return True

        # Post-blackout recovery: exit Red's House back into Pallet Town.
        if gs.overworld.map_id in (M_REDS_1F, M_REDS_2F):
            for _ in range(30):
                g = drv.gs()
                if g.overworld.map_id == M_PALLET:
                    break
                if g.overworld.y < 7:
                    drv.press("down")
                elif g.overworld.x > 3:
                    drv.press("left")
                else:
                    drv.press("down")
            drv.idle(60)

        map_id = drv.gs().overworld.map_id
        # If we're inside Oak's Lab (walked back in through the door
        # warp), step down+left to exit via the front mat.
        if map_id == M_OAKS_LAB:
            for _ in range(20):
                g = drv.gs()
                if g.overworld.map_id == M_PALLET:
                    break
                drv.press("down")
            drv.idle(30)
            map_id = drv.gs().overworld.map_id

        # Encounter suppression before every traversal leg (safe to set
        # repeatedly; the game decrements it per step).
        _activate_repel(drv)
        try:
            if map_id == M_ROUTE_1:
                # Walk the Route 1 map to its north warp into Viridian.
                _pathfind_and_walk(
                    drv, session, outdir,
                    goal_xy="10,0", label=f"route1_a_star_{attempt}",
                    rom=rom, sym=sym, sha1=sha1,
                    stop_map_ids=(M_VIRIDIAN,),
                )
                # A* goal at (10, 0) lands ON the northern edge but the
                # actual map warp only fires when we *step* off the
                # edge — mash UP until the map id flips.
                for _ in range(4):
                    if drv.gs().overworld.map_id != M_ROUTE_1:
                        break
                    drv.press("up")
            elif map_id == M_PALLET:
                # Step off the lab door threshold (12, 11) first — A*
                # from (12, 11) toward (10, 0) otherwise routes straight
                # UP through the door warp back into Oak's Lab.
                px, py = drv.gs().overworld.x, drv.gs().overworld.y
                if (px, py) == (12, 11):
                    drv.press("down")
                _pathfind_and_walk(
                    drv, session, outdir,
                    goal_xy="10,0", label=f"pallet_a_star_{attempt}",
                    rom=rom, sym=sym, sha1=sha1,
                    stop_map_ids=(M_ROUTE_1,),
                )
                for _ in range(4):
                    if drv.gs().overworld.map_id != M_PALLET:
                        break
                    drv.press("up")
            else:
                # Lab-exit case: let the smart driver do its zig-zag.
                drv.run_pallet_to_viridian()
        except RuntimeError as e:
            print(f"  pathfinder failed: {e}", flush=True)

        gs = drv.gs()
        if gs.overworld.map_id == M_VIRIDIAN:
            return True

    print("  FAILED: could not reach Viridian after "
          f"{max_attempts} attempts; last map="
          f"0x{drv.gs().overworld.map_id:02x}", flush=True)
    return False


def save_milestone(session: Session, outdir: Path, name: str) -> Path:
    p = outdir / "milestones" / f"{name}.state"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(session.save_state())
    print(f"  saved {name}", flush=True)
    return p


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--outdir", default="walkthrough_badge")
    p.add_argument("--skip-to", choices=[
        "start", "viridian", "grind", "forest", "pewter", "brock"
    ], default="start", help="resume from a specific phase")
    p.add_argument(
        "--legacy-grind", action="store_true",
        help="Use the old level_up.py grinder instead of the heal-loop "
             "grinder in grind.py (diagnostic fallback).",
    )
    args = p.parse_args()

    rom = os.environ["POKERED_ROM_PATH"]
    sym = os.environ["POKERED_SYM_PATH"]
    sha1 = os.environ.get(
        "POKERED_ROM_SHA1", "e1deed63080bc24cad5fba18ecb3184f905d16d4",
    )
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    session = Session.from_files(rom, sym, expected_rom_sha1=sha1)
    register_default_hooks(session)

    # Phase 1: use walkthrough.py + run_to_brock's verified phases to
    # reach Viridian and get onto Route 2.
    wt_drv = wt.WalkthroughDriver(
        session=session, outdir=outdir / "walkthrough_frames",
    )
    drv = rtb.Driver(session)

    if args.skip_to == "start":
        for name, fn in [
            ("intro", lambda: wt.run_phase_intro(wt_drv)),
            ("exit_house", lambda: wt.run_phase_exit_house(wt_drv)),
            ("oak_intercept", lambda: wt.run_phase_oak_intercept(wt_drv)),
            ("pick_starter", lambda: wt.run_phase_pick_starter(wt_drv)),
            ("rival_battle", lambda: wt.run_phase_rival_battle(wt_drv)),
            # run_pallet_to_viridian uses the smart (PP- and type-aware)
            # battle AI from run_to_brock.Driver instead of the mash-A path
            # in walkthrough.py. On Blue the mash-A path blacks out on
            # Route 1 because RNG-dictated wild encounters grind Bulbasaur
            # down. The smart AI + blackout retry below survives either
            # ROM.
            ("pallet_to_viridian",
             lambda: navigate_to_viridian_with_retry(
                 drv, outdir, rom, sym, sha1, session)),
            ("viridian_to_route2", drv.run_viridian_to_route2),
        ]:
            print(f"\n=== phase: {name} ===", flush=True)
            fn()
            save_milestone(session, outdir, name)

    # Phase 2: grind Bulba to Lv 13 with periodic heals.
    if args.skip_to in ("start", "viridian", "grind"):
        print("\n=== phase: grind_to_level_13 ===", flush=True)
        if args.legacy_grind:
            lu.grind_to_level(session, target_level=13, max_battles=60)
        else:
            grind.grind_to(
                session,
                outdir=outdir,
                rom=rom, sym=sym, sha1=sha1,
                target_level=13,
                target_move_id=grind.MOVE_VINE_WHIP,
                max_battles=80,
                max_wall_seconds=900.0,
            )
        save_milestone(session, outdir, "grind_complete")

    # Phase 3: Route 2 → Forest South Gate.
    if args.skip_to in ("start", "viridian", "grind", "forest"):
        print("\n=== phase: route2_to_forest ===", flush=True)
        drv.run_route2_to_forest()
        save_milestone(session, outdir, "route2_to_forest")

        # Through the gate
        for d in ["right", "up", "up", "up", "up"]:
            if drv.gs().overworld.map_id == rtb.M_VIRIDIAN_FOREST:
                break
            if drv.gs().battle.active:
                drv.resolve_battle()
                continue
            drv.press(d)
        forest_entry = save_milestone(session, outdir, "forest_entry")

        # Path through forest using A*, recomputing after battles desync us
        print("\n=== phase: forest_traversal ===", flush=True)
        attempts = 0
        while attempts < 5:
            gs = drv.gs()
            if gs.overworld.map_id == 0x2F:  # north gate
                break
            path_file = outdir / f"forest_leg_{attempts}.txt"
            path = run_pathfinder(
                Path(outdir / "milestones" /
                     (f"forest_leg_{attempts}.state" if attempts else "forest_entry.state")),
                "1,0", path_file, rom, sym, sha1,
            )
            print(f"  forest leg {attempts}: {len(path)} steps", flush=True)
            walk_path(drv, path, label="forest", stop_map_ids=(0x2F,))
            save_milestone(session, outdir, f"forest_leg_{attempts}")
            attempts += 1

        save_milestone(session, outdir, "forest_exit")

    # Phase 4: Pewter City → Gym.
    if args.skip_to in ("start", "viridian", "grind", "forest", "pewter"):
        print("\n=== phase: pewter_approach ===", flush=True)
        # Walk UP out of the north gate into Pewter
        for _ in range(15):
            if drv.gs().overworld.map_id == 0x02:  # Pewter
                break
            drv.press("up")
        save_milestone(session, outdir, "pewter_entry")

    # Phase 5: Brock gym + battle.
    print("\n=== phase: brock_badge ===", flush=True)
    got_badge = bg.run_pewter_to_brock_badge(session, driver=drv)
    save_milestone(session, outdir, "after_brock")

    gs = session.read_game_state()
    print(f"\n=== FINAL ===", flush=True)
    print(f"map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y}) "
          f"badges=0x{gs.progress.badges_raw:02x} "
          f"party[0]=L{gs.party.mons[0].level} HP{gs.party.mons[0].hp}/{gs.party.mons[0].max_hp}",
          flush=True)

    if got_badge:
        print("\nBOULDER BADGE OBTAINED!", flush=True)
        return 0
    print("\nNo badge yet", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
