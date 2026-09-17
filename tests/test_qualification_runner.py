"""ROM-free tests for the qualification-runner allocation check."""

from __future__ import annotations

import argparse
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
        cgroup_descendants={"nr_descendants": 0, "nr_dying_descendants": 0},
        cgroup_sibling_competitors=[],
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
        foreign_process_affinity={},
    )
    for key, value in overrides.items():
        setattr(facts, key, value)
    return facts


def make_declaration(**overrides):
    declaration = _make_declaration_base()
    declaration.update(overrides)
    return declaration


def _make_declaration_base():
    declaration = {
        "declaration_version": runner.SCHEMA_VERSION,
        "runner_id": "test-runner",
        "reservation_mechanism": "cgroup-quota",
        "reservation": {
            "allocation_id": "test-runner",
            "job_dir": "target/qualification-runs/test-runner",
            "descriptor_path": "target/qualification-runs/test-runner/allocation.json",
            "descriptor_sha256": "a" * 64,
            "host_lock_path": "target/qualification-runs/host.lock",
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
            "native_fingerprint": NATIVE_FINGERPRINT,
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
    return declaration


def native_identity() -> dict:
    """The installed-runtime identity a genuine native build would report."""

    return {
        "python": "3.11.9",
        "version": "2.7.0",
        "revision": "c565df66c3731fad2856169a90f6bbec99925915",
        "cython_compiled": True,
        "modules": {
            name: {"kind": "cython", "sha256": "d" * 64}
            for name in runner._EXTENSION_BACKED_MODULES
        },
    }


NATIVE_IDENTITY = native_identity()
NATIVE_FINGERPRINT = runner._native_build_fingerprint(NATIVE_IDENTITY)


_HELD_FDS: list[int] = []


def _lock_observation_supported() -> bool:
    import tempfile

    directory = tempfile.mkdtemp(prefix="qualification-lock-probe-")
    path = Path(directory) / "probe.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        holders = runner._flock_holder_pids(path)
        return holders is not None and os.getpid() in holders
    except OSError:
        return False
    finally:
        os.close(fd)
        os.unlink(path)
        os.rmdir(directory)


_LOCK_OBSERVATION_SUPPORTED = _lock_observation_supported()


def held_reservation(tmp_path: Path, mechanism: str = "cgroup-quota", **overrides):
    """Create a real, held allocation descriptor and matching host facts."""

    if not _LOCK_OBSERVATION_SUPPORTED:
        pytest.skip("kernel lock table is not observable in this sandbox")
    suffix = uuid.uuid4().hex[:8]
    job_dir = tmp_path / f"job-{mechanism}-{suffix}"
    job_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(job_dir, 0o700)
    host_lock = tmp_path / f"host-{mechanism}-{suffix}.lock"
    lock_fd = os.open(host_lock, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    _HELD_FDS.append(lock_fd)
    identity = runner._lock_identity(host_lock)
    assert identity is not None
    runner._HELD_LEASE_FDS.setdefault((identity["device"], identity["inode"]), []).append(lock_fd)

    if mechanism == "cgroup-quota":
        affinity = [0, 1, 2, 3]
        quota = 4.0
    elif mechanism == "cpuset-affinity":
        affinity = [0, 1, 2, 3]
        quota = None
    else:
        affinity = list(range(16))
        quota = None

    exclusive_token = f"operator-token-{suffix}"
    marker = tmp_path / f"exclusive-{suffix}.marker"
    reservation = {
        "allocation_id": "test-runner",
        "job_dir": str(job_dir),
        "descriptor_path": str(job_dir / "allocation.json"),
        "host_lock_path": str(host_lock),
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
        "lock_path": str(host_lock),
        "lock_identity": identity,
        "cgroup_path": "/user.slice/session.scope",
        "cpuset": affinity,
        "cpu_quota_cores": quota,
        "cpu_weight": 100,
        "logical_cpus": 16,
        "exclusive": mechanism == "dedicated-host",
        "created_at": "2026-01-01T00:00:00Z",
    }
    if mechanism == "dedicated-host":
        marker.write_text(exclusive_token, encoding="utf-8")
        os.chmod(marker, 0o400)
        reservation["exclusive_marker_path"] = str(marker)
        reservation["exclusive_token"] = exclusive_token
        descriptor["exclusive_marker_path"] = str(marker)
        descriptor["exclusive_token"] = exclusive_token
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
    lock_path = Path(declaration["reservation"]["host_lock_path"])
    fd = _HELD_FDS.pop()
    os.close(fd)
    lock_holders = runner._flock_holder_pids(lock_path)
    assert not lock_holders
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["reservation-evidence"] == "fail"


def test_dedicated_reservation_requires_exclusive_marker(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "dedicated-host")
    Path(declaration["reservation"]["exclusive_marker_path"]).unlink()
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["reservation-evidence"] == "fail"


def test_dedicated_reservation_rejects_job_generated_marker(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "dedicated-host")
    inside = Path(declaration["reservation"]["job_dir"]) / "exclusive.marker"
    inside.write_text(declaration["reservation"]["exclusive_token"], encoding="utf-8")
    rewrite_descriptor(declaration, {"exclusive_marker_path": str(inside)})
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["reservation-evidence"] == "fail"


def test_dedicated_reservation_rejects_partial_host(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "dedicated-host")
    facts.affinity_cpus = [0, 1, 2, 3]
    facts.affinity_count = 4
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["reservation-evidence"] == "fail"


def test_dedicated_reservation_rejects_competing_cpu_load(tmp_path: Path, monkeypatch):
    declaration, facts = held_reservation(tmp_path, "dedicated-host")
    monkeypatch.setattr(runner, "_measure_competing_cpu_cores", lambda interval=None: (4.5, 1.0))
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["reservation-evidence"] == "fail"


def test_dedicated_reservation_fails_closed_when_cpu_unmeasurable(tmp_path: Path, monkeypatch):
    declaration, facts = held_reservation(tmp_path, "dedicated-host")
    monkeypatch.setattr(runner, "_measure_competing_cpu_cores", lambda interval=None: (None, 1.0))
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["reservation-evidence"] == "unsupported"


def test_dedicated_reservation_accepts_quiet_host(tmp_path: Path, monkeypatch):
    declaration, facts = held_reservation(tmp_path, "dedicated-host")
    monkeypatch.setattr(runner, "_measure_competing_cpu_cores", lambda interval=None: (0.05, 1.0))
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["reservation-evidence"] == "ok"


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


def test_cgroup_reservation_rejects_competing_descendant_cgroups(tmp_path: Path):
    """A quota on one cgroup is not a reservation while sibling cgroups compete."""

    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    facts.cgroup_descendants = {"nr_descendants": 3, "nr_dying_descendants": 0}
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["reservation-evidence"] == "fail"


def test_cgroup_reservation_unsupported_when_descendants_unobservable(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    facts.cgroup_descendants = None
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["reservation-evidence"] == "unsupported"


def test_cgroup_reservation_rejects_populated_sibling_cgroups(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    facts.cgroup_sibling_competitors = ["session-a.scope", "session-b.scope"]
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["reservation-evidence"] == "fail"


def test_cgroup_reservation_unsupported_when_siblings_unobservable(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    facts.cgroup_sibling_competitors = None
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["reservation-evidence"] == "unsupported"


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


def test_evaluate_resources_skips_quota_for_verified_dedicated_host(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(runner, "_measure_competing_cpu_cores", lambda interval=None: (0.05, 1.0))
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
    (child / "cgroup.stat").write_text(
        "nr_descendants 0\nnr_dying_descendants 0\n", encoding="utf-8"
    )
    facts = runner._read_cgroup_facts(tmp_path, "0::/user.slice/session.scope\n")
    assert facts["cgroup_version"] == "v2"
    assert facts["cpu_quota_cores"] == 2.0
    assert facts["cgroup_relative_path"] == "/user.slice/session.scope"
    assert facts["cgroup_member_pids"] == [os.getpid()]
    assert runner._cgroup_descendant_counts(facts["cgroup_dir"]) == {
        "nr_descendants": 0,
        "nr_dying_descendants": 0,
    }


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


def _probe_payload(fingerprint: str, identity: dict | None = None) -> dict:
    return {
        "identity": dict(identity if identity is not None else NATIVE_IDENTITY),
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


def with_native_evidence(
    declaration: dict,
    tmp_path: Path,
    build_inputs: str = "b" * 64,
    fingerprint: str = NATIVE_FINGERPRINT,
    procedure: str = runner._NATIVE_BUILD_EVIDENCE_PROCEDURE,
    status: str = "complete",
    evidence_version: int = runner._NATIVE_BUILD_EVIDENCE_VERSION,
    mode: str = "cython",
    identity: dict | None = None,
    producer: dict | None = None,
) -> dict:
    """Write retained evidence shaped like the real bootstrap's own record."""

    if identity is None:
        identity = dict(NATIVE_IDENTITY)
    if producer is None:
        producer = {
            "script": "scripts/bootstrap_pyboy.py",
            "script_sha256": hashlib.sha256(
                (REPO_ROOT / "scripts" / "bootstrap_pyboy.py").read_bytes()
            ).hexdigest(),
        }
    document = {
        "evidence_version": evidence_version,
        "procedure": procedure,
        "mode": mode,
        "status": status,
        "build_inputs_sha256": build_inputs,
        "installed_fingerprint": fingerprint,
        "runtime_identity": identity,
        "producer": producer,
        "completed_at": "2026-01-01T00:00:00Z",
    }
    path = tmp_path / "native-build-evidence.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    declaration.setdefault("interpreters", {})["native_build_evidence"] = str(path)
    declaration["interpreters"]["native_build_evidence_sha256"] = runner._sha256_of_file(path)
    return declaration


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
    with_native_evidence(declaration, tmp_path)
    monkeypatch.setattr(runner, "_required_asset_entries", lambda decl, root: requirements)
    monkeypatch.setattr(runner, "_native_source_digest", lambda root: "b" * 64)
    monkeypatch.setattr(runner, "_asset_tree_writable", lambda root: False)
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
            "descriptor_sha256": "a" * 64,
        }
    )
    status, _message = runner._recover_allocation(declaration, tmp_path)
    assert status == "ok"
    assert not descriptor_path.exists()


def test_recover_keeps_active_lease(tmp_path: Path):
    declaration, _facts = held_reservation(tmp_path, "cgroup-quota")
    descriptor_path = Path(declaration["reservation"]["descriptor_path"])
    status, _message = runner._recover_allocation(declaration, tmp_path)
    assert status == "fail"
    assert descriptor_path.exists()


def rewrite_descriptor(declaration: dict, changes: dict) -> None:
    path = Path(declaration["reservation"]["descriptor_path"])
    os.chmod(path, 0o600)
    document = json.loads(path.read_text(encoding="utf-8"))
    document.update(changes)
    path.write_text(json.dumps(document), encoding="utf-8")
    os.chmod(path, 0o400)
    declaration["reservation"]["descriptor_sha256"] = runner._sha256_of_file(path)


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


def _write_grandchild_command(tmp_path: Path, exit_code: int, pidfile: Path) -> list[str]:
    """Build a command that forks a sleeping grandchild, waits for it, then exits.

    The grandchild redirects its own stdio and survives its parent, which is the
    exact shape of the round-5 finding: ``run_command`` returns the parent's
    status while a descendant is still consuming the allocation.
    """

    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    if pidfile.exists():
        pidfile.unlink()
    code = (
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "with open(os.devnull, 'wb') as null:\n"
        f"    subprocess.Popen([sys.executable, {str(grandchild)!r}], "
        "stdin=null, stdout=null, stderr=null)\n"
        f"deadline = time.monotonic() + 20\n"
        f"while not Path({str(pidfile)!r}).exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.05)\n"
        f"sys.exit({exit_code})\n"
    )
    return [sys.executable, "-c", code]


def _wait_for_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and runner._pid_alive(pid):
        time.sleep(0.05)
    return not runner._pid_alive(pid)


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


def _recreate_lock_at_same_path(declaration: dict) -> Path:
    """Replace the recorded lock pathname with a fresh, uncontended inode.

    This reproduces the round-4 reproduction: the original inode is still held,
    but the pathname now names a different inode that no allocation contends
    for. Anything that keys mutual exclusion on the path alone is defeated.
    """

    lock_path = Path(declaration["reservation"]["host_lock_path"])
    original_inode = lock_path.stat().st_ino
    # Build the replacement under a distinct name so the filesystem cannot
    # hand back the same inode number for the recreated pathname.
    replacement = lock_path.with_suffix(lock_path.suffix + ".new")
    fd = os.open(replacement, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.replace(replacement, lock_path)
    assert lock_path.stat().st_ino != original_inode
    return lock_path


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

    def fake_run(command, cwd, timeout=None, env=None):
        captured["timeout"] = timeout
        captured["env"] = env
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
    assert captured["env"]["TMPDIR"] == str(job_dir / "tmp")
    assert captured["env"]["POKERED_QUALIFICATION_EVIDENCE_DIR"] == str(job_dir / "evidence")


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
