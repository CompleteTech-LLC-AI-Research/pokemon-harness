"""ROM-free tests for the qualification-runner allocation check."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from scripts import qualification_runner as runner

REPO_ROOT = Path(__file__).resolve().parents[1]


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
        cgroup_dir="/sys/fs/cgroup/user.slice/session.scope",
        cgroup_member_pids=[os.getpid()],
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
        process_pid=os.getpid(),
        process_ancestor_pids=[],
        process_tree_pids=[os.getpid()],
        process_start_time=runner._process_start_time(os.getpid()),
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
            "job_dir": "target/qualification-runs/test-runner",
            "descriptor_path": "target/qualification-runs/test-runner/allocation.json",
            "descriptor_sha256": "a" * 64,
            "cgroup_path": "/user.slice/session.scope",
        },
        "logical_cpus": 4,
        "affinity_cpus": [0, 1, 2, 3],
        "cpu_quota_cores": 4.0,
        "cpu_weight": 100,
        "memory_bytes": 8 * 1024**3,
        "disk_free_bytes_min": 10**9,
        "shm_bytes_min": 10**8,
        "interpreters": {
            "source": sys.executable,
            "native": sys.executable,
            "native_build_inputs_sha256": "b" * 64,
            "native_fingerprint": "c" * 64,
        },
        "assets": {
            "rom_root": "/tmp/rom",
            "fixture_root": "/tmp/fixtures",
            "inputs": [
                {
                    "path": "rom/red/pokemon-red.gb",
                    "sha1": "ea9bcae617fdf159b045185467ae58b2e4a48b9a",
                }
            ],
        },
    }
    declaration.update(overrides)
    return declaration


_HELD_FDS: list[int] = []


def held_reservation(tmp_path: Path, mechanism: str = "cgroup-quota", **overrides):
    """Create a real, held allocation descriptor and matching host facts."""

    job_dir = tmp_path / f"job-{mechanism}-{uuid.uuid4().hex[:8]}"
    job_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(job_dir, 0o700)
    lock_path = job_dir / "allocation.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    _HELD_FDS.append(lock_fd)

    if mechanism == "cgroup-quota":
        affinity = [0, 1, 2, 3]
        quota = 4.0
    elif mechanism == "cpuset-affinity":
        affinity = [0, 1, 2, 3]
        quota = None
    else:
        affinity = list(range(16))
        quota = None

    reservation = {
        "allocation_id": "test-runner",
        "job_dir": str(job_dir),
        "descriptor_path": str(job_dir / "allocation.json"),
        "cgroup_path": "/user.slice/session.scope",
        "exclusive": mechanism == "dedicated-host",
    }
    descriptor = {
        "descriptor_version": runner._DESCRIPTOR_VERSION,
        "allocation_id": "test-runner",
        "runner_id": "test-runner",
        "mechanism": mechanism,
        "state": "held",
        "lease_id": "lease-123",
        "holder_pid": os.getpid(),
        "holder_start_time": runner._process_start_time(os.getpid()),
        "lock_path": str(lock_path),
        "cgroup_path": "/user.slice/session.scope",
        "cpuset": affinity,
        "cpu_quota_cores": quota,
        "cpu_weight": 100,
        "logical_cpus": 16,
        "exclusive": mechanism == "dedicated-host",
        "created_at": "2026-01-01T00:00:00Z",
    }
    if mechanism == "dedicated-host":
        marker = job_dir / "exclusive.marker"
        marker.write_text("lease-123", encoding="utf-8")
        os.chmod(marker, 0o400)
        descriptor["exclusive_marker_path"] = str(marker)
    descriptor_path = job_dir / "allocation.json"
    descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
    os.chmod(descriptor_path, 0o400)
    reservation["descriptor_sha256"] = runner._sha256_of_file(descriptor_path)

    declaration = make_declaration(
        reservation_mechanism=mechanism,
        affinity_cpus=affinity,
        cpu_quota_cores=quota,
        reservation=reservation,
    )
    if mechanism == "cgroup-quota":
        facts = make_facts(
            logical_cpus=16,
            affinity_cpus=list(range(8)),
            affinity_count=8,
            cpu_quota_cores=quota,
            cgroup_relative_path="/user.slice/session.scope",
            cgroup_member_pids=[os.getpid()],
            process_tree_pids=[os.getpid()],
        )
    elif mechanism == "cpuset-affinity":
        facts = make_facts(
            logical_cpus=16,
            affinity_cpus=affinity,
            affinity_count=len(affinity),
            cpu_quota_cores=None,
        )
    else:
        facts = make_facts(
            logical_cpus=16,
            affinity_cpus=list(range(16)),
            affinity_count=16,
            cpu_quota_cores=None,
            cpu_weight=100,
        )
    declaration.update(overrides)
    return declaration, facts


def statuses(results):
    return {item.name: item.status for item in results}


def write_input(root: Path, relative: str, data: bytes = b"rom-bytes") -> dict:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return {"path": relative, "sha1": hashlib.sha1(data).hexdigest()}


def required_entries(entries: list[tuple[str, str, str, bytes]]) -> dict:
    requirements = {}
    for root, key, sha1, _data in entries:
        requirements[(root, key)] = runner._AssetRequirement(key=key, root=root, sha1=sha1)
    return requirements


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


def test_validate_declaration_requires_pinned_descriptor():
    declaration = make_declaration()
    declaration["reservation"] = {"allocation_id": "test-runner"}
    results = runner.validate_declaration(declaration)
    assert statuses(results)["reservation-descriptor"] == "fail"
    assert statuses(results)["reservation-descriptor-sha256"] == "fail"
    assert statuses(results)["reservation-job-dir"] == "fail"


def test_validate_declaration_requires_native_build_pins():
    results = runner.validate_declaration(
        make_declaration(interpreters={"source": sys.executable, "native": sys.executable})
    )
    assert statuses(results)["interpreter-native-build-inputs"] == "fail"
    assert statuses(results)["interpreter-native-fingerprint"] == "fail"


def test_validate_declaration_binds_runner_id_to_allocation():
    results = runner.validate_declaration(make_declaration(runner_id="other-runner"))
    assert statuses(results)["reservation-allocation"] == "fail"


def test_validate_declaration_requires_declared_assets():
    declaration = make_declaration(
        assets={"rom_root": "/tmp/rom", "fixture_root": "/tmp/fixtures", "inputs": []}
    )
    results = runner.validate_declaration(declaration)
    assert statuses(results)["assets-inputs"] == "fail"


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


def test_evaluate_resources_accepts_a_held_cgroup_reservation(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["reservation-evidence"] == "ok"
    assert runner.overall_status(results) == "ok"


def test_reservation_negative_control_never_passes(tmp_path: Path):
    reservation = {
        "allocation_id": "test-runner",
        "exclusive": True,
        "cgroup_path": "/user.slice/session.scope",
    }
    facts = make_facts(load_average=[117.0] * 3, psi_cpu_some_avg300=95.0)
    for mechanism in ("dedicated-host", "cpuset-affinity", "cgroup-quota"):
        declaration = make_declaration(
            reservation_mechanism=mechanism,
            affinity_cpus=list(range(8)),
            reservation=dict(reservation),
        )
        results = runner.validate_declaration(declaration) + runner.evaluate_resources(
            declaration, facts, tmp_path
        )
        assert runner.overall_status(results) != "ok"


def test_reservation_rejects_missing_descriptor_file(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    Path(declaration["reservation"]["descriptor_path"]).unlink()
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["reservation-evidence"] != "ok"


def test_reservation_rejects_descriptor_digest_mismatch(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    declaration["reservation"]["descriptor_sha256"] = "f" * 64
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["reservation-evidence"] == "fail"


def test_reservation_rejects_writable_descriptor(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    os.chmod(declaration["reservation"]["descriptor_path"], 0o600)
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["reservation-evidence"] == "fail"


def test_reservation_rejects_unheld_lease(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    lock_path = Path(declaration["reservation"]["job_dir"]) / "allocation.lock"
    fd = _HELD_FDS.pop()
    os.close(fd)
    lock_holders = runner._flock_holder_pids(lock_path)
    assert not lock_holders
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["reservation-evidence"] == "fail"


def test_dedicated_reservation_requires_exclusive_marker(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "dedicated-host")
    Path(declaration["reservation"]["job_dir"], "exclusive.marker").unlink()
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["reservation-evidence"] == "fail"


def test_dedicated_reservation_rejects_partial_host(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "dedicated-host")
    facts.affinity_cpus = [0, 1, 2, 3]
    facts.affinity_count = 4
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["reservation-evidence"] == "fail"


def test_cpuset_reservation_rejects_full_host_affinity(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cpuset-affinity")
    facts.affinity_cpus = list(range(16))
    facts.affinity_count = 16
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["reservation-evidence"] == "fail"


def test_cgroup_reservation_rejects_competing_members(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    facts.cgroup_member_pids = [os.getpid(), 999999]
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["reservation-evidence"] == "fail"


def test_evaluate_resources_fails_when_cpus_are_insufficient(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota", logical_cpus=64)
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["logical-cpus"] == "fail"


def test_evaluate_resources_fails_on_restrictive_observed_quota(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    facts.cpu_quota_cores = 0.5
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["logical-cpus"] == "fail"
    assert runner.overall_status(results) != "ok"


def test_evaluate_resources_fails_when_declared_quota_is_absent(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota", cpu_quota_cores=None)
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["cpu-quota"] == "fail"


def test_evaluate_resources_requires_quota_for_cgroup_reservation(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    declaration["cpu_quota_cores"] = None
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["cpu-quota"] == "fail"


def test_evaluate_resources_quota_equality_is_ok(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota", cpu_quota_cores=8.0)
    facts.cpu_quota_cores = 8.0
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["cpu-quota"] == "ok"


def test_evaluate_resources_fails_when_weight_is_lower(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota", cpu_weight=500)
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["cpu-weight"] == "fail"


def test_evaluate_resources_fails_when_affinity_exceeds_allocation(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota", affinity_cpus=[0, 99])
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["affinity"] == "fail"


def test_evaluate_resources_fails_on_insufficient_memory(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota", memory_bytes=10**15)
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["memory"] == "fail"


def test_evaluate_resources_fails_on_insufficient_disk(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota", disk_free_bytes_min=10**15)
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["disk-free-repo"] == "fail"
    assert statuses(results)["disk-free-temp"] == "fail"


def test_evaluate_resources_fails_when_shared_memory_is_not_writable(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota", shm_bytes_min=10**8)
    facts.shm_writable = False
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["shm"] == "fail"


def test_evaluate_resources_marks_affinity_unsupported(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    facts.affinity_supported = False
    facts.affinity_cpus = []
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["affinity"] == "unsupported"


def test_evaluate_resources_skips_quota_for_verified_dedicated_host(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "dedicated-host")
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["cpu-quota"] == "skipped"
    assert statuses(results)["reservation-evidence"] == "ok"
    assert runner.overall_status(results) == "ok"


def test_evaluate_resources_marks_missing_shm_unsupported(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    facts.shm_writable = False
    facts.unsupported = ["shm-missing"]
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["shm"] == "unsupported"


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


def test_cgroup_v2_detected_below_hierarchy_root(tmp_path: Path):
    child = tmp_path / "user.slice" / "session.scope"
    child.mkdir(parents=True)
    (child / "cpu.max").write_text("200000 100000\n", encoding="utf-8")
    (child / "cgroup.procs").write_text(str(os.getpid()), encoding="utf-8")
    facts = runner._read_cgroup_facts(tmp_path, "0::/user.slice/session.scope\n")
    assert facts["cgroup_version"] == "v2"
    assert facts["cpu_quota_cores"] == 2.0
    assert facts["cgroup_relative_path"] == "/user.slice/session.scope"
    assert facts["cgroup_member_pids"] == [os.getpid()]


def test_cgroup_v1_detected_below_hierarchy_root(tmp_path: Path):
    child = tmp_path / "cpu" / "user.slice" / "session.scope"
    child.mkdir(parents=True)
    (child / "cpu.cfs_quota_us").write_text("50000\n", encoding="utf-8")
    (child / "cpu.cfs_period_us").write_text("100000\n", encoding="utf-8")
    facts = runner._read_cgroup_facts(tmp_path, "12:cpu,cpuacct:/user.slice/session.scope\n")
    assert facts["cgroup_version"] == "v1"
    assert facts["cpu_quota_cores"] == 0.5


class Completed:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _probe_payload(fingerprint: str) -> dict:
    return {
        "identity": {
            "revision": "c565df66c3731fad2856169a90f6bbec99925915",
            "cython_compiled": True,
            "modules": {
                name: {"kind": "cython", "sha256": "d" * 64}
                for name in runner._EXTENSION_BACKED_MODULES
            },
        },
        "fingerprint": fingerprint,
    }


def _prerequisite_runner(probe_payload: dict):
    calls = []

    def fake_runner(command, cwd):
        calls.append((command, cwd))
        if "-c" in command:
            return Completed(0, json.dumps(probe_payload))
        return Completed(0, "ok")

    return fake_runner, calls


def _prepare_assets(tmp_path: Path) -> tuple[Path, Path, dict]:
    rom_root = tmp_path / "rom"
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir(parents=True, exist_ok=True)
    data = b"rom-bytes"
    entry = write_input(rom_root, "red/pokemon-red.gb", data)
    requirements = required_entries(
        [("rom_root", "red/pokemon-red.gb", hashlib.sha1(data).hexdigest(), data)]
    )
    declaration = make_declaration(
        assets={
            "rom_root": str(rom_root),
            "fixture_root": str(fixture_root),
            "inputs": [entry],
        }
    )
    return requirements, declaration


def test_prerequisite_checks_use_injected_runner(tmp_path: Path, monkeypatch):
    requirements, declaration = _prepare_assets(tmp_path)
    monkeypatch.setattr(runner, "_required_asset_entries", lambda decl, root: requirements)
    monkeypatch.setattr(runner, "_native_source_digest", lambda root: "b" * 64)
    monkeypatch.setattr(runner, "_asset_tree_writable", lambda root: False)
    probe_payload = _probe_payload("c" * 64)
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
    probe_payload = _probe_payload("c" * 64)
    fake_runner, _calls = _prerequisite_runner(probe_payload)
    results = runner._native_build_evidence(declaration, tmp_path, fake_runner)
    assert statuses(results)["native-build-inputs"] == "ok"
    assert statuses(results)["native-runtime-fingerprint"] == "ok"


def test_native_build_evidence_rejects_source_mismatch(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner, "_native_source_digest", lambda root: "z" * 64)
    declaration = make_declaration()
    probe_payload = _probe_payload("c" * 64)
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


def test_cli_reserve_run_holds_and_releases_lease(tmp_path: Path):
    declaration = make_declaration()
    declaration_path = tmp_path / "declaration.json"
    declaration_path.write_text(json.dumps(declaration), encoding="utf-8")
    job_dir = tmp_path / "lease-job"
    sentinel = tmp_path / "ran.txt"
    script = f"from pathlib import Path; Path({str(sentinel)!r}).write_text('ran')"
    env = dict(os.environ, PYTHONPATH=f"{REPO_ROOT}:{REPO_ROOT / 'src'}")
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/qualification_runner.py",
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
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
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
            {"holder_pid": 2**31 - 1, "holder_start_time": "0", "lock_path": str(lock_path)}
        ),
        encoding="utf-8",
    )
    declaration = make_declaration(
        reservation={
            "allocation_id": "test-runner",
            "job_dir": str(job_dir),
            "descriptor_path": str(descriptor_path),
            "descriptor_sha256": "a" * 64,
        }
    )
    status, _message = runner._recover_allocation(declaration, tmp_path)
    assert status == "ok"
    assert not descriptor_path.exists()


def test_recover_keeps_active_lease(tmp_path: Path, monkeypatch):
    declaration, _facts = held_reservation(tmp_path, "cgroup-quota")
    descriptor_path = Path(declaration["reservation"]["descriptor_path"])
    monkeypatch.setattr(runner, "_holder_is_owned", lambda holder, start: True)
    status, _message = runner._recover_allocation(declaration, tmp_path)
    assert status == "fail"
    assert descriptor_path.exists()


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
