"""Repository asset discovery, VERSIONS.md parsing and asset inspection.

Split from ``scripts/production_gate.py`` for issue #124 with no behavior
change: the code below is copied verbatim except that calls to facade-owned,
monkeypatch-patched entry points resolve through ``_entry`` so attribute patches
on the loaded gate module stay visible.
"""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Direct names this module calls; cyclic back-edges go through _entry.
from scripts.production_gate_model import (
    _PATH_RE,
    _PYBOY_RE,
    _SHA1_RE,
    _SYMBOL_PATH_RE,
    _SYMBOL_SHA1_RE,
    KNOWN_ROM_FILES,
    KNOWN_SYMBOL_FILES,
    REQUIRED_FIXTURES,
    RUNTIME_MODE_ALIASES,
    RUNTIME_MODES,
    AssetRecord,
    _asset_key,
)


def normalize_runtime_mode(value: str) -> str:
    """Normalize the dual-runtime spelling while preserving old modes."""

    return RUNTIME_MODE_ALIASES.get(value, value)


def runtime_modes_for_gate(runtime_mode: str) -> tuple[str, ...]:
    """Expand a CLI runtime selection into explicit runtime executions."""

    normalized = normalize_runtime_mode(runtime_mode)
    if normalized in RUNTIME_MODES:
        return (normalized,)
    if normalized == "both":
        return RUNTIME_MODES
    raise ValueError(f"unsupported runtime mode: {runtime_mode!r}")


def _python_path_from_argument(value: Path, cwd: Path) -> Path:
    """Make a CLI Python path absolute without dereferencing its symlinks."""
    path = value.expanduser()
    if path.is_absolute():
        return path
    return cwd / path


def _path_from_project_root(project_root: Path, value: str | Path) -> Path:
    """Anchor a relative CLI/environment path to the inspected checkout."""

    path = Path(value).expanduser()
    return path if path.is_absolute() else project_root / path


def _path_from_env(name: str, project_root: Path) -> Path | None:
    value = os.environ.get(name)
    return _path_from_project_root(project_root, value) if value else None


def find_rom_root(project_root: Path, explicit: Path | None = None) -> Path:
    """Resolve ROM storage without assuming the worktree owns ``rom/``."""

    if explicit is not None:
        return _path_from_project_root(project_root, explicit)
    configured = _path_from_env("POKERED_ROM_ROOT", project_root)
    if configured is not None:
        return configured

    # If only a primary ROM path is configured, use its nearest ``rom``
    # ancestor when possible.  This keeps the report useful for a worktree
    # whose ROMs live in a sibling checkout.
    configured_rom = _path_from_env("POKERED_ROM_PATH", project_root)
    if configured_rom is not None:
        for parent in (configured_rom.parent, *configured_rom.parents):
            if parent.name == "rom":
                return parent

    for parent in (project_root, *project_root.parents):
        candidate = parent / "rom"
        if candidate.is_dir():
            return candidate
    return project_root / "rom"


def find_fixture_root(project_root: Path, explicit: Path | None = None) -> Path:
    if explicit is not None:
        return _path_from_project_root(project_root, explicit)
    configured = _path_from_env("POKERED_FIXTURE_ROOT", project_root)
    if configured is not None:
        return configured
    for parent in (project_root, *project_root.parents):
        candidate = parent / "tests" / "fixtures" / "link"
        if candidate.is_dir():
            return candidate
    return project_root / "tests" / "fixtures" / "link"


