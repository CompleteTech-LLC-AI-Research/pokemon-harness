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
claimed proven (fail closed).  The window between two observations is closed by the
job-local guardian below: every job runs under a subreaper process (``prctl``
``PR_SET_CHILD_SUBREAPER``), so a descendant orphaned at any moment is re-parented to
the guardian, which terminates it and writes an authenticated receipt; a job whose
guardian could not establish the subreaper never runs its worker (fail closed).
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import hmac
import json
import os
import re
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self

from scripts.production_gate_parallel_adoption import read_subreaper, restore_adoption
from scripts.production_gate_parallel_cgroup import JobCgroup
from scripts.production_gate_parallel_cleanup import terminate_tree
from scripts.production_gate_parallel_ownership import observe_cgroup_membership
from scripts.production_gate_parallel_validation import loads_strict

JOB_TOKEN_ENV = "POKERED_PARALLEL_JOB_TOKEN"
LISTEN_STATE = "0A"
DEFAULT_OBSERVE_SECONDS = 0.1
SIGKILL_NUMBER = signal.SIGKILL
PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
ORPHAN_ROUNDS = 3


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


def read_session(pid: int) -> int | None:
    """Session id of ``pid`` (field 6 of /proc/<pid>/stat), or None when it is gone."""

    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes().decode("utf-8", "replace")
        return int(raw[raw.rindex(")") + 2 :].split()[3])
    except (OSError, ValueError, IndexError):
        return None


def children_of(pid: int) -> list[int] | None:
    """Direct children of ``pid`` from its own per-thread ``children`` files.

    Only the named process is read, never the whole process table.  GONE is told apart from
    UNREADABLE: a process whose whole /proc entry is absent, or a thread that vanished while the
    list was read (ENOENT with its entry missing, or ESRCH), has no children and adds nothing.
    Any other failure - a permission, I/O or resource error, a live process or thread whose
    ``children`` list is not offered (no CONFIG_PROC_CHILDREN), or content that is not a list of
    decimal pids - returns None: an unreadable list is never an empty one, and the caller must
    treat the tree as containment unproven.
    """

    base = Path(f"/proc/{pid}/task")
    try:
        threads = os.listdir(base)
    except (FileNotFoundError, ProcessLookupError):
        return None if Path(f"/proc/{pid}").exists() else []
    except OSError:
        return None
    found: list[int] = []
    for thread in threads:
        try:
            text = (base / thread / "children").read_text(encoding="ascii")
        except ProcessLookupError:
            continue
        except FileNotFoundError:
            if (base / thread).exists():
                return None
            continue
        except (OSError, ValueError):
            return None
        for item in text.split():
            if not item.isdigit():
                return None
            found.append(int(item))
    return found


