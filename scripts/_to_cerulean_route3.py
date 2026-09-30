"""Pewter Gym -> Route 3 leg of the Pewter -> Cerulean walkthrough.

Extracted from ``scripts/to_cerulean.py`` (see issue #145).  State and
shared helpers come from ``_to_cerulean_support``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import full_to_brock as ftb
import run_to_brock as rtb
from _to_cerulean_support import (
    _ROUTE3_EXIT_MAPS,
    M_PEWTER_CITY,
    M_PEWTER_POKECENTER,
    M_ROUTE_3,
    M_ROUTE_4,
    _gs_summary,
    _pathfind_walk,
    _step_by_step_walk,
)

from pokered_harness.session import Session

# --- Phase 1: exit gym + heal at Pewter PC --------------------------------

def exit_gym(drv: rtb.Driver, session: Session, outdir: Path,
             rom: str, sym: str, sha1: str) -> bool:
    """A* from current gym xy down to the south warp tile, then step
    onto it to trigger the warp out. Pewter Gym south warp is at
    (4, 13)/(5, 13); A* picks whichever is reachable."""
    res = _pathfind_walk(drv, session, outdir, "4,13", "exit_gym",
                         rom, sym, sha1, stop_map_ids=(M_PEWTER_CITY,))
    print(f"  exit_gym walk: {res}", flush=True)
    # Step DOWN through the warp if not already out
    for _ in range(6):
        if drv.gs().overworld.map_id == M_PEWTER_CITY:
            break
        drv.press("down")
    session.step(60, render=True)
    return drv.gs().overworld.map_id == M_PEWTER_CITY


def heal_at_pewter_pc(drv: rtb.Driver, session: Session, outdir: Path,
                      rom: str, sym: str, sha1: str) -> bool:
    """A* to Pewter PC entrance, talk to nurse, exit south."""
    # Pewter PC entry warp is around (13, 26) — door tile that warps to
    # PEWTER_POKECENTER. We path to the tile DIRECTLY in front of it
    # (13, 27) then step UP onto the warp.
    res = _pathfind_walk(drv, session, outdir, "13,27", "to_pewter_pc",
                         rom, sym, sha1)
    print(f"  to_pewter_pc result: {res}", flush=True)
    # Cross into PC
    for _ in range(6):
        if drv.gs().overworld.map_id == M_PEWTER_POKECENTER:
            break
        drv.press("up")
    session.step(60, render=True)
    if drv.gs().overworld.map_id != M_PEWTER_POKECENTER:
        print(f"  WARN failed entry to Pewter PC "
              f"(map=0x{drv.gs().overworld.map_id:02x})", flush=True)
        return False
    # Walk up to nurse and talk
    for _ in range(6):
        drv.press("up")
    drv.press("a")
    for _ in range(30):
        mx = drv.sym.read_u8(drv.mem, "wMaxMenuItem")
        if mx == 1 and not drv.gs().text.dest_in_vram_tilemap:
            break
        drv.press("a")
    drv.press("a", step=60)
    for _ in range(60):
        try:
            m = drv.gs().party.mons[0]
            if m.hp == m.max_hp:
                break
        except IndexError:
            pass
        drv.press("a")
    for _ in range(15):
        drv.press("b", step=60)
    # Exit south
    for _ in range(8):
        if drv.gs().overworld.map_id == M_PEWTER_CITY:
            break
        drv.press("down")
    session.step(60, render=True)
    return drv.gs().overworld.map_id == M_PEWTER_CITY


# --- Phase 2: cross Pewter east -> Route 3 --------------------------------

def walk_to_route3(drv: rtb.Driver, session: Session, outdir: Path,
                   rom: str, sym: str, sha1: str) -> bool:
    """A* east across Pewter to the Route 3 entry tile.

    Route 3 entry from Pewter is the east warp around (33, 19) -
    crossing it lands the player on Route 3 at (0, 4).
    """
    ftb._activate_repel(drv)
    # Pewter east-to-Route-3 warp is around (35, 19) — pathfind to the
    # tile JUST west of the warp then step RIGHT through.
    res = _pathfind_walk(drv, session, outdir, "35,19", "pewter_east",
                         rom, sym, sha1, stop_map_ids=(M_ROUTE_3,))
    print(f"  pewter_east result: {res}", flush=True)
    # Cross the east warp
    for _ in range(8):
        if drv.gs().overworld.map_id == M_ROUTE_3:
            break
        drv.press("right")
    session.step(60, render=True)
    return drv.gs().overworld.map_id == M_ROUTE_3


# --- Phase 3: cross Route 3 east -> Mt. Moon entry -----------------------

def _recover_to_route3(drv: rtb.Driver, session: Session, outdir: Path,
                       rom: str, sym: str, sha1: str) -> bool:
    """After a blackout that dumped us into Pewter (or its PC), walk
    south out of the PC if needed, settle the engine, then A* east
    back into Route 3."""
    # Settle longer than usual — blackout warp transitions leave the
    # tileset / WRAM in a transient state that breaks save_state
    # snapshots used by the pathfinder subprocess.
    session.step(180, render=True)
    # Brute-mash A: blackout-warp can land us face-to-face with the
    # Pewter PC bench-guy NPC, who fires multi-page dialog
    # ("Any POKEMON that takes part in battle, however short, earns
    #  EXP! ..."). Yellow doesn't surface this through
    # ``text.dest_in_vram_tilemap`` or ``joy_locked``, so the standard
    # "stop on no text" guard exits early. Detection: try a SOUTH step
    # after each A — if it moves us, dialog cleared; if not, more A.
    cleared = False
    for _ in range(80):
        gs = drv.gs()
        if gs.battle.active:
            drv.resolve_battle()
            continue
        before = (drv.gs().overworld.x, drv.gs().overworld.y)
        drv.press("a")
        # Probe: try to step DOWN. If movement happens, dialog is gone.
        drv.press("down")
        if (drv.gs().overworld.x, drv.gs().overworld.y) != before:
            cleared = True
            break
    if not cleared:
        print("  recover: dialog mash didn't free us; bailing", flush=True)
    session.step(60, render=True)
    gs = drv.gs()
    if gs.overworld.map_id == M_PEWTER_POKECENTER:
        for _ in range(8):
            if drv.gs().overworld.map_id == M_PEWTER_CITY:
                break
            drv.press("down")
        session.step(60, render=True)
    if drv.gs().overworld.map_id != M_PEWTER_CITY:
        print(f"  recover: not on Pewter (map=0x"
              f"{drv.gs().overworld.map_id:02x}); giving up", flush=True)
        return False
    # If blackout left us in front of a building entrance, take a
    # cardinal step (south usually opens up the city) to clear any
    # lingering screen-edge transition.
    for _ in range(4):
        before = (drv.gs().overworld.x, drv.gs().overworld.y)
        drv.press("up")
        if drv.gs().battle.active:
            drv.resolve_battle()
        if (drv.gs().overworld.x, drv.gs().overworld.y) != before:
            break
    session.step(60, render=True)
    ftb._activate_repel(drv)
    for retry in range(3):
        res = _pathfind_walk(drv, session, outdir, "35,19",
                             f"recover_east_r{retry}",
                             rom, sym, sha1, stop_map_ids=(M_ROUTE_3,))
        print(f"  recover_east_r{retry}: {res}", flush=True)
        if res in ("done", "stop"):
            break
        session.step(180, render=True)
    for _ in range(8):
        if drv.gs().overworld.map_id == M_ROUTE_3:
            break
        drv.press("right")
    session.step(60, render=True)
    return drv.gs().overworld.map_id == M_ROUTE_3


def _greedy_east(drv: rtb.Driver, session: Session,
                 target_x: int, target_map_id: int | None = None,
                 max_steps: int = 400) -> str:
    """Walk east greedily, handling obstacles by trying alternate
    directions. Returns ``"reached"`` if x>=target_x, ``"map"`` if
    target_map_id matched, ``"blackout"`` if we landed on Pewter,
    ``"stuck"`` if no progress for many tries.

    Key mechanic: when a trainer's sight-line catches us, the game
    sets wJoyIgnore and auto-walks the trainer toward our tile. This
    takes several seconds and our direction/A presses are ignored.
    We detect joy_locked and idle the emulator instead — letting the
    trainer-approach animation complete naturally. Once battle fires
    we resolve it via drv.resolve_battle (which delegates to the
    grind battle-turn logic with Double Kick + ThunderShock).
    """
    no_progress = 0
    last_x = drv.gs().overworld.x
    for _ in range(max_steps):
        gs = drv.gs()
        if gs.battle.active:
            drv.resolve_battle()
            continue
        if (gs.party.mons and gs.party.mons[0].hp == 0
                and gs.overworld.map_id != M_ROUTE_3):
            return "blackout"
        if gs.overworld.map_id in (M_PEWTER_CITY, M_PEWTER_POKECENTER):
            return "blackout"
        if target_map_id is not None and gs.overworld.map_id == target_map_id:
            return "map"
        if gs.overworld.x >= target_x and gs.overworld.map_id == M_ROUTE_3:
            return "reached"
        # Trainer-approach detection: wJoyIgnore locks all input while
        # the trainer's sprite walks toward us. Just idle the emulator
        # so that auto-walk completes and a battle eventually fires.
        if drv.joy_locked():
            session.step(120, render=True)
            continue
        # Try directions in priority: east, then alternate UD to dodge
        # sprite blockers, then push through dialog with A.
        before = (gs.overworld.x, gs.overworld.y)
        for d in ("right", "up", "right", "down", "right",
                  "down", "right", "up"):
            drv.press(d)
            if drv.gs().battle.active:
                drv.resolve_battle()
                break
            if drv.joy_locked():
                session.step(120, render=True)
                break
            after = (drv.gs().overworld.x, drv.gs().overworld.y)
            if after != before:
                break
        # If pressing didn't move us, mash A in case of dialog
        after = (drv.gs().overworld.x, drv.gs().overworld.y)
        if after == before and not drv.joy_locked():
            for _ in range(8):
                drv.press("a")
                if drv.gs().battle.active:
                    drv.resolve_battle()
                    break
                if (drv.gs().overworld.x, drv.gs().overworld.y) != before:
                    break
        # Track east progress
        cur_x = drv.gs().overworld.x
        if cur_x > last_x:
            no_progress = 0
            last_x = cur_x
        else:
            no_progress += 1
            if no_progress >= 25:
                return "stuck"
    return "stuck"


def cross_route3(drv: rtb.Driver, session: Session, outdir: Path,
                 rom: str, sym: str, sha1: str,
                 max_blackout_recoveries: int = 20) -> bool:
    """A* east across Route 3 to the Mt. Moon 1F entrance.

    Route 3 east edge warps to MT_MOON_1F (0x3B). Trainer sight-lines
    along the corridor block direct A* paths (defeated trainers stay
    as sprite blockers in the seed state until we actually fight them).
    Solution: iterative chunk pathfinding — A* to a sequence of
    waypoints east, walking each chunk and resolving any trainer
    battles that fire en route.

    Blackout recovery: Pikachu at L16 with Double Kick can take down
    individual trainers but multiple back-to-back fights drain HP.
    On blackout we land in Pewter PC fully healed; walk back through
    Pewter east into Route 3 and continue from where we left off (the
    defeated trainers' sprite blockers are gone, so each cycle covers
    new ground).
    """
    ftb._activate_repel(drv)
    blackouts = 0
    # Route 3 pen avoidance. An L53 Pikachu crushes all Route 3
    # trainers in 1-2 hits, so the "pinned after battle" concern is
    # tolerable — we fight, win, and walk on. The pens were added for
    # an L16 Pikachu that needed to minimize fights. With overlevel
    # Pikachu the pens sometimes over-restrict A* and leave us unable
    # to find ANY path east. Disable for now; if we ever grind honestly
    # to L15-16, re-enable.
    route3_pens = None
    # Route 3 connects NORTH to Route 4 (per map header) — Mt. Moon is
    # accessed from Route 4, not Route 3. So the east-exit illusion
    # (player walking off east edge of Route 3) is actually: player
    # walking UP off the north edge into Route 4. Target a step cell
    # on Route 3's top row (sy=0) in the walkable band sx=56..63, where
    # Route 4's south connection attaches. Pressing UP at (60, 0)
    # triggers the map-connection warp to Route 4.
    # Route 3 is mostly open once you're past the initial trainer
    # gauntlet. With an overleveled starter, A* straight to (60, 0)
    # (the north map-connection tile to Route 4) usually works on the
    # first try — intermediate waypoints are a fallback for when the
    # full plan can't be computed due to sprite blockers.
    waypoints = [
        ("60,0", "r3_wp_n"),
        ("45,11", "r3_wp1b"),
        ("30,11", "r3_wp1"),
    ]
    # Use step-by-step re-A* instead of linear walk_path. Route 3's
    # trainer sight cones cause post-battle sprite shifts that
    # invalidate pre-planned paths mid-walk; re-planning per step
    # routes around the new sprite positions.
    # Override via TO_CERULEAN_R3_LINEAR=1 for ROMs where step-by-step
    # loops trigger spurious map transitions to 0x00 (observed on
    # Blue color after Option-B boost).
    use_sbs = os.environ.get("TO_CERULEAN_R3_LINEAR") != "1"
    while drv.gs().overworld.map_id not in _ROUTE3_EXIT_MAPS:
        cur_map = drv.gs().overworld.map_id
        # Transient map=0x00: game mid-transition. Wait and retry
        # before treating as a real map change.
        if cur_map == 0x00:
            session.step(60, render=True)
            cur_map = drv.gs().overworld.map_id
            if cur_map == 0x00:
                print("  map still 0x00 after settle; bailing",
                      flush=True)
                return False
        if cur_map in (M_PEWTER_CITY, M_PEWTER_POKECENTER):
            if blackouts >= max_blackout_recoveries:
                print(f"  too many blackouts ({blackouts}); bailing",
                      flush=True)
                return False
            blackouts += 1
            print(f"  blackout #{blackouts} -> recovering to Route 3",
                  flush=True)
            if not _recover_to_route3(drv, session, outdir, rom, sym, sha1):
                return False
            ftb._activate_repel(drv)
            continue
        if cur_map != M_ROUTE_3:
            print(f"  unexpected map=0x{cur_map:02x}; bailing", flush=True)
            return False
        # Try A* waypoints first, then greedy_east as fallback.
        ftb._activate_repel(drv)

        for goal, label in waypoints:
            goal_x = int(goal.split(",")[0])
            goal_y = int(goal.split(",")[1])
            cur_x = drv.gs().overworld.x
            cur_y = drv.gs().overworld.y
            # Skip waypoint if already past its x AND near-or-past its y
            # (don't skip (60, 0) just because x>=60).
            if cur_x >= goal_x and abs(cur_y - goal_y) <= 2 and goal_y > 0:
                continue
            if drv.gs().overworld.map_id != M_ROUTE_3:
                break
            if use_sbs:
                # Step-by-step re-A*: robust to trainer post-battle
                # sprite shifts but 10-50x slower than linear walk.
                res = _step_by_step_walk(drv, session, outdir, goal,
                                          f"{label}_b{blackouts}",
                                          rom, sym, sha1,
                                          target_map_id=None,
                                          max_presses=300,
                                          extra_blockers=route3_pens,
                                          stop_map_ids=_ROUTE3_EXIT_MAPS)
            else:
                res = _pathfind_walk(drv, session, outdir, goal,
                                     f"{label}_b{blackouts}",
                                     rom, sym, sha1,
                                     stop_map_ids=_ROUTE3_EXIT_MAPS,
                                     extra_blockers=route3_pens)
            print(f"  {label}_b{blackouts}: {res} -> "
                  f"{_gs_summary(session)}", flush=True)
            if res in ("done", "stop", "map", "reached"):
                # If we reached the final north-edge waypoint (60, 0),
                # try the UP press now to trigger the Route 4 map
                # connection. Otherwise subsequent waypoints may walk
                # us back south/west.
                if goal == "60,0" and drv.gs().overworld.map_id == M_ROUTE_3:
                    for _ in range(8):
                        if drv.gs().overworld.map_id != M_ROUTE_3:
                            break
                        drv.press("up")
                    session.step(60, render=True)
                    if drv.gs().overworld.map_id in _ROUTE3_EXIT_MAPS:
                        break
                continue
            # pathfail/stalled/stuck: fall through to greedy_east
            break
        if drv.gs().overworld.map_id != M_ROUTE_3:
            continue
        # Greedy fallback from current position.
        # For Route 3, the "east exit" is actually the NORTH map-
        # connection to Route 4. Set target_x very high and rely on
        # target_map_id to detect the transition once A* drops us
        # onto sy=0 and the next UP press wraps to Route 4.
        res = _greedy_east(drv, session, target_x=139,
                           target_map_id=M_ROUTE_4, max_steps=400)
        print(f"  greedy east: {res} -> {_gs_summary(session)}",
              flush=True)
        if res == "blackout":
            continue  # outer loop handles recovery
        if res == "stuck":
            # Pinned at a trainer sight-cone exit with no east escape.
            # The defeated trainer's sprite blocks the tile to the
            # south; north/east are walls. West is the ONLY exit but
            # moves us AWAY from the goal, so greedy-east never tries
            # it. Manually step west 2-3 tiles to escape the pen,
            # then re-A* from the new position.
            stuck_xy = (drv.gs().overworld.x, drv.gs().overworld.y)
            print(f"  greedy stuck at {stuck_xy}; stepping WEST to "
                  f"escape trainer pin", flush=True)
            escape_moved = False
            for _ in range(4):
                before = (drv.gs().overworld.x, drv.gs().overworld.y)
                drv.press("left")
                if drv.gs().battle.active:
                    drv.resolve_battle()
                if drv.joy_locked():
                    session.step(120, render=True)
                    continue
                after = (drv.gs().overworld.x, drv.gs().overworld.y)
                if after != before:
                    escape_moved = True
                    # Also step south once to bypass sight cone.
                    drv.press("down")
                    if drv.gs().battle.active:
                        drv.resolve_battle()
            print(f"  after west-escape: {_gs_summary(session)}", flush=True)
            if escape_moved:
                # Re-try A* to the goal from new position.
                esc = _pathfind_walk(drv, session, outdir,
                                      "60,0", f"r3_escape_b{blackouts}",
                                      rom, sym, sha1,
                                      stop_map_ids=_ROUTE3_EXIT_MAPS,
                                      extra_blockers=None)
                print(f"  r3_escape: {esc} -> {_gs_summary(session)}",
                      flush=True)
                if esc in ("done", "stop", "map"):
                    continue
                if drv.gs().overworld.map_id in _ROUTE3_EXIT_MAPS:
                    break
            # Escape failed too. Force-blackout fallback (existing
            # poison-tick cheat). This is a harness-level escape
            # hatch when A* AND greedy both can't make progress.
            print(f"  stuck at {_gs_summary(session)}; forcing blackout",
                  flush=True)
            try:
                mem = session._pyboy.memory  # type: ignore[attr-defined]
                drv.mem[drv.sym.addr_of("wRepelRemainingSteps")] = 0
                drv.mem[drv.sym.addr_of("wLastBlackoutMap")] = M_PEWTER_CITY
                from pokered_harness.state.party import (
                    _OFFSET_HP,
                    _OFFSET_STATUS,
                )
                base = drv.sym.addr_of("wPartyMons")
                mem[base + _OFFSET_HP + 0] = 0
                mem[base + _OFFSET_HP + 1] = 1
                mem[base + _OFFSET_STATUS] = 1 << 3  # PSN
                print("  poked Pikachu HP=1 + POISON", flush=True)
            except (AttributeError, LookupError, TypeError) as e:
                print(f"  poke failed: {e}", flush=True)
            forced = False
            for _ in range(120):
                gs = drv.gs()
                if gs.overworld.map_id != M_ROUTE_3:
                    forced = True
                    break
                if gs.battle.active:
                    drv.resolve_battle()
                    continue
                # Just step in any direction; poison ticks per step
                # count, not terrain. Dialog/A-mash handles trainer
                # interrupts.
                for d in ("up", "down", "right", "left"):
                    drv.press(d)
                    if drv.gs().battle.active:
                        break
                    if drv.gs().overworld.map_id != M_ROUTE_3:
                        break
                # If nothing moved, A-mash to advance any dialog
                drv.press("a")
            if not forced:
                print("  force-blackout failed; bailing", flush=True)
                return False
            # Settle on new map
            session.step(300, render=True)
            print(f"  blackout landed at {_gs_summary(session)}",
                  flush=True)
            continue
        # Greedy east-walker returned "reached" or "map" — fall through
        # to the warp-cross step below.
        if drv.gs().overworld.map_id in _ROUTE3_EXIT_MAPS:
            break
    # Cross the north map-connection warp (press UP off the top row
    # of Route 3 into Route 4).
    for _ in range(8):
        if drv.gs().overworld.map_id in _ROUTE3_EXIT_MAPS:
            break
        drv.press("up")
    session.step(60, render=True)
    return drv.gs().overworld.map_id in _ROUTE3_EXIT_MAPS
