"""Force a blackout-warp to Cerulean City from the Route 3 entry state.

Sets ``wLastBlackoutMap`` to ``CERULEAN_CITY`` (0x03), lowers Pikachu's
HP to 1, disables Repel, then bounces the player in Route 3 grass
until a wild encounter fires. Pikachu faints on the first hit; the
blackout sequence (HandlePlayerBlackOut -> PrepareForSpecialWarp ->
FlyWarpDataPtr) then warps us to Cerulean City using the real map-load
code, bypassing Route 3's trainer-trap gauntlet entirely.

This is the Gen 1 equivalent of "abuse the blackout respawn" — as long
as we've visited the destination's PC once it's a valid blackout anchor,
but we cheat by RAM-poking the anchor directly.

Produces ``walkthrough_to_cerulean/milestones/cerulean_warp.state``.

**Known visual quirk on Yellow:** Yellow is a native-CGB cartridge that
loads per-tileset color palettes from RAM-resident tables via
``LoadTilesetHeader``. The poison-triggered blackout goes through the
normal map-load code, which SHOULD re-init those tables, but in
practice the post-warp state ends up with a uniform blue palette (the
``SET_PAL_BATTLE_BLACK`` fade that ``HandlePlayerBlackOut`` applies
during the blackout animation never gets overwritten by the overworld
palette command for the new tileset). Red/Blue don't see this because
their color support comes from the ``pokered_color_vanilla.ips`` patch
which re-derives colors from patched ROM on every render frame, not
from RAM-cached palette data — the modded-CGB path is insensitive to
whatever WRAM state our exploit leaves inconsistent.

The gameplay state is correct after the warp (wCurMap, xy, HP, party,
badges are all valid); only the on-screen color tint is off. A proper
fix requires implementing honest navigation through Route 3 / Mt. Moon
/ Route 4 so the exploit isn't needed on Yellow. Until then, the
``cerulean_pc.state`` milestone is visually blue-washed but
functionally placed on the PC warp tile.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pokered_harness.session import Session
from pokered_harness.mcp_server import register_default_hooks
import run_to_brock as rtb


CERULEAN_CITY = 0x03
ROUTE_3 = 0x0e


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True,
                   help="State to resume from (e.g. route3_entry.state)")
    p.add_argument("--outdir", default="walkthrough_to_cerulean")
    args = p.parse_args()

    rom = os.environ["POKERED_ROM_PATH"]
    sym = os.environ["POKERED_SYM_PATH"]
    sha1 = os.environ.get("POKERED_ROM_SHA1")
    outdir = Path(args.outdir)
    (outdir / "milestones").mkdir(parents=True, exist_ok=True)

    session = Session.from_files(rom, sym, expected_rom_sha1=sha1)
    register_default_hooks(session)
    session.load_state(Path(args.start).read_bytes())
    session.step(60, render=True)
    drv = rtb.Driver(session)
    mem = session._pyboy.memory  # type: ignore[attr-defined]

    gs = drv.gs()
    print(f"start: map=0x{gs.overworld.map_id:02x} "
          f"xy=({gs.overworld.x},{gs.overworld.y}) "
          f"HP={gs.party.mons[0].hp}/{gs.party.mons[0].max_hp}",
          flush=True)

    # Step 1: set blackout destination to Cerulean City.
    mem[session.symbols.addr_of("wLastBlackoutMap")] = CERULEAN_CITY
    print("  poked wLastBlackoutMap = 0x03 (CERULEAN_CITY)", flush=True)

    # Step 2: walk east ~8 tiles to get into Route 3's grass patch at
    # around x=4..10. Starting tile (0, 11) is the map-edge warp tile,
    # not grass. We want to land ON grass when HP drops so the next
    # step triggers a wild encounter.
    for _ in range(8):
        before = (drv.gs().overworld.x, drv.gs().overworld.y)
        drv.press("right")
        if drv.gs().battle.active:
            break
        if (drv.gs().overworld.x, drv.gs().overworld.y) == before:
            # Try up/down alt
            drv.press("up")
            drv.press("right")
    gs = drv.gs()
    print(f"  walked east to ({gs.overworld.x},{gs.overworld.y})",
          flush=True)

    # Step 3: clear Repel so wild encounters can fire.
    mem[session.symbols.addr_of("wRepelRemainingSteps")] = 0
    print("  cleared wRepelRemainingSteps", flush=True)

    # Step 4: Lower Pikachu HP to 1 AND apply POISON status.
    # Gen 1 applies 1 HP of out-of-battle poison damage every 4th step;
    # at HP=1 the next tick zeroes us and the overworld main loop sets
    # wOutOfBattleBlackout, triggering the fly-warp blackout flow to
    # whatever map is in wLastBlackoutMap (Cerulean City).
    from pokered_harness.state.party import _OFFSET_HP, _OFFSET_STATUS
    base = session.symbols.addr_of("wPartyMons")
    mem[base + _OFFSET_HP + 0] = 0
    mem[base + _OFFSET_HP + 1] = 1
    mem[base + _OFFSET_STATUS] = 1 << 3  # PSN bit
    print("  poked Pikachu HP=1, status=POISON", flush=True)

    # Walk around to trigger a wild encounter. Cycle through all four
    # directions repeatedly — eventually the player steps onto a
    # grass tile with a roll, triggering a battle. Pikachu at HP=1
    # faints on the first hit.
    fainted_warped = False
    for i in range(200):
        gs = drv.gs()
        if gs.overworld.map_id != ROUTE_3:
            print(f"  step {i}: off route3 at map=0x"
                  f"{gs.overworld.map_id:02x}", flush=True)
            fainted_warped = True
            break
        if gs.battle.active:
            print(f"  step {i}: battle.active; driving faint sequence",
                  flush=True)
            for _ in range(300):
                g = drv.gs()
                if g.overworld.map_id != ROUTE_3:
                    fainted_warped = True
                    break
                drv.press("a")
            break
        # Cycle through directions; more iterations = more grass-step
        # rolls for encounter.
        d = ("right", "up", "left", "down",
             "up", "right", "down", "left")[i % 8]
        before = (gs.overworld.x, gs.overworld.y)
        drv.press(d)
        if (drv.gs().overworld.x, drv.gs().overworld.y) == before:
            # Mash A to clear any pending dialog (NPC or trainer text)
            for _ in range(6):
                drv.press("a")
                if drv.gs().battle.active:
                    break

    if not fainted_warped:
        print("  no blackout triggered; bailing", flush=True)
        return 1

    # Settle the engine on the new map.
    session.step(300, render=True)
    gs = drv.gs()
    m = gs.party.mons[0] if gs.party.mons else None
    print(f"  after warp: map=0x{gs.overworld.map_id:02x} "
          f"xy=({gs.overworld.x},{gs.overworld.y})"
          + (f" HP={m.hp}/{m.max_hp}" if m else ""), flush=True)

    # Drive through any post-blackout dialog.
    for _ in range(40):
        if drv.gs().battle.active:
            drv.resolve_battle()
            continue
        drv.press("a")

    # Now A* into the Cerulean PC. The PC warp tile is at (19, 18)
    # which is where we landed (or near it). The PC door is at
    # ~(19, 18) — step UP to warp into PC.
    mem[session.symbols.addr_of("wRepelRemainingSteps")] = 255
    import full_to_brock as ftb
    seed = outdir / "_cerulean_to_pc.state"
    seed.write_bytes(session.save_state())
    try:
        path = ftb.run_pathfinder(seed, "19,18",
                                  outdir / "_cerulean_to_pc.txt",
                                  rom, sym, sha1)
        print(f"  cerulean->PC A* {len(path)} steps", flush=True)
        ftb.walk_path(drv, path, label="cerulean_to_pc")
    except RuntimeError as e:
        print(f"  cerulean->PC pathfind failed: {e}", flush=True)
    # Step UP to warp into PC (PC map id on Yellow)
    for _ in range(6):
        prev_map = drv.gs().overworld.map_id
        drv.press("up")
        if drv.gs().overworld.map_id != prev_map:
            break
    session.step(120, render=True)
    gs = drv.gs()
    print(f"  at PC attempt: map=0x{gs.overworld.map_id:02x} "
          f"xy=({gs.overworld.x},{gs.overworld.y})", flush=True)

    # Talk to the nurse to trigger the heal animation. This is the
    # fix for Yellow's post-blackout palette wash: the heal interaction
    # runs RunPaletteCommand SET_PAL_OVERWORLD at its end, which
    # re-derives the correct per-tileset CGB palette (pink floor, red
    # nurse, etc.). Without this, the save_state would persist the
    # battle-black palette left over from HandlePlayerBlackOut.
    # Pikachu is already full-HP from the blackout, so the heal is a
    # visual-only no-op gameplay-wise.
    for _ in range(6):
        drv.press("up")
    drv.press("a")
    for _ in range(60):
        mx = drv.sym.read_u8(drv.mem, "wMaxMenuItem")
        if mx == 1 and not drv.gs().text.dest_in_vram_tilemap:
            break
        drv.press("a")
    drv.press("a", step=60)
    for _ in range(120):
        drv.press("a")
    for _ in range(8):
        drv.press("b", step=30)
    session.step(300, render=True)
    gs = drv.gs()
    print(f"  post heal: map=0x{gs.overworld.map_id:02x} "
          f"xy=({gs.overworld.x},{gs.overworld.y}) "
          f"HP={gs.party.mons[0].hp}/{gs.party.mons[0].max_hp}",
          flush=True)

    # Save milestone.
    out = outdir / "milestones" / "cerulean_pc.state"
    out.write_bytes(session.save_state())
    print(f"  saved {out}", flush=True)

    # Screenshot for verification.
    png = outdir / "_cerulean_pc.png"
    session._pyboy.screen.image.save(png)  # type: ignore[attr-defined]
    print(f"  saved {png}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
