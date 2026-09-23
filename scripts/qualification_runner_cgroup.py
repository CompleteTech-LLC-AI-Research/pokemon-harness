"""cgroup, CPU and memory fact readers.

Split from ``scripts/qualification_runner.py`` for issue #112 with no
behavior change: the code below is copied verbatim except that calls to
facade-owned, monkeypatch-patched entry points resolve through ``_entry`` so
attribute patches on ``scripts.qualification_runner`` stay visible.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


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
    raw = cgroup_text if cgroup_text is not None else _entry._read_text(Path("/proc/self/cgroup"))
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
        raw = _entry._read_text(candidate)
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


def _cgroup_memory_headroom(
    paths: list[Path], limit_filename: str, usage_filename: str
) -> tuple[int | None, bool]:
    """Return the tightest remaining memory under any finite cgroup limit.

    A finite limit overstates what this job can use when an ancestor is already
    consuming most of it: the headroom is ``limit - usage`` at each level that
    imposes a finite limit, and the tightest such remainder is the real bound.
    The second element is ``False`` when a finite limit or its usage could not
    be read, so admission fails closed instead of assuming the limit is free.
    """

    headroom: int | None = None
    observable = True
    for path in paths:
        limit_file = path / limit_filename
        if not limit_file.exists():
            continue
        raw_limit = _entry._read_text(limit_file)
        if raw_limit is None:
            observable = False
            continue
        value = raw_limit.strip()
        if not value or value == "max" or not value.isdigit():
            continue
        number = int(value)
        if number >= 1 << 60:
            continue
        raw_usage = _entry._read_text(path / usage_filename)
        if raw_usage is None or not raw_usage.strip().isdigit():
            observable = False
            continue
        remaining = number - int(raw_usage.strip())
        headroom = remaining if headroom is None else min(headroom, remaining)
    return headroom, observable


def _cgroup_cpu_quota_v2(paths: list[Path]) -> tuple[float | None, bool]:
    """Return the tightest cgroup-v2 CPU quota for *paths* and its observability.

    The second element is ``False`` when a ``cpu.max`` exists but could not be
    read or parsed.  An unreadable controller is an *unknown* bound, not an
    absent one: a leaf that looks like four cores while an unreadable ancestor
    throttles it to half a core would otherwise be admitted with no headroom.
    """

    quotas: list[float] = []
    observable = True
    for path in paths:
        candidate = path / "cpu.max"
        if not candidate.exists():
            continue
        raw = _entry._read_text(candidate)
        if raw is None:
            observable = False
            continue
        try:
            quota = _parse_cpu_max(raw)
        except ValueError:
            observable = False
            continue
        if quota is not None:
            quotas.append(quota)
    return (min(quotas) if quotas else None), observable


def _cgroup_cpu_quota_v1(paths: list[Path]) -> tuple[float | None, bool]:
    """Return the tightest cgroup-v1 CPU quota for *paths* and its observability.

    A quota or period file that is present but unreadable makes the effective
    quota unknown, so admission fails closed instead of reading ``-1``-like
    silence as "unlimited".
    """

    quotas: list[float] = []
    observable = True
    for path in paths:
        quota_file = path / "cpu.cfs_quota_us"
        period_file = path / "cpu.cfs_period_us"
        if not quota_file.exists() and not period_file.exists():
            continue
        quota_raw = _entry._read_text(quota_file)
        period_raw = _entry._read_text(period_file)
        if not quota_raw or not period_raw:
            observable = False
            continue
        try:
            quota_us = int(quota_raw)
            period_us = int(period_raw)
        except ValueError:
            observable = False
            continue
        if quota_us >= 0 and period_us > 0:
            quotas.append(quota_us / period_us)
    return (min(quotas) if quotas else None), observable


def _read_cgroup_facts(root: Path | None = None, cgroup_text: str | None = None) -> dict[str, Any]:
    """Read the cgroup facts for this process.

    The controller files are detected on the process cgroup and its ancestors,
    not only at the mount root: a cgroup-v2 hierarchy can expose ``cpu.max``
    below ``/sys/fs/cgroup`` without it existing at the root itself.
    """

    base = Path("/sys/fs/cgroup") if root is None else Path(root)
    version = "unavailable"
    quota_cores: float | None = None
    quota_observable = True
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
        quota_cores, quota_observable = _cgroup_cpu_quota_v2(v2_paths)
        for path in v2_paths:
            raw_weight = _entry._read_text(path / "cpu.weight")
            if raw_weight is not None and raw_weight.isdigit():
                weight = int(raw_weight)
                break
        stat_paths = v2_paths
    elif any((path / "cpu.cfs_quota_us").exists() for path in v1_paths):
        version = "v1"
        relative = v1_relative
        primary = v1_paths[0]
        quota_cores, quota_observable = _cgroup_cpu_quota_v1(v1_paths)
        for path in v1_paths:
            shares_raw = _entry._read_text(path / "cpu.shares")
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
        memory_usage_filename = "memory.usage_in_bytes"
    else:
        memory_paths = v2_paths
        memory_filename = "memory.max"
        memory_usage_filename = "memory.current"
    memory_limit_bytes, memory_limit_observable = _cgroup_memory_limit(
        memory_paths, memory_filename
    )
    memory_headroom_bytes, memory_headroom_observable = _cgroup_memory_headroom(
        memory_paths, memory_filename, memory_usage_filename
    )

    # An absent or unresolvable hierarchy is an *unknown* bound, not a
    # positively observed unlimited one.  When no CPU controller file was found
    # anywhere on the walk, this process is either in a cgroup whose controller
    # layout cannot be resolved or in a cgroupfs mount that is absent or hidden;
    # admission must fail closed rather than treat "I could not look" as
    # "unlimited".  The same applies to memory when no memory controller file
    # was observable at all, so an unreadable hierarchy cannot admit a job
    # against unknown CPU and memory capacity.
    if version == "unavailable":
        quota_observable = False
        if not any((path / memory_filename).exists() for path in memory_paths):
            memory_limit_observable = False

    for path in stat_paths:
        stat_raw = _entry._read_text(path / "cpu.stat")
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
        procs_raw = _entry._read_text(primary / "cgroup.procs")
        if procs_raw is not None:
            member_pids = []
            for token in procs_raw.split():
                if token.isdigit():
                    member_pids.append(int(token))
    return {
        "cgroup_version": version,
        "cpu_quota_cores": quota_cores,
        "cpu_quota_observable": quota_observable,
        "cpu_weight": weight,
        "cpu_throttled": throttled,
        "cgroup_relative_path": relative,
        "cgroup_dir": cgroup_dir,
        "cgroup_member_pids": member_pids,
        "memory_limit_bytes": memory_limit_bytes,
        "memory_limit_observable": memory_limit_observable,
        "memory_headroom_bytes": memory_headroom_bytes,
        "memory_headroom_observable": memory_headroom_observable,
    }


def _read_memory_facts() -> tuple[int | None, int | None]:
    raw = _entry._read_text(Path("/proc/meminfo"))
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
    raw = _entry._read_text(Path(cgroup_dir) / "cgroup.stat")
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
    raw = _entry._read_text(directory / "cgroup.procs")
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
        parent_own = _entry._read_text(parent / "cgroup.procs")
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
    raw = _entry._read_text(Path("/proc/pressure/cpu"))
    if raw is None:
        return None
    for line in raw.splitlines():
        if not line.startswith("some"):
            continue
        for token in line.split():
            if token.startswith("avg300="):
                return float(token.split("=", 1)[1])
    return None


# Call-time indirection so facade-level monkeypatches stay visible here.
import scripts.qualification_runner as _entry
