"""Support for the orphan controls of the concurrent-runtime gate tests (#107).

Not a product rule.  It does two test-side things: (1) removes a process the project reported
but could not attribute, through a HELD pidfd on a validated instance and with bounded waits,
and says exactly what it could not clean; (2) is the worker-side handshake that records kernel
evidence that the orchestrator holds a pidfd for the worker's detached grandchild BEFORE the
guardian is killed.  Cleanup done here is outer cleanup, never project proof.  "Gone" is claimed
only from the held pidfd's own exit evidence (or the kernel's "no such process" on open): a
missing, unreadable or malformed /proc stat of a process the pidfd shows alive is UNKNOWN and is
reported as such, never as gone.  Imported by the test module and by the authored fake worker.
"""

import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

from scripts.production_gate_parallel_processes import read_stat

ORPHAN_ARGV = [sys.executable, "-c", "import time; time.sleep(120)"]
SENTINEL_ARGV = [sys.executable, "-c", "import time; time.sleep(119)"]
OBSERVATION = "orphan.observation.json"


def stop_owned_child(child, timeout=5.0):
    """SIGKILL and reap a child this test spawned (an unreaped direct child's pid is reserved)."""

    child.kill()
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(f"owned child {child.pid} ran {timeout} s past SIGKILL") from exc


def _open_pidfd(pid):
    return os.pidfd_open(pid)


def _read_cmdline(pid):
    return Path(f"/proc/{pid}/cmdline").read_bytes()


def _exited(descriptor):
    """True once the pidfd shows its process terminated; a poll error is raised, never guessed."""

    poller = select.poll()
    poller.register(descriptor, select.POLLIN)
    events = poller.poll(0)
    if any(flags & (select.POLLERR | select.POLLNVAL) for _fd, flags in events):
        raise AssertionError("the pidfd cannot be polled; its exit is unknown")
    return any(flags & select.POLLIN for _fd, flags in events)


def _close(descriptor):
    """Close ``descriptor``; return the problems found (never raises) so the caller reports them."""

    try:
        os.close(descriptor)
    except OSError as exc:
        return [f"closing the pidfd failed ({exc!r})"]
    return []


def _judge(descriptor, pid, argv):
    """("gone", None) only on the pidfd's exit evidence, ("held", start) for a validated live
    child; otherwise raise: what could not be read or proven is unknown, never "gone"."""

    # read_stat folds a gone pid, any read error and malformed text into None: not evidence of exit
    problem, stat = "", read_stat(pid)
    if stat is None:
        problem = "its /proc stat is missing, unreadable or malformed"
    elif stat[2] == "Z":
        problem = "it is a zombie that its pidfd does not show as exited"
    elif stat[1] != os.getpid():
        problem = f"its parent is {stat[1]}, not this process"
    else:
        try:
            raw = _read_cmdline(pid)
        except OSError as exc:
            problem = f"its command line is unreadable ({exc!r})"
        else:
            if raw.decode(errors="replace").split("\0")[:-1] != argv:
                problem = "its command line is not the fixture's orphan"
        if not problem:
            again = read_stat(pid)
            if again is None or again[:2] != stat[:2]:
                problem = "its /proc stat changed or became unreadable during validation"
    if _exited(descriptor):  # also discards anything read from a number that may be a stranger's
        return "gone", None
    if problem:
        raise AssertionError(
            f"pid {pid} is still running according to its pidfd but {problem}: unreadable or "
            "unproven, NOT gone; not killed"
        )
    return "held", stat[0]


