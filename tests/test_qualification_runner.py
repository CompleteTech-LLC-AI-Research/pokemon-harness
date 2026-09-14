"""ROM-free tests for the qualification-runner allocation check."""

from __future__ import annotations

import hashlib
import json
import os
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
        cgroup_relative_path="/user.slice/session.scope",
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
        "declaration_version": runner.SCHEMA_VERSION,
        "runner_id": "test-runner",
        "reservation_mechanism": "cgroup-quota",
        "reservation": {
            "allocation_id": "test-runner",
            "cgroup_path": "/user.slice/session.scope",
        },
        "logical_cpus": 4,
        "affinity_cpus": [0, 1, 2, 3],
        "cpu_quota_cores": 4.0,
        "cpu_weight": 100,
        "memory_bytes": 8 * 1024**3,
        "disk_free_bytes_min": 10**9,
        "shm_bytes_min": 10**8,
        "interpreters": {"source": sys.executable, "native": sys.executable},
        "assets": {
            "rom_root": "/tmp/rom",
            "fixture_root": "/tmp/fixtures",
            "inputs": [
                {"path": "red/pokemon-red.gb", "sha1": "ea9bcae617fdf159b045185467ae58b2e4a48b9a"}
            ],
        },
    }
    declaration.update(overrides)
    return declaration


def write_input(root: Path, relative: str, data: bytes = b"rom-bytes") -> dict:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"path": relative, "sha1": hashlib.sha1(data).hexdigest()}


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
    skipped = runner.CheckResult("e", "skipped", None, None, "")
    assert runner.overall_status([ok]) == "ok"
    assert runner.overall_status([ok, blocked]) == "blocked"
    assert runner.overall_status([ok, unsupported]) == "unsupported"
    assert runner.overall_status([ok, blocked, fail]) == "fail"
    assert runner.overall_status([ok, skipped]) == "ok"
    assert runner.overall_status([skipped]) == "blocked"
    assert runner.overall_status([]) == "blocked"


def test_validate_declaration_rejects_malformed_values_without_crashing():
    results = runner.validate_declaration(
        make_declaration(
            logical_cpus="many",
            affinity_cpus=None,
            memory_bytes=-1,
            cpu_quota_cores=0,
            cpu_weight="high",
        )
    )
    observed = statuses(results)
    assert observed["logical-cpus-value"] == "fail"
    assert observed["affinity-value"] == "fail"
    assert observed["memory_bytes-value"] == "fail"
    assert observed["cpu-quota-value"] == "fail"
    assert observed["cpu-weight-value"] == "fail"


def test_evaluate_resources_skips_quota_for_verified_dedicated_host():
    declaration = make_declaration(
        reservation_mechanism="dedicated-host",
        cpu_quota_cores=None,
        affinity_cpus=[0, 1, 2, 3],
        reservation={"allocation_id": "test-runner", "exclusive": True},
    )
    facts = make_facts(affinity_cpus=[0, 1, 2, 3], affinity_count=4, cpu_quota_cores=None)
    results = runner.evaluate_resources(declaration, facts)
    assert statuses(results)["cpu-quota"] == "skipped"
    assert statuses(results)["reservation-evidence"] == "ok"
    assert runner.overall_status(results) == "ok"


def test_evaluate_resources_rejects_dedicated_host_that_merely_contains_cpus():
    declaration = make_declaration(
        reservation_mechanism="dedicated-host",
        cpu_quota_cores=None,
        affinity_cpus=[0, 1, 2, 3],
        reservation={"allocation_id": "test-runner", "exclusive": True},
    )
    results = runner.evaluate_resources(declaration, make_facts())
    assert statuses(results)["reservation-evidence"] == "fail"
    assert runner.overall_status(results) != "ok"


def test_evaluate_resources_rejects_dedicated_host_without_exclusive_evidence():
    declaration = make_declaration(
        reservation_mechanism="dedicated-host",
        cpu_quota_cores=None,
        affinity_cpus=[0, 1, 2, 3],
        reservation={"allocation_id": "test-runner"},
    )
    facts = make_facts(affinity_cpus=[0, 1, 2, 3], affinity_count=4, cpu_quota_cores=None)
    results = runner.evaluate_resources(declaration, facts)
    assert statuses(results)["reservation-evidence"] == "fail"


def test_evaluate_resources_requires_quota_for_cgroup_reservation():
    declaration = make_declaration(reservation_mechanism="cgroup-quota", cpu_quota_cores=None)
    results = runner.evaluate_resources(declaration, make_facts())
    assert statuses(results)["cpu-quota"] == "fail"


def test_evaluate_resources_quota_equality_is_ok():
    declaration = make_declaration(cpu_quota_cores=8.0)
    results = runner.evaluate_resources(declaration, make_facts(cpu_quota_cores=8.0))
    assert statuses(results)["cpu-quota"] == "ok"


