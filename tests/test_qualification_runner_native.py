"""Native build evidence and probe tests.

Split from ``tests/test_qualification_runner.py`` for issue #112 with no
behavior change: every test below is preserved verbatim.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import qualification_runner as runner
from tests._qualification_runner_support import (
    _LOCK_OBSERVATION_SUPPORTED,
    REPO_ROOT,
    _prerequisite_runner,
    _probe_payload,
    _run_native_probe,
    _stale_job_dir,
    _write_fake_pyboy,
    held_reservation,
    make_declaration,
    make_facts,
    native_identity,
    statuses,
    with_native_evidence,
)


def test_native_probe_covers_the_complete_installed_output_set(tmp_path: Path):
    """A compiled module the module list never names must change the identity.

    The round-9 finding mutated ``pyboy.core.cpu`` inside an otherwise identical
    installed runtime.  The reported identity and fingerprint did not change,
    because only the six named entry modules were hashed, so a mixed build could
    be admitted as a consistent one.
    """

    fake_root = tmp_path / "site"
    _write_fake_pyboy(fake_root)
    identity = _run_native_probe(fake_root, tmp_path)["identity"]
    covered = identity["artifacts"]
    assert "core/cpu.py" in covered
    for name in runner._RUNTIME_MODULES:
        entry = identity["modules"][name]
        assert covered[entry["artifact"]] == entry["sha256"]

    original = _run_native_probe(fake_root, tmp_path)
    (fake_root / "pyboy" / "core" / "cpu.py").write_text("CPU = False\n", encoding="utf-8")
    mutated = _run_native_probe(fake_root, tmp_path)
    assert mutated["identity"]["artifacts"]["core/cpu.py"] != covered["core/cpu.py"]
    assert mutated["fingerprint"] != original["fingerprint"]


def test_native_probe_and_bootstrap_publish_the_same_artifact_set(tmp_path: Path):
    """Both identity producers must describe the installed output set identically."""

    fake_root = tmp_path / "site"
    _write_fake_pyboy(fake_root)
    probe_identity = _run_native_probe(fake_root, tmp_path)["identity"]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(fake_root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    code = (
        "import json, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import bootstrap_pyboy\n"
        "print(json.dumps(bootstrap_pyboy._runtime_identity()))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code, str(REPO_ROOT / "scripts")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    bootstrap_identity = json.loads(completed.stdout)
    assert bootstrap_identity["artifacts"] == probe_identity["artifacts"]
    assert runner._native_build_fingerprint(bootstrap_identity) == runner._native_build_fingerprint(
        probe_identity
    )


def test_native_build_evidence_rejects_uncovered_extension_module(tmp_path: Path, monkeypatch):
    """An identity whose artifact map misses a named extension is rejected."""

    monkeypatch.setattr(runner, "_native_source_digest", lambda root: "b" * 64)
    declaration = make_declaration()
    with_native_evidence(declaration, tmp_path)
    identity = native_identity()
    del identity["artifacts"]["pyboy/core/serial.cpython-311-x86_64-linux-gnu.so"]
    fingerprint = runner._native_build_fingerprint(identity)
    with_native_evidence(declaration, tmp_path, fingerprint=fingerprint, identity=identity)
    probe_payload = _probe_payload(fingerprint, identity)
    fake_runner, _calls = _prerequisite_runner(probe_payload)
    results = runner._native_build_evidence(declaration, tmp_path, fake_runner)
    status = statuses(results)["native-runtime-fingerprint"]
    assert status == "fail"


def test_cli_reserve_run_holds_and_releases_lease(monkeypatch, capsys, tmp_path: Path):
    if not _LOCK_OBSERVATION_SUPPORTED:
        pytest.skip("kernel lock table is not observable in this sandbox")
    declaration = make_declaration()
    declaration["reservation"]["host_lock_path"] = str(tmp_path / "host.lock")
    declaration_path = tmp_path / "declaration.json"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    job_dir = tmp_path / "lease-job"
    sentinel = tmp_path / "ran.txt"
    script = f"from pathlib import Path; Path({str(sentinel)!r}).write_text('ran')"
    monkeypatch.setattr(runner, "collect_facts", lambda root: make_facts(cpu_quota_cores=4.0))
    monkeypatch.setattr(
        runner,
        "prerequisite_checks",
        lambda decl, root: [runner.CheckResult("stub", "ok", 1, 1, "")],
    )

    exit_code = runner.main(
        [
            "--reserve",
            "--json",
            "--declaration",
            str(declaration_path),
            "--job-dir",
            str(job_dir),
            "--run",
            sys.executable,
            "-c",
            script,
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0, payload
    assert sentinel.read_text(encoding="utf-8") == "ran"
    assert not (job_dir / "allocation.json").exists()
    pinned = json.loads(declaration_path.read_text(encoding="utf-8"))
    assert pinned["reservation"]["descriptor_sha256"]


def test_recover_removes_stale_allocation(tmp_path: Path):
    job_dir = tmp_path / "stale-job"
    job_dir.mkdir()
    os.chmod(job_dir, 0o700)
    lock_path = job_dir / "allocation.lock"
    lock_path.write_text("", encoding="utf-8")
    descriptor_path = job_dir / "allocation.json"
    descriptor_path.write_text(
        json.dumps(
            {
                "holder_pid": 2**31 - 1,
                "holder_start_time": "0",
                "lock_path": str(lock_path),
                "lock_identity": runner._lock_identity(lock_path),
            }
        ),
        encoding="utf-8",
    )
    declaration = make_declaration(
        reservation={
            "allocation_id": "test-runner",
            "job_dir": str(job_dir),
            "descriptor_path": str(descriptor_path),
            "descriptor_sha256": runner._sha256_of_file(descriptor_path),
        }
    )
    status, _message = runner._recover_allocation(declaration, tmp_path)
    assert status == "ok"
    assert not descriptor_path.exists()


def test_recover_refuses_a_descriptor_rewritten_by_another_lease(tmp_path: Path):
    """A saved declaration must not act on a later lease's descriptor bytes."""

    declaration, descriptor_path, _job_dir = _stale_job_dir(tmp_path, None)
    replacement = {
        "descriptor_version": runner._DESCRIPTOR_VERSION,
        "holder_pid": os.getpid(),
        "holder_start_time": runner._process_start_time(os.getpid()),
    }
    descriptor_path.write_text(json.dumps(replacement), encoding="utf-8")
    status, message = runner._recover_allocation(declaration, tmp_path)
    assert status == "blocked", message
    assert "digest" in message
    assert descriptor_path.exists(), "the replacement lease's descriptor was removed"


def test_release_refuses_a_descriptor_rewritten_by_another_lease(tmp_path: Path):
    """A stale declaration must not signal or remove a replacement lease."""

    declaration, descriptor_path, _job_dir = _stale_job_dir(tmp_path, None)
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    descriptor["holder_pid"] = os.getpid()
    descriptor["holder_start_time"] = runner._process_start_time(os.getpid())
    descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
    status, message = runner._release_allocation(declaration, tmp_path)
    assert status == "blocked", message
    assert "digest" in message
    assert descriptor_path.exists()


def test_recover_keeps_active_lease(tmp_path: Path):
    declaration, _facts = held_reservation(tmp_path, "cgroup-quota")
    descriptor_path = Path(declaration["reservation"]["descriptor_path"])
    status, _message = runner._recover_allocation(declaration, tmp_path)
    assert status == "fail"
    assert descriptor_path.exists()
