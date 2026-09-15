#!/usr/bin/env python3
"""Verify a declared qualification-runner allocation and its prerequisites.

The production gate runs emulator pairs whose deadlines are sensitive to CPU
scheduling.  This tool verifies, without stopping or reprioritizing any other
process, whether the current host matches an operator-declared allocation.

Modes:

* ``--report`` prints the observed host facts (CPU affinity, cgroup quota and
  weight, throttling counters, memory, disk, shared memory, load).
* ``--check`` compares those facts against an explicit declaration plus a
  pinned, immutable allocation descriptor and runs the runtime/asset
  prerequisites.  It exits ``0`` only when every check is ``ok``; a missing
  declaration is reported as ``blocked``, never as a pass.
* ``--setup`` creates the private per-job directory tree and validates
  prerequisites and the native build fingerprint without reserving capacity.
* ``--reserve`` writes the allocation descriptor, takes an exclusive lease on
  it, and holds that lease while running an optional command under it.
* ``--release`` ends a lease this tool owns; ``--recover`` removes only stale
  lease state whose owner is already gone.  Neither reprioritizes unrelated
  processes.

A matching declaration alone is never accepted as a reservation.  The run is
bound to an operator-owned allocation descriptor whose bytes are pinned by
SHA-256, and whose holder, lease lock, cgroup membership, cpuset, and quota are
re-observed on this host.

The declaration is operator-owned and deliberately external to the repository.
Use ``--declaration`` or ``POKERED_QUALIFICATION_DECLARATION``.  No ROM bytes,
credentials, or machine-local paths are written into Git or the report; paths
are emitted relative to the repository root or reduced to their basename.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 3
_DECLARATION_ENV = "POKERED_QUALIFICATION_DECLARATION"
_RESERVATION_MECHANISMS = ("dedicated-host", "cgroup-quota", "cpuset-affinity")
_CPU_BINDING_MECHANISMS = ("dedicated-host", "cpuset-affinity")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DESCRIPTOR_VERSION = 1
_PROBE_TIMEOUT_ENV = "POKERED_QUALIFICATION_COMMAND_TIMEOUT_SECONDS"
_DEFAULT_PROBE_TIMEOUT_SECONDS = 300.0
_TIMEOUT_RETURNCODE = 124
_CHILD_TERMINATION_GRACE_SECONDS = 5.0
_RUNTIME_MODULES = (
    "pyboy",
    "pyboy.pyboy",
    "pyboy.utils",
    "pyboy.core.mb",
    "pyboy.core.serial",
    "pyboy.link",
)
_EXTENSION_BACKED_MODULES = (
    "pyboy.pyboy",
    "pyboy.utils",
    "pyboy.core.mb",
    "pyboy.core.serial",
)
_NATIVE_PROBE = """\
import hashlib, importlib, importlib.machinery, json, sys

names = %(modules)r
report = {}
for name in names:
    try:
        module = importlib.import_module(name)
    except Exception as exc:
        print(json.dumps({"error": f"{name}: {type(exc).__name__}: {exc}"}))
        raise SystemExit(2)
    filename = str(getattr(module, "__file__", "") or "")
    kind = "unknown"
    digest = None
    if filename:
        if filename.endswith(tuple(importlib.machinery.EXTENSION_SUFFIXES)):
            kind = "cython"
        elif filename.endswith(".py"):
            kind = "source"
        try:
            with open(filename, "rb") as stream:
                digest = hashlib.sha256(stream.read()).hexdigest()
        except OSError:
            digest = None
    report[name] = {"kind": kind, "sha256": digest}

import pyboy
from pyboy import utils

