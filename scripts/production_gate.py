#!/usr/bin/env python3
"""Run the Pokémon harness production acceptance gate.

The gate deliberately runs pytest in separate subprocesses.  This keeps ROM
state, emulator globals, and network listeners isolated between tiers and
makes the reported counts independent of pytest's in-process plugin state.

Examples::

    python scripts/production_gate.py
    python scripts/production_gate.py --unit-only
    python scripts/production_gate.py --tier remote --repeat-timing 5
    python scripts/production_gate.py --runtime-mode both --unit-only
    python scripts/production_gate.py --runtime-mode both --python .venv/bin/python \
        --cython-python .venv-cython/bin/python --unit-only
    python scripts/production_gate.py --unit-only --evidence-dir /tmp/pokered-evidence

The default command is strict for every selected tier, including the
stateful trade and battle acceptance cases. Missing ROMs or derived fixtures
are reported as blocked rather than converted into a green skip.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import runpy
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter, deque
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any

KNOWN_ROM_FILES: tuple[tuple[str, Path], ...] = (
    ("red-stock", Path("red/pokemon-red.gb")),
    ("red-color", Path("red/pokemon-red-color.gb")),
    ("blue-stock", Path("blue/pokemon-blue.gb")),
    ("blue-color", Path("blue/pokemon-blue-color.gb")),
    ("yellow", Path("yellow/pokemon-yellow.gbc")),
)
KNOWN_SYMBOL_FILES: tuple[tuple[str, Path], ...] = (
    ("red", Path("red/pokemon-red.sym")),
    ("blue", Path("blue/pokemon-blue.sym")),
    ("yellow", Path("yellow/pokemon-yellow.sym")),
)
REQUIRED_FIXTURES: tuple[tuple[str, Path], ...] = (
    ("red cable-club", Path("red/cable_club.state")),
    ("blue cable-club", Path("blue/cable_club.state")),
    ("yellow cable-club", Path("yellow/cable_club.state")),
)

REQUIRED_TIER_ASSETS = frozenset({"local", "remote", "trade", "battle"})
OPTIONAL_TIERS = frozenset()

TIER_EXPRESSIONS: dict[str, str] = {
    "unit": "unit",
    "local": "real_rom and not remote_link and not acceptance",
    "remote": "real_rom and remote_link and not acceptance",
    "trade": "real_rom and trade_acceptance",
    "battle": "real_rom and battle_acceptance",
    "timing": "timing_sensitive",
}
TIER_DESCRIPTIONS: dict[str, str] = {
    "unit": "ROM-free unit tests",
    "local": "real-ROM local/session/link tests",
    "remote": "real-ROM remote TCP/subprocess tests",
    "trade": "strict real-ROM party-swap acceptance",
    "battle": "strict real-ROM battle-turn acceptance",
    "timing": "fivefold scheduling-sensitive regression",
}
DEFAULT_TIERS = ("unit", "local", "remote", "trade", "battle", "timing")

DEFAULT_TIMEOUT_SECONDS: dict[str, float] = {
    "unit": 900.0,
    "local": 3600.0,
    "remote": 3600.0,
    "trade": 3600.0,
    "battle": 3600.0,
    "timing": 900.0,
}
# Strict acceptance rows each run an emulator pair (two PyBoy instances for a
# local row or two child processes for a TCP row).  A conservative default is
# important because oversubscribing the host changes emulator scheduling and
# can turn a valid cross-version run into a timing failure.  Dedicated CI can
# opt into more parallelism with ``--matrix-workers`` after measuring its
# available CPU budget.
DEFAULT_MATRIX_WORKERS = 1
MATRIX_CASE_TIMEOUT_SECONDS: dict[str, float] = {
    "trade": 900.0,
    "battle": 1200.0,
}
MATRIX_AGGREGATE_GRACE_SECONDS = 10.0
COLLECTION_TIMEOUT_SECONDS = 300.0
RUNTIME_MODES = ("source", "cython")
RUNTIME_MODE_ALIASES = {"dual": "both"}
RUNTIME_MODE_CHOICES = (*RUNTIME_MODES, "both")
PYBOY_RUNTIME_MODULES = (
    "pyboy",
    "pyboy.pyboy",
    "pyboy.utils",
    "pyboy.core.mb",
    "pyboy.core.serial",
)
CYTHON_RUNTIME_MODULES = tuple(name for name in PYBOY_RUNTIME_MODULES if name != "pyboy")
EVIDENCE_SCHEMA_VERSION = 1
EVIDENCE_REPORT_FILENAME = "gate-report.json"
EVIDENCE_TEXT_FILENAME = "gate-report.txt"
EVIDENCE_MANIFEST_FILENAME = "evidence-manifest.json"
FIXTURE_MANIFEST_RELATIVE_PATH = Path("release-evidence/fixture-manifest.json")
CERTIFIED_FIXTURE_IDS = frozenset(
    {
        "red-color-ordinary",
        "red-color-battle",
        "blue-color-ordinary",
        "blue-color-battle",
        "yellow-cgb-ordinary",
        "yellow-cgb-battle",
    }
)
GATE_CONTROLLED_ENVIRONMENT = (
    "PYTEST_ADDOPTS",
    "PYTEST_PLUGINS",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
    "POKERED_GATE_REPORT",
    "POKERED_GATE_PROGRESS_REPORT",
)
# The gate must not inherit arbitrary pytest plugins or configuration.  The
# async integration tests still need their pinned plugin, so load that plugin
# explicitly alongside the gate report plugin in every child process.
PYTEST_GATE_ARGUMENTS = (
    "--strict-config",
    "--strict-markers",
    "-p",
    "pytest_asyncio.plugin",
    "-p",
    "tests._gate_report",
)

_SHA1_RE = re.compile(r"^\|\s*SHA-?1\s*\|\s*`([0-9A-Fa-f]{40})`\s*\|")
_PATH_RE = re.compile(r"^\|\s*Path\s*\|\s*`([^`]+)`\s*\|")
_SYMBOL_SHA1_RE = re.compile(r"^\|\s*Symbol\s+SHA-?1\s*\|\s*`([0-9A-Fa-f]{40})`\s*\|")
_SYMBOL_PATH_RE = re.compile(r"^\|\s*Symbols?\s*\|\s*`([^`]+)`\s*\|")
_PYBOY_RE = re.compile(
    r"^\|\s*PyBoy\s*\|\s*`([^`]+)`"
    r"(?:\s*\+\s*fork\s*`([0-9A-Fa-f]{40})`)?"
)
_CREDENTIAL_TEXT_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|auth(?:orization)?|bearer|"
    r"credential|password|passwd|private[_-]?key|secret|token)\b"
    r"\s*(?:[:=]\s*|\s+)(?:bearer\s+)?[^\s,;]+"
)
_URI_CREDENTIAL_RE = re.compile(r"(?i)(https?://[^/\s:@]+):[^@\s]+@")
_BYTE_LITERAL_RE = re.compile(r"(?is)\bb(?:'(?:\\.|[^'])*'|\"(?:\\.|[^\"])*\")")
_LONG_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9+/=_-]{128,}(?![A-Za-z0-9])")
_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9>])(?:[A-Za-z]:[\\/]|/)[^\s,;()]+")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9>])[A-Za-z]:[\\/]"
    r"(?:[^\\/\r\n,;()]+[\\/])*"
    r"[^\s\\/\r\n,;()]+"
)
_SPACED_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9>])/(?:[^/\r\n,;()]+/)*"
    r"[^\s/\r\n,;()]+"
)
_QUOTED_ABSOLUTE_PATH_RE = re.compile(
    r"(?P<quote>[\"'])"
    r"(?P<path>(?:[A-Za-z]:[\\/]|/)[^\"'\r\n]+)"
    r"(?P=quote)"
)


def _asset_key(value: str | Path) -> Path:
    """Normalize a documented repository-relative asset path."""

    parts = [part for part in str(value).replace("\\", "/").split("/") if part]
    if parts and parts[0] == ".":
        parts = parts[1:]
    if parts and parts[0].lower() == "rom":
        parts = parts[1:]
    return Path(*parts)


@dataclass(frozen=True)
class AssetRecord:
    """Observed state for one ROM, symbol, or derived fixture."""

    label: str
    kind: str
    path: str
    status: str
    size: int | None = None
    expected_sha1: str | None = None
    actual_sha1: str | None = None


@dataclass
class Counts:
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    xfailed: int = 0
    xpassed: int = 0
    errors: int = 0

    @classmethod
    def from_report(cls, payload: dict[str, Any]) -> Counts:
        raw = payload.get("counts")
        if not isinstance(raw, dict):
            raise TypeError("pytest report has no counts object")

        values: dict[str, int] = {}
        for field_name in cls.__dataclass_fields__:
            value = raw.get(field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(
                    f"pytest report count {field_name!r} must be a non-negative integer"
                )
            values[field_name] = value

        return cls(**values)


@dataclass(frozen=True)
class GateReport:
    """Validated outcome data emitted by the pytest gate plugin."""

    counts: Counts
    skip_reasons: dict[str, int] = field(default_factory=dict)
    nodeids: tuple[str, ...] = ()
    collection_errors: tuple[dict[str, str], ...] = ()
    collection_skips: tuple[dict[str, str], ...] = ()
    collection_only: bool = False
    error: str = ""


@dataclass
class MatrixCaseResult:
    """Retained result for one independently scheduled acceptance row."""

    nodeid: str
    status: str
    returncode: int | None
    duration_seconds: float
    counts: Counts = field(default_factory=Counts)
    reason: str = ""
    output_tail: str = ""
    deadline_seconds: float = 0.0
    report_kind: str = "final"
    partial: bool = False


@dataclass
class TierResult:
    name: str
    description: str
    expression: str
    required: bool
    status: str
    counts: Counts = field(default_factory=Counts)
    returncodes: list[int] = field(default_factory=list)
    duration_seconds: float = 0.0
    skip_reasons: dict[str, int] = field(default_factory=dict)
    command: list[str] | None = None
    output_tail: str = ""
    reason: str = ""
    iteration_failures: list[str] = field(default_factory=list)
    selected_nodeids: list[str] = field(default_factory=list)
    case_results: list[MatrixCaseResult] = field(default_factory=list)


@dataclass
class CollectionResult:
    """Result for one supported pytest collection entry point."""

    name: str
    command: list[str]
    status: str
    returncode: int | None
    nodeids: tuple[str, ...] = ()
    duration_seconds: float = 0.0
    output_tail: str = ""
    reason: str = ""


@dataclass
class RuntimeGateResult:
    """Complete gate result for one explicit PyBoy runtime mode."""

    mode: str
    runtime: dict[str, Any]
    collections: list[CollectionResult]
    fixture_manifest: dict[str, Any]
    matrix_audit: dict[str, Any]
    tiers: list[TierResult]
    gate_problems: list[str] = field(default_factory=list)


def project_root_from_script() -> Path:
    return Path(__file__).resolve().parents[1]


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


def build_test_environment(
    project_root: Path,
    rom_root: Path,
    fixture_root: Path,
    expected_sha1: dict[Path, str],
    *,
    runtime_mode: str = "source",
) -> dict[str, str]:
    """Return the exact environment inherited by every pytest subprocess."""

    if runtime_mode not in RUNTIME_MODES:
        raise ValueError(f"unsupported runtime mode: {runtime_mode!r}")

    environment = {key: value for key, value in os.environ.items()}
    environment["POKERED_ROM_ROOT"] = str(rom_root)
    environment["POKERED_FIXTURE_ROOT"] = str(fixture_root)
    environment["PYTHONUNBUFFERED"] = "1"

    # A caller's pytest selection/plugin environment is not part of the
    # release contract.  In particular, inherited ``PYTEST_ADDOPTS`` can
    # silently deselect required tests, and a third-party plugin can alter
    # collection or xfail behavior.  The gate owns these settings.
    for key in GATE_CONTROLLED_ENVIRONMENT:
        environment.pop(key, None)
    # Remove the rest of pytest's parent-process state as well (for example
    # PYTEST_CURRENT_TEST and xdist variables), then opt into the controlled
    # plugin set above.  Leaving autoload enabled would let an installed
    # third-party plugin change collection, skips, or xfail semantics.
    for key in tuple(environment):
        if key.startswith("PYTEST_"):
            environment.pop(key, None)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    for key in tuple(environment):
        if key.startswith("POKERED_PEER_") or key == "POKERED_ROM_VERSION":
            environment.pop(key, None)
    # Never pass the diagnostic hash bypass into a production-gate child.
    # ``main`` separately reports its presence as a policy failure so merely
    # stripping it cannot turn an unsafe invocation green.
    environment.pop("POKERED_SKIP_SHA1", None)

    versions_file = project_root / "VERSIONS.md"
    if versions_file.is_file():
        environment["POKERED_VERSIONS_PATH"] = str(versions_file)
    else:
        environment.pop("POKERED_VERSIONS_PATH", None)

    # Ensure the subprocess tests import this checkout, not an editable
    # install from a different worktree.  Source mode intentionally places
    # the vendored PyBoy first. Cython mode places only the harness source on
    # PYTHONPATH so the interpreter's installed extension modules are used.
    vendored_pyboy = (project_root / "vendor" / "pyboy-src").resolve(strict=False)
    source_entries = [str(project_root / "src"), str(project_root)]
    if runtime_mode == "source":
        source_entries.insert(0, str(vendored_pyboy))
    old_pythonpath = environment.get("PYTHONPATH")
    if old_pythonpath:
        for entry in old_pythonpath.split(os.pathsep):
            if not entry:
                continue
            entry_path = Path(entry).expanduser()
            if not entry_path.is_absolute():
                entry_path = project_root / entry_path
            if runtime_mode == "cython" and entry_path.resolve(strict=False) == vendored_pyboy:
                continue
            source_entries.append(entry)
    environment["PYTHONPATH"] = os.pathsep.join(source_entries)
    if runtime_mode == "source":
        environment["PYBOY_NO_CYTHON"] = "1"
    else:
        environment.pop("PYBOY_NO_CYTHON", None)

    # The stdio and golden-path tests are environment-driven.  Default them
    # to the pinned Red stock ROM when it is available, without overwriting a
    # caller's explicit selection.
    red_rom = rom_root / "red" / "pokemon-red.gb"
    red_sym = rom_root / "red" / "pokemon-red.sym"
    if "POKERED_ROM_PATH" not in environment and red_rom.is_file():
        environment["POKERED_ROM_PATH"] = str(red_rom)
    if "POKERED_SYM_PATH" not in environment and red_sym.is_file():
        environment["POKERED_SYM_PATH"] = str(red_sym)
    if "POKERED_ROM_SHA1" not in environment:
        selected_rom = environment.get("POKERED_ROM_PATH")
        if selected_rom:
            selected_path = Path(selected_rom).expanduser()
            if not selected_path.is_absolute():
                selected_path = project_root / selected_path
            try:
                selected_key = _asset_key(
                    selected_path.resolve(strict=False).relative_to(
                        rom_root.expanduser().resolve(strict=False)
                    )
                )
            except ValueError:
                selected_key = _asset_key(selected_path)
        else:
            selected_key = Path("red/pokemon-red.gb")
        selected_expected = expected_sha1.get(selected_key)
        if selected_expected:
            environment["POKERED_ROM_SHA1"] = selected_expected
    if "POKERED_SYM_SHA1" not in environment:
        selected_sym = environment.get("POKERED_SYM_PATH")
        if selected_sym:
            selected_path = Path(selected_sym).expanduser()
            if not selected_path.is_absolute():
                selected_path = project_root / selected_path
            try:
                selected_key = _asset_key(
                    selected_path.resolve(strict=False).relative_to(
                        rom_root.expanduser().resolve(strict=False)
                    )
                )
            except ValueError:
                selected_key = _asset_key(selected_path)
            selected_expected = expected_sha1.get(selected_key)
            if selected_expected:
                environment["POKERED_SYM_SHA1"] = selected_expected
    return environment


def probe_runtime(
    python_executable: Path,
    project_root: Path,
    environment: dict[str, str],
) -> dict[str, Any]:
    """Probe the interpreter that will run pytest, not the parent shell."""

    probe = r"""
