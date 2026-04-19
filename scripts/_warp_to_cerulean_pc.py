"""Direct warp to Cerulean Pokémon Center via hook-based wCurMap override.

Strategy: load post-Brock state, let the player walk down out of the gym
(which triggers a warp). Hook LoadMapData — at that moment the game has
already set wCurMap to the next destination. We overwrite wCurMap to
CERULEAN_POKECENTER (0x40) before LoadMapData reads it, so the engine
loads Cerulean PC's map data, sprites, tiles, etc. properly.

After the warp, set position near the Cable Club attendant at (11, 3)
facing up. Add a second party Pokémon so trade is valid. Save the state.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from pokered_harness.session import Session
import run_to_brock as rtb


ROM = "C:/Users/timot/Documents/projects/pokemon/rom/blue/pokemon-blue-color.gb"
SYM = "C:/Users/timot/Documents/projects/pokemon/rom/blue/pokemon-blue.sym"
SHA = "5f4b05725a860e04077045462176d3e2771c5022"
IN_STATE = Path("C:/Users/timot/Documents/projects/pokemon/walkthrough_blue/milestones/after_brock.state")
OUT_STATE = Path("C:/Users/timot/Documents/projects/pokemon/walkthrough_blue/milestones/cable_club.state")
FIXTURE = Path("tests/fixtures/link/blue/cable_club.state")

CERULEAN_POKECENTER = 0x40


def add_second_party_mon(s: Session) -> None:
    """Duplicate slot 0 into slot 1, then patch the species to Rattata
    (internal id 0xA5). Player now has 2 mons (Ivysaur L54 + Rattata L54),
    satisfying the Cable Club's "can't trade your only mon" check."""
    mem = s._pyboy.memory
    sym = s.symbols

    count_addr = sym.addr_of("wPartyCount")
    species_addr = sym.addr_of("wPartySpecies")
    mons_base = sym.addr_of("wPartyMons")

    # PartyMon struct size is 44 (0x2C).
    STRUCT = 44

    # Copy slot 0 -> slot 1
    for i in range(STRUCT):
        mem[mons_base + STRUCT + i] = mem[mons_base + i]

    # Update counts and species list (terminated by 0xFF).
    mem[count_addr] = 2
    mem[species_addr + 0] = mem[mons_base + 0]  # slot 0 species
    mem[species_addr + 1] = 0xA5  # Rattata
    mem[species_addr + 2] = 0xFF  # list terminator

    # Patch slot 1's species byte too (first byte of its struct).
    mem[mons_base + STRUCT + 0] = 0xA5

    # wPartyMonOT / wPartyMonNicks — these hold trainer/nickname strings.
    # For simplicity, copy slot 0's entries to slot 1.
    for tag in ("wPartyMonOT", "wPartyMonNicks"):
        if tag not in sym:
            continue
        base = sym.addr_of(tag)
        for i in range(11):  # 11-byte encoded strings
            mem[base + 11 + i] = mem[base + i]


def main() -> int:
    s = Session.from_files(ROM, SYM, expected_rom_sha1=SHA)
    s.load_state(IN_STATE.read_bytes())
    mem = s._pyboy.memory
    sym = s.symbols
    drv = rtb.Driver(s)

    # 1. Unstick post-Brock dialog.
    for _ in range(80):
        if not drv.joy_locked() and not drv.gs().battle.active:
            break
        drv.press("a")

    # 2. Arm the LoadMapData hook: when the game is about to load the next
    #    map, force wCurMap = CERULEAN_POKECENTER.
    load_map_data_addr = sym.addr_of("LoadMapData")
    load_map_data_bank = sym.bank_addr("LoadMapData")[0]
    hit = {"fired": False}

    def override_curmap(_ctx):
        if hit["fired"]:
            return
        mem[sym.addr_of("wCurMap")] = CERULEAN_POKECENTER
        # Also clear warp-related flags that could make the engine
        # re-route back to Pewter City.
        if "wWarpedFromWhichMap" in sym:
            mem[sym.addr_of("wWarpedFromWhichMap")] = 0xFF
        if "wWarpedFromWhichWarp" in sym:
            mem[sym.addr_of("wWarpedFromWhichWarp")] = 0xFF
        hit["fired"] = True
        print(f"  hook fired: wCurMap overridden to 0x{CERULEAN_POKECENTER:02x}", flush=True)

    s._pyboy.hook_register(load_map_data_bank, load_map_data_addr, override_curmap, None)

    # 3. Walk the player DOWN out of Pewter Gym. This triggers a natural
    #    warp from gym -> Pewter City, which invokes LoadMapData.
    print("walking out of gym to trigger warp...", flush=True)
    for _ in range(20):
        if hit["fired"]:
            break
        if drv.gs().overworld.map_id != 0x36:  # PEWTER_GYM
            break
        drv.press("down")

    # 4. Let the game settle with the new map loaded.
    s.step(120, render=False)
    gs = drv.gs()
    print(f"post-warp: map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y})")

    if gs.overworld.map_id != CERULEAN_POKECENTER:
        print(f"  WARN: expected map 0x{CERULEAN_POKECENTER:02x}, got 0x{gs.overworld.map_id:02x}")

    # 5. Position player near the Cable Club attendant at (11, 2).
    mem[sym.addr_of("wYCoord")] = 3   # stand one tile south
    mem[sym.addr_of("wXCoord")] = 11  # same column as attendant
    # Face UP so pressing A talks to attendant.
    if "wPlayerMovingDirection" in sym:
        mem[sym.addr_of("wPlayerMovingDirection")] = 0x04  # UP
    if "wSpriteStateData1" in sym:
        # Player sprite facing direction is at wSpriteStateData1 + 9 (facing).
        mem[sym.addr_of("wSpriteStateData1") + 9] = 0x00  # 0=DOWN, 4=UP, 8=LEFT, C=RIGHT — 0 means down
        # Actually in pokered direction bytes are 0=down, 4=up, 8=left, C=right per facing_direction_constants.asm
        mem[sym.addr_of("wSpriteStateData1") + 9] = 0x04
    s.step(30, render=False)

    # 6. Add a second party Pokémon.
    add_second_party_mon(s)
    gs = drv.gs()
    print(f"party_count={gs.party.count} mons={[m.species for m in gs.party.mons]}")

    # 7. Save final state.
    OUT_STATE.write_bytes(s.save_state())
    print(f"saved {OUT_STATE}")

    # 8. Copy to fixture path.
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_bytes(OUT_STATE.read_bytes())
    print(f"copied to {FIXTURE}")

    gs = drv.gs()
    print(f"FINAL: map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y}) party={gs.party.count}")
    s.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
