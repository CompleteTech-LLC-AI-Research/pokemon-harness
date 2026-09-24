"""ROM-free tests for the versioned capacity policy, admission, and telemetry."""

from __future__ import annotations

import contextlib
import importlib.util
import inspect
import io
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import gate_capacity

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "gate_capacity_production_gate", ROOT / "scripts" / "production_gate.py"
)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)


def make_facts(**overrides):
    """A complete, valid fact mapping with deterministic defaults."""

    data = {
        "platform": "Linux test",
        "logical_cpus": 8,
        "affinity_cpus": [0, 1, 2, 3],
        "affinity_count": 4,
        "affinity_supported": True,
        "cgroup_version": "v2",
        "cpu_quota_cores": 4.0,
        "cpu_weight": 100,
        "cpu_throttled": {"nr_throttled": 0},
        "cpu_quota_status": "limited",
        "cgroup_cpu_some_avg300": 1.0,
        "cgroup_visibility": ["visible hierarchy only"],
        "memory_total_bytes": 16 * 1024**3,
        "memory_available_bytes": 8 * 1024**3,
        "load_average": [0.1, 0.2, 0.3],
        "psi_cpu_some_avg300": 1.0,
        "repo_disk_free_bytes": 100 * 1024**3,
        "temp_disk_free_bytes": 100 * 1024**3,
        "shm_path": "/dev/shm",
        "shm_size_bytes": 1024**3,
        "shm_writable": True,
        "unsupported": [],
    }
    data.update(overrides)
    return data


def make_policy(**overrides):
    values = {
        "policy_version": 2,
        "runner_id": "runner-86",
        "effective_cpus": 4,
        "max_concurrent_pairs": 2,
        "memory_bytes_min": 1024,
        "disk_free_bytes_min": 1024,
        "observation_seconds": 0.0,
        "admission_deadline_seconds": 10.0,
        "max_system_some_avg300": 20.0,
        "max_cgroup_some_avg300": 20.0,
        "max_load_per_cpu": 1.0,
        "measurement_sha256": "a" * 64,
    }
    values.update(overrides)
    return gate_capacity.CapacityPolicy(**values)


def write_tier_config(
    root: Path,
    *,
    markers: dict[str, tuple[str, ...]] | None = None,
    tier_rules: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] | None = None,
) -> None:
    """Write a minimal, self-contained ``tests/_tier_config.py``.

    ``markers`` maps a test key (``"module.py::test_name"`` or just a module
    name) to its markers; ``tier_rules`` maps a tier to its
    ``(required, excluded)`` marker sets.  The generated module exposes the
    same ``classify_test``/``tier_matches`` contract the gate loads.
    """

    markers = markers or {"test_rom_boot.py": ("real_rom",)}
    tier_rules = tier_rules or {"local": (("real_rom",), ())}
    config = root / "tests" / "_tier_config.py"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "from __future__ import annotations\n"
        f"_MARKERS = {markers!r}\n"
        f"_RULES = {tier_rules!r}\n"
        "def classify_test(path, test_name):\n"
        "    import os\n"
        "    filename = os.path.basename(str(path))\n"
        "    key = filename + '::' + str(test_name)\n"
        "    marks = _MARKERS.get(key) or _MARKERS.get(filename)\n"
        "    if marks is None:\n"
        "        raise ValueError('test module is not classified')\n"
        "    return frozenset(marks)\n"
        "def tier_matches(name, markers):\n"
        "    rule = _RULES.get(name)\n"
        "    if rule is None:\n"
        "        return False\n"
        "    required, excluded = rule\n"
        "    marks = set(markers)\n"
        "    return all(item in marks for item in required) and not any(\n"
        "        item in marks for item in excluded\n"
        "    )\n",
        encoding="utf-8",
    )


class SequenceSampler:
    def __init__(self, samples):
        self.samples = list(samples)
        self.calls = 0

    def __call__(self):
        sample = self.samples[min(self.calls, len(self.samples) - 1)]
        self.calls += 1
        return sample


class FakeClock:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def test_policy_round_trips_and_rejects_unknown_or_missing_fields():
    policy = make_policy()
    assert gate_capacity.capacity_policy_from_dict(policy.as_dict()) == policy
    assert gate_capacity.validate_policy(policy) == []

    with pytest.raises(ValueError, match="missing required fields"):
        gate_capacity.capacity_policy_from_dict({"policy_version": 2})
    with pytest.raises(ValueError, match="unknown fields"):
        gate_capacity.capacity_policy_from_dict({**policy.as_dict(), "extra": 1})
    with pytest.raises(TypeError, match="JSON object"):
        gate_capacity.capacity_policy_from_dict(["not", "an", "object"])


@pytest.mark.parametrize(
    "overrides, fragment",
    (
        ({"policy_version": 99}, "policy_version"),
        ({"runner_id": ""}, "runner_id"),
        ({"effective_cpus": 0}, "effective_cpus"),
        ({"max_concurrent_pairs": -1}, "max_concurrent_pairs"),
        ({"memory_bytes_min": -1}, "memory_bytes_min"),
        ({"disk_free_bytes_min": -1}, "disk_free_bytes_min"),
        ({"observation_seconds": -1}, "observation_seconds"),
        ({"admission_deadline_seconds": 0}, "admission_deadline_seconds"),
    ),
)
def test_policy_validation_is_explicit(overrides, fragment):
    problems = gate_capacity.validate_policy(make_policy(**overrides))
    assert any(fragment in problem for problem in problems)


def test_load_policy_reports_missing_and_malformed(tmp_path):
    missing, error = gate_capacity.load_capacity_policy(tmp_path / "absent.json")
    assert missing is None and "not found" in error

    malformed = tmp_path / "bad.json"
    malformed.write_text("{not json", encoding="utf-8")
    loaded, error = gate_capacity.load_capacity_policy(malformed)
    assert loaded is None and "not valid JSON" in error

    invalid = tmp_path / "invalid.json"
    invalid.write_text(
        json.dumps({**make_policy().as_dict(), "effective_cpus": 0}), encoding="utf-8"
    )
    loaded, error = gate_capacity.load_capacity_policy(invalid)
    assert loaded is None and "effective_cpus" in error

    valid = tmp_path / "policy.json"
    valid.write_text(json.dumps(make_policy().as_dict()), encoding="utf-8")
    loaded, error = gate_capacity.load_capacity_policy(valid)
    assert error is None and loaded == make_policy()


def test_sample_facts_reuses_qualification_runner_and_stamps_time(tmp_path, monkeypatch):
    captured = {}

    def fake_collect(repo_root, temp_root=None):
        captured["args"] = (repo_root, temp_root)
        return make_facts()

    monkeypatch.setattr(gate_capacity.qualification_runner, "collect_facts", fake_collect)
    sample = gate_capacity.sample_facts(tmp_path)

    assert captured["args"][0] == tmp_path
    assert sample.status == "ok"
    assert sample.monotonic_seconds > 0
    assert sample.utc_timestamp
    assert sample.facts["platform"] == "Linux test"


