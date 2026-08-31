"""Portable discovery of BYO-ROM and derived link fixtures for tests.

The default checkout keeps ROMs outside the Git tree, while CI and isolated
worktrees may provide them through an explicit environment variable.  Tests
should use these helpers rather than assuming that ``rom/`` is an ancestor of
the current worktree.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _configured_path(name: str) -> Path | None:
    value = os.environ.get(name)
    if not value:
        return None
    return Path(value).expanduser()


def find_rom_root(project_root: Path | None = None) -> Path:
    """Return the configured or nearest existing ``rom`` directory.

    ``POKERED_ROM_ROOT`` takes precedence.  The ancestor fallback preserves
    the existing shared-ROM worktree workflow.
    """

    root = project_root or PROJECT_ROOT
    configured = _configured_path("POKERED_ROM_ROOT")
    if configured is not None:
        return configured

    for parent in (root, *root.parents):
        candidate = parent / "rom"
        if candidate.is_dir():
            return candidate
    return root / "rom"


def find_fixture_root(project_root: Path | None = None) -> Path:
    """Return the configured or nearest link-fixture directory."""

    root = project_root or PROJECT_ROOT
    configured = _configured_path("POKERED_FIXTURE_ROOT")
    if configured is not None:
        return configured

    for parent in (root, *root.parents):
        candidate = parent / "tests" / "fixtures" / "link"
        if candidate.is_dir():
            return candidate
    return root / "tests" / "fixtures" / "link"


def rom_path(version: str, *, color: bool = False, project_root: Path | None = None) -> Path:
    """Resolve the ROM path used by the real-ROM tests."""

    root = find_rom_root(project_root)
    if version == "red":
        name = "pokemon-red-color.gb" if color else "pokemon-red.gb"
    elif version == "blue":
        name = "pokemon-blue-color.gb" if color else "pokemon-blue.gb"
    elif version == "yellow":
        name = "pokemon-yellow.gbc"
    else:
        raise ValueError(f"unsupported ROM version: {version}")
    return root / version / name


def sym_path(version: str, project_root: Path | None = None) -> Path:
    """Resolve the symbol file used by the real-ROM tests."""

    root = find_rom_root(project_root)
    name = {
        "red": "pokemon-red.sym",
        "blue": "pokemon-blue.sym",
        "yellow": "pokemon-yellow.sym",
    }.get(version)
    if name is None:
        raise ValueError(f"unsupported ROM version: {version}")
    return root / version / name


def fixture_path(
    version: str,
    name: str = "cable_club.state",
    project_root: Path | None = None,
) -> Path:
    """Resolve a derived link fixture without making it part of the repo."""

    return find_fixture_root(project_root) / version / name


__all__ = [
    "PROJECT_ROOT",
    "find_fixture_root",
    "find_rom_root",
    "fixture_path",
    "rom_path",
    "sym_path",
]
