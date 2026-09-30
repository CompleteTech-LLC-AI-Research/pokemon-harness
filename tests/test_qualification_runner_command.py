"""Command containment and run_command tests.

Split from ``tests/test_qualification_runner.py`` for issue #112 with no
behavior change: every test below is preserved verbatim.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import qualification_runner as runner
from tests._qualification_runner_support import (
    _LOCK_OBSERVATION_SUPPORTED,
    REPO_ROOT,
    _job_record,
    _stale_job_dir,
    _wait_for_dead,
    _write_grandchild_command,
    held_reservation,
    rewrite_descriptor,
)


def test_recover_refuses_while_the_recorded_job_still_runs(tmp_path: Path):
    """A dead holder is not free capacity while its recorded job is alive."""

    record = _job_record(
        job_pid=os.getpid(),
        job_start_time=runner._process_start_time(os.getpid()),
        job_process_group=os.getpid(),
        job_session=os.getpid(),
    )
    declaration, descriptor_path, _job_dir = _stale_job_dir(tmp_path, record)
    status, message = runner._recover_allocation(declaration, tmp_path)
    assert status == "blocked"
    assert "still running" in message
    assert descriptor_path.exists()


def test_recover_refuses_unconfirmed_containment_it_cannot_verify(tmp_path: Path, monkeypatch):
    """A record that never proved containment keeps the lease unless proven now."""

    declaration, descriptor_path, job_dir = _stale_job_dir(
        tmp_path, _job_record(containment_confirmed=False)
    )
    monkeypatch.setattr(runner, "_child_subreaper_state", lambda: 0)
    monkeypatch.setattr(runner, "_contain_adopted_descendants", lambda *a, **k: (True, []))
    status, message = runner._recover_allocation(declaration, tmp_path)
    assert status == "blocked"
    assert "never confirmed descendant containment" in message
    assert descriptor_path.exists()
    assert (job_dir / runner._JOB_RUN_RECORD_NAME).exists()


def test_recover_refuses_when_adopted_descendants_survive(tmp_path: Path, monkeypatch):
    """An orphaned survivor keeps the lease even when the holder is gone."""

    declaration, descriptor_path, _job_dir = _stale_job_dir(tmp_path, _job_record())
    monkeypatch.setattr(runner, "_contain_adopted_descendants", lambda *a, **k: (False, [4242]))
    status, message = runner._recover_allocation(declaration, tmp_path)
    assert status == "blocked"
    assert "4242" in message
    assert descriptor_path.exists()


def test_recover_removes_state_for_a_confirmed_contained_job(tmp_path: Path, monkeypatch):
    """A recorded, confirmed, no-longer-running job releases the stale state."""

    declaration, descriptor_path, _job_dir = _stale_job_dir(tmp_path, _job_record())
    monkeypatch.setattr(runner, "_contain_adopted_descendants", lambda *a, **k: (True, []))
    status, message = runner._recover_allocation(declaration, tmp_path)
    assert status == "ok", message
    assert not descriptor_path.exists()


def test_release_refuses_when_the_job_record_never_confirmed_containment(tmp_path: Path):
    """The recorded holder only releases a lease it proved was contained."""

    if not _LOCK_OBSERVATION_SUPPORTED:
        pytest.skip("kernel lock table is not observable in this sandbox")
    declaration, _facts = held_reservation(tmp_path, "cgroup-quota")
    job_dir = Path(declaration["reservation"]["job_dir"])
    (job_dir / runner._JOB_RUN_RECORD_NAME).write_text(
        json.dumps(_job_record(containment_confirmed=False)), encoding="utf-8"
    )
    status, message = runner._release_allocation(declaration, tmp_path)
    assert status == "blocked"
    assert "never confirmed descendant containment" in message
    assert Path(declaration["reservation"]["descriptor_path"]).exists()


def test_release_refuses_unconfirmed_containment_for_an_external_holder(
    tmp_path: Path, monkeypatch
):
    """A releaser outside the holder's ancestry cannot adopt its orphans.

    The round-12 finding: an external ``--release`` snapshotted the holder's
    tree, killed it, and returned ``ok`` even though the holder's SIGTERM
    handler could fork a detached descendant after the snapshot.  Setting the
    releaser's subreaper flag does not adopt a sibling's children, so a record
    that never proved containment must keep the lease blocked.
    """

    if not _LOCK_OBSERVATION_SUPPORTED:
        pytest.skip("kernel lock table is not observable in this sandbox")
    declaration, _facts = held_reservation(tmp_path, "cgroup-quota")
    descriptor_path = Path(declaration["reservation"]["descriptor_path"])
    job_dir = Path(declaration["reservation"]["job_dir"])
    # A holder that is not this process and never recorded containment proof.
    rewrite_descriptor(declaration, {"holder_pid": 2**31 - 1, "holder_start_time": "0"})
    monkeypatch.setattr(runner, "_holder_is_owned", lambda holder, start: True)
    (job_dir / runner._JOB_RUN_RECORD_NAME).write_text(
        json.dumps(_job_record(containment_confirmed=False)), encoding="utf-8"
    )
    status, message = runner._release_allocation(declaration, tmp_path)
    assert status == "blocked"
    assert "never confirmed descendant containment" in message
    assert descriptor_path.exists(), "release removed state it could not prove was free"


def test_release_blocks_when_a_token_owned_descendant_survives(tmp_path: Path):
    """The launch's inherited token reveals a descendant the sweep cannot see."""

    if not _LOCK_OBSERVATION_SUPPORTED:
        pytest.skip("kernel lock table is not observable in this sandbox")
    declaration, _facts = held_reservation(tmp_path, "cgroup-quota")
    descriptor_path = Path(declaration["reservation"]["descriptor_path"])
    job_dir = Path(declaration["reservation"]["job_dir"])
    token = "c0ffee" * 5 + "abcd"
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True,
        env={**os.environ, runner._JOB_OWNERSHIP_TOKEN_ENV: token},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        (job_dir / runner._JOB_RUN_RECORD_NAME).write_text(
            json.dumps(_job_record(containment_confirmed=True, job_token=token)),
            encoding="utf-8",
        )
        status, message = runner._release_allocation(declaration, tmp_path)
        assert status == "blocked", message
        assert str(sleeper.pid) in message
        assert descriptor_path.exists(), "release removed state while owned work ran"
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)


