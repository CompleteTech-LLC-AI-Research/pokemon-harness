"""Per-job HOLDER above the real guardian (#107 ownership design v2, trusted cooperating jobs only).

Tree: orchestrator O -> holder H -> guardian G -> worker W -> descendants.  H is a tiny project-authored
subreaper that runs no job code and forks exactly one child (G).  When G dies for ANY reason the kernel
re-parents the job's orphans to H (nearest living subreaper ancestor), never to the shared O, so the
orphans are exclusively this job's by lineage.  H then runs a bounded, COMPLETED sweep: it pins every
child by pidfd, signals only through the held pidfds, reaps, and proves completion only when its child
wait is exhausted (ECHILD) with every held pidfd exited and reaped and nothing Unknown or incomplete.

Proof rules (O side, ``HolderRef.verdict``): a missing, stale, forged, unauthenticated or non-final holder
receipt ALWAYS withholds the proof; the holder and guardian instances are authenticated separately (the
holder against the pinned root, the guardian against the identity the holder recorded); every adopted
(pid, start) is re-verified absent by kernel evidence.  A lost guardian stays a failure with sibling
cancellation: this module never fabricates a guardian final receipt.

Not a security boundary against malicious same-UID code.  Upstream-kernel semantics (subreaper
re-parenting, PDEATHSIG) are used as documented; the installed kernel and nested re-parenting are unexecuted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.production_gate_parallel_holder_schema import terminal_schema_problems
from scripts.production_gate_parallel_processes import (
    GUARDIAN_SOURCE,
    GuardianStatus,
    _exited,
    read_guardian_receipt,
    read_stat,
)
from scripts.production_gate_parallel_validation import loads_strict

HOLDER_MAX_SECONDS = (
    86400.0  # the single steady-state wait; real stops come from O's deadline or PDEATHSIG
)

HOLDER_SOURCE = r"""
import ctypes, hashlib, hmac, json, os, select, signal, subprocess, sys, time

holder_path, guardian_path, grace_text = sys.argv[1], sys.argv[2], sys.argv[3]
secret_fd, status_fd, guardian_source = int(sys.argv[4]), int(sys.argv[5]), sys.argv[6]
max_seconds, grace = float(sys.argv[7]), float(grace_text)
owner_pid, owner_start, guardian_argv = int(sys.argv[8]), int(sys.argv[9]), sys.argv[10:]
began, stopped = time.monotonic(), []
for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(number, lambda signum, frame: stopped.append(signum))
secret = bytes.fromhex(os.read(secret_fd, 256).decode())
os.close(secret_fd)
me = os.getpid()


def stat_field(pid, index):
    try:
        with open("/proc/%d/stat" % pid, "rb") as handle:
            return int(handle.read().decode("utf-8", "replace").rsplit(")", 1)[1].split()[index])
    except (OSError, ValueError, IndexError):
        return None


start = stat_field(me, 19)
libc = ctypes.CDLL(None, use_errno=True)
flag = ctypes.c_int(0)
subreaper_start = None
state = {"guardian": None, "guardian_status": None, "forks": [], "adopted": [], "killed": [], "reaped": [],
         "reaped_unheld": [], "unknown": [], "remaining": [], "incomplete": False, "echild": False,
         "never_started": False, "guardian_killed": False}


def subreaper_on():
    return libc.prctl(37, ctypes.byref(flag), 0, 0, 0) == 0 and flag.value == 1


def note(data):
    try:
        os.write(status_fd, data)
    except OSError:
        stopped.append(signal.SIGTERM)  # lost owner pipe: never keep launching unobserved work


def write(body):
    body = dict(body, holder_pid=me, holder_start=start, owner_pid=owner_pid, owner_start=owner_start)
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    body["mac"] = hmac.new(secret, text.encode(), hashlib.sha256).hexdigest()
    with open(holder_path + ".tmp", "w") as handle:
        json.dump(body, handle)
    os.replace(holder_path + ".tmp", holder_path)


def final(**fields):
    body = {"phase": "final", "contained": True, "proof": False, "reason": "", "subreaper_start": subreaper_start,
            "subreaper_end": subreaper_on(), "elapsed": round(time.monotonic() - began, 3)}
    body.update(state)
    body.update(fields)
    write(body)


def owner_matches():
    return os.getppid() == owner_pid and stat_field(owner_pid, 19) == owner_start


if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG must succeed before any fork
    state["never_started"] = True
    final(contained=False, reason="parent-death signal could not be established")
    sys.exit(70)
if not owner_matches():
    stopped.append(signal.SIGTERM)
if libc.prctl(36, 1, 0, 0, 0) != 0 or not subreaper_on():  # set, then READ BACK, before any fork
    state["never_started"] = True
    final(contained=False, reason="subreaper could not be established or read back")
    sys.exit(70)
subreaper_start = True
write({"phase": "ready", "subreaper_start": True})
note(b"H")
if stopped:
    state.update(never_started=True, echild=True)
    final(proof=True)
    sys.exit(128 + stopped[0])
