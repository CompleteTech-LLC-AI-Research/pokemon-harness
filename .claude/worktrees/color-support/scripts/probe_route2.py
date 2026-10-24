"""Probe Route 2 navigation from the Viridian endpoint.

Loads the `route1_to_viridian` milestone (player at Viridian City (21, 35))
and walks north to see where navigation stalls on Route 2. Logs the
(map_id, x, y, direction) after each press so the real open-tile path can
be reconstructed.

Usage:
  PYTHONPATH=src POKERED_ROM_PATH=... POKERED_SYM_PATH=... \
    POKERED_ROM_SHA1=... python scripts/probe_route2.py \
    --state walkthrough_brock/milestones/route1_to_viridian.state
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from pokered_harness.session import Session
from pokered_harness.mcp_server import register_default_hooks


def probe(session: Session, path: list[str], outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    gs = session.read_game_state()
    print(f"start: map=0x{gs.overworld.map_id:02x} xy=({gs.overworld.x},{gs.overworld.y})")
    for i, d in enumerate(path, 1):
        session.press(d, duration=6)
        session.step(24, render=True)
        gs = session.read_game_state()
        print(f"  #{i:03d} {d:5s} -> map=0x{gs.overworld.map_id:02x} "
              f"xy=({gs.overworld.x:>2},{gs.overworld.y:>2}) "
              f"dir={gs.overworld.direction} "
              f"batt={gs.battle.active}")
        # If a wild battle appears, mash A until it ends
        if gs.battle.active:
            for _ in range(80):
                session.press("a", duration=6); session.step(24, render=True)
                if not session.read_game_state().battle.active: break
            print(f"       (cleared battle)")
        # Save screenshot every 10 presses
        if i % 10 == 0 or i == len(path):
            session._pyboy.screen.image.save(outdir / f"probe_{i:03d}.png")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", required=True, help="path to a saved Session state")
    p.add_argument("--outdir", default="walkthrough_brock/probe")
    p.add_argument("--path", default="up"*60,
                   help="sequence of direction chars, e.g. 'uuullluuurr'")
    args = p.parse_args()

    rom = os.environ["POKERED_ROM_PATH"]
    sym = os.environ["POKERED_SYM_PATH"]
    session = Session.from_files(
        rom, sym,
        expected_rom_sha1=os.environ.get(
            "POKERED_ROM_SHA1", "ea9bcae617fdf159b045185467ae58b2e4a48b9a"
        ),
    )
    register_default_hooks(session)
    session.load_state(Path(args.state).read_bytes())

    # Expand shorthand: u=up, d=down, l=left, r=right, plus comma-separated
    # long-form like "up,up,left"
    tok = args.path
    if "," in tok:
        path = [t.strip() for t in tok.split(",") if t.strip()]
    else:
        MAP = {"u": "up", "d": "down", "l": "left", "r": "right"}
        path = [MAP[c] for c in tok if c in MAP]

    probe(session, path, Path(args.outdir))
    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
