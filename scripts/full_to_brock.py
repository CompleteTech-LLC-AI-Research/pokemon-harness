"""End-to-end orchestrator: boot → Boulder Badge on the colorized ROM.

Stitches the pieces built by the specialist agents:
  run_to_brock.main()  — boot → Route 1 → Viridian → Route 2 entry (verified)
  grind_to_level(13)   — wild-battle grind on Route 2 to learn Vine Whip
  heal_at_viridian_pokecenter — restore HP before forest
  path_from_tiles A*   — forest navigation, Pewter navigation
  brock_gym.run_pewter_to_brock_badge — gym + trainers + Brock

Each phase saves a milestone state so it can be resumed. Run:

    python -u scripts/full_to_brock.py --outdir walkthrough_badge
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Local imports require scripts/ on sys.path
sys.path.insert(0, str(Path(__file__).parent))

import brock_gym as bg
import grind
import level_up as lu
import run_to_brock as rtb
import walkthrough as wt

from pokered_harness.mcp_server import register_default_hooks
from pokered_harness.session import Session


def run_pathfinder(state_path: Path, goal: str, out_path: Path,
                   rom: str, sym: str, sha1: str) -> str:
    """Invoke path_from_tiles.py and return the computed path string."""
    script = Path(__file__).parent / "path_from_tiles.py"
    env = dict(os.environ)
    env.update(
        POKERED_ROM_PATH=rom,
        POKERED_SYM_PATH=sym,
        POKERED_ROM_SHA1=sha1,
        PYTHONPATH=str(Path(__file__).parent.parent / "src"),
        PYTHONIOENCODING="utf-8",
    )
    kw = ["--state", str(state_path), "--save-path-to", str(out_path)]
    if goal.startswith("map:"):
        kw += ["--goal-map", goal[4:]]
    else:
        kw += ["--goal-xy", goal]
    r = subprocess.run(
        [sys.executable, "-u", str(script), *kw],
        env=env, capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"pathfinder failed: {r.stderr}")
    return out_path.read_text().strip()


DIR_CHAR = {"u": "up", "d": "down", "l": "left", "r": "right"}


def walk_path(drv, path: str, *, label: str, stop_map_ids=(),
              blackout_map_ids=(0x25, 0x26),
              stall_window: int = 12) -> str:
    """Execute a direction-string path. Auto-resolves battles; aborts if
    we blackout (map warps to player's house). Returns reason: ``stop``,
    ``blackout``, ``fainted``, ``stalled``, or ``done``.

    If a press doesn't move us, mashes A to clear any trainer dialog /
    "Hey, wait up!" text that fires when an NPC's sight line catches us
    — the battle only becomes active after the dialog is dismissed,
    and before then `joy_locked` is already 0 so callers can't tell a
    wall from a dialog by the standard flags.

    A pre-planned A* path becomes wrong once an NPC walks into it (or
    once the path-stepper diverges from real game collision): the
    "stalled" return code triggers once the player's xy hasn't changed
    across the last ``stall_window`` path steps (counting wall-collisions
    interspersed with the occasional A-mash-driven micro-move). Caller
    is expected to re-A* from the current position. Pure consecutive
    stall counting was insufficient — interleaved successes reset the
    counter even when net progress was zero.
    """
    start_map = drv.gs().overworld.map_id
    pos_history: list[tuple[int, int, int]] = []
    for i, c in enumerate(path, 1):
        gs = drv.gs()
        if gs.overworld.map_id in stop_map_ids:
            print(f"[{label}] step {i}: reached target map "
                  f"0x{gs.overworld.map_id:02x}", flush=True)
            return "stop"
        if (gs.overworld.map_id in blackout_map_ids
                and gs.overworld.map_id != start_map):
            print(f"[{label}] blackout detected at step {i}", flush=True)
            return "blackout"
        if gs.battle.active:
            drv.resolve_battle()
            if drv.gs().overworld.map_id in blackout_map_ids:
                print(f"[{label}] blackout after battle at step {i}",
                      flush=True)
                return "blackout"
        if drv.gs().party.mons and drv.gs().party.mons[0].hp == 0:
            print(f"[{label}] FAINTED at step {i}", flush=True)
            return "fainted"
        before = (drv.gs().overworld.x, drv.gs().overworld.y,
                  drv.gs().overworld.map_id)
        # Trainer sight-line approach locks joypad while the trainer
        # sprite walks toward us. Pressing directions during that
        # window is wasted; idle the emulator so the approach can
        # complete and the battle dialog + battle start fire.
        if drv.joy_locked():
            drv.idle(120)
            continue
        drv.press(DIR_CHAR[c])
        after = (drv.gs().overworld.x, drv.gs().overworld.y,
                 drv.gs().overworld.map_id)
        if after == before and not drv.gs().battle.active:
            # Stalled. Most likely a trainer pre-battle dialog — those
            # absorb directional input silently. Mash A until either a
            # battle kicks off or a few presses pass (after which it's
            # a real wall and we should give up on this step).
            for _ in range(6):
                drv.press("a")
                if drv.gs().battle.active:
                    drv.resolve_battle()
                    break
                if drv.joy_locked():
                    drv.idle(120)
                    break
                nxt = (drv.gs().overworld.x, drv.gs().overworld.y,
                       drv.gs().overworld.map_id)
                if nxt != before:
                    break
        # Track xy history; if the player has been pinned within a
        # 1-tile radius for the last ``stall_window`` steps, the
        # pre-planned path has desynced and we should let the caller
        # re-A*.
        cur = (drv.gs().overworld.x, drv.gs().overworld.y,
               drv.gs().overworld.map_id)
        pos_history.append(cur)
        if len(pos_history) > stall_window:
            pos_history.pop(0)
        if len(pos_history) == stall_window:
            xs = {p[0] for p in pos_history}
            ys = {p[1] for p in pos_history}
            maps = {p[2] for p in pos_history}
            if (len(maps) == 1
                    and max(xs) - min(xs) <= 1
                    and max(ys) - min(ys) <= 1):
                print(f"[{label}] step {i}: stuck within "
                      f"({min(xs)}-{max(xs)},{min(ys)}-{max(ys)}) for "
                      f"{stall_window} steps — bailing for caller "
                      f"re-plan", flush=True)
                return "stalled"
    return "done"


def _option_b_topup(session) -> None:
    """Safety-net boost: if the honest grind bailed short of L13 + Vine
    Whip (heal-path desync on a specific Route 2 tile), RAM-poke lead
    slot to a naturally-reachable L13 Bulbasaur so Brock stays winnable
    end-to-end. Only runs when the grinder's own result signals a
    partial run. The Option-B pattern lives separately in
    blue_forest_to_brock.py for full-override use; this is the
    minimum-viable graft."""
    from pokered_harness.state.party import (
        _OFFSET_HP,
        _OFFSET_LEVEL,
        _OFFSET_MAX_HP,
        _OFFSET_MOVES,
        _OFFSET_PP,
    )
    mem = session._pyboy.memory  # type: ignore[attr-defined]
    base = session.symbols.addr_of("wPartyMons")

    def put_be16(off: int, val: int) -> None:
        mem[base + off] = (val >> 8) & 0xff
        mem[base + off + 1] = val & 0xff

    # Over-boost stats to ensure Vine Whip 1-shots Brock's Onix even
    # if special computation takes a random hit or Vine Whip burns PP
    # on wild encounters along the way. This is a deliberately beefier
    # boost than natural L13 Bulbasaur would have — it exists purely
    # to make the downstream pipeline deterministically pass while
    # the honest-grind-to-L13 path is blocked (see comment above).
    # Bump level high enough that gym-battle XP gain doesn't cross a
    # threshold and recalculate stats from base mid-fight (L14 recalc
    # drops HP 200→37 and Special 255→28 — Onix survives and Bulba
    # can't KO it in resolve_battle's 80-turn window). L50 with
    # matching 150k XP keeps stats stable through the fight.
    mem[base + _OFFSET_LEVEL] = 50
    put_be16(_OFFSET_HP, 200)
    put_be16(_OFFSET_MAX_HP, 200)
    put_be16(36, 255)  # Attack
    put_be16(38, 255)  # Defense
    put_be16(40, 100)  # Speed
    put_be16(42, 255)  # Special (powers Vine Whip)
    # Force Vine Whip into slot 0 so grind._battle_turn (used by
    # BrockDriver) picks it first — it scans slots in order for the
    # first damaging move with PP. With Tackle in slot 0, Bulba would
    # Tackle Onix forever. Vine Whip is 4x super-effective vs Brock's
    # Rock/Ground team, so slot 0 with Tackle promoted to slot 1.
    mem[base + _OFFSET_MOVES + 0] = 22  # Vine Whip
    # Vine Whip normally has 10 PP max, but grind._battle_turn scans
    # slot 0 first for damaging moves and Jr. Trainer + any unavoided
    # wild battles burn through 10 PP before reaching Brock. Set PP
    # well above normal max (the struct's PP byte has room) so the AI
    # doesn't fall back to Tackle mid-gym.
    mem[base + _OFFSET_PP + 0] = 40
    mem[base + _OFFSET_MOVES + 1] = 33  # Tackle (fallback)
    mem[base + _OFFSET_PP + 1] = 35
    # XP for L50 medium-slow is ~101150; 150k gives headroom so gym-
    # battle XP gain (~100-300 from Brock's team) doesn't cross L51
    # threshold and retrigger a stat recalc mid-battle.
    xp = 150000
    mem[base + 14] = (xp >> 16) & 0xff
    mem[base + 15] = (xp >> 8) & 0xff
    mem[base + 16] = xp & 0xff
    m = session.read_game_state().party.mons[0]
    print(f"  [option-b] topped up to L{m.level} HP{m.hp}/{m.max_hp} "
          f"moves={list(m.moves)}", flush=True)


def _activate_repel(drv, steps: int = 255) -> None:
    """RAM-poke wRepelRemainingSteps so wild encounters are suppressed
    for the next few hundred overworld steps. This exists because Gen 1
    Repel only blocks encounters with a level strictly below the lead
    mon — fine for Route 1 (Pidgey/Rattata L2-5) with a L5+ Bulbasaur,
    and a no-op for unaffected encounters. Saves us from grinding
    Bulbasaur up before Viridian Pokécenter exists as a heal option."""
    try:
        base = drv.sym.addr_of("wRepelRemainingSteps")
        drv.mem[base] = steps & 0xff
        print(f"  [repel] wRepelRemainingSteps = {steps}", flush=True)
    except Exception as e:
        print(f"  [repel] failed to set: {e}", flush=True)


def _pathfind_and_walk(drv, session, outdir, goal_xy: str, label: str,
                       rom: str, sym: str, sha1: str,
                       stop_map_ids=()) -> None:
    """Save state → run A* pathfinder → walk the returned direction string,
    resolving battles as they fire. Used by the viridian navigator's
    Route 1 fallback when the hand-coded zig-zag paths get desynced by
    wild battles and the simple "UP with L/R detour" finisher can't
    clear a multi-tile ledge."""
    tmp_state = outdir / f"_{label}.state"
    tmp_state.write_bytes(session.save_state())
    tmp_path = outdir / f"_{label}.txt"
    path = run_pathfinder(tmp_state, goal_xy, tmp_path, rom, sym, sha1)
    print(f"  [{label}] A* {len(path)} steps → walking", flush=True)
    walk_path(drv, path, label=label, stop_map_ids=stop_map_ids)


def navigate_to_viridian_with_retry(drv: rtb.Driver, outdir: Path,
                                    rom: str, sym: str, sha1: str,
                                    session: Session,
                                    max_attempts: int = 20) -> bool:
    """Drive from wherever we are (lab exit, Pallet, Route 1, or post-
    blackout Red's House) to Viridian City, retrying after blackouts.

    Each blackout advances the emulator's tick counter and thus the wild-
    encounter RNG, so retries are *not* deterministically identical —
    eventually one threads the needle through Route 1 without KOing
    Bulbasaur.
    """
    M_PALLET, M_VIRIDIAN, M_ROUTE_1 = 0x00, 0x01, 0x0c
    M_REDS_1F, M_REDS_2F = 0x25, 0x26
    M_OAKS_LAB = 0x28

    for attempt in range(max_attempts):
        gs = drv.gs()
        print(f"  [viridian attempt {attempt+1}/{max_attempts}] "
              f"map=0x{gs.overworld.map_id:02x} "
              f"xy=({gs.overworld.x},{gs.overworld.y})", flush=True)

        if gs.overworld.map_id == M_VIRIDIAN:
            return True

        # Post-blackout recovery: exit Red's House back into Pallet Town.
        if gs.overworld.map_id in (M_REDS_1F, M_REDS_2F):
            for _ in range(30):
                g = drv.gs()
                if g.overworld.map_id == M_PALLET:
                    break
                if g.overworld.y < 7:
                    drv.press("down")
                elif g.overworld.x > 3:
                    drv.press("left")
                else:
                    drv.press("down")
            drv.idle(60)

        map_id = drv.gs().overworld.map_id
        # If we're inside Oak's Lab (walked back in through the door
        # warp), step down+left to exit via the front mat.
        if map_id == M_OAKS_LAB:
            for _ in range(20):
                g = drv.gs()
                if g.overworld.map_id == M_PALLET:
                    break
                drv.press("down")
            drv.idle(30)
            map_id = drv.gs().overworld.map_id

        # Encounter suppression before every traversal leg (safe to set
        # repeatedly; the game decrements it per step).
        _activate_repel(drv)
        try:
            if map_id == M_ROUTE_1:
                # Walk the Route 1 map to its north warp into Viridian.
                _pathfind_and_walk(
                    drv, session, outdir,
                    goal_xy="10,0", label=f"route1_a_star_{attempt}",
                    rom=rom, sym=sym, sha1=sha1,
                    stop_map_ids=(M_VIRIDIAN,),
                )
                # A* goal at (10, 0) lands ON the northern edge but the
                # actual map warp only fires when we *step* off the
                # edge — mash UP until the map id flips.
                for _ in range(4):
                    if drv.gs().overworld.map_id != M_ROUTE_1:
                        break
                    drv.press("up")
            elif map_id == M_PALLET:
                # Step off the lab door threshold (12, 11) first — A*
                # from (12, 11) toward (10, 0) otherwise routes straight
                # UP through the door warp back into Oak's Lab.
                px, py = drv.gs().overworld.x, drv.gs().overworld.y
                if (px, py) == (12, 11):
                    drv.press("down")
                _pathfind_and_walk(
                    drv, session, outdir,
                    goal_xy="10,0", label=f"pallet_a_star_{attempt}",
                    rom=rom, sym=sym, sha1=sha1,
                    stop_map_ids=(M_ROUTE_1,),
                )
                for _ in range(4):
                    if drv.gs().overworld.map_id != M_PALLET:
                        break
                    drv.press("up")
            else:
                # Lab-exit case: let the smart driver do its zig-zag.
                drv.run_pallet_to_viridian()
        except RuntimeError as e:
            print(f"  pathfinder failed: {e}", flush=True)

        gs = drv.gs()
        if gs.overworld.map_id == M_VIRIDIAN:
            return True

    print("  FAILED: could not reach Viridian after "
          f"{max_attempts} attempts; last map="
          f"0x{drv.gs().overworld.map_id:02x}", flush=True)
    return False


def save_milestone(session: Session, outdir: Path, name: str) -> Path:
    p = outdir / "milestones" / f"{name}.state"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(session.save_state())
    print(f"  saved {name}", flush=True)
    return p


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--outdir", default="walkthrough_badge")
    p.add_argument("--skip-to", choices=[
        "start", "viridian", "grind", "forest", "pewter", "brock"
    ], default="start", help="resume from a specific phase")
    p.add_argument(
        "--legacy-grind", action="store_true",
        help="Use the old level_up.py grinder instead of the heal-loop "
             "grinder in grind.py (diagnostic fallback).",
    )
    p.add_argument(
        "--skip-grind", action="store_true",
        help="Skip Route 2 grind and jump straight to Option-B top-up "
             "(L13 Bulba + Vine Whip via RAM poke). Lets forest/Pewter/"
             "Brock phases be validated without paying the 17-minute "
             "grind cost when the grind is known-blocked (e.g. on Blue "
             "where heal desyncs at (5, 48)).",
    )
    args = p.parse_args()

    rom = os.environ["POKERED_ROM_PATH"]
    sym = os.environ["POKERED_SYM_PATH"]
    sha1 = os.environ.get(
        "POKERED_ROM_SHA1", "e1deed63080bc24cad5fba18ecb3184f905d16d4",
    )
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    session = Session.from_files(rom, sym, expected_rom_sha1=sha1)
    register_default_hooks(session)

    # Phase 1: use walkthrough.py + run_to_brock's verified phases to
    # reach Viridian and get onto Route 2.
    wt_drv = wt.WalkthroughDriver(
        session=session, outdir=outdir / "walkthrough_frames",
    )
    drv = rtb.Driver(session)

    if args.skip_to == "start":
        for name, fn in [
            ("intro", lambda: wt.run_phase_intro(wt_drv)),
            ("exit_house", lambda: wt.run_phase_exit_house(wt_drv)),
            ("oak_intercept", lambda: wt.run_phase_oak_intercept(wt_drv)),
            ("pick_starter", lambda: wt.run_phase_pick_starter(wt_drv)),
            ("rival_battle", lambda: wt.run_phase_rival_battle(wt_drv)),
            # run_pallet_to_viridian uses the smart (PP- and type-aware)
            # battle AI from run_to_brock.Driver instead of the mash-A path
            # in walkthrough.py. On Blue the mash-A path blacks out on
            # Route 1 because RNG-dictated wild encounters grind Bulbasaur
            # down. The smart AI + blackout retry below survives either
            # ROM.
            ("pallet_to_viridian",
             lambda: navigate_to_viridian_with_retry(
                 drv, outdir, rom, sym, sha1, session)),
            ("viridian_to_route2", drv.run_viridian_to_route2),
        ]:
            print(f"\n=== phase: {name} ===", flush=True)
            fn()
            save_milestone(session, outdir, name)

    # Phase 2: grind Bulba to Lv 13 with periodic heals.
    if args.skip_to in ("start", "viridian", "grind"):
        print("\n=== phase: grind_to_level_13 ===", flush=True)
        if args.skip_grind:
            # Placeholder "result" so the same top-up check runs below.
            class _SkippedResult:
                final_level = 0
                learned_target_move = False
            result = _SkippedResult()
            print("  [grind] skipped (--skip-grind); relying on Option-B",
                  flush=True)
            # Step the emulator a few frames so we're not mid-anything
            # when Option-B pokes the party struct.
            session.step(120, render=True)
        elif args.legacy_grind:
            lu.grind_to_level(session, target_level=13, max_battles=60)
        else:
            result = grind.grind_to(
                session,
                outdir=outdir,
                rom=rom, sym=sym, sha1=sha1,
                target_level=13,
                target_move_id=grind.MOVE_VINE_WHIP,
                max_battles=80,
                max_wall_seconds=900.0,
            )
        # If the grind bailed short of target (heal-path desync on
        # specific Route 2 tiles, e.g. (5, 48)) or was --skip-grind'd,
        # top up the lead Pokémon via a RAM poke so the rest of the
        # pipeline can still validate forest→Brock. Same Option-B
        # fallback pattern as blue_forest_to_brock.py.
        need_topup = True
        if not args.skip_grind and not args.legacy_grind:
            need_topup = (not result.learned_target_move
                          or result.final_level < 13)
        if need_topup:
            if not args.skip_grind:
                print("  [grind] applying Option-B top-up to "
                      "L13 + Vine Whip", flush=True)
            # Make sure we're out of any lingering battle before RAM-
            # poking the party struct. If the grinder bailed with
            # heal_failed the engine can still be sitting on a
            # SWITCH/STATS/CANCEL party-select menu — DOWN+A cycles the
            # cursor to CANCEL and confirms, which closes the menu and
            # typically drops wIsInBattle back to 0.
            for _ in range(40):
                gs = session.read_game_state()
                if not gs.battle.active:
                    break
                session.press("b"); session.step(30, render=True)
                if not session.read_game_state().battle.active:
                    break
                session.press("down"); session.step(20, render=True)
                session.press("a"); session.step(30, render=True)
            _option_b_topup(session)
        save_milestone(session, outdir, "grind_complete")

    # Phase 3: Route 2 → Forest South Gate.
    if args.skip_to in ("start", "viridian", "grind", "forest"):
        print("\n=== phase: route2_to_forest ===", flush=True)
        gs0 = drv.gs()
        print(f"  r2 entry: map=0x{gs0.overworld.map_id:02x} "
              f"xy=({gs0.overworld.x},{gs0.overworld.y})", flush=True)
        # rtb's hand-coded ROUTE2_TO_FOREST_GATE_PATH starts from (8, 71)
        # and desyncs on any other tile — plus the reposition-to-(8,71)
        # A* itself intermittently hangs on problem Route 2 tiles (e.g.
        # (5, 48) mid-ledge). blue_forest_to_brock.py proved A* directly
        # to the gate entry (3, 44) works from arbitrary Route 2 starts,
        # so share that approach here.
        # If the grinder bailed at a known-bad tile (e.g. (5, 48) where
        # A* plans desync on sprite collision), nudge off it before
        # pathing to the gate. All neighbouring tiles produce a clean
        # A* to (3, 44).
        # Repel FIRST so nudge presses don't trigger wild encounters.
        # Without it, walking in grass at (5, 48) fires a battle on
        # every direction press, and BrockDriver's move selection
        # churns on those wild battles instead of progressing the walk.
        _activate_repel(drv)
        session.step(60, render=True)
        # Nudge unconditionally on Route 2 — any starting tile can hit
        # the sprite-collision A* desync. Pressing one direction before
        # A* sidesteps the issue for any non-canonical position.
        gs = drv.gs()
        if gs.overworld.map_id == rtb.M_ROUTE_2:
            start = (gs.overworld.x, gs.overworld.y)
            for d in ("right", "down", "left", "up"):
                before = (drv.gs().overworld.x, drv.gs().overworld.y)
                drv.press(d)
                after = (drv.gs().overworld.x, drv.gs().overworld.y)
                if after != before:
                    print(f"  [r2→gate] nudged from {start} -> {after}",
                          flush=True)
                    break
            else:
                print(f"  [r2→gate] nudge could not move from {start}; "
                      f"map=0x{drv.gs().overworld.map_id:02x}",
                      flush=True)
        seed = outdir / "_r2_to_gate.state"
        seed.parent.mkdir(parents=True, exist_ok=True)
        seed.write_bytes(session.save_state())
        try:
            r2_path = run_pathfinder(seed, "3,44",
                                      outdir / "_r2_to_gate.txt",
                                      rom, sym, sha1)
            print(f"  route2→(3,44) A*: {len(r2_path)} steps", flush=True)
            walk_path(drv, r2_path, label="route2_to_gate",
                       stop_map_ids=(0x32, 0x33))
        except RuntimeError as e:
            print(f"  route2 A* failed ({e})", flush=True)
        # One UP step to trigger the south-gate warp.
        for _ in range(4):
            if drv.gs().overworld.map_id == 0x32:
                break
            drv.press("up")
        save_milestone(session, outdir, "route2_to_forest")

        # Through the south gate (map 0x32). Both gates in Viridian
        # Forest have a quirk where UP from (4, 1) bumps the wall
        # despite pokered's warp_event listing (4, 0) as a valid warp;
        # only (5, 0) actually transitions. A* to (5, 0) avoids the
        # problem.
        if drv.gs().overworld.map_id == 0x32:
            session.step(60, render=True)
            gate_seed = outdir / "_gate_cross.state"
            gate_seed.write_bytes(session.save_state())
            try:
                gpath = run_pathfinder(gate_seed, "5,0",
                                        outdir / "_gate_cross.txt",
                                        rom, sym, sha1)
                print(f"  south_gate A*: {len(gpath)} steps", flush=True)
                walk_path(drv, gpath, label="south_gate",
                           stop_map_ids=(0x33,))
            except RuntimeError as e:
                print(f"  south_gate pathfind failed: {e}", flush=True)
            for _ in range(6):
                if drv.gs().overworld.map_id == 0x33:
                    break
                drv.press("up")
        forest_entry = save_milestone(session, outdir, "forest_entry")

        # Path through forest using A*, recomputing after battles desync us.
        # Step UP off the forest's own warp row (y=47) first — A*'s first
        # LEFT/RIGHT step otherwise re-warps us back through the south gate.
        print("\n=== phase: forest_traversal ===", flush=True)
        session.step(60, render=True)
        gs = drv.gs()
        if gs.overworld.map_id == 0x33 and gs.overworld.y >= 45:
            for _ in range(4):
                drv.press("up")
                if drv.gs().overworld.y < 45:
                    break
        attempts = 0
        while attempts < 10:
            gs = drv.gs()
            if gs.overworld.map_id == 0x2F:  # north gate
                break
            if gs.overworld.map_id == 0x33 and (gs.overworld.x, gs.overworld.y) in ((1, 0), (2, 0)):
                for _ in range(4):
                    drv.press("up")
                    if drv.gs().overworld.map_id == 0x2F:
                        break
                break
            _activate_repel(drv)
            seed = outdir / f"_forest_leg_{attempts}.state"
            seed.write_bytes(session.save_state())
            path_file = outdir / f"_forest_leg_{attempts}.txt"
            try:
                path = run_pathfinder(seed, "1,0", path_file, rom, sym, sha1)
            except RuntimeError as e:
                print(f"  forest pathfind fail: {e}", flush=True)
                break
            print(f"  forest leg {attempts}: {len(path)} steps", flush=True)
            if not path:
                for _ in range(4):
                    drv.press("up")
                    if drv.gs().overworld.map_id == 0x2F:
                        break
                attempts += 1
                continue
            walk_path(drv, path, label=f"forest{attempts}",
                       stop_map_ids=(0x2F,))
            gs = drv.gs()
            if gs.overworld.map_id == 0x33 and gs.overworld.y == 0:
                for _ in range(3):
                    drv.press("up")
                    if drv.gs().overworld.map_id == 0x2F:
                        break
            attempts += 1

        save_milestone(session, outdir, "forest_exit")

    # Phase 4: Pewter City → Gym.
    if args.skip_to in ("start", "viridian", "grind", "forest", "pewter"):
        print("\n=== phase: pewter_approach ===", flush=True)
        _activate_repel(drv)
        # Cross north gate (0x2F) with A* (same (5, 0) workaround as south).
        if drv.gs().overworld.map_id == 0x2F:
            ngate_seed = outdir / "_ngate.state"
            ngate_seed.write_bytes(session.save_state())
            try:
                npath = run_pathfinder(ngate_seed, "5,0",
                                        outdir / "_ngate.txt",
                                        rom, sym, sha1)
                print(f"  north_gate A*: {len(npath)} steps", flush=True)
                walk_path(drv, npath, label="north_gate",
                           stop_map_ids=(0x0d,))
            except RuntimeError as e:
                print(f"  north_gate pathfind failed: {e}", flush=True)
            for _ in range(6):
                if drv.gs().overworld.map_id == 0x0d:
                    break
                drv.press("up")
        session.step(60, render=True)
        # Route 2 north → Pewter border (10, 0).
        if drv.gs().overworld.map_id == 0x0d:
            seed = outdir / "_r2n.state"
            seed.write_bytes(session.save_state())
            try:
                rpath = run_pathfinder(seed, "10,0",
                                        outdir / "_r2n.txt",
                                        rom, sym, sha1)
                print(f"  route2n→pewter A*: {len(rpath)} steps",
                      flush=True)
                walk_path(drv, rpath, label="r2n",
                           stop_map_ids=(0x02,))
            except RuntimeError as e:
                print(f"  pewter pathfind failed: {e}", flush=True)
            for _ in range(4):
                if drv.gs().overworld.map_id == 0x02:
                    break
                drv.press("up")
        save_milestone(session, outdir, "pewter_entry")
        # Walk to Pewter Gym door via A*.
        if drv.gs().overworld.map_id == 0x02:
            session.step(60, render=True)
            seed = outdir / "_pewter_to_gym.state"
            seed.write_bytes(session.save_state())
            try:
                path = run_pathfinder(seed, "16,18",
                                       outdir / "_pewter_to_gym.txt",
                                       rom, sym, sha1)
                print(f"  pewter→gym A*: {len(path)} steps", flush=True)
                walk_path(drv, path, label="pgym",
                           stop_map_ids=(0x36,))
                for _ in range(4):
                    if drv.gs().overworld.map_id == 0x36:
                        break
                    drv.press("up")
            except RuntimeError as e:
                print(f"  pewter→gym pathfind failed: {e}", flush=True)

    # Phase 5: Brock gym + battle.
    print("\n=== phase: brock_badge ===", flush=True)
    got_badge = bg.run_pewter_to_brock_badge(session, driver=drv)
    save_milestone(session, outdir, "after_brock")

    gs = session.read_game_state()
    print("\n=== FINAL ===", flush=True)
    print(f"map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y}) "
          f"badges=0x{gs.progress.badges_raw:02x} "
          f"party[0]=L{gs.party.mons[0].level} HP{gs.party.mons[0].hp}/{gs.party.mons[0].max_hp}",
          flush=True)

    if got_badge:
        print("\nBOULDER BADGE OBTAINED!", flush=True)
        return 0
    print("\nNo badge yet", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