class OrchestratorAdoption:
    """The orchestrator itself is a child subreaper while it runs jobs.

    A guardian that is SIGKILLed can no longer contain its subtree; with this flag the kernel
    re-parents that subtree to the orchestrator instead of init, so every orphan stays inside the
    orchestrator's own children list, where it can be pinned by pidfd and terminated.  The flag is
    read back, and restored when the run ends.  Where it cannot be established nothing is claimed.
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.active = False
        self.reason = "" if enabled else "disabled by the caller"
        self._previous = 0
        self._libc: Any = None
        self.restoration_pending = False
        self.restoration_problem = ""

    def verify(self) -> bool:
        if self._libc is None:
            return False
        try:
            return read_subreaper(self._libc) == 1
        except (OSError, AttributeError, RuntimeError):
            return False

    def __enter__(self) -> Self:
        if self.restoration_pending:
            raise RuntimeError("prior subreaper ownership is unresolved; retry restoration first")
        if not self.enabled:
            return self
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            self._previous = read_subreaper(libc)
            self._libc = libc
            self.restoration_pending = True
            if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
                raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER")
            self.active = self.verify()
            if not self.active:
                raise RuntimeError("enabled subreaper readback is unproven")
        except (OSError, AttributeError, RuntimeError) as exc:
            self.reason = f"subreaper not established: {exc}"
        return self

    def __exit__(self, *_exc: object) -> None:
        original = _exc[1] if len(_exc) > 1 and isinstance(_exc[1], BaseException) else None
        restore_adoption(self, original)


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


def proc_state(pid: int, descriptor: int | None = None) -> str:
    """ "alive", "exited" (ONLY on kernel evidence: a readable held pidfd or pidfd_open ESRCH) or "unknown"
    (stat missing, unreadable or malformed while no exit is shown).  Unknown is never an exit."""

    if read_stat(pid) is not None and (descriptor is None or not _exited(descriptor)):
        return "alive"
    if descriptor is not None:
        return "exited" if _exited(descriptor) else "unknown"
    try:
        opened = os.pidfd_open(pid)
    except ProcessLookupError:
        return "exited"
    except (AttributeError, OSError):
        return "unknown"
    try:
        return "exited" if _exited(opened) else "unknown"
    finally:
        os.close(opened)


class OwnedTree:
    """Every process a job's worker ever started, each pinned by a verified pidfd."""

    def __init__(
        self,
        root_pid: int,
        token: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        observe_seconds: float = DEFAULT_OBSERVE_SECONDS,
        adoption: OrchestratorAdoption | None = None,
        status: GuardianStatus | None = None,
        known_children: Callable[[], set[int]] = set,
        cgroup: JobCgroup | None = None,
    ) -> None:
        self.root_pid = root_pid
        self.cgroup = cgroup
        self.cgroup_joined = False
        self._known_children = known_children
        self.adoption = adoption
        self.status = status
        self.holder_mode = (
            getattr(status, "holder", None) is not None
        )  # root is a holder, not a guardian
        self.launch_adopting = bool(adoption is not None and adoption.verify())
        self.orphans_pinned = 0
        self.unattributed: list[int] = []
        self.unknown_live: list[int] = []
        self._orphans: set[int] = set()
        self.token = token  # attestation only; never used to find or signal a process
        self._clock = clock
        self._observe_seconds = observe_seconds
        self._last = float("-inf")
        self._entries: dict[int, tuple[int, int]] = {}
        self.ports: set[int] = set()
        self.root_start: int | None = None
        self.adopted_total = 0
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
            self.root_start = stat[0]

    @property
    def tracked(self) -> dict[int, int]:
        return {pid: started for pid, (_, started) in self._entries.items()}

    def _adopt(self, pid: int, seen_start: int, parent: int | None = None) -> bool:
        descriptor = _pidfd_open(pid)
        if descriptor is None:
            self.contained = False
            self.reason = "containment not established: a descendant could not be pinned"
            return False
        stat = read_stat(pid)
        if stat is None or stat[0] != seen_start:
            unknown = stat is None and proc_state(pid, descriptor) == "unknown"
            os.close(descriptor)  # the number was recycled before the pidfd pinned it
            if unknown:
                self._mark_unknown(pid)  # alive by its pidfd but unreadable: not an exit
            return False
        if parent is not None and not self._parent_instance_valid(parent):
            os.close(descriptor)  # the parent may have died and its number been reused: discard
            return False
        self._entries[pid] = (descriptor, seen_start)
        self.adopted_total += 1
        return True

    def _alive_set(self) -> set[int]:
        return {pid for pid, (fd, _) in self._entries.items() if not _exited(fd)}

    def alive(self, table: object = None) -> set[int]:
        return self._alive_set()

    def _parent_instance_valid(self, parent: int) -> bool:
        """The very instance pinned earlier is still unexited (held pidfd) and keeps its start time."""

        entry = self._entries.get(parent)
        if entry is None or _exited(entry[0]):
            return False
        stat = read_stat(parent)
        if stat is None:
            self._mark_unknown(
                parent
            )  # its pidfd says alive, its stat cannot be read: unknown, not gone
        return stat is not None and stat[0] == entry[1]

    def _mark_unknown(self, pid: int) -> None:
        self.contained = False
        self.unknown_live.append(pid)
        self.reason = (
            "containment not established: a live process cannot be read (unknown, not an exit)"
        )

    def _targets(self) -> set[int]:
        """Pinned live processes to signal now; a holder root is signalled last, by terminate itself."""
        alive = self._alive_set()
        return alive - {self.root_pid} if self.holder_mode else alive

    def observe(self, *, force: bool = False) -> None:
        """Adopt new descendants, walking only down from verified-owned live parents.

        Each owned process is asked for its own children; no other process is read.
        """

        now = self._clock()
        if not self.contained or (not force and now - self._last < self._observe_seconds):
            return
        self._last = now
        frontier = sorted(self._alive_set())
        while frontier and self.contained:
            parent = frontier.pop()
            if not self._parent_instance_valid(parent):
                continue  # the pinned instance is gone: its number may already be reused
            kids = children_of(parent)
            if not self._parent_instance_valid(parent):
                continue  # gone or replaced while listed: neither the list nor a failure counts
            if kids is None:
                self.contained = False
                self.reason = (
                    "containment not established: the children of a live process cannot be read"
                )
                return
            for pid in kids:
                if pid in self._entries:
                    continue
                stat = read_stat(pid)
                if stat is None:
                    if proc_state(pid) == "unknown":
                        self._mark_unknown(
                            pid
                        )  # listed by a live owned parent, unreadable: not an exit
                        break
                    continue
                if stat[1] != parent:
                    continue
                if not self._parent_instance_valid(parent):
                    break  # the parent is gone: nothing read from here on proves ownership
                if self._adopt(pid, stat[0], parent):
                    frontier.append(pid)
                elif not self.contained:
                    return
        observe_cgroup_membership(self)
        for ports in socket_ports(self._alive_set()).values():
            self.ports.update(ports)

    def pin_orphans(self) -> None:
        """Pin what the kernel re-parented to this orchestrator and belongs to this job.

        Ownership is kernel-held: "our own child, pinned by pidfd after its start time was
        re-read" AND either in a session this job's guardian or worker leads, or the very worker
        (pid, start) the guardian reported over its status pipe.  No environment is read and the
        job token is not consulted.  Any other child is listed as unattributed, never signalled,
        and keeps the cleanup proof from being claimed (bounded UNPROVEN retention).
        """

        adoption = self.adoption
        if adoption is None or not self.contained:
            return
        if not adoption.verify():
            self.reason = "orchestrator subreaper flag is no longer set: adoption unproven"
            self.launch_adopting = False
            return
        me = os.getpid()
        kids = children_of(me)
        if kids is None:
            self.contained = False
            self.reason = "containment not established: the orchestrator's children cannot be read"
            return
        if self.status is not None:
            self.status.drain()
        worker = self.status.worker if self.status is not None else None
        sessions = {self.root_pid} | ({worker[0]} if worker is not None else set())
        root_start = self.root_start or 0
        foreign: list[int] = []
        known = self._known_children()
        for pid in kids:
            if pid in self._entries or pid in known:
                continue
            stat = read_stat(pid)
            if stat is None:
                if proc_state(pid) == "unknown":
                    self._mark_unknown(
                        pid
                    )  # a live child of the orchestrator whose state is unreadable
                    return
                continue
            if stat[1] != me:
                continue
            descriptor = _pidfd_open(pid)
            if descriptor is None:
                self.contained = False
                self.reason = "containment not established: an orphan could not be pinned"
                return
            again = read_stat(pid)
            if again is None or again[0] != stat[0]:
                gone = again is not None or _exited(descriptor)
                os.close(descriptor)
                if not gone:
                    self._mark_unknown(pid)
                    return
                continue
            mine = (
                read_session(pid) in sessions
                or (worker is not None and (pid, stat[0]) == worker)
                or (self.cgroup_joined and self.cgroup is not None and self.cgroup.contains(pid))
            )
            if mine:
                self._entries[pid] = (descriptor, stat[0])
                self._orphans.add(pid)
                self.adopted_total += 1
                self.orphans_pinned += 1
            else:
                os.close(descriptor)
                if stat[0] > root_start and stat[2] != "Z":
                    foreign.append(pid)
        self.unattributed = foreign

    def pin_new(self) -> None:
        self.observe(force=True)
        self.pin_orphans()
        self.observe(force=True)

    def _reap_orphans(self) -> None:
        """Reap exited pinned processes that are this orchestrator's children (never the root).

        The root is reaped by its ``Popen``; a pinned process that is not our child makes
        ``waitid`` fail with ECHILD, which is ignored, and a pidfd cannot name a recycled pid.
        """

        for pid, (descriptor, _started) in list(self._entries.items()):
            if pid != self.root_pid and _exited(descriptor):
                with contextlib.suppress(OSError, AttributeError):
                    os.waitid(os.P_PIDFD, descriptor, os.WEXITED | os.WNOHANG)

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
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """Stop pinned instances under a single absolute cleanup deadline."""

        return terminate_tree(
            self,
            grace_seconds=grace_seconds,
            clock=clock,
            sleeper=sleeper,
            deadline=deadline,
            orphan_rounds=ORPHAN_ROUNDS,
            late_kill_number=SIGKILL_NUMBER,
        )

    def signal_root(self, signum: int) -> bool:
        """Signal only the pinned root (the guardian) so it contains its own subtree first."""

        entry = self._entries.get(self.root_pid)
        if entry is None:
            return False
        try:
            signal.pidfd_send_signal(entry[0], signum)
        except OSError:
            return False
        return True

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


