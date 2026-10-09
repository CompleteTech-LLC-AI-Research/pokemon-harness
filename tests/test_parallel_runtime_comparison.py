"""Authored controls: comparison of two complete re-validated bundles (#107.5, finding 10).

The bundles are fabricated from authored artifacts.  A COMPARED result here proves the interface
validates identities, selection, capacity, resources, lifecycle proofs and the REAL execution
intervals (serial non-overlap, concurrent overlap, smoke before selected) and takes elapsed time
from the parent-observed intervals; it is not the real-ROM six-tier comparison.
"""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts.production_gate_parallel_artifacts import aggregate_outcomes, outcome_record
from scripts.production_gate_parallel_comparison import compare_bundles, equivalence_digest
from tests._parallel_runtime_report_support import TOKEN, good_artifact, make_plan

ORIGIN = datetime(2026, 10, 9, 0, 0, 0, tzinfo=UTC)
CONCURRENT = [(0, 10), (1, 11)]
SERIAL = [(0, 10), (10.5, 20.5)]


def stamp(seconds):
    return (ORIGIN + timedelta(seconds=seconds)).isoformat()


def bundle(mode, intervals, *, run_id, **plan_overrides):
    plan = make_plan(run_id=run_id, **plan_overrides)
    outcomes, cleanup = [], {}
    for job, (start, end) in zip(plan.as_dict()["jobs"], intervals, strict=True):
        artifact = good_artifact(plan, job["job_id"], started=stamp(start), finished=stamp(end))
        record = outcome_record(
            plan,
            job["job_id"],
            "passed",
            child_exit_status=0,
            artifact=artifact,
            artifact_problems=[],
            launched_utc=stamp(start),
            finished_utc=stamp(end),
        )
        record["launch_token"] = TOKEN
        outcomes.append(record)
        cleanup[job["job_id"]] = {
            "process_tree": {"proof": True, "contained": True},
            "guardian": {
                "phase": "final",
                "proof": True,
                "contained": True,
                "rusage": {"utime": 4.0, "stime": 1.0, "maxrss_kb": 250000},
            },
        }
    report = aggregate_outcomes(plan, outcomes)
    report.update(
        {
            "plan": plan.text,
            "execution_mode": mode,
            "orchestration_problems": [],
            "cleanup": cleanup,
            "capacity_observations": [{"event": "probe", "available": 2}],
            "resource_trace": {"affinity_cpus": 6, "loadavg_start": [0.1], "loadavg_end": [0.2]},
        }
    )
    return report


def pair(**overrides):
    return (
        bundle("concurrent", CONCURRENT, run_id="1" * 32),
        bundle("serial", overrides.pop("serial_intervals", SERIAL), run_id="2" * 32, **overrides),
    )


def refused(concurrent, serial, needle):
    result = compare_bundles(concurrent, serial)
    assert result["status"] == "REFUSED", result
    assert any(needle in item for item in result["problems"]), result["problems"]


def test_equivalent_bundles_are_compared_from_parent_observed_intervals():
    result = compare_bundles(*pair())
    assert result["status"] == "COMPARED"
    assert (result["concurrent_seconds"], result["serial_seconds"]) == (11.0, 20.5)
    assert result["speedup"] == round(20.5 / 11.0, 4)
    assert set(result["resource_traces"]) == {"concurrent", "serial"}


def test_a_serial_label_without_serial_intervals_is_refused():
    concurrent = bundle("concurrent", CONCURRENT, run_id="1" * 32)
    fake_serial = bundle("serial", [(0, 10), (0, 10)], run_id="2" * 32)  # same start for every job
    refused(concurrent, fake_serial, "serial bundle has overlapping job intervals")


def test_a_concurrent_label_without_overlapping_runtimes_is_refused():
    refused(
        bundle("concurrent", SERIAL, run_id="1" * 32),
        bundle("serial", SERIAL, run_id="2" * 32),
        "never overlapped",
    )


def test_every_smoke_job_must_end_before_any_selected_job_starts():
    overlapping_smoke = [(0, 5), (0, 5), (3, 9), (3, 9)]
    concurrent = bundle("concurrent", overlapping_smoke, run_id="1" * 32, early_smoke=True)
    serial = bundle(
        "serial", [(0, 5), (5.5, 10), (10.5, 15), (15.5, 20)], run_id="2" * 32, early_smoke=True
    )
    refused(concurrent, serial, "started before every smoke job had ended")


def test_lifecycle_proofs_are_required_for_every_job():
    concurrent, serial = pair()
    del serial["cleanup"]["source-job"]
    refused(concurrent, serial, "no lifecycle record")
    concurrent, serial = pair()
    serial["cleanup"]["cython-job"]["guardian"]["phase"] = "ready"
    refused(concurrent, serial, "guardian's authenticated final proof is missing")
    concurrent, serial = pair()
    concurrent["cleanup"]["source-job"]["process_tree"]["proof"] = False
    refused(concurrent, serial, "parent's own cleanup proof is missing")


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda b: b["resource_trace"].update(affinity_cpus=0), "no CPU affinity"),
        (lambda b: b["resource_trace"].update(loadavg_start=[]), "not measured load"),
        (lambda b: b["resource_trace"].update(loadavg_end=["x"]), "not measured load"),
        (lambda b: b.update(resource_trace="trace"), "resource trace is missing"),
        (lambda b: b.update(capacity_observations=[{"event": "loss"}]), "capacity was lost"),
        (lambda b: b.update(capacity_observations="none"), "capacity observations are missing"),
    ],
)
def test_resource_trace_and_capacity_observations_must_be_measured(mutate, needle):
    concurrent, serial = pair()
    mutate(serial)
    refused(concurrent, serial, needle)


