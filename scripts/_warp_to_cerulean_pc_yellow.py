"""Yellow Cable Club state via HandleBlackOut PC-write.

Same approach as Blue but with different HandleBlackOut address and a
workaround for Yellow's audio-init hang: instead of waiting for the
blackout sequence to complete on its own, we force-clear the audio
fade-out state machine that tends to stall in save states."""

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

CERULEAN_CITY = 0x03
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
    pb = s._pyboy
    drv = rtb.Driver(s)

    for _ in range(80):
        if not drv.joy_locked() and not drv.gs().battle.active:
            break
        drv.press("a")
    # Walk out of gym so we're in a clean overworld state.
    for _ in range(25):
        if drv.gs().overworld.map_id != 0x36:
            break
        drv.press("down")
    s.step(180, render=False)

    # Clear audio fade-out + active-sound state that tends to stall Yellow's
    # HandleBlackOut → StopMusic loop when invoked from a synthetic state.
    for tag in ("wAudioFadeOutControl", "wAudioFadeOutCounter",
                "wAudioFadeOutCounterReloadValue", "wNewSoundID",
                "wSoundID", "wLastMusicSoundID"):
        if tag in sym:
            mem[sym.addr_of(tag)] = 0

    mem[sym.addr_of("wLastBlackoutMap")] = CERULEAN_CITY
    mem[sym.addr_of("wStatusFlags6")] |= (1 << 6)  # BIT_ESCAPE_WARP

    handle_blackout = sym.addr_of("HandleBlackOut")
    pb.register_file.PC = handle_blackout
    print(f"PC -> HandleBlackOut (0x{handle_blackout:04x})")

    # Step long enough for full fade + warp + map load.
    for phase in range(24):
        s.step(60, render=False)
        gs = drv.gs()
        if gs.overworld.map_id == CERULEAN_CITY and not drv.joy_locked():
            print(f"  arrived at Cerulean City after {(phase+1)*60} frames")
            break
        # Keep stomping audio state so any wait loops don't hang forever.
        for tag in ("wAudioFadeOutControl", "wAudioFadeOutCounter"):
            if tag in sym:
                mem[sym.addr_of(tag)] = 0

    gs = drv.gs()
    print(f"post-blackout: map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y})")
    if gs.overworld.map_id != CERULEAN_CITY:
        print(f"  FAIL: still not in Cerulean City")
        s.close()
        return 1

    # Walk up into the PC.
    for _ in range(5):
        drv.press("up")
        s.step(60, render=False)
        if drv.gs().overworld.map_id == CERULEAN_POKECENTER:
            break
    gs = drv.gs()
    if gs.overworld.map_id != CERULEAN_POKECENTER:
        print(f"  FAIL: couldn't enter PC, map=0x{gs.overworld.map_id:02x}")
        s.close()
        return 1

    # Walk to (11, 3).
    for ch in "uuuudrrurrrrrr":
        gs = drv.gs()
        if (gs.overworld.x, gs.overworld.y) == (11, 3):
            break
        drv.press({"u": "up", "d": "down", "l": "left", "r": "right"}[ch])
    drv.press("up")
    s.step(30, render=False)

    # Restore HP + add second party slot + set EVENT_GOT_POKEDEX.
    STRUCT = 44
    mons_base = sym.addr_of("wPartyMons")
    for i in range(mem[sym.addr_of("wPartyCount")]):
        base = mons_base + i * STRUCT
        max_hp = (mem[base + 34] << 8) | mem[base + 35]
        if max_hp == 0:
            max_hp = 150
            mem[base + 34] = (max_hp >> 8) & 0xFF
            mem[base + 35] = max_hp & 0xFF
        mem[base + 1] = (max_hp >> 8) & 0xFF
        mem[base + 2] = max_hp & 0xFF

    add_second_party_mon(s)
    for i in range(mem[sym.addr_of("wPartyCount")]):
        base = mons_base + i * STRUCT
        max_hp = (mem[base + 34] << 8) | mem[base + 35]
        if max_hp == 0:
            max_hp = 150
            mem[base + 34] = (max_hp >> 8) & 0xFF
            mem[base + 35] = max_hp & 0xFF
        mem[base + 1] = (max_hp >> 8) & 0xFF
        mem[base + 2] = max_hp & 0xFF

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
