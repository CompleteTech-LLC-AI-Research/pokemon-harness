"""Yellow post-Brock: Pewter Gym → Cerulean PokéCenter.

Resumes from a Yellow Boulder Badge state (e.g.
``walkthrough_yellow_honest15/milestones/brock_badge.state``) and
walks the canonical post-Brock route:

    Pewter Gym → Pewter PC heal → Route 3 → Mt. Moon (1F → B1F → B2F →
    1F east exit) → Route 4 → Cerulean City → Cerulean PC

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

from pokered_harness.session import Session
from pokered_harness.mcp_server import register_default_hooks

import run_to_brock as rtb
import full_to_brock as ftb


# Map IDs (Yellow / Red / Blue all share these — pokered constants).
M_PEWTER_CITY = 0x02
M_CERULEAN_CITY = 0x03
M_ROUTE_3 = 0x0e
M_ROUTE_4 = 0x0f
M_PEWTER_GYM = 0x36
M_PEWTER_POKECENTER = 0x3A
M_CERULEAN_POKECENTER = 0x40
M_MT_MOON_1F = 0x3B
M_MT_MOON_B1F = 0x3C
M_MT_MOON_B2F = 0x3D

# Maps the cross_route3 loop treats as "left Route 3 by map connection".
# Route 3 connects north to Route 4, so Route 4 (0x0f) is a success exit.
_ROUTE3_EXIT_MAPS = (M_ROUTE_4, M_MT_MOON_1F)


def _save(session: Session, outdir: Path, name: str) -> Path:
    p = outdir / "milestones" / f"{name}.state"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(session.save_state())
    print(f"  saved milestone: {name}", flush=True)
    return p


def _gs_summary(session: Session) -> str:
    gs = session.read_game_state()
    parts = [
        f"map=0x{gs.overworld.map_id:02x}",
        f"xy=({gs.overworld.x},{gs.overworld.y})",
        f"badges=0x{gs.progress.badges_raw:02x}",
    ]
    if gs.party.mons:
        m = gs.party.mons[0]
        parts.append(f"L{m.level} hp={m.hp}/{m.max_hp}")
    return " ".join(parts)


def _pathfind_walk(drv: rtb.Driver, session: Session, outdir: Path,
                   goal_xy: str, label: str,
                   rom: str, sym: str, sha1: str,
                   stop_map_ids=(),
                   extra_blockers: str | None = None) -> str:
    """Save state, run A* to ``goal_xy``, walk the path. Returns the
    walk_path result code (``done``/``stop``/``stalled``/etc.).

    ``extra_blockers`` optional string ``"x,y;x,y;..."`` passed through
    to path_from_tiles for pen-avoidance."""
    seed = outdir / f"_{label}.state"
    seed.write_bytes(session.save_state())
    out_txt = outdir / f"_{label}.txt"
    try:
        path = _run_pathfinder_ex(seed, goal_xy, out_txt,
                                  rom, sym, sha1, extra_blockers)
    except RuntimeError as e:
        print(f"  [{label}] pathfind failed: {e}", flush=True)
        return "pathfail"
    print(f"  [{label}] A* {len(path)} steps -> walking", flush=True)
    return ftb.walk_path(drv, path, label=label, stop_map_ids=stop_map_ids)


def _run_pathfinder_ex(state_path: Path, goal: str, out_path: Path,
                        rom: str, sym: str, sha1: str,
                        extra_blockers: str | None) -> str:
    """Wraps ftb.run_pathfinder with optional --extra-blockers arg."""
    import subprocess
    script = Path(__file__).resolve().parent / "path_from_tiles.py"
    env = dict(os.environ)
    env.update(
        POKERED_ROM_PATH=rom,
        POKERED_SYM_PATH=sym,
        POKERED_ROM_SHA1=sha1,
        PYTHONPATH=str(Path(__file__).resolve().parent.parent / "src"),
        PYTHONIOENCODING="utf-8",
    )
    kw = ["--state", str(state_path), "--save-path-to", str(out_path),
          "--goal-xy", goal]
    if extra_blockers:
        kw += ["--extra-blockers", extra_blockers]
    r = subprocess.run([sys.executable, "-u", str(script), *kw],
                       env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"pathfinder failed: {r.stderr}")
    return out_path.read_text().strip()


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
    # Route 3 trainer-pen terminus tiles. Empirically these are the
    # 1-tile pockets we get pinned in after a trainer's sight-line
    # walks them to engage us. Marking them impassable makes A* route
    # the player to tiles OUTSIDE each trainer's sight cone so engagement
    # happens in a tile where the post-battle layout still has a
    # walkable exit. Fuller sight-cone blocking makes A* return NO PATH
    # (Route 3's corridors are thin enough that sight cones cover all
    # walkable columns at y=5..9).
    route3_pens = "15,8;16,8;14,9;22,8;22,12;24,6"
    # Route 3 connects NORTH to Route 4 (per map header) — Mt. Moon is
    # accessed from Route 4, not Route 3. So the east-exit illusion
    # (player walking off east edge of Route 3) is actually: player
    # walking UP off the north edge into Route 4. Target a step cell
    # on Route 3's top row (sy=0) in the walkable band sx=56..63, where
    # Route 4's south connection attaches. Pressing UP at (60, 0)
    # triggers the map-connection warp to Route 4.
    waypoints = [
        ("30,11", "r3_wp1"),
        ("45,11", "r3_wp1b"),
        ("60,0", "r3_wp_n"),
    ]
    while drv.gs().overworld.map_id not in _ROUTE3_EXIT_MAPS:
        cur_map = drv.gs().overworld.map_id
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
        start_x = drv.gs().overworld.x
        a_star_progressed = False
        for goal, label in waypoints:
            goal_x = int(goal.split(",")[0])
            if drv.gs().overworld.x >= goal_x:
                continue
            if drv.gs().overworld.map_id != M_ROUTE_3:
                break
            res = _pathfind_walk(drv, session, outdir, goal,
                                 f"{label}_b{blackouts}",
                                 rom, sym, sha1,
                                 stop_map_ids=_ROUTE3_EXIT_MAPS,
                                 extra_blockers=route3_pens)
            print(f"  {label}_b{blackouts}: {res} -> "
                  f"{_gs_summary(session)}", flush=True)
            if res in ("done", "stop", "map"):
                a_star_progressed = True
                continue
            # pathfail or stalled: fall through to greedy_east
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
            # Force a blackout via the out-of-battle poison path:
            # poke Pikachu HP=1 + STATUS=POISON, then step. Gen 1's
            # ApplyOutOfBattlePoisonDamage ticks every 4th step; with
            # HP=1 the next tick zeroes us and sets
            # wOutOfBattleBlackout → the overworld loop warps to
            # wLastBlackoutMap. We set wLastBlackoutMap=Pewter so
            # recovery loops back into Route 3 from the west — each
            # cycle the previously-defeated trainers stay defeated and
            # we cover more ground.
            print(f"  stuck at {_gs_summary(session)}; forcing blackout",
                  flush=True)
            try:
                mem = session._pyboy.memory  # type: ignore[attr-defined]
                drv.mem[drv.sym.addr_of("wRepelRemainingSteps")] = 0
                drv.mem[drv.sym.addr_of("wLastBlackoutMap")] = M_PEWTER_CITY
                from pokered_harness.state.party import (
                    _OFFSET_HP, _OFFSET_STATUS,
                )
                base = drv.sym.addr_of("wPartyMons")
                mem[base + _OFFSET_HP + 0] = 0
                mem[base + _OFFSET_HP + 1] = 1
                mem[base + _OFFSET_STATUS] = 1 << 3  # PSN
                print("  poked Pikachu HP=1 + POISON", flush=True)
            except Exception as e:
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


# --- Phase 5: cross Route 4 east -> Cerulean City ------------------------

def cross_route4(drv: rtb.Driver, session: Session, outdir: Path,
                 rom: str, sym: str, sha1: str) -> bool:
    """Walk east across Route 4 to the Cerulean City connection.

    Route 4 is split in two halves by the Mt. Moon mountain range:
      - West half (sx <= 20) where Pikachu emerges from Route 3.
      - East half (sx >= 23) connecting east to Cerulean.
    There is no overworld bridge. To cross you MUST go through
    Mt. Moon: enter via Route 4 warp at (18, 5) → MT_MOON_1F (14, 35),
    path through 1F to B1F warp, through B1F to its east exit at
    (27, 3) → Route 4 warp 3 at (24, 5) — now on east half.
    """
    ftb._activate_repel(drv)
    session.step(120, render=True)
    # Phase A: walk to Route 4 Mt. Moon warp at (18, 5) on west half.
    for _ in range(4):
        before = (drv.gs().overworld.x, drv.gs().overworld.y)
        drv.press("up")
        if drv.gs().battle.active:
            drv.resolve_battle()
        if (drv.gs().overworld.x, drv.gs().overworld.y) != before:
            break
    ftb._activate_repel(drv)
    res = _pathfind_walk(drv, session, outdir, "18,5", "r4w_to_mm",
                         rom, sym, sha1,
                         stop_map_ids=(M_MT_MOON_1F,))
    print(f"  r4w_to_mm: {res} -> {_gs_summary(session)}", flush=True)
    for _ in range(6):
        if drv.gs().overworld.map_id == M_MT_MOON_1F:
            break
        drv.press("up")
    session.step(120, render=True)
    if drv.gs().overworld.map_id != M_MT_MOON_1F:
        print(f"  failed to enter Mt. Moon 1F", flush=True)
        return False

    # Phase B: Mt. Moon 1F → B1F via warp (5, 5). Mt. Moon has
    # wandering NPC trainers so sprite-blocker positions change during
    # walk, causing mid-path stalls. Re-A* from the stuck position on
    # each stall, up to 6 retries.
    ftb._activate_repel(drv)
    for retry in range(6):
        if drv.gs().overworld.map_id != M_MT_MOON_1F:
            break
        res = _pathfind_walk(drv, session, outdir, "5,5",
                              f"mm1f_to_b1f_r{retry}",
                              rom, sym, sha1,
                              stop_map_ids=(M_MT_MOON_B1F,))
        print(f"  mm1f_to_b1f_r{retry}: {res} -> "
              f"{_gs_summary(session)}", flush=True)
        if res in ("done", "stop") or \
                drv.gs().overworld.map_id == M_MT_MOON_B1F:
            break
        ftb._activate_repel(drv)
        session.step(60, render=True)
    for _ in range(6):
        if drv.gs().overworld.map_id == M_MT_MOON_B1F:
            break
        drv.press("up")
    session.step(120, render=True)
    if drv.gs().overworld.map_id != M_MT_MOON_B1F:
        print(f"  failed Mt. Moon 1F → B1F", flush=True)
        return False

    # Phase C: Mt. Moon B1F east exit at (27, 3) → Route 4 (24, 5).
    ftb._activate_repel(drv)
    for retry in range(6):
        if drv.gs().overworld.map_id != M_MT_MOON_B1F:
            break
        res = _pathfind_walk(drv, session, outdir, "27,3",
                              f"mmb1f_to_r4e_r{retry}",
                              rom, sym, sha1,
                              stop_map_ids=(M_ROUTE_4,))
        print(f"  mmb1f_to_r4e_r{retry}: {res} -> "
              f"{_gs_summary(session)}", flush=True)
        if res in ("done", "stop") or \
                drv.gs().overworld.map_id == M_ROUTE_4:
            break
        ftb._activate_repel(drv)
        session.step(60, render=True)
    for _ in range(6):
        if drv.gs().overworld.map_id == M_ROUTE_4:
            break
        drv.press("up")
    session.step(120, render=True)
    if drv.gs().overworld.map_id != M_ROUTE_4:
        print(f"  failed B1F → Route 4 east", flush=True)
        return False

    # Phase D: Route 4 east → Cerulean map-connection.
    ftb._activate_repel(drv)
    for goal, label in [("60,6", "r4e_wp1"), ("89,6", "r4e_wp2")]:
        if drv.gs().overworld.map_id != M_ROUTE_4:
            break
        res = _pathfind_walk(drv, session, outdir, goal, label,
                              rom, sym, sha1,
                              stop_map_ids=(M_CERULEAN_CITY,))
        print(f"  {label}: {res} -> {_gs_summary(session)}", flush=True)
        ftb._activate_repel(drv)
    for _ in range(8):
        if drv.gs().overworld.map_id == M_CERULEAN_CITY:
            break
        drv.press("right")
    session.step(60, render=True)
    return drv.gs().overworld.map_id == M_CERULEAN_CITY


# --- Phase 6: walk Cerulean → Cerulean PC -------------------------------

def walk_to_cerulean_pc(drv: rtb.Driver, session: Session, outdir: Path,
                        rom: str, sym: str, sha1: str) -> bool:
    """A* from Cerulean City entry tile to the Cerulean PC door at
    (19, 18) and step UP through the warp."""
    ftb._activate_repel(drv)
    session.step(60, render=True)
    res = _pathfind_walk(drv, session, outdir, "19,18", "to_cpc",
                         rom, sym, sha1,
                         stop_map_ids=(M_CERULEAN_POKECENTER,))
    print(f"  to_cpc: {res}", flush=True)
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

    print("\n=== phase: exit_gym ===", flush=True)
    if not exit_gym(drv, session, outdir, rom, sym, sha1):
        print(f"  FAIL exit_gym: {_gs_summary(session)}", flush=True)
        return 1
    print(f"  out: {_gs_summary(session)}", flush=True)
    _save(session, outdir, "exit_gym")
    if args.stop_after == "exit_gym":
        return 0

    print("\n=== phase: pewter_pc ===", flush=True)
    if not heal_at_pewter_pc(drv, session, outdir, rom, sym, sha1):
        print(f"  FAIL pewter_pc: {_gs_summary(session)}", flush=True)
        return 1
    print(f"  healed: {_gs_summary(session)}", flush=True)
    _save(session, outdir, "pewter_pc")
    if args.stop_after == "pewter_pc":
        return 0

    print("\n=== phase: route3_entry ===", flush=True)
    if not walk_to_route3(drv, session, outdir, rom, sym, sha1):
        print(f"  FAIL route3_entry: {_gs_summary(session)}", flush=True)
        return 1
    print(f"  on route3: {_gs_summary(session)}", flush=True)
    _save(session, outdir, "route3_entry")
    if args.stop_after == "route3_entry":
        return 0

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
