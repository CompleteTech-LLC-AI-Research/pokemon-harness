"""Produce Blue Cable Club state by directly jumping CPU to HandleBlackOut.

Breakthrough: PyBoy 2.7.0's `register_file.PC` is writable. Set
`wLastBlackoutMap = CERULEAN_POKECENTER`, then redirect PC to
`HandleBlackOut`. The game runs its full, proper blackout sequence
(fade out, stop music, ResetStatusAndHalveMoneyOnBlackout,
PrepareForSpecialWarp, SpecialEnterMap) which loads Cerulean PC with
fully initialized map / sprite / movement state.

Unlike the EnterMap hook-warp, this produces a state the player can
actually walk around in.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import run_to_brock as rtb

from pokered_harness.session import Session

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROM = REPO_ROOT / "rom/blue/pokemon-blue-color.gb"
DEFAULT_SYM = REPO_ROOT / "rom/blue/pokemon-blue.sym"
DEFAULT_SHA = "5f4b05725a860e04077045462176d3e2771c5022"
DEFAULT_FIXTURE = REPO_ROOT / "tests/fixtures/link/blue/cable_club.state"

CERULEAN_CITY = 0x03
CERULEAN_POKECENTER = 0x40


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rom",
        type=Path,
        default=Path(os.environ.get("POKERED_ROM_PATH", DEFAULT_ROM)),
    )
    parser.add_argument(
        "--sym",
        type=Path,
        default=Path(os.environ.get("POKERED_SYM_PATH", DEFAULT_SYM)),
    )
    parser.add_argument(
        "--sha",
        default=os.environ.get("POKERED_ROM_SHA1", DEFAULT_SHA),
    )
    parser.add_argument(
        "--in-state",
        type=Path,
        default=(
            Path(os.environ["POKERED_INPUT_STATE"])
            if os.environ.get("POKERED_INPUT_STATE")
            else None
        ),
        required=False,
        help="source save state; also settable via POKERED_INPUT_STATE",
    )
    parser.add_argument(
        "--out-state",
        type=Path,
        default=DEFAULT_FIXTURE,
        help="generated state path (defaults to the ignored fixture path)",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=DEFAULT_FIXTURE,
        help="fixture copy path (defaults to the ignored fixture path)",
    )
    args = parser.parse_args()
    if args.in_state is None:
        parser.error("--in-state or POKERED_INPUT_STATE is required")
    return args


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
    args = parse_args()
    s = Session.from_files(args.rom, args.sym, expected_rom_sha1=args.sha)
    s.load_state(args.in_state.read_bytes())
    mem = s._pyboy.memory
    sym = s.symbols
    pb = s._pyboy
    drv = rtb.Driver(s)

    # 1. Unstick the post-Brock dialog and let the overworld loop start.
    for _ in range(80):
        if not drv.joy_locked() and not drv.gs().battle.active:
            break
        drv.press("a")

    # 2. Step a few frames so the emulator is firmly in the overworld loop,
    #    not mid-dialog-rendering.
    s.step(30, render=False)

    # 3. FlyWarpDataPtr only maps to outdoor city coords, not PC interiors.
    #    Aim for Cerulean City: blackout lands us at (19, 18), one tile
    #    south of the PC door at (19, 17). Then we step UP to warp into
    #    the PC naturally, producing a fully-initialized interior state.
    mem[sym.addr_of("wLastBlackoutMap")] = CERULEAN_CITY
    sf6_addr = sym.addr_of("wStatusFlags6")
    mem[sf6_addr] = mem[sf6_addr] | (1 << 6)  # BIT_ESCAPE_WARP
    print(f"wLastBlackoutMap=0x{CERULEAN_CITY:02x} (CITY) wStatusFlags6=0x{mem[sf6_addr]:02x}")

    # 4. HandleBlackOut is at bank 0 addr 0x0931. Set the ROM bank to 0
    #    (home bank is always mapped at 0x0000-0x3FFF regardless, but we
    #    also need the current ROM bank state to be sensible for the later
    #    farcalls). Then redirect PC.
    handle_blackout = sym.addr_of("HandleBlackOut")
    pc_before = pb.register_file.PC
    pb.register_file.PC = handle_blackout
    print(f"PC redirected: 0x{pc_before:04x} -> 0x{handle_blackout:04x} (HandleBlackOut)")

    # 5. Run the blackout → SpecialEnterMap sequence.
    for phase in range(12):
        s.step(60, render=False)
        gs = drv.gs()
        if gs.overworld.map_id == CERULEAN_CITY and not drv.joy_locked():
            print(f"  arrived at Cerulean City after {(phase+1)*60} frames")
            break
        if phase % 3 == 2:
            print(f"  phase {phase+1}: map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y}) joy={drv.joy_locked()}")

    gs = drv.gs()
    print(f"post-blackout: map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y})")

    if gs.overworld.map_id != CERULEAN_CITY:
        print(f"  FAIL: expected map 0x{CERULEAN_CITY:02x}")
        s.close()
        return 1

    # 6. Sanity check: walking.
    before = (drv.gs().overworld.x, drv.gs().overworld.y)
    drv.press("left")
    after = (drv.gs().overworld.x, drv.gs().overworld.y)
    drv.press("right")
    print(f"walk test: {before} -> {after}")

    # 7. Step onto the PC warp. Blackout drops player at (19, 18), PC door
    #    is at (19, 17). Walking UP should warp us into the PC.
    print("entering Pokémon Center (step up)...")
    for _ in range(5):
        drv.press("up")
        s.step(60, render=False)
        gs = drv.gs()
        if gs.overworld.map_id == CERULEAN_POKECENTER:
            print(f"  entered PC: ({gs.overworld.x},{gs.overworld.y})")
            break

    gs = drv.gs()
    if gs.overworld.map_id != CERULEAN_POKECENTER:
        print(f"  FAIL: couldn't enter PC, stuck at map=0x{gs.overworld.map_id:02x}")
        s.close()
        return 1

    # 8. Walk to (11, 3) — one tile south of the attendant. The route
    #    dodges the gentleman (4, 3) and super nerd (10, 5) sprites via
    #    a short down→right→up detour. A* suggested `drrurrrrrr` from
    #    (3, 3). Add a few extra `u`s for up-movement when approaching
    #    from (3, 7) entry.
    print("walking to attendant (11, 2)...")
    for ch in "uuuudrrurrrrrr":
        gs = drv.gs()
        if gs.overworld.map_id != CERULEAN_POKECENTER:
            break
        if (gs.overworld.x, gs.overworld.y) == (11, 3):
            break
        drv.press({"u": "up", "d": "down", "l": "left", "r": "right"}[ch])
    drv.press("up")  # bump into attendant to face up
    s.step(30, render=False)

    gs = drv.gs()
    print(f"arrived: ({gs.overworld.x},{gs.overworld.y})")

    # 8. Restore party HP (blackout leaves everyone fainted — but wait, we
    #    didn't actually zero HP before the PC-write. Blackout fades
    #    regardless of party HP, but ResetStatusAndHalveMoneyOnBlackout
    #    may clobber HP to 0. Re-fill to max.).
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

    # Set EVENT_GOT_POKEDEX (bit 37 of wEventFlags = byte +4, bit 5).
    # Without it the Cable Club attendant refuses to link, printing
    # "Please wait. We're making preparations" and exiting without
    # attempting a serial handshake.
    ev_addr = sym.addr_of("wEventFlags") + 4
    mem[ev_addr] = mem[ev_addr] | (1 << 5)
    print(f"set EVENT_GOT_POKEDEX bit; wEventFlags+4 = 0x{mem[ev_addr]:02x}")

    # Re-restore HP for slot 1
    for i in range(mem[sym.addr_of("wPartyCount")]):
        base = mons_base + i * STRUCT
        max_hp = (mem[base + 34] << 8) | mem[base + 35]
        if max_hp == 0:
            max_hp = 150
            mem[base + 34] = (max_hp >> 8) & 0xFF
            mem[base + 35] = max_hp & 0xFF
        mem[base + 1] = (max_hp >> 8) & 0xFF
        mem[base + 2] = max_hp & 0xFF

    gs = drv.gs()
    print(f"party: count={gs.party.count} mon0_hp={gs.party.mons[0].hp}/{gs.party.mons[0].max_hp}")

    args.out_state.parent.mkdir(parents=True, exist_ok=True)
    args.out_state.write_bytes(s.save_state())
    args.fixture.parent.mkdir(parents=True, exist_ok=True)
    args.fixture.write_bytes(args.out_state.read_bytes())

    gs = drv.gs()
    print(f"FINAL: map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y}) party={gs.party.count}")
    print(f"saved {args.out_state}")
    print(f"fixture {args.fixture}")
    s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
