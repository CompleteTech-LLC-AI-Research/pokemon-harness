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

from pokered_harness.session import Session
from pokered_harness.mcp_server import register_default_hooks

import run_to_brock as rtb
import full_to_brock as ftb
import trainer_sight_cones


def _sight_cone_blockers(map_name: str) -> str | None:
    """Compute sight-cone blockers for a given map. Returns a
    semicolon-separated "x,y;x,y;..." string suitable for
    path_from_tiles' --extra-blockers arg, or None if the pret
    clone isn't available. Uses dev-machine vendored path; adjust
    if the repo moves."""
    pret = Path("G:/project/pokemon/_vendor/pokeyellow")
    if not (pret / "data" / "maps" / "objects").exists():
        return None
    tiles = trainer_sight_cones.sight_cone_tiles(map_name, pret)
    if not tiles:
        return None
    return ";".join(f"{x},{y}" for x, y in sorted(tiles))


# Mt. Moon 1F trainer event-flag bits. Seven trainers at event
# numbers $571..$577 (all within byte 174 of wEventFlags, bits 1-7).
# Setting these before traversal marks them as already-defeated so
# they don't engage on sight-line, eliminating sight-cone blockers
# entirely. Walk freedom > fight-every-trainer since we just want
# to cross. Trainers: 1 Hiker + 4 Youngsters/Cooltrainers/Supernerd.
_MT_MOON_1F_TRAINER_EVENTS = [0x571, 0x572, 0x573, 0x574,
                               0x575, 0x576, 0x577]
# Mt. Moon B2F: Super Nerd (exit), Jessie&James, 3 Rocket trainers.
# Events $579-$57D (byte 175, bits 1-5). Pre-solve to avoid all
# engagements on the B2F traversal.
_MT_MOON_B2F_TRAINER_EVENTS = [0x579, 0x57A, 0x57B, 0x57C, 0x57D]


# _MT_MOON_WARPS_BY_FLOOR is defined after the Mt Moon map-id
# constants below — see "Mt. Moon warp-hop routing" section.


def _try_warp_hop(drv: rtb.Driver, session: Session, outdir: Path,
                  rom: str, sym: str, sha1: str,
                  warps: list[tuple[int, int]], label: str,
                  stop_map_ids: tuple[int, ...],
                  tried_edges: set[tuple[int, int, int, int, int]]
                  ) -> str:
    """Try each warp in order; walk to the first one A* can reach
    AND we haven't taken from the current position before. Tracks
    edges as (cur_map, cur_x, cur_y, warp_x, warp_y) — this is the
    correct granularity for Mt. Moon's warp maze: the same warp
    tile may be used multiple times from different positions, but
    taking the SAME warp from the SAME position would just loop.

    Uses step-by-step re-A*-planning so NPC wandering (Jessie/James
    auto-movement on B2F, trainer sight-cone approach) doesn't
    desync the plan mid-walk — a critical property for the Mt Moon
    maze where wild encounters can briefly halt the player and a
    pre-planned path goes stale.

    Returns the walk_path result code, or ``"no_warp"`` if no
    warp is reachable + un-tried."""
    gs = drv.gs()
    cur_map = gs.overworld.map_id
    cur_x, cur_y = gs.overworld.x, gs.overworld.y
    for (wx, wy) in warps:
        edge = (cur_map, cur_x, cur_y, wx, wy)
        if edge in tried_edges:
            continue
        # Cheap pre-check: can A* even find the path from here?
        try:
            seed = outdir / f"_{label}_seed.state"
            seed.write_bytes(session.save_state())
            path = _run_pathfinder_ex(seed, f"{wx},{wy}",
                                       outdir / f"_{label}.txt",
                                       rom, sym, sha1, None)
        except RuntimeError:
            continue
        if not path:
            continue
        print(f"  [{label}] ({cur_x},{cur_y}) -> warp ({wx},{wy}) "
              f"A* {len(path)} steps (step-by-step walk)", flush=True)
        tried_edges.add(edge)
        # Step-by-step walk: re-plans per press so Jessie/James
        # movements + brief wild-battle interruptions don't break
        # the plan. Target map is the DESTINATION floor of the warp
        # (which we don't know without decoding warp_event data),
        # so we watch for any map change as "warp fired" signal.
        return _step_by_step_walk(drv, session, outdir,
                                   f"{wx},{wy}", label,
                                   rom, sym, sha1,
                                   target_map_id=None,  # any map change
                                   max_presses=150,
                                   extra_blockers=None,
                                   stop_map_ids=stop_map_ids)
    return "no_warp"


