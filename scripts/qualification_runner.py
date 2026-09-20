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
import contextlib
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
from typing import Any, Self

SCHEMA_VERSION = 3
_DECLARATION_ENV = "POKERED_QUALIFICATION_DECLARATION"
_RESERVATION_MECHANISMS = ("dedicated-host", "cgroup-quota", "cpuset-affinity")
_CPU_BINDING_MECHANISMS = ("dedicated-host", "cpuset-affinity")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DESCRIPTOR_VERSION = 1
_PROBE_TIMEOUT_ENV = "POKERED_QUALIFICATION_COMMAND_TIMEOUT_SECONDS"
_QUALIFICATION_TIMEOUT_ENV = "POKERED_QUALIFICATION_RUN_TIMEOUT_SECONDS"
_DEFAULT_PROBE_TIMEOUT_SECONDS = 300.0
_DEFAULT_QUALIFICATION_TIMEOUT_SECONDS = 86400.0
_NATIVE_BUILD_EVIDENCE_PROCEDURE = "bootstrap_pyboy --mode cython"
_NATIVE_BUILD_EVIDENCE_VERSION = 2
_TIMEOUT_RETURNCODE = 124
_CHILD_TERMINATION_GRACE_SECONDS = 5.0
_FOREIGN_SAMPLE_ENV = "POKERED_QUALIFICATION_FOREIGN_SAMPLE_SECONDS"
_DEFAULT_FOREIGN_SAMPLE_SECONDS = 2.0
_DEFAULT_FOREIGN_CPU_CORES_TOLERANCE = 0.25

# Descriptors this process holds open for an allocation lease, keyed by the
# lock's kernel identity.  Release must explicitly close them: removing the
# state file while a descriptor still pins the flock would report a released
# lease that this process still holds.
_HELD_LEASE_FDS: dict[tuple[int, int], list[int]] = {}
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
from pathlib import Path

names = %(modules)r
try:
    import pyboy
except Exception as exc:
    print(json.dumps({"error": f"pyboy: {type(exc).__name__}: {exc}"}))
    raise SystemExit(2)

package_root = Path(pyboy.__file__).resolve().parent


def _relative(filename):
    path = Path(filename)
    try:
        return path.resolve().relative_to(package_root).as_posix()
    except ValueError:
        return path.name


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
    report[name] = {
        "kind": kind,
        "sha256": digest,
        "artifact": _relative(filename) if filename else None,
    }

# The complete installed output set, not only the named entry modules: a mixed
# build can replace a compiled module the module list never names (for example
# ``pyboy/core/cpu*.so``) while every listed module stays byte-identical.
artifacts = {}
for entry in sorted(package_root.rglob("*")):
    if "__pycache__" in entry.parts or entry.suffix in (".pyc", ".pyo"):
        continue
    if not entry.is_file():
        continue
    try:
        artifacts[entry.relative_to(package_root).as_posix()] = hashlib.sha256(
            entry.read_bytes()
        ).hexdigest()
    except OSError:
        print(json.dumps({"error": f"unreadable installed artifact: {entry.name}"}))
        raise SystemExit(3)

from pyboy import utils