def test_sample_facts_marks_collection_failure_explicit(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise OSError("procfs unavailable")

    monkeypatch.setattr(gate_capacity.qualification_runner, "collect_facts", boom)
    sample = gate_capacity.sample_facts(tmp_path)
    assert sample.status == "failed"
    assert "procfs unavailable" in sample.problems[0]


@pytest.mark.parametrize(
    "overrides, expected",
    (
        ({}, "ok"),
        ({"affinity_count": 2, "affinity_cpus": [0, 1]}, "blocked"),
        ({"affinity_count": 4, "cpu_quota_cores": 1.0}, "blocked"),
        ({"affinity_supported": False, "affinity_count": 0}, "unsupported"),
        ({"memory_available_bytes": None}, "unsupported"),
        ({"memory_available_bytes": 10}, "blocked"),
        ({"repo_disk_free_bytes": None}, "unsupported"),
        ({"repo_disk_free_bytes": 10}, "blocked"),
    ),
)
def test_evaluate_capacity_reports_explicit_outcomes(overrides, expected):
    status, reasons = gate_capacity.evaluate_capacity(make_policy(), make_facts(**overrides))
    assert status == expected
    if expected != "ok":
        assert reasons


def test_evaluate_capacity_rejects_malformed_sample():
    status, reasons = gate_capacity.evaluate_capacity(make_policy(), {"platform": "x"})
    assert status == "unsupported"
    assert any("malformed" in reason for reason in reasons)


def test_admission_admits_max_pairs_and_promotes_queued_owner():
    clock = FakeClock()
    admission = gate_capacity.CapacityAdmission(make_policy(max_concurrent_pairs=2), clock=clock)
    assert admission.admit("pair-1").status == "ok"
    assert admission.admit("pair-2").status == "ok"
    assert admission.admit("pair-3").status == "queued"
    assert admission.admit("pair-4").status == "queued"
    assert admission.admitted_count == 2

    promoted = admission.release("pair-1")
    assert promoted is not None and promoted.status == "ok" and promoted.pair_id == "pair-3"
    assert promoted.promoted is True
    assert admission.admit("pair-5").status == "queued"

    promoted = admission.release("pair-2")
    assert promoted is not None and promoted.pair_id == "pair-4"
    assert admission.admitted_count == 4
    assert sorted(admission.active) == ["pair-3", "pair-4"]


def test_admission_never_lets_an_active_owner_be_paused_or_deadline_enlarged():
    clock = FakeClock()
    admission = gate_capacity.CapacityAdmission(make_policy(max_concurrent_pairs=1), clock=clock)
    owner = admission.admit("owner")
    assert owner.status == "ok"
    clock.advance(5.0)
    assert admission.admit("waiter").status == "queued"
    # The active owner decision is untouched by the waiting pair.
    assert admission.active["owner"] is owner
    assert admission.active["owner"].virtual_seconds == 0.0


def test_admission_expires_without_infinite_wait():
    clock = FakeClock()
    admission = gate_capacity.CapacityAdmission(
        make_policy(max_concurrent_pairs=1, admission_deadline_seconds=3.0), clock=clock
    )
    assert admission.admit("owner").status == "ok"
    assert admission.admit("waiter").status == "queued"
    clock.advance(3.5)
    expired = admission.admit("waiter")
    assert expired.status == "expired"
    assert admission.queued == []
    # A second waiting pair also expires once its deadline elapses.
    assert admission.admit("later").status == "queued"
    clock.advance(4.0)
    assert admission.admit("later").status == "expired"


def test_expire_waiting_marks_queued_pairs():
    clock = FakeClock()
    admission = gate_capacity.CapacityAdmission(
        make_policy(max_concurrent_pairs=1, admission_deadline_seconds=1.0), clock=clock
    )
    admission.admit("owner")
    admission.admit("waiter")
    clock.advance(2.0)
    expired = admission.expire_waiting()
    assert [decision.pair_id for decision in expired] == ["waiter"]
    assert expired[0].status == "expired"


def test_admission_blocks_when_capacity_unavailable():
    clock = FakeClock()
    admission = gate_capacity.CapacityAdmission(
        make_policy(), clock=clock, availability=lambda: ("blocked", "no CPUs")
    )
    decision = admission.admit("pair-1")
    assert decision.status == "queued"
    assert decision.reason == "no CPUs"
    assert admission.admitted_count == 0


def test_provider_change_stops_admission(tmp_path):
    session = gate_capacity.CapacitySession(
        make_policy(),
        repo_root=tmp_path,
        sampler=SequenceSampler(
            [
                make_facts(affinity_count=4, cpu_quota_cores=4.0),
                make_facts(affinity_count=1, affinity_cpus=[0], cpu_quota_cores=1.0),
            ]
        ),
    )
    assert session.begin() == "ok"
    assert session.admission.admit("pair-1").status == "ok"
    assert session.observe() == "blocked"
    assert session.admission.admit("pair-2").status == "queued"
    assert session.admission.admitted_count == 1


def test_inaccessible_and_malformed_telemetry_are_explicit(tmp_path):
    def boom():
        raise RuntimeError("telemetry-unavailable")

    failing = gate_capacity.CapacitySession(make_policy(), repo_root=tmp_path, sampler=boom)
    assert failing.begin() == "unsupported"
    assert failing.admission.admit("pair-1").status == "queued"

    malformed = gate_capacity.CapacitySession(
        make_policy(), repo_root=tmp_path, sampler=lambda: {"platform": "x"}
    )
    assert malformed.begin() == "unsupported"
    assert malformed.telemetry.samples[0].status == "malformed"
    assert malformed.admission.admit("pair-1").status == "queued"


def test_telemetry_records_lifecycle_and_durable_reference(tmp_path):
    recorder = gate_capacity.TelemetryRecorder(
        repo_root=tmp_path, policy=make_policy(), sampler=lambda: make_facts()
    )
    assert recorder.observe().status == "ok"
    recorder.mark("r1", "running")
    recorder.mark("r1", "completed")
    recorder.mark("r2", "not_started")
    recorder.mark("r3", "running")
    recorder.mark("r3", "interrupted")
    assert recorder.lifecycle == {
        "running": 0,
        "completed": 1,
        "interrupted": 1,
        "not_started": 1,
    }
    with pytest.raises(ValueError):
        recorder.mark("r4", "unknown-state")
    reference = recorder.reference()
    assert reference["algorithm"] == "sha256"
    assert len(reference["sha256"]) == 64
    assert reference["sample_count"] == 1
    assert reference["collection_failures"] == 0


def test_capability_assumptions_are_visible_in_the_report(tmp_path):
    session = gate_capacity.CapacitySession(
        make_policy(),
        repo_root=tmp_path,
        sampler=lambda: make_facts(
            affinity_supported=False,
            affinity_count=0,
            unsupported=["affinity", "cgroup"],
        ),
    )
    assert session.begin() == "unsupported"
    report = session.report()
    assert report["availability"]["status"] == "unsupported"
    assert "affinity" in report["capability_assumptions"]
    assert "cgroup" in report["capability_assumptions"]
    text = gate_capacity.render_capacity_text(report)
    assert "capability-assumptions" in text
    assert "availability: unsupported" in text


def test_overall_outcome_zero_admitted_is_never_ok():
    assert gate_capacity.overall_outcome(admitted=0) == "blocked"
    assert gate_capacity.overall_outcome(admitted=1, failed=1) == "failed"
    assert gate_capacity.overall_outcome(admitted=1, collection_failures=1) == "unsupported"
    assert gate_capacity.overall_outcome(admitted=1, availability="blocked") == "blocked"
    assert gate_capacity.overall_outcome(admitted=1, interrupted=1) == "blocked"
    assert gate_capacity.overall_outcome(admitted=1, not_started=1) == "blocked"
    assert gate_capacity.overall_outcome(admitted=1) == "ok"


def test_session_report_with_zero_admitted_is_not_ok(tmp_path):
    session = gate_capacity.CapacitySession(
        make_policy(),
        repo_root=tmp_path,
        sampler=lambda: make_facts(affinity_count=1, affinity_cpus=[0], cpu_quota_cores=1.0),
    )
    session.begin()
    report = session.report()
    assert report["status"] == "blocked"
    assert report["admission"]["admitted"] == 0


def test_telemetry_failure_cannot_crash_cleanup_or_erase_failure(tmp_path):
    def boom():
        raise RuntimeError("telemetry-unavailable")

    recorder = gate_capacity.TelemetryRecorder(
        repo_root=tmp_path, policy=make_policy(), sampler=boom
    )
    sample = recorder.observe()
    assert sample.status == "failed"
    assert "telemetry-unavailable" in sample.problems[0]
    assert recorder.collection_failures == 1
    recorder.mark("pair-1", "running")
    recorder.mark("pair-1", "completed")
    assert recorder.lifecycle == {
        "running": 0,
        "completed": 1,
        "interrupted": 0,
        "not_started": 0,
    }

    detail = gate.FailureDetail(
        iteration=1,
        nodeid="tests/test_x.py::test_y",
        outcome="failed",
        reason="ORIGINAL-FAILURE",
        original_chars=len("ORIGINAL-FAILURE"),
        omitted_chars=0,
        truncated=False,
        nodeid_original_chars=len("tests/test_x.py::test_y"),
        nodeid_omitted_chars=0,
    )
    tier = gate.TierResult(
        name="unit",
        description="unit",
        expression="unit",
        required=True,
        status="FAIL",
        counts=gate.Counts(total=1, failed=1),
        failure_details=[detail],
    )
    session = gate_capacity.CapacitySession(make_policy(), repo_root=tmp_path, sampler=boom)
    session.begin()
    payload = gate.build_evidence_payload(
        project_root=tmp_path,
        rom_root=tmp_path / "rom",
        fixture_root=tmp_path / "fixtures",
        runtime={},
        assets=[],
        collections=[],
        tiers=[tier],
        gate_problems=[],
        overall="FAIL",
        capacity=session.report(failed=1),
    )
    assert payload["tiers"][0]["failure_details"][0]["reason"] == "ORIGINAL-FAILURE"
    assert payload["capacity"]["collection_failures"] >= 1
    assert payload["capacity"]["status"] == "failed"


def _matrix_report(nodeid, outcome="passed", reason=""):
    counts = {
        "total": 1,
        "passed": int(outcome == "passed"),
        "failed": int(outcome == "failed"),
        "skipped": int(outcome == "skipped"),
        "xfailed": 0,
        "xpassed": 0,
        "errors": 0,
    }
    return {
        "collection_only": False,
        "counts": counts,
        "tests": [
            {
                "nodeid": nodeid,
                "outcome": outcome,
                "when": "call",
                "reason": reason,
                "was_xfail": False,
            }
        ],
        "collection_errors": [],
        "collection_skips": [],
        "collected": 1,
        "nodeids": [nodeid],
        "exitstatus": int(outcome == "failed"),
    }


class FakeMatrixPopen:
    """Deterministic matrix child; node IDs ending in ``fail`` fail."""

    def __init__(self, command, *, env, **kwargs):
        del kwargs
        self.pid = 999999999
        nodeid = command[3]
        self.outcome = "failed" if nodeid.endswith("fail]") else "passed"
        self.returncode = int(self.outcome == "failed")
        reason = "runtime-identity-marker" if self.outcome == "failed" else ""
        report = _matrix_report(nodeid, self.outcome, reason=reason)
        Path(env["POKERED_GATE_REPORT"]).write_text(json.dumps(report), encoding="utf-8")
        Path(env["POKERED_GATE_PROGRESS_REPORT"]).write_text(json.dumps(report), encoding="utf-8")
        self.stdout = io.StringIO(f"MATRIX-OUTPUT {nodeid}\n")

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_rising_pressure_stops_admission_and_retains_existing_failure(tmp_path, monkeypatch):
    import time

    class SlowFailingChild(FakeMatrixPopen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.finish = time.monotonic() + 0.05

        def poll(self):
            return self.returncode if time.monotonic() >= self.finish else None

    monkeypatch.setattr(gate.subprocess, "Popen", SlowFailingChild)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1, admission_deadline_seconds=0.05),
        repo_root=tmp_path,
        sampler=SequenceSampler(
            [
                make_facts(),
                make_facts(),
                make_facts(psi_cpu_some_avg300=100),
            ]
        ),
    )
    nodeids = (
        "tests/test_matrix.py::test_pair[aaa-fail]",
        "tests/test_matrix.py::test_pair[zzz-block-a]",
        "tests/test_matrix.py::test_pair[zzz-block-b]",
    )
    result = gate.run_matrix_tier(
        name="trade",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        timeout_override=5,
        report_directory=tmp_path,
        required_nodeids=nodeids,
        matrix_workers=2,
        capacity_session=session,
    )

    assert result.status == "FAIL"
    cases = {case.nodeid: case for case in result.case_results}
    assert cases[nodeids[0]].status == "FAIL"
    assert "MATRIX-OUTPUT" in cases[nodeids[0]].output_tail
    assert cases[nodeids[1]].status == "BLOCKED"
    assert cases[nodeids[2]].status == "BLOCKED"
    reasons = {detail.nodeid: detail.reason for detail in result.failure_details}
    assert any("runtime-identity-marker" in reason for reason in reasons.values())
    lifecycle = session.telemetry.lifecycle
    assert lifecycle["completed"] == 1
    assert lifecycle["not_started"] == 2


def test_matrix_workers_are_capped_by_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1), repo_root=tmp_path, sampler=lambda: make_facts()
    )
    nodeids = tuple(f"tests/test_matrix.py::test_pair[pass-{index}]" for index in range(3))
    result = gate.run_matrix_tier(
        name="trade",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        timeout_override=5,
        report_directory=tmp_path,
        required_nodeids=nodeids,
        matrix_workers=3,
        capacity_session=session,
    )
    assert result.status == "PASS"
    assert "--matrix-workers=1" in result.command
    assert all(case.status == "PASS" for case in result.case_results)


def test_capacity_tier_reason_skips_asset_free_tiers(tmp_path):
    session = gate_capacity.CapacitySession(
        make_policy(admission_deadline_seconds=0.05),
        repo_root=tmp_path,
        sampler=lambda: make_facts(affinity_count=1, affinity_cpus=[0], cpu_quota_cores=1.0),
    )
    assert gate._capacity_tier_reason(None, "trade") == ""
    assert gate._capacity_tier_reason(session, "unit") == ""
    assert gate._capacity_tier_reason(session, "timing") == ""
    reason = gate._capacity_tier_reason(session, "trade")
    assert "capacity" in reason
    assert "blocked" in reason


def _runtime_result(mode, tier_status="PASS"):
    return gate.RuntimeGateResult(
        mode=mode,
        runtime={"pyboy_mode": mode},
        collections=[gate.CollectionResult("python-module", [], "PASS", 0)],
        fixture_manifest={"status": "PASS", "mode": "schema"},
        matrix_audit={"status": "PASS"},
        tiers=[
            gate.TierResult(
                name="unit",
                description="unit",
                expression="unit",
                required=True,
                status=tier_status,
                counts=gate.Counts(total=1, passed=int(tier_status == "PASS")),
            )
        ],
    )


@pytest.fixture
def runtime_gate_stubs(tmp_path, monkeypatch):
    calls = []

    def environment(*_args, runtime_mode):
        return {"TEST_MODE": runtime_mode}

    def probe(_python, _root, env):
        return {"pyboy_mode": env["TEST_MODE"]}

    def tier(**kwargs):
        calls.append(kwargs)
        return gate.TierResult(
            name=kwargs["name"],
            description=kwargs["name"],
            expression=kwargs["name"],
            required=True,
            status="PASS",
            counts=gate.Counts(total=1, passed=1),
        )

    monkeypatch.setattr(gate, "build_test_environment", environment)
    monkeypatch.setattr(gate, "probe_runtime", probe)
    monkeypatch.setattr(gate, "runtime_problems", lambda _root, _runtime, **_kwargs: [])
    monkeypatch.setattr(gate, "environment_policy_problems", lambda **_kwargs: [])
    monkeypatch.setattr(
        gate,
        "run_collection_preflight",
        lambda **_kwargs: [
            gate.CollectionResult("python-module", [], "PASS", 0, nodeids=()),
            gate.CollectionResult("pytest-console", [], "PASS", 0, nodeids=()),
        ],
    )
    monkeypatch.setattr(
        gate,
        "run_fixture_manifest_validation",
        lambda **_kwargs: {"status": "PASS", "mode": "byte"},
    )
    monkeypatch.setattr(gate, "fixture_manifest_input_problems", lambda *_a, **_k: [])
    monkeypatch.setattr(gate, "fixture_manifest_provenance_problems", lambda *_a: [])
    monkeypatch.setattr(
        gate,
        "run_matrix_collection_audit",
        lambda **_kwargs: {
            "status": "PASS",
            "structural_pass": True,
            "acceptance_matrix_complete": True,
            "audited_nodeids": {},
            "groups": {},
        },
    )
    monkeypatch.setattr(gate, "run_tier", tier)
    return calls


def test_present_policy_blocks_real_tier_but_runs_unit(tmp_path, runtime_gate_stubs):
    session = gate_capacity.CapacitySession(
        make_policy(admission_deadline_seconds=0.05),
        repo_root=tmp_path,
        sampler=lambda: make_facts(affinity_count=1, affinity_cpus=[0], cpu_quota_cores=1.0),
    )
    results = gate.run_runtime_gates(
        runtime_mode="source",
        project_root=tmp_path,
        python_executable=tmp_path / "python",
        rom_root=tmp_path / "rom",
        fixture_root=tmp_path / "fixtures",
        expected_sha1={},
        assets=[],
        selected=["unit", "trade"],
        required_tests_by_tier={},
        required_nodeids_by_tier={},
        capacity_session=session,
    )
    result = results[0]
    statuses = {tier.name: tier.status for tier in result.tiers}
    assert statuses["unit"] == "PASS"
    assert statuses["trade"] == "BLOCKED"
    trade = next(tier for tier in result.tiers if tier.name == "trade")
    assert "capacity" in trade.reason
    assert any(call for call in runtime_gate_stubs if call["name"] == "unit")


def test_absent_policy_preserves_real_tier_dispatch(tmp_path, runtime_gate_stubs):
    results = gate.run_runtime_gates(
        runtime_mode="source",
        project_root=tmp_path,
        python_executable=tmp_path / "python",
        rom_root=tmp_path / "rom",
        fixture_root=tmp_path / "fixtures",
        expected_sha1={},
        assets=[],
        selected=["unit", "trade"],
        required_tests_by_tier={},
        required_nodeids_by_tier={},
    )
    result = results[0]
    assert all(tier.status == "PASS" for tier in result.tiers)
    assert {call["name"] for call in runtime_gate_stubs} == {"unit", "trade"}


