"""Robust forest traversal: enter forest, walk to north gate, handle
battles with Vine Whip. Designed to work around Game Boy menu quirks
seen in iterative runs."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from pokered_harness.session import Session
from pokered_harness.mcp_server import register_default_hooks
import run_to_brock as rtb


DIR = {"u": "up", "d": "down", "l": "left", "r": "right"}


def pathfind(s: Session, goal: tuple[int, int]) -> str | None:
    with tempfile.NamedTemporaryFile(delete=False, suffix=".state") as tf:
        tf.write(s.save_state()); tp = tf.name
    op = tp + ".txt"
    try:
        r = subprocess.run(
            [sys.executable, "-u", "scripts/path_from_tiles.py",
             "--state", tp, "--goal-xy", f"{goal[0]},{goal[1]}",
             "--save-path-to", op],
            env=os.environ, capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return None
        return Path(op).read_text().strip() if Path(op).exists() else None
    finally:
        Path(tp).unlink(missing_ok=True)
        Path(op).unlink(missing_ok=True)


def fight_with_vine_whip(drv) -> None:
    """Robust battle resolver: handles main menu, move menu, PKMN sub-menu."""
    for turn in range(80):
        gs = drv.gs()
        if not gs.battle.active:
            return
        mon = gs.party.mons[0] if gs.party.mons else None
        if mon is None or mon.hp == 0:
            # Faint — mash A through whiteout
            for _ in range(200):
                drv.press("a")
                if not drv.gs().battle.active:
                    for _ in range(60): drv.press("a")
                    return
            return
        # Press B first to escape any sub-menu (PKMN, ITEM), return to main
        for _ in range(3):
            drv.press("b")
        # Now we should be on the main battle menu (FIGHT/PKMN/ITEM/RUN).
        # Press A on FIGHT (cursor position 0).
        # Actually wMaxMenuItem may be nonzero because we're on PKMN menu.
        # The cleanest: press UP + LEFT several times to force cursor to top-left (FIGHT),
        # then A.
        for _ in range(3):
            drv.press("up")
        for _ in range(3):
            drv.press("left")
        drv.press("a")  # FIGHT
        # Now on move menu (max_menu should be 3 = 4 moves visible). Navigate to Vine Whip.
        moves = list(mon.moves)
        slot = None
        pp = list(mon.pp)
        for pref in [22, 33]:  # Vine Whip, Tackle
            if pref in moves:
                i = moves.index(pref)
                if pp[i] > 0:
                    slot = i; break
        if slot is None:
            # Any damaging move
            for i, mid in enumerate(moves):
                if mid in rtb.DAMAGING_MOVE_IDS and pp[i] > 0:
                    slot = i; break
        if slot is None:
            # Struggle — just press A (game forces Struggle)
            drv.press("a"); continue
        # Navigate cursor (starts at slot 0)
        for _ in range(slot):
            drv.press("down")
        drv.press("a")  # Confirm move
        # Mash A until battle ends OR move menu reopens
        for _ in range(120):
            gs = drv.gs()
            if not gs.battle.active:
                return
            max_menu = drv.sym.read_u8(drv.mem, "wMaxMenuItem")
            # Move menu is max=3, main menu is max=1. If we're back on
            # a menu (user input wanted), break and pick again.
            if max_menu in (1, 3) and not drv.joy_locked():
                break
            drv.press("a")


def main() -> int:
    rom = os.environ["POKERED_ROM_PATH"]
    sym = os.environ["POKERED_SYM_PATH"]
    sha1 = os.environ.get("POKERED_ROM_SHA1", "e1deed63080bc24cad5fba18ecb3184f905d16d4")
    s = Session.from_files(rom, sym, expected_rom_sha1=sha1)
    register_default_hooks(s)
    state_path = Path("walkthrough_brock_color/milestones/forest_proper.state")
    s.load_state(state_path.read_bytes())
    s.step(60, render=True)
    drv = rtb.Driver(s)

    # Clear any residual battle
    if drv.gs().battle.active:
        fight_with_vine_whip(drv)

    for attempt in range(15):
        gs = drv.gs()
        print(f"[attempt {attempt}] map=0x{gs.overworld.map_id:02x} "
              f"xy=({gs.overworld.x},{gs.overworld.y}) "
              f"HP={gs.party.mons[0].hp}/{gs.party.mons[0].max_hp} "
              f"PP={list(gs.party.mons[0].pp)}", flush=True)
        if gs.overworld.map_id == 0x2F:
            print("REACHED Forest North Gate!", flush=True)
            break
        if gs.overworld.map_id != 0x33:
            print(f"unexpected map 0x{gs.overworld.map_id:02x}", flush=True)
            break
        path = pathfind(s, (1, 0))
        if not path:
            print("no path", flush=True); break
        print(f"  path: {len(path)} steps", flush=True)
        for i, c in enumerate(path):
            gs = drv.gs()
            if gs.battle.active:
                print(f"    step {i}: battle", flush=True)
                fight_with_vine_whip(drv)
                break
            if gs.overworld.map_id == 0x2F:
                break
            if drv.joy_locked():
                for _ in range(30):
                    drv.press("a")
                    if not drv.joy_locked(): break
            drv.press(DIR[c])
    gs = drv.gs()
    print(f"FINAL: map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y}) "
          f"HP={gs.party.mons[0].hp}", flush=True)
    s._pyboy.screen.image.save("walkthrough_brock_color/shots/forest_final.png")
    if gs.overworld.map_id == 0x2F:
        Path("walkthrough_brock_color/milestones/forest_north_gate.state").write_bytes(s.save_state())
        print("saved forest_north_gate.state", flush=True)
        return 0
    Path("walkthrough_brock_color/milestones/forest_last.state").write_bytes(s.save_state())
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
