"""Yellow version of the hook-based Cerulean PC warp (see
_warp_to_cerulean_pc.py for the Blue version — same strategy)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from pokered_harness.session import Session
import run_to_brock as rtb


ROM = "C:/Users/timot/Documents/projects/pokemon/rom/yellow/pokemon-yellow.gbc"
SYM = "C:/Users/timot/Documents/projects/pokemon/rom/yellow/pokemon-yellow.sym"
IN_STATE = Path("C:/Users/timot/Documents/projects/pokemon/walkthrough_yellow/milestones/brock_badge.state")
OUT_STATE = Path("C:/Users/timot/Documents/projects/pokemon/walkthrough_yellow/milestones/cable_club.state")
FIXTURE = Path("tests/fixtures/link/yellow/cable_club.state")

CERULEAN_POKECENTER = 0x40


def add_second_party_mon(s: Session) -> None:
    mem = s._pyboy.memory
    sym = s.symbols
    STRUCT = 44
    mons_base = sym.addr_of("wPartyMons")
    for i in range(STRUCT):
        mem[mons_base + STRUCT + i] = mem[mons_base + i]
    mem[sym.addr_of("wPartyCount")] = 2
    species_addr = sym.addr_of("wPartySpecies")
    mem[species_addr + 1] = 0xA5
    mem[species_addr + 2] = 0xFF
    mem[mons_base + STRUCT + 0] = 0xA5
    for tag in ("wPartyMonOT", "wPartyMonNicks"):
        if tag not in sym:
            continue
        base = sym.addr_of(tag)
        for i in range(11):
            mem[base + 11 + i] = mem[base + i]


def main() -> int:
    s = Session.from_files(ROM, SYM)
    s.load_state(IN_STATE.read_bytes())
    mem = s._pyboy.memory
    sym = s.symbols
    drv = rtb.Driver(s)

    for _ in range(80):
        if not drv.joy_locked() and not drv.gs().battle.active:
            break
        drv.press("a")

    gs = drv.gs()
    print(f"initial: map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y})")

    # In Yellow brock_badge.state the player may already be outside the gym.
    # LoadMapData fires on the next warp. Arm the hook and force a warp
    # by walking toward the gym door if we're inside it.
    if "LoadMapData" not in sym:
        print("  LoadMapData symbol missing — bailing")
        return 1
    load_map_data_addr = sym.addr_of("LoadMapData")
    load_map_data_bank = sym.bank_addr("LoadMapData")[0]
    hit = {"fired": False}

    def override_curmap(_ctx):
        if hit["fired"]:
            return
        mem[sym.addr_of("wCurMap")] = CERULEAN_POKECENTER
        if "wWarpedFromWhichMap" in sym:
            mem[sym.addr_of("wWarpedFromWhichMap")] = 0xFF
        if "wWarpedFromWhichWarp" in sym:
            mem[sym.addr_of("wWarpedFromWhichWarp")] = 0xFF
        hit["fired"] = True
        print(f"  hook fired: wCurMap -> 0x{CERULEAN_POKECENTER:02x}", flush=True)

    s._pyboy.hook_register(load_map_data_bank, load_map_data_addr, override_curmap, None)

    # Force a warp: walk down out of the gym. In Yellow's brock_badge.state
    # the player is at (4, 2) inside Pewter Gym (map 0x36); the exit warp
    # is at (4, 13). 20 downs is plenty.
    print("walking down to trigger warp...")
    for step in range(25):
        if hit["fired"]:
            break
        drv.press("down")

    s.step(120, render=False)
    gs = drv.gs()
    print(f"post-warp: map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y}) hit={hit['fired']}")

    if not hit["fired"]:
        print("  hook never fired — no warp triggered. Yellow start state may need different approach.")
        return 1

    # Position at (11, 3) facing attendant at (11, 2).
    mem[sym.addr_of("wYCoord")] = 3
    mem[sym.addr_of("wXCoord")] = 11
    if "wPlayerMovingDirection" in sym:
        mem[sym.addr_of("wPlayerMovingDirection")] = 0x04
    if "wSpriteStateData1" in sym:
        mem[sym.addr_of("wSpriteStateData1") + 9] = 0x04
    s.step(30, render=False)

    add_second_party_mon(s)
    gs = drv.gs()
    print(f"party_count={gs.party.count}")

    OUT_STATE.write_bytes(s.save_state())
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_bytes(OUT_STATE.read_bytes())

    gs = drv.gs()
    print(f"FINAL: map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y}) party={gs.party.count}")
    print(f"saved {OUT_STATE}")
    print(f"fixture {FIXTURE}")
    s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
