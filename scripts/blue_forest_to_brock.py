"""Blue end-to-end: resume from viridian_to_route2.state and drive
through Route 2 grind → forest → Pewter → Brock badge.

By default Bulbasaur is grinded honestly on Route 2 with a heal-loop
back to Viridian's Pokecenter between battles (see :mod:`grind`). The
legacy RAM-boost hack that L50s the starter survives behind a
``--option-b`` flag as a diagnostic when downstream phases need
debugging.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from pokered_harness.session import Session
from pokered_harness.mcp_server import register_default_hooks
from pokered_harness.state.party import (
    PARTY_STRUCT_SIZE,
    _OFFSET_HP,
    _OFFSET_LEVEL,
    _OFFSET_MAX_HP,
    _OFFSET_MOVES,
    _OFFSET_PP,
)

import run_to_brock as rtb
import brock_gym as bg
import full_to_brock as ftb
import grind


def boost_bulbasaur(session: Session) -> None:
    """Upgrade lead party slot to an L13 Bulbasaur with Vine Whip. Writes
    stats directly to WRAM using the pokered party_struct layout."""
    mem = session._pyboy.memory
    base = session.symbols.addr_of("wPartyMons")  # slot 0

    # Level and HP/MaxHP (big-endian u16 in the struct).
    def put_be16(off: int, val: int) -> None:
        mem[base + off] = (val >> 8) & 0xff
        mem[base + off + 1] = val & 0xff

    # Over-boosted on purpose — Option B's job is to validate the
    # downstream phases, not the grind mechanics. A comfortably L20
    # Bulbasaur with Vine Whip 1-shots Brock's Geodude/Onix and lets
    # `brock_gym` complete deterministically.
    # Max out everything — Option B's job is *not* to prove the battle
    # AI's move-menu navigation, it's to validate the navigation/phase
    # plumbing. The earlier runs showed the BrockDriver cursor pick
    # sometimes lands on Tackle instead of Vine Whip, and a L21
    # Bulbasaur's Tackle barely dents Onix (160 def). At L50 with 255
    # attack, Tackle reliably one-shots both of Brock's mons and the
    # phase completes.
    mem[base + _OFFSET_LEVEL] = 50
    put_be16(_OFFSET_HP, 200)
    put_be16(_OFFSET_MAX_HP, 200)
    put_be16(36, 255)  # Attack
    put_be16(38, 255)  # Defense
    put_be16(40, 120)  # Speed
    put_be16(42, 255)  # Special
    # L50 medium-slow XP is ~101150; 150000 covers with margin.
    xp = 150000
    mem[base + 14] = (xp >> 16) & 0xff
    mem[base + 15] = (xp >> 8) & 0xff
    mem[base + 16] = xp & 0xff

    # Moves: Tackle (33), Growl (45), Leech Seed (73), Vine Whip (22).
    moves = (33, 45, 73, 22)
    for i, m in enumerate(moves):
        mem[base + _OFFSET_MOVES + i] = m
    # PP: default 35/40/10/10
    for i, pp in enumerate((35, 40, 10, 10)):
        mem[base + _OFFSET_PP + i] = pp

    gs = session.read_game_state()
    m = gs.party.mons[0]
    print(
        f"  boosted: L{m.level} HP{m.hp}/{m.max_hp} moves={m.moves}",
        flush=True,
    )


def run_pathfinder(state_path, goal, out_path, rom, sym, sha1):
    """Mirror full_to_brock.run_pathfinder."""
    script = Path(__file__).parent / "path_from_tiles.py"
    env = dict(os.environ)
    env.update(
        POKERED_ROM_PATH=rom, POKERED_SYM_PATH=sym, POKERED_ROM_SHA1=sha1,
        PYTHONPATH=str(Path(__file__).parent.parent / "src"),
        PYTHONIOENCODING="utf-8",
    )
    kw = ["--state", str(state_path), "--save-path-to", str(out_path),
          "--goal-xy", goal]
    r = subprocess.run([sys.executable, "-u", str(script), *kw],
                       env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"pathfinder failed: {r.stderr}")
    return out_path.read_text().strip()


def main() -> int:
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--outdir", default="walkthrough_blue")
    p.add_argument(
        "--option-b", action="store_true",
        help="RAM-boost Bulbasaur to L50 instead of grinding on Route 2. "
             "Diagnostic fallback for when the honest grind is broken or "
             "too slow — downstream forest/Pewter/Brock phases are not "
             "exercised by the grinder itself.",
    )
    args = p.parse_args()

    rom = os.environ["POKERED_ROM_PATH"]
    sym = os.environ["POKERED_SYM_PATH"]
    sha1 = os.environ["POKERED_ROM_SHA1"]
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    session = Session.from_files(rom, sym, expected_rom_sha1=sha1)
    register_default_hooks(session)
    state_path = outdir / "milestones" / "viridian_to_route2.state"
    session.load_state(state_path.read_bytes())
    session.step(60, render=True)

    if args.option_b:
        print("\n=== RAM boost Bulbasaur -> L50 + Vine Whip (Option-B) ===",
              flush=True)
        boost_bulbasaur(session)
    else:
        print("\n=== phase: grind_to_level_13 (heal-loop) ===", flush=True)
        grind.grind_to(
            session,
            outdir=outdir,
            rom=rom, sym=sym, sha1=sha1,
            target_level=13,
            target_move_id=grind.MOVE_VINE_WHIP,
            max_battles=80,
            max_wall_seconds=900.0,
        )

    drv = rtb.Driver(session)

    # Save post-level (boosted or grinded) state as the grind-complete milestone.
    (outdir / "milestones" / "grind_boosted.state").write_bytes(session.save_state())

    # Phase: Route 2 → Forest South Gate — A* from current position
    # rather than the hardcoded zig-zag, because encounter-triggered
    # battles + Route 2 ledges desync the fixed path on Blue.
    print("\n=== phase: route2_to_forest ===", flush=True)
    ftb._activate_repel(drv)
    seed = outdir / "_r2_to_gate.state"
    seed.write_bytes(session.save_state())
    path = run_pathfinder(seed, "3,44", outdir / "_r2_to_gate.txt",
                          rom, sym, sha1)
    print(f"  route2 A*: {len(path)} steps", flush=True)
    ftb.walk_path(drv, path, label="route2",
                  stop_map_ids=(0x32, 0x33))
    # Step UP to warp from (3,44) into south gate.
    for _ in range(4):
        if drv.gs().overworld.map_id == 0x32:
            break
        drv.press("up")
    # Walk through the south gate (map 0x32) to its north warp. The
    # gate's interior is small enough that A* on its tilemap is trivial
    # but the warp itself depends on which tile column we entered from.
    if drv.gs().overworld.map_id == 0x32:
        gate_seed = outdir / "_gate_cross.state"
        gate_seed.write_bytes(session.save_state())
        try:
            # The gate has two warps at (4,0) and (5,0). In practice
            # only (5,0) routes cleanly — UP from (4,1) bumps on tile
            # 0x4a even though the warp lives there. Bug worth tracking
            # down in the pathfinder, but (5,0) is fine for now.
            path = run_pathfinder(gate_seed, "5,0",
                                  outdir / "_gate_cross.txt",
                                  rom, sym, sha1)
            print(f"  gate A*: {len(path)} steps", flush=True)
            ftb.walk_path(drv, path, label="gate",
                          stop_map_ids=(0x33,))
        except RuntimeError as e:
            print(f"  gate pathfind failed: {e}", flush=True)
        # Follow-up UP presses to cross the warp threshold.
        for _ in range(6):
            if drv.gs().overworld.map_id == rtb.M_VIRIDIAN_FOREST:
                break
            drv.press("up")
    (outdir / "milestones" / "forest_entry.state").write_bytes(session.save_state())

    # Phase: Forest traversal (A* to northern warp at (1, 0)).
    # The south-gate exit dumps us on the forest's own warp row y=47.
    # A* doesn't know about warps, so its first LEFT/RIGHT step would
    # warp us right back to the south gate. Step UP once to clear the
    # warp row before planning.
    print("\n=== phase: forest_traversal ===", flush=True)
    # Post-warp xy lags ~60 ticks behind the map-id update on Game Boy;
    # idle before reading or we'll act on stale coords.
    session.step(60, render=True)
    gs = drv.gs()
    print(f"  forest entry: map=0x{gs.overworld.map_id:02x} "
          f"xy=({gs.overworld.x},{gs.overworld.y})", flush=True)
    if gs.overworld.map_id == 0x33 and gs.overworld.y >= 45:
        for _ in range(4):
            drv.press("up")
            gs2 = drv.gs()
            print(f"    after up: xy=({gs2.overworld.x},{gs2.overworld.y}) "
                  f"map=0x{gs2.overworld.map_id:02x}", flush=True)
            if gs2.overworld.y < 45:
                break
    for attempt in range(10):
        gs = drv.gs()
        if gs.overworld.map_id == 0x2F:  # Viridian Forest North Gate
            break
        # If we're already on the goal tile, an A* run returns a 0-step
        # path and we spin forever — step UP to fire the warp instead.
        if gs.overworld.map_id == 0x33 and (gs.overworld.x, gs.overworld.y) in ((1, 0), (2, 0)):
            print(f"  forest at warp tile {gs.overworld.x},{gs.overworld.y} — pressing UP", flush=True)
            for _ in range(4):
                drv.press("up")
                if drv.gs().overworld.map_id == 0x2F:
                    break
            break
        ftb._activate_repel(drv)
        seed_state = outdir / f"_forest_leg_{attempt}.state"
        seed_state.write_bytes(session.save_state())
        path_file = outdir / f"_forest_leg_{attempt}.txt"
        try:
            path = run_pathfinder(seed_state, "1,0", path_file, rom, sym, sha1)
        except RuntimeError as e:
            print(f"  pathfinder failed: {e}", flush=True)
            break
        print(f"  forest leg {attempt}: {len(path)} steps", flush=True)
        if not path:
            # path_from_tiles returned empty — we're already on goal
            # (covered above) or no path exists (dead end). Fall through
            # to mash UP a few times as a last resort; the next attempt
            # either warps or bails.
            for _ in range(4):
                drv.press("up")
                if drv.gs().overworld.map_id == 0x2F:
                    break
            continue
        result = ftb.walk_path(drv, path, label=f"forest{attempt}",
                               stop_map_ids=(0x2F,))
        # After walking, if we're on the warp row but didn't transition
        # (goal tile was reached but UP not pressed), nudge.
        gs = drv.gs()
        if gs.overworld.map_id == 0x33 and gs.overworld.y == 0:
            for _ in range(3):
                drv.press("up")
                if drv.gs().overworld.map_id == 0x2F:
                    break

    # Phase: Pewter approach. Two hops: cross forest *north* gate to
    # Route 2 (north half), then A* through Route 2 to Pewter.
    print("\n=== phase: pewter_approach ===", flush=True)
    ftb._activate_repel(drv)
    # Cross the north gate. Same bug as south gate — UP from (4,1)
    # bumps the wall even though pokered's warp_event lives there, so
    # target (5,0) via A* and then step UP through the warp.
    if drv.gs().overworld.map_id == 0x2F:
        ngate_seed = outdir / "_ngate_cross.state"
        ngate_seed.write_bytes(session.save_state())
        try:
            path = run_pathfinder(ngate_seed, "5,0",
                                  outdir / "_ngate_cross.txt",
                                  rom, sym, sha1)
            print(f"  north gate A*: {len(path)} steps", flush=True)
            ftb.walk_path(drv, path, label="ngate", stop_map_ids=(0x0d,))
        except RuntimeError as e:
            print(f"  north gate pathfind failed: {e}", flush=True)
        for _ in range(6):
            if drv.gs().overworld.map_id == 0x0d:
                break
            drv.press("up")
    # Now on Route 2 north half (~ (3, 11) after warp). Step 60 ticks
    # so post-warp xy settles before we use it.
    session.step(60, render=True)
    if drv.gs().overworld.map_id == 0x0d:
        gs = drv.gs()
        print(f"  at route2 north: xy=({gs.overworld.x},{gs.overworld.y})",
              flush=True)
        seed = outdir / "_r2n_to_pewter.state"
        seed.write_bytes(session.save_state())
        # Pewter border is the north edge (y=0). Only x=8..10 have a
        # walkable corridor — (3, 0), (13, 0), (15, 0), (17, 0) all fail
        # A* due to trees/rocks. (10, 0) is the reliable choice.
        try:
            path = run_pathfinder(seed, "10,0",
                                  outdir / "_r2n_to_pewter.txt",
                                  rom, sym, sha1)
            print(f"  route2 north A*: {len(path)} steps", flush=True)
            ftb.walk_path(drv, path, label="r2n", stop_map_ids=(0x02,))
        except RuntimeError as e:
            print(f"  pewter pathfind failed: {e}", flush=True)
        for _ in range(4):
            if drv.gs().overworld.map_id == 0x02:
                break
            drv.press("up")

    # Phase: Walk to Pewter Gym door via A* (bypasses brock_gym's naive
    # deadlock handler, which presses DOWN at (19, 35) and warps us
    # back into Route 2).
    print("\n=== phase: pewter_to_gym ===", flush=True)
    if drv.gs().overworld.map_id == 0x02:
        session.step(60, render=True)
        gs = drv.gs()
        print(f"  at pewter: xy=({gs.overworld.x},{gs.overworld.y})",
              flush=True)
        seed = outdir / "_pewter_to_gym.state"
        seed.write_bytes(session.save_state())
        try:
            # Gym warp tile is (16, 17). The tile south (16, 18) is the
            # approach — stepping UP from there triggers the warp.
            path = run_pathfinder(seed, "16,18",
                                  outdir / "_pewter_to_gym.txt",
                                  rom, sym, sha1)
            print(f"  pewter→gym A*: {len(path)} steps", flush=True)
            ftb.walk_path(drv, path, label="pgym",
                          stop_map_ids=(0x36,))  # 0x36 == PEWTER_GYM
            for _ in range(4):
                if drv.gs().overworld.map_id == 0x36:
                    break
                drv.press("up")
        except RuntimeError as e:
            print(f"  pewter→gym pathfind failed: {e}", flush=True)

    # Phase: Inside gym — A* to (4, 2) to cross Brock's sight line, then
    # let brock_gym's battle + post-dialog loop finish.
    if drv.gs().overworld.map_id == 0x36:
        session.step(60, render=True)
        gs = drv.gs()
        print(f"\n=== phase: gym_interior ===", flush=True)
        print(f"  in gym: xy=({gs.overworld.x},{gs.overworld.y})",
              flush=True)
        seed = outdir / "_gym_interior.state"
        seed.write_bytes(session.save_state())
        try:
            path = run_pathfinder(seed, "4,2",
                                  outdir / "_gym_interior.txt",
                                  rom, sym, sha1)
            print(f"  gym A*: {len(path)} steps", flush=True)
            ftb.walk_path(drv, path, label="gym")
        except RuntimeError as e:
            print(f"  gym pathfind failed: {e}", flush=True)

    # Phase: Brock battle. On Blue (and Red too, apparently), Brock's
    # trainer sight line does NOT fire when we walk UP into his column
    # — we end up on (4, 2) directly below him with no battle. The
    # trigger is an explicit A-press to talk to him, followed by ~30
    # A-presses of pre-battle dialog before the battle state flips on.
    # Once we're in battle, brock_gym's BrockDriver handles move
    # selection and post-battle dialog.
    print("\n=== phase: brock_badge ===", flush=True)
    brock_drv = bg.BrockDriver(session)
    # The gym A* usually lands us at (4, 6) after the Jr. Trainer fires
    # mid-path. Make sure we're standing on (4, 2) before trying to
    # trigger Brock. Resolve any residual battle/dialog first, then
    # walk UP (resolving battles as they fire).
    for _ in range(30):
        gs = brock_drv.gs()
        if gs.overworld.x == 4 and gs.overworld.y == 2:
            break
        if gs.battle.active:
            brock_drv.resolve_battle(max_turns=40)
            continue
        if brock_drv.joy_locked():
            brock_drv.press("a")
            continue
        before = (gs.overworld.x, gs.overworld.y)
        brock_drv.press("up")
        after = (brock_drv.gs().overworld.x, brock_drv.gs().overworld.y)
        if after == before and not brock_drv.gs().battle.active:
            # stalled — mash A to clear any lingering dialog
            for _ in range(4):
                brock_drv.press("a")
                if brock_drv.gs().battle.active:
                    brock_drv.resolve_battle(max_turns=30)
                    break
    print(f"  pre-brock: xy=({brock_drv.gs().overworld.x},{brock_drv.gs().overworld.y})", flush=True)
    # Mash A to open + advance the pre-battle dialog, stop when the
    # battle state flips on (typically 30-40 A presses).
    for i in range(60):
        if brock_drv.gs().battle.active:
            print(f"  brock dialog closed after {i} A-presses", flush=True)
            break
        brock_drv.press("a")
    # Fight the battle (Geodude, then Onix).
    brock_drv.resolve_battle(max_turns=50)
    # Post-battle dialog until the badge bit flips.
    for _ in range(200):
        gs = brock_drv.gs()
        if gs.progress.badges_raw & 0x01:
            break
        if gs.battle.active:
            brock_drv.resolve_battle(max_turns=30)
            continue
        brock_drv.press("a")
    got_badge = bool(brock_drv.gs().progress.badges_raw & 0x01)
    (outdir / "milestones" / "after_brock.state").write_bytes(session.save_state())

    gs = session.read_game_state()
    m = gs.party.mons[0]
    print(f"\n=== FINAL ===\nmap=0x{gs.overworld.map_id:02x} "
          f"xy=({gs.overworld.x},{gs.overworld.y}) "
          f"badges=0x{gs.progress.badges_raw:02x} "
          f"party[0]=L{m.level} HP{m.hp}/{m.max_hp}", flush=True)

    session._pyboy.screen.image.save(outdir / "FINAL.png")

    if got_badge:
        print("\nBOULDER BADGE OBTAINED on Blue!", flush=True)
        return 0
    print("\nNo badge yet", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
