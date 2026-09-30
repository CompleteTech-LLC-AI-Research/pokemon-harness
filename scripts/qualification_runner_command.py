"""Command containment, ownership and execution.

Split from ``scripts/qualification_runner.py`` for issue #112 with no
behavior change: the code below is copied verbatim except that calls to
facade-owned, monkeypatch-patched entry points resolve through ``_entry`` so
attribute patches on ``scripts.qualification_runner`` stay visible.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

# Direct names this module calls; cyclic back-edges go through _entry.
from scripts.qualification_runner_host import _reap_zombie
from scripts.qualification_runner_model import (
    _DEFAULT_FOREIGN_SAMPLE_SECONDS,
    _DEFAULT_PROBE_TIMEOUT_SECONDS,
    _DEFAULT_QUALIFICATION_TIMEOUT_SECONDS,
    _FOREIGN_SAMPLE_ENV,
    _PROBE_TIMEOUT_ENV,
    _QUALIFICATION_TIMEOUT_ENV,
    _TIMEOUT_RETURNCODE,
)


def _command_timeout() -> float:
    raw = os.environ.get(_PROBE_TIMEOUT_ENV)
    if raw is None:
        return _DEFAULT_PROBE_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_PROBE_TIMEOUT_SECONDS
    return value if value > 0 else _DEFAULT_PROBE_TIMEOUT_SECONDS


def _foreign_sample_seconds() -> float:
    raw = os.environ.get(_FOREIGN_SAMPLE_ENV)
    if raw is None:
        return _DEFAULT_FOREIGN_SAMPLE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_FOREIGN_SAMPLE_SECONDS
    return value if value > 0 else _DEFAULT_FOREIGN_SAMPLE_SECONDS


def _clock_ticks_per_second() -> int:
    try:
        return int(os.sysconf("SC_CLK_TCK"))
    except (ValueError, OSError):
        return 100


def _qualification_timeout(override: float | None = None) -> float:
    """Return the qualification-command deadline, distinct from prerequisites."""

    raw = override if override is not None else os.environ.get(_QUALIFICATION_TIMEOUT_ENV)
    if raw is None:
        return _DEFAULT_QUALIFICATION_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_QUALIFICATION_TIMEOUT_SECONDS
    return value if value > 0 else _DEFAULT_QUALIFICATION_TIMEOUT_SECONDS


def _posix_child_preexec() -> None:
    """Ask the kernel to kill an owned child when this process dies."""

    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(1, signal.SIGKILL, 0, 0, 0)
    except Exception:  # noqa: BLE001 - best-effort parent-death signal
        return


def _process_group_options() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True, "preexec_fn": _posix_child_preexec}


_OWNED_PROCESSES: set[subprocess.Popen[str]] = set()


# Descendants of an owned command that survived the group sweep.  While this is
# non-empty the capacity is still in use and the lease must not be released.
_LEFTOVER_OWNED_PIDS: set[int] = set()


# A process scan that could not read the whole process table is *not* evidence
# that the table was empty.  Every scan that fails appends a reason here, and a
# containment decision that folds in these reasons refuses to report proof.
_PROC_ENUMERATION_FAILURES: list[str] = []


# A durable record of the command a lease is actually running.  It lives in the
# job directory so a later ``--recover``/``--release`` process can identify the
# job's own process group and session even after its holder died.
_JOB_RUN_RECORD_NAME = "job-run.json"


# The launch intent is written before the command is spawned, so a lease whose
# ownership record never landed is still distinguishable from one that never
# launched anything.  Without it a swallowed record write would read as "no job
# ran" and a later recovery would delete the lease while the job still ran.
_JOB_LAUNCH_INTENT_NAME = "job-launch-intent.json"


# A random per-launch token injected into the job's environment and recorded
# with its ownership.  A live process that carries the token in
# ``/proc/<pid>/environ`` was demonstrably started by this exact job; a process
# that merely reused a pid or a process-group id does not.  Recovery requires
# the token before it will signal anything, so reused identifiers can never
# turn into an unrelated process being killed.
_JOB_OWNERSHIP_TOKEN_ENV = "POKERED_QUALIFICATION_JOB_TOKEN"


class JobOwnershipError(RuntimeError):
    """A lease could not durably record the job it launched.

    The command is torn down and the lease is kept: capacity that cannot be
    attributed to a recorded job must never be reported as free.
    """


@dataclass(frozen=True)
class CommandContainment:
    """The observed containment outcome of one ``run_command`` call.

    ``proven`` is the only claim a caller may act on.  It is true only when the
    command's process group *and* every adopted descendant were observed gone;
    a host that cannot adopt detached descendants reports ``proven=False`` even
    when no survivor was seen, because an unobserved survivor cannot be ruled
    out.
    """

    proven: bool
    detail: str
    adoption_active: bool
    leftovers: tuple[int, ...] = ()


def last_command_containment() -> CommandContainment | None:
    """Return the containment outcome of the most recent ``run_command``."""

    return _entry._LAST_COMMAND_CONTAINMENT


def _timeout_partial_streams(exc: subprocess.TimeoutExpired) -> tuple[str, str]:
    """Return the child's output observed before a stream wait timed out.

    ``subprocess.TimeoutExpired`` carries whatever ``communicate`` had read when
    the deadline expired.  The previous behaviour replaced that with empty
    strings, which discarded the command's own failure diagnostics whenever a
    detached descendant kept the output pipe open past the deadline.
    """

    def decode(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value)

    raw_stdout = getattr(exc, "stdout", None)
    if raw_stdout is None:
        raw_stdout = getattr(exc, "output", None)
    return decode(raw_stdout), decode(getattr(exc, "stderr", None))


def _drain_after_termination(
    process: subprocess.Popen[str], fallback: tuple[str, str] = ("", "")
) -> tuple[str, str]:
    """Collect a terminated command's streams without discarding partial output.

    Retrying ``communicate`` after a timeout does not lose output, so the retry
    normally returns everything the child wrote.  When the stream is still held
    open the retry times out as well and its own partial snapshot is used; an
    unreadable stream falls back to whatever was already captured.
    """

    try:
        return process.communicate(timeout=_entry._CHILD_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired as exc:
        partial = _timeout_partial_streams(exc)
        return partial if any(partial) else fallback
    except (OSError, ValueError):
        return fallback


_SIGNAL_HANDLERS_INSTALLED = False


# True while owned-process cleanup is running.  A SIGINT/SIGTERM received then
# is deferred (recorded in _DEFERRED_SIGNAL) instead of being raised into the
# sweep, so the bounded containment work always completes before the process
# exits with the signal status.
_CLEANUP_ACTIVE = False


_DEFERRED_SIGNAL: int | None = None


_PR_SET_CHILD_SUBREAPER = 36


_PR_GET_CHILD_SUBREAPER = 37


def _libc_prctl() -> Any:
    import ctypes

    return ctypes.CDLL("libc.so.6", use_errno=True)


def _set_child_subreaper(enabled: bool) -> bool:
    """Set ``PR_SET_CHILD_SUBREAPER``, returning whether it took effect."""

    if os.name == "nt":
        return False
    try:
        return _libc_prctl().prctl(_PR_SET_CHILD_SUBREAPER, 1 if enabled else 0, 0, 0, 0) == 0
    except Exception:  # noqa: BLE001 - best-effort subreaper
        return False


def _child_subreaper_state() -> int | None:
    """Return the current child-subreaper flag, or ``None`` if unreadable."""

    if os.name == "nt":
        return None
    try:
        import ctypes

        libc = _libc_prctl()
        value = ctypes.c_int(0)
        if libc.prctl(_PR_GET_CHILD_SUBREAPER, ctypes.byref(value), 0, 0, 0) != 0:
            return None
        return value.value
    except Exception:  # noqa: BLE001 - best-effort subreaper
        return None


class _adopted_descendants:
    """Temporarily become the reaper for a command's orphaned descendants.

    Without ``PR_SET_CHILD_SUBREAPER`` a grandchild that outlives the direct
    command is reparented to init and disappears from this process's tree,
    which makes "the command completed, so the capacity is free" a false
    statement.  As a subreaper this process is the reaper for those orphans and
    can find, terminate, and reap them before the lease is released.

    The flag is process-wide, so the previous value is restored on exit.  A
    caller that is itself a subreaper (or a test process that must keep its own
    reaping semantics) is unaffected.
    """

    def __enter__(self) -> Self:
        self._previous = _entry._child_subreaper_state()
        self._enabled = False
        # A process that is already a subreaper is still a subreaper for the
        # command's orphans, so adoption is observable in that case too.
        self.active = self._previous == 1
        if self._previous != 1:
            self._enabled = _entry._set_child_subreaper(True)
            self.active = self._enabled
        return self

    def __exit__(self, *_exc: object) -> bool:
        if self._enabled and self._previous is not None:
            _entry._set_child_subreaper(bool(self._previous))
        return False


def _note_proc_enumeration_failure(context: str) -> None:
    """Record that a process scan could not observe the whole process table."""

    _PROC_ENUMERATION_FAILURES.append(context)


def _reset_proc_enumeration_failures() -> None:
    _PROC_ENUMERATION_FAILURES.clear()


def _proc_enumeration_failures() -> list[str]:
    return list(_PROC_ENUMERATION_FAILURES)


def _read_proc_stat(pid: int) -> str | None:
    """Read ``/proc/<pid>/stat``, separating a gone pid from an unreadable one.

    A pid whose ``/proc`` entry has vanished has confirmed exited and is skipped
    silently.  A pid whose entry still exists but cannot be read or decoded is
    *unknown*, not absent: it is recorded as a process-enumeration failure so the
    containment decision that folds in these failures stays unproven and keeps
    the lease and the host exclusion.  Treating an unreadable live process as
    absent would let it survive an otherwise-clean sweep.
    """

    try:
        return Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, ProcessLookupError):
        return None
    except (OSError, ValueError):
        _note_proc_enumeration_failure(
            f"the state of live process {pid} could not be read while scanning for owned work"
        )
        return None


def _process_group_members(pgid: int) -> list[int]:
    """Return every live pid whose process group is *pgid* (excluding self)."""

    if pgid <= 0:
        return []
    me = os.getpid()
    members: list[int] = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        _note_proc_enumeration_failure(
            "the process table could not be enumerated while sweeping the command's process group"
        )
        return members
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        raw = _read_proc_stat(pid)
        if raw is None:
            continue
        closing = raw.rfind(")")
        if closing == -1:
            _note_proc_enumeration_failure(
                f"the state of live process {pid} was malformed while sweeping the "
                "command's process group"
            )
            continue
        fields = raw[closing + 1 :].split()
        if len(fields) < 3:
            _note_proc_enumeration_failure(
                f"the state of live process {pid} was incomplete while sweeping the "
                "command's process group"
            )
            continue
        if fields[0] == "Z":
            # An exited descendant awaiting reaping holds no CPU capacity.
            _reap_zombie(pid)
            continue
        try:
            if int(fields[2]) == pgid:
                members.append(pid)
        except ValueError:
            _note_proc_enumeration_failure(
                f"the process-group id of live process {pid} was unreadable while sweeping "
                "the command's process group"
            )
            continue
    return sorted(members)


def _terminate_process_group(pgid: int, grace: float | None = None) -> tuple[bool, list[int]]:
    """Terminate every remaining member of *pgid* and confirm they exited.

    A completed parent does not imply its descendants exited.  This sweeps the
    process group the command created, escalates SIGTERM -> SIGKILL, and only
    reports success after the group is observed empty.  Remaining pids are
    returned so the caller can keep the lease instead of releasing capacity
    that is still in use.
    """

    if os.name == "nt":
        return True, []
    grace = _entry._CHILD_TERMINATION_GRACE_SECONDS if grace is None else grace
    remaining = _process_group_members(pgid)
    if not remaining:
        return True, []
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except OSError:
            for pid in remaining:
                try:
                    os.kill(pid, sig)
                except OSError:
                    continue
        deadline = time.monotonic() + grace
        while True:
            remaining = _process_group_members(pgid)
            if not remaining:
                return True, []
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    return False, _process_group_members(pgid)


def _adopted_orphan_pids(exclude: set[int] | None = None) -> list[int]:
    """Return live orphaned children of this process that are not tracked.

    A command that detaches a grandchild with ``start_new_session=True`` moves
    that grandchild into a different process group, so the process-group sweep
    cannot see it.  Because the runner is a child subreaper, such an orphan is
    reparented to this process when its parent exits, which makes it a *direct*
    child here.  Restricting the scan to direct children is deliberate: it
    reaches exactly the adopted orphans, and it cannot touch an unrelated
    process that merely shares this subtree.

    ``exclude`` holds pids observed before the command started, so a
    pre-existing child of the runner is never mistaken for the command's
    descendant.  Zombies are reaped rather than reported, because a reaped
    orphan holds no CPU capacity.
    """

    excluded = set(exclude or ())
    excluded.update(process.pid for process in _OWNED_PROCESSES if process.poll() is None)
    me = os.getpid()
    owned: list[int] = []
    for pid in _direct_child_pids(me):
        if pid in excluded:
            continue
        if _entry._pid_is_zombie(pid):
            _reap_zombie(pid)
            continue
        if _entry._pid_alive(pid):
            owned.append(pid)
    return sorted(owned)


def _direct_child_pids(parent_pid: int | None = None) -> list[int]:
    """Return the pids whose parent is *parent_pid* (default: this process)."""

    if parent_pid is None:
        parent_pid = os.getpid()
    children: list[int] = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        _note_proc_enumeration_failure(
            "the process table could not be enumerated while scanning for direct children"
        )
        return children
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == parent_pid:
            continue
        raw = _read_proc_stat(pid)
        if raw is None:
            continue
        closing = raw.rfind(")")
        if closing == -1:
            _note_proc_enumeration_failure(
                f"the state of live process {pid} was malformed while scanning for direct children"
            )
            continue
        fields = raw[closing + 1 :].split()
        if len(fields) < 2:
            _note_proc_enumeration_failure(
                f"the state of live process {pid} was incomplete while scanning for direct children"
            )
            continue
        try:
            if int(fields[1]) == parent_pid:
                children.append(pid)
        except ValueError:
            _note_proc_enumeration_failure(
                f"the parent pid of live process {pid} was unreadable while scanning for "
                "direct children"
            )
            continue
    return sorted(children)


def _terminate_pids(pids: list[int], grace: float | None = None) -> tuple[bool, list[int]]:
    """Terminate *pids* outside a shared process group and confirm they exited.

    Detached descendants do not share the command's process group, so they are
    signalled individually.  The escalation and confirmation mirror
    ``_terminate_process_group`` so that a surviving descendant keeps the lease
    instead of being reported as released capacity.
    """

    if os.name == "nt":
        return True, []
    grace = _entry._CHILD_TERMINATION_GRACE_SECONDS if grace is None else grace
    remaining = sorted({pid for pid in pids if _entry._pid_alive(pid)})
    if not remaining:
        return True, []
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in remaining:
            if _entry._pid_is_zombie(pid):
                _reap_zombie(pid)
                continue
            try:
                os.kill(pid, sig)
            except OSError:
                try:
                    os.killpg(os.getpgid(pid), sig)
                except OSError:
                    continue
        deadline = time.monotonic() + grace
        while True:
            surviving: list[int] = []
            for pid in remaining:
                if _entry._pid_is_zombie(pid):
                    _reap_zombie(pid)
                    continue
                if _entry._pid_alive(pid):
                    surviving.append(pid)
            remaining = surviving
            if not remaining:
                return True, []
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    return False, remaining


def _sweep_leftover_owned_processes() -> list[int]:
    """Re-sweep recorded leftover descendants and return the survivors.

    Returning an empty list means every recorded pid is gone and the capacity
    can be released.  A non-empty list means owned work is still running, so
    the caller must keep the lease and report blocked cleanup.
    """

    still_alive: list[int] = []
    for pid in sorted(_LEFTOVER_OWNED_PIDS):
        if _entry._pid_is_zombie(pid):
            _reap_zombie(pid)
            _LEFTOVER_OWNED_PIDS.discard(pid)
            continue
        if not _entry._pid_alive(pid):
            _LEFTOVER_OWNED_PIDS.discard(pid)
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            _LEFTOVER_OWNED_PIDS.discard(pid)
            continue
        except OSError:
            still_alive.append(pid)
            continue
        deadline = time.monotonic() + _entry._CHILD_TERMINATION_GRACE_SECONDS
        while time.monotonic() < deadline and _entry._pid_alive(pid):
            time.sleep(0.05)
        if _entry._pid_alive(pid):
            still_alive.append(pid)
        else:
            _LEFTOVER_OWNED_PIDS.discard(pid)
    return still_alive


def _token_owned_live_pids(job_token: Any) -> list[int]:
    """Return live pids, other than this one, carrying *job_token* in their environment.

    A launch's ownership token is inherited by every descendant it forks or
    execs, so it is the one piece of evidence that survives both pid and
    process-group reuse.  It is also the only way a releaser that is not the
    holder's ancestor or subreaper can observe a descendant the holder detached
    during its own teardown.
    """

    if not isinstance(job_token, str) or not job_token.strip():
        return []
    if os.name == "nt":
        return []
    try:
        entries = os.listdir("/proc")
    except OSError:
        _note_proc_enumeration_failure(
            "the process table could not be enumerated while scanning for token-owned survivors"
        )
        return []
    found: list[int] = []
    for entry in entries:
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == os.getpid():
            continue
        if not _entry._pid_alive(pid) or _entry._pid_is_zombie(pid):
            continue
        environ = _entry._process_environ(pid)
        if environ is None:
            continue
        if environ.get(_JOB_OWNERSHIP_TOKEN_ENV) == job_token:
            found.append(pid)
    return sorted(found)


def _await_no_token_owned_survivors(job_token: Any, *, settle_seconds: float = 1.0) -> list[int]:
    """Return token-carrying descendants still alive after a bounded settle.

    A teardown handler can fork a detached descendant moments after the
    holder's tree was snapshotted.  The owned-process sweep cannot see that
    process (it is not our child), but it inherits the ownership token, so the
    scan is repeated across a short window before the caller may remove state.
    """

    if not isinstance(job_token, str) or not job_token.strip():
        return []
    deadline = time.monotonic() + max(0.0, settle_seconds)
    while True:
        survivors = _token_owned_live_pids(job_token)
        if survivors or time.monotonic() >= deadline:
            return survivors
        time.sleep(0.05)


def _contain_adopted_descendants(
    exclude: set[int] | None = None,
    *,
    deadline_seconds: float | None = None,
) -> tuple[bool, list[int]]:
    """Terminate every adopted orphan within a bounded deadline.

    One snapshot of this process's direct children is not enough.  Killing an
    adopted process reparents the descendants *it* had detached into its own
    session, so a nested orphan only becomes a direct child after the first
    sweep.  The adopted set is therefore rescanned until it stays empty, and a
    pid that cannot be confirmed gone is returned as a survivor so the caller
    keeps the lease instead of reporting released capacity.
    """

    if os.name == "nt":
        return True, []
    budget = (
        _entry._CHILD_TERMINATION_GRACE_SECONDS if deadline_seconds is None else deadline_seconds
    )
    deadline = time.monotonic() + budget
    confirmed = True
    survivors: set[int] = set()
    while True:
        # A short per-batch grace keeps a deep detached chain inside the
        # overall deadline instead of multiplying the escalation wait.
        per_batch = max(0.2, min(budget, 1.0))
        candidates = _adopted_orphan_pids(exclude)
        if candidates:
            batch_confirmed, remaining = _terminate_pids(candidates, grace=per_batch)
            if not batch_confirmed:
                confirmed = False
                survivors.update(remaining)
        remaining_now = _adopted_orphan_pids(exclude)
        if not remaining_now:
            return confirmed, sorted(survivors)
        if time.monotonic() >= deadline:
            return False, sorted(survivors | set(remaining_now))
        time.sleep(0.05)


def _register_owned(process: subprocess.Popen[str]) -> None:
    _OWNED_PROCESSES.add(process)


def _unregister_owned(process: subprocess.Popen[str]) -> None:
    _OWNED_PROCESSES.discard(process)


def _terminate_owned_process(process: subprocess.Popen[str]) -> None:
    """Terminate *process* and its descendants without masking the failure."""

    if os.name == "nt":
        taskkill = shutil.which("taskkill")
        if taskkill is not None:
            try:
                subprocess.run(
                    [taskkill, "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=_entry._CHILD_TERMINATION_GRACE_SECONDS,
                )
            except (OSError, subprocess.TimeoutExpired, KeyboardInterrupt):
                pass
        else:
            try:
                process.terminate()
            except (OSError, ValueError):
                pass
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            try:
                process.terminate()
            except (OSError, ValueError):
                pass
    try:
        process.wait(timeout=_entry._CHILD_TERMINATION_GRACE_SECONDS)
    except (subprocess.TimeoutExpired, OSError, KeyboardInterrupt):
        pass
    if os.name == "nt":
        try:
            process.kill()
        except (OSError, ValueError):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except (OSError, ValueError):
                pass
    try:
        process.wait(timeout=_entry._CHILD_TERMINATION_GRACE_SECONDS)
    except (subprocess.TimeoutExpired, OSError, KeyboardInterrupt):
        pass


def _terminate_owned_processes(*_args: object) -> None:
    for process in list(_OWNED_PROCESSES):
        _terminate_owned_process(process)
    _OWNED_PROCESSES.clear()


def _begin_cleanup() -> None:
    """Mark that owned-process cleanup is running.

    A cancel request that arrives here must not be delivered as a Python
    exception, because doing so would abort the very sweep that contains the
    allocation's descendants.  The signal is recorded instead and re-raised
    once cleanup finishes.
    """

    global _CLEANUP_ACTIVE
    _CLEANUP_ACTIVE = True


def _end_cleanup() -> int | None:
    """Stop deferring cancellation and return any deferred signal number."""

    global _CLEANUP_ACTIVE, _DEFERRED_SIGNAL
    _CLEANUP_ACTIVE = False
    signum = _DEFERRED_SIGNAL
    _DEFERRED_SIGNAL = None
    return signum


def _install_signal_handlers() -> None:
    global _SIGNAL_HANDLERS_INSTALLED
    if _SIGNAL_HANDLERS_INSTALLED or os.name == "nt":
        return
    _SIGNAL_HANDLERS_INSTALLED = True

    def _handler(signum: int, _frame: object) -> None:
        global _DEFERRED_SIGNAL
        if _CLEANUP_ACTIVE:
            # Cleanup is mid-flight; recording the signal keeps the bounded
            # descendant sweep from being interrupted, which would otherwise
            # exit with the signal's status while an owned process stayed live.
            _DEFERRED_SIGNAL = signum
            return
        _terminate_owned_processes()
        raise SystemExit(128 + signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, _handler)
        except (OSError, ValueError):
            continue


def run_command(
    command: list[str],
    cwd: Path,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
    on_start: Callable[[subprocess.Popen[str]], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run *command* under a deadline with owned-process cleanup.

    The command runs in its own session/process group, receives a parent-death
    signal, and is torn down as a group on timeout.  The timeout is reported as
    a distinct terminal result without discarding captured output.  *timeout*
    defaults to the bounded prerequisite deadline; callers running a
    qualification command pass their own deadline.

    When the command itself exits (cleanly, failing, or on timeout) it may
    still have left descendants running.  Two sweeps cover both shapes: the
    command's whole process group, and any descendant that detached into its
    own session/process group with ``start_new_session``.  Detached orphans are
    adopted because the runner is a child subreaper, so they stay visible in
    this process's tree.  Any descendant that cannot be confirmed gone is
    recorded on ``_LEFTOVER_OWNED_PIDS`` so the caller refuses to release the
    lease.  The original exit status and captured output are preserved either
    way.

    *on_start*, when given, is called with the live ``Popen`` immediately after
    the command has started, so a lease can persist durable job ownership
    before the command outlives its holder.  If it raises, the command and its
    descendants are torn down and the exception is re-raised only once
    containment has run, so a lease can never keep running work it cannot
    attribute.

    The sweeps run on the cancellation path too.  The installed signal handlers
    raise ``SystemExit`` from inside ``communicate``, so containment is not left
    to the normal-return path: a command interrupted by SIGINT/SIGTERM still
    has its whole group and every adopted descendant terminated and confirmed
    before the signal is allowed to end the process.
    """

    timeout = _command_timeout() if timeout is None else timeout
    # The containment outcome is published for the caller that owns the lease,
    # so the assignment below must reach the module global rather than a local.
    timed_out = False
    output_stream_held = False
    notes: list[str] = []
    group_leftovers: list[int] = []
    detached_leftovers: list[int] = []
    enumeration_failures: list[str] = []
    pending_error: BaseException | None = None
    deferred_signum: int | None = None
    with _adopted_descendants() as adoption:
        adoption_active = adoption.active
        preexisting = set(_direct_child_pids(os.getpid()))
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                **_process_group_options(),
            )
        except OSError as exc:
            _entry._LAST_COMMAND_CONTAINMENT = CommandContainment(
                proven=True,
                detail="the command could not be started, so no descendant exists",
                adoption_active=adoption_active,
            )
            return subprocess.CompletedProcess(command, 127, "", f"{type(exc).__name__}: {exc}")
        preexisting.discard(process.pid)
        _register_owned(process)
        try:
            try:
                # ``on_start`` runs inside the containment window: if a lease
                # cannot durably attribute the command it just spawned, the
                # command is torn down here instead of being left running with
                # no record that would let a later recovery find it.
                if on_start is not None:
                    on_start(process)
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                _begin_cleanup()
                # ``communicate`` waits for EOF as well as for the command to
                # exit.  A descendant that inherited the output pipe keeps it
                # open after the command itself finished, so this timeout is not
                # by itself proof that the command ran too long.  Whatever the
                # command wrote before the deadline is retained instead of being
                # replaced by empty strings.
                stdout, stderr = _timeout_partial_streams(exc)
                if process.poll() is None:
                    timed_out = True
                    _terminate_owned_process(process)
                else:
                    output_stream_held = True
                stdout, stderr = _drain_after_termination(process, (stdout, stderr))
            except (KeyboardInterrupt, SystemExit) as exc:
                _begin_cleanup()
                # A signal that interrupts the wait must not skip containment:
                # collect the command's output opportunistically, tear the
                # command down, and remember the exception to re-raise once the
                # owned process group and every adopted descendant are gone.
                pending_error = exc
                _terminate_owned_process(process)
                stdout, stderr = "", ""
                with contextlib.suppress(KeyboardInterrupt, SystemExit):
                    stdout, stderr = _drain_after_termination(process)
            except BaseException as exc:  # noqa: BLE001 - containment must run first
                _begin_cleanup()
                # A decoding failure (``UnicodeDecodeError``) or any other
                # error raised while reading the child's streams must not skip
                # containment.  The command may still have detached
                # descendants, so tear it down and remember the original error
                # to re-raise only after both sweeps have run.  The original
                # exception is preserved as the raised exception, so callers
                # still see the real failure.
                pending_error = exc
                _terminate_owned_process(process)
                stdout, stderr = "", ""
                with contextlib.suppress(BaseException):
                    stdout, stderr = _drain_after_termination(process)
        finally:
            _unregister_owned(process)
            # Cleanup of the just-terminated command is about to start; defer
            # any cancel request that would otherwise interrupt it.
            _begin_cleanup()

        # Containment runs on every path, including the cancellation path, so a
        # signal cannot leave an owned descendant holding the allocation.
        _reset_proc_enumeration_failures()
        try:
            group_confirmed, group_leftovers = _entry._terminate_process_group(process.pid)
            detached_confirmed, detached_leftovers = _entry._contain_adopted_descendants(
                preexisting
            )
            if not adoption_active:
                notes.append(
                    "the runner could not become a child subreaper, so descendants that "
                    "detached into a new session could not be observed or contained; "
                    "this run's containment is unproven"
                )
        finally:
            deferred_signum = _end_cleanup()
        enumeration_failures = _proc_enumeration_failures()

    leftovers = sorted(set(group_leftovers) | set(detached_leftovers))
    if not group_confirmed or not detached_confirmed:
        _LEFTOVER_OWNED_PIDS.update(leftovers)
    containment_proven = bool(
        group_confirmed and detached_confirmed and adoption_active and not enumeration_failures
    )
    if enumeration_failures:
        # An unreadable process table is unknown visibility, not an empty table.
        # A sweep that could not enumerate processes cannot prove containment,
        # so the lease must stay blocked rather than be released.
        containment_detail = (
            "the process table could not be fully enumerated, so containment is unproven: "
            + "; ".join(sorted(set(enumeration_failures))[:4])
        )
    elif not adoption_active:
        containment_detail = (
            "the runner could not become a child subreaper, so detached descendants "
            "could not be observed or contained"
        )
    elif leftovers:
        containment_detail = (
            f"{len(leftovers)} owned descendant(s) survived containment "
            "(" + ", ".join(str(pid) for pid in leftovers[:16]) + ")"
        )
    elif not group_confirmed:
        containment_detail = "the command's process group still has live members"
    elif not detached_confirmed:
        containment_detail = "adopted descendants survived containment"
    else:
        containment_detail = (
            "the command's process group and every adopted descendant were observed gone"
        )
    _entry._LAST_COMMAND_CONTAINMENT = CommandContainment(
        proven=containment_proven,
        detail=containment_detail,
        adoption_active=adoption_active,
        leftovers=tuple(leftovers),
    )
    if pending_error is not None:
        # The signal or interrupt still terminates the run, but only after the
        # descendant sweeps above have run; a survivor keeps the lease.
        raise pending_error
    if deferred_signum is not None:
        # A cancel request arrived while containment was running; it is honored
        # only now that the bounded cleanup has finished.
        raise SystemExit(128 + deferred_signum)
    if timed_out:
        notes.insert(
            0,
            f"command exceeded {timeout:g}s; its owned process group was terminated",
        )
    if output_stream_held:
        notes.append(
            "the command exited but a descendant kept its output stream open; "
            "the captured output may be truncated"
        )
    if leftovers:
        notes.append(
            f"{len(leftovers)} owned descendant(s) remained after the command exited; "
            "capacity must not be released"
        )
    returncode = _TIMEOUT_RETURNCODE if timed_out else process.returncode
    for note in notes:
        stderr = (stderr or "") + f"\n[qualification-runner] {note}"
    return subprocess.CompletedProcess(command, returncode, stdout, stderr)


# Call-time indirection so facade-level monkeypatches stay visible here.
import scripts.qualification_runner as _entry
