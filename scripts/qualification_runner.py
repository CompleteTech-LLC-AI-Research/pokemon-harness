#!/usr/bin/env python3
"""Verify a declared qualification-runner allocation and its prerequisites.

The production gate runs emulator pairs whose deadlines are sensitive to CPU
scheduling.  This tool verifies, without stopping or reprioritizing any other
process, whether the current host matches an operator-declared allocation.

Two modes are supported:

* ``--report`` prints the observed host facts (CPU affinity, cgroup quota and
  weight, throttling counters, memory, disk, shared memory, load).
* ``--check`` compares those facts against an explicit declaration and also
  runs the runtime/asset prerequisites.  It exits ``0`` only when every check
  is ``ok``; a missing declaration is reported as ``blocked``, never as a pass.

The declaration is operator-owned and deliberately external to the repository.
Use ``--declaration`` or ``POKERED_QUALIFICATION_DECLARATION``.  No ROM bytes,
credentials, or machine-local paths are written into Git or the report; paths
are reported only as observed local values.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
_DECLARATION_ENV = "POKERED_QUALIFICATION_DECLARATION"
_RESERVATION_MECHANISMS = ("dedicated-host", "cgroup-quota", "cpuset-affinity")
_STATUS_ORDER = ("fail", "blocked", "unsupported", "ok")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def parse_cpuset(value: str | list[int]) -> list[int]:
    """Parse a kernel cpuset string like ``0-3,8`` or a list of ints."""

    if isinstance(value, list):
        return sorted({int(item) for item in value})
    cpus: set[int] = set()
    for chunk in str(value).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start, end = chunk.split("-", 1)
            low, high = int(start), int(end)
            if high < low:
                raise ValueError(f"invalid cpuset range: {chunk!r}")
            cpus.update(range(low, high + 1))
        else:
            cpus.add(int(chunk))
    return sorted(cpus)


def _parse_cpu_max(text: str) -> float | None:
    parts = text.split()
    if not parts or parts[0] == "max" or len(parts) < 2:
        return None
    period = int(parts[1])
    if period <= 0:
        return None
    return int(parts[0]) / period


def _cgroup_relative_path() -> str | None:
    raw = _read_text(Path("/proc/self/cgroup"))
    if raw is None:
        return None
    for line in raw.splitlines():
        hierarchy, controllers, path = (line.split(":", 2) + ["", "", ""])[:3]
        if hierarchy == "0" and not controllers:
            return path or "/"
    return None


def _iter_cgroup_paths(base: Path, relative: str | None) -> list[Path]:
    if relative is None:
        return [base]
    paths: list[Path] = []
    current = base / relative.lstrip("/")
    while True:
        paths.append(current)
        if current == base or current.parent == current:
            break
        current = current.parent
    return paths


def _read_cgroup_facts() -> dict[str, Any]:
    root = Path("/sys/fs/cgroup")
    version = "unavailable"
    quota_cores: float | None = None
    weight: int | None = None
    throttled: dict[str, int] | None = None
    relative = _cgroup_relative_path()

    if (root / "cpu.max").exists():
        version = "v2"
        quotas = [
            quota
            for quota in (
                _parse_cpu_max(_read_text(path / "cpu.max") or "")
                for path in _iter_cgroup_paths(root, relative)
            )
            if quota is not None
        ]
        quota_cores = min(quotas) if quotas else None
        for path in _iter_cgroup_paths(root, relative):
            raw_weight = _read_text(path / "cpu.weight")
            if raw_weight is not None and raw_weight.isdigit():
                weight = int(raw_weight)
                break
    elif (root / "cpu" / "cpu.cfs_quota_us").exists():
        version = "v1"
        v1_root = root / "cpu"
        quotas = []
        for path in _iter_cgroup_paths(v1_root, relative):
            quota_raw = _read_text(path / "cpu.cfs_quota_us")
            period_raw = _read_text(path / "cpu.cfs_period_us")
            if not quota_raw or not period_raw:
                continue
            quota_us = int(quota_raw)
            period_us = int(period_raw)
            if quota_us >= 0 and period_us > 0:
                quotas.append(quota_us / period_us)
        quota_cores = min(quotas) if quotas else None
        for path in _iter_cgroup_paths(v1_root, relative):
            shares_raw = _read_text(path / "cpu.shares")
            if shares_raw and shares_raw.isdigit():
                weight = int(shares_raw)
                break

    stat_base = root if version == "v2" else root / "cpu"
    for path in _iter_cgroup_paths(stat_base, relative):
        stat_raw = _read_text(path / "cpu.stat")
        if not stat_raw:
            continue
        throttled = {}
        for line in stat_raw.splitlines():
            key, _, value = line.partition(" ")
            if value.strip().isdigit():
                throttled[key] = int(value.strip())
        break
    return {
        "cgroup_version": version,
        "cpu_quota_cores": quota_cores,
        "cpu_weight": weight,
        "cpu_throttled": throttled,
    }


def _read_memory_facts() -> tuple[int | None, int | None]:
    raw = _read_text(Path("/proc/meminfo"))
    if raw is None:
        return None, None
    total = available = None
    for line in raw.splitlines():
        key, _, rest = line.partition(":")
        value = rest.strip().split()
        if not value:
            continue
        kib = int(value[0])
        if key == "MemTotal":
            total = kib * 1024
        elif key == "MemAvailable":
            available = kib * 1024
    return total, available


def _read_psi_cpu() -> float | None:
    raw = _read_text(Path("/proc/pressure/cpu"))
    if raw is None:
        return None
    for line in raw.splitlines():
        if not line.startswith("some"):
            continue
        for token in line.split():
            if token.startswith("avg300="):
                return float(token.split("=", 1)[1])
    return None


@dataclass
class RunnerFacts:
    declaration_version: int = SCHEMA_VERSION
    platform: str = ""
    kernel: str = ""
    logical_cpus: int = 0
    affinity_cpus: list[int] = field(default_factory=list)
    affinity_count: int = 0
    affinity_supported: bool = True
    cgroup_version: str = "unavailable"
    cpu_quota_cores: float | None = None
    cpu_weight: int | None = None
    cpu_throttled: dict[str, int] | None = None
    memory_total_bytes: int | None = None
    memory_available_bytes: int | None = None
    load_average: list[float] | None = None
    psi_cpu_some_avg300: float | None = None
    repo_disk_free_bytes: int | None = None
    temp_disk_free_bytes: int | None = None
    shm_path: str = "/dev/shm"
    shm_size_bytes: int | None = None
    shm_writable: bool = False
    unsupported: list[str] = field(default_factory=list)


def collect_facts(repo_root: Path, temp_root: Path | None = None) -> RunnerFacts:
    """Collect read-only host facts.  Never signals or reprioritizes a process."""

    facts = RunnerFacts()
    system = platform.system()
    facts.platform = f"{system} {platform.release()}"
    facts.kernel = platform.version()
    facts.logical_cpus = os.cpu_count() or 0

    if hasattr(os, "sched_getaffinity"):
        try:
            facts.affinity_cpus = sorted(os.sched_getaffinity(0))
            facts.affinity_count = len(facts.affinity_cpus)
        except OSError:
            facts.affinity_supported = False
            facts.unsupported.append("affinity")
    else:
        facts.affinity_supported = False
        facts.unsupported.append("affinity")

    facts.memory_total_bytes, facts.memory_available_bytes = _read_memory_facts()
    try:
        facts.load_average = list(os.getloadavg())
    except OSError:
        facts.load_average = None

    try:
        facts.repo_disk_free_bytes = shutil.disk_usage(repo_root).free
    except OSError:
        facts.repo_disk_free_bytes = None
    temp = temp_root or Path(os.environ.get("TMPDIR") or "/tmp")
    try:
        facts.temp_disk_free_bytes = shutil.disk_usage(temp).free
    except OSError:
        facts.temp_disk_free_bytes = None

    shm = Path(facts.shm_path)
    if shm.is_dir():
        try:
            stats = os.statvfs(shm)
            facts.shm_size_bytes = stats.f_bsize * stats.f_blocks
            probe = shm / f".qualification-runner-probe-{os.getpid()}"
            try:
                probe.write_bytes(b"ok")
            finally:
                probe.unlink(missing_ok=True)
            facts.shm_writable = True
        except OSError:
            facts.shm_writable = False
    else:
        facts.unsupported.append("shm-missing")

    if system != "Linux":
        facts.unsupported.extend(["cgroup", "pressure", "procfs"])
        return facts

    cgroup = _read_cgroup_facts()
    facts.cgroup_version = cgroup["cgroup_version"]
    facts.cpu_quota_cores = cgroup["cpu_quota_cores"]
    facts.cpu_weight = cgroup["cpu_weight"]
    facts.cpu_throttled = cgroup["cpu_throttled"]
    facts.psi_cpu_some_avg300 = _read_psi_cpu()
    return facts


@dataclass
class CheckResult:
    name: str
    status: str
    required: Any
    observed: Any
    detail: str


def _result(name: str, status: str, required: Any, observed: Any, detail: str) -> CheckResult:
    return CheckResult(
        name=name, status=status, required=required, observed=observed, detail=detail
    )


def validate_declaration(declaration: dict[str, Any]) -> list[CheckResult]:
    """Validate the declaration structure and required fields."""

    required_fields = (
        "declaration_version",
        "runner_id",
        "reservation_mechanism",
        "logical_cpus",
        "affinity_cpus",
        "memory_bytes",
        "disk_free_bytes_min",
        "shm_bytes_min",
        "interpreters",
        "assets",
    )
    results: list[CheckResult] = []
    missing = [name for name in required_fields if name not in declaration]
    results.append(
        _result(
            "declaration-fields",
            "ok" if not missing else "blocked",
            list(required_fields),
            sorted(declaration),
            "declaration is missing required fields" if missing else "declaration fields present",
        )
    )
    if declaration.get("declaration_version") != SCHEMA_VERSION:
        results.append(
            _result(
                "declaration-version",
                "unsupported",
                SCHEMA_VERSION,
                declaration.get("declaration_version"),
                f"only schema version {SCHEMA_VERSION} is supported",
            )
        )
    mechanism = declaration.get("reservation_mechanism")
    if mechanism not in _RESERVATION_MECHANISMS:
        results.append(
            _result(
                "reservation-mechanism",
                "fail",
                list(_RESERVATION_MECHANISMS),
                mechanism,
                "affinity or a quota alone is not a reservation; declare a mechanism",
            )
        )
    interpreters = declaration.get("interpreters")
    if (
        not isinstance(interpreters, dict)
        or not interpreters.get("source")
        or not interpreters.get("native")
    ):
        results.append(
            _result(
                "interpreters",
                "fail",
                {"source": "path", "native": "path"},
                interpreters,
                "both a source and a native interpreter are required",
            )
        )
    assets = declaration.get("assets")
    if not isinstance(assets, dict) or not assets.get("rom_root") or not assets.get("fixture_root"):
        results.append(
            _result(
                "assets",
                "fail",
                {"rom_root": "path", "fixture_root": "path"},
                assets,
                "external rom_root and fixture_root are required",
            )
        )

    logical = declaration.get("logical_cpus")
    if isinstance(logical, bool) or not isinstance(logical, int) or logical <= 0:
        results.append(
            _result(
                "logical-cpus-value",
                "fail",
                "positive int",
                logical,
                "logical_cpus must be a positive integer",
            )
        )
    try:
        parse_cpuset(declaration.get("affinity_cpus"))
    except (TypeError, ValueError):
        results.append(
            _result(
                "affinity-value",
                "fail",
                "cpuset string or list[int]",
                declaration.get("affinity_cpus"),
                "affinity_cpus is malformed",
            )
        )
    for name in ("memory_bytes", "disk_free_bytes_min", "shm_bytes_min"):
        value = declaration.get(name)
        if name in declaration and (
            isinstance(value, bool) or not isinstance(value, int) or value < 0
        ):
            results.append(
                _result(
                    f"{name}-value",
                    "fail",
                    "non-negative int",
                    value,
                    f"{name} must be a non-negative integer",
                )
            )
    quota = declaration.get("cpu_quota_cores")
    if quota is not None and (
        isinstance(quota, bool) or not isinstance(quota, (int, float)) or quota <= 0
    ):
        results.append(
            _result(
                "cpu-quota-value",
                "fail",
                "positive number or null",
                quota,
                "cpu_quota_cores must be positive",
            )
        )
    weight = declaration.get("cpu_weight")
    if weight is not None and (
        isinstance(weight, bool) or not isinstance(weight, int) or weight <= 0
    ):
        results.append(
            _result(
                "cpu-weight-value",
                "fail",
                "positive int",
                weight,
                "cpu_weight must be a positive integer",
            )
        )
    return results


def evaluate_resources(declaration: dict[str, Any], facts: RunnerFacts) -> list[CheckResult]:
    """Compare declared allocation requirements against observed host facts."""

    mechanism = declaration.get("reservation_mechanism")
    results: list[CheckResult] = []

    declared_cpus = int(declaration.get("logical_cpus") or 0)
    observed_cpus = facts.affinity_count or facts.logical_cpus
    results.append(
        _result(
            "logical-cpus",
            "ok" if observed_cpus >= declared_cpus > 0 else "fail",
            declared_cpus,
            observed_cpus,
            "effective CPUs available to this process",
        )
    )

    if not facts.affinity_supported:
        results.append(
            _result(
                "affinity",
                "unsupported",
                declaration.get("affinity_cpus"),
                None,
                "affinity is unavailable here",
            )
        )
    else:
        try:
            declared_affinity = parse_cpuset(declaration.get("affinity_cpus"))
        except (TypeError, ValueError):
            declared_affinity = []
        usable = set(declared_affinity).issubset(set(facts.affinity_cpus))
        bounded = bool(declared_affinity) and len(declared_affinity) <= len(facts.affinity_cpus)
        results.append(
            _result(
                "affinity",
                "ok" if usable and bounded else "fail",
                declared_affinity,
                facts.affinity_cpus,
                "declared affinity must be usable by this process and no larger than the allocation",
            )
        )

    declared_quota = declaration.get("cpu_quota_cores")
    if mechanism == "cgroup-quota":
        if declared_quota is None:
            results.append(
                _result(
                    "cpu-quota",
                    "fail",
                    "required for cgroup-quota",
                    None,
                    "a cgroup-quota reservation must declare cpu_quota_cores",
                )
            )
        elif facts.cpu_quota_cores is None:
            results.append(
                _result("cpu-quota", "fail", declared_quota, None, "no cgroup CPU quota in effect")
            )
        else:
            results.append(
                _result(
                    "cpu-quota",
                    "ok" if facts.cpu_quota_cores >= declared_quota else "fail",
                    declared_quota,
                    facts.cpu_quota_cores,
                    "effective cgroup CPU quota in cores",
                )
            )
    elif declared_quota is not None:
        if facts.cpu_quota_cores is None:
            results.append(
                _result(
                    "cpu-quota",
                    "fail",
                    declared_quota,
                    None,
                    "a quota was declared but none is in effect",
                )
            )
        else:
            results.append(
                _result(
                    "cpu-quota",
                    "ok" if facts.cpu_quota_cores >= declared_quota else "fail",
                    declared_quota,
                    facts.cpu_quota_cores,
                    "effective cgroup CPU quota in cores",
                )
            )
    else:
        results.append(
            _result(
                "cpu-quota",
                "skipped",
                None,
                facts.cpu_quota_cores,
                "not applicable to this reservation mechanism",
            )
        )

    declared_weight = declaration.get("cpu_weight")
    if declared_weight is None:
        results.append(
            _result("cpu-weight", "skipped", None, facts.cpu_weight, "no CPU weight declared")
        )
    elif facts.cpu_weight is None:
        results.append(
            _result("cpu-weight", "unsupported", declared_weight, None, "no CPU weight visible")
        )
    else:
        results.append(
            _result(
                "cpu-weight",
                "ok" if facts.cpu_weight >= declared_weight else "fail",
                declared_weight,
                facts.cpu_weight,
                "CPU weight is a share, not a reservation",
            )
        )

    declared_memory = int(declaration.get("memory_bytes") or 0)
    if declared_memory <= 0:
        results.append(
            _result(
                "memory",
                "skipped",
                declared_memory,
                facts.memory_total_bytes,
                "no minimum declared",
            )
        )
    else:
        observed_memory = facts.memory_total_bytes
        status = (
            "unsupported"
            if observed_memory is None
            else ("ok" if observed_memory >= declared_memory else "fail")
        )
        results.append(
            _result("memory", status, declared_memory, observed_memory, "total memory bytes")
        )

    declared_disk = int(declaration.get("disk_free_bytes_min") or 0)
    for name, observed in (
        ("disk-free-repo", facts.repo_disk_free_bytes),
        ("disk-free-temp", facts.temp_disk_free_bytes),
    ):
        if declared_disk <= 0:
            results.append(_result(name, "skipped", declared_disk, observed, "no minimum declared"))
            continue
        status = (
            "unsupported" if observed is None else ("ok" if observed >= declared_disk else "fail")
        )
        results.append(_result(name, status, declared_disk, observed, "free disk bytes"))

    declared_shm = int(declaration.get("shm_bytes_min") or 0)
    if declared_shm <= 0:
        results.append(
            _result("shm", "skipped", declared_shm, facts.shm_size_bytes, "no minimum declared")
        )
    elif not facts.shm_writable:
        results.append(
            _result(
                "shm", "fail", declared_shm, facts.shm_size_bytes, "shared memory is not writable"
            )
        )
    else:
        status = (
            "unsupported"
            if facts.shm_size_bytes is None
            else ("ok" if facts.shm_size_bytes >= declared_shm else "fail")
        )
        results.append(
            _result(
                "shm", status, declared_shm, facts.shm_size_bytes, "writable shared memory bytes"
            )
        )

    return results


def run_command(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)


def prerequisite_checks(
    declaration: dict[str, Any],
    repo_root: Path,
    runner: Callable[[list[str], Path], subprocess.CompletedProcess[str]] | None = None,
) -> list[CheckResult]:
    """Verify interpreters and pinned assets using the repository validators."""

    if runner is None:
        runner = run_command
    results: list[CheckResult] = []
    interpreters = declaration.get("interpreters") or {}
    for mode in ("source", "native"):
        python = interpreters.get(mode)
        if not python:
            results.append(
                _result(f"interpreter-{mode}", "fail", "path", None, "missing interpreter path")
            )
            continue
        if not Path(python).exists():
            results.append(
                _result(f"interpreter-{mode}", "fail", python, None, "interpreter not found")
            )
            continue
        bootstrap_mode = "cython" if mode == "native" else "source"
        proc = runner(
            [str(python), "scripts/bootstrap_pyboy.py", "--mode", bootstrap_mode, "--check"],
            repo_root,
        )
        results.append(
            _result(
                f"interpreter-{mode}",
                "ok" if proc.returncode == 0 else "fail",
                f"bootstrap --mode {bootstrap_mode} --check",
                proc.returncode,
                (proc.stdout + proc.stderr).strip()[-300:],
            )
        )

    assets = declaration.get("assets") or {}
    rom_root = assets.get("rom_root")
    if rom_root and not Path(rom_root).is_dir():
        results.append(
            _result("assets-rom-root", "fail", "directory", rom_root, "rom_root is not a directory")
        )

    manifest = repo_root / "release-evidence" / "fixture-manifest.json"
    schema = runner(
        [
            sys.executable,
            "scripts/validate_fixture_manifest.py",
            "--manifest",
            str(manifest),
            "--schema-only",
        ],
        repo_root,
    )
    results.append(
        _result(
            "fixture-manifest-schema",
            "ok" if schema.returncode == 0 else "fail",
            "schema-only",
            schema.returncode,
            (schema.stdout + schema.stderr).strip()[-300:],
        )
    )
    fixture_root = assets.get("fixture_root")
    if fixture_root:
        validate = runner(
            [
                sys.executable,
                "scripts/validate_fixture_manifest.py",
                "--manifest",
                str(manifest),
                "--fixture-root",
                str(fixture_root),
            ],
            repo_root,
        )
        results.append(
            _result(
                "fixture-manifest-bytes",
                "ok" if validate.returncode == 0 else "fail",
                "byte-validated",
                validate.returncode,
                (validate.stdout + validate.stderr).strip()[-300:],
            )
        )
    return results


def overall_status(results: list[CheckResult]) -> str:
    active = [item for item in results if item.status != "skipped"]
    if not active:
        return "blocked"
    for status in ("fail", "blocked", "unsupported"):
        if any(item.status == status for item in active):
            return status
    if all(item.status == "ok" for item in active):
        return "ok"
    return "blocked"


def load_declaration(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"declaration not found: {path}"
    except json.JSONDecodeError as exc:
        return None, f"declaration is not valid JSON: {exc}"
    if not isinstance(document, dict):
        return None, "declaration must be a JSON object"
    return document, None


def render_text(payload: dict[str, Any]) -> str:
    lines = [f"qualification runner: {payload['overall']}", f"mode: {payload['mode']}"]
    if payload.get("declaration"):
        lines.append(f"declaration: {payload['declaration']}")
    for item in payload["checks"]:
        lines.append(
            f"  {item['status']:>11}  {item['name']}: required={item['required']!r} "
            f"observed={item['observed']!r} :: {item['detail']}"
        )
    facts = payload.get("facts")
    if facts:
        lines.append("facts:")
        for key in (
            "platform",
            "logical_cpus",
            "affinity_cpus",
            "cgroup_version",
            "cpu_quota_cores",
            "cpu_weight",
            "memory_total_bytes",
            "memory_available_bytes",
            "load_average",
            "psi_cpu_some_avg300",
            "repo_disk_free_bytes",
            "temp_disk_free_bytes",
            "shm_size_bytes",
            "shm_writable",
            "unsupported",
        ):
            lines.append(f"  {key}={facts.get(key)!r}")
    if payload.get("message"):
        lines.append(payload["message"])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a declared qualification-runner allocation."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--check", action="store_true", help="verify the declaration and prerequisites"
    )
    mode.add_argument("--report", action="store_true", help="print observed host facts only")
    parser.add_argument("--declaration", type=Path, default=None)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true", help="emit a structured JSON report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    facts = collect_facts(repo_root)
    payload: dict[str, Any] = {
        "mode": "check" if args.check else "report",
        "declaration_version": SCHEMA_VERSION,
        "repo_root": str(repo_root),
        "checks": [],
        "facts": asdict(facts),
        "overall": "report",
    }

    if args.report:
        print(json.dumps(payload, indent=2) if args.json else render_text(payload))
        return 0

    declaration_path = args.declaration or (
        Path(os.environ[_DECLARATION_ENV]) if os.environ.get(_DECLARATION_ENV) else None
    )
    checks: list[CheckResult] = []
    if declaration_path is None:
        payload["overall"] = "blocked"
        payload["message"] = (
            "no declaration provided; set --declaration or "
            f"{_DECLARATION_ENV}. Actual capacity requires an operator-owned "
            "allocation (see issue #85)."
        )
    else:
        declaration, error = load_declaration(declaration_path)
        payload["declaration"] = str(declaration_path)
        if declaration is None:
            payload["overall"] = "blocked"
            payload["message"] = error
        else:
            checks.extend(validate_declaration(declaration))
            if overall_status(checks) == "ok":
                checks.extend(evaluate_resources(declaration, facts))
                checks.extend(prerequisite_checks(declaration, repo_root))
            payload["overall"] = overall_status(checks)
    payload["checks"] = [asdict(item) for item in checks]

    print(json.dumps(payload, indent=2) if args.json else render_text(payload))
    return 0 if payload["overall"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
