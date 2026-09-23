"""Yellow post-Brock: Pewter Gym -> Cerulean PokéCenter.

Resumes from a Yellow Boulder Badge state (e.g.
``walkthrough_yellow_honest15/milestones/brock_badge.state``) and
walks the canonical post-Brock route:

    Pewter Gym -> Pewter PC heal -> Route 3 -> Mt. Moon (1F -> B1F -> B2F ->
    1F east exit) -> Route 4 -> Cerulean City -> Cerulean PC

Each leg saves a milestone state under ``--outdir/milestones/`` so
later iterations can resume mid-pipeline. Uses ``path_from_tiles.py``
A* for in-map navigation; map-warp transitions are handled by stepping
the relevant direction at the warp tile until the map id changes.

Usage::

    PYTHONIOENCODING=utf-8 \\
    POKERED_ROM_PATH=rom/yellow/pokemon-yellow.gbc \\
    POKERED_SYM_PATH=rom/yellow/pokemon-yellow.sym \\
    POKERED_ROM_SHA1=cc7d03262ebfaf2f06772c1a480c7d9d5f4a38e1 \\
    python scripts/to_cerulean.py \\
        --start walkthrough_yellow_honest15/milestones/brock_badge.state \\
        --outdir walkthrough_yellow_to_cerulean
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import full_to_brock as ftb
import run_to_brock as rtb
from _to_cerulean_route3 import (
    cross_route3,
    exit_gym,
    heal_at_pewter_pc,
    walk_to_route3,
)
from _to_cerulean_support import (
    _MT_MOON_1F_TRAINER_EVENTS,
    _MT_MOON_B2F_TRAINER_EVENTS,
    _MT_MOON_WARPS_BY_FLOOR,
    M_CERULEAN_CITY,
    M_CERULEAN_POKECENTER,
    M_MT_MOON_1F,
    M_MT_MOON_B1F,
    M_MT_MOON_B2F,
    M_PEWTER_CITY,
    M_PEWTER_POKECENTER,
    M_ROUTE_3,
    M_ROUTE_4,
    _b2f_fossil_sprites_present,
    _clear_b2f_fossils,
    _gs_summary,
    _mark_trainers_defeated,
    _pathfind_walk,
    _save,
    _step_by_step_walk,
    _try_warp_hop,
)

from pokered_harness.mcp_server import register_default_hooks
from pokered_harness.session import Session

# --- Phase 5: cross Route 4 east -> Cerulean City ------------------------

def _perturb_rng(session: Session, seed_hash: int) -> None:
    """Poke hRandomAdd (0xFFD3) / hRandomSub (0xFFD4) to break
    deterministic NPC-walk cycles across blackout recoveries. Without
    this, each cycle Pikachu traverses identical NPC patterns and
    gets pinned at the same sprite-attractor tiles indefinitely."""
    try:
        mem = session._pyboy.memory  # type: ignore[attr-defined]
        mem[0xFFD3] = (seed_hash * 37 + 123) & 0xFF
        mem[0xFFD4] = (seed_hash * 211 + 17) & 0xFF
    except (AttributeError, LookupError, TypeError):
        return


def _recover_to_route4_west(drv: rtb.Driver, session: Session,
                             outdir: Path, rom: str, sym: str, sha1: str,
                             max_cycles: int = 4) -> bool:
    """After a Mt. Moon blackout lands us at Pewter PC (0x3a or 0x02),
    walk back through Pewter -> Route 3 -> Route 4 west to resume Mt.
    Moon traversal. Perturbs the game's RNG (hRandomAdd/Sub) before
    re-entering so NPC walk patterns differ per cycle, avoiding the
    sprite-attractor cycles that trap identical replays."""
    import time as _time
    _perturb_rng(session, int(_time.time()))
    # Exit Pewter PC if inside
    for _ in range(8):
        if drv.gs().overworld.map_id == M_PEWTER_CITY:
            break
        drv.press("down")
    if drv.gs().overworld.map_id != M_PEWTER_CITY:
        return False
    # Clear any bench-guy dialog
    for _ in range(60):
        before = (drv.gs().overworld.x, drv.gs().overworld.y)
        drv.press("a")
        drv.press("down")
        if (drv.gs().overworld.x, drv.gs().overworld.y) != before:
            break
    # East to Route 3
    ftb._activate_repel(drv)
    _pathfind_walk(drv, session, outdir, "35,19",
                   "rec_pewter_east",
                   rom, sym, sha1, stop_map_ids=(M_ROUTE_3,))
    for _ in range(8):
        if drv.gs().overworld.map_id == M_ROUTE_3:
            break
        drv.press("right")
    session.step(120, render=True)
    # Cross Route 3 via the known waypoints
    for goal, label in [("30,11", "rec_r3_1"), ("45,11", "rec_r3_2"),
                         ("60,0", "rec_r3_n")]:
        if drv.gs().overworld.map_id != M_ROUTE_3:
            break
        _pathfind_walk(drv, session, outdir, goal, label,
                        rom, sym, sha1,
                        stop_map_ids=(M_ROUTE_4,))
        ftb._activate_repel(drv)
    for _ in range(8):
        if drv.gs().overworld.map_id == M_ROUTE_4:
            break
        drv.press("up")
    session.step(120, render=True)
    return drv.gs().overworld.map_id == M_ROUTE_4


def cross_route4(drv: rtb.Driver, session: Session, outdir: Path,
                 rom: str, sym: str, sha1: str) -> bool:
    """Walk east across Route 4 to the Cerulean City connection.

    Route 4 is split in two halves by the Mt. Moon mountain range:
      - West half (sx <= 20) where Pikachu emerges from Route 3.
      - East half (sx >= 23) connecting east to Cerulean.
    There is no overworld bridge. To cross you MUST go through
    Mt. Moon: enter via Route 4 warp at (18, 5) -> MT_MOON_1F (14, 35),
    path through 1F to B1F warp, through B1F to its east exit at
    (27, 3) -> Route 4 warp 3 at (24, 5) — now on east half.
    """
    ftb._activate_repel(drv)
    session.step(120, render=True)
    # Multi-cycle wrapper: Mt. Moon trainers may faint Pikachu,
    # blackout -> Pewter PC. Recover and retry — each cycle covers
    # new ground because defeated trainers stay defeated.
    blackout_cycles = 0
    stuck_in_mm_count = 0
    last_mm_xy = None
    tried_edges: set[tuple[int, int, int, int, int]] = set()
    while drv.gs().overworld.map_id != M_CERULEAN_CITY:
        cur_map = drv.gs().overworld.map_id
        if cur_map in (M_PEWTER_CITY, M_PEWTER_POKECENTER):
            if blackout_cycles >= 15:
                print("  mt_moon: too many blackouts; bailing",
                      flush=True)
                return False
            blackout_cycles += 1
            stuck_in_mm_count = 0
            print(f"  mt_moon blackout #{blackout_cycles} -> recovering",
                  flush=True)
            if not _recover_to_route4_west(drv, session, outdir,
                                            rom, sym, sha1):
                return False
            continue
        # If stuck in Mt. Moon same xy across multiple phases, force
        # a blackout to reset NPC positions via the poison trick.
        if cur_map in (M_MT_MOON_1F, M_MT_MOON_B1F, M_MT_MOON_B2F):
            cur_xy = (drv.gs().overworld.x, drv.gs().overworld.y)
            if cur_xy == last_mm_xy:
                stuck_in_mm_count += 1
                if stuck_in_mm_count >= 2:
                    print(f"  mt_moon stuck at {cur_xy}; poison-blackout "
                          "to reset NPC state", flush=True)
                    try:
                        mem = session._pyboy.memory  # type: ignore
                        drv.mem[drv.sym.addr_of("wLastBlackoutMap")] = M_PEWTER_CITY
                        from pokered_harness.state.party import (
                            _OFFSET_HP,
                            _OFFSET_STATUS,
                        )
                        base = drv.sym.addr_of("wPartyMons")
                        mem[base + _OFFSET_HP + 0] = 0
                        mem[base + _OFFSET_HP + 1] = 1
                        mem[base + _OFFSET_STATUS] = 1 << 3
                    except Exception:  # noqa: BLE001, S110 - optional poison poke must not abort navigation
                        pass
                    for _ in range(120):
                        if drv.gs().overworld.map_id != cur_map:
                            break
                        for d in ("up", "down", "left", "right"):
                            drv.press(d)
                            if drv.gs().battle.active:
                                drv.resolve_battle()
                            if drv.gs().overworld.map_id != cur_map:
                                break
                    session.step(300, render=True)
                    stuck_in_mm_count = 0
                    continue
            else:
                stuck_in_mm_count = 0
                last_mm_xy = cur_xy
        # Phase A: walk to Route 4 (18, 5) Mt. Moon warp.
        if drv.gs().overworld.map_id == M_ROUTE_4:
            # Only press UP for the initial entry from Route 3's north
            # map-connection (player arrives on Route 4 south edge,
            # y~17). After arriving from Mt. Moon's east exit at
            # (24, 5), y is already near 5 and UP bumps into a wall —
            # worse, on arrival the warp tile cooldown may be stale
            # and stepping into it again re-fires B1F warp. Skip the
            # up-press when y < 10.
            if drv.gs().overworld.y >= 10:
                for _ in range(4):
                    before = (drv.gs().overworld.x, drv.gs().overworld.y)
                    drv.press("up")
                    if drv.gs().battle.active:
                        drv.resolve_battle()
                    if (drv.gs().overworld.x, drv.gs().overworld.y) != before:
                        break
            ftb._activate_repel(drv)
            # If on west half (x<=20), warp into Mt. Moon 1F.
            if drv.gs().overworld.x <= 20:
                _pathfind_walk(drv, session, outdir, "18,5", "r4w_to_mm",
                                rom, sym, sha1,
                                stop_map_ids=(M_MT_MOON_1F,))
                for _ in range(6):
                    if drv.gs().overworld.map_id == M_MT_MOON_1F:
                        break
                    drv.press("up")
                session.step(120, render=True)
            else:
                # Already on east half — path to Cerulean via a
                # step-by-step walker so trainer post-battle sprite
                # shifts don't invalidate the pre-planned path.
                # Cerulean map-connection runs along Route 4's east
                # edge at col 89. Row 6 there (the "obvious" east exit)
                # is surrounded by walls — tile-unreachable. The
                # actually-walkable rows at col 89 are 10-11 (lower
                # plateau, tile 0x39). Target (89, 10) and let the
                # final RIGHT-mash step off the edge into Cerulean.
                if drv.gs().overworld.x < 89:
                    res = _step_by_step_walk(drv, session, outdir,
                                              "89,10", "r4e_to_cerulean",
                                              rom, sym, sha1,
                                              target_map_id=M_CERULEAN_CITY,
                                              max_presses=300,
                                              extra_blockers=None,
                                              stop_map_ids=(M_CERULEAN_CITY,))
                    print(f"  r4e_to_cerulean: {res} -> "
                          f"{_gs_summary(session)}", flush=True)
                # Final RIGHT-mash to cross the map-connection boundary.
                for _ in range(16):
                    before = (drv.gs().overworld.x, drv.gs().overworld.y)
                    drv.press("right")
                    if drv.gs().overworld.map_id == M_CERULEAN_CITY:
                        break
                    if drv.gs().overworld.map_id != M_ROUTE_4:
                        break
                    if (drv.gs().overworld.x, drv.gs().overworld.y) == before:
                        drv.press("up")
                        drv.press("right")
                        drv.press("down")
                        drv.press("right")
                session.step(60, render=True)
                continue
        # Phase B-D: Mt. Moon warp-puzzle hopping. The Route 4 east
        # exit at B1F (27, 3) is only reachable from the B1F "upper
        # strip" which is isolated from the main cave area by walls.
        # You reach the upper strip by taking B2F (5, 7) -> B1F
        # (23, 3). Getting to B2F (5, 7) requires crossing B2F,
        # which may itself require multiple warp hops. Approach:
        # on each Mt Moon floor, A* to any REACHABLE warp tile (in
        # priority order), walk it, let the game warp, and repeat
        # until we land on Route 4. A visited-warp set breaks
        # A <-> B ping-pong.
        if drv.gs().overworld.map_id in (M_MT_MOON_1F, M_MT_MOON_B1F,
                                          M_MT_MOON_B2F):
            ftb._activate_repel(drv)
            cur_map = drv.gs().overworld.map_id
            if cur_map == M_MT_MOON_1F:
                _mark_trainers_defeated(session,
                                         _MT_MOON_1F_TRAINER_EVENTS,
                                         label="mm1f_pre_solve")
            elif cur_map == M_MT_MOON_B2F:
                _mark_trainers_defeated(session,
                                         _MT_MOON_B2F_TRAINER_EVENTS,
                                         label="mmb2f_pre_solve")
                # Let sprite state settle after warp — fresh-map
                # entry can have sprite slots mid-initialization.
                session.step(60, render=True)
                # Clear fossil+Super Nerd pen so (5, 7) is reachable.
                # No-op if sprites already gone (post-pickup). Must run
                # after pre-solve so Super Nerd skips the forced battle.
                present = _b2f_fossil_sprites_present(session)
                print(f"  mmb2f: fossil sprites present={present}",
                      flush=True)
                if present:
                    ok = _clear_b2f_fossils(drv, session, outdir,
                                             rom, sym, sha1)
                    if not ok:
                        print("  mmb2f: fossil clear failed, warp-hop "
                              "will likely fail too", flush=True)
            warps = _MT_MOON_WARPS_BY_FLOOR[cur_map]
            label = {M_MT_MOON_1F: "mm1f_warp",
                     M_MT_MOON_B1F: "mmb1f_warp",
                     M_MT_MOON_B2F: "mmb2f_warp"}[cur_map]
            res = _try_warp_hop(drv, session, outdir, rom, sym, sha1,
                                warps, label,
                                stop_map_ids=(M_ROUTE_4,),
                                tried_edges=tried_edges)
            print(f"  {label}: {res} -> {_gs_summary(session)}",
                  flush=True)
            session.step(120, render=True)
            if res == "no_warp":
                print(f"  no reachable warps on map 0x{cur_map:02x}; "
                      f"bailing", flush=True)
                return False
    return drv.gs().overworld.map_id == M_CERULEAN_CITY


# --- Phase 6: walk Cerulean -> Cerulean PC -------------------------------

def walk_to_cerulean_pc(drv: rtb.Driver, session: Session, outdir: Path,
                        rom: str, sym: str, sha1: str) -> bool:
    """Walk from the Cerulean City entry tile to the Cerulean PC door
    approach cell (19, 18), then step UP onto the warp at (19, 17)
    which triggers the Pokémon Center transition.

    Uses step-by-step re-A* to route around the wandering Super Nerd
    at (15, 18) (WALK UP_DOWN) — linear walk stalls at (14, 18) when
    he's on the east-bound path."""
    ftb._activate_repel(drv)
    session.step(60, render=True)
    res = _step_by_step_walk(drv, session, outdir, "19,18", "to_cpc",
                              rom, sym, sha1,
                              target_map_id=M_CERULEAN_POKECENTER,
                              max_presses=120,
                              extra_blockers=None,
                              stop_map_ids=(M_CERULEAN_POKECENTER,))
    print(f"  to_cpc: {res} -> {_gs_summary(session)}", flush=True)
    for _ in range(6):
        if drv.gs().overworld.map_id == M_CERULEAN_POKECENTER:
            break
        drv.press("up")
    session.step(60, render=True)
    return drv.gs().overworld.map_id == M_CERULEAN_POKECENTER