import importlib.machinery
import importlib.metadata
import importlib.util
import json
import platform
import sys
from pathlib import Path

PYBOY_RUNTIME_MODULES = (
    "pyboy",
    "pyboy.pyboy",
    "pyboy.utils",
    "pyboy.core.mb",
    "pyboy.core.serial",
)
CYTHON_RUNTIME_MODULES = tuple(
    name for name in PYBOY_RUNTIME_MODULES if name != "pyboy"
)

def spec_path(name):
    spec = importlib.util.find_spec(name)
    return str(spec.origin) if spec and spec.origin else None

def module_kind(path):
    if not path:
        return "missing"
    suffixes = tuple(importlib.machinery.EXTENSION_SUFFIXES)
    if any(path.endswith(suffix) for suffix in suffixes):
        return "cython/native-extension"
    if path.endswith(".py"):
        return "python-source"
    return "unknown"

result = {
    "python_executable": sys.executable,
    "python_version": platform.python_version(),
    "platform": platform.platform(),
    "pytest_version": None,
    "pyboy_version": None,
    "pyboy_revision": None,
    "pyboy_module": spec_path("pyboy"),
    "pyboy_kind": module_kind(spec_path("pyboy")),
    "serial_module": spec_path("pyboy.core.serial"),
    "pyboy_modules": {
        name: spec_path(name) for name in PYBOY_RUNTIME_MODULES
    },
    "pyboy_module_kinds": {},
    "pyboy_mode": "unknown",
    "harness_module": spec_path("pokered_harness"),
    "serial_core": None,
    "serial_contract": "unavailable",
}
result["pyboy_module_kinds"] = {
    name: module_kind(path) for name, path in result["pyboy_modules"].items()
}
module_kinds = result["pyboy_module_kinds"]
if all(module_kinds.get(name) == "cython/native-extension" for name in CYTHON_RUNTIME_MODULES):
    result["pyboy_mode"] = "cython"
elif all(module_kinds.get(name) == "python-source" for name in PYBOY_RUNTIME_MODULES):
    result["pyboy_mode"] = "source"
for package, key in (("pytest", "pytest_version"), ("pyboy", "pyboy_version")):
    try:
        result[key] = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        pass
try:
    import pyboy

    result["pyboy_version"] = getattr(pyboy, "__version__", None)
    result["pyboy_revision"] = getattr(pyboy, "__pokered_harness_revision__", None)
except Exception as exc:
    result["pyboy_import_error"] = f"{type(exc).__name__}: {exc}"
try:
    import pokered_harness.link.serial_core as serial_core
    serial_cls = getattr(serial_core, "SerialCore", None)
    result["serial_core"] = repr(serial_cls)
    required = ("set_SB", "set_SC", "apply_external_edge", "tick")
    result["serial_contract"] = (
        "bit-accurate-backend"
        if serial_cls is not None and all(hasattr(serial_cls, name) for name in required)
        else "incompatible-stock-or-partial"
    )
except Exception as exc:
    result["serial_contract_error"] = f"{type(exc).__name__}: {exc}"
print(json.dumps(result, sort_keys=True))
"""
    try:
        completed = subprocess.run(
            [str(python_executable), "-c", probe],
            cwd=project_root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=30.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"probe_error": f"{type(exc).__name__}: {exc}"}
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if completed.returncode != 0 or not lines:
        return {
            "probe_error": (
                f"runtime probe exit={completed.returncode}; "
                f"stdout={completed.stdout[-1000:]!r}; stderr={completed.stderr[-1000:]!r}"
            )
        }
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        return {"probe_error": f"invalid runtime probe JSON: {exc}: {lines[-1]!r}"}


def runtime_problems(
    project_root: Path,
    runtime: dict[str, Any],
    *,
    expected_mode: str | None = None,
) -> list[str]:
    """Return runtime identity failures that must prevent a green gate."""

    problems: list[str] = []
    if expected_mode is not None and expected_mode not in RUNTIME_MODES:
        raise ValueError(f"unsupported runtime mode: {expected_mode!r}")
    if runtime.get("probe_error"):
        problems.append(f"runtime probe failed: {runtime['probe_error']}")
        return problems

    if expected_mode is not None and runtime.get("pyboy_mode") != expected_mode:
        problems.append(
            "selected interpreter runtime mode mismatch: "
            f"expected {expected_mode!r}, got {runtime.get('pyboy_mode')!r}"
        )

    versions_file = project_root / "VERSIONS.md"
    expected_version = parse_expected_pyboy_version(versions_file)
    expected_manifest_revision = parse_expected_pyboy_revision(versions_file)
    actual_version = runtime.get("pyboy_version")
    if not actual_version:
        problems.append("PyBoy version was not reported by the selected interpreter")
    elif expected_version and actual_version != expected_version:
        problems.append(
            f"PyBoy version mismatch: expected {expected_version!r}, got {actual_version!r}"
        )

    revision_file = project_root / "vendor" / "pyboy-src" / "POKERED_HARNESS_PYBOY_REVISION"
    if not revision_file.is_file():
        problems.append(f"pinned PyBoy revision marker is missing: {revision_file}")
    else:
        try:
            expected_revision = revision_file.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError) as exc:
            problems.append(f"could not read PyBoy revision marker: {type(exc).__name__}: {exc}")
        else:
            if not expected_revision:
                problems.append("pinned PyBoy revision marker is empty")
            elif runtime.get("pyboy_revision") != expected_revision:
                problems.append(
                    "PyBoy revision mismatch: "
                    f"expected {expected_revision!r}, got {runtime.get('pyboy_revision')!r}"
                )
            if (
                expected_manifest_revision
                and runtime.get("pyboy_revision") != expected_manifest_revision
            ):
                problems.append(
                    "PyBoy revision does not match VERSIONS.md: "
                    f"expected {expected_manifest_revision!r}, "
                    f"got {runtime.get('pyboy_revision')!r}"
                )

    if runtime.get("serial_contract") != "bit-accurate-backend":
        problems.append(
            "selected interpreter does not expose the bit-accurate serial contract "
            f"({runtime.get('serial_contract')!r})"
        )
    if not runtime.get("pyboy_module"):
        problems.append("selected interpreter cannot resolve the PyBoy module")
    if not runtime.get("harness_module"):
        problems.append("selected interpreter cannot resolve the harness package")
    return problems


def _resolve_child_path(value: str, project_root: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = project_root / path
    return path.resolve(strict=False)


def _asset_record_for_path(records: Iterable[AssetRecord], path: Path) -> AssetRecord | None:
    resolved = path.resolve(strict=False)
    for record in records:
        if Path(record.path).resolve(strict=False) == resolved:
            return record
    return None


def environment_policy_problems(
    *,
    project_root: Path,
    environment: dict[str, str],
    assets: Iterable[AssetRecord],
) -> list[str]:
    """Reject inherited settings that could make the gate test another input."""

    problems: list[str] = []
    if os.environ.get("POKERED_SKIP_SHA1", "").strip():
        problems.append("POKERED_SKIP_SHA1 is set; release gates cannot use the hash bypass")

    asset_list = list(assets)
    rom_record: AssetRecord | None = None
    sym_record: AssetRecord | None = None
    rom_value = environment.get("POKERED_ROM_PATH")
    sym_value = environment.get("POKERED_SYM_PATH")

    if rom_value:
        rom_path = _resolve_child_path(rom_value, project_root)
        rom_record = _asset_record_for_path(asset_list, rom_path)
        if rom_record is None or rom_record.kind != "rom":
            problems.append(f"primary ROM is outside the inspected asset set: {rom_value}")
        elif rom_record.status != "ok":
            problems.append(
                f"primary ROM is not a verified asset: {rom_record.label} {rom_record.status}"
            )
    elif rom_value is not None:
        problems.append("POKERED_ROM_PATH is blank")

    if sym_value:
        sym_path = _resolve_child_path(sym_value, project_root)
        sym_record = _asset_record_for_path(asset_list, sym_path)
        if sym_record is None or sym_record.kind != "symbol":
            problems.append(f"primary symbols are outside the inspected asset set: {sym_value}")
        elif sym_record.status != "ok":
            problems.append(
                f"primary symbols are not a verified asset: {sym_record.label} {sym_record.status}"
            )
    elif sym_value is not None:
        problems.append("POKERED_SYM_PATH is blank")

    if rom_record is not None and sym_record is not None:
        rom_parent = Path(rom_record.path).parent.name
        sym_parent = Path(sym_record.path).parent.name
        if rom_parent != sym_parent:
            problems.append(
                "primary ROM and symbol files are from different version directories: "
                f"{rom_parent!r} vs {sym_parent!r}"
            )

    if rom_record is not None and rom_record.kind == "rom":
        selected_sha = environment.get("POKERED_ROM_SHA1", "").strip().lower()
        if not selected_sha:
            problems.append("POKERED_ROM_SHA1 is missing for the primary ROM")
        elif rom_record.actual_sha1 != selected_sha:
            problems.append(
                f"primary ROM SHA-1 does not match inspected bytes: "
                f"expected {rom_record.actual_sha1!r}, got {selected_sha!r}"
            )
    if sym_record is not None and sym_record.kind == "symbol":
        selected_sha = environment.get("POKERED_SYM_SHA1", "").strip().lower()
        if not selected_sha:
            problems.append("POKERED_SYM_SHA1 is missing for the primary symbols")
        elif sym_record.actual_sha1 != selected_sha:
            problems.append(
                f"primary symbols SHA-1 does not match inspected bytes: "
                f"expected {sym_record.actual_sha1!r}, got {selected_sha!r}"
            )
    return problems


def load_required_test_keys(
    project_root: Path,
) -> tuple[dict[str, frozenset[tuple[str, str]]], str]:
    """Load the strict acceptance manifest from the checked-out tier config."""

    config_path = project_root / "tests" / "_tier_config.py"
    if not config_path.is_file():
        return {}, f"tier configuration is missing: {config_path}"
    try:
        namespace = runpy.run_path(str(config_path))
    except Exception as exc:  # noqa: BLE001 - fail closed on any config-load error
        return {}, f"tier configuration could not be loaded: {type(exc).__name__}: {exc}"

    raw = namespace.get("TIER_REQUIRED_TESTS")
    if not isinstance(raw, dict):
        return {}, "tier configuration has no TIER_REQUIRED_TESTS manifest"

    result: dict[str, frozenset[tuple[str, str]]] = {}
    for tier_name, raw_keys in raw.items():
        if not isinstance(tier_name, str) or not isinstance(
            raw_keys, (set, frozenset, tuple, list)
        ):
            return {}, f"invalid required-test manifest entry for {tier_name!r}"
        normalized: set[tuple[str, str]] = set()
        for key in raw_keys:
            if not isinstance(key, (tuple, list)) or len(key) != 2:
                return {}, f"invalid required-test key in tier {tier_name!r}: {key!r}"
            module, test_name = key
            if not isinstance(module, str) or not isinstance(test_name, str):
                return {}, f"invalid required-test key in tier {tier_name!r}: {key!r}"
            normalized.add((module, test_name))
        result[tier_name] = frozenset(normalized)
    return result, ""


def load_required_nodeids(
    project_root: Path,
) -> tuple[dict[str, frozenset[str]], str]:
    """Load exact matrix/role node IDs required by the production gate."""

    config_path = project_root / "tests" / "_tier_config.py"
    if not config_path.is_file():
        return {}, f"tier configuration is missing: {config_path}"
    try:
        namespace = runpy.run_path(str(config_path))
    except Exception as exc:  # noqa: BLE001 - fail closed on any config-load error
        return {}, f"tier configuration could not be loaded: {type(exc).__name__}: {exc}"

    raw = namespace.get("TIER_REQUIRED_NODEIDS")
    if not isinstance(raw, dict):
        return {}, "tier configuration has no TIER_REQUIRED_NODEIDS manifest"

    result: dict[str, frozenset[str]] = {}
    for tier_name, raw_nodeids in raw.items():
        if not isinstance(tier_name, str) or not isinstance(
            raw_nodeids, (set, frozenset, tuple, list)
        ):
            return {}, f"invalid required-nodeid manifest entry for {tier_name!r}"
        normalized: set[str] = set()
        for nodeid in raw_nodeids:
            if not isinstance(nodeid, str) or not nodeid or "::" not in nodeid:
                return {}, f"invalid required node ID in tier {tier_name!r}: {nodeid!r}"
            normalized.add(_normalize_nodeid(nodeid))
        result[tier_name] = frozenset(normalized)
    return result, ""


def _reason_counter(payload: dict[str, Any]) -> dict[str, int]:
    reasons = Counter()
    for record in payload.get("tests", []):
        if record.get("outcome") == "skipped" or record.get("was_xfail"):
            reason = str(record.get("reason") or "(no reason reported)").strip()
            reasons[reason] += 1
    for entry in payload.get("collection_skips", []):
        reason = str(entry.get("reason") or "(collection skip without a reason)").strip()
        reasons[reason] += 1
    return dict(sorted(reasons.items()))


def _load_gate_report(
    path: Path,
    *,
    expected_returncode: int | None = None,
    allow_partial: bool = False,
) -> GateReport:
    """Read and validate a report produced by the gate pytest plugin.

    A timed-out pytest process cannot run ``pytest_sessionfinish``.  The
    plugin therefore maintains a separate progress report while tests run;
    that report is allowed to contain the selected node set plus only the
    terminal outcomes observed before termination.  It is never accepted as
    a complete report.
    """

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return GateReport(
            Counts(errors=1),
            error=f"could not read pytest report: {type(exc).__name__}: {exc}",
        )
    if not isinstance(payload, dict):
        return GateReport(Counts(errors=1), error="pytest report root is not an object")

    try:
        counts = Counts.from_report(payload)
        records = payload.get("tests")
        if not isinstance(records, list):
            raise TypeError("pytest report has no tests list")
        collection_only = payload.get("collection_only", False)
        if not isinstance(collection_only, bool):
            raise TypeError("pytest report collection_only is not boolean")

        collection_errors = payload.get("collection_errors")
        if not isinstance(collection_errors, list):
            raise TypeError("pytest report has no collection_errors list")
        collection_skips = payload.get("collection_skips", [])
        if not isinstance(collection_skips, list):
            raise TypeError("pytest report collection_skips is not a list")
        for collection_entry in (*collection_errors, *collection_skips):
            if not isinstance(collection_entry, dict):
                raise TypeError("pytest report collection entry is not an object")
            if not isinstance(collection_entry.get("nodeid"), str):
                raise TypeError("pytest report collection nodeid is not a string")
            if not isinstance(collection_entry.get("reason"), str):
                raise TypeError("pytest report collection reason is not a string")

        exitstatus = payload.get("exitstatus")
        if isinstance(exitstatus, bool) or not isinstance(exitstatus, int):
            raise TypeError("pytest report exitstatus is not an integer")
        if (
            expected_returncode is not None
            and expected_returncode >= 0
            and exitstatus != expected_returncode
        ):
            raise ValueError(
                "pytest report exitstatus does not match process returncode: "
                f"{exitstatus} != {expected_returncode}"
            )

        collected = payload.get("collected")
        nodeids = payload.get("nodeids")
        if isinstance(collected, bool) or not isinstance(collected, int) or collected < 0:
            raise ValueError("pytest report collected count is invalid")
        if not isinstance(nodeids, list) or any(not isinstance(nodeid, str) for nodeid in nodeids):
            raise ValueError("pytest report nodeids is invalid")
        if collected != len(nodeids) or len(set(nodeids)) != len(nodeids):
            raise ValueError("pytest report collected/nodeids are inconsistent")
        if not collection_only and not allow_partial and collected != len(records):
            raise ValueError(
                "pytest report collected item count does not match reported test outcomes: "
                f"{collected} != {len(records)}"
            )
        if collection_only and records:
            raise ValueError("collection-only pytest report contains test outcomes")

        derived = Counts(total=len(records), errors=len(collection_errors))
        seen_nodeids: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                raise TypeError("pytest report contains a non-object test record")
            nodeid = record.get("nodeid")
            if not isinstance(nodeid, str) or not nodeid or nodeid in seen_nodeids:
                raise ValueError("pytest report contains an invalid or duplicate nodeid")
            seen_nodeids.add(nodeid)
            outcome = record.get("outcome")
            if outcome not in {"passed", "failed", "skipped", "error"}:
                raise ValueError(f"pytest report contains unknown outcome {outcome!r}")
            if record.get("when") not in {"setup", "call", "teardown"}:
                raise ValueError("pytest report contains an invalid test phase")
            if not isinstance(record.get("was_xfail"), bool):
                raise TypeError("pytest report was_xfail must be boolean")
            if not isinstance(record.get("reason"), str):
                raise TypeError("pytest report reason must be a string")

            if record["was_xfail"] and outcome == "skipped":
                derived.xfailed += 1
            elif record["was_xfail"] and outcome == "passed":
                derived.xpassed += 1
            elif outcome == "passed":
                derived.passed += 1
            elif outcome == "skipped":
                derived.skipped += 1
            elif outcome == "failed":
                derived.failed += 1
            else:
                derived.errors += 1

        if derived.total != sum(
            getattr(derived, field_name)
            for field_name in ("passed", "failed", "skipped", "xfailed", "xpassed")
        ) + (derived.errors - len(collection_errors)):
            raise ValueError("pytest report test outcomes do not add up to total")

        if not collection_only and not allow_partial and seen_nodeids != set(nodeids):
            raise ValueError("pytest report nodeids do not match test records")
        if allow_partial and not seen_nodeids.issubset(set(nodeids)):
            raise ValueError("partial pytest report contains an unselected test outcome")
        if counts != derived:
            raise ValueError(
                "pytest report count fields do not match individual outcomes: "
                f"declared={counts!r}, derived={derived!r}"
            )
    except (TypeError, ValueError) as exc:
        return GateReport(Counts(errors=1), error=f"invalid pytest report: {exc}")

    return GateReport(
        counts=counts,
        skip_reasons=_reason_counter(payload),
        nodeids=tuple(nodeids),
        collection_errors=tuple(collection_errors),
        collection_skips=tuple(collection_skips),
        collection_only=collection_only,
    )


def load_gate_report(path: Path) -> tuple[Counts, dict[str, int], str]:
    """Load the stable JSON emitted by ``tests._gate_report``.

    Keep this small compatibility wrapper for focused callers; subprocess
    execution uses the richer validated :class:`GateReport` directly.
    """

    report = _load_gate_report(path)
    return report.counts, report.skip_reasons, report.error


def _terminate_process(process: subprocess.Popen[str]) -> None:
    """Terminate and reap a gate child and its process group."""

    pid = getattr(process, "pid", None)
    if pid is None:
        return
    if os.name == "posix":
        try:
            # ``start_new_session=True`` makes the child PID the process-group
            # ID.  Signal the group even when the parent already exited: a
            # grandchild can otherwise keep the stdout pipe open forever.
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                pass
        return

    # Windows has no killpg.  The caller creates a new process group; taskkill
    # is the portable last resort for descendants when a timeout occurs.
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=5.0)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pass


def _process_creation_kwargs() -> dict[str, Any]:
    """Return process-group options for a gate subprocess."""

    if os.name == "posix":
        return {"start_new_session": True}
    return {
        "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    }


def _communicate_after_termination(process: subprocess.Popen[str]) -> str:
    """Drain a terminated child without allowing a leaked pipe to hang us."""

    try:
        output, _ = process.communicate(timeout=5.0)
        return output or ""
    except subprocess.TimeoutExpired as exc:
        _terminate_process(process)
        # A descendant that escaped the process-group boundary can retain the
        # read end.  Close this process's copy and retain the diagnostic text
        # already collected instead of waiting forever on EOF.
        stream = getattr(process, "stdout", None)
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
        partial = exc.output
        if isinstance(partial, bytes):
            return partial.decode(errors="replace")
        return partial or ""


def _pytest_console_script(python_executable: Path) -> Path | None:
    """Find the pytest console script belonging to ``python_executable``."""

    bin_directory = python_executable.parent
    names = ("pytest.exe", "pytest") if os.name == "nt" else ("pytest", "pytest.exe")
    for name in names:
        candidate = bin_directory / name
        if candidate.is_file():
            return candidate
    return None


def run_collection_preflight(
    *,
    project_root: Path,
    python_executable: Path,
    environment: dict[str, str],
    timeout_seconds: float = COLLECTION_TIMEOUT_SECONDS,
) -> list[CollectionResult]:
    """Run both supported pytest collection entry points.

    A release must prove that the module invocation and the console script
    resolve the same test tree.  This is intentionally separate from the
    marker-tier subprocesses: a collection failure must remain visible even
    when a selected tier happens to contain no affected tests.
    """

    commands: list[tuple[str, list[str]]] = [
        (
            "python-module",
            [
                str(python_executable),
                "-m",
                "pytest",
                "tests",
                "--collect-only",
                "-q",
                *PYTEST_GATE_ARGUMENTS,
            ],
        )
    ]
    console_script = _pytest_console_script(python_executable)
    console_missing = console_script is None
    if console_missing:
        commands.append(
            (
                "pytest-console",
                [
                    str(python_executable.parent / "pytest"),
                    "tests",
                    "--collect-only",
                    "-q",
                    *PYTEST_GATE_ARGUMENTS,
                ],
            )
        )
    else:
        commands.append(
            (
                "pytest-console",
                [
                    str(console_script),
                    "tests",
                    "--collect-only",
                    "-q",
                    *PYTEST_GATE_ARGUMENTS,
                ],
            )
        )

    with tempfile.TemporaryDirectory(prefix="pokered-collection-") as directory:
        results: list[CollectionResult] = []
        for index, (name, command) in enumerate(commands):
            if name == "pytest-console" and console_missing:
                results.append(
                    CollectionResult(
                        name=name,
                        command=command,
                        status="FAIL",
                        returncode=None,
                        reason=(
                            "pytest console script was not found beside the selected "
                            f"interpreter {python_executable}"
                        ),
                    )
                )
                continue
            results.append(
                _run_collection_command(
                    name=name,
                    command=command,
                    project_root=project_root,
                    environment=environment,
                    timeout_seconds=timeout_seconds,
                    report_path=Path(directory) / f"{index}.json",
                )
            )

    if all(result.status == "PASS" for result in results):
        first, second = results
        first_nodes = set(first.nodeids)
        second_nodes = set(second.nodeids)
        if first_nodes != second_nodes:
            missing_from_second = sorted(first_nodes - second_nodes)
            missing_from_first = sorted(second_nodes - first_nodes)
            reason = (
                "pytest collection entry points selected different test trees: "
                f"missing_from_console={missing_from_second!r}; "
                f"missing_from_module={missing_from_first!r}"
            )
            for result in results:
                result.status = "FAIL"
                result.reason = reason
    return results


def run_fixture_manifest_validation(
    *,
    project_root: Path,
    python_executable: Path,
    environment: dict[str, str],
    fixture_root: Path,
    validate_bytes: bool,
    timeout_seconds: float = COLLECTION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Validate the tracked external-fixture manifest with the gate runtime."""

    manifest_path = project_root / FIXTURE_MANIFEST_RELATIVE_PATH
    validator_path = project_root / "scripts" / "validate_fixture_manifest.py"
    mode = "byte" if validate_bytes else "schema"
    command = [
        str(python_executable),
        str(validator_path),
        "--manifest",
        str(manifest_path),
    ]
    if validate_bytes:
        command.extend(("--fixture-root", str(fixture_root)))
    else:
        command.append("--schema-only")

    try:
        completed = subprocess.run(
            command,
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "status": "FAIL",
            "mode": mode,
            "returncode": 124 if isinstance(exc, subprocess.TimeoutExpired) else None,
            "entries": None,
            "reason": f"fixture manifest validator failed: {type(exc).__name__}: {exc}",
        }

    output = "\n".join(
        part for part in (completed.stdout.strip(), completed.stderr.strip()) if part
    )
    entries: int | None = None
    match = re.search(r"validation passed: (\d+) entries", completed.stdout)
    if match is not None:
        entries = int(match.group(1))
    return {
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "mode": mode,
        "returncode": completed.returncode,
        "entries": entries,
        "reason": "" if completed.returncode == 0 else output[-4000:],
    }


