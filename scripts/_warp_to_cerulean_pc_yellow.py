"""Yellow Cable Club state via EnterMap hook-warp.

Blue uses HandleBlackOut (a proper game-engine teleport that produces a
fully playable state); Yellow's equivalent stalls in the fade/music
sub-routine when jumped to via register_file.PC, so Yellow falls back
to the simpler EnterMap hook. The resulting state loads + pairs cleanly
for link-cable testing, though full walkability through the PC isn't
guaranteed."""

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

    enter_map_bank, enter_map_addr = sym.bank_addr("EnterMap")
    hit = {"fired": False}

    def override_curmap(_ctx):
        if hit["fired"]:
            return
        mem[sym.addr_of("wCurMap")] = CERULEAN_POKECENTER
        hit["fired"] = True

    s._pyboy.hook_register(enter_map_bank, enter_map_addr, override_curmap, None)

    print("walking down to trigger gym-exit warp...")
    for _ in range(25):
        if hit["fired"]:
            break
        if drv.gs().overworld.map_id != 0x36:
            break
        drv.press("down")

    s.step(180, render=False)
    gs = drv.gs()
    print(f"post-warp: map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y})")

    add_second_party_mon(s)

    # Set EVENT_GOT_POKEDEX (bit 37 = byte +4, bit 5) so the Cable Club
    # attendant will actually attempt a link connection.
    ev_addr = sym.addr_of("wEventFlags") + 4
    mem[ev_addr] = mem[ev_addr] | (1 << 5)

    OUT_STATE.write_bytes(s.save_state())
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_bytes(OUT_STATE.read_bytes())

    gs = drv.gs()
    print(f"FINAL: map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y}) party={gs.party.count}")
    print(f"fixture {FIXTURE}")
    s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
