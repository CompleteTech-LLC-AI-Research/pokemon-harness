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


# Per-version defaults keyed by (version, variant). The ``color`` and
# ``cgb`` variants use the color-patched (R/B) or CGB (Yellow) ROM;
# the ``vanilla`` variants use the un-patched DMG R/B ROMs. PyBoy save
# states are bit-tied to the exact ROM bytes they were captured
# against, so each variant needs its own fixture on disk even when
# the player destination (Cable Club receptionist) is identical.
# ROM + sym paths resolve against the repo root's ``rom/<version>/``
# directory; the source-state default points at the sibling
# walkthrough_to_cerulean worktree(s). Override any of these with the
# matching CLI flag.
_VERSIONS = {
    ("red", "color"): {
        "rom": "red/pokemon-red-color.gb",
        "sym": "red/pokemon-red.sym",
        "out_name": "cable_club.state",
        "source_candidates": (
            "walkthrough_to_cerulean_red",
            "walkthrough_red_to_cerulean",
        ),
    },
    ("red", "vanilla"): {
        "rom": "red/pokemon-red.gb",
        "sym": "red/pokemon-red.sym",
        "out_name": "cable_club-vanilla.state",
        # Vanilla needs a vanilla-ROM-captured cerulean_pc source;
        # point --source at one produced by running the walkthrough
        # scripts (run_to_brock -> to_cerulean) with
        # POKERED_ROM_PATH=rom/red/pokemon-red.gb.
        "source_candidates": (),
    },
    ("blue", "color"): {
        "rom": "blue/pokemon-blue-color.gb",
        "sym": "blue/pokemon-blue.sym",
        "out_name": "cable_club.state",
        "source_candidates": (
            "walkthrough_to_cerulean_blue",
            "walkthrough_blue_to_cerulean",
        ),
    },
    ("blue", "vanilla"): {
        "rom": "blue/pokemon-blue.gb",
        "sym": "blue/pokemon-blue.sym",
        "out_name": "cable_club-vanilla.state",
        "source_candidates": (),
    },
    ("yellow", "cgb"): {
        "rom": "yellow/pokemon-yellow.gbc",
        "sym": "yellow/pokemon-yellow.sym",
        "out_name": "cable_club.state",
        "source_candidates": (
            "walkthrough_to_cerulean",
            "walkthrough_to_cerulean_yellow",
        ),
    },
}
_DEFAULT_VARIANTS = {"red": "color", "blue": "color", "yellow": "cgb"}
_KNOWN_VERSIONS = sorted({v for v, _ in _VERSIONS})


def _repo_rom_root() -> Path:
    for parent in [_REPO, *_REPO.parents]:
        if (parent / "rom").is_dir():
            return parent / "rom"
    return _REPO / "rom"


def _default_source(version: str, variant: str) -> Path | None:
    """Find ``cerulean_pc.state`` for this (version, variant) under any
    sibling worktree. Walks two levels down from .claude/worktrees/ so
    both ``<name>/<candidate>/milestones/cerulean_pc.state`` and a
    same-worktree ``<candidate>/milestones/cerulean_pc.state`` layout
    match. Caller is expected to point a matching-variant ROM at the
    source state (the two must share ROM bytes)."""
    worktrees_root = _REPO.parent  # .claude/worktrees/
    if not worktrees_root.is_dir():
        return None
    candidate_names = _VERSIONS[(version, variant)]["source_candidates"]
    for worktree in sorted(worktrees_root.iterdir()):
        if not worktree.is_dir() or worktree == _REPO:
            continue
        for name in candidate_names:
            p = worktree / name / "milestones" / "cerulean_pc.state"
            if p.exists():
                return p
    return None


def _default_rom(version: str, variant: str) -> Path:
    return _repo_rom_root() / _VERSIONS[(version, variant)]["rom"]


def _default_sym(version: str, variant: str) -> Path:
    return _repo_rom_root() / _VERSIONS[(version, variant)]["sym"]


def _default_out(version: str, variant: str) -> Path:
    return (
        _REPO / "tests" / "fixtures" / "link" / version
        / _VERSIONS[(version, variant)]["out_name"]
    )


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
        "--version", choices=_KNOWN_VERSIONS, required=True,
        help="ROM version (red / blue / yellow)",
    )
    ap.add_argument(
        "--variant", choices=("color", "vanilla", "cgb"), default=None,
        help=(
            "ROM variant. Defaults to the per-version canonical "
            "variant (color for R/B, cgb for Y). Pass ``vanilla`` on "
            "Red or Blue to produce cable_club-vanilla.state against "
            "the un-patched DMG ROM — requires a matching "
            "vanilla-captured --source cerulean_pc.state."
        ),
    )
    ap.add_argument("--source", type=Path, default=None)
    ap.add_argument("--rom", type=Path, default=None)
    ap.add_argument("--sym", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    v = args.version
    variant = args.variant or _DEFAULT_VARIANTS[v]
    if (v, variant) not in _VERSIONS:
        raise SystemExit(
            f"no fixture recipe for {v}/{variant}; known: "
            + ", ".join(f"{a}/{b}" for a, b in _VERSIONS)
        )
    source = args.source or _default_source(v, variant)
    rom = args.rom or _default_rom(v, variant)
    sym = args.sym or _default_sym(v, variant)
    out = args.out or _default_out(v, variant)
    if source is None or not source.exists():
        candidates = _VERSIONS[(v, variant)]["source_candidates"]
        raise SystemExit(
            f"source cerulean_pc.state not found for {v}/{variant}. "
            f"Looked for: {', '.join(candidates) or '<none>'}. "
            f"Produce a {variant}-ROM-captured cerulean_pc.state via "
            f"the walkthrough scripts (run_to_brock -> to_cerulean) "
            f"with POKERED_ROM_PATH pointing at the {variant} ROM, "
            f"then pass --source explicitly."
        )
    if not rom.exists():
        raise SystemExit(f"ROM not found: {rom}")
    if not sym.exists():
        raise SystemExit(f"sym file not found: {sym}")
    produce(source, rom, sym, out)


if __name__ == "__main__":
    main()
