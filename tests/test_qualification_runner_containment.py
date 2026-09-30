"""Descendant containment and teardown tests.

Split from ``tests/test_qualification_runner.py`` for issue #112 with no
behavior change: every test below is preserved verbatim.
"""

from __future__ import annotations

import hashlib
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
    _wait_for_dead,
    _write_detached_grandchild_command,
    held_reservation,
    make_declaration,
    required_entries,
    rewrite_descriptor,
    statuses,
    write_input,
)


def test_run_command_sweeps_detached_grandchild_after_success(tmp_path: Path):
    """A descendant that calls ``start_new_session`` must still be contained."""

    pidfile = tmp_path / "detached.pid"
    command = _write_detached_grandchild_command(tmp_path, 0, pidfile)
    process = runner.run_command(command, tmp_path)
    assert process.returncode == 0
    assert pidfile.exists()
    detached = int(pidfile.read_text(encoding="utf-8"))
    assert _wait_for_dead(detached), "the detached grandchild was not contained"
    assert detached not in runner._LEFTOVER_OWNED_PIDS


def test_run_command_preserves_failure_with_detached_grandchild(tmp_path: Path):
    """Detached-descendant cleanup must preserve the original failing status."""

    pidfile = tmp_path / "detached.pid"
    command = _write_detached_grandchild_command(tmp_path, 9, pidfile)
    process = runner.run_command(command, tmp_path)
    assert process.returncode == 9
    assert pidfile.exists()
    detached = int(pidfile.read_text(encoding="utf-8"))
    assert _wait_for_dead(detached)


def test_run_command_timeout_contains_detached_grandchild(tmp_path: Path):
    """The timeout path must contain detached descendants too, not only the group."""

    pidfile = tmp_path / "detached.pid"
    command = _write_detached_grandchild_command(tmp_path, 0, pidfile, parent_sleeps=True)
    process = runner.run_command(command, tmp_path, timeout=0.75)
    assert process.returncode == runner._TIMEOUT_RETURNCODE
    assert pidfile.exists()
    detached = int(pidfile.read_text(encoding="utf-8"))
    assert _wait_for_dead(detached)


def test_run_command_defers_cancellation_until_containment_finishes(tmp_path: Path, monkeypatch):
    """A cancel that lands mid-sweep must not abort the descendant sweep.

    The round-11 finding: the SIGTERM handler raised ``SystemExit`` from inside
    ``_contain_adopted_descendants``, so the process exited 143 with a detached
    descendant still alive.  The handler must record the signal and let the
    bounded cleanup finish before the status is delivered.
    """

    runner._install_signal_handlers()
    pidfile = tmp_path / "detached.pid"
    command = _write_detached_grandchild_command(tmp_path, 0, pidfile, parent_sleeps=True)
    real = runner._contain_adopted_descendants
    state = {"fired": False}

    def fire(exclude=None, **kwargs):
        if not state["fired"]:
            state["fired"] = True
            os.kill(os.getpid(), signal.SIGTERM)
        return real(exclude, **kwargs)

    monkeypatch.setattr(runner, "_contain_adopted_descendants", fire)
    with pytest.raises(SystemExit) as excinfo:
        runner.run_command(command, tmp_path, timeout=0.75)
    assert excinfo.value.code == 128 + signal.SIGTERM
    assert state["fired"]
    assert pidfile.exists()
    detached = int(pidfile.read_text(encoding="utf-8"))
    assert _wait_for_dead(detached), "the deferred cancel interrupted containment"


def test_run_command_retains_output_when_a_descendant_holds_the_stream(tmp_path: Path):
    """Returning 124 with empty output hid the command's own diagnostics.

    A detached descendant that inherits the output pipe keeps it open after the
    command exits, so both ``communicate`` calls can raise ``TimeoutExpired``.
    The round-9 finding replaced the partial output captured by the first
    timeout with empty strings and reported 124 for a command that exited 7.
    """

    runner._LAST_COMMAND_CONTAINMENT = None
    code = (
        "import subprocess, sys\n"
        "subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        "    start_new_session=True,\n"
        ")\n"
        "print('diagnostic: stdout retained')\n"
        "sys.stderr.write('diagnostic: stderr retained\\n')\n"
        "sys.exit(7)\n"
    )
    process = runner.run_command([sys.executable, "-c", code], tmp_path, timeout=2.0)
    assert process.returncode == 7, process
    assert "diagnostic: stdout retained" in process.stdout
    assert "diagnostic: stderr retained" in process.stderr
    assert "kept its output stream open" in process.stderr
    containment = runner.last_command_containment()
    assert containment is not None
    assert containment.proven is True


