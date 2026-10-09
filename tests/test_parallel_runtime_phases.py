"""Authored controls: both additional smokes finish before selected tiers (#107.4, finding 7)."""

from scripts.production_gate_parallel_phases import (
    barrier_decision,
    jobs_in_phase,
    may_cancel_siblings,
    phase_names,
)
from tests._parallel_runtime_report_support import make_plan


def test_the_plan_declares_a_smoke_job_per_runtime_before_the_selected_jobs():
    body = make_plan(early_smoke=True).as_dict()
    assert phase_names(body) == ["smoke", "selected"]
    assert [j["job_id"] for j in jobs_in_phase(body, "smoke")] == [
        "source-smoke-job",
        "cython-smoke-job",
    ]
    assert [j["job_id"] for j in jobs_in_phase(body, "selected")] == ["source-job", "cython-job"]
    smoke = jobs_in_phase(body, "smoke")[0]
    assert smoke["tiers"] == ["smoke"] and smoke["timing_rep_ids"] == []
    assert smoke["steps"] == [{"mode": "source", "tier": "smoke", "role": "additional-probe"}]


def test_without_the_early_smoke_only_the_selected_phase_exists():
    body = make_plan(early_smoke=False).as_dict()
    assert phase_names(body) == ["selected"] and len(body["jobs"]) == 2


def test_a_failed_smoke_never_cancels_its_sibling_smoke_even_with_fail_fast():
    assert may_cancel_siblings("smoke", True) is False
    assert may_cancel_siblings("selected", True) is True
    assert may_cancel_siblings("selected", False) is False


def test_the_barrier_waits_for_both_outcomes_and_fail_fast_only_gates_the_next_phase():
    passed = {"job_id": "source-smoke-job", "state": "passed"}
    failed = {"job_id": "cython-smoke-job", "state": "failed"}
    assert barrier_decision([passed, passed], fail_fast=True) == (True, "")
    go, reason = barrier_decision([failed, passed], fail_fast=True)
    assert go is False and "cython-smoke-job" in reason
    assert barrier_decision([passed, failed], fail_fast=True)[0] is False
    assert barrier_decision([failed, failed], fail_fast=False) == (True, "")
