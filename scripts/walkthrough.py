"""Scripted playthrough: Pokémon Red, fresh boot → Viridian City.

Run:

    POKERED_ROM_PATH=... POKERED_SYM_PATH=... python scripts/walkthrough.py

Outputs (gitignored) land in ``walkthrough_output/``:

* ``press_NNNN.png``           — framebuffer screenshot after the press
* ``press_NNNN.state``         — PyBoy save state (bytes)
* ``press_NNNN.json``          — parsed GameState snapshot
* ``playthrough.md``           — human-readable log, one row per press
* ``playthrough.json``         — machine-readable log

Design: the script is a *linear* tour. Every map layout and trigger tile
was empirically verified against the pinned ROM via targeted BFS probes
(see scripts/WALKTHROUGH_README.md). The phases below are ordered
chains of button presses with state-based waits only where the engine
takes asynchronous control (cutscenes, battles).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from pokered_harness.mcp_server import register_default_hooks
from pokered_harness.serialize import to_jsonable
from pokered_harness.session import Session

# Map constants (from pret/pokered/constants/map_constants.asm).
MAP_PALLET_TOWN = 0x00
MAP_VIRIDIAN_CITY = 0x01
MAP_ROUTE_1 = 0x0C
MAP_REDS_HOUSE_1F = 0x25
MAP_REDS_HOUSE_2F = 0x26
MAP_OAKS_LAB = 0x28

MAP_NAMES = {
    MAP_PALLET_TOWN: "PALLET_TOWN",
    MAP_VIRIDIAN_CITY: "VIRIDIAN_CITY",
    MAP_ROUTE_1: "ROUTE_1",
    MAP_REDS_HOUSE_1F: "REDS_HOUSE_1F",
    MAP_REDS_HOUSE_2F: "REDS_HOUSE_2F",
    MAP_OAKS_LAB: "OAKS_LAB",
}


@dataclass
class WalkthroughDriver:
    session: Session
    outdir: Path
    press_index: int = 0
    log_rows: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.outdir.mkdir(parents=True, exist_ok=True)

    def press(self, button: str, *, note: str = "", step_ticks: int = 24,
              duration: int = 6) -> dict:
        self.press_index += 1
        self.session.press(button, duration=duration)
        self.session.step(step_ticks, render=True)
        stem = f"press_{self.press_index:04d}"
        self.session._pyboy.screen.image.save(  # type: ignore[attr-defined]
            self.outdir / f"{stem}.png"
        )
        (self.outdir / f"{stem}.state").write_bytes(self.session.save_state())
        gs = self.session.read_game_state()
        (self.outdir / f"{stem}.json").write_text(
            json.dumps(to_jsonable(gs), indent=2)
        )
        row = {
            "n": self.press_index, "button": button, "note": note,
            "tick": self.session.current_tick(),
            "map_id": gs.overworld.map_id,
            "map_name": MAP_NAMES.get(gs.overworld.map_id, f"0x{gs.overworld.map_id:02x}"),
            "xy": [gs.overworld.x, gs.overworld.y],
            "in_battle": gs.battle.active,
            "party": gs.party.count,
        }
        self.log_rows.append(row)
        print(
            f"#{row['n']:04d} {button:>5} tick={row['tick']:>5} "
            f"map={row['map_name']} xy={tuple(row['xy'])} "
            f"party={row['party']} battle={gs.battle.kind.name if gs.battle.kind else '-'} {note}",
            file=sys.stderr,
        )
        return row

    def idle(self, ticks: int, *, render: bool = False) -> None:
        self.session.step(ticks, render=render)

    def input_locked(self) -> bool:
        if "wJoyIgnore" not in self.session.symbols:
            return False
        mem = self.session._pyboy.memory  # type: ignore[attr-defined]
        return self.session.symbols.read_u8(mem, "wJoyIgnore") != 0

    def write_log(self) -> None:
        md = self.outdir / "playthrough.md"
        with md.open("w", encoding="utf-8") as f:
            f.write("# Playthrough log\n\n")
            f.write(f"Total presses: **{self.press_index}**  \n")
            f.write(f"Final tick: **{self.session.current_tick()}**\n\n")
            f.write("| # | Button | Tick | Map | XY | Party | Battle | Note |\n")
            f.write("|---|---|---|---|---|---|---|---|\n")
            for r in self.log_rows:
                f.write(
                    f"| {r['n']} | {r['button']} | {r['tick']} | {r['map_name']} | "
                    f"{tuple(r['xy'])} | {r['party']} | "
                    f"{'Y' if r['in_battle'] else ''} | {r['note']} |\n"
                )
        (self.outdir / "playthrough.json").write_text(
            json.dumps(self.log_rows, indent=2)
        )


# ---------------------------------------------------------------------------
# Phase sequences. Each is a list of (button, note, step_ticks?) tuples or
# a callable for async waits.
# ---------------------------------------------------------------------------


def run_phase_intro(drv: WalkthroughDriver) -> None:
    """Title screen → Oak intro → naming menus → bedroom control."""
    drv.idle(300, render=False)
    drv.press("start", note="title", step_ticks=60)
    drv.press("a", note="NEW GAME", step_ticks=60)

    # Alternating DOWN+A is safe on both dialog (DOWN no-op) and
    # preset menus (DOWN selects RED/BLUE). 300-iter cap with the
    # "actually-moved" probe as escape.
    for _ in range(300):
        # Quick motion probe to detect control.
        gs_before = drv.session.read_game_state()
        if gs_before.overworld.map_id == MAP_REDS_HOUSE_2F and not drv.input_locked():
            drv.press("down", note="bedroom: probe", step_ticks=20)
            gs = drv.session.read_game_state()
            if (gs.overworld.x, gs.overworld.y) != (gs_before.overworld.x, gs_before.overworld.y):
                drv.idle(16, render=False)
                return
        if drv.input_locked():
            drv.idle(60, render=False)
            continue
        drv.press("down", note="intro advance D", step_ticks=20)
        drv.press("a", note="intro advance A", step_ticks=30)


def run_phase_exit_house(drv: WalkthroughDriver) -> None:
    """Bedroom (3, 6) → stairs (7, 1) → 1F → Pallet Town.

    Verified paths:
      bedroom: DOWN LEFT UP×5 RIGHT×5 UP (warp at (7, 1))
      1F: from warp-in, DOWN×5 LEFT×5 DOWN (warp at (3, 7))
    """
    # 2F → stairs
    for d in ["down", "left"] + ["up"] * 5 + ["right"] * 5 + ["up"]:
        drv.press(d, note=f"bedroom: {d}")
    # Wait for warp fade
    for _ in range(20):
        if drv.session.read_game_state().overworld.map_id == MAP_REDS_HOUSE_1F:
            break
        drv.idle(30, render=False)
    # 1F → front door. First 3 DOWN presses after warp-in are no-ops
    # (warp settle), so use condition-based walking to reach y=7.
    for _ in range(15):
        gs = drv.session.read_game_state()
        if gs.overworld.map_id == MAP_PALLET_TOWN: break
        if gs.overworld.y >= 7: break
        drv.press("down", note="1F → south wall")
    # Walk LEFT to door column (x=3 or x=2).
    for _ in range(8):
        gs = drv.session.read_game_state()
        if gs.overworld.map_id == MAP_PALLET_TOWN: break
        if gs.overworld.x <= 3: break
        drv.press("left", note="1F → door col")
    # Step DOWN through the door.
    for _ in range(4):
        gs = drv.session.read_game_state()
        if gs.overworld.map_id == MAP_PALLET_TOWN: break
        drv.press("down", note="1F → door warp")
    # Wait for pallet fade-in
    for _ in range(20):
        if drv.session.read_game_state().overworld.map_id == MAP_PALLET_TOWN:
            break
        drv.idle(30, render=False)


def run_phase_oak_intercept(drv: WalkthroughDriver) -> None:
    """Pallet (5, 6) → north trigger (10, 1) → Oak warps us into lab.

    Oak's script (PalletTown_Script states 1→3→4) runs an automated
    sprite walk that teleports both the player and Oak into the lab.
    We need to mash A to advance the "OAK: Hey! Wait!" dialog AND
    idle to let the sprite animations play. Keep doing both until
    we're inside OAKS_LAB with input control regained.
    """
    drv.idle(60, render=False)
    for _ in range(5):
        drv.press("right", note="pallet → intercept row")
    for _ in range(5):
        drv.press("up", note="pallet → intercept (10, 1)")
    # Drive the script: A when locked (to skip text), idle when freed.
    for _ in range(200):
        gs = drv.session.read_game_state()
        if gs.overworld.map_id == MAP_OAKS_LAB and not drv.input_locked():
            # Extra patience: give a bit more idle for Oak to finish
            # walking to his spot, then check again.
            drv.idle(60, render=False)
            if drv.session.read_game_state().overworld.map_id == MAP_OAKS_LAB:
                return
        if drv.input_locked():
            # Sprite animation: just idle
            drv.idle(30, render=False)
        else:
            # Dialog: advance with A
            drv.press("a", note="Oak intercept dialog", step_ticks=30)


def run_phase_pick_starter(drv: WalkthroughDriver) -> None:
    """Inside Oak's lab: clear his speech, walk to Bulbasaur at (8, 3),
    press A, confirm, skip nickname.

    Precondition: player is inside OAKS_LAB. Bails out immediately if
    not, so we don't pollute other maps with rogue A/UP/RIGHT presses.
    """
    if drv.session.read_game_state().overworld.map_id != MAP_OAKS_LAB:
        print("WARN: pick_starter called outside OAKS_LAB", file=sys.stderr)
        return

    # Mash A through Oak's "here are 3 Pokéballs" speech. Control is
    # restored when the player can move (probed by trying DOWN and
    # checking position change). Try at least 40 A presses first.
    for _ in range(40):
        drv.press("a", note="lab: Oak speech", step_ticks=30)
        if not drv.input_locked():
            # Try movement probe
            gs_before = drv.session.read_game_state()
            drv.press("down", note="lab: control probe", step_ticks=20)
            gs_after = drv.session.read_game_state()
            if (gs_after.overworld.x, gs_after.overworld.y) != (
                gs_before.overworld.x, gs_before.overworld.y
            ):
                break

    # Walk to (8, 4) — one south of Bulbasaur ball at (8, 3).
    for _ in range(6):
        gs = drv.session.read_game_state()
        if gs.overworld.x >= 8: break
        drv.press("right", note="lab → Bulbasaur row")
    # Adjust y if needed (want y=4).
    for _ in range(3):
        gs = drv.session.read_game_state()
        if gs.overworld.y <= 4: break
        drv.press("up", note="lab → Bulbasaur col")
    for _ in range(3):
        gs = drv.session.read_game_state()
        if gs.overworld.y >= 4: break
        drv.press("down", note="lab → Bulbasaur col")
    # Face UP toward ball.
    drv.press("up", note="lab: face Bulbasaur")
    # Interact.
    drv.press("a", note="pick Bulbasaur", step_ticks=60)

    # Starter pickup: ~13 A presses trigger party[0] = Bulbasaur.
    for _ in range(25):
        drv.press("a", note="starter dialog", step_ticks=30)
        if drv.session.read_game_state().party.count > 0:
            break
    # "Give a nickname?" prompt: DOWN + A selects "NO".
    drv.press("down", note="no nickname")
    drv.press("a", note="no nickname confirm", step_ticks=60)
    # Post-starter chatter.
    for _ in range(40):
        drv.press("a", note="post-starter", step_ticks=30)
        gs = drv.session.read_game_state()
        if not drv.input_locked():
            # Probe: pressing A during dialog advances; during control
            # it does nothing. Try DOWN to detect.
            before = (gs.overworld.x, gs.overworld.y)
            drv.press("down", note="probe DOWN", step_ticks=20)
            after = drv.session.read_game_state()
            if (after.overworld.x, after.overworld.y) != before:
                break


def run_phase_rival_battle(drv: WalkthroughDriver) -> None:
    """Walk to (5, 6) in the lab to trigger rival challenge, then
    mash A through the battle."""
    # From (8, 4) after probe: DOWN LEFT×3 DOWN → (5, 6).
    for d in ["down", "left", "left", "left", "down"]:
        drv.press(d, note=f"rival-trigger {d}")
    # Wait for battle to start.
    for _ in range(20):
        gs = drv.session.read_game_state()
        if gs.battle.active:
            break
        drv.idle(60, render=False)
    # Battle loop.
    for i in range(400):
        gs = drv.session.read_game_state()
        if not gs.battle.active:
            break
        drv.press("a", note=f"rival battle ~{i}", step_ticks=30)
    # Post-battle: Oak's "Go talk to your rival" etc. Walk DOWN to exit.
    for _ in range(80):
        gs = drv.session.read_game_state()
        if gs.overworld.map_id == MAP_PALLET_TOWN:
            break
        if drv.input_locked():
            drv.press("a", note="post-rival dialog", step_ticks=30)
        else:
            drv.press("down", note="lab → exit", step_ticks=30)


def run_phase_pallet_to_route1(drv: WalkthroughDriver) -> None:
    """Pallet (post-lab-exit at around (12, 12)) → Route 1.

    Verified path: LEFT×3 UP×10 RIGHT UP×3 — transitions at (10, 35)
    in Route 1.
    """
    gs = drv.session.read_game_state()
    if gs.overworld.map_id != MAP_PALLET_TOWN:
        print(f"WARN: not in Pallet, map=0x{gs.overworld.map_id:02x}",
              file=sys.stderr)
        return
    path = ["left"] * 3 + ["up"] * 10 + ["right"] + ["up"] * 3
    for d in path:
        gs = drv.session.read_game_state()
        if gs.overworld.map_id == MAP_ROUTE_1:
            break
        drv.press(d, note=f"pallet → Route 1 ({d})")


def run_phase_route1_to_viridian(drv: WalkthroughDriver) -> None:
    """Route 1 → Viridian City via BFS-verified zig-zag path.

    The exact 55-move sequence was discovered by the BFS probe in
    scripts/WALKTHROUGH_README.md. It dodges ledges (which only allow
    southbound jumps) by going west then east around each one.
    """
    path = (
        ["up"] * 7 + ["left"] * 3 + ["up"] * 4 +
        ["right"] * 5 + ["up"] * 4 + ["left"] * 3 +
        ["up"] * 6 + ["right"] * 5 + ["up"] * 11 +
        ["left"] * 3 + ["up"] * 3
    )
    for d in path:
        gs = drv.session.read_game_state()
        if gs.overworld.map_id == MAP_VIRIDIAN_CITY:
            return
        if gs.battle.active:
            # Mash A to use Tackle; Bulbasaur usually OHKOs Route 1 mons.
            for _ in range(100):
                g = drv.session.read_game_state()
                if not g.battle.active:
                    break
                drv.press("a", note="wild battle", step_ticks=30)
            continue
        if drv.input_locked():
            drv.press("a", note="route locked dialog", step_ticks=30)
            continue
        drv.press(d, note=f"Route 1 → Viridian ({d})")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


PHASES = [
    ("intro",           run_phase_intro),
    ("exit_house",      run_phase_exit_house),
    ("oak_intercept",   run_phase_oak_intercept),
    ("pick_starter",    run_phase_pick_starter),
    ("rival_battle",    run_phase_rival_battle),
    ("pallet_to_route1", run_phase_pallet_to_route1),
    ("route1_to_viridian", run_phase_route1_to_viridian),
]

STOP_AFTER = {name for name, _ in PHASES}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outdir", default="walkthrough_output")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--stop-after", choices=list(STOP_AFTER), default="route1_to_viridian")
    args = parser.parse_args()

    rom = os.environ.get("POKERED_ROM_PATH")
    sym = os.environ.get("POKERED_SYM_PATH")
    if not (rom and sym):
        print("set POKERED_ROM_PATH and POKERED_SYM_PATH", file=sys.stderr)
        return 2

    outdir = Path(args.outdir)
    if args.clean and outdir.exists():
        shutil.rmtree(outdir)

    session = Session.from_files(
        rom, sym,
        expected_rom_sha1=os.environ.get(
            "POKERED_ROM_SHA1", "ea9bcae617fdf159b045185467ae58b2e4a48b9a"
        ),
    )
    register_default_hooks(session)
    drv = WalkthroughDriver(session=session, outdir=outdir)

    try:
        for name, phase in PHASES:
            print(f"\n=== phase: {name} ===", file=sys.stderr)
            phase(drv)
            if name == args.stop_after:
                break
    finally:
        drv.write_log()
        final = session.read_game_state()
        print(
            f"\nFinal: map=0x{final.overworld.map_id:02x} "
            f"({MAP_NAMES.get(final.overworld.map_id, '?')}) "
            f"xy=({final.overworld.x},{final.overworld.y}) "
            f"party={final.party.count} presses={drv.press_index}",
            file=sys.stderr,
        )
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