def test_run_command_publishes_proven_containment(tmp_path: Path):
    """A contained command publishes the outcome its lease depends on."""

    runner._LAST_COMMAND_CONTAINMENT = None
    process = runner.run_command([sys.executable, "-c", "pass"], tmp_path, timeout=30)
    assert process.returncode == 0
    containment = runner.last_command_containment()
    assert containment is not None
    assert containment.proven is True
    assert containment.adoption_active is True
    assert containment.leftovers == ()


def test_run_command_reports_unproven_containment_without_a_subreaper(tmp_path: Path, monkeypatch):
    """Without subreaper adoption a detached survivor cannot be ruled out."""

    monkeypatch.setattr(runner, "_child_subreaper_state", lambda: 0)
    monkeypatch.setattr(runner, "_set_child_subreaper", lambda enabled: False)
    runner._LAST_COMMAND_CONTAINMENT = None
    process = runner.run_command([sys.executable, "-c", "pass"], tmp_path, timeout=30)
    assert process.returncode == 0
    containment = runner.last_command_containment()
    assert containment is not None
    assert containment.proven is False
    assert containment.adoption_active is False
    assert "subreaper" in containment.detail


def test_run_command_does_not_touch_preexisting_children(tmp_path: Path):
    """A child that existed before the command is not the command's descendant."""

    bystander = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        assert bystander.pid in runner._direct_child_pids(os.getpid())
        process = runner.run_command([sys.executable, "-c", "pass"], tmp_path, timeout=30)
        assert process.returncode == 0
        assert bystander.poll() is None, "an unrelated pre-existing child was terminated"
    finally:
        if bystander.poll() is None:
            bystander.kill()
            bystander.wait(timeout=5)


def test_decode_failure_still_contains_detached_descendant(tmp_path: Path):
    """A stream-decoding error must not skip the containment sweeps.

    A reviewer ran a command that wrote a non-UTF-8 byte, exited 7, and left a
    detached child.  ``run_command`` raised ``UnicodeDecodeError`` before either
    sweep, so the child survived and the leftover registry stayed empty.  The
    original error must still surface, but only after containment confirms the
    detached descendant is gone.
    """

    pidfile = tmp_path / "detached.json"
    code = (
        "import json, os, subprocess, sys\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
        "start_new_session=True)\n"
        f"Path({str(pidfile)!r}).write_text(json.dumps({{'pid': child.pid}}))\n"
        "os.write(1, b'\\xff')\n"
        "sys.exit(7)\n"
    )
    child_pid: int | None = None
    try:
        with pytest.raises(UnicodeDecodeError):
            runner.run_command([sys.executable, "-c", code], tmp_path, timeout=10)
        assert pidfile.exists(), "the owned command did not record its detached child"
        child_pid = json.loads(pidfile.read_text())["pid"]
        assert not runner._pid_alive(child_pid), (
            "UnicodeDecodeError bypassed containment; "
            f"detached child {child_pid} is alive and leftovers={runner._LEFTOVER_OWNED_PIDS}"
        )
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        runner._LEFTOVER_OWNED_PIDS.discard(child_pid)


