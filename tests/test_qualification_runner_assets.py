"""Asset-input and native-build evidence tests.

Split from ``tests/test_qualification_runner.py`` for issue #112 with no
behavior change: every test below is preserved verbatim.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from scripts import qualification_runner as runner
from tests._qualification_runner_support import (
    NATIVE_FINGERPRINT,
    NATIVE_IDENTITY,
    Completed,
    _prepare_assets,
    _prerequisite_runner,
    _probe_payload,
    held_reservation,
    make_declaration,
    make_facts,
    statuses,
    with_native_evidence,
)


def test_prerequisite_checks_use_injected_runner(tmp_path: Path, monkeypatch):
    requirements, declaration = _prepare_assets(tmp_path)
    with_native_evidence(declaration, tmp_path)
    monkeypatch.setattr(runner, "_required_asset_entries", lambda decl, root: requirements)
    monkeypatch.setattr(runner, "_native_source_digest", lambda root: "b" * 64)
    monkeypatch.setattr(runner, "_asset_tree_immutability_problem", lambda root: None)
    probe_payload = _probe_payload(NATIVE_FINGERPRINT)
    fake_runner, calls = _prerequisite_runner(probe_payload)
    results = runner.prerequisite_checks(declaration, tmp_path, runner=fake_runner)
    assert runner.overall_status(results) == "ok"
    assert any("bootstrap_pyboy.py" in " ".join(command) for command, _ in calls)
    assert any("validate_fixture_manifest.py" in " ".join(command) for command, _ in calls)
    assert any("-c" in command for command, _ in calls)


def test_prerequisite_checks_fail_when_bootstrap_fails(tmp_path: Path):
    declaration = make_declaration()
    results = runner.prerequisite_checks(
        declaration, tmp_path, runner=lambda command, cwd: Completed(1, "", "boom")
    )
    assert statuses(results)["interpreter-source"] == "fail"
    assert statuses(results)["interpreter-native"] == "fail"


def test_prerequisite_checks_fail_missing_interpreter(tmp_path: Path):
    declaration = make_declaration(
        interpreters={
            "source": str(tmp_path / "nope"),
            "native": sys.executable,
            "native_build_inputs_sha256": "b" * 64,
            "native_fingerprint": "c" * 64,
        }
    )
    results = runner.prerequisite_checks(
        declaration, tmp_path, runner=lambda command, cwd: Completed()
    )
    assert statuses(results)["interpreter-source"] == "fail"


def test_native_build_evidence_accepts_consistent_build(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner, "_native_source_digest", lambda root: "b" * 64)
    declaration = make_declaration()
    with_native_evidence(declaration, tmp_path)
    probe_payload = _probe_payload(NATIVE_FINGERPRINT)
    fake_runner, _calls = _prerequisite_runner(probe_payload)
    results = runner._native_build_evidence(declaration, tmp_path, fake_runner)
    assert statuses(results)["native-build-inputs"] == "ok"
    assert statuses(results)["native-runtime-fingerprint"] == "ok"
    assert statuses(results)["native-build-evidence"] == "ok"


def test_native_build_evidence_requires_retained_evidence(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner, "_native_source_digest", lambda root: "b" * 64)
    declaration = make_declaration()
    probe_payload = _probe_payload(NATIVE_FINGERPRINT)
    fake_runner, _calls = _prerequisite_runner(probe_payload)
    results = runner._native_build_evidence(declaration, tmp_path, fake_runner)
    assert statuses(results)["native-build-evidence"] == "unsupported"
    assert runner.overall_status(results) != "ok"


def test_native_build_evidence_rejects_invented_record(tmp_path: Path, monkeypatch):
    """A hand-written record with matching hashes must not pass.

    This is the round-4 finding: the previous validator accepted an operator's
    own JSON carrying a procedure string, ``status="complete"``, and matching
    digests, because a fabricated summary could satisfy every field. The record
    is now required to carry the producer identity and the full runtime
    identity that only an executed build emits.
    """

    monkeypatch.setattr(runner, "_native_source_digest", lambda root: "b" * 64)
    declaration = make_declaration(
        interpreters={
            "source": sys.executable,
            "native": sys.executable,
            "native_build_inputs_sha256": "b" * 64,
            "native_fingerprint": NATIVE_FINGERPRINT,
        }
    )
    invented = {
        "procedure": runner._NATIVE_BUILD_EVIDENCE_PROCEDURE,
        "status": "complete",
        "build_inputs_sha256": "b" * 64,
        "installed_fingerprint": NATIVE_FINGERPRINT,
        "completed_at": "2026-01-01T00:00:00Z",
    }
    path = tmp_path / "invented-build-evidence.json"
    path.write_text(json.dumps(invented), encoding="utf-8")
    declaration["interpreters"]["native_build_evidence"] = str(path)
    declaration["interpreters"]["native_build_evidence_sha256"] = runner._sha256_of_file(path)
    probe_payload = _probe_payload(NATIVE_FINGERPRINT)
    fake_runner, _calls = _prerequisite_runner(probe_payload)
    results = runner._native_build_evidence(declaration, tmp_path, fake_runner)
    assert statuses(results)["native-build-evidence"] == "fail"
    assert runner.overall_status(results) != "ok"


def test_native_build_evidence_rejects_forged_producer_digest(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner, "_native_source_digest", lambda root: "b" * 64)
    declaration = make_declaration()
    with_native_evidence(
        declaration,
        tmp_path,
        producer={"script": "scripts/bootstrap_pyboy.py", "script_sha256": "a" * 64},
    )
    probe_payload = _probe_payload(NATIVE_FINGERPRINT)
    fake_runner, _calls = _prerequisite_runner(probe_payload)
    results = runner._native_build_evidence(declaration, tmp_path, fake_runner)
    assert statuses(results)["native-build-evidence"] == "fail"


def test_native_build_evidence_rejects_reused_identity_for_other_fingerprint(
    tmp_path: Path, monkeypatch
):
    """A record whose identity and fingerprint disagree must not pass."""

    monkeypatch.setattr(runner, "_native_source_digest", lambda root: "b" * 64)
    declaration = make_declaration(
        interpreters={
            "source": sys.executable,
            "native": sys.executable,
            "native_build_inputs_sha256": "b" * 64,
            "native_fingerprint": "e" * 64,
        }
    )
    with_native_evidence(declaration, tmp_path, fingerprint="e" * 64)
    probe_payload = _probe_payload("e" * 64)
    fake_runner, _calls = _prerequisite_runner(probe_payload)
    results = runner._native_build_evidence(declaration, tmp_path, fake_runner)
    assert statuses(results)["native-build-evidence"] == "fail"


def test_native_build_evidence_rejects_unsupported_evidence_version(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner, "_native_source_digest", lambda root: "b" * 64)
    declaration = make_declaration()
    with_native_evidence(declaration, tmp_path, evidence_version=1)
    probe_payload = _probe_payload(NATIVE_FINGERPRINT)
    fake_runner, _calls = _prerequisite_runner(probe_payload)
    results = runner._native_build_evidence(declaration, tmp_path, fake_runner)
    assert statuses(results)["native-build-evidence"] == "fail"


def test_native_build_evidence_rejects_wrong_mode(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner, "_native_source_digest", lambda root: "b" * 64)
    declaration = make_declaration()
    with_native_evidence(declaration, tmp_path, mode="source")
    probe_payload = _probe_payload(NATIVE_FINGERPRINT)
    fake_runner, _calls = _prerequisite_runner(probe_payload)
    results = runner._native_build_evidence(declaration, tmp_path, fake_runner)
    assert statuses(results)["native-build-evidence"] == "fail"


def test_native_build_evidence_rejects_identity_mismatch(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner, "_native_source_digest", lambda root: "b" * 64)
    declaration = make_declaration()
    other_identity = dict(NATIVE_IDENTITY)
    other_identity["modules"] = {
        name: {"kind": "cython", "sha256": "9" * 64} for name in runner._EXTENSION_BACKED_MODULES
    }
    with_native_evidence(
        declaration,
        tmp_path,
        fingerprint=runner._native_build_fingerprint(other_identity),
        identity=other_identity,
    )
    declaration["interpreters"]["native_fingerprint"] = runner._native_build_fingerprint(
        other_identity
    )
    probe_payload = _probe_payload(NATIVE_FINGERPRINT)
    fake_runner, _calls = _prerequisite_runner(probe_payload)
    results = runner._native_build_evidence(declaration, tmp_path, fake_runner)
    assert statuses(results)["native-build-evidence"] == "fail"


def test_native_build_evidence_rejects_mixed_build_evidence(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner, "_native_source_digest", lambda root: "b" * 64)
    declaration = make_declaration()
    with_native_evidence(declaration, tmp_path, fingerprint="e" * 64)
    probe_payload = _probe_payload(NATIVE_FINGERPRINT)
    fake_runner, _calls = _prerequisite_runner(probe_payload)
    results = runner._native_build_evidence(declaration, tmp_path, fake_runner)
    assert statuses(results)["native-build-evidence"] == "fail"
    assert runner.overall_status(results) != "ok"


def test_native_build_evidence_rejects_incomplete_build(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner, "_native_source_digest", lambda root: "b" * 64)
    declaration = make_declaration()
    with_native_evidence(declaration, tmp_path, status="in-progress")
    probe_payload = _probe_payload(NATIVE_FINGERPRINT)
    fake_runner, _calls = _prerequisite_runner(probe_payload)
    results = runner._native_build_evidence(declaration, tmp_path, fake_runner)
    assert statuses(results)["native-build-evidence"] == "fail"


def test_native_build_evidence_rejects_source_mismatch(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner, "_native_source_digest", lambda root: "z" * 64)
    declaration = make_declaration()
    probe_payload = _probe_payload(NATIVE_FINGERPRINT)
    fake_runner, _calls = _prerequisite_runner(probe_payload)
    results = runner._native_build_evidence(declaration, tmp_path, fake_runner)
    assert statuses(results)["native-build-inputs"] == "fail"


def test_native_build_evidence_rejects_fingerprint_mismatch(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner, "_native_source_digest", lambda root: "b" * 64)
    declaration = make_declaration()
    probe_payload = _probe_payload("e" * 64)
    fake_runner, _calls = _prerequisite_runner(probe_payload)
    results = runner._native_build_evidence(declaration, tmp_path, fake_runner)
    assert statuses(results)["native-runtime-fingerprint"] == "fail"


def test_collect_facts_reports_sane_values(tmp_path: Path):
    facts = runner.collect_facts(tmp_path)
    assert facts.logical_cpus >= 1
    assert facts.platform
    assert isinstance(facts.affinity_cpus, list)
    assert facts.process_pid == os.getpid()


def test_cli_report_emits_json_without_claiming_success(capsys):
    exit_code = runner.main(["--report", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "report"
    assert payload["overall"] == "report"
    assert payload["checks"] == []
    assert payload["facts"]["logical_cpus"] >= 1


def test_cli_check_without_declaration_is_blocked(monkeypatch, capsys):
    monkeypatch.delenv(runner._DECLARATION_ENV, raising=False)
    exit_code = runner.main(["--check", "--json"])
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["overall"] == "blocked"


def test_cli_check_reads_environment_declaration(monkeypatch, capsys, tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    declaration_path = tmp_path / "declaration.json"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    monkeypatch.setenv(runner._DECLARATION_ENV, str(declaration_path))
    monkeypatch.setattr(runner, "collect_facts", lambda root: facts)
    monkeypatch.setattr(runner, "prerequisite_checks", lambda decl, root: [])

    exit_code = runner.main(["--check", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["declaration"] == declaration_path.name
    assert payload["overall"] == "ok"
    assert exit_code == 0


def test_cli_setup_prepares_private_job_directory(monkeypatch, capsys, tmp_path: Path):
    declaration = make_declaration()
    declaration_path = tmp_path / "declaration.json"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    job_dir = tmp_path / "private-job"
    monkeypatch.setattr(runner, "collect_facts", lambda root: make_facts())
    monkeypatch.setattr(runner, "prerequisite_checks", lambda decl, root: [])

    exit_code = runner.main(
        [
            "--setup",
            "--json",
            "--declaration",
            str(declaration_path),
            "--job-dir",
            str(job_dir),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["overall"] == "ok"
    assert job_dir.is_dir()
    assert (job_dir / "evidence").is_dir()
