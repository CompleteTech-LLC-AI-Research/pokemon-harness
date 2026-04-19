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


def walk_path(drv, path: str, *, label: str, stop_map_ids=()) -> None:
    """Execute a direction-string path. Auto-resolves battles; re-computes
    remaining path if a battle interrupts and shifts position."""
    for i, c in enumerate(path, 1):
        gs = drv.gs()
        if gs.overworld.map_id in stop_map_ids:
            print(f"[{label}] step {i}: reached target map "
                  f"0x{gs.overworld.map_id:02x}", flush=True)
            return
        if gs.battle.active:
            drv.resolve_battle()
        if drv.gs().party.mons and drv.gs().party.mons[0].hp == 0:
            print(f"[{label}] FAINTED at step {i}", flush=True)
            return
        drv.press(DIR_CHAR[c])


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
            ("pallet_to_route1",
             lambda: wt.run_phase_pallet_to_route1(wt_drv)),
            ("route1_to_viridian",
             lambda: wt.run_phase_route1_to_viridian(wt_drv)),
            ("viridian_to_route2", drv.run_viridian_to_route2),
        ]:
            print(f"\n=== phase: {name} ===", flush=True)
            fn()
            save_milestone(session, outdir, name)

    # Phase 2: grind Bulba to Lv 13 with periodic heals.
    if args.skip_to in ("start", "viridian", "grind"):
        print("\n=== phase: grind_to_level_13 ===", flush=True)
        lu.grind_to_level(session, target_level=13, max_battles=60)
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