def test_evaluate_resources_fails_when_weight_is_lower():
    declaration = make_declaration(cpu_weight=500)
    results = runner.evaluate_resources(declaration, make_facts(cpu_weight=100))
    assert statuses(results)["cpu-weight"] == "fail"


def test_evaluate_resources_marks_missing_shm_unsupported():
    facts = make_facts(shm_writable=False, unsupported=["shm-missing"])
    results = runner.evaluate_resources(make_declaration(), facts)
    assert statuses(results)["shm"] == "unsupported"


def test_load_declaration_reports_missing_file(tmp_path: Path):
    declaration, error = runner.load_declaration(tmp_path / "absent.json")
    assert declaration is None
    assert error and "not found" in error


def test_load_declaration_rejects_directory(tmp_path: Path):
    declaration, error = runner.load_declaration(tmp_path)
    assert declaration is None
    assert error and "cannot read" in error


def test_load_declaration_rejects_non_utf8(tmp_path: Path):
    binary = tmp_path / "declaration.bin"
    binary.write_bytes(b"\xff\xfe\x00\x01")
    declaration, error = runner.load_declaration(binary)
    assert declaration is None
    assert error and "cannot read" in error


def test_cgroup_relative_path_resolves_v1_and_v2():
    v2 = "12:pids:/\n0::/user.slice/session.scope\n"
    v1 = "12:cpu,cpuacct:/user.slice/session.scope\n"
    assert runner._cgroup_relative_path(None, v2) == "/user.slice/session.scope"
    assert runner._cgroup_relative_path("cpu", v1) == "/user.slice/session.scope"
    assert runner._cgroup_relative_path("memory", v1) is None


def test_iter_cgroup_paths_walks_to_base(tmp_path: Path):
    base = tmp_path / "cgroup"
    paths = runner._iter_cgroup_paths(base, "/user.slice/session.scope")
    assert paths[0] == base / "user.slice" / "session.scope"
    assert paths[-1] == base
    assert base / "user.slice" in paths