def test_cancelled_tiers_skip_capacity_admission_waits(tmp_path, monkeypatch):
    """A reviewed regression: cancellation must not spend admission deadlines."""

    import time

    calls = []

    def tier(**kwargs):
        calls.append(kwargs["name"])
        if kwargs["name"] == "smoke":
            return gate.TierResult(
                name="smoke",
                description="smoke",
                expression="smoke",
                required=True,
                status="INTERRUPTED",
                counts=gate.Counts(total=0),
            )
        return gate.TierResult(
            name=kwargs["name"],
            description=kwargs["name"],
            expression=kwargs["name"],
            required=True,
            status="PASS",
            counts=gate.Counts(total=1, passed=1),
        )

    monkeypatch.setattr(
        gate, "build_test_environment", lambda *_a, runtime_mode: {"M": runtime_mode}
    )
    monkeypatch.setattr(gate, "probe_runtime", lambda _p, _r, env: {"pyboy_mode": env["M"]})
    monkeypatch.setattr(gate, "runtime_problems", lambda *_a, **_k: [])
    monkeypatch.setattr(gate, "environment_policy_problems", lambda **_k: [])
    monkeypatch.setattr(
        gate,
        "run_collection_preflight",
        lambda **_k: [
            gate.CollectionResult("python-module", [], "PASS", 0, nodeids=()),
            gate.CollectionResult("pytest-console", [], "PASS", 0, nodeids=()),
        ],
    )
    monkeypatch.setattr(
        gate, "run_fixture_manifest_validation", lambda **_k: {"status": "PASS", "mode": "byte"}
    )
    monkeypatch.setattr(gate, "fixture_manifest_input_problems", lambda *_a, **_k: [])
    monkeypatch.setattr(gate, "fixture_manifest_provenance_problems", lambda *_a: [])
    monkeypatch.setattr(
        gate,
        "run_matrix_collection_audit",
        lambda **_k: {
            "status": "PASS",
            "structural_pass": True,
            "acceptance_matrix_complete": True,
            "audited_nodeids": {},
            "groups": {},
        },
    )
    monkeypatch.setattr(gate, "run_tier", tier)

    admissions = []

    class CountingSession(gate_capacity.CapacitySession):
        def wait_until_available(self, **_kwargs):
            admissions.append(time.monotonic())
            return super().wait_until_available(**_kwargs)

    session = CountingSession(
        make_policy(admission_deadline_seconds=1.0),
        repo_root=tmp_path,
        sampler=lambda: make_facts(),
    )
    started = time.monotonic()
    results = gate.run_runtime_gates(
        runtime_mode="source",
        project_root=tmp_path,
        python_executable=tmp_path / "python",
        rom_root=tmp_path / "rom",
        fixture_root=tmp_path / "fixtures",
        expected_sha1={},
        assets=[],
        selected=["local", "remote"],
        required_tests_by_tier={},
        required_nodeids_by_tier={},
        capacity_session=session,
        early_smoke=True,
        fail_fast=True,
    )
    elapsed = time.monotonic() - started
    statuses = {tier.name: tier.status for tier in results[0].tiers}
    smoke = next(t for t in results[0].tiers if t.name == "smoke")
    assert statuses["smoke"] == "INTERRUPTED", smoke.reason
    # The remaining real-ROM tier cannot execute after cancellation, so it must
    # not pay a capacity admission deadline before reporting NOT_STARTED.
    assert statuses["local"] == "NOT_STARTED"
    assert statuses["remote"] == "NOT_STARTED"
    # Only the smoke tier can still execute; every cancelled tier must skip the
    # admission deadline entirely instead of waiting once per runtime.
    assert len(admissions) == 1, admissions
    assert elapsed < 1.0


def test_cancelled_admission_wait_records_rows_and_keeps_prior_results(
    tmp_path, runtime_gate_stubs
):
    """Regression: a SIGINT while waiting for capacity must not lose evidence.

    A reviewer delivered a real SIGINT during the admission wait and observed
    ``KeyboardInterrupt`` escaping the gate: no report, and the pending row
    absent from lifecycle accounting.  Cancellation has to stop dispatch,
    account for the rows that never ran, and preserve the tiers that already
    finished.
    """

    rows = (
        "tests/test_matrix.py::test_pair[red-blue]",
        "tests/test_matrix.py::test_pair[blue-red]",
    )

    class InterruptingSession(gate_capacity.CapacitySession):
        def wait_until_available(self, **_kwargs):
            if not self.admission.decisions:
                raise KeyboardInterrupt
            return super().wait_until_available(**_kwargs)

    session = InterruptingSession(
        make_policy(admission_deadline_seconds=5.0),
        repo_root=tmp_path,
        sampler=lambda: make_facts(),
    )
    try:
        results = gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=tmp_path / "python",
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["unit", "trade"],
            required_tests_by_tier={},
            required_nodeids_by_tier={"trade": rows},
            capacity_session=session,
        )
    except KeyboardInterrupt:
        pytest.fail("cancellation escaped the gate instead of finalizing evidence")

    statuses = {tier.name: tier.status for tier in results[0].tiers}
    assert statuses["unit"] == "PASS", results[0].tiers
    assert statuses["trade"] == "INTERRUPTED", results[0].tiers
    # Every row the cancelled tier would have dispatched is terminal, and no
    # queued waiter survives to be promoted into a later slot.
    assert session.telemetry.lifecycle["interrupted"] == len(rows)
    assert session.admission.queued == []
    recorded = {decision.pair_id.split(":", 2)[-1] for decision in session.admission.decisions}
    assert recorded == set(rows)
    keys = {
        decision.pair_id.split(":", 2)[-1]: decision.pair_id
        for decision in session.admission.decisions
    }
    assert all(session.admission.state(keys[nodeid]) == "expired" for nodeid in rows)


def test_interrupt_during_capacity_observation_records_rows(tmp_path, runtime_gate_stubs):
    """Regression: cancellation during observation setup is not lost either."""

    rows = ("tests/test_matrix.py::test_pair[yellow-red]",)

    class ObservationInterrupt(gate_capacity.CapacitySession):
        def ensure_started(self):
            raise KeyboardInterrupt

    session = ObservationInterrupt(
        make_policy(admission_deadline_seconds=5.0),
        repo_root=tmp_path,
        sampler=lambda: make_facts(),
    )
    try:
        results = gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=tmp_path / "python",
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["trade"],
            required_tests_by_tier={},
            required_nodeids_by_tier={"trade": rows},
            capacity_session=session,
        )
    except KeyboardInterrupt:
        pytest.fail("cancellation escaped observation setup")

    tier = results[0].tiers[0]
    assert tier.name == "trade"
    assert tier.status == "INTERRUPTED", tier.reason
    assert session.telemetry.lifecycle["interrupted"] == len(rows)
    assert [decision.pair_id.split(":", 2)[-1] for decision in session.admission.decisions] == list(
        rows
    )


def test_sigint_during_observer_shutdown_keeps_completed_tier(tmp_path, monkeypatch):
    """Regression: a SIGINT during observer shutdown must keep the tier result.

    A reviewer delivered a real SIGINT while the capacity observer was being
    joined, *after* the tier had already finished and loaded its failure
    report.  The previous handlers replaced that executed tier with an empty
    ``INTERRUPTED`` row, so the loaded failure disappeared and the same planned
    row was registered twice (completed and interrupted).  The completed tier,
    its counts, and its failure details must survive, the cancellation must be
    recorded separately, and no row may be accounted for twice.
    """

    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    signalled: list[bool] = []
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    row = "tests/test_matrix.py::test_pair[aaa-fail]"

    def prepare(**kwargs):
        return gate.PreparedRuntimeGate(
            result=gate.RuntimeGateResult(
                mode=kwargs["mode"],
                runtime={"pyboy_mode": kwargs["mode"]},
                collections=[gate.CollectionResult("python-module", [], "PASS", 0, nodeids=(row,))],
                fixture_manifest={"status": "PASS"},
                matrix_audit={"status": "PASS"},
                tiers=[],
                gate_problems=[],
            ),
            environment={},
            required_problems=[],
        )

    monkeypatch.setattr(gate, "prepare_runtime_gate", prepare)

    class SignalOnShutdown(gate_capacity.CapacitySession):
        @contextlib.contextmanager
        def monitoring(self):
            with super().monitoring():
                yield
            if not signalled:
                # A genuine SIGINT, delivered only after the tier completed and
                # its report was loaded, exactly during observer teardown.
                signalled.append(True)
                os.kill(os.getpid(), signal.SIGINT)

    session = SignalOnShutdown(
        make_policy(max_concurrent_pairs=1),
        repo_root=tmp_path,
        sampler=lambda: make_facts(),
    )
    try:
        results = gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=tmp_path / "python",
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["trade"],
            required_tests_by_tier={},
            required_nodeids_by_tier={"trade": (row,)},
            timeout_override=5,
            capacity_session=session,
        )
    except KeyboardInterrupt:  # pragma: no cover - regression guard
        pytest.fail("cancellation escaped the gate instead of finalizing evidence")
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    assert signalled, "the regression did not deliver its signal"
    result = results[0]
    assert result.cancellation, result
    # Exactly one tier: the executed one.  No empty replacement row was added.
    assert [tier.name for tier in result.tiers] == ["trade"], result.tiers
    tier = result.tiers[0]
    assert tier.status == "FAIL", tier.reason
    assert tier.counts.failed == 1, tier.counts
    assert any("runtime-identity-marker" in detail.reason for detail in tier.failure_details), (
        tier.failure_details
    )
    # A cancelled run can never be reported as a clean PASS.
    assert gate.runtime_gate_passes(result) is False
    # A cancellation after an otherwise passing tier must still fail the run,
    # even when every executed row passed.
    cancelled_pass = _runtime_result("source")
    cancelled_pass.cancellation = "observer shutdown interrupted after a passing tier"
    assert gate.runtime_gate_passes(cancelled_pass) is False
    # A cancellation recorded on a completed tier must stop the outer runtime
    # plan: no later runtime may be prepared, waited for, or dispatched.

    def cancelled_cleanup(**kwargs):
        prepared = gate.PreparedRuntimeGate(
            result=gate.RuntimeGateResult(
                mode=kwargs["mode"],
                runtime={"pyboy_mode": kwargs["mode"], "pyboy_revision": "runtime-identity-marker"},
                collections=[gate.CollectionResult("python-module", [], "PASS", 0, nodeids=(row,))],
                fixture_manifest={"status": "PASS"},
                matrix_audit={"status": "PASS"},
                tiers=[],
            ),
            environment={},
            required_problems=[],
        )
        prepared.result.tiers.append(
            gate.TierResult(
                name="trade",
                description="trade",
                expression="trade",
                required=True,
                status="PASS",
                counts=gate.Counts(total=1, passed=1),
            )
        )
        prepared.result.cancellation = "observer shutdown interrupted after a passing tier"
        return prepared

    calls: list[str] = []
    monkeypatch.setattr(gate, "prepare_runtime_gate", cancelled_cleanup)

    def record_dispatch(command, **kwargs):
        calls.append(kwargs["env"].get("pyboy_mode", ""))
        return FakeMatrixPopen(command, **kwargs)

    monkeypatch.setattr(gate.subprocess, "Popen", record_dispatch)
    dual = gate.run_runtime_gates(
        runtime_mode="both",
        project_root=tmp_path,
        python_executable=tmp_path / "python",
        rom_root=tmp_path / "rom",
        fixture_root=tmp_path / "fixtures",
        expected_sha1={},
        assets=[],
        selected=["trade"],
        required_tests_by_tier={},
        required_nodeids_by_tier={"trade": (row,)},
        timeout_override=5,
    )
    assert [result.cancellation for result in dual] == [
        "observer shutdown interrupted after a passing tier",
        "not started after source was interrupted",
    ], [(result.mode, result.cancellation) for result in dual]
    assert dual[1].tiers[0].status == "NOT_STARTED", dual[1].tiers
    # The single row is accounted for once, not once as completed and again as
    # interrupted.
    assert sum(session.telemetry.lifecycle.values()) == 1, session.telemetry.lifecycle


def test_outer_interrupt_keeps_completed_tier_counts_and_identity(tmp_path, monkeypatch, capsys):
    """Regression: the outer SIGINT fallback must not erase completed rows.

    A reviewer delivered a real SIGINT while the outer plan accounted for a
    fail-fast-stopped row after a completed trade failure.  The CLI fallback
    rebuilt every runtime from scratch, so the executed trade row became an
    empty ``INTERRUPTED`` result with no counts, diagnostics, or runtime
    identity, and its lifecycle entry was duplicated.  Completed results must
    survive the outer cancellation boundary and only unfinished rows may be
    finalized.
    """

    rows = {
        "trade": ("tests/test_matrix.py::test_pair[aaa-fail]",),
        "battle": ("tests/test_matrix.py::test_battle[pending]",),
    }

    def prepare(**kwargs):
        return gate.PreparedRuntimeGate(
            result=gate.RuntimeGateResult(
                mode=kwargs["mode"],
                runtime={
                    "pyboy_mode": kwargs["mode"],
                    "pyboy_revision": "runtime-identity-marker",
                },
                collections=[
                    gate.CollectionResult(
                        "python-module",
                        [],
                        "PASS",
                        0,
                        nodeids=tuple(rows["trade"]) + tuple(rows["battle"]),
                    )
                ],
                fixture_manifest={"status": "PASS"},
                matrix_audit={"status": "PASS"},
                tiers=[],
            ),
            environment={},
            required_problems=[],
        )

    monkeypatch.setattr(gate, "prepare_runtime_gate", prepare)
    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1),
        repo_root=tmp_path,
        sampler=make_facts,
    )
    original_mark = session.telemetry.mark
    signalled: list[str] = []

    def mark_then_interrupt(pair_id, state):
        original_mark(pair_id, state)
        if ":battle:" in pair_id and state == "not_started" and not signalled:
            signalled.append(pair_id)
            os.kill(os.getpid(), signal.SIGINT)

    monkeypatch.setattr(session.telemetry, "mark", mark_then_interrupt)
    monkeypatch.setattr(gate.gate_capacity, "CapacitySession", lambda *_, **__: session)
    monkeypatch.setattr(gate, "parse_expected_sha1", lambda *_: {})
    monkeypatch.setattr(gate, "inspect_assets", lambda *_: [])
    monkeypatch.setattr(gate, "load_required_test_keys", lambda *_: ({}, ""))
    monkeypatch.setattr(gate, "load_required_nodeids", lambda *_: (rows, ""))
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(session.policy.as_dict()))
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        returncode = gate.main(
            [
                "--repo-root",
                str(tmp_path),
                "--tier",
                "trade",
                "--tier",
                "battle",
                "--fail-fast",
                "--capacity-policy",
                str(policy_path),
                "--format",
                "json",
                "--evidence-dir",
                str(tmp_path / "fallback-evidence"),
            ]
        )
    finally:
        signal.signal(signal.SIGINT, previous)
    payload = json.loads(capsys.readouterr().out)
    assert signalled and returncode == 130
    assert session.telemetry.lifecycle["completed"] == 1, session.telemetry.lifecycle
    # One planned row, one lifecycle entry: finalizing the partially registered
    # tier must transition the existing admission identity rather than
    # allocating a second one for the same planned row.
    assert len(session.telemetry.pair_states) == 2, session.telemetry.pair_states
    row_ids = [pair_id.split(":", 2)[-1] for pair_id in session.telemetry.pair_states]
    assert len(row_ids) == len(set(row_ids)), session.telemetry.pair_states
    trade = next(t for t in payload["tiers"] if t["name"] == "trade")
    assert trade["counts"]["failed"] == 1, trade
    assert "runtime-identity-marker" in json.dumps(trade), trade
    battle = next(t for t in payload["tiers"] if t["name"] == "battle")
    assert battle["status"] == "INTERRUPTED", battle


