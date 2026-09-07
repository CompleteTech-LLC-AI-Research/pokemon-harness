#!/usr/bin/env python3
"""Run the Pokémon harness production acceptance gate.

The gate deliberately runs pytest in separate subprocesses.  This keeps ROM
state, emulator globals, and network listeners isolated between tiers and
makes the reported counts independent of pytest's in-process plugin state.

Examples::

    python scripts/production_gate.py
    python scripts/production_gate.py --unit-only
    python scripts/production_gate.py --tier remote --repeat-timing 5

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
import signal
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence


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
    ("red battle cable-club", Path("red/cable_club-battle.state")),
    ("blue battle cable-club", Path("blue/cable_club-battle.state")),
    ("yellow battle cable-club", Path("yellow/cable_club-battle.state")),
)

REQUIRED_TIER_ASSETS = frozenset({"local", "remote", "trade", "battle"})
OPTIONAL_TIERS = frozenset()

TIER_EXPRESSIONS: dict[str, str] = {
    "unit": "unit",
    "local": "real_rom and not remote_link and not acceptance",
    "remote": "real_rom and remote_link and not acceptance and not battle_diagnostic",
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

_SHA1_RE = re.compile(r"^\|\s*SHA-?1\s*\|\s*`([0-9A-Fa-f]{40})`\s*\|")
_PATH_RE = re.compile(r"^\|\s*Path\s*\|\s*`([^`]+)`\s*\|")


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
    def from_report(cls, payload: dict[str, Any]) -> "Counts":
        raw = payload.get("counts", {})
        return cls(**{field: int(raw.get(field, 0)) for field in cls.__dataclass_fields__})


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


def project_root_from_script() -> Path:
    return Path(__file__).resolve().parents[1]


def _path_from_env(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else None


def find_rom_root(project_root: Path, explicit: Path | None = None) -> Path:
    """Resolve ROM storage without assuming the worktree owns ``rom/``."""

    if explicit is not None:
        return explicit.expanduser()
    configured = _path_from_env("POKERED_ROM_ROOT")
    if configured is not None:
        return configured

    # If only a primary ROM path is configured, use its nearest ``rom``
    # ancestor when possible.  This keeps the report useful for a worktree
    # whose ROMs live in a sibling checkout.
    configured_rom = _path_from_env("POKERED_ROM_PATH")
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
        return explicit.expanduser()
    configured = _path_from_env("POKERED_FIXTURE_ROOT")
    if configured is not None:
        return configured
    for parent in (project_root, *project_root.parents):
        candidate = parent / "tests" / "fixtures" / "link"
        if candidate.is_dir():
            return candidate
    return project_root / "tests" / "fixtures" / "link"


def parse_expected_sha1(versions_file: Path) -> dict[Path, str]:
    """Extract ROM SHA-1 pins from the per-ROM tables in VERSIONS.md."""

    expected: dict[Path, str] = {}
    pending_sha: str | None = None
    if not versions_file.is_file():
        return expected

    for line in versions_file.read_text(encoding="utf-8").splitlines():
        sha_match = _SHA1_RE.match(line)
        if sha_match:
            pending_sha = sha_match.group(1).lower()
            continue
        path_match = _PATH_RE.match(line)
        if path_match and pending_sha:
            relative = Path(path_match.group(1))
            if relative.parts and relative.parts[0] == "rom":
                relative = Path(*relative.parts[1:])
            expected[relative] = pending_sha
            pending_sha = None
    return expected


def parse_expected_symbol_sha1(versions_file: Path) -> dict[Path, str]:
    """Extract documented symbol-file SHA-1 pins from ``VERSIONS.md``."""

    expected: dict[Path, str] = {}
    current_symbol: Path | None = None
    for line in versions_file.read_text(encoding="utf-8").splitlines() if versions_file.is_file() else ():
        if line.startswith("##"):
            current_symbol = None
            continue
        symbols_match = re.match(r"^\|\s*Symbols\s*\|\s*`([^`]+)`\s*\|", line)
        if symbols_match:
            relative = Path(symbols_match.group(1))
            if relative.parts and relative.parts[0] == "rom":
                relative = Path(*relative.parts[1:])
            current_symbol = relative
            continue
        sha_match = re.match(
            r"^\|\s*Symbol\s+SHA-?1\s*\|\s*`([0-9A-Fa-f]{40})`\s*\|",
            line,
        )
        if sha_match and current_symbol is not None:
            expected[current_symbol] = sha_match.group(1).lower()
            current_symbol = None
    return expected


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
    expected_symbol_sha1: dict[Path, str] | None = None,
) -> list[AssetRecord]:
    records: list[AssetRecord] = []

    for label, relative in KNOWN_ROM_FILES:
        path = rom_root / relative
        expected = expected_sha1.get(relative)
        if not path.is_file():
            records.append(
                AssetRecord(label, "rom", str(path), "missing", expected_sha1=expected)
            )
            continue
        actual = sha1_of_file(path)
        status = "ok" if expected is None or actual == expected else "sha1-mismatch"
        records.append(
            AssetRecord(
                label,
                "rom",
                str(path),
                status,
                size=path.stat().st_size,
                expected_sha1=expected,
                actual_sha1=actual,
            )
        )

    for label, relative in KNOWN_SYMBOL_FILES:
        path = rom_root / relative
        expected = (
            expected_symbol_sha1.get(relative)
            if expected_symbol_sha1 is not None
            else None
        )
        actual = sha1_of_file(path) if path.is_file() else None
        if not path.is_file():
            status = "missing"
        elif expected_symbol_sha1 is not None and expected is None:
            status = "missing-pin"
        elif expected is not None and actual != expected:
            status = "sha1-mismatch"
        else:
            status = "ok"
        records.append(
            AssetRecord(
                label,
                "symbol",
                str(path),
                status,
                size=path.stat().st_size if path.is_file() else None,
                expected_sha1=expected,
                actual_sha1=actual,
            )
        )

    for label, relative in REQUIRED_FIXTURES:
        path = fixture_root / relative
        records.append(
            AssetRecord(
                label,
                "fixture",
                str(path),
                "ok" if path.is_file() else "missing",
                size=path.stat().st_size if path.is_file() else None,
            )
        )
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
    runtime_mode: str = "source",
) -> dict[str, str]:
    """Return the exact environment inherited by every pytest subprocess."""

    if runtime_mode not in {"source", "cython"}:
        raise ValueError(f"unsupported runtime mode: {runtime_mode!r}")

    environment = {key: value for key, value in os.environ.items()}
    environment["POKERED_ROM_ROOT"] = str(rom_root)
    environment["POKERED_FIXTURE_ROOT"] = str(fixture_root)
    environment["PYTHONUNBUFFERED"] = "1"

    # Ensure the subprocess tests import this checkout, not an editable
    # install from a different worktree.  Preserve user-provided entries.
    # Source mode must import the pinned vendored tree. Cython mode must not
    # prepend that tree, otherwise it silently shadows the native extension
    # the caller selected in its interpreter.
    source_entries = [str(project_root / "src"), str(project_root)]
    if runtime_mode == "source":
        source_entries.insert(0, str(project_root / "vendor" / "pyboy-src"))
        environment["PYBOY_NO_CYTHON"] = "1"
    else:
        environment.pop("PYBOY_NO_CYTHON", None)
    old_pythonpath = environment.get("PYTHONPATH")
    if old_pythonpath:
        source_entries.append(old_pythonpath)
    environment["PYTHONPATH"] = os.pathsep.join(source_entries)

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
        red_expected = expected_sha1.get(Path("red/pokemon-red.gb"))
        if red_expected:
            environment["POKERED_ROM_SHA1"] = red_expected
    return environment


def probe_runtime(
    python_executable: Path,
    project_root: Path,
    environment: dict[str, str],
) -> dict[str, Any]:
    """Probe the interpreter that will run pytest, not the parent shell."""

    probe = r'''
import importlib.machinery
import importlib.metadata
import importlib.util
import json
import platform
import sys
from pathlib import Path

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
    "pyboy_module": spec_path("pyboy"),
    "pyboy_kind": module_kind(spec_path("pyboy")),
    "serial_module": spec_path("pyboy.core.serial"),
    "harness_module": spec_path("pokered_harness"),
    "serial_core": None,
    "serial_contract": "unavailable",
    "pyboy_serial_kind": module_kind(spec_path("pyboy.core.serial")),
}
for package, key in (("pytest", "pytest_version"), ("pyboy", "pyboy_version")):
    try:
        result[key] = importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        pass
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
'''
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


def _reason_counter(payload: dict[str, Any]) -> dict[str, int]:
    reasons = Counter()
    for record in payload.get("tests", []):
        if record.get("outcome") == "skipped" or record.get("was_xfail"):
            reason = str(record.get("reason") or "(no reason reported)").strip()
            reasons[reason] += 1
    return dict(sorted(reasons.items()))


def load_gate_report(path: Path) -> tuple[Counts, dict[str, int], str]:
    """Load the stable JSON emitted by ``tests._gate_report``."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return Counts(errors=1), {}, f"could not read pytest report: {type(exc).__name__}: {exc}"
    return Counts.from_report(payload), _reason_counter(payload), ""


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5.0)
            return
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
    try:
        process.kill()
    except OSError:
        pass