def test_release_blocks_on_launch_intent_without_ownership_record(tmp_path: Path, monkeypatch):
    """An external releaser must not free a lease whose launched work is unidentified.

    The round-13 finding: a launch intent written before the spawn survived a
    holder that died before its ownership record was written.  The external
    ``--release`` read "no record" as "no job", deleted the descriptor, and left
    a detached descendant alive.  Missing ownership evidence must block.
    """

    if not _LOCK_OBSERVATION_SUPPORTED:
        pytest.skip("kernel lock table is not observable in this sandbox")
    declaration, _facts = held_reservation(tmp_path, "cgroup-quota")
    descriptor_path = Path(declaration["reservation"]["descriptor_path"])
    job_dir = Path(declaration["reservation"]["job_dir"])
    rewrite_descriptor(declaration, {"holder_pid": 2**31 - 1, "holder_start_time": "0"})
    monkeypatch.setattr(runner, "_holder_is_owned", lambda holder, start: True)
    runner._write_job_launch_intent(job_dir, [sys.executable, "-c", "pass"])
    assert not (job_dir / runner._JOB_RUN_RECORD_NAME).exists()
    status, message = runner._release_allocation(declaration, tmp_path)
    assert status == "blocked"
    assert "launch intent" in message
    assert descriptor_path.exists(), "release removed state whose ownership was unproven"


