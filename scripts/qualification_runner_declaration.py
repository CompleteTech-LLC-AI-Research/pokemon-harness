"""Declaration and descriptor validation.

Split from ``scripts/qualification_runner.py`` for issue #112 with no
behavior change: the code below is copied verbatim except that calls to
facade-owned, monkeypatch-patched entry points resolve through ``_entry`` so
attribute patches on ``scripts.qualification_runner`` stay visible.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Direct names this module calls; cyclic back-edges go through _entry.
from scripts.qualification_runner_cgroup import parse_cpuset
from scripts.qualification_runner_facts import CheckResult, RunnerFacts, _is_sha256, _result
from scripts.qualification_runner_host import (
    _lock_identity,
    _lock_identity_matches,
    _path_within,
    _process_start_time,
    _sha256_of_file,
)
from scripts.qualification_runner_model import _RESERVATION_MECHANISMS, _SHA256_RE, SCHEMA_VERSION


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


def _pinned_descriptor_binding_error(
    declaration: dict[str, Any], descriptor_path: Path
) -> str | None:
    """Return why *descriptor_path* does not belong to *declaration*, or ``None``.

    A saved declaration names exactly one lease.  If that lease was released and
    a later lease reused the same descriptor path, the declaration's pinned
    digest no longer matches the bytes on disk.  Acting on the new bytes would
    signal or remove state this declaration never authorized, so the binding is
    re-checked by digest before any release or recovery touches the lease.
    """

    reservation = declaration.get("reservation")
    reservation = reservation if isinstance(reservation, dict) else {}
    pinned = reservation.get("descriptor_sha256")
    if not _is_sha256(pinned):
        return "the declaration does not pin the allocation descriptor by SHA-256"
    try:
        actual = _sha256_of_file(descriptor_path)
    except OSError as exc:
        return f"the pinned allocation descriptor could not be read ({exc})"
    if actual != str(pinned).strip().lower():
        return (
            "the pinned allocation descriptor digest no longer matches this declaration; "
            "the descriptor belongs to a different lease, so no state may be signalled "
            "or removed"
        )
    return None


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
    if not _entry._pid_alive(holder):
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
    holders = _entry._observe_flock_holders(lock)
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
        owned = set(_entry._process_tree_pids(holder_pid))
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
        owned.update(_entry._process_tree_pids(holder))
    owned.update(facts.process_tree_pids)
    return owned


# Call-time indirection so facade-level monkeypatches stay visible here.
import scripts.qualification_runner as _entry
