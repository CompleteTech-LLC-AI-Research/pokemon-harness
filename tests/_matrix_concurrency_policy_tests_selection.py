"""Policy tests grouped by contract."""

from __future__ import annotations

import pytest

from tests._matrix_concurrency_policy_test_support import (
    _arm,
    _full_set,
    _tier,
    bench,
)


class TestCompleteness:
    """Incomplete, interrupted and unstarted arms are never selectable."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"incomplete": 1},
            {"interrupted": 1},
            {"not_started": 2},
        ],
    )
    def test_any_missing_terminal_row_blocks_selection(self, kwargs):
        arms = _full_set(source_walls=(100.0, 90.0, 80.0), native_walls=(100.0, 90.0, 80.0))
        arms[2] = _arm(runtime="source", workers=4, wall=10.0, **kwargs)
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "unselected"
        assert any("incomplete" in r for r in selection["reasons"])

    def test_a_missing_worker_arm_blocks_selection(self):
        arms = [
            _arm(runtime="source", workers=1),
            _arm(runtime="native", workers=1),
            _arm(runtime="native", workers=4),
        ]
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "unselected"
        assert any("no result for source workers=2" in r for r in selection["reasons"])

    def test_a_failed_required_row_blocks_selection(self):
        arms = _full_set(source_walls=(100.0, 90.0, 80.0), native_walls=(100.0, 90.0, 80.0))
        arms[0] = _arm(runtime="source", workers=1, passing=17, failed=1, wall=100.0)
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "unselected"
        assert any("failed 1 required row" in r for r in selection["reasons"])

    def test_an_arm_with_no_passing_row_is_never_a_reference(self):
        arms = _full_set(source_walls=(100.0, 90.0, 80.0), native_walls=(100.0, 90.0, 80.0))
        arms[0] = _arm(runtime="source", workers=1, passing=0, wall=1.0)
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "unselected"
        assert any("no passing row" in r for r in selection["reasons"])


class TestComparability:
    """A capacity clamp must not be reported as a worker-count effect."""

    def test_a_clamped_arm_blocks_selection_with_its_own_reason(self):
        # An arm whose effective worker count differs from the requested one was
        # clamped by the capacity policy.  It must block selection with a reason
        # naming the clamp, not be read as a worker-count effect.
        arms = _full_set(source_walls=(100.0, 90.0, 80.0), native_walls=(100.0, 90.0, 80.0))
        arms[2] = _arm(runtime="source", workers=4, effective=2, wall=80.0)
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "unselected"
        assert any("clamped" in r for r in selection["reasons"])

    def test_differing_effective_workers_alone_remain_comparable(self):
        # Varying effective concurrency across arms is the experiment itself,
        # so it must not by itself make the arms incomparable.
        arms = _full_set(source_walls=(100.0, 90.0, 80.0), native_walls=(100.0, 90.0, 80.0))
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "selected"
        assert bench.comparable(arms[:3]) is True

    def test_differing_required_row_counts_are_not_comparable(self):
        arms = _full_set(source_walls=(100.0, 90.0, 80.0), native_walls=(100.0, 90.0, 80.0))
        arms[1] = _arm(runtime="source", workers=2, passing=9, wall=90.0)
        assert bench.comparable(arms[:3]) is False


class TestSelectionOutcomes:
    """Selection is a report, and 'no policy' is a valid measured result."""

    def test_faster_concurrency_is_selected_when_measured(self):
        arms = _full_set(source_walls=(120.0, 90.0, 80.0), native_walls=(120.0, 90.0, 80.0))
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "selected"
        assert selection["policy"]["matrix_workers"] == 4

    def test_a_smaller_measured_optimum_is_accepted(self):
        arms = _full_set(source_walls=(120.0, 80.0, 200.0), native_walls=(120.0, 80.0, 200.0))
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "selected"
        assert selection["policy"]["matrix_workers"] == 2

    def test_no_improvement_retains_the_workers_one_default(self):
        arms = _full_set(source_walls=(100.0, 100.0, 100.0), native_walls=(100.0, 100.0, 100.0))
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "unselected"
        assert selection["policy"] is None
        assert any("workers=1 reference" in r for r in selection["reasons"])

    def test_runtimes_disagreeing_is_not_selected(self):
        # Source peaks at 4 workers, native peaks at 2.  One total budget cannot
        # honour both, so nothing may be selected.
        arms = _full_set(source_walls=(120.0, 90.0, 80.0), native_walls=(120.0, 80.0, 200.0))
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "unselected"
        assert any("disagree" in r for r in selection["reasons"])

    def test_a_non_improving_runtime_still_constrains_the_shared_budget(self):
        # Source improves to 4 workers; native never beats its own workers=1
        # reference.  Because one total budget covers both runtimes, native's
        # best count is still 1 and the two disagree, so nothing is selected.
        arms = _full_set(source_walls=(120.0, 90.0, 80.0), native_walls=(120.0, 120.0, 120.0))
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "unselected"
        assert any("disagree" in r for r in selection["reasons"])

    def test_agreeing_runtimes_select_the_shared_count(self):
        arms = _full_set(source_walls=(120.0, 90.0, 80.0), native_walls=(120.0, 90.0, 80.0))
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "selected"
        assert selection["policy"]["matrix_workers"] == 4

    def test_a_union_of_partial_arms_cannot_satisfy_full_matrix_selection(self):
        # Every arm must cover the whole matrix on its own.  A union across arms
        # would let workers=1 measure trade only while workers=2/4 measure
        # battle only, and the union would look like a full-matrix benchmark
        # even though no worker count was ever measured on both tiers.
        arms = [
            _arm(runtime="source", workers=1, tiers=("trade",), wall=120.0),
            _arm(runtime="source", workers=2, tiers=("battle",), wall=90.0),
            _arm(runtime="source", workers=4, tiers=("battle",), wall=80.0),
            _arm(runtime="native", workers=1, tiers=("trade",), wall=120.0),
            _arm(runtime="native", workers=2, tiers=("battle",), wall=90.0),
            _arm(runtime="native", workers=4, tiers=("battle",), wall=80.0),
        ]
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "unselected"
        assert any(
            "did not measure the full" in reason and "not measured: ['battle']" in reason
            for reason in selection["reasons"]
        )

    def test_an_unrequested_tier_does_not_license_partial_arms(self):
        # Passing --tiers trade only cannot select a policy that governs the
        # battle tier too, however many workers the trade arms cover.
        arms = [
            _arm(runtime="source", workers=1, tiers=("trade",), wall=120.0),
            _arm(runtime="source", workers=2, tiers=("trade",), wall=90.0),
            _arm(runtime="source", workers=4, tiers=("trade",), wall=80.0),
            _arm(runtime="native", workers=1, tiers=("trade",), wall=120.0),
            _arm(runtime="native", workers=2, tiers=("trade",), wall=90.0),
            _arm(runtime="native", workers=4, tiers=("trade",), wall=80.0),
        ]
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "unselected"
        assert any("not measured: ['battle']" in reason for reason in selection["reasons"])

    def test_differing_tier_sets_make_arms_incomparable(self):
        arms = [
            _arm(runtime="source", workers=1, tiers=("trade", "battle"), wall=100.0),
            _arm(runtime="source", workers=2, tiers=("trade",), wall=90.0),
        ]
        assert bench.comparable(arms) is False


class TestGateVerdictGatesSelection:
    """A passing row count is not a passing gate verdict."""

    def test_a_non_zero_gate_returncode_blocks_selection(self):
        # production_gate.py exits 0 if and only if its overall verdict is PASS.
        # An arm whose rows happened to pass while the gate rejected its
        # runtime, collection, evidence, or capacity prerequisite must not select.
        arms = _full_set(source_walls=(120.0, 90.0, 80.0), native_walls=(120.0, 90.0, 80.0))
        arms[2] = _arm(runtime="source", workers=4, wall=80.0, gate_passed=False)
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "unselected"
        assert any("gate verdict is not PASS" in r for r in selection["reasons"])

    def test_a_missing_capacity_ceiling_blocks_selection(self):
        # Without --capacity-policy the gate reports admission as unenforced, so
        # no worker count can be justified against a CPU budget.
        arms = _full_set(source_walls=(120.0, 90.0, 80.0), native_walls=(120.0, 90.0, 80.0))
        arms[0] = _arm(runtime="source", workers=1, wall=120.0, effective=0)
        arms[0].effective_workers = None
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "unselected"
        assert any("no admitted capacity ceiling" in r for r in selection["reasons"])


class TestSelectionCannotBypassTheShippedDefault:
    """A policy is only evidence if measured against workers=1."""

    def test_selection_without_a_workers_one_reference_is_refused(self):
        # Comparing 4 against 2 shows an improvement over nothing in
        # particular; the shipped default is 1.
        arms = [
            _arm(runtime=rt, workers=w, wall=wall)
            for rt in ("source", "native")
            for w, wall in ((2, 100.0), (4, 60.0))
        ]
        selection = bench.select_policy(arms, worker_counts=(2, 4))
        assert selection["outcome"] == "unselected"
        assert any("workers=1 reference" in r for r in selection["reasons"])

    def test_a_partial_tier_set_cannot_select_a_matrix_policy(self):
        # Trade-only arms have no evidence about battle performance or
        # correctness, so they must not select a matrix-wide worker policy.
        arms = [
            _arm(runtime=rt, workers=w, wall=wall, tiers=("trade",))
            for rt in ("source", "native")
            for w, wall in ((1, 120.0), (2, 90.0), (4, 80.0))
        ]
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "unselected"
        assert any("not measured" in r for r in selection["reasons"])

    def test_the_full_tier_set_still_selects(self):
        arms = [
            _arm(runtime=rt, workers=w, wall=wall, tiers=("trade", "battle"))
            for rt in ("source", "native")
            for w, wall in ((1, 120.0), (2, 90.0), (4, 80.0))
        ]
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "selected"
        assert selection["policy"]["matrix_workers"] == 4


class TestUndeclaredWorkerCountsCannotBeSelected:
    """Only the declared experiment may be ranked or selected."""

    def test_an_undeclared_worker_arm_cannot_win(self):
        # The selector must not pick a worker count the run never set out to
        # measure, even when that arm looks like the fastest.  An API caller can
        # supply any arm it likes; the selector has to refuse the extras itself.
        arms = [
            _arm(runtime=rt, workers=w, wall=wall)
            for rt in ("source", "native")
            for w, wall in ((1, 120.0), (2, 90.0), (4, 80.0), (8, 10.0))
        ]
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "unselected"
        assert any(
            "workers=8, which is not a declared worker count" in reason
            for reason in selection["reasons"]
        )


class TestPerTierRowIdentityIsComparableWork:
    """A manifest change between sequential arms must be visible."""

    def test_a_per_tier_row_shift_is_not_comparable(self):
        # Arms run sequentially.  If the manifest changes between them, 43 trade
        # + 19 battle rows can become 42 + 20 with the same total of 62, and the
        # timing difference would be attributed to concurrency instead.
        arms = [
            _arm(runtime="source", workers=1, wall=100.0),
            _arm(runtime="source", workers=2, wall=90.0),
            _arm(runtime="source", workers=4, wall=80.0),
        ]
        for arm in arms:
            arm.tier_row_totals = {"trade": 43, "battle": 19}
        arms[2].tier_row_totals = {"trade": 42, "battle": 20}
        assert bench.comparable(arms) is False
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "unselected"
        assert any("not comparable" in reason for reason in selection["reasons"])

    def test_matching_per_tier_rows_are_comparable(self):
        arms = [
            _arm(runtime="source", workers=1, wall=100.0),
            _arm(runtime="source", workers=2, wall=90.0),
            _arm(runtime="source", workers=4, wall=80.0),
        ]
        for arm in arms:
            arm.tier_row_totals = {"trade": 43, "battle": 19}
        assert bench.comparable(arms) is True

    def test_an_arm_without_recorded_rows_is_not_comparable(self):
        # An arm that never recorded its per-tier rows cannot be shown to have
        # run the same work as its peers, so it is not comparable by assumption.
        arms = [
            _arm(runtime="source", workers=1, wall=100.0),
            _arm(runtime="source", workers=2, wall=90.0),
            _arm(runtime="source", workers=4, wall=80.0),
        ]
        for arm in arms:
            arm.tier_row_totals = {"trade": 43, "battle": 19}
        arms[1].tier_row_totals = {}
        assert bench.comparable(arms) is False


class TestPerTierConcurrencyIsReportedHonestly:
    """effective_workers is the governing count, not a uniform description."""

    def test_per_tier_concurrency_reflects_each_tier_s_row_count(self):
        report = {
            "capacity": {"admission": {"max_concurrent_pairs": 64}},
            "tiers": [_tier("trade", rows=43), _tier("battle", rows=19)],
        }
        # Trade ran 30-wide and battle 19-wide; the governing value for the arm
        # is the smallest, 19, but the report records both honestly.
        assert bench.tier_effective_workers(report, 30, ("trade", "battle")) == {
            "trade": 30,
            "battle": 19,
        }
        assert bench.effective_workers(report, 30, ("trade", "battle")) == 19

    def test_per_tier_concurrency_is_unknown_without_a_readable_row_count(self):
        report = {
            "capacity": {"admission": {"max_concurrent_pairs": 64}},
            "tiers": [_tier("battle", rows=19)],
        }
        assert bench.tier_effective_workers(report, 4, ("trade", "battle")) is None


class TestDeclaredRowsMustAllProduceAnOutcome:
    """An arm cannot select a policy having run less than it declared."""

    def test_a_single_passing_row_cannot_satisfy_a_sixty_two_row_declaration(self):
        # Every other check passes: no row failed, none was skipped, and the
        # positive-passing test is satisfied.  But 61 declared rows produced no
        # outcome at all, so the arm ran less than it declared and cannot be
        # compared against an arm that ran all of them.
        arms = [
            _arm(runtime=rt, workers=w, wall=wall, passing=1)
            for rt in ("source", "native")
            for w, wall in ((1, 120.0), (2, 90.0), (4, 80.0))
        ]
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "unselected"
        assert any(
            "recorded 1 outcome(s) for 62 declared row(s)" in reason
            for reason in selection["reasons"]
        )

    def test_a_full_arm_is_unaffected_by_the_new_check(self):
        arms = _full_set(source_walls=(120.0, 90.0, 80.0), native_walls=(120.0, 90.0, 80.0))
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "selected"
        assert selection["policy"]["matrix_workers"] == 4

    def test_a_partial_declaration_is_not_reported_as_complete_work(self):
        # A single-tier arm declares 43 rows; recording all 43 is fine.
        arms = [
            _arm(runtime=rt, workers=w, wall=wall, passing=43, tiers=("trade",))
            for rt in ("source", "native")
            for w, wall in ((1, 120.0), (2, 90.0))
        ]
        for arm in arms:
            arm.tier_row_totals = {"trade": 43}
        reasons = bench.select_policy(arms, worker_counts=(1, 2))["reasons"]
        assert not any("produced no outcome" in reason for reason in reasons)


class TestRuntimesMustMeasureTheSameMatrix:
    """A single worker policy is selected from both runtimes together."""

    def test_runtimes_with_different_per_tier_rows_are_refused(self):
        # Each runtime is internally consistent, so per-runtime comparability
        # cannot see this.  A manifest change between the sequential runtime
        # blocks would otherwise be folded into the concurrency comparison.
        arms = [
            _arm(runtime=rt, workers=w, wall=wall)
            for rt, walls in (("source", (120.0, 90.0, 80.0)), ("native", (120.0, 90.0, 80.0)))
            for w, wall in zip((1, 2, 4), walls, strict=True)
        ]
        for arm in arms:
            counts = (
                {"trade": 43, "battle": 19}
                if arm.plan.runtime == "source"
                else {"trade": 42, "battle": 20}
            )
            arm.tier_row_totals = counts
            arm.tier_rows = {
                tier: frozenset(f"tests/t.py::test[{tier}-{index}]" for index in range(count))
                for tier, count in counts.items()
            }
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "unselected"
        assert any("did not measure the same matrix" in reason for reason in selection["reasons"])

    def test_matching_runtimes_are_still_selectable(self):
        arms = _full_set(source_walls=(120.0, 90.0, 80.0), native_walls=(120.0, 90.0, 80.0))
        assert bench.select_policy(arms, worker_counts=(1, 2, 4))["outcome"] == "selected"


class TestRowIdentityNotJustCounts:
    """Matching counts must not stand in for matching work."""

    def test_a_swapped_row_id_at_identical_counts_is_not_comparable(self):
        # The manifest can swap one 43-row trade node id for another between two
        # sequential arms.  Every count still reads 43/19 while a different set
        # of tests was timed, so counts alone would let selection proceed.
        arms = [
            _arm(runtime="source", workers=w, wall=wall)
            for w, wall in ((1, 100.0), (2, 90.0), (4, 80.0))
        ]
        swapped = frozenset({"tests/t.py::test[trade-swapped]"}) | frozenset(
            f"tests/t.py::test[trade-{index}]" for index in range(1, 43)
        )
        arms[2].tier_rows = {"trade": swapped, "battle": arms[2].tier_rows["battle"]}
        assert bench.comparable(arms) is False
        assert bench.select_policy(arms, worker_counts=(1, 2, 4))["outcome"] == "unselected"

    def test_matching_row_ids_are_comparable(self):
        arms = [
            _arm(runtime="source", workers=w, wall=wall)
            for w, wall in ((1, 100.0), (2, 90.0), (4, 80.0))
        ]
        assert bench.comparable(arms) is True

    def test_an_arm_without_recorded_row_ids_is_not_comparable(self):
        arms = [
            _arm(runtime="source", workers=w, wall=wall)
            for w, wall in ((1, 100.0), (2, 90.0), (4, 80.0))
        ]
        arms[1].tier_rows = {}
        assert bench.comparable(arms) is False

    def test_row_identity_is_read_from_the_gate_report(self):
        report = {
            "tiers": [
                {
                    "name": "trade",
                    "status": "PASS",
                    "counts": {"passed": 2, "failed": 0},
                    "selected_nodeids": ["tests/a.py::test[1]", "tests/a.py::test[2]"],
                },
                {
                    "name": "battle",
                    "status": "PASS",
                    "counts": {"passed": 1, "failed": 0},
                    "selected_nodeids": ["tests/b.py::test[1]"],
                },
            ]
        }
        assert bench.tier_row_identity(report, ("trade", "battle")) == {
            "trade": frozenset({"tests/a.py::test[1]", "tests/a.py::test[2]"}),
            "battle": frozenset({"tests/b.py::test[1]"}),
        }

    def test_an_unreadable_row_identity_is_empty_not_inferred(self):
        assert bench.tier_row_identity({"tiers": "oops"}, ("trade",)) == {"trade": frozenset()}


class TestDuplicateArmsCannotFakeAnImprovement:
    """Two runs at one worker count are variance, not a concurrency effect."""

    def test_two_runs_at_the_same_count_are_not_ranked_against_each_other(self):
        # With only workers=1 declared, ranking a second 60 s run above the
        # 120 s reference would report run-to-run variance as the effect of
        # concurrency.  No worker-count effect was measured at all.
        arms = [
            _arm(runtime=rt, workers=1, wall=wall)
            for rt in ("source", "native")
            for wall in (120.0, 60.0)
        ]
        selection = bench.select_policy(arms, worker_counts=(1,))
        assert selection["outcome"] == "unselected"
        assert any("duplicate arm" in r for r in selection["reasons"])

    def test_one_arm_per_count_is_accepted(self):
        arms = [
            _arm(runtime=rt, workers=w, wall=wall)
            for rt in ("source", "native")
            for w, wall in ((1, 120.0), (2, 90.0))
        ]
        assert bench.select_policy(arms, worker_counts=(1, 2))["outcome"] == "selected"


class TestPerTierWorkerEvidenceMustAgreeWithTheArm:
    """The arm's own per-tier record must not contradict its effective count."""

    def test_per_tier_clamp_is_refused_even_when_the_arm_count_agrees(self):
        # The arm-level effective_workers equals the request while the arm's own
        # per-tier evidence says battle ran at one worker.  Trusting only the
        # arm-level value would select a count the matrix never reached.
        arms = [
            _arm(runtime=rt, workers=w, wall=wall)
            for rt in ("source", "native")
            for w, wall in ((1, 120.0), (2, 90.0))
        ]
        for arm in arms:
            if arm.plan.requested_workers == 2:
                arm.tier_effective_workers = {"trade": 2, "battle": 1}
        selection = bench.select_policy(arms, worker_counts=(1, 2))
        assert selection["outcome"] == "unselected"
        assert any("per-tier evidence recorded" in r for r in selection["reasons"])

    def test_matching_per_tier_worker_evidence_is_accepted(self):
        arms = [
            _arm(runtime=rt, workers=w, wall=wall)
            for rt in ("source", "native")
            for w, wall in ((1, 120.0), (2, 90.0))
        ]
        for arm in arms:
            arm.tier_effective_workers = {
                "trade": arm.plan.requested_workers,
                "battle": arm.plan.requested_workers,
            }
        assert bench.select_policy(arms, worker_counts=(1, 2))["outcome"] == "selected"
