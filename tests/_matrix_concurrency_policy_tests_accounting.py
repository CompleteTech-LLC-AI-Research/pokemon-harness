"""Policy tests grouped by contract."""

from __future__ import annotations

import pytest

from tests._matrix_concurrency_policy_test_support import (
    _arm,
    _full_set,
    _plan,
    bench,
)


class TestThroughputAccounting:
    """Failed and unstarted rows must stay in the denominator."""

    def test_required_rows_per_hour_uses_required_rows_over_wall_time(self):
        arm = _arm(passing=36, wall=72.0)
        assert arm.required_rows_per_hour == pytest.approx(36 / 72.0 * 3600.0)

    def test_more_passing_rows_never_reduce_throughput(self):
        # Holding wall time fixed, doing more passing rows must score higher.
        # The previous fixtures varied wall time as well, which made the
        # smaller job look faster and tested nothing about row count.
        few_rows = _arm(passing=10, wall=100.0)
        many_rows = _arm(passing=18, wall=100.0)
        assert many_rows.required_rows_per_hour > few_rows.required_rows_per_hour

    def test_dropping_failed_rows_would_inflate_speed_and_is_not_done(self):
        # Two arms execute the same 19 declared rows in the same wall time, but
        # one fails a row.  A benchmark that counted only passing rows and
        # divided by rows actually attempted would report the failing arm as
        # faster.  Both must report the same required_total.
        clean = _arm(passing=19, wall=100.0)
        broken = _arm(passing=18, failed=1, wall=100.0)
        assert clean.required_rows == broken.required_rows == 19
        assert broken.clean_pass is False
        assert clean.clean_pass is True

    def test_zero_wall_time_never_reports_infinite_throughput(self):
        arm = _arm(passing=18, wall=0.0)
        assert arm.required_rows_per_hour == 0.0


class TestMatrixRowAccounting:
    """Rows that contribute no aggregate counts must still be counted."""

    def _report(self, statuses):
        return {
            "tiers": [
                {
                    "name": "trade",
                    "status": "INTERRUPTED",
                    "counts": {"passed": 2, "failed": 0, "errors": 0},
                    "case_results": [
                        {
                            "status": status,
                            "duration_seconds": 1.0,
                            "deadline_seconds": 600.0,
                            "counts": {"passed": 1, "failed": 0, "errors": 0},
                        }
                        for status in statuses
                    ],
                }
            ]
        }

    def test_not_started_rows_stay_in_the_denominator(self):
        counts = bench.tier_counts(self._report(["PASS", "NOT_STARTED", "PASS"]), ("trade",))
        assert counts["completed_passing"] == 2
        assert counts["not_started"] == 1

    def test_interrupted_rows_are_counted_as_interrupted(self):
        counts = bench.tier_counts(self._report(["PASS", "INTERRUPTED"]), ("trade",))
        assert counts["completed_passing"] == 1
        assert counts["interrupted"] == 1
        assert counts["not_started"] == 0

    def test_an_absent_declared_tier_is_incomplete(self):
        counts = bench.tier_counts({"tiers": []}, ("trade", "battle"))
        assert counts["incomplete"] == 2
        assert counts["completed_passing"] == 0

    def test_case_durations_come_from_rows_not_tier_totals(self):
        # A tier's duration_seconds is the sum of its rows and is not a
        # per-case latency, so it must not populate the percentile sample.
        report = self._report(["PASS", "PASS"])
        report["tiers"][0]["duration_seconds"] = 999.0
        durations, deadline = bench.case_durations(report, ("trade",))
        assert durations == [1.0, 1.0]
        # Headroom is per row: 600 s deadline minus 1 s duration.
        assert deadline == pytest.approx(599.0)