def test_release_contains_holder_descendants(tmp_path: Path, monkeypatch):
    """Release must not report ok while a holder descendant is still alive.

    A reviewer used a SIGTERM-resistant holder that owned a detached sleeper.
    Release escalated to SIGKILL, returned ``ok``, removed the descriptor, and
    left the sleeper alive.  Confirmed cleanup must gate the release, and the
    detached descendant must be gone before the descriptor is removed.
    """

    if not _LOCK_OBSERVATION_SUPPORTED:
        pytest.skip("kernel lock table is not observable in this sandbox")
    declaration, _facts = held_reservation(tmp_path, "cgroup-quota")
    descriptor_path = Path(declaration["reservation"]["descriptor_path"])
    ready = tmp_path / "holder-ready.json"
    holder_code = (
        "import json, os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "detached = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, "
        "start_new_session=True)\n"
        f"Path({str(ready)!r}).write_text(json.dumps({{'detached': detached.pid}}))\n"
        "time.sleep(120)\n"
    )
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    detached_pid: int | None = None
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            # ``write_text`` creates the file before it has written the payload,
            # so a loaded host can briefly expose an empty handshake file.
            try:
                detached_pid = json.loads(ready.read_text(encoding="utf-8"))["detached"]
            except (OSError, ValueError):
                time.sleep(0.02)
                continue
            break
        assert detached_pid is not None, "the holder did not start its detached descendant"
        rewrite_descriptor(
            declaration,
            {
                "holder_pid": holder.pid,
                "holder_start_time": runner._process_start_time(holder.pid),
            },
        )
        monkeypatch.setattr(runner, "_holder_is_owned", lambda holder, start: True)
        status, message = runner._release_allocation(declaration, tmp_path)
        assert status == "ok", message
        assert not runner._pid_alive(holder.pid), "the holder survived release"
        assert not runner._pid_alive(detached_pid), (
            f"release reported {status} while its detached descendant "
            f"{detached_pid} was still alive"
        )
        assert not descriptor_path.exists(), "the released descriptor was retained"
    finally:
        runner._LEFTOVER_OWNED_PIDS.discard(detached_pid)
        runner._LEFTOVER_OWNED_PIDS.discard(holder.pid)
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=5)
        if detached_pid is not None:
            try:
                os.kill(detached_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_release_is_blocked_while_owned_leftovers_survive(tmp_path: Path):
    if not _LOCK_OBSERVATION_SUPPORTED:
        pytest.skip("kernel lock table is not observable in this sandbox")
    declaration, _facts = held_reservation(tmp_path, "cgroup-quota")
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True
    )
    try:
        runner._LEFTOVER_OWNED_PIDS.add(sleeper.pid)
        real_pid_alive = runner._pid_alive
        real_terminate = runner._terminate_process_group
        try:
            # Simulate a descendant that cannot be confirmed gone.
            runner._pid_alive = lambda pid: True
            runner._terminate_process_group = lambda pgid, grace=None: (False, [sleeper.pid])
            status, message = runner._release_allocation(declaration, tmp_path)
        finally:
            runner._pid_alive = real_pid_alive
            runner._terminate_process_group = real_terminate
        assert status == "blocked"
        assert "still running" in message
    finally:
        runner._LEFTOVER_OWNED_PIDS.discard(sleeper.pid)
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=5)


def test_collect_facts_measures_job_filesystems(tmp_path: Path):
    """Disk admission must target the job's tmp/evidence, not the caller TMPDIR."""

    job_dir = tmp_path / "job"
    facts = runner.collect_facts(tmp_path, temp_root=tmp_path)
    runner._populate_job_filesystem_facts(facts, job_dir)
    assert facts.job_tmp_path == str(job_dir / "tmp")
    assert facts.job_evidence_path == str(job_dir / "evidence")
    assert facts.job_tmp_disk_free_bytes is not None
    assert facts.job_evidence_disk_free_bytes is not None
    assert (job_dir / "tmp").is_dir()
    assert (job_dir / "evidence").is_dir()


