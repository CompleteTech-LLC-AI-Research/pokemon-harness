"""Allocation state, reserve, release, recover.

Split from ``scripts/qualification_runner.py`` for issue #112 with no
behavior change: the code below is copied verbatim except that calls to
facade-owned, monkeypatch-patched entry points resolve through ``_entry`` so
attribute patches on ``scripts.qualification_runner`` stay visible.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

# Direct names this module calls; cyclic back-edges go through _entry.
from scripts.qualification_runner_cgroup import parse_cpuset
from scripts.qualification_runner_command import (
    _JOB_LAUNCH_INTENT_NAME,
    _JOB_OWNERSHIP_TOKEN_ENV,
    _JOB_RUN_RECORD_NAME,
    JobOwnershipError,
    _await_no_token_owned_survivors,
    _proc_enumeration_failures,
    _process_group_members,
    _reset_proc_enumeration_failures,
    _sweep_leftover_owned_processes,
    _terminate_pids,
)
from scripts.qualification_runner_declaration import (
    _load_allocation_descriptor,
    _pinned_descriptor_binding_error,
)
from scripts.qualification_runner_facts import RunnerFacts
from scripts.qualification_runner_host import (
    _close_held_lease_descriptors,
    _iso_now,
    _lock_identity,
    _lock_identity_matches,
    _lock_owned_by_this_process,
    _path_within,
    _process_start_time,
    _resolve_declared_path,
    _sha256_of_file,
)
from scripts.qualification_runner_model import _DESCRIPTOR_VERSION, _HELD_LEASE_FDS
from scripts.qualification_runner_report import _prepare_job_directory

_UNRESOLVED_ALLOCATION_SUFFIX = ".unresolved-allocation.json"


class UnresolvedAllocationError(ValueError):
    """A prior lease on this host lock was never proven contained.

    A blocked holder keeps its lease only while the process lives; the kernel
    drops the flock when that process exits.  A host-wide record written next to
    the host lock keeps the exclusion in force across holder exit, and every
    reservation checks it under the same lock before it admits a new lease.
    """


def _unresolved_allocation_path(host_lock: Path) -> Path:
    """Return the host-wide record path for *host_lock*.

    The record sits beside the host lock, so any job directory that reserves the
    same host-wide lock finds the same record regardless of its own job dir.
    """

    return host_lock.with_name(host_lock.name + _UNRESOLVED_ALLOCATION_SUFFIX)


def _declared_host_lock(declaration: dict[str, Any], repo_root: Path) -> Path | None:
    reservation = declaration.get("reservation")
    if not isinstance(reservation, dict):
        return None
    return _resolve_declared_path(reservation.get("host_lock_path"), repo_root)


def _descriptor_lock_path(descriptor_path: Path) -> Path | None:
    """Best-effort read of the host lock a descriptor names, without validating it."""

    try:
        document = json.loads(descriptor_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    raw = document.get("lock_path")
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw)


def _write_unresolved_allocation(
    host_lock: Path,
    *,
    reason: str,
    descriptor_path: Path | None = None,
    lock_identity: Any = None,
) -> Path | None:
    """Durably record that the allocation on *host_lock* is still unresolved.

    This must survive the holder's exit, so it is written to disk rather than
    held only in process memory.  It is created atomically (write, then rename)
    so a reader never observes a partial record.
    """

    record_path = _unresolved_allocation_path(host_lock)
    identity = lock_identity if isinstance(lock_identity, dict) else _lock_identity(host_lock)
    document = {
        "record_version": 1,
        "kind": "unresolved-allocation",
        "lock_path": str(host_lock),
        "lock_identity": identity,
        "holder_pid": os.getpid(),
        "descriptor_path": str(descriptor_path) if descriptor_path is not None else None,
        "reason": reason,
        "created_at": _iso_now(),
    }
    tmp = record_path.with_name(f"{record_path.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, record_path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return None
    return record_path


def _mark_allocation_unresolved(descriptor_path: Path | None, reason: str) -> Path | None:
    """Record the unresolved allocation named by a descriptor, if it is readable."""

    if descriptor_path is None:
        return None
    lock_path = _descriptor_lock_path(descriptor_path)
    if lock_path is None:
        return None
    return _write_unresolved_allocation(lock_path, reason=reason, descriptor_path=descriptor_path)


def _blocked_allocation(descriptor_path: Path | None, reason: str) -> tuple[str, str]:
    """Keep the lease blocked and persist the reason across this process's exit."""

    _mark_allocation_unresolved(descriptor_path, reason)
    return "blocked", reason


