"""Policy tests grouped by contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._matrix_concurrency_policy_test_support import (
    PROJECT_ROOT,
    _plan,
    bench,
)


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
                            "status": "PASS",
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
                            "status": "PASS",
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
                            "status": "PASS",
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
                            "status": "FAIL",
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


class TestArmTimeoutChargesTheWholeMatrix:
    """An arm killed before its report must not shrink its own denominator."""

    def test_a_timed_out_arm_is_charged_every_declared_row(self, tmp_path, monkeypatch):
        class _Timeout:
            def __init__(self, *a, **k):
                raise bench.subprocess.TimeoutExpired(cmd="gate", timeout=5.0)

        monkeypatch.setattr(bench.subprocess, "run", _Timeout)
        result = bench.run_arm(
            _plan(tiers=("trade", "battle")),
            project_root=PROJECT_ROOT,
            evidence_root=tmp_path / "evidence",
            timeout_seconds=5.0,
            capacity_policy=Path("/policy.json"),
        )
        assert result.interrupted == 1
        # The declared matrix is charged, not one synthetic row: a 62-row arm
        # must not report that it required a single row.
        assert result.tier_row_totals == {"trade": 43, "battle": 19}
        assert result.required_rows == 62
        assert result.complete is False
        assert bench.select_policy([result], worker_counts=(1,))["outcome"] == "unselected"