def run_pytest_once(
    *,
    project_root: Path,
    python_executable: Path,
    environment: dict[str, str],
    expression: str,
    timeout_seconds: float,
    report_path: Path,
) -> tuple[int, Counts, dict[str, int], str, list[str]]:
    command = [
        str(python_executable),
        "-m",
        "pytest",
        "tests",
        "-m",
        expression,
        "-p",
        "tests._gate_report",
        "-rA",
        "--maxfail=0",
    ]
    child_environment = dict(environment)
    child_environment["POKERED_GATE_REPORT"] = str(report_path)
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=project_root,
        env=child_environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=os.name == "posix",
    )
    try:
        output, _ = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _terminate_process(process)
        output, _ = process.communicate()
        output = (
            f"pytest timed out after {timeout_seconds:.1f}s\n"
            f"{output}"
        )
        returncode = 124
    else:
        returncode = int(process.returncode or 0)

    counts, reasons, report_error = load_gate_report(report_path)
    if report_error:
        output = f"{output}\n{report_error}"
    # Keep the command and enough output to diagnose a failed gate without
    # flooding the orchestrator with every emulator trace.
    del started
    return returncode, counts, reasons, output[-8000:], command


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
) -> TierResult:
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
    started = time.monotonic()
    for iteration in range(1, repeat + 1):
        report_path = report_directory / f"{name}-{iteration}.json"
        returncode, counts, reasons, output, command = run_pytest_once(
            project_root=project_root,
            python_executable=python_executable,
            environment=environment,
            expression=TIER_EXPRESSIONS[name],
            timeout_seconds=timeout,
            report_path=report_path,
        )
        aggregate.total += counts.total
        aggregate.passed += counts.passed
        aggregate.failed += counts.failed
        aggregate.skipped += counts.skipped
        aggregate.xfailed += counts.xfailed
        aggregate.xpassed += counts.xpassed
        aggregate.errors += counts.errors
        aggregate_reasons.update(reasons)
        returncodes.append(returncode)
        if output.strip():
            output_tail = output

    duration = time.monotonic() - started
    unexpected = aggregate.failed + aggregate.errors + aggregate.xfailed + aggregate.xpassed
    if any(code not in (0, 5) for code in returncodes):
        status = "FAIL"
    elif unexpected:
        status = "FAIL"
    elif aggregate.total == 0:
        status = "FAIL"
        output_tail = f"pytest selected no tests for marker expression {TIER_EXPRESSIONS[name]!r}"
    elif required and aggregate.skipped:
        status = "FAIL"
        output_tail = (
            f"required tier produced {aggregate.skipped} skip(s); "
            "missing/unsupported coverage is not accepted in the production gate\n"
            + output_tail
        )
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
    )