def _b2f_fossil_sprites_present(session: Session) -> bool:
    """Return True if either Mt Moon B2F fossil (DOME or HELIX) is
    still a map-placed object that would block movement.

    We check the pickup event flags (EVENT_GOT_DOME_FOSSIL = $578,
    EVENT_GOT_HELIX_FOSSIL = $57F). Either being set means the player
    has picked up one fossil, which also triggers the
    SUPER_NERD_TAKES_OTHER_FOSSIL script that hides the OTHER fossil
    and moves the Super Nerd aside — so a single pickup event is the
    reliable ground-truth "fossil area cleared" signal.

    Per-frame sprite visibility ($wSpriteStateData1+2 == $FF) is
    unreliable: off-screen sprites are marked $FF for rendering
    culling but still exist and re-appear when the player approaches.
    Only HideObject sets the toggle flag that permanently hides a
    sprite, which is downstream of the pickup event.
    """
    EV_GOT_DOME = 0x578
    EV_GOT_HELIX = 0x57F
    mem = session._pyboy.memory  # type: ignore[attr-defined]
    base = session.symbols.addr_of("wEventFlags")
    for ev in (EV_GOT_DOME, EV_GOT_HELIX):
        byte = int(mem[base + ev // 8]) & 0xFF
        if byte & (1 << (ev % 8)):
            return False  # picked up -> area cleared
    return True


def _clear_b2f_fossils(drv: "rtb.Driver", session: Session, outdir: Path,
                       rom: str, sym: str, sha1: str) -> bool:
    """Pick up DOME_FOSSIL on B2F to unlock the path to (5, 7).

    Mt Moon B2F's fossil platform ((12-13, 6)) is a 2-cell sprite wall
    that severs the compB main chamber (containing warp arrivals at
    (25, 9) and (21, 17)) from the compB alcove containing the
    exit-warp (5, 7). Walking around is impossible — the surrounding
    cells (cols 8, 11/14, rows 5-8) form a pen.

    Sequence:
      1. Pre-set EVENT_BEAT_MT_MOON_EXIT_SUPER_NERD (0x579). Skips the
         forced Super Nerd battle when the player reaches (13, 8) and
         lets the fossil interaction script run directly.
      2. A* to (12, 7) (cell south of DOME_FOSSIL at (12, 6)).
      3. Press UP — bumps into fossil, sets facing to UP (no move).
      4. Press A — opens "You want DOME FOSSIL?" YesNoChoice dialog
         (cursor defaults to YES).
      5. Mash A — confirms YES, gives item, hides DOME_FOSSIL, kicks
         off MOVE_SUPER_NERD cutscene. Super Nerd walks off the
         platform, then SUPER_NERD_TAKES_OTHER_FOSSIL hides HELIX_FOSSIL
         and the Super Nerd sprite. All three blockers gone.

    Preconditions: caller has already marked
    _MT_MOON_B2F_TRAINER_EVENTS (includes 0x579) so the approach won't
    trigger the forced Super Nerd battle. Must be called while on B2F.
    """
    gs = drv.gs()
    if gs.overworld.map_id != M_MT_MOON_B2F:
        print(f"  b2f_fossils: not on B2F (map=0x{gs.overworld.map_id:02x})",
              flush=True)
        return False
    if not _b2f_fossil_sprites_present(session):
        print("  b2f_fossils: sprites already absent, skipping", flush=True)
        return True
    # Safety: re-assert Super Nerd defeated so the fossil script goes
    # straight to the "pick up?" dialog.
    _mark_trainers_defeated(session, [0x579], label="mmb2f_nerd_pre")
    # We arrive on B2F at a warp tile (usually (25, 9) or (21, 17)).
    # Stepping anywhere fine at first since Gen 1 warps only fire on
    # step ONTO the tile, not OFF it. But the walker may accidentally
    # route BACK through a warp during re-planning. To avoid that:
    # 1. Take ONE non-warp step to get off the arrival warp.
    # 2. Then A* to (12, 7) with all B2F warp tiles in --extra-blockers
    #    so no intermediate step targets a warp.
    arrival = (drv.gs().overworld.x, drv.gs().overworld.y)
    print(f"  b2f_fossils: arrival at {arrival}", flush=True)
    warp_cells = {(25, 9), (21, 17), (15, 27), (5, 7)}
    if arrival in warp_cells:
        # Step away — preference order: toward fossil area (west/
        # north). Try in order that's most likely walkable.
        for d in ("left", "up", "down", "right"):
            before = (drv.gs().overworld.x, drv.gs().overworld.y)
            drv.press(d)
            after = (drv.gs().overworld.x, drv.gs().overworld.y)
            if after != before and after not in warp_cells:
                print(f"  b2f_fossils: stepped off arrival warp to "
                      f"{after} via {d}", flush=True)
                break
        else:
            print(f"  b2f_fossils: could not step off arrival warp",
                  flush=True)
            return False
    b2f_warp_blockers = ";".join(f"{x},{y}" for x, y in warp_cells)
    res = _step_by_step_walk(drv, session, outdir, "12,7",
                              "b2f_to_fossil",
                              rom, sym, sha1,
                              target_map_id=None,
                              max_presses=200,
                              extra_blockers=b2f_warp_blockers,
                              stop_map_ids=())
    print(f"  b2f_fossils: walk to (12,7): {res} -> {_gs_summary(session)}",
          flush=True)
    cur_xy = (drv.gs().overworld.x, drv.gs().overworld.y)
    if drv.gs().overworld.map_id != M_MT_MOON_B2F:
        print(f"  b2f_fossils: left B2F (now on 0x{drv.gs().overworld.map_id:02x}"
              f" at {cur_xy}); bailing so outer loop re-warps", flush=True)
        return False
    if cur_xy != (12, 7):
        print(f"  b2f_fossils: not at (12,7); trying (13,7) as fallback",
              flush=True)
        res = _step_by_step_walk(drv, session, outdir, "13,7",
                                  "b2f_to_fossil2",
                                  rom, sym, sha1,
                                  target_map_id=None,
                                  max_presses=150,
                                  extra_blockers=b2f_warp_blockers,
                                  stop_map_ids=())
        cur_xy = (drv.gs().overworld.x, drv.gs().overworld.y)
        if drv.gs().overworld.map_id != M_MT_MOON_B2F:
            print(f"  b2f_fossils: left B2F on fallback; bailing",
                  flush=True)
            return False
        if cur_xy not in {(12, 7), (13, 7)}:
            print(f"  b2f_fossils: could not reach fossil approach "
                  f"cell (at {cur_xy})", flush=True)
            return False
    # Face UP (press up — collision with fossil keeps us in place but
    # rotates facing).
    for _ in range(3):
        drv.press("up")
        session.step(4, render=True)
        if drv.gs().battle.active:
            drv.resolve_battle()
    # Press A — opens fossil dialog.
    drv.press("a")
    session.step(30, render=True)
    # Mash A through:
    #   "You want DOME FOSSIL?" (no-wait), Yes/No menu (cursor on Yes),
    #   "Received DOME FOSSIL!", Super Nerd move cutscene, "Then this
    #   is mine!" closing text. Total ~40 A-presses is overkill but
    #   harmless — dialog A after closure is a no-op.
    for _ in range(60):
        drv.press("a")
        session.step(30, render=True)
        if drv.gs().battle.active:
            drv.resolve_battle()
        # Short-circuit once both fossils gone and no dialog queued.
        if not _b2f_fossil_sprites_present(session):
            break
    session.step(180, render=True)
    ok = not _b2f_fossil_sprites_present(session)
    print(f"  b2f_fossils: cleared={ok} -> {_gs_summary(session)}",
          flush=True)
    return ok


def _mark_trainers_defeated(session: Session, event_nums: list[int],
                             label: str = "trainers") -> None:
    """Set the given wEventFlags bits so each trainer reads as
    already-defeated. Prevents sight-line engagement on maps where
    we just want to cross."""
    mem = session._pyboy.memory  # type: ignore[attr-defined]
    base = session.symbols.addr_of("wEventFlags")
    by_byte: dict[int, int] = {}
    for ev in event_nums:
        bo = ev // 8
        bi = ev % 8
        by_byte[bo] = by_byte.get(bo, 0) | (1 << bi)
    for bo, mask in sorted(by_byte.items()):
        cur = mem[base + bo]
        mem[base + bo] = cur | mask
        print(f"  [{label}] wEventFlags[{bo}] 0x{cur:02x} -> "
              f"0x{mem[base+bo]:02x}", flush=True)


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


# Mt. Moon warp-hop routing. Each list = warp tiles on that floor in
# priority order. Stepping onto (x, y) fires the game's warp logic;
# we A* there and the game handles the floor transition. Visited-
# warp tracking (see _try_warp_hop) prevents infinite A<->B ping-pong.
#
# Mt Moon warp-puzzle routing. The game's cavern pair-collision rules
# (CAVERN $20<->$05 etc.) carve B2F into 4 disconnected regions that
# must be hopped between via warps:
#   R1 (high-ground, cols 24-35 rows 5-11): entered via B2F (25, 9)
#   R2 (05-ground main, incl. fossils + (5, 7)): entered via B2F (21, 17)
#   R3 (bottom alcove, isolated): entered via B2F (15, 27)
#   R4 (exit alcove containing (5, 7)): accessible from R2 via fossil clear
#
# The exit to Route 4 goes through B1F comp4 [(23, 3), (27, 3)], which
# is only reachable by warping B2F (5, 7) -> B1F (23, 3). Getting to
# B2F (5, 7) requires entering R2 (pair-collision-isolated), which is
# reached via B1F (21, 17) in comp3, which is reached via 1F (5, 5).
#
# Dead-end branch: 1F (25, 15) -> B1F (25, 15) [comp1] -> B2F (15, 27)
# [R3 dead end]. Omitted from priority lists.
_MT_MOON_WARPS_BY_FLOOR = {
    M_MT_MOON_1F: [
        (5, 5),    # -> B1F comp3 (contains the (21, 17) warp to R2)
        (17, 11),  # -> B1F comp2 (contains (25, 9) warp to R1, dead-end
                   #   under pair-collisions — kept only as a fallback)
    ],
    M_MT_MOON_B1F: [
        (27, 3),    # Route 4 exit (only reachable from comp4)
        (23, 3),    # comp4 also
        (21, 17),   # -> B2F R2 (contains fossils + (5, 7) exit warp)
        (17, 11),   # -> B2F R1 (fallback, dead-end in R1)
    ],
    M_MT_MOON_B2F: [
        (5, 7),     # exit to B1F comp4 upper strip
        (21, 17),   # back to B1F comp3 (inter-region in compB main)
        (25, 9),    # back to B1F comp2 (R1 only)
    ],
}


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


def _step_by_step_walk(drv: rtb.Driver, session: Session, outdir: Path,
                        goal_xy: str, label: str,
                        rom: str, sym: str, sha1: str,
                        target_map_id: int | None,
                        max_presses: int = 200,
                        extra_blockers: str | None = None,
                        stop_map_ids: tuple[int, ...] = ()) -> str:
    """Walk toward ``goal_xy`` one press at a time, re-A*-planning
    from the current position + current NPC sprite layout after
    every single step. This is slow (each step is ~1 s of pathfinder
    subprocess time) but immune to stale-plan desyncs in maps with
    wandering NPCs (Mt. Moon, caves, indoor floors with patrols).

    Returns ``"map"`` when ``target_map_id`` reached, ``"reached"``
    when goal_xy reached, ``"stuck"`` if no progress over many
    consecutive presses, ``"blackout"`` if we land on Pewter.
    """
    no_progress = 0
    last_xy = (drv.gs().overworld.x, drv.gs().overworld.y)
    start_map = drv.gs().overworld.map_id
    for i in range(max_presses):
        gs = drv.gs()
        if gs.battle.active:
            drv.resolve_battle()
            continue
        if target_map_id is not None and gs.overworld.map_id == target_map_id:
            return "map"
        if gs.overworld.map_id in stop_map_ids:
            return "stop"
        # target_map_id=None means ANY map change counts as success
        # (warp-hop pattern: we A* to a warp tile, walk it, and the
        # moment the game changes map we've triggered the warp).
        if target_map_id is None and gs.overworld.map_id != start_map:
            return "map"
        if gs.overworld.map_id in (M_PEWTER_CITY, M_PEWTER_POKECENTER):
            return "blackout"
        gx, gy = [int(v) for v in goal_xy.split(",")]
        if (gs.overworld.x, gs.overworld.y) == (gx, gy):
            return "reached"
        # B-mash a few times before re-A*ing to close any stale NPC
        # dialog. A-mash-on-stall elsewhere can open adjacent-NPC
        # text which then absorbs all subsequent direction presses —
        # B-press closes those dialogs and restores overworld input.
        for _ in range(3):
            before = (drv.gs().overworld.x, drv.gs().overworld.y)
            drv.press("b")
            if drv.gs().battle.active:
                drv.resolve_battle()
                break
            if (drv.gs().overworld.x, drv.gs().overworld.y) != before:
                break
        seed = outdir / f"_{label}_step.state"
        seed.write_bytes(session.save_state())
        try:
            path = _run_pathfinder_ex(seed, goal_xy,
                                       outdir / f"_{label}_step.txt",
                                       rom, sym, sha1, extra_blockers,
                                       expand_npc_neighbors=False)
        except RuntimeError:
            # No path — blind nudge every direction, retry.
            for d in ("up", "left", "down", "right"):
                before = (drv.gs().overworld.x, drv.gs().overworld.y)
                drv.press(d)
                if drv.gs().battle.active:
                    drv.resolve_battle()
                    break
                if (drv.gs().overworld.x, drv.gs().overworld.y) != before:
                    break
            continue
        if not path:
            return "reached"
        # Take ONE step from the plan.
        d_char = path[0]
        d_name = {"u": "up", "d": "down", "l": "left", "r": "right"}[d_char]
        before = (drv.gs().overworld.x, drv.gs().overworld.y)
        drv.press(d_name)
        if drv.gs().battle.active:
            drv.resolve_battle()
        if drv.joy_locked():
            session.step(120, render=True)
        after = (drv.gs().overworld.x, drv.gs().overworld.y)
        # No A-mash on stall here — would trigger adjacent-NPC dialog
        # and absorb subsequent direction presses. If we're genuinely
        # in dialog we'll clear it via the B-mash at the TOP of the
        # next iteration. Re-A* will naturally pick an alternate
        # neighbor if this direction is permanently walled.
        cur = (drv.gs().overworld.x, drv.gs().overworld.y)
        if cur != last_xy:
            no_progress = 0
            last_xy = cur
        else:
            no_progress += 1
            if no_progress >= 30:
                return "stuck"
    return "stuck"


def _pathfind_walk(drv: rtb.Driver, session: Session, outdir: Path,
                   goal_xy: str, label: str,
                   rom: str, sym: str, sha1: str,
                   stop_map_ids=(),
                   extra_blockers: str | None = None,
                   stall_window: int = 12) -> str:
    """Save state, run A* to ``goal_xy``, walk the path. Returns the
    walk_path result code (``done``/``stop``/``stalled``/etc.).

    ``extra_blockers`` optional string ``"x,y;x,y;..."`` passed through
    to path_from_tiles for pen-avoidance.
    ``stall_window`` forwarded to ftb.walk_path; cave maps with NPCs
    benefit from a larger window."""
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
    return ftb.walk_path(drv, path, label=label,
                         stop_map_ids=stop_map_ids,
                         stall_window=stall_window)


def _run_pathfinder_ex(state_path: Path, goal: str, out_path: Path,
                        rom: str, sym: str, sha1: str,
                        extra_blockers: str | None,
                        expand_npc_neighbors: bool = False) -> str:
    """Wraps ftb.run_pathfinder with optional --extra-blockers and
    --expand-npc-neighbors args."""
    import subprocess
    script = Path(__file__).resolve().parent / "path_from_tiles.py"
    env = dict(os.environ)
    env.update(
        POKERED_ROM_PATH=rom,
        POKERED_SYM_PATH=sym,
        PYTHONPATH=str(Path(__file__).resolve().parent.parent / "src"),
        PYTHONIOENCODING="utf-8",
    )
    if sha1:
        env["POKERED_ROM_SHA1"] = sha1
    kw = ["--state", str(state_path), "--save-path-to", str(out_path),
          "--goal-xy", goal]
    if extra_blockers:
        kw += ["--extra-blockers", extra_blockers]
    if expand_npc_neighbors:
        kw += ["--expand-npc-neighbors"]
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
    use_sbs = True
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
                a_star_progressed = True
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

def _perturb_rng(session: Session, seed_hash: int) -> None:
    """Poke hRandomAdd (0xFFD3) / hRandomSub (0xFFD4) to break
    deterministic NPC-walk cycles across blackout recoveries. Without
    this, each cycle Pikachu traverses identical NPC patterns and
    gets pinned at the same sprite-attractor tiles indefinitely."""
    try:
        mem = session._pyboy.memory  # type: ignore[attr-defined]
        mem[0xFFD3] = (seed_hash * 37 + 123) & 0xFF
        mem[0xFFD4] = (seed_hash * 211 + 17) & 0xFF
    except Exception:
        pass


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
    res = _pathfind_walk(drv, session, outdir, "35,19",
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
                print(f"  mt_moon: too many blackouts; bailing",
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
                            _OFFSET_HP, _OFFSET_STATUS,
                        )
                        base = drv.sym.addr_of("wPartyMons")
                        mem[base + _OFFSET_HP + 0] = 0
                        mem[base + _OFFSET_HP + 1] = 1
                        mem[base + _OFFSET_STATUS] = 1 << 3
                    except Exception:
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
