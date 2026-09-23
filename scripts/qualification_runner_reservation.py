"""Reservation and resource evaluation.

Split from ``scripts/qualification_runner.py`` for issue #112 with no
behavior change: the code below is copied verbatim except that calls to
facade-owned, monkeypatch-patched entry points resolve through ``_entry`` so
attribute patches on ``scripts.qualification_runner`` stay visible.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Direct names this module calls; cyclic back-edges go through _entry.
from scripts.qualification_runner_cgroup import parse_cpuset
from scripts.qualification_runner_declaration import (
    _cgroup_members_are_owned,
    _descriptor_lease_status,
    _effective_cpu_cores,
    _load_allocation_descriptor,
    _reservation_owned_pids,
)
from scripts.qualification_runner_facts import CheckResult, RunnerFacts, _is_sha256, _result
from scripts.qualification_runner_host import (
    _descriptor_is_private,
    _directory_is_private,
    _path_within,
    _resolve_declared_path,
    _sha256_of_file,
)
from scripts.qualification_runner_model import (
    _CPU_BINDING_MECHANISMS,
    _DEFAULT_FOREIGN_CPU_CORES_TOLERANCE,
    _DESCRIPTOR_VERSION,
    _RESERVATION_MECHANISMS,
)


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
    measured, interval = _entry._measure_competing_cpu_cores()
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
        # A finite limit is only headroom the job can use if the cgroup is not
        # already near it.  Admit against limit-minus-current-usage, not the
        # bare limit, or a nearly-full ancestor is admitted as if it were empty.
        if not facts.memory_headroom_observable:
            return None, "the allocation's cgroup memory usage could not be read"
        if facts.memory_headroom_bytes is None:
            return None, "the allocation's cgroup memory headroom is not observable"
        bounds["cgroup-headroom"] = max(facts.memory_headroom_bytes, 0)
        detail = "usable memory bytes (the lowest of host-total, host-available, cgroup-headroom)"
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
    if not facts.cpu_quota_observable:
        # An unreadable ancestor controller is unknown capacity, not unlimited
        # capacity: a leaf that reports four cores under a half-core ancestor
        # that cannot be read must never be admitted as four usable cores.
        results.append(
            _result(
                "cpu-quota",
                "unsupported",
                declared_quota,
                facts.cpu_quota_cores,
                "the effective cgroup CPU quota could not be observed; "
                "refusing to treat an unknown bound as unlimited",
            )
        )
    elif mechanism == "cgroup-quota":
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
    # Availability and writability are checked independently of the size
    # minimum.  A declaration that requires no minimum still needs a writable
    # shared-memory namespace to run the qualification job, so a zero minimum
    # must not be able to opt out of the host-fact check.
    if "shm-missing" in facts.unsupported:
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
    elif declared_shm <= 0:
        results.append(
            _result(
                "shm",
                "ok",
                declared_shm,
                facts.shm_size_bytes,
                "shared memory is available and writable; no size minimum declared",
            )
        )
    else:
        # ``shm_size_bytes`` is the filesystem's total size, which can be
        # almost entirely reserved; admission must use the space actually
        # available to an unprivileged writer.
        observed_shm = facts.shm_available_bytes
        status = (
            "unsupported"
            if observed_shm is None
            else ("ok" if observed_shm >= declared_shm else "fail")
        )
        results.append(
            _result(
                "shm",
                status,
                declared_shm,
                observed_shm,
                "available writable shared memory bytes",
            )
        )

    return results


# Call-time indirection so facade-level monkeypatches stay visible here.
import scripts.qualification_runner as _entry