def _optional_preflight_reason(name: str, records: list[AssetRecord]) -> str | None:
    roms = [record for record in records if record.kind == "rom"]
    fixtures = [record for record in records if record.kind == "fixture"]
    if not any(record.status == "ok" for record in roms):
        return f"optional {name} acceptance unavailable: no pinned ROM assets found"
    if not any(record.status == "ok" for record in fixtures):
        return f"optional {name} acceptance unavailable: no derived link fixtures found"
    return None


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
    tiers: list[TierResult],
    overall: str,
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
        "pyboy_kind",
        "pyboy_serial_kind",
        "pyboy_module",
        "serial_module",
        "serial_contract",
        "harness_module",
    ):
        if key in runtime:
            lines.append(f"  {key}={runtime[key]}")
    if runtime.get("probe_error"):
        lines.append(f"  probe_error={runtime['probe_error']}")

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
        lines.append(f"  {asset.status.upper():13} {asset.kind:7} {asset.label}: {asset.path}{detail}")

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
        if tier.status in {"FAIL", "BLOCKED"} and tier.output_tail:
            lines.append("    output tail:")
            lines.extend(f"      {line}" for line in tier.output_tail.splitlines()[-60:])
    lines.append(f"overall: {overall}")
    return "\n".join(lines)


def _jsonable_tier(tier: TierResult) -> dict[str, Any]:
    data = asdict(tier)
    data["counts"] = asdict(tier.counts)
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=project_root_from_script())
    parser.add_argument("--rom-root", type=Path)
    parser.add_argument("--fixture-root", type=Path)
    parser.add_argument("--python", dest="python_executable", type=Path, default=Path(sys.executable))
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
        "--timeout-seconds",
        type=float,
        help="override the per-run timeout for every selected tier",
    )
    parser.add_argument(
        "--runtime-mode",
        choices=("source", "cython"),
        default="source",
        help="select the bundled Python source runtime or installed Cython runtime",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="report format (default: text)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.repeat_timing < 5:
        parser.error("--repeat-timing must be at least 5")
    if args.timeout_seconds is not None and args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.unit_only and args.tier:
        parser.error("--unit-only cannot be combined with --tier")

    project_root = args.repo_root.expanduser().resolve()
    # Do not call ``resolve()`` here: POSIX virtualenv interpreters are often
    # symlinks to the system interpreter, and resolving would silently drop
    # the environment containing pytest/PyBoy.  Only resolve relative paths.
    python_executable = args.python_executable.expanduser()
    if not python_executable.is_absolute():
        python_executable = (Path.cwd() / python_executable).resolve()
    rom_root = find_rom_root(project_root, args.rom_root)
    fixture_root = find_fixture_root(project_root, args.fixture_root)
    expected_sha1 = parse_expected_sha1(project_root / "VERSIONS.md")
    expected_symbol_sha1 = parse_expected_symbol_sha1(project_root / "VERSIONS.md")
    assets = inspect_assets(
        rom_root, fixture_root, expected_sha1, expected_symbol_sha1
    )
    environment = build_test_environment(
        project_root, rom_root, fixture_root, expected_sha1, args.runtime_mode
    )
    # Subprocess acceptance tests must use the exact interpreter whose runtime
    # contract was probed above, not a stale auxiliary virtualenv discovered
    # from the worktree.
    environment["POKERED_PYTHON"] = str(python_executable)
    runtime = probe_runtime(python_executable, project_root, environment)
    runtime["requested_runtime_mode"] = args.runtime_mode
    expected_serial_kind = (
        "python-source" if args.runtime_mode == "source" else "cython/native-extension"
    )
    actual_serial_kind = runtime.get("pyboy_serial_kind")
    if actual_serial_kind != expected_serial_kind:
        runtime["probe_error"] = (
            f"requested runtime mode {args.runtime_mode!r} requires "
            f"pyboy.core.serial={expected_serial_kind}, got {actual_serial_kind}"
        )

    if args.unit_only:
        selected = ["unit", "timing"]
    elif args.tier:
        selected = list(dict.fromkeys(args.tier))
    else:
        selected = list(DEFAULT_TIERS)

    required_problems = required_asset_problems(assets)
    tiers: list[TierResult] = []
    with tempfile.TemporaryDirectory(prefix="pokered-gate-") as temp_directory:
        report_directory = Path(temp_directory)
        for name in selected:
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
                    repeat=args.repeat_timing,
                    timeout_override=args.timeout_seconds,
                    report_directory=report_directory,
                )
            )

    overall = (
        "PASS"
        if not runtime.get("probe_error")
        and all(tier.status in {"PASS", "SKIP"} for tier in tiers)
        else "FAIL"
    )
    if args.format == "json":
        payload = {
            "project_root": str(project_root),
            "rom_root": str(rom_root),
            "fixture_root": str(fixture_root),
            "runtime": runtime,
            "assets": [asdict(asset) for asset in assets],
            "tiers": [_jsonable_tier(tier) for tier in tiers],
            "overall": overall,
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            render_text(
                project_root=project_root,
                rom_root=rom_root,
                fixture_root=fixture_root,
                runtime=runtime,
                assets=assets,
                tiers=tiers,
                overall=overall,
            )
        )
    return 0 if overall == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
