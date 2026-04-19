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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from pokered_harness.session import Session
from pokered_harness.mcp_server import register_default_hooks

import run_to_brock as rtb
import full_to_brock as ftb


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
    p.add_argument("--stop-after", default="receive_pikachu",
                   choices=["intro", "exit_house", "oak_intercept",
                            "receive_pikachu"])
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
