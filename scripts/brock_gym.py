"""Drive the full Pewter Gym sequence: enter, defeat Jr. Trainer, defeat Brock.

This is the final puzzle piece of the run-to-Boulder-Badge pipeline. The
assumed preconditions are:

* Bulbasaur is at least Lv 13 with Vine Whip (move 22) learned.
* Party is at full HP.
* Player is standing on Pewter City (map 0x02), at or near the Pewter
  Gym warp tile (16, 17).

It is intentionally self-contained: it imports :class:`Driver` from
``run_to_brock.py`` only to reuse the already-verified battle / button
plumbing, and does NOT depend on the upstream phases.

Map facts (verified against ``pret/pokered``):

* Pewter Gym warp on Pewter City: ``warp_event 16, 17, PEWTER_GYM, 1``
  (``data/maps/objects/PewterCity.asm``).
* Inside the gym (``data/maps/objects/PewterGym.asm``):
  - Entry warps at map-coord (4, 13) and (5, 13) on the south wall.
  - Jr. Trainer (``PEWTERGYM_COOLTRAINER_M``, ``OPP_JR_TRAINER_M``) at
    (3, 6), facing RIGHT → sight line extends east along y=6.
  - Brock (``PEWTERGYM_BROCK``, ``OPP_BROCK``) at (4, 1), facing DOWN →
    sight line extends south along x=4.
  - Gym Guide NPC at (7, 10), no trainer.

Run:

  PYTHONPATH=src POKERED_ROM_PATH=rom/pokemon-red-color.gb \\
      POKERED_SYM_PATH=rom/pokemon-red.sym \\
      POKERED_ROM_SHA1=e1deed63080bc24cad5fba18ecb3184f905d16d4 \\
      python -u scripts/brock_gym.py --state path/to/pewter_entry.state
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from pokered_harness.session import Session
from pokered_harness.mcp_server import register_default_hooks

# Reuse Driver (battle resolver, press/idle helpers) from run_to_brock.
# That module also defines the map constants and the base move IDs, but
# we re-export a preference-aware battle helper below.
sys.path.insert(0, str(Path(__file__).parent))
import run_to_brock as rtb  # noqa: E402

M_PEWTER = rtb.M_PEWTER
M_PEWTER_GYM = rtb.M_PEWTER_GYM

# Move IDs (see pret/pokered/data/moves/names.asm and moves.asm).
MOVE_TACKLE = 33
MOVE_GROWL = 45
MOVE_LEECH_SEED = 73
MOVE_VINE_WHIP = 22
MOVE_POISON_POWDER = 77
MOVE_SLEEP_POWDER = 79

# Ordered move preference against Brock's Rock/Ground team. Vine Whip is
# super-effective (4x, grass vs rock+ground). Tackle is a fallback; status
# moves are excluded because Rock-types are immune to PoisonPowder and
# anything here that doesn't damage is wasted vs Diglett/Geodude/Onix.
MOVE_PREFERENCE = [MOVE_VINE_WHIP, MOVE_TACKLE]


def _select_brock_move(pp: list[int], moves: list[int]) -> int | None:
    """Return the 0..3 move slot to use this turn, or None for Struggle.

    Uses :data:`MOVE_PREFERENCE` (Vine Whip > Tackle). If none of the
    preferred moves are usable, falls back to any damaging move with PP,
    and only then to any move with PP (worst case: Growl buys a turn
    without fainting).
    """
    # 1) Preference list (Vine Whip, then Tackle).
    for want in MOVE_PREFERENCE:
        for i in range(4):
            if moves[i] == want and pp[i] > 0:
                return i
    # 2) Any "damaging" move with PP (as scored by run_to_brock).
    for i in range(4):
        if pp[i] > 0 and moves[i] in rtb.DAMAGING_MOVE_IDS:
            return i
    # 3) Any move with PP at all.
    for i in range(4):
        if pp[i] > 0:
            return i
    return None


class BrockDriver(rtb.Driver):
    """Driver extension that prefers Vine Whip in gym battles."""

    def battle_turn(self) -> None:  # noqa: C901 — mirrors parent shape
        """Override of Driver.battle_turn with Vine-Whip-first selection.

        Structure parallels ``rtb.Driver.battle_turn`` exactly; only the
        move selection step differs. Kept in one place so future tweaks
        to the navigation (menu cursoring) stay in ``run_to_brock``.
        """
        # Step 1: advance dialog to main battle menu.
        for _ in range(60):
            gs = self.gs()
            if not gs.battle.active:
                return
            mx = self.sym.read_u8(self.mem, "wMaxMenuItem")
            if not self.joy_locked() and mx == 3:
                break
            self.press("a")

        # Step 2: cursor to FIGHT (slot 0).
        self.press("up")
        self.press("up")
        self.press("left")

        # Step 3: select FIGHT.
        self.press("a", step=60)

        # Step 4: move-menu selection — prefer Vine Whip.
        pp = self.read_pp()
        moves = (
            list(self.gs().party.mons[0].moves)
            if self.gs().party.mons
            else [0, 0, 0, 0]
        )
        target = _select_brock_move(pp, moves)
        print(
            f"    battle: moves={moves} pp={pp} → slot {target}"
            + (f" (move {moves[target]})" if target is not None else " (Struggle)"),
            file=sys.stderr,
            flush=True,
        )
        if target is None:
            # No PP anywhere — mash A and let Struggle do its thing.
            self.press("a", step=90)
            return

        # Step 5: park cursor at slot 0, then down to target.
        for _ in range(4):
            self.press("up")
        for _ in range(target):
            self.press("down")
        self.press("a", step=90)

        # Step 6: advance turn-outcome dialog until back at main menu.
        for _ in range(40):
            gs = self.gs()
            if not gs.battle.active:
                return
            mx = self.sym.read_u8(self.mem, "wMaxMenuItem")
            if not self.joy_locked() and mx == 3:
                return
            self.press("a", step=20)


# ---- Navigation helpers -------------------------------------------------

def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _enter_gym(drv: BrockDriver) -> bool:
    """Walk from current Pewter position onto the gym warp at (16, 17)
    and step through it.

    Pewter is a small 20x18-block outdoor map. The gym warp is on the
    south-west cluster. We path by direct L/R + U/D bumping with a bail
    on map transition, which is more than sufficient for this short hop.

    Returns True if we ended up inside PEWTER_GYM (map 0x36).
    """
    gs = drv.gs()
    _log(
        f"  enter_gym: start map=0x{gs.overworld.map_id:02x} "
        f"xy=({gs.overworld.x},{gs.overworld.y})"
    )
    if gs.overworld.map_id == M_PEWTER_GYM:
        _log("  already inside gym")
        return True
    if gs.overworld.map_id != M_PEWTER:
        _log(f"  WARN: expected map 0x02 PEWTER, got 0x{gs.overworld.map_id:02x}")

    # Walk to (16, 18) — the tile directly south of the warp — then UP
    # onto (16, 17) which triggers the warp. Approaching from the south
    # is the canonical entry and avoids bumping into the stray NPC at
    # (8, 15) on the gym's west side.
    target_x, target_y = 16, 18
    for _ in range(60):
        gs = drv.gs()
        if gs.overworld.map_id == M_PEWTER_GYM:
            break
        if gs.battle.active:
            drv.resolve_battle()
            continue
        if drv.joy_locked():
            drv.press("a")
            continue
        cx, cy = gs.overworld.x, gs.overworld.y
        if cx < target_x:
            drv.press("right")
        elif cx > target_x:
            drv.press("left")
        elif cy > target_y:
            drv.press("up")
        elif cy < target_y:
            drv.press("down")
        else:
            # Standing on (16, 18): one step UP onto the warp.
            drv.press("up")
        new = drv.gs()
        if (new.overworld.x, new.overworld.y) == (cx, cy) and new.overworld.map_id == M_PEWTER:
            # Bumped a wall/NPC; nudge down+right to break the deadlock.
            drv.press("down")
            drv.press("right")

    # Push once more just in case the warp animation needs a button poll.
    for _ in range(10):
        if drv.gs().overworld.map_id == M_PEWTER_GYM:
            break
        drv.press("up")

    ok = drv.gs().overworld.map_id == M_PEWTER_GYM
    gs = drv.gs()
    _log(
        f"  enter_gym: done map=0x{gs.overworld.map_id:02x} "
        f"xy=({gs.overworld.x},{gs.overworld.y}) ok={ok}"
    )
    return ok


def _walk_up_until_battle(drv: BrockDriver, max_steps: int, label: str) -> bool:
    """Press UP until a battle starts, the badge lands, or we run out.

    ``label`` is printed for diagnostics. Returns True if a battle fired.
    """
    for _ in range(max_steps):
        gs = drv.gs()
        if gs.battle.active:
            _log(f"  {label}: battle triggered at ({gs.overworld.x},{gs.overworld.y})")
            return True
        if gs.progress.badges_raw & 0x01:
            return False
        if drv.joy_locked():
            drv.press("a")
            continue
        before = (gs.overworld.x, gs.overworld.y)
        drv.press("up")
        now = drv.gs()
        if now.battle.active:
            _log(f"  {label}: battle triggered after step at "
                 f"({now.overworld.x},{now.overworld.y})")
            return True
        if (now.overworld.x, now.overworld.y) == before:
            # Wall/NPC — try a small left-right wiggle.
            drv.press("left")
            drv.press("up")
            if drv.gs().battle.active:
                return True
            drv.press("right")
            drv.press("right")
            drv.press("up")
    return drv.gs().battle.active


def _walk_to_brock_line(drv: BrockDriver) -> bool:
    """After beating Jr. Trainer, walk onto Brock's sight line (x=4).

    The Jr. Trainer is defeated after our column has passed x=3 along
    y=6, which means we're already at (4 or 5, 6-ish). Brock stands at
    (4, 1) facing DOWN, so any step onto column x=4 at y in 2..12 will
    trigger him.

    Strategy: walk UP. If x != 4 after several presses, slide L/R to x=4
    and resume.
    """
    for _ in range(30):
        gs = drv.gs()
        if gs.battle.active:
            return True
        if gs.progress.badges_raw & 0x01:
            return False
        if drv.joy_locked():
            drv.press("a")
            continue
        cx, cy = gs.overworld.x, gs.overworld.y
        if cx < 4:
            drv.press("right")
            continue
        if cx > 4:
            drv.press("left")
            continue
        if cy <= 2:
            # Past Brock somehow — shouldn't happen, but safe.
            break
        before = (cx, cy)
        drv.press("up")
        now = drv.gs()
        if now.battle.active:
            return True
        if (now.overworld.x, now.overworld.y) == before:
            # Blocked — drift right or left and retry.
            drv.press("right")
            drv.press("up")
            if drv.gs().battle.active:
                return True
            drv.press("left")
            drv.press("left")
            drv.press("up")
    return drv.gs().battle.active


def run_pewter_to_brock_badge(session: Session, driver: rtb.Driver | None = None) -> bool:
    """Walk into the Pewter Gym, defeat both trainers, return badge status.

    :param session: active :class:`Session` with the player on (or near)
        the Pewter Gym warp on Pewter City.
    :param driver: optional pre-built :class:`Driver`; if None we wrap
        the session in a :class:`BrockDriver`.
    :returns: True iff bit 0 of ``wObtainedBadges`` is set at the end
        (i.e. the Boulder Badge was obtained).

    Phases:
      1. Enter gym (walk onto warp, verify map 0x36).
      2. Walk UP from gym entry — this crosses Jr. Trainer's east-facing
         sight line along y=6, triggering the battle. Resolve it.
      3. Continue UP / align to x=4, crossing Brock's south-facing sight
         line, triggering the battle. Resolve it.
      4. Mash A through the Boulder Badge award dialog until the badge
         bit is set or we exhaust the retry count.
    """
    drv = driver if isinstance(driver, BrockDriver) else BrockDriver(session)
    _log("=== brock_gym: start ===")

    gs = drv.gs()
    _log(
        f"  pre map=0x{gs.overworld.map_id:02x} "
        f"xy=({gs.overworld.x},{gs.overworld.y}) "
        f"badges=0x{gs.progress.badges_raw:02x} "
        f"party={gs.party.count}"
    )
    if gs.party.mons:
        m = gs.party.mons[0]
        _log(f"  pre bulba: L{m.level} HP{m.hp}/{m.max_hp} "
             f"moves={list(m.moves)} pp={list(m.pp)}")

    # --- Phase 1: enter gym ------------------------------------------
    if not _enter_gym(drv):
        _log("  FAIL: could not enter Pewter Gym")
        return bool(drv.gs().progress.badges_raw & 0x01)

    # Let the map settle after warp.
    drv.idle(30)
    gs = drv.gs()
    _log(f"  inside gym at ({gs.overworld.x},{gs.overworld.y})")

    # --- Phase 2: Jr. Trainer ----------------------------------------
    # From entry (4,13) or (5,13), marching UP will drag us onto y=6 at
    # x=4 or x=5. Jr. Trainer at (3,6) facing RIGHT locks sight on any
    # sprite along that row with x>3 — so the battle should fire before
    # we reach him in person.
    if not (drv.gs().progress.badges_raw & 0x01):
        fired = _walk_up_until_battle(drv, max_steps=25, label="jr_trainer")
        if fired:
            drv.resolve_battle()
            _log("  jr_trainer: resolved")
        else:
            _log("  jr_trainer: no battle (already defeated? continuing)")

    # --- Phase 3: Brock ----------------------------------------------
    if not (drv.gs().progress.badges_raw & 0x01):
        in_battle = _walk_to_brock_line(drv)
        if in_battle or drv.gs().battle.active:
            drv.resolve_battle()
            _log("  brock: battle resolved")
        else:
            # Fallback: if we didn't snap onto column 4, just mash UP.
            _walk_up_until_battle(drv, max_steps=20, label="brock_fallback")
            if drv.gs().battle.active:
                drv.resolve_battle()

    # --- Phase 4: post-fight dialog (badge + TM34) -------------------
    for _ in range(120):
        gs = drv.gs()
        if gs.progress.badges_raw & 0x01:
            break
        if gs.battle.active:
            drv.resolve_battle()
            continue
        drv.press("a", step=30)

    gs = drv.gs()
    ok = bool(gs.progress.badges_raw & 0x01)
    _log(
        f"=== brock_gym: done ok={ok} "
        f"map=0x{gs.overworld.map_id:02x} "
        f"xy=({gs.overworld.x},{gs.overworld.y}) "
        f"badges=0x{gs.progress.badges_raw:02x} ==="
    )
    if gs.party.mons:
        m = gs.party.mons[0]
        _log(f"  post bulba: L{m.level} HP{m.hp}/{m.max_hp} "
             f"moves={list(m.moves)} pp={list(m.pp)}")
    return ok


# ---- CLI ---------------------------------------------------------------


def _cli() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--state",
        required=True,
        help="path to a .state snapshot to load before running",
    )
    p.add_argument(
        "--save-result",
        default=None,
        help="optional path to save the resulting state after the run",
    )
    args = p.parse_args()

    rom = os.environ.get("POKERED_ROM_PATH")
    sym = os.environ.get("POKERED_SYM_PATH")
    if not (rom and sym):
        print("need POKERED_ROM_PATH, POKERED_SYM_PATH env vars", file=sys.stderr)
        return 2

    sha1 = os.environ.get(
        "POKERED_ROM_SHA1", "ea9bcae617fdf159b045185467ae58b2e4a48b9a"
    )
    session = Session.from_files(rom, sym, expected_rom_sha1=sha1)
    register_default_hooks(session)

    state_path = Path(args.state)
    _log(f"loading state {state_path}")
    session.load_state(state_path.read_bytes())
    # Let the map transition scripts finish before reading state.
    session.step(60, render=True)

    ok = run_pewter_to_brock_badge(session)

    if args.save_result:
        out = Path(args.save_result)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(session.save_state())
        _log(f"saved result state → {out}")

    session.close()
    print("BOULDER_BADGE" if ok else "NO_BADGE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
