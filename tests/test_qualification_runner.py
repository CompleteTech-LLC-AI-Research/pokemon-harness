"""Declaration, reservation and resource-admission tests.

Split from ``tests/test_qualification_runner.py`` for issue #112 with no
behavior change: every test below is preserved verbatim.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from scripts import qualification_runner as runner
from tests._qualification_runner_support import (
    _HELD_FDS,
    held_reservation,
    make_declaration,
    make_facts,
    rewrite_descriptor,
    statuses,
)


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


def test_evaluate_resources_fails_on_read_only_shm_with_zero_minimum(tmp_path: Path):
    """A zero minimum must not opt out of the shared-memory writability check.

    The round-13 finding: ``shm_bytes_min=0`` made the admission ``skipped``, so
    a read-only shared-memory mount still admitted the job.  Availability and
    writability are host facts the contract requires independently of the size
    minimum.
    """

    declaration, facts = held_reservation(tmp_path, "cgroup-quota", shm_bytes_min=0)
    facts.shm_writable = False
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    assert statuses(results)["shm"] == "fail"


def test_evaluate_resources_admits_writable_shm_with_zero_minimum(tmp_path: Path):
    """A writable shared-memory mount is admitted when no size minimum is set."""

    declaration, facts = held_reservation(tmp_path, "cgroup-quota", shm_bytes_min=0)
    facts.shm_writable = True
    results = runner.evaluate_resources(declaration, facts, tmp_path)
    shm = next(item for item in results if item.name == "shm")
    assert shm.status == "ok"
    assert "no size minimum" in shm.detail


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
