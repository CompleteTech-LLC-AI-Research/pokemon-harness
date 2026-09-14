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
are emitted relative to the repository root or reduced to their basename.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
_DECLARATION_ENV = "POKERED_QUALIFICATION_DECLARATION"
_RESERVATION_MECHANISMS = ("dedicated-host", "cgroup-quota", "cpuset-affinity")
_CPU_BINDING_MECHANISMS = ("dedicated-host", "cpuset-affinity")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")


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


def _cgroup_relative_path(controller: str | None, cgroup_text: str | None = None) -> str | None:
    raw = cgroup_text if cgroup_text is not None else _read_text(Path("/proc/self/cgroup"))
    if raw is None:
        return None
    for line in raw.splitlines():
        hierarchy, controllers, path = (line.split(":", 2) + ["", "", ""])[:3]
        if controller is None:
            if hierarchy == "0" and not controllers:
                return path or "/"
        elif controller in controllers.split(","):
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


def _read_cgroup_facts(root: Path | None = None, cgroup_text: str | None = None) -> dict[str, Any]:
    """Read the cgroup facts for this process.

    The controller files are detected on the process cgroup and its ancestors,
    not only at the mount root: a cgroup-v2 hierarchy can expose ``cpu.max``
    below ``/sys/fs/cgroup`` without it existing at the root itself.
    """

    base = Path("/sys/fs/cgroup") if root is None else Path(root)
    version = "unavailable"
    quota_cores: float | None = None
    weight: int | None = None
    throttled: dict[str, int] | None = None
    relative: str | None = None

    v2_relative = _cgroup_relative_path(None, cgroup_text)
    v2_paths = _iter_cgroup_paths(base, v2_relative)
    v1_base = base / "cpu"
    v1_relative = _cgroup_relative_path("cpu", cgroup_text)
    v1_paths = _iter_cgroup_paths(v1_base, v1_relative)

    if any((path / "cpu.max").exists() for path in v2_paths):
        version = "v2"
        relative = v2_relative
        quotas = [
            quota
            for quota in (_parse_cpu_max(_read_text(path / "cpu.max") or "") for path in v2_paths)
            if quota is not None
        ]
        quota_cores = min(quotas) if quotas else None
        for path in v2_paths:
            raw_weight = _read_text(path / "cpu.weight")
            if raw_weight is not None and raw_weight.isdigit():
                weight = int(raw_weight)
                break
        stat_paths = v2_paths
    elif any((path / "cpu.cfs_quota_us").exists() for path in v1_paths):
        version = "v1"
        relative = v1_relative
        quotas = []
        for path in v1_paths:
            quota_raw = _read_text(path / "cpu.cfs_quota_us")
            period_raw = _read_text(path / "cpu.cfs_period_us")
            if not quota_raw or not period_raw:
                continue
            quota_us = int(quota_raw)
            period_us = int(period_raw)
            if quota_us >= 0 and period_us > 0:
                quotas.append(quota_us / period_us)
        quota_cores = min(quotas) if quotas else None
        for path in v1_paths:
            shares_raw = _read_text(path / "cpu.shares")
            if shares_raw and shares_raw.isdigit():
                weight = int(shares_raw)
                break
        stat_paths = v1_paths
    else:
        stat_paths = v2_paths

    for path in stat_paths:
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
        "cgroup_relative_path": relative,
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
    cgroup_relative_path: str | None = None
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


def _probe_shm(shm: Path) -> tuple[bool, str | None]:
    """Atomically prove shared memory is writable.

    The probe name is predictable (it embeds the PID), so it is created with
    ``O_CREAT | O_EXCL``.  A pre-existing entry is never overwritten and is
    reported back so the caller can surface it as unsupported.
    """

    probe = shm / f".qualification-runner-probe-{os.getpid()}"
    try:
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False, "shm-probe-exists"
    except OSError:
        return False, None
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(b"ok")
    except OSError:
        return False, None
    finally:
        try:
            probe.unlink()
        except OSError:
            pass
    return True, None


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
            writable, marker = _probe_shm(shm)
            facts.shm_writable = writable
            if marker is not None:
                facts.unsupported.append(marker)
        except OSError:
            facts.shm_writable = False
    else:
        facts.unsupported.append("shm-missing")

    if system != "Linux":
        facts.unsupported.extend(["cgroup", "pressure", "procfs"])
        return facts

    cgroup = _read_cgroup_facts()
    facts.cgroup_version = cgroup["cgroup_version"]
    facts.cgroup_relative_path = cgroup["cgroup_relative_path"]
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


