"""Authored controls: comparison of two complete re-validated bundles (#107.5, finding 10).

The bundles are fabricated from authored artifacts.  A COMPARED result here proves the
interface validates identities, selection and capacity and takes elapsed time from the
validated artifacts; it is not the real-ROM six-tier comparison.
"""

import copy

import pytest

from scripts.production_gate_parallel_artifacts import aggregate_outcomes, outcome_record
from scripts.production_gate_parallel_comparison import compare_bundles, equivalence_digest
from tests._parallel_runtime_report_support import TOKEN, good_artifact, make_plan


def bundle(mode, *, seconds, run_id, **plan_overrides):
    plan = make_plan(run_id=run_id, **plan_overrides)
    outcomes = []
    for job in plan.as_dict()["jobs"]:
        artifact = good_artifact(
            plan,
            job["job_id"],
            started="2026-10-09T00:00:00+00:00",
            finished=f"2026-10-09T00:{seconds // 60:02d}:{seconds % 60:02d}+00:00",
        )
        record = outcome_record(
            plan,
            job["job_id"],
            "passed",
            child_exit_status=0,
            artifact=artifact,
            artifact_problems=[],
        )
        record["launch_token"] = TOKEN
        outcomes.append(record)
    report = aggregate_outcomes(plan, outcomes)
    report.update(
        {
            "plan": plan.text,
            "execution_mode": mode,
            "orchestration_problems": [],
            "resource_trace": {"affinity_cpus": 6, "loadavg_start": [0.1], "loadavg_end": [0.2]},
        }
    )
    return report


def test_equivalent_complete_bundles_are_compared_from_validated_artifact_timestamps():
    result = compare_bundles(
        bundle("concurrent", seconds=10, run_id="1" * 32),
        bundle("serial", seconds=20, run_id="2" * 32),
    )
    assert result["status"] == "COMPARED"
    assert (result["concurrent_seconds"], result["serial_seconds"], result["speedup"]) == (
        10.0,
        20.0,
        2.0,
    )
    assert set(result["resource_traces"]) == {"concurrent", "serial"}


@pytest.mark.parametrize(
    ("label", "overrides", "needle"),
    [
        ("different commit", {"commit": "9" * 40}, "plans differ"),
        ("different selection", {"tiers": ("unit",)}, "plans differ"),
        ("different capacity", {"policy_digest": "8" * 64}, "plans differ"),
        ("different assets", {"asset_digest": "7" * 64}, "plans differ"),
    ],
)
def test_non_equivalent_conditions_are_refused(label, overrides, needle):
    other = bundle("serial", seconds=20, run_id="2" * 32, **overrides)
    result = compare_bundles(bundle("concurrent", seconds=10, run_id="1" * 32), other)
    assert result["status"] == "REFUSED" and any(needle in item for item in result["problems"]), (
        label
    )


def test_the_legacy_pass_plus_elapsed_dictionary_is_not_a_bundle():
    legacy = {"overall": "PASS", "elapsed_seconds": 20, "commit": "a" * 40}
    result = compare_bundles(bundle("concurrent", seconds=10, run_id="1" * 32), legacy)
    assert result["status"] == "REFUSED"


def test_a_failed_job_a_tampered_artifact_or_a_wrong_mode_is_refused():
    good = bundle("concurrent", seconds=10, run_id="1" * 32)
    failed = copy.deepcopy(bundle("serial", seconds=20, run_id="2" * 32))
    failed["jobs"][0]["state"] = "failed"
    tampered = copy.deepcopy(bundle("serial", seconds=20, run_id="3" * 32))
    tampered["jobs"][1]["artifact"]["overall"] = "FAIL"
    same_mode = bundle("concurrent", seconds=20, run_id="4" * 32)
    no_trace = copy.deepcopy(bundle("serial", seconds=20, run_id="5" * 32))
    del no_trace["resource_trace"]
    for other in (failed, tampered, same_mode, no_trace):
        assert compare_bundles(good, other)["status"] == "REFUSED"


def test_the_same_run_cannot_be_compared_with_itself():
    one = bundle("concurrent", seconds=10, run_id="1" * 32)
    other = copy.deepcopy(one)
    other["execution_mode"] = "serial"
    assert any("same run" in item for item in compare_bundles(one, other)["problems"])


def test_the_equivalence_digest_ignores_only_run_identity_and_time():
    a = make_plan(run_id="1" * 32, created_utc="2026-10-09T00:00:00+00:00").as_dict()
    b = make_plan(run_id="2" * 32, created_utc="2026-10-09T01:00:00+00:00").as_dict()
    assert equivalence_digest(a) == equivalence_digest(b)
    assert equivalence_digest(a) != equivalence_digest(make_plan(fail_fast=True).as_dict())
