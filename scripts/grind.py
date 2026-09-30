"""Route 2 heal-loop grinder: level the starter up to a target without
relying on blackout auto-heal.

Callable from any end-to-end pipeline as a phase. Strategy::

    while not done:
        if on Route 2:
            walk into grass, roll a battle
            resolve battle
        if HP low:
            walk Route 2 south → Viridian → Pokecenter
            heal at nurse
            walk Viridian north → Route 2
        elif blacked out (map != Route 2 / Viridian):
            bounce back via navigate_to_viridian_with_retry
        check for target level / target-move learned

The grinder reuses:
  * :class:`run_to_brock.Driver` for battle resolution and heal.
  * ``full_to_brock.run_pathfinder`` subprocess to get A*-computed
    direction strings for intra-map navigation.
  * ``full_to_brock.walk_path`` to execute those direction strings while
    auto-resolving any wild battles fired along the way.

Species-agnostic: all checks read ``wPartyMons[0]`` via the existing
state parser. Moves deemed "damaging" are the union of known useful
damaging moves for Bulbasaur / Squirtle / Charmander / Pikachu.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import full_to_brock as ftb
import run_to_brock as rtb
from _grind_support import (
    DAMAGING_MOVE_IDS,
    GRASS_ANCHOR,
    GRASS_Y_MAX,
    GRASS_Y_MIN,
    M_PALLET,
    M_PEWTER,
    M_REDS_1F,
    M_REDS_2F,
    M_ROUTE_1,
    M_ROUTE_2,
    M_VIRIDIAN,
    M_VIRIDIAN_POKECENTER,
    MOVE_DOUBLE_KICK,
    MOVE_VINE_WHIP,
    ROUTE2_SOUTH_EXIT,
    VIRIDIAN_NORTH_EXIT,
    VIRIDIAN_PC_DOOR,
    GrindResult,
    Session,
    _battle_turn,
    _clear_repel,
    _drive_post_battle_dialogs,
    _ensure_in_grass,
    _force_blackout_heal,
    _hp_fraction,
    _lead,
    _pathfind,
    _resolve_battle_no_blackout_mash,
    _safe_walk,
    _walk_until_battle,
    walk_to_viridian_and_heal,
)

__all__ = [
    "DAMAGING_MOVE_IDS",
    "GRASS_ANCHOR",
    "GRASS_Y_MAX",
    "GRASS_Y_MIN",
    "MOVE_DOUBLE_KICK",
    "MOVE_VINE_WHIP",
    "M_PALLET",
    "M_PEWTER",
    "M_REDS_1F",
    "M_REDS_2F",
    "M_ROUTE_1",
    "M_ROUTE_2",
    "M_VIRIDIAN",
    "M_VIRIDIAN_POKECENTER",
    "ROUTE2_SOUTH_EXIT",
    "VIRIDIAN_NORTH_EXIT",
    "VIRIDIAN_PC_DOOR",
    "GrindResult",
    "Session",
    "_battle_turn",
    "_clear_repel",
    "_drive_post_battle_dialogs",
    "_ensure_in_grass",
    "_force_blackout_heal",
    "_hp_fraction",
    "_lead",
    "_pathfind",
    "_resolve_battle_no_blackout_mash",
    "_safe_walk",
    "_walk_until_battle",
    "ftb",
    "grind_to",
    "main",
    "rtb",
    "walk_to_viridian_and_heal",
]
def grind_to(
    session: Session,
    *,
    outdir: Path,
    rom: str,
    sym: str,
    sha1: str,
    target_level: int = 13,
    target_move_id: int | None = MOVE_VINE_WHIP,
    max_battles: int = 60,
    max_wall_seconds: float = 600.0,
    heal_threshold: float = 0.60,
) -> GrindResult:
    """Drive Route 2 encounters until the starter reaches ``target_level``
    or learns ``target_move_id``.

    Precondition: ``session`` is on Route 2 (map 0x0D) with a healthy
    starter in slot 0. Post: player is on Route 2 ready for the
    ``route2_to_forest`` phase.
    """
    drv = rtb.Driver(session)
    start_time = time.time()
    start_mon = _lead(drv)
    start_level = start_mon.level if start_mon else 0
    battles = 0
    blackouts = 0
    heals = 0
    stopped_reason = "unknown"

    print(f"  [grind] start L{start_level} target L{target_level} "
          f"target_move={target_move_id}", flush=True)

    def _check_done() -> tuple[bool, str]:
        m = _lead(drv)
        if m is None:
            return True, "party_empty"
        if target_move_id is not None and target_move_id in m.moves:
            return True, "target_move"
        if m.level >= target_level:
            return True, "target_level"
        return False, ""

    # Always start by walking into the grass. Then loop: encounter →
    # battle → post-battle triage (heal / blackout recovery / done?).
    map_id = drv.gs().overworld.map_id
    if map_id == M_ROUTE_2:
        _clear_repel(drv)
        _ensure_in_grass(drv, session, outdir, rom, sym, sha1)

    while battles < max_battles:
        if (time.time() - start_time) > max_wall_seconds:
            stopped_reason = "timeout"
            break

        done, reason = _check_done()
        if done:
            stopped_reason = reason
            break

        gs = drv.gs()
        map_id = gs.overworld.map_id

        # Stuck in Viridian PC (e.g. blackout respawn after the first
        # PC heal — Gen 1 remembers the last-healed center as the new
        # blackout location). Pathfind to the south door warp.
        if map_id == M_VIRIDIAN_POKECENTER:
            print("  [grind] in Viridian PC — pathfinding to south exit",
                  flush=True)
            for _ in range(80):
                if not drv.joy_locked():
                    break
                drv.press("a")
            session.step(30, render=True)
            try:
                path = _pathfind(drv, session, outdir, "3,7",
                                  "pc_exit", rom, sym, sha1)
                print(f"  [grind] PC exit A* {len(path)} steps",
                      flush=True)
                _safe_walk(drv, path, label="pc_exit",
                            stop_map_ids=(M_VIRIDIAN,))
            except RuntimeError as e:
                print(f"  [grind] PC pathfind failed: {e}", flush=True)
            # Nudge DOWN to cross the door warp.
            for _ in range(6):
                if drv.gs().overworld.map_id == M_VIRIDIAN:
                    break
                drv.press("down")
            session.step(60, render=True)
            if drv.gs().overworld.map_id != M_VIRIDIAN:
                print(f"  [grind] PC exit failed at "
                      f"xy=({drv.gs().overworld.x},"
                      f"{drv.gs().overworld.y})", flush=True)
                stopped_reason = "pc_exit_failed"
                break
            continue

        # Blackout recovery: any map outside Route 2 / Viridian / PC is
        # definitely a blackout, or we walked into a wrong gate.
        if map_id not in (M_ROUTE_2, M_VIRIDIAN, M_VIRIDIAN_POKECENTER):
            print(f"  [grind] blackout/wrong-map "
                  f"0x{map_id:02x} — recovering", flush=True)
            blackouts += 1
            # Let the blackout map-transition settle before reading the
            # post-whiteout map id. The "blacked out" map is 0x00 briefly
            # during the warp but actually lands us inside Red's House
            # 1F (0x25) after the fade.
            session.step(120, render=True)
            for _ in range(120):
                if not drv.joy_locked():
                    break
                drv.press("a")
            session.step(30, render=True)
            ftb._activate_repel(drv)

            cur_map = drv.gs().overworld.map_id
            print(f"  [grind] post-blackout settled: map=0x{cur_map:02x} "
                  f"xy=({drv.gs().overworld.x},{drv.gs().overworld.y})",
                  flush=True)

            # If we landed in Red's House, exit via walkthrough's
            # canonical bedroom→1F→front-door sequence.
            if cur_map in (M_REDS_1F, M_REDS_2F):
                try:
                    import walkthrough as wt
                    wt_drv = wt.WalkthroughDriver(
                        session=session,
                        outdir=outdir / "_blackout_exit",
                    )
                    wt.run_phase_exit_house(wt_drv)
                except Exception as e:  # noqa: BLE001 - recovery must continue after a best-effort house exit
                    print(f"  [grind] wt.exit_house failed: {e}",
                          flush=True)
                session.step(60, render=True)

            # Now (hopefully) in Pallet Town. Let the ftb helper pathfind
            # through Pallet→Route 1→Viridian. It already handles the
            # Red's-house-reentry hazard by detecting the map before
            # picking a route, and has a 20-attempt retry loop for
            # Route 1 ledge fumbles.
            ok = ftb.navigate_to_viridian_with_retry(
                drv, outdir, rom, sym, sha1, session, max_attempts=6,
            )
            if not ok:
                stopped_reason = "blackout_recovery_failed"
                break
            # Now in Viridian — push up to Route 2.
            drv.run_viridian_to_route2()
            session.step(60, render=True)
            if drv.gs().overworld.map_id != M_ROUTE_2:
                stopped_reason = "cannot_reach_route2"
                break
            _clear_repel(drv)
            _ensure_in_grass(drv, session, outdir, rom, sym, sha1)
            continue

        # If we're in Viridian (e.g. after a heal), walk back north. If
        # we're standing on the PC door tile (23, 25/26) we'd warp
        # straight back INTO the PC on the first UP press; nudge
        # DOWN+LEFT first to escape the door column before the UP zig-
        # zag kicks in.
        if map_id == M_VIRIDIAN:
            gs = drv.gs()
            if gs.overworld.x >= 22 and gs.overworld.y >= 25:
                drv.press("down")
                drv.press("left")
                drv.press("left")
            drv.run_viridian_to_route2()
            session.step(60, render=True)
            if drv.gs().overworld.map_id == M_ROUTE_2:
                _clear_repel(drv)
                _ensure_in_grass(drv, session, outdir, rom, sym, sha1)
            continue

        # On Route 2: walk to trigger an encounter, then resolve.
        _clear_repel(drv)  # idempotent; handles post-heal leftover Repel
        got_battle = _walk_until_battle(drv, max_steps=40)
        if not got_battle:
            # Might have been blocked, ended up out of grass, etc. Loop
            # around — _ensure_in_grass would re-centre if we're on
            # Route 2 still.
            if drv.gs().overworld.map_id == M_ROUTE_2:
                _ensure_in_grass(drv, session, outdir, rom, sym, sha1)
            continue

        fainted = _resolve_battle_no_blackout_mash(
            drv, allow_learn=(target_move_id is not None),
        )
        battles += 1
        # Let post-battle animation/map redraw settle so wPartyCount /
        # wPartyMons read back a stable struct.
        session.step(60, render=True)
        m = _lead(drv)
        elapsed = time.time() - start_time
        map_after = drv.gs().overworld.map_id
        if m is not None:
            print(f"  [grind] battle #{battles} post L{m.level} "
                  f"HP{m.hp}/{m.max_hp} map=0x{map_after:02x} "
                  f"fainted={fainted} t={elapsed:.0f}s", flush=True)
        else:
            print(f"  [grind] battle #{battles} post: no party "
                  f"map=0x{map_after:02x} "
                  f"fainted={fainted} t={elapsed:.0f}s", flush=True)

        if fainted:
            # Blackout handling: the engine mashes through on its own
            # once we keep pressing A. Then recovery path on next loop.
            for _ in range(80):
                if not drv.gs().battle.active and not drv.joy_locked():
                    break
                drv.press("a")
            blackouts += 1
            # Fallthrough — next loop will detect off-map and recover.
            continue

        # Heal if HP fell below threshold. Bail cleanly on failure —
        # the caller (full_to_brock) handles a partial grind result by
        # topping up with Option-B boost rather than letting the whole
        # run time out.
        m = _lead(drv)
        if m is not None and _hp_fraction(m) < heal_threshold:
            print(f"  [grind] HP {m.hp}/{m.max_hp} < "
                  f"{heal_threshold*100:.0f}% — healing", flush=True)
            heals += 1
            healed = walk_to_viridian_and_heal(
                drv, session, outdir, rom, sym, sha1,
            )
            if not healed:
                print("  [grind] heal failed — bailing so the caller "
                      "can top up via Option-B", flush=True)
                stopped_reason = "heal_failed"
                break
            _clear_repel(drv)
            _ensure_in_grass(drv, session, outdir, rom, sym, sha1)

    else:
        stopped_reason = "max_battles"

    final_mon = _lead(drv)
    final_level = final_mon.level if final_mon else 0
    learned_target = (
        target_move_id is not None
        and final_mon is not None
        and target_move_id in final_mon.moves
    )
    hit_level = final_level >= target_level
    wall = time.time() - start_time
    print(f"  [grind] done: L{start_level}->L{final_level} "
          f"battles={battles} blackouts={blackouts} heals={heals} "
          f"reason={stopped_reason} wall={wall:.0f}s", flush=True)
    return GrindResult(
        start_level=start_level,
        final_level=final_level,
        battles=battles,
        blackouts=blackouts,
        heals=heals,
        wall_seconds=wall,
        hit_target_level=hit_level,
        learned_target_move=learned_target,
        stopped_reason=stopped_reason,
    )


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--state", required=True,
        help="Save state to load (e.g. viridian_to_route2.state)",
    )
    parser.add_argument("--target-level", type=int, default=13)
    parser.add_argument("--target-move-id", type=int, default=MOVE_VINE_WHIP)
    parser.add_argument("--max-battles", type=int, default=60)
    parser.add_argument("--max-wall-seconds", type=float, default=600.0)
    parser.add_argument("--heal-threshold", type=float, default=0.40)
    parser.add_argument("--outdir", default="walkthrough_grind")
    parser.add_argument("--out-state", default=None)
    args = parser.parse_args()

    rom = os.environ["POKERED_ROM_PATH"]
    sym = os.environ["POKERED_SYM_PATH"]
    sha1 = os.environ["POKERED_ROM_SHA1"]
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    from pokered_harness.mcp_server import register_default_hooks
    session = Session.from_files(rom, sym, expected_rom_sha1=sha1)
    register_default_hooks(session)
    session.load_state(Path(args.state).read_bytes())
    session.step(60, render=True)

    res = grind_to(
        session,
        outdir=outdir,
        rom=rom, sym=sym, sha1=sha1,
        target_level=args.target_level,
        target_move_id=args.target_move_id,
        max_battles=args.max_battles,
        max_wall_seconds=args.max_wall_seconds,
        heal_threshold=args.heal_threshold,
    )

    print("\n=== GRIND RESULT ===", flush=True)
    print(f"start_level={res.start_level} final_level={res.final_level}",
          flush=True)
    print(f"battles={res.battles} blackouts={res.blackouts} "
          f"heals={res.heals} reason={res.stopped_reason}", flush=True)
    print(f"hit_target_level={res.hit_target_level} "
          f"learned_target_move={res.learned_target_move}", flush=True)

    if args.out_state:
        Path(args.out_state).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out_state).write_bytes(session.save_state())
        print(f"saved state to {args.out_state}", flush=True)

    session.close()
    return 0 if (res.hit_target_level or res.learned_target_move) else 1
if __name__ == "__main__":
    raise SystemExit(main())
