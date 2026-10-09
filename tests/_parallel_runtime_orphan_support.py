"""Support for the orphan controls of the concurrent-runtime gate tests (#107).

Not a product rule.  It does two test-side things: (1) removes a process the project reported
but could not attribute, through a HELD pidfd on a validated instance and with bounded waits,
and says exactly what it could not clean; (2) is the worker-side handshake that records kernel
evidence that the orchestrator holds a pidfd for the worker's detached grandchild BEFORE the
guardian is killed.  Cleanup done here is outer cleanup, never project proof.  Imported by the
test module and by the authored fake worker.
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


class Survivor:
    """A process the project left behind, held by pidfd so exactly that instance is removed."""

    def __init__(self):
        self.descriptor = None
        self.pid = self.start = None

    def capture(self, pid, argv=ORPHAN_ARGV):
        """Pin ``pid``: "gone" (or already exited) or "held" (alive).

        A live process that cannot be pinned, or is not provably the expected orphan (a child of
        this process with the fixture's command line), raises and is NOT signalled.
        """

        try:
            descriptor = os.pidfd_open(pid)
        except ProcessLookupError:
            return "gone"
        except OSError as exc:
            raise AssertionError(f"pid {pid} cannot be pinned ({exc!r}); not signalled") from exc
        stat = read_stat(pid)
        if stat is None:
            os.close(descriptor)
            return "gone"
        problem = ""
        if stat[2] != "Z":
            if stat[1] != os.getpid():
                problem = f"its parent is {stat[1]}, not this process"
            else:
                try:
                    raw = Path(f"/proc/{pid}/cmdline").read_bytes()
                except OSError as exc:
                    problem = f"its command line is unreadable ({exc!r})"
                else:
                    if raw.decode(errors="replace").split("\0")[:-1] != argv:
                        problem = "its command line is not the fixture's orphan"
        if problem:
            os.close(descriptor)
            raise AssertionError(f"pid {pid} (start {stat[0]}) is alive but {problem}; not killed")
        self.descriptor, self.pid, self.start = descriptor, pid, stat[0]
        return "gone" if stat[2] == "Z" else "held"

    def remove(self, timeout=5.0):
        """SIGKILL the held instance, wait (bounded) for its exit, reap it; raise if incomplete."""

        descriptor, self.descriptor = self.descriptor, None
        if descriptor is None:
            return
        problems, deadline = [], time.monotonic() + timeout
        try:
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as exc:
                problems.append(f"SIGKILL through the pidfd failed ({exc!r})")
            poller = select.poll()
            poller.register(descriptor, select.POLLIN)
            if not poller.poll(int(max(0.0, deadline - time.monotonic()) * 1000)):
                problems.append(f"still running {timeout} s after SIGKILL")
            else:
                problems += self._reap(descriptor, deadline)
        finally:
            os.close(descriptor)
        if problems:
            raise AssertionError(
                f"outer cleanup of pid {self.pid} (start {self.start}) incomplete: "
                + "; ".join(problems)
            )

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
    orchestrator = stat[1] if stat is not None else None
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