identity = {
    "python": sys.version.split()[0],
    "version": getattr(pyboy, "__version__", None),
    "revision": getattr(pyboy, "__pokered_harness_revision__", None),
    "cython_compiled": bool(getattr(utils, "cython_compiled", False)),
    "modules": report,
}
fingerprint = hashlib.sha256(
    json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()
print(json.dumps({"identity": identity, "fingerprint": fingerprint}))
"""
_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w./-])(/(?:[^\s:'\"()\[\],;]+)+)")


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_declared_path(raw: Any, repo_root: Path) -> Path | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    return candidate


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


def _process_start_time(pid: int) -> str | None:
    """Return the Linux start time field for *pid*, or ``None`` if unavailable."""

    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    closing = stat_text.rfind(")")
    if closing == -1:
        return None
    fields = stat_text[closing + 1 :].split()
    if len(fields) < 20:
        return None
    return fields[19]


def _process_ancestor_pids(pid: int | None = None) -> list[int]:
    """Walk the parent chain of *pid* (default: this process)."""

    if pid is None:
        pid = os.getpid()
    ancestors: list[int] = []
    seen: set[int] = set()
    current = pid
    while current > 1 and current not in seen:
        seen.add(current)
        try:
            stat_text = Path(f"/proc/{current}/stat").read_text(encoding="utf-8")
        except (OSError, ValueError):
            break
        closing = stat_text.rfind(")")
        if closing == -1:
            break
        fields = stat_text[closing + 1 :].split()
        if len(fields) < 2:
            break
        try:
            current = int(fields[1])
        except ValueError:
            break
        if current <= 1:
            break
        ancestors.append(current)
    return ancestors


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_tree_pids(root_pid: int | None = None) -> list[int]:
    """Return *root_pid* (default: this process) and every live descendant."""

    if root_pid is None:
        root_pid = os.getpid()
    tree = {root_pid}
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return sorted(tree)
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == root_pid:
            continue
        if root_pid in _process_ancestor_pids(pid):
            tree.add(pid)
    return sorted(tree)


def _flock_holder_pids(lock_path: Path) -> set[int] | None:
    """Return the pids holding a POSIX lock on *lock_path* per ``/proc/locks``.

    ``None`` means the kernel lock table is unavailable, so the caller must
    fail closed rather than assume the lease is held.
    """

    try:
        inode = lock_path.stat().st_ino
    except OSError:
        return None
    try:
        table = Path("/proc/locks").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    holders: set[int] = set()
    for line in table.splitlines():
        fields = line.split()
        if len(fields) < 8:
            continue
        # e.g. ``12: FLOCK ADVISORY WRITE 1234 00:35:12345 0 EOF``
        if "FLOCK" not in fields[1]:
            continue
        device_inode = fields[5]
        if ":" not in device_inode:
            continue
        try:
            entry_inode = int(device_inode.rsplit(":", 1)[1])
            holder = int(fields[4])
        except ValueError:
            continue
        if entry_inode == inode:
            holders.add(holder)
    return holders


def _descriptor_is_private(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) and not (mode & 0o222)


def _directory_is_private(path: Path) -> bool:
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return stat.S_ISDIR(mode) and not (mode & 0o077)


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

    primary: Path | None = None
    if any((path / "cpu.max").exists() for path in v2_paths):
        version = "v2"
        relative = v2_relative
        primary = v2_paths[0]
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
        primary = v1_paths[0]
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
        primary = v2_paths[0] if v2_paths else None

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

    member_pids: list[int] | None = None
    cgroup_dir: str | None = None
    if primary is not None:
        cgroup_dir = str(primary)
        procs_raw = _read_text(primary / "cgroup.procs")
        if procs_raw is not None:
            member_pids = []
            for token in procs_raw.split():
                if token.isdigit():
                    member_pids.append(int(token))
    return {
        "cgroup_version": version,
        "cpu_quota_cores": quota_cores,
        "cpu_weight": weight,
        "cpu_throttled": throttled,
        "cgroup_relative_path": relative,
        "cgroup_dir": cgroup_dir,
        "cgroup_member_pids": member_pids,
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
    cgroup_dir: str | None = None
    cgroup_member_pids: list[int] | None = None
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
    process_pid: int = 0
    process_ancestor_pids: list[int] = field(default_factory=list)
    process_tree_pids: list[int] = field(default_factory=list)
    process_start_time: str | None = None
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
    facts.process_pid = os.getpid()
    facts.process_start_time = _process_start_time(os.getpid())
    facts.process_ancestor_pids = _process_ancestor_pids()
    facts.process_tree_pids = _process_tree_pids()

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
    facts.cgroup_dir = cgroup["cgroup_dir"]
    facts.cgroup_member_pids = cgroup["cgroup_member_pids"]
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


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and _SHA256_RE.fullmatch(value.strip().lower()) is not None


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
        job_dir = reservation.get("job_dir")
        if not isinstance(job_dir, str) or not job_dir.strip():
            results.append(
                _result(
                    "reservation-job-dir",
                    "fail",
                    "non-empty private job directory",
                    job_dir,
                    "reservation.job_dir is required so the descriptor has a private home",
                )
            )
        descriptor_path = reservation.get("descriptor_path")
        if not isinstance(descriptor_path, str) or not descriptor_path.strip():
            results.append(
                _result(
                    "reservation-descriptor",
                    "fail",
                    "path to an immutable allocation descriptor",
                    descriptor_path,
                    "a pinned allocation descriptor is required; a matching id is not a reservation",
                )
            )
        descriptor_sha256 = reservation.get("descriptor_sha256")
        if not isinstance(descriptor_sha256, str) or not _SHA256_RE.fullmatch(
            descriptor_sha256.strip().lower()
        ):
            results.append(
                _result(
                    "reservation-descriptor-sha256",
                    "fail",
                    "64-hex sha256",
                    descriptor_sha256,
                    "the allocation descriptor must be pinned by its SHA-256 digest",
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
    if isinstance(interpreters, dict):
        build_pin = interpreters.get("native_build_inputs_sha256")
        fingerprint = interpreters.get("native_fingerprint")
        if not _is_sha256(build_pin):
            results.append(
                _result(
                    "interpreter-native-build-inputs",
                    "fail",
                    "native build-inputs sha256",
                    build_pin,
                    "pin the deterministic native build-inputs digest computed by --setup",
                )
            )
        if not _is_sha256(fingerprint):
            results.append(
                _result(
                    "interpreter-native-fingerprint",
                    "fail",
                    "native runtime fingerprint sha256",
                    fingerprint,
                    "pin the installed native runtime fingerprint reported by --setup",
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


def _load_allocation_descriptor(path: Path) -> tuple[dict[str, Any] | None, str]:
    if path.is_symlink():
        return None, "the allocation descriptor must be a regular file, not a symlink"
    if not path.is_file():
        return None, "the pinned allocation descriptor is missing"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"the allocation descriptor is not readable JSON: {exc}"
    if not isinstance(document, dict):
        return None, "the allocation descriptor must be a JSON object"
    return document, ""


def _descriptor_lease_status(descriptor: dict[str, Any], facts: RunnerFacts) -> tuple[str, str]:
    """Re-observe that the descriptor's lease is held by the live holder."""

    holder = descriptor.get("holder_pid")
    if not isinstance(holder, int) or isinstance(holder, bool) or holder <= 0:
        return "fail", "descriptor.holder_pid must be a positive integer"
    if not _pid_alive(holder):
        return "fail", "the recorded allocation holder is not running"
    live_start = _process_start_time(holder)
    if live_start is None:
        return "unsupported", "the holder start time is not observable on this host"
    if descriptor.get("holder_start_time") != live_start:
        return "fail", "the recorded holder pid was reused by another process"
    current = os.getpid()
    if holder != current and holder not in facts.process_ancestor_pids:
        return "fail", "this process does not run inside the recorded allocation holder"
    lock_raw = descriptor.get("lock_path")
    if not isinstance(lock_raw, str) or not lock_raw.strip():
        return "fail", "descriptor.lock_path is required to prove the lease is held"
    lock = Path(lock_raw)
    if lock.is_symlink() or not lock.is_file():
        return "fail", "the lease lock file is missing"
    holders = _flock_holder_pids(lock)
    if holders is None:
        return "unsupported", "the kernel lock table is unavailable, so holding is unproven"
    if holder not in holders:
        return "fail", "the recorded holder does not hold the lease lock"
    return "ok", "allocation lease is held by the recorded live holder"


def _cgroup_members_are_owned(facts: RunnerFacts) -> bool:
    members = facts.cgroup_member_pids
    if members is None:
        return False
    owned = set(facts.process_tree_pids)
    if not owned:
        owned = {os.getpid()}
    return set(members).issubset(owned)


def evaluate_reservation(
    declaration: dict[str, Any], facts: RunnerFacts, repo_root: Path | None = None
) -> CheckResult:
    """Verify that the run is bound to a pinned, held allocation descriptor.

    A bare declaration, a matching deployment id, an affinity mask that merely
    contains the declared CPUs, or a CPU share is not a reservation.  The run
    must carry an operator-owned descriptor whose bytes are pinned by SHA-256,
    and whose lease, cgroup membership, cpuset, and quota are re-observed here.
    """

    mechanism = declaration.get("reservation_mechanism")
    reservation = declaration.get("reservation")
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
    if repo_root is None:
        repo_root = Path.cwd()
    job_dir = _resolve_declared_path(reservation.get("job_dir"), repo_root)
    descriptor_path = _resolve_declared_path(reservation.get("descriptor_path"), repo_root)
    descriptor_sha = reservation.get("descriptor_sha256")
    if job_dir is None or descriptor_path is None or not _is_sha256(descriptor_sha):
        return _result(
            "reservation-evidence",
            "fail",
            "pinned descriptor inside reservation.job_dir",
            descriptor_path.name if descriptor_path else descriptor_path,
            "a descriptor pinned by SHA-256 inside a private job directory is required",
        )
    if not _path_within(descriptor_path, job_dir):
        return _result(
            "reservation-evidence",
            "fail",
            "descriptor inside reservation.job_dir",
            descriptor_path.name,
            "the allocation descriptor must live inside reservation.job_dir",
        )
    if not _directory_is_private(job_dir):
        return _result(
            "reservation-evidence",
            "fail",
            "owner-only job directory",
            job_dir.name,
            "reservation.job_dir is not an owner-only directory",
        )
    if not _descriptor_is_private(descriptor_path):
        return _result(
            "reservation-evidence",
            "fail",
            "read-only allocation descriptor",
            descriptor_path.name,
            "the allocation descriptor is writable or not a regular file",
        )
    try:
        actual_sha = _sha256_of_file(descriptor_path)
    except OSError:
        return _result(
            "reservation-evidence",
            "unsupported",
            descriptor_sha,
            None,
            "the pinned allocation descriptor could not be read",
        )
    if actual_sha != descriptor_sha.strip().lower():
        return _result(
            "reservation-evidence",
            "fail",
            descriptor_sha,
            actual_sha,
            "the allocation descriptor bytes do not match the pinned digest",
        )
    descriptor, error = _load_allocation_descriptor(descriptor_path)
    if descriptor is None:
        return _result("reservation-evidence", "fail", "valid descriptor JSON", None, error)
    if descriptor.get("descriptor_version") != _DESCRIPTOR_VERSION:
        return _result(
            "reservation-evidence",
            "unsupported",
            _DESCRIPTOR_VERSION,
            descriptor.get("descriptor_version"),
            f"only descriptor version {_DESCRIPTOR_VERSION} is supported",
        )
    if descriptor.get("allocation_id") != runner_id or descriptor.get("runner_id") != runner_id:
        return _result(
            "reservation-evidence",
            "fail",
            runner_id,
            descriptor.get("allocation_id"),
            "the descriptor is not bound to this runner_id",
        )
    if descriptor.get("mechanism") != mechanism:
        return _result(
            "reservation-evidence",
            "fail",
            mechanism,
            descriptor.get("mechanism"),
            "descriptor mechanism disagrees with the declaration",
        )
    if descriptor.get("state") != "held":
        return _result(
            "reservation-evidence",
            "fail",
            "held",
            descriptor.get("state"),
            "the allocation descriptor does not report a held lease",
        )
    lease_status, lease_detail = _descriptor_lease_status(descriptor, facts)
    if lease_status != "ok":
        return _result(
            "reservation-evidence", lease_status, "held allocation lease", None, lease_detail
        )

    if mechanism == "cgroup-quota":
        return _verify_cgroup_reservation(descriptor, facts)
    if mechanism == "cpuset-affinity":
        return _verify_cpuset_reservation(descriptor, facts)
    if mechanism == "dedicated-host":
        return _verify_dedicated_reservation(descriptor, declaration, facts, repo_root)
    return _result(
        "reservation-evidence",
        "fail",
        list(_RESERVATION_MECHANISMS),
        mechanism,
        "unsupported reservation mechanism",
    )


def _verify_cgroup_reservation(descriptor: dict[str, Any], facts: RunnerFacts) -> CheckResult:
    cgroup_path = descriptor.get("cgroup_path")
    if not isinstance(cgroup_path, str) or not cgroup_path.strip() or cgroup_path == "/":
        return _result(
            "reservation-evidence",
            "fail",
            "a non-root allocation cgroup path",
            cgroup_path,
            "a cgroup-quota allocation must name a non-root cgroup",
        )
    if facts.cgroup_relative_path is None:
        return _result(
            "reservation-evidence",
            "unsupported",
            cgroup_path,
            None,
            "the observed cgroup path is unavailable",
        )
    if facts.cgroup_relative_path != cgroup_path:
        return _result(
            "reservation-evidence",
            "fail",
            cgroup_path,
            facts.cgroup_relative_path,
            "the observed cgroup is not the declared allocation",
        )
    quota = descriptor.get("cpu_quota_cores")
    if isinstance(quota, bool) or not isinstance(quota, (int, float)) or quota <= 0:
        return _result(
            "reservation-evidence",
            "fail",
            "a positive reserved CPU quota",
            quota,
            "the descriptor does not record a finite CPU quota",
        )
    if facts.cpu_quota_cores is None:
        return _result(
            "reservation-evidence",
            "fail",
            quota,
            None,
            "the declared allocation has no observed CPU quota",
        )
    if facts.cpu_quota_cores > quota + 1e-6:
        return _result(
            "reservation-evidence",
            "fail",
            quota,
            facts.cpu_quota_cores,
            "the observed CPU quota is wider than the reserved allocation",
        )
    if facts.logical_cpus and facts.cpu_quota_cores >= facts.logical_cpus:
        return _result(
            "reservation-evidence",
            "fail",
            f"< {facts.logical_cpus} cores",
            facts.cpu_quota_cores,
            "a quota that reaches every host CPU is not an allocation",
        )
    if facts.cgroup_member_pids is None:
        return _result(
            "reservation-evidence",
            "unsupported",
            "observable cgroup membership",
            None,
            "competing cgroup workloads cannot be ruled out on this host",
        )
    if not _cgroup_members_are_owned(facts):
        return _result(
            "reservation-evidence",
            "fail",
            "this job's process tree only",
            facts.cgroup_member_pids,
            "competing processes share the declared allocation cgroup",
        )
    return _result(
        "reservation-evidence",
        "ok",
        cgroup_path,
        facts.cgroup_member_pids,
        "the held allocation cgroup contains only this job with a finite quota",
    )


def _verify_cpuset_reservation(descriptor: dict[str, Any], facts: RunnerFacts) -> CheckResult:
    if not facts.affinity_supported:
        return _result(
            "reservation-evidence",
            "unsupported",
            descriptor.get("cpuset"),
            None,
            "affinity is unavailable here",
        )
    try:
        descriptor_cpuset = set(parse_cpuset(descriptor.get("cpuset")))
    except (TypeError, ValueError):
        descriptor_cpuset = set()
    if not descriptor_cpuset:
        return _result(
            "reservation-evidence",
            "fail",
            "a non-empty descriptor cpuset",
            descriptor.get("cpuset"),
            "the descriptor does not record the reserved cpuset",
        )
    if set(facts.affinity_cpus) != descriptor_cpuset:
        return _result(
            "reservation-evidence",
            "fail",
            sorted(descriptor_cpuset),
            facts.affinity_cpus,
            "the observed affinity does not exactly equal the reserved cpuset",
        )
    if facts.logical_cpus and len(descriptor_cpuset) >= facts.logical_cpus:
        return _result(
            "reservation-evidence",
            "fail",
            f"< {facts.logical_cpus} cpus",
            sorted(descriptor_cpuset),
            "a cpuset spanning every host CPU is not an allocation",
        )
    return _result(
        "reservation-evidence",
        "ok",
        sorted(descriptor_cpuset),
        facts.affinity_cpus,
        "the observed affinity is exactly the reserved, narrower cpuset",
    )


def _verify_dedicated_reservation(
    descriptor: dict[str, Any],
    declaration: dict[str, Any],
    facts: RunnerFacts,
    repo_root: Path,
) -> CheckResult:
    declared_cpus = int(declaration.get("logical_cpus") or 0)
    if descriptor.get("exclusive") is not True:
        return _result(
            "reservation-evidence",
            "fail",
            True,
            descriptor.get("exclusive"),
            "a dedicated host requires an exclusive allocation descriptor",
        )
    if not facts.affinity_supported:
        return _result(
            "reservation-evidence",
            "unsupported",
            "observed affinity",
            None,
            "affinity is unavailable here",
        )
    if facts.logical_cpus and set(facts.affinity_cpus) != set(range(facts.logical_cpus)):
        return _result(
            "reservation-evidence",
            "fail",
            f"all {facts.logical_cpus} host cpus",
            facts.affinity_cpus,
            "a dedicated host must own every host CPU",
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
    marker = _resolve_declared_path(descriptor.get("exclusive_marker_path"), repo_root)
    if marker is None or marker.is_symlink() or not marker.is_file():
        return _result(
            "reservation-evidence",
            "fail",
            "a documented exclusive marker",
            descriptor.get("exclusive_marker_path"),
            "a dedicated host needs an operator-created exclusive marker",
        )
    try:
        marker_value = marker.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        marker_value = ""
    if marker_value != descriptor.get("lease_id"):
        return _result(
            "reservation-evidence",
            "fail",
            descriptor.get("lease_id"),
            marker_value[:64],
            "the exclusive marker does not match the allocation lease id",
        )
    return _result(
        "reservation-evidence",
        "ok",
        facts.affinity_cpus,
        marker.name,
        "dedicated host owns every CPU with a matching exclusive marker and held lease",
    )


def evaluate_resources(
    declaration: dict[str, Any], facts: RunnerFacts, repo_root: Path | None = None
) -> list[CheckResult]:
    """Compare declared allocation requirements against observed host facts."""

    mechanism = declaration.get("reservation_mechanism")
    results: list[CheckResult] = [evaluate_reservation(declaration, facts, repo_root)]

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


def _command_timeout() -> float:
    raw = os.environ.get(_PROBE_TIMEOUT_ENV)
    if raw is None:
        return _DEFAULT_PROBE_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_PROBE_TIMEOUT_SECONDS
    return value if value > 0 else _DEFAULT_PROBE_TIMEOUT_SECONDS


def _posix_child_preexec() -> None:
    """Ask the kernel to kill an owned child when this process dies."""

    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGKILL, 0, 0, 0)
    except Exception:  # noqa: BLE001 - best-effort parent-death signal
        return


def _process_group_options() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True, "preexec_fn": _posix_child_preexec}