def test_prerequisite_checks_fail_when_bootstrap_fails(tmp_path: Path):
    class Completed:
        def __init__(self, returncode: int):
            self.returncode = returncode
            self.stdout = ""
            self.stderr = "boom"

    declaration = make_declaration()
    results = runner.prerequisite_checks(
        declaration, tmp_path, runner=lambda command, cwd: Completed(1)
    )
    assert statuses(results)["interpreter-source"] == "fail"
    assert statuses(results)["interpreter-native"] == "fail"


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
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    entry = write_input(rom_root, "custom/rom.bin")
    declaration = make_declaration(
        assets={
            "rom_root": str(rom_root),
            "fixture_root": str(fixture_root),
            "inputs": [entry],
        }
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
    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    rom_root = tmp_path / "rom"
    rom_root.mkdir()
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    entry = write_input(rom_root, "custom/rom.bin")
    declaration = make_declaration(
        assets={
            "rom_root": str(rom_root),
            "fixture_root": str(fixture_root),
            "inputs": [entry],
        }
    )
    declaration_path = tmp_path / "declaration.json"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    monkeypatch.setenv(runner._DECLARATION_ENV, str(declaration_path))
    monkeypatch.setattr(runner, "collect_facts", lambda root: make_facts())
    monkeypatch.setattr(runner, "run_command", lambda command, cwd: Completed())

    exit_code = runner.main(["--check", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert payload["declaration"] == declaration_path.name
    assert payload["overall"] == "ok"
    assert exit_code == 0


def test_validate_declaration_requires_reservation_evidence():
    declaration = make_declaration()
    declaration.pop("reservation")
    results = runner.validate_declaration(declaration)
    assert statuses(results)["reservation-structure"] == "fail"


def test_validate_declaration_binds_runner_id_to_allocation():
    results = runner.validate_declaration(make_declaration(runner_id="other-runner"))
    assert statuses(results)["reservation-allocation"] == "fail"


def test_validate_declaration_requires_declared_assets():
    declaration = make_declaration(
        assets={"rom_root": "/tmp/rom", "fixture_root": "/tmp/fixtures", "inputs": []}
    )
    results = runner.validate_declaration(declaration)
    assert statuses(results)["assets-inputs"] == "fail"


def test_evaluate_resources_cgroup_quota_requires_observed_path():
    facts = make_facts(cgroup_relative_path="/system.slice/other.scope")
    results = runner.evaluate_resources(make_declaration(), facts)
    assert statuses(results)["reservation-evidence"] == "fail"


def test_evaluate_resources_cpuset_affinity_must_be_exact():
    declaration = make_declaration(reservation_mechanism="cpuset-affinity", cpu_quota_cores=None)
    results = runner.evaluate_resources(declaration, make_facts())
    assert statuses(results)["affinity"] == "fail"
    assert statuses(results)["reservation-evidence"] == "fail"


@pytest.mark.parametrize("mechanism", ["cgroup-quota", "dedicated-host", "cpuset-affinity"])
def test_cpu_bandwidth_negative_control_never_passes(mechanism):
    reservation = {"allocation_id": "test-runner", "cgroup_path": "/user.slice/session.scope"}
    if mechanism == "dedicated-host":
        reservation["exclusive"] = True
    declaration = make_declaration(
        reservation_mechanism=mechanism,
        logical_cpus=4,
        affinity_cpus=list(range(8)),
        cpu_quota_cores=0.5,
        reservation=reservation,
    )
    facts = make_facts(affinity_cpus=list(range(8)), affinity_count=8, cpu_quota_cores=0.5)
    results = runner.evaluate_resources(declaration, facts)
    assert runner.overall_status(results) != "ok"


@pytest.mark.parametrize("mechanism", ["dedicated-host", "cpuset-affinity"])
def test_undeclared_restrictive_quota_is_not_skipped(mechanism):
    reservation = {"allocation_id": "test-runner"}
    if mechanism == "dedicated-host":
        reservation["exclusive"] = True
    declaration = make_declaration(
        reservation_mechanism=mechanism,
        logical_cpus=4,
        affinity_cpus=list(range(8)),
        cpu_quota_cores=None,
        reservation=reservation,
    )
    facts = make_facts(affinity_cpus=list(range(8)), affinity_count=8, cpu_quota_cores=0.5)
    results = runner.evaluate_resources(declaration, facts)
    assert statuses(results)["cpu-quota"] == "fail"
    assert runner.overall_status(results) != "ok"


def test_cgroup_v2_detected_below_hierarchy_root(tmp_path: Path):
    child = tmp_path / "user.slice" / "session.scope"
    child.mkdir(parents=True)
    (child / "cpu.max").write_text("200000 100000\n", encoding="utf-8")
    facts = runner._read_cgroup_facts(tmp_path, "0::/user.slice/session.scope\n")
    assert facts["cgroup_version"] == "v2"
    assert facts["cpu_quota_cores"] == 2.0
    assert facts["cgroup_relative_path"] == "/user.slice/session.scope"


def test_cgroup_v1_detected_below_hierarchy_root(tmp_path: Path):
    child = tmp_path / "cpu" / "user.slice" / "session.scope"
    child.mkdir(parents=True)
    (child / "cpu.cfs_quota_us").write_text("50000\n", encoding="utf-8")
    (child / "cpu.cfs_period_us").write_text("100000\n", encoding="utf-8")
    facts = runner._read_cgroup_facts(tmp_path, "12:cpu,cpuacct:/user.slice/session.scope\n")
    assert facts["cgroup_version"] == "v1"
    assert facts["cpu_quota_cores"] == 0.5


def _asset_declaration(tmp_path: Path, rom_root: Path, entry: dict) -> dict:
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir(exist_ok=True)
    return make_declaration(
        assets={
            "rom_root": str(rom_root),
            "fixture_root": str(fixture_root),
            "inputs": [entry],
        }
    )


def test_validate_asset_inputs_verifies_declared_bytes(tmp_path: Path):
    rom_root = tmp_path / "rom"
    entry = write_input(rom_root, "red/custom.gb")
    results = runner.validate_asset_inputs(_asset_declaration(tmp_path, rom_root, entry), tmp_path)
    assert runner.overall_status(results) == "ok"


def test_validate_asset_inputs_blocks_missing_file(tmp_path: Path):
    rom_root = tmp_path / "rom"
    rom_root.mkdir()
    entry = {"path": "red/missing.gb", "sha1": "0" * 40}
    results = runner.validate_asset_inputs(_asset_declaration(tmp_path, rom_root, entry), tmp_path)
    assert statuses(results)["assets-input-0"] == "fail"


def test_validate_asset_inputs_blocks_empty_file(tmp_path: Path):
    rom_root = tmp_path / "rom"
    entry = write_input(rom_root, "red/empty.gb", b"")
    results = runner.validate_asset_inputs(_asset_declaration(tmp_path, rom_root, entry), tmp_path)
    assert statuses(results)["assets-input-0"] == "fail"


def test_validate_asset_inputs_blocks_hash_mismatch(tmp_path: Path):
    rom_root = tmp_path / "rom"
    entry = write_input(rom_root, "red/custom.gb", b"content")
    entry["sha1"] = "0" * 40
    results = runner.validate_asset_inputs(_asset_declaration(tmp_path, rom_root, entry), tmp_path)
    assert statuses(results)["assets-input-0"] == "fail"


def test_validate_asset_inputs_prefers_repository_pins(tmp_path: Path):
    repo_root = Path(__file__).resolve().parents[1]
    rom_root = tmp_path / "rom"
    entry = write_input(rom_root, "red/pokemon-red.gb", b"not-the-pinned-rom")
    entry["sha1"] = "0" * 40
    results = runner.validate_asset_inputs(_asset_declaration(tmp_path, rom_root, entry), repo_root)
    assert statuses(results)["assets-input-0"] == "fail"
    assert "repository pin" in results[0].detail


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