def _is_sha1(value: Any) -> bool:
    return isinstance(value, str) and _SHA1_RE.fullmatch(value.strip().lower()) is not None


def validate_declaration(declaration: dict[str, Any]) -> list[CheckResult]:
    """Validate the declaration structure and required fields."""

    required_fields = (
        "declaration_version",
        "runner_id",
        "reservation_mechanism",
        "reservation",
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
    reservation = declaration.get("reservation")
    if not isinstance(reservation, dict):
        results.append(
            _result(
                "reservation-structure",
                "fail",
                "allocation evidence object",
                reservation,
                "a reservation must carry verifiable allocation evidence, not a bare declaration",
            )
        )
    else:
        allocation_id = reservation.get("allocation_id")
        runner_id = declaration.get("runner_id")
        if not isinstance(allocation_id, str) or not allocation_id.strip():
            results.append(
                _result(
                    "reservation-allocation",
                    "fail",
                    "non-empty allocation_id",
                    allocation_id,
                    "reservation.allocation_id is required to bind the run",
                )
            )
        elif allocation_id != runner_id:
            results.append(
                _result(
                    "reservation-allocation",
                    "fail",
                    runner_id,
                    allocation_id,
                    "runner_id must equal the reservation allocation_id",
                )
            )
        else:
            results.append(
                _result(
                    "reservation-allocation",
                    "ok",
                    runner_id,
                    allocation_id,
                    "runner is bound to a declared allocation",
                )
            )
        exclusive = reservation.get("exclusive")
        if exclusive is not None and not isinstance(exclusive, bool):
            results.append(
                _result(
                    "reservation-exclusive",
                    "fail",
                    "bool",
                    exclusive,
                    "reservation.exclusive must be a boolean",
                )
            )
        cgroup_path = reservation.get("cgroup_path")
        if cgroup_path is not None and (
            not isinstance(cgroup_path, str) or not cgroup_path.strip()
        ):
            results.append(
                _result(
                    "reservation-cgroup-path",
                    "fail",
                    "non-empty string",
                    cgroup_path,
                    "reservation.cgroup_path must be a non-empty string",
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
    inputs = assets.get("inputs") if isinstance(assets, dict) else None
    if not isinstance(inputs, list) or not inputs:
        results.append(
            _result(
                "assets-inputs",
                "fail",
                "non-empty list of {path, sha1}",
                inputs,
                "declared ROM/SYM inputs are required",
            )
        )
    elif any(not isinstance(entry, dict) or not entry.get("path") for entry in inputs):
        results.append(
            _result(
                "assets-inputs",
                "fail",
                "list of {path, sha1}",
                inputs,
                "every asset input must be an object with a path",
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


def _effective_cpu_cores(facts: RunnerFacts) -> float:
    """Effective CPUs are bounded by both affinity and any cgroup quota."""

    if facts.affinity_supported:
        affinity = float(facts.affinity_count)
    else:
        affinity = float(facts.logical_cpus)
    if facts.cpu_quota_cores is not None:
        return min(affinity, float(facts.cpu_quota_cores))
    return affinity


def evaluate_reservation(declaration: dict[str, Any], facts: RunnerFacts) -> CheckResult:
    """Verify that the run is bound to an observable allocation.

    A bare declaration, an affinity mask that merely contains the declared
    CPUs, or a CPU share is not a reservation.  Each mechanism must be
    confirmed against host facts, otherwise the result is fail/unsupported.
    """

    mechanism = declaration.get("reservation_mechanism")
    reservation = declaration.get("reservation")
    declared_cpus = int(declaration.get("logical_cpus") or 0)
    if not isinstance(reservation, dict):
        return _result(
            "reservation-evidence",
            "fail",
            "allocation evidence object",
            reservation,
            "no verifiable allocation evidence was declared",
        )
    allocation_id = reservation.get("allocation_id")
    runner_id = declaration.get("runner_id")
    if not allocation_id or allocation_id != runner_id:
        return _result(
            "reservation-evidence",
            "fail",
            f"bound to {runner_id!r}",
            allocation_id,
            "runner_id is not bound to an allocation",
        )
    try:
        declared_affinity = set(parse_cpuset(declaration.get("affinity_cpus")))
    except (TypeError, ValueError):
        declared_affinity = set()
    exact_affinity = (
        facts.affinity_supported
        and bool(declared_affinity)
        and declared_affinity == set(facts.affinity_cpus)
    )

    if mechanism == "cgroup-quota":
        cgroup_path = reservation.get("cgroup_path")
        if not isinstance(cgroup_path, str) or not cgroup_path.strip():
            return _result(
                "reservation-evidence",
                "fail",
                "reservation.cgroup_path",
                cgroup_path,
                "a cgroup-quota allocation must declare its cgroup path",
            )
        if facts.cgroup_relative_path is None:
            return _result(
                "reservation-evidence",
                "unsupported",
                cgroup_path,
                None,
                "observed cgroup path is unavailable",
            )
        if facts.cgroup_relative_path != cgroup_path:
            return _result(
                "reservation-evidence",
                "fail",
                cgroup_path,
                facts.cgroup_relative_path,
                "observed cgroup is not the declared allocation",
            )
        if facts.cpu_quota_cores is None:
            return _result(
                "reservation-evidence",
                "fail",
                cgroup_path,
                None,
                "the declared allocation has no observed CPU quota",
            )
        return _result(
            "reservation-evidence",
            "ok",
            cgroup_path,
            facts.cgroup_relative_path,
            "cgroup allocation verified by path and quota",
        )
    if mechanism == "cpuset-affinity":
        if not facts.affinity_supported:
            return _result(
                "reservation-evidence",
                "unsupported",
                sorted(declared_affinity),
                None,
                "affinity is unavailable here",
            )
        return _result(
            "reservation-evidence",
            "ok" if exact_affinity else "fail",
            sorted(declared_affinity),
            facts.affinity_cpus,
            "cpuset allocation must exactly equal the observed affinity",
        )
    if mechanism == "dedicated-host":
        if reservation.get("exclusive") is not True:
            return _result(
                "reservation-evidence",
                "fail",
                True,
                reservation.get("exclusive"),
                "dedicated-host requires reservation.exclusive=true",
            )
        if not facts.affinity_supported:
            return _result(
                "reservation-evidence",
                "unsupported",
                sorted(declared_affinity),
                None,
                "affinity is unavailable here",
            )
        if not exact_affinity:
            return _result(
                "reservation-evidence",
                "fail",
                sorted(declared_affinity),
                facts.affinity_cpus,
                "a dedicated host must own exactly the declared CPUs",
            )
        if facts.cpu_quota_cores is not None and facts.cpu_quota_cores < declared_cpus:
            return _result(
                "reservation-evidence",
                "fail",
                declared_cpus,
                facts.cpu_quota_cores,
                "a competing CPU quota restricts the dedicated host",
            )
        declared_weight = declaration.get("cpu_weight")
        if (
            declared_weight is not None
            and facts.cpu_weight is not None
            and facts.cpu_weight < declared_weight
        ):
            return _result(
                "reservation-evidence",
                "fail",
                declared_weight,
                facts.cpu_weight,
                "a competing CPU weight exists on the dedicated host",
            )
        return _result(
            "reservation-evidence",
            "ok",
            sorted(declared_affinity),
            facts.affinity_cpus,
            "dedicated host owns exactly the declared CPUs with no competing quota",
        )
    return _result(
        "reservation-evidence",
        "fail",
        list(_RESERVATION_MECHANISMS),
        mechanism,
        "unsupported reservation mechanism",
    )


def evaluate_resources(declaration: dict[str, Any], facts: RunnerFacts) -> list[CheckResult]:
    """Compare declared allocation requirements against observed host facts."""

    mechanism = declaration.get("reservation_mechanism")
    results: list[CheckResult] = [evaluate_reservation(declaration, facts)]

    declared_cpus = int(declaration.get("logical_cpus") or 0)
    effective_cpus = _effective_cpu_cores(facts)
    results.append(
        _result(
            "logical-cpus",
            "ok" if effective_cpus >= declared_cpus > 0 else "fail",
            declared_cpus,
            effective_cpus,
            "effective CPUs available: min(affinity, cgroup quota)",
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
        declared_set = set(declared_affinity)
        observed_set = set(facts.affinity_cpus)
        usable = declared_set.issubset(observed_set)
        bounded = bool(declared_affinity) and len(declared_affinity) <= len(facts.affinity_cpus)
        if mechanism in _CPU_BINDING_MECHANISMS:
            ok = usable and bounded and declared_set == observed_set
            detail = "declared affinity must exactly equal the process affinity for this mechanism"
        else:
            ok = usable and bounded
            detail = (
                "declared affinity must be usable by this process and no larger than the allocation"
            )
        results.append(
            _result(
                "affinity", "ok" if ok else "fail", declared_affinity, facts.affinity_cpus, detail
            )
        )

    declared_quota = declaration.get("cpu_quota_cores")
    quota_below_requirement = declared_quota is not None and declared_quota < declared_cpus
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
        elif quota_below_requirement:
            results.append(
                _result(
                    "cpu-quota",
                    "fail",
                    declared_cpus,
                    declared_quota,
                    "declared quota is below the declared CPU requirement",
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
        if quota_below_requirement:
            results.append(
                _result(
                    "cpu-quota",
                    "fail",
                    declared_cpus,
                    declared_quota,
                    "declared quota is below the declared CPU requirement",
                )
            )
        elif facts.cpu_quota_cores is None:
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
    elif facts.cpu_quota_cores is not None and facts.cpu_quota_cores < declared_cpus:
        results.append(
            _result(
                "cpu-quota",
                "fail",
                declared_cpus,
                facts.cpu_quota_cores,
                "an undeclared cgroup quota restricts the declared CPU requirement",
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
    elif "shm-missing" in facts.unsupported:
        results.append(
            _result(
                "shm",
                "unsupported",
                declared_shm,
                facts.shm_size_bytes,
                "shared memory is unavailable on this host",
            )
        )
    elif "shm-probe-exists" in facts.unsupported:
        results.append(
            _result(
                "shm",
                "unsupported",
                declared_shm,
                facts.shm_size_bytes,
                "the shared-memory probe path already exists; not overwritten",
            )
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


def _sha1_of_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_versions_pins(repo_root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Return ROM and symbol pins keyed by root-relative and full paths."""

    try:
        from pokered_harness.config import load_versions
    except ImportError:
        return {}, {}
    versions_path = repo_root / "VERSIONS.md"
    if not versions_path.is_file():
        return {}, {}
    try:
        config = load_versions(versions_path)
    except (OSError, ValueError):
        return {}, {}
    rom_pins: dict[str, str] = {}
    symbol_pins: dict[str, str] = {}
    for documented, sha1 in config.rom_sha1_by_path:
        rom_pins[documented] = sha1
        rom_pins[documented.removeprefix("rom/")] = sha1
    for documented, sha1 in config.symbol_sha1_by_path:
        symbol_pins[documented] = sha1
        symbol_pins[documented.removeprefix("rom/")] = sha1
    return rom_pins, symbol_pins


def validate_asset_inputs(declaration: dict[str, Any], repo_root: Path) -> list[CheckResult]:
    """Verify declared ROM/SYM bytes exist, are readable, and match pins.

    Missing, empty, unreadable, or hash-mismatched inputs are blocked, never
    accepted.  The repository pins in ``VERSIONS.md`` take precedence; an
    explicit ``sha1`` is required only when no repository pin matches.
    """

    assets = declaration.get("assets") or {}
    results: list[CheckResult] = []
    rom_root_value = assets.get("rom_root")
    rom_root = Path(rom_root_value) if rom_root_value else None
    if rom_root is None or not rom_root.is_dir():
        results.append(
            _result(
                "assets-rom-root",
                "fail",
                "directory",
                rom_root_value,
                "rom_root is not a directory",
            )
        )
    fixture_root_value = assets.get("fixture_root")
    fixture_root = Path(fixture_root_value) if fixture_root_value else None
    if fixture_root is None or not fixture_root.is_dir():
        results.append(
            _result(
                "assets-fixture-root",
                "fail",
                "directory",
                fixture_root_value,
                "fixture_root is not a directory",
            )
        )

    inputs = assets.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        results.append(
            _result(
                "assets-inputs",
                "fail",
                "non-empty list of {path, sha1}",
                inputs,
                "declared ROM/SYM inputs are required",
            )
        )
        return results
    if rom_root is None or not rom_root.is_dir():
        return results

    rom_pins, symbol_pins = _load_versions_pins(repo_root)
    for index, entry in enumerate(inputs):
        name = f"assets-input-{index}"
        if not isinstance(entry, dict):
            results.append(_result(name, "fail", "object", entry, "asset input must be an object"))
            continue
        relative = entry.get("path")
        declared_sha1 = entry.get("sha1")
        if not isinstance(relative, str) or not relative.strip():
            results.append(
                _result(name, "fail", "relative path", relative, "asset path is required")
            )
            continue
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            results.append(
                _result(
                    name,
                    "fail",
                    "relative path under rom_root",
                    relative,
                    "asset input must stay under rom_root",
                )
            )
            continue
        resolved = rom_root / candidate
        if not resolved.is_file():
            results.append(
                _result(
                    name, "fail", "readable non-empty file", relative, "declared input is missing"
                )
            )
            continue
        try:
            if resolved.stat().st_size <= 0:
                results.append(
                    _result(name, "fail", "non-empty file", relative, "declared input is empty")
                )
                continue
            actual_sha1 = _sha1_of_file(resolved)
        except OSError as exc:
            results.append(
                _result(
                    name, "fail", "readable file", relative, f"declared input is unreadable: {exc}"
                )
            )
            continue

        normalised = relative.replace("\\", "/").lstrip("./").lower()
        expected = rom_pins.get(normalised) or symbol_pins.get(normalised)
        if expected is not None:
            if (
                declared_sha1 is not None
                and _is_sha1(declared_sha1)
                and declared_sha1.lower() != expected
            ):
                results.append(
                    _result(
                        name,
                        "fail",
                        expected,
                        declared_sha1,
                        "declared hash disagrees with the repository pin",
                    )
                )
                continue
            target = expected
        elif _is_sha1(declared_sha1):
            target = declared_sha1.lower()
        else:
            results.append(
                _result(
                    name,
                    "fail",
                    "repository pin or declared sha1",
                    declared_sha1,
                    "asset input has no verifiable hash",
                )
            )
            continue
        if actual_sha1 != target:
            results.append(
                _result(
                    name,
                    "fail",
                    target,
                    actual_sha1,
                    "asset input bytes do not match the pinned hash",
                )
            )
            continue
        results.append(_result(name, "ok", target, actual_sha1, "asset input bytes verified"))
    return results


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
    results.extend(validate_asset_inputs(declaration, repo_root))

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
    except OSError as exc:
        return None, f"cannot read declaration: {exc}"
    except UnicodeDecodeError as exc:
        return None, f"cannot read declaration: {exc}"
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


def _sanitize_path(value: Any, root: Path) -> str:
    """Return a relative path when possible, never a machine-local absolute."""

    text = str(value)
    candidate = Path(text)
    if not candidate.is_absolute():
        return candidate.as_posix()
    try:
        relative = candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return candidate.name
    return relative.as_posix() or "."


def _redact_payload(value: Any, redactions: dict[str, str]) -> Any:
    if isinstance(value, str):
        for token, replacement in redactions.items():
            if token:
                value = value.replace(token, replacement)
        return value
    if isinstance(value, list):
        return [_redact_payload(item, redactions) for item in value]
    if isinstance(value, dict):
        return {key: _redact_payload(item, redactions) for key, item in value.items()}
    return value


def _collect_redactions(
    repo_root: Path, declaration: dict[str, Any] | None, declaration_path: Path | None
) -> dict[str, str]:
    redactions = {str(repo_root): _sanitize_path(repo_root, repo_root)}
    if declaration_path is not None:
        redactions[str(declaration_path)] = _sanitize_path(declaration_path, repo_root)
    if declaration:
        assets = declaration.get("assets") or {}
        for key in ("rom_root", "fixture_root"):
            value = assets.get(key)
            if isinstance(value, str) and value:
                redactions[value] = _sanitize_path(value, repo_root)
        interpreters = declaration.get("interpreters") or {}
        for value in interpreters.values():
            if isinstance(value, str) and value:
                redactions[value] = _sanitize_path(value, repo_root)
    return redactions


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
    facts_payload = asdict(facts)
    facts_payload["shm_path"] = _sanitize_path(facts.shm_path, repo_root)
    payload: dict[str, Any] = {
        "mode": "check" if args.check else "report",
        "declaration_version": SCHEMA_VERSION,
        "repo_root": _sanitize_path(repo_root, repo_root),
        "checks": [],
        "facts": facts_payload,
        "overall": "report",
    }

    if args.report:
        payload = _redact_payload(payload, _collect_redactions(repo_root, None, None))
        print(json.dumps(payload, indent=2) if args.json else render_text(payload))
        return 0

    declaration_path = args.declaration or (
        Path(os.environ[_DECLARATION_ENV]) if os.environ.get(_DECLARATION_ENV) else None
    )
    checks: list[CheckResult] = []
    declaration: dict[str, Any] | None = None
    if declaration_path is None:
        payload["overall"] = "blocked"
        payload["message"] = (
            "no declaration provided; set --declaration or "
            f"{_DECLARATION_ENV}. Actual capacity requires an operator-owned "
            "allocation (see issue #85)."
        )
    else:
        declaration, error = load_declaration(declaration_path)
        payload["declaration"] = _sanitize_path(declaration_path, repo_root)
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
    payload = _redact_payload(
        payload, _collect_redactions(repo_root, declaration, declaration_path)
    )

    print(json.dumps(payload, indent=2) if args.json else render_text(payload))
    return 0 if payload["overall"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
