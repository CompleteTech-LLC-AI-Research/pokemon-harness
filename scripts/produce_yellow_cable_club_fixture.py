"""Regenerate tests/fixtures/link/yellow/cable_club.state.

Walks a Yellow ROM from a Cerulean-Pokecenter source state to the Cable
Club link receptionist at tile (11, 3) and writes the result as the
link-cable fixture used by
:mod:`tests.test_link_integration_remote`.

The source state is expected at:

    walkthrough_to_cerulean/milestones/cerulean_pc.state

(this is produced by the sibling Yellow walkthrough harness — the
state captures the player inside Cerulean Pokecenter, map 0x40, tile
(3, 7), with correct CGB palette via the nurse-heal trigger).

Map geometry observed from that source state (tile (3,7) is the
pokecenter door, y=4 is the walkable row in front of the counter,
y=3 is the "adjacent to NPC behind counter" row):

    x=0 1 2 3 4 5 6 7 8 9 10 11 12 13
y=2   #       N L   G          C  R  #   <- counter; N=nurse, R=link receptionist
y=3   #     .   . . . . . . . .  .  #
y=4   . . . . . . . . . . . . .  .
y=7   . . . D . . . . . . . . .  .     <- start
                ^
                door out to Cerulean City

Press-A-from-(11,3) fires CableClubNPC on Yellow (confirmed by
hooking 01:7035 CableClubNPC across x=5..12 — only x=11 triggers).
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
os.environ.setdefault("POKERED_SKIP_SHA1", "1")

from pokered_harness.session import Session  # noqa: E402


# Path of the source state. Override with --source if your worktree
# layout differs.
_DEFAULT_SOURCE = (
    _REPO.parent
    / "agent-a22ef83a"
    / "walkthrough_to_cerulean"
    / "milestones"
    / "cerulean_pc.state"
)
_DEFAULT_ROM = _REPO.parents[3] / "rom" / "yellow" / "pokemon-yellow.gbc"
_DEFAULT_SYM = _REPO.parents[3] / "rom" / "yellow" / "pokemon-yellow.sym"
_DEFAULT_OUT = _REPO / "tests" / "fixtures" / "link" / "yellow" / "cable_club.state"


def produce(source: Path, rom: Path, sym: Path, out: Path) -> None:
    _buf = io.StringIO()
    with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
        session = Session.from_files(rom, sym)
        session.load_state(source.read_bytes())
        session.step(10)  # let overworld settle

        # UP x4 — walk to counter row (caps at y=3 in front of nurse at (4,2))
        for _ in range(4):
            session.press("up", duration=8)
            session.step(20)
        # LEFT x2 — route around the nurse block at (4,3)
        for _ in range(2):
            session.press("left", duration=8)
            session.step(20)
        # DOWN — step to y=4 (open row), sidestepping nurse
        session.press("down", duration=8)
        session.step(20)
        # RIGHT until x=11 — the receptionist's adjacent tile
        while session.read_game_state().overworld.x < 11:
            session.press("right", duration=8)
            session.step(20)
        # UP — step onto (11, 3) facing the receptionist
        session.press("up", duration=8)
        session.step(30)

        gs = session.read_game_state()
        assert gs.overworld.map_id == 0x40, (
            f"expected map 0x40 (Cerulean Pokecenter), got 0x{gs.overworld.map_id:02x}"
        )
        assert (gs.overworld.x, gs.overworld.y) == (11, 3), (
            f"expected final position (11, 3), got ({gs.overworld.x}, {gs.overworld.y})"
        )

        payload = session.save_state()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(payload)
        session.close()
    print(
        f"wrote {out} ({len(payload)} bytes); "
        f"player at map=0x{gs.overworld.map_id:02x} ({gs.overworld.x}, {gs.overworld.y})"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", type=Path, default=_DEFAULT_SOURCE)
    ap.add_argument("--rom", type=Path, default=_DEFAULT_ROM)
    ap.add_argument("--sym", type=Path, default=_DEFAULT_SYM)
    ap.add_argument("--out", type=Path, default=_DEFAULT_OUT)
    args = ap.parse_args()
    if not args.source.exists():
        raise SystemExit(f"source state not found: {args.source}")
    if not args.rom.exists():
        raise SystemExit(f"ROM not found: {args.rom}")
    produce(args.source, args.rom, args.sym, args.out)


if __name__ == "__main__":
    main()