_OWNED_PROCESSES: set[subprocess.Popen[str]] = set()
_SIGNAL_HANDLERS_INSTALLED = False


def _register_owned(process: subprocess.Popen[str]) -> None:
    _OWNED_PROCESSES.add(process)


def _unregister_owned(process: subprocess.Popen[str]) -> None:
    _OWNED_PROCESSES.discard(process)


def _terminate_owned_process(process: subprocess.Popen[str]) -> None:
    """Terminate *process* and its descendants without masking the failure."""

    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill is not None:
            try:
                subprocess.run(
                    [taskkill, "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=_CHILD_TERMINATION_GRACE_SECONDS,
                )
            except (OSError, subprocess.TimeoutExpired, KeyboardInterrupt):
                pass
        else:
            try:
                process.terminate()
            except (OSError, ValueError):
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            try:
                process.terminate()
            except (OSError, ValueError):
                pass
    try:
        process.wait(timeout=_CHILD_TERMINATION_GRACE_SECONDS)
    except (subprocess.TimeoutExpired, OSError, KeyboardInterrupt):
        pass
    if os.name == "nt":
        try:
            process.kill()
        except (OSError, ValueError):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except (OSError, ValueError):
                pass
    try:
        process.wait(timeout=_CHILD_TERMINATION_GRACE_SECONDS)
    except (subprocess.TimeoutExpired, OSError, KeyboardInterrupt):
        pass


def _terminate_owned_processes(*_args: object) -> None:
    for process in list(_OWNED_PROCESSES):
        _terminate_owned_process(process)
    _OWNED_PROCESSES.clear()


def _install_signal_handlers() -> None:
    global _SIGNAL_HANDLERS_INSTALLED
    if _SIGNAL_HANDLERS_INSTALLED or os.name == "nt":
        return
    _SIGNAL_HANDLERS_INSTALLED = True

    def _handler(signum: int, _frame: object) -> None:
        _terminate_owned_processes()
        raise SystemExit(128 + signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, _handler)
        except (OSError, ValueError):
            continue


def run_command(
    command: list[str], cwd: Path, timeout: float | None = None
) -> subprocess.CompletedProcess[str]:
    """Run *command* under a deadline with owned-process cleanup.

    The command runs in its own session/process group, receives a parent-death
    signal, and is torn down as a group on timeout.  The timeout is reported as
    a distinct terminal result without discarding captured output.
    """

    timeout = _command_timeout() if timeout is None else timeout
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **_process_group_options(),
        )
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, "", f"{type(exc).__name__}: {exc}")
    _register_owned(process)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_owned_process(process)
        try:
            stdout, stderr = process.communicate(timeout=_CHILD_TERMINATION_GRACE_SECONDS)
        except (subprocess.TimeoutExpired, OSError):
            stdout, stderr = "", ""
        note = (
            f"\n[qualification-runner] command exceeded {timeout:g}s; "
            "its owned process group was terminated"
        )
        return subprocess.CompletedProcess(
            command, _TIMEOUT_RETURNCODE, stdout, (stderr or "") + note
        )
    finally:
        _unregister_owned(process)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


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