def test_blocked_release_keeps_the_lease_lock_held(tmp_path: Path):
    """A blocked release must not relinquish the kernel lease lock.

    The round-14 finding: the holder closed its lease descriptors before the
    token-survivor check, so a ``blocked`` release still freed the lock and a
    competing reservation could acquire it while a descendant was alive.
    """

    if not _LOCK_OBSERVATION_SUPPORTED:
        pytest.skip("kernel lock table is not observable in this sandbox")
    declaration, _facts = held_reservation(tmp_path, "cgroup-quota")
    job_dir = Path(declaration["reservation"]["job_dir"])
    lock_path = Path(declaration["reservation"]["host_lock_path"])
    token = "round-14-live-descendant"
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env={**os.environ, runner._JOB_OWNERSHIP_TOKEN_ENV: token},
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        (job_dir / runner._JOB_RUN_RECORD_NAME).write_text(
            json.dumps(_job_record(containment_confirmed=True, job_token=token)),
            encoding="utf-8",
        )
        status, message = runner._release_allocation(declaration, tmp_path)
        assert status == "blocked", message
        assert sleeper.poll() is None
        competitor = subprocess.run(
            [
                sys.executable,
                "-c",
                "import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_RDWR); "
                + "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)",
                str(lock_path),
            ],
            capture_output=True,
            timeout=5,
            check=False,
        )
        assert competitor.returncode != 0, (
            "release reported blocked but another process acquired the lease lock"
        )
    finally:
        sleeper.kill()
        sleeper.wait(timeout=5)
        runner._close_held_lease_descriptors(runner._lock_identity(lock_path))


def test_run_command_preserves_failure(tmp_path: Path):
    process = runner.run_command([sys.executable, "-c", "import sys; sys.exit(3)"], tmp_path)
    assert process.returncode == 3


def test_run_command_times_out_and_reaps_tree(tmp_path: Path):
    pidfile = tmp_path / "sleep.pid"
    code = (
        "import os, time; from pathlib import Path; "
        f"Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )
    process = runner.run_command([sys.executable, "-c", code], tmp_path, timeout=1.0)
    assert process.returncode == runner._TIMEOUT_RETURNCODE
    assert "exceeded" in process.stderr
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not pidfile.exists():
        time.sleep(0.05)
    assert pidfile.exists()
    child_pid = int(pidfile.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and runner._pid_alive(child_pid):
        time.sleep(0.05)
    assert not runner._pid_alive(child_pid)


def test_run_command_child_dies_with_parent(tmp_path: Path):
    pidfile = tmp_path / "child.pid"
    child = tmp_path / "child.py"
    child.write_text(
        "import os, time\n"
        "from pathlib import Path\n"
        f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "from scripts import qualification_runner as runner\n"
        f"runner.run_command([sys.executable, {str(child)!r}], Path({str(tmp_path)!r}))\n",
        encoding="utf-8",
    )
    env = dict(os.environ, PYTHONPATH=f"{REPO_ROOT}:{REPO_ROOT / 'src'}")
    process = subprocess.Popen(
        [sys.executable, str(parent)], cwd=REPO_ROOT, env=env, start_new_session=True
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not pidfile.exists():
            time.sleep(0.05)
        assert pidfile.exists()
        child_pid = int(pidfile.read_text(encoding="utf-8"))
        process.kill()
        process.wait(timeout=5)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and runner._pid_alive(child_pid):
            time.sleep(0.05)
        assert not runner._pid_alive(child_pid)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_run_command_sweeps_grandchild_after_successful_parent_exit(tmp_path: Path):
    """A command that exits 0 must not leave a live descendant running."""

    pidfile = tmp_path / "grandchild.pid"
    command = _write_grandchild_command(tmp_path, 0, pidfile)
    process = runner.run_command(command, tmp_path)
    assert process.returncode == 0
    assert pidfile.exists()
    grandchild = int(pidfile.read_text(encoding="utf-8"))
    assert _wait_for_dead(grandchild), "the surviving grandchild was not swept"


def test_run_command_preserves_failure_with_live_grandchild(tmp_path: Path):
    """Descendant cleanup must preserve the original failing exit status."""

    pidfile = tmp_path / "grandchild.pid"
    command = _write_grandchild_command(tmp_path, 7, pidfile)
    process = runner.run_command(command, tmp_path)
    assert process.returncode == 7
    assert pidfile.exists()
    grandchild = int(pidfile.read_text(encoding="utf-8"))
    assert _wait_for_dead(grandchild)
