"""Policy, fact validation, and admission semantics for the capacity gate."""

from __future__ import annotations

import json

import pytest

from scripts import gate_capacity
from tests._gate_capacity_support import (
    FakeClock,
    FakeMatrixPopen,
    SequenceSampler,
    gate,
    make_facts,
    make_policy,
)


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


@pytest.mark.parametrize("field", ["observation_seconds", "admission_deadline_seconds"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_policy_rejects_nonfinite_timing(field, value):
    assert any(
        field in problem for problem in gate_capacity.validate_policy(make_policy(**{field: value}))
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


def test_retry_budget_lapse_never_overwrites_determinate_verdict(tmp_path):
    """Regression: a shrinking retry budget must not invent a collection failure.

    ``wait_until_available`` re-observes with whatever is left of the admission
    deadline, so each retry is handed a smaller budget than the last and the
    final one shrinks toward zero.  A collector that cannot finish inside that
    shard lapses, and the lapse used to replace the real policy verdict with
    "capacity collection exceeded admission deadline" - a cause the host never
    demonstrated.  The gate then reported a collection error instead of the
    actionable reason ("effective CPUs 1 below declared minimum 4") that the
    first real sample had already established.

    The injected clock advances far past the declared deadline, so the retry
    budget is a shard by construction and the test does not depend on how
    slowly this host happens to schedule a thread.  The declared first
    collection keeps a realistic budget, so *it* is never the thing under test.
    """

    import threading

    clock = FakeClock()
    release = threading.Event()
    calls = 0

    def sampler():
        nonlocal calls
        calls += 1
        if calls == 1:
            # The one real collection: instantly reports insufficient capacity,
            # so the policy verdict is determinate before any retry runs.
            return make_facts(affinity_count=1, affinity_cpus=[0], cpu_quota_cores=1.0)
        # Far longer than every retry budget the loop can hand out.
        release.wait(5)
        return make_facts(affinity_count=1, affinity_cpus=[0], cpu_quota_cores=1.0)

    def jump(_seconds):
        # One sleep step lands just short of the declared deadline, so the next
        # retry is handed a small fraction of it.
        clock.advance(0.97)

    session = gate_capacity.CapacitySession(
        make_policy(observation_seconds=0.01, admission_deadline_seconds=1.0),
        repo_root=tmp_path,
        clock=clock,
        sampler=sampler,
    )
    try:
        assert session.wait_until_available(sleep=jump) == "blocked"
        reason = "; ".join(session.availability_reasons)
        assert "below declared minimum" in reason
        assert "exceeded" not in reason
        # A retry really was attempted and really did lapse, so the verdict
        # above is the restored determinate one rather than an untried loop.
        assert calls > 1
        assert session.telemetry.collection_failures >= 1
        failed = [sample for sample in session.telemetry.samples if sample.status == "failed"]
        assert failed
        assert "remaining admission budget" in failed[0].problems[0]
    finally:
        release.set()


def test_declared_deadline_lapse_is_still_reported_as_unsupported(tmp_path):
    """A lapse of the declared deadline stays real evidence, not a retry shard."""

    import threading

    release = threading.Event()

    def sampler():
        release.wait(2)
        return make_facts()

    session = gate_capacity.CapacitySession(
        make_policy(admission_deadline_seconds=0.02), repo_root=tmp_path, sampler=sampler
    )
    try:
        assert session.wait_until_available() == "unsupported"
        assert "exceeded admission deadline" in "; ".join(session.availability_reasons)
    finally:
        release.set()