def _canonical_asset_key(value: str | Path) -> str:
    key = str(value).replace("\\", "/").lstrip("./").lower()
    return key.removeprefix("rom/")


@dataclass
class _AssetRequirement:
    key: str
    root: str
    sha1: str | None = None
    sha256: str | None = None
    size: int | None = None


def _fixture_requirements(repo_root: Path) -> dict[str, _AssetRequirement]:
    manifest_path = repo_root / "release-evidence" / "fixture-manifest.json"
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    fixtures = document.get("fixtures")
    if not isinstance(fixtures, list):
        return {}
    requirements: dict[str, _AssetRequirement] = {}
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            continue
        relative = fixture.get("path")
        if not isinstance(relative, str) or not relative.strip():
            continue
        key = _canonical_asset_key(relative)
        sha1 = fixture.get("sha1")
        sha256 = fixture.get("sha256")
        size = fixture.get("size_bytes")
        requirements[key] = _AssetRequirement(
            key=key,
            root="fixture_root",
            sha1=sha1.lower() if _is_sha1(sha1) else None,
            sha256=sha256.lower() if _is_sha256(sha256) else None,
            size=size if isinstance(size, int) and not isinstance(size, bool) else None,
        )
    return requirements


def _required_asset_entries(
    declaration: dict[str, Any], repo_root: Path
) -> dict[tuple[str, str], _AssetRequirement]:
    """Return the complete pinned ROM/SYM/fixture set for the declared scope."""

    assets = declaration.get("assets")
    assets = assets if isinstance(assets, dict) else {}
    scope = assets.get("scope")
    allowed: set[str] | None = None
    if isinstance(scope, list) and scope:
        allowed = {str(item).lower() for item in scope}

    def in_scope(key: str) -> bool:
        if allowed is None:
            return True
        return bool(allowed.intersection(Path(key).parts))

    requirements: dict[tuple[str, str], _AssetRequirement] = {}

    def add(requirement: _AssetRequirement) -> None:
        if in_scope(requirement.key):
            requirements[(requirement.root, requirement.key)] = requirement

    rom_pins, symbol_pins = _load_versions_pins(repo_root)
    for documented, sha1 in rom_pins.items():
        add(_AssetRequirement(key=_canonical_asset_key(documented), root="rom_root", sha1=sha1))
    for documented, sha1 in symbol_pins.items():
        add(_AssetRequirement(key=_canonical_asset_key(documented), root="rom_root", sha1=sha1))
    for requirement in _fixture_requirements(repo_root).values():
        add(requirement)
    return requirements