key_read, key_write = os.pipe()
os.write(key_write, secret.hex().encode())
os.close(key_write)
write({"phase": "spawning", "subreaper_start": True})  # intent persisted BEFORE the fork
if stopped or not owner_matches():  # recheck original O, never a replacement parent, just before fork
    os.close(key_read)
    state.update(never_started=True, echild=True)
    final(proof=True)
    sys.exit(128 + signal.SIGTERM)
try:
    guardian = subprocess.Popen([sys.executable, "-c", guardian_source, guardian_path, grace_text,
                                 str(key_read), str(status_fd), *guardian_argv],
                                 pass_fds=(key_read, status_fd), start_new_session=True)
except OSError as exc:
    os.close(key_read)
    state.update(never_started=True, echild=True)
    final(proof=True, start_error=type(exc).__name__)
    sys.exit(127)
os.close(key_read)
gstart = stat_field(guardian.pid, 19)
state["guardian"] = {"pid": guardian.pid, "start": gstart}
state["forks"] = [[guardian.pid, gstart]]
write({"phase": "spawned", "subreaper_start": True})
note(("G%d:%s\n" % (guardian.pid, gstart)).encode())
sent_term, stop_at, hard_at = False, None, None
while guardian.poll() is None:
    now = time.monotonic()
    if not stopped and now - began > max_seconds:
        stopped.append(signal.SIGTERM)
    if stopped and not sent_term:
        sent_term, stop_at = True, now + 3 * grace + 2.0
        hard_at = stop_at + max(grace, 1.0) + 2.0
        guardian.send_signal(signal.SIGTERM)
    if stop_at is not None and now > stop_at and not state["guardian_killed"]:
        state["guardian_killed"] = True
        guardian.send_signal(signal.SIGKILL)
    if hard_at is not None and now > hard_at:
        break
    time.sleep(0.05)
state["guardian_status"] = guardian.returncode


def children_now():
    found = []
    try:
        tasks = os.listdir("/proc/%d/task" % me)
    except OSError:
        return None
    for task in tasks:
        try:
            with open("/proc/%d/task/%s/children" % (me, task)) as handle:
                text = handle.read()
        except FileNotFoundError:
            if os.path.exists("/proc/%d/task/%s" % (me, task)):
                return None
            continue
        except ProcessLookupError:
            continue
        except (OSError, ValueError):
            return None
        for item in text.split():
            if not item.isdigit():
                return None
            found.append(int(item))
    return found


def exited(fd):
    return bool(select.select([fd], [], [], 0)[0])


held, signalled = {}, set()
deadline = time.monotonic() + 2 * (grace + max(grace, 1.0)) + 2.0
term_end = time.monotonic() + grace
while True:
    kids = children_now()
    state["incomplete"] = kids is None
    for pid in kids or []:
        if pid in held or (guardian.returncode is None and pid == guardian.pid):
            continue
        first = stat_field(pid, 19)
        try:
            fd = os.pidfd_open(pid)
        except ProcessLookupError:
            continue
        except OSError:
            state["unknown"].append([pid, first])
            continue
        if first is None or stat_field(pid, 19) != first or stat_field(pid, 1) != me:
            gone = exited(fd)
            os.close(fd)
            if not gone:
                state["unknown"].append([pid, first])
            continue
        held[pid] = (fd, first)
        state["adopted"].append([pid, first])
    for pid, (fd, first) in list(held.items()):
        if exited(fd):
            try:
                os.waitid(os.P_PIDFD, fd, os.WEXITED)
            except ChildProcessError:
                pass
            except OSError:
                continue
            state["reaped"].append([pid, first])
            os.close(fd)
            del held[pid]
        else:
            sig = signal.SIGTERM if time.monotonic() < term_end else signal.SIGKILL
            if (pid, sig) not in signalled:
                signalled.add((pid, sig))
                try:
                    signal.pidfd_send_signal(fd, sig)
                    if sig == signal.SIGKILL:
                        state["killed"].append([pid, first])
                except OSError:
                    pass
    try:  # exhausted child wait: raises ECHILD only when H has NO child left, zombies included
        waiting = os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        state["echild"] = not held
        break
    unheld = waiting is not None and waiting.si_pid not in held
    if unheld and (guardian.returncode is not None or waiting.si_pid != guardian.pid):
        try:
            os.waitid(os.P_PID, waiting.si_pid, os.WEXITED | os.WNOHANG)
            state["reaped_unheld"].append([waiting.si_pid, None])
        except OSError:
            pass
    if time.monotonic() > deadline:
        break
    time.sleep(0.02)
state["remaining"] = [[pid, first] for pid, (fd, first) in held.items()]
known = state["guardian_status"] is not None
proof = bool(state["echild"] and not held and not state["unknown"] and not state["incomplete"]
             and not state["reaped_unheld"] and subreaper_on() and known)
final(proof=proof)
code = state["guardian_status"]
if code is None:
    sys.exit(70)
if code < 0:  # mirror a signal death so the orchestrator sees what it always saw for a lost guardian
    try:
        signal.signal(-code, signal.SIG_DFL)
    except (OSError, ValueError):
        pass
    os.kill(me, -code)
    time.sleep(1.0)
    sys.exit(128 - code)
