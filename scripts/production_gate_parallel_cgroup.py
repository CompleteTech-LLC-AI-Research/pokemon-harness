"""Kernel-owned job membership through a delegated cgroup v2 subtree (#107 F4).

Lineage and sessions can be lost when a process is orphaned, but cgroup membership is inherited by
every fork and cannot be shed without write access to another cgroup.  When the cgroup this process
runs in is delegated to the executing user (the usual systemd user slice), each job gets its own
child cgroup, the job-local guardian moves itself into it before it starts the worker, and cleanup is
``cgroup.kill`` plus the kernel's own ``populated 0`` reading.  Where delegation, ``cgroup.kill`` or
the join is not available nothing here is claimed and the adoption proof of the process module applies.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path

CGROUP_ROOT = Path("/sys/fs/cgroup")
KILL_WAIT_SECONDS = 5.0
REQUIRED_FILES = ("cgroup.procs", "cgroup.events", "cgroup.kill")


def own_cgroup_path(proc_root: Path = Path("/proc")) -> str | None:
    """The unified-hierarchy path of this process (the ``0::`` line), or None."""

    try:
        for line in (proc_root / "self" / "cgroup").read_text(encoding="ascii").splitlines():
            if line.startswith("0::"):
                return line[3:]
    except OSError:
        return None
    return None


class JobCgroup:
    """One job's cgroup: created by the orchestrator, joined by the guardian, killed and removed."""

    def __init__(
        self,
        path: Path | None,
        reason: str = "",
        *,
        root: Path = CGROUP_ROOT,
        proc_root: Path = Path("/proc"),
    ) -> None:
        self.path = path
        self.reason = reason
        self.root = root
        self.proc_root = proc_root

    @property
    def available(self) -> bool:
        return self.path is not None

    @property
    def relative(self) -> str:
        return "/" + self.path.relative_to(self.root).as_posix() if self.path is not None else ""

    @classmethod
    def create(
        cls,
        name: str,
        *,
        root: Path = CGROUP_ROOT,
        own: str | None = None,
        required: tuple[str, ...] = REQUIRED_FILES,
    ) -> JobCgroup:
        own = own if own is not None else own_cgroup_path()
        if own is None:
            return cls(None, "no cgroup v2 hierarchy for this process", root=root)
        parent = root / own.lstrip("/")
        try:
            if not (os.access(parent, os.W_OK | os.X_OK) and (parent / "cgroup.procs").exists()):
                return cls(
                    None, "this process's cgroup is not delegated to the executing user", root=root
                )
            path = parent / name
            path.mkdir(mode=0o700)
        except OSError as exc:
            return cls(None, f"cgroup creation refused: {type(exc).__name__}", root=root)
        missing = [item for item in required if not (path / item).exists()]
        if missing:
            os.rmdir(path)
            return cls(None, f"the kernel exposes no {missing[0]}", root=root)
        return cls(path, root=root)

    def contains(self, pid: int) -> bool:
        """Kernel membership of ``pid`` (read from its own /proc entry)."""

        if self.path is None:
            return False
        try:
            text = (self.proc_root / str(pid) / "cgroup").read_text(encoding="ascii")
        except OSError:
            return False
        return f"0::{self.relative}" in text.splitlines()

    def populated(self) -> bool | None:
        if self.path is None:
            return None
        try:
            lines = (self.path / "cgroup.events").read_text(encoding="ascii").splitlines()
        except OSError:
            return None
        for line in lines:
            if line.startswith("populated "):
                return line.split()[1] == "1"
        return None

    def kill(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> bool:
        """Kill every member atomically and return True only when the kernel reports it empty."""

        if self.path is None:
            return False
        try:
            (self.path / "cgroup.kill").write_text("1", encoding="ascii")
        except OSError:
            return False
        limit = clock() + KILL_WAIT_SECONDS
        while clock() < limit:
            if self.populated() is False:
                return True
            sleeper(0.02)
        return self.populated() is False

    def remove(self) -> None:
        if self.path is not None:
            try:
                os.rmdir(self.path)
            except OSError:
                pass


def kernel_ownership(
    required: bool, *, create: Callable[[str], JobCgroup] | None = None
) -> dict[str, object]:
    """Whether whole-tree kernel ownership (a delegated per-job cgroup) can be established here.

    One empty probe cgroup is created and removed.  ``admitted`` is False only when ownership is
    required and unavailable: the run is then refused BEFORE any child exists.  A run that is not
    required to be qualified proceeds on adoption and retention evidence alone and is labelled
    unqualified; neither outcome ever claims full #107 acceptance.
    """

    probe = (create or JobCgroup.create)(f"pokered-probe-{os.getpid()}")
    qualified = probe.available
    if qualified:
        probe.remove()
    return {
        "required": required,
        "qualified": qualified,
        "admitted": qualified or not required,
        "reason": "" if qualified else probe.reason,
        "qualification": (
            "kernel ownership available; full #107 acceptance is not claimed"
            if qualified
            else "UNQUALIFIED_COMPATIBILITY: no kernel ownership, retention evidence only"
        ),
    }


class OwnershipError(RuntimeError):
    """Kernel ownership was required for a job and could not be established."""


def qualification_after(
    ownership: Mapping[str, object], cleanup: Mapping[str, Mapping[str, object]]
) -> dict[str, object]:
    """Final kernel-ownership truth from per-job facts, never from a label or a probe alone.

    A run is kernel-qualified only if ownership was REQUIRED, the probe passed and every job that
    started both joined its cgroup and had it emptied by the kernel's own reading.  The explicit
    unqualified compatibility mode (not required) can never be qualified, whatever the probe saw.
    """

    jobs: dict[str, dict[str, bool]] = {}
    for job_id, entry in cleanup.items():
        tree = entry.get("process_tree") if isinstance(entry, Mapping) else None
        adoption = tree.get("adoption") if isinstance(tree, Mapping) else None
        group = adoption.get("cgroup") if isinstance(adoption, Mapping) else None
        group = group if isinstance(group, Mapping) else {}
        jobs[job_id] = {
            "joined": group.get("joined") is True,
            "emptied": group.get("emptied") is True,
        }
    qualified = (
        ownership.get("required") is True
        and ownership.get("qualified") is True
        and bool(jobs)
        and all(fact["joined"] and fact["emptied"] for fact in jobs.values())
    )
    return {
        **ownership,
        "jobs": jobs,
        "qualified_final": qualified,
        "qualification_final": (
            "kernel ownership proven for every started job; full #107 acceptance is not claimed"
            if qualified
            else "UNQUALIFIED: kernel ownership was not required, not available or not proven per job"
        ),
    }


__all__ = (
    "JobCgroup",
    "OwnershipError",
    "kernel_ownership",
    "own_cgroup_path",
    "qualification_after",
)