def _verify_required_asset(
    name: str, requirement: _AssetRequirement, resolved: Path
) -> CheckResult:
    relative = requirement.key
    if resolved.is_symlink() or not resolved.is_file():
        return _result(name, "fail", "required file", relative, "required input is missing")
    try:
        size = resolved.stat().st_size
        if size <= 0:
            return _result(name, "fail", "non-empty file", relative, "required input is empty")
        if requirement.size is not None and size != requirement.size:
            return _result(
                name,
                "fail",
                requirement.size,
                size,
                "required input size does not match the pin",
            )
        actual_sha1 = _sha1_of_file(resolved)
        actual_sha256 = _sha256_of_file(resolved) if requirement.sha256 else None
    except OSError as exc:
        return _result(
            name, "fail", "readable file", relative, f"required input is unreadable: {exc}"
        )
    if requirement.sha1 is not None and actual_sha1 != requirement.sha1:
        return _result(
            name,
            "fail",
            requirement.sha1,
            actual_sha1,
            "required input bytes do not match the pinned SHA-1",
        )
    if (
        requirement.sha256 is not None
        and actual_sha256 is not None
        and actual_sha256 != requirement.sha256
    ):
        return _result(
            name,
            "fail",
            requirement.sha256,
            actual_sha256,
            "required input bytes do not match the pinned SHA-256",
        )
    if requirement.sha1 is None and requirement.sha256 is None:
        return _result(name, "fail", "pinned hash", None, "required input has no verifiable pin")
    return _result(
        name, "ok", requirement.sha1 or requirement.sha256, actual_sha1, "required input verified"
    )