def test_matrix_finalization_interrupt_keeps_loaded_failure_evidence(tmp_path, monkeypatch):
    """Regression: an interrupt during matrix assembly must keep the evidence.

    A reviewer delivered a real SIGINT after the failing child report had been
    loaded but before ``run_matrix_tier`` returned.  The tier was replaced by an
    empty ``INTERRUPTED`` row, so ``failed`` dropped from 1 to 0 and the
    diagnostics and output vanished, while the same planned row was recorded a
    second time.  Assembly is in-memory only, so the cancellation must be
    deferred until the complete tier result exists and then carried out.
    """

    row = "tests/test_matrix.py::test_pair[aaa-fail]"

    def prepare(**kwargs):
        return gate.PreparedRuntimeGate(
            result=gate.RuntimeGateResult(
                mode=kwargs["mode"],
                runtime={"pyboy_mode": kwargs["mode"], "pyboy_revision": "runtime-identity-marker"},
                collections=[gate.CollectionResult("python-module", [], "PASS", 0, nodeids=(row,))],
                fixture_manifest={"status": "PASS"},
                matrix_audit={"status": "PASS"},
                tiers=[],
            ),
            environment={},
            required_problems=[],
        )

    monkeypatch.setattr(gate, "prepare_runtime_gate", prepare)
    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1), repo_root=tmp_path, sampler=lambda: make_facts()
    )
    # Deliver a real SIGINT exactly at the in-memory assembly boundary, after
    # the failing child report has been loaded.
    target = inspect.unwrap(gate.run_matrix_tier)
    source, start = inspect.getsourcelines(target)
    signal_line = start + next(
        index
        for index, line in enumerate(source)
        if line.startswith("    case_results = [case_results_by_nodeid")
    )
    signalled: list[bool] = []

    def trace(frame, event, arg):
        if (
            not signalled
            and event == "line"
            and frame.f_code is target.__code__
            and frame.f_lineno == signal_line
        ):
            signalled.append(True)
            os.kill(os.getpid(), signal.SIGINT)
        return trace

    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    previous_trace = sys.gettrace()
    try:
        sys.settrace(trace)
        results = gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=tmp_path / "python",
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["trade"],
            required_tests_by_tier={},
            required_nodeids_by_tier={"trade": (row,)},
            timeout_override=5,
            capacity_session=session,
        )
    except KeyboardInterrupt:  # pragma: no cover - regression guard
        pytest.fail("assembly cancellation escaped instead of finalizing evidence")
    finally:
        sys.settrace(previous_trace)
        signal.signal(signal.SIGINT, previous_handler)

    assert signalled, "the regression did not deliver its signal"
    result = results[0]
    assert result.cancellation, result
    assert [tier.name for tier in result.tiers] == ["trade"], result.tiers
    tier = result.tiers[0]
    # The executed failure, its diagnostics, and its output survive assembly.
    assert tier.counts.failed == 1, tier.counts
    assert any("runtime-identity-marker" in detail.reason for detail in tier.failure_details), (
        tier.failure_details
    )
    assert "MATRIX-OUTPUT" in tier.output_tail, tier.output_tail
    assert gate.runtime_gate_passes(result) is False
    # Exactly one admission identity for the single planned row.
    assert len(session.telemetry.pair_states) == 1, session.telemetry.pair_states


def test_non_fail_fast_cli_retains_completed_runtime_progress(tmp_path, monkeypatch, capsys):
    """Regression: a between-tiers interrupt keeps the completed runtime.

    With ordinary ``--tier trade --tier battle`` selection and no ``--fail-fast``
    the accumulator was appended only after ``run_runtime_gate`` returned.  A
    SIGINT between the failing trade tier and the next tier therefore rebuilt the
    runtime as unstarted: ``failed`` changed from 1 to 0, the failure evidence
    disappeared, and the observed runtime identity became ``not-run``.
    """

    rows = {
        "trade": ("tests/test_matrix.py::test_pair[aaa-fail]",),
        "battle": ("tests/test_matrix.py::test_pair[pending]",),
    }

    def prepare(**kwargs):
        return gate.PreparedRuntimeGate(
            result=gate.RuntimeGateResult(
                mode=kwargs["mode"],
                runtime={
                    "pyboy_mode": kwargs["mode"],
                    "pyboy_revision": "runtime-identity-marker",
                },
                collections=[
                    gate.CollectionResult(
                        "python-module",
                        [],
                        "PASS",
                        0,
                        nodeids=tuple(rows["trade"]) + tuple(rows["battle"]),
                    )
                ],
                fixture_manifest={"status": "PASS"},
                matrix_audit={"status": "PASS"},
                tiers=[],
            ),
            environment={},
            required_problems=[],
        )

    monkeypatch.setattr(gate, "prepare_runtime_gate", prepare)
    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1), repo_root=tmp_path, sampler=make_facts
    )
    monkeypatch.setattr(gate.gate_capacity, "CapacitySession", lambda *_, **__: session)
    monkeypatch.setattr(gate, "parse_expected_sha1", lambda *_: {})
    monkeypatch.setattr(gate, "inspect_assets", lambda *_: [])
    monkeypatch.setattr(gate, "load_required_test_keys", lambda *_: ({}, ""))
    monkeypatch.setattr(gate, "load_required_nodeids", lambda *_: (rows, ""))
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(session.policy.as_dict()))
    # Deliver a real SIGINT after the failing trade tier has been appended but
    # before the next tier starts.  The tier-loop cancellation handlers are not
    # involved, so only publishing the runtime result before dispatch keeps the
    # completed failure and the observed runtime identity.
    target = gate.run_runtime_gate
    source, start = inspect.getsourcelines(target)
    signal_line = start + next(
        index
        for index, line in enumerate(source)
        if line.startswith('        if prepared.result.tiers[-1].status == "INTERRUPTED":')
    )
    signalled: list[bool] = []

    def trace(frame, event, arg):
        if (
            not signalled
            and event == "line"
            and frame.f_code is target.__code__
            and frame.f_lineno == signal_line
        ):
            signalled.append(True)
            os.kill(os.getpid(), signal.SIGINT)
        return trace

    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    previous_trace = sys.gettrace()
    try:
        sys.settrace(trace)
        returncode = gate.main(
            [
                "--repo-root",
                str(tmp_path),
                "--tier",
                "trade",
                "--tier",
                "battle",
                "--capacity-policy",
                str(policy_path),
                "--format",
                "json",
                "--evidence-dir",
                str(tmp_path / "nonfailfast-evidence"),
            ]
        )
    finally:
        sys.settrace(previous_trace)
        signal.signal(signal.SIGINT, previous)
    payload = json.loads(capsys.readouterr().out)
    assert signalled, "the regression did not deliver its signal"
    assert returncode == 130
    # The observed runtime identity and the executed failure are retained.
    assert payload["runtime"].get("pyboy_revision") == "runtime-identity-marker", payload["runtime"]
    trade = next(tier for tier in payload["tiers"] if tier["name"] == "trade")
    assert trade["counts"]["failed"] == 1, trade
    assert "runtime-identity-marker" in json.dumps(trade), trade
    battle = next(tier for tier in payload["tiers"] if tier["name"] == "battle")
    assert battle["status"] == "INTERRUPTED", battle


def test_capacity_only_aggregate_stop_is_blocked_and_drops_the_queue(tmp_path, monkeypatch):
    """Regression: a capacity-only stop is BLOCKED, not a product FAIL.

    A reviewer reproduced a tier with one passing row and one row queued solely
    because declared pressure was high.  The supervisor reported FAIL with zero
    failed tests, and the abandoned waiter was still queued, so a later release
    promoted it into a live slot and starved the next real row.
    """

    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    session = gate_capacity.CapacitySession(
        make_policy(
            max_concurrent_pairs=1, admission_deadline_seconds=60.0, observation_seconds=0.01
        ),
        repo_root=tmp_path,
        sampler=make_facts,
    )
    rows = (
        "tests/test_matrix.py::test_pair[pass-a]",
        "tests/test_matrix.py::test_pair[queued-b]",
    )

    def pressured_popen(command, **kwargs):
        child = FakeMatrixPopen(command, **kwargs)
        # Once the first pair owns the slot, declared pressure rises and stays
        # high, so the second row can only ever queue.
        session.telemetry._sampler = lambda: make_facts(psi_cpu_some_avg300=99)
        return child

    monkeypatch.setattr(gate.subprocess, "Popen", pressured_popen)
    result = gate.run_tier(
        name="trade",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        repeat=1,
        timeout_override=5,
        report_directory=tmp_path,
        required_nodeids=rows,
        matrix_workers=2,
        matrix_timeout_override=0.2,
        capacity_session=session,
    )

    cases = {case.nodeid: case.status for case in result.case_results}
    assert cases[rows[0]] == "PASS"
    assert cases[rows[1]] == "BLOCKED"
    assert result.status == "BLOCKED", result.status
    # The abandoned waiter no longer holds queue state and is terminal, so a
    # recovered tier admits the next real row instead of the abandoned one.
    assert session.admission.queued == []
    keys = {
        decision.pair_id.split(":", 2)[-1]: decision.pair_id
        for decision in session.admission.decisions
    }
    assert session.admission.state(keys[rows[1]]) == "expired"
    report = session.report()
    assert report["lifecycle"]["not_started"] == 1
    assert report["lifecycle"]["completed"] == 1
    assert report["status"] == "blocked"
    session.telemetry._sampler = make_facts
    session.observe()
    decision = session.admission.admit("2:next")
    assert decision.status == "ok" and decision.owner is True, decision


def test_asset_blocked_strict_rows_are_registered(tmp_path, monkeypatch):
    """Regression: a directly asset-blocked strict row still accounts its rows."""

    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    session = gate_capacity.CapacitySession(
        make_policy(), repo_root=tmp_path, sampler=lambda: make_facts()
    )
    rows = (
        "tests/test_matrix.py::test_pair[red-blue]",
        "tests/test_matrix.py::test_pair[blue-red]",
    )
    result = gate.run_tier(
        name="trade",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=["missing ROM for red"],
        repeat=1,
        timeout_override=5,
        report_directory=tmp_path,
        required_nodeids=rows,
        matrix_workers=2,
        capacity_session=session,
    )
    assert result.status == "BLOCKED", result.reason
    report = session.report()
    assert report["lifecycle"]["not_started"] == len(rows)
    recorded = {
        decision["pair_id"].split(":", 2)[-1] for decision in report["admission"]["decisions"]
    }
    assert recorded == set(rows)


def test_preflight_failure_registers_blocked_rows_for_every_tier(tmp_path, monkeypatch):
    """Regression: fail-fast/preflight exits must not hide planned rows."""

    runtime_gate_stubs.__wrapped__(tmp_path, monkeypatch)
    monkeypatch.setattr(
        gate,
        "run_collection_preflight",
        lambda **_kwargs: [
            gate.CollectionResult("python-module", [], "FAIL", 1, reason="collection failed")
        ],
    )
    session = gate_capacity.CapacitySession(
        make_policy(), repo_root=tmp_path, sampler=lambda: make_facts()
    )
    rows = (
        "tests/test_matrix.py::test_pair[red-blue]",
        "tests/test_matrix.py::test_pair[blue-red]",
    )
    results = gate.run_runtime_gates(
        runtime_mode="source",
        project_root=tmp_path,
        python_executable=tmp_path / "python",
        rom_root=tmp_path / "rom",
        fixture_root=tmp_path / "fixtures",
        expected_sha1={},
        assets=[],
        selected=["unit", "trade"],
        required_tests_by_tier={},
        required_nodeids_by_tier={"trade": rows},
        capacity_session=session,
        early_smoke=True,
        fail_fast=True,
    )
    statuses = {tier.name: tier.status for tier in results[0].tiers}
    assert statuses["trade"] in {"BLOCKED", "NOT_STARTED"}, statuses
    report = session.report()
    recorded = {
        decision["pair_id"].split(":", 2)[-1] for decision in report["admission"]["decisions"]
    }
    assert set(rows) <= recorded, recorded
    assert report["lifecycle"]["not_started"] >= len(rows)


