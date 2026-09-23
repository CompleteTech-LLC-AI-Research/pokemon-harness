"""Shared constants and helpers for the Pewter -> Cerulean walkthrough.

Extracted from ``scripts/to_cerulean.py`` (see issue #145).  Holds the
pathfinding wrappers, milestone/save helpers, and the Mt. Moon
trainer-event / warp-hop tables shared by ``_to_cerulean_route3`` and the
``to_cerulean`` entry point.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import full_to_brock as ftb
import run_to_brock as rtb
import trainer_sight_cones

from pokered_harness.session import Session


def _sight_cone_blockers(map_name: str) -> str | None:
    """Compute sight-cone blockers for a given map. Returns a
    semicolon-separated "x,y;x,y;..." string suitable for
    path_from_tiles' --extra-blockers arg, or None if the pret
    clone isn't available. Set ``POKERED_PRET_ROOT`` to the checkout
    containing the Yellow data."""
    configured = os.environ.get("POKERED_PRET_ROOT")
    if not configured:
        return None
    pret = Path(configured).expanduser()
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


def _clear_b2f_fossils(drv: rtb.Driver, session: Session, outdir: Path,
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
            print("  b2f_fossils: could not step off arrival warp",
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
        print("  b2f_fossils: not at (12,7); trying (13,7) as fallback",
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
            print("  b2f_fossils: left B2F on fallback; bailing",
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
        # Defensive: if map reads as 0x00 (pallet-town or null), the
        # game is likely mid-transition (scripted NPC walk, dialog
        # close, etc.). A couple extra frames usually settles it;
        # only if still 0x00 after that do we treat it as a real
        # map change. This avoids spurious "map" returns on Blue/Red
        # color where joy_locked scripted sequences transiently show
        # map=0x00 between frames.
        if gs.overworld.map_id == 0x00 and start_map != 0x00:
            session.step(30, render=True)
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
            # Guard against 0x00 transient: if the "changed" map is
            # null, probably a scripted-movement frame. Wait to see
            # if it settles back to start_map.
            if gs.overworld.map_id == 0x00:
                session.step(60, render=True)
                if drv.gs().overworld.map_id == start_map:
                    continue
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
                       env=env, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"pathfinder failed: {r.stderr}")
    return out_path.read_text().strip()


