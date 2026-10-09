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

import hashlib
import hmac
import json
import os
import select
import signal
import subprocess
import sys
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
        self.adopted_total += 1
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
        states_before = {
            pid: ("exited" if _exited(fd) else "alive") for pid, (fd, _) in self._entries.items()
        }
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
            "adopted_total": self.adopted_total,
            "states_before": states_before,
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
import ctypes, hashlib, hmac, json, os, subprocess, time

receipt_path, grace, secret_fd, argv = sys.argv[1], float(sys.argv[2]), int(sys.argv[3]), sys.argv[5:]
secret = bytes.fromhex(os.read(secret_fd, 256).decode())
os.close(secret_fd)
me = os.getpid()
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
            "worker_pgid": None, "stopped_by_signal": stopped[0] if stopped else None,
            "adopted": [], "killed": [], "remaining": [], "proof": True}
    write(dict(base, **fields))


libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
    final(contained=False, proof=False, reason="subreaper could not be established")
    sys.exit(70)
write({"phase": "ready"})  # handlers and subreaper are in place: a stop signal is now handled
if stopped:
    final(never_started=True)
    sys.exit(128 + stopped[0])
try:
    worker = subprocess.Popen(argv, start_new_session=True)
except OSError as exc:
    final(start_error=type(exc).__name__)
    sys.exit(127)
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
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name, "rb") as handle:
                raw = handle.read().decode("utf-8", "replace")
            rest = raw[raw.rindex(")") + 2:].split()
            if int(rest[1]) == me:
                found.append((int(name), rest[0]))
        except (OSError, ValueError, IndexError):
            continue
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
    live = [pid for pid, state in children() if state != "Z"]
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
adopted.discard(worker.pid)
final(worker_pid=worker.pid, worker_status=status, worker_pgid=worker_pgid,
      adopted=sorted(adopted), killed=sorted(killed), remaining=sorted(remaining),
      proof=not remaining)
if stopped:
    sys.exit(128 + stopped[0])
sys.exit(status if status >= 0 else 128 - status)
"""


def launch_guardian(
    receipt_path: Path, grace_seconds: float, worker_argv: list[str], **popen_kwargs: Any
) -> tuple[subprocess.Popen[Any], bytes]:
    """Start the guardian; its HMAC key travels over a pipe the worker never inherits."""

    secret = os.urandom(32)
    read_end, write_end = os.pipe()
    try:
        os.write(write_end, secret.hex().encode())
        os.close(write_end)
        write_end = -1
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                GUARDIAN_SOURCE,
                str(receipt_path),
                str(grace_seconds),
                str(read_end),
                "--",
                *worker_argv,
            ],
            pass_fds=(read_end,),
            **popen_kwargs,
        )
    finally:
        os.close(read_end)
        if write_end >= 0:
            os.close(write_end)
    return process, secret


def _authentic(
    body: dict[str, Any], secret: bytes, expected_pid: int | None, expected_start: int | None
) -> list[str]:
    mac = body.pop("mac", None)
    expected = hmac.new(
        secret, json.dumps(body, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256
    ).hexdigest()
    if not isinstance(mac, str) or not hmac.compare_digest(mac, expected):
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
        body = json.loads(path.read_text(encoding="utf-8"))
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
        return body, ["the guardian could not establish containment"]
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
    owned_receipt: dict[str, Any], guardian: dict[str, Any] | None, receipt_exists: bool
) -> bool:
    """Budget may be released only with the parent's own proof AND an authenticated guardian proof.

    The one exception is a guardian that never produced any receipt: the worker is spawned only
    after the readiness receipt is written, so with no receipt file, no adopted descendant and
    nothing alive in the parent's own pidfd measurement, no worker can have existed.
    """

    if owned_receipt.get("proof") is not True:
        return False
    if guardian is not None and guardian.get("phase") == "final":
        return guardian.get("contained") is True and guardian.get("proof") is True
    return not receipt_exists and owned_receipt.get("adopted_total") == 0
