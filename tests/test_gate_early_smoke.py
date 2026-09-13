"""ROM-free observations of gate ordering, cancellation and retained scope."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import select
import signal
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "gate_early_smoke_under_test", ROOT / "scripts" / "production_gate.py"
)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)

QUALIFICATION_TIERS = ("unit", "local", "remote", "trade", "battle", "timing")
TIMED_NODES = frozenset(
    f"tests/test_mcp_timed_rom.py::test_timed_rom_stdio_pair[{listener}-listen-{connector}-connect]"
    for listener in ("red_color", "blue_color", "yellow")
    for connector in ("red_color", "blue_color", "yellow")
)
STDIO_NODES = frozenset(
    f"tests/test_mcp_stdio_integration.py::{name}"
    for name in (
        "test_stdio_list_tools_and_call_step",
        "test_stdio_game_state_resource_is_parseable",
        "test_stdio_save_state_roundtrip_is_deterministic",
        "test_stdio_remote_link_lifecycle_and_explicit_disconnect",
    )
)


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    calls = []
    probes = []
    outcomes = {}
    problems = {}
    required = {
        name: frozenset({f"tests/test_fake.py::test_{name}"}) for name in ("trade", "battle")
    }

    def environment(*_args, runtime_mode):
        return {"TEST_MODE": runtime_mode}

    def probe(_python, _root, env):
        probes.append(env["TEST_MODE"])
        return {"pyboy_mode": env["TEST_MODE"]}

    def tier(**kwargs):
        key = (kwargs["environment"]["TEST_MODE"], kwargs["name"])
        calls.append((key, kwargs))
        status = outcomes.get(key, "PASS")
        return gate.TierResult(
            name=key[1],
            description=key[1],
            expression=key[1],
            required=True,
            status=status,
            counts=gate.Counts(total=1, passed=int(status == "PASS"), failed=int(status != "PASS")),
            returncodes=[0 if status == "PASS" else 1],
            iteration_failures=[]
            if status == "PASS"
            else [f"tests/test_fake.py::test_{key[0]}_{key[1]}: original frame deadline"],
        )

    monkeypatch.setattr(gate, "build_test_environment", environment)
    monkeypatch.setattr(gate, "probe_runtime", probe)
    monkeypatch.setattr(
        gate,
        "runtime_problems",
        lambda _root, runtime, **_kwargs: problems.get(runtime["pyboy_mode"], []),
    )
    monkeypatch.setattr(gate, "environment_policy_problems", lambda **_kwargs: [])
    monkeypatch.setattr(
        gate,
        "run_collection_preflight",
        lambda **_kwargs: [
            gate.CollectionResult(
                "python-module", [], "PASS", 0, nodeids=tuple(sorted(TIMED_NODES | STDIO_NODES))
            ),
            gate.CollectionResult(
                "pytest-console", [], "PASS", 0, nodeids=tuple(sorted(TIMED_NODES | STDIO_NODES))
            ),
        ],
    )
    monkeypatch.setattr(
        gate,
        "run_fixture_manifest_validation",
        lambda **_kwargs: {"status": "PASS", "mode": "byte"},
    )
    monkeypatch.setattr(gate, "fixture_manifest_input_problems", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(gate, "fixture_manifest_provenance_problems", lambda *_args: [])
    monkeypatch.setattr(
        gate,
        "run_matrix_collection_audit",
        lambda **_kwargs: {
            "status": "PASS",
            "structural_pass": True,
            "acceptance_matrix_complete": True,
            "audited_nodeids": {name: tuple(nodes) for name, nodes in required.items()},
            "groups": {
                f"strict-{name}-entrypoints": {"expected": len(nodes)}
                for name, nodes in required.items()
            },
        },
    )
    monkeypatch.setattr(gate, "run_tier", tier)

    def run(*, fail_fast=True, selected=QUALIFICATION_TIERS, early_smoke=True):
        return gate.run_runtime_gates(
            runtime_mode="both",
            project_root=tmp_path,
            python_executable=tmp_path / "source" / "python",
            cython_python_executable=tmp_path / "native" / "python",
            rom_root=tmp_path / "rom",
            fixture_root=tmp_path / "fixtures",
            expected_sha1={},
            assets=[],
            selected=selected,
            required_tests_by_tier={},
            required_nodeids_by_tier=required,
            early_smoke=early_smoke,
            fail_fast=fail_fast,
        )

    return run, calls, probes, outcomes, problems


def test_both_runtime_smokes_precede_all_long_tiers_without_repeating_preflight(scenario):
    run, calls, probes, _outcomes, _problems = scenario
    results = run()

    assert [key for key, _ in calls] == [
        ("source", "smoke"),
        ("cython", "smoke"),
        *[(mode, tier) for mode in ("source", "cython") for tier in QUALIFICATION_TIERS],
    ]
    assert probes == ["source", "cython"]
    for result in results:
        assert [tier.name for tier in result.tiers] == ["smoke", *QUALIFICATION_TIERS]
        assert result.execution_plan["scope"] == "full"
        assert result.execution_plan["smoke_role"] == "additional-probe"
    assert gate.runtime_gates_pass(results)
    assert calls[-1][1]["repeat"] == 5


def test_early_native_failure_retains_both_results_and_marks_long_work_not_started(scenario):
    run, calls, probes, outcomes, _problems = scenario
    outcomes[("cython", "smoke")] = "FAIL"
    results = run()

    assert [key for key, _ in calls] == [("source", "smoke"), ("cython", "smoke")]
    assert probes == ["source", "cython"]
    assert not gate.runtime_gates_pass(results)
    assert "original frame deadline" in results[1].tiers[0].iteration_failures[0]
    for result in results:
        remaining = result.tiers[1:]
        assert [tier.name for tier in remaining] == list(QUALIFICATION_TIERS)
        assert all(tier.status == "NOT_STARTED" and tier.counts.total == 0 for tier in remaining)
        assert all("cython" in tier.reason and "smoke" in tier.reason for tier in remaining)


def test_source_smoke_failure_still_observes_native_before_stopping_long_work(scenario):
    run, calls, _probes, outcomes, _problems = scenario
    outcomes[("source", "smoke")] = "FAIL"
    results = run()

    assert [key for key, _ in calls] == [("source", "smoke"), ("cython", "smoke")]
    assert results[1].runtime["pyboy_mode"] == "cython"
    assert results[1].tiers[0].status == "PASS"
    assert not gate.runtime_gates_pass(results)


def test_keep_going_executes_original_tiers_but_cannot_erase_smoke_failure(scenario):
    run, calls, _probes, outcomes, _problems = scenario
    outcomes[("cython", "smoke")] = "FAIL"
    results = run(fail_fast=False)

    assert len(calls) == 14
    assert all(tier.status == "PASS" for result in results for tier in result.tiers[1:])
    assert not gate.runtime_gates_pass(results)


def test_later_failure_stops_queued_work_without_losing_native_smoke(scenario):
    run, calls, _probes, outcomes, _problems = scenario
    outcomes[("source", "unit")] = "FAIL"
    results = run()

    assert [key for key, _ in calls] == [
        ("source", "smoke"),
        ("cython", "smoke"),
        ("source", "unit"),
    ]
    assert results[1].tiers[0].status == "PASS"
    assert all(tier.status == "NOT_STARTED" for tier in results[1].tiers[1:])
    assert not gate.runtime_gates_pass(results)


def test_runtime_preflight_failure_never_starts_its_smoke(scenario):
    run, calls, _probes, _outcomes, problems = scenario
    problems["cython"] = ["native import resolved to source fallback"]
    results = run()

    assert [key for key, _ in calls] == [("source", "smoke")]
    assert results[1].tiers[0].status == "BLOCKED"
    assert results[1].gate_problems == ["native import resolved to source fallback"]
    assert not gate.runtime_gates_pass(results)


def test_planned_required_results_cannot_be_removed_or_duplicated_to_make_pass(scenario):
    run, _calls, _probes, _outcomes, _problems = scenario
    results = run()
    removed = results[0].tiers.pop()
    assert not gate.runtime_gates_pass(results)
    results[0].tiers.append(removed)
    results[0].tiers.append(removed)
    assert not gate.runtime_gates_pass(results)
    results[0].tiers.pop()
    assert gate.runtime_gates_pass(results)
    assert not gate.runtime_gates_pass(results[:1])


def test_smoke_only_result_is_explicitly_scoped(scenario):
    run, calls, _probes, _outcomes, _problems = scenario
    results = run(selected=("smoke",), early_smoke=False)
    assert [key for key, _ in calls] == [("source", "smoke"), ("cython", "smoke")]
    assert all(result.execution_plan["scope"] == "smoke-only" for result in results)
    assert gate.runtime_gates_pass(results)


def test_smoke_only_observes_both_runtimes_even_with_fail_fast(scenario):
    run, calls, _probes, outcomes, _problems = scenario
    outcomes[("source", "smoke")] = "FAIL"
    results = run(selected=("smoke",), early_smoke=False, fail_fast=True)
    assert [key for key, _ in calls] == [("source", "smoke"), ("cython", "smoke")]
    assert results[1].tiers[0].status == "PASS"
    assert not gate.runtime_gates_pass(results)


def test_evidence_and_text_retain_additional_probe_role_and_unrun_scope(scenario, tmp_path):
    run, _calls, _probes, outcomes, _problems = scenario
    outcomes[("cython", "smoke")] = "FAIL"
    results = run()
    payload = gate.build_dual_evidence_payload(
        project_root=tmp_path,
        rom_root=tmp_path / "rom",
        fixture_root=tmp_path / "fixtures",
        assets=[],
        runtime_results=results,
        overall="FAIL",
    )
    paths = gate.write_evidence_bundle(tmp_path / "evidence", payload)
    gate.verify_evidence_bundle(paths["report"].parent)
    saved = json.loads(paths["report"].read_text())
    assert [item["execution_plan"]["scope"] for item in saved["runtimes"]] == ["full", "full"]
    assert saved["runtimes"][1]["tiers"][0]["iteration_failures"]
    assert all(item["status"] == "NOT_STARTED" for item in saved["runtimes"][0]["tiers"][1:])
    text = paths["text"].read_text()
    assert "execution-scope: full" in text and "smoke-role: additional-probe" in text
    assert "NOT_STARTED" in text and "original frame deadline" in text
    assert str(tmp_path) not in paths["report"].read_text()


def test_untrusted_plan_fields_are_not_copied_into_sanitized_evidence(scenario, tmp_path):
    run, _calls, _probes, _outcomes, _problems = scenario
    results = run()
    results[0].execution_plan = {
        "scope": "password=private-plan-secret",
        "steps": ["/private/asset"],
    }
    assert not gate.runtime_gates_pass(results)
    payload = gate.build_dual_evidence_payload(
        project_root=tmp_path,
        rom_root=tmp_path / "rom",
        fixture_root=tmp_path / "fixtures",
        assets=[],
        runtime_results=results,
        overall="FAIL",
    )
    text = json.dumps(payload)
    assert "private-plan-secret" not in text and "/private/asset" not in text
    assert payload["runtimes"][0]["execution_plan"]["scope"] == "invalid"


@pytest.mark.parametrize("omitted", [None, *sorted(TIMED_NODES | STDIO_NODES)])
def test_smoke_cannot_pass_with_a_missing_public_workflow_or_orientation(
    tmp_path, monkeypatch, omitted
):
    observed = []
    nodes = (TIMED_NODES | STDIO_NODES) - ({omitted} if omitted else set())
    nodes |= frozenset(f"tests/{module}::{name}" for module, name in gate.SMOKE_REQUIRED_TESTS)

    def run_once(**kwargs):
        observed.append(kwargs)
        return (
            0,
            gate.GateReport(
                gate.Counts(total=len(nodes), passed=len(nodes)), nodeids=tuple(sorted(nodes))
            ),
            "passed output",
            ["python", "-m", "pytest"],
        )

    monkeypatch.setattr(gate, "run_pytest_once", run_once)
    result = gate.run_tier(
        name="smoke",
        project_root=tmp_path,
        python_executable=Path(sys.executable),
        environment={},
        required_problems=[],
        repeat=5,
        timeout_override=None,
        report_directory=tmp_path,
    )
    assert result.status == ("FAIL" if omitted else "PASS")
    assert len(observed) == 1
    assert set(observed[0]["selectors"]) == {
        "tests/test_pyboy_link_imports.py",
        "tests/test_mcp_timed_stdio.py",
        "tests/test_mcp_stdio_integration.py",
        "tests/test_mcp_timed_rom.py",
    }
    if omitted:
        assert any(omitted in reason for reason in result.iteration_failures)


def test_cli_smoke_and_stopping_policies_are_explicit():
    parser = gate.build_parser()
    assert parser.parse_args([]).fail_fast is None
    assert parser.parse_args(["--smoke-only"]).smoke_only
    assert parser.parse_args(["--fail-fast"]).fail_fast is True
    assert parser.parse_args(["--keep-going"]).fail_fast is False
    with pytest.raises(SystemExit) as error:
        parser.parse_args(["--fail-fast", "--keep-going"])
    assert error.value.code == 2


@pytest.mark.parametrize(
    "arguments",
    [
        ["--smoke-only", "--unit-only"],
        ["--smoke-only", "--tier", "remote"],
    ],
)
def test_conflicting_scopes_fail_before_any_subprocess(arguments, monkeypatch):
    monkeypatch.setattr(
        gate,
        "inspect_assets",
        lambda *_args: pytest.fail("scope validation must precede asset inspection"),
    )
    with pytest.raises(SystemExit) as error:
        gate.main(arguments)
    assert error.value.code == 2


@pytest.mark.parametrize("collection", [False, True])
def test_interruption_reaps_the_owned_child_and_retains_raw_output(
    tmp_path, monkeypatch, collection
):
    actions = []

    class InterruptedProcess:
        returncode = -15

        def communicate(self, timeout):
            actions.append(("communicate", timeout))
            if len(actions) == 1:
                raise KeyboardInterrupt
            return "original interrupted output", None

    process = InterruptedProcess()
    monkeypatch.setattr(gate.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(gate, "_terminate_process", lambda child: actions.append(("reap", child)))
    raw = tmp_path / "raw"
    arguments = {
        "project_root": tmp_path,
        "environment": {},
        "timeout_seconds": 10,
        "report_path": tmp_path / "report.json",
        "raw_output_directory": raw,
    }
    try:
        if collection:
            result = gate._run_collection_command(
                name="python-module",
                command=["python", "-m", "pytest"],
                **arguments,
            )
            assert result.status == "INTERRUPTED" and result.returncode == 130
            log = raw / "collection-python-module.log"
        else:
            code, report, _output, _command = gate.run_pytest_once(
                python_executable=Path(sys.executable),
                expression="unit",
                **arguments,
            )
            assert code == 130 and "interrupted" in report.error
            log = raw / "report.log"
    except KeyboardInterrupt:
        pytest.fail("interruption escaped before owned-child cleanup and report retention")
    assert ("reap", process) in actions
    assert log.read_text() == "original interrupted output"


def test_cancelled_smoke_stops_other_work_even_with_keep_going(scenario):
    run, calls, probes, outcomes, _problems = scenario
    outcomes[("source", "smoke")] = "INTERRUPTED"
    results = run(fail_fast=False)
    assert [key for key, _ in calls] == [("source", "smoke")]
    assert probes == ["source"]
    assert results[1].runtime["pyboy_mode"] == "not-run"
    assert all(tier.status == "NOT_STARTED" for tier in results[1].tiers)
    assert not gate.runtime_gates_pass(results)


def test_collection_interrupt_stops_fixture_validation_and_native_probe(scenario, monkeypatch):
    run, calls, probes, _outcomes, _problems = scenario
    monkeypatch.setattr(
        gate,
        "run_collection_preflight",
        lambda **_kwargs: [
            gate.CollectionResult(
                "python-module", [], "INTERRUPTED", 130, reason="operator interrupt"
            )
        ],
    )
    monkeypatch.setattr(
        gate,
        "run_fixture_manifest_validation",
        lambda **_kwargs: pytest.fail("fixture validation ran after cancellation"),
    )
    results = run(fail_fast=False)
    assert not calls and probes == ["source"]
    assert results[0].collections[0].status == "INTERRUPTED"
    assert all(tier.status == "NOT_STARTED" for result in results for tier in result.tiers)
    assert not gate.runtime_gates_pass(results)


@pytest.mark.parametrize("at_reader_start", [False, True])
def test_matrix_interrupt_reaps_active_rows_and_marks_queued_rows_unrun(
    tmp_path, monkeypatch, at_reader_start
):
    processes = []
    reaped = []

    class PendingProcess:
        def __init__(self):
            self.stdout = io.StringIO("original matrix output\n")
            self.returncode = None

        def poll(self):
            return self.returncode

        def communicate(self, timeout):
            return self.stdout.read(), None

    def start(*_args, **_kwargs):
        process = PendingProcess()
        processes.append(process)
        return process

    def reap(process, **_kwargs):
        process.returncode = -9
        reaped.append(process)
        return True

    def interrupt(_seconds):
        raise KeyboardInterrupt

    monkeypatch.setattr(gate.subprocess, "Popen", start)
    monkeypatch.setattr(gate, "_kill_matrix_process", reap)
    if at_reader_start:
        monkeypatch.setattr(gate.threading.Thread, "start", interrupt)
    else:
        monkeypatch.setattr(gate.time, "sleep", interrupt)
    nodes = ["tests/test_fake.py::test_one", "tests/test_fake.py::test_two"]
    try:
        result = gate.run_matrix_tier(
            name="trade",
            project_root=tmp_path,
            python_executable=Path(sys.executable),
            environment={},
            required_problems=[],
            timeout_override=30,
            report_directory=tmp_path,
            required_nodeids=nodes,
            matrix_workers=1,
            raw_output_directory=tmp_path / "raw",
        )
    except KeyboardInterrupt:
        pytest.fail(
            "matrix interruption escaped before active-row cleanup and queued-row accounting"
        )
    assert len(processes) == 1 and reaped == processes
    assert result.status == "INTERRUPTED" and result.returncodes == [130]
    assert [case.status for case in result.case_results] == ["INTERRUPTED", "NOT_STARTED"]
    assert "original matrix output" in next((tmp_path / "raw").glob("matrix-*.log")).read_text()


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group cleanup")
def test_termination_reaps_parent_and_closes_pipe_held_by_sigterm_ignoring_grandchild(tmp_path):
    script = tmp_path / "owned_process_group.py"
    script.write_text(
        "import os, signal, time\n"
        "if os.fork() == 0:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    print('owned grandchild ready', flush=True)\n"
        "    time.sleep(60)\n"
        "else:\n"
        "    os.wait()\n"
    )
    process = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        assert select.select([process.stdout], [], [], 10)[0], "child did not become ready"
        assert process.stdout.readline().strip() == "owned grandchild ready"
        gate._terminate_process(process)
        assert process.returncode is not None
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            pytest.fail("owned grandchild survived termination and retained the output pipe")
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate(timeout=5)


@pytest.fixture
def main_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "parse_expected_sha1", lambda _path: {})
    monkeypatch.setattr(gate, "inspect_assets", lambda *_args: [])
    monkeypatch.setattr(gate, "load_required_test_keys", lambda _root: ({}, ""))
    monkeypatch.setattr(gate, "load_required_nodeids", lambda _root: ({}, ""))
    return [
        "--repo-root",
        str(tmp_path),
        "--rom-root",
        str(tmp_path / "rom"),
        "--fixture-root",
        str(tmp_path / "fixtures"),
        "--runtime-mode",
        "both",
        "--format",
        "json",
    ]


@pytest.mark.parametrize(
    ("arguments", "early_smoke", "fail_fast", "selected"),
    [
        ([], True, True, QUALIFICATION_TIERS),
        (["--keep-going"], True, False, QUALIFICATION_TIERS),
        (["--smoke-only"], False, False, ("smoke",)),
        (["--unit-only"], False, False, ("unit", "timing")),
        (["--tier", "remote", "--fail-fast"], False, True, ("remote",)),
    ],
)
def test_main_forwards_documented_scope_and_stopping_policy(
    scenario, main_inputs, monkeypatch, capsys, arguments, early_smoke, fail_fast, selected
):
    run, *_rest = scenario
    results = run()
    observed = {}

    def runner(**kwargs):
        observed.update(kwargs)
        return results

    monkeypatch.setattr(gate, "run_runtime_gates", runner)
    assert gate.main([*main_inputs, *arguments]) == 0
    assert observed["early_smoke"] is early_smoke
    assert observed["fail_fast"] is fail_fast
    assert tuple(observed["selected"]) == selected
    assert observed["repeat"] == 5
    assert json.loads(capsys.readouterr().out)["overall"] == "PASS"


@pytest.mark.parametrize("missing", ["source", "cython", "both"])
def test_main_cannot_pass_or_hide_a_missing_requested_runtime(
    scenario, main_inputs, monkeypatch, capsys, missing, tmp_path
):
    run, *_rest = scenario
    results = tuple(result for result in run() if result.mode != missing and missing != "both")
    monkeypatch.setattr(gate, "run_runtime_gates", lambda **_kwargs: results)
    evidence = tmp_path / "evidence"
    assert gate.main([*main_inputs, "--evidence-dir", str(evidence)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["overall"] == "FAIL" and payload["runtime_mode"] == "both"
    assert [item["mode"] for item in payload["runtimes"]] == ["source", "cython"]
    for item in payload["runtimes"]:
        if missing in (item["mode"], "both"):
            assert item["overall"] == "FAIL"
            assert item["runtime"]["pyboy_mode"] == "not-run"
            assert all(tier["status"] == "NOT_STARTED" for tier in item["tiers"])
    gate.verify_evidence_bundle(evidence)
    assert json.loads((evidence / "gate-report.json").read_text())["overall"] == "FAIL"


def test_main_returns_interrupt_status_and_retains_both_runtime_scopes(
    scenario, main_inputs, monkeypatch, capsys
):
    run, _calls, _probes, outcomes, _problems = scenario
    outcomes[("source", "smoke")] = "INTERRUPTED"
    results = run()
    monkeypatch.setattr(gate, "run_runtime_gates", lambda **_kwargs: results)
    assert gate.main(main_inputs) == 130
    payload = json.loads(capsys.readouterr().out)
    assert payload["overall"] == "FAIL"
    assert payload["runtimes"][1]["runtime"]["pyboy_mode"] == "not-run"
