"""Post-ACK live-child refusal controls; authored, NOTRUN pending execution review."""

import ctypes
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from scripts.production_gate_parallel_adoption import read_subreaper
from tests import _parallel_runtime_nested_cleanup as nested
from tests._parallel_runtime_nested_retention import pending_nested_cleanup
from tests._parallel_runtime_nested_startup import NESTED_SOURCE
from tests._parallel_runtime_nested_startup_controls import exited, wait_json


@pytest.mark.parametrize("point", ["signal", "poll", "survived", "wait", "wait_pending", "close"])
@pytest.mark.parametrize("body_failure", [False, True])
def test_post_ack_refusal_retains_exact_owner_until_explicit_retry(
    tmp_path, monkeypatch, point, body_failure
):
    original_flag = read_subreaper(ctypes.CDLL(None, use_errno=True))
    owner = nested.NestedCleanup(tmp_path, NESTED_SOURCE)
    original_error = ValueError("controlled original fixture assertion")
    process, parent_fd, child_fd = None, None, None
    send_signal, waitid, close = signal.pidfd_send_signal, os.waitid, os.close
    rescue_actions = []
    try:
        owner.__enter__()
        job = tmp_path / "owned-post-ack-fixture"
        job.mkdir()
        process = subprocess.Popen(
            [sys.executable, "-c", NESTED_SOURCE, str(job), owner.endpoint, owner.token],
            start_new_session=True,
        )
        parent_fd = os.pidfd_open(process.pid)  # our unreaped direct Popen instance
        ready = wait_json(job / "nested-child.json")
        assert owner.metadata["child"] == ready
        target = owner.fds["child"]
        child_fd = os.dup(target)  # independent rescue handle to the same authenticated instance
        binding = os.fstat(target)
        actual_state = owner.state

        def failed_signal(fd, *args, **kwargs):
            if fd == target and point in ("signal", "survived"):
                if point == "signal":
                    raise OSError("controlled signal failure before syscall")
                return None  # child really survives; no artificial state() result
            return send_signal(fd, *args, **kwargs)

        def failed_state(name):
            if name == "child" and point == "poll":
                raise OSError("controlled poll failure")
            return actual_state(name)

        def failed_wait(kind, fd, options):
            if fd == target and point in ("wait", "wait_pending"):
                if point == "wait":
                    raise OSError("controlled wait failure before syscall")
                return None
            return waitid(kind, fd, options)

        def failed_close(fd):
            if fd == target and point == "close":
                raise OSError("controlled close failure before release")
            return close(fd)

        with monkeypatch.context() as patch:
            patch.setattr(signal, "pidfd_send_signal", failed_signal)
            patch.setattr(owner, "state", failed_state)
            patch.setattr(os, "waitid", failed_wait)
            patch.setattr(os, "close", failed_close)
            if body_failure:
                owner.__exit__(ValueError, original_error, None)
                assert any("outer cleanup also failed" in note for note in original_error.__notes__)
            else:
                with pytest.raises(AssertionError):
                    owner.__exit__()
        assert pending_nested_cleanup() == (owner,)
        assert owner.fds["child"] == target
        retained = os.fstat(target)
        assert (retained.st_dev, retained.st_ino) == (binding.st_dev, binding.st_ino)
        assert owner.adoption.restoration_pending and owner.adoption.active
        assert read_subreaper(ctypes.CDLL(None, use_errno=True)) == 1
        assert owner.directory is not None and os.path.isdir(owner.directory.name)
        if point in ("signal", "poll", "survived"):
            assert not exited(child_fd), "fault control failed to retain a genuinely live child"
        first = json.loads((tmp_path / "outer-nested-cleanup-attempt-1.json").read_text())
        assert first["problems"] and "child" in first["retained_instances"]
        deadline = owner.cleanup_deadline
        with pytest.raises(AssertionError, match="admission refused"):
            nested.NestedCleanup(tmp_path, NESTED_SOURCE).__enter__()
        try:
            owner.retry_cleanup()
        except AssertionError:
            assert pending_nested_cleanup() == (owner,)
        limit = time.monotonic() + 5.0
        while not exited(child_fd) and time.monotonic() < limit:
            time.sleep(0.01)
        assert exited(child_fd), "explicit retry did not terminate the held child"
        owner.retry_cleanup()
        assert owner.cleanup_deadline == deadline, "retry renewed the original cleanup allowance"
        assert not owner.fds and not pending_nested_cleanup()
        assert not owner.adoption.restoration_pending
        assert read_subreaper(ctypes.CDLL(None, use_errno=True)) == original_flag
        assert json.loads((tmp_path / "outer-nested-cleanup-attempt-1.json").read_text()) == first
    finally:
        # Independently held descriptors rescue failed assertions. Rescue is written
        # separately and cannot turn an earlier failed product assertion into PASS.
        for name, fd in (("child", child_fd), ("parent", parent_fd)):
            if fd is not None and not exited(fd):
                send_signal(fd, signal.SIGKILL)
                rescue_actions.append(name)
        if hasattr(owner, "cleanup_deadline"):
            try:
                owner.retry_cleanup()
            except AssertionError:
                assert owner in pending_nested_cleanup()
        if process is not None:
            process.wait(timeout=5.0)
        if child_fd is not None:
            limit = time.monotonic() + 5.0
            while not exited(child_fd) and time.monotonic() < limit:
                time.sleep(0.01)
            assert exited(child_fd), "independently owned rescue did not finish"
        if owner in pending_nested_cleanup():
            owner.retry_cleanup()
        for fd in (child_fd, parent_fd):
            if fd is not None:
                close(fd)
        (tmp_path / "post-ack-rescue.json").write_text(json.dumps({"signals": rescue_actions}))
        assert read_subreaper(ctypes.CDLL(None, use_errno=True)) == original_flag
