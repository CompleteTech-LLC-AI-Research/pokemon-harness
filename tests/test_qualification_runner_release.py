"""CLI, recovery and release-path tests.

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
    REPO_ROOT,
    make_declaration,
    make_facts,
)


def test_unreadable_process_table_is_unproven_containment(tmp_path: Path, monkeypatch):
    """An unreadable ``/proc`` is unknown visibility, not an empty process table.

    Round-17 finding: enumeration errors returned empty lists, so a sweep that
    could not see the process table reported a live detached child as contained.
    """

    pidfile = tmp_path / "detached.pid"
    command = (
        "import subprocess,sys\nfrom pathlib import Path\n"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'],"
        "start_new_session=True,stdin=subprocess.DEVNULL,"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        f"Path({str(pidfile)!r}).write_text(str(p.pid))\n"
    )
    original_iterdir = Path.iterdir

    def unreadable_proc(path: Path):
        if path == Path("/proc"):
            raise PermissionError("injected unavailable process enumeration")
        return original_iterdir(path)

    detached: int | None = None
    try:
        result = runner.run_command(
            [sys.executable, "-c", command],
            tmp_path,
            timeout=10,
            on_start=lambda process: monkeypatch.setattr(Path, "iterdir", unreadable_proc),
        )
        assert result.returncode == 0
        assert pidfile.exists()
        detached = int(pidfile.read_text(encoding="utf-8"))
        assert runner._pid_alive(detached), "the negative-control child exited"
        containment = runner.last_command_containment()
        assert containment is not None
        assert not containment.proven, (
            f"a sweep that could not enumerate processes claimed proof: {containment.detail!r}"
        )
    finally:
        monkeypatch.setattr(Path, "iterdir", original_iterdir)
        if detached is None and pidfile.exists():
            detached = int(pidfile.read_text(encoding="utf-8"))
        if detached is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(detached, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(detached, 0)


def test_unreadable_live_process_stat_is_unproven_containment(tmp_path: Path, monkeypatch):
    """A live process whose stat cannot be read is unknown, not absent.

    Round-18 finding: ``_direct_child_pids``/``_process_group_members`` skipped
    any error reading ``/proc/<pid>/stat``, so a detached descendant whose stat
    record was unreadable vanished from the sweep and a live child was reported
    as contained.
    """

    pidfile = tmp_path / "detached.pid"
    command = (
        "import subprocess,sys\nfrom pathlib import Path\n"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'],"
        "start_new_session=True,stdin=subprocess.DEVNULL,"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        f"Path({str(pidfile)!r}).write_text(str(p.pid))\n"
    )
    original_read_text = Path.read_text

    def deny_live_child_stat(path: Path, *args, **kwargs):
        if (
            path.name == "stat"
            and path.parent.parent == Path("/proc")
            and pidfile.exists()
            and path.parent.name == original_read_text(pidfile, *args, **kwargs).strip()
        ):
            raise PermissionError("injected unreadable live descendant stat")
        return original_read_text(path, *args, **kwargs)

    detached: int | None = None
    try:
        result = runner.run_command(
            [sys.executable, "-c", command],
            tmp_path,
            timeout=10,
            on_start=lambda process: monkeypatch.setattr(Path, "read_text", deny_live_child_stat),
        )
        assert result.returncode == 0
        assert pidfile.exists()
        detached = int(pidfile.read_text(encoding="utf-8"))
        assert runner._pid_alive(detached), "the negative-control child exited"
        containment = runner.last_command_containment()
        assert containment is not None
        assert not containment.proven, (
            f"an unreadable live descendant was reported as contained: {containment.detail!r}"
        )
    finally:
        monkeypatch.setattr(Path, "read_text", original_read_text)
        if detached is None and pidfile.exists():
            detached = int(pidfile.read_text(encoding="utf-8"))
        if detached is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(detached, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(detached, 0)


def test_unreadable_process_group_member_stat_is_unproven_containment(tmp_path: Path, monkeypatch):
    """A live member of the command's group with an unreadable stat is unknown.

    Round-18 finding: ``_process_group_members`` skipped unreadable ``/proc``
    stat records, so a surviving same-group member disappeared from the group
    sweep and ``_terminate_process_group`` reported the group empty without ever
    signalling it.
    """

    pidfile = tmp_path / "group-member.pid"
    command = (
        "import subprocess,sys,time\nfrom pathlib import Path\n"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'])\n"
        f"Path({str(pidfile)!r}).write_text(str(p.pid))\n"
        "time.sleep(120)\n"
    )
    original_read_text = Path.read_text

    def deny_live_member_stat(path: Path, *args, **kwargs):
        if (
            path.name == "stat"
            and path.parent.parent == Path("/proc")
            and pidfile.exists()
            and path.parent.name == original_read_text(pidfile, *args, **kwargs).strip()
        ):
            raise PermissionError("injected unreadable live group member stat")
        return original_read_text(path, *args, **kwargs)

    member: int | None = None
    try:
        result = runner.run_command(
            [sys.executable, "-c", command],
            tmp_path,
            timeout=10,
            on_start=lambda process: monkeypatch.setattr(Path, "read_text", deny_live_member_stat),
        )
        assert result.returncode == runner._TIMEOUT_RETURNCODE
        assert pidfile.exists()
        member = int(pidfile.read_text(encoding="utf-8"))
        assert runner._pid_alive(member), "the negative-control group member exited"
        containment = runner.last_command_containment()
        assert containment is not None
        assert not containment.proven, (
            f"an unreadable live group member was reported as contained: {containment.detail!r}"
        )
    finally:
        monkeypatch.setattr(Path, "read_text", original_read_text)
        if member is None and pidfile.exists():
            member = int(pidfile.read_text(encoding="utf-8"))
        if member is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(member, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(member, 0)


def test_unwritable_host_lock_directory_refuses_to_launch(tmp_path: Path, monkeypatch):
    """A lock that cannot carry the durable exclusion must never launch work.

    Round-18 finding: the pre-spawn record write was best-effort, so a writable
    lock file inside an unwritable directory permitted launching a command with
    no host-wide exclusion; after the holder died, a second lease admitted over
    the still-live descendant.
    """

    lock_dir = tmp_path / "operator-owned-locks"
    lock_dir.mkdir()
    lock = lock_dir / "host.lock"
    lock.touch(mode=0o600)
    lock_dir.chmod(0o555)
    declaration = make_declaration(
        reservation_mechanism="cpuset-affinity",
        affinity_cpus=[0, 1, 2, 3],
        cpu_quota_cores=None,
    )
    declaration["reservation"]["host_lock_path"] = str(lock)
    declaration["reservation"]["job_dir"] = str(tmp_path / "job")
    declaration_path = tmp_path / "declaration.json"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    marker = tmp_path / "command-launched"
    facts = make_facts(affinity_cpus=[0, 1, 2, 3], affinity_count=4, cpu_quota_cores=None)
    monkeypatch.setattr(runner, "collect_facts", lambda root: facts)
    monkeypatch.setattr(
        runner,
        "prerequisite_checks",
        lambda *args: [
            runner.CheckResult("stub-prerequisites", "ok", None, None, "ROM-free fault injection")
        ],
    )
    try:
        status, message, _extra = runner._do_reserve(
            argparse.Namespace(
                job_dir=tmp_path / "job",
                run=[
                    sys.executable,
                    "-c",
                    f"from pathlib import Path; Path({str(marker)!r}).touch()",
                ],
                run_timeout=10.0,
            ),
            declaration,
            declaration_path,
            tmp_path,
            facts,
        )
        assert not marker.exists(), "launched without a durable host exclusion"
        assert status != "ok", message
        assert not runner._unresolved_allocation_path(lock).exists()
    finally:
        lock_dir.chmod(0o755)
        runner._close_held_lease_descriptors(runner._lock_identity(lock))


def test_abrupt_holder_death_keeps_host_exclusion(tmp_path: Path):
    """SIGKILL of the holder must not free a host lease with live work.

    Round-17 finding: the host-wide record was written only on handled failure
    paths, so an abrupt signal dropped the flock with no record and a competing
    job reserved the same lock while a detached descendant was still alive.
    """

    declaration = make_declaration(
        reservation_mechanism="cpuset-affinity",
        affinity_cpus=[0, 1, 2, 3],
        cpu_quota_cores=None,
    )
    job_dir = tmp_path / "job"
    host_lock = tmp_path / "host.lock"
    declaration["reservation"]["host_lock_path"] = str(host_lock)
    declaration["reservation"]["job_dir"] = str(job_dir)
    declaration_path = tmp_path / "declaration.json"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    pidfile = tmp_path / "detached.pid"
    command = (
        "import subprocess,sys,time\nfrom pathlib import Path\n"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'],"
        "start_new_session=True,stdin=subprocess.DEVNULL,"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        f"Path({str(pidfile)!r}).write_text(str(p.pid))\n"
        "time.sleep(120)\n"
    )
    program = (
        "from scripts import qualification_runner as r\n"
        "from tests.test_qualification_runner import make_facts\n"
        "r.collect_facts=lambda root: make_facts(affinity_cpus=[0,1,2,3],"
        "affinity_count=4,cpu_quota_cores=None)\n"
        "r.prerequisite_checks=lambda *args: [r.CheckResult('stub-prerequisites',"
        "'ok',None,None,'ROM-free fault injection')]\n"
        "raise SystemExit(r.main("
        f"{['--reserve', '--json', '--declaration', str(declaration_path), '--run', sys.executable, '-c', command]!r}"
        "))\n"
    )
    env = dict(os.environ, PYTHONPATH=f"{REPO_ROOT}:{REPO_ROOT / 'src'}")
    previous_subreaper = runner._child_subreaper_state()
    runner._set_child_subreaper(True)
    holder: subprocess.Popen[str] | None = None
    detached: int | None = None
    job_pid: int | None = None
    try:
        holder = subprocess.Popen(
            [sys.executable, "-c", program],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + 30
        while not pidfile.exists():
            if holder.poll() is not None or time.monotonic() >= deadline:
                raise AssertionError("the leased command never launched")
            time.sleep(0.02)
        detached = int(pidfile.read_text(encoding="utf-8"))
        record = json.loads((job_dir / runner._JOB_RUN_RECORD_NAME).read_text(encoding="utf-8"))
        job_pid = record["job_pid"]
        holder.kill()
        holder.communicate(timeout=30)
        assert holder.returncode == -signal.SIGKILL
        assert runner._pid_alive(detached), "the detached descendant exited unexpectedly"
        with pytest.raises(runner.UnresolvedAllocationError):
            runner._reserve_allocation(
                declaration,
                tmp_path,
                tmp_path / "second-job",
                make_facts(affinity_cpus=[0, 1, 2, 3], affinity_count=4, cpu_quota_cores=None),
            )
    finally:
        if holder is not None and holder.poll() is None:
            holder.kill()
            holder.communicate(timeout=30)
        if detached is None and pidfile.exists():
            detached = int(pidfile.read_text(encoding="utf-8"))
        for pid in (detached, job_pid):
            if pid is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
                with contextlib.suppress(ChildProcessError):
                    os.waitpid(pid, 0)
        if previous_subreaper is not None:
            runner._set_child_subreaper(bool(previous_subreaper))
        runner._close_held_lease_descriptors(runner._lock_identity(host_lock))


def test_json_report_keeps_child_output_sanitized(tmp_path: Path, monkeypatch, capsys):
    """A ``--json`` report must stay one sanitized document with child output.

    Round-17 finding: the child's stdout was echoed before the JSON document and
    never sanitized, so a raw absolute path corrupted and leaked from the report.
    """

    declaration = make_declaration(
        reservation_mechanism="cpuset-affinity",
        affinity_cpus=[0, 1, 2, 3],
        cpu_quota_cores=None,
    )
    job_dir = tmp_path / "job"
    host_lock = tmp_path / "host.lock"
    declaration["reservation"]["host_lock_path"] = str(host_lock)
    declaration["reservation"]["job_dir"] = str(job_dir)
    declaration_path = tmp_path / "declaration.json"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    monkeypatch.setattr(
        runner,
        "collect_facts",
        lambda root: make_facts(affinity_cpus=[0, 1, 2, 3], affinity_count=4, cpu_quota_cores=None),
    )
    monkeypatch.setattr(
        runner,
        "prerequisite_checks",
        lambda *args: [
            runner.CheckResult("stub-prerequisites", "ok", None, None, "ROM-free fault injection")
        ],
    )
    try:
        status = runner.main(
            [
                "--reserve",
                "--json",
                "--declaration",
                str(declaration_path),
                "--run",
                sys.executable,
                "-c",
                "print('child output /private/operator/rom.gb')",
            ]
        )
        stdout = capsys.readouterr().out
        assert status == 0, stdout
        assert "/private/operator/rom.gb" not in stdout
        payload = json.loads(stdout)
        assert payload["overall"] == "ok"
        assert "child output" in payload["command_stdout"]
        assert "<redacted-path>" in payload["command_stdout"]
    finally:
        runner._close_held_lease_descriptors(runner._lock_identity(host_lock))


def test_clear_unresolved_allocation_only_removes_the_records_own_lease(tmp_path: Path):
    """A release must not erase a *different* lease's host-wide exclusion.

    Round-16 finding: the marker is host-wide, so a release can race a competing
    reservation that admitted after the holder dropped its flock and recorded
    its own exclusion in that window.  Clearing by pathname alone deleted the
    other lease's record and admitted new work over live, uncontained use.
    """

    host_lock = tmp_path / "host.lock"
    own_descriptor = tmp_path / "own-job" / "allocation.json"
    other_descriptor = tmp_path / "other-job" / "allocation.json"
    record_path = runner._write_unresolved_allocation(
        host_lock,
        reason="competing job still has uncontained work",
        descriptor_path=other_descriptor,
    )
    assert record_path is not None and record_path.exists()

    # Without a verifiable descriptor there is no lease identity to match.
    runner._clear_unresolved_allocation(host_lock)
    assert record_path.exists(), "a descriptor-less clear erased the record"

    # A release of an unrelated lease must leave the record in force.
    runner._clear_unresolved_allocation(host_lock, descriptor_path=own_descriptor)
    assert record_path.exists(), "the record of another lease was erased"

    # Only the record's own lease may clear it.
    runner._clear_unresolved_allocation(host_lock, descriptor_path=other_descriptor)
    assert not record_path.exists(), "the record's own lease could not clear it"


def test_release_preserves_another_leases_unresolved_record(tmp_path: Path, monkeypatch):
    """A successful release that races a competitor keeps the competitor's block.

    This reproduces the round-16 interleaving deterministically: while the first
    release is removing its private state (after dropping its flock), a
    competing job admits and persists its own unresolved record.  The first
    release must still report success without deleting that other record, and a
    third reservation must then be refused.
    """

    declaration = make_declaration(
        reservation_mechanism="cpuset-affinity",
        affinity_cpus=[0, 1, 2, 3],
        cpu_quota_cores=None,
    )
    job_dir = tmp_path / "job"
    host_lock = tmp_path / "host.lock"
    declaration["reservation"]["host_lock_path"] = str(host_lock)
    declaration["reservation"]["job_dir"] = str(job_dir)
    declaration_path = tmp_path / "declaration.json"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    facts = make_facts(affinity_cpus=[0, 1, 2, 3], affinity_count=4, cpu_quota_cores=None)
    descriptor_path, _descriptor, _lock_fd = runner._reserve_allocation(
        declaration, tmp_path, job_dir, facts
    )
    runner._pin_declaration(declaration_path, declaration, job_dir, descriptor_path)

    other_descriptor = tmp_path / "other-job" / "allocation.json"
    original_remove = runner._remove_allocation_state

    def interleave(path: Path) -> None:
        # The first runner has already dropped its flock at this point, which is
        # exactly when a competing job can admit and record its own exclusion.
        runner._write_unresolved_allocation(
            host_lock,
            reason="simulated competing job with uncontained work",
            descriptor_path=other_descriptor,
        )
        original_remove(path)

    monkeypatch.setattr(runner, "_remove_allocation_state", interleave)
    try:
        status, message = runner._release_allocation(declaration, tmp_path)
    finally:
        runner._close_held_lease_descriptors(runner._lock_identity(host_lock))

    assert status == "ok", message
    record = runner._unresolved_allocation_path(host_lock)
    assert record.exists(), "the release erased another lease's unresolved record"
    with pytest.raises(runner.UnresolvedAllocationError):
        runner._reserve_allocation(declaration, tmp_path, tmp_path / "third-job", facts)


def test_failed_command_spawn_releases_the_unused_lease(tmp_path: Path, monkeypatch):
    """A launch that never created a process must not strand the lease.

    Round-16 finding: a nonexistent executable reported exit 127, but the
    recorded launch intent remained, so the release refused and left the lock
    held and a blocking record behind even though no process ever existed.
    """

    declaration = make_declaration(
        reservation_mechanism="cpuset-affinity",
        affinity_cpus=[0, 1, 2, 3],
        cpu_quota_cores=None,
    )
    job_dir = tmp_path / "job"
    host_lock = tmp_path / "host.lock"
    declaration["reservation"]["host_lock_path"] = str(host_lock)
    declaration["reservation"]["job_dir"] = str(job_dir)
    declaration_path = tmp_path / "declaration.json"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    monkeypatch.setattr(
        runner,
        "collect_facts",
        lambda root: make_facts(affinity_cpus=[0, 1, 2, 3], affinity_count=4, cpu_quota_cores=None),
    )
    monkeypatch.setattr(
        runner,
        "prerequisite_checks",
        lambda *args: [
            runner.CheckResult("stub-prerequisites", "ok", None, None, "ROM-free fault injection")
        ],
    )
    args = argparse.Namespace(
        job_dir=job_dir, run=[str(tmp_path / "no-such-command")], run_timeout=5.0
    )
    try:
        status, message, _ = runner._do_reserve(
            args, declaration, declaration_path, tmp_path, make_facts()
        )
    finally:
        runner._close_held_lease_descriptors(runner._lock_identity(host_lock))

    assert status == "fail", message
    assert "127" in message
    assert not (job_dir / runner._JOB_RUN_RECORD_NAME).exists()
    assert not (job_dir / runner._JOB_LAUNCH_INTENT_NAME).exists()
    assert not runner._unresolved_allocation_path(host_lock).exists()
    assert runner._observe_flock_holders(host_lock) == set()


def test_cancellation_records_durable_exclusion_before_exit(tmp_path: Path):
    """SIGTERM with an unavailable subreaper must still persist the exclusion.

    Round-16 finding: a cancel raised ``SystemExit`` straight out of the run,
    dropping the flock without writing the host-wide record, so another job
    reserved the same lock while a detached descendant was still alive.
    """

    declaration = make_declaration(
        reservation_mechanism="cpuset-affinity",
        affinity_cpus=[0, 1, 2, 3],
        cpu_quota_cores=None,
    )
    job_dir = tmp_path / "cancel-job"
    host_lock = tmp_path / "host.lock"
    declaration["reservation"]["host_lock_path"] = str(host_lock)
    declaration["reservation"]["job_dir"] = str(job_dir)
    declaration_path = tmp_path / "declaration.json"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    pidfile = tmp_path / "detached.pid"
    command = (
        "import subprocess,sys,time\n"
        "from pathlib import Path\n"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)'],"
        "start_new_session=True,stdin=subprocess.DEVNULL,"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        f"Path({str(pidfile)!r}).write_text(str(p.pid))\n"
        "time.sleep(120)\n"
    )
    program = (
        "from scripts import qualification_runner as r\n"
        "from tests.test_qualification_runner import make_facts\n"
        "r.collect_facts=lambda root: make_facts(affinity_cpus=[0,1,2,3],"
        "affinity_count=4,cpu_quota_cores=None)\n"
        "r.prerequisite_checks=lambda *args: [r.CheckResult('stub-prerequisites',"
        "'ok',None,None,'ROM-free fault injection')]\n"
        "r._child_subreaper_state=lambda: 0\n"
        "r._set_child_subreaper=lambda enabled: False\n"
        "raise SystemExit(r.main("
        f"{['--reserve', '--json', '--declaration', str(declaration_path), '--run', sys.executable, '-c', command]!r}"
        "))\n"
    )
    env = dict(os.environ, PYTHONPATH=f"{REPO_ROOT}:{REPO_ROOT / 'src'}")
    previous_subreaper = runner._child_subreaper_state()
    runner._set_child_subreaper(True)
    child: subprocess.Popen[str] | None = None
    detached: int | None = None
    try:
        child = subprocess.Popen(
            [sys.executable, "-c", program],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + 30
        while not pidfile.exists():
            if child.poll() is not None or time.monotonic() >= deadline:
                raise AssertionError("the leased command never launched")
            time.sleep(0.02)
        detached = int(pidfile.read_text(encoding="utf-8"))
        child.send_signal(signal.SIGTERM)
        child.communicate(timeout=60)
        assert child.returncode == 143, child.returncode
        assert runner._pid_alive(detached), "the detached descendant was not left running"
        record = runner._unresolved_allocation_path(host_lock)
        assert record.exists(), "the cancelled run left no host-wide unresolved record"
        with pytest.raises(runner.UnresolvedAllocationError):
            runner._reserve_allocation(
                declaration,
                tmp_path,
                tmp_path / "second-job",
                make_facts(affinity_cpus=[0, 1, 2, 3], affinity_count=4, cpu_quota_cores=None),
            )
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        if detached is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(detached, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(detached, 0)
        if previous_subreaper is not None:
            runner._set_child_subreaper(bool(previous_subreaper))
        runner._close_held_lease_descriptors(runner._lock_identity(host_lock))
