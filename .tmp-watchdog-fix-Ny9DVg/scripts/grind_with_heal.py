"""Grind Bulbasaur on Route 2 grass, tolerating blackouts.

Assumes wLastBlackoutMap is set to Viridian City (accomplished by first
visiting Viridian Pokemon Center). When Bulbasaur faints in a wild
battle, the game blackouts to Viridian PC at full HP. We detect the
map change to VIRIDIAN_POKECENTER (0x29), walk out, navigate back to
Route 2, resume grinding.

Loops until the target level is reached OR blackout_budget is exhausted.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import subprocess

import run_to_brock as rtb

from pokered_harness.mcp_server import register_default_hooks
from pokered_harness.session import Session


def _pathfind(state_bytes: bytes, goal_xy: tuple[int, int]) -> str | None:
    """Call path_from_tiles.py as a subprocess. Takes the current state
    as bytes, writes to a temp file, invokes the pathfinder, returns the
    path string or None if no path found."""
    import tempfile
    with tempfile.NamedTemporaryFile(delete=False, suffix=".state") as tf:
        tf.write(state_bytes)
        tf_path = tf.name
    out_path = tf_path + ".txt"
    try:
        env = dict(os.environ)
        r = subprocess.run(
            [sys.executable, "-u",
             str(Path(__file__).parent / "path_from_tiles.py"),
             "--state", tf_path,
             "--goal-xy", f"{goal_xy[0]},{goal_xy[1]}",
             "--save-path-to", out_path],
            env=env, capture_output=True, text=True, timeout=30,
            check=False,
        )
        if r.returncode != 0:
            return None
        return Path(out_path).read_text().strip() if Path(out_path).exists() else None
    finally:
        Path(tf_path).unlink(missing_ok=True)
        Path(out_path).unlink(missing_ok=True)


M_PALLET = 0x00
M_VIRIDIAN = 0x01
M_ROUTE_2 = 0x0D
M_VIRIDIAN_PC = 0x29

# Route 2 south-grass area where wild encounters happen (per agent C's
# tile survey): y ~ 45-52, x ~ 4-15. The main path south is NOT grass.
# From Viridian-north entry at (8, 71) we need to walk NORTH into grass.
# The nearest grass row is around y=60 on x=8 (verified empirically).
GRASS_PATCH_XY = (8, 60)


DIR = {"u": "up", "d": "down", "l": "left", "r": "right"}


def select_tackle_or_vinewhip(drv) -> None:
    """Press A to select FIGHT, pick strongest damaging move."""
    drv.press("a")  # FIGHT
    # Read party moves
    m = drv.gs().party.mons[0]
    moves = list(m.moves)
    pp = list(m.pp)
    # Prefer Vine Whip (22), fallback Tackle (33)
    target_slot = None
    for pref in [22, 33]:
        if pref in moves:
            idx = moves.index(pref)
            if idx < len(pp) and pp[idx] > 0:
                target_slot = idx
                break
    if target_slot is None:
        # Any damaging move with PP
        for i, mid in enumerate(moves):
            if mid in rtb.DAMAGING_MOVE_IDS and i < len(pp) and pp[i] > 0:
                target_slot = i
                break
    if target_slot is None:
        # Struggle — just mash A
        drv.press("a")
        return
    # Navigate menu cursor to target_slot (starts at 0)
    for _ in range(target_slot):
        drv.press("down")
    drv.press("a")


def resolve_battle_smart(drv, max_turns: int = 60) -> None:
    for _ in range(max_turns):
        gs = drv.gs()
        if not gs.battle.active:
            return
        if gs.party.mons and gs.party.mons[0].hp == 0:
            # Faint — mash A through whiteout + blackout text
            print("    fainted, mashing through blackout", file=sys.stderr, flush=True)
            for _ in range(150):
                drv.press("a")
                if not drv.gs().battle.active:
                    # broke out of battle — keep mashing through blackout dialog
                    for _ in range(60):
                        drv.press("a")
                    return
            return
        # Wait for menu to open (max_menu_item == 3 means FIGHT menu)
        for _ in range(40):
            if drv.sym.read_u8(drv.mem, "wMaxMenuItem") == 3 and not drv.joy_locked():
                break
            drv.press("a")
        select_tackle_or_vinewhip(drv)
        # Mash A until next menu or battle ends
        for _ in range(80):
            gs = drv.gs()
            if not gs.battle.active:
                return
            if drv.sym.read_u8(drv.mem, "wMaxMenuItem") == 3 and not drv.joy_locked():
                break
            drv.press("a")


def nav_to_route2_grass(drv, max_steps: int = 100) -> bool:
    """From wherever we are, navigate to grass on Route 2. Handles:
      - Viridian PC (after blackout): exit south, walk to Route 2 north
      - Viridian city: path to Route 2 north edge
      - Route 2 south: walk UP to grass row ~y=60
    Returns True iff we reach grass."""
    for attempt in range(max_steps):
        gs = drv.gs()
        if gs.overworld.map_id == M_VIRIDIAN_PC:
            # Walk down to exit
            for _ in range(8):
                drv.press("down")
                if drv.gs().overworld.map_id != M_VIRIDIAN_PC:
                    break
            continue
        if gs.overworld.map_id == M_PALLET:
            # Blackout went to Pallet — no blackout-dest yet. Bail.
            print("    ERROR: blackout went to Pallet, not Viridian PC",
                  file=sys.stderr, flush=True)
            return False
        if gs.overworld.map_id == M_VIRIDIAN:
            # Use the pathfinder to get from current position to the
            # Viridian north-exit tile (18, 0). Then push UP to trigger
            # the map-edge transition to Route 2.
            path = _pathfind(drv.s.save_state(), (18, 0))
            if path is None:
                print(f"    pathfinder failed from ({gs.overworld.x},{gs.overworld.y})",
                      flush=True)
                return False
            dir_map = {"u": "up", "d": "down", "l": "left", "r": "right"}
            for c in path:
                if drv.gs().overworld.map_id != M_VIRIDIAN:
                    break
                if drv.gs().battle.active:
                    drv.resolve_battle()
                drv.press(dir_map[c])
            # Push UP to trigger edge transition
            for _ in range(5):
                if drv.gs().overworld.map_id == M_ROUTE_2:
                    break
                drv.press("up")
            continue
        if gs.overworld.map_id == M_ROUTE_2:
            # Walk the BFS-verified path up to the grass patch at (4, 52).
            # Empirically, only once we're near x=4 y=50-55 do encounters fire.
            if gs.overworld.y > 52 or gs.overworld.x > 5:
                # Use the first portion of ROUTE2_TO_FOREST_GATE_PATH
                # (up×9, left, up×5, left×2, up, left, up×5) lands at (4, 51)
                short_path = ["up"]*9 + ["left"] + ["up"]*5 + ["left"]*2 + ["up"] + ["left"] + ["up"]*5
                for d in short_path:
                    gs = drv.gs()
                    if gs.battle.active: drv.resolve_battle(); break
                    if gs.overworld.y <= 52 and gs.overworld.x <= 5: break
                    drv.press(d)
                continue
            # In grass area
            return True
        # Unknown map
        print(f"    unknown map 0x{gs.overworld.map_id:02x}, pressing A",
              file=sys.stderr, flush=True)
        drv.press("a")
    return False


def _safe_mon(drv):
    """Read party.mons[0] with retries — during battle transitions and
    blackouts the party struct can momentarily be empty."""
    for _ in range(20):
        gs = drv.gs()
        if gs.party.mons:
            return gs.party.mons[0]
        drv.s.step(24, render=True)
    return None


def grind(drv, target_level: int = 13, max_blackouts: int = 20) -> int:
    blackouts = 0
    battles = 0
    while True:
        mon = _safe_mon(drv)
        if mon is None:
            print("  party empty for >20 frames, aborting", flush=True)
            return 0
        if mon.level >= target_level:
            return mon.level
        print(f"  L{mon.level} HP{mon.hp}/{mon.max_hp} battles={battles} blackouts={blackouts}", flush=True)
        if not nav_to_route2_grass(drv):
            print("  failed to reach grass", flush=True)
            return drv.gs().party.mons[0].level
        # Walk in place to trigger encounters
        for step in range(40):
            gs = drv.gs()
            if gs.battle.active:
                battles += 1
                was_map = gs.overworld.map_id
                resolve_battle_smart(drv)
                after = drv.gs()
                if after.overworld.map_id != was_map:
                    # Blackout triggered
                    blackouts += 1
                    if blackouts >= max_blackouts:
                        print("  blackout budget exhausted", flush=True)
                        return after.party.mons[0].level
                    break  # navigate again
                continue
            if gs.party.mons[0].level >= target_level:
                return gs.party.mons[0].level
            # Alternate up/down to stay on grass
            drv.press("up" if step % 2 == 0 else "down")
    return drv.gs().party.mons[0].level


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--state", required=True)
    p.add_argument("--target-level", type=int, default=13)
    p.add_argument("--max-blackouts", type=int, default=20)
    p.add_argument("--save-to", default=None)
    args = p.parse_args()

    rom = os.environ["POKERED_ROM_PATH"]
    sym = os.environ["POKERED_SYM_PATH"]
    sha1 = os.environ.get(
        "POKERED_ROM_SHA1", "e1deed63080bc24cad5fba18ecb3184f905d16d4",
    )
    s = Session.from_files(rom, sym, expected_rom_sha1=sha1)
    register_default_hooks(s)
    s.load_state(Path(args.state).read_bytes())
    s.step(60, render=True)
    drv = rtb.Driver(s)
    gs = drv.gs()
    print(f"start: map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y}) "
          f"L{gs.party.mons[0].level}", flush=True)
    final_level = grind(drv, args.target_level, args.max_blackouts)
    gs = drv.gs()
    print(f"\nFINAL: L{gs.party.mons[0].level} HP{gs.party.mons[0].hp}/{gs.party.mons[0].max_hp} "
          f"moves={list(gs.party.mons[0].moves)} pp={list(gs.party.mons[0].pp)}", flush=True)
    if args.save_to:
        Path(args.save_to).write_bytes(s.save_state())
        print(f"saved {args.save_to}", flush=True)
    return 0 if final_level >= args.target_level else 1


if __name__ == "__main__":
    raise SystemExit(main())