def test_parser_accepts_capacity_policy():
    args = gate.build_parser().parse_args(["--capacity-policy", "policy.json"])
    assert args.capacity_policy == Path("policy.json")


def test_main_without_policy_records_unavailable_capacity(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gate, "parse_expected_sha1", lambda _path: {})
    monkeypatch.setattr(gate, "inspect_assets", lambda *_args: [])
    monkeypatch.setattr(gate, "load_required_test_keys", lambda _path: ({}, ""))
    monkeypatch.setattr(gate, "load_required_nodeids", lambda _path: ({}, ""))

    def fake_runtime_gates(**_kwargs):
        result = _runtime_result("source")
        result.execution_plan = gate.build_execution_plan(
            ("source",), ("unit",), early_smoke=False, fail_fast=False
        )
        return [result]

    monkeypatch.setattr(gate, "run_runtime_gates", fake_runtime_gates)
    evidence = tmp_path / "evidence"
    returncode = gate.main(
        [
            "--repo-root",
            str(tmp_path),
            "--tier",
            "unit",
            "--format",
            "json",
            "--evidence-dir",
            str(evidence),
        ]
    )
    capsys.readouterr()
    assert returncode == 0
    report = json.loads((evidence / "gate-report.json").read_text(encoding="utf-8"))
    assert report["capacity"]["status"] == "unavailable"
    assert report["capacity"]["capacity_policy"] == "unavailable"
    text = (evidence / "gate-report.txt").read_text(encoding="utf-8")
    assert "capacity-policy: unavailable" in text


def test_admitted_count_is_idempotent_for_re_admission():
    clock = FakeClock()
    admission = gate_capacity.CapacityAdmission(make_policy(max_concurrent_pairs=2), clock=clock)
    assert admission.admit("pair-1").status == "ok"
    assert admission.admit("pair-2").status == "ok"
    # Re-admitting an already-active owner must not double count.
    assert admission.admit("pair-1").status == "ok"
    assert admission.admitted_count == 2
    assert admission.release("pair-1") is None
    # Re-admitting the same pair after release still counts it once.
    assert admission.admit("pair-1").status == "ok"
    assert admission.admitted_count == 2


def test_main_with_policy_asset_free_reports_not_applicable(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gate, "parse_expected_sha1", lambda _path: {})
    monkeypatch.setattr(gate, "inspect_assets", lambda *_args: [])
    monkeypatch.setattr(gate, "load_required_test_keys", lambda _path: ({}, ""))
    monkeypatch.setattr(gate, "load_required_nodeids", lambda _path: ({}, ""))

    def fake_runtime_gates(**_kwargs):
        result = _runtime_result("source")
        result.execution_plan = gate.build_execution_plan(
            ("source",), ("unit",), early_smoke=False, fail_fast=False
        )
        return [result]

    monkeypatch.setattr(gate, "run_runtime_gates", fake_runtime_gates)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(make_policy(effective_cpus=99).as_dict()), encoding="utf-8")
    evidence = tmp_path / "evidence"
    returncode = gate.main(
        [
            "--repo-root",
            str(tmp_path),
            "--tier",
            "unit",
            "--capacity-policy",
            str(policy_path),
            "--format",
            "json",
            "--evidence-dir",
            str(evidence),
        ]
    )
    capsys.readouterr()
    assert returncode == 0
    report = json.loads((evidence / "gate-report.json").read_text(encoding="utf-8"))
    assert report["overall"] == "PASS"
    assert report["capacity"]["status"] == "not_applicable"
    assert report["capacity"]["availability"]["status"] == "not_applicable"
    assert report["capacity"]["admission"]["admitted"] == 0
    text = (evidence / "gate-report.txt").read_text(encoding="utf-8")
    assert "capacity-policy: not_applicable" in text


def _standalone_environment():
    """Environment where ``scripts`` is not importable as a package."""

    environment = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "vendor" / "pyboy-src"), str(ROOT / "src")]
    )
    environment["PYBOY_NO_CYTHON"] = "1"
    environment.pop("PYTEST_ADDOPTS", None)
    environment.pop("POKERED_SKIP_SHA1", None)
    return environment


def test_standalone_gate_help_runs_without_package_layout():
    # Regression for the dynamic qualification_runner load: the loaded module
    # must be registered in ``sys.modules`` before ``exec_module`` so the
    # sibling module's annotated ``@dataclass`` can resolve its own globals.
    completed = subprocess.run(
        [sys.executable, "scripts/production_gate.py", "--help"],
        cwd=ROOT,
        env=_standalone_environment(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Traceback" not in completed.stderr
    assert "usage:" in completed.stdout


@pytest.mark.parametrize("field", ["observation_seconds", "admission_deadline_seconds"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_policy_rejects_nonfinite_timing(field, value):
    assert any(
        field in problem for problem in gate_capacity.validate_policy(make_policy(**{field: value}))
    )


def test_unavailable_capacity_recovers_before_admission_deadline(tmp_path):
    clock = FakeClock()
    sampler = SequenceSampler([make_facts(cpu_quota_cores=1), make_facts()])
    session = gate_capacity.CapacitySession(
        make_policy(observation_seconds=1), repo_root=tmp_path, clock=clock, sampler=sampler
    )
    assert session.begin() == "blocked"
    assert session.admission.admit("pair").status == "queued"
    clock.advance(1)
    assert session.maybe_observe() == "ok"
    assert session.admission.admit("pair").status == "ok"
    assert session.admission.queued == []
    assert sampler.calls == 2


def test_unavailable_queue_expires_even_when_capacity_recovers(tmp_path):
    clock = FakeClock()
    session = gate_capacity.CapacitySession(
        make_policy(admission_deadline_seconds=1),
        repo_root=tmp_path,
        clock=clock,
        sampler=SequenceSampler([make_facts(cpu_quota_cores=1), make_facts()]),
    )
    session.begin()
    assert session.admission.admit("pair").status == "queued"
    clock.advance(1)
    assert session.observe() == "ok"
    assert session.admission.admit("pair").status == "expired"
    assert session.admission.admitted_count == 0


def test_pressure_change_preserves_owner_and_waiter_deadline():
    clock = FakeClock()
    availability = ["ok"]
    admission = gate_capacity.CapacityAdmission(
        make_policy(), clock=clock, availability=lambda: (availability[0], "pressure")
    )
    owner = admission.admit("owner")
    availability[0] = "blocked"
    assert admission.admit("waiter").status == "queued"
    clock.advance(1)
    assert admission.admit("owner").status == "ok"
    assert admission.active["owner"] is owner
    admission.release("owner")
    assert admission.queued == ["waiter"]
    assert admission.waiting_since["waiter"] == 0


def test_expired_waiter_is_terminal_and_cannot_be_readmitted():
    clock = FakeClock()
    availability = ["blocked"]
    admission = gate_capacity.CapacityAdmission(
        make_policy(admission_deadline_seconds=1),
        clock=clock,
        availability=lambda: (availability[0], "pressure"),
    )
    assert admission.admit("pair").status == "queued"
    clock.advance(1)
    assert admission.admit("pair").status == "expired"
    # Capacity recovers, but the expired row must not be re-admitted as new.
    availability[0] = "ok"
    assert admission.admit("pair").status == "expired"
    assert admission.admitted_count == 0
    assert "pair" not in admission.active
    assert "pair" not in admission.queued


def test_expire_waiting_is_terminal_for_later_admission():
    clock = FakeClock()
    availability = ["blocked"]
    admission = gate_capacity.CapacityAdmission(
        make_policy(admission_deadline_seconds=1),
        clock=clock,
        availability=lambda: (availability[0], "pressure"),
    )
    assert admission.admit("pair").status == "queued"
    clock.advance(2)
    expired = admission.expire_waiting()
    assert [decision.status for decision in expired] == ["expired"]
    availability[0] = "ok"
    assert admission.admit("pair").status == "expired"
    assert admission.admitted_count == 0


def test_expired_queue_cannot_be_readmitted_after_recovery(tmp_path, monkeypatch):
    """A reviewed regression: queued -> expired -> ok -> PASS must be impossible."""

    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    clock = FakeClock()
    recovery = {"at": 0.0}

    class RecoveringSampler:
        """Blocked while the row is queued; healthy only after its deadline."""

        def __init__(self):
            self.calls = 0

        def __call__(self):
            self.calls += 1
            if clock.value >= recovery["at"]:
                return make_facts()
            return make_facts(psi_cpu_some_avg300=100)

    recovery["at"] = 0.3
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1, admission_deadline_seconds=0.1),
        repo_root=tmp_path,
        clock=clock,
        sampler=RecoveringSampler(),
    )
    key = "row"
    assert session.admission.admit(key).status == "queued"
    clock.advance(0.5)
    # Capacity is healthy now, yet the expired row stays terminal instead of
    # being re-admitted as new (which previously let the row run and PASS).
    assert session.admission.admit(key).status == "expired"
    assert session.admission.admitted_count == 0
    assert key not in session.admission.active
    # Releasing the slot the row never owned must not resurface it either.
    session.admission.release(key)
    assert session.admission.admit(key).status == "expired"
    assert session.admission.admitted_count == 0


def test_tier_capacity_wait_is_bounded_and_can_recover(tmp_path):
    clock = FakeClock()
    session = gate_capacity.CapacitySession(
        make_policy(observation_seconds=1, admission_deadline_seconds=3),
        repo_root=tmp_path,
        clock=clock,
        sampler=SequenceSampler([make_facts(cpu_quota_cores=1), make_facts()]),
    )
    assert session.wait_until_available(sleep=clock.advance) == "ok"
    assert clock() == 1
    session.telemetry._sampler = lambda: make_facts(cpu_quota_cores=1)
    session.observe()
    assert session.wait_until_available(sleep=clock.advance) == "blocked"
    assert clock() == 4


def test_matrix_retries_unavailable_capacity(tmp_path, monkeypatch):
    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    sampler = SequenceSampler(
        [make_facts(cpu_quota_cores=1), make_facts(cpu_quota_cores=1), make_facts()]
    )
    session = gate_capacity.CapacitySession(
        make_policy(admission_deadline_seconds=1), repo_root=tmp_path, sampler=sampler
    )
    result = gate.run_matrix_tier(
        name="trade",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        timeout_override=5,
        report_directory=tmp_path,
        required_nodeids=("tests/test_matrix.py::test_pair[pass]",),
        capacity_session=session,
    )
    assert result.status == "PASS"
    assert sampler.calls >= 3
    assert session.admission.admitted_count == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"cpu_quota_status": "unknown", "cpu_quota_cores": None},
        {"cpu_quota_status": "unknown", "cpu_quota_cores": 8},
        {"cpu_quota_status": "unlimited", "cpu_quota_cores": 8},
        {"psi_cpu_some_avg300": None},
        {"cgroup_cpu_some_avg300": None},
        {"cpu_throttled": None},
        {"load_average": [float("nan"), 0, 0]},
    ],
)
def test_unknown_required_capacity_fails_closed(overrides):
    assert (
        gate_capacity.evaluate_capacity(make_policy(), make_facts(**overrides))[0] == "unsupported"
    )


def test_confirmed_unlimited_quota_is_distinct_from_unknown():
    assert (
        gate_capacity.evaluate_capacity(
            make_policy(), make_facts(cpu_quota_status="unlimited", cpu_quota_cores=None)
        )[0]
        == "ok"
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"psi_cpu_some_avg300": 21},
        {"cgroup_cpu_some_avg300": 21},
        {"load_average": [0, 0, 5]},
    ],
)
def test_declared_pressure_and_load_thresholds_stop_admission(overrides):
    assert gate_capacity.evaluate_capacity(make_policy(), make_facts(**overrides))[0] == "blocked"