def parse_expected_sha1(versions_file: Path) -> dict[Path, str]:
    """Extract ROM and symbol SHA-1 pins from ``VERSIONS.md``.

    The gate must fail closed when a required ROM or symbol is present but is
    not pinned.  The existing version document stores ROM pins as
    ``SHA-1``/``Path`` rows and symbol pins as ``Symbol SHA-1``/``Symbols``
    rows, so preserve both in one path-keyed map.
    """

    expected: dict[Path, str] = {}
    pending_sha: str | None = None
    current_symbol_path: Path | None = None
    if not versions_file.is_file():
        return expected

    for line in versions_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("##"):
            pending_sha = None
            current_symbol_path = None
            continue
        sha_match = _SHA1_RE.match(line)
        if sha_match:
            pending_sha = sha_match.group(1).lower()
            continue
        path_match = _PATH_RE.match(line)
        if path_match and pending_sha:
            relative = _asset_key(path_match.group(1))
            previous = expected.get(relative)
            if previous is not None and previous != pending_sha:
                raise ValueError(f"conflicting SHA-1 pins for {relative}")
            expected[relative] = pending_sha
            pending_sha = None
            continue

        symbol_path_match = _SYMBOL_PATH_RE.match(line)
        if symbol_path_match:
            current_symbol_path = _asset_key(symbol_path_match.group(1))
            continue

        symbol_sha_match = _SYMBOL_SHA1_RE.match(line)
        if symbol_sha_match and current_symbol_path is not None:
            symbol_sha = symbol_sha_match.group(1).lower()
            previous = expected.get(current_symbol_path)
            if previous is not None and previous != symbol_sha:
                raise ValueError(f"conflicting SHA-1 pins for {current_symbol_path}")
            expected[current_symbol_path] = symbol_sha
            continue
    return expected


def parse_expected_pyboy_version(versions_file: Path) -> str | None:
    """Return the pinned PyBoy version, if the release manifest has one."""

    if not versions_file.is_file():
        return None
    for line in versions_file.read_text(encoding="utf-8").splitlines():
        match = _PYBOY_RE.match(line)
        if match:
            return match.group(1).strip()
    return None


def parse_expected_pyboy_revision(versions_file: Path) -> str | None:
    """Return the exact pinned PyBoy fork revision, if one is documented."""

    if not versions_file.is_file():
        return None
    for line in versions_file.read_text(encoding="utf-8").splitlines():
        match = _PYBOY_RE.match(line)
        if match and match.group(2):
            return match.group(2).lower()
    return None


def sha1_of_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_assets(
    rom_root: Path,
    fixture_root: Path,
    expected_sha1: dict[Path, str],
) -> list[AssetRecord]:
    records: list[AssetRecord] = []

    def inspect_file(
        label: str,
        kind: str,
        path: Path,
        relative: Path,
        *,
        require_pin: bool,
    ) -> AssetRecord:
        expected = expected_sha1.get(_asset_key(relative))
        if not path.is_file():
            return AssetRecord(
                label,
                kind,
                str(path),
                "missing",
                expected_sha1=expected,
            )
        try:
            size = path.stat().st_size
            actual = sha1_of_file(path)
        except (OSError, ValueError):
            return AssetRecord(
                label,
                kind,
                str(path),
                "unreadable",
                expected_sha1=expected,
            )
        if size == 0:
            status = "empty"
        elif require_pin and expected is None:
            status = "sha1-unpinned"
        elif expected is not None and actual != expected:
            status = "sha1-mismatch"
        else:
            status = "ok"
        return AssetRecord(
            label,
            kind,
            str(path),
            status,
            size=size,
            expected_sha1=expected,
            actual_sha1=actual,
        )

    for label, relative in KNOWN_ROM_FILES:
        path = rom_root / relative
        records.append(inspect_file(label, "rom", path, relative, require_pin=True))

    for label, relative in KNOWN_SYMBOL_FILES:
        path = rom_root / relative
        records.append(inspect_file(label, "symbol", path, relative, require_pin=True))

    for label, relative in REQUIRED_FIXTURES:
        path = fixture_root / relative
        # Fixture hashes are reported for evidence, but the current manifest
        # deliberately does not pin generated save-state bytes.  Presence and
        # readability remain required; a future fixture pin in VERSIONS.md is
        # automatically enforced by ``inspect_file``.
        records.append(inspect_file(label, "fixture", path, relative, require_pin=False))
    return records


def required_asset_problems(records: Iterable[AssetRecord]) -> list[str]:
    """Return the fail-closed asset problems for required real-ROM tiers."""

    return [
        f"{record.kind} {record.label}: {record.status} ({record.path})"
        for record in records
        if record.status != "ok"
    ]
