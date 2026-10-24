"""Regenerate ``tests/fixtures/link/<version>/cable_club.state``.

Walks a Red / Blue / Yellow ROM from a Cerulean-Pokecenter source
state to the Cable Club link receptionist at tile (11, 3) and writes
the result as the link-cable fixture used by
:mod:`tests.test_link_integration_remote`.

The source state is expected under a sibling worktree's
``walkthrough_to_cerulean*/milestones/cerulean_pc.state`` (produced by
the per-version walkthrough harness — captures the player inside
Cerulean Pokecenter with the correct CGB palette via the nurse-heal
trigger).

Map geometry — identical across R/B/Y because all three share the
pokered Cerulean Pokecenter interior (map 0x40):

    x=0 1 2 3 4 5 6 7 8 9 10 11 12 13
y=2   #       N L   G          C  R  #   <- counter; N=nurse, R=link receptionist
y=3   #     .   . . . . . . . .  .  #
y=4   . . . . . . . . . . . . .  .
y=7   . . . D . . . . . . . . .  .     <- start (door to Cerulean City)

The only tile where pressing A fires ``CableClubNPC`` is (11, 3),
discovered by hooking ``CableClubNPC`` across x=5..12 on the loaded
state.

Usage::

    python scripts/produce_cable_club_fixture.py --version yellow
    python scripts/produce_cable_club_fixture.py --version blue  --source path/to/blue/cerulean_pc.state
    python scripts/produce_cable_club_fixture.py --version red   --source path/to/red/cerulean_pc.state

Each ``--version`` resolves ``--rom``, ``--sym``, and ``--out`` to
defaults rooted at the repo's ``rom/`` and ``tests/fixtures/link/``
directories; pass any of them explicitly to override.
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


# Per-version defaults. ROM + sym paths resolve against the repo root's
# ``rom/<version>/`` directory; the source-state default points at the
# sibling walkthrough_to_cerulean worktree(s). Override any of these
# with the matching CLI flag.
_VERSIONS = {
    "red": {
        "rom": "red/pokemon-red.gb",
        "sym": "red/pokemon-red.sym",
        "source_candidates": (
            "walkthrough_to_cerulean_red",
            "walkthrough_red_to_cerulean",
        ),
    },
    "blue": {
        "rom": "blue/pokemon-blue-color.gb",
        "sym": "blue/pokemon-blue.sym",
        "source_candidates": (
            "walkthrough_to_cerulean_blue",
            "walkthrough_blue_to_cerulean",
        ),
    },
    "yellow": {
        "rom": "yellow/pokemon-yellow.gbc",
        "sym": "yellow/pokemon-yellow.sym",
        # Yellow is the first version where this worktree landed —
        # the un-suffixed path is the real one.
        "source_candidates": (
            "walkthrough_to_cerulean",
            "walkthrough_to_cerulean_yellow",
        ),
    },
}


def _repo_rom_root() -> Path:
    for parent in [_REPO, *_REPO.parents]:
        if (parent / "rom").is_dir():
            return parent / "rom"
    return _REPO / "rom"


def _default_source(version: str) -> Path | None:
    """Find ``cerulean_pc.state`` for this version under any sibling
    worktree. Walks two levels down from .claude/worktrees/ so that
    both ``<name>/<candidate>/milestones/cerulean_pc.state`` and a
    same-worktree ``<candidate>/milestones/cerulean_pc.state`` layout
    match. Version-agnostic — caller is expected to point the right
    ROM at a matching source state."""
    worktrees_root = _REPO.parent  # .claude/worktrees/
    if not worktrees_root.is_dir():
        return None
    candidate_names = _VERSIONS[version]["source_candidates"]
    for worktree in sorted(worktrees_root.iterdir()):
        if not worktree.is_dir() or worktree == _REPO:
            continue
        for name in candidate_names:
            p = worktree / name / "milestones" / "cerulean_pc.state"
            if p.exists():
                return p
    return None


def _default_rom(version: str) -> Path:
    return _repo_rom_root() / _VERSIONS[version]["rom"]


def _default_sym(version: str) -> Path:
    return _repo_rom_root() / _VERSIONS[version]["sym"]


def _default_out(version: str) -> Path:
    return _REPO / "tests" / "fixtures" / "link" / version / "cable_club.state"


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
            f"expected final position (11, 3), got "
            f"({gs.overworld.x}, {gs.overworld.y})"
        )

        payload = session.save_state()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(payload)
        session.close()
    print(
        f"wrote {out} ({len(payload)} bytes); "
        f"player at map=0x{gs.overworld.map_id:02x} "
        f"({gs.overworld.x}, {gs.overworld.y})"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--version", choices=sorted(_VERSIONS), required=True,
        help="ROM version (red / blue / yellow)",
    )
    ap.add_argument("--source", type=Path, default=None)
    ap.add_argument("--rom", type=Path, default=None)
    ap.add_argument("--sym", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    v = args.version
    source = args.source or _default_source(v)
    rom = args.rom or _default_rom(v)
    sym = args.sym or _default_sym(v)
    out = args.out or _default_out(v)
    if source is None or not source.exists():
        candidates = _VERSIONS[v]["source_candidates"]
        raise SystemExit(
            f"source cerulean_pc.state not found for version {v!r}. "
            f"Looked for: " + ", ".join(candidates) + ". "
            f"Produce it via one of those sibling worktrees, or pass "
            f"--source explicitly."
        )
    if not rom.exists():
        raise SystemExit(f"ROM not found: {rom}")
    if not sym.exists():
        raise SystemExit(f"sym file not found: {sym}")
    produce(source, rom, sym, out)


if __name__ == "__main__":
    main()