class TestThroughputCountsAllRequiredWork:
    """Skipped work must never look faster."""

    def test_unstarted_work_is_not_rewarded_as_speed(self):
        # 18 of 19 required rows produced in 10 s looks ~9x faster per hour than
        # 19 of 19 in 100 s.  Counting required rows alone cannot see this,
        # because both arms carry 19 required rows; what protects the result is
        # that the selector refuses any arm that skipped a row.
        complete = _arm(passing=19, failed=0, wall=100.0)
        skipped = _arm(passing=18, not_started=1, wall=10.0)
        assert skipped.required_rows == complete.required_rows == 19
        assert complete.clean_pass is True
        assert skipped.complete is False
        selection = bench.select_policy(
            # The skipped arm is passed in on purpose: otherwise the refusal
            # would come from the absent workers=2/4 arms and would pass even
            # if unstarted rows stopped blocking selection.
            [
                complete,
                skipped,
                _arm(runtime="native", workers=1, wall=100.0),
            ],
            worker_counts=(1, 2, 4),
        )
        assert selection["outcome"] == "unselected"
        assert any(
            "source/workers-1 is incomplete" in reason and "not_started=1" in reason
            for reason in selection["reasons"]
        )

    def test_a_failed_row_is_counted_in_required_work(self):
        complete = _arm(passing=19, failed=0, wall=100.0)
        partial = _arm(passing=18, failed=1, wall=10.0)
        # Both carry 19 required rows; the failed row is attempted work, so
        # throughput counts it.  The arm that genuinely spent less wall time
        # doing the same required work is correctly reported as faster, and the
        # selector separately refuses it for the failure.
        assert partial.required_rows == complete.required_rows == 19
        assert partial.clean_pass is False
        assert partial.required_rows_per_hour > complete.required_rows_per_hour

    def test_the_passing_only_rate_is_still_reported_separately(self):
        partial = _arm(passing=18, not_started=1, wall=10.0)
        # clean_passing_per_hour divides only passing rows by wall time and is
        # reported for context, never used to rank or select.  The two differ,
        # so a reader can see that a row was skipped.
        assert partial.clean_passing_per_hour < partial.required_rows_per_hour
        assert partial.required_rows == 19


class TestBlockedTiersKeepTheirDeclaredRows:
    """A pre-dispatch BLOCKED tier must not erase the work it declared."""

    def test_a_blocked_tier_counts_every_declared_row_as_unstarted(self):
        # production_gate_matrix returns BLOCKED with selected_nodeids and no
        # case_results when required assets are missing.  Reading only the
        # aggregates reports zero, which would drop all 62 rows from the
        # denominator and describe an arm that attempted nothing as an arm that
        # measured nothing.
        report = {
            "tiers": [
                {
                    "name": "trade",
                    "status": "BLOCKED",
                    "reason": "required assets unavailable",
                    "counts": {"passed": 0, "failed": 0, "errors": 0, "skipped": 0},
                    "selected_nodeids": [f"tests/t.py::test[trade-{i}]" for i in range(43)],
                    "case_results": [],
                },
                {
                    "name": "battle",
                    "status": "BLOCKED",
                    "reason": "required assets unavailable",
                    "counts": {"passed": 0, "failed": 0, "errors": 0, "skipped": 0},
                    "selected_nodeids": [f"tests/t.py::test[battle-{i}]" for i in range(19)],
                    "case_results": [],
                },
            ]
        }
        counts = bench.tier_counts(report, ("trade", "battle"))
        assert counts["not_started"] == 62
        assert counts["completed_passing"] == 0
        arm = bench.ArmResult(
            plan=_plan(),
            completed_passing=counts["completed_passing"],
            not_started=counts["not_started"],
            tier_row_totals={"trade": 43, "battle": 19},
        )
        assert arm.required_rows == 62
        assert arm.complete is False

    def test_a_blocked_tier_without_declared_rows_is_incomplete(self):
        report = {
            "tiers": [{"name": "trade", "status": "BLOCKED", "counts": {"passed": 0, "failed": 0}}]
        }
        counts = bench.tier_counts(report, ("trade",))
        assert counts["incomplete"] == 1
        assert counts["not_started"] == 0


class TestArmTotalsMustBeInternallyConsistent:
    """Outcomes and declarations must agree in both directions."""

    def test_more_outcomes_than_declared_rows_is_refused(self):
        # The gate's own PASS path guards this, but a caller can construct the
        # arm directly, and an inflated denominator inflates the throughput
        # used to rank arms.
        arms = [
            _arm(runtime=rt, workers=w, wall=wall, passing=63)
            for rt in ("source", "native")
            for w, wall in ((1, 120.0), (2, 90.0), (4, 80.0))
        ]
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "unselected"
        assert any("outcome(s) exceed the declaration" in r for r in selection["reasons"])