def test_different_cpu_affinity_is_not_equivalent_conditions():
    concurrent, serial = pair()
    serial["resource_trace"]["affinity_cpus"] = 3
    refused(concurrent, serial, "different CPU affinity")


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"commit": "9" * 40}, "plans differ"),
        ({"tiers": ("unit",)}, "plans differ"),
        ({"policy_digest": "8" * 64}, "plans differ"),
        ({"asset_digest": "7" * 64}, "plans differ"),
    ],
)
def test_non_equivalent_conditions_are_refused(overrides, needle):
    refused(
        bundle("concurrent", CONCURRENT, run_id="1" * 32),
        bundle("serial", SERIAL, run_id="2" * 32, **overrides),
        needle,
    )


def test_hostile_bundles_are_refused_without_an_exception():
    concurrent, serial = pair()
    hostile = copy.deepcopy(serial)
    hostile["jobs"].append("not an object")
    assert compare_bundles(concurrent, hostile)["status"] == "REFUSED"
    hostile = copy.deepcopy(serial)
    hostile["jobs"][0]["artifact"] = ["x"]
    assert compare_bundles(concurrent, hostile)["status"] == "REFUSED"
    for junk in ({"overall": "PASS", "elapsed_seconds": 20}, "text", None, [], {"plan": 5}):
        assert compare_bundles(concurrent, junk)["status"] == "REFUSED"


def test_a_failed_job_a_tampered_artifact_or_a_wrong_mode_is_refused():
    concurrent, serial = pair()
    failed = copy.deepcopy(serial)
    failed["jobs"][0]["state"] = "failed"
    tampered = copy.deepcopy(serial)
    tampered["jobs"][1]["artifact"]["overall"] = "FAIL"
    same_mode = copy.deepcopy(serial)
    same_mode["execution_mode"] = "concurrent"
    for other in (failed, tampered, same_mode):
        assert compare_bundles(concurrent, other)["status"] == "REFUSED"


def test_the_same_run_cannot_be_compared_with_itself():
    concurrent, _ = pair()
    other = copy.deepcopy(concurrent)
    other["execution_mode"] = "serial"
    assert any("same run" in item for item in compare_bundles(concurrent, other)["problems"])


def test_the_equivalence_digest_ignores_only_run_identity_and_time():
    a = make_plan(run_id="1" * 32, created_utc="2026-10-09T00:00:00+00:00").as_dict()
    b = make_plan(run_id="2" * 32, created_utc="2026-10-09T01:00:00+00:00").as_dict()
    assert equivalence_digest(a) == equivalence_digest(b)
    assert equivalence_digest(a) != equivalence_digest(make_plan(fail_fast=True).as_dict())


def test_measured_resources_allocation_and_cumulative_deadlines_are_reported():
    result = compare_bundles(*pair())
    assert result["status"] == "COMPARED"
    assert result["measured_resources"]["concurrent"] == {
        "cpu_seconds": 10.0,
        "max_rss_kb": 250000.0,
    }
    assert result["measured_resources"]["serial"] == result["measured_resources"]["concurrent"]


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (
            lambda c, s: c["cleanup"]["source-job"]["guardian"].pop("rusage"),
            "no measured resource usage",
        ),
        (
            lambda c, s: s["cleanup"]["cython-job"]["guardian"]["rusage"].update(utime=-1.0),
            "no measured resource usage",
        ),
        (
            lambda c, s: s["cleanup"]["cython-job"]["guardian"]["rusage"].update(utime=900.0),
            "very different CPU time",
        ),
    ],
)
def test_missing_negative_or_wildly_different_measurements_are_refused(mutate, needle):
    concurrent, serial = pair()
    mutate(concurrent, serial)
    refused(concurrent, serial, needle)


def test_a_run_past_its_cumulative_deadline_is_refused():
    slow = [(0, 500), (500.5, 1000)]
    refused(
        bundle("concurrent", CONCURRENT, run_id="1" * 32),
        bundle("serial", slow, run_id="2" * 32),
        "ran past its derived deadline",
    )


def test_a_phase_that_allocates_more_pairs_than_the_single_budget_is_refused():
    concurrent, serial = pair()
    for bundle_report in (concurrent, serial):
        plan = json.loads(bundle_report["plan"])
        for job in plan["jobs"]:
            job["pairs"] = 2
        bundle_report["plan"] = json.dumps(plan)
    result = compare_bundles(concurrent, serial)
    assert result["status"] == "REFUSED"
