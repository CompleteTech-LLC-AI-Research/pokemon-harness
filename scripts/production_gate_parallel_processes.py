"""Owned process-tree tracking and bounded cleanup for concurrent runtime jobs (#107.4).

The real gate starts preflight, pytest and matrix children with ``start_new_session``,
so signalling the worker's process group is not enough.  Ownership here is established
only by kernel-verified identity, never by a number, a name or an environment variable:

* the root is the orchestrator's own unreaped child, pinned by a pidfd right after
  ``Popen`` (its pid cannot be recycled while it is unreaped);
* a descendant is adopted only if its parent is a verified-owned live process, and only
  after a pidfd was opened for it and its start time still equals the one seen with that
  parent, so the pidfd pins the very instance that was observed;
* every signal goes through ``pidfd_send_signal`` on a pinned instance, closing the
  window between "is it alive" and "kill it";
* nothing else is read or signalled: no other process's environment is scanned, and the
  per-launch token is an attestation carried by artifacts, never ownership.

If pidfds are unavailable the tree reports ``contained=False`` and cleanup is never
claimed proven (fail closed).  Residual limit, stated rather than hidden: a descendant
that is created and orphaned between two observations cannot be attributed without a
subreaper/guardian launcher, which this module does not provide.
"""

from __future__ import annotations

import os
import select
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

JOB_TOKEN_ENV = "POKERED_PARALLEL_JOB_TOKEN"
LISTEN_STATE = "0A"
DEFAULT_OBSERVE_SECONDS = 0.1


def read_stat(pid: int) -> tuple[int, int, str] | None:
    """Return (start ticks, parent pid, state) from /proc/<pid>/stat, or None when gone."""

    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes().decode("utf-8", "replace")
    except OSError:
        return None
    try:
        rest = raw[raw.rindex(")") + 2 :].split()
        return int(rest[19]), int(rest[1]), rest[0]
    except (ValueError, IndexError):
        return None


def process_table() -> dict[int, tuple[int, int, str]]:
    table: dict[int, tuple[int, int, str]] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return table
    for name in names:
        if name.isdigit():
            stat = read_stat(int(name))
            if stat is not None:
                table[int(name)] = stat
    return table


def socket_ports(pids: set[int]) -> dict[int, list[int]]:
    """Listening TCP ports held by ``pids`` via fd inode ownership (not by number guessing)."""

    inodes: dict[str, int] = {}
    for pid in pids:
        try:
            for entry in os.listdir(f"/proc/{pid}/fd"):
                target = os.readlink(f"/proc/{pid}/fd/{entry}")
                if target.startswith("socket:["):
                    inodes[target[8:-1]] = pid
        except OSError:
            continue
    ports: dict[int, list[int]] = {}
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(table).read_text(encoding="ascii", errors="replace").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) > 9 and fields[3] == LISTEN_STATE and fields[9] in inodes:
                port = int(fields[1].rsplit(":", 1)[1], 16)
                ports.setdefault(inodes[fields[9]], []).append(port)
    return ports


def _pidfd_open(pid: int) -> int | None:
    try:
        return os.pidfd_open(pid)
    except (AttributeError, OSError):
        return None


def _exited(descriptor: int) -> bool:
    """A pidfd is readable once the process it pins has terminated."""

    readable, _, _ = select.select([descriptor], [], [], 0)
    return bool(readable)


class OwnedTree:
    """Every process a job's worker ever started, each pinned by a verified pidfd."""

    def __init__(
        self,
        root_pid: int,
        token: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        observe_seconds: float = DEFAULT_OBSERVE_SECONDS,
    ) -> None:
        self.root_pid = root_pid
        self.token = token  # attestation only; never used to find or signal a process
        self._clock = clock
        self._observe_seconds = observe_seconds
        self._last = float("-inf")
        self._entries: dict[int, tuple[int, int]] = {}
        self.ports: set[int] = set()
        self.contained = True
        self.reason = ""
        stat = read_stat(root_pid)
        descriptor = _pidfd_open(root_pid)
        if stat is None or descriptor is None:
            self.contained = False
            self.reason = "containment not established: the root could not be pinned by a pidfd"
            if descriptor is not None:
                os.close(descriptor)
        else:
            self._entries[root_pid] = (descriptor, stat[0])

    @property
    def tracked(self) -> dict[int, int]:
        return {pid: started for pid, (_, started) in self._entries.items()}

    def _adopt(self, pid: int, seen_start: int) -> bool:
        descriptor = _pidfd_open(pid)
        if descriptor is None:
            self.contained = False
            self.reason = "containment not established: a descendant could not be pinned"
            return False
        stat = read_stat(pid)
        if stat is None or stat[0] != seen_start:
            os.close(descriptor)  # the number was recycled before the pidfd pinned it
            return False
        self._entries[pid] = (descriptor, seen_start)
        return True

    def _alive_set(self) -> set[int]:
        return {pid for pid, (fd, _) in self._entries.items() if not _exited(fd)}

    def alive(self, table: object = None) -> set[int]:
        return self._alive_set()

    def observe(self, *, force: bool = False) -> None:
        """Adopt new descendants, walking only down from verified-owned live parents."""

        now = self._clock()
        if not self.contained or (not force and now - self._last < self._observe_seconds):
            return
        self._last = now
        table = process_table()
        frontier = self._alive_set()
        grew = True
        while grew:
            grew = False
            for pid, (started, parent, _state) in table.items():
                if pid not in self._entries and parent in frontier and self._adopt(pid, started):
                    frontier.add(pid)
                    grew = True
        for ports in socket_ports(self._alive_set()).values():
            self.ports.update(ports)

    def current_ports(self) -> set[int]:
        return {p for ports in socket_ports(self._alive_set()).values() for p in ports}

    def _signal(self, pids: set[int], signum: int) -> list[int]:
        sent: list[int] = []
        for pid in sorted(pids):
            try:
                signal.pidfd_send_signal(self._entries[pid][0], signum)
                sent.append(pid)
            except OSError:
                continue
        return sent

    def terminate(
        self,
        *,
        grace_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> dict[str, Any]:
        """TERM every pinned live process, wait a bounded time, KILL the rest, prove absence."""

        self.observe(force=True)
        terminated = self._signal(self._alive_set(), signal.SIGTERM) if self.contained else []
        limit = clock() + grace_seconds
        while self._alive_set() and clock() < limit:
            sleeper(0.02)
        killed = self._signal(self._alive_set(), signal.SIGKILL)
        limit = clock() + max(grace_seconds, 1.0)
        while self._alive_set() and clock() < limit:
            sleeper(0.02)
        remaining = sorted(self._alive_set())
        surviving = sorted(self.current_ports())
        receipt = {
            "tracked": len(self._entries),
            "terminated": terminated,
            "killed": killed,
            "remaining": remaining,
            "ports_seen": sorted(self.ports),
            "surviving_ports": surviving,
            "contained": self.contained,
            "reason": self.reason,
            "proof": self.contained and not remaining and not surviving,
        }
        self.close()
        return receipt

    def straggler_check(self) -> list[int]:
        """Live owned processes (call after the worker exited normally)."""

        self.observe(force=True)
        return sorted(self._alive_set())

    def close(self) -> None:
        for descriptor, _ in self._entries.values():
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._entries = {}
