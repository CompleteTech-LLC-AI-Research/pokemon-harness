"""ROM-free tests for the qualification-runner allocation check."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts import qualification_runner as runner


def make_facts(**overrides):
    facts = runner.RunnerFacts(
        platform="Linux test",
        kernel="test",
        logical_cpus=16,
        affinity_cpus=list(range(8)),
        affinity_count=8,
        affinity_supported=True,
        cgroup_version="v2",
        cpu_quota_cores=8.0,
        cpu_weight=100,
        cpu_throttled={"nr_throttled": 0},
        memory_total_bytes=64 * 1024**3,
        memory_available_bytes=32 * 1024**3,
        load_average=[1.0, 1.0, 1.0],
        psi_cpu_some_avg300=1.0,
        repo_disk_free_bytes=10**12,
        temp_disk_free_bytes=10**12,
        shm_path="/dev/shm",
        shm_size_bytes=10**9,
        shm_writable=True,
    )
    for key, value in overrides.items():
        setattr(facts, key, value)
    return facts


def make_declaration(**overrides):
    declaration = {
        "declaration_version": 1,
        "runner_id": "test-runner",
        "reservation_mechanism": "cgroup-quota",
        "logical_cpus": 4,
        "affinity_cpus": [0, 1, 2, 3],
        "cpu_quota_cores": 4.0,
        "cpu_weight": 100,
        "memory_bytes": 8 * 1024**3,
        "disk_free_bytes_min": 10**9,
        "shm_bytes_min": 10**8,
        "interpreters": {"source": sys.executable, "native": sys.executable},
        "assets": {"rom_root": "/tmp/rom", "fixture_root": "/tmp/fixtures"},
    }
    declaration.update(overrides)
    return declaration


def statuses(results):
    return {item.name: item.status for item in results}


def test_parse_cpuset_accepts_ranges_and_lists():
    assert runner.parse_cpuset("0-3,8") == [0, 1, 2, 3, 8]
    assert runner.parse_cpuset([3, 1, 3, 2]) == [1, 2, 3]
    assert runner.parse_cpuset("5") == [5]


def test_parse_cpuset_rejects_reversed_range():
    with pytest.raises(ValueError):
        runner.parse_cpuset("4-1")


def test_validate_declaration_reports_missing_fields():
    results = runner.validate_declaration({"declaration_version": 1})
    assert statuses(results)["declaration-fields"] == "blocked"


def test_validate_declaration_rejects_unversioned_schema():
    results = runner.validate_declaration(make_declaration(declaration_version=99))
    assert statuses(results)["declaration-version"] == "unsupported"


def test_validate_declaration_requires_a_reservation_mechanism():
    results = runner.validate_declaration(make_declaration(reservation_mechanism="none"))
    assert statuses(results)["reservation-mechanism"] == "fail"


def test_validate_declaration_accepts_a_complete_declaration():
    results = runner.validate_declaration(make_declaration())
    assert runner.overall_status(results) == "ok"


def test_evaluate_resources_accepts_a_satisfied_declaration():
    results = runner.evaluate_resources(make_declaration(), make_facts())
    assert runner.overall_status(results) == "ok"


def test_evaluate_resources_fails_when_cpus_are_insufficient():
    results = runner.evaluate_resources(make_declaration(logical_cpus=64), make_facts())
    assert statuses(results)["logical-cpus"] == "fail"


def test_evaluate_resources_fails_when_affinity_exceeds_allocation():
    results = runner.evaluate_resources(make_declaration(affinity_cpus=[0, 99]), make_facts())
    assert statuses(results)["affinity"] == "fail"


def test_evaluate_resources_fails_when_declared_quota_is_absent():
    results = runner.evaluate_resources(make_declaration(), make_facts(cpu_quota_cores=None))
    assert statuses(results)["cpu-quota"] == "fail"


def test_evaluate_resources_fails_when_observed_quota_is_lower():
    results = runner.evaluate_resources(make_declaration(cpu_quota_cores=12.0), make_facts())
    assert statuses(results)["cpu-quota"] == "fail"


def test_evaluate_resources_fails_on_insufficient_memory():
    results = runner.evaluate_resources(make_declaration(memory_bytes=10**15), make_facts())
    assert statuses(results)["memory"] == "fail"


def test_evaluate_resources_fails_on_insufficient_disk():
    results = runner.evaluate_resources(make_declaration(disk_free_bytes_min=10**15), make_facts())
    assert statuses(results)["disk-free-repo"] == "fail"
    assert statuses(results)["disk-free-temp"] == "fail"


def test_evaluate_resources_fails_when_shared_memory_is_not_writable():
    results = runner.evaluate_resources(make_declaration(), make_facts(shm_writable=False))
    assert statuses(results)["shm"] == "fail"


def test_evaluate_resources_marks_affinity_unsupported():
    results = runner.evaluate_resources(
        make_declaration(), make_facts(affinity_supported=False, affinity_cpus=[])
    )
    assert statuses(results)["affinity"] == "unsupported"


def test_overall_status_precedence():
    ok = runner.CheckResult("a", "ok", 1, 1, "")
    fail = runner.CheckResult("b", "fail", 1, 0, "")
    blocked = runner.CheckResult("c", "blocked", 1, None, "")
    unsupported = runner.CheckResult("d", "unsupported", 1, None, "")
    assert runner.overall_status([ok]) == "ok"
    assert runner.overall_status([ok, blocked]) == "blocked"
    assert runner.overall_status([ok, unsupported]) == "unsupported"
    assert runner.overall_status([ok, blocked, fail]) == "fail"


def test_prerequisite_checks_use_injected_runner(tmp_path: Path):
    class Completed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    calls = []

    def fake_runner(command, cwd):
        calls.append((command, cwd))
        return Completed()

    rom_root = tmp_path / "rom"
    rom_root.mkdir()
    declaration = make_declaration(
        assets={"rom_root": str(rom_root), "fixture_root": str(tmp_path)}
    )
    results = runner.prerequisite_checks(declaration, tmp_path, runner=fake_runner)
    assert runner.overall_status(results) == "ok"
    assert any("bootstrap_pyboy.py" in " ".join(command) for command, _ in calls)
    assert any("validate_fixture_manifest.py" in " ".join(command) for command, _ in calls)


def test_prerequisite_checks_fail_missing_interpreter(tmp_path: Path):
    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    declaration = make_declaration(
        interpreters={"source": str(tmp_path / "nope"), "native": sys.executable}
    )
    results = runner.prerequisite_checks(declaration, tmp_path, runner=lambda c, w: Completed())
    assert statuses(results)["interpreter-source"] == "fail"


def test_collect_facts_reports_sane_values(tmp_path: Path):
    facts = runner.collect_facts(tmp_path)
    assert facts.logical_cpus >= 1
    assert facts.platform
    assert isinstance(facts.affinity_cpus, list)


def test_cli_report_emits_json(capsys):
    exit_code = runner.main(["--report", "--json"])
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "report"
    assert payload["facts"]["logical_cpus"] >= 1


def test_cli_check_without_declaration_is_blocked(monkeypatch, capsys):
    monkeypatch.delenv(runner._DECLARATION_ENV, raising=False)
    exit_code = runner.main(["--check", "--json"])
    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["overall"] == "blocked"


def test_cli_check_reads_environment_declaration(monkeypatch, capsys, tmp_path: Path):
    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(runner, "run_command", lambda command, cwd: Completed())
    rom_root = tmp_path / "rom"
    rom_root.mkdir()
    declaration = make_declaration(
        assets={"rom_root": str(rom_root), "fixture_root": str(tmp_path)}
    )
    declaration_path = tmp_path / "declaration.json"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    monkeypatch.setenv(runner._DECLARATION_ENV, str(declaration_path))
    exit_code = runner.main(["--check", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["overall"] in {"ok", "fail", "blocked", "unsupported"}
    assert isinstance(exit_code, int)