def test_observer_samples_while_matrix_owner_runs(tmp_path, monkeypatch):
    import time

    class SlowChild(FakeMatrixPopen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.finish = time.monotonic() + 0.15

        def poll(self):
            return self.returncode if time.monotonic() >= self.finish else None

    monkeypatch.setattr(gate.subprocess, "Popen", SlowChild)
    session = gate_capacity.CapacitySession(
        make_policy(observation_seconds=0.01), repo_root=tmp_path, sampler=make_facts
    )
    result = gate.run_matrix_tier(
        name="trade",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        timeout_override=5,
        report_directory=tmp_path,
        required_nodeids=("tests/test_matrix.py::test_pair[pass]",),
        capacity_session=session,
    )
    assert result.status == "PASS"
    assert len(session.telemetry.samples) >= 3
    assert not session._monitor_thread.is_alive()


def test_nonmatrix_tier_rows_are_admitted_and_observed(tmp_path, monkeypatch):
    """An ordinary real-ROM tier admits each planned pair, not one batch slot."""

    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    row = "tests/test_rom_boot.py::test_pair[red-color]"
    write_tier_config(tmp_path)
    session = gate_capacity.CapacitySession(
        make_policy(observation_seconds=0.01), repo_root=tmp_path, sampler=make_facts
    )
    result = gate.run_tier(
        name="local",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        timeout_override=5,
        report_directory=tmp_path,
        repeat=1,
        capacity_session=session,
        collections=(gate.CollectionResult("python-module", [], "PASS", 0, nodeids=(row,)),),
    )
    assert result.status == "PASS", result.reason
    assert session.report()["status"] == "ok"
    # One slot per planned pair, counted once even though dispatch is per row.
    assert session.admission.admitted_count == 1
    assert session.telemetry.lifecycle["completed"] == 1
    assert len(session.telemetry.samples) >= 3
    assert [case.status for case in result.case_results] == ["PASS"]


def test_observer_hang_cannot_hold_cleanup_or_modify_final_report(tmp_path):
    import threading
    import time

    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def sampler():
        nonlocal calls
        calls += 1
        if calls > 1:
            entered.set()
            release.wait(5)
        return make_facts()

    session = gate_capacity.CapacitySession(
        make_policy(observation_seconds=0.01), repo_root=tmp_path, sampler=sampler
    )
    try:
        with session.monitoring():
            assert entered.wait(1)
            start = time.monotonic()
        assert time.monotonic() - start < 1
        final = session.report(failed=1)
        assert final["status"] == "failed"
        assert final["collection_failures"] == 1
    finally:
        release.set()
        session._monitor_thread.join(1)
    assert session.report(failed=1) == final


def test_complete_sanitized_stream_is_persisted_and_verified(tmp_path):
    session = gate_capacity.CapacitySession(make_policy(), repo_root=tmp_path, sampler=make_facts)
    for _ in range(270):
        session.observe()
    session.telemetry.samples[-1].problems = [str(tmp_path / "private-fixture")]
    payload = gate.build_evidence_payload(
        project_root=tmp_path,
        rom_root=tmp_path / "rom",
        fixture_root=tmp_path / "fixtures",
        runtime={},
        assets=[],
        collections=[],
        tiers=[],
        gate_problems=[],
        overall="FAIL",
        capacity=session.report(),
    )
    evidence = tmp_path / "evidence"
    gate.write_evidence_bundle(evidence, payload)
    report = json.loads((evidence / gate.EVIDENCE_REPORT_FILENAME).read_text())
    capacity = report["capacity"]
    content = (evidence / gate.CAPACITY_STREAM_FILENAME).read_bytes()
    assert str(tmp_path).encode() not in content
    assert len(json.loads(content)) == 270
    assert len(capacity["samples"]) == 256
    assert capacity["samples_omitted"] == 14
    assert "sample_stream" not in capacity
    import hashlib

    assert capacity["sample_reference"]["sha256"] == hashlib.sha256(content).hexdigest()
    gate.verify_evidence_bundle(evidence)
    (evidence / gate.CAPACITY_STREAM_FILENAME).write_bytes(content + b" ")
    with pytest.raises(ValueError, match="mismatch"):
        gate.verify_evidence_bundle(evidence)


def test_main_cannot_pass_with_unresolved_capacity(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gate, "parse_expected_sha1", lambda _path: {})
    monkeypatch.setattr(gate, "inspect_assets", lambda *_args: [])
    monkeypatch.setattr(gate, "load_required_test_keys", lambda _path: ({}, ""))
    monkeypatch.setattr(gate, "load_required_nodeids", lambda _path: ({}, ""))
    monkeypatch.setattr(gate, "run_runtime_gates", lambda **kwargs: [_runtime_result("source")])
    monkeypatch.setattr(
        gate_capacity.qualification_runner, "collect_facts", lambda *args: make_facts()
    )
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(make_policy().as_dict()))
    code = gate.main(
        [
            "--repo-root",
            str(tmp_path),
            "--tier",
            "smoke",
            "--capacity-policy",
            str(policy_path),
            "--format",
            "json",
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 1
    assert report["overall"] == "FAIL"
    assert report["capacity"]["status"] == "blocked"


@pytest.mark.parametrize(
    "overrides",
    [
        {"cpu_quota_status": []},
        {"cpu_throttled": "invalid"},
        {"affinity_count": 4, "affinity_cpus": [0]},
        {"affinity_count": 0, "affinity_cpus": []},
        {"affinity_count": 2, "affinity_cpus": [0, 0]},
    ],
)
def test_malformed_capacity_never_admits(overrides):
    assert (
        gate_capacity.evaluate_capacity(make_policy(), make_facts(**overrides))[0] == "unsupported"
    )


def test_malformed_periodic_sample_stops_admission_and_retains_failure(tmp_path):
    import time

    session = gate_capacity.CapacitySession(
        make_policy(observation_seconds=0.01),
        repo_root=tmp_path,
        sampler=SequenceSampler([make_facts(), make_facts(cpu_quota_status=[])]),
    )
    with session.monitoring():
        limit = time.monotonic() + 1
        while session.availability_status == "ok" and time.monotonic() < limit:
            time.sleep(0.01)
        assert session.availability_status == "unsupported"
        assert session._monitor_thread.is_alive()
        assert session.admission.admit("new-row").status == "queued"
    assert session.report(failed=1)["status"] == "failed"
    assert session.telemetry.samples[-1].status == "malformed"


def test_initial_collection_deadline_discards_late_result(tmp_path):
    import threading
    import time

    release = threading.Event()
    finished = threading.Event()

    def sampler():
        release.wait(2)
        finished.set()
        return make_facts()

    session = gate_capacity.CapacitySession(
        make_policy(admission_deadline_seconds=0.02), repo_root=tmp_path, sampler=sampler
    )
    try:
        start = time.monotonic()
        assert session.wait_until_available() == "unsupported"
        assert time.monotonic() - start < 0.25
        assert session.admission.admitted_count == 0
        final = session.report()
        assert final["samples"][0]["status"] == "failed"
    finally:
        release.set()
        assert finished.wait(1)
    assert session.report() == final


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_telemetry_cannot_erase_product_failure_bundle(tmp_path, value):
    session = gate_capacity.CapacitySession(
        make_policy(), repo_root=tmp_path, sampler=lambda: make_facts(psi_cpu_some_avg300=value)
    )
    session.begin()
    payload = gate.build_evidence_payload(
        project_root=tmp_path,
        rom_root=tmp_path / "rom",
        fixture_root=tmp_path / "fixtures",
        runtime={},
        assets=[],
        collections=[],
        tiers=[],
        gate_problems=["original product failure"],
        overall="FAIL",
        capacity=session.report(failed=1),
    )
    evidence = tmp_path / "evidence"
    gate.write_evidence_bundle(evidence, payload)
    gate.verify_evidence_bundle(evidence)
    report = json.loads((evidence / gate.EVIDENCE_REPORT_FILENAME).read_text())
    assert report["overall"] == "FAIL"
    assert report["capacity"]["status"] == "failed"
    assert report["capacity"]["samples"][0]["facts"]["psi_cpu_some_avg300"] is None
    assert "original product failure" in report["gate_problems"]


def test_stalled_live_observer_cannot_admit_using_stale_health(tmp_path):
    import threading
    import time

    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def sampler():
        nonlocal calls
        calls += 1
        if calls > 1:
            entered.set()
            release.wait(2)
        return make_facts()

    session = gate_capacity.CapacitySession(
        make_policy(observation_seconds=0.01, admission_deadline_seconds=0.02),
        repo_root=tmp_path,
        sampler=sampler,
    )
    try:
        with session.monitoring():
            assert entered.wait(1)
            time.sleep(0.06)
            assert session.maybe_observe() == "unsupported"
            assert session.admission.admit("later").status == "queued"
            assert session.report(failed=1)["status"] == "failed"
            assert "freshness" in session.telemetry.samples[-1].problems[0]
            release.set()
    finally:
        release.set()
        session._monitor_thread.join(1)


def test_monitor_restart_refreshes_before_admitting_next_tier(tmp_path):
    import time

    sampler = SequenceSampler([make_facts(), make_facts(psi_cpu_some_avg300=100)])
    session = gate_capacity.CapacitySession(
        make_policy(observation_seconds=0.02), repo_root=tmp_path, sampler=sampler
    )
    with session.monitoring():
        assert session.admission.admit("first").status == "ok"
        session.admission.release("first")
    time.sleep(0.08)
    with session.monitoring():
        assert sampler.calls >= 2
        assert session.admission.admit("next-tier").status == "queued"
        assert session.availability_status == "blocked"


def test_unit_tier_is_not_capacity_gated(tmp_path, monkeypatch):
    """A ROM-free tier must keep its batch dispatch under a capacity policy."""

    observed = []

    def run_once(**kwargs):
        observed.append(kwargs)
        return (
            0,
            gate.GateReport(
                gate.Counts(total=1, passed=1), nodeids=("tests/test_unit.py::test_x",)
            ),
            "",
            ["pytest"],
        )

    monkeypatch.setattr(gate, "run_pytest_once", run_once)
    session = gate_capacity.CapacitySession(
        make_policy(), repo_root=tmp_path, sampler=lambda: make_facts(psi_cpu_some_avg300=99)
    )
    result = gate.run_tier(
        name="unit",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        timeout_override=5,
        report_directory=tmp_path,
        repeat=1,
        capacity_session=session,
    )
    assert result.status == "PASS"
    assert len(observed) == 1
    assert session.admission.admitted_count == 0


def test_second_pair_never_starts_while_observed_capacity_is_blocked(tmp_path, monkeypatch):
    """Regression: an ordinary tier must re-check admission between pairs.

    A reviewer reproduced a two-pair local batch whose second pair started
    after the observer saw blocked pressure, and the whole tier still reported
    PASS.  With per-pair admission the second row must be recorded BLOCKED and
    the tier must report BLOCKED instead of borrowing the first pair's pass.
    """

    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    write_tier_config(
        tmp_path,
        markers={
            "test_rom_boot.py::test_pair[ok]": ("real_rom",),
            "test_rom_boot.py": ("real_rom",),
        },
        tier_rules={"local": (("real_rom",), ())},
    )
    rows = (
        "tests/test_rom_boot.py::test_pair[ok]",
        "tests/test_rom_boot.py::test_pair[second]",
    )
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1, admission_deadline_seconds=0.05),
        repo_root=tmp_path,
        sampler=make_facts,
    )
    started: list[str] = []

    def recording_popen(command, **kwargs):
        started.append(command[3])
        if len(started) == 1:
            # Once the first pair is active, declared pressure spikes.
            session.telemetry._sampler = lambda: make_facts(psi_cpu_some_avg300=99)
            session.observe()
        return FakeMatrixPopen(command, **kwargs)

    monkeypatch.setattr(gate.subprocess, "Popen", recording_popen)
    result = gate.run_tier(
        name="local",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        timeout_override=5,
        report_directory=tmp_path,
        repeat=1,
        capacity_session=session,
        collections=(gate.CollectionResult("python-module", [], "PASS", 0, nodeids=rows),),
    )
    assert started == [rows[0]], started
    cases = {case.nodeid: case.status for case in result.case_results}
    assert cases[rows[0]] == "PASS"
    assert cases[rows[1]] == "BLOCKED"
    assert result.status == "BLOCKED", result.status
    # The unstarted row is terminal for capacity accounting and never passed.
    assert session.telemetry.lifecycle["not_started"] == 1
    assert any(
        decision.status in {"blocked", "expired"} for decision in session.admission.decisions
    )


def test_preflight_blocked_rows_are_recorded_in_lifecycle(tmp_path, monkeypatch):
    """Regression: pre-dispatch BLOCKED rows must not vanish from accounting.

    A reviewer reproduced two BLOCKED matrix rows with ``not_started=0`` and no
    admission decisions.  Every row the tier would have dispatched must be
    registered as a terminal blocked decision.
    """

    runtime_gate_stubs.__wrapped__(tmp_path, monkeypatch)
    write_tier_config(
        tmp_path,
        markers={"test_matrix.py": ("real_rom", "trade_acceptance")},
        tier_rules={"trade": (("real_rom", "trade_acceptance"), ())},
    )
    session = gate_capacity.CapacitySession(
        make_policy(admission_deadline_seconds=0.01),
        repo_root=tmp_path,
        sampler=lambda: make_facts(psi_cpu_some_avg300=99),
    )
    rows = (
        "tests/test_matrix.py::test_pair[a]",
        "tests/test_matrix.py::test_pair[b]",
    )
    results = gate.run_runtime_gates(
        runtime_mode="source",
        project_root=tmp_path,
        python_executable=tmp_path / "python",
        rom_root=tmp_path / "rom",
        fixture_root=tmp_path / "fixtures",
        expected_sha1={},
        assets=[],
        selected=["trade"],
        required_tests_by_tier={},
        required_nodeids_by_tier={"trade": rows},
        capacity_session=session,
    )
    assert results[0].tiers[0].status == "BLOCKED"
    report = session.report()
    assert report["lifecycle"]["not_started"] == len(rows)
    recorded = {
        decision["pair_id"].split(":", 2)[-1] for decision in report["admission"]["decisions"]
    }
    assert recorded == set(rows)
    assert all(decision["status"] == "blocked" for decision in report["admission"]["decisions"])


