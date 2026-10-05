"""Policy tests grouped by contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests._matrix_concurrency_policy_test_support import (
    _full_set,
    _plan,
    _tier,
    bench,
)


class TestReportParsing:
    """An unparseable or unexpected report degrades to incomplete, not fast."""

    def test_non_list_tiers_is_incomplete_not_empty(self):
        counts = bench.tier_counts({"tiers": "oops"}, ("trade",))
        assert counts["completed_passing"] == 0
        assert counts["incomplete"] == 1

    def test_unknown_tier_status_is_incomplete(self):
        report = {
            "tiers": [
                {
                    "name": "trade",
                    "status": "WEIRD",
                    "counts": {"passed": 5, "failed": 0},
                }
            ]
        }
        counts = bench.tier_counts(report, ("trade",))
        assert counts["completed_passing"] == 5
        assert counts["incomplete"] == 1

    def test_skipped_and_xfailed_rows_count_as_not_started(self):
        report = {
            "tiers": [
                {
                    "name": "battle",
                    "status": "PASS",
                    "counts": {"passed": 3, "failed": 0, "skipped": 2, "xfailed": 1},
                }
            ]
        }
        counts = bench.tier_counts(report, ("battle",))
        assert counts["not_started"] == 3
        assert (
            bench.ArmResult(
                plan=_plan(),
                completed_passing=counts["completed_passing"],
                not_started=counts["not_started"],
            ).complete
            is False
        )

    def test_errors_count_as_failed_not_passed(self):
        report = {
            "tiers": [
                {
                    "name": "trade",
                    "status": "FAIL",
                    "counts": {"passed": 4, "failed": 1, "errors": 2},
                }
            ]
        }
        counts = bench.tier_counts(report, ("trade",))
        assert counts["failed"] == 3
        assert counts["completed_passing"] == 4

    def test_effective_workers_clamps_to_the_admitted_ceiling(self):
        report = {
            "capacity": {"admission": {"max_concurrent_pairs": 2}},
            "tiers": [_tier("trade", rows=43), _tier("battle", rows=19)],
        }
        assert bench.effective_workers(report, 4, ("trade", "battle")) == 2
        assert bench.effective_workers(report, 1, ("trade", "battle")) == 1

    def test_missing_capacity_leaves_effective_workers_unknown(self):
        assert bench.effective_workers({}, 4, ("trade", "battle")) is None

    def test_effective_workers_never_exceeds_the_smallest_declared_tier(self):
        # The gate caps concurrency at min(matrix_workers, len(nodeids)) per
        # tier, so a 44-worker request runs battle at 19 in practice.  Reading
        # that as an arm at 44 workers would claim a concurrency the matrix
        # cannot reach, and the selection clamp would silently approve it.
        report = {
            "capacity": {"admission": {"max_concurrent_pairs": 64}},
            "tiers": [_tier("trade", rows=43), _tier("battle", rows=19)],
        }
        assert bench.effective_workers(report, 44, ("trade", "battle")) == 19
        assert bench.tier_row_counts(report, ("trade", "battle")) == {
            "trade": 43,
            "battle": 19,
        }

    def test_effective_workers_falls_back_to_declared_selection_when_no_cases_ran(self):
        # A tier that produced no case_results still declares its rows, so the
        # row ceiling must not become "unknown" and hide the clamp.
        report = {
            "capacity": {"admission": {"max_concurrent_pairs": 64}},
            "tiers": [
                _tier("trade", rows=43, with_cases=False),
                _tier("battle", rows=19),
            ],
        }
        assert bench.effective_workers(report, 8, ("trade", "battle")) == 8

    def test_effective_workers_is_unknown_when_a_tier_row_count_is_unreadable(self):
        # An arm whose row count cannot be read has no capacity evidence, so it
        # must not be reported as running at exactly the requested count.
        report = {
            "capacity": {"admission": {"max_concurrent_pairs": 64}},
            "tiers": [_tier("battle", rows=19)],
        }
        assert bench.effective_workers(report, 4, ("trade", "battle")) is None


class TestPercentiles:
    """Reported percentiles must be observed samples, never interpolations."""

    def test_p95_is_an_observed_sample(self):
        values = [float(v) for v in range(1, 21)]
        assert bench.percentile(values, 0.95) in values

    def test_empty_sample_is_none(self):
        assert bench.percentile([], 0.95) is None

    def test_single_sample_returns_that_sample(self):
        assert bench.percentile([4.2], 0.95) == 4.2


class TestReportShape:
    """The report must be self-describing and must not imply release."""

    def test_report_records_identity_and_partial_release(self, tmp_path):
        arms = _full_set(source_walls=(100.0, 90.0, 80.0), native_walls=(100.0, 90.0, 80.0))
        plans = [arm.plan for arm in arms]
        report = bench.build_report(
            project_root=tmp_path,
            plans=plans,
            results=arms,
            selection=bench.select_policy(arms, worker_counts=(1, 2, 4)),
            rom_root="unavailable",
            fixture_root="unavailable",
            timeout_seconds=60.0,
        )
        assert report["report_schema_version"] == bench.REPORT_SCHEMA_VERSION
        assert report["release_status"] == "PARTIAL"
        assert len(report["arms"]) == 6
        assert report["identity"]["sha256"]
        json.dumps(report)  # must be serializable

    def test_rendered_text_includes_every_arm(self, tmp_path):
        arms = _full_set(source_walls=(100.0, 90.0, 80.0), native_walls=(100.0, 90.0, 80.0))
        report = bench.build_report(
            project_root=tmp_path,
            plans=[arm.plan for arm in arms],
            results=arms,
            selection=bench.select_policy(arms, worker_counts=(1, 2, 4)),
            rom_root="unavailable",
            fixture_root="unavailable",
            timeout_seconds=60.0,
        )
        text = bench.render_text(report)
        for arm in report["arms"]:
            assert arm["arm"] in text
        assert "selection: selected" in text


class TestPlanValidation:
    """Bad plans are refused rather than guessed at."""

    def test_unknown_runtime_is_refused(self):
        assert bench.ExperimentPlan(
            runtime="cython", requested_workers=1, tiers=("trade",)
        ).validate()

    def test_non_positive_workers_are_refused(self):
        assert "workers must be positive" in _plan(workers=0).validate()

    def test_empty_tiers_are_refused(self):
        plan = bench.ExperimentPlan(runtime="source", requested_workers=1, tiers=())
        assert "at least one tier" in " ".join(plan.validate())

    def test_worker_count_parsing_rejects_garbage(self):
        with pytest.raises(ValueError):
            bench._parse_counts("1,two,4")
        with pytest.raises(ValueError):
            bench._parse_counts("1,0,4")

    def test_worker_count_parsing_deduplicates_and_preserves_order(self):
        assert bench._parse_counts("4,1,4,2") == [4, 1, 2]


class TestArmBoundIsScopedToTheRequestedRoot:
    """The derived bound must describe the tree it will actually run."""

    def test_an_unreadable_root_does_not_borrow_another_checkout_manifest(self):
        # Loading the manifest by module name would return whichever copy was
        # already imported, so a nonexistent root would silently report this
        # checkout's 61,500 s matrix.  The bound must instead be unknown and
        # fall back to the conservative floor.
        assert bench._declared_matrix_worst_case_seconds(Path("/nonexistent-matrix-root")) == 0.0

    def test_the_bound_reads_the_matrix_from_the_given_root(self, tmp_path):
        # A synthetic root with a larger matrix must raise the derived bound,
        # proving the value tracks the requested tree rather than the process's
        # own checkout.
        tests_dir = tmp_path / "tests"
        scripts_dir = tmp_path / "scripts"
        tests_dir.mkdir()
        scripts_dir.mkdir()
        (tests_dir / "_tier_config.py").write_text(
            "TIER_REQUIRED_NODEIDS = {\n"
            "    'trade': frozenset(f'ter-{i}' for i in range(600)),\n"
            "    'battle': frozenset(f'battle-{i}' for i in range(300)),\n"
            "}\n",
            encoding="utf-8",
        )
        (scripts_dir / "production_gate_model.py").write_text(
            "MATRIX_CASE_TIMEOUT_SECONDS = {'trade': 900.0, 'battle': 1200.0}\n",
            encoding="utf-8",
        )
        worst = bench._declared_matrix_worst_case_seconds(tmp_path)
        assert worst == 600 * 900.0 + 300 * 1200.0
        # The floor is a floor, not a cap: a larger matrix must raise the bound.
        assert bench.default_arm_timeout_seconds(tmp_path) > worst > 108000.0