def validate_asset_inputs(declaration: dict[str, Any], repo_root: Path) -> list[CheckResult]:
    """Verify the complete pinned ROM/SYM/fixture set for the declared scope.

    Every repository pin for the declared scope must be present on disk and
    hash-valid.  Operator-declared inputs may only name files inside that pinned
    set; an unknown or unrelated path fails instead of being trusted.
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

    requirements = _required_asset_entries(declaration, repo_root)
    if not requirements:
        results.append(
            _result(
                "assets-required-set",
                "unsupported",
                "complete pinned input set",
                {},
                "the repository pins for the declared scope could not be resolved",
            )
        )
        return results

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

    required_keys = {(req.root, req.key) for req in requirements.values()}
    rom_keys = {key for root, key in required_keys if root == "rom_root"}
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
        key = _canonical_asset_key(relative)
        if ("rom_root", key) not in required_keys and ("fixture_root", key) not in required_keys:
            results.append(
                _result(
                    name,
                    "fail",
                    "a path in the pinned input set",
                    relative,
                    "asset input is not part of the declared qualification scope",
                )
            )
            continue
        if key in rom_keys:
            expected = requirements[("rom_root", key)].sha1
            if declared_sha1 is not None and _is_sha1(declared_sha1):
                if expected is not None and declared_sha1.lower() != expected:
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
            elif expected is not None:
                results.append(
                    _result(
                        name,
                        "fail",
                        expected,
                        declared_sha1,
                        "declared input must carry the pinned SHA-1",
                    )
                )
                continue
        results.append(_result(name, "ok", key, key, "asset input is in the pinned scope"))

    rom_root_path = rom_root if rom_root is not None and rom_root.is_dir() else None
    fixture_root_path = fixture_root if fixture_root is not None and fixture_root.is_dir() else None
    ordered = sorted(requirements.values(), key=lambda req: (req.root, req.key))
    for index, requirement in enumerate(ordered):
        name = f"assets-required-{requirement.root}-{index}"
        root_path = rom_root_path if requirement.root == "rom_root" else fixture_root_path
        if root_path is None:
            continue
        results.append(_verify_required_asset(name, requirement, root_path / requirement.key))
    return results


def _load_bootstrap_module() -> Any:
    path = Path(__file__).resolve().parent / "bootstrap_pyboy.py"
    try:
        spec = importlib.util.spec_from_file_location("_qualification_bootstrap_pyboy", path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 - absence of the bootstrap is reported, not raised
        return None


def _native_source_digest(repo_root: Path) -> str | None:
    """Recompute the deterministic native build-input digest from source.

    This mirrors the fresh-snapshot staging in ``scripts/bootstrap_pyboy.py`` so
    an installed extension build can be tied back to the pinned source.
    """

    bootstrap = _load_bootstrap_module()
    if bootstrap is None:
        return None
    source_root = repo_root / "vendor" / "pyboy-src"
    if source_root.is_symlink() or not source_root.is_dir():
        return None
    revision_file = source_root / "POKERED_HARNESS_PYBOY_REVISION"
    suffixes = bootstrap.NATIVE_INPUT_SUFFIXES
    digest = hashlib.sha256()
    try:
        for directory, children, filenames in os.walk(source_root):
            children[:] = sorted(
                name
                for name in children
                if not name.startswith(".")
                and name not in {"build", "dist", "__pycache__", "venv"}
                and not name.endswith(".egg-info")
            )
            for name in children:
                if (Path(directory) / name).is_symlink():
                    return None
            for name in sorted(filenames):
                source = Path(directory) / name
                if source.suffix not in suffixes and source != revision_file:
                    continue
                if source.is_symlink():
                    return None
                relative = source.relative_to(source_root)
                digest.update(relative.as_posix().encode("utf-8") + b"\0")
                digest.update(hashlib.sha256(source.read_bytes()).digest())
    except OSError:
        return None
    return digest.hexdigest()


def _native_build_evidence(
    declaration: dict[str, Any],
    repo_root: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> list[CheckResult]:
    """Tie the installed native runtime to a pinned, consistent source build."""

    results: list[CheckResult] = []
    interpreters = declaration.get("interpreters")
    interpreters = interpreters if isinstance(interpreters, dict) else {}
    native = interpreters.get("native")
    expected_inputs = interpreters.get("native_build_inputs_sha256")
    expected_fingerprint = interpreters.get("native_fingerprint")
    if not native:
        results.append(
            _result(
                "native-build-inputs", "fail", "native interpreter", None, "missing interpreter"
            )
        )
        return results

    source_digest = _native_source_digest(repo_root)
    if source_digest is None:
        results.append(
            _result(
                "native-build-inputs",
                "unsupported",
                "pinned vendored PyBoy source",
                None,
                "the vendored source snapshot is unavailable for a consistent-build check",
            )
        )
    elif not _is_sha256(expected_inputs):
        results.append(
            _result(
                "native-build-inputs",
                "fail",
                "pinned native build-inputs sha256",
                expected_inputs,
                "the expected build-inputs digest is not pinned in the declaration",
            )
        )
    elif source_digest != expected_inputs.strip().lower():
        results.append(
            _result(
                "native-build-inputs",
                "fail",
                expected_inputs,
                source_digest,
                "the vendored native build inputs do not match the pinned digest",
            )
        )
    else:
        results.append(
            _result(
                "native-build-inputs",
                "ok",
                expected_inputs,
                source_digest,
                "the vendored source matches the pinned native build-inputs digest",
            )
        )

    probe = runner([str(native), "-c", _NATIVE_PROBE % {"modules": _RUNTIME_MODULES}], repo_root)
    if probe.returncode != 0:
        results.append(
            _result(
                "native-runtime-fingerprint",
                "fail",
                "installed native fingerprint",
                probe.returncode,
                (probe.stdout + probe.stderr).strip()[-300:],
            )
        )
        return results
    try:
        payload = json.loads(probe.stdout)
    except (TypeError, ValueError):
        results.append(
            _result(
                "native-runtime-fingerprint",
                "fail",
                "fingerprint JSON",
                probe.stdout.strip()[-120:],
                "the native runtime probe did not report parseable JSON",
            )
        )
        return results
    identity = payload.get("identity") if isinstance(payload, dict) else None
    fingerprint = payload.get("fingerprint") if isinstance(payload, dict) else None
    if not isinstance(identity, dict) or not _is_sha256(fingerprint):
        results.append(
            _result(
                "native-runtime-fingerprint",
                "fail",
                "fingerprint JSON",
                None,
                "the native runtime probe did not report an identity and fingerprint",
            )
        )
        return results

    bootstrap = _load_bootstrap_module()
    expected_revision = getattr(bootstrap, "EXPECTED_REVISION", None)
    problems: list[str] = []
    if identity.get("revision") != expected_revision:
        problems.append("the installed extension revision does not match the pinned fork")
    if identity.get("cython_compiled") is not True:
        problems.append("the installed runtime does not report compiled extensions")
    modules = identity.get("modules")
    modules = modules if isinstance(modules, dict) else {}
    for name in _EXTENSION_BACKED_MODULES:
        entry = modules.get(name)
        kind = entry.get("kind") if isinstance(entry, dict) else None
        if kind != "cython":
            problems.append(f"{name} is not an installed extension")
    if problems:
        results.append(
            _result(
                "native-runtime-fingerprint",
                "fail",
                "installed extensions with a pinned fingerprint",
                fingerprint,
                "; ".join(problems),
            )
        )
    elif not _is_sha256(expected_fingerprint):
        results.append(
            _result(
                "native-runtime-fingerprint",
                "fail",
                "pinned native runtime fingerprint",
                expected_fingerprint,
                "the expected native runtime fingerprint is not pinned in the declaration",
            )
        )
    elif fingerprint.lower() != expected_fingerprint.strip().lower():
        results.append(
            _result(
                "native-runtime-fingerprint",
                "fail",
                expected_fingerprint,
                fingerprint,
                "the installed native runtime does not match the pinned fingerprint",
            )
        )
    else:
        results.append(
            _result(
                "native-runtime-fingerprint",
                "ok",
                expected_fingerprint,
                fingerprint,
                "the installed native runtime matches the pinned consistent-build fingerprint",
            )
        )
    return results


def _asset_tree_writable(root: Path) -> bool:
    if os.access(root, os.W_OK):
        return True
    try:
        for entry in root.rglob("*"):
            if entry.is_file() and os.access(entry, os.W_OK):
                return True
    except OSError:
        return True
    return False


def _immutable_asset_checks(declaration: dict[str, Any], repo_root: Path) -> list[CheckResult]:
    assets = declaration.get("assets")
    assets = assets if isinstance(assets, dict) else {}
    results: list[CheckResult] = []
    for key in ("rom_root", "fixture_root"):
        value = assets.get(key)
        path = Path(value) if isinstance(value, str) and value else None
        if path is None or not path.is_dir():
            continue
        writable = _asset_tree_writable(path)
        results.append(
            _result(
                f"assets-immutable-{key}",
                "fail" if writable else "ok",
                "read-only shared asset root",
                path.name,
                "the asset root is writable by this job; mount it read-only"
                if writable
                else "the asset root is read-only for this job",
            )
        )
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
    results.extend(_immutable_asset_checks(declaration, repo_root))
    results.extend(_native_build_evidence(declaration, repo_root, runner))
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


_PATH_FRAGMENT_RE = re.compile(
    r"(?<![\w./-])(/(?:[^\s:'\"()\[\],;]+/)*"
    r"(?:lib/python[0-9.]*|site-packages|dist-packages|bin/python[0-9.]*)"
    r"(?:/[^\s:'\"()\[\],;]+)*)"
)


def _scrub_absolute_paths(text: str) -> str:
    return _PATH_FRAGMENT_RE.sub("<redacted-path>", text)


def _redact_text(text: str, redactions: dict[str, str]) -> str:
    for token in sorted(redactions, key=len, reverse=True):
        if token:
            text = text.replace(token, redactions[token])
    return _scrub_absolute_paths(text)


def _redact_payload(value: Any, redactions: dict[str, str]) -> Any:
    if isinstance(value, str):
        return _redact_text(value, redactions)
    if isinstance(value, list):
        return [_redact_payload(item, redactions) for item in value]
    if isinstance(value, dict):
        return {key: _redact_payload(item, redactions) for key, item in value.items()}
    return value


def _collect_redactions(
    repo_root: Path, declaration: dict[str, Any] | None, declaration_path: Path | None
) -> dict[str, str]:
    redactions = {str(repo_root): _sanitize_path(repo_root, repo_root)}

    def register(value: Any) -> None:
        if isinstance(value, str) and value.strip():
            redactions[value] = _sanitize_path(value, repo_root)

    if declaration_path is not None:
        register(str(declaration_path))
    if isinstance(declaration, dict):
        assets = declaration.get("assets")
        if isinstance(assets, dict):
            for key in ("rom_root", "fixture_root"):
                register(assets.get(key))
            inputs = assets.get("inputs")
            if isinstance(inputs, list):
                for entry in inputs:
                    if isinstance(entry, dict):
                        register(entry.get("path"))
        interpreters = declaration.get("interpreters")
        if isinstance(interpreters, dict):
            for value in interpreters.values():
                if not isinstance(value, str) or not value.strip():
                    continue
                register(value)
                candidate = Path(value)
                register(str(candidate.parent))
                register(str(candidate.parent.parent))
                try:
                    resolved = candidate.resolve()
                except (OSError, RuntimeError, ValueError):
                    continue
                register(str(resolved))
                register(str(resolved.parent))
                register(str(resolved.parent.parent))
        reservation = declaration.get("reservation")
        if isinstance(reservation, dict):
            for key in ("descriptor_path", "job_dir", "exclusive_marker_path"):
                register(reservation.get(key))
    return redactions


def _job_directory(args: argparse.Namespace, declaration: dict[str, Any], repo_root: Path) -> Path:
    if getattr(args, "job_dir", None) is not None:
        candidate = Path(args.job_dir).expanduser()
        return candidate if candidate.is_absolute() else repo_root / candidate
    reservation = declaration.get("reservation")
    if isinstance(reservation, dict):
        declared = _resolve_declared_path(reservation.get("job_dir"), repo_root)
        if declared is not None:
            return declared
    runner_id = str(declaration.get("runner_id") or "default")
    return repo_root / "target" / "qualification-runs" / runner_id


def _prepare_job_directory(job_dir: Path) -> None:
    """Create the owner-only per-job layout without touching shared assets."""

    job_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(job_dir, 0o700)
    for name in ("tmp", "evidence", "logs"):
        child = job_dir / name
        child.mkdir(exist_ok=True)
        os.chmod(child, 0o700)


def _holder_is_owned(holder: Any, start_time: Any) -> bool:
    if not isinstance(holder, int) or isinstance(holder, bool) or holder <= 0:
        return False
    if start_time != _process_start_time(holder):
        return False
    try:
        raw = Path(f"/proc/{holder}/cmdline").read_bytes()
    except OSError:
        return False
    command = b" ".join(part for part in raw.split(b"\0") if part).decode("utf-8", "replace")
    return "qualification_runner" in command or "qualification-runner" in command


def _reserve_allocation(
    declaration: dict[str, Any], repo_root: Path, job_dir: Path, facts: RunnerFacts
) -> tuple[Path, dict[str, Any], int]:
    import fcntl

    reservation = declaration.get("reservation")
    reservation = reservation if isinstance(reservation, dict) else {}
    mechanism = declaration.get("reservation_mechanism")
    _prepare_job_directory(job_dir)
    lock_path = job_dir / "allocation.lock"
    lease_id = uuid.uuid4().hex
    try:
        cpuset = parse_cpuset(declaration.get("affinity_cpus"))
    except (TypeError, ValueError):
        cpuset = []
    quota = declaration.get("cpu_quota_cores")
    if quota is None:
        quota = facts.cpu_quota_cores
    weight = declaration.get("cpu_weight")
    if weight is None:
        weight = facts.cpu_weight
    descriptor: dict[str, Any] = {
        "descriptor_version": _DESCRIPTOR_VERSION,
        "allocation_id": declaration.get("runner_id"),
        "runner_id": declaration.get("runner_id"),
        "mechanism": mechanism,
        "state": "held",
        "lease_id": lease_id,
        "holder_pid": os.getpid(),
        "holder_start_time": _process_start_time(os.getpid()),
        "lock_path": str(lock_path),
        "cgroup_path": reservation.get("cgroup_path") or facts.cgroup_relative_path,
        "cpuset": cpuset,
        "cpu_quota_cores": quota,
        "cpu_weight": weight,
        "logical_cpus": declaration.get("logical_cpus"),
        "exclusive": bool(reservation.get("exclusive", mechanism == "dedicated-host")),
        "created_at": _iso_now(),
    }
    if mechanism == "dedicated-host":
        marker = job_dir / "exclusive.marker"
        marker.write_text(lease_id, encoding="utf-8")
        os.chmod(marker, 0o400)
        descriptor["exclusive_marker_path"] = str(marker)
    descriptor_path = job_dir / "allocation.json"
    descriptor_path.write_text(
        json.dumps(descriptor, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.chmod(descriptor_path, 0o400)
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return descriptor_path, descriptor, lock_fd


def _pin_declaration(
    declaration_path: Path,
    declaration: dict[str, Any],
    job_dir: Path,
    descriptor_path: Path,
) -> str:
    reservation = declaration.get("reservation")
    if not isinstance(reservation, dict):
        reservation = {}
        declaration["reservation"] = reservation
    reservation["job_dir"] = str(job_dir)
    reservation["descriptor_path"] = str(descriptor_path)
    digest = _sha256_of_file(descriptor_path)
    reservation["descriptor_sha256"] = digest
    declaration_path.write_text(
        json.dumps(declaration, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return digest


def _allocation_descriptor_path(declaration: dict[str, Any], repo_root: Path) -> Path | None:
    reservation = declaration.get("reservation")
    if not isinstance(reservation, dict):
        return None
    return _resolve_declared_path(reservation.get("descriptor_path"), repo_root)


def _remove_allocation_state(descriptor: dict[str, Any], descriptor_path: Path) -> None:
    for key in ("lock_path", "exclusive_marker_path"):
        raw = descriptor.get(key)
        if isinstance(raw, str) and raw:
            try:
                Path(raw).unlink()
            except OSError:
                pass
    try:
        descriptor_path.unlink()
    except OSError:
        pass


def _release_allocation(declaration: dict[str, Any], repo_root: Path) -> tuple[str, str]:
    descriptor_path = _allocation_descriptor_path(declaration, repo_root)
    if descriptor_path is None or not descriptor_path.is_file():
        return "blocked", "no pinned allocation descriptor to release"
    descriptor, error = _load_allocation_descriptor(descriptor_path)
    if descriptor is None:
        return "fail", error
    holder = descriptor.get("holder_pid")
    owned = _holder_is_owned(holder, descriptor.get("holder_start_time"))
    if owned and holder != os.getpid():
        try:
            os.kill(holder, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + _CHILD_TERMINATION_GRACE_SECONDS
        while time.monotonic() < deadline and _pid_alive(holder):
            time.sleep(0.05)
        if _pid_alive(holder):
            try:
                os.kill(holder, signal.SIGKILL)
            except OSError:
                pass
    _remove_allocation_state(descriptor, descriptor_path)
    if owned:
        return "ok", "the owned lease holder was terminated and the lease removed"
    return "fail", "the recorded holder is not an owned qualification-runner lease"


def _recover_allocation(declaration: dict[str, Any], repo_root: Path) -> tuple[str, str]:
    descriptor_path = _allocation_descriptor_path(declaration, repo_root)
    if descriptor_path is None or not descriptor_path.is_file():
        return "ok", "no allocation descriptor to recover"
    descriptor, error = _load_allocation_descriptor(descriptor_path)
    if descriptor is None:
        try:
            descriptor_path.unlink()
        except OSError:
            pass
        return "ok", f"removed a stale descriptor ({error})"
    holder = descriptor.get("holder_pid")
    lock_holders: set[int] = set()
    raw = descriptor.get("lock_path")
    if isinstance(raw, str) and raw:
        lock_holders = _flock_holder_pids(Path(raw)) or set()
    if _holder_is_owned(holder, descriptor.get("holder_start_time")) and holder in lock_holders:
        return "fail", "an active owned lease is still held; release it first"
    _remove_allocation_state(descriptor, descriptor_path)
    return "ok", "removed stale allocation state whose owner is gone"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a declared qualification-runner allocation."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="verify declaration and prerequisites")
    mode.add_argument("--report", action="store_true", help="print observed host facts only")
    mode.add_argument("--setup", action="store_true", help="prepare the private job directory")
    mode.add_argument("--reserve", action="store_true", help="reserve and hold the allocation")
    mode.add_argument("--release", action="store_true", help="release an owned lease")
    mode.add_argument("--recover", action="store_true", help="remove only stale lease state")
    parser.add_argument("--declaration", type=Path, default=None)
    parser.add_argument("--job-dir", type=Path, default=None)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true", help="emit a structured JSON report")
    parser.add_argument(
        "--run",
        nargs=argparse.REMAINDER,
        default=[],
        help="command to run under a held lease (must be last)",
    )
    return parser


def _emit(payload: dict[str, Any], args: argparse.Namespace) -> None:
    print(json.dumps(payload, indent=2) if args.json else render_text(payload))


def _do_reserve(
    args: argparse.Namespace,
    declaration: dict[str, Any],
    declaration_path: Path,
    repo_root: Path,
    facts: RunnerFacts,
) -> tuple[str, str, dict[str, Any]]:
    job_dir = _job_directory(args, declaration, repo_root)
    try:
        descriptor_path, _descriptor, _lock_fd = _reserve_allocation(
            declaration, repo_root, job_dir, facts
        )
        digest = _pin_declaration(declaration_path, declaration, job_dir, descriptor_path)
    except OSError as exc:
        return "fail", f"could not reserve the allocation: {exc}", {}
    extra = {
        "job_dir": _sanitize_path(job_dir, repo_root),
        "descriptor": _sanitize_path(descriptor_path, repo_root),
        "descriptor_sha256": digest,
    }
    if not args.run:
        return (
            "blocked",
            (
                "descriptor written and pinned; rerun with --run <command> to hold the lease "
                "while the qualification job executes (a bare reserve releases the lease)"
            ),
            extra,
        )
    command = list(args.run)
    process = run_command(command, repo_root)
    if process.stdout:
        print(process.stdout, end="")
    if process.stderr:
        print(process.stderr, end="", file=sys.stderr)
    _release_allocation(declaration, repo_root)
    status = "ok" if process.returncode == 0 else "fail"
    return status, f"leased command exited {process.returncode}", extra


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _install_signal_handlers()
    repo_root = args.repo_root.resolve()
    facts = collect_facts(repo_root)
    facts_payload = asdict(facts)
    facts_payload["shm_path"] = _sanitize_path(facts.shm_path, repo_root)
    mode = "report"
    for candidate in ("check", "setup", "reserve", "release", "recover"):
        if getattr(args, candidate):
            mode = candidate
            break
    payload: dict[str, Any] = {
        "mode": mode,
        "declaration_version": SCHEMA_VERSION,
        "repo_root": _sanitize_path(repo_root, repo_root),
        "checks": [],
        "facts": facts_payload,
        "overall": "report" if mode == "report" else "blocked",
    }

    if mode == "report":
        payload = _redact_payload(payload, _collect_redactions(repo_root, None, None))
        _emit(payload, args)
        return 0

    declaration_path = args.declaration or (
        Path(os.environ[_DECLARATION_ENV]) if os.environ.get(_DECLARATION_ENV) else None
    )
    checks: list[CheckResult] = []
    declaration: dict[str, Any] | None = None
    if declaration_path is None:
        payload["message"] = (
            "no declaration provided; set --declaration or "
            f"{_DECLARATION_ENV}. Actual capacity requires an operator-owned "
            "allocation (see issue #85)."
        )
    else:
        declaration, error = load_declaration(declaration_path)
        payload["declaration"] = _sanitize_path(declaration_path, repo_root)
        if declaration is None:
            payload["message"] = error
        elif mode == "check":
            checks.extend(validate_declaration(declaration))
            if overall_status(checks) == "ok":
                checks.extend(evaluate_resources(declaration, facts, repo_root))
                checks.extend(prerequisite_checks(declaration, repo_root))
        elif mode == "setup":
            job_dir = _job_directory(args, declaration, repo_root)
            try:
                _prepare_job_directory(job_dir)
                ready = True
            except OSError as exc:
                ready = False
                payload["message"] = f"could not prepare the job directory: {exc}"
            checks.append(
                _result(
                    "job-directory",
                    "ok" if ready and _directory_is_private(job_dir) else "fail",
                    "owner-only private job directory",
                    job_dir.name,
                    "private per-job directory prepared"
                    if ready
                    else "private per-job directory could not be prepared",
                )
            )
            payload["job_dir"] = _sanitize_path(job_dir, repo_root)
            checks.extend(prerequisite_checks(declaration, repo_root))
            payload["native"] = {
                result.name: result.observed
                for result in checks
                if result.name in {"native-build-inputs", "native-runtime-fingerprint"}
            }
        elif mode == "reserve":
            status, message, extra = _do_reserve(
                args, declaration, declaration_path, repo_root, facts
            )
            payload["overall"] = status
            payload["message"] = message
            payload.update(extra)
            payload = _redact_payload(
                payload, _collect_redactions(repo_root, declaration, declaration_path)
            )
            _emit(payload, args)
            return 0 if status == "ok" else 1
        elif mode == "release":
            status, message = _release_allocation(declaration, repo_root)
            payload["message"] = message
            payload["overall"] = status
        elif mode == "recover":
            status, message = _recover_allocation(declaration, repo_root)
            payload["message"] = message
            payload["overall"] = status

    if mode in {"check", "setup"}:
        payload["overall"] = overall_status(checks) if declaration is not None else "blocked"
    payload["checks"] = [asdict(item) for item in checks]
    payload = _redact_payload(
        payload, _collect_redactions(repo_root, declaration, declaration_path)
    )
    _emit(payload, args)
    return 0 if payload["overall"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
