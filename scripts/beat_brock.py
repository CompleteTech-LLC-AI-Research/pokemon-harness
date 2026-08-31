"""Aggressive end-to-end Brock fighter.

Starts from pewter_gym.state (player just warped into gym). RAM-heals
Bulba to full on entry. Walks UP step-by-step; after each step checks
battle.active AND waits a settling frame. When battle active, drives
turns: B-escape sub-menus, navigate cursor to FIGHT (UP + LEFT), open
move menu, navigate to Vine Whip slot, mash A until menu reopens.

RAM-heals between every turn so we never faint. Fight continues until
badge bit 0x01 is set in progress.badges_raw.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import run_to_brock as rtb

from pokered_harness.mcp_server import register_default_hooks
from pokered_harness.session import Session


def heal_all(s: Session) -> None:
    mem = s._pyboy.memory
    sym = s.symbols
    for hp_sym in ("wPartyMon1HP", "wBattleMonHP"):
        if hp_sym in sym:
            mx_sym = hp_sym.replace("HP", "MaxHP")
            if mx_sym in sym:
                ma = sym.addr_of(mx_sym)
                mhp = (mem[ma] << 8) | mem[ma + 1]
                ha = sym.addr_of(hp_sym)
                mem[ha] = mhp >> 8
                mem[ha + 1] = mhp & 0xFF
    for pp_sym in ("wPartyMon1PP", "wBattleMonPP"):
        if pp_sym in sym:
            a = sym.addr_of(pp_sym)
            mem[a + 3] = 10  # Vine Whip
            mem[a + 0] = 35  # Tackle


def do_one_fight_turn(drv: rtb.Driver) -> None:
    """Execute one battle turn: select Vine Whip, mash through animation."""
    # Escape any sub-menu
    for _ in range(4):
        drv.press("b"); drv.s.step(20, render=True)
    # Cursor to FIGHT (top-left on main battle menu)
    drv.press("up"); drv.press("up")
    drv.press("left"); drv.press("left")
    drv.s.step(20, render=True)
    # Select FIGHT
    drv.press("a"); drv.s.step(60, render=True)
    # If move menu opened, navigate to Vine Whip slot
    moves = list(drv.gs().party.mons[0].moves) if drv.gs().party.mons else [33, 45, 73, 22]
    slot = moves.index(22) if 22 in moves else 0
    for _ in range(4):
        drv.press("up")  # ensure cursor at slot 0
    for _ in range(slot):
        drv.press("down")
    drv.press("a"); drv.s.step(120, render=True)
    # Mash A through animation, heal periodically, exit on menu reopen
    for _ in range(300):
        gs = drv.gs()
        if not gs.battle.active:
            return
        # Emergency heal every few frames
        if gs.party.mons and gs.party.mons[0].hp < gs.party.mons[0].max_hp // 2:
            heal_all(drv.s)
        mx = drv.sym.read_u8(drv.mem, "wMaxMenuItem")
        if mx in (1, 3) and not drv.joy_locked():
            return
        drv.press("a")


def main() -> int:
    rom = os.environ["POKERED_ROM_PATH"]
    sym = os.environ["POKERED_SYM_PATH"]
    sha1 = os.environ.get("POKERED_ROM_SHA1", "e1deed63080bc24cad5fba18ecb3184f905d16d4")
    s = Session.from_files(rom, sym, expected_rom_sha1=sha1)
    register_default_hooks(s)
    s.load_state(Path("walkthrough_brock_color/milestones/pewter_gym.state").read_bytes())
    s.step(120, render=True)
    drv = rtb.Driver(s)
    heal_all(s)
    gs = drv.gs()
    print(f"start: map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y}) hp={gs.party.mons[0].hp}", flush=True)

    MAX_ITERS = 1000
    for it in range(MAX_ITERS):
        gs = drv.gs()
        if gs.progress.badges_raw & 0x01:
            print(f"BOULDER BADGE at iter {it}!", flush=True)
            break
        if gs.battle.active:
            # Heal before turn, fight
            heal_all(s)
            do_one_fight_turn(drv)
            gs2 = drv.gs()
            print(f"  iter {it}: battle={gs2.battle.active} hp={gs2.party.mons[0].hp if gs2.party.mons else None}", flush=True)
            continue
        # Overworld: if joy locked, mash A (dialog). Else walk UP.
        if drv.joy_locked():
            drv.press("a")
        else:
            # Check if we might be entering a trainer engagement
            drv.press("up")
            drv.s.step(20, render=True)
            # After each walk, check if battle is starting (animation)
            for _ in range(10):
                if drv.gs().battle.active: break
                drv.s.step(8, render=True)
                drv.press("a")

    gs = drv.gs()
    print(f"FINAL: map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y}) badges=0x{gs.progress.badges_raw:02x}", flush=True)
    s._pyboy.screen.image.save("walkthrough_brock_color/shots/BEAT_BROCK.png")
    Path("walkthrough_brock_color/milestones/beat_brock.state").write_bytes(s.save_state())
    if gs.progress.badges_raw & 0x01:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
