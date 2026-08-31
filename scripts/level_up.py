"""Grind Bulbasaur from L5 toward L13 by walking Route 2 grass.

Starts from a saved state where the player is on Route 2 (map 0x0D) with
Bulbasaur L5. Walks into the grass patch at ~(6, 51), bounces UP/DOWN to
roll encounters, and resolves each wild battle by selecting Tackle.

Known limitation (see run report):
  - From the bare ``viridian_to_route2`` starter state the bag is empty
    (no Potions) and the nearest Pokecenter is in Viridian, separated by
    ~25 tiles of Route 2 grass. At L5-6 Bulbasaur's 19-21 max HP plus
    Tackle's modest damage means ~2 HP lost per battle — Bulba goes from
    full to critical after 2-3 encounters, and the south-walk back to
    Viridian triggers new encounters while HP is low, so Bulba faints en
    route. Blackout then teleports to Pallet (Red's bed, map 0x00),
    which ends the grind session.
  - Practical outcome: reliably reaches L6 in one session (~3 battles,
    ~400 presses). Getting to L13 without a patched bag (Potions) or a
    RAM heal would require multi-session orchestration where the caller
    re-runs the grinder after each blackout, OR a separate full heal
    flow that navigates to Pallet's starter-house heal on blackout and
    walks back up through Route 1 and Viridian.

Grass-patch discovery: Route 2's encounter grass (tile 0x52) on the
southern half of the route occupies y in roughly [45, 52] around
x=4..15. The central corridor (x=7-8) that the BFS route follows is
paved and yields zero encounters. ``ensure_in_grass`` walks the BFS
corridor up to (4, 56), then pushes UP×5 into the grass (y<=52) and
RIGHT×2 to center the player.

Run:
  POKERED_ROM_PATH=... POKERED_SYM_PATH=... POKERED_ROM_SHA1=... \\
      python -u scripts/level_up.py

Reports: final level, battle count, whether Vine Whip (move 22) was
learned, wall-clock time.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from pokered_harness.mcp_server import register_default_hooks
from pokered_harness.session import Session

# Map constants
M_ROUTE_2 = 0x0D
M_VIRIDIAN = 0x01
M_VIRIDIAN_POKECENTER = 0x29

# Move IDs
MOVE_TACKLE = 33
MOVE_GROWL = 45
MOVE_LEECH_SEED = 73
MOVE_VINE_WHIP = 22

DAMAGING_MOVE_IDS = {MOVE_TACKLE, MOVE_VINE_WHIP, MOVE_LEECH_SEED}


class Grinder:
    def __init__(self, session: Session):
        self.s = session
        self.mem = session._pyboy.memory  # type: ignore
        self.sym = session.symbols
        self.press_count = 0
        self.battle_count = 0
        self.heal_count = 0
        self.blackout_count = 0
        self._last_safe_state: bytes | None = None

    def press(self, button: str, *, duration: int = 6, step: int = 24) -> None:
        self.s.press(button, duration=duration)
        self.s.step(step, render=True)
        self.press_count += 1

    def idle(self, ticks: int) -> None:
        self.s.step(ticks)

    def gs(self):
        return self.s.read_game_state()

    def joy_locked(self) -> bool:
        return (
            self.sym.read_u8(self.mem, "wJoyIgnore") != 0
            if "wJoyIgnore" in self.sym
            else False
        )

    def read_battle_pp(self) -> list[int]:
        base = self.sym.addr_of("wBattleMonPP")
        return [self.mem[base + i] for i in range(4)]

    # ---- Battle resolution -------------------------------------------

    def resolve_battle(self, max_turns: int = 80) -> None:
        """Drive an active battle to completion, preferring damaging moves."""
        for _ in range(max_turns):
            gs = self.gs()
            if not gs.battle.active:
                return
            if gs.party.mons and gs.party.mons[0].hp == 0:
                # Fainted — blackout teleports to Viridian PC. Mash A to
                # clear whiteout text.
                print("  Bulbasaur fainted! Mashing through blackout...",
                      file=sys.stderr, flush=True)
                self.blackout_count += 1
                for _ in range(80):
                    self.press("a")
                    if not self.gs().battle.active and not self.joy_locked():
                        break
                # After blackout, Bulbasaur is fully healed.
                return
            self.battle_turn()
        # Safety: if we ran out of turns, mash A to try to exit
        for _ in range(20):
            if not self.gs().battle.active:
                return
            self.press("a")

    def battle_turn(self) -> None:
        # Step 1: advance dialog until main 2x2 battle menu is responsive.
        # wMaxMenuItem == 3 means the four-option (FIGHT/PKMN/ITEM/RUN)
        # menu is showing.
        for _ in range(80):
            gs = self.gs()
            if not gs.battle.active:
                return
            mx = self.sym.read_u8(self.mem, "wMaxMenuItem")
            if not self.joy_locked() and mx == 3:
                break
            self.press("a")
        if not self.gs().battle.active:
            return

        # Step 2: force cursor to FIGHT (top-left slot).
        self.press("up")
        self.press("up")
        self.press("left")

        # Step 3: select FIGHT
        self.press("a", step=60)

        # Step 4: pick best move (PP > 0, prefer damaging).
        pp = self.read_battle_pp()
        gs = self.gs()
        moves = list(gs.party.mons[0].moves) if gs.party.mons else [0, 0, 0, 0]
        target = None
        for i in range(4):
            if pp[i] > 0 and moves[i] in DAMAGING_MOVE_IDS:
                target = i
                break
        if target is None:
            for i in range(4):
                if pp[i] > 0:
                    target = i
                    break
        if target is None:
            # No PP — press A for Struggle.
            self.press("a", step=90)
            return

        # Reset cursor to slot 0, then move down.
        for _ in range(4):
            self.press("up")
        for _ in range(target):
            self.press("down")
        self.press("a", step=90)

        # Step 5: advance result dialog until either battle ends or we're
        # back at the main battle menu for another turn. Handle level-up
        # screens and "learned a new move" prompts by mashing A (keeps
        # existing moves unless a replace prompt appears — if it does we
        # press B later).
        for _ in range(80):
            gs = self.gs()
            if not gs.battle.active:
                return
            mx = self.sym.read_u8(self.mem, "wMaxMenuItem")
            if not self.joy_locked() and mx == 3:
                return
            # If a Yes/No "forget a move?" prompt (mx == 1) appears, press B
            # to decline (keeps Tackle/Growl/Leech Seed).
            if not self.joy_locked() and mx == 1:
                self.press("b", step=20)
                continue
            self.press("a", step=20)

    # ---- Walking / encounter triggering ------------------------------

    def ensure_in_grass(self) -> None:
        """Walk from the Route 2 south entry (8, 71) into the big grass
        patch to the north.

        Route 2 encounter-grass (tile ID 0x52) is confined to a patch
        at world y <= ~52 around x=4..15 (verified by dumping wTileMap).
        This follows the BFS-verified corridor north through the tree
        wall and steps into grass.
        """
        path = (
            ["up"] * 9        # (8, 71) -> (8, 62)
            + ["left"]        # (8, 62) -> (7, 62)
            + ["up"] * 5      # (7, 62) -> (7, 57)
            + ["left"] * 2    # (7, 57) -> (5, 57)
            + ["up"]          # (5, 57) -> (5, 56)
            + ["left"]        # (5, 56) -> (4, 56)
            + ["up"] * 5      # (4, 56) -> (4, 51)  -- into grass (y<=52)
            + ["right"] * 2   # (4, 51) -> (6, 51)  -- center of grass patch
        )
        for d in path:
            gs = self.gs()
            if gs.battle.active:
                return
            if gs.overworld.map_id != M_ROUTE_2:
                return
            if self.joy_locked():
                self.press("a")
                continue
            self.press(d)

    # Grass zone y range on Route 2 lower half (verified by tilemap dump):
    # grass tiles (0x52) occupy approximately y=45..52 around x=4..15.
    GRASS_Y_MIN = 46
    GRASS_Y_MAX = 52


    def walk_in_grass(self, max_steps: int = 40) -> bool:
        """Alternate UP/DOWN within the Route 2 grass patch.

        Keeps the player within y in [GRASS_Y_MIN, GRASS_Y_MAX] to
        maximise encounter probability (grass tile 0x52 is the only
        encounter surface). Returns True if a battle started.
        """
        for i in range(max_steps):
            gs = self.gs()
            if gs.battle.active:
                return True
            if gs.overworld.map_id != M_ROUTE_2:
                return False
            if self.joy_locked():
                self.press("a")
                continue
            y = gs.overworld.y
            # Stay inside grass zone.
            if y < self.GRASS_Y_MIN:
                d = "down"
            elif y > self.GRASS_Y_MAX:
                d = "up"
            else:
                d = "up" if (i % 2 == 0) else "down"
            before = (gs.overworld.x, gs.overworld.y)
            self.press(d)
            if self.gs().battle.active:
                return True
            new_pos = (self.gs().overworld.x, self.gs().overworld.y)
            if new_pos == before:
                # Blocked — try the other vertical.
                self.press("up" if d == "down" else "down")
            if (i + 1) % 10 == 0:
                mon = self.gs().party.mons[0] if self.gs().party.mons else None
                print(
                    f"  walk step {i+1}: xy={new_pos} "
                    f"L{mon.level if mon else '?'} "
                    f"HP{mon.hp if mon else '?'}/{mon.max_hp if mon else '?'} "
                    f"presses={self.press_count}",
                    file=sys.stderr,
                    flush=True,
                )
        return False

    # ---- Main loop ---------------------------------------------------

    def grind(self, target_level: int, max_battles: int) -> int:
        """Main grinding loop. Returns final level."""
        start_time = time.time()
        gs = self.gs()
        start_level = gs.party.mons[0].level if gs.party.mons else 0
        print(
            f"Starting grind: map=0x{gs.overworld.map_id:02x} "
            f"xy=({gs.overworld.x},{gs.overworld.y}) "
            f"Bulba L{start_level} HP{gs.party.mons[0].hp}/{gs.party.mons[0].max_hp} "
            f"moves={gs.party.mons[0].moves}",
            file=sys.stderr,
            flush=True,
        )

        # Reposition into grass (walking off the paved south path east).
        if not self.gs().battle.active and self.gs().overworld.map_id == M_ROUTE_2:
            self.ensure_in_grass()
            gs = self.gs()
            print(
                f"  After positioning: xy=({gs.overworld.x},{gs.overworld.y})",
                file=sys.stderr,
                flush=True,
            )

        while self.battle_count < max_battles:
            gs = self.gs()
            if not gs.party.mons:
                print("  ERROR: party empty", file=sys.stderr, flush=True)
                break
            mon = gs.party.mons[0]

            if mon.level >= target_level:
                print(
                    f"  Reached target level {target_level}!",
                    file=sys.stderr,
                    flush=True,
                )
                break

            # If we left Route 2 (blackout to Pallet, or wandered into the
            # south gate) we can't keep grinding — stop here. Blackout
            # auto-heals Bulba but erases in-session XP above the last
            # save point, so restarting from viridian_to_route2.state is
            # the simplest recovery strategy.
            if gs.overworld.map_id not in (M_ROUTE_2, M_VIRIDIAN):
                print(
                    f"  Post-blackout on map 0x{gs.overworld.map_id:02x}. "
                    f"Stopping grind.",
                    file=sys.stderr,
                    flush=True,
                )
                break

            # Handle in-battle state (e.g., we loaded a state mid-battle)
            if gs.battle.active:
                self.resolve_battle()
                self.battle_count += 1
                elapsed = time.time() - start_time
                post = self.gs().party.mons[0] if self.gs().party.mons else None
                post_map = self.gs().overworld.map_id
                if post:
                    print(
                        f"  [battle #{self.battle_count}] L{post.level} "
                        f"HP{post.hp}/{post.max_hp} map=0x{post_map:02x} "
                        f"t={elapsed:.0f}s presses={self.press_count}",
                        file=sys.stderr,
                        flush=True,
                    )
                continue

            # HP low? Give up (a full heal flow would require Viridian PC
            # nav, which is out of scope and too fragile for this grinder.)
            # Blackout handling below lets the emulator auto-heal on faint.
            if mon.hp > 0 and mon.hp / max(mon.max_hp, 1) < 0.25:
                print(
                    f"  HP low: {mon.hp}/{mon.max_hp} — continuing anyway "
                    f"(blackout will auto-heal if needed)",
                    file=sys.stderr,
                    flush=True,
                )

            # Walk to trigger encounter
            self.walk_in_grass(max_steps=30)

        elapsed = time.time() - start_time
        final = self.gs().party.mons[0] if self.gs().party.mons else None
        final_level = final.level if final else 0
        print(
            f"\nGrind done: L{start_level} -> L{final_level} "
            f"in {self.battle_count} battles, {self.blackout_count} blackouts, "
            f"{self.press_count} presses, {elapsed:.0f}s wall-clock",
            file=sys.stderr,
            flush=True,
        )
        return final_level


def grind_to_level(
    session: Session, target_level: int = 13, max_battles: int = 40
) -> int:
    """Grind wild encounters on Route 2 until Bulbasaur hits target_level.

    Returns the final level reached. Does NOT reset state — caller is
    responsible for loading an appropriate starting state.
    """
    g = Grinder(session)
    return g.grind(target_level=target_level, max_battles=max_battles)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--state",
        default="walkthrough_brock_color/milestones/viridian_to_route2.state",
        help="Path to starting save-state",
    )
    parser.add_argument("--target-level", type=int, default=13)
    parser.add_argument("--max-battles", type=int, default=40)
    parser.add_argument(
        "--out-state",
        default=None,
        help="Optional path to save the final state (e.g. for use by run_to_brock)",
    )
    args = parser.parse_args()

    rom = os.environ.get("POKERED_ROM_PATH")
    sym = os.environ.get("POKERED_SYM_PATH")
    if not (rom and sym):
        raise SystemExit("need POKERED_ROM_PATH, POKERED_SYM_PATH env vars")

    session = Session.from_files(
        rom,
        sym,
        expected_rom_sha1=os.environ.get(
            "POKERED_ROM_SHA1", "ea9bcae617fdf159b045185467ae58b2e4a48b9a"
        ),
    )
    register_default_hooks(session)

    state_path = Path(args.state)
    if not state_path.exists():
        raise SystemExit(f"start state not found: {state_path}")
    session.load_state(state_path.read_bytes())
    # After load_state the screen output lags the internal state; burn a
    # few ticks so any pending vblank completes before we read state.
    session.step(30, render=True)

    start = time.time()
    try:
        final_level = grind_to_level(
            session,
            target_level=args.target_level,
            max_battles=args.max_battles,
        )
    finally:
        gs = session.read_game_state()
        elapsed = time.time() - start
        print("\n=== FINAL ===", file=sys.stderr)
        print(
            f"map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y}) "
            f"wall_time={elapsed:.0f}s",
            file=sys.stderr,
        )
        if gs.party.mons:
            m = gs.party.mons[0]
            learned_vine = MOVE_VINE_WHIP in m.moves
            print(
                f"Bulba: L{m.level} HP{m.hp}/{m.max_hp} "
                f"moves={m.moves} pp={m.pp} "
                f"vine_whip={'YES' if learned_vine else 'NO'}",
                file=sys.stderr,
            )
        if args.out_state:
            out = Path(args.out_state)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(session.save_state())
            print(f"  saved final state to {out}", file=sys.stderr)
        session.close()


if __name__ == "__main__":
    main()