def fixture_manifest_provenance_problems(
    manifest_path: Path,
    required_ids: Iterable[str] = CERTIFIED_FIXTURE_IDS,
) -> list[str]:
    """Return missing or non-verified provenance for certified fixtures."""

    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"fixture manifest provenance could not be read: {type(exc).__name__}: {exc}"]
    fixtures = document.get("fixtures") if isinstance(document, dict) else None
    if not isinstance(fixtures, list):
        return ["fixture manifest provenance has no fixture list"]
    by_id = {
        fixture.get("id"): fixture
        for fixture in fixtures
        if isinstance(fixture, dict) and isinstance(fixture.get("id"), str)
    }
    problems: list[str] = []
    for fixture_id in sorted(set(required_ids)):
        fixture = by_id.get(fixture_id)
        if fixture is None:
            problems.append(f"certified fixture is absent from manifest: {fixture_id}")
            continue
        provenance = fixture.get("provenance")
        status = provenance.get("status") if isinstance(provenance, dict) else None
        if status != "verified":
            problems.append(
                f"certified fixture provenance is not verified: {fixture_id} ({status!r})"
            )
    return problems


def fixture_manifest_input_problems(
    manifest_path: Path,
    *,
    rom_root: Path,
    assets: Iterable[AssetRecord],
    expected_sha1: dict[Path, str],
) -> list[str]:
    """Check fixture input pins against the inspected ROM/SYM inventory.

    Byte validation proves that a state matches the hashes written in the
    manifest.  This second check proves that those manifest pins still agree
    with ``VERSIONS.md`` and with the files the gate actually inspected.  A
    manifest that is internally self-consistent but points at a different
    ROM/SYM set must not be accepted as release evidence.
    """

    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"fixture manifest input pins could not be read: {type(exc).__name__}: {exc}"]
    fixtures = document.get("fixtures") if isinstance(document, dict) else None
    if not isinstance(fixtures, list):
        return ["fixture manifest input pins have no fixture list"]

    try:
        resolved_rom_root = rom_root.expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        resolved_rom_root = rom_root
    records_by_key: dict[Path, AssetRecord] = {}
    for record in assets:
        if record.kind not in {"rom", "symbol"}:
            continue
        try:
            relative = Path(record.path).resolve(strict=False).relative_to(resolved_rom_root)
        except (OSError, RuntimeError, ValueError):
            continue
        records_by_key[_asset_key(relative)] = record

    problems: list[str] = []
    seen: set[str] = set()

    def add(problem: str) -> None:
        if problem not in seen:
            seen.add(problem)
            problems.append(problem)

    for index, fixture in enumerate(fixtures):
        if not isinstance(fixture, dict):
            add(f"fixture manifest entry {index} is not an object")
            continue
        fixture_id = fixture.get("id", f"index {index}")
        for field_name, kind in (("expected_rom", "rom"), ("expected_symbols", "symbol")):
            pin = fixture.get(field_name)
            if not isinstance(pin, dict):
                add(f"fixture {fixture_id} has no valid {field_name} pin")
                continue
            raw_path = pin.get("path")
            pin_sha1 = pin.get("sha1")
            if not isinstance(raw_path, str) or not isinstance(pin_sha1, str):
                add(f"fixture {fixture_id} has an invalid {field_name} pin")
                continue
            key = _asset_key(raw_path)
            expected = expected_sha1.get(key)
            if expected is None:
                add(f"fixture {fixture_id} {field_name} is not pinned in VERSIONS.md: {raw_path}")
            elif expected != pin_sha1.lower():
                add(
                    f"fixture {fixture_id} {field_name} pin disagrees with VERSIONS.md: "
                    f"expected {expected}, got {pin_sha1.lower()}"
                )
            record = records_by_key.get(key)
            if record is None or record.kind != kind:
                add(
                    f"fixture {fixture_id} {field_name} is outside the inspected {kind} set: "
                    f"{raw_path}"
                )
                continue
            if record.status != "ok":
                add(
                    f"fixture {fixture_id} {field_name} uses an unverified asset: "
                    f"{record.label} {record.status}"
                )
            elif record.actual_sha1 != pin_sha1.lower():
                add(
                    f"fixture {fixture_id} {field_name} does not match inspected bytes: "
                    f"expected {record.actual_sha1}, got {pin_sha1.lower()}"
                )
    return problems


