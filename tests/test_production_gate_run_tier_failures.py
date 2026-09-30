"""Repeated-tier failure retention and sanitized evidence (#129).

Split from ``tests/test_production_gate.py`` for #129 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module. The shared ``repeated_tier_report`` fixture stays with the
tests that use it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests._production_gate_support import (
    _matrix_report,
    _runtime_result,
    gate,
)


@pytest.fixture
def repeated_tier_report(tmp_path, monkeypatch):
    """Exercise the real report loader using synthetic pytest child outcomes."""

    def run(
        failures,
        *,
        repeat=5,
        sensitive="",
        output_size=10000,
        empty_failed_output=False,
        reason_middle="",
        failure_kind="failed",
    ):
        calls = []
        nodeids = [f"tests/test_timing.py::test_case_{index}" for index in range(3)]

        def fake_run_pytest_once(**kwargs):
            iteration = len(calls) + 1
            calls.append(iteration)
            failed_indexes = failures.get(iteration, ())
            records = []
            for index, nodeid in enumerate(nodeids):
                failed = index in failed_indexes
                records.append(
                    {
                        "nodeid": nodeid,
                        "outcome": failure_kind if failed else "passed",
                        "when": "call",
                        "reason": (
                            f"failure-{iteration}-{index} {sensitive}{reason_middle}"
                            " assertion-conclusion"
                            if failed
                            else ""
                        ),
                        "was_xfail": False,
                    }
                )
            payload = {
                "collection_only": False,
                "counts": {
                    "total": 3,
                    "passed": 3 - len(failed_indexes),
                    "failed": len(failed_indexes) if failure_kind == "failed" else 0,
                    "skipped": len(failed_indexes) if failure_kind == "skipped" else 0,
                    "xfailed": 0,
                    "xpassed": 0,
                    "errors": 0,
                },
                "tests": records,
                "collection_errors": [],
                "collection_skips": [],
                "collected": 3,
                "nodeids": nodeids,
                "exitstatus": int(bool(failed_indexes) and failure_kind == "failed"),
            }
            report_path = kwargs["report_path"]
            report_path.write_text(json.dumps(payload), encoding="utf-8")
            report = gate._load_gate_report(report_path, expected_returncode=payload["exitstatus"])
            assert report.error == ""
            label = "FAILED" if failed_indexes else "PASSED"
            output = "trace line\n" * (output_size // 11)
            output += f"\n{label}-OUTPUT-{iteration} {sensitive}\n"
            if failed_indexes and empty_failed_output:
                output = ""
            return payload["exitstatus"], report, output, ["python", "-m", "pytest"]

        monkeypatch.setattr(gate, "run_pytest_once", fake_run_pytest_once)
        result = gate.run_tier(
            name="timing",
            project_root=tmp_path,
            python_executable=Path("python"),
            environment={},
            required_problems=[],
            repeat=repeat,
            timeout_override=1,
            report_directory=tmp_path,
        )
        assert calls == list(range(1, repeat + 1))
        return result, nodeids

    return run


@pytest.mark.parametrize("evidence", ("identities", "output"))
def test_run_tier_retains_iteration_two_failure_after_later_passes(repeated_tier_report, evidence):
    result, nodeids = repeated_tier_report({2: (1,)})

    assert result.status == "FAIL"
    assert result.counts == gate.Counts(total=15, passed=14, failed=1)
    assert result.returncodes == [0, 1, 0, 0, 0]
    if evidence == "identities":
        assert any(
            "iteration 2:" in failure and nodeids[1] in failure and "failure-2-1" in failure
            for failure in result.iteration_failures
        )
    else:
        assert "FAILED-OUTPUT-2" in result.output_tail
        assert "PASSED-OUTPUT-5" not in result.output_tail
        assert len(result.output_tail) <= 8000


def test_run_tier_retains_multiple_failure_identities_with_bounded_output(repeated_tier_report):
    failures = {iteration: (0, 1) for iteration in range(2, 10)}
    result, nodeids = repeated_tier_report(failures, repeat=10)

    assert result.status == "FAIL"
    assert result.counts == gate.Counts(total=30, passed=14, failed=16)
    assert result.returncodes == [0, *([1] * 8), 0]
    for iteration, indexes in failures.items():
        for index in indexes:
            assert any(
                f"iteration {iteration}:" in failure
                and nodeids[index] in failure
                and f"failure-{iteration}-{index}" in failure
                for failure in result.iteration_failures
            )
    assert "FAILED-OUTPUT-2" in result.output_tail
    assert "PASSED-OUTPUT-10" not in result.output_tail
    assert len(result.output_tail) <= 8000


def test_run_tier_empty_failure_output_does_not_retain_later_pass_output(repeated_tier_report):
    result, _ = repeated_tier_report({2: (1,)}, empty_failed_output=True)

    assert result.status == "FAIL"
    assert "PASSED-OUTPUT" not in result.output_tail
    assert "iteration 2:" in result.output_tail
    assert "[no subprocess output]" in result.output_tail
    assert len(result.output_tail) <= 8000


def test_run_tier_long_failure_reason_retains_identity_and_conclusion(repeated_tier_report):
    result, nodeids = repeated_tier_report({2: (1,)}, reason_middle="traceback-frame\n" * 1000)

    assert result.status == "FAIL"
    failure = next(
        entry
        for entry in result.iteration_failures
        if "iteration 2:" in entry and nodeids[1] in entry
    )
    assert "assertion-conclusion" in failure
    assert len(failure) <= 2000


def test_run_tier_failure_overflow_is_explicit_and_preserves_first_failure(repeated_tier_report):
    result, nodeids = repeated_tier_report(
        {iteration: (0, 1) for iteration in range(2, 22)}, repeat=22
    )

    assert result.status == "FAIL"
    assert result.counts == gate.Counts(total=66, passed=26, failed=40)
    assert len(result.iteration_failures) <= 33  # 32 entries plus an omission summary.
    assert all(len(entry) <= 2000 for entry in result.iteration_failures)
    assert any(
        "iteration 2:" in entry and nodeids[0] in entry and "failure-2-0" in entry
        for entry in result.iteration_failures
    )
    assert any("omitted" in entry.lower() for entry in result.iteration_failures)
    assert "FAILED-OUTPUT-2" in result.output_tail
    assert "omitted" in result.output_tail.lower()
    assert "PASSED-OUTPUT-22" not in result.output_tail
    assert len(result.output_tail) <= 8000


def test_run_tier_required_skip_retains_case_reason_after_later_passes(repeated_tier_report):
    result, nodeids = repeated_tier_report({2: (1,)}, failure_kind="skipped")

    assert result.status == "FAIL"
    assert result.returncodes == [0] * 5
    assert result.counts == gate.Counts(total=15, passed=14, skipped=1)
    assert any(
        "iteration 2:" in entry
        and nodeids[1] in entry
        and "skipped" in entry
        and "failure-2-1" in entry
        for entry in result.iteration_failures
    )
    assert "FAILED-OUTPUT-2" in result.output_tail
    assert "PASSED-OUTPUT" not in result.output_tail


@pytest.mark.parametrize("kind", ("collection_errors", "collection_skips"))
def test_run_tier_collection_failure_retains_identity_and_reason(tmp_path, monkeypatch, kind):
    nodeid = "tests/test_missing_dependency.py"

    def fake_run_pytest_once(**kwargs):
        payload = _matrix_report("tests/test_timing.py::test_pass")
        payload[kind] = [{"nodeid": nodeid, "reason": "dependency-unavailable"}]
        if kind == "collection_errors":
            payload["counts"]["errors"] = 1
            payload["exitstatus"] = 2
        kwargs["report_path"].write_text(json.dumps(payload), encoding="utf-8")
        report = gate._load_gate_report(
            kwargs["report_path"], expected_returncode=payload["exitstatus"]
        )
        assert report.error == ""
        return payload["exitstatus"], report, "collection-diagnostic", ["python", "-m", "pytest"]

    monkeypatch.setattr(gate, "run_pytest_once", fake_run_pytest_once)
    result = gate.run_tier(
        name="unit",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        repeat=1,
        timeout_override=1,
        report_directory=tmp_path,
    )
    assert result.status == "FAIL"
    assert any(
        "iteration 1:" in entry and nodeid in entry and "dependency-unavailable" in entry
        for entry in result.iteration_failures
    )


def test_run_tier_timeout_preserves_partial_failed_record(tmp_path, monkeypatch):
    nodeid = "tests/test_timing.py::test_timed_failure"

    class TimedOutPytest:
        def __init__(self, command, *, env, **kwargs):
            self.command = command
            payload = _matrix_report(nodeid, outcome="failed")
            payload["tests"][0]["reason"] = "assertion-before-timeout"
            payload["nodeids"].append("tests/test_timing.py::test_unfinished")
            payload["collected"] = 2
            payload["exitstatus"] = -1
            Path(env["POKERED_GATE_PROGRESS_REPORT"]).write_text(
                json.dumps(payload), encoding="utf-8"
            )

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(self.command, timeout)

    monkeypatch.setattr(gate.subprocess, "Popen", TimedOutPytest)
    monkeypatch.setattr(gate, "_terminate_process", lambda process: None)
    monkeypatch.setattr(gate, "_communicate_after_termination", lambda process: "timeout-output")
    result = gate.run_tier(
        name="unit",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        repeat=1,
        timeout_override=1,
        report_directory=tmp_path,
    )

    assert result.status == "FAIL"
    assert result.returncodes == [124]
    assert result.counts == gate.Counts(total=1, failed=1)
    assert any(
        "iteration 1:" in entry and nodeid in entry and "assertion-before-timeout" in entry
        for entry in result.iteration_failures
    )
    assert any("timed out" in entry for entry in result.iteration_failures)
    assert "timeout-output" in result.output_tail


def test_run_pytest_once_sanitizes_credentials_before_output_tail_boundary(tmp_path, monkeypatch):
    class CompletedPytest:
        returncode = 1

        def __init__(self, command, *, env, **kwargs):
            payload = _matrix_report("tests/test_timing.py::test_failure", outcome="failed")
            payload["exitstatus"] = 1
            Path(env["POKERED_GATE_REPORT"]).write_text(json.dumps(payload), encoding="utf-8")

        def communicate(self, timeout=None):
            # Each fragment is short enough to evade the generic long-token
            # filter. Truncating first would discard the credential prefix
            # and expose fragments at the retained tail boundary.
            return "password=" + "private-fragment-" * 700 + "\nassertion-tail", None

    monkeypatch.setattr(gate.subprocess, "Popen", CompletedPytest)
    returncode, report, output, _ = gate.run_pytest_once(
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        expression="unit",
        timeout_seconds=1,
        report_path=tmp_path / "report.json",
    )

    assert returncode == 1
    assert report.error == ""
    assert report.counts == gate.Counts(total=1, failed=1)
    assert "private-fragment" not in output
    assert "assertion-tail" in output
    assert len(output) <= 8000
    assert not list(tmp_path.glob("*.log"))


def test_run_tier_failure_survives_sanitized_report_serialization(tmp_path, repeated_tier_report):
    private_path = str(tmp_path / "private" / "failure.log")
    sensitive = (
        f"token=iterationsecret path={private_path} "
        r"path=C:\private\failure.log b'PRIVATE_BYTES'"
    )
    result, nodeids = repeated_tier_report({2: (1,)}, sensitive=sensitive, output_size=100)
    payload = gate.build_evidence_payload(
        project_root=tmp_path,
        rom_root=tmp_path / "rom",
        fixture_root=tmp_path / "fixtures",
        runtime={},
        assets=[],
        collections=[],
        tiers=[result],
        gate_problems=[],
        overall="FAIL",
    )
    paths = gate.write_evidence_bundle(tmp_path / "evidence", payload)
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    retained = report["tiers"][0]
    assert retained["status"] == "FAIL"
    assert any(
        "iteration 2:" in failure and nodeids[1] in failure and "failure-2-1" in failure
        for failure in retained["iteration_failures"]
    )
    assert "FAILED-OUTPUT-2" in retained["output_tail"]
    assert len(retained["output_tail"]) <= 8000
    for kind in ("report", "text"):
        serialized = paths[kind].read_text(encoding="utf-8")
        assert "failure-2-1" in serialized
        assert "FAILED-OUTPUT-2" in serialized
        for forbidden in (
            "iterationsecret",
            str(tmp_path),
            private_path,
            r"C:\private",
            "PRIVATE_BYTES",
        ):
            assert forbidden not in serialized
    gate.verify_evidence_bundle(tmp_path / "evidence")


@pytest.mark.parametrize("output_format", ("text", "json"))
def test_main_retains_sanitized_failure_evidence(
    tmp_path, monkeypatch, capsys, repeated_tier_report, output_format
):
    private_path = "/private-diagnostics/iteration-failure.log"

    def fake_runtime_gates(**kwargs):
        tier, _ = repeated_tier_report(
            {2: (1,)}, sensitive=f"token=clisecret path={private_path}", output_size=100
        )
        runtime_result = _runtime_result("source")
        runtime_result.tiers = [tier]
        return [runtime_result]

    monkeypatch.setattr(gate, "parse_expected_sha1", lambda path: {})
    monkeypatch.setattr(gate, "inspect_assets", lambda *args: [])
    monkeypatch.setattr(gate, "load_required_test_keys", lambda path: ({}, ""))
    monkeypatch.setattr(gate, "load_required_nodeids", lambda path: ({}, ""))
    monkeypatch.setattr(gate, "run_runtime_gates", fake_runtime_gates)
    evidence_dir = tmp_path / "cli-evidence"
    returncode = gate.main(
        [
            "--repo-root",
            str(tmp_path),
            "--unit-only",
            "--format",
            output_format,
            "--evidence-dir",
            str(evidence_dir),
        ]
    )
    output = capsys.readouterr().out
    assert returncode == 1
    assert "clisecret" not in output
    assert private_path not in output
    assert "tests/test_timing.py::test_case_1" in output
    assert "failure-2-1" in output
    assert "FAILED-OUTPUT-2" in output
    assert "PASSED-OUTPUT-5" not in output
    if output_format == "json":
        document = json.loads(output)
        assert document["overall"] == "FAIL"
        assert document["tiers"][0]["status"] == "FAIL"
        assert document["tiers"][0]["returncodes"] == [0, 1, 0, 0, 0]
    else:
        assert "overall: FAIL" in output
    for filename in ("gate-report.json", "gate-report.txt"):
        saved = (evidence_dir / filename).read_text(encoding="utf-8")
        assert "clisecret" not in saved
        assert private_path not in saved
        assert str(tmp_path) not in saved
        assert "tests/test_timing.py::test_case_1" in saved
        assert "failure-2-1" in saved
        assert "FAILED-OUTPUT-2" in saved
    gate.verify_evidence_bundle(evidence_dir)