GUARDIAN_SOURCE = r"""
import signal, sys

stopped = []
for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(number, lambda signum, frame: stopped.append(signum))
import ctypes, hashlib, hmac, json, os, resource, subprocess, time

receipt_path, grace = sys.argv[1], float(sys.argv[2])
secret_fd, status_fd, argv = int(sys.argv[3]), int(sys.argv[4]), sys.argv[6:]
cgroup = sys.argv[5][9:] if sys.argv[5].startswith("--cgroup=") else ""
required = cgroup.startswith("!")  # qualified mode: no ownership, no worker
cgroup = cgroup.lstrip("!")
cgroup_error = ""
parent = os.getppid()
secret = bytes.fromhex(os.read(secret_fd, 256).decode())
os.close(secret_fd)
me = os.getpid()


def note(byte):
    try:
        os.write(status_fd, byte)
    except OSError:
        pass

with open("/proc/self/stat", "rb") as handle:
    start = int(handle.read().decode("utf-8", "replace").rsplit(")", 1)[1].split()[19])


def write(body):
    body = dict(body, guardian_pid=me, guardian_start=start, guardian_pgid=os.getpgid(0))
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    body["mac"] = hmac.new(secret, text.encode(), hashlib.sha256).hexdigest()
    temporary = receipt_path + ".tmp"
    with open(temporary, "w") as handle:
        json.dump(body, handle)
    os.replace(temporary, receipt_path)


def final(**fields):
    base = {"phase": "final", "contained": True, "worker_pid": None, "worker_status": None,
            "cgroup": cgroup, "cgroup_error": cgroup_error,
            "worker_pgid": None, "stopped_by_signal": stopped[0] if stopped else None,
            "adopted": [], "killed": [], "remaining": [], "proof": True}
    write(dict(base, **fields))


libc = ctypes.CDLL(None, use_errno=True)
libc.prctl(1, signal.SIGTERM, 0, 0, 0)  # PR_SET_PDEATHSIG: contain if the orchestrator dies
if os.getppid() != parent:
    stopped.append(signal.SIGTERM)
if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
    final(contained=False, proof=False, reason="subreaper could not be established")
    sys.exit(70)
if required and not cgroup:
    final(contained=False, proof=False, never_started=True,
          reason="kernel ownership is required but no cgroup was supplied")
    sys.exit(70)
if cgroup:
    try:
        own = [l[3:] for l in open("/proc/self/cgroup").read().splitlines() if l.startswith("0::")][0]
        with open(cgroup + "/cgroup.procs", "w") as handle:
            handle.write(str(me))
        joined = ("0::" + own.rstrip("/") + "/" + os.path.basename(cgroup)) in open("/proc/self/cgroup").read().splitlines()
        if not joined:
            cgroup_error = "join not visible in /proc/self/cgroup"
    except (OSError, IndexError) as exc:
        cgroup_error = type(exc).__name__ + (": " + str(exc) if str(exc) else "")
    if cgroup_error and required:
        final(contained=False, proof=False, never_started=True,
              reason="cgroup join failed: " + cgroup_error)
        sys.exit(70)
write({"phase": "ready", "cgroup": cgroup, "cgroup_error": cgroup_error})  # handlers and subreaper are in place
note(b"R")
if stopped:
    final(never_started=True)
    sys.exit(128 + stopped[0])
note(b"S")  # spawn intent, written strictly before the worker can exist
try:
    worker = subprocess.Popen(
        argv,
        start_new_session=True,
        preexec_fn=lambda: libc.prctl(1, signal.SIGKILL, 0, 0, 0),
    )
except OSError as exc:
    final(start_error=type(exc).__name__)
    sys.exit(127)
try:
    with open("/proc/%d/stat" % worker.pid, "rb") as handle:
        worker_start = int(handle.read().decode("utf-8", "replace").rsplit(")", 1)[1].split()[19])
except (OSError, ValueError, IndexError):
    worker_start = 0
note(("W%d:%d\n" % (worker.pid, worker_start)).encode())
try:
    worker_pgid = os.getpgid(worker.pid)
except OSError:
    worker_pgid = None
status = None
while status is None and not stopped:
    status = worker.poll()
    time.sleep(0.05)


def children():
    found = []
    try:
        threads = os.listdir("/proc/%d/task" % me)
        for thread in threads:
            with open("/proc/%d/task/%s/children" % (me, thread)) as handle:
                names = handle.read().split()
            for name in names:
                try:
                    with open("/proc/%s/stat" % name, "rb") as stat:
                        raw = stat.read().decode("utf-8", "replace")
                    found.append((int(name), raw[raw.rindex(")") + 2:].split()[0]))
                except (OSError, ValueError, IndexError):
                    continue
    except OSError:
        return None
    return found


def reap():
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


if status is None:
    worker.terminate()
    try:
        status = worker.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        worker.kill()
        status = worker.wait()
adopted, killed, remaining = set(), set(), []
deadline = time.monotonic() + grace
limit = deadline + max(grace, 1.0)
while True:
    reap()
    listing = children()
    if listing is None:
        remaining = [-1]
        break
    live = [pid for pid, state in listing if state != "Z"]
    if not live:
        break
    adopted.update(live)
    if time.monotonic() > limit:
        remaining = live
        break
    sending = signal.SIGTERM if time.monotonic() < deadline else signal.SIGKILL
    for pid in live:
        try:
            os.kill(pid, sending)
            if sending == signal.SIGKILL:
                killed.add(pid)
        except ProcessLookupError:
            pass
    time.sleep(0.05)
reap()
done = resource.getrusage(resource.RUSAGE_CHILDREN)
usage = {"utime": done.ru_utime, "stime": done.ru_stime, "maxrss_kb": done.ru_maxrss,
         "majflt": done.ru_majflt, "nvcsw": done.ru_nvcsw, "nivcsw": done.ru_nivcsw}
adopted.discard(worker.pid)
final(worker_pid=worker.pid, worker_status=status, worker_pgid=worker_pgid,
      adopted=sorted(adopted), killed=sorted(killed), remaining=sorted(remaining),
      proof=not remaining, rusage=usage)
if stopped:
    sys.exit(128 + stopped[0])
sys.exit(status if status >= 0 else 128 - status)
"""


