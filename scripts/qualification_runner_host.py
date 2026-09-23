"""Filesystem, identity and process helpers.

Split from ``scripts/qualification_runner.py`` for issue #112 with no
behavior change: the code below is copied verbatim except that calls to
facade-owned, monkeypatch-patched entry points resolve through ``_entry`` so
attribute patches on ``scripts.qualification_runner`` stay visible.
"""

from __future__ import annotations

import hashlib
import os
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Direct names this module calls; cyclic back-edges go through _entry.
from scripts.qualification_runner_model import _HELD_LEASE_FDS


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
        stat_text = _entry._read_proc_stat(current)
        if stat_text is None:
            break
        closing = stat_text.rfind(")")
        if closing == -1:
            _entry._note_proc_enumeration_failure(
                f"the state of live process {current} was malformed while walking the process tree"
            )
            break
        fields = stat_text[closing + 1 :].split()
        if len(fields) < 2:
            _entry._note_proc_enumeration_failure(
                f"the state of live process {current} was incomplete while walking the process tree"
            )
            break
        try:
            current = int(fields[1])
        except ValueError:
            _entry._note_proc_enumeration_failure(
                f"the parent pid of live process {current} was unreadable while walking the "
                "process tree"
            )
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
        return not _entry._pid_is_zombie(pid)
    return not _entry._pid_is_zombie(pid)


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
        _entry._note_proc_enumeration_failure(
            "the process table could not be enumerated while scanning the process tree"
        )
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

    text = _entry._read_text(Path(f"/proc/{pid}/status"))
    if text is None:
        return None
    for line in text.splitlines():
        if line.startswith("Cpus_allowed_list:"):
            _, _, value = line.partition(":")
            try:
                return _entry.parse_cpuset(value.strip())
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
        cpus = _entry._observed_affinity(int(entry.name))
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

    owned = set(_entry._process_tree_pids(os.getpid()))
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
        if _entry._pid_is_zombie(pid):
            continue
        cpus = _observed_task_affinities(pid)
        if cpus is None:
            if _entry._pid_exists(pid):
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

    raw = _entry._read_text(Path("/proc/stat"))
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

    interval = _entry._foreign_sample_seconds() if interval is None else interval
    hz = clock_ticks if clock_ticks is not None else _entry._clock_ticks_per_second()
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

    holders = _entry._observe_flock_holders(lock_path)
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


# Call-time indirection so facade-level monkeypatches stay visible here.
import scripts.qualification_runner as _entry
