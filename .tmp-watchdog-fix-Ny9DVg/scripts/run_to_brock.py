"""End-to-end drive from boot to Boulder Badge.

This is the "get the first badge" monolithic script. It builds on the
walkthrough.py foundation but adds:

* Smart move-selection battle AI (PP-aware, prefers damaging moves)
* Viridian-Old-Man bypass via RAM-set of EVENT_GOT_POKEDEX
* Viridian Pokécenter healing when HP is low
* Forest → Pewter → Gym → Brock pipeline

Run:
  POKERED_ROM_PATH=... POKERED_SYM_PATH=... python scripts/run_to_brock.py
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from pokered_harness.mcp_server import register_default_hooks
from pokered_harness.session import Session

# Reuse the verified early-game phases from walkthrough.py rather than
# maintaining a parallel (and divergent) copy here. Those phases are the
# only ones empirically proven to reach Viridian City.
sys.path.insert(0, str(Path(__file__).parent))
import walkthrough as wt

# Map constants
M_PALLET = 0x00
M_VIRIDIAN = 0x01
M_PEWTER = 0x02
M_ROUTE_1 = 0x0C
M_ROUTE_2 = 0x0D
M_REDS_2F = 0x26
M_REDS_1F = 0x25
M_OAKS_LAB = 0x28
M_VIRIDIAN_POKECENTER = 0x29
M_VIRIDIAN_FOREST_SOUTH_GATE = 0x32
M_VIRIDIAN_FOREST = 0x33
M_VIRIDIAN_FOREST_NORTH_GATE = 0x2F
M_PEWTER_POKECENTER = 0x3A  # VIRIDIAN is 0x29, Pewter around 0x3A
M_PEWTER_GYM = 0x36

# Damaging moves (to prefer over status moves). Originally a
# Bulbasaur-only set for Red/Blue, but Yellow Pikachu needs its
# electric moves here too — without ThunderShock the Driver.battle_turn
# falls back to "any move with PP" and picks Growl, which loops forever
# against trainer Pokémon (resolve_battle exhausts max_turns with the
# enemy at full HP). See: forest Bug Catcher trainer fight at (1, 18).
DAMAGING_MOVE_IDS = {
    33,  # Tackle
    73,  # Leech Seed (drains HP, counts as damaging for our purposes)
    22,  # Vine Whip
    77,  # PoisonPowder (minor)
    78,  # Stun Spore
    79,  # Sleep Powder
    # Yellow Pikachu lead set
    84,  # ThunderShock
    85,  # Thunderbolt
    87,  # Thunder
    24,  # Double Kick
    98,  # Quick Attack
    104,  # Slam (Pikachu learns at L20; included for late-set parity)
    21,  # Slam alternate?
    86,  # Skull Bash (rare; included so we never accidentally miss)
}

# Known moves Bulbasaur gets
MOVE_TACKLE = 33
MOVE_GROWL = 45
MOVE_LEECH_SEED = 73
MOVE_VINE_WHIP = 22


class Driver:
    def __init__(self, session: Session):
        self.s = session
        self.mem = session._pyboy.memory  # type: ignore
        self.sym = session.symbols
        self.press_count = 0

    def press(self, button: str, duration: int = 6, step: int = 24) -> None:
        self.s.press(button, duration=duration)
        self.s.step(step, render=True)
        self.press_count += 1
        if self.press_count % 50 == 0:
            gs = self.s.read_game_state()
            print(f"  #{self.press_count} map=0x{gs.overworld.map_id:02x} "
                  f"xy=({gs.overworld.x},{gs.overworld.y}) "
                  f"party_lvl={gs.party.mons[0].level if gs.party.mons else '-'} "
                  f"hp={gs.party.mons[0].hp if gs.party.mons else '-'}",
                  file=sys.stderr, flush=True)

    def idle(self, ticks: int) -> None:
        self.s.step(ticks)

    def joy_locked(self) -> bool:
        return self.sym.read_u8(self.mem, "wJoyIgnore") != 0 if "wJoyIgnore" in self.sym else False

    def read_pp(self) -> list[int]:
        base = self.sym.addr_of("wBattleMonPP")
        return [self.mem[base + i] for i in range(4)]

    def gs(self):
        return self.s.read_game_state()

    # ---- Battle AI -----------------------------------------------------

    def resolve_battle(self, max_turns: int = 80) -> None:
        """Drive a battle to completion with smart move selection."""
        for turn in range(max_turns):
            gs = self.gs()
            if not gs.battle.active:
                return
            # Check if we're fainted
            if gs.party.mons and gs.party.mons[0].hp == 0:
                print("  Bulbasaur fainted in battle!", file=sys.stderr, flush=True)
                # Mash A to get through blackout
                for _ in range(50):
                    self.press("a")
                return
            self.battle_turn()

    def battle_turn(self) -> None:
        # Prefer the grinder's robust implementation when available — it
        # has a wider DAMAGING_MOVE_IDS set, better dialog flushing, and
        # has been driven through 95+ wild + several trainer battles
        # successfully across all three ROMs. Lazy import avoids the
        # circular dependency at module load (grind imports rtb).
        try:
            from grind import _battle_turn as _grind_battle_turn
            _grind_battle_turn(self, force_fight=True)
            return
        except ImportError:
            pass
        # Step 1: Advance dialog until main menu (max=3, unlocked)
        for _ in range(60):
            gs = self.gs()
            if not gs.battle.active:
                return
            mx = self.sym.read_u8(self.mem, "wMaxMenuItem")
            if not self.joy_locked() and mx == 3:
                break
            self.press("a")

        # Step 2: Ensure cursor at FIGHT (slot 0)
        self.press("up")
        self.press("up")
        self.press("left")

        # Step 3: Select FIGHT
        self.press("a", step=60)

        # Step 4: In move menu — find move with PP > 0 (prefer damaging)
        pp = self.read_pp()
        moves = list(self.gs().party.mons[0].moves) if self.gs().party.mons else [0, 0, 0, 0]
        # Pick target slot
        target = None
        # First pass: damaging moves with PP
        for i in range(4):
            if pp[i] > 0 and moves[i] in DAMAGING_MOVE_IDS:
                target = i
                break
        # Second pass: any move with PP
        if target is None:
            for i in range(4):
                if pp[i] > 0:
                    target = i
                    break
        if target is None:
            # Struggle — just press A
            self.press("a", step=90)
            return

        # Step 5: Navigate to target slot. Reset cursor to 0 first.
        for _ in range(4):
            self.press("up")
        for _ in range(target):
            self.press("down")
        # Confirm move
        self.press("a", step=90)

        # Step 6: Advance turn outcome dialog
        for _ in range(40):
            gs = self.gs()
            if not gs.battle.active:
                return
            mx = self.sym.read_u8(self.mem, "wMaxMenuItem")
            if not self.joy_locked() and mx == 3:
                return  # back to main menu for next turn
            self.press("a", step=20)

    # ---- Phases --------------------------------------------------------

    def _down_or_a(self, attempts: int = 1) -> None:
        """Basic overworld step."""
        if self.joy_locked() or self.gs().text.dest_in_vram_tilemap:
            self.press("a")
        else:
            self.press("down")

    def run_intro(self) -> None:
        """Boot → Bedroom with control."""
        self.idle(300)
        self.press("start", step=60)
        self.press("a", step=60)
        # DOWN+A alternating with motion probe
        for _ in range(300):
            gs = self.gs()
            if gs.overworld.map_id == M_REDS_2F and not self.joy_locked():
                before = (gs.overworld.x, gs.overworld.y)
                self.press("down", step=20)
                after = self.gs()
                if (after.overworld.x, after.overworld.y) != before:
                    self.idle(16)
                    return
            if self.joy_locked():
                self.idle(60)
                continue
            self.press("down", step=20)
            self.press("a", step=30)

    def run_bedroom_to_pallet(self) -> None:
        # 2F stairs
        for d in ["down", "left"] + ["up"]*5 + ["right"]*5 + ["up"]:
            self.press(d)
        for _ in range(20):
            if self.gs().overworld.map_id == M_REDS_1F:
                break
            self.idle(30)
        # 1F out front door
        for _ in range(15):
            gs = self.gs()
            if gs.overworld.map_id == M_PALLET: break
            if gs.overworld.y >= 7: break
            self.press("down")
        for _ in range(8):
            gs = self.gs()
            if gs.overworld.map_id == M_PALLET: break
            if gs.overworld.x <= 3: break
            self.press("left")
        for _ in range(4):
            if self.gs().overworld.map_id == M_PALLET: break
            self.press("down")
        for _ in range(10):
            if self.gs().overworld.map_id == M_PALLET: break
            self.idle(30)

    def run_pallet_to_lab(self) -> None:
        """Walk to (10, 1), Oak intercepts."""
        self.idle(60)
        for _ in range(5): self.press("right")
        for _ in range(5): self.press("up")
        # Wait for Oak script, mash A
        for _ in range(120):
            gs = self.gs()
            if gs.overworld.map_id == M_OAKS_LAB:
                self.idle(60)
                if self.gs().overworld.map_id == M_OAKS_LAB:
                    return
            self.press("a", step=60)

    def run_pick_starter(self) -> None:
        if self.gs().overworld.map_id != M_OAKS_LAB:
            print("WARN: not in lab", file=sys.stderr)
            return
        # Mash A to advance Oak's speech; detect control via single DOWN probe
        for _ in range(80):
            if not self.joy_locked():
                gs_before = self.gs()
                self.press("down", step=30)
                gs_after = self.gs()
                if (gs_after.overworld.x, gs_after.overworld.y) != (gs_before.overworld.x, gs_before.overworld.y):
                    break  # we moved, control is ours
            self.press("a", step=30)
        # Walk to (8, 4)
        for _ in range(8):
            if self.gs().overworld.x >= 8: break
            self.press("right")
        for _ in range(4):
            gs = self.gs()
            if gs.overworld.y == 4: break
            self.press("up" if gs.overworld.y > 4 else "down")
        # Face UP, A
        self.press("up")
        self.press("a", step=60)
        # Starter dialog
        for _ in range(30):
            self.press("a", step=30)
            if self.gs().party.count > 0: break
        if self.gs().party.count == 0:
            print("WARN: no starter", file=sys.stderr)
            return
        # No nickname
        self.press("down")
        self.press("a", step=60)
        # Clear post-starter dialog
        for _ in range(60):
            self.press("a", step=30)
            gs_b = self.gs()
            self.press("left", step=30)
            gs_a = self.gs()
            if (gs_a.overworld.x, gs_a.overworld.y) != (gs_b.overworld.x, gs_b.overworld.y):
                break

    def run_rival_battle(self) -> None:
        """Walk to (5, 6) then battle."""
        # Wait for script >= 10
        for _ in range(100):
            sc = self.sym.read_u8(self.mem, "wOaksLabCurScript")
            if sc >= 10: break
            self.press("a", step=30)
        # Walk to (5, 6)
        for d in ["down", "left", "left", "left", "down"]:
            if self.gs().battle.active: break
            self.press(d)
        # Clear pre-battle dialog
        for _ in range(50):
            if self.gs().battle.active: break
            self.press("a", step=30)
        # Battle
        self.resolve_battle()
        # Exit lab
        for _ in range(80):
            gs = self.gs()
            if gs.overworld.map_id == M_PALLET: break
            if self.joy_locked():
                self.press("a", step=30)
            else:
                self.press("down", step=30)

    def run_pallet_to_viridian(self) -> None:
        """Post-rival Pallet → Route 1 → Viridian."""
        # After lab exit at (12, 11), step down
        self.press("down")
        # Path LEFT×4 UP×11 RIGHT UP×3
        for d in (["left"]*4 + ["up"]*11 + ["right"] + ["up"]*3):
            gs = self.gs()
            if gs.overworld.map_id == M_ROUTE_1: break
            if gs.battle.active: self.resolve_battle()
            self.press(d)
        # Route 1 → Viridian via BFS-like zigzag
        path = (["up"]*7 + ["left"]*3 + ["up"]*4 + ["right"]*5 + ["up"]*4 +
                ["left"]*3 + ["up"]*6 + ["right"]*5 + ["up"]*11 + ["left"]*3 + ["up"]*3)
        for d in path:
            gs = self.gs()
            if gs.overworld.map_id == M_VIRIDIAN: return
            if gs.battle.active: self.resolve_battle(); continue
            if self.joy_locked(): self.press("a", step=30); continue
            self.press(d)
        # Finisher: bump-walk up with L/R detours around ledges.
        # Route 1 is tall (~35 tiles) and has multiple ledges requiring
        # zig-zag detours, so give plenty of iterations.
        for _ in range(200):
            gs = self.gs()
            if gs.overworld.map_id == M_VIRIDIAN: return
            if gs.battle.active: self.resolve_battle(); continue
            if self.joy_locked(): self.press("a"); continue
            before = (gs.overworld.x, gs.overworld.y)
            self.press("up")
            if (self.gs().overworld.x, self.gs().overworld.y) == before:
                self.press("left"); self.press("up")
                if (self.gs().overworld.x, self.gs().overworld.y) == before:
                    self.press("right"); self.press("right"); self.press("up")

    # ---- Healing detour -----------------------------------------------

    # BFS-verified path from Viridian south entry (21, 35) to the
    # Viridian Pokemon Center door at (23, 25). 16 steps. Discovered via
    # ``scripts/bfs_route.py --goal-map 0x29``.
    VIRIDIAN_TO_PC_PATH = (
        ["up"] * 5 + ["left"] +
        ["up"] * 2 + ["left"] +
        ["up"] * 2 + ["right"] * 4 +
        ["up"]
    )

    def _hp_full(self) -> bool:
        """Is party slot 0 at full HP? Safe during heal animation."""
        try:
            m = self.gs().party.mons[0]
            return m.hp == m.max_hp
        except IndexError:
            # Party struct is transiently invalid while the heal
            # animation copies data around; treat as "not yet full".
            return False

    def heal_at_viridian_pokecenter(self) -> None:
        """From inside Viridian City, walk to PC, heal, walk back out.

        Preconditions: map_id == VIRIDIAN_CITY (0x01), player near the
        south entrance around (21, 35). Postcondition: party HP fully
        restored, player back in Viridian on the PC doorstep (23, 25)
        which is a fine launch point for :meth:`run_viridian_to_route2`.
        """
        gs = self.gs()
        if gs.overworld.map_id != M_VIRIDIAN:
            print(f"  WARN: heal_at_viridian_pokecenter called on map="
                  f"0x{gs.overworld.map_id:02x}, skipping",
                  file=sys.stderr, flush=True)
            return

        # 1) Walk to the PC door (warp into map 0x29).
        for d in self.VIRIDIAN_TO_PC_PATH:
            if self.gs().overworld.map_id == M_VIRIDIAN_POKECENTER:
                break
            self.press(d)
        # Settle after the map transition so we have fresh RAM to read.
        self.idle(60)
        if self.gs().overworld.map_id != M_VIRIDIAN_POKECENTER:
            print(f"  WARN: failed to enter PC, map="
                  f"0x{self.gs().overworld.map_id:02x}",
                  file=sys.stderr, flush=True)
            return

        # 2) Walk up to the nurse counter. The counter blocks at y=3 so
        #    we can just mash up — the last presses become no-ops.
        for _ in range(6):
            self.press("up")

        # 3) Talk to the nurse — A to open dialog.
        self.press("a")
        # Advance intro dialog until the HEAL/CANCEL menu is visible
        # (wMaxMenuItem == 1 for a 2-item menu). ``dest_in_vram_tilemap``
        # drops between writes so we wait until both the menu is up and
        # no text box is actively rendering.
        for _ in range(30):
            mx = self.sym.read_u8(self.mem, "wMaxMenuItem")
            if mx == 1 and not self.gs().text.dest_in_vram_tilemap:
                break
            self.press("a")

        # 4) Select HEAL (cursor defaults to slot 0).
        self.press("a", step=60)

        # 5) Mash A through the heal animation and dialog until HP is
        #    restored. Party RAM is transiently invalid during the
        #    animation — ``_hp_full`` swallows IndexError and returns
        #    False until the restore finishes.
        for _ in range(60):
            if self._hp_full():
                break
            self.press("a")

        # 6) After HP restoration the nurse still has a farewell line
        #    and the HEAL/CANCEL menu reappears. Mashing A loops back
        #    into another heal cycle; B is the only reliable way to
        #    dismiss both the menu and the final dialog.
        for _ in range(15):
            self.press("b", step=60)

        # 7) Walk south through the warp back into Viridian.
        for _ in range(8):
            if self.gs().overworld.map_id == M_VIRIDIAN:
                break
            self.press("down")
        self.idle(30)
        gs = self.gs()
        m = gs.party.mons[0] if gs.party.mons else None
        print(f"  heal done: map=0x{gs.overworld.map_id:02x} "
              f"xy=({gs.overworld.x},{gs.overworld.y}) "
              f"HP={m.hp}/{m.max_hp}" if m else "no party",
              file=sys.stderr, flush=True)

    def set_pokedex_flag(self) -> None:
        """RAM-write EVENT_GOT_POKEDEX to unblock Viridian Old Man."""
        base = self.sym.addr_of("wEventFlags")
        byte_off = 37 // 8  # 4
        bit = 37 % 8  # 5
        current = self.mem[base + byte_off]
        self.mem[base + byte_off] = current | (1 << bit)
        print(f"  set EVENT_GOT_POKEDEX: flags[{byte_off}] 0x{current:02x} -> 0x{self.mem[base+byte_off]:02x}",
              file=sys.stderr, flush=True)

    def run_viridian_to_route2(self) -> None:
        """Walk straight north, bumping around obstacles, into Route 2."""
        self.set_pokedex_flag()
        for _ in range(100):
            gs = self.gs()
            if gs.overworld.map_id == M_ROUTE_2: return
            if gs.battle.active: self.resolve_battle(); continue
            before = (gs.overworld.x, gs.overworld.y)
            self.press("up")
            if (self.gs().overworld.x, self.gs().overworld.y) == before:
                self.press("left"); self.press("up")
                if (self.gs().overworld.x, self.gs().overworld.y) == before:
                    self.press("right"); self.press("right"); self.press("up")

    def _step_up_with_left_detour(self, max_left: int = 6) -> bool:
        """Try to move UP one tile, detouring LEFT as needed.

        Route 2's tree-wall forces a zig-zag: at each latitude only one
        specific x has a northbound opening, and that x drifts further
        west the further north you go. Empirically (see probe_route2.py):
          (8,62) UP blocked → LEFT → (7,62) UP opens for 5 tiles
          (7,57) UP blocked → LEFT×3 → (4,57) UP opens for 9 tiles
          (4,48) UP blocked → LEFT×? → next opening
        So: try UP; if blocked, step LEFT once and retry, up to `max_left`.
        Returns True if we advanced north, False if boxed in.
        """
        before = (self.gs().overworld.x, self.gs().overworld.y)
        self.press("up")
        if (self.gs().overworld.x, self.gs().overworld.y) != before:
            return True
        for _ in range(max_left):
            self.press("left")
            gs = self.gs()
            if (gs.overworld.x, gs.overworld.y) == before:
                # west also blocked — give up for this call
                return False
            before = (gs.overworld.x, gs.overworld.y)
            self.press("up")
            if (self.gs().overworld.x, self.gs().overworld.y) != before:
                return True
        return False

    # BFS-verified path from Route 2 south entry (8, 71) to Forest South
    # Gate door at (3, 43). Discovered via scripts/bfs_route.py. 41 steps.
    ROUTE2_TO_FOREST_GATE_PATH = (
        ["up"] * 9 + ["left"] +           # (8,71) → (8,62) → (7,62)
        ["up"] * 5 + ["left"] * 2 +       # (7,62) → (7,57) → (5,57)
        ["up"] + ["left"] +               # (5,57) → (5,56) → (4,56)
        ["up"] * 7 + ["right"] * 4 +      # (4,56) → (4,49) → (8,49)
        ["up"] * 4 + ["left"] * 5 +       # (8,49) → (8,45) → (3,45)
        ["up"]                             # (3,45) → (3,44) warp to 0x32
    )

    def run_route2_to_forest(self) -> None:
        """Route 2 (8, 71) → Viridian Forest south gate (map 0x32).

        Uses the BFS-verified 41-step path in :attr:`ROUTE2_TO_FOREST_GATE_PATH`.
        Handles wild-battle interrupts by running the existing battle resolver.
        """
        DIR_NAMES = {"up", "down", "left", "right"}
        for d in self.ROUTE2_TO_FOREST_GATE_PATH:
            # check for map transition — we're done
            gs = self.gs()
            if gs.overworld.map_id in (M_VIRIDIAN_FOREST_SOUTH_GATE,
                                       M_VIRIDIAN_FOREST):
                break
            if gs.battle.active:
                self.resolve_battle()
            if self.joy_locked():
                # advance any dialog (shouldn't happen on Route 2, but safe)
                for _ in range(5):
                    self.press("a")
                    if not self.joy_locked(): break
            assert d in DIR_NAMES
            self.press(d)
        # Push through the gate interior (map 0x32) into forest (0x33).
        # BFS-verified: gate entry (4,1) → RIGHT then UP → forest.
        for d in ["right", "up"] + ["up"] * 3:
            if self.gs().overworld.map_id == M_VIRIDIAN_FOREST: break
            if self.gs().battle.active: self.resolve_battle(); continue
            self.press(d)

    def run_forest_traversal(self) -> None:
        """Forest is 17x24 blocks = 34x48 tiles. Enter at bottom, exit at top via north gate."""
        for _ in range(700):
            gs = self.gs()
            if gs.overworld.map_id == M_VIRIDIAN_FOREST_NORTH_GATE: break
            if gs.overworld.map_id not in (M_VIRIDIAN_FOREST, M_VIRIDIAN_FOREST_NORTH_GATE):
                print(f"  forest exited to map=0x{gs.overworld.map_id:02x}", file=sys.stderr)
                break
            if gs.battle.active: self.resolve_battle(); continue
            if self.joy_locked(): self.press("a"); continue
            before = (gs.overworld.x, gs.overworld.y)
            # Prefer UP, then LEFT if blocked
            if gs.overworld.x > 4:
                self.press("left")
            else:
                self.press("up")
            if (self.gs().overworld.x, self.gs().overworld.y) == before:
                # Bump around
                for d in ["up","left","up","right","up"]:
                    self.press(d)
                    if (self.gs().overworld.x, self.gs().overworld.y) != before: break

    def run_forest_gate_to_pewter(self) -> None:
        """Exit north gate of forest into Route 2 (northern section), then north to Pewter."""
        # In the gate, walk UP through
        for _ in range(15):
            gs = self.gs()
            if gs.overworld.map_id == M_ROUTE_2: break
            self.press("up")
        # Route 2 north to Pewter
        for _ in range(100):
            gs = self.gs()
            if gs.overworld.map_id == M_PEWTER: return
            if gs.battle.active: self.resolve_battle(); continue
            before = (gs.overworld.x, gs.overworld.y)
            self.press("up")
            if (self.gs().overworld.x, self.gs().overworld.y) == before:
                self.press("left"); self.press("up")
                if (self.gs().overworld.x, self.gs().overworld.y) == before:
                    self.press("right"); self.press("right"); self.press("up")

    def run_pewter_to_gym(self) -> None:
        """Pewter Gym is on the west side. Enter from south."""
        # Pewter Gym warp approximately at (16, 18) in Pewter
        # Navigate to it
        for _ in range(200):
            gs = self.gs()
            if gs.overworld.map_id == M_PEWTER_GYM: return
            if gs.battle.active: self.resolve_battle(); continue
            before = (gs.overworld.x, gs.overworld.y)
            # Target (16, 18) — navigate there
            target_x, target_y = 16, 18
            if gs.overworld.x < target_x:
                self.press("right")
            elif gs.overworld.x > target_x:
                self.press("left")
            elif gs.overworld.y < target_y:
                self.press("down")
            elif gs.overworld.y > target_y:
                self.press("up")
            else:
                self.press("up")  # enter
            if (self.gs().overworld.x, self.gs().overworld.y) == before:
                # bump
                self.press("down"); self.press("right")

    def run_brock_battle(self) -> None:
        """In gym: walk up to Brock, fight."""
        # Mash UP to reach Brock, handle Jr. Trainer on the way
        for _ in range(80):
            gs = self.gs()
            if gs.battle.active:
                self.resolve_battle()
                continue
            if self.joy_locked():
                self.press("a")
                continue
            # Check if we got the badge
            if gs.progress.badges_raw & 0x01:
                print("  BOULDER BADGE OBTAINED!", file=sys.stderr)
                return
            self.press("up")
        # Post-battle dialog
        for _ in range(60):
            gs = self.gs()
            if gs.progress.badges_raw & 0x01: return
            self.press("a", step=30)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--outdir", default="walkthrough_output")
    args = parser.parse_args()

    rom = os.environ.get("POKERED_ROM_PATH")
    sym = os.environ.get("POKERED_SYM_PATH")
    if not (rom and sym):
        raise SystemExit("need POKERED_ROM_PATH, POKERED_SYM_PATH")

    session = Session.from_files(rom, sym,
        expected_rom_sha1=os.environ.get(
            "POKERED_ROM_SHA1", "ea9bcae617fdf159b045185467ae58b2e4a48b9a"
        ))
    register_default_hooks(session)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    milestones = outdir / "milestones"
    milestones.mkdir(exist_ok=True)

    drv = Driver(session)
    # Drive early-game phases through the walkthrough.py driver so we
    # reuse its verified button sequences (Route 1 zig-zag etc.). The
    # walkthrough driver writes per-press PNG/state files; point it at a
    # scratch subdir so run_to_brock's main artifacts stay clean.
    wt_outdir = outdir / "walkthrough_frames"
    wt_drv = wt.WalkthroughDriver(session=session, outdir=wt_outdir)

    def save(name: str) -> None:
        (milestones / f"{name}.state").write_bytes(session.save_state())
        print(f"  saved {name}.state", file=sys.stderr)

    phases = [
        ("intro", lambda: wt.run_phase_intro(wt_drv)),
        ("exit_house", lambda: wt.run_phase_exit_house(wt_drv)),
        ("oak_intercept", lambda: wt.run_phase_oak_intercept(wt_drv)),
        ("pick_starter", lambda: wt.run_phase_pick_starter(wt_drv)),
        ("rival_battle", lambda: wt.run_phase_rival_battle(wt_drv)),
        ("pallet_to_route1", lambda: wt.run_phase_pallet_to_route1(wt_drv)),
        ("route1_to_viridian", lambda: wt.run_phase_route1_to_viridian(wt_drv)),
        ("viridian_to_route2", drv.run_viridian_to_route2),
        ("route2_to_forest", drv.run_route2_to_forest),
        ("forest_traversal", drv.run_forest_traversal),
        ("forest_to_pewter", drv.run_forest_gate_to_pewter),
        ("pewter_to_gym", drv.run_pewter_to_gym),
        ("brock_battle", drv.run_brock_battle),
    ]

    try:
        for name, fn in phases:
            print(f"\n=== {name} ===", file=sys.stderr, flush=True)
            fn()
            save(name)
            gs = session.read_game_state()
            total_presses = drv.press_count + wt_drv.press_index
            print(f"  → map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y}) "
                  f"presses={total_presses} party={gs.party.count} "
                  f"badges=0x{gs.progress.badges_raw:02x}",
                  file=sys.stderr, flush=True)
            if gs.progress.badges_raw & 0x01:
                print(f"\nBOULDER BADGE at {total_presses} presses!", file=sys.stderr)
                break
    finally:
        gs = session.read_game_state()
        total_presses = drv.press_count + wt_drv.press_index
        print("\n=== FINAL ===", file=sys.stderr)
        print(f"map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y}) "
              f"presses={total_presses} badges=0x{gs.progress.badges_raw:02x}",
              file=sys.stderr)
        if gs.party.mons:
            m = gs.party.mons[0]
            print(f"Bulba: L{m.level} HP{m.hp}/{m.max_hp} moves={m.moves} pp={m.pp}",
                  file=sys.stderr)
        session.close()


if __name__ == "__main__":
    main()