def run_matrix_collection_audit(
    *,
    project_root: Path,
    collections: Iterable[CollectionResult],
) -> dict[str, Any]:
    """Apply the repository matrix auditor to the gate's collection result."""

    collection_list = list(collections)
    matrix_path = project_root / "scripts" / "tcp_link_matrix.py"
    try:
        namespace = runpy.run_path(str(matrix_path))
        audit_collection = namespace["audit_collection"]
    except (OSError, KeyError, TypeError, ValueError) as exc:
        return {
            "status": "FAIL",
            "structural_pass": False,
            "acceptance_matrix_complete": False,
            "collected": 0,
            "groups": {},
            "acceptance_gaps": {},
            "runtime": "not-run",
            "reason": f"matrix auditor could not be loaded: {type(exc).__name__}: {exc}",
        }

    nodeids = collection_list[0].nodeids if collection_list else ()
    errors = tuple(
        collection.reason
        for collection in collection_list
        if collection.status != "PASS" and collection.reason
    )
    skips = tuple(
        collection.reason
        for collection in collection_list
        if collection.status == "PASS" and collection.reason
    )
    try:
        audit = audit_collection(
            nodeids,
            collection_errors=errors,
            collection_skips=skips,
        )
    except (TypeError, ValueError) as exc:
        return {
            "status": "FAIL",
            "structural_pass": False,
            "acceptance_matrix_complete": False,
            "collected": len(nodeids),
            "groups": {},
            "acceptance_gaps": {},
            "runtime": "not-run",
            "reason": f"matrix audit failed: {type(exc).__name__}: {exc}",
        }
    audit["status"] = (
        "PASS"
        if audit.get("structural_pass") and audit.get("acceptance_matrix_complete")
        else "FAIL"
    )
    return audit