class TestThroughputIsLabelledForWhatItDivides:
    """Required-row rate must not be reported as a passing-row rate."""

    def test_the_report_names_the_rate_it_actually_measures(self):
        arm = _arm(passing=61, failed=1, wall=10.0)
        record = arm.as_dict()
        assert "required_rows_per_hour" in record
        assert "clean_passing_per_hour" in record
        # 62 required rows over 10 s is not a passing rate: only 61 passed.
        assert record["required_rows_per_hour"] == pytest.approx(62 / 10.0 * 3600.0)
        assert record["clean_passing_per_hour"] == pytest.approx(61 / 10.0 * 3600.0)
        assert record["required_rows_per_hour"] != record["clean_passing_per_hour"]

    def test_a_failing_arm_is_never_reported_as_a_passing_rate(self):
        arm = _arm(passing=61, failed=1, wall=10.0)
        assert arm.required_rows_per_hour > arm.clean_passing_per_hour
        assert "passing_per_hour" not in arm.as_dict()

    def test_the_selection_summary_uses_the_required_row_rate(self):
        arms = _full_set(source_walls=(120.0, 90.0, 80.0), native_walls=(120.0, 90.0, 80.0))
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "selected"
        per_runtime = selection["per_runtime"]
        assert "best_required_rows_per_hour" in per_runtime["source"]
        assert "reference_required_rows_per_hour" in per_runtime["source"]
        assert "best_passing_per_hour" not in per_runtime["source"]


class TestGateRowStatusesAreAllAccountedFor:
    """Every status the gate emits must land in a denominator."""

    def test_a_timed_out_row_is_attempted_work_not_a_missing_one(self):
        # production_gate_matrix records a row killed at its per-row deadline as
        # TIMEOUT with zero pytest counts.  Counting it only through those counts
        # dropped it from the denominator entirely, so an arm with one pass and
        # one timeout reported a single required row and could even read as
        # complete and clean while the gate failed.
        report = {
            "tiers": [
                {
                    "name": "trade",
                    "status": "FAIL",
                    "counts": {"passed": 1, "failed": 0},
                    "case_results": [
                        {"nodeid": "a", "status": "PASS", "counts": {"passed": 1}},
                        {
                            "nodeid": "b",
                            "status": "TIMEOUT",
                            "counts": {"passed": 0, "failed": 0, "errors": 0},
                        },
                    ],
                }
            ]
        }
        counts = bench.tier_counts(report, ("trade",))
        assert counts["completed_passing"] == 1
        assert counts["failed"] == 1
        assert counts["incomplete"] == 0
        arm = bench.ArmResult(
            plan=_plan(tiers=("trade",)), **counts, wall_seconds=10.0, gate_passed=False
        )
        assert arm.required_rows == 2
        assert arm.clean_pass is False

    def test_an_unmodelled_status_is_unclassified_not_passing(self):
        report = {
            "tiers": [
                {
                    "name": "trade",
                    "status": "FAIL",
                    "counts": {"passed": 1, "failed": 0},
                    "case_results": [
                        {"nodeid": "a", "status": "PASS", "counts": {"passed": 1}},
                        {
                            "nodeid": "b",
                            "status": "SOMETHING_NEW",
                            "counts": {"passed": 1, "failed": 0},
                        },
                    ],
                }
            ]
        }
        counts = bench.tier_counts(report, ("trade",))
        # The unknown status must not be credited as a pass.
        assert counts["completed_passing"] == 1
        assert counts["incomplete"] == 1


class TestDeclaredTotalsMustMatchRecordedRowIds:
    """Totals and ids must describe the same rows."""

    def test_claiming_rows_without_recording_their_ids_is_refused(self):
        # Every arm claims 43+19 rows and 62 passes while supplying one id per
        # tier.  Cross-arm identity comparison cannot see it because every arm
        # is equally wrong.
        arms = [
            _arm(runtime=rt, workers=w, wall=wall)
            for rt in ("source", "native")
            for w, wall in ((1, 120.0), (2, 90.0), (4, 80.0))
        ]
        for arm in arms:
            arm.tier_rows = {"trade": frozenset({"only-one"}), "battle": frozenset({"only-one"})}
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "unselected"
        assert any("inconsistent tiers" in r for r in selection["reasons"])
