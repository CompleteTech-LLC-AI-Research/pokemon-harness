"""Regenerate ``tests/fixtures/link/<version>/cable_club.state``.

Walks a Red / Blue / Yellow ROM from a Cerulean-Pokecenter source
state to the Cable Club link receptionist at tile (11, 3) and writes
the result as the link-cable fixture used by
:mod:`tests.test_link_integration_remote` and its split
:mod:`tests.test_link_integration_remote_rpc` /
:mod:`tests.test_link_integration_remote_trade` modules.

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
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

from pokered_harness.session import Session

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
_DEFAULT_TIMEOUT_SECONDS = 180.0
_DEFAULT_MAX_MOVEMENT_STEPS = 64


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


def _check_budget(deadline: float, phase: str) -> None:
    if time.monotonic() >= deadline:
        raise TimeoutError(
            f"fixture production exceeded the {phase} deadline; "
            "check that the source state was captured from the selected ROM"
        )


def _press_and_step(
    session: Session,
    button: str,
    *,
    deadline: float,
    movement_steps: int,
    max_movement_steps: int,
    step_frames: int = 20,
) -> int:
    if movement_steps >= max_movement_steps:
        raise TimeoutError(
            "fixture production exceeded the movement-step budget; "
            "check that the source state was captured from the selected ROM"
        )
    _check_budget(deadline, "movement")
    session.press(button, duration=8)
    session.step(step_frames)
    _check_budget(deadline, "movement")
    return movement_steps + 1


def produce(
    source: Path,
    rom: Path,
    sym: Path,
    out: Path,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    max_movement_steps: int = _DEFAULT_MAX_MOVEMENT_STEPS,
) -> None:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than zero")
    if max_movement_steps <= 0:
        raise ValueError("max_movement_steps must be greater than zero")

    from pokered_harness.config import load_versions

    pins = load_versions(_REPO / "VERSIONS.md")
    expected_rom_sha1 = pins.sha1_for_path(rom)
    expected_symbol_sha1 = pins.symbol_sha1_for_path(sym)
    if expected_rom_sha1 is None:
        raise ValueError(f"ROM is not pinned in VERSIONS.md: {rom}")
    if expected_symbol_sha1 is None:
        raise ValueError(f"symbol file is not pinned in VERSIONS.md: {sym}")

    deadline = time.monotonic() + timeout_seconds
    _buf = io.StringIO()
    session: Session | None = None
    with contextlib.redirect_stdout(_buf), contextlib.redirect_stderr(_buf):
        try:
            session = Session.from_files(
                rom,
                sym,
                expected_rom_sha1=expected_rom_sha1,
                expected_symbol_sha1=expected_symbol_sha1,
                expected_pyboy_version=pins.pyboy_version,
                expected_pyboy_revision=pins.pyboy_revision,
            )
            session.load_state(source.read_bytes())
            _check_budget(deadline, "initialization")
            session.step(10)  # let overworld settle
            _check_budget(deadline, "initialization")

            movement_steps = 0
            # UP x4 — walk to counter row (caps at y=3 in front of nurse at (4,2))
            for _ in range(4):
                movement_steps = _press_and_step(
                    session,
                    "up",
                    deadline=deadline,
                    movement_steps=movement_steps,
                    max_movement_steps=max_movement_steps,
                )
            # LEFT x2 — route around the nurse block at (4,3)
            for _ in range(2):
                movement_steps = _press_and_step(
                    session,
                    "left",
                    deadline=deadline,
                    movement_steps=movement_steps,
                    max_movement_steps=max_movement_steps,
                )
            # DOWN — step to y=4 (open row), sidestepping nurse
            movement_steps = _press_and_step(
                session,
                "down",
                deadline=deadline,
                movement_steps=movement_steps,
                max_movement_steps=max_movement_steps,
            )
            # RIGHT until x=11 — the receptionist's adjacent tile
            while session.read_game_state().overworld.x < 11:
                movement_steps = _press_and_step(
                    session,
                    "right",
                    deadline=deadline,
                    movement_steps=movement_steps,
                    max_movement_steps=max_movement_steps,
                )
            # UP — step onto (11, 3) facing the receptionist
            movement_steps = _press_and_step(
                session,
                "up",
                deadline=deadline,
                movement_steps=movement_steps,
                max_movement_steps=max_movement_steps,
                step_frames=30,
            )

            gs = session.read_game_state()
            if gs.overworld.map_id != 0x40:
                raise RuntimeError(
                    "expected map 0x40 (Cerulean Pokecenter), got "
                    f"0x{gs.overworld.map_id:02x}"
                )
            if (gs.overworld.x, gs.overworld.y) != (11, 3):
                raise RuntimeError(
                    "expected final position (11, 3), got "
                    f"({gs.overworld.x}, {gs.overworld.y})"
                )

            payload = session.save_state()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(payload)
        finally:
            if session is not None:
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
    ap.add_argument(
        "--timeout-seconds",
        type=float,
        default=_DEFAULT_TIMEOUT_SECONDS,
        help=f"wall-clock production budget (default: {_DEFAULT_TIMEOUT_SECONDS:g}s)",
    )
    ap.add_argument(
        "--max-movement-steps",
        type=int,
        default=_DEFAULT_MAX_MOVEMENT_STEPS,
        help=(
            "maximum directional inputs before failing (default: "
            f"{_DEFAULT_MAX_MOVEMENT_STEPS})"
        ),
    )
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
    try:
        produce(
            source,
            rom,
            sym,
            out,
            timeout_seconds=args.timeout_seconds,
            max_movement_steps=args.max_movement_steps,
        )
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        raise SystemExit(f"fixture production failed: {exc}") from exc


if __name__ == "__main__":
    main()