def test_evaluate_resources_fails_on_job_evidence_filesystem(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    runner._populate_job_filesystem_facts(facts, Path(declaration["reservation"]["job_dir"]))
    facts.job_evidence_disk_free_bytes = 1
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["disk-free-job-evidence"] == "fail"


def test_evaluate_resources_fails_on_job_tmp_filesystem(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    runner._populate_job_filesystem_facts(facts, Path(declaration["reservation"]["job_dir"]))
    facts.job_tmp_disk_free_bytes = 1
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["disk-free-temp"] == "fail"


def test_cpuset_reservation_rejects_competing_foreign_affinity(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cpuset-affinity")
    facts.foreign_process_affinity = {999999: [0, 1]}
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["reservation-evidence"] == "fail"


def test_cpuset_reservation_unsupported_when_affinity_unobservable(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cpuset-affinity")
    facts.foreign_process_affinity = None
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["reservation-evidence"] == "unsupported"


def test_foreign_process_affinity_fails_closed_when_process_unreadable(monkeypatch):
    """An unreadable process must not be silently dropped from the competitor set."""

    real_observed = runner._observed_affinity
    real_zombie = runner._pid_is_zombie
    real_entries = list(Path("/proc").iterdir())

    class _Entry:
        def __init__(self, name: str):
            self.name = name

    monkeypatch.setattr(
        runner.Path,
        "iterdir",
        lambda self: real_entries if str(self) != "/proc" else [_Entry("1"), _Entry("self")],
    )
    monkeypatch.setattr(runner, "_pid_is_zombie", lambda pid: False)
    monkeypatch.setattr(runner, "_observed_affinity", lambda pid: None)
    monkeypatch.setattr(runner, "_pid_exists", lambda pid: True)
    try:
        assert runner._foreign_process_affinity() is None
    finally:
        monkeypatch.setattr(runner, "_observed_affinity", real_observed)
        monkeypatch.setattr(runner, "_pid_is_zombie", real_zombie)


def test_foreign_process_affinity_skips_exited_process(monkeypatch):
    """A process that exited between enumeration and read is not a competitor."""

    entries = list(Path("/proc").iterdir())

    class _Entry:
        def __init__(self, name: str):
            self.name = name

    monkeypatch.setattr(
        runner.Path,
        "iterdir",
        lambda self: entries if str(self) != "/proc" else [_Entry("999999")],
    )
    monkeypatch.setattr(runner, "_observed_affinity", lambda pid: None)
    monkeypatch.setattr(runner, "_pid_exists", lambda pid: False)
    assert runner._foreign_process_affinity() == {}


def test_cgroup_sibling_competitors_detects_populated_descendant(tmp_path: Path):
    """A sibling whose own process list is empty but whose child is busy counts."""

    root = tmp_path / "cgroup"
    allocation = root / "user.slice" / "session.scope"
    sibling_leaf = root / "user.slice" / "other.scope" / "child.scope"
    allocation.mkdir(parents=True)
    sibling_leaf.mkdir(parents=True)
    for directory in (root, root / "user.slice", root / "user.slice" / "other.scope"):
        (directory / "cgroup.controllers").write_text("cpu\n", encoding="utf-8")
        (directory / "cgroup.procs").write_text("", encoding="utf-8")
    (allocation / "cgroup.controllers").write_text("cpu\n", encoding="utf-8")
    (allocation / "cgroup.procs").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (sibling_leaf / "cgroup.controllers").write_text("cpu\n", encoding="utf-8")
    (sibling_leaf / "cgroup.procs").write_text("4242\n", encoding="utf-8")

    competitors = runner._cgroup_sibling_competitors(str(allocation))
    assert competitors == ["other.scope"]


def test_cgroup_sibling_competitors_detects_ancestor_sibling(tmp_path: Path):
    """A busy cgroup beside an ancestor also competes for the same CPUs."""

    root = tmp_path / "cgroup"
    allocation = root / "user.slice" / "session.scope"
    outer = root / "system.slice"
    allocation.mkdir(parents=True)
    outer.mkdir(parents=True)
    for directory in (root, root / "user.slice", outer):
        (directory / "cgroup.controllers").write_text("cpu\n", encoding="utf-8")
        (directory / "cgroup.procs").write_text("", encoding="utf-8")
    (allocation / "cgroup.controllers").write_text("cpu\n", encoding="utf-8")
    (allocation / "cgroup.procs").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (outer / "cgroup.procs").write_text("31337\n", encoding="utf-8")

    competitors = runner._cgroup_sibling_competitors(str(allocation))
    assert competitors == ["system.slice"]


def test_cgroup_sibling_competitors_accepts_quiet_tree(tmp_path: Path):
    root = tmp_path / "cgroup"
    allocation = root / "user.slice" / "session.scope"
    allocation.mkdir(parents=True)
    for directory in (root, root / "user.slice", allocation):
        (directory / "cgroup.controllers").write_text("cpu\n", encoding="utf-8")
        (directory / "cgroup.procs").write_text("", encoding="utf-8")
    (allocation / "cgroup.procs").write_text(f"{os.getpid()}\n", encoding="utf-8")
    assert runner._cgroup_sibling_competitors(str(allocation)) == []


def test_cgroup_sibling_competitors_unsupported_when_unreadable(tmp_path: Path, monkeypatch):
    root = tmp_path / "cgroup"
    allocation = root / "user.slice" / "session.scope"
    allocation.mkdir(parents=True)
    (root / "cgroup.controllers").write_text("cpu\n", encoding="utf-8")
    (root / "cgroup.procs").write_text("", encoding="utf-8")
    (root / "user.slice" / "cgroup.controllers").write_text("cpu\n", encoding="utf-8")
    (root / "user.slice" / "cgroup.procs").write_text("", encoding="utf-8")
    (allocation / "cgroup.controllers").write_text("cpu\n", encoding="utf-8")
    (allocation / "cgroup.procs").write_text("", encoding="utf-8")

    real_iterdir = Path.iterdir

    def _iterdir(self):
        if self == root:
            raise PermissionError("hidden")
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", _iterdir)
    assert runner._cgroup_sibling_competitors(str(allocation)) is None


def test_validate_asset_inputs_rejects_unrelated_only(tmp_path: Path):
    rom_root = tmp_path / "rom"
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    entry = write_input(rom_root, "unrelated.txt", b"not a ROM or symbol")
    declaration = make_declaration(
        assets={
            "rom_root": str(rom_root),
            "fixture_root": str(fixture_root),
            "inputs": [entry],
        }
    )
    results = runner.validate_asset_inputs(declaration, REPO_ROOT)
    assert runner.overall_status(results) != "ok"
    assert statuses(results)["assets-input-0"] == "fail"
    assert any(
        name.startswith("assets-required-rom_root-") and status == "fail"
        for name, status in statuses(results).items()
    )


def test_validate_asset_inputs_requires_complete_pinned_set(tmp_path: Path, monkeypatch):
    data = b"rom-bytes"
    rom_root = tmp_path / "rom"
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    entry = write_input(rom_root, "red/pokemon-red.gb", data)
    requirements = required_entries(
        [
            ("rom_root", "red/pokemon-red.gb", hashlib.sha1(data).hexdigest(), data),
            ("rom_root", "blue/pokemon-blue.gb", hashlib.sha1(b"blue").hexdigest(), b"blue"),
        ]
    )
    monkeypatch.setattr(runner, "_required_asset_entries", lambda decl, root: requirements)
    declaration = make_declaration(
        assets={
            "rom_root": str(rom_root),
            "fixture_root": str(fixture_root),
            "inputs": [entry],
        }
    )
    results = runner.validate_asset_inputs(declaration, tmp_path)
    assert runner.overall_status(results) == "fail"
    assert any(
        name.startswith("assets-required-rom_root-") and status == "fail"
        for name, status in statuses(results).items()
    )


def test_validate_asset_inputs_verifies_complete_set(tmp_path: Path, monkeypatch):
    data = b"rom-bytes"
    rom_root = tmp_path / "rom"
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    entry = write_input(rom_root, "red/pokemon-red.gb", data)
    requirements = required_entries(
        [("rom_root", "red/pokemon-red.gb", hashlib.sha1(data).hexdigest(), data)]
    )
    monkeypatch.setattr(runner, "_required_asset_entries", lambda decl, root: requirements)
    declaration = make_declaration(
        assets={
            "rom_root": str(rom_root),
            "fixture_root": str(fixture_root),
            "inputs": [entry],
        }
    )
    results = runner.validate_asset_inputs(declaration, tmp_path)
    assert runner.overall_status(results) == "ok"