class GuardianStatus:
    """The parent's own, independently observed view of the guardian's launch.

    The guardian writes ``R`` (handlers and subreaper in place) and ``S`` (immediately before it
    may spawn the worker) to a pipe only it can write.  The worker never inherits the pipe, so
    EOF means the guardian itself is gone, and EOF without ``S`` is affirmative proof that no
    worker was ever spawned - unlike a missing file, which anything with write access could cause.
    """

    def __init__(self, descriptor: int, holder: Any = None) -> None:
        self.fd = descriptor
        os.set_blocking(descriptor, False)
        self.holder = holder  # a HolderRef when the root is a per-job holder above the guardian
        self.guardian: tuple[int, int] | None = None  # the guardian instance the holder recorded
        self.ready = False
        self.spawned = False
        self.worker: tuple[int, int] | None = None
        self._buffer = b""
        self.eof = False
        self.error = False

    def drain(self) -> None:
        while self.fd >= 0 and not self.eof and not self.error:
            try:
                data = os.read(self.fd, 64)
            except BlockingIOError:
                return
            except OSError:
                self.error = True
                return
            if not data:
                self.eof = True
                return
            self.ready = self.ready or b"R" in data or (self.holder is not None and b"H" in data)
            self.spawned = self.spawned or b"S" in data
            self._buffer = (self._buffer + data)[-256:]
            found = re.search(rb"W(\d+):(\d+)\n", self._buffer)
            if found and self.worker is None:
                self.worker = (int(found.group(1)), int(found.group(2)))
            named = re.search(rb"G(\d+):(\d+)\n", self._buffer)
            if named and self.guardian is None:
                self.guardian = (int(named.group(1)), int(named.group(2)))

    def read_receipts(
        self,
        guardian_path: Path,
        secret: bytes,
        root_pid: int,
        root_start: int | None,
        *,
        reader: Any = None,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """Receipts of this job.  Legacy: the root IS the guardian.  Holder mode: the root is the holder and
        the guardian is authenticated against ITS OWN recorded instance (never the root's)."""
        reader = reader or read_guardian_receipt
        if self.holder is None:
            return reader(guardian_path, secret, root_pid, root_start)
        return self.holder.read_receipts(
            self, guardian_path, secret, root_pid, root_start, reader=reader
        )

    def absent_proof(self) -> bool:
        self.drain()
        return self.eof and not self.error and not self.spawned

    def close(self) -> None:
        if self.fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = -1


def launch_guardian(
    receipt_path: Path,
    grace_seconds: float,
    worker_argv: list[str],
    *,
    cgroup: str = "",
    **popen_kwargs: Any,
) -> tuple[subprocess.Popen[Any], bytes]:
    """Start the guardian; the HMAC key and the status pipe never reach the worker."""

    secret = os.urandom(32)
    secret_read, secret_write = os.pipe()
    status_read, status_write = os.pipe()
    try:
        os.write(secret_write, secret.hex().encode())
        os.close(secret_write)
        secret_write = -1
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                GUARDIAN_SOURCE,
                str(receipt_path),
                str(grace_seconds),
                str(secret_read),
                str(status_write),
                f"--cgroup={cgroup}",
                *worker_argv,
            ],
            pass_fds=(secret_read, status_write),
            **popen_kwargs,
        )
    except BaseException:
        os.close(status_read)
        raise
    finally:
        os.close(secret_read)
        os.close(status_write)
        if secret_write >= 0:
            os.close(secret_write)
    process.guardian_status = GuardianStatus(status_read)  # type: ignore[attr-defined]
    return process, secret


