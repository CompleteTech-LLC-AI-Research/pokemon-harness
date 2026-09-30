"""Lease-lock identity and recovery tests.

Split from ``tests/test_qualification_runner.py`` for issue #112 with no
behavior change: every test below is preserved verbatim.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import qualification_runner as runner
from tests._qualification_runner_support import (
    _HELD_FDS,
    held_reservation,
    make_declaration,
    make_facts,
    required_entries,
    rewrite_descriptor,
    statuses,
    write_input,
)


def test_validate_asset_inputs_blocks_hash_mismatch(tmp_path: Path, monkeypatch):
    rom_root = tmp_path / "rom"
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    entry = write_input(rom_root, "red/pokemon-red.gb", b"content")
    entry["sha1"] = "0" * 40
    requirements = required_entries([("rom_root", "red/pokemon-red.gb", "0" * 40, b"content")])
    monkeypatch.setattr(runner, "_required_asset_entries", lambda decl, root: requirements)
    declaration = make_declaration(
        assets={
            "rom_root": str(rom_root),
            "fixture_root": str(fixture_root),
            "inputs": [entry],
        }
    )
    results = runner.validate_asset_inputs(declaration, tmp_path)
    assert statuses(results)["assets-required-rom_root-0"] == "fail"


def test_validate_asset_inputs_rejects_unknown_declared_input(tmp_path: Path, monkeypatch):
    data = b"rom-bytes"
    rom_root = tmp_path / "rom"
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    write_input(rom_root, "red/pokemon-red.gb", data)
    unknown = write_input(rom_root, "unrelated.txt", b"other")
    requirements = required_entries(
        [("rom_root", "red/pokemon-red.gb", hashlib.sha1(data).hexdigest(), data)]
    )
    monkeypatch.setattr(runner, "_required_asset_entries", lambda decl, root: requirements)
    declaration = make_declaration(
        assets={
            "rom_root": str(rom_root),
            "fixture_root": str(fixture_root),
            "inputs": [unknown],
        }
    )
    results = runner.validate_asset_inputs(declaration, tmp_path)
    assert statuses(results)["assets-input-0"] == "fail"


def test_immutable_asset_check_rejects_writable_root(tmp_path: Path):
    rom_root = tmp_path / "rom"
    rom_root.mkdir()
    declaration = make_declaration(
        assets={"rom_root": str(rom_root), "fixture_root": str(tmp_path / "fixtures")}
    )
    results = runner._immutable_asset_checks(declaration, tmp_path)
    assert statuses(results)["assets-immutable-rom_root"] == "fail"


def test_immutable_asset_check_rejects_writable_nested_directory(tmp_path: Path):
    """A read-only root with a writable nested directory is not immutable.

    A reviewer kept the root at mode 0555 and the asset file at 0444, but made
    the containing subdirectory writable; the check reported ``ok`` and the
    reviewer then deleted and replaced the protected file through that parent.
    The writable nested directory must fail the admission.
    """

    rom_root = tmp_path / "rom"
    nested = rom_root / "red"
    nested.mkdir(parents=True)
    (nested / "pokemon-red.gb").write_bytes(b"dummy")
    for path in (nested / "pokemon-red.gb", nested, rom_root):
        path.chmod(0o555 if path.is_dir() else 0o444)
    nested.chmod(0o755)
    declaration = make_declaration(
        assets={"rom_root": str(rom_root), "fixture_root": str(tmp_path / "fixtures")}
    )
    try:
        results = runner._immutable_asset_checks(declaration, tmp_path)
        assert statuses(results)["assets-immutable-rom_root"] == "fail"
    finally:
        # Restore writability so pytest can remove the temporary tree.
        nested.chmod(0o755)
        rom_root.chmod(0o755)


def test_immutable_asset_check_rejects_a_symlinked_directory(tmp_path: Path):
    """A linked read-only directory can hide a writable leaf from ``rglob``.

    The round-12 finding: a mode-0555 asset root containing a symlink to a
    mode-0555 directory passed the scan while a mode-0644 ROM inside the target
    stayed writable, because ``Path.rglob`` does not descend into a linked
    directory.  Reject the link instead of trusting the tree.
    """

    target = tmp_path / "real-assets"
    target.mkdir()
    rom = target / "pokemon-red.gb"
    rom.write_bytes(b"dummy")
    rom.chmod(0o644)
    rom_root = tmp_path / "rom"
    rom_root.mkdir()
    (rom_root / "red").symlink_to(target)
    for path in (target, rom_root):
        path.chmod(0o555)
    declaration = make_declaration(
        assets={"rom_root": str(rom_root), "fixture_root": str(tmp_path / "fixtures")}
    )
    try:
        results = runner._immutable_asset_checks(declaration, tmp_path)
        assert statuses(results)["assets-immutable-rom_root"] == "fail"
        detail = next(item.detail for item in results if item.name == "assets-immutable-rom_root")
        assert "symbolic link" in detail
    finally:
        target.chmod(0o755)
        rom_root.chmod(0o755)


def test_immutable_asset_check_rejects_an_unlistable_directory(tmp_path: Path):
    """A searchable but unlistable directory must not hide a writable input.

    The round-14 finding: ``os.walk`` swallows ``scandir`` errors by default, so
    a mode-0111 directory under a mode-0555 root vanished from the scan while a
    mode-0644 pinned file inside it stayed writable.
    """

    root = tmp_path / "assets"
    directory = root / "red"
    directory.mkdir(parents=True)
    asset = directory / "pokemon-red.gb"
    asset.write_bytes(b"synthetic reviewer control, not a ROM")
    asset.chmod(0o644)
    directory.chmod(0o111)
    root.chmod(0o555)
    try:
        problem = runner._asset_tree_immutability_problem(root)
        assert problem is not None, "an unobservable asset tree was admitted"
        assert "could not be inspected" in problem
    finally:
        root.chmod(0o755)
        directory.chmod(0o755)


def test_required_asset_rejects_a_symlinked_directory_component(tmp_path: Path):
    """A read-only leaf reached through a linked parent is refused."""

    target = tmp_path / "real"
    target.mkdir()
    payload = b"rom-bytes"
    (target / "pokemon-red.gb").write_bytes(payload)
    rom_root = tmp_path / "rom"
    rom_root.mkdir()
    (rom_root / "linked").symlink_to(target)
    requirement = runner._AssetRequirement(
        key="linked/pokemon-red.gb",
        root="rom_root",
        sha1=hashlib.sha1(payload).hexdigest(),
    )
    result = runner._verify_required_asset(
        "assets-required-rom_root-0", requirement, rom_root, rom_root / requirement.key
    )
    assert result.status == "fail"
    assert "symbolic link" in result.detail
    assert runner._symlinked_path_component(rom_root, "linked/pokemon-red.gb") == "linked"
    assert runner._symlinked_path_component(rom_root, "plain.gb") is None


def test_observed_task_affinities_unions_every_thread(tmp_path: Path, monkeypatch):
    """A worker thread's affinity must not be hidden by its leader's."""

    observed: dict[int, list[int]] = {}

    class _Entry:
        def __init__(self, name: str):
            self.name = name

    monkeypatch.setattr(runner, "_observed_affinity", lambda pid: observed.get(pid))
    real_iterdir = runner.Path.iterdir

    def fake_iterdir(self):
        if str(self).endswith("/task"):
            return [_Entry("100"), _Entry("101")]
        return real_iterdir(self)

    monkeypatch.setattr(runner.Path, "iterdir", fake_iterdir)
    observed[100] = [1]
    observed[101] = [0]
    assert runner._observed_task_affinities(999) == [0, 1]
    # An unobservable thread fails closed rather than dropping the competitor.
    observed.pop(101)
    assert runner._observed_task_affinities(999) is None


def test_foreign_process_affinity_spans_worker_threads(monkeypatch):
    """``_foreign_process_affinity`` must report the union across threads."""

    class _Entry:
        def __init__(self, name: str):
            self.name = name

    real_iterdir = runner.Path.iterdir

    def fake_iterdir(self):
        if str(self) == "/proc":
            return [_Entry("5000")]
        if str(self).endswith("/task"):
            return [_Entry("5001"), _Entry("5002")]
        return real_iterdir(self)

    monkeypatch.setattr(runner.Path, "iterdir", fake_iterdir)
    monkeypatch.setattr(runner, "_process_tree_pids", lambda pid=None: [os.getpid()])
    monkeypatch.setattr(runner, "_pid_is_zombie", lambda pid: False)
    monkeypatch.setattr(runner, "_observed_affinity", lambda pid: {5001: [1], 5002: [0]}.get(pid))
    assert runner._foreign_process_affinity() == {5000: [0, 1]}


def test_redact_payload_scrubs_interpreter_paths():
    declaration = make_declaration(
        interpreters={
            "source": "/private/operator/env/bin/python",
            "native": "/private/operator/native/bin/python",
            "native_build_inputs_sha256": "b" * 64,
            "native_fingerprint": "c" * 64,
        }
    )
    redactions = runner._collect_redactions(
        Path("/repo"), declaration, Path("/repo/declaration.json")
    )
    payload = {
        "detail": (
            "pyboy loaded outside pinned runtime roots: "
            "/private/operator/env/lib/python3.11/site-packages/pyboy/__init__.py"
        )
    }
    rendered = json.dumps(runner._redact_payload(payload, redactions))
    assert "/private/operator" not in rendered


def test_collect_redactions_handles_malformed_declaration(tmp_path: Path):
    declaration = make_declaration(assets=["broken"], interpreters=["broken"])
    redactions = runner._collect_redactions(tmp_path, declaration, None)
    assert str(tmp_path) in redactions


def test_shm_probe_refuses_to_overwrite_existing(tmp_path: Path):
    existing = tmp_path / f".qualification-runner-probe-{os.getpid()}"
    existing.write_bytes(b"keep")
    writable, marker = runner._probe_shm(tmp_path)
    assert writable is False
    assert marker == "shm-probe-exists"
    assert existing.read_bytes() == b"keep"


def test_shm_probe_creates_and_removes(tmp_path: Path):
    writable, marker = runner._probe_shm(tmp_path)
    assert writable is True
    assert marker is None
    assert not (tmp_path / f".qualification-runner-probe-{os.getpid()}").exists()


def test_report_sanitizes_absolute_paths(capsys):
    exit_code = runner.main(["--report", "--json"])
    assert exit_code == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["repo_root"] == "."
    assert str(Path.cwd().resolve()) not in output


def test_validate_declaration_requires_host_wide_lock():
    declaration = make_declaration()
    del declaration["reservation"]["host_lock_path"]
    results = runner.validate_declaration(declaration)
    assert statuses(results)["reservation-host-lock"] == "fail"


def test_validate_declaration_requires_operator_exclusive_marker():
    declaration = make_declaration(reservation_mechanism="dedicated-host")
    results = runner.validate_declaration(declaration)
    assert statuses(results)["reservation-exclusive-marker"] == "fail"
    assert statuses(results)["reservation-exclusive-token"] == "fail"


def test_lease_status_rejects_private_job_lock(tmp_path: Path):
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    lock = job_dir / "allocation.lock"
    lock_fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    _HELD_FDS.append(lock_fd)
    descriptor = {
        "holder_pid": os.getpid(),
        "holder_start_time": runner._process_start_time(os.getpid()),
        "lock_path": str(lock),
    }
    status, detail = runner._descriptor_lease_status(descriptor, make_facts(), job_dir)
    assert status == "fail"
    assert "private" in detail


def test_lease_status_rejects_competing_lock_holder(tmp_path: Path, monkeypatch):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    descriptor = json.loads(
        Path(declaration["reservation"]["descriptor_path"]).read_text(encoding="utf-8")
    )
    job_dir = Path(declaration["reservation"]["job_dir"])
    monkeypatch.setattr(runner, "_observe_flock_holders", lambda path: {os.getpid(), 999999})
    status, detail = runner._descriptor_lease_status(descriptor, facts, job_dir)
    assert status == "fail"
    assert "competing" in detail


def test_all_mechanisms_require_a_real_host_wide_lock(tmp_path: Path):
    for mechanism in runner._RESERVATION_MECHANISMS:
        declaration, facts = held_reservation(tmp_path, mechanism)
        private_lock = Path(declaration["reservation"]["job_dir"]) / "allocation.lock"
        lock_fd = os.open(private_lock, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _HELD_FDS.append(lock_fd)
        rewrite_descriptor(declaration, {"lock_path": str(private_lock)})
        results = runner.evaluate_resources(declaration, facts, tmp_path)
        assert runner.overall_status(results) != "ok", mechanism
        assert statuses(results)["reservation-evidence"] == "fail", mechanism


def test_reserve_allocation_rejects_contended_host_lock(tmp_path: Path):
    host_lock = tmp_path / "host.lock"
    fd = os.open(host_lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        declaration = make_declaration()
        declaration["reservation"]["host_lock_path"] = str(host_lock)
        with pytest.raises(OSError):
            runner._reserve_allocation(declaration, tmp_path, tmp_path / "job", make_facts())
    finally:
        os.close(fd)


def test_reserve_allocation_rejects_private_job_lock(tmp_path: Path):
    job_dir = tmp_path / "job"
    declaration = make_declaration()
    declaration["reservation"]["host_lock_path"] = str(job_dir / "allocation.lock")
    with pytest.raises(ValueError):
        runner._reserve_allocation(declaration, tmp_path, job_dir, make_facts())


def test_cgroup_members_ownership_follows_lease_holder(monkeypatch):
    facts = make_facts(cgroup_member_pids=[10, 20, 30])
    monkeypatch.setattr(runner, "_process_tree_pids", lambda root: [10, 20, 30])
    assert runner._cgroup_members_are_owned(facts, 10) is True
    facts.cgroup_member_pids = [10, 99]
    assert runner._cgroup_members_are_owned(facts, 10) is False


def test_recover_fails_closed_when_lock_unobservable(tmp_path: Path, monkeypatch):
    declaration, _facts = held_reservation(tmp_path, "cgroup-quota")
    descriptor_path = Path(declaration["reservation"]["descriptor_path"])
    monkeypatch.setattr(runner, "_observe_flock_holders", lambda path: None)
    status, _message = runner._recover_allocation(declaration, tmp_path)
    assert status == "blocked"
    assert descriptor_path.exists()


def test_recover_refuses_unrecognized_lock_holder(tmp_path: Path, monkeypatch):
    declaration, _facts = held_reservation(tmp_path, "cgroup-quota")
    descriptor_path = Path(declaration["reservation"]["descriptor_path"])
    monkeypatch.setattr(runner, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(runner, "_observe_flock_holders", lambda path: {999999})
    status, _message = runner._recover_allocation(declaration, tmp_path)
    assert status == "blocked"
    assert descriptor_path.exists()


def test_release_refuses_unrecognized_holder(tmp_path: Path):
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
        status, _message = runner._release_allocation(declaration, tmp_path)
        assert status == "fail"
        assert descriptor_path.exists()
        assert child.poll() is None
    finally:
        child.kill()
        child.wait(timeout=5)


def test_release_refuses_when_lock_unobservable(tmp_path: Path, monkeypatch):
    declaration, _facts = held_reservation(tmp_path, "cgroup-quota")
    descriptor_path = Path(declaration["reservation"]["descriptor_path"])
    monkeypatch.setattr(runner, "_observe_flock_holders", lambda path: None)
    status, _message = runner._release_allocation(declaration, tmp_path)
    assert status == "blocked"
    assert descriptor_path.exists()
