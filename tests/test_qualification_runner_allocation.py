"""Allocation reserve and release tests.

Split from ``tests/test_qualification_runner.py`` for issue #112 with no
behavior change: every test below is preserved verbatim.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts import qualification_runner as runner
from tests._qualification_runner_support import (
    _LOCK_OBSERVATION_SUPPORTED,
    REPO_ROOT,
    Completed,
    _job_record,
    _recreate_lock_at_same_path,
    _stale_job_dir,
    _StubProcess,
    held_reservation,
    make_declaration,
    make_facts,
    rewrite_descriptor,
    statuses,
)


def test_release_refuses_recreated_lock_inode(tmp_path: Path):
    declaration, _facts = held_reservation(tmp_path, "cgroup-quota")
    descriptor_path = Path(declaration["reservation"]["descriptor_path"])
    _recreate_lock_at_same_path(declaration)
    status, message = runner._release_allocation(declaration, tmp_path)
    assert status == "blocked"
    assert "different allocation" in message or "inode" in message
    assert descriptor_path.exists()


def test_recover_refuses_recreated_lock_inode(tmp_path: Path, monkeypatch):
    declaration, _facts = held_reservation(tmp_path, "cgroup-quota")
    descriptor_path = Path(declaration["reservation"]["descriptor_path"])
    _recreate_lock_at_same_path(declaration)
    monkeypatch.setattr(runner, "_pid_alive", lambda pid: False)
    status, _message = runner._recover_allocation(declaration, tmp_path)
    assert status == "blocked"
    assert descriptor_path.exists()


def test_lease_status_rejects_recreated_lock_inode(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    descriptor = json.loads(
        Path(declaration["reservation"]["descriptor_path"]).read_text(encoding="utf-8")
    )
    job_dir = Path(declaration["reservation"]["job_dir"])
    _recreate_lock_at_same_path(declaration)
    status, detail = runner._descriptor_lease_status(descriptor, facts, job_dir)
    assert status == "fail"
    assert "recreated" in detail


def test_lease_status_rejects_descriptor_without_lock_identity(tmp_path: Path):
    """A pathname-only lease cannot prove exclusivity against a recreated inode."""

    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    descriptor = json.loads(
        Path(declaration["reservation"]["descriptor_path"]).read_text(encoding="utf-8")
    )
    job_dir = Path(declaration["reservation"]["job_dir"])
    descriptor.pop("lock_identity")
    status, detail = runner._descriptor_lease_status(descriptor, facts, job_dir)
    assert status == "fail"
    assert "does not record the allocation lock identity" in detail


def test_release_preserves_shared_lock_and_marker(tmp_path: Path):
    """Release must remove only the private descriptor.

    Deleting the host lock would let the next allocation create a fresh,
    uncontended inode while this one is still held, which is the round-4
    finding. The operator-owned exclusive marker likewise outlives the job.
    """

    declaration, _facts = held_reservation(tmp_path, "dedicated-host")
    reservation = declaration["reservation"]
    lock_path = Path(reservation["host_lock_path"])
    marker_path = Path(reservation["exclusive_marker_path"])
    descriptor_path = Path(reservation["descriptor_path"])
    status, message = runner._release_allocation(declaration, tmp_path)
    assert status == "ok", message
    assert not descriptor_path.exists()
    assert lock_path.exists()
    assert marker_path.exists()
    assert runner._lock_identity(lock_path) is not None


def test_terminate_and_confirm_reports_failure_when_holder_survives(monkeypatch):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    monkeypatch.setattr(runner, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(runner, "_CHILD_TERMINATION_GRACE_SECONDS", 0.05)
    try:
        terminated, detail = runner._terminate_and_confirm(child.pid)
        assert terminated is False
        assert "still running" in detail
    finally:
        child.kill()
        child.wait(timeout=5)


def test_terminate_and_confirm_succeeds_when_holder_exits(monkeypatch):
    monkeypatch.setattr(runner, "_pid_alive", lambda pid: False)
    terminated, detail = runner._terminate_and_confirm(2**31 - 1)
    assert terminated is True
    assert detail == ""


def test_release_keeps_state_when_termination_unconfirmed(tmp_path: Path, monkeypatch):
    declaration, _facts = held_reservation(tmp_path, "cgroup-quota")
    descriptor_path = Path(declaration["reservation"]["descriptor_path"])
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        rewrite_descriptor(
            declaration,
            {
                "holder_pid": child.pid,
                "holder_start_time": runner._process_start_time(child.pid),
            },
        )
        # The holder must be treated as an owned lease for release to attempt
        # termination at all; this test isolates the confirmation step.
        monkeypatch.setattr(runner, "_holder_is_owned", lambda holder, start: True)
        monkeypatch.setattr(runner, "_terminate_and_confirm", lambda pid: (False, "still running"))
        status, message = runner._release_allocation(declaration, tmp_path)
        assert status == "blocked"
        assert message == "still running"
        assert descriptor_path.exists()
    finally:
        child.kill()
        child.wait(timeout=5)


def test_reserve_allocation_rejects_unidentifiable_lock(tmp_path: Path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    target = tmp_path / "host.lock"
    target.write_text("", encoding="utf-8")
    link = tmp_path / "host-link.lock"
    link.symlink_to(target)
    declaration = make_declaration()
    declaration["reservation"]["host_lock_path"] = str(link)
    with pytest.raises(ValueError):
        runner._reserve_allocation(declaration, tmp_path, job_dir, make_facts())


def test_lock_identity_rejects_symlink_and_non_regular(tmp_path: Path):
    target = tmp_path / "target.lock"
    target.write_text("", encoding="utf-8")
    link = tmp_path / "link.lock"
    link.symlink_to(target)
    directory = tmp_path / "dir.lock"
    directory.mkdir()
    assert runner._lock_identity(link) is None
    assert runner._lock_identity(directory) is None
    assert runner._lock_identity(tmp_path / "missing.lock") is None


def test_lock_identity_mismatch_detects_recreated_inode(tmp_path: Path):
    lock_path = tmp_path / "host.lock"
    lock_path.write_text("", encoding="utf-8")
    recorded = runner._lock_identity(lock_path)
    assert recorded is not None
    other_path = tmp_path / "other.lock"
    other_path.write_text("", encoding="utf-8")
    other = runner._lock_identity(other_path)
    assert other is not None
    assert other["inode"] != recorded["inode"]
    assert not runner._lock_identity_matches(recorded, other)
    assert runner._lock_identity_matches(recorded, runner._lock_identity(lock_path))
    assert not runner._lock_identity_matches(None, recorded)
    assert not runner._lock_identity_matches(recorded, None)


def test_qualification_timeout_is_separate_from_prerequisite(monkeypatch):
    monkeypatch.delenv(runner._PROBE_TIMEOUT_ENV, raising=False)
    monkeypatch.delenv(runner._QUALIFICATION_TIMEOUT_ENV, raising=False)
    assert runner._command_timeout() == 300.0
    assert runner._qualification_timeout() == runner._DEFAULT_QUALIFICATION_TIMEOUT_SECONDS
    assert runner._qualification_timeout(5.0) == 5.0
    assert runner._qualification_timeout(0) == runner._DEFAULT_QUALIFICATION_TIMEOUT_SECONDS


def test_reserve_run_requires_admission(monkeypatch, capsys, tmp_path: Path):
    declaration_path = tmp_path / "declaration.json"
    declaration_path.write_text(json.dumps({}), encoding="utf-8")
    sentinel = tmp_path / "ran.txt"
    script = f"from pathlib import Path; Path({str(sentinel)!r}).write_text('ran')"
    calls: list[int] = []
    monkeypatch.setattr(runner, "prerequisite_checks", lambda decl, root: calls.append(1) or [])
    exit_code = runner.main(
        [
            "--reserve",
            "--json",
            "--declaration",
            str(declaration_path),
            "--job-dir",
            str(tmp_path / "job"),
            "--run",
            sys.executable,
            "-c",
            script,
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert "admission" in payload["message"]
    assert not sentinel.exists()
    assert calls == []


def test_reserve_run_configures_private_paths_and_timeout(monkeypatch, tmp_path: Path):
    if not _LOCK_OBSERVATION_SUPPORTED:
        pytest.skip("kernel lock table is not observable in this sandbox")
    declaration = make_declaration()
    declaration["reservation"]["host_lock_path"] = str(tmp_path / "host.lock")
    declaration_path = tmp_path / "declaration.json"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    job_dir = tmp_path / "job"
    captured: dict = {}

    def fake_run(command, cwd, timeout=None, env=None, on_start=None):
        captured["timeout"] = timeout
        captured["env"] = env
        captured["on_start"] = on_start
        # ``run_command`` records durable ownership of the live command before
        # it can outlive the holder, so the stub does the same against a real
        # short-lived child: the record must carry an observable start time,
        # exactly as a production run's does.
        child = subprocess.Popen([sys.executable, "-c", "pass"], **runner._process_group_options())
        on_start(child)
        child.wait()
        # ``_do_reserve`` only releases the lease on a *proven* containment
        # outcome, so the stub publishes one exactly as ``run_command`` does.
        runner._LAST_COMMAND_CONTAINMENT = runner.CommandContainment(
            proven=True, detail="stub command contained", adoption_active=True
        )
        return Completed(0, "", "")

    monkeypatch.setattr(runner, "run_command", fake_run)
    monkeypatch.setattr(
        runner,
        "prerequisite_checks",
        lambda decl, root: [runner.CheckResult("stub", "ok", 1, 1, "")],
    )
    monkeypatch.setattr(
        runner,
        "evaluate_resources",
        lambda decl, facts, root: [runner.CheckResult("stub", "ok", 1, 1, "")],
    )
    args = argparse.Namespace(job_dir=job_dir, run=[sys.executable, "-c", "pass"], run_timeout=5.0)
    status, message, _extra = runner._do_reserve(
        args, declaration, declaration_path, tmp_path, make_facts()
    )
    assert status == "ok", message
    assert captured["timeout"] == 5.0
    assert callable(captured["on_start"])
    assert captured["env"]["TMPDIR"] == str(job_dir / "tmp")
    assert captured["env"]["POKERED_QUALIFICATION_EVIDENCE_DIR"] == str(job_dir / "evidence")


def test_reserve_run_keeps_the_lease_when_containment_is_unproven(monkeypatch, tmp_path: Path):
    """A run whose descendant containment was not proven must not release.

    The durable job record is written before the command can outlive its
    holder, and an unproven outcome keeps the lease so a later ``--recover``
    still has the state it needs to contain the survivors.
    """

    if not _LOCK_OBSERVATION_SUPPORTED:
        pytest.skip("kernel lock table is not observable in this sandbox")
    declaration = make_declaration()
    declaration["reservation"]["host_lock_path"] = str(tmp_path / "host.lock")
    declaration_path = tmp_path / "declaration.json"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    job_dir = tmp_path / "job"
    recorded: dict = {}

    def fake_run(command, cwd, timeout=None, env=None, on_start=None):
        assert on_start is not None, "durable ownership must be recorded before the run"
        on_start(_StubProcess(os.getpid()))
        recorded["record"] = json.loads(
            (job_dir / runner._JOB_RUN_RECORD_NAME).read_text(encoding="utf-8")
        )
        runner._LAST_COMMAND_CONTAINMENT = runner.CommandContainment(
            proven=False, detail="stub adoption unavailable", adoption_active=False
        )
        return Completed(0, "ran", "")

    monkeypatch.setattr(runner, "run_command", fake_run)
    monkeypatch.setattr(
        runner,
        "prerequisite_checks",
        lambda decl, root: [runner.CheckResult("stub", "ok", 1, 1, "")],
    )
    monkeypatch.setattr(
        runner,
        "evaluate_resources",
        lambda decl, facts, root: [runner.CheckResult("stub", "ok", 1, 1, "")],
    )
    args = argparse.Namespace(job_dir=job_dir, run=[sys.executable, "-c", "pass"], run_timeout=5.0)
    status, message, _extra = runner._do_reserve(
        args, declaration, declaration_path, tmp_path, make_facts()
    )
    assert status == "blocked", message
    assert "could not be proven" in message
    assert recorded["record"]["containment_confirmed"] is False
    assert (job_dir / "allocation.json").exists(), "the unproven lease was released"


def test_scrub_absolute_paths_redacts_build_diagnostics():
    text = "cc failed at /var/private/operator-name/build-tmp/compiler.log via /opt/tool/bin/cc"
    scrubbed = runner._scrub_absolute_paths(text)
    assert "/var/private" not in scrubbed
    assert "/opt/tool" not in scrubbed
    assert "<redacted-path>" in scrubbed


def test_redact_payload_scrubs_unregistered_build_paths():
    payload = {"detail": "/var/private/operator-name/build-tmp/compiler.log"}
    rendered = json.dumps(runner._redact_payload(payload, {}))
    assert "/var/private" not in rendered
    assert "<redacted-path>" in rendered


def test_recover_does_not_signal_a_group_whose_pid_was_reused(tmp_path: Path):
    """A reused pid must not authorize signalling the recorded process group.

    The record is the only ownership evidence recovery has.  When the recorded
    pid now belongs to an unrelated process, the recorded group id may belong to
    that process, so recovery must block instead of terminating it.
    """

    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert runner._process_start_time(sleeper.pid) != "0"
        declaration, descriptor_path, _job_dir = _stale_job_dir(
            tmp_path,
            _job_record(
                job_pid=sleeper.pid,
                job_process_group=sleeper.pid,
                job_session=sleeper.pid,
            ),
        )
        status, message = runner._recover_allocation(declaration, tmp_path)
        assert status == "blocked", message
        assert "ownership cannot be proven" in message
        assert sleeper.poll() is None, (
            f"recovery killed an unrelated process that reused the recorded pid "
            f"(rc={sleeper.returncode}, message={message!r})"
        )
        assert descriptor_path.exists(), "the unproven lease state was removed"
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
        sleeper.wait(timeout=5)


def test_recorded_group_ownership_requires_the_job_token(tmp_path: Path):
    """A member is owned only when it carries the launch's environment token."""

    token = "0123456789abcdef" * 2
    untokened = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
        env={**os.environ, "UNRELATED": "1"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    tokened = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
        env={**os.environ, runner._JOB_OWNERSHIP_TOKEN_ENV: token},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        unowned, detail = runner._recorded_job_group_ownership(
            untokened.pid, runner._process_start_time(untokened.pid), token
        )
        assert not unowned
        assert "ownership token" in detail
        owned, owned_detail = runner._recorded_job_group_ownership(
            tokened.pid, runner._process_start_time(tokened.pid), token
        )
        assert owned, owned_detail
        # A start time alone never authorizes the group.
        missing, missing_detail = runner._recorded_job_group_ownership(
            tokened.pid, runner._process_start_time(tokened.pid), None
        )
        assert not missing
        assert "ownership token" in missing_detail
    finally:
        for process in (untokened, tokened):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def test_recover_does_not_signal_a_group_without_the_ownership_token(tmp_path: Path):
    """A newer start time is not ownership: a reused group needs the token."""

    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
        env={**os.environ, "UNRELATED": "1"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    dead = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        dead_start = runner._process_start_time(dead.pid)
        dead.wait(timeout=5)
        declaration, descriptor_path, _job_dir = _stale_job_dir(
            tmp_path,
            _job_record(
                job_pid=dead.pid,
                job_start_time=dead_start,
                job_process_group=sleeper.pid,
                job_session=sleeper.pid,
                job_token="f" * 32,
            ),
        )
        status, message = runner._recover_allocation(declaration, tmp_path)
        assert status == "blocked", message
        assert "ownership token" in message
        assert sleeper.poll() is None, "recovery killed a group it did not own"
        assert descriptor_path.exists(), "the unproven lease state was removed"
    finally:
        for process in (sleeper, dead):
            if process.poll() is None:
                process.kill()
            with contextlib.suppress(OSError):
                process.wait(timeout=5)


def test_recover_refuses_an_incomplete_ownership_record(tmp_path: Path):
    """A record without a usable pid/start time cannot authorize a group sweep."""

    declaration, descriptor_path, _job_dir = _stale_job_dir(
        tmp_path, _job_record(job_pid=None, job_start_time=None)
    )
    status, message = runner._recover_allocation(declaration, tmp_path)
    assert status == "blocked", message
    assert "ownership" in message
    assert descriptor_path.exists()


def test_launch_intent_without_an_ownership_record_blocks_containment(tmp_path: Path):
    """A recorded launch whose ownership record is missing is unproven work."""

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    runner._write_job_launch_intent(job_dir, [sys.executable, "-c", "pass"])
    for contain_adopted in (False, True):
        confirmed, detail = runner._confirm_recorded_job_containment(
            job_dir, contain_adopted=contain_adopted
        )
        assert not confirmed, (contain_adopted, detail)
        assert "launch intent" in detail


def test_recover_refuses_a_launch_intent_without_an_ownership_record(tmp_path: Path):
    """Recovery must not delete a lease whose launched job cannot be identified."""

    declaration, descriptor_path, job_dir = _stale_job_dir(tmp_path, None)
    runner._write_job_launch_intent(job_dir, [sys.executable, "-c", "pass"])
    status, message = runner._recover_allocation(declaration, tmp_path)
    assert status == "blocked", message
    assert "launch intent" in message
    assert descriptor_path.exists(), "lease state was removed with no ownership evidence"


def test_write_job_run_record_is_terminal_when_the_record_cannot_be_written(tmp_path: Path):
    """A swallowed write failure would leave running work that reads as no job."""

    if os.geteuid() == 0:
        pytest.skip("permission failures cannot be provoked as root")
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        **runner._process_group_options(),
    )
    try:
        os.chmod(job_dir, 0o500)
        with pytest.raises(runner.JobOwnershipError) as excinfo:
            runner._write_job_run_record(job_dir, child, containment_confirmed=False)
        assert "could not be written" in str(excinfo.value)
        assert not (job_dir / runner._JOB_RUN_RECORD_NAME).exists()
    finally:
        os.chmod(job_dir, 0o700)
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


def test_do_reserve_contains_the_command_when_the_record_cannot_be_written(
    tmp_path: Path, monkeypatch
):
    """An unattributable command is torn down and its lease kept, not left running."""

    if not _LOCK_OBSERVATION_SUPPORTED:
        pytest.skip("kernel lock table is not observable in this sandbox")
    declaration = make_declaration()
    declaration["reservation"]["host_lock_path"] = str(tmp_path / "host.lock")
    declaration_path = tmp_path / "declaration.json"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    job_dir = tmp_path / "job"
    pidfile = tmp_path / "command.pid"
    code = (
        "import os, time; from pathlib import Path; "
        f"Path({str(pidfile)!r}).write_text(str(os.getpid())); time.sleep(60)"
    )

    def reject(*_args, **_kwargs):
        raise runner.JobOwnershipError("simulated unwritable ownership record")

    monkeypatch.setattr(runner, "_write_job_run_record", reject)
    monkeypatch.setattr(
        runner,
        "prerequisite_checks",
        lambda decl, root: [runner.CheckResult("stub", "ok", 1, 1, "")],
    )
    monkeypatch.setattr(
        runner,
        "evaluate_resources",
        lambda decl, facts, root: [runner.CheckResult("stub", "ok", 1, 1, "")],
    )
    args = argparse.Namespace(job_dir=job_dir, run=[sys.executable, "-c", code], run_timeout=30.0)
    status, message, _extra = runner._do_reserve(
        args, declaration, declaration_path, tmp_path, make_facts()
    )
    assert status == "blocked", message
    assert "could not be recorded" in message
    assert (job_dir / "allocation.json").exists(), "the unattributed lease was released"
    assert (job_dir / runner._JOB_LAUNCH_INTENT_NAME).exists()
    deadline = time.monotonic() + 5
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    if pidfile.exists():
        launched = int(pidfile.read_text(encoding="utf-8"))
        while time.monotonic() < deadline and runner._pid_alive(launched):
            time.sleep(0.05)
        assert not runner._pid_alive(launched), "the unattributed command was left running"


def test_admission_recollects_capacity_after_the_prerequisite_probes(
    tmp_path: Path, monkeypatch, capsys
):
    """Resources are re-measured under the held lease, not reused from before."""

    if not _LOCK_OBSERVATION_SUPPORTED:
        pytest.skip("kernel lock table is not observable in this sandbox")
    phase = {"probed": False, "samples": 0}
    declaration = make_declaration(
        reservation_mechanism="cpuset-affinity",
        affinity_cpus=[0, 1, 2, 3],
        cpu_quota_cores=None,
    )
    declaration["reservation"]["host_lock_path"] = str(tmp_path / "host.lock")
    declaration["reservation"]["job_dir"] = str(tmp_path / "job")
    declaration_path = tmp_path / "declaration.json"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    sentinel = tmp_path / "launched"

    def sample(_root):
        phase["samples"] += 1
        return make_facts(
            affinity_cpus=[0, 1, 2, 3],
            affinity_count=4,
            cpu_quota_cores=None,
            foreign_process_affinity={999999: [0, 1]} if phase["probed"] else {},
        )

    def probes(_declaration, _root):
        phase["probed"] = True
        return [runner.CheckResult("diagnostic-prerequisites", "ok", None, None, "stub")]

    monkeypatch.setattr(runner, "collect_facts", sample)
    monkeypatch.setattr(runner, "prerequisite_checks", probes)
    exit_code = runner.main(
        [
            "--reserve",
            "--json",
            "--declaration",
            str(declaration_path),
            "--run",
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(sentinel)!r}).touch()",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code != 0, payload
    assert phase["samples"] > 1, "capacity was never re-collected after the probes"
    assert not sentinel.exists(), (
        f"admitted a changed allocation: exit={exit_code}, samples={phase['samples']}"
    )


def test_unreadable_ancestor_cpu_quota_is_not_treated_as_unlimited(tmp_path: Path, monkeypatch):
    """An unreadable controller is unknown capacity, not absent capacity."""

    root = tmp_path / "cgroup"
    allocation = root / "allocation"
    allocation.mkdir(parents=True)
    (root / "cpu.max").write_text("50000 100000\n", encoding="utf-8")
    (allocation / "cpu.max").write_text("400000 100000\n", encoding="utf-8")
    real_read_text = runner._read_text

    def hide_ancestor_quota(path):
        return None if path == root / "cpu.max" else real_read_text(path)

    monkeypatch.setattr(runner, "_read_text", hide_ancestor_quota)
    observed = runner._read_cgroup_facts(root, "0::/allocation")
    assert observed["cpu_quota_observable"] is False
    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    for key, value in observed.items():
        setattr(facts, key, value)
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["cpu-quota"] == "unsupported"
    assert runner.overall_status(results) != "ok", (
        "an unobservable half-core ancestor was admitted as four usable cores: "
        + repr(statuses(results))
    )


def test_cgroup_cpu_quota_reports_an_unreadable_level(tmp_path: Path, monkeypatch):
    """The v2 controller walk must publish observability, not just the quota."""

    root = tmp_path / "cgroup"
    leaf = root / "user.slice" / "session.scope"
    leaf.mkdir(parents=True)
    (leaf / "cpu.max").write_text("400000 100000\n", encoding="utf-8")
    paths = runner._iter_cgroup_paths(root, "/user.slice/session.scope")
    real_read_text = runner._read_text

    def unreadable(path: Path) -> str | None:
        if path.name == "cpu.max" and path.parent == leaf:
            return None
        return real_read_text(path)

    monkeypatch.setattr(runner, "_read_text", unreadable)
    quota, observable = runner._cgroup_cpu_quota_v2(paths)
    assert observable is False
    assert quota is None


def test_cgroup_v1_cpu_quota_reports_an_unreadable_period(tmp_path: Path, monkeypatch):
    """A v1 period that cannot be read makes the effective quota unknown."""

    path = tmp_path / "cpu" / "user.slice"
    path.mkdir(parents=True)
    (path / "cpu.cfs_quota_us").write_text("50000\n", encoding="utf-8")
    (path / "cpu.cfs_period_us").write_text("100000\n", encoding="utf-8")
    real_read_text = runner._read_text

    def unreadable(candidate: Path) -> str | None:
        if candidate.name == "cpu.cfs_period_us":
            return None
        return real_read_text(candidate)

    monkeypatch.setattr(runner, "_read_text", unreadable)
    quota, observable = runner._cgroup_cpu_quota_v1([path])
    assert observable is False
    assert quota is None


def test_absent_cgroup_hierarchy_is_unknown_not_unlimited(tmp_path: Path):
    """An unreadable hierarchy must not be admitted as unlimited capacity.

    The round-15 finding: with no cgroupfs root, ``_read_cgroup_facts`` left
    ``cpu_quota_observable`` and ``memory_limit_observable`` at ``True`` even
    though it reported ``cgroup_version=unavailable``.  A valid cpuset
    declaration with no optional CPU weight then admitted with ``overall=ok``.
    """

    unmounted = tmp_path / "sys-fs-cgroup"
    observed = runner._read_cgroup_facts(unmounted, "0::/limited-job\n")
    assert observed["cgroup_version"] == "unavailable"
    assert observed["cpu_quota_observable"] is False
    assert observed["memory_limit_observable"] is False

    declaration, facts = held_reservation(tmp_path, "cpuset-affinity")
    declaration.pop("cpu_weight")
    for key, value in observed.items():
        setattr(facts, key, value)
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["cpu-quota"] == "unsupported"
    assert statuses(results)["memory"] == "unsupported"
    assert runner.overall_status(results) != "ok"


def test_hierarchy_with_an_observed_unlimited_quota_stays_observable(tmp_path: Path):
    """A controller that positively reports ``max`` is observed, not unknown."""

    base = tmp_path / "cgroup"
    leaf = base / "limited-job"
    leaf.mkdir(parents=True)
    (leaf / "cpu.max").write_text("max 100000\n", encoding="utf-8")
    (leaf / "memory.max").write_text("max\n", encoding="utf-8")
    observed = runner._read_cgroup_facts(base, "0::/limited-job\n")
    assert observed["cgroup_version"] == "v2"
    assert observed["cpu_quota_cores"] is None
    assert observed["cpu_quota_observable"] is True
    assert observed["memory_limit_bytes"] is None
    assert observed["memory_limit_observable"] is True


def test_reserve_report_carries_the_admitted_snapshot_and_full_checks(
    tmp_path: Path, monkeypatch, capsys
):
    """A successful report must show the facts admission used, not stale ones.

    The round-15 finding: ``main`` built ``payload.facts`` before the lease was
    held, so a run admitted against affinity 0-3 reported 0-7, ``checks=[]``,
    and no job filesystem measurements.
    """

    if not _LOCK_OBSERVATION_SUPPORTED:
        pytest.skip("kernel lock table is not observable in this sandbox")
    declaration = make_declaration(
        reservation_mechanism="cpuset-affinity",
        affinity_cpus=[0, 1, 2, 3],
        cpu_quota_cores=None,
    )
    declaration["reservation"]["host_lock_path"] = str(tmp_path / "host.lock")
    declaration["reservation"]["job_dir"] = str(tmp_path / "job")
    declaration_path = tmp_path / "declaration.json"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    samples = [
        make_facts(affinity_cpus=list(range(8)), affinity_count=8, cpu_quota_cores=None),
        make_facts(affinity_cpus=[0, 1, 2, 3], affinity_count=4, cpu_quota_cores=None),
    ]
    monkeypatch.setattr(runner, "collect_facts", lambda root: samples.pop(0))
    monkeypatch.setattr(
        runner,
        "prerequisite_checks",
        lambda decl, root: [runner.CheckResult("stub", "ok", None, None, "")],
    )
    exit_code = runner.main(
        [
            "--reserve",
            "--json",
            "--declaration",
            str(declaration_path),
            "--run",
            sys.executable,
            "-c",
            "pass",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0, payload
    assert payload["facts"]["affinity_cpus"] == [0, 1, 2, 3]
    assert payload["facts"]["job_tmp_path"] is not None
    assert payload["facts"]["job_tmp_disk_free_bytes"] is not None
    names = {item["name"] for item in payload["checks"]}
    assert {"affinity", "cpu-quota", "memory"} <= names
    assert payload["initial_facts"]["affinity_cpus"] == list(range(8))


def test_blocked_cli_exit_refuses_a_later_reservation_on_the_same_host_lock(
    tmp_path: Path,
):
    """A blocked CLI exit must not let another job take the same host lock.

    The round-15 finding: the blocked path kept the lease only while the holder
    process lived.  The kernel dropped the flock at exit, so a different job
    directory reserved the identical host lock while a detached descendant was
    still alive.  A host-wide unresolved record must keep exclusion across exit.
    """

    declaration = make_declaration(
        reservation_mechanism="cpuset-affinity",
        affinity_cpus=[0, 1, 2, 3],
        cpu_quota_cores=None,
    )
    job_dir = tmp_path / "blocked-job"
    host_lock = tmp_path / "host.lock"
    declaration["reservation"]["host_lock_path"] = str(host_lock)
    declaration["reservation"]["job_dir"] = str(job_dir)
    declaration_path = tmp_path / "declaration.json"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    pidfile = tmp_path / "detached.pid"
    command = (
        "import subprocess,sys\n"
        "from pathlib import Path\n"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'],"
        "start_new_session=True,stdin=subprocess.DEVNULL,"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        f"Path({str(pidfile)!r}).write_text(str(p.pid))\n"
    )
    program = (
        "from scripts import qualification_runner as r\n"
        "from tests.test_qualification_runner import make_facts\n"
        "r.collect_facts=lambda root: make_facts(affinity_cpus=[0,1,2,3],"
        "affinity_count=4,cpu_quota_cores=None)\n"
        "r.prerequisite_checks=lambda *args: [r.CheckResult('stub-prerequisites',"
        "'ok',None,None,'ROM-free fault injection')]\n"
        # Force the supported subreaper-unavailable failure path; Popen,
        # containment, the lease, main(), and the real process exit are intact.
        "r._child_subreaper_state=lambda: 0\n"
        "r._set_child_subreaper=lambda enabled: False\n"
        "raise SystemExit(r.main("
        f"{['--reserve', '--json', '--declaration', str(declaration_path), '--run', sys.executable, '-c', command]!r}"
        "))\n"
    )
    env = dict(os.environ, PYTHONPATH=f"{REPO_ROOT}:{REPO_ROOT / 'src'}")
    previous_subreaper = runner._child_subreaper_state()
    runner._set_child_subreaper(True)
    detached: int | None = None
    try:
        completed = subprocess.run(
            [sys.executable, "-c", program],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        payload = json.loads(completed.stdout)
        assert completed.returncode == 1, payload
        assert payload["overall"] == "blocked"
        assert (job_dir / "allocation.json").exists(), "the blocked lease was removed"
        assert pidfile.exists()
        detached = int(pidfile.read_text(encoding="utf-8"))
        assert runner._pid_alive(detached), "the detached descendant was not left running"
        record = host_lock.with_name(host_lock.name + runner._UNRESOLVED_ALLOCATION_SUFFIX)
        assert record.exists(), "the blocked exit left no host-wide unresolved record"
        # A different job directory must be refused the same host-wide lock
        # while the unresolved allocation persists, even though the flock the
        # exited holder dropped is free.
        other_job = tmp_path / "other-job"
        with pytest.raises(runner.UnresolvedAllocationError):
            runner._reserve_allocation(
                declaration,
                tmp_path,
                other_job,
                make_facts(affinity_cpus=[0, 1, 2, 3], affinity_count=4, cpu_quota_cores=None),
            )
        assert not (other_job / "allocation.json").exists()
    finally:
        if detached is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(detached, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(detached, 0)
        if previous_subreaper is not None:
            runner._set_child_subreaper(bool(previous_subreaper))
        runner._close_held_lease_descriptors(runner._lock_identity(host_lock))