identity = {
    "python": sys.version.split()[0],
    "version": getattr(pyboy, "__version__", None),
    "revision": getattr(pyboy, "__pokered_harness_revision__", None),
    "cython_compiled": bool(getattr(utils, "cython_compiled", False)),
    "modules": report,
    "artifacts": artifacts,
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
        # Visibility is restricted, but the pid still exists; a zombie check
        # needs /proc/<pid>/stat, which may itself be unreadable.
        return not _pid_is_zombie(pid)
    return not _pid_is_zombie(pid)


def _pid_is_zombie(pid: int) -> bool:
    """Return ``True`` when *pid* has exited but has not been reaped.

    As a child subreaper this process is the parent of adopted orphans, so an
    exited orphan stays visible as a zombie until it is waited on.  A zombie
    consumes no CPU, so it must not count as live capacity.
    """

    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    closing = raw.rfind(")")
    if closing == -1:
        return False
    fields = raw[closing + 1 :].split()
    return bool(fields) and fields[0] == "Z"


def _reap_zombie(pid: int) -> None:
    """Best-effort reap of an adopted zombie child."""

    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        return


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


def _observed_affinity(pid: int) -> list[int] | None:
    """Return *pid*'s allowed CPU list, or ``None`` when it is unobservable."""

    text = _read_text(Path(f"/proc/{pid}/status"))
    if text is None:
        return None
    for line in text.splitlines():
        if line.startswith("Cpus_allowed_list:"):
            _, _, value = line.partition(":")
            try:
                return parse_cpuset(value.strip())
            except (TypeError, ValueError):
                return None
    return None


def _pid_exists(pid: int) -> bool:
    """Return whether *pid* names a live or zombie process, not its affinity."""

    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # PermissionError and friends: the entry exists but is not inspectable,
        # which is exactly the case that must not be read as "no competitor".
        return True
    return True


def _observed_task_affinities(pid: int) -> list[int] | None:
    """Return the union of every thread's allowed CPUs for *pid*.

    ``Cpus_allowed_list`` in ``/proc/<pid>/status`` reports only the process
    leader.  A foreign process can confine its leader to one CPU while a
    worker thread remains allowed on the reserved CPU, so reading the leader
    alone would miss a real competitor.  Every thread under
    ``/proc/<pid>/task`` is inspected and the allowed sets are unioned.

    ``None`` means at least one thread's affinity could not be observed, so
    the caller must fail closed rather than assume the reserved CPUs are free.
    """

    task_dir = Path(f"/proc/{pid}/task")
    try:
        entries = list(task_dir.iterdir())
    except OSError:
        return None
    if not entries:
        return None
    union: set[int] = set()
    for entry in entries:
        if not entry.name.isdigit():
            continue
        cpus = _observed_affinity(int(entry.name))
        if cpus is None:
            return None
        union.update(cpus)
    if not union:
        return None
    return sorted(union)


def _foreign_process_affinity() -> dict[int, list[int]] | None:
    """Return the allowed CPUs of every live process outside this job's tree.

    A cpuset is only an exclusive reservation when nothing else is allowed to
    run on it.  Affinity alone restricts where *this* process may run; it does
    not move a competitor off those CPUs.  Enumerating ``/proc`` and comparing
    each foreign process's ``Cpus_allowed_list`` against the declared cpuset is
    the observable evidence that the CPUs are not shared.

    ``None`` means competing affinity could not be fully observed, so the
    caller must fail closed.  An unreadable process is *not* evidence that the
    CPUs are free: omitting it silently would turn a permission error into a
    passing reservation.  Only a process that has genuinely exited or that is
    an exited zombie is skipped, because neither can consume CPU time.  The
    reported set for each process spans every thread, since a leader confined
    away from the reserved CPUs can still own a worker that is allowed on
    them.
    """

    owned = set(_process_tree_pids(os.getpid()))
    foreign: dict[int, list[int]] = {}
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in owned or pid == os.getpid():
            continue
        if _pid_is_zombie(pid):
            continue
        cpus = _observed_task_affinities(pid)
        if cpus is None:
            if _pid_exists(pid):
                return None
            continue
        foreign[pid] = cpus
    return foreign


def _host_cpu_totals() -> tuple[int, int] | None:
    """Return ``(busy_ticks, total_ticks)`` across all CPUs from ``/proc/stat``.

    ``/proc/stat`` is kernel-wide accounting, so it is independent of which
    process tree a competing workload belongs to.  Per-pid enumeration would
    miss a competitor spawned as a descendant of the measuring process, which
    is exactly how a reviewer-supplied negative control is usually launched.
    """

    raw = _read_text(Path("/proc/stat"))
    if raw is None:
        return None
    for line in raw.splitlines():
        if not line.startswith("cpu "):
            continue
        fields = line.split()[1:]
        if len(fields) < 4:
            return None
        try:
            values = [int(value) for value in fields]
        except ValueError:
            return None
        # user, nice, system, idle, iowait, irq, softirq, steal, guest...
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        return total - idle, total
    return None


def _measure_competing_cpu_cores(
    interval: float | None = None, clock_ticks: int | None = None
) -> tuple[float | None, float]:
    """Measure host-wide busy CPU cores over *interval* seconds.

    "Dedicated host" is a claim about competing load, not just about this
    process's affinity.  Admission samples the host before the job starts, so
    any busy cores observed belong to something other than this job.  Returns
    ``(cores_or_None, measured_interval)``.
    """

    interval = _foreign_sample_seconds() if interval is None else interval
    hz = clock_ticks if clock_ticks is not None else _clock_ticks_per_second()
    first = _host_cpu_totals()
    started = time.monotonic()
    time.sleep(interval)
    elapsed = time.monotonic() - started
    second = _host_cpu_totals()
    if first is None or second is None or hz <= 0 or elapsed <= 0:
        return None, elapsed
    busy = max(0, second[0] - first[0])
    total = max(0, second[1] - first[1])
    if total <= 0:
        return None, elapsed
    return busy / (hz * elapsed), elapsed


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


def _observe_flock_holders(lock_path: Path) -> set[int] | None:
    """Observe the flock holders for *lock_path*.

    A missing lock file is a positive observation that no process holds the
    lock; an unreadable file or lock table returns ``None`` so callers fail
    closed instead of assuming the lease is free.
    """

    if lock_path.is_symlink():
        return None
    try:
        exists = lock_path.exists()
    except OSError:
        return None
    if not exists:
        return set()
    return _flock_holder_pids(lock_path)


def _lock_identity(path: Path) -> dict[str, Any] | None:
    """Return the stable kernel identity of an allocation lock file.

    Path equality is not lock identity: a recreated pathname is a different
    inode, so two allocations that each create "the same" lock path can hold
    independent locks.  Recording ``(device, inode)`` lets a later check prove
    that the observed lock is the very inode whose bytes were pinned, rather
    than a look-alike that merely shares a name.
    """

    try:
        info = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    return {"device": info.st_dev, "inode": info.st_ino}


def _lock_identity_matches(expected: Any, observed: dict[str, Any] | None) -> bool:
    if not isinstance(expected, dict) or observed is None:
        return False
    return expected.get("device") == observed.get("device") and expected.get(
        "inode"
    ) == observed.get("inode")


def _lock_owned_by_this_process(lock_path: Path) -> bool:
    """Report whether this process holds the flock, re-observed from the kernel."""

    holders = _observe_flock_holders(lock_path)
    return holders is not None and os.getpid() in holders


def _close_held_lease_descriptors(identity: dict[str, Any] | None) -> list[int]:
    """Close and report the lease descriptors this process opened for *identity*.

    The lock file itself is never unlinked, so relinquishing the lease means
    releasing the kernel lock.  Returning the closed descriptors lets the
    caller confirm from the lock table that this process no longer holds it.
    """

    if not isinstance(identity, dict):
        return []
    key = (identity.get("device"), identity.get("inode"))
    closed: list[int] = []
    for fd in _HELD_LEASE_FDS.pop(key, []):
        try:
            os.close(fd)
        except OSError:
            continue
        closed.append(fd)
    return closed


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


def _cgroup_memory_limit(paths: list[Path], filename: str) -> tuple[int | None, bool]:
    """Return the tightest finite memory limit for *paths* and its observability.

    The second element is ``False`` when a limit file exists but could not be
    read.  Admission must treat that as an unknown bound rather than as an
    unlimited one, because a job admitted against an unknown limit can be
    started with no headroom at all.
    """

    limits: list[int] = []
    observable = True
    for path in paths:
        candidate = path / filename
        if not candidate.exists():
            continue
        raw = _read_text(candidate)
        if raw is None:
            observable = False
            continue
        value = raw.strip()
        if not value or value == "max" or not value.isdigit():
            # ``max`` (cgroup v2) and the v1 sentinel mean this level imposes
            # no finite limit; the walk continues to the other ancestors.
            continue
        number = int(value)
        if number >= 1 << 60:
            continue
        limits.append(number)
    return (min(limits) if limits else None), observable


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
    memory_limit_bytes: int | None = None
    memory_limit_observable = True

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

    # The memory bound is read independently of the CPU controller: a hierarchy
    # that exposes ``memory.max`` without a delegated ``cpu.max`` still bounds
    # the allocation, and treating that as unbounded would admit a job the
    # cgroup will kill.
    if version == "v1":
        memory_paths = _iter_cgroup_paths(
            base / "memory", _cgroup_relative_path("memory", cgroup_text)
        )
        memory_filename = "memory.limit_in_bytes"
    else:
        memory_paths = v2_paths
        memory_filename = "memory.max"
    memory_limit_bytes, memory_limit_observable = _cgroup_memory_limit(
        memory_paths, memory_filename
    )

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
        "memory_limit_bytes": memory_limit_bytes,
        "memory_limit_observable": memory_limit_observable,
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


def _cgroup_descendant_counts(cgroup_dir: str | None) -> dict[str, int] | None:
    """Return ``cgroup.stat`` counters for *cgroup_dir*, or ``None`` if unreadable.

    A quota that applies to one cgroup says nothing about sibling or child
    cgroups that are allowed to run on the same CPUs.  ``cgroup.stat`` reports
    how many descendants exist below the allocation cgroup; any non-zero count
    means competing workloads can consume the same CPU time, so a finite
    ``cpu.max`` there is a ceiling, not a reservation.
    """

    if not cgroup_dir:
        return None
    raw = _read_text(Path(cgroup_dir) / "cgroup.stat")
    if raw is None:
        return None
    counters: dict[str, int] = {}
    for line in raw.splitlines():
        key, _, value = line.partition(" ")
        value = value.strip()
        if key and value.isdigit():
            counters[key] = int(value)
    return counters or None


def _is_cgroup_directory(directory: Path) -> bool | None:
    """Return whether *directory* exposes the cgroup kernel interface files.

    ``None`` means the directory could not be inspected, which the caller must
    propagate as unobservable rather than treat as "not a cgroup" (which would
    silently end the ancestor walk and hide competing cgroups).
    """

    try:
        entries = list(directory.iterdir())
    except OSError:
        return None
    return any(entry.name.startswith("cgroup.") for entry in entries)


def _cgroup_subtree_populated(directory: Path, seen: set[tuple[int, int]]) -> bool | None:
    """Return whether *directory* or any descendant cgroup holds a live process.

    ``None`` means the population could not be established, so the caller must
    fail closed.  Checking only a sibling's own ``cgroup.procs`` is not enough:
    a sibling scope often holds no processes directly while its children do,
    and those children compete for the same CPUs as the declared allocation.
    """

    try:
        stats = directory.stat()
    except OSError:
        return None
    key = (stats.st_dev, stats.st_ino)
    if key in seen:
        # A bind-mount or symlink cycle must not loop forever.
        return False
    seen.add(key)
    raw = _read_text(directory / "cgroup.procs")
    if raw is None:
        return None
    if raw.strip():
        return True
    try:
        entries = sorted(directory.iterdir())
    except OSError:
        return None
    for entry in entries:
        if not entry.is_dir():
            continue
        entry_is_cgroup = _is_cgroup_directory(entry)
        if entry_is_cgroup is None:
            # An unreadable nested directory cannot be classified.  Treating it
            # as "not a cgroup" would silently drop a competing subtree, so the
            # population of this subtree is unobservable and the caller must
            # fail closed.
            return None
        if not entry_is_cgroup:
            continue
        nested = _cgroup_subtree_populated(entry, seen)
        if nested is None:
            return None
        if nested:
            return True
    return False


def _cgroup_sibling_competitors(cgroup_dir: str | None) -> list[str] | None:
    """Return cgroups outside the allocation that contain live processes.

    A CPU quota limits the cgroup it is set on; it does not stop a sibling
    cgroup on the same CPUs from consuming run queue time.  The declared
    allocation is only exclusive when no cgroup outside the allocation subtree
    holds a process.  That means checking the *whole* subtree of every sibling
    at every ancestor level, because a quota sibling or an ancestor's sibling
    can be empty directly while its descendants are busy.

    An ancestor's *own* ``cgroup.procs`` is inspected as well: a process can be
    recorded directly in an ancestor (the hierarchy root is the common case),
    where it is a competitor that no sibling scan would ever see.  The
    allocation's own directory is skipped, because the runner and the command
    it owns legitimately live there.

    ``None`` means the cgroup tree could not be enumerated, so the caller must
    fail closed rather than assume exclusivity.
    """

    if not cgroup_dir:
        return None
    current = Path(cgroup_dir)
    competitors: list[str] = []
    seen: set[tuple[int, int]] = set()
    while True:
        parent = current.parent
        if parent == current:
            # Reached the filesystem root; there is nothing above it.
            break
        parent_is_cgroup = _is_cgroup_directory(parent)
        if parent_is_cgroup is None:
            return None
        if not parent_is_cgroup:
            # The parent is outside the cgroup hierarchy, so the walk is done.
            break
        parent_own = _read_text(parent / "cgroup.procs")
        if parent_own is None:
            return None
        if parent_own.strip():
            # A process recorded directly in an ancestor competes for the same
            # CPUs as the allocation, whether or not that ancestor has any
            # populated descendant cgroup.
            competitors.append(parent.name)
        try:
            entries = sorted(parent.iterdir())
        except OSError:
            return None
        for entry in entries:
            if entry == current or not entry.is_dir():
                continue
            entry_is_cgroup = _is_cgroup_directory(entry)
            if entry_is_cgroup is None:
                return None
            if not entry_is_cgroup:
                continue
            populated = _cgroup_subtree_populated(entry, seen)
            if populated is None:
                return None
            if populated:
                competitors.append(entry.name)
        current = parent
    return sorted(set(competitors))


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
    cgroup_descendants: dict[str, int] | None = None
    cgroup_sibling_competitors: list[str] | None = None
    cpu_quota_cores: float | None = None
    cpu_weight: int | None = None
    cpu_throttled: dict[str, int] | None = None
    memory_total_bytes: int | None = None
    memory_available_bytes: int | None = None
    memory_limit_bytes: int | None = None
    memory_limit_observable: bool = True
    load_average: list[float] | None = None
    psi_cpu_some_avg300: float | None = None
    repo_disk_free_bytes: int | None = None
    temp_disk_free_bytes: int | None = None
    job_tmp_path: str | None = None
    job_tmp_disk_free_bytes: int | None = None
    job_evidence_path: str | None = None
    job_evidence_disk_free_bytes: int | None = None
    shm_path: str = "/dev/shm"
    shm_size_bytes: int | None = None
    shm_writable: bool = False
    process_pid: int = 0
    process_ancestor_pids: list[int] = field(default_factory=list)
    process_tree_pids: list[int] = field(default_factory=list)
    process_start_time: str | None = None
    foreign_process_affinity: dict[int, list[int]] | None = None
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
    facts.foreign_process_affinity = _foreign_process_affinity()

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
    facts.cgroup_descendants = _cgroup_descendant_counts(cgroup["cgroup_dir"])
    facts.cgroup_sibling_competitors = _cgroup_sibling_competitors(cgroup["cgroup_dir"])
    facts.cpu_quota_cores = cgroup["cpu_quota_cores"]
    facts.cpu_weight = cgroup["cpu_weight"]
    facts.cpu_throttled = cgroup["cpu_throttled"]
    facts.memory_limit_bytes = cgroup["memory_limit_bytes"]
    facts.memory_limit_observable = cgroup["memory_limit_observable"]
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
        host_lock = reservation.get("host_lock_path")
        if not isinstance(host_lock, str) or not host_lock.strip():
            results.append(
                _result(
                    "reservation-host-lock",
                    "fail",
                    "host-wide allocation lock path",
                    host_lock,
                    "a single host-wide lock is required for mutual exclusion between jobs",
                )
            )
        if mechanism == "dedicated-host":
            marker_path = reservation.get("exclusive_marker_path")
            if not isinstance(marker_path, str) or not marker_path.strip():
                results.append(
                    _result(
                        "reservation-exclusive-marker",
                        "fail",
                        "operator-created exclusive marker path",
                        marker_path,
                        "a dedicated host requires an operator-created exclusive marker",
                    )
                )
            token = reservation.get("exclusive_token")
            if not isinstance(token, str) or not token.strip():
                results.append(
                    _result(
                        "reservation-exclusive-token",
                        "fail",
                        "operator-issued exclusive token",
                        token,
                        "a dedicated host requires an operator-issued exclusive token",
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
    competing = declaration.get("competing_cpu_cores_max")
    if competing is not None and (
        isinstance(competing, bool) or not isinstance(competing, (int, float)) or competing < 0
    ):
        results.append(
            _result(
                "competing-cpu-cores-value",
                "fail",
                "non-negative number or null",
                competing,
                "competing_cpu_cores_max must be non-negative",
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


def _descriptor_lease_status(
    descriptor: dict[str, Any], facts: RunnerFacts, job_dir: Path
) -> tuple[str, str]:
    """Re-observe that the descriptor's host-wide lease is held by the holder.

    The lease lock must be host-wide (outside ``reservation.job_dir``) so
    overlapping jobs contend on it, and the kernel lock table must show the
    recorded holder as the only holder.  Any extra holder is a competing
    unregistered process and fails the lease.
    """

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
    if _path_within(lock, job_dir):
        return "fail", "the allocation lock is private to the job, not a host-wide allocation lock"
    if lock.is_symlink() or not lock.is_file():
        return "fail", "the lease lock file is missing"
    observed_identity = _lock_identity(lock)
    if observed_identity is None:
        return "unsupported", "the allocation lock identity is not observable on this host"
    if not isinstance(descriptor.get("lock_identity"), dict):
        return (
            "fail",
            (
                "the descriptor does not record the allocation lock identity; a lease keyed on a "
                "pathname alone is not provably exclusive"
            ),
        )
    if not _lock_identity_matches(descriptor.get("lock_identity"), observed_identity):
        return (
            "fail",
            (
                "the allocation lock pathname was recreated; it is a different inode than the "
                "recorded lease, so overlapping allocations would not contend on it"
            ),
        )
    holders = _observe_flock_holders(lock)
    if holders is None:
        return "unsupported", "the kernel lock table is unavailable, so holding is unproven"
    if holder not in holders:
        return "fail", "the recorded holder does not hold the lease lock"
    if holders - {holder}:
        return "fail", "a competing unregistered process holds the allocation lock"
    return "ok", "allocation lease is held by the recorded live holder as the only holder"


def _cgroup_members_are_owned(facts: RunnerFacts, holder_pid: Any = None) -> bool:
    members = facts.cgroup_member_pids
    if members is None:
        return False
    if isinstance(holder_pid, int) and not isinstance(holder_pid, bool) and holder_pid > 0:
        owned = set(_process_tree_pids(holder_pid))
    else:
        owned = set(facts.process_tree_pids)
    if not owned:
        owned = {os.getpid()}
    return set(members).issubset(owned)


def _reservation_owned_pids(descriptor: dict[str, Any], facts: RunnerFacts) -> set[int]:
    """Return the processes owned by the verified lease holder's job tree.

    A ``--check`` nested under the documented ``--reserve --run`` flow is a
    child of the holder, so the checker's own descendants are not the whole
    owned set: the *holder's* job tree is.  Everything in it (the holder, the
    checker, the command the holder runs) shares the declared allocation by
    design.  A process outside that tree is a genuine competitor and is still
    counted.  When the descriptor records no usable holder the caller's own
    tree is used, which is the narrowest honest fallback.
    """

    owned: set[int] = {os.getpid()}
    holder = descriptor.get("holder_pid")
    if isinstance(holder, int) and not isinstance(holder, bool) and holder > 0:
        owned.update(_process_tree_pids(holder))
    owned.update(facts.process_tree_pids)
    return owned


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
    lease_status, lease_detail = _descriptor_lease_status(descriptor, facts, job_dir)
    if lease_status != "ok":
        return _result(
            "reservation-evidence", lease_status, "held allocation lease", None, lease_detail
        )

    if mechanism == "cgroup-quota":
        return _verify_cgroup_reservation(descriptor, facts)
    if mechanism == "cpuset-affinity":
        return _verify_cpuset_reservation(descriptor, facts)
    if mechanism == "dedicated-host":
        return _verify_dedicated_reservation(descriptor, declaration, facts, repo_root, job_dir)
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
    if not _cgroup_members_are_owned(facts, descriptor.get("holder_pid")):
        return _result(
            "reservation-evidence",
            "fail",
            "the lease holder and its job descendants only",
            facts.cgroup_member_pids,
            "competing processes share the declared allocation cgroup",
        )
    descendants = facts.cgroup_descendants
    if descendants is None:
        return _result(
            "reservation-evidence",
            "unsupported",
            "an allocation cgroup with no competing descendant cgroups",
            None,
            "the allocation cgroup's descendant count is not observable",
        )
    if descendants.get("nr_descendants", 0) or descendants.get("nr_dying_descendants", 0):
        return _result(
            "reservation-evidence",
            "fail",
            "an allocation cgroup with no competing descendant cgroups",
            descendants,
            (
                "competing child cgroups share the declared allocation's CPU; a quota "
                "on this cgroup does not reserve capacity against sibling workloads"
            ),
        )
    siblings = facts.cgroup_sibling_competitors
    if siblings is None:
        return _result(
            "reservation-evidence",
            "unsupported",
            "an allocation cgroup with no populated sibling cgroups",
            None,
            "the allocation cgroup's sibling cgroups are not observable",
        )
    if siblings:
        return _result(
            "reservation-evidence",
            "fail",
            "an allocation cgroup with no populated sibling cgroups",
            siblings[:16],
            (
                "populated sibling cgroups share the declared allocation's CPUs; a quota "
                "on this cgroup does not reserve capacity against them"
            ),
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
    foreign = facts.foreign_process_affinity
    if foreign is None:
        return _result(
            "reservation-evidence",
            "unsupported",
            sorted(descriptor_cpuset),
            None,
            "competing process affinity cannot be observed; refusing to claim exclusivity",
        )
    # The holder's job tree legitimately runs on the reserved cpuset: a
    # ``--check`` nested under ``--reserve --run`` is a descendant of the
    # holder, and reading the checker's own descendants as the owned set would
    # classify that holder as a foreign overlapping process.  Everything
    # outside the holder's tree is still a competitor and still fails here.
    owned = _reservation_owned_pids(descriptor, facts)
    overlapping = sorted(
        pid
        for pid, cpus in foreign.items()
        if pid not in owned and descriptor_cpuset.intersection(cpus)
    )
    if overlapping:
        return _result(
            "reservation-evidence",
            "fail",
            sorted(descriptor_cpuset),
            overlapping[:16],
            (
                "unrelated processes are allowed to run on the reserved cpuset; "
                "affinity alone does not move competing workloads off the CPUs"
            ),
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
    job_dir: Path,
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
            "an operator-created exclusive marker",
            descriptor.get("exclusive_marker_path"),
            "a dedicated host needs an operator-created exclusive marker",
        )
    if _path_within(marker, job_dir):
        return _result(
            "reservation-evidence",
            "fail",
            "an operator-owned exclusive marker outside the job directory",
            marker.name,
            "the checker must not generate the exclusive marker inside its own job directory",
        )
    token = descriptor.get("exclusive_token")
    if not isinstance(token, str) or not token.strip():
        return _result(
            "reservation-evidence",
            "fail",
            "an operator-issued exclusive token",
            token,
            "the allocation descriptor does not record the operator exclusive token",
        )
    try:
        marker_value = marker.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        marker_value = ""
    if marker_value != token:
        return _result(
            "reservation-evidence",
            "fail",
            token,
            marker_value[:64],
            "the operator exclusive marker does not match the declared token",
        )
    tolerance = descriptor.get("competing_cpu_cores_max")
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or tolerance < 0:
        tolerance = declaration.get("competing_cpu_cores_max")
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)) or tolerance < 0:
        tolerance = _DEFAULT_FOREIGN_CPU_CORES_TOLERANCE
    measured, interval = _measure_competing_cpu_cores()
    if measured is None:
        return _result(
            "reservation-evidence",
            "unsupported",
            tolerance,
            None,
            (
                "competing CPU consumption could not be measured; refusing to "
                "claim a dedicated host on a label alone"
            ),
        )
    if measured > tolerance:
        return _result(
            "reservation-evidence",
            "fail",
            tolerance,
            round(measured, 4),
            (
                f"non-job processes consumed {measured:.3f} cores over {interval:.1f}s, "
                "so the host is not dedicated to this allocation"
            ),
        )
    return _result(
        "reservation-evidence",
        "ok",
        facts.affinity_cpus,
        marker.name,
        (
            "dedicated host owns every CPU with a matching exclusive marker, a held lease, "
            f"and ≤{tolerance} competing cores measured over {interval:.1f}s"
        ),
    )


def _usable_memory_bytes(facts: RunnerFacts) -> tuple[int | None, str]:
    """Return the memory this job can actually use, and how it was derived.

    A host's ``MemTotal`` is the same on an idle host and a host with no free
    memory, and it ignores any cgroup memory limit the allocation is placed
    under.  The usable figure is therefore the tightest of the observable
    bounds: host total, ``MemAvailable``, and the allocation/ancestor cgroup
    limit.  A bound that cannot be observed is reported as unobservable instead
    of being assumed away, so admission fails closed rather than admitting a
    job against an unknown limit.
    """

    if facts.memory_total_bytes is None:
        return None, "the host's total memory is not observable"
    if facts.memory_available_bytes is None:
        return None, "the memory available on this host is not observable"
    if not facts.memory_limit_observable:
        return None, "the allocation's cgroup memory limit could not be read"
    bounds: dict[str, int] = {
        "host-total": facts.memory_total_bytes,
        "host-available": facts.memory_available_bytes,
    }
    if facts.memory_limit_bytes is not None:
        bounds["cgroup-limit"] = facts.memory_limit_bytes
        detail = "usable memory bytes (the lowest of host-total, host-available, cgroup-limit)"
    else:
        detail = (
            "usable memory bytes (the lowest of host-total, host-available; "
            "no finite cgroup memory limit is observable)"
        )
    return min(bounds.values()), detail


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
        usable_memory, memory_detail = _usable_memory_bytes(facts)
        status = (
            "unsupported"
            if usable_memory is None
            else ("ok" if usable_memory >= declared_memory else "fail")
        )
        results.append(_result("memory", status, declared_memory, usable_memory, memory_detail))

    declared_disk = int(declaration.get("disk_free_bytes_min") or 0)
    for name, observed, detail in (
        ("disk-free-repo", facts.repo_disk_free_bytes, "free disk bytes"),
        (
            "disk-free-temp",
            facts.job_tmp_disk_free_bytes
            if facts.job_tmp_path is not None
            else facts.temp_disk_free_bytes,
            "free disk bytes on the job temporary filesystem"
            if facts.job_tmp_path is not None
            else "free disk bytes",
        ),
        (
            "disk-free-job-evidence",
            facts.job_evidence_disk_free_bytes,
            "free disk bytes on the job evidence filesystem",
        ),
    ):
        if declared_disk <= 0:
            results.append(_result(name, "skipped", declared_disk, observed, "no minimum declared"))
            continue
        if name == "disk-free-job-evidence" and facts.job_evidence_path is None:
            results.append(
                _result(
                    name,
                    "skipped",
                    declared_disk,
                    None,
                    "no resolved job evidence filesystem",
                )
            )
            continue
        status = (
            "unsupported" if observed is None else ("ok" if observed >= declared_disk else "fail")
        )
        results.append(_result(name, status, declared_disk, observed, detail))

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


def _foreign_sample_seconds() -> float:
    raw = os.environ.get(_FOREIGN_SAMPLE_ENV)
    if raw is None:
        return _DEFAULT_FOREIGN_SAMPLE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_FOREIGN_SAMPLE_SECONDS
    return value if value > 0 else _DEFAULT_FOREIGN_SAMPLE_SECONDS


def _clock_ticks_per_second() -> int:
    try:
        return int(os.sysconf("SC_CLK_TCK"))
    except (ValueError, OSError):
        return 100


def _qualification_timeout(override: float | None = None) -> float:
    """Return the qualification-command deadline, distinct from prerequisites."""

    raw = override if override is not None else os.environ.get(_QUALIFICATION_TIMEOUT_ENV)
    if raw is None:
        return _DEFAULT_QUALIFICATION_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_QUALIFICATION_TIMEOUT_SECONDS
    return value if value > 0 else _DEFAULT_QUALIFICATION_TIMEOUT_SECONDS


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
# Descendants of an owned command that survived the group sweep.  While this is
# non-empty the capacity is still in use and the lease must not be released.
_LEFTOVER_OWNED_PIDS: set[int] = set()
# A durable record of the command a lease is actually running.  It lives in the
# job directory so a later ``--recover``/``--release`` process can identify the
# job's own process group and session even after its holder died.
_JOB_RUN_RECORD_NAME = "job-run.json"


@dataclass(frozen=True)
class CommandContainment:
    """The observed containment outcome of one ``run_command`` call.

    ``proven`` is the only claim a caller may act on.  It is true only when the
    command's process group *and* every adopted descendant were observed gone;
    a host that cannot adopt detached descendants reports ``proven=False`` even
    when no survivor was seen, because an unobserved survivor cannot be ruled
    out.
    """

    proven: bool
    detail: str
    adoption_active: bool
    leftovers: tuple[int, ...] = ()


_LAST_COMMAND_CONTAINMENT: CommandContainment | None = None


def last_command_containment() -> CommandContainment | None:
    """Return the containment outcome of the most recent ``run_command``."""

    return _LAST_COMMAND_CONTAINMENT


def _timeout_partial_streams(exc: subprocess.TimeoutExpired) -> tuple[str, str]:
    """Return the child's output observed before a stream wait timed out.

    ``subprocess.TimeoutExpired`` carries whatever ``communicate`` had read when
    the deadline expired.  The previous behaviour replaced that with empty
    strings, which discarded the command's own failure diagnostics whenever a
    detached descendant kept the output pipe open past the deadline.
    """

    def decode(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value)

    raw_stdout = getattr(exc, "stdout", None)
    if raw_stdout is None:
        raw_stdout = getattr(exc, "output", None)
    return decode(raw_stdout), decode(getattr(exc, "stderr", None))


def _drain_after_termination(
    process: subprocess.Popen[str], fallback: tuple[str, str] = ("", "")
) -> tuple[str, str]:
    """Collect a terminated command's streams without discarding partial output.

    Retrying ``communicate`` after a timeout does not lose output, so the retry
    normally returns everything the child wrote.  When the stream is still held
    open the retry times out as well and its own partial snapshot is used; an
    unreadable stream falls back to whatever was already captured.
    """

    try:
        return process.communicate(timeout=_CHILD_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired as exc:
        partial = _timeout_partial_streams(exc)
        return partial if any(partial) else fallback
    except (OSError, ValueError):
        return fallback


_SIGNAL_HANDLERS_INSTALLED = False
_PR_SET_CHILD_SUBREAPER = 36
_PR_GET_CHILD_SUBREAPER = 37


def _libc_prctl() -> Any:
    import ctypes

    return ctypes.CDLL("libc.so.6", use_errno=True)


def _set_child_subreaper(enabled: bool) -> bool:
    """Set ``PR_SET_CHILD_SUBREAPER``, returning whether it took effect."""

    if os.name == "nt":
        return False
    try:
        return _libc_prctl().prctl(_PR_SET_CHILD_SUBREAPER, 1 if enabled else 0, 0, 0, 0) == 0
    except Exception:  # noqa: BLE001 - best-effort subreaper
        return False


def _child_subreaper_state() -> int | None:
    """Return the current child-subreaper flag, or ``None`` if unreadable."""

    if os.name == "nt":
        return None
    try:
        import ctypes

        libc = _libc_prctl()
        value = ctypes.c_int(0)
        if libc.prctl(_PR_GET_CHILD_SUBREAPER, ctypes.byref(value), 0, 0, 0) != 0:
            return None
        return value.value
    except Exception:  # noqa: BLE001 - best-effort subreaper
        return None


class _adopted_descendants:
    """Temporarily become the reaper for a command's orphaned descendants.

    Without ``PR_SET_CHILD_SUBREAPER`` a grandchild that outlives the direct
    command is reparented to init and disappears from this process's tree,
    which makes "the command completed, so the capacity is free" a false
    statement.  As a subreaper this process is the reaper for those orphans and
    can find, terminate, and reap them before the lease is released.

    The flag is process-wide, so the previous value is restored on exit.  A
    caller that is itself a subreaper (or a test process that must keep its own
    reaping semantics) is unaffected.
    """

    def __enter__(self) -> Self:
        self._previous = _child_subreaper_state()
        self._enabled = False
        # A process that is already a subreaper is still a subreaper for the
        # command's orphans, so adoption is observable in that case too.
        self.active = self._previous == 1
        if self._previous != 1:
            self._enabled = _set_child_subreaper(True)
            self.active = self._enabled
        return self

    def __exit__(self, *_exc: object) -> bool:
        if self._enabled and self._previous is not None:
            _set_child_subreaper(bool(self._previous))
        return False


def _process_group_members(pgid: int) -> list[int]:
    """Return every live pid whose process group is *pgid* (excluding self)."""

    if pgid <= 0:
        return []
    me = os.getpid()
    members: list[int] = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return members
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        closing = raw.rfind(")")
        if closing == -1:
            continue
        fields = raw[closing + 1 :].split()
        if len(fields) < 3:
            continue
        if fields[0] == "Z":
            # An exited descendant awaiting reaping holds no CPU capacity.
            _reap_zombie(pid)
            continue
        try:
            if int(fields[2]) == pgid:
                members.append(pid)
        except ValueError:
            continue
    return sorted(members)


def _terminate_process_group(pgid: int, grace: float | None = None) -> tuple[bool, list[int]]:
    """Terminate every remaining member of *pgid* and confirm they exited.

    A completed parent does not imply its descendants exited.  This sweeps the
    process group the command created, escalates SIGTERM -> SIGKILL, and only
    reports success after the group is observed empty.  Remaining pids are
    returned so the caller can keep the lease instead of releasing capacity
    that is still in use.
    """

    if os.name == "nt":
        return True, []
    grace = _CHILD_TERMINATION_GRACE_SECONDS if grace is None else grace
    remaining = _process_group_members(pgid)
    if not remaining:
        return True, []
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except OSError:
            for pid in remaining:
                try:
                    os.kill(pid, sig)
                except OSError:
                    continue
        deadline = time.monotonic() + grace
        while True:
            remaining = _process_group_members(pgid)
            if not remaining:
                return True, []
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    return False, _process_group_members(pgid)


def _adopted_orphan_pids(exclude: set[int] | None = None) -> list[int]:
    """Return live orphaned children of this process that are not tracked.

    A command that detaches a grandchild with ``start_new_session=True`` moves
    that grandchild into a different process group, so the process-group sweep
    cannot see it.  Because the runner is a child subreaper, such an orphan is
    reparented to this process when its parent exits, which makes it a *direct*
    child here.  Restricting the scan to direct children is deliberate: it
    reaches exactly the adopted orphans, and it cannot touch an unrelated
    process that merely shares this subtree.

    ``exclude`` holds pids observed before the command started, so a
    pre-existing child of the runner is never mistaken for the command's
    descendant.  Zombies are reaped rather than reported, because a reaped
    orphan holds no CPU capacity.
    """

    excluded = set(exclude or ())
    excluded.update(process.pid for process in _OWNED_PROCESSES if process.poll() is None)
    me = os.getpid()
    owned: list[int] = []
    for pid in _direct_child_pids(me):
        if pid in excluded:
            continue
        if _pid_is_zombie(pid):
            _reap_zombie(pid)
            continue
        if _pid_alive(pid):
            owned.append(pid)
    return sorted(owned)


def _direct_child_pids(parent_pid: int | None = None) -> list[int]:
    """Return the pids whose parent is *parent_pid* (default: this process)."""

    if parent_pid is None:
        parent_pid = os.getpid()
    children: list[int] = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return children
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == parent_pid:
            continue
        try:
            raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        closing = raw.rfind(")")
        if closing == -1:
            continue
        fields = raw[closing + 1 :].split()
        if len(fields) < 2:
            continue
        try:
            if int(fields[1]) == parent_pid:
                children.append(pid)
        except ValueError:
            continue
    return sorted(children)


def _terminate_pids(pids: list[int], grace: float | None = None) -> tuple[bool, list[int]]:
    """Terminate *pids* outside a shared process group and confirm they exited.

    Detached descendants do not share the command's process group, so they are
    signalled individually.  The escalation and confirmation mirror
    ``_terminate_process_group`` so that a surviving descendant keeps the lease
    instead of being reported as released capacity.
    """

    if os.name == "nt":
        return True, []
    grace = _CHILD_TERMINATION_GRACE_SECONDS if grace is None else grace
    remaining = sorted({pid for pid in pids if _pid_alive(pid)})
    if not remaining:
        return True, []
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in remaining:
            if _pid_is_zombie(pid):
                _reap_zombie(pid)
                continue
            try:
                os.kill(pid, sig)
            except OSError:
                try:
                    os.killpg(os.getpgid(pid), sig)
                except OSError:
                    continue
        deadline = time.monotonic() + grace
        while True:
            surviving: list[int] = []
            for pid in remaining:
                if _pid_is_zombie(pid):
                    _reap_zombie(pid)
                    continue
                if _pid_alive(pid):
                    surviving.append(pid)
            remaining = surviving
            if not remaining:
                return True, []
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    return False, remaining


def _sweep_leftover_owned_processes() -> list[int]:
    """Re-sweep recorded leftover descendants and return the survivors.

    Returning an empty list means every recorded pid is gone and the capacity
    can be released.  A non-empty list means owned work is still running, so
    the caller must keep the lease and report blocked cleanup.
    """

    still_alive: list[int] = []
    for pid in sorted(_LEFTOVER_OWNED_PIDS):
        if _pid_is_zombie(pid):
            _reap_zombie(pid)
            _LEFTOVER_OWNED_PIDS.discard(pid)
            continue
        if not _pid_alive(pid):
            _LEFTOVER_OWNED_PIDS.discard(pid)
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            _LEFTOVER_OWNED_PIDS.discard(pid)
            continue
        except OSError:
            still_alive.append(pid)
            continue
        deadline = time.monotonic() + _CHILD_TERMINATION_GRACE_SECONDS
        while time.monotonic() < deadline and _pid_alive(pid):
            time.sleep(0.05)
        if _pid_alive(pid):
            still_alive.append(pid)
        else:
            _LEFTOVER_OWNED_PIDS.discard(pid)
    return still_alive


def _contain_adopted_descendants(
    exclude: set[int] | None = None,
    *,
    deadline_seconds: float | None = None,
) -> tuple[bool, list[int]]:
    """Terminate every adopted orphan within a bounded deadline.

    One snapshot of this process's direct children is not enough.  Killing an
    adopted process reparents the descendants *it* had detached into its own
    session, so a nested orphan only becomes a direct child after the first
    sweep.  The adopted set is therefore rescanned until it stays empty, and a
    pid that cannot be confirmed gone is returned as a survivor so the caller
    keeps the lease instead of reporting released capacity.
    """

    if os.name == "nt":
        return True, []
    budget = _CHILD_TERMINATION_GRACE_SECONDS if deadline_seconds is None else deadline_seconds
    deadline = time.monotonic() + budget
    confirmed = True
    survivors: set[int] = set()
    while True:
        # A short per-batch grace keeps a deep detached chain inside the
        # overall deadline instead of multiplying the escalation wait.
        per_batch = max(0.2, min(budget, 1.0))
        candidates = _adopted_orphan_pids(exclude)
        if candidates:
            batch_confirmed, remaining = _terminate_pids(candidates, grace=per_batch)
            if not batch_confirmed:
                confirmed = False
                survivors.update(remaining)
        remaining_now = _adopted_orphan_pids(exclude)
        if not remaining_now:
            return confirmed, sorted(survivors)
        if time.monotonic() >= deadline:
            return False, sorted(survivors | set(remaining_now))
        time.sleep(0.05)


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
    command: list[str],
    cwd: Path,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    on_start: Callable[[subprocess.Popen[str]], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run *command* under a deadline with owned-process cleanup.

    The command runs in its own session/process group, receives a parent-death
    signal, and is torn down as a group on timeout.  The timeout is reported as
    a distinct terminal result without discarding captured output.  *timeout*
    defaults to the bounded prerequisite deadline; callers running a
    qualification command pass their own deadline.

    When the command itself exits (cleanly, failing, or on timeout) it may
    still have left descendants running.  Two sweeps cover both shapes: the
    command's whole process group, and any descendant that detached into its
    own session/process group with ``start_new_session``.  Detached orphans are
    adopted because the runner is a child subreaper, so they stay visible in
    this process's tree.  Any descendant that cannot be confirmed gone is
    recorded on ``_LEFTOVER_OWNED_PIDS`` so the caller refuses to release the
    lease.  The original exit status and captured output are preserved either
    way.

    *on_start*, when given, is called with the live ``Popen`` immediately after
    the command has started, so a lease can persist durable job ownership
    before the command outlives its holder.

    The sweeps run on the cancellation path too.  The installed signal handlers
    raise ``SystemExit`` from inside ``communicate``, so containment is not left
    to the normal-return path: a command interrupted by SIGINT/SIGTERM still
    has its whole group and every adopted descendant terminated and confirmed
    before the signal is allowed to end the process.
    """

    timeout = _command_timeout() if timeout is None else timeout
    # The containment outcome is published for the caller that owns the lease,
    # so the assignment below must reach the module global rather than a local.
    global _LAST_COMMAND_CONTAINMENT
    timed_out = False
    output_stream_held = False
    notes: list[str] = []
    group_leftovers: list[int] = []
    detached_leftovers: list[int] = []
    pending_error: BaseException | None = None
    with _adopted_descendants() as adoption:
        adoption_active = adoption.active
        preexisting = set(_direct_child_pids(os.getpid()))
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                **_process_group_options(),
            )
        except OSError as exc:
            _LAST_COMMAND_CONTAINMENT = CommandContainment(
                proven=True,
                detail="the command could not be started, so no descendant exists",
                adoption_active=adoption_active,
            )
            return subprocess.CompletedProcess(command, 127, "", f"{type(exc).__name__}: {exc}")
        preexisting.discard(process.pid)
        _register_owned(process)
        if on_start is not None:
            on_start(process)
        try:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                # ``communicate`` waits for EOF as well as for the command to
                # exit.  A descendant that inherited the output pipe keeps it
                # open after the command itself finished, so this timeout is not
                # by itself proof that the command ran too long.  Whatever the
                # command wrote before the deadline is retained instead of being
                # replaced by empty strings.
                stdout, stderr = _timeout_partial_streams(exc)
                if process.poll() is None:
                    timed_out = True
                    _terminate_owned_process(process)
                else:
                    output_stream_held = True
                stdout, stderr = _drain_after_termination(process, (stdout, stderr))
            except (KeyboardInterrupt, SystemExit) as exc:
                # A signal that interrupts the wait must not skip containment:
                # collect the command's output opportunistically, tear the
                # command down, and remember the exception to re-raise once the
                # owned process group and every adopted descendant are gone.
                pending_error = exc
                _terminate_owned_process(process)
                stdout, stderr = "", ""
                with contextlib.suppress(KeyboardInterrupt, SystemExit):
                    stdout, stderr = _drain_after_termination(process)
            except BaseException as exc:  # noqa: BLE001 - containment must run first
                # A decoding failure (``UnicodeDecodeError``) or any other
                # error raised while reading the child's streams must not skip
                # containment.  The command may still have detached
                # descendants, so tear it down and remember the original error
                # to re-raise only after both sweeps have run.  The original
                # exception is preserved as the raised exception, so callers
                # still see the real failure.
                pending_error = exc
                _terminate_owned_process(process)
                stdout, stderr = "", ""
                with contextlib.suppress(BaseException):
                    stdout, stderr = _drain_after_termination(process)
        finally:
            _unregister_owned(process)

        # Containment runs on every path, including the cancellation path, so a
        # signal cannot leave an owned descendant holding the allocation.
        group_confirmed, group_leftovers = _terminate_process_group(process.pid)
        detached_confirmed, detached_leftovers = _contain_adopted_descendants(preexisting)
        if not adoption_active:
            notes.append(
                "the runner could not become a child subreaper, so descendants that "
                "detached into a new session could not be observed or contained; "
                "this run's containment is unproven"
            )

    leftovers = sorted(set(group_leftovers) | set(detached_leftovers))
    if not group_confirmed or not detached_confirmed:
        _LEFTOVER_OWNED_PIDS.update(leftovers)
    containment_proven = bool(group_confirmed and detached_confirmed and adoption_active)
    if not adoption_active:
        containment_detail = (
            "the runner could not become a child subreaper, so detached descendants "
            "could not be observed or contained"
        )
    elif leftovers:
        containment_detail = (
            f"{len(leftovers)} owned descendant(s) survived containment "
            "(" + ", ".join(str(pid) for pid in leftovers[:16]) + ")"
        )
    elif not group_confirmed:
        containment_detail = "the command's process group still has live members"
    elif not detached_confirmed:
        containment_detail = "adopted descendants survived containment"
    else:
        containment_detail = (
            "the command's process group and every adopted descendant were observed gone"
        )
    _LAST_COMMAND_CONTAINMENT = CommandContainment(
        proven=containment_proven,
        detail=containment_detail,
        adoption_active=adoption_active,
        leftovers=tuple(leftovers),
    )
    if pending_error is not None:
        # The signal or interrupt still terminates the run, but only after the
        # descendant sweeps above have run; a survivor keeps the lease.
        raise pending_error
    if timed_out:
        notes.insert(
            0,
            f"command exceeded {timeout:g}s; its owned process group was terminated",
        )
    if output_stream_held:
        notes.append(
            "the command exited but a descendant kept its output stream open; "
            "the captured output may be truncated"
        )
    if leftovers:
        notes.append(
            f"{len(leftovers)} owned descendant(s) remained after the command exited; "
            "capacity must not be released"
        )
    returncode = _TIMEOUT_RETURNCODE if timed_out else process.returncode
    for note in notes:
        stderr = (stderr or "") + f"\n[qualification-runner] {note}"
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


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


def _load_native_build_evidence(path: Path) -> tuple[dict[str, Any] | None, str]:
    if path.is_symlink():
        return None, "the retained native build evidence must be a regular file"
    if not path.is_file():
        return None, "the retained native build evidence is missing"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"the retained native build evidence is not readable JSON: {exc}"
    if not isinstance(document, dict):
        return None, "the retained native build evidence must be a JSON object"
    return document, ""


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
    artifacts = identity.get("artifacts")
    artifacts = artifacts if isinstance(artifacts, dict) else {}
    if not artifacts:
        problems.append(
            "the installed runtime did not report its complete installed output set, "
            "so a replaced compiled module could go undetected"
        )
    for name in _EXTENSION_BACKED_MODULES:
        entry = modules.get(name)
        kind = entry.get("kind") if isinstance(entry, dict) else None
        if kind != "cython":
            problems.append(f"{name} is not an installed extension")
        artifact = entry.get("artifact") if isinstance(entry, dict) else None
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        if not isinstance(artifact, str) or artifacts.get(artifact) != digest:
            problems.append(f"{name} is not covered by the reported complete installed output set")
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
    results.append(
        _native_build_evidence_check(
            interpreters,
            repo_root,
            expected_inputs,
            expected_fingerprint,
            fingerprint,
            identity,
        )
    )
    return results


def _native_build_evidence_check(
    interpreters: dict[str, Any],
    repo_root: Path,
    expected_inputs: Any,
    expected_fingerprint: Any,
    probe_fingerprint: str,
    probe_identity: dict[str, Any],
) -> CheckResult:
    """Require retained evidence from the complete fresh native build procedure.

    Pinning the source bytes and the installed fingerprint independently does not
    prove the installed extensions were compiled from the pinned inputs: a
    pre-existing mixed build satisfies both comparisons.  The operator must
    retain the fresh-build procedure's own record connecting the staged inputs,
    build completion, and installed outputs; absent that record the check is
    ``unsupported`` rather than a pass.

    The record is only meaningful if a hand-written document cannot pass.  The
    validator therefore requires the fields that the executed procedure alone
    emits -- the evidence format version, the producing script's own digest, and
    a full runtime identity whose canonical fingerprint must equal both the
    recorded fingerprint and the fingerprint observed in the live runtime --
    and recomputes every derivable value instead of trusting the document's own
    summary.  This binds the record to the on-host bootstrap and to the exact
    installed extension bytes; it is not a cryptographic attestation against a
    determined operator that has already read those same bytes.
    """

    evidence_path = _resolve_declared_path(interpreters.get("native_build_evidence"), repo_root)
    if evidence_path is None:
        return _result(
            "native-build-evidence",
            "unsupported",
            "retained fresh native build evidence",
            None,
            "no retained evidence connects the pinned inputs to the installed outputs",
        )
    name = "native-build-evidence"
    evidence_sha = interpreters.get("native_build_evidence_sha256")
    if not _is_sha256(evidence_sha):
        return _result(
            name,
            "fail",
            "sha256 pin for the retained evidence",
            evidence_sha,
            "the retained native build evidence must be pinned by SHA-256",
        )
    try:
        actual_sha = _sha256_of_file(evidence_path)
    except OSError:
        return _result(
            name,
            "unsupported",
            evidence_sha,
            None,
            "the retained native build evidence could not be read",
        )
    if actual_sha != evidence_sha.strip().lower():
        return _result(
            name,
            "fail",
            evidence_sha,
            actual_sha,
            "the retained native build evidence bytes do not match the pinned digest",
        )
    document, error = _load_native_build_evidence(evidence_path)
    if document is None:
        return _result(name, "fail", "valid retained evidence JSON", None, error)
    problems: list[str] = []
    if document.get("evidence_version") != _NATIVE_BUILD_EVIDENCE_VERSION:
        problems.append(
            "the retained evidence does not declare the supported evidence format version"
        )
    if document.get("procedure") != _NATIVE_BUILD_EVIDENCE_PROCEDURE:
        problems.append(
            "the retained evidence was not produced by the fresh native build procedure"
        )
    if document.get("mode") != "cython":
        problems.append("the retained evidence does not record a cython-mode build")
    if document.get("status") != "complete":
        problems.append("the retained evidence does not record a completed native build")
    recorded_inputs = document.get("build_inputs_sha256")
    if recorded_inputs != expected_inputs:
        problems.append("the retained evidence staged inputs do not match the pinned build inputs")
    source_digest = _native_source_digest(repo_root)
    if source_digest is not None and recorded_inputs != source_digest:
        problems.append(
            "the retained evidence staged inputs do not match the vendored build inputs"
        )
    problems.extend(_build_evidence_producer_problems(document))

    recorded_identity = document.get("runtime_identity")
    recorded_fingerprint = document.get("installed_fingerprint")
    if not isinstance(recorded_identity, dict):
        problems.append("the retained evidence does not record the installed runtime identity")
    else:
        derived = _native_build_fingerprint(recorded_identity)
        if derived != recorded_fingerprint:
            problems.append(
                "the retained evidence runtime identity does not correspond to its recorded "
                "fingerprint"
            )
        if recorded_identity != probe_identity:
            problems.append(
                "the retained evidence runtime identity does not match the installed runtime"
            )
    if recorded_fingerprint != expected_fingerprint:
        problems.append(
            "the retained evidence installed outputs do not match the pinned fingerprint"
        )
    if recorded_fingerprint != probe_fingerprint:
        problems.append(
            "the retained evidence installed outputs do not match the installed runtime"
        )
    if problems:
        return _result(
            name,
            "fail",
            "a consistent fresh native build",
            recorded_fingerprint,
            "; ".join(problems),
        )
    return _result(
        name,
        "ok",
        expected_inputs,
        recorded_fingerprint,
        "the retained evidence ties the pinned inputs and completed build to the installed outputs",
    )


def _build_evidence_producer_problems(document: dict[str, Any]) -> list[str]:
    """Verify the record identifies the bootstrap script that actually ran.

    A document that omits the producer entry, or pins a digest that does not
    match the checked-in bootstrap, was not emitted by this host's build
    procedure.  The script bytes are recomputed here rather than trusted from
    the declaration.
    """

    producer = document.get("producer")
    if not isinstance(producer, dict):
        return ["the retained evidence does not identify the producing build script"]
    problems: list[str] = []
    if producer.get("script") != "scripts/bootstrap_pyboy.py":
        problems.append("the retained evidence names an unrecognized producing build script")
    script_path = Path(__file__).resolve().parent / "bootstrap_pyboy.py"
    try:
        actual_script_sha = _sha256_of_file(script_path)
    except OSError:
        return problems + ["the checked-in bootstrap script could not be read"]
    if producer.get("script_sha256") != actual_script_sha:
        problems.append("the retained evidence was not produced by the checked-in bootstrap script")
    return problems


def _native_build_fingerprint(identity: dict[str, Any]) -> str:
    """Recompute the canonical native runtime fingerprint from an identity.

    This mirrors the digest calculation in ``_NATIVE_PROBE`` so a recorded
    identity can be checked against the fingerprint claimed for it.
    """

    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _asset_tree_writable(root: Path) -> bool:
    if os.access(root, os.W_OK):
        return True
    try:
        for entry in root.rglob("*"):
            # A read-only directory containing a writable subdirectory is not
            # immutable: the owner can delete or replace a protected file via
            # that parent.  Check directories as well as files so a writable
            # nested directory fails the admission instead of passing.
            if os.access(entry, os.W_OK):
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
    """Redact any remaining absolute path, including unregistered diagnostics.

    Registered paths are already replaced by relative or basename forms before
    this runs; this fallback removes arbitrary absolute build, temporary, and
    compiler diagnostic paths that were never declared.
    """

    return _ABSOLUTE_PATH_RE.sub("<redacted-path>", _PATH_FRAGMENT_RE.sub("<redacted-path>", text))


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
            for key in (
                "descriptor_path",
                "job_dir",
                "exclusive_marker_path",
                "host_lock_path",
            ):
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


def _populate_job_filesystem_facts(facts: RunnerFacts, job_dir: Path) -> None:
    """Record free space on the filesystems the job will actually write to.

    Admission must measure the destination, not the caller's ``TMPDIR``.  The
    spawned job receives ``TMPDIR=<job-dir>/tmp`` and writes evidence under
    ``<job-dir>/evidence``; either can live on a different filesystem from
    ``/tmp``.  The directories are created first (a private job directory is
    about to be prepared anyway) so ``statvfs`` measures the real target.
    """

    for label, path in (
        ("job_tmp", job_dir / "tmp"),
        ("job_evidence", job_dir / "evidence"),
    ):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            path = path.parent
        setattr(facts, f"{label}_path", str(path))
        try:
            setattr(facts, f"{label}_disk_free_bytes", shutil.disk_usage(path).free)
        except OSError:
            setattr(facts, f"{label}_disk_free_bytes", None)


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
    host_lock_raw = reservation.get("host_lock_path")
    if not isinstance(host_lock_raw, str) or not host_lock_raw.strip():
        raise ValueError("reservation.host_lock_path is required for a mutually exclusive lease")
    host_lock = _resolve_declared_path(host_lock_raw, repo_root)
    if host_lock is None:
        raise ValueError("reservation.host_lock_path could not be resolved")
    if _path_within(host_lock, job_dir):
        raise ValueError("the allocation lock must be host-wide, not inside reservation.job_dir")

    exclusive_token: str | None = None
    marker_path: Path | None = None
    if mechanism == "dedicated-host":
        token = reservation.get("exclusive_token")
        marker_path = _resolve_declared_path(reservation.get("exclusive_marker_path"), repo_root)
        if marker_path is None or not isinstance(token, str) or not token.strip():
            raise ValueError("a dedicated host requires an operator-created exclusive marker")
        if marker_path.is_symlink() or not marker_path.is_file():
            raise ValueError("the operator exclusive marker is missing")
        if _path_within(marker_path, job_dir):
            raise ValueError(
                "the exclusive marker must be operator-owned outside reservation.job_dir"
            )
        try:
            marker_value = marker_path.read_text(encoding="utf-8").strip()
        except (OSError, ValueError) as exc:
            raise ValueError(f"the operator exclusive marker is unreadable: {exc}") from exc
        if marker_value != token:
            raise ValueError("the operator exclusive marker does not match the declared token")
        exclusive_token = token

    _prepare_job_directory(job_dir)
    lock_fd = os.open(host_lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        raise

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
    lock_identity = _lock_identity(host_lock)
    if lock_identity is None:
        os.close(lock_fd)
        raise ValueError("the host-wide allocation lock could not be identified")
    _HELD_LEASE_FDS.setdefault((lock_identity["device"], lock_identity["inode"]), []).append(
        lock_fd
    )
    descriptor: dict[str, Any] = {
        "descriptor_version": _DESCRIPTOR_VERSION,
        "allocation_id": declaration.get("runner_id"),
        "runner_id": declaration.get("runner_id"),
        "mechanism": mechanism,
        "state": "held",
        "lease_id": lease_id,
        "holder_pid": os.getpid(),
        "holder_start_time": _process_start_time(os.getpid()),
        "lock_path": str(host_lock),
        "lock_identity": lock_identity,
        "cgroup_path": reservation.get("cgroup_path") or facts.cgroup_relative_path,
        "cpuset": cpuset,
        "cpu_quota_cores": quota,
        "cpu_weight": weight,
        "logical_cpus": declaration.get("logical_cpus"),
        "exclusive": bool(reservation.get("exclusive", mechanism == "dedicated-host")),
        "created_at": _iso_now(),
    }
    if mechanism == "dedicated-host":
        descriptor["exclusive_marker_path"] = str(marker_path)
        descriptor["exclusive_token"] = exclusive_token
    descriptor_path = job_dir / "allocation.json"
    try:
        descriptor_path.write_text(
            json.dumps(descriptor, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.chmod(descriptor_path, 0o400)
    except OSError:
        os.close(lock_fd)
        raise
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


def _remove_allocation_state(descriptor_path: Path) -> None:
    """Remove only the private per-job descriptor.

    The host-wide allocation lock and any operator-owned exclusive marker are
    reservation infrastructure that outlives a single job.  Unlinking them
    would let a later allocation create a fresh pathname (and therefore a
    fresh, uncontended lock inode) while the original inode is still held,
    destroying mutual exclusion.  Ownership of the lease is relinquished by
    closing the descriptor, never by deleting the shared path.
    """

    try:
        descriptor_path.unlink()
    except OSError:
        pass


def _read_job_run_record(job_dir: Path) -> tuple[dict[str, Any] | None, str]:
    """Return the durable record of the job a lease launched, if any.

    ``(None, "")`` means the lease never launched a job.  A non-empty error
    means a record exists but cannot be trusted, which callers must treat as
    unobservable rather than as "no job ran".
    """

    path = job_dir / _JOB_RUN_RECORD_NAME
    if not path.exists():
        return None, ""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"the recorded job ownership could not be read: {exc}"
    if not isinstance(document, dict):
        return None, "the recorded job ownership is not a JSON object"
    return document, ""


def _write_job_run_record(
    job_dir: Path, process: subprocess.Popen[str], *, containment_confirmed: bool
) -> None:
    """Persist verifiable ownership of the job running under a lease.

    The in-process leftover pid set cannot survive the holder's death, so a
    later ``--recover`` has no way to tell whether a dead holder left a
    descendant consuming the allocation.  The record ties the job directory to
    the launched job's own pid/start time and its process group/session, and
    ``containment_confirmed`` is set only after the runner proved every
    descendant was gone.
    """

    try:
        process_group = os.getpgid(process.pid)
    except (OSError, AttributeError):
        process_group = process.pid
    try:
        session = os.getsid(process.pid)
    except (OSError, AttributeError):
        session = process.pid
    record = {
        "record_version": 1,
        "job_pid": process.pid,
        "job_start_time": _process_start_time(process.pid),
        "job_process_group": process_group,
        "job_session": session,
        "containment_confirmed": containment_confirmed,
        "recorded_at": _iso_now(),
    }
    path = job_dir / _JOB_RUN_RECORD_NAME
    try:
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        # The lease is still valid; the run path reports the missing record as
        # unobservable ownership rather than silently claiming containment.
        pass


def _update_job_run_record(job_dir: Path, *, containment_confirmed: bool) -> None:
    """Record whether the launched job's descendant containment was proven."""

    record, error = _read_job_run_record(job_dir)
    if record is None or error:
        return
    record["containment_confirmed"] = containment_confirmed
    record["confirmed_at"] = _iso_now()
    path = job_dir / _JOB_RUN_RECORD_NAME
    try:
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError:
        # An unreadable record already fails closed in recovery; rewriting it is
        # best effort so a missing marker can never be mistaken for containment.
        pass


def _confirm_recorded_job_containment(
    job_dir: Path, *, contain_adopted: bool = True
) -> tuple[bool, str]:
    """Terminate and positively confirm the recorded job's descendants are gone.

    Recovery and release both destroy the lease state, so neither may proceed on
    an assumption.  The recorded job's process group/session is swept, adopted
    orphans are contained, and a record that never confirmed containment keeps
    the lease blocked unless *this* process can positively adopt and account for
    the orphans.
    """

    record, error = _read_job_run_record(job_dir)
    if error:
        return False, error
    if record is not None:
        job_pid = record.get("job_pid")
        start_time = record.get("job_start_time")
        if (
            isinstance(job_pid, int)
            and not isinstance(job_pid, bool)
            and job_pid > 0
            and _pid_alive(job_pid)
            and _process_start_time(job_pid) == start_time
        ):
            return False, f"the recorded qualification job (pid {job_pid}) is still running"
        groups = {
            value
            for value in (record.get("job_process_group"), record.get("job_session"))
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
        }
        for pgid in sorted(groups):
            confirmed, survivors = _terminate_process_group(pgid)
            if not confirmed:
                return (
                    False,
                    "the recorded qualification job's process group still has live members: "
                    + ", ".join(str(pid) for pid in survivors[:16]),
                )
    if contain_adopted:
        adopted_confirmed, adopted_survivors = _contain_adopted_descendants()
        if not adopted_confirmed:
            return (
                False,
                "descendants of the recorded holder survived containment: "
                + ", ".join(str(pid) for pid in adopted_survivors[:16]),
            )
    if record is not None and not record.get("containment_confirmed"):
        if not contain_adopted:
            return (
                False,
                (
                    "the recorded qualification job never confirmed descendant containment; "
                    "refusing to release the lease while its capacity may still be in use"
                ),
            )
        if _child_subreaper_state() != 1:
            return (
                False,
                (
                    "the recorded qualification job never confirmed descendant containment "
                    "and this process cannot adopt its orphans; refusing to report the "
                    "allocation free"
                ),
            )
    return True, ""


def _terminate_and_confirm(pid: int) -> tuple[bool, str]:
    """Terminate an owned holder and positively confirm it is gone.

    A kill request is not evidence of termination.  Report success only after
    the process is observed to have exited; otherwise the lease state is kept
    so cleanup can be retried instead of silently reporting a released lease.
    """

    signals = (signal.SIGTERM, signal.SIGKILL)
    for index, sig in enumerate(signals):
        if not _pid_alive(pid):
            return True, ""
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return True, ""
        except OSError as exc:
            return False, f"could not signal the lease holder: {exc}"
        deadline = time.monotonic() + _CHILD_TERMINATION_GRACE_SECONDS
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                return True, ""
            time.sleep(0.05)
        if index == len(signals) - 1:
            break
    return False, "the lease holder is still running after SIGKILL"


def _release_confirm_holder_tree(holder: int) -> tuple[bool, str]:
    """Terminate the lease holder and confirm its whole tree is gone.

    Killing the holder is not evidence that its capacity is free: a
    SIGTERM-resistant command can own a detached descendant that keeps
    consuming the allocation after the holder exits.  The holder's tree is
    snapshotted *before* it is signalled, every member is confirmed gone, and
    any adopted descendant or recorded leftover is contained and re-checked.
    Release must never report ``ok`` while a descendant survives.
    """

    tree = set(_process_tree_pids(holder)) - {os.getpid()}
    terminated, detail = _terminate_and_confirm(holder)
    if not terminated:
        return False, detail
    # A detached descendant reparents onto this subreaper; contain both the
    # snapshotted tree and anything that arrived as an adopted orphan.
    remaining = sorted(
        pid for pid in tree if pid != holder and _pid_alive(pid) and not _pid_is_zombie(pid)
    )
    if remaining:
        confirmed, survivors = _terminate_pids(remaining)
        if not confirmed:
            return False, (
                "the lease holder's descendants are still running after SIGKILL: "
                + ", ".join(str(pid) for pid in survivors[:16])
            )
    adopted_confirmed, adopted_survivors = _contain_adopted_descendants()
    if not adopted_confirmed:
        return False, (
            "adopted descendants survived containment: "
            + ", ".join(str(pid) for pid in adopted_survivors[:16])
        )
    live_leftovers = _sweep_leftover_owned_processes()
    if live_leftovers:
        return False, (
            "owned descendants are still running after releasing the holder: "
            + ", ".join(str(pid) for pid in live_leftovers[:16])
        )
    return True, ""


def _release_allocation(declaration: dict[str, Any], repo_root: Path) -> tuple[str, str]:
    live_leftovers = _sweep_leftover_owned_processes()
    if live_leftovers:
        return (
            "blocked",
            (
                "owned descendants are still running "
                f"({len(live_leftovers)} pid(s)); the lease is kept because the "
                "allocation is still in use"
            ),
        )
    descriptor_path = _allocation_descriptor_path(declaration, repo_root)
    if descriptor_path is None or not descriptor_path.is_file():
        return "blocked", "no pinned allocation descriptor to release"
    descriptor, error = _load_allocation_descriptor(descriptor_path)
    if descriptor is None:
        return "fail", error
    holder = descriptor.get("holder_pid")
    owned = holder == os.getpid() or _holder_is_owned(holder, descriptor.get("holder_start_time"))
    if not owned:
        return (
            "fail",
            "the recorded holder is not an owned qualification-runner lease; refusing to remove state",
        )
    raw = descriptor.get("lock_path")
    lock_path = Path(raw) if isinstance(raw, str) and raw else None
    lock_holders = _observe_flock_holders(lock_path) if lock_path is not None else None
    if lock_holders is None:
        return "blocked", "lock ownership is not observable; refusing to remove state"
    if lock_path is not None:
        observed_identity = _lock_identity(lock_path)
        if observed_identity is None:
            return "blocked", "the allocation lock identity is not observable; refusing to release"
        if not _lock_identity_matches(descriptor.get("lock_identity"), observed_identity):
            return (
                "blocked",
                (
                    "the allocation lock pathname no longer names the recorded lease inode; "
                    "refusing to release, because this state belongs to a different allocation"
                ),
            )
    if holder == os.getpid():
        if lock_path is not None and not _lock_owned_by_this_process(lock_path):
            return "blocked", "this process does not hold the recorded allocation lock"
        recorded_ok, recorded_detail = _confirm_recorded_job_containment(
            descriptor_path.parent, contain_adopted=False
        )
        if not recorded_ok:
            return "blocked", recorded_detail
        _close_held_lease_descriptors(descriptor.get("lock_identity"))
        if lock_path is not None and _lock_owned_by_this_process(lock_path):
            return "blocked", "this process still holds the allocation lock after releasing it"
    else:
        if _pid_alive(holder) or holder in lock_holders:
            terminated, detail = _release_confirm_holder_tree(holder)
            if not terminated:
                return "blocked", detail
        else:
            # The holder already exited, but it may have detached a descendant
            # that is still consuming the allocation.  Confirm containment
            # before any state is removed.
            adopted_confirmed, adopted_survivors = _contain_adopted_descendants()
            if not adopted_confirmed:
                return "blocked", (
                    "descendants of the exited holder survived containment: "
                    + ", ".join(str(pid) for pid in adopted_survivors[:16])
                )
        if lock_path is not None and holder in (_observe_flock_holders(lock_path) or set()):
            return "blocked", "the recorded holder still holds the allocation lock"
    live_leftovers = _sweep_leftover_owned_processes()
    if live_leftovers:
        return "blocked", (
            "owned descendants are still running after release "
            f"({len(live_leftovers)} pid(s)); including "
            + ", ".join(str(pid) for pid in live_leftovers[:16])
        )
    _remove_allocation_state(descriptor_path)
    return "ok", "the owned lease was relinquished and the private job state removed"


def _recover_allocation(declaration: dict[str, Any], repo_root: Path) -> tuple[str, str]:
    descriptor_path = _allocation_descriptor_path(declaration, repo_root)
    if descriptor_path is None or not descriptor_path.is_file():
        return "ok", "no allocation descriptor to recover"
    # A recovery removes the lease state, so it must first prove no owned work
    # survived a cancelled command: a rejected signal path can leave a detached
    # descendant that would otherwise keep consuming the allocation after the
    # state was deleted and the lease reported free.
    live_leftovers = _sweep_leftover_owned_processes()
    if live_leftovers:
        return (
            "blocked",
            (
                "owned descendants are still running "
                f"({len(live_leftovers)} pid(s)); refusing to remove lease state "
                "while the allocation is still in use"
            ),
        )
    descriptor, error = _load_allocation_descriptor(descriptor_path)
    if descriptor is None:
        return "blocked", f"the descriptor could not be read; refusing to remove state ({error})"
    holder = descriptor.get("holder_pid")
    holder_valid = isinstance(holder, int) and not isinstance(holder, bool) and holder > 0
    raw = descriptor.get("lock_path")
    if not isinstance(raw, str) or not raw.strip():
        return "blocked", "the lease lock path is unknown; refusing to remove state"
    lock_path = Path(raw)
    lock_holders = _observe_flock_holders(lock_path)
    if lock_holders is None:
        return "blocked", "lock ownership is not observable; refusing to remove state"
    observed_identity = _lock_identity(lock_path)
    if observed_identity is None:
        return "blocked", "the allocation lock identity is not observable; refusing to remove state"
    if not _lock_identity_matches(descriptor.get("lock_identity"), observed_identity):
        return (
            "blocked",
            (
                "the allocation lock pathname no longer names the recorded lease inode; "
                "refusing to remove state that belongs to a different allocation"
            ),
        )
    if holder_valid and _pid_alive(holder):
        return "fail", "the recorded holder is still running; refusing to remove state"
    if holder_valid and holder in lock_holders:
        return "fail", "the recorded holder still holds the lease; release it first"
    if lock_holders:
        return (
            "blocked",
            "the lease lock is held by an unrecognized process; refusing to remove state",
        )
    # A dead holder is not evidence that its capacity is free: it may have left
    # a detached descendant consuming the allocation.  The durable job record
    # and the adopted-orphan sweep must both positively confirm otherwise before
    # the state is deleted and the allocation reported free.
    confirmed, detail = _confirm_recorded_job_containment(descriptor_path.parent)
    if not confirmed:
        return "blocked", detail
    live_leftovers = _sweep_leftover_owned_processes()
    if live_leftovers:
        return (
            "blocked",
            (
                "owned descendants are still running after containment "
                f"({len(live_leftovers)} pid(s)): "
                + ", ".join(str(pid) for pid in live_leftovers[:16])
            ),
        )
    _remove_allocation_state(descriptor_path)
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
        "--run-timeout",
        type=float,
        default=None,
        help="deadline in seconds for --run, separate from the prerequisite timeout",
    )
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
    admission = validate_declaration(declaration)
    if overall_status(admission) != "ok":
        return (
            "fail",
            "the declaration failed prerequisite admission; refusing to reserve or run",
            {"checks": [asdict(item) for item in admission]},
        )
    prerequisites = prerequisite_checks(declaration, repo_root)
    if overall_status(prerequisites) != "ok":
        return (
            "fail",
            "runtime and asset prerequisites failed admission; refusing to reserve or run",
            {"checks": [asdict(item) for item in prerequisites]},
        )

    # Resolve the job directory before admission so disk checks measure the
    # filesystems the job will actually write to, not the caller's TMPDIR.
    job_dir = _job_directory(args, declaration, repo_root)
    _populate_job_filesystem_facts(facts, job_dir)
    try:
        descriptor_path, _descriptor, _lock_fd = _reserve_allocation(
            declaration, repo_root, job_dir, facts
        )
        digest = _pin_declaration(declaration_path, declaration, job_dir, descriptor_path)
    except (OSError, ValueError) as exc:
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

    resources = evaluate_resources(declaration, facts, repo_root)
    if overall_status(resources) != "ok":
        _release_allocation(declaration, repo_root)
        return (
            "fail",
            "the observed allocation failed verification; refusing to launch the qualification job",
            {"checks": [asdict(item) for item in resources], **extra},
        )

    command = list(args.run)
    child_env = dict(os.environ)
    for name, child in (
        ("TMPDIR", job_dir / "tmp"),
        ("POKERED_QUALIFICATION_JOB_DIR", job_dir),
        ("POKERED_QUALIFICATION_EVIDENCE_DIR", job_dir / "evidence"),
    ):
        child.mkdir(parents=True, exist_ok=True)
        child_env[name] = str(child)

    # Durable job ownership is written before the command runs, so a holder that
    # dies mid-run still leaves a verifiable record of what it launched.
    def _record_start(process: subprocess.Popen[str]) -> None:
        _write_job_run_record(job_dir, process, containment_confirmed=False)

    process = run_command(
        command,
        repo_root,
        timeout=_qualification_timeout(getattr(args, "run_timeout", None)),
        env=child_env,
        on_start=_record_start,
    )
    containment = last_command_containment()
    containment_proven = containment is not None and containment.proven
    _update_job_run_record(job_dir, containment_confirmed=containment_proven)
    if process.stdout:
        print(process.stdout, end="")
    if process.stderr:
        print(process.stderr, end="", file=sys.stderr)
    if not containment_proven:
        detail = (
            containment.detail
            if containment is not None
            else "the command's containment outcome was not observed"
        )
        return (
            "blocked",
            (
                "the leased command's descendant containment could not be proven "
                f"({detail}); the lease is kept because the allocation may still be in use"
            ),
            extra,
        )
    release_status, _release_message = _release_allocation(declaration, repo_root)
    if release_status != "ok":
        return "fail", f"leased command exited {process.returncode}; {_release_message}", extra
    status = "ok" if process.returncode == 0 else "fail"
    return status, f"leased command exited {process.returncode}", extra


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _install_signal_handlers()
    repo_root = args.repo_root.resolve()
    facts = collect_facts(repo_root)

    def facts_payload() -> dict[str, Any]:
        payload = asdict(facts)
        payload["shm_path"] = _sanitize_path(facts.shm_path, repo_root)
        for key in ("job_tmp_path", "job_evidence_path"):
            value = payload.get(key)
            if value is not None:
                payload[key] = _sanitize_path(value, repo_root)
        return payload

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
        "facts": facts_payload(),
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
            _populate_job_filesystem_facts(facts, _job_directory(args, declaration, repo_root))
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
    payload["facts"] = facts_payload()
    payload = _redact_payload(
        payload, _collect_redactions(repo_root, declaration, declaration_path)
    )
    _emit(payload, args)
    return 0 if payload["overall"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
