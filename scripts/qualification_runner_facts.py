"""Host fact collection and result records.

Split from ``scripts/qualification_runner.py`` for issue #112 with no
behavior change: the code below is copied verbatim except that calls to
facade-owned, monkeypatch-patched entry points resolve through ``_entry`` so
attribute patches on ``scripts.qualification_runner`` stay visible.
"""

from __future__ import annotations

import os
import platform
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Direct names this module calls; cyclic back-edges go through _entry.
from scripts.qualification_runner_cgroup import (
    _cgroup_descendant_counts,
    _cgroup_sibling_competitors,
    _read_cgroup_facts,
    _read_memory_facts,
    _read_psi_cpu,
)
from scripts.qualification_runner_host import (
    _foreign_process_affinity,
    _process_ancestor_pids,
    _process_start_time,
)
from scripts.qualification_runner_model import _SHA1_RE, _SHA256_RE, SCHEMA_VERSION


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
    cpu_quota_observable: bool = True
    cpu_quota_status: str = "unknown"
    cgroup_cpu_some_avg300: float | None = None
    cgroup_visibility: list[str] = field(default_factory=list)
    cpu_weight: int | None = None
    cpu_throttled: dict[str, int] | None = None
    memory_total_bytes: int | None = None
    memory_available_bytes: int | None = None
    memory_limit_bytes: int | None = None
    memory_limit_observable: bool = True
    memory_headroom_bytes: int | None = None
    memory_headroom_observable: bool = True
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
    shm_available_bytes: int | None = None
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
    facts.process_tree_pids = _entry._process_tree_pids()
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
            # f_bavail is what an unprivileged writer can actually allocate; the
            # total size (f_blocks) can be almost entirely reserved elsewhere.
            facts.shm_available_bytes = stats.f_bsize * stats.f_bavail
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
    facts.cpu_quota_observable = cgroup["cpu_quota_observable"]
    facts.cpu_quota_status = cgroup["cpu_quota_status"]
    facts.cgroup_cpu_some_avg300 = cgroup["cgroup_cpu_some_avg300"]
    facts.cgroup_visibility = cgroup["cgroup_visibility"]
    facts.cpu_weight = cgroup["cpu_weight"]
    facts.cpu_throttled = cgroup["cpu_throttled"]
    facts.memory_limit_bytes = cgroup["memory_limit_bytes"]
    facts.memory_limit_observable = cgroup["memory_limit_observable"]
    facts.memory_headroom_bytes = cgroup["memory_headroom_bytes"]
    facts.memory_headroom_observable = cgroup["memory_headroom_observable"]
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


# Call-time indirection so facade-level monkeypatches stay visible here.
import scripts.qualification_runner as _entry
