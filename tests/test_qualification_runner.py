"""ROM-free tests for the qualification-runner allocation check."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import signal
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
        shm_available_bytes=10**9,
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

    artifacts = {
        f"{name.replace('.', '/')}.cpython-311-x86_64-linux-gnu.so": "d" * 64
        for name in runner._EXTENSION_BACKED_MODULES
    }
    # The installed output set is wider than the named entry modules: a mixed
    # build can replace a compiled module this list never names.
    artifacts["core/cpu.cpython-311-x86_64-linux-gnu.so"] = "a" * 64
    return {
        "python": "3.11.9",
        "version": "2.7.0",
        "revision": "c565df66c3731fad2856169a90f6bbec99925915",
        "cython_compiled": True,
        "modules": {
            name: {
                "kind": "cython",
                "sha256": "d" * 64,
                "artifact": f"{name.replace('.', '/')}.cpython-311-x86_64-linux-gnu.so",
            }
            for name in runner._EXTENSION_BACKED_MODULES
        },
        "artifacts": artifacts,
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


def test_evaluate_resources_memory_uses_available_not_only_total(tmp_path: Path):
    """A host's ``MemTotal`` is not evidence the job can allocate that much.

    The round-9 finding admitted an 8 GiB requirement against a host reporting
    1 byte available, because admission compared ``memory_total_bytes`` only.
    """

    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    facts.memory_total_bytes = 8 * 1024**3
    facts.memory_available_bytes = 1
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    memory = next(item for item in results if item.name == "memory")
    assert memory.status == "fail"
    assert memory.observed == 1


def test_evaluate_resources_memory_respects_the_allocation_cgroup_limit(tmp_path: Path):
    """A cgroup limit below the declared requirement must fail admission."""

    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    facts.memory_total_bytes = 64 * 1024**3
    facts.memory_available_bytes = 32 * 1024**3
    facts.memory_limit_bytes = 2 * 1024**3
    facts.memory_headroom_bytes = 2 * 1024**3
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    memory = next(item for item in results if item.name == "memory")
    assert memory.status == "fail"
    assert memory.observed == 2 * 1024**3
    assert "cgroup-headroom" in memory.detail


def test_evaluate_resources_memory_respects_cgroup_headroom(tmp_path: Path):
    """A nearly-full ancestor must not be admitted as if it were empty."""

    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    facts.memory_total_bytes = 64 * 1024**3
    facts.memory_available_bytes = 32 * 1024**3
    # The ancestor's limit is generous but its existing usage leaves only a
    # quarter of a GiB for this job; the declared requirement is far larger.
    facts.memory_limit_bytes = 8 * 1024**3
    facts.memory_headroom_bytes = 256 * 1024**2
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    memory = next(item for item in results if item.name == "memory")
    assert memory.status == "fail"
    assert memory.observed == 256 * 1024**2
    assert "cgroup-headroom" in memory.detail


def test_evaluate_resources_memory_fails_closed_on_unreadable_headroom(tmp_path: Path):
    """A finite limit with unreadable usage is unknown headroom, not free memory."""

    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    facts.memory_total_bytes = 64 * 1024**3
    facts.memory_available_bytes = 32 * 1024**3
    facts.memory_limit_bytes = 2 * 1024**3
    facts.memory_headroom_observable = False
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    memory = next(item for item in results if item.name == "memory")
    assert memory.status == "unsupported"


def test_evaluate_resources_memory_fails_closed_on_unreadable_limit(tmp_path: Path):
    """An unreadable cgroup limit is unknown capacity, not unlimited capacity."""

    declaration, facts = held_reservation(tmp_path, "cgroup-quota")
    facts.memory_total_bytes = 64 * 1024**3
    facts.memory_available_bytes = 32 * 1024**3
    facts.memory_limit_observable = False
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    memory = next(item for item in results if item.name == "memory")
    assert memory.status == "unsupported"
    assert "could not be read" in memory.detail


def test_cgroup_memory_limit_reads_the_tightest_ancestor_limit(tmp_path: Path):
    """``memory.max`` is walked across ancestors and the tightest finite wins."""

    root = tmp_path / "cgroup"
    leaf = root / "user.slice" / "session.scope"
    leaf.mkdir(parents=True)
    (root / "memory.max").write_text("max\n", encoding="utf-8")
    (root / "user.slice" / "memory.max").write_text(str(4 * 1024**3) + "\n", encoding="utf-8")
    (leaf / "memory.max").write_text(str(2 * 1024**3) + "\n", encoding="utf-8")
    paths = runner._iter_cgroup_paths(root, "/user.slice/session.scope")
    limit, observable = runner._cgroup_memory_limit(paths, "memory.max")
    assert observable is True
    assert limit == 2 * 1024**3


def test_cgroup_memory_limit_reports_an_unreadable_level(tmp_path: Path, monkeypatch):
    """A limit file that exists but cannot be read must not look like ``max``."""

    root = tmp_path / "cgroup"
    leaf = root / "user.slice" / "session.scope"
    leaf.mkdir(parents=True)
    (leaf / "memory.max").write_text(str(2 * 1024**3) + "\n", encoding="utf-8")
    paths = runner._iter_cgroup_paths(root, "/user.slice/session.scope")
    real_read_text = runner._read_text

    def unreadable(path: Path) -> str | None:
        if path.name == "memory.max" and path.parent == leaf:
            return None
        return real_read_text(path)

    monkeypatch.setattr(runner, "_read_text", unreadable)
    limit, observable = runner._cgroup_memory_limit(paths, "memory.max")
    assert observable is False
    assert limit is None


def test_read_cgroup_facts_reports_the_memory_bound(tmp_path: Path):
    """The v2 walk must publish both the limit and its observability."""

    base = tmp_path / "cgroup"
    leaf = base / "user.slice" / "session.scope"
    leaf.mkdir(parents=True)
    (leaf / "cpu.max").write_text("200000 100000\n", encoding="utf-8")
    (base / "user.slice" / "memory.max").write_text(str(6 * 1024**3) + "\n", encoding="utf-8")
    facts = runner._read_cgroup_facts(base, "0::/user.slice/session.scope\n")
    assert facts["cgroup_version"] == "v2"
    assert facts["memory_limit_bytes"] == 6 * 1024**3
    assert facts["memory_limit_observable"] is True


def test_read_cgroup_facts_bounds_memory_without_a_cpu_controller(tmp_path: Path):
    """A memory-only hierarchy still bounds admission when ``cpu.max`` is absent."""

    base = tmp_path / "cgroup"
    leaf = base / "user.slice" / "session.scope"
    leaf.mkdir(parents=True)
    (leaf / "memory.max").write_text(str(3 * 1024**3) + "\n", encoding="utf-8")
    facts = runner._read_cgroup_facts(base, "0::/user.slice/session.scope\n")
    assert facts["cgroup_version"] == "unavailable"
    assert facts["memory_limit_bytes"] == 3 * 1024**3


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


def test_evaluate_resources_shm_uses_available_space_not_total(tmp_path: Path):
    """A shared-memory mount with little free space must fail admission."""

    declaration, facts = held_reservation(tmp_path, "cgroup-quota", shm_bytes_min=8 * 1024**2)
    # The filesystem is large in total, but almost all of it is reserved, so an
    # unprivileged writer cannot satisfy the declared minimum.
    facts.shm_size_bytes = 64 * 1024**2
    facts.shm_available_bytes = 4 * 1024
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    shm = next(item for item in results if item.name == "shm")
    assert shm.status == "fail"
    assert shm.observed == 4 * 1024
    assert "available" in shm.detail


def test_evaluate_resources_shm_unsupported_when_availability_is_unobservable(tmp_path: Path):
    declaration, facts = held_reservation(tmp_path, "cgroup-quota", shm_bytes_min=8 * 1024**2)
    facts.shm_available_bytes = None
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["shm"] == "unsupported"


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


_FAKE_PYBOY_SOURCES = {
    "__init__.py": '__version__ = "2.7.0"\n__pokered_harness_revision__ = "' + "c" * 40 + '"\n',
    "pyboy.py": "from pyboy import utils  # noqa: F401\n",
    "utils.py": "cython_compiled = True\n",
    "link.py": "LINK = True\n",
    "core/__init__.py": "",
    "core/mb.py": "MB = True\n",
    "core/serial.py": "SERIAL = True\n",
    "core/cpu.py": "CPU = True\n",
}


def _write_fake_pyboy(root: Path) -> Path:
    """Create a stand-in installed ``pyboy`` package for the native probe."""

    package = root / "pyboy"
    for relative, content in _FAKE_PYBOY_SOURCES.items():
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return package


def _run_native_probe(fake_root: Path, cwd: Path) -> dict:
    """Execute the real probe script against the stand-in installed runtime."""

    env = dict(os.environ)
    env["PYTHONPATH"] = str(fake_root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    probe = runner._NATIVE_PROBE % {"modules": runner._RUNTIME_MODULES}
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=cwd,
        env=env,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout)


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


class _StubProcess:
    """The minimum of ``Popen`` that durable job ownership needs to record."""

    def __init__(self, pid: int):
        self.pid = pid


def _stale_job_dir(tmp_path: Path, record: dict | None) -> tuple[dict, Path, Path]:
    """Build a stale lease whose holder is gone, optionally with a job record."""

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
    if record is not None:
        (job_dir / runner._JOB_RUN_RECORD_NAME).write_text(json.dumps(record), encoding="utf-8")
    declaration = make_declaration(
        reservation={
            "allocation_id": "test-runner",
            "job_dir": str(job_dir),
            "descriptor_path": str(descriptor_path),
            "descriptor_sha256": runner._sha256_of_file(descriptor_path),
        }
    )
    return declaration, descriptor_path, job_dir


def _job_record(**overrides) -> dict:
    record = {
        "record_version": 1,
        "job_pid": 2**31 - 1,
        "job_start_time": "0",
        "job_process_group": 2**31 - 1,
        "job_session": 2**31 - 1,
        "containment_confirmed": True,
        "recorded_at": "2026-01-01T00:00:00Z",
    }
    record.update(overrides)
    return record


def test_recover_refuses_while_the_recorded_job_still_runs(tmp_path: Path):
    """A dead holder is not free capacity while its recorded job is alive."""

    record = _job_record(
        job_pid=os.getpid(),
        job_start_time=runner._process_start_time(os.getpid()),
        job_process_group=os.getpid(),
        job_session=os.getpid(),
    )
    declaration, descriptor_path, _job_dir = _stale_job_dir(tmp_path, record)
    status, message = runner._recover_allocation(declaration, tmp_path)
    assert status == "blocked"
    assert "still running" in message
    assert descriptor_path.exists()


def test_recover_refuses_unconfirmed_containment_it_cannot_verify(tmp_path: Path, monkeypatch):
    """A record that never proved containment keeps the lease unless proven now."""

    declaration, descriptor_path, job_dir = _stale_job_dir(
        tmp_path, _job_record(containment_confirmed=False)
    )
    monkeypatch.setattr(runner, "_child_subreaper_state", lambda: 0)
    monkeypatch.setattr(runner, "_contain_adopted_descendants", lambda *a, **k: (True, []))
    status, message = runner._recover_allocation(declaration, tmp_path)
    assert status == "blocked"
    assert "never confirmed descendant containment" in message
    assert descriptor_path.exists()
    assert (job_dir / runner._JOB_RUN_RECORD_NAME).exists()


def test_recover_refuses_when_adopted_descendants_survive(tmp_path: Path, monkeypatch):
    """An orphaned survivor keeps the lease even when the holder is gone."""

    declaration, descriptor_path, _job_dir = _stale_job_dir(tmp_path, _job_record())
    monkeypatch.setattr(runner, "_contain_adopted_descendants", lambda *a, **k: (False, [4242]))
    status, message = runner._recover_allocation(declaration, tmp_path)
    assert status == "blocked"
    assert "4242" in message
    assert descriptor_path.exists()


def test_recover_removes_state_for_a_confirmed_contained_job(tmp_path: Path, monkeypatch):
    """A recorded, confirmed, no-longer-running job releases the stale state."""

    declaration, descriptor_path, _job_dir = _stale_job_dir(tmp_path, _job_record())
    monkeypatch.setattr(runner, "_contain_adopted_descendants", lambda *a, **k: (True, []))
    status, message = runner._recover_allocation(declaration, tmp_path)
    assert status == "ok", message
    assert not descriptor_path.exists()


def test_release_refuses_when_the_job_record_never_confirmed_containment(tmp_path: Path):
    """The recorded holder only releases a lease it proved was contained."""

    if not _LOCK_OBSERVATION_SUPPORTED:
        pytest.skip("kernel lock table is not observable in this sandbox")
    declaration, _facts = held_reservation(tmp_path, "cgroup-quota")
    job_dir = Path(declaration["reservation"]["job_dir"])
    (job_dir / runner._JOB_RUN_RECORD_NAME).write_text(
        json.dumps(_job_record(containment_confirmed=False)), encoding="utf-8"
    )
    status, message = runner._release_allocation(declaration, tmp_path)
    assert status == "blocked"
    assert "never confirmed descendant containment" in message
    assert Path(declaration["reservation"]["descriptor_path"]).exists()


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


def _write_detached_grandchild_command(
    tmp_path: Path, exit_code: int, pidfile: Path, *, parent_sleeps: bool = False
) -> list[str]:
    """Build a command whose grandchild detaches into its own session.

    ``start_new_session=True`` moves the grandchild out of the command's process
    group, which is the round-6 finding: the process-group sweep cannot see it,
    so it survives even though the command's group is empty.

    ``parent_sleeps`` keeps the parent alive after the detached grandchild
    starts, so the command itself hits its deadline instead of exiting first.
    """

    grandchild = tmp_path / "detached_grandchild.py"
    grandchild.write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        f"Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    if pidfile.exists():
        pidfile.unlink()
    tail = "time.sleep(120)\n" if parent_sleeps else ""
    code = (
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "with open(os.devnull, 'wb') as null:\n"
        f"    subprocess.Popen([sys.executable, {str(grandchild)!r}], "
        "stdin=null, stdout=null, stderr=null, start_new_session=True)\n"
        "deadline = time.monotonic() + 20\n"
        f"while not Path({str(pidfile)!r}).exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.05)\n"
        f"{tail}"
        f"sys.exit({exit_code})\n"
    )
    return [sys.executable, "-c", code]


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
