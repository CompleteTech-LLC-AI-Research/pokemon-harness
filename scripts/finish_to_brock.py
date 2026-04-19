"""From the grind_complete state (Bulba L13 on Route 2 grass),
play the rest: grass → Forest gate → forest → Pewter → Brock.

Uses the pathfinder iteratively (recompute after each battle that
shifts position). Auto-resolves battles with Vine Whip preference.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from pokered_harness.session import Session
from pokered_harness.mcp_server import register_default_hooks
import run_to_brock as rtb
import brock_gym as bg


DIR = {"u": "up", "d": "down", "l": "left", "r": "right"}


def pathfind(session: Session, goal_xy: tuple[int, int]) -> str | None:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".state") as tf:
        tf.write(session.save_state())
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
        )
        if r.returncode != 0:
            print(f"pathfinder error: {r.stderr[-500:]}", flush=True)
            return None
        p = Path(out_path).read_text().strip() if Path(out_path).exists() else None
        return p
    finally:
        Path(tf_path).unlink(missing_ok=True)
        Path(out_path).unlink(missing_ok=True)


def select_vine_whip_move(drv) -> None:
    """Pick Vine Whip if learned + PP, else Tackle, else any damaging."""
    drv.press("a")  # FIGHT
    m = drv.gs().party.mons[0]
    moves = list(m.moves)
    pp = list(m.pp)
    slot = None
    for pref in [22, 33]:  # Vine Whip, Tackle
        if pref in moves:
            i = moves.index(pref)
            if i < len(pp) and pp[i] > 0:
                slot = i; break
    if slot is None:
        for i, mid in enumerate(moves):
            if mid in rtb.DAMAGING_MOVE_IDS and i < len(pp) and pp[i] > 0:
                slot = i; break
    if slot is None:
        drv.press("a"); return  # Struggle
    for _ in range(slot):
        drv.press("down")
    drv.press("a")


def resolve_battle_vw(drv, max_turns: int = 60) -> None:
    for _ in range(max_turns):
        gs = drv.gs()
        if not gs.battle.active:
            return
        if gs.party.mons and gs.party.mons[0].hp == 0:
            print("    fainted!", flush=True)
            for _ in range(150):
                drv.press("a")
                if not drv.gs().battle.active:
                    for _ in range(60):
                        drv.press("a")
                    return
            return
        # wait for menu
        for _ in range(40):
            if drv.sym.read_u8(drv.mem, "wMaxMenuItem") == 3 and not drv.joy_locked():
                break
            drv.press("a")
        select_vine_whip_move(drv)
        for _ in range(100):
            gs = drv.gs()
            if not gs.battle.active: return
            if drv.sym.read_u8(drv.mem, "wMaxMenuItem") == 3 and not drv.joy_locked():
                break
            drv.press("a")


def apply_path_with_recompute(drv, label: str, goal_xy: tuple[int, int] | None,
                              goal_map_ids: tuple[int, ...], max_attempts: int = 8) -> bool:
    """Apply a path. After each battle-interrupt, recompute from current state."""
    for attempt in range(max_attempts):
        gs = drv.gs()
        if gs.overworld.map_id in goal_map_ids:
            print(f"[{label}] reached goal map 0x{gs.overworld.map_id:02x}", flush=True)
            return True
        if goal_xy and (gs.overworld.x, gs.overworld.y) == goal_xy:
            print(f"[{label}] reached goal coord {goal_xy}", flush=True)
            return True
        if not goal_xy:
            # try generic goal on the current map — shouldn't happen
            return False
        path = pathfind(drv.s, goal_xy)
        if path is None:
            print(f"[{label}] pathfinder found no route", flush=True)
            return False
        print(f"[{label}] attempt {attempt}: {len(path)} steps", flush=True)
        made_progress = False
        for c in path:
            gs = drv.gs()
            if gs.overworld.map_id in goal_map_ids:
                print(f"[{label}] transitioned to 0x{gs.overworld.map_id:02x}", flush=True)
                return True
            if gs.battle.active:
                resolve_battle_vw(drv)
                # path might be stale now — recompute
                break
            before = (gs.overworld.x, gs.overworld.y)
            drv.press(DIR[c])
            if (drv.gs().overworld.x, drv.gs().overworld.y) != before:
                made_progress = True
        # If we consumed the path and are at/near goal_xy, try pushing one more tile
        if goal_xy:
            gs = drv.gs()
            if (gs.overworld.x, gs.overworld.y) == goal_xy:
                print(f"[{label}] at goal coord", flush=True)
                return True
    return False


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--state", default="walkthrough_brock_color/milestones/grind_complete.state")
    p.add_argument("--outdir", default="walkthrough_brock_color")
    args = p.parse_args()

    rom = os.environ["POKERED_ROM_PATH"]
    sym = os.environ["POKERED_SYM_PATH"]
    sha1 = os.environ.get("POKERED_ROM_SHA1", "e1deed63080bc24cad5fba18ecb3184f905d16d4")
    outdir = Path(args.outdir)
    (outdir / "milestones").mkdir(parents=True, exist_ok=True)

    s = Session.from_files(rom, sym, expected_rom_sha1=sha1)
    register_default_hooks(s)
    s.load_state(Path(args.state).read_bytes())
    s.step(60, render=True)
    drv = rtb.Driver(s)
    gs = drv.gs()
    print(f"START: map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y}) "
          f"L{gs.party.mons[0].level} HP{gs.party.mons[0].hp}/{gs.party.mons[0].max_hp} "
          f"moves={list(gs.party.mons[0].moves)}", flush=True)

    # Phase 1: Route 2 grass → Forest South Gate entry at (3, 43)
    print("\n=== Phase 1: Route 2 grass → Forest South Gate ===", flush=True)
    ok = apply_path_with_recompute(
        drv, "to_gate", goal_xy=(3, 43),
        goal_map_ids=(0x32,),
    )
    if not ok:
        print("FAILED Phase 1", flush=True); return 1
    # Push UP to enter the gate (warp)
    for _ in range(5):
        if drv.gs().overworld.map_id == 0x32: break
        drv.press("up")
    (outdir / "milestones" / "phase1_gate.state").write_bytes(s.save_state())
    s._pyboy.screen.image.save(outdir / "shots" / "phase1_gate.png")
    print(f"Phase 1 done: map=0x{drv.gs().overworld.map_id:02x} "
          f"xy=({drv.gs().overworld.x},{drv.gs().overworld.y})", flush=True)

    # Phase 2: through gate → into forest (0x33)
    print("\n=== Phase 2: gate → forest interior ===", flush=True)
    for d in ["right", "up", "up", "up"]:
        if drv.gs().overworld.map_id == 0x33: break
        if drv.gs().battle.active: resolve_battle_vw(drv); continue
        drv.press(d)
    # push extra UP in case we need
    for _ in range(5):
        if drv.gs().overworld.map_id == 0x33: break
        drv.press("up")
    (outdir / "milestones" / "phase2_forest.state").write_bytes(s.save_state())
    s._pyboy.screen.image.save(outdir / "shots" / "phase2_forest.png")
    print(f"Phase 2 done: map=0x{drv.gs().overworld.map_id:02x} "
          f"xy=({drv.gs().overworld.x},{drv.gs().overworld.y})", flush=True)

    # Phase 3: forest → north gate (map 0x2F)
    print("\n=== Phase 3: forest → Forest North Gate ===", flush=True)
    ok = apply_path_with_recompute(
        drv, "forest", goal_xy=(1, 0),
        goal_map_ids=(0x2F,), max_attempts=12,
    )
    if not ok:
        print("FAILED Phase 3", flush=True); return 1
    # push UP through north gate
    for _ in range(5):
        if drv.gs().overworld.map_id == 0x2F: break
        drv.press("up")
    (outdir / "milestones" / "phase3_north_gate.state").write_bytes(s.save_state())
    s._pyboy.screen.image.save(outdir / "shots" / "phase3_north_gate.png")
    print(f"Phase 3 done: map=0x{drv.gs().overworld.map_id:02x} "
          f"xy=({drv.gs().overworld.x},{drv.gs().overworld.y})", flush=True)

    # Phase 4: north gate → Route 2 north half → Pewter (map 0x02)
    print("\n=== Phase 4: north gate → Pewter ===", flush=True)
    # walk up through the gate to Route 2 north
    for _ in range(15):
        if drv.gs().overworld.map_id != 0x2F: break
        drv.press("up")
    # continue up into Pewter
    for _ in range(60):
        if drv.gs().overworld.map_id == 0x02: break
        if drv.gs().battle.active: resolve_battle_vw(drv); continue
        before = (drv.gs().overworld.x, drv.gs().overworld.y)
        drv.press("up")
        if (drv.gs().overworld.x, drv.gs().overworld.y) == before:
            drv.press("left"); drv.press("up")
            if (drv.gs().overworld.x, drv.gs().overworld.y) == before:
                drv.press("right"); drv.press("right"); drv.press("up")
    (outdir / "milestones" / "phase4_pewter.state").write_bytes(s.save_state())
    s._pyboy.screen.image.save(outdir / "shots" / "phase4_pewter.png")
    print(f"Phase 4 done: map=0x{drv.gs().overworld.map_id:02x} "
          f"xy=({drv.gs().overworld.x},{drv.gs().overworld.y})", flush=True)

    # Phase 5: Pewter → Brock
    print("\n=== Phase 5: Brock Gym + battle ===", flush=True)
    got_badge = bg.run_pewter_to_brock_badge(s, driver=drv)
    (outdir / "milestones" / "phase5_after_brock.state").write_bytes(s.save_state())
    s._pyboy.screen.image.save(outdir / "shots" / "phase5_after_brock.png")

    gs = drv.gs()
    print(f"\n=== FINAL ===", flush=True)
    print(f"map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y}) "
          f"badges=0x{gs.progress.badges_raw:02x} "
          f"party[0]=L{gs.party.mons[0].level} HP{gs.party.mons[0].hp}/{gs.party.mons[0].max_hp}",
          flush=True)
    if got_badge and (gs.progress.badges_raw & 0x01):
        print("\n*** BOULDER BADGE OBTAINED ***", flush=True)
        return 0
    print("\nbadge not obtained", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
