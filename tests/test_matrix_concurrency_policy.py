"""Policy and accounting tests for the #106 matrix-concurrency benchmark.

The selection logic is the part that can silently do the wrong thing: a
benchmark that drops failed rows from its denominator will recommend a faster
configuration precisely because it did less work.  These tests pin that
behaviour down, along with the comparability rule and the fresh-directory
requirement that keeps two arms from colliding.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_benchmark():
    """Import the benchmark module by path, like other script-level tests."""

    path = PROJECT_ROOT / "scripts" / "benchmark_matrix_concurrency.py"
    spec = importlib.util.spec_from_file_location("benchmark_matrix_concurrency", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bench = _load_benchmark()


def _plan(runtime: str = "source", workers: int = 1, tiers=("trade", "battle")):
    return bench.ExperimentPlan(
        runtime=runtime,
        requested_workers=workers,
        tiers=tuple(tiers),
        python_executable=None,
    )


def _arm(
    *,
    runtime: str = "source",
    workers: int = 1,
    passing: int = 62,
    failed: int = 0,
    incomplete: int = 0,
    not_started: int = 0,
    interrupted: int = 0,
    wall: float = 100.0,
    effective: int | None = None,
    gate_passed: bool = True,
    tiers=("trade", "battle"),
):
    plan = _plan(runtime, workers, tiers)
    # A real arm records what each tier ran, because that is what makes two
    # arms comparable.  Model that here so fixtures are not incomparable by
    # omission; tests that care about the row identity override it.
    tier_rows = {"trade": 43, "battle": 19}
    nodeids = {
        tier: frozenset(f"tests/t.py::test[{tier}-{index}]" for index in range(count))
        for tier, count in tier_rows.items()
    }
    return bench.ArmResult(
        plan=plan,
        completed_passing=passing,
        failed=failed,
        incomplete=incomplete,
        interrupted=interrupted,
        not_started=not_started,
        effective_workers=workers if effective is None else effective,
        tier_row_totals={tier: tier_rows[tier] for tier in plan.tiers if tier in tier_rows},
        tier_rows={tier: nodeids[tier] for tier in plan.tiers if tier in nodeids},
        wall_seconds=wall,
        gate_passed=gate_passed,
    )


def _tier(name: str, *, rows: int, with_cases: bool = True):
    """Build one gate tier payload with a known declared row count."""

    tier = {
        "name": name,
        "status": "PASS",
        "counts": {"passed": rows, "failed": 0},
        "selected_nodeids": [f"tests/t.py::test[{name}-{index}]" for index in range(rows)],
    }
    if with_cases:
        tier["case_results"] = [
            {"nodeid": nodeid, "status": "PASS", "counts": {"passed": 1, "failed": 0}}
            for nodeid in tier["selected_nodeids"]
        ]
    return tier


def _full_set(*, source_walls, native_walls, passing: int = 62):
    """Return complete arms for both runtimes at workers 1/2/4.

    ``passing`` defaults to the full declared matrix (43 trade + 19 battle) so
    a positive selection fixture describes an arm that actually produced an
    outcome for every row it declared.
    """

    arms = []
    for runtime, walls in (("source", source_walls), ("native", native_walls)):
        for workers, wall in zip((1, 2, 4), walls, strict=True):
            arms.append(_arm(runtime=runtime, workers=workers, passing=passing, wall=wall))
    return arms


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


class TestArmCommand:
    """Each arm must be isolated and must use its declared interpreter."""

    def test_arm_command_isolates_directories_and_workers(self, tmp_path):
        plan = bench.ExperimentPlan(
            runtime="source",
            requested_workers=4,
            tiers=("trade", "battle"),
            python_executable=Path("/opt/py/bin/python"),
        )
        command = bench.build_arm_command(
            plan, project_root=PROJECT_ROOT, evidence_dir=tmp_path / "arm"
        )
        assert "--matrix-workers" in command
        assert command[command.index("--matrix-workers") + 1] == "4"
        assert str(tmp_path / "arm") in command
        assert command[command.index("--runtime-mode") + 1] == "source"
        assert command[command.index("--python") + 1] == "/opt/py/bin/python"
        assert command[command.index("--format") + 1] == "json"

    def test_raw_output_directory_is_a_sibling_not_a_child(self, tmp_path):
        # production_gate.py refuses a --raw-output-dir nested inside
        # --evidence-dir, so the benchmark must place it beside the arm
        # directory instead of inside it.
        plan = _plan()
        arm = tmp_path / "source" / "workers-1"
        command = bench.build_arm_command(plan, project_root=PROJECT_ROOT, evidence_dir=arm)
        evidence = Path(command[command.index("--evidence-dir") + 1])
        raw = Path(command[command.index("--raw-output-dir") + 1])
        assert not raw.is_relative_to(evidence)
        assert raw == arm.with_name(f"{arm.name}-raw")

    @pytest.mark.parametrize(
        ("runtime", "expected_mode"),
        [("source", "source"), ("native", "cython")],
    )
    def test_runtime_mode_is_valid_for_the_gate(self, runtime, expected_mode, tmp_path):
        # The gate's vocabulary is source|cython|both.  Passing the report-facing
        # "native" label straight through would abort every compiled arm.
        plan = bench.ExperimentPlan(
            runtime=runtime,
            requested_workers=1,
            tiers=("trade",),
            python_executable=None,
        )
        command = bench.build_arm_command(
            plan, project_root=PROJECT_ROOT, evidence_dir=tmp_path / "arm"
        )
        mode = command[command.index("--runtime-mode") + 1]
        assert mode == expected_mode
        assert mode in ("source", "cython", "both")

    def test_compiled_arm_still_declares_its_interpreter(self, tmp_path):
        # A single cython-mode arm is executed with --python; the gate maps
        # --python onto the one runtime it was asked for.  Dropping it would
        # silently measure the ambient interpreter instead.
        plan = bench.ExperimentPlan(
            runtime="native",
            requested_workers=1,
            tiers=("trade",),
            python_executable=Path("/opt/cython/bin/python"),
        )
        command = bench.build_arm_command(
            plan, project_root=PROJECT_ROOT, evidence_dir=tmp_path / "arm"
        )
        assert command[command.index("--python") + 1] == "/opt/cython/bin/python"

    def test_capacity_policy_is_passed_to_the_gate(self, tmp_path):
        # Setting POKERED_CAPACITY_POLICY is not enough: production_gate.py
        # reads the policy from --capacity-policy and otherwise reports
        # "capacity admission is not enforced".
        plan = _plan()
        command = bench.build_arm_command(
            plan,
            project_root=PROJECT_ROOT,
            evidence_dir=tmp_path / "arm",
            capacity_policy=Path("/etc/policy.json"),
        )
        assert command[command.index("--capacity-policy") + 1] == "/etc/policy.json"

    def test_absent_capacity_policy_is_simply_omitted(self, tmp_path):
        plan = _plan()
        command = bench.build_arm_command(
            plan,
            project_root=PROJECT_ROOT,
            evidence_dir=tmp_path / "arm",
            capacity_policy=None,
        )
        assert "--capacity-policy" not in command

    def test_arm_command_keeps_going_rather_than_stopping_early(self, tmp_path):
        plan = _plan()
        command = bench.build_arm_command(
            plan, project_root=PROJECT_ROOT, evidence_dir=tmp_path / "arm"
        )
        assert "--keep-going" in command
        assert "--fail-fast" not in command

    def test_a_reused_evidence_directory_is_refused(self, tmp_path):
        plan = _plan()
        first = bench.run_arm(
            plan,
            project_root=tmp_path,
            evidence_root=tmp_path / "evidence",
            timeout_seconds=1.0,
        )
        # The gate cannot run from an empty project root, so the first arm
        # reports a terminal error rather than a pass.  What matters is that it
        # is never reported as a clean success.
        assert first.clean_pass is False
        (tmp_path / "evidence" / "source" / "workers-1").mkdir(parents=True, exist_ok=True)
        second = bench.run_arm(
            plan,
            project_root=tmp_path,
            evidence_root=tmp_path / "evidence",
            timeout_seconds=1.0,
        )
        assert "not fresh" in second.error


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
        counts = bench.tier_counts(self._report(["PASSED", "NOT_STARTED", "PASSED"]), ("trade",))
        assert counts["completed_passing"] == 2
        assert counts["not_started"] == 1

    def test_interrupted_rows_are_counted_as_interrupted(self):
        counts = bench.tier_counts(self._report(["PASSED", "INTERRUPTED"]), ("trade",))
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
        report = self._report(["PASSED", "PASSED"])
        report["tiers"][0]["duration_seconds"] = 999.0
        durations, deadline = bench.case_durations(report, ("trade",))
        assert durations == [1.0, 1.0]
        # Headroom is per row: 600 s deadline minus 1 s duration.
        assert deadline == pytest.approx(599.0)


class TestInputRootsAreForwarded:
    """Declared input roots must reach the gate, not just the report."""

    def test_rom_and_fixture_roots_reach_the_gate(self, tmp_path):
        plan = _plan()
        command = bench.build_arm_command(
            plan,
            project_root=PROJECT_ROOT,
            evidence_dir=tmp_path / "arm",
            rom_root=Path("/assets/rom"),
            fixture_root=Path("/assets/fixtures"),
        )
        assert command[command.index("--rom-root") + 1] == "/assets/rom"
        assert command[command.index("--fixture-root") + 1] == "/assets/fixtures"

    def test_absent_roots_are_omitted(self, tmp_path):
        plan = _plan()
        command = bench.build_arm_command(
            plan, project_root=PROJECT_ROOT, evidence_dir=tmp_path / "arm"
        )
        assert "--rom-root" not in command
        assert "--fixture-root" not in command


class TestGateVerdictIsRecordedOnBothPaths:
    """A passing gate must actually be able to select."""

    def _gate_report(self):
        return {
            "tiers": [
                {
                    "name": tier,
                    "status": "PASS",
                    "duration_seconds": 42.0,
                    "counts": {"passed": 19, "failed": 0, "errors": 0},
                    # A real report carries the exact rows it declared, which
                    # is what makes two arms provably comparable.
                    "selected_nodeids": [
                        f"tests/t.py::test[{tier}-{index}]" for index in range(19)
                    ],
                    "case_results": [
                        {
                            "status": "PASSED",
                            "duration_seconds": 2.0,
                            "deadline_seconds": 900.0 if tier == "trade" else 1200.0,
                            "counts": {"passed": 1, "failed": 0, "errors": 0},
                        }
                        for _ in range(19)
                    ],
                }
                for tier in ("trade", "battle")
            ],
            "capacity": {"admission": {"max_concurrent_pairs": 8}},
        }

    def test_a_zero_returncode_records_a_passing_verdict(self, tmp_path, monkeypatch):
        # Regression: run_arm only ever assigned False, so a passing gate could
        # never select a policy and the requirement was unreachable.
        import json as _json

        report = self._gate_report()

        class _Completed:
            returncode = 0
            stdout = _json.dumps(report)
            stderr = ""

        monkeypatch.setattr(bench.subprocess, "run", lambda *a, **k: _Completed())
        plan = _plan(runtime="source", workers=1, tiers=("trade",))
        result = bench.run_arm(
            plan,
            project_root=tmp_path,
            evidence_root=tmp_path / "evidence",
            timeout_seconds=5.0,
            capacity_policy=Path("/policy.json"),
        )
        assert result.error == ""
        assert result.gate_passed is True
        assert result.completed_passing == 19
        assert result.failed == 0
        assert result.effective_workers == 1

    def test_a_nonzero_returncode_records_a_failing_verdict(self, tmp_path, monkeypatch):
        import json as _json

        report = self._gate_report()

        class _Completed:
            returncode = 1
            stdout = _json.dumps(report)
            stderr = ""

        monkeypatch.setattr(bench.subprocess, "run", lambda *a, **k: _Completed())
        plan = _plan(runtime="source", workers=1, tiers=("trade",))
        result = bench.run_arm(
            plan,
            project_root=tmp_path,
            evidence_root=tmp_path / "evidence",
            timeout_seconds=5.0,
            capacity_policy=Path("/policy.json"),
        )
        assert result.gate_passed is False
        # A non-PASS verdict blocks selection through gate_passed itself.  No
        # synthetic row is invented for it, so the arm's required-row total
        # still matches what the gate actually reported rather than exceeding
        # the declaration.
        assert result.required_rows == sum(result.tier_row_totals.values())
        selection = bench.select_policy([result], worker_counts=(1,))
        assert selection["outcome"] == "unselected"
        assert any("gate verdict is not PASS" in reason for reason in selection["reasons"])

    def test_a_full_passing_run_reaches_a_selection(self, tmp_path, monkeypatch):
        import json as _json

        report = self._gate_report()

        class _Completed:
            returncode = 0
            stdout = _json.dumps(report)
            stderr = ""

        monkeypatch.setattr(bench.subprocess, "run", lambda *a, **k: _Completed())
        arms = []
        for workers, wall in ((1, 120.0), (2, 90.0), (4, 80.0)):
            for runtime in ("source", "native"):
                plan = _plan(runtime=runtime, workers=workers, tiers=("trade", "battle"))
                arm = bench.run_arm(
                    plan,
                    project_root=tmp_path,
                    evidence_root=tmp_path / f"evidence-{runtime}-{workers}",
                    timeout_seconds=5.0,
                    capacity_policy=Path("/policy.json"),
                )
                arm.wall_seconds = wall
                arms.append(arm)
        selection = bench.select_policy(arms, worker_counts=(1, 2, 4))
        assert selection["outcome"] == "selected"
        assert selection["policy"]["matrix_workers"] == 4


class TestDeadlineHeadroomIsPerRow:
    """Headroom must not mix one row's duration with another's deadline."""

    def test_rows_under_their_own_deadlines_report_positive_headroom(self):
        # A 1000 s battle row under a 1200 s deadline and a 100 s trade row
        # under a 900 s deadline both pass.  min-deadline minus max-duration
        # would wrongly report -100 s.
        report = {
            "tiers": [
                {
                    "name": "battle",
                    "status": "PASS",
                    "counts": {"passed": 1, "failed": 0, "errors": 0},
                    "case_results": [
                        {
                            "status": "PASSED",
                            "duration_seconds": 1000.0,
                            "deadline_seconds": 1200.0,
                            "counts": {"passed": 1},
                        }
                    ],
                },
                {
                    "name": "trade",
                    "status": "PASS",
                    "counts": {"passed": 1, "failed": 0, "errors": 0},
                    "case_results": [
                        {
                            "status": "PASSED",
                            "duration_seconds": 100.0,
                            "deadline_seconds": 900.0,
                            "counts": {"passed": 1},
                        }
                    ],
                },
            ]
        }
        durations, headroom = bench.case_durations(report, ("trade", "battle"))
        assert sorted(durations) == [100.0, 1000.0]
        assert headroom == pytest.approx(200.0)

    def test_an_overrunning_row_reports_negative_headroom(self):
        report = {
            "tiers": [
                {
                    "name": "trade",
                    "status": "FAIL",
                    "counts": {"passed": 0, "failed": 1},
                    "case_results": [
                        {
                            "status": "FAILED",
                            "duration_seconds": 950.0,
                            "deadline_seconds": 900.0,
                            "counts": {"failed": 1},
                        }
                    ],
                }
            ]
        }
        _, headroom = bench.case_durations(report, ("trade",))
        assert headroom == pytest.approx(-50.0)


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


class TestArmTimeoutBound:
    """The harness ceiling must outlast a legitimate workers=1 baseline."""

    def test_the_bound_covers_a_serial_run_of_the_declared_matrix(self):
        # At one worker the gate may spend every declared row's full per-row
        # budget before it reports any failure.  A bound below that interrupts a
        # valid slow baseline and reports an unsupported "unselected" result.
        worst = bench._declared_matrix_worst_case_seconds(PROJECT_ROOT)
        assert worst > 0
        assert bench.default_arm_timeout_seconds(PROJECT_ROOT) > worst

    def test_the_bound_is_derived_from_the_live_matrix(self):
        # The bound must track the declared rows and the gate's own per-tier
        # timeouts rather than a hardcoded total, so growing the matrix raises
        # it automatically.
        import importlib

        tier_config = importlib.import_module("tests._tier_config")
        gate_model = importlib.import_module("scripts.production_gate_model")
        expected = sum(
            len(tier_config.TIER_REQUIRED_NODEIDS[tier])
            * gate_model.MATRIX_CASE_TIMEOUT_SECONDS[tier]
            for tier in bench.REQUIRED_SELECTION_TIERS
        )
        assert bench._declared_matrix_worst_case_seconds(PROJECT_ROOT) == expected

    def test_an_unreadable_matrix_keeps_the_conservative_floor(self):
        # A manifest that cannot be read is not evidence of a small matrix, so
        # the floor applies instead of a value derived from nothing.
        assert (
            bench.default_arm_timeout_seconds(Path("/nonexistent-matrix-root"))
            == bench.DEFAULT_ARM_TIMEOUT_SECONDS_FLOOR
        )

    def test_the_cli_default_is_derived_and_still_overridable(self):
        parser = bench.build_parser()
        assert parser.parse_args(["--evidence-dir", "e"]).arm_timeout_seconds is None
        assert (
            parser.parse_args(
                ["--evidence-dir", "e", "--arm-timeout-seconds", "5"]
            ).arm_timeout_seconds
            == 5.0
        )


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