def test_zero_interval_collector_cannot_delay_active_child_deadline(tmp_path, monkeypatch):
    import threading
    import time

    release = threading.Event()
    calls = 0
    main_thread = threading.get_ident()
    collection_threads = []
    killed_after = []

    def sampler():
        nonlocal calls
        calls += 1
        collection_threads.append(threading.get_ident())
        if calls > 1:
            release.wait(2)
        return make_facts()

    class StuckChild(FakeMatrixPopen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.started = time.monotonic()

        def poll(self):
            return None

    def kill(process, **kwargs):
        killed_after.append(time.monotonic() - process.started)
        return True

    monkeypatch.setattr(gate.subprocess, "Popen", StuckChild)
    monkeypatch.setattr(gate, "_kill_matrix_process", kill)
    session = gate_capacity.CapacitySession(
        make_policy(observation_seconds=0, admission_deadline_seconds=0.4),
        repo_root=tmp_path,
        sampler=sampler,
    )
    try:
        result = gate.run_matrix_tier(
            name="trade",
            project_root=tmp_path,
            python_executable=Path("python"),
            environment={},
            required_problems=[],
            timeout_override=0.03,
            report_directory=tmp_path,
            required_nodeids=(
                "tests/test_matrix.py::test_pair[first]",
                "tests/test_matrix.py::test_pair[second]",
            ),
            matrix_workers=2,
            capacity_session=session,
        )
        assert result.status == "FAIL"
        assert len(killed_after) == 2
        assert max(killed_after) < 0.2
        assert main_thread not in collection_threads
    finally:
        release.set()
        session._monitor_thread.join(1)


def _line_of(function, prefix):
    """Return the absolute line number of the first line starting with prefix."""

    source, start = inspect.getsourcelines(inspect.unwrap(function))
    return start + next(index for index, line in enumerate(source) if line.startswith(prefix))


@contextlib.contextmanager
def _real_sigint_at_line(function, prefix):
    """Deliver one real SIGINT when ``function`` reaches ``prefix``."""

    target = inspect.unwrap(function)
    boundary = _line_of(target, prefix)
    signalled: list[bool] = []

    def trace(frame, event, arg):
        if (
            not signalled
            and event == "line"
            and frame.f_code is target.__code__
            and frame.f_lineno == boundary
        ):
            signalled.append(True)
            os.kill(os.getpid(), signal.SIGINT)
        return trace

    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    previous_trace = sys.gettrace()
    try:
        sys.settrace(trace)
        yield signalled
    finally:
        sys.settrace(previous_trace)
        signal.signal(signal.SIGINT, previous_handler)


def _matrix_row_prepare(rows):
    def prepare(**kwargs):
        return gate.PreparedRuntimeGate(
            result=gate.RuntimeGateResult(
                mode=kwargs["mode"],
                runtime={"pyboy_mode": kwargs["mode"], "pyboy_revision": "runtime-identity-marker"},
                collections=[
                    gate.CollectionResult(
                        "python-module",
                        [],
                        "PASS",
                        0,
                        nodeids=tuple(nodeid for values in rows.values() for nodeid in values),
                    )
                ],
                fixture_manifest={"status": "PASS"},
                matrix_audit={"status": "PASS"},
                tiers=[],
            ),
            environment={},
            required_problems=[],
        )

    return prepare


@pytest.mark.parametrize("fail_fast", [False, True])
def test_return_boundary_interrupt_keeps_the_executed_tier(tmp_path, monkeypatch, fail_fast):
    """Regression: a SIGINT between the call and its bookkeeping kept nothing.

    A reviewer delivered a real SIGINT on the final ``return`` of
    ``run_prepared_tier``.  The completed :class:`TierResult` only existed in
    that frame, so the orchestrator synthesized an empty ``INTERRUPTED`` row:
    ``failed`` dropped from 1 to 0 and the diagnostics, output, and runtime
    identity vanished.  The tier must be published before the cancellation can
    escape the call.
    """

    row = "tests/test_matrix.py::test_pair[aaa-fail]"
    monkeypatch.setattr(gate, "prepare_runtime_gate", _matrix_row_prepare({"trade": (row,)}))
    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1), repo_root=tmp_path, sampler=make_facts
    )
    with _real_sigint_at_line(gate.run_prepared_tier, "    return tier\n") as signalled:
        result = gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=Path("python"),
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["trade"],
            required_tests_by_tier={},
            required_nodeids_by_tier={"trade": (row,)},
            timeout_override=5,
            capacity_session=session,
            fail_fast=fail_fast,
        )[0]

    assert signalled, "the regression did not deliver its signal"
    assert [tier.name for tier in result.tiers] == ["trade"], result.tiers
    tier = result.tiers[0]
    assert tier.counts.failed == 1, tier.counts
    assert "MATRIX-OUTPUT" in tier.output_tail, tier.output_tail
    assert any("runtime-identity-marker" in detail.reason for detail in tier.failure_details), (
        tier.failure_details
    )
    assert result.cancellation, result
    assert gate.runtime_gate_passes(result) is False
    # One planned row, one admission identity, and one completed lifecycle
    # entry: the completed tier must not be re-registered as unstarted.
    assert len(session.telemetry.pair_states) == 1, session.telemetry.pair_states
    assert session.telemetry.lifecycle["completed"] == 1, session.telemetry.lifecycle


def test_batch_tier_finalization_interrupt_keeps_loaded_failure_evidence(tmp_path, monkeypatch):
    """Regression: an ordinary batch tier lost its loaded failure on SIGINT.

    A reviewer delivered a real SIGINT after ``run_tier`` had loaded a failing
    child report but before the batch tier returned.  Only matrix tiers had the
    deferral guard, so the failure counts, diagnostics, and captured output were
    replaced by an empty ``INTERRUPTED`` row.
    """

    row = "tests/test_matrix.py::test_pair[aaa-fail]"

    def run_once(**kwargs):
        return (
            1,
            gate.GateReport(
                gate.Counts(total=1, failed=1),
                nodeids=(row,),
                failed_records=(
                    {"nodeid": row, "outcome": "failed", "reason": "REAL-FAILED-OUTCOME"},
                ),
            ),
            "RETAIN-FAILED-OUTPUT",
            ["pytest"],
        )

    monkeypatch.setattr(gate, "prepare_runtime_gate", _matrix_row_prepare({}))
    monkeypatch.setattr(gate, "run_pytest_once", run_once)
    with _real_sigint_at_line(gate.run_tier, "    if omitted_failures:\n") as signalled:
        result = gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=Path("python"),
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["unit"],
            required_tests_by_tier={},
            required_nodeids_by_tier={},
            timeout_override=5,
        )[0]

    assert signalled, "the regression did not deliver its signal"
    tier = result.tiers[0]
    assert tier.counts.failed == 1, tier.counts
    assert "RETAIN-FAILED-OUTPUT" in tier.output_tail, tier.output_tail
    assert any("REAL-FAILED-OUTCOME" in detail.reason for detail in tier.failure_details), (
        tier.failure_details
    )
    assert result.cancellation, result


def test_fail_fast_dispatch_and_finalization_share_one_row_identity(tmp_path, monkeypatch):
    """Regression: dispatch and finalization used different runtime scopes.

    The fail-fast and early-smoke branches called ``run_prepared_tier`` without
    the runtime scope, so admission identities used ``("", tier)`` while the
    cancellation and finalization paths used ``(mode, tier)``.  A SIGINT during
    the pre-dispatch blocked-row accounting therefore recorded the same planned
    row twice under two identities.  The scope is now derived from the prepared
    runtime, so the row is transitioned instead of duplicated.
    """

    row = "tests/test_matrix.py::test_pair[aaa-fail]"

    def prepare(**kwargs):
        prepared = _matrix_row_prepare({"trade": (row,)})(**kwargs)
        prepared.required_problems = ["required asset is unavailable"]
        return prepared

    monkeypatch.setattr(gate, "prepare_runtime_gate", prepare)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1), repo_root=tmp_path, sampler=make_facts
    )
    original_mark = session.telemetry.mark
    signalled: list[str] = []

    def mark_then_interrupt(pair_id, state):
        original_mark(pair_id, state)
        if not signalled:
            signalled.append(pair_id)
            os.kill(os.getpid(), signal.SIGINT)

    monkeypatch.setattr(session.telemetry, "mark", mark_then_interrupt)
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        result = gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=Path("python"),
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["trade"],
            required_tests_by_tier={},
            required_nodeids_by_tier={"trade": (row,)},
            timeout_override=5,
            capacity_session=session,
            fail_fast=True,
        )[0]
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    assert signalled, "the regression did not deliver its signal"
    assert len(session.telemetry.pair_states) == 1, session.telemetry.pair_states
    (pair_id,) = session.telemetry.pair_states
    assert pair_id.endswith(row), pair_id
    # The row was registered under the dispatched runtime scope, so neither
    # dispatch nor cancellation allocated a second identity for it.
    assert set(session._tier_run_ids) == {("source", "trade")}, session._tier_run_ids
    assert {key[0] for key in session._tier_blocked_rows} == {"source"}, session._tier_blocked_rows
    assert result.tiers[0].status == "INTERRUPTED", result.tiers[0]


def test_dispatch_return_boundary_interrupt_keeps_the_executed_tier(tmp_path, monkeypatch):
    """Regression: a SIGINT on the dispatch helper's own ``return`` dropped it.

    The round-9 review delivered a real SIGINT on ``_dispatch_prepared_tier``'s
    final line.  The completed :class:`TierResult` had already been loaded, but
    the fail-fast branch only recovered evidence the helper itself returned, so
    the signal was indistinguishable from a tier that never started: the loaded
    failure (``failed`` 1 -> 0), diagnostics, output, and runtime identity were
    replaced by an empty ``INTERRUPTED`` row.  Every dispatch caller now
    consults the durable mapping on the runtime result.
    """

    row = "tests/test_matrix.py::test_pair[aaa-fail]"
    monkeypatch.setattr(gate, "prepare_runtime_gate", _matrix_row_prepare({"trade": (row,)}))
    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1), repo_root=tmp_path, sampler=make_facts
    )
    with _real_sigint_at_line(gate._dispatch_prepared_tier, '    return tier, ""\n') as signalled:
        result = gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=Path("python"),
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["trade"],
            required_tests_by_tier={},
            required_nodeids_by_tier={"trade": (row,)},
            timeout_override=5,
            capacity_session=session,
            fail_fast=True,
        )[0]

    assert signalled, "the regression did not deliver its signal"
    assert [tier.name for tier in result.tiers] == ["trade"], result.tiers
    tier = result.tiers[0]
    assert tier.counts.failed == 1, tier.counts
    assert "MATRIX-OUTPUT" in tier.output_tail, tier.output_tail
    assert any("runtime-identity-marker" in detail.reason for detail in tier.failure_details), (
        tier.failure_details
    )
    assert result.cancellation, result
    assert gate.runtime_gate_passes(result) is False
    # The executed row keeps its single admission identity and is not
    # re-registered as an unstarted interruption.
    assert set(session._tier_run_ids) == {("source", "trade")}, session._tier_run_ids
    assert len(session.telemetry.pair_states) == 1, session.telemetry.pair_states


def test_matrix_supervisor_return_boundary_keeps_loaded_case_evidence(tmp_path, monkeypatch):
    """Regression: a SIGINT on ``run_matrix_tier``'s own ``return`` erased it.

    The assembled matrix tier only existed in the supervisor's frame.  A
    cancellation delivered after the finalization guard was restored but before
    the value reached ``run_tier`` escaped the whole dispatch chain, so the
    orchestrator replaced the executed rows (``failed`` 1) with an empty
    ``INTERRUPTED`` row.  The supervisor now publishes the assembled tier into
    the runtime's durable ``completed_tiers`` mapping while the guard is still
    installed.
    """

    row = "tests/test_matrix.py::test_pair[aaa-fail]"
    monkeypatch.setattr(gate, "prepare_runtime_gate", _matrix_row_prepare({"trade": (row,)}))
    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1), repo_root=tmp_path, sampler=make_facts
    )
    with _real_sigint_at_line(gate.run_matrix_tier, "    return tier_result\n") as signalled:
        result = gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=Path("python"),
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["trade"],
            required_tests_by_tier={},
            required_nodeids_by_tier={"trade": (row,)},
            timeout_override=5,
            capacity_session=session,
        )[0]

    assert signalled, "the regression did not deliver its signal"
    assert [tier.name for tier in result.tiers] == ["trade"], result.tiers
    tier = result.tiers[0]
    assert tier.counts.failed == 1, tier.counts
    assert "MATRIX-OUTPUT" in tier.output_tail, tier.output_tail
    assert any("runtime-identity-marker" in detail.reason for detail in tier.failure_details), (
        tier.failure_details
    )
    assert result.cancellation, result
    assert gate.runtime_gate_passes(result) is False