sys.exit(code)
"""


def instance_absent(pid: int, start: int | None) -> bool | None:
    """True only on kernel evidence that the (pid, start) instance is gone; None when it cannot be told."""

    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return True
    except (AttributeError, OSError):
        return None
    try:
        if _exited(descriptor):
            return True
        stat = read_stat(pid)
        return None if stat is None else stat[0] != start
    finally:
        os.close(descriptor)


def _authentic(
    body: dict[str, Any], secret: bytes, pid: int | None, start: int | None
) -> list[str]:
    mac = body.pop("mac", None)
    text = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    if (
        not isinstance(mac, str)
        or len(mac) != 64
        or any(character not in "0123456789abcdef" for character in mac)
    ):
        return ["holder receipt is not authenticated by the producer's key"]
    if not hmac.compare_digest(mac, hmac.new(secret, text, hashlib.sha256).hexdigest()):
        return ["holder receipt is not authenticated by the producer's key"]
    if (
        pid is None
        or start is None
        or body.get("holder_pid") != pid
        or body.get("holder_start") != start
    ):
        return ["holder receipt names a different producer instance than the pinned root"]
    return []


@dataclass
class HolderRef:
    """What O knows about one job's holder: receipt path, the shared key, the launched pid."""

    path: Path
    secret: bytes
    pid: int

    def read_receipts(
        self,
        status: GuardianStatus,
        guardian_path: Path,
        secret: bytes,
        root_pid: int,
        root_start: int | None,
        *,
        reader: Any = read_guardian_receipt,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """Guardian receipt authenticated against the guardian's OWN recorded instance (status pipe first,
        else the authenticated holder receipt); with no recorded identity nothing is trusted."""

        status.drain()
        identity = status.guardian or self.verdict(root_pid, root_start)["guardian"]
        if identity is None:
            return None, [
                "the guardian instance was never recorded by the holder: its receipt is not trusted"
            ]
        return reader(guardian_path, secret, *identity)

    def verdict(self, root_pid: int | None, root_start: int | None) -> dict[str, Any]:
        """Always-conservative reading of the holder receipt; ``proof`` only for an authentic, final, completed sweep."""

        out: dict[str, Any] = {
            "present": False,
            "authentic": False,
            "final": False,
            "proof": False,
            "guardian": None,
            "problems": [],
        }
        try:
            body = loads_strict(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out["problems"].append(
                "holder terminal receipt is missing or malformed: proof withheld"
            )
            return out
        if not isinstance(body, dict):
            out["problems"].append("holder receipt is not an object")
            return out
        out["present"] = True
        problems = _authentic(
            body, self.secret, root_pid if root_pid == self.pid else None, root_start
        )
        if problems:
            out["problems"] += problems
            return out
        out["authentic"] = True
        guardian = body.get("guardian")
        if (
            isinstance(guardian, dict)
            and type(guardian.get("pid")) is int
            and type(guardian.get("start")) is int
        ):
            out["guardian"] = (guardian["pid"], guardian["start"])
        if body.get("phase") != "final":
            out["problems"].append("the holder never finished: proof withheld")
            return out
        out["final"] = True
        problems = terminal_schema_problems(body)
        if problems:
            out["problems"].extend(problems)
            return out
        adopted = body["adopted"]
        absent = [instance_absent(pid, start) for pid, start in adopted]
        if any(item is not True for item in absent):
            out["problems"].append("an adopted instance is not proven absent by kernel evidence")
            return out
        out["proof"] = True
        out["adopted"] = adopted
        return out


def launch_holder(
    receipt_path: Path,
    grace_seconds: float,
    worker_argv: list[str],
    *,
    cgroup: str = "",
    max_seconds: float = HOLDER_MAX_SECONDS,
    **popen_kwargs: Any,
) -> tuple[subprocess.Popen[Any], bytes]:
    """Start H (which starts the real guardian); same shape as ``launch_guardian`` but the returned
    process is the HOLDER, so its pid/start are the holder's and the guardian is authenticated separately."""

    owner_pid = os.getpid()
    owner = read_stat(owner_pid)
    if owner is None:
        raise OSError("the original orchestrator instance could not be established")
    secret = os.urandom(32)
    secret_read, secret_write = os.pipe()
    status_read, status_write = os.pipe()
    holder_path = Path(receipt_path).with_name("holder.json")
    try:
        os.write(secret_write, secret.hex().encode())
        os.close(secret_write)
        secret_write = -1
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                HOLDER_SOURCE,
                str(holder_path),
                str(receipt_path),
                str(grace_seconds),
                str(secret_read),
                str(status_write),
                GUARDIAN_SOURCE,
                str(max_seconds),
                str(owner_pid),
                str(owner[0]),
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
    process.guardian_status = GuardianStatus(
        status_read, holder=HolderRef(holder_path, secret, process.pid)
    )  # type: ignore[attr-defined]
    return process, secret


__all__ = ("HOLDER_SOURCE", "HolderRef", "instance_absent", "launch_holder")
