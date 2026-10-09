"""Owned-pidfd early-death controls; authored only, never product containment proof."""

import json
import os
import select
import signal
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path

from scripts.production_gate_parallel_processes import read_stat
from tests._parallel_runtime_nested_cleanup import NestedCleanup
from tests._parallel_runtime_nested_startup import NESTED_CHILD_SOURCE, NESTED_SOURCE


def exited(fd):
    poller = select.poll()
    poller.register(fd, select.POLLIN)
    events = poller.poll(0)
    assert not any(bits & (select.POLLERR | select.POLLNVAL) for _, bits in events)
    return any(bits & select.POLLIN for _, bits in events)


def wait_json(path):
    limit = time.monotonic() + 5.0
    while not path.exists() and time.monotonic() < limit:
        time.sleep(0.01)
    assert path.exists(), f"fixture did not reach controlled window: {path.name}"
    return json.loads(path.read_text())


def pin_child(instance, parent_pid, parent_argv, body, point):
    """Pin only the exact, independently observed child of our live owned parent."""

    pid, start = instance["pid"], instance["start"]
    first = read_stat(pid)
    assert first is not None and first[:2] == (start, parent_pid)
    fd = os.pidfd_open(pid)
    try:
        identity = next(
            line.split()[1]
            for line in Path(f"/proc/self/fdinfo/{fd}").read_text().splitlines()
            if line.startswith("Pid:")
        )
        assert int(identity) == pid
        command = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")[:-1]
        if point == "before_guard":
            assert command == parent_argv  # still in its trusted single-threaded preexec callback
        else:
            controls = body["control_fds"]
            assert command == [
                sys.executable,
                "-c",
                NESTED_CHILD_SOURCE,
                str(controls["release"]),
                str(controls["confirm"]),
                str(parent_pid),
            ]
        again = read_stat(pid)
        assert again is not None and again[:2] == first[:2]
        return fd
    except BaseException:
        os.close(fd)
        raise


def early_nested_death_case(tmp_path, point):
    job = tmp_path / "early-owned-fixture"
    job.mkdir()
    pause = threading.Event() if point in ("after_sendmsg", "reject_handoff") else None
    owner = NestedCleanup(
        tmp_path,
        NESTED_SOURCE,
        startup_mode=point,
        handoff_pause=pause,
        reject_handoff=point == "reject_handoff",
    )
    process, parent_fd, child_fd = None, None, None
    automatic_exit, cleanup_actions = None, []
    with owner, ExitStack() as descriptors:
        try:
            argv = [
                sys.executable,
                "-c",
                NESTED_SOURCE,
                str(job),
                owner.endpoint,
                owner.token,
                point,
            ]
            process = subprocess.Popen(argv, start_new_session=True)
            parent_fd = os.pidfd_open(process.pid)  # our unreaped direct Popen instance
            descriptors.callback(os.close, parent_fd)
            marker = {
                "before_guard": "nested-child-startup.json",
                "before_handoff": "nested.before-handoff.json",
                "after_sendmsg": "nested.after-sendmsg.json",
                "reject_handoff": "nested.after-sendmsg.json",
            }[point]
            body = wait_json(job / marker)
            instance = body if point == "before_guard" else body["child"]
            child_fd = pin_child(instance, process.pid, argv, body, point)
            descriptors.callback(os.close, child_fd)
            if point == "reject_handoff":
                pause.set()
            else:
                signal.pidfd_send_signal(parent_fd, signal.SIGKILL)
                if pause is not None:
                    pause.set()
            process.wait(timeout=5.0)
            limit = time.monotonic() + 5.0
            while not exited(child_fd) and time.monotonic() < limit:
                time.sleep(0.01)
            automatic_exit = exited(child_fd)
            # This assertion occurs before any outer/manual cleanup signal to the child.
            assert automatic_exit, "child survived parent death/rejected ownership"
            assert not (job / "nested-child.json").exists(), "startup published unowned readiness"
            assert process.returncode != 0
        finally:
            cleanup_problems = []
            if pause is not None:
                pause.set()
            for name, fd in (("child", child_fd), ("parent", parent_fd)):
                try:
                    if fd is not None and not exited(fd):
                        signal.pidfd_send_signal(fd, signal.SIGKILL)
                        cleanup_actions.append(name)
                except Exception as error:  # noqa: BLE001 - later held instances still owe cleanup
                    cleanup_problems.append(f"{name}: {type(error).__name__}: {error}")
            if process is not None:
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    cleanup_problems.append("owned nested parent did not exit within cleanup bound")
            if child_fd is not None:
                try:
                    limit = time.monotonic() + 3.0
                    while not exited(child_fd) and time.monotonic() < limit:
                        time.sleep(0.01)
                    assert exited(child_fd), "owned child survived failure cleanup"
                    os.waitid(os.P_PIDFD, child_fd, os.WEXITED | os.WNOHANG)
                except ChildProcessError:
                    pass  # rejected parent reaped it before exiting
                except Exception as error:  # noqa: BLE001 - descriptor ExitStack still runs
                    cleanup_problems.append(f"child cleanup: {type(error).__name__}: {error}")
            (job / "early-startup-control.json").write_text(
                json.dumps(
                    {
                        "scope": "trusted fixture startup safety, never product containment proof",
                        "point": point,
                        "child_exited_before_failure_cleanup": automatic_exit,
                        "failure_cleanup_signals": cleanup_actions,
                        "unobserved_child_identity": child_fd is None,
                        "cleanup_problems": cleanup_problems,
                    }
                )
            )
            assert not cleanup_problems, "; ".join(cleanup_problems)
    assert cleanup_actions == [], (
        "negative control needed rescue instead of automatic startup cleanup"
    )