class Survivor:
    """A process the project left behind, held by pidfd so exactly that instance is removed."""

    def __init__(self):
        self.descriptor = None
        self.pid = self.start = None

    def capture(self, pid, argv=ORPHAN_ARGV):
        """Pin ``pid`` and return "gone" or "held"; raise (nothing signalled, descriptor closed)
        when the instance cannot be pinned or its state cannot be proven."""

        if self.descriptor is not None:
            raise AssertionError(f"already holding pid {self.pid}; remove() it first")
        try:
            descriptor = _open_pidfd(pid)
        except ProcessLookupError:
            return "gone"  # the kernel itself reports no such process
        except OSError as exc:
            raise AssertionError(f"pid {pid} cannot be pinned ({exc!r}); not killed") from exc
        try:
            verdict, start = _judge(descriptor, pid, argv)
        except BaseException as exc:
            closing = _close(descriptor)
            if closing and isinstance(exc, AssertionError):
                raise AssertionError(f"{exc}; also {closing[0]}") from exc
            raise
        if verdict == "held":
            self.descriptor, self.pid, self.start = descriptor, pid, start
            return "held"
        closing = _close(descriptor)
        if closing:
            raise AssertionError(f"pid {pid} is gone but {closing[0]}")
        return "gone"

    def remove(self, timeout=5.0):
        """SIGKILL the held instance, wait (bounded) for its exit, reap it; raise if incomplete."""

        descriptor, self.descriptor = self.descriptor, None
        if descriptor is None:
            return
        problems = []
        try:
            try:
                problems += self._kill_and_wait(descriptor, timeout)
            except Exception as exc:  # noqa: BLE001 - reported below, never swallowed
                problems.append(f"unexpected {exc!r}")
        finally:
            problems += _close(descriptor)
        if problems:
            raise AssertionError(
                f"outer cleanup of pid {self.pid} (start {self.start}) incomplete: "
                + "; ".join(problems)
            )

    def _kill_and_wait(self, descriptor, timeout):
        deadline, problems = time.monotonic() + timeout, []
        try:
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            problems.append(f"SIGKILL through the pidfd failed ({exc!r})")
        while not _exited(descriptor):
            if time.monotonic() >= deadline:
                return [*problems, f"still running {timeout} s after SIGKILL"]
            time.sleep(0.01)
        return problems + self._reap(descriptor, deadline)

    @staticmethod
    def _reap(descriptor, deadline):
        while True:
            try:
                if os.waitid(os.P_PIDFD, descriptor, os.WEXITED | os.WNOHANG) is not None:
                    return []
            except ChildProcessError:
                return []  # not our child: its exit was confirmed through the pidfd
            except OSError as exc:
                return [f"reaping failed ({exc!r})"]
            if time.monotonic() >= deadline:
                return ["exited but was not reaped in time"]
            time.sleep(0.01)


def _pidfd_held_for(holder, target):
    """Evidence from the kernel that process ``holder`` holds a pidfd for ``target``, else None."""

    try:
        names = os.listdir(f"/proc/{holder}/fdinfo")
    except OSError:
        return None
    for name in names:
        try:
            if os.readlink(f"/proc/{holder}/fd/{name}") != "anon_inode:[pidfd]":
                continue
            with open(f"/proc/{holder}/fdinfo/{name}") as info:
                if f"Pid:\t{target}" in info.read().splitlines():
                    return {"fd": int(name), "fdinfo_line": f"Pid:\t{target}"}
        except (OSError, ValueError):
            continue
    return None


def pin_handshake_then_kill_guardian(job_root, wait_seconds=20.0):
    """Fake-worker scenario: start a detached grandchild and wait (monotonic, bounded) until the
    orchestrator's own /proc shows a pidfd for it; record that evidence; only then SIGKILL the
    guardian.  Without the evidence the guardian is NOT killed and the worker exits 3."""

    root = Path(job_root)
    orphan = subprocess.Popen(ORPHAN_ARGV, start_new_session=True)
    (root / "orphan.pid").write_text(str(orphan.pid))
    guardian = os.getppid()
    stat = read_stat(guardian)
    parent = stat[1] if stat is not None else None
    orchestrator, holder = parent, None
    holder_path = root / "holder.json"
    if holder_path.exists():
        try:
            record = json.loads(holder_path.read_text())
            hstat = read_stat(parent) if parent is not None else None
            if (
                hstat is None
                or record["holder_pid"] != parent
                or record["holder_start"] != hstat[0]
                or record["owner_pid"] != hstat[1]
            ):
                orchestrator = None
            else:
                holder, orchestrator = parent, record["owner_pid"]
                ostate = read_stat(orchestrator)
                if ostate is None or ostate[0] != record["owner_start"]:
                    orchestrator = None
        except (OSError, ValueError, KeyError, TypeError):
            orchestrator = None  # a partial topology observation cannot establish this precondition
    held, began = None, time.monotonic()
    while orchestrator is not None:
        held = _pidfd_held_for(orchestrator, orphan.pid)
        if held is not None or time.monotonic() - began >= wait_seconds:
            break
        time.sleep(0.02)
    own, alive = read_stat(orphan.pid), os.getppid() == guardian
    evidence = {
        "pinned": held is not None,
        "guardian_alive": alive,
        "orphan_pid": orphan.pid,
        "orphan_start": own[0] if own is not None else None,
        "guardian_pid": guardian,
        "orchestrator_pid": orchestrator,
        "holder_pid": holder,
        "waited_seconds": round(time.monotonic() - began, 3),
        "wait_limit_seconds": wait_seconds,
        **(held or {}),
    }
    (root / f"{OBSERVATION}.tmp").write_text(json.dumps(evidence))
    os.replace(root / f"{OBSERVATION}.tmp", root / OBSERVATION)
    if held is None or not alive:
        os._exit(3)  # the precondition failed: no deterministic observation is claimed
    os.kill(guardian, signal.SIGKILL)
    time.sleep(0.3)
    os._exit(0)