def test_tier_supervisor_return_boundary_keeps_loaded_failure_evidence(tmp_path, monkeypatch):
    """Regression: a SIGINT on ``run_tier``'s own ``return`` lost the batch tier.

    An ordinary batch tier was assembled and its finalization guard restored,
    but the completed :class:`TierResult` still only existed in that frame.  A
    cancellation landing between ``restore()`` and ``run_prepared_tier``
    receiving the value therefore discarded the loaded failure, its diagnostics,
    and its captured output, leaving an empty ``INTERRUPTED`` row.
    """

    row = "tests/test_matrix.py::test_pair[aaa-fail]"

    def run_once(**kwargs):
        return (
            1,
            gate.GateReport(
                gate.Counts(total=1, failed=1),
                nodeids=(row,),
                failed_records=(
                    {"nodeid": row, "outcome": "failed", "reason": "BATCH-RETURN-FAILURE"},
                ),
            ),
            "RETAIN-BATCH-OUTPUT",
            ["pytest"],
        )

    monkeypatch.setattr(gate, "prepare_runtime_gate", _matrix_row_prepare({}))
    monkeypatch.setattr(gate, "run_pytest_once", run_once)
    with _real_sigint_at_line(gate.run_tier, "    return tier_result\n") as signalled:
        result = gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=Path("python"),
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["unit"],
            required_tests_by_tier={},
            required_nodeids_by_tier={},
            timeout_override=5,
        )[0]

    assert signalled, "the regression did not deliver its signal"
    tier = result.tiers[0]
    # The tier did execute and did fail; only the cancellation is separate, so a
    # real failure is never laundered into an empty ``INTERRUPTED`` row.
    assert tier.status == "FAIL", tier.status
    assert tier.counts.failed == 1, tier.counts
    assert "RETAIN-BATCH-OUTPUT" in tier.output_tail, tier.output_tail
    assert any("BATCH-RETURN-FAILURE" in detail.reason for detail in tier.failure_details), (
        tier.failure_details
    )
    assert result.cancellation, result


def test_cancellation_between_timing_repetitions_keeps_loaded_iteration(tmp_path, monkeypatch):
    """Regression: a SIGINT between timing repetitions discarded iteration 1.

    The timing tier repeats its expression at least five times.  A cancellation
    delivered after the first repetition's child report was loaded, but before
    the second repetition started, escaped past the repetition loop and threw
    away the counts, diagnostics, output tail and return codes already
    collected.  The loop now records the cancellation so the partial tier is
    finalized with its loaded evidence.
    """

    row = "tests/test_matrix.py::test_pair[aaa-fail]"
    iterations: list[int] = []

    def run_once(**kwargs):
        iterations.append(len(iterations) + 1)
        if len(iterations) > 1:
            # The first repetition has already been loaded; deliver the
            # cancellation on the boundary to the next one.
            os.kill(os.getpid(), signal.SIGINT)
        return (
            1,
            gate.GateReport(
                gate.Counts(total=1, failed=1),
                nodeids=(row,),
                failed_records=(
                    {"nodeid": row, "outcome": "failed", "reason": "TIMING-ITERATION-FAILURE"},
                ),
            ),
            "RETAIN-TIMING-OUTPUT",
            ["pytest"],
        )

    monkeypatch.setattr(gate, "prepare_runtime_gate", _matrix_row_prepare({}))
    monkeypatch.setattr(gate, "run_pytest_once", run_once)
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        result = gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=Path("python"),
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["timing"],
            required_tests_by_tier={},
            required_nodeids_by_tier={},
            timeout_override=5,
        )[0]
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    assert len(iterations) == 2, iterations
    tier = result.tiers[0]
    assert tier.name == "timing", tier.name
    assert tier.status == "INTERRUPTED", tier.status
    assert tier.counts.total == 1, tier.counts
    assert tier.counts.failed == 1, tier.counts
    assert "RETAIN-TIMING-OUTPUT" in tier.output_tail, tier.output_tail
    assert any("TIMING-ITERATION-FAILURE" in detail.reason for detail in tier.failure_details), (
        tier.failure_details
    )
    assert result.cancellation, result


def test_capacity_gated_matrix_dispatch_threads_the_publish_sink(tmp_path, monkeypatch):
    """Regression: the capacity-gated dispatch dropped the runtime's sink.

    Real-ROM tiers that are not dispatched from a strict nodeid manifest hand
    their rows to ``run_matrix_tier`` from inside ``run_tier``.  That call site
    did not forward the publish sink, so the very same return-boundary
    cancellation recovered for trade/battle erased the executed rows here.
    """

    row = "tests/test_matrix.py::test_pair[aaa-fail]"
    monkeypatch.setattr(gate, "planned_tier_nodeids", lambda *args, **kwargs: ((row,), ""))
    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1), repo_root=tmp_path, sampler=make_facts
    )
    published: list[gate.TierResult] = []
    with (
        _real_sigint_at_line(gate.run_matrix_tier, "    return tier_result\n") as signalled,
        pytest.raises(KeyboardInterrupt),
    ):
        gate.run_tier(
            name="local",
            project_root=tmp_path,
            python_executable=Path("python"),
            environment={},
            required_problems=[],
            repeat=1,
            timeout_override=5,
            report_directory=tmp_path,
            required_nodeids=(row,),
            matrix_workers=1,
            capacity_session=session,
            collections=(gate.CollectionResult("python-module", [], "PASS", 0, nodeids=(row,)),),
            publish=published.append,
        )

    assert signalled, "the regression did not deliver its signal"
    assert len(published) == 1, published
    (tier,) = published
    assert tier.name == "local", tier.name
    assert tier.counts.failed == 1, tier.counts
    assert "MATRIX-OUTPUT" in tier.output_tail, tier.output_tail


def test_caller_append_boundary_interrupt_is_finalized_with_the_executed_tier(
    tmp_path, monkeypatch
):
    """Regression: a SIGINT before the caller's append erased loaded evidence.

    The round-9 review showed the completed tier was only reachable from the
    dispatching frame: a signal delivered between the dispatch helper's return
    and ``run_runtime_gates``' ``prepared.result.tiers.append(tier)`` escaped
    the whole orchestration, so the CLI fallback rebuilt the runtime from the
    plan and produced an empty ``INTERRUPTED`` tier with ``failed`` 0.  The
    finalizer now recovers the executed tier through the runtime result's
    durable mapping.
    """

    row = "tests/test_matrix.py::test_pair[aaa-fail]"
    monkeypatch.setattr(gate, "prepare_runtime_gate", _matrix_row_prepare({"trade": (row,)}))
    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1), repo_root=tmp_path, sampler=make_facts
    )
    accumulated: list[gate.RuntimeGateResult] = []
    append_line = "            prepared.result.tiers.append(tier)\n"
    with (
        _real_sigint_at_line(gate.run_runtime_gates, append_line) as signalled,
        pytest.raises(KeyboardInterrupt),
    ):
        gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=Path("python"),
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["trade"],
            required_tests_by_tier={},
            required_nodeids_by_tier={"trade": (row,)},
            timeout_override=5,
            capacity_session=session,
            fail_fast=True,
            accumulator=accumulated,
        )

    assert signalled, "the regression did not deliver its signal"
    assert len(accumulated) == 1, accumulated
    plan = gate.build_execution_plan(("source",), ["trade"], early_smoke=False, fail_fast=True)
    finalized = gate._finalize_interrupted_results(
        accumulated=accumulated,
        modes=("source",),
        plan=plan,
        reason="gate cancelled by an interrupt",
        required_nodeids_by_tier={"trade": (row,)},
        capacity_session=session,
        project_root=tmp_path,
    )

    assert len(finalized) == 1, finalized
    result = finalized[0]
    assert [tier.name for tier in result.tiers] == ["trade"], result.tiers
    tier = result.tiers[0]
    assert tier.counts.failed == 1, tier.counts
    assert "MATRIX-OUTPUT" in tier.output_tail, tier.output_tail
    assert result.cancellation, result
    assert gate.runtime_gate_passes(result) is False
    assert set(session._tier_run_ids) == {("source", "trade")}, session._tier_run_ids
    assert len(session.telemetry.pair_states) == 1, session.telemetry.pair_states


def test_skipped_runtime_rows_keep_the_plan_row_identity(tmp_path, monkeypatch):
    """Regression: skipped rows were registered under the empty runtime scope.

    The round-9 review interrupted the non-fail-fast dual-runtime branch while
    it registered the rows of the runtime that never started.  That accounting
    called ``_unrun_tier`` without the runtime scope, so the skipped native row
    was admitted as ``("", tier)`` while the CLI's finalizer used
    ``(mode, tier)``.  A second SIGINT therefore left three lifecycle rows for
    two planned rows.  Both paths must agree on the plan row identity.
    """

    row = "tests/test_matrix.py::test_pair[aaa-fail]"
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1), repo_root=tmp_path, sampler=make_facts
    )
    accumulated: list[gate.RuntimeGateResult] = []
    armed = {"value": False}
    signalled: list[str] = []
    original_mark = session.telemetry.mark

    def mark_then_interrupt(pair_id, state):
        original_mark(pair_id, state)
        if armed["value"] and not signalled:
            signalled.append(pair_id)
            os.kill(os.getpid(), signal.SIGINT)

    monkeypatch.setattr(session.telemetry, "mark", mark_then_interrupt)

    def interrupted_runtime(**kwargs):
        mode = kwargs["mode"]
        result = gate._unstarted_preparation(mode, "source was interrupted").result
        result.cancellation = "source was interrupted"
        result.tiers = [
            gate._unrun_tier(
                "trade",
                "source was interrupted",
                (row,),
                status="INTERRUPTED",
                capacity_session=session,
                collections=result.collections,
                project_root=tmp_path,
                scope=mode,
            )
        ]
        # Only the runtime that is skipped *because* the first one stopped may
        # deliver the second signal; the first runtime's own rows are recorded
        # normally.
        armed["value"] = True
        accumulated.append(result)
        return result

    monkeypatch.setattr(gate, "run_runtime_gate", interrupted_runtime)
    previous_handler = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            gate.run_runtime_gates(
                runtime_mode="both",
                project_root=tmp_path,
                python_executable=Path("python"),
                cython_python_executable=Path("python"),
                rom_root=tmp_path / "rom",
                fixture_root=tmp_path / "fixtures",
                expected_sha1={},
                assets=[],
                selected=["trade"],
                required_tests_by_tier={},
                required_nodeids_by_tier={"trade": (row,)},
                timeout_override=5,
                capacity_session=session,
                accumulator=accumulated,
            )
    finally:
        signal.signal(signal.SIGINT, previous_handler)

    assert signalled, "the regression did not deliver its signal"
    plan = gate.build_execution_plan(
        ("source", "cython"), ["trade"], early_smoke=False, fail_fast=False
    )
    finalized = gate._finalize_interrupted_results(
        accumulated=accumulated,
        modes=("source", "cython"),
        plan=plan,
        reason="gate cancelled by an interrupt",
        required_nodeids_by_tier={"trade": (row,)},
        capacity_session=session,
        project_root=tmp_path,
    )

    # Two planned rows, two admission identities: the skipped runtime's row is
    # transitioned by the finalizer instead of being allocated a second one.
    assert set(session._tier_run_ids) == {("source", "trade"), ("cython", "trade")}, (
        session._tier_run_ids
    )
    assert {key[0] for key in session._tier_blocked_rows} == {"source", "cython"}, (
        session._tier_blocked_rows
    )
    assert len(session.telemetry.pair_states) == 2, session.telemetry.pair_states
    assert [result.mode for result in finalized] == ["source", "cython"], finalized
    assert all(tier.status == "INTERRUPTED" for result in finalized for tier in result.tiers)


def test_early_smoke_return_boundary_interrupt_keeps_the_executed_smoke(tmp_path, monkeypatch):
    """Regression: the early-smoke branch dropped a smoke that already ran.

    The additional smoke is dispatched through the same helper as the selected
    tiers, so the same SIGINT on the helper's ``return`` used to make the
    early-smoke branch overwrite the executed smoke row with an empty
    ``INTERRUPTED`` one.  The branch now consults the runtime result's durable
    mapping before declaring the smoke unstarted.
    """

    row = "tests/test_matrix.py::test_pair[aaa-fail]"
    monkeypatch.setattr(gate, "prepare_runtime_gate", _matrix_row_prepare({"smoke": (row,)}))
    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=1), repo_root=tmp_path, sampler=make_facts
    )
    with _real_sigint_at_line(gate._dispatch_prepared_tier, '    return tier, ""\n') as signalled:
        result = gate.run_runtime_gates(
            runtime_mode="source",
            project_root=tmp_path,
            python_executable=Path("python"),
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=["trade"],
            required_tests_by_tier={},
            required_nodeids_by_tier={"trade": (row,)},
            timeout_override=5,
            capacity_session=session,
            early_smoke=True,
        )[0]

    assert signalled, "the regression did not deliver its signal"
    assert [tier.name for tier in result.tiers] == ["smoke", "trade"], result.tiers
    smoke = result.tiers[0]
    # The dispatched smoke's own decision is retained.  Before the fix the
    # branch replaced it with a synthesized INTERRUPTED row whose status and
    # reason were indistinguishable from a smoke that never ran.
    assert smoke.status == "BLOCKED", smoke
    assert smoke.reason.startswith("capacity could not enumerate planned rows for tier smoke:"), (
        smoke.reason
    )
    assert result.cancellation, result
    assert result.tiers[1].status == "NOT_STARTED", result.tiers[1]
    assert gate.runtime_gate_passes(result) is False
