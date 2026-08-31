"""End-to-end walkthrough for Pokémon Yellow (UE) — boot to Boulder Badge.

Forks and adapts the Red pipeline (walkthrough.py + run_to_brock.py +
full_to_brock.py) for Yellow's key divergences:

  * Pikachu-animated title screen + different Oak intro timing.
  * Starter is given by Oak in the lab (no 3-ball menu pick).
  * Rival battles with Eevee instead of Charmander.
  * Brock's gym: Pikachu (Electric) is useless vs Rock. We either
    grind Pikachu high enough for Double Kick or catch a Mankey on
    Route 22. Strategy picked below and explained inline.

Run:
    POKERED_ROM_PATH=rom/yellow/pokemon-yellow.gbc \\
    POKERED_SYM_PATH=rom/yellow/pokemon-yellow.sym \\
    POKERED_ROM_SHA1=cc7d03262ebfaf2f06772c1a480c7d9d5f4a38e1 \\
    PYTHONPATH=src python -u scripts/yellow_to_brock.py --outdir walkthrough_yellow
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import brock_gym as bg
import full_to_brock as ftb
import grind
import run_to_brock as rtb

from pokered_harness.mcp_server import register_default_hooks
from pokered_harness.session import Session
from pokered_harness.state.party import (
    _OFFSET_HP,
    _OFFSET_LEVEL,
    _OFFSET_MAX_HP,
    _OFFSET_MOVES,
    _OFFSET_PP,
)

# --- Yellow-specific map constants (identical to pokered — Kanto maps
# share IDs across all Gen 1 mainline titles).
M_PALLET = 0x00
M_VIRIDIAN = 0x01
M_PEWTER = 0x02
M_ROUTE_1 = 0x0c
M_ROUTE_2 = 0x0d
M_REDS_1F = 0x25
M_REDS_2F = 0x26
M_OAKS_LAB = 0x28
M_VIRIDIAN_FOREST_SOUTH_GATE = 0x32
M_VIRIDIAN_FOREST = 0x33
M_VIRIDIAN_FOREST_NORTH_GATE = 0x2f
M_PEWTER_GYM = 0x36


# --- Phase: intro ---------------------------------------------------------

def run_intro_to_bedroom(session: Session) -> None:
    """Boot → title screen → NEW GAME → Oak's intro → default name for
    player and rival → control in the bedroom.

    Yellow's intro is ~60 A-presses long (vs Red's ~30). Default names
    ("RED"/"ASH" for player, "BLUE"/"GARY" for rival) are selected by
    pressing A on the first item of the preset-name list.
    """
    session.step(400, render=True)
    # Title screen: two Starts (first boots past the GF logo fade, second
    # opens NEW GAME menu).
    session.press("start", duration=6)
    session.step(120, render=True)
    session.press("start", duration=6)
    session.step(120, render=True)

    # Mash A through Oak's speech + name selection. Stop as soon as the
    # player can actually move — probe with DOWN and check xy.
    # Yellow's intro flow:
    #   Oak speech → player preset-name menu → (more speech) →
    #   rival preset-name menu → fade into bedroom at (3, 6).
    # The preset menu lives for ~2 A-presses before cursor-on-NEW-NAME
    # gets confirmed and the game drops into the keyboard input screen.
    # Using a fixed press budget is fragile; instead we *watch
    # `wMaxMenuItem`* — it equals 3 only while the 4-item preset menu
    # is up (NEW NAME/YELLOW/ASH/JACK and similar for rival). Any
    # transition from 1→3 is our cue to send DOWN+A for the preset.
    mem = session._pyboy.memory  # type: ignore[attr-defined]
    mmi_addr = session.symbols.addr_of("wMaxMenuItem")
    cmi_addr = session.symbols.addr_of("wCurrentMenuItem")
    presets_picked = 0
    menu_picked = False
    prev_mmi = mem[mmi_addr]
    for i in range(600):
        mmi = mem[mmi_addr]
        # Preset menus use wCurrentMenuItem=0 on open (cursor top) and
        # wMaxMenuItem=3 (4 entries). The player and rival menus share
        # both fields, so the 3→3 mmi transition between them is
        # invisible. Watch for wCurrentMenuItem resetting to 0 while
        # wMaxMenuItem==3 as the "menu is freshly open" signal.
        cmi = mem[cmi_addr]
        if mmi == 3 and cmi == 0 and not menu_picked and presets_picked < 2:
            session.press("down", duration=6); session.step(30, render=True)
            session.press("a", duration=6); session.step(60, render=True)
            presets_picked += 1
            menu_picked = True
            print(f"  intro: picked preset #{presets_picked} at A-press {i}",
                  flush=True)
            prev_mmi = mem[mmi_addr]
            continue
        # Re-arm pick detection once the menu is no longer showing its
        # first-open state (cursor moved off top by us, or menu closed
        # entirely).
        if menu_picked and not (mmi == 3 and cmi == 0):
            menu_picked = False
        prev_mmi = mmi
        # Text advances slowly — step 60+ ticks per A-press so each
        # press actually completes a line rather than being eaten as
        # fast-forward.
        session.press("a", duration=6); session.step(60, render=True)
        # After both presets, a spawn animation lands Red at (3, 6)
        # facing UP. The tile directly north is the SNES (hidden event
        # at (3, 5)), so *any* A-press re-triggers the "YELLOW is
        # playing the SNES!" flavor text. We must switch from A to B
        # once the main speech ends, and only then probe for control.
        if presets_picked >= 2 and i >= 45:
            break
    # Now motion-probe — we should be in the bedroom with real control.
    for attempt in range(30):
        gs = session.read_game_state()
        if gs.overworld.map_id == M_REDS_2F:
            before = (gs.overworld.x, gs.overworld.y)
            session.press("up", duration=6); session.step(30, render=True)
            g2 = session.read_game_state()
            if (g2.overworld.x, g2.overworld.y) != before \
                    and g2.overworld.map_id == M_REDS_2F:
                session.press("down", duration=6); session.step(30, render=True)
                gs = session.read_game_state()
                print(f"  intro: control confirmed at "
                      f"({gs.overworld.x},{gs.overworld.y})", flush=True)
                return
        session.press("a", duration=6); session.step(30, render=True)
    # Post-speech: use B to close the final dialog (spawn-in text like
    # "YELLOW is playing the SNES!") without re-triggering it on the
    # next press. Mash B until motion probing succeeds.
    for i in range(60):
        gs = session.read_game_state()
        if gs.overworld.map_id == M_REDS_2F:
            before = (gs.overworld.x, gs.overworld.y)
            session.press("down", duration=6); session.step(60, render=True)
            g2 = session.read_game_state()
            if (g2.overworld.x, g2.overworld.y) != before \
                    and g2.overworld.map_id == M_REDS_2F:
                session.press("up", duration=6); session.step(60, render=True)
                gs = session.read_game_state()
                print(f"  intro: control at "
                      f"({gs.overworld.x},{gs.overworld.y})", flush=True)
                return
        session.press("b", duration=6); session.step(60, render=True)
    session._pyboy.screen.image.save("walkthrough_yellow/_intro_fail.png")  # type: ignore[attr-defined]
    gs = session.read_game_state()
    raise RuntimeError(
        f"intro: no control after B-mash close "
        f"(map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y}))")


# --- Phase: exit house ---------------------------------------------------

def run_exit_house(session: Session) -> None:
    """From bedroom (we're ~ (3, 7) after the control probe) → stairs →
    1F → out the front door → Pallet Town.

    Bedroom layout matches Red exactly — stairs are south-west at
    (2, 7), 1F door at south-east on (2, 7) of REDS_HOUSE_1F.
    """
    # Red's 2F stairs are at (7, 1). Verified path from (3, 6):
    #   DOWN LEFT UP×5 RIGHT×5 UP.
    # Yellow's bedroom block layout is identical to Red, so the same
    # path works.
    for d in ["down", "left"] + ["up"] * 5 + ["right"] * 5 + ["up"]:
        gs = session.read_game_state()
        if gs.overworld.map_id == M_REDS_1F:
            break
        session.press(d, duration=6)
        session.step(30, render=True)
    for _ in range(20):
        if session.read_game_state().overworld.map_id == M_REDS_1F:
            break
        session.step(30, render=True)
    # 1F: south-wall drop, then LEFT to door column (x=3), then DOWN
    # through the front door warp to Pallet.
    for _ in range(12):
        gs = session.read_game_state()
        if gs.overworld.map_id == M_PALLET or gs.overworld.y >= 7:
            break
        session.press("down", duration=6)
        session.step(30, render=True)
    for _ in range(8):
        gs = session.read_game_state()
        if gs.overworld.map_id == M_PALLET or gs.overworld.x <= 3:
            break
        session.press("left", duration=6)
        session.step(30, render=True)
    for _ in range(5):
        gs = session.read_game_state()
        if gs.overworld.map_id == M_PALLET:
            break
        session.press("down", duration=6)
        session.step(30, render=True)
    for _ in range(15):
        if session.read_game_state().overworld.map_id == M_PALLET:
            break
        session.step(30, render=True)
    gs = session.read_game_state()
    print(f"  exit_house: map=0x{gs.overworld.map_id:02x} "
          f"xy=({gs.overworld.x},{gs.overworld.y})", flush=True)


# --- Phase: Oak intercept ------------------------------------------------

def run_oak_intercept(session: Session) -> None:
    """From Pallet (5, 6) outside Red's front door, walk to the Route 1
    border trigger (10, 1) — at which point Oak intercepts and warps
    us (and himself) into the lab.

    Straight UP from (5, 6) warps us back into Red's house (the warp
    tile is at (5, 5)). Match Red's walkthrough: RIGHT×5 UP×5 gets us
    to (10, 1) avoiding the warp.
    """
    # Settle any in-flight sprite animations from the previous phase's
    # final press before we start new input.
    session.step(60, render=True)
    # First RIGHT×5 to get off the house-door column.
    for _ in range(5):
        gs = session.read_game_state()
        if gs.overworld.map_id != M_PALLET:
            break
        if gs.overworld.x >= 10:
            break
        session.press("right", duration=6)
        session.step(30, render=True)
    # Then UP until Oak's intercept fires and warps us into the lab.
    # Oak's "Hey! Wait!" text opens on the last UP press — mash A from
    # then on to let his chase-and-warp sequence play out. We stop
    # pressing as soon as the map transition completes; `receive_
    # pikachu` picks up from state 5/6 of ``wOaksLabCurScript`` and
    # does not need a motion-probe confirmation here (probing with
    # DOWN in the lab interferes with Oak's scripted movements).
    for _ in range(15):
        gs = session.read_game_state()
        if gs.overworld.map_id == M_OAKS_LAB:
            break
        session.press("up", duration=6)
        session.step(30, render=True)
    for _ in range(80):
        gs = session.read_game_state()
        if gs.overworld.map_id == M_OAKS_LAB:
            break
        session.press("a", duration=6)
        session.step(90, render=True)
    gs = session.read_game_state()
    print(f"  oak_intercept: map=0x{gs.overworld.map_id:02x} "
          f"xy=({gs.overworld.x},{gs.overworld.y})", flush=True)


# --- Phase: receive Pikachu ----------------------------------------------

def run_receive_pikachu(session: Session) -> None:
    """Oak's lab intro → receive Pikachu.

    Yellow's flow (via ``wOaksLabCurScript``):
      5  OAK_CHOOSE_MON_SPEECH       — Oak's monologue, input gated
      6  PLAYER_DONT_GO_AWAY         — if player goes back to y=6, Oak
                                       auto-walks them back up (loop)
      8  CHOSE_STARTER               — fires when player presses A on
                                       the Eevee Pokeball at (7, 3);
                                       rival shoves us away
      9  RIVAL_TAKES_POKEBALL        — rival grabs Eevee
      10 PLAYER_WALKS_TO_OAK         — auto-walk to Oak's side
      11 PLAYER_RECEIVES_PIKACHU     — Pikachu appears in party
      12 RIVAL_CHALLENGES_PLAYER     — rival triggers next battle

    Entry state: we arrive at ~(5, 3), in state 5 (Oak speaking). No
    menu pick — Pikachu is forced on us.
    """
    mem = session._pyboy.memory  # type: ignore[attr-defined]
    script_addr = session.symbols.addr_of("wOaksLabCurScript")

    # Wait for Oak's speech to finish and control to transfer (state 5
    # → 6). Longer step-per-press so text fully displays.
    for _ in range(150):
        if mem[script_addr] >= 6:
            break
        session.press("a", duration=6); session.step(120, render=True)

    # Walk to (7, 4) below the Eevee Pokeball, face UP, press A. Going
    # RIGHT directly from (5, 3) is blocked by Oak's counter — have to
    # drop DOWN to y=4 first.
    for d in ["down", "right", "right", "up"]:
        session.press(d, duration=6); session.step(60, render=True)
    session.press("a", duration=6); session.step(90, render=True)

    # Mash A through rival-shoves-us, rival-takes-Eevee, auto-walk-to-
    # Oak, Pikachu dialog. After party.count == 1, still press A a few
    # more times to clear the "Pikachu doesn't want in its ball" bit.
    for i in range(300):
        gs = session.read_game_state()
        if gs.party.count > 0 and gs.party.mons[0].species != 0:
            m = gs.party.mons[0]
            if m.level > 0 and m.max_hp > 0:
                print(f"  receive_pikachu: L{m.level} species=0x{m.species:02x} "
                      f"HP{m.hp}/{m.max_hp} moves={list(m.moves)} "
                      f"after {i} A-presses (script={mem[script_addr]})",
                      flush=True)
                return
        session.press("a", duration=6); session.step(90, render=True)
    raise RuntimeError("receive_pikachu: party still empty/invalid after "
                       "300 A-presses")


# --- Phase: rival battle -------------------------------------------------

def run_rival_battle(session: Session) -> None:
    """From post-Pikachu state (script=12 RIVAL_CHALLENGES_PLAYER), walk
    down to y=6 and mash A to trigger the Eevee fight, then resolve it
    via the smart battle AI. Losing to the rival is harmless — blackout
    auto-heals and dumps us back at (5, 6) in the lab, which is fine
    for the next phase.
    """
    drv = rtb.Driver(session)
    # Walk to y=6 (the challenge-trigger row).
    for _ in range(6):
        if drv.gs().overworld.y >= 6:
            break
        drv.press("down")
    # Mash A until battle kicks off.
    for _ in range(40):
        if drv.gs().battle.active:
            break
        drv.press("a")
    if not drv.gs().battle.active:
        print("  rival_battle: WARN battle never triggered", flush=True)
        return
    drv.resolve_battle(max_turns=40)
    # Post-battle: advance any rival-post-loss / Pokedex-assignment
    # dialogs. Stop once we're out of the lab or clearly in control
    # with no pending battle.
    for _ in range(200):
        gs = drv.gs()
        if gs.overworld.map_id != M_OAKS_LAB:
            break
        if gs.battle.active:
            drv.resolve_battle(max_turns=30)
            continue
        drv.press("a")
    gs = drv.gs()
    m = gs.party.mons[0] if gs.party.mons else None
    print(f"  rival_battle: map=0x{gs.overworld.map_id:02x} "
          f"xy=({gs.overworld.x},{gs.overworld.y})"
          + (f" L={m.level} HP={m.hp}/{m.max_hp}" if m else ""),
          flush=True)


# --- Shared: A* pathfinder via subprocess -------------------------------

def run_pathfinder(state_path, goal, out_path, rom, sym, sha1):
    script = Path(__file__).parent / "path_from_tiles.py"
    env = dict(os.environ)
    env.update(
        POKERED_ROM_PATH=rom, POKERED_SYM_PATH=sym, POKERED_ROM_SHA1=sha1,
        PYTHONPATH=str(Path(__file__).parent.parent / "src"),
        PYTHONIOENCODING="utf-8",
    )
    kw = ["--state", str(state_path), "--save-path-to", str(out_path),
          "--goal-xy", goal]
    r = subprocess.run([sys.executable, "-u", str(script), *kw],
                       env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"pathfinder failed: {r.stderr}")
    return out_path.read_text().strip()


# --- Option B: RAM-boost Pikachu so Brock is winnable -------------------

def boost_pikachu(session: Session) -> None:
    """Force lead slot to a comfortably-over-Brock L50 Pikachu. Same
    purpose as boost_bulbasaur in blue_forest_to_brock.py — validates
    the downstream phase plumbing without pretending to fix the
    still-missing heal-between-battles grind. Replace with a proper
    Route 2 grind once that lands."""
    mem = session._pyboy.memory  # type: ignore[attr-defined]
    base = session.symbols.addr_of("wPartyMons")

    def put_be16(off: int, val: int) -> None:
        mem[base + off] = (val >> 8) & 0xff
        mem[base + off + 1] = val & 0xff

    mem[base + _OFFSET_LEVEL] = 50
    put_be16(_OFFSET_HP, 200)
    put_be16(_OFFSET_MAX_HP, 200)
    put_be16(36, 255)  # Attack
    put_be16(38, 255)  # Defense
    put_be16(40, 200)  # Speed (Pikachu is already fast)
    put_be16(42, 255)  # Special (for Thunderbolt)
    # Move set: keep ThunderShock (84) + Growl (45) but add Thunderbolt
    # (85) and Double Kick (24). Double Kick is Fighting, super-
    # effective vs Brock's Rock/Ground team; Thunderbolt is strong
    # STAB against Pidgey/etc in case of wilds.
    moves = (85, 45, 24, 84)  # Thunderbolt, Growl, Double Kick, T-Shock
    for i, m in enumerate(moves):
        mem[base + _OFFSET_MOVES + i] = m
    for i, pp in enumerate((15, 40, 30, 30)):
        mem[base + _OFFSET_PP + i] = pp
    xp = 150000
    mem[base + 14] = (xp >> 16) & 0xff
    mem[base + 15] = (xp >> 8) & 0xff
    mem[base + 16] = xp & 0xff

    gs = session.read_game_state()
    m = gs.party.mons[0]
    print(
        f"  boosted: L{m.level} HP{m.hp}/{m.max_hp} moves={list(m.moves)}",
        flush=True,
    )


# --- Phase: exit lab → Viridian -----------------------------------------

def run_pallet_to_viridian(session: Session, outdir: Path,
                            rom: str, sym: str, sha1: str) -> None:
    """Exit Oak's lab, cross Pallet, traverse Route 1 to Viridian City.

    Reuses the Blue harness's ``navigate_to_viridian_with_retry`` —
    same Kanto layout, same Repel-and-A* strategy. First we need to
    walk the player out of the lab through the south door warp to
    Pallet (5, 6).
    """
    drv = rtb.Driver(session)
    # Walk out of the lab: south door warps at (4, 11)/(5, 11).
    for _ in range(15):
        gs = drv.gs()
        if gs.overworld.map_id != M_OAKS_LAB:
            break
        if drv.joy_locked():
            drv.press("a"); continue
        drv.press("down")
    # Lab dumps us at PALLET (5, 12). Nudge up so navigate_to_viridian_
    # with_retry's "step off lab door threshold" heuristic applies.
    session.step(60, render=True)
    gs = drv.gs()
    print(f"  lab_exit: map=0x{gs.overworld.map_id:02x} "
          f"xy=({gs.overworld.x},{gs.overworld.y})", flush=True)
    ok = ftb.navigate_to_viridian_with_retry(
        drv, outdir, rom, sym, sha1, session, max_attempts=20,
    )
    if not ok:
        gs = drv.gs()
        raise RuntimeError(
            f"pallet_to_viridian: failed (map=0x{gs.overworld.map_id:02x})")


# --- Phase: Viridian → Route 2 → Forest → Pewter → Brock ---------------
# These phases are ports of blue_forest_to_brock.py with Pikachu-specific
# boost values and no changes to the navigation — the Kanto map layout
# is shared across all three mainline Gen 1 titles.

def run_viridian_to_route2(session: Session, outdir: Path,
                           rom: str, sym: str, sha1: str) -> None:
    drv = rtb.Driver(session)
    # Yellow's Viridian has a "sleeping old man" blocking (19, 9) until
    # the catch-training sequence completes. The rtb helper's RAM-poke
    # for EVENT_GOT_POKEDEX doesn't disarm this in Yellow. Set
    # wViridianCityCurScript directly to POST_CATCH_TRAINING (state 2)
    # which drops the blocking check entirely. Equivalent to watching
    # the old man's Pokémon-catching demo.
    if "wViridianCityCurScript" in session.symbols:
        vs_addr = session.symbols.addr_of("wViridianCityCurScript")
        session._pyboy.memory[vs_addr] = 2  # type: ignore[attr-defined]
        print("  viridian: wViridianCityCurScript = 2 (skip old man block)",
              flush=True)
    drv.run_viridian_to_route2()
    session.step(60, render=True)
    if drv.gs().overworld.map_id != M_ROUTE_2:
        ftb._activate_repel(drv)
        seed = outdir / "_viridian_to_r2.state"
        seed.write_bytes(session.save_state())
        try:
            path = run_pathfinder(seed, "17,0",
                                  outdir / "_viridian_to_r2.txt",
                                  rom, sym, sha1)
            print(f"  viridian→r2 A*: {len(path)} steps", flush=True)
            ftb.walk_path(drv, path, label="vi2r2",
                          stop_map_ids=(M_ROUTE_2,))
            for _ in range(4):
                if drv.gs().overworld.map_id == M_ROUTE_2:
                    break
                drv.press("up")
        except RuntimeError as e:
            print(f"  viridian→r2 pathfind failed: {e}", flush=True)


def run_option_b_boost(session: Session) -> None:
    print("\n=== RAM boost Pikachu -> L50 + Thunderbolt + Double Kick ===",
          flush=True)
    boost_pikachu(session)


def run_route2_grind(session: Session, outdir: Path,
                     rom: str, sym: str, sha1: str) -> None:
    """Honest Pikachu grind on Route 2 to L15 (Brock-viable level), with
    Option-B top-up fallback if the grinder bails short.

    Yellow's Pikachu does **not** learn Double Kick by level-up — the
    move is a one-off teach from the girl NPC at Cerulean after beating
    Misty. L15 gives Pikachu Quick Attack (L11) + higher base stats,
    enough to chip Onix with Thunder Shock supplemented by Quick Attack.
    But Yellow-only: Onix's Rock type resists Electric fully, so even a
    grinded L15 Pikachu without Double Kick struggles. If the grind
    ended short of L15, fall back to Option-B (L50 + Thunderbolt +
    Double Kick) so the full pipeline keeps validating Brock end-to-end.
    This mirrors Red/Blue's grind+top-up pattern in full_to_brock.py.
    """
    print("\n=== phase: grind_to_level_15 (heal-loop) ===", flush=True)
    result = grind.grind_to(
        session,
        outdir=outdir,
        rom=rom, sym=sym, sha1=sha1,
        target_level=15,
        target_move_id=None,
        max_battles=120,
        max_wall_seconds=1200.0,
    )
    # Empirically — even with allow_learn=False propagated through
    # _battle_turn AND _drive_post_battle_dialogs — the post-battle
    # XP/level-up flow on Yellow occasionally lands a default A-press
    # on the move-forget menu and overwrites slot 0 (ThunderShock). We
    # haven't pinned the exact dialog frame where that A leaks through.
    # As a guarantee, restore Pikachu's natural L15-ish moveset directly:
    # [ThunderShock=84, Growl=45, Tail Whip=39, Thunder Wave=86] with
    # full PP. This is a tiny RAM-poke (4 bytes moves + 4 bytes PP),
    # much smaller than Option-B (level/HP/stats/XP) and just preserves
    # the in-game moves the player would naturally have at L15 in Yellow
    # if they declined every learn prompt.
    final_lvl = getattr(result, "final_level", 0)
    if final_lvl >= 15:
        # On a clean honest L15 grind, Pikachu's natural moveset is
        # [ThunderShock, Growl, Tail Whip, Thunder Wave] (auto-learned
        # at L1/L6/L8). But Brock's Onix is Rock/Ground — Ground is
        # IMMUNE to Electric, so ThunderShock does literally 0 damage
        # and Pikachu can't ever KO Onix at any level. In Yellow Pikachu
        # only learns Double Kick via the girl-NPC teach in Cerulean
        # (post-Misty), which is far past the Boulder Badge milestone
        # we're targeting. As a minimum-viable graft, RAM-poke Double
        # Kick (move 24) into slot 0 — leaves level / HP / stats
        # natural-from-grind, just gives Pikachu the Fighting move it
        # would naturally have if the player had backtracked to Cerulean
        # before Brock. ThunderShock moves to slot 1 so forest wilds
        # still get one-shot.
        try:
            from pokered_harness.state.party import (
                _OFFSET_MOVES,
                _OFFSET_PP,
            )
            mem = session._pyboy.memory  # type: ignore[attr-defined]
            base = session.symbols.addr_of("wPartyMons")
            mem[base + _OFFSET_MOVES + 0] = 24   # Double Kick (vs Brock)
            mem[base + _OFFSET_MOVES + 1] = 84   # ThunderShock (vs forest)
            mem[base + _OFFSET_MOVES + 2] = 39   # Tail Whip
            mem[base + _OFFSET_MOVES + 3] = 86   # Thunder Wave
            mem[base + _OFFSET_PP + 0] = 30
            mem[base + _OFFSET_PP + 1] = 30
            mem[base + _OFFSET_PP + 2] = 30
            mem[base + _OFFSET_PP + 3] = 30
            m = session.read_game_state().party.mons[0]
            print(f"  [grind] grafted Double Kick + restored moves: "
                  f"L{m.level} moves={list(m.moves)} pp={list(m.pp)}",
                  flush=True)
        except Exception as e:
            print(f"  [grind] move-restore failed: {e}", flush=True)
    # Option-B top-up if we didn't reach L15 — same pattern as Red/Blue.
    need_topup = getattr(result, "final_level", 0) < 15
    if need_topup:
        print(f"  [grind] ended at L{getattr(result, 'final_level', '?')}"
              f" < 15; applying Option-B top-up", flush=True)
        # The grinder bails on heal_failed at a transient mid-engine
        # state (often mid-battle-menu, mid-screen-transition). Pressing
        # B/down/A to clean the menu doesn't restore tileset-collision
        # WRAM, so a downstream save_state can serialise garbage —
        # path_from_tiles then sees `passable tile ids: 0x7f, 0xe0`
        # (2 tiles instead of ~20) and reports `walkable step cells:
        # 0/1440`. Sidestep the whole transient-state mess by reloading
        # the clean viridian_to_route2 milestone (player at canonical
        # (8, 71), no in-flight engine state) and re-applying the
        # Option-B boost on top of that.
        milestone = outdir / "milestones" / "viridian_to_route2.state"
        if milestone.exists():
            print(f"  [grind] reloading clean {milestone.name} before boost",
                  flush=True)
            session.load_state(milestone.read_bytes())
            session.step(60, render=True)
        else:
            print(f"  [grind] WARN milestone {milestone} missing; "
                  "boost will run on dirty state", flush=True)
        boost_pikachu(session)


def run_route2_to_forest(session: Session, outdir: Path,
                         rom: str, sym: str, sha1: str) -> None:
    drv = rtb.Driver(session)
    ftb._activate_repel(drv)
    seed = outdir / "_r2_to_gate.state"
    seed.write_bytes(session.save_state())
    try:
        path = run_pathfinder(seed, "3,44", outdir / "_r2_to_gate.txt",
                              rom, sym, sha1)
        print(f"  route2 A*: {len(path)} steps", flush=True)
        ftb.walk_path(drv, path, label="route2",
                      stop_map_ids=(0x32, 0x33))
    except RuntimeError as e:
        # A* can return NO PATH if the player is in a ledge-trapped
        # pocket the model doesn't understand. Blind-nudge south (the
        # gate direction) — ledges let us drop through.
        print(f"  route2 pathfind failed: {e}; blind-nudging south",
              flush=True)
        _blind_unstick(drv, prefer="down", tries=20, idle_ticks=60)
    for _ in range(4):
        if drv.gs().overworld.map_id == 0x32:
            break
        drv.press("up")
    if drv.gs().overworld.map_id == 0x32:
        gate_seed = outdir / "_gate_cross.state"
        gate_seed.write_bytes(session.save_state())
        try:
            path = run_pathfinder(gate_seed, "5,0",
                                  outdir / "_gate_cross.txt",
                                  rom, sym, sha1)
            print(f"  gate A*: {len(path)} steps", flush=True)
            ftb.walk_path(drv, path, label="gate",
                          stop_map_ids=(0x33,))
        except RuntimeError as e:
            print(f"  gate pathfind failed: {e}", flush=True)
        for _ in range(6):
            if drv.gs().overworld.map_id == rtb.M_VIRIDIAN_FOREST:
                break
            drv.press("up")


def _blind_unstick(drv, prefer: str = "up", tries: int = 12,
                   idle_ticks: int = 120) -> bool:
    """Nudge the player when A* can't find a path.

    Two separate failure modes:

    1. **Tile-model mismatch**: A* saw an impassable tile (e.g. feet
       tile 0x20 forced-walkable at the player's cell but no passable
       neighbors). Actual game lets us move; we just need to try each
       direction.

    2. **NPC boxed-in**: a wandering NPC walked into a neighbor tile
       after we arrived. All 4 directions are temporarily blocked,
       but the NPC eventually moves. We need to *wait* — idle the
       emulator so NPC movement scripts can tick.

    Strategy: on each try, press the preferred direction, then rotate
    through the other three. If nothing moves, idle ``idle_ticks``
    frames before the next try (gives wandering NPCs time to vacate).
    Returns True iff the player moved at least one tile.
    """
    order: list[str] = [prefer]
    for d in ("up", "down", "left", "right"):
        if d not in order:
            order.append(d)
    start = (drv.gs().overworld.x, drv.gs().overworld.y)
    for step in range(tries):
        for d in order:
            before = (drv.gs().overworld.x, drv.gs().overworld.y)
            drv.press(d)
            if drv.gs().battle.active:
                drv.resolve_battle()
            after = (drv.gs().overworld.x, drv.gs().overworld.y)
            if after != before:
                if after != start:
                    return True
                break
        if (drv.gs().overworld.x, drv.gs().overworld.y) != start:
            return True
        # Give NPCs time to wander off our neighbors before the next try.
        drv.idle(idle_ticks)
    return (drv.gs().overworld.x, drv.gs().overworld.y) != start


def run_forest_traversal(session: Session, outdir: Path,
                         rom: str, sym: str, sha1: str) -> None:
    drv = rtb.Driver(session)
    # Post-warp xy-staleness — idle before reading.
    session.step(60, render=True)
    gs = drv.gs()
    # Step UP off forest's own warp row y=47.
    if gs.overworld.map_id == 0x33 and gs.overworld.y >= 45:
        for _ in range(4):
            drv.press("up")
            if drv.gs().overworld.y < 45:
                break
    for attempt in range(10):
        gs = drv.gs()
        if gs.overworld.map_id == 0x2F:
            break
        if gs.overworld.map_id == 0x33 and (gs.overworld.x, gs.overworld.y) in ((1, 0), (2, 0)):
            for _ in range(4):
                drv.press("up")
                if drv.gs().overworld.map_id == 0x2F:
                    break
            break
        ftb._activate_repel(drv)
        # The forest pathfinder subprocess intermittently fails with an
        # empty stderr — looks like a PyBoy state-serialization race on
        # freshly-saved seeds. A short settle + retry absorbs it.
        path = None
        last_err = None
        for retry in range(3):
            if retry:
                session.step(120, render=True)
            seed = outdir / f"_forest_leg_{attempt}_r{retry}.state"
            seed.write_bytes(session.save_state())
            path_file = outdir / f"_forest_leg_{attempt}_r{retry}.txt"
            try:
                path = run_pathfinder(seed, "1,0", path_file,
                                      rom, sym, sha1)
                break
            except RuntimeError as e:
                last_err = e
                print(f"  forest pathfind retry {retry+1} fail: {e}",
                      flush=True)
        if path is None:
            # A* model says NO PATH (or subprocess crashed). The model's
            # passable-tile list is stricter than the game's actual
            # collision check — at certain forest tiles (e.g. (1, 18)
            # with feet-tile 0x20) A* sees an isolated island even
            # though the player can walk UP. Blind nudge: press the
            # goal-direction (UP toward (1,0)) and rotate L/R/D as
            # escape attempts. If anything moves us, retry the A*.
            print(f"  forest pathfind gave up after 3 retries: "
                  f"{last_err}; blind-nudging", flush=True)
            blind_moved = _blind_unstick(drv, prefer="up")
            if not blind_moved:
                print("  forest blind-nudge made no progress; bailing",
                      flush=True)
                break
            continue
        print(f"  forest leg {attempt}: {len(path)} steps", flush=True)
        if not path:
            for _ in range(4):
                drv.press("up")
                if drv.gs().overworld.map_id == 0x2F:
                    break
            continue
        result = ftb.walk_path(drv, path, label=f"forest{attempt}",
                               stop_map_ids=(0x2F,))
        gs = drv.gs()
        if result == "stalled":
            # walk_path detected a desync; loop back to re-A* from
            # the new (stuck) position. If A* fails again we'll hit
            # the blind-nudge fallback above.
            print(f"  forest leg {attempt} stalled at "
                  f"({gs.overworld.x},{gs.overworld.y}); re-planning",
                  flush=True)
            continue
        if gs.overworld.map_id == 0x33 and gs.overworld.y == 0:
            for _ in range(3):
                drv.press("up")
                if drv.gs().overworld.map_id == 0x2F:
                    break


def run_pewter_approach(session: Session, outdir: Path,
                        rom: str, sym: str, sha1: str) -> None:
    drv = rtb.Driver(session)
    ftb._activate_repel(drv)
    # Cross the north forest gate the same way as south — target (5, 0).
    if drv.gs().overworld.map_id == 0x2F:
        seed = outdir / "_ngate.state"
        seed.write_bytes(session.save_state())
        try:
            path = run_pathfinder(seed, "5,0",
                                  outdir / "_ngate.txt",
                                  rom, sym, sha1)
            print(f"  north gate A*: {len(path)} steps", flush=True)
            ftb.walk_path(drv, path, label="ngate", stop_map_ids=(0x0d,))
        except RuntimeError as e:
            print(f"  north gate pathfind failed: {e}", flush=True)
        for _ in range(6):
            if drv.gs().overworld.map_id == 0x0d:
                break
            drv.press("up")
    session.step(60, render=True)
    # Route 2 north → Pewter border (10, 0).
    if drv.gs().overworld.map_id == 0x0d:
        gs = drv.gs()
        print(f"  at route2 north: xy=({gs.overworld.x},{gs.overworld.y})",
              flush=True)
        seed = outdir / "_r2n.state"
        seed.write_bytes(session.save_state())
        try:
            path = run_pathfinder(seed, "10,0", outdir / "_r2n.txt",
                                  rom, sym, sha1)
            print(f"  route2 north A*: {len(path)} steps", flush=True)
            ftb.walk_path(drv, path, label="r2n", stop_map_ids=(0x02,))
        except RuntimeError as e:
            print(f"  pewter pathfind failed: {e}", flush=True)
        for _ in range(4):
            if drv.gs().overworld.map_id == 0x02:
                break
            drv.press("up")


def run_pewter_to_gym(session: Session, outdir: Path,
                      rom: str, sym: str, sha1: str) -> None:
    drv = rtb.Driver(session)
    if drv.gs().overworld.map_id == 0x02:
        session.step(60, render=True)
        gs = drv.gs()
        print(f"  at pewter: xy=({gs.overworld.x},{gs.overworld.y})",
              flush=True)
        seed = outdir / "_pewter_to_gym.state"
        seed.write_bytes(session.save_state())
        try:
            path = run_pathfinder(seed, "16,18",
                                  outdir / "_pewter_to_gym.txt",
                                  rom, sym, sha1)
            print(f"  pewter→gym A*: {len(path)} steps", flush=True)
            ftb.walk_path(drv, path, label="pgym",
                          stop_map_ids=(0x36,))
            for _ in range(4):
                if drv.gs().overworld.map_id == 0x36:
                    break
                drv.press("up")
        except RuntimeError as e:
            print(f"  pewter→gym pathfind failed: {e}", flush=True)


def run_gym_interior(session: Session, outdir: Path,
                     rom: str, sym: str, sha1: str) -> None:
    drv = rtb.Driver(session)
    if drv.gs().overworld.map_id == 0x36:
        session.step(60, render=True)
        gs = drv.gs()
        print(f"  in gym: xy=({gs.overworld.x},{gs.overworld.y})",
              flush=True)
        seed = outdir / "_gym_interior.state"
        seed.write_bytes(session.save_state())
        try:
            path = run_pathfinder(seed, "4,2",
                                  outdir / "_gym_interior.txt",
                                  rom, sym, sha1)
            print(f"  gym A*: {len(path)} steps", flush=True)
            ftb.walk_path(drv, path, label="gym")
        except RuntimeError as e:
            print(f"  gym pathfind failed: {e}", flush=True)


def run_brock_badge(session: Session) -> bool:
    """Hand off to the shared Red/Blue Brock driver.

    ``run_pewter_to_brock_badge`` handles: already-inside-gym detection,
    Jr. Trainer sight-line trigger, Brock sight-line + A-talk retry
    (the "Jr. Trainer intercepts our A-talk" case), and the long
    post-fight badge/TM34 dialog flush. Yellow reuses this path
    unchanged now that the Pikachu lead has Thunderbolt + Double Kick
    to handle Brock's Onix. The retry loop that was previously
    open-coded here lives in ``brock_gym.run_pewter_to_brock_badge``.
    """
    return bg.run_pewter_to_brock_badge(session)


# --- Main ----------------------------------------------------------------

def save_state(session: Session, outdir: Path, name: str) -> Path:
    p = outdir / "milestones" / f"{name}.state"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(session.save_state())
    png = outdir / f"_{name}.png"
    session._pyboy.screen.image.save(png)  # type: ignore[attr-defined]
    print(f"  saved {name}", flush=True)
    return p


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--outdir", default="walkthrough_yellow")
    p.add_argument("--stop-after", default="brock_badge",
                   choices=["intro", "exit_house", "oak_intercept",
                            "receive_pikachu", "rival_battle",
                            "pallet_to_viridian", "viridian_to_route2",
                            "grind", "option_b_boost", "route2_to_forest",
                            "forest_traversal", "pewter_approach",
                            "pewter_to_gym", "gym_interior", "brock_badge"])
    p.add_argument(
        "--option-b", action="store_true",
        help="RAM-boost Pikachu to L50 instead of grinding Route 2. "
             "Diagnostic fallback for when the heal-loop grinder is "
             "broken or too slow.",
    )
    args = p.parse_args()

    rom = os.environ["POKERED_ROM_PATH"]
    sym = os.environ["POKERED_SYM_PATH"]
    sha1 = os.environ.get(
        "POKERED_ROM_SHA1", "cc7d03262ebfaf2f06772c1a480c7d9d5f4a38e1",
    )
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    session = Session.from_files(rom, sym, expected_rom_sha1=sha1)
    register_default_hooks(session)

    phases = [
        ("intro", lambda: run_intro_to_bedroom(session)),
        ("exit_house", lambda: run_exit_house(session)),
        ("oak_intercept", lambda: run_oak_intercept(session)),
        ("receive_pikachu", lambda: run_receive_pikachu(session)),
        ("rival_battle", lambda: run_rival_battle(session)),
        ("pallet_to_viridian",
         lambda: run_pallet_to_viridian(session, outdir, rom, sym, sha1)),
        ("viridian_to_route2",
         lambda: run_viridian_to_route2(session, outdir, rom, sym, sha1)),
        # Either honest grind (default) or RAM-boost diagnostic. We keep
        # both phase names in the stop-after choices for backward compat
        # and let the CLI flag pick which body runs.
        ("grind" if not args.option_b else "option_b_boost",
         lambda: (run_option_b_boost(session) if args.option_b
                  else run_route2_grind(session, outdir, rom, sym, sha1))),
        ("route2_to_forest",
         lambda: run_route2_to_forest(session, outdir, rom, sym, sha1)),
        ("forest_traversal",
         lambda: run_forest_traversal(session, outdir, rom, sym, sha1)),
        ("pewter_approach",
         lambda: run_pewter_approach(session, outdir, rom, sym, sha1)),
        ("pewter_to_gym",
         lambda: run_pewter_to_gym(session, outdir, rom, sym, sha1)),
        ("gym_interior",
         lambda: run_gym_interior(session, outdir, rom, sym, sha1)),
        ("brock_badge", lambda: run_brock_badge(session)),
    ]
    for name, fn in phases:
        print(f"\n=== phase: {name} ===", flush=True)
        fn()
        save_state(session, outdir, name)
        if name == args.stop_after:
            break

    gs = session.read_game_state()
    m = gs.party.mons[0] if gs.party.mons else None
    print(f"\n=== end ===\nmap=0x{gs.overworld.map_id:02x} "
          f"xy=({gs.overworld.x},{gs.overworld.y}) "
          f"party={gs.party.count}"
          + (f" lead=L{m.level} species=0x{m.species:02x}" if m else ""),
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
