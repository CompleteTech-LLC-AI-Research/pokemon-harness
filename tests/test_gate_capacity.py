"""ROM-free tests for the versioned capacity policy, admission, and telemetry."""

from __future__ import annotations

import importlib.util
import io
import json
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
        "cpu_throttled": None,
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
        "policy_version": 1,
        "runner_id": "runner-86",
        "effective_cpus": 4,
        "max_concurrent_pairs": 2,
        "memory_bytes_min": 1024,
        "disk_free_bytes_min": 1024,
        "observation_seconds": 0.0,
        "admission_deadline_seconds": 10.0,
    }
    values.update(overrides)
    return gate_capacity.CapacityPolicy(**values)


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
        gate_capacity.capacity_policy_from_dict({"policy_version": 1})
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
    assert decision.status == "blocked"
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
    assert session.admission.admit("pair-2").status == "blocked"
    assert session.admission.admitted_count == 1


def test_inaccessible_and_malformed_telemetry_are_explicit(tmp_path):
    def boom():
        raise RuntimeError("telemetry-unavailable")

    failing = gate_capacity.CapacitySession(make_policy(), repo_root=tmp_path, sampler=boom)
    assert failing.begin() == "unsupported"
    assert failing.admission.admit("pair-1").status == "blocked"

    malformed = gate_capacity.CapacitySession(
        make_policy(), repo_root=tmp_path, sampler=lambda: {"platform": "x"}
    )
    assert malformed.begin() == "unsupported"
    assert malformed.telemetry.samples[0].status == "malformed"
    assert malformed.admission.admit("pair-1").status == "blocked"


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
    monkeypatch.setattr(gate.subprocess, "Popen", FakeMatrixPopen)
    session = gate_capacity.CapacitySession(
        make_policy(max_concurrent_pairs=2),
        repo_root=tmp_path,
        sampler=SequenceSampler(
            [
                make_facts(),
                make_facts(),
                make_facts(affinity_count=1, affinity_cpus=[0], cpu_quota_cores=1.0),
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
        make_policy(),
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
        make_policy(),
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