@contextlib.contextmanager
def interrupt_on_signals(event: threading.Event) -> Any:
    """Turn SIGTERM/SIGHUP of the orchestrator into the bounded-cancel interrupt event."""

    previous: dict[int, Any] = {}
    try:
        for number in (signal.SIGTERM, signal.SIGHUP):
            previous[number] = signal.signal(number, lambda _signum, _frame: event.set())
    except ValueError:  # not the main thread: nothing was installed
        previous = {}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _authentic(
    body: dict[str, Any], secret: bytes, expected_pid: int | None, expected_start: int | None
) -> list[str]:
    mac = body.pop("mac", None)
    expected = hmac.new(
        secret, json.dumps(body, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256
    ).hexdigest()
    if (
        not isinstance(mac, str)
        or len(mac) != 64
        or any(character not in "0123456789abcdef" for character in mac)
    ):
        return ["guardian receipt is not authenticated by the producer's key"]
    if not hmac.compare_digest(mac, expected):
        return ["guardian receipt is not authenticated by the producer's key"]
    if expected_pid is not None and body.get("guardian_pid") != expected_pid:
        return ["guardian receipt names a different producer process"]
    if expected_start is not None and body.get("guardian_start") != expected_start:
        return ["guardian receipt names a different producer instance (start time)"]
    return []


def read_guardian_receipt(
    path: Path,
    secret: bytes | None = None,
    expected_pid: int | None = None,
    expected_start: int | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Read the guardian receipt; unauthenticated, missing or unproven ones fail closed."""

    try:
        body = loads_strict(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, ["guardian receipt is missing or malformed: containment unproven"]
    if not isinstance(body, dict):
        return None, ["guardian receipt is not an object"]
    if secret is not None:
        problems = _authentic(body, secret, expected_pid, expected_start)
        if problems:
            return None, problems
    if body.get("phase") == "ready":
        return body, ["the guardian never finished: containment unproven"]
    if body.get("contained") is not True:
        return body, [
            "the guardian could not establish containment ("
            + str(body.get("reason") or body.get("cgroup_error") or "no reason recorded")
            + ")"
        ]
    if body.get("start_error"):
        return body, [f"could not start: {body['start_error']}"]
    problems = []
    if body.get("proof") is not True or body.get("remaining"):
        problems.append("the guardian could not remove every adopted descendant")
    if body.get("adopted"):
        problems.append(f"descendants outlived the worker and were adopted: {body['adopted'][:5]}")
    if body.get("never_started"):
        return body, problems
    if not isinstance(body.get("worker_pid"), int) or isinstance(body.get("worker_pid"), bool):
        problems.append("guardian receipt names no worker pid")
    return body, problems


def cleanup_proven(
    owned_receipt: dict[str, Any],
    guardian: dict[str, Any] | None,
    status: GuardianStatus | None,
) -> bool:
    """Budget may be released only with the parent's own proof AND an authenticated guardian proof.

    Two exceptions exist when the guardian left no final receipt.  An affirmative absence proof from
    the parent's own status pipe: the guardian is gone (EOF) and never signalled spawn intent, so no
    worker can have existed.  Or an adoption proof: the orchestrator was a subreaper from before the
    launch until the proof, the guardian is gone, and every re-parented orphan of the job was pinned
    by pidfd and terminated, with none left unattributed.  A missing, removed or unauthenticated
    file proves nothing.
    """

    if owned_receipt.get("proof") is not True:
        return False
    if getattr(status, "holder", None) is not None:  # holder mode: its authentic final ALWAYS gates
        root, adoption = owned_receipt.get("root") or {}, owned_receipt.get("adoption") or {}
        proven = status.holder.verdict(root.get("pid"), root.get("start"))["proof"]
        if not (proven and adoption.get("root_gone")):
            return False
        return (
            guardian is None
            or guardian.get("phase") != "final"
            or (guardian.get("contained") is True and guardian.get("proof") is True)
        )
    if guardian is not None and guardian.get("phase") == "final":
        return guardian.get("contained") is True and guardian.get("proof") is True
    if status is not None and status.absent_proof():
        return True
    adoption = owned_receipt.get("adoption")
    return isinstance(adoption, dict) and adoption.get("proof") is True