def _run_collection_command(
    *,
    name: str,
    command: list[str],
    project_root: Path,
    environment: dict[str, str],
    timeout_seconds: float,
    report_path: Path,
) -> CollectionResult:
    started = time.monotonic()
    child_environment = dict(environment)
    child_environment["POKERED_GATE_REPORT"] = str(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        report_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        return CollectionResult(
            name=name,
            command=command,
            status="FAIL",
            returncode=None,
            duration_seconds=time.monotonic() - started,
            reason=f"could not prepare collection report: {type(exc).__name__}: {exc}",
        )
    try:
        process = subprocess.Popen(
            command,
            cwd=project_root,
            env=child_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            **_process_creation_kwargs(),
        )
    except OSError as exc:
        return CollectionResult(
            name=name,
            command=command,
            status="FAIL",
            returncode=None,
            duration_seconds=time.monotonic() - started,
            reason=f"could not start collection command: {type(exc).__name__}: {exc}",
        )

    try:
        output, _ = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate_process(process)
        output = _communicate_after_termination(process)
        return CollectionResult(
            name=name,
            command=command,
            status="FAIL",
            returncode=124,
            duration_seconds=time.monotonic() - started,
            output_tail=(
                f"pytest collection timed out after {timeout_seconds:.1f}s\n{output[-8000:]}"
            ),
            reason="collection timeout",
        )

    raw_returncode = process.returncode
    returncode = int(raw_returncode) if raw_returncode is not None else 125
    report = _load_gate_report(report_path, expected_returncode=returncode)
    problems: list[str] = []
    if report.error:
        problems.append(report.error)
    if returncode != 0:
        problems.append(f"pytest collection returned exit code {returncode}")
    if not report.collection_only:
        problems.append("pytest collection report did not identify a collection-only run")
    if report.collection_errors:
        problems.append(f"pytest reported {len(report.collection_errors)} collection error(s)")
    if report.collection_skips:
        problems.append(f"pytest reported {len(report.collection_skips)} collection skip(s)")
    return CollectionResult(
        name=name,
        command=command,
        status="PASS" if not problems else "FAIL",
        returncode=returncode,
        nodeids=report.nodeids,
        duration_seconds=time.monotonic() - started,
        output_tail=output[-8000:],
        reason="; ".join(problems),
    )


def run_pytest_once(
    *,
    project_root: Path,
    python_executable: Path,
    environment: dict[str, str],
    expression: str,
    timeout_seconds: float,
    report_path: Path,
    selectors: Sequence[str] = (),
) -> tuple[int, GateReport, str, list[str]]:
    command = [str(python_executable), "-m", "pytest"]
    command.extend(selectors or ("tests",))
    command.extend(
        (
            "-m",
            expression,
            *PYTEST_GATE_ARGUMENTS,
            "-rA",
            "--maxfail=0",
        )
    )
    child_environment = dict(environment)
    child_environment["POKERED_GATE_REPORT"] = str(report_path)
    progress_path = report_path.with_suffix(".progress.json")
    child_environment["POKERED_GATE_PROGRESS_REPORT"] = str(progress_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        report_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        return (
            127,
            GateReport(
                Counts(errors=1),
                error=f"could not prepare pytest report path: {type(exc).__name__}: {exc}",
            ),
            "",
            command,
        )
    try:
        progress_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        return (
            127,
            GateReport(
                Counts(errors=1),
                error=(
                    f"could not prepare pytest progress report path: {type(exc).__name__}: {exc}"
                ),
            ),
            "",
            command,
        )

    try:
        process = subprocess.Popen(
            command,
            cwd=project_root,
            env=child_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            **_process_creation_kwargs(),
        )
    except OSError as exc:
        return (
            127,
            GateReport(
                Counts(errors=1),
                error=f"could not start pytest: {type(exc).__name__}: {exc}",
            ),
            "",
            command,
        )

    timed_out = False
    try:
        output, _ = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process(process)
        output = _communicate_after_termination(process)
        returncode = 124
    else:
        raw_returncode = process.returncode
        returncode = int(raw_returncode) if raw_returncode is not None else 125

    report = _load_gate_report(
        report_path,
        expected_returncode=None if timed_out else returncode,
    )
    # A progress report is evidence of the tests that had reached a terminal
    # outcome only after an actual timeout.  A missing or malformed final
    # report on an otherwise exited process must remain an error; falling back
    # to stale/partial progress there could turn a broken runner green.
    if timed_out and report.error:
        progress_report = _load_gate_report(progress_path, allow_partial=True)
        if not progress_report.error:
            report = progress_report
    if timed_out:
        timeout_reason = f"pytest timed out after {timeout_seconds:.1f}s"
        report = GateReport(
            counts=report.counts,
            skip_reasons=report.skip_reasons,
            nodeids=report.nodeids,
            collection_errors=report.collection_errors,
            collection_skips=report.collection_skips,
            error=(f"{timeout_reason}; {report.error}" if report.error else timeout_reason),
        )
    if report.error:
        output = f"{output}\n{report.error}"
    # Keep the command and enough output to diagnose a failed gate without
    # flooding the orchestrator with every emulator trace.
    return returncode, report, output[-8000:], command


def synthetic_optional_skip(
    name: str,
    reason: str,
) -> TierResult:
    return TierResult(
        name=name,
        description=TIER_DESCRIPTIONS[name],
        expression=TIER_EXPRESSIONS[name],
        required=False,
        status="SKIP",
        counts=Counts(total=1, skipped=1),
        skip_reasons={reason: 1},
        reason=reason,
    )


def _test_key_from_nodeid(nodeid: str) -> tuple[str, str] | None:
    if "::" not in nodeid:
        return None
    path, test_name = nodeid.split("::", 1)
    return Path(path).name, test_name.split("[", 1)[0]


def _normalize_nodeid(nodeid: str) -> str:
    """Normalize the path portion of a pytest node ID for manifest checks."""

    path, separator, test_name = nodeid.partition("::")
    normalized_path = path.replace("\\", "/")
    while normalized_path.startswith("./"):
        normalized_path = normalized_path[2:]
    return f"{normalized_path}::{test_name}" if separator else normalized_path


def _required_test_problems(
    nodeids: Iterable[str],
    required_test_keys: Iterable[tuple[str, str]],
) -> list[str]:
    actual = {key for nodeid in nodeids if (key := _test_key_from_nodeid(nodeid)) is not None}
    missing = sorted(set(required_test_keys) - actual)
    return [
        f"required acceptance test is absent from selected items: {module}::{name}"
        for module, name in missing
    ]


def _required_nodeid_problems(
    nodeids: Iterable[str],
    required_nodeids: Iterable[str],
) -> list[str]:
    actual = {_normalize_nodeid(nodeid) for nodeid in nodeids}
    missing = sorted({_normalize_nodeid(nodeid) for nodeid in required_nodeids} - actual)
    return [f"required matrix case is absent from selected items: {nodeid}" for nodeid in missing]


def _add_counts(target: Counts, source: Counts) -> None:
    """Add one validated pytest result to an aggregate counter."""

    for field_name in Counts.__dataclass_fields__:
        setattr(target, field_name, getattr(target, field_name) + getattr(source, field_name))


def _matrix_report_path(report_directory: Path, nodeid: str) -> Path:
    """Return a collision-resistant report path for one matrix selector."""

    digest = hashlib.sha256(nodeid.encode("utf-8")).hexdigest()[:20]
    return report_directory / f"matrix-{digest}.json"


def _drain_matrix_stream(stream: Any, chunks: list[str]) -> None:
    """Drain one matrix child stream without coupling it to the supervisor."""

    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            chunks.append(chunk)
    except (OSError, ValueError):
        return
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _kill_matrix_process(process: subprocess.Popen[str]) -> bool:
    """Kill and reap a matrix child process group within a bounded deadline."""

    pid = getattr(process, "pid", None)
    if pid is None:
        return False

    def wait_for_exit(timeout: float) -> bool:
        try:
            process.wait(timeout=timeout)
        except (subprocess.TimeoutExpired, OSError):
            return process.poll() is not None
        return process.poll() is not None

    if process.poll() is None:
        if os.name == "posix":
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
        else:
            try:
                process.kill()
            except OSError:
                pass

    # A successful signal does not mean that Popen has reaped the child. Give
    # it a bounded wait, then fall back to the direct handle if a process-group
    # signal was unavailable or ineffective. The return value lets the gate
    # retain a cleanup failure rather than claiming a complete case result.
    reaped = wait_for_exit(5.0)
    if not reaped:
        try:
            process.kill()
        except OSError:
            pass
        reaped = wait_for_exit(5.0)

    stream = getattr(process, "stdout", None)
    if stream is not None:
        try:
            stream.close()
        except OSError:
            pass
    return reaped


def run_matrix_tier(
    *,
    name: str,
    project_root: Path,
    python_executable: Path,
    environment: dict[str, str],
    required_problems: list[str],
    timeout_override: float | None,
    report_directory: Path,
    required_test_keys: Iterable[tuple[str, str]] = (),
    required_nodeids: Iterable[str] = (),
    matrix_workers: int = DEFAULT_MATRIX_WORKERS,
    matrix_timeout_override: float | None = None,
) -> TierResult:
    """Run strict acceptance rows under hard per-case and aggregate bounds.

    Each selector gets its own pytest process and report files.  A small
    supervisor keeps at most ``matrix_workers`` processes active, kills active
    process groups at the aggregate cutoff, and records queued rows as
    ``NOT_STARTED``.  It never relies on an executor context whose shutdown
    could wait indefinitely for an emulator child.
    """

    required = name not in OPTIONAL_TIERS
    nodeids = tuple(sorted(set(required_nodeids)))
    if not nodeids:
        return TierResult(
            name=name,
            description=TIER_DESCRIPTIONS[name],
            expression=TIER_EXPRESSIONS[name],
            required=required,
            status="FAIL",
            reason="strict matrix tier has no required node IDs",
        )
    if matrix_workers <= 0:
        raise ValueError("matrix_workers must be positive")
    if required and name in REQUIRED_TIER_ASSETS and required_problems:
        reason = "required assets unavailable: " + "; ".join(required_problems)
        return TierResult(
            name=name,
            description=TIER_DESCRIPTIONS[name],
            expression=TIER_EXPRESSIONS[name],
            required=True,
            status="BLOCKED",
            reason=reason,
            selected_nodeids=list(nodeids),
        )

    selected_keys = {
        key for nodeid in nodeids if (key := _test_key_from_nodeid(nodeid)) is not None
    }
    failures = [
        f"required acceptance test is absent from matrix selectors: {module}::{test_name}"
        for module, test_name in sorted(set(required_test_keys) - selected_keys)
    ]
    timeout = timeout_override or MATRIX_CASE_TIMEOUT_SECONDS.get(
        name, DEFAULT_TIMEOUT_SECONDS[name]
    )
    max_workers = min(matrix_workers, len(nodeids))
    waves = (len(nodeids) + max_workers - 1) // max_workers
    aggregate_timeout = matrix_timeout_override or (
        timeout * waves + MATRIX_AGGREGATE_GRACE_SECONDS
    )
    if aggregate_timeout <= 0:
        raise ValueError("matrix aggregate timeout must be positive")
    started = time.monotonic()
    aggregate_deadline = started + aggregate_timeout
    aggregate = Counts()
    aggregate_reasons: Counter[str] = Counter()
    case_results_by_nodeid: dict[str, MatrixCaseResult] = {}
    output_tails: list[str] = []
    pending = deque(nodeids)
    active: dict[str, dict[str, Any]] = {}

    def record_case(
        *,
        nodeid: str,
        returncode: int | None,
        report: GateReport,
        output: str,
        duration: float,
        timed_out: bool = False,
        timeout_reason: str = "",
        report_kind: str = "final",
        cleanup_error: str = "",
    ) -> None:
        _add_counts(aggregate, report.counts)
        aggregate_reasons.update(report.skip_reasons)
        problems: list[str] = []
        if timed_out:
            problems.append(timeout_reason or f"matrix case timed out after {timeout:.1f}s")
        if cleanup_error:
            problems.append(cleanup_error)
        if report.error:
            problems.append(report.error)
        if returncode not in (None, 0) and not timed_out:
            problems.append(f"pytest returned exit code {returncode}")
        if report.collection_errors:
            problems.append(f"pytest reported {len(report.collection_errors)} collection error(s)")
        if report.collection_skips:
            problems.append(f"pytest reported {len(report.collection_skips)} collection skip(s)")
        if report.counts.total == 0:
            problems.append("pytest selected no tests for matrix selector")
        if report.counts.total != 1:
            problems.append(
                f"matrix selector produced {report.counts.total} test outcomes; expected exactly 1"
            )
        normalized_report_nodeids = {_normalize_nodeid(item) for item in report.nodeids}
        if _normalize_nodeid(nodeid) not in normalized_report_nodeids:
            problems.append(f"matrix selector was not collected: {nodeid}")
        if (
            report.counts.failed
            or report.counts.errors
            or report.counts.xfailed
            or report.counts.xpassed
        ):
            problems.append(
                "unexpected outcomes: "
                f"failed={report.counts.failed} errors={report.counts.errors} "
                f"xfailed={report.counts.xfailed} xpassed={report.counts.xpassed}"
            )
        if required and report.counts.skipped:
            problems.append("required matrix case produced a test skip")
        if report.counts.passed != 1:
            problems.append(
                f"matrix case did not produce exactly one pass: passed={report.counts.passed}"
            )
        if problems:
            failures.extend(f"{nodeid}: {problem}" for problem in problems)
            if output.strip():
                output_tails.append(f"{nodeid}\n{output[-4000:]}")
        case_results_by_nodeid[nodeid] = MatrixCaseResult(
            nodeid=nodeid,
            status="TIMEOUT" if timed_out else ("PASS" if not problems else "FAIL"),
            returncode=124 if timed_out else returncode,
            duration_seconds=duration,
            counts=report.counts,
            reason="; ".join(problems),
            output_tail=output[-4000:],
            deadline_seconds=timeout,
            report_kind=report_kind,
            partial=timed_out or report_kind == "partial",
        )

    def record_not_started(nodeid: str, reason: str) -> None:
        failures.append(f"{nodeid}: {reason}")
        case_results_by_nodeid[nodeid] = MatrixCaseResult(
            nodeid=nodeid,
            status="NOT_STARTED",
            returncode=None,
            duration_seconds=0.0,
            reason=reason,
            deadline_seconds=timeout,
            report_kind="not-started",
            partial=True,
        )

    def load_case_report(
        state: dict[str, Any],
        *,
        returncode: int,
        timed_out: bool,
    ) -> tuple[GateReport, str]:
        final_report = _load_gate_report(
            state["report_path"],
            expected_returncode=None if timed_out else returncode,
        )
        if not timed_out:
            return final_report, "final"
        if not final_report.error:
            return final_report, "final"
        progress_report = _load_gate_report(state["progress_path"], allow_partial=True)
        if not progress_report.error:
            return progress_report, "partial"
        return GateReport(
            Counts(errors=1),
            error=(f"{final_report.error}; progress report: {progress_report.error}"),
        ), "missing"

    def start_case(nodeid: str) -> None:
        report_path = _matrix_report_path(report_directory, nodeid)
        progress_path = report_path.with_suffix(".progress.json")
        for path in (report_path, progress_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                record_case(
                    nodeid=nodeid,
                    returncode=None,
                    report=GateReport(
                        Counts(errors=1),
                        error=f"could not prepare matrix report path: {type(exc).__name__}: {exc}",
                    ),
                    output="",
                    duration=0.0,
                    report_kind="missing",
                )
                return
        child_environment = dict(environment)
        child_environment["POKERED_GATE_REPORT"] = str(report_path)
        child_environment["POKERED_GATE_PROGRESS_REPORT"] = str(progress_path)
        command = [str(python_executable), "-m", "pytest", nodeid]
        command.extend(
            (
                "-m",
                TIER_EXPRESSIONS[name],
                *PYTEST_GATE_ARGUMENTS,
                "-rA",
                "--maxfail=0",
            )
        )
        try:
            process = subprocess.Popen(
                command,
                cwd=project_root,
                env=child_environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                **_process_creation_kwargs(),
            )
        except OSError as exc:
            record_case(
                nodeid=nodeid,
                returncode=None,
                report=GateReport(
                    Counts(errors=1),
                    error=f"could not start matrix pytest: {type(exc).__name__}: {exc}",
                ),
                output="",
                duration=0.0,
                report_kind="missing",
            )
            return
        chunks: list[str] = []
        reader = threading.Thread(
            target=_drain_matrix_stream,
            args=(process.stdout, chunks),
            name=f"pokered-gate-output-{name}",
            daemon=True,
        )
        reader.start()
        now = time.monotonic()
        active[nodeid] = {
            "process": process,
            "chunks": chunks,
            "reader": reader,
            "started_at": now,
            "deadline_at": now + timeout,
            "report_path": report_path,
            "progress_path": progress_path,
        }

    def finish_case(nodeid: str, *, timed_out: bool, reason: str = "") -> None:
        state = active.pop(nodeid)
        process = state["process"]
        cleanup_error = ""
        if timed_out:
            if not _kill_matrix_process(process):
                cleanup_error = "matrix child did not terminate after the cleanup deadline"
            returncode = 124
        else:
            returncode = process.poll()
            if returncode is None:
                returncode = 125
        # ``poll()`` only means the process has exited; the reader can still
        # be draining its pipe.  Join it before retaining diagnostics so a
        # fast case does not lose the very failure text needed to audit it.
        reader = state["reader"]
        reader.join(timeout=1.0)
        if reader.is_alive():
            cleanup_error = (
                f"{cleanup_error}; " if cleanup_error else ""
            ) + "matrix output reader did not terminate after the cleanup deadline"
        report, report_kind = load_case_report(
            state,
            returncode=int(returncode),
            timed_out=timed_out,
        )
        record_case(
            nodeid=nodeid,
            returncode=int(returncode),
            report=report,
            output="".join(state["chunks"]),
            duration=time.monotonic() - state["started_at"],
            timed_out=timed_out,
            timeout_reason=reason,
            report_kind=report_kind,
            cleanup_error=cleanup_error,
        )

    while pending or active:
        now = time.monotonic()
        for nodeid, state in list(active.items()):
            process = state["process"]
            if process.poll() is not None:
                finish_case(nodeid, timed_out=False)
            elif now >= state["deadline_at"]:
                finish_case(
                    nodeid,
                    timed_out=True,
                    reason=f"matrix case timed out after {timeout:.1f}s",
                )

        if time.monotonic() >= aggregate_deadline:
            for nodeid in list(active):
                finish_case(
                    nodeid,
                    timed_out=True,
                    reason=(f"matrix aggregate deadline exceeded after {aggregate_timeout:.1f}s"),
                )
            while pending:
                record_not_started(
                    pending.popleft(),
                    "matrix aggregate deadline expired before the case started",
                )
            break

        while pending and len(active) < max_workers:
            if time.monotonic() >= aggregate_deadline:
                break
            start_case(pending.popleft())

        if not active:
            continue
        remaining = aggregate_deadline - time.monotonic()
        if remaining > 0:
            next_deadline = min(
                [state["deadline_at"] for state in active.values()] + [aggregate_deadline]
            )
            time.sleep(min(0.05, max(0.0, next_deadline - time.monotonic())))

    case_results = [case_results_by_nodeid[nodeid] for nodeid in sorted(nodeids)]
    unexpected = aggregate.failed + aggregate.errors + aggregate.xfailed + aggregate.xpassed
    status = (
        "PASS"
        if not failures
        and not unexpected
        and len(case_results) == len(nodeids)
        and aggregate.total == len(nodeids)
        and aggregate.skipped == 0
        and aggregate.passed == len(nodeids)
        else "FAIL"
    )
    return TierResult(
        name=name,
        description=TIER_DESCRIPTIONS[name],
        expression=TIER_EXPRESSIONS[name],
        required=required,
        status=status,
        counts=aggregate,
        returncodes=[case.returncode for case in case_results if case.returncode is not None],
        duration_seconds=time.monotonic() - started,
        skip_reasons=dict(sorted(aggregate_reasons.items())),
        command=[
            str(python_executable),
            "-m",
            "pytest",
            "<one-required-nodeid-per-worker>",
            "-m",
            TIER_EXPRESSIONS[name],
            f"--matrix-workers={max_workers}",
            f"--case-timeout={timeout:.1f}",
            f"--aggregate-timeout={aggregate_timeout:.1f}",
        ],
        output_tail="\n\n".join(output_tails)[-8000:],
        iteration_failures=failures,
        selected_nodeids=list(nodeids),
        case_results=case_results,
    )


def run_tier(
    *,
    name: str,
    project_root: Path,
    python_executable: Path,
    environment: dict[str, str],
    required_problems: list[str],
    repeat: int,
    timeout_override: float | None,
    report_directory: Path,
    required_test_keys: Iterable[tuple[str, str]] = (),
    required_nodeids: Iterable[str] = (),
    matrix_workers: int = DEFAULT_MATRIX_WORKERS,
    matrix_timeout_override: float | None = None,
) -> TierResult:
    if name in {"trade", "battle"} and required_nodeids:
        return run_matrix_tier(
            name=name,
            project_root=project_root,
            python_executable=python_executable,
            environment=environment,
            required_problems=required_problems,
            timeout_override=timeout_override,
            report_directory=report_directory,
            required_test_keys=required_test_keys,
            required_nodeids=required_nodeids,
            matrix_workers=matrix_workers,
            matrix_timeout_override=matrix_timeout_override,
        )

    required = name not in OPTIONAL_TIERS
    if required and name in REQUIRED_TIER_ASSETS and required_problems:
        reason = "required assets unavailable: " + "; ".join(required_problems)
        return TierResult(
            name=name,
            description=TIER_DESCRIPTIONS[name],
            expression=TIER_EXPRESSIONS[name],
            required=True,
            status="BLOCKED",
            reason=reason,
        )

    timeout = timeout_override or DEFAULT_TIMEOUT_SECONDS[name]
    if name == "timing":
        repeat = max(repeat, 5)
    else:
        repeat = 1

    aggregate = Counts()
    aggregate_reasons: Counter[str] = Counter()
    returncodes: list[int] = []
    output_tail = ""
    command: list[str] | None = None
    iteration_failures: list[str] = []
    baseline_nodeids: set[str] | None = None
    selected_nodeids: list[str] = []
    started = time.monotonic()
    for iteration in range(1, repeat + 1):
        report_path = report_directory / f"{name}-{iteration}.json"
        returncode, report, output, command = run_pytest_once(
            project_root=project_root,
            python_executable=python_executable,
            environment=environment,
            expression=TIER_EXPRESSIONS[name],
            timeout_seconds=timeout,
            report_path=report_path,
        )
        counts = report.counts
        aggregate.total += counts.total
        aggregate.passed += counts.passed
        aggregate.failed += counts.failed
        aggregate.skipped += counts.skipped
        aggregate.xfailed += counts.xfailed
        aggregate.xpassed += counts.xpassed
        aggregate.errors += counts.errors
        aggregate_reasons.update(report.skip_reasons)
        returncodes.append(returncode)
        if output.strip():
            output_tail = output

        problems: list[str] = []
        if report.error:
            problems.append(report.error)
        if returncode != 0:
            problems.append(f"pytest returned exit code {returncode}")
        if report.collection_errors:
            problems.append(f"pytest reported {len(report.collection_errors)} collection error(s)")
        if report.collection_skips:
            problems.append(f"pytest reported {len(report.collection_skips)} collection skip(s)")
        if counts.total == 0:
            problems.append("pytest selected no tests for the tier expression")
        if counts.failed or counts.errors or counts.xfailed or counts.xpassed:
            problems.append(
                "unexpected outcomes: "
                f"failed={counts.failed} errors={counts.errors} "
                f"xfailed={counts.xfailed} xpassed={counts.xpassed}"
            )
        if required and counts.skipped:
            problems.append(f"required tier produced {counts.skipped} test skip(s)")
        if not counts.passed and not counts.xpassed:
            problems.append("iteration produced no passing test outcome")

        required_problems = _required_test_problems(report.nodeids, required_test_keys)
        problems.extend(required_problems)
        problems.extend(_required_nodeid_problems(report.nodeids, required_nodeids))
        current_nodeids = set(report.nodeids)
        if baseline_nodeids is None:
            baseline_nodeids = current_nodeids
            selected_nodeids = list(report.nodeids)
        elif name == "timing" and current_nodeids != baseline_nodeids:
            problems.append(
                "timing-tier selected test set changed between repetitions: "
                f"baseline={len(baseline_nodeids)} current={len(current_nodeids)}"
            )
        if problems:
            iteration_failures.extend(f"iteration {iteration}: {problem}" for problem in problems)

    duration = time.monotonic() - started
    unexpected = aggregate.failed + aggregate.errors + aggregate.xfailed + aggregate.xpassed
    if iteration_failures or any(code != 0 for code in returncodes) or unexpected:
        status = "FAIL"
    elif aggregate.total == 0:
        status = "FAIL"
        output_tail = f"pytest selected no tests for marker expression {TIER_EXPRESSIONS[name]!r}"
    elif required and aggregate.skipped:
        status = "FAIL"
        output_tail = (
            f"required tier produced {aggregate.skipped} skip(s); "
            "missing/unsupported coverage is not accepted in the production gate\n" + output_tail
        )
    elif aggregate.skipped:
        # Optional coverage may be unavailable on a BYO-ROM machine, but a
        # partial run must remain visibly optional instead of being reported as
        # a full PASS merely because another optional case passed.
        status = "SKIP"
    else:
        status = "PASS" if aggregate.passed else "SKIP"

    return TierResult(
        name=name,
        description=TIER_DESCRIPTIONS[name],
        expression=TIER_EXPRESSIONS[name],
        required=required,
        status=status,
        counts=aggregate,
        returncodes=returncodes,
        duration_seconds=duration,
        skip_reasons=dict(sorted(aggregate_reasons.items())),
        command=command,
        output_tail=output_tail,
        iteration_failures=iteration_failures,
        selected_nodeids=selected_nodeids,
    )


def _optional_preflight_reason(name: str, records: list[AssetRecord]) -> str | None:
    roms = [record for record in records if record.kind == "rom"]
    fixtures = [record for record in records if record.kind == "fixture"]
    if not any(record.status == "ok" for record in roms):
        return f"optional {name} acceptance unavailable: no pinned ROM assets found"
    if not any(record.status == "ok" for record in fixtures):
        return f"optional {name} acceptance unavailable: no derived link fixtures found"
    return None


def run_runtime_gate(
    *,
    mode: str,
    project_root: Path,
    python_executable: Path,
    rom_root: Path,
    fixture_root: Path,
    expected_sha1: dict[Path, str],
    assets: list[AssetRecord],
    selected: Sequence[str],
    required_tests_by_tier: dict[str, frozenset[tuple[str, str]]],
    required_nodeids_by_tier: dict[str, frozenset[str]],
    configuration_problems: Iterable[str] = (),
    repeat: int = 5,
    timeout_override: float | None = None,
    matrix_workers: int = DEFAULT_MATRIX_WORKERS,
    matrix_timeout_override: float | None = None,
) -> RuntimeGateResult:
    """Run the complete gate once under one explicit runtime.

    ``both`` is an orchestration choice, not a PyBoy runtime.  Keeping this
    function restricted to ``source`` and ``cython`` makes it impossible for
    a dual invocation to accidentally probe or run tests under an ambiguous
    inherited environment.
    """

    if mode not in RUNTIME_MODES:
        raise ValueError(f"runtime gate requires an explicit runtime mode: {mode!r}")

    selected_tiers = tuple(selected)
    real_rom_scope = bool(set(selected_tiers) & REQUIRED_TIER_ASSETS)
    environment = build_test_environment(
        project_root,
        rom_root,
        fixture_root,
        expected_sha1,
        runtime_mode=mode,
    )
    # Subprocess acceptance tests must use the exact interpreter whose runtime
    # contract was probed above, not a stale auxiliary virtualenv discovered
    # from the worktree.
    environment["POKERED_PYTHON"] = str(python_executable)
    runtime = probe_runtime(python_executable, project_root, environment)
    gate_problems = runtime_problems(
        project_root,
        runtime,
        expected_mode=mode,
    )
    gate_problems.extend(
        environment_policy_problems(
            project_root=project_root,
            environment=environment,
            assets=assets,
        )
    )
    gate_problems.extend(configuration_problems)

    collections = run_collection_preflight(
        project_root=project_root,
        python_executable=python_executable,
        environment=environment,
        timeout_seconds=timeout_override or COLLECTION_TIMEOUT_SECONDS,
    )

    fixture_manifest = run_fixture_manifest_validation(
        project_root=project_root,
        python_executable=python_executable,
        environment=environment,
        fixture_root=fixture_root,
        validate_bytes=real_rom_scope,
        timeout_seconds=timeout_override or COLLECTION_TIMEOUT_SECONDS,
    )
    if fixture_manifest["status"] != "PASS":
        gate_problems.append(
            "fixture manifest validation failed: "
            f"mode={fixture_manifest['mode']} "
            f"{fixture_manifest.get('reason') or 'unknown error'}"
        )
    elif real_rom_scope:
        gate_problems.extend(
            fixture_manifest_input_problems(
                project_root / FIXTURE_MANIFEST_RELATIVE_PATH,
                rom_root=rom_root,
                assets=assets,
                expected_sha1=expected_sha1,
            )
        )
        gate_problems.extend(
            fixture_manifest_provenance_problems(project_root / FIXTURE_MANIFEST_RELATIVE_PATH)
        )

    matrix_audit = run_matrix_collection_audit(
        project_root=project_root,
        collections=collections,
    )
    if real_rom_scope:
        if not matrix_audit.get("structural_pass"):
            gate_problems.append("link matrix structural audit failed")
        if not matrix_audit.get("acceptance_matrix_complete"):
            gaps = matrix_audit.get("acceptance_gaps", {})
            gap_counts = ", ".join(
                f"{name}={len(entries)}"
                for name, entries in gaps.items()
                if isinstance(entries, (tuple, list))
            )
            gate_problems.append(
                "strict acceptance matrix declaration is incomplete"
                + (f" ({gap_counts})" if gap_counts else "")
            )

    required_problems = required_asset_problems(assets)
    tiers: list[TierResult] = []
    with tempfile.TemporaryDirectory(prefix=f"pokered-gate-{mode}-") as temp_directory:
        report_directory = Path(temp_directory)
        for name in selected_tiers:
            if name in OPTIONAL_TIERS:
                reason = _optional_preflight_reason(name, assets)
                if reason:
                    tiers.append(synthetic_optional_skip(name, reason))
                    continue
            tiers.append(
                run_tier(
                    name=name,
                    project_root=project_root,
                    python_executable=python_executable,
                    environment=environment,
                    required_problems=required_problems,
                    repeat=repeat,
                    timeout_override=timeout_override,
                    report_directory=report_directory,
                    required_test_keys=required_tests_by_tier.get(name, ()),
                    required_nodeids=required_nodeids_by_tier.get(name, ()),
                    matrix_workers=matrix_workers,
                    matrix_timeout_override=matrix_timeout_override,
                )
            )

    return RuntimeGateResult(
        mode=mode,
        runtime=runtime,
        collections=collections,
        fixture_manifest=fixture_manifest,
        matrix_audit=matrix_audit,
        tiers=tiers,
        gate_problems=gate_problems,
    )


def run_runtime_gates(
    *,
    runtime_mode: str,
    project_root: Path,
    python_executable: Path,
    cython_python_executable: Path | None = None,
    rom_root: Path,
    fixture_root: Path,
    expected_sha1: dict[Path, str],
    assets: list[AssetRecord],
    selected: Sequence[str],
    required_tests_by_tier: dict[str, frozenset[tuple[str, str]]],
    required_nodeids_by_tier: dict[str, frozenset[str]],
    configuration_problems: Iterable[str] = (),
    repeat: int = 5,
    timeout_override: float | None = None,
    matrix_workers: int = DEFAULT_MATRIX_WORKERS,
    matrix_timeout_override: float | None = None,
) -> tuple[RuntimeGateResult, ...]:
    """Run identical selected tiers for each runtime named by the CLI.

    ``--python`` remains the source-runtime interpreter and the interpreter
    used by single-runtime invocations.  A dual gate may provide a separate
    ``--cython-python`` environment so source-mode packaging checks do not see
    metadata left by a native PyBoy installation.  When it is omitted, retain
    the historical one-interpreter behavior; the source child still has its
    explicit source import environment and the gate remains fail-closed if
    that environment violates the packaging contract.
    """

    configuration_problems = tuple(configuration_problems)
    python_by_mode = {
        "source": python_executable,
        "cython": cython_python_executable or python_executable,
    }
    return tuple(
        run_runtime_gate(
            mode=mode,
            project_root=project_root,
            python_executable=python_by_mode[mode],
            rom_root=rom_root,
            fixture_root=fixture_root,
            expected_sha1=expected_sha1,
            assets=assets,
            selected=selected,
            required_tests_by_tier=required_tests_by_tier,
            required_nodeids_by_tier=required_nodeids_by_tier,
            configuration_problems=configuration_problems,
            repeat=repeat,
            timeout_override=timeout_override,
            matrix_workers=matrix_workers,
            matrix_timeout_override=matrix_timeout_override,
        )
        for mode in runtime_modes_for_gate(runtime_mode)
    )


def runtime_gate_passes(result: RuntimeGateResult) -> bool:
    """Return whether one runtime result satisfies every existing gate rule."""

    tier_ok = all(
        tier.status == "PASS" if tier.required else tier.status in {"PASS", "SKIP"}
        for tier in result.tiers
    )
    return (
        result.runtime.get("pyboy_mode") == result.mode
        and bool(result.collections)
        and bool(result.tiers)
        and not result.gate_problems
        and all(collection.status == "PASS" for collection in result.collections)
        and tier_ok
    )


def runtime_gates_pass(results: Iterable[RuntimeGateResult]) -> bool:
    """Aggregate runtime results fail-closed, including an empty result set."""

    result_list = tuple(results)
    return bool(result_list) and all(runtime_gate_passes(result) for result in result_list)


def _format_counts(counts: Counts) -> str:
    return (
        f"total={counts.total} passed={counts.passed} failed={counts.failed} "
        f"skipped={counts.skipped} xfailed={counts.xfailed} "
        f"xpassed={counts.xpassed} errors={counts.errors}"
    )


def render_text(
    *,
    project_root: Path,
    rom_root: Path,
    fixture_root: Path,
    runtime: dict[str, Any],
    assets: list[AssetRecord],
    collections: list[CollectionResult],
    tiers: list[TierResult],
    gate_problems: list[str],
    overall: str,
    fixture_manifest: dict[str, Any] | None = None,
    matrix_audit: dict[str, Any] | None = None,
) -> str:
    lines = [
        "Pokémon harness production gate",
        f"project={project_root}",
        f"rom_root={rom_root}",
        f"fixture_root={fixture_root}",
        "environment:",
    ]
    for key in (
        "python_executable",
        "python_version",
        "pytest_version",
        "pyboy_version",
        "pyboy_revision",
        "pyboy_kind",
        "pyboy_module",
        "serial_module",
        "serial_contract",
        "harness_module",
    ):
        if key in runtime:
            lines.append(f"  {key}={runtime[key]}")
    if runtime.get("probe_error"):
        lines.append(f"  probe_error={runtime['probe_error']}")

    lines.append("gate-policy:")
    if gate_problems:
        lines.extend(f"  FAIL: {problem}" for problem in gate_problems)
    else:
        lines.append("  PASS")

    lines.append("collection:")
    for collection in collections:
        command = " ".join(collection.command)
        lines.append(
            f"  {collection.status:4} {collection.name}: "
            f"returncode={collection.returncode} duration={collection.duration_seconds:.1f}s"
        )
        lines.append(f"    command: {command}")
        if collection.reason:
            lines.append(f"    reason: {collection.reason}")
        if collection.status == "FAIL" and collection.output_tail:
            lines.append("    output tail:")
            lines.extend(f"      {line}" for line in collection.output_tail.splitlines()[-60:])

    lines.append("assets:")
    for asset in assets:
        suffix = []
        if asset.expected_sha1:
            suffix.append(f"expected_sha1={asset.expected_sha1}")
        if asset.actual_sha1:
            suffix.append(f"actual_sha1={asset.actual_sha1}")
        if asset.size is not None:
            suffix.append(f"size={asset.size}")
        detail = " " + " ".join(suffix) if suffix else ""
        lines.append(
            f"  {asset.status.upper():13} {asset.kind:7} {asset.label}: {asset.path}{detail}"
        )

    if fixture_manifest is not None:
        lines.append("fixture-manifest:")
        lines.append(
            f"  {fixture_manifest.get('status', 'UNKNOWN')}: "
            f"mode={fixture_manifest.get('mode', 'unknown')} "
            f"entries={fixture_manifest.get('entries')} "
            f"returncode={fixture_manifest.get('returncode')}"
        )
        if fixture_manifest.get("reason"):
            lines.append(f"  reason: {fixture_manifest['reason']}")

    if matrix_audit is not None:
        lines.append("matrix-audit:")
        lines.append(
            f"  {matrix_audit.get('status', 'UNKNOWN')}: "
            f"collected={matrix_audit.get('collected', 0)} "
            f"structural={'PASS' if matrix_audit.get('structural_pass') else 'FAIL'} "
            f"acceptance-declaration="
            f"{'PASS' if matrix_audit.get('acceptance_matrix_complete') else 'FAIL'}"
        )
        groups = matrix_audit.get("groups", {})
        if isinstance(groups, dict):
            for name, group in groups.items():
                if isinstance(group, dict):
                    lines.append(
                        f"  {name}: {group.get('present', 0)}/{group.get('expected', 0)} collected"
                    )
        if matrix_audit.get("reason"):
            lines.append(f"  reason: {matrix_audit['reason']}")

    lines.append("tiers:")
    for tier in tiers:
        lines.append(
            f"  {tier.status:8} {tier.name:7} {_format_counts(tier.counts)} "
            f"duration={tier.duration_seconds:.1f}s"
        )
        if tier.reason:
            lines.append(f"    reason: {tier.reason}")
        for reason, count in tier.skip_reasons.items():
            lines.append(f"    skip[{count}]: {reason}")
        for failure in tier.iteration_failures:
            lines.append(f"    iteration-failure: {failure}")
        for case in tier.case_results:
            lines.append(
                f"    case {case.status:4} {case.nodeid}: "
                f"{_format_counts(case.counts)} "
                f"returncode={case.returncode} duration={case.duration_seconds:.1f}s"
            )
            if case.reason:
                lines.append(f"      reason: {case.reason}")
        if tier.status in {"FAIL", "BLOCKED"} and tier.output_tail:
            lines.append("    output tail:")
            lines.extend(f"      {line}" for line in tier.output_tail.splitlines()[-60:])
    lines.append(f"overall: {overall}")
    return "\n".join(lines)


def render_dual_text(
    *,
    project_root: Path,
    rom_root: Path,
    fixture_root: Path,
    assets: Sequence[AssetRecord],
    runtime_results: Sequence[RuntimeGateResult],
    overall: str,
) -> str:
    """Render explicit source/Cython results without collapsing either run."""

    lines = [
        "Pokémon harness production gate",
        f"project={project_root}",
        f"rom_root={rom_root}",
        f"fixture_root={fixture_root}",
        "runtime-mode=both",
        "assets:",
    ]
    for asset in assets:
        suffix = []
        if asset.expected_sha1:
            suffix.append(f"expected_sha1={asset.expected_sha1}")
        if asset.actual_sha1:
            suffix.append(f"actual_sha1={asset.actual_sha1}")
        if asset.size is not None:
            suffix.append(f"size={asset.size}")
        detail = " " + " ".join(suffix) if suffix else ""
        lines.append(
            f"  {asset.status.upper():13} {asset.kind:7} {asset.label}: {asset.path}{detail}"
        )
    lines.append(
        "runtime-results:",
    )
    for result in runtime_results:
        status = "PASS" if runtime_gate_passes(result) else "FAIL"
        lines.append(f"  {result.mode}: {status}")
        detail = render_text(
            project_root=project_root,
            rom_root=rom_root,
            fixture_root=fixture_root,
            runtime=result.runtime,
            assets=[],
            collections=result.collections,
            tiers=result.tiers,
            gate_problems=result.gate_problems,
            overall=status,
            fixture_manifest=result.fixture_manifest,
            matrix_audit=result.matrix_audit,
        )
        lines.extend(f"    {line}" for line in detail.splitlines()[4:])
    lines.append(f"overall: {overall}")
    return "\n".join(lines)


def _jsonable_tier(tier: TierResult) -> dict[str, Any]:
    data = asdict(tier)
    data["counts"] = asdict(tier.counts)
    return data


def _runtime_result_json(result: RuntimeGateResult) -> dict[str, Any]:
    """Serialize one explicit runtime result for the dual JSON report."""

    return {
        "mode": result.mode,
        "runtime": result.runtime,
        "collections": [asdict(collection) for collection in result.collections],
        "fixture_manifest": result.fixture_manifest,
        "matrix_audit": result.matrix_audit,
        "tiers": [_jsonable_tier(tier) for tier in result.tiers],
        "gate_problems": result.gate_problems,
        "overall": "PASS" if runtime_gate_passes(result) else "FAIL",
    }


def _safe_text(value: Any, *, limit: int = 8000) -> str:
    """Bound and redact free-form diagnostics before retaining them."""

    text = "" if value is None else str(value)
    # Redact paths before credential matching. A path component such as
    # ``secret`` must not cause the credential regex to consume only the
    # suffix of a spaced path and leave the remainder machine-specific.
    text = _QUOTED_ABSOLUTE_PATH_RE.sub("<external-path>", text)
    text = _WINDOWS_ABSOLUTE_PATH_RE.sub("<external-path>", text)
    text = _SPACED_ABSOLUTE_PATH_RE.sub("<external-path>", text)
    text = _ABSOLUTE_PATH_RE.sub("<external-path>", text)
    text = _URI_CREDENTIAL_RE.sub(r"\1:[REDACTED]@", text)
    text = _BYTE_LITERAL_RE.sub("[BINARY DATA REDACTED]", text)
    text = _CREDENTIAL_TEXT_RE.sub("[CREDENTIAL REDACTED]", text)
    text = _LONG_TOKEN_RE.sub("[LONG TOKEN REDACTED]", text)
    text = "".join(
        character
        if character in "\n\r\t" or character.isprintable()
        else f"\\x{ord(character):02x}"
        for character in text
    )
    if len(text) > limit:
        text = "[...truncated...]\n" + text[-limit:]
    return text


def _evidence_roots(
    project_root: Path,
    rom_root: Path,
    fixture_root: Path,
) -> tuple[tuple[str, Path], ...]:
    return (
        ("rom-root", rom_root),
        ("fixture-root", fixture_root),
        ("project-root", project_root),
    )


def _portable_path(
    value: str | Path,
    roots: tuple[tuple[str, Path], ...],
) -> str:
    """Represent a path without retaining machine-local absolute paths."""

    candidate = Path(value)
    try:
        resolved_candidate = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        resolved_candidate = candidate

    for label, root in roots:
        try:
            resolved_root = root.expanduser().resolve(strict=False)
            relative = resolved_candidate.relative_to(resolved_root)
        except (OSError, RuntimeError, ValueError):
            continue
        if not relative.parts:
            return f"<{label}>"
        return f"<{label}>/{relative.as_posix()}"

    return "<external-path>"


def _safe_diagnostic(
    value: Any,
    roots: tuple[tuple[str, Path], ...],
    *,
    limit: int = 8000,
) -> str:
    text = "" if value is None else str(value)
    replacements: dict[str, str] = {}
    for label, root in roots:
        for raw in (str(root), str(root.expanduser())):
            if raw:
                replacements[raw] = f"<{label}>"
        try:
            replacements[str(root.expanduser().resolve(strict=False))] = f"<{label}>"
        except (OSError, RuntimeError):
            pass
    for raw, replacement in sorted(replacements.items(), key=lambda item: -len(item[0])):
        text = text.replace(raw, replacement)
    return _safe_text(text, limit=limit)


def _safe_command(
    command: list[str] | None,
    roots: tuple[tuple[str, Path], ...],
) -> list[str] | None:
    if command is None:
        return None
    result: list[str] = []
    for argument in command:
        try:
            path = Path(argument)
            is_absolute = path.is_absolute()
        except (TypeError, ValueError):
            is_absolute = False
        if is_absolute:
            result.append(_portable_path(argument, roots))
        else:
            result.append(_safe_diagnostic(argument, roots, limit=1000))
    return result


def _safe_runtime(
    runtime: dict[str, Any],
    roots: tuple[tuple[str, Path], ...],
) -> dict[str, Any]:
    path_keys = frozenset({"python_executable", "pyboy_module", "serial_module", "harness_module"})
    result: dict[str, Any] = {}
    for key, value in runtime.items():
        if value is None or isinstance(value, (bool, int, float)):
            result[key] = value
        elif key in path_keys:
            result[key] = _portable_path(str(value), roots)
        else:
            result[key] = _safe_diagnostic(value, roots)
    return result


def _safe_asset(
    asset: AssetRecord,
    roots: tuple[tuple[str, Path], ...],
) -> dict[str, Any]:
    data = asdict(asset)
    data["path"] = _portable_path(asset.path, roots)
    return data


def _safe_collection(
    collection: CollectionResult,
    roots: tuple[tuple[str, Path], ...],
) -> dict[str, Any]:
    data = asdict(collection)
    data["command"] = _safe_command(collection.command, roots) or []
    data["nodeids"] = [_safe_diagnostic(nodeid, roots, limit=1000) for nodeid in collection.nodeids]
    data["output_tail"] = _safe_diagnostic(collection.output_tail, roots)
    data["reason"] = _safe_diagnostic(collection.reason, roots, limit=2000)
    return data


def _safe_fixture_manifest(result: dict[str, Any]) -> dict[str, Any]:
    """Keep only bounded, non-path fields from manifest validation."""

    return {
        "status": result.get("status"),
        "mode": result.get("mode"),
        "returncode": result.get("returncode"),
        "entries": result.get("entries"),
        "reason": _safe_text(result.get("reason"), limit=4000),
    }


def _safe_matrix_audit(result: dict[str, Any]) -> dict[str, Any]:
    """Sanitize the matrix auditor while retaining exact coverage counts."""

    safe: dict[str, Any] = {
        "status": result.get("status"),
        "structural_pass": result.get("structural_pass"),
        "acceptance_matrix_complete": result.get("acceptance_matrix_complete"),
        "collected": result.get("collected"),
        "runtime": _safe_text(result.get("runtime"), limit=100),
        "reason": _safe_text(result.get("reason"), limit=2000),
        "groups": {},
        "acceptance_gaps": {},
    }
    raw_groups = result.get("groups", {})
    if isinstance(raw_groups, dict):
        groups: dict[str, Any] = {}
        for name, raw_group in raw_groups.items():
            if not isinstance(raw_group, dict):
                continue
            groups[str(name)] = {
                "expected": raw_group.get("expected"),
                "present": raw_group.get("present"),
                "missing": [
                    _safe_diagnostic(item, (), limit=1000) for item in raw_group.get("missing", ())
                ],
            }
        safe["groups"] = groups
    raw_gaps = result.get("acceptance_gaps", {})
    if isinstance(raw_gaps, dict):
        safe["acceptance_gaps"] = {
            str(name): [_safe_diagnostic(item, (), limit=1000) for item in entries]
            for name, entries in raw_gaps.items()
            if isinstance(entries, (tuple, list))
        }
    for key in (
        "acceptance_classifications",
        "remote_link_menu_classifications",
    ):
        raw_classifications = result.get(key)
        if isinstance(raw_classifications, dict):
            safe[key] = {
                str(name): _safe_diagnostic(status, (), limit=500)
                for name, status in raw_classifications.items()
            }
    raw_profile_pairs = result.get("remote_strict_profile_pairs")
    if isinstance(raw_profile_pairs, list):
        safe["remote_strict_profile_pairs"] = [
            {str(name): _safe_diagnostic(value, (), limit=500) for name, value in pair.items()}
            for pair in raw_profile_pairs
            if isinstance(pair, dict)
        ]
    return safe


def _safe_tier(
    tier: TierResult,
    roots: tuple[tuple[str, Path], ...],
) -> dict[str, Any]:
    data = _jsonable_tier(tier)
    data["command"] = _safe_command(tier.command, roots)
    data["output_tail"] = _safe_diagnostic(tier.output_tail, roots)
    data["reason"] = _safe_diagnostic(tier.reason, roots, limit=2000)
    data["iteration_failures"] = [
        _safe_diagnostic(failure, roots, limit=2000) for failure in tier.iteration_failures
    ]
    data["selected_nodeids"] = [
        _safe_diagnostic(nodeid, roots, limit=1000) for nodeid in tier.selected_nodeids
    ]
    data["skip_reasons"] = {
        _safe_diagnostic(reason, roots, limit=2000): count
        for reason, count in tier.skip_reasons.items()
    }
    data["case_results"] = []
    for case in tier.case_results:
        safe_case = asdict(case)
        safe_case["nodeid"] = _safe_diagnostic(case.nodeid, roots, limit=1000)
        safe_case["reason"] = _safe_diagnostic(case.reason, roots, limit=2000)
        safe_case["output_tail"] = _safe_diagnostic(case.output_tail, roots)
        data["case_results"].append(safe_case)
    return data


def build_evidence_payload(
    *,
    project_root: Path,
    rom_root: Path,
    fixture_root: Path,
    runtime: dict[str, Any],
    assets: list[AssetRecord],
    collections: list[CollectionResult],
    tiers: list[TierResult],
    gate_problems: list[str],
    overall: str,
    generated_at: str | None = None,
    evidence_error: str = "",
    fixture_manifest: dict[str, Any] | None = None,
    matrix_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the sanitized, metadata-only payload retained by the gate.

    The normal stdout JSON remains backward-compatible.  This separate
    payload is deliberately allow-listed and redacts free-form diagnostics so
    an evidence directory never becomes a copy of ROMs, save states, or the
    inherited process environment.
    """

    roots = _evidence_roots(project_root, rom_root, fixture_root)
    payload: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "project_root": "<project-root>",
        "rom_root": "<rom-root>",
        "fixture_root": "<fixture-root>",
        "runtime": _safe_runtime(runtime, roots),
        "collections": [_safe_collection(item, roots) for item in collections],
        "assets": [_safe_asset(item, roots) for item in assets],
        "tiers": [_safe_tier(item, roots) for item in tiers],
        "gate_problems": [
            _safe_diagnostic(problem, roots, limit=2000) for problem in gate_problems
        ],
        "overall": overall,
        "safety": {
            "rom_bytes": "not included",
            "credentials": "environment is not captured; free-form diagnostics are redacted",
            "diagnostics": "bounded text tails only",
        },
    }
    if fixture_manifest is not None:
        payload["fixture_manifest"] = _safe_fixture_manifest(fixture_manifest)
    if matrix_audit is not None:
        payload["matrix_audit"] = _safe_matrix_audit(matrix_audit)
    if evidence_error:
        payload["evidence_error"] = _safe_diagnostic(evidence_error, roots, limit=2000)
    return payload


def build_dual_evidence_payload(
    *,
    project_root: Path,
    rom_root: Path,
    fixture_root: Path,
    assets: list[AssetRecord],
    runtime_results: Sequence[RuntimeGateResult],
    overall: str,
    requested_mode: str = "both",
    generated_at: str | None = None,
    evidence_error: str = "",
) -> dict[str, Any]:
    """Build a sanitized evidence bundle containing both runtime executions."""

    roots = _evidence_roots(project_root, rom_root, fixture_root)
    runtimes: list[dict[str, Any]] = []
    for result in runtime_results:
        runtimes.append(
            {
                "mode": result.mode,
                "runtime": _safe_runtime(result.runtime, roots),
                "collections": [
                    _safe_collection(collection, roots) for collection in result.collections
                ],
                "fixture_manifest": _safe_fixture_manifest(result.fixture_manifest),
                "matrix_audit": _safe_matrix_audit(result.matrix_audit),
                "tiers": [_safe_tier(tier, roots) for tier in result.tiers],
                "gate_problems": [
                    _safe_diagnostic(problem, roots, limit=2000) for problem in result.gate_problems
                ],
                "overall": "PASS" if runtime_gate_passes(result) else "FAIL",
            }
        )

    payload: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "project_root": "<project-root>",
        "rom_root": "<rom-root>",
        "fixture_root": "<fixture-root>",
        "runtime_mode": requested_mode,
        "runtimes": runtimes,
        "assets": [_safe_asset(item, roots) for item in assets],
        "overall": overall,
        "safety": {
            "rom_bytes": "not included",
            "credentials": "environment is not captured; free-form diagnostics are redacted",
            "diagnostics": "bounded text tails only",
        },
    }
    if evidence_error:
        payload["evidence_error"] = _safe_diagnostic(evidence_error, roots, limit=2000)
    return payload


def _payload_counts_text(counts: dict[str, Any]) -> str:
    fields = ("total", "passed", "failed", "skipped", "xfailed", "xpassed", "errors")
    return " ".join(f"{field}={counts.get(field, 0)}" for field in fields)


def _render_dual_evidence_text(payload: dict[str, Any]) -> str:
    """Render a dual-runtime evidence payload with each run clearly scoped."""

    lines = [
        "Pokémon harness production gate evidence bundle",
        f"generated_at={payload.get('generated_at', '')}",
        f"overall: {payload.get('overall', 'UNKNOWN')}",
        f"runtime-mode={payload.get('runtime_mode', 'both')}",
        "assets:",
    ]
    for asset in payload.get("assets", []):
        details = []
        for key in ("expected_sha1", "actual_sha1", "size"):
            if asset.get(key) is not None:
                details.append(f"{key}={asset[key]}")
        suffix = f" {' '.join(details)}" if details else ""
        lines.append(
            f"  {str(asset.get('status', 'unknown')).upper():13} "
            f"{asset.get('kind', ''):7} {asset.get('label', '')}: "
            f"{asset.get('path', '')}{suffix}"
        )

    lines.append("runtimes:")
    for result in payload.get("runtimes", []):
        if not isinstance(result, dict):
            continue
        child_payload = {
            "generated_at": payload.get("generated_at", ""),
            "overall": result.get("overall", "UNKNOWN"),
            "runtime": result.get("runtime", {}),
            "collections": result.get("collections", []),
            "assets": [],
            "fixture_manifest": result.get("fixture_manifest"),
            "matrix_audit": result.get("matrix_audit"),
            "tiers": result.get("tiers", []),
            "gate_problems": result.get("gate_problems", []),
            "safety": {},
        }
        child_lines = render_evidence_text(child_payload).rstrip("\n").splitlines()
        lines.append(f"  {result.get('mode', 'unknown')}: {result.get('overall', 'UNKNOWN')}")
        # Omit the child's title, timestamp, and duplicate overall line; the
        # parent already identifies the combined evidence bundle.
        lines.extend(f"    {line}" for line in child_lines[3:])

    if payload.get("evidence_error"):
        lines.append(f"evidence-error: {payload['evidence_error']}")
    lines.append("safety:")
    for key, value in payload.get("safety", {}).items():
        lines.append(f"  {key}: {value}")
    return "\n".join(lines) + "\n"


def render_evidence_text(payload: dict[str, Any]) -> str:
    """Render the already-sanitized payload for human inspection."""

    if isinstance(payload.get("runtimes"), list):
        return _render_dual_evidence_text(payload)

    lines = [
        "Pokémon harness production gate evidence bundle",
        f"generated_at={payload.get('generated_at', '')}",
        f"overall: {payload.get('overall', 'UNKNOWN')}",
        "environment:",
    ]
    runtime = payload.get("runtime", {})
    if isinstance(runtime, dict):
        for key in (
            "python_executable",
            "python_version",
            "pytest_version",
            "pyboy_version",
            "pyboy_revision",
            "pyboy_kind",
            "pyboy_module",
            "serial_module",
            "serial_contract",
            "harness_module",
        ):
            if key in runtime:
                lines.append(f"  {key}={runtime[key]}")

    problems = payload.get("gate_problems", [])
    lines.append("gate-policy:")
    if problems:
        lines.extend(f"  FAIL: {problem}" for problem in problems)
    else:
        lines.append("  PASS")

    lines.append("collection:")
    for collection in payload.get("collections", []):
        lines.append(
            f"  {collection.get('status', 'UNKNOWN'):4} {collection.get('name', '')}: "
            f"returncode={collection.get('returncode')} "
            f"duration={collection.get('duration_seconds', 0.0):.1f}s"
        )
        command = collection.get("command", [])
        lines.append(f"    command: {' '.join(command)}")
        if collection.get("reason"):
            lines.append(f"    reason: {collection['reason']}")
        if collection.get("status") != "PASS" and collection.get("output_tail"):
            lines.append("    output tail:")
            lines.extend(f"      {line}" for line in collection["output_tail"].splitlines())

    lines.append("assets:")
    for asset in payload.get("assets", []):
        details = []
        for key in ("expected_sha1", "actual_sha1", "size"):
            if asset.get(key) is not None:
                details.append(f"{key}={asset[key]}")
        suffix = f" {' '.join(details)}" if details else ""
        lines.append(
            f"  {str(asset.get('status', 'unknown')).upper():13} "
            f"{asset.get('kind', ''):7} {asset.get('label', '')}: "
            f"{asset.get('path', '')}{suffix}"
        )

    fixture_manifest = payload.get("fixture_manifest")
    if isinstance(fixture_manifest, dict):
        lines.append("fixture-manifest:")
        lines.append(
            f"  {fixture_manifest.get('status', 'UNKNOWN')}: "
            f"mode={fixture_manifest.get('mode', 'unknown')} "
            f"entries={fixture_manifest.get('entries')} "
            f"returncode={fixture_manifest.get('returncode')}"
        )
        if fixture_manifest.get("reason"):
            lines.append(f"  reason: {fixture_manifest['reason']}")

    matrix_audit = payload.get("matrix_audit")
    if isinstance(matrix_audit, dict):
        lines.append("matrix-audit:")
        lines.append(
            f"  {matrix_audit.get('status', 'UNKNOWN')}: "
            f"collected={matrix_audit.get('collected', 0)} "
            f"structural={'PASS' if matrix_audit.get('structural_pass') else 'FAIL'} "
            f"acceptance-declaration="
            f"{'PASS' if matrix_audit.get('acceptance_matrix_complete') else 'FAIL'}"
        )
        groups = matrix_audit.get("groups", {})
        if isinstance(groups, dict):
            for name, group in groups.items():
                if isinstance(group, dict):
                    lines.append(
                        f"  {name}: {group.get('present', 0)}/{group.get('expected', 0)} collected"
                    )

    lines.append("tiers:")
    for tier in payload.get("tiers", []):
        lines.append(
            f"  {tier.get('status', 'UNKNOWN'):8} {tier.get('name', ''):7} "
            f"{_payload_counts_text(tier.get('counts', {}))} "
            f"duration={tier.get('duration_seconds', 0.0):.1f}s"
        )
        if tier.get("reason"):
            lines.append(f"    reason: {tier['reason']}")
        for reason, count in tier.get("skip_reasons", {}).items():
            lines.append(f"    skip[{count}]: {reason}")
        for failure in tier.get("iteration_failures", []):
            lines.append(f"    iteration-failure: {failure}")
        for case in tier.get("case_results", []):
            if not isinstance(case, dict):
                continue
            counts = case.get("counts", {})
            lines.append(
                f"    case {case.get('status', 'UNKNOWN'):4} "
                f"{case.get('nodeid', '')}: {_payload_counts_text(counts)} "
                f"returncode={case.get('returncode')} "
                f"duration={case.get('duration_seconds', 0.0):.1f}s"
            )
            if case.get("reason"):
                lines.append(f"      reason: {case['reason']}")
        if tier.get("status") in {"FAIL", "BLOCKED"} and tier.get("output_tail"):
            lines.append("    output tail:")
            lines.extend(f"      {line}" for line in tier["output_tail"].splitlines())

    if payload.get("evidence_error"):
        lines.append(f"evidence-error: {payload['evidence_error']}")
    lines.append("safety:")
    for key, value in payload.get("safety", {}).items():
        lines.append(f"  {key}: {value}")
    return "\n".join(lines) + "\n"


def _evidence_file_metadata(path: Path, relative_name: str) -> dict[str, Any]:
    content = path.read_bytes()
    return {
        "path": relative_name,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def verify_evidence_bundle(evidence_dir: Path) -> None:
    """Verify the metadata and content hashes of a retained evidence bundle."""

    evidence_dir = evidence_dir.expanduser().resolve(strict=False)
    expected_bundle_names = {
        EVIDENCE_REPORT_FILENAME,
        EVIDENCE_TEXT_FILENAME,
        EVIDENCE_MANIFEST_FILENAME,
    }
    try:
        actual_bundle_names = {path.name for path in evidence_dir.iterdir()}
    except OSError as exc:
        raise ValueError(
            f"could not enumerate evidence bundle: {type(exc).__name__}: {exc}"
        ) from exc
    unexpected_files = sorted(actual_bundle_names - expected_bundle_names)
    if unexpected_files:
        raise ValueError(
            "evidence bundle contains unexpected files: " + ", ".join(unexpected_files)
        )
    for name in expected_bundle_names:
        path = evidence_dir / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"evidence bundle entry is not a regular file: {name!r}")
    manifest_path = evidence_dir / EVIDENCE_MANIFEST_FILENAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read evidence manifest: {type(exc).__name__}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise TypeError("evidence manifest root is not an object")
    if manifest.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise ValueError("evidence manifest schema version is unsupported")

    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise TypeError("evidence manifest files is not a list")
    expected_names = {EVIDENCE_REPORT_FILENAME, EVIDENCE_TEXT_FILENAME}
    actual_names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise TypeError("evidence manifest file entry is not an object")
        relative_name = entry.get("path")
        if not isinstance(relative_name, str) or not relative_name:
            raise ValueError("evidence manifest file path is invalid")
        if relative_name in actual_names:
            raise ValueError(f"evidence manifest repeats file {relative_name!r}")
        actual_names.add(relative_name)
        relative_path = Path(relative_name)
        if (
            relative_path.is_absolute()
            or PureWindowsPath(relative_name).is_absolute()
            or "\\" in relative_name
            or ".." in relative_path.parts
        ):
            raise ValueError(f"evidence manifest file escapes its directory: {relative_name!r}")
        path = (evidence_dir / relative_path).resolve(strict=False)
        try:
            path.relative_to(evidence_dir)
        except ValueError as exc:
            raise ValueError(
                f"evidence manifest file escapes its directory: {relative_name!r}"
            ) from exc
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ValueError(
                f"could not read evidence file {relative_name!r}: {type(exc).__name__}: {exc}"
            ) from exc
        if entry.get("size") != len(content):
            raise ValueError(f"evidence file size mismatch for {relative_name!r}")
        if entry.get("sha256") != hashlib.sha256(content).hexdigest():
            raise ValueError(f"evidence file sha256 mismatch for {relative_name!r}")

    if actual_names != expected_names:
        raise ValueError(
            "evidence manifest must cover exactly gate-report.json and gate-report.txt"
        )
    try:
        report = json.loads((evidence_dir / EVIDENCE_REPORT_FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"could not read retained gate report: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(report, dict):
        raise TypeError("retained gate report root is not an object")
    if report.get("overall") != manifest.get("overall"):
        raise ValueError("evidence manifest overall status does not match gate report")


def write_evidence_bundle(
    evidence_dir: Path,
    payload: dict[str, Any],
) -> dict[str, Path]:
    """Write a small, self-contained report bundle without copying inputs."""

    evidence_dir = evidence_dir.expanduser()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    report_path = evidence_dir / EVIDENCE_REPORT_FILENAME
    text_path = evidence_dir / EVIDENCE_TEXT_FILENAME
    manifest_path = evidence_dir / EVIDENCE_MANIFEST_FILENAME

    report_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    text_path.write_text(render_evidence_text(payload), encoding="utf-8")
    manifest = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "generated_at": payload.get("generated_at"),
        "overall": payload.get("overall"),
        "files": [
            _evidence_file_metadata(report_path, EVIDENCE_REPORT_FILENAME),
            _evidence_file_metadata(text_path, EVIDENCE_TEXT_FILENAME),
        ],
        "safety": payload.get("safety", {}),
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    verify_evidence_bundle(evidence_dir)
    return {
        "report": report_path,
        "text": text_path,
        "manifest": manifest_path,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=project_root_from_script())
    parser.add_argument("--rom-root", type=Path)
    parser.add_argument("--fixture-root", type=Path)
    parser.add_argument(
        "--python", dest="python_executable", type=Path, default=Path(sys.executable)
    )
    parser.add_argument(
        "--cython-python",
        dest="cython_python_executable",
        type=Path,
        help=("interpreter for the Cython runtime when --runtime-mode both; defaults to --python"),
    )
    parser.add_argument(
        "--tier",
        action="append",
        choices=tuple(TIER_EXPRESSIONS),
        help="run only this tier; repeat the option to select multiple tiers",
    )
    parser.add_argument(
        "--unit-only",
        action="store_true",
        help="run the ROM-free unit tier and its fivefold timing regression",
    )
    parser.add_argument(
        "--repeat-timing",
        type=int,
        default=5,
        help="number of timing-tier repetitions (minimum 5; default: 5)",
    )
    parser.add_argument(
        "--matrix-workers",
        type=int,
        default=DEFAULT_MATRIX_WORKERS,
        help=(
            "parallel subprocess workers for strict trade/battle matrix rows "
            f"(default: {DEFAULT_MATRIX_WORKERS})"
        ),
    )
    parser.add_argument(
        "--runtime-mode",
        type=normalize_runtime_mode,
        choices=RUNTIME_MODE_CHOICES,
        default="source",
        help=(
            "PyBoy runtime to require for every selected tier: source uses the "
            "vendored Python modules; cython uses installed extension modules; "
            "both runs every selected tier under each explicit runtime "
            "(dual is accepted as an alias) (default: source)"
        ),
    )
    parser.add_argument(
        "--matrix-timeout-seconds",
        type=float,
        help=(
            "hard aggregate timeout for each strict trade/battle matrix tier; "
            "otherwise it is derived from row timeout and worker count"
        ),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        help="override the per-run timeout for every selected tier",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="report format (default: text)",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        help=(
            "write a sanitized gate-report.json, gate-report.txt, and "
            "evidence-manifest.json to this directory"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.repeat_timing < 5:
        parser.error("--repeat-timing must be at least 5")
    if args.timeout_seconds is not None and args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.matrix_workers <= 0:
        parser.error("--matrix-workers must be positive")
    if args.matrix_timeout_seconds is not None and args.matrix_timeout_seconds <= 0:
        parser.error("--matrix-timeout-seconds must be positive")
    if args.unit_only and args.tier:
        parser.error("--unit-only cannot be combined with --tier")
    if args.cython_python_executable is not None and args.runtime_mode != "both":
        parser.error("--cython-python requires --runtime-mode both")

    project_root = args.repo_root.expanduser().resolve()
    # Do not call ``resolve()`` here: POSIX virtualenv interpreters are often
    # symlinks to the system interpreter, and resolving would silently drop
    # the environment containing pytest/PyBoy.
    python_executable = _python_path_from_argument(args.python_executable, project_root)
    cython_python_executable = (
        _python_path_from_argument(args.cython_python_executable, project_root)
        if args.cython_python_executable is not None
        else None
    )
    rom_root = find_rom_root(project_root, args.rom_root)
    fixture_root = find_fixture_root(project_root, args.fixture_root)
    expected_sha1 = parse_expected_sha1(project_root / "VERSIONS.md")
    assets = inspect_assets(rom_root, fixture_root, expected_sha1)
    required_tests_by_tier, tier_config_error = load_required_test_keys(project_root)
    configuration_problems: list[str] = []
    if tier_config_error:
        configuration_problems.append(tier_config_error)
    required_nodeids_by_tier, nodeid_config_error = load_required_nodeids(project_root)
    if nodeid_config_error:
        configuration_problems.append(nodeid_config_error)

    if args.unit_only:
        selected = ["unit", "timing"]
    elif args.tier:
        selected = list(dict.fromkeys(args.tier))
    else:
        selected = list(DEFAULT_TIERS)
    runtime_results = run_runtime_gates(
        runtime_mode=args.runtime_mode,
        project_root=project_root,
        python_executable=python_executable,
        cython_python_executable=cython_python_executable,
        rom_root=rom_root,
        fixture_root=fixture_root,
        expected_sha1=expected_sha1,
        assets=assets,
        selected=selected,
        required_tests_by_tier=required_tests_by_tier,
        required_nodeids_by_tier=required_nodeids_by_tier,
        configuration_problems=configuration_problems,
        repeat=args.repeat_timing,
        timeout_override=args.timeout_seconds,
        matrix_workers=args.matrix_workers,
        matrix_timeout_override=args.matrix_timeout_seconds,
    )

    dual_runtime = len(runtime_results) > 1
    if not dual_runtime:
        result = runtime_results[0]
        runtime = result.runtime
        collections = result.collections
        fixture_manifest = result.fixture_manifest
        matrix_audit = result.matrix_audit
        tiers = result.tiers
        gate_problems = result.gate_problems
        overall = "PASS" if runtime_gate_passes(result) else "FAIL"
    else:
        runtime = {}
        collections = []
        fixture_manifest = {}
        matrix_audit = {}
        tiers = []
        gate_problems = [
            f"{result.mode}: {problem}"
            for result in runtime_results
            for problem in result.gate_problems
        ]
        overall = "PASS" if runtime_gates_pass(runtime_results) else "FAIL"
    evidence_error = ""
    if args.evidence_dir is not None:
        evidence_dir = args.evidence_dir.expanduser()
        if not evidence_dir.is_absolute():
            evidence_dir = (Path.cwd() / evidence_dir).resolve()
        if dual_runtime:
            evidence_payload = build_dual_evidence_payload(
                project_root=project_root,
                rom_root=rom_root,
                fixture_root=fixture_root,
                assets=assets,
                runtime_results=runtime_results,
                overall=overall,
                requested_mode="both",
            )
        else:
            evidence_payload = build_evidence_payload(
                project_root=project_root,
                rom_root=rom_root,
                fixture_root=fixture_root,
                runtime=runtime,
                assets=assets,
                collections=collections,
                tiers=tiers,
                gate_problems=gate_problems,
                overall=overall,
                fixture_manifest=fixture_manifest,
                matrix_audit=matrix_audit,
            )
        try:
            write_evidence_bundle(evidence_dir, evidence_payload)
        except (OSError, TypeError, ValueError) as exc:
            evidence_error = (
                f"could not write evidence bundle to {evidence_dir}: {type(exc).__name__}: {exc}"
            )
            gate_problems.append(evidence_error)
            overall = "FAIL"

    if args.format == "json":
        if dual_runtime:
            payload = {
                "project_root": str(project_root),
                "rom_root": str(rom_root),
                "fixture_root": str(fixture_root),
                "runtime_mode": "both",
                "runtimes": [_runtime_result_json(result) for result in runtime_results],
                "assets": [asdict(asset) for asset in assets],
                "gate_problems": gate_problems,
                "overall": overall,
            }
        else:
            payload = {
                "project_root": str(project_root),
                "rom_root": str(rom_root),
                "fixture_root": str(fixture_root),
                "runtime": runtime,
                "collections": [asdict(collection) for collection in collections],
                "assets": [asdict(asset) for asset in assets],
                "tiers": [_jsonable_tier(tier) for tier in tiers],
                "gate_problems": gate_problems,
                "overall": overall,
                "fixture_manifest": fixture_manifest,
                "matrix_audit": matrix_audit,
            }
        if evidence_error:
            payload["evidence_error"] = evidence_error
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        if dual_runtime:
            output = render_dual_text(
                project_root=project_root,
                rom_root=rom_root,
                fixture_root=fixture_root,
                assets=assets,
                runtime_results=runtime_results,
                overall=overall,
            )
        else:
            output = render_text(
                project_root=project_root,
                rom_root=rom_root,
                fixture_root=fixture_root,
                runtime=runtime,
                assets=assets,
                collections=collections,
                tiers=tiers,
                gate_problems=gate_problems,
                overall=overall,
                fixture_manifest=fixture_manifest,
                matrix_audit=matrix_audit,
            )
        print(output)
    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