# --- Main -----------------------------------------------------------------

PHASES = [
    "exit_gym",
    "pewter_pc",
    "route3_entry",
    "mt_moon_entry",
    "cerulean_entry",
    "cerulean_pc",
]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True,
                   help="Path to a .state to resume from (e.g. brock_badge.state)")
    p.add_argument("--outdir", default="walkthrough_yellow_to_cerulean")
    p.add_argument("--stop-after", default=PHASES[-1], choices=PHASES)
    p.add_argument("--skip-to", default=None, choices=PHASES,
                   help="Skip phases up to (but not including) this one.")
    args = p.parse_args()

    rom = os.environ["POKERED_ROM_PATH"]
    sym = os.environ["POKERED_SYM_PATH"]
    sha1 = os.environ.get("POKERED_ROM_SHA1")
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    session = Session.from_files(rom, sym, expected_rom_sha1=sha1)
    register_default_hooks(session)
    session.load_state(Path(args.start).read_bytes())
    session.step(60, render=True)
    drv = rtb.Driver(session)
    print(f"start: {_gs_summary(session)}", flush=True)

    skip_idx = PHASES.index(args.skip_to) if args.skip_to else 0

    if skip_idx <= PHASES.index("exit_gym"):
        print("\n=== phase: exit_gym ===", flush=True)
        if not exit_gym(drv, session, outdir, rom, sym, sha1):
            print(f"  FAIL exit_gym: {_gs_summary(session)}", flush=True)
            return 1
        print(f"  out: {_gs_summary(session)}", flush=True)
        _save(session, outdir, "exit_gym")
        if args.stop_after == "exit_gym":
            return 0

    if skip_idx <= PHASES.index("pewter_pc"):
        print("\n=== phase: pewter_pc ===", flush=True)
        if not heal_at_pewter_pc(drv, session, outdir, rom, sym, sha1):
            print(f"  FAIL pewter_pc: {_gs_summary(session)}", flush=True)
            return 1
        print(f"  healed: {_gs_summary(session)}", flush=True)
        _save(session, outdir, "pewter_pc")
        if args.stop_after == "pewter_pc":
            return 0

    if skip_idx <= PHASES.index("route3_entry"):
        print("\n=== phase: route3_entry ===", flush=True)
        if not walk_to_route3(drv, session, outdir, rom, sym, sha1):
            print(f"  FAIL route3_entry: {_gs_summary(session)}", flush=True)
            return 1
        print(f"  on route3: {_gs_summary(session)}", flush=True)
        _save(session, outdir, "route3_entry")
        if args.stop_after == "route3_entry":
            return 0

    if skip_idx <= PHASES.index("mt_moon_entry"):
        print("\n=== phase: mt_moon_entry ===", flush=True)
        if not cross_route3(drv, session, outdir, rom, sym, sha1):
            print(f"  FAIL mt_moon_entry: {_gs_summary(session)}", flush=True)
            return 1
        print(f"  on route4: {_gs_summary(session)}", flush=True)
        _save(session, outdir, "mt_moon_entry")
        if args.stop_after == "mt_moon_entry":
            return 0

    print("\n=== phase: cerulean_entry ===", flush=True)
    if not cross_route4(drv, session, outdir, rom, sym, sha1):
        print(f"  FAIL cerulean_entry: {_gs_summary(session)}", flush=True)
        return 1
    print(f"  in cerulean: {_gs_summary(session)}", flush=True)
    _save(session, outdir, "cerulean_entry")
    if args.stop_after == "cerulean_entry":
        return 0

    print("\n=== phase: cerulean_pc ===", flush=True)
    if not walk_to_cerulean_pc(drv, session, outdir, rom, sym, sha1):
        print(f"  FAIL cerulean_pc: {_gs_summary(session)}", flush=True)
        return 1
    print(f"  at cerulean PC: {_gs_summary(session)}", flush=True)
    _save(session, outdir, "cerulean_pc")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