def _read_unresolved_allocation(record_path: Path) -> dict[str, Any] | None:
    """Return the parsed host-wide unresolved record, or ``None`` if unusable."""

    try:
        document = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _clear_unresolved_allocation(
    host_lock: Path | None, *, descriptor_path: Path | None = None
) -> None:
    """Remove the unresolved record for *host_lock* only when it names this lease.

    The record is host-wide, so a release can race another job directory that
    reserves the same lock and writes its own record: the holder drops its flock
    before the state removal finishes, which lets a competing reservation admit,
    block, and record its exclusion in that window.  Removal therefore runs
    while holding the host lock and deletes the record only when its recorded
    descriptor names the lease being cleared.  Anything else -- a different
    lease's record, an unreadable record, no verifiable descriptor, or a lock
    this process cannot acquire -- is left in force rather than erased.
    """

    import fcntl

    if host_lock is None or descriptor_path is None:
        return
    record_path = _unresolved_allocation_path(host_lock)
    if not record_path.exists() and not record_path.is_symlink():
        return
    try:
        lock_fd = os.open(host_lock, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # A competing reservation (or a live lease) holds the lock, so the
            # record cannot be inspected and removed atomically; keep it.
            return
        document = _read_unresolved_allocation(record_path)
        recorded = document.get("descriptor_path") if isinstance(document, dict) else None
        if recorded != str(descriptor_path):
            # The record belongs to a different lease; erasing it would drop that
            # allocation's durable exclusion and admit new work over live use.
            return
        try:
            record_path.unlink()
        except OSError:
            pass
    finally:
        os.close(lock_fd)


def _unresolved_allocation_present(host_lock: Path | None) -> Path | None:
    """Return the unresolved record path for *host_lock* when one is present."""

    if host_lock is None:
        return None
    record_path = _unresolved_allocation_path(host_lock)
    if record_path.exists() or record_path.is_symlink():
        return record_path
    return None


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

    # A prior holder that exited while its containment was unproven left a
    # host-wide record beside this lock.  The flock it drops on exit is not the
    # exclusion that matters: a new job directory reserving the same host lock
    # must be refused until containment is proven or an operator recovers the
    # unresolved allocation.  Check under the lock so two reservations cannot
    # both pass the record check and then admit.
    unresolved = _unresolved_allocation_present(host_lock)
    if unresolved is not None:
        os.close(lock_fd)
        raise UnresolvedAllocationError(
            "a previous allocation on this host lock was never proven contained "
            f"({unresolved.name}); refusing to admit a new lease until its owned "
            "work is contained or the unresolved allocation is recovered by an operator"
        )

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
    job_dir: Path,
    process: subprocess.Popen[str],
    *,
    containment_confirmed: bool,
    job_token: str | None = None,
) -> None:
    """Persist verifiable ownership of the job running under a lease.

    The in-process leftover pid set cannot survive the holder's death, so a
    later ``--recover`` has no way to tell whether a dead holder left a
    descendant consuming the allocation.  The record ties the job directory to
    the launched job's own pid/start time and its process group/session, and
    ``containment_confirmed`` is set only after the runner proved every
    descendant was gone.

    The record is the *only* thing that can attribute a surviving descendant to
    this lease, so a write that fails is a terminal failure rather than a
    silently unobservable detail: the caller tears the command down while it is
    still inside a proved-containment window, and the lease stays held.
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
        "job_token": job_token,
        "containment_confirmed": containment_confirmed,
        "recorded_at": _iso_now(),
    }
    path = job_dir / _JOB_RUN_RECORD_NAME
    try:
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError as exc:
        raise JobOwnershipError(
            f"the launched job's durable ownership record could not be written ({exc}); "
            "the command is being torn down because its descendants cannot be attributed"
        ) from exc


def _write_job_launch_intent(job_dir: Path, command: list[str]) -> None:
    """Record the intent to launch *command* before the command is spawned.

    Recovery must never delete a lease whose launched job cannot be identified.
    Writing this marker first makes the two states distinguishable: a lease with
    an intent but no ownership record is *unknown* work and stays blocked, while
    a lease with neither never launched anything and is safe to remove.
    """

    digest = hashlib.sha256("\x00".join(command).encode("utf-8", "surrogateescape")).hexdigest()
    intent = {
        "intent_version": 1,
        "command_sha256": digest,
        "recorded_at": _iso_now(),
    }
    path = job_dir / _JOB_LAUNCH_INTENT_NAME
    try:
        path.write_text(json.dumps(intent, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
    except OSError as exc:
        raise JobOwnershipError(
            f"the job's launch intent could not be persisted ({exc}); refusing to spawn a "
            "command whose ownership could not be recorded"
        ) from exc


def _remove_launch_intent(job_dir: Path) -> None:
    """Best-effort removal of a launch intent that never produced a command.

    Only the caller that knows nothing was spawned may call this, because the
    intent is what keeps recovery from treating unidentifiable work as an idle
    lease.  A partially written or chmod-failed intent must not outlive the
    failure it recorded.
    """

    try:
        (job_dir / _JOB_LAUNCH_INTENT_NAME).unlink()
    except OSError:
        return


def _recorded_start_time_at_or_after(observed: str | None, recorded: Any) -> bool:
    """Return ``True`` when *observed* cannot predate the recorded start time.

    Start times are the ``starttime`` field of ``/proc/<pid>/stat`` in clock
    ticks since boot, so a process that started *before* the recorded job can
    never be one of its descendants.  Anything that cannot be compared is
    treated as unprovable rather than as ownership.
    """

    if observed is None or not isinstance(recorded, str) or not recorded.strip():
        return False
    try:
        return int(observed) >= int(recorded)
    except ValueError:
        return False


def _process_environ(pid: int) -> dict[str, str] | None:
    """Return *pid*'s environment, or ``None`` when it cannot be observed.

    ``/proc/<pid>/environ`` is the initial environment a process was started
    with; children inherit it.  It is therefore durable evidence of which
    launch a process belongs to, unlike a reusable pid or process-group id.
    """

    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    environ: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        if not entry:
            continue
        name, sep, value = entry.partition(b"=")
        if not sep:
            continue
        environ[name.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return environ or None


def _recorded_job_group_ownership(
    pgid: int, job_start_time: Any, job_token: Any
) -> tuple[bool, str]:
    """Prove that every live member of *pgid* can belong to the recorded job.

    A process-group id is reusable once its members exit, so recovering a stale
    lease must never signal a group merely because the id appears in the record.
    A start time cannot prove ownership either: an unrelated process that joined
    a reused group id starts *after* the recorded job.  Each live member must
    instead carry the launch's own ownership token in its environment, and be
    observable; otherwise the group is unproven and must be left alone.
    """

    members = [pid for pid in _process_group_members(pgid) if pid != os.getpid()]
    if not members:
        return True, ""
    if not isinstance(job_token, str) or not job_token.strip():
        return False, (
            "the recorded job has no durable ownership token; refusing to signal a group "
            "whose ownership cannot be proven"
        )
    for pid in members:
        environ = _process_environ(pid)
        if environ is None:
            return False, (
                f"the environment of process {pid} in the recorded job's group is not "
                "observable; refusing to signal a group whose ownership cannot be proven"
            )
        if environ.get(_JOB_OWNERSHIP_TOKEN_ENV) != job_token:
            return False, (
                f"process {pid} in the recorded job's group does not carry the recorded "
                "job's ownership token; refusing to signal a group whose ownership cannot "
                "be proven"
            )
        observed = _process_start_time(pid)
        if observed is None:
            return False, (
                f"the start time of process {pid} in the recorded job's group is not "
                "observable; refusing to signal a group whose ownership cannot be proven"
            )
        if not _recorded_start_time_at_or_after(observed, job_start_time):
            return False, (
                f"process {pid} in the recorded job's group started before the recorded job; "
                "refusing to signal a group whose ownership cannot be proven"
            )
    return True, ""


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
    if record is None and (job_dir / _JOB_LAUNCH_INTENT_NAME).exists():
        return False, (
            "this lease recorded a launch intent but no job ownership record exists; "
            "refusing to signal or release an allocation whose ownership cannot be proven"
        )
    if record is not None:
        job_pid = record.get("job_pid")
        start_time = record.get("job_start_time")
        owned_pid = isinstance(job_pid, int) and not isinstance(job_pid, bool) and job_pid > 0
        if not owned_pid or not isinstance(start_time, str) or not start_time.strip():
            return False, (
                "the recorded job's ownership (pid and start time) is incomplete; refusing to "
                "signal or release an allocation whose ownership cannot be proven"
            )
        if _entry._pid_alive(job_pid):
            if _process_start_time(job_pid) == start_time:
                return False, f"the recorded qualification job (pid {job_pid}) is still running"
            # The pid now belongs to someone else.  Signalling the recorded
            # group here is exactly how recovery killed unrelated work, so the
            # record is treated as unproven ownership instead of as a target.
            return False, (
                f"pid {job_pid} belongs to an unrelated process that reused the recorded "
                "job's pid; refusing to signal a group whose ownership cannot be proven"
            )
        groups = {
            value
            for value in (record.get("job_process_group"), record.get("job_session"))
            if isinstance(value, int) and not isinstance(value, bool) and value > 0
        }
        for pgid in sorted(groups):
            group_owned, ownership_detail = _recorded_job_group_ownership(
                pgid, start_time, record.get("job_token")
            )
            if not group_owned:
                return False, ownership_detail
            confirmed, survivors = _entry._terminate_process_group(pgid)
            if not confirmed:
                return (
                    False,
                    "the recorded qualification job's process group still has live members: "
                    + ", ".join(str(pid) for pid in survivors[:16]),
                )
    if contain_adopted:
        adopted_confirmed, adopted_survivors = _entry._contain_adopted_descendants()
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
        if _entry._child_subreaper_state() != 1:
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
        if not _entry._pid_alive(pid):
            return True, ""
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return True, ""
        except OSError as exc:
            return False, f"could not signal the lease holder: {exc}"
        deadline = time.monotonic() + _entry._CHILD_TERMINATION_GRACE_SECONDS
        while time.monotonic() < deadline:
            if not _entry._pid_alive(pid):
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

    tree = set(_entry._process_tree_pids(holder)) - {os.getpid()}
    terminated, detail = _entry._terminate_and_confirm(holder)
    if not terminated:
        return False, detail
    # A detached descendant reparents onto this subreaper; contain both the
    # snapshotted tree and anything that arrived as an adopted orphan.
    remaining = sorted(
        pid
        for pid in tree
        if pid != holder and _entry._pid_alive(pid) and not _entry._pid_is_zombie(pid)
    )
    if remaining:
        confirmed, survivors = _terminate_pids(remaining)
        if not confirmed:
            return False, (
                "the lease holder's descendants are still running after SIGKILL: "
                + ", ".join(str(pid) for pid in survivors[:16])
            )
    adopted_confirmed, adopted_survivors = _entry._contain_adopted_descendants()
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
    descriptor_path = _allocation_descriptor_path(declaration, repo_root)

    def blocked(reason: str) -> tuple[str, str]:
        # The lease state is kept, but a holder that exits drops the flock it
        # held.  Persist the reason beside the host lock so a later reservation
        # on the same host lock cannot admit new work until containment is
        # proven or an operator recovers the unresolved allocation.
        _mark_allocation_unresolved(descriptor_path, reason)
        return "blocked", reason

    _reset_proc_enumeration_failures()
    live_leftovers = _sweep_leftover_owned_processes()
    if live_leftovers:
        return blocked(
            "owned descendants are still running "
            f"({len(live_leftovers)} pid(s)); the lease is kept because the "
            "allocation is still in use"
        )
    if descriptor_path is None or not descriptor_path.is_file():
        return "blocked", "no pinned allocation descriptor to release"
    binding_error = _pinned_descriptor_binding_error(declaration, descriptor_path)
    if binding_error is not None:
        return blocked(binding_error)
    descriptor, error = _load_allocation_descriptor(descriptor_path)
    if descriptor is None:
        return "fail", error
    holder = descriptor.get("holder_pid")
    owned = holder == os.getpid() or _entry._holder_is_owned(
        holder, descriptor.get("holder_start_time")
    )
    if not owned:
        return (
            "fail",
            "the recorded holder is not an owned qualification-runner lease; refusing to remove state",
        )
    raw = descriptor.get("lock_path")
    lock_path = Path(raw) if isinstance(raw, str) and raw else None
    lock_holders = _entry._observe_flock_holders(lock_path) if lock_path is not None else None
    if lock_holders is None:
        return blocked("lock ownership is not observable; refusing to remove state")
    if lock_path is not None:
        observed_identity = _lock_identity(lock_path)
        if observed_identity is None:
            return blocked("the allocation lock identity is not observable; refusing to release")
        if not _lock_identity_matches(descriptor.get("lock_identity"), observed_identity):
            return blocked(
                "the allocation lock pathname no longer names the recorded lease inode; "
                "refusing to release, because this state belongs to a different allocation"
            )
    if holder == os.getpid():
        if lock_path is not None and not _lock_owned_by_this_process(lock_path):
            return blocked("this process does not hold the recorded allocation lock")
        recorded_ok, recorded_detail = _confirm_recorded_job_containment(
            descriptor_path.parent, contain_adopted=False
        )
        if not recorded_ok:
            return blocked(recorded_detail)
    else:
        if _entry._pid_alive(holder) or holder in lock_holders:
            terminated, detail = _release_confirm_holder_tree(holder)
            if not terminated:
                return blocked(detail)
        else:
            # The holder already exited, but it may have detached a descendant
            # that is still consuming the allocation.  Confirm containment
            # before any state is removed.
            adopted_confirmed, adopted_survivors = _entry._contain_adopted_descendants()
            if not adopted_confirmed:
                return blocked(
                    "descendants of the exited holder survived containment: "
                    + ", ".join(str(pid) for pid in adopted_survivors[:16])
                )
        if lock_path is not None and holder in (_entry._observe_flock_holders(lock_path) or set()):
            return blocked("the recorded holder still holds the allocation lock")
        # This process is not the holder's ancestor, so setting its own
        # subreaper flag cannot adopt the holder's children and it cannot even
        # observe a descendant the holder detaches during teardown.  Only a job
        # record that already proved containment can stand in for that proof: a
        # launch intent with no ownership record is unidentified work, and a
        # record that never confirmed containment may still own a detached
        # descendant.  Both keep the state rather than report the allocation
        # free.  ``contain_adopted`` stays false because adoption cannot reach a
        # sibling's children.
        confirmed, detail = _confirm_recorded_job_containment(
            descriptor_path.parent, contain_adopted=False
        )
        if not confirmed:
            return blocked(detail)
    live_leftovers = _sweep_leftover_owned_processes()
    if live_leftovers:
        return blocked(
            "owned descendants are still running after release "
            f"({len(live_leftovers)} pid(s)); including "
            + ", ".join(str(pid) for pid in live_leftovers[:16])
        )
    # A teardown handler can fork a detached descendant after the holder's tree
    # was snapshotted.  This process is not its ancestor, so the owned-process
    # sweep cannot see it; the launch's inherited ownership token can.
    record, _record_error = _read_job_run_record(descriptor_path.parent)
    job_token = record.get("job_token") if isinstance(record, dict) else None
    token_survivors = _await_no_token_owned_survivors(job_token)
    if token_survivors:
        return blocked(
            "descendants of the recorded job are still running after containment "
            f"({len(token_survivors)} pid(s)); the lease is kept because the "
            "allocation is still in use: " + ", ".join(str(pid) for pid in token_survivors[:16])
        )
    enumeration_failures = _proc_enumeration_failures()
    if enumeration_failures:
        # An unreadable process table is unknown visibility, so the sweeps above
        # may have missed a live descendant; keep the lease and the exclusion.
        return blocked(
            "the process table could not be fully enumerated, so containment cannot be "
            "proven: " + "; ".join(sorted(set(enumeration_failures))[:4])
        )
    if holder == os.getpid():
        # Relinquish the lock only after every containment check has passed.  A
        # blocked release must keep effective exclusion: closing the descriptors
        # earlier would let a competing reservation acquire the lock while owned
        # work was still alive, even though the release reported it was keeping
        # the lease.
        _close_held_lease_descriptors(descriptor.get("lock_identity"))
        if lock_path is not None and _lock_owned_by_this_process(lock_path):
            return blocked("this process still holds the allocation lock after releasing it")
    _entry._remove_allocation_state(descriptor_path)
    _clear_unresolved_allocation(lock_path, descriptor_path=descriptor_path)
    return "ok", "the owned lease was relinquished and the private job state removed"


def _recover_allocation(declaration: dict[str, Any], repo_root: Path) -> tuple[str, str]:
    descriptor_path = _allocation_descriptor_path(declaration, repo_root)

    def blocked(reason: str) -> tuple[str, str]:
        # Recovery keeps the state, so a process that exits must not silently
        # free the host: persist the unresolved record beside the host lock.
        _mark_allocation_unresolved(descriptor_path, reason)
        return "blocked", reason

    _reset_proc_enumeration_failures()
    if descriptor_path is None or not descriptor_path.is_file():
        pending = _unresolved_allocation_present(_declared_host_lock(declaration, repo_root))
        if pending is not None:
            return (
                "blocked",
                (
                    "an unresolved allocation record is present for this host lock "
                    f"({pending.name}) and no lease descriptor remains to prove containment; "
                    "confirm no owned work survives before removing it"
                ),
            )
        return "ok", "no allocation descriptor to recover"
    binding_error = _pinned_descriptor_binding_error(declaration, descriptor_path)
    if binding_error is not None:
        return blocked(binding_error)
    # A recovery removes the lease state, so it must first prove no owned work
    # survived a cancelled command: a rejected signal path can leave a detached
    # descendant that would otherwise keep consuming the allocation after the
    # state was deleted and the lease reported free.
    live_leftovers = _sweep_leftover_owned_processes()
    if live_leftovers:
        return blocked(
            "owned descendants are still running "
            f"({len(live_leftovers)} pid(s)); refusing to remove lease state "
            "while the allocation is still in use"
        )
    descriptor, error = _load_allocation_descriptor(descriptor_path)
    if descriptor is None:
        return blocked(f"the descriptor could not be read; refusing to remove state ({error})")
    holder = descriptor.get("holder_pid")
    holder_valid = isinstance(holder, int) and not isinstance(holder, bool) and holder > 0
    raw = descriptor.get("lock_path")
    if not isinstance(raw, str) or not raw.strip():
        return blocked("the lease lock path is unknown; refusing to remove state")
    lock_path = Path(raw)
    lock_holders = _entry._observe_flock_holders(lock_path)
    if lock_holders is None:
        return blocked("lock ownership is not observable; refusing to remove state")
    observed_identity = _lock_identity(lock_path)
    if observed_identity is None:
        return blocked("the allocation lock identity is not observable; refusing to remove state")
    if not _lock_identity_matches(descriptor.get("lock_identity"), observed_identity):
        return blocked(
            "the allocation lock pathname no longer names the recorded lease inode; "
            "refusing to remove state that belongs to a different allocation"
        )
    if holder_valid and _entry._pid_alive(holder):
        return "fail", "the recorded holder is still running; refusing to remove state"
    if holder_valid and holder in lock_holders:
        return "fail", "the recorded holder still holds the lease; release it first"
    if lock_holders:
        return blocked(
            "the lease lock is held by an unrecognized process; refusing to remove state"
        )
    # A dead holder is not evidence that its capacity is free: it may have left
    # a detached descendant consuming the allocation.  The durable job record
    # and the adopted-orphan sweep must both positively confirm otherwise before
    # the state is deleted and the allocation reported free.
    confirmed, detail = _confirm_recorded_job_containment(descriptor_path.parent)
    if not confirmed:
        return blocked(detail)
    live_leftovers = _sweep_leftover_owned_processes()
    if live_leftovers:
        return blocked(
            "owned descendants are still running after containment "
            f"({len(live_leftovers)} pid(s)): " + ", ".join(str(pid) for pid in live_leftovers[:16])
        )
    record, _record_error = _read_job_run_record(descriptor_path.parent)
    job_token = record.get("job_token") if isinstance(record, dict) else None
    token_survivors = _await_no_token_owned_survivors(job_token)
    if token_survivors:
        return blocked(
            "descendants of the recorded job are still running after containment "
            f"({len(token_survivors)} pid(s)); refusing to remove lease state "
            "while the allocation is still in use: "
            + ", ".join(str(pid) for pid in token_survivors[:16])
        )
    enumeration_failures = _proc_enumeration_failures()
    if enumeration_failures:
        # An unreadable process table is unknown visibility, so containment was
        # not actually proven; refuse to remove the state.
        return blocked(
            "the process table could not be fully enumerated, so containment cannot be "
            "proven: " + "; ".join(sorted(set(enumeration_failures))[:4])
        )
    _entry._remove_allocation_state(descriptor_path)
    _clear_unresolved_allocation(lock_path, descriptor_path=descriptor_path)
    return "ok", "removed stale allocation state whose owner is gone"


# Call-time indirection so facade-level monkeypatches stay visible here.
import scripts.qualification_runner as _entry
