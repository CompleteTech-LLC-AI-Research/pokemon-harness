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
    passing: int = 18,
    failed: int = 0,
    incomplete: int = 0,
    not_started: int = 0,
    interrupted: int = 0,
    wall: float = 100.0,
    effective: int | None = None,
    gate_passed: bool = True,
):
    return bench.ArmResult(
        plan=_plan(runtime, workers),
        completed_passing=passing,
        failed=failed,
        incomplete=incomplete,
        interrupted=interrupted,
        not_started=not_started,
        effective_workers=workers if effective is None else effective,
        wall_seconds=wall,
        gate_passed=gate_passed,
    )


def _full_set(*, source_walls, native_walls, passing: int = 18):
    """Return complete arms for both runtimes at workers 1/2/4."""

    arms = []
    for runtime, walls in (("source", source_walls), ("native", native_walls)):
        for workers, wall in zip((1, 2, 4), walls, strict=True):
            arms.append(_arm(runtime=runtime, workers=workers, passing=passing, wall=wall))
    return arms


class TestThroughputAccounting:
    """Failed and unstarted rows must stay in the denominator."""

    def test_passing_per_hour_uses_passing_rows_over_wall_time(self):
        arm = _arm(passing=36, wall=72.0)
        assert arm.passing_per_hour == pytest.approx(36 / 72.0 * 3600.0)

    def test_more_passing_rows_never_reduce_throughput(self):
        # Holding wall time fixed, doing more passing rows must score higher.
        # The previous fixtures varied wall time as well, which made the
        # smaller job look faster and tested nothing about row count.
        few_rows = _arm(passing=10, wall=100.0)
        many_rows = _arm(passing=18, wall=100.0)
        assert many_rows.passing_per_hour > few_rows.passing_per_hour

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
        assert arm.passing_per_hour == 0.0


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
        report = {"capacity": {"admission": {"max_concurrent_pairs": 2}}}
        assert bench.effective_workers(report, 4) == 2
        assert bench.effective_workers(report, 1) == 1

    def test_missing_capacity_leaves_effective_workers_unknown(self):
        assert bench.effective_workers({}, 4) is None


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
                    "name": "trade",
                    "status": "PASS",
                    "duration_seconds": 42.0,
                    "counts": {"passed": 19, "failed": 0, "errors": 0},
                    "case_results": [
                        {
                            "status": "PASSED",
                            "duration_seconds": 2.0,
                            "deadline_seconds": 900.0,
                            "counts": {"passed": 1, "failed": 0, "errors": 0},
                        }
                        for _ in range(19)
                    ],
                }
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
        assert result.complete is False

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
                plan = _plan(runtime=runtime, workers=workers, tiers=("trade",))
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
