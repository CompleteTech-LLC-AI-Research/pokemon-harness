"""Authored controls for the immutable shared plan of concurrent runtimes (#107.1).

These are deterministic unit controls over the plan model.  They start no gate,
no emulator and no ROM, and they do not qualify gameplay or performance.
"""

import json

import pytest

from scripts.production_gate_parallel_plan import (
    PlanError,
    build_parallel_plan,
    canonical_json,
    load_plan,
    validate_plan_body,
)

REQUIRED = {
    "unit": ["tests/test_a.py::test_one", "tests/test_a.py::test_two"],
    "timing": ["tests/test_t.py::test_time"],
}


def make_plan(**overrides):
    arguments = {
        "commit": "a" * 40,
        "build_identity": {"source": "e" * 64, "cython": "f" * 64},
        "input_digests": {"VERSIONS.md": "b" * 64},
        "asset_digest": "c" * 64,
        "games": ("red", "blue"),
        "roles": ("listener", "connector"),
        "modes": ("source", "cython"),
        "tiers": ("unit", "timing"),
        "required_nodeids_by_tier": REQUIRED,
        "repetitions": 5,
        "deadline_seconds": 30.0,
        "total_pairs": 2,
        "policy_digest": "d" * 64,
        "early_smoke": False,
        "fail_fast": False,
    }
    arguments.update(overrides)
    return build_parallel_plan(**arguments)


def test_the_frozen_plan_pins_every_identity_before_dispatch():
    body = make_plan(run_id="1" * 32).as_dict()
    assert body["run_id"] == "1" * 32
    assert body["commit"] == "a" * 40
    assert body["build_identity"] == {"cython": "f" * 64, "source": "e" * 64}
    assert body["asset_digest"] == "c" * 64
    assert body["input_digests"] == {"VERSIONS.md": "b" * 64}
    assert body["tiers"] == ["unit", "timing"]
    assert body["repetitions"] == 5
    assert body["games"] == ["red", "blue"]
    assert body["roles"] == ["listener", "connector"]
    assert body["required_nodeids"]["unit"] == sorted(REQUIRED["unit"])
    assert body["capacity"] == {"total_pairs": 2, "policy_digest": "d" * 64}
    assert body["fail_fast"] is False
    assert [job["mode"] for job in body["jobs"]] == ["source", "cython"]
    assert [job["pairs"] for job in body["jobs"]] == [1, 1]
    assert all(job["deadline_seconds"] == 30.0 for job in body["jobs"])
    assert body["jobs"][0]["timing_rep_ids"] == [f"timing-rep-{n}" for n in range(1, 6)]


def test_the_plan_text_round_trips_and_its_digest_is_stable():
    plan = make_plan(run_id="2" * 32, created_utc="2026-10-09T00:00:00+00:00")
    again = make_plan(run_id="2" * 32, created_utc="2026-10-09T00:00:00+00:00")
    assert plan.text == again.text
    assert plan.digest == again.digest
    loaded = load_plan(plan.text)
    assert loaded.digest == plan.digest
    assert canonical_json(json.loads(plan.text)) == plan.text


def test_a_caller_cannot_mutate_the_frozen_plan_through_a_returned_copy():
    plan = make_plan()
    copy = plan.as_dict()
    copy["commit"] = "f" * 40
    copy["jobs"][0]["pairs"] = 99
    assert plan.as_dict()["commit"] == "a" * 40
    assert plan.as_dict()["jobs"][0]["pairs"] == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body.update(commit="b" * 40),
        lambda body: body.update(run_id="3" * 32),
        lambda body: body["jobs"][0].update(deadline_seconds=1.0),
        lambda body: body["required_nodeids"]["unit"].append("tests/test_a.py::test_three"),
        lambda body: body["capacity"].update(total_pairs=1),
        lambda body: body.update(fail_fast=True),
    ],
)
def test_any_edit_to_the_serialized_plan_breaks_its_digest(mutate):
    body = json.loads(make_plan().text)
    mutate(body)
    with pytest.raises(PlanError):
        load_plan(canonical_json(body))


def test_unknown_keys_and_non_canonical_text_are_rejected():
    body = json.loads(make_plan().text)
    body["note"] = "extra"
    assert any("unknown keys" in item for item in validate_plan_body(body))
    plan = make_plan()
    with pytest.raises(PlanError):
        load_plan(json.dumps(json.loads(plan.text), indent=2))
    with pytest.raises(PlanError):
        load_plan("not json")


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"commit": "abc"}, "commit"),
        ({"commit": "A" * 40}, "commit"),
        ({"run_id": "xyz"}, "run_id"),
        ({"tiers": ("unit", "unit")}, "tiers"),
        ({"tiers": ("unit", "nonexistent")}, "tiers"),
        ({"repetitions": 4}, "repetitions"),
        ({"modes": ("source", "source")}, "modes"),
        ({"modes": ("source", "bogus")}, "runtime mode"),
        ({"input_digests": {}}, "input_digests"),
        ({"input_digests": {"VERSIONS.md": "short"}}, "input_digests"),
        ({"asset_digest": "z" * 64}, "asset_digest"),
        ({"games": ()}, "games"),
        ({"roles": ("listener", "listener")}, "roles"),
        ({"deadline_seconds": 0}, "deadline"),
        ({"deadline_seconds": True}, "deadline"),
        ({"policy_digest": "nope"}, "policy_digest"),
        ({"early_smoke": 1}, "early_smoke"),
    ],
)
def test_invalid_declarations_are_refused_instead_of_normalised(overrides, needle):
    with pytest.raises(PlanError, match=needle):
        make_plan(**overrides)


def test_one_global_pair_budget_must_cover_every_job_share():
    with pytest.raises(PlanError, match="pairs"):
        make_plan(total_pairs=1)
    with pytest.raises(PlanError, match="pairs"):
        make_plan(total_pairs=0)
    with pytest.raises(PlanError, match="global pair budget"):
        make_plan(total_pairs=2, pairs_per_job={"source": 2, "cython": 1})
    shares = make_plan(total_pairs=6, pairs_per_job={"source": 4, "cython": 2}).as_dict()
    assert [job["pairs"] for job in shares["jobs"]] == [4, 2]


def test_the_additional_smoke_keeps_its_role_and_order_before_the_selected_tiers():
    plan = make_plan(tiers=("unit", "timing"), early_smoke=True).as_dict()
    smoke, selected = plan["jobs"][0], plan["jobs"][2]
    assert (smoke["job_id"], smoke["phase"]) == ("source-smoke-job", "smoke")
    assert smoke["steps"] == [{"mode": "source", "tier": "smoke", "role": "additional-probe"}]
    assert (selected["job_id"], selected["phase"]) == ("source-job", "selected")
    assert [step["tier"] for step in selected["steps"]] == ["unit", "timing"]
    assert all(step["role"] == "selected-tier" for step in selected["steps"])
    with pytest.raises(PlanError, match="smoke"):
        make_plan(tiers=("smoke", "unit"), early_smoke=True)


def test_fail_fast_is_recorded_exactly_as_declared_and_defaults_to_false_in_the_model():
    assert make_plan(fail_fast=False).as_dict()["fail_fast"] is False
    assert make_plan(fail_fast=True).as_dict()["fail_fast"] is True


def test_job_lookup_rejects_unknown_ids_and_lists_planned_jobs_in_order():
    plan = make_plan()
    assert plan.job_ids() == ["source-job", "cython-job"]
    assert plan.job("cython-job")["mode"] == "cython"
    with pytest.raises(PlanError):
        plan.job("other-job")
