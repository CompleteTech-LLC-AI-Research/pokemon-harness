"""Strict matrix supervision and dual-runtime gate orchestration (#129).

Split from ``tests/test_production_gate.py`` for #129 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import signal
from pathlib import Path

import pytest

from scripts.tcp_link_matrix import required_matrix_nodeids
from tests._production_gate_support import (
    _FakeMatrixPopen,
    _matrix_report,
    _patch_main_inputs,
    _runtime_result,
    gate,
)


def test_strict_matrix_tier_runs_each_required_node_in_isolated_selector(tmp_path, monkeypatch):
    _FakeMatrixPopen.mode = "pass"
    _FakeMatrixPopen.commands = []
    monkeypatch.setattr(gate.subprocess, "Popen", _FakeMatrixPopen)
    required = (
        "tests/test_matrix.py::test_pair[red-blue]",
        "tests/test_matrix.py::test_pair[blue-red]",
    )
    result = gate.run_tier(
        name="trade",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        repeat=1,
        timeout_override=1,
        report_directory=tmp_path,
        required_nodeids=required,
        matrix_workers=2,
    )

    assert result.status == "PASS"
    assert result.counts == gate.Counts(total=2, passed=2)
    assert result.selected_nodeids == sorted(required)
    assert [case.status for case in result.case_results] == ["PASS", "PASS"]
    assert {command[3] for command, _env in _FakeMatrixPopen.commands} == set(required)
    assert all(
        command[5] == "real_rom and trade_acceptance" for command, _env in _FakeMatrixPopen.commands
    )


def test_strict_matrix_tier_rejects_a_process_observed_after_its_deadline(tmp_path, monkeypatch):
    clock = [0.0]

    class _LateMatrixPopen:
        def __init__(self, command, *, env, **kwargs):
            del kwargs
            self.returncode = None
            self.pid = 999999999
            self.stdout = io.StringIO("")
            nodeid = command[3]
            Path(env["POKERED_GATE_REPORT"]).write_text(
                json.dumps(_matrix_report(nodeid)), encoding="utf-8"
            )

        def poll(self):
            if clock[0] >= 0.012:
                self.returncode = 0
            return self.returncode

        def wait(self, timeout=None):
            del timeout
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(gate.subprocess, "Popen", _LateMatrixPopen)
    monkeypatch.setattr(gate.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        gate.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds + 0.002),
    )

    result = gate.run_tier(
        name="trade",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        repeat=1,
        timeout_override=0.01,
        report_directory=tmp_path,
        required_nodeids=("tests/test_matrix.py::test_pair[red-blue]",),
        matrix_workers=1,
        matrix_timeout_override=1.0,
    )

    assert result.status == "FAIL"
    assert result.case_results[0].status == "TIMEOUT"


def test_strict_matrix_tier_rejects_a_skipped_required_row(tmp_path, monkeypatch):
    _FakeMatrixPopen.mode = "skipped"
    _FakeMatrixPopen.commands = []
    monkeypatch.setattr(gate.subprocess, "Popen", _FakeMatrixPopen)
    result = gate.run_tier(
        name="battle",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        repeat=1,
        timeout_override=1,
        report_directory=tmp_path,
        required_nodeids=("tests/test_matrix.py::test_pair[red-blue]",),
        matrix_workers=1,
    )

    assert result.status == "FAIL"
    assert result.counts.skipped == 1
    assert any(
        "required matrix case produced a test skip" in failure
        for failure in result.iteration_failures
    )


def test_strict_matrix_supervisor_marks_timeout_and_queued_rows(tmp_path, monkeypatch):
    _FakeMatrixPopen.mode = "hang"
    _FakeMatrixPopen.commands = []
    _FakeMatrixPopen.instances = []
    monkeypatch.setattr(gate.subprocess, "Popen", _FakeMatrixPopen)
    monkeypatch.setattr(gate.os, "killpg", lambda _pid, _signal: None)
    monkeypatch.setattr(gate, "MATRIX_CASE_TIMEOUT_SECONDS", {"trade": 0.05, "battle": 0.05})
    monkeypatch.setattr(gate, "MATRIX_AGGREGATE_GRACE_SECONDS", 0.0)

    result = gate.run_tier(
        name="trade",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        repeat=1,
        timeout_override=None,
        report_directory=tmp_path,
        required_nodeids=(
            "tests/test_matrix.py::test_pair[red-blue]",
            "tests/test_matrix.py::test_pair[blue-red]",
            "tests/test_matrix.py::test_pair[yellow-red]",
        ),
        matrix_workers=1,
        matrix_timeout_override=0.06,
    )

    assert result.status == "FAIL"
    # The selector startup and thread scheduling overhead is intentionally
    # host-dependent. Assert the accounting invariant (a timeout plus queued
    # work is recorded) without promising how many rows fit before the hard
    # aggregate deadline.
    assert len(result.case_results) == 3
    assert {case.status for case in result.case_results} <= {
        "TIMEOUT",
        "NOT_STARTED",
    }
    assert any(case.status == "TIMEOUT" for case in result.case_results)
    assert any(case.status == "NOT_STARTED" for case in result.case_results)
    assert any("aggregate deadline expired" in failure for failure in result.iteration_failures)
    assert all(instance.poll() is not None for instance in _FakeMatrixPopen.instances)


@pytest.mark.parametrize("tier", ("trade", "battle"))
def test_strict_matrix_execution_rejects_a_reduced_required_manifest(tier):
    complete_matrix = tuple(sorted(required_matrix_nodeids()[tier]))
    reduced_manifest = complete_matrix[:-1]
    matrix_audit = {
        "audited_nodeids": {
            "trade": tuple(sorted(required_matrix_nodeids()["trade"])),
            "battle": tuple(sorted(required_matrix_nodeids()["battle"])),
        },
        "groups": {
            "strict-trade-entrypoints": {
                "expected": len(required_matrix_nodeids()["trade"]),
            },
            "strict-battle-entrypoints": {
                "expected": len(required_matrix_nodeids()["battle"]),
            },
        },
    }

    # A passing subprocess cannot make an incomplete required manifest safe.
    problems = gate._matrix_execution_problems(
        matrix_audit=matrix_audit,
        required_nodeids_by_tier={tier: frozenset(reduced_manifest)},
        selected=(tier,),
    )

    assert problems, "a reduced required matrix must fail closed"
    assert "does not match the audited strict matrix" in problems[0]


def test_strict_matrix_aggregate_timeout_kills_and_reaps_the_process_group(tmp_path, monkeypatch):
    _FakeMatrixPopen.mode = "hang"
    _FakeMatrixPopen.commands = []
    _FakeMatrixPopen.instances = []
    _FakeMatrixPopen.creation_kwargs = []
    monkeypatch.setattr(gate.subprocess, "Popen", _FakeMatrixPopen)

    class _Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, duration):
            self.now += duration

    clock = _Clock()
    monkeypatch.setattr(gate, "time", clock)
    monkeypatch.setattr(gate, "MATRIX_CASE_TIMEOUT_SECONDS", {"trade": 10.0})

    killed_groups = []

    def fake_killpg(pid, sig):
        killed_groups.append((pid, sig))
        for instance in _FakeMatrixPopen.instances:
            if instance.pid == pid:
                instance.returncode = -int(sig)

    monkeypatch.setattr(gate.os, "killpg", fake_killpg)

    nodeids = (
        "tests/test_matrix.py::test_pair[blue-red]",
        "tests/test_matrix.py::test_pair[red-blue]",
    )
    result = gate.run_tier(
        name="trade",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        repeat=1,
        timeout_override=None,
        report_directory=tmp_path,
        required_nodeids=nodeids,
        matrix_workers=1,
        matrix_timeout_override=0.1,
    )

    statuses = {case.nodeid: case.status for case in result.case_results}
    assert result.status == "FAIL"
    assert statuses == {
        nodeids[0]: "TIMEOUT",
        nodeids[1]: "NOT_STARTED",
    }
    assert killed_groups == [(999999999, signal.SIGKILL)]
    assert all(instance.poll() is not None for instance in _FakeMatrixPopen.instances)
    assert len(_FakeMatrixPopen.creation_kwargs) == 1
    assert _FakeMatrixPopen.creation_kwargs[0]["start_new_session"] is True
    assert any("aggregate deadline exceeded" in failure for failure in result.iteration_failures)


def test_asset_inspection_reports_hash_mismatch_and_missing_inputs(tmp_path):
    rom_root = tmp_path / "rom"
    fixture_root = tmp_path / "fixtures"
    red = rom_root / "red" / "pokemon-red.gb"
    red.parent.mkdir(parents=True)
    red.write_bytes(b"wrong bytes")
    (rom_root / "red" / "pokemon-red.sym").write_text("symbols", encoding="utf-8")
    (fixture_root / "red").mkdir(parents=True)
    (fixture_root / "red" / "cable_club.state").write_bytes(b"state")

    records = gate.inspect_assets(
        rom_root,
        fixture_root,
        {
            Path("red/pokemon-red.gb"): "0" * 40,
            Path("red/pokemon-red.sym"): hashlib.sha1(b"symbols").hexdigest(),
        },
    )
    by_label = {record.label: record for record in records}
    assert by_label["red-stock"].status == "sha1-mismatch"
    assert by_label["red-stock"].actual_sha1 == hashlib.sha1(b"wrong bytes").hexdigest()
    assert by_label["red"].status == "ok"
    assert by_label["red"].actual_sha1 == hashlib.sha1(b"symbols").hexdigest()
    assert by_label["red cable-club"].actual_sha1 == hashlib.sha1(b"state").hexdigest()
    assert by_label["blue"].status == "missing"
    assert any("blue-stock" in problem for problem in gate.required_asset_problems(records))


def test_environment_uses_gate_worktree_and_does_not_override_explicit_rom(tmp_path, monkeypatch):
    rom_root = tmp_path / "rom"
    red = rom_root / "red"
    red.mkdir(parents=True)
    rom_path_value = red / "pokemon-red.gb"
    sym_path_value = red / "pokemon-red.sym"
    rom_path_value.write_bytes(b"rom")
    sym_path_value.write_text("sym", encoding="utf-8")
    explicit = "/caller/selected.gb"
    monkeypatch.setenv("POKERED_ROM_PATH", explicit)
    ambient_sha = "f" * 40
    monkeypatch.setenv("POKERED_ROM_SHA1", ambient_sha)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_gate.py::test_parent")
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "0")
    environment = gate.build_test_environment(
        tmp_path,
        rom_root,
        tmp_path / "fixtures",
        {Path("red/pokemon-red.gb"): hashlib.sha1(b"rom").hexdigest()},
    )
    assert environment["POKERED_ROM_ROOT"] == str(rom_root)
    assert environment["POKERED_ROM_PATH"] == explicit
    # Explicit path/digest pairs are preserved for the later policy check;
    # the gate must not silently replace a caller-selected ROM identity.
    assert environment["POKERED_ROM_SHA1"] == ambient_sha
    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert "PYTEST_CURRENT_TEST" not in environment
    assert environment["PYTHONPATH"].split(os.pathsep)[:3] == [
        str(tmp_path / "vendor" / "pyboy-src"),
        str(tmp_path / "src"),
        str(tmp_path),
    ]


def test_environment_cython_mode_does_not_shadow_installed_pyboy(tmp_path, monkeypatch):
    vendored = tmp_path / "vendor" / "pyboy-src"
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join((str(vendored), str(tmp_path / "ambient"))),
    )
    monkeypatch.setenv("PYBOY_NO_CYTHON", "1")

    environment = gate.build_test_environment(
        tmp_path,
        tmp_path / "rom",
        tmp_path / "fixtures",
        {},
        runtime_mode="cython",
    )

    entries = environment["PYTHONPATH"].split(os.pathsep)
    assert entries[:2] == [str(tmp_path / "src"), str(tmp_path)]
    assert str(vendored) not in entries
    assert "PYBOY_NO_CYTHON" not in environment


def test_runtime_problems_reject_an_unexpected_runtime_mode(tmp_path):
    runtime = {
        "pyboy_mode": "source",
        "pyboy_version": "2.7.0",
        "pyboy_revision": "revision",
        "serial_contract": "bit-accurate-backend",
        "pyboy_module": "pyboy",
        "harness_module": "pokered_harness",
    }

    problems = gate.runtime_problems(tmp_path, runtime, expected_mode="cython")

    assert any("runtime mode mismatch" in problem for problem in problems)


def test_runtime_problems_reject_foreign_project_and_vendor_module_paths(tmp_path):
    project_root = tmp_path / "selected-project"
    vendor_root = project_root / "vendor" / "pyboy-src"
    vendor_root.mkdir(parents=True)
    revision = "a" * 40
    (project_root / "VERSIONS.md").write_text(
        f"| PyBoy | `2.7.0` + fork `{revision}` |\n",
        encoding="utf-8",
    )
    (vendor_root / "POKERED_HARNESS_PYBOY_REVISION").write_text(
        revision + "\n",
        encoding="ascii",
    )

    foreign_vendor_root = tmp_path / "foreign-project" / "vendor" / "pyboy-src"
    foreign_harness_root = tmp_path / "foreign-project" / "src" / "pokered_harness"
    pyboy_modules = {}
    for name in gate.PYBOY_RUNTIME_MODULES:
        relative = Path(*name.split("."))
        module_path = relative / "__init__.py" if name == "pyboy" else relative.with_suffix(".py")
        pyboy_modules[name] = str(foreign_vendor_root / module_path)

    runtime = {
        "pyboy_mode": "source",
        "pyboy_version": "2.7.0",
        "pyboy_revision": revision,
        "serial_contract": "bit-accurate-backend",
        "pyboy_kind": "python-source",
        "pyboy_module": pyboy_modules["pyboy"],
        "serial_module": pyboy_modules["pyboy.core.serial"],
        "pyboy_modules": pyboy_modules,
        "pyboy_module_kinds": {name: "python-source" for name in gate.PYBOY_RUNTIME_MODULES},
        "harness_module": str(foreign_harness_root / "__init__.py"),
    }

    problems = gate.runtime_problems(project_root, runtime, expected_mode="source")

    # Version, revision, mode, and serial contract are valid; only module
    # provenance is foreign to the selected checkout.
    assert problems, "foreign project/vendor modules must fail the runtime gate"


def test_runtime_mode_parser_preserves_explicit_modes_and_accepts_dual_selection():
    parser = gate.build_parser()

    assert parser.parse_args(["--runtime-mode", "source"]).runtime_mode == "source"
    assert parser.parse_args(["--runtime-mode", "cython"]).runtime_mode == "cython"
    assert parser.parse_args(["--runtime-mode", "both"]).runtime_mode == "both"
    assert parser.parse_args(["--runtime-mode", "dual"]).runtime_mode == "both"
    assert gate.runtime_modes_for_gate("source") == ("source",)
    assert gate.runtime_modes_for_gate("cython") == ("cython",)
    assert gate.runtime_modes_for_gate("both") == ("source", "cython")


def test_runtime_gate_orchestrator_runs_identical_selection_under_both_modes(tmp_path, monkeypatch):
    calls = []

    def fake_run_runtime_gate(**kwargs):
        calls.append(kwargs)
        return _runtime_result(kwargs["mode"])

    monkeypatch.setattr(gate, "run_runtime_gate", fake_run_runtime_gate)
    selected = ("unit", "timing")
    required_tests = {"unit": frozenset({("test_gate.py", "test_one")})}
    required_nodeids = {"unit": frozenset({"tests/test_gate.py::test_one"})}

    results = gate.run_runtime_gates(
        runtime_mode="both",
        project_root=tmp_path,
        python_executable=Path("python"),
        rom_root=tmp_path / "rom",
        fixture_root=tmp_path / "fixtures",
        expected_sha1={},
        assets=[],
        selected=selected,
        required_tests_by_tier=required_tests,
        required_nodeids_by_tier=required_nodeids,
        configuration_problems=("configuration problem",),
        repeat=5,
        timeout_override=3.0,
        matrix_workers=2,
        matrix_timeout_override=4.0,
    )

    assert [result.mode for result in results] == ["source", "cython"]
    assert [call["mode"] for call in calls] == ["source", "cython"]
    assert [call["python_executable"] for call in calls] == [Path("python")] * 2
    assert all(call["selected"] == selected for call in calls)
    assert all(call["required_tests_by_tier"] == required_tests for call in calls)
    assert all(call["required_nodeids_by_tier"] == required_nodeids for call in calls)
    assert all(call["configuration_problems"] == ("configuration problem",) for call in calls)


def test_runtime_gate_orchestrator_maps_a_separate_cython_interpreter(tmp_path, monkeypatch):
    calls = []

    def fake_run_runtime_gate(**kwargs):
        calls.append(kwargs)
        return _runtime_result(kwargs["mode"])

    monkeypatch.setattr(gate, "run_runtime_gate", fake_run_runtime_gate)
    source_python = tmp_path / "source" / "bin" / "python"
    cython_python = tmp_path / "cython" / "bin" / "python"

    results = gate.run_runtime_gates(
        runtime_mode="both",
        project_root=tmp_path,
        python_executable=source_python,
        cython_python_executable=cython_python,
        rom_root=tmp_path / "rom",
        fixture_root=tmp_path / "fixtures",
        expected_sha1={},
        assets=[],
        selected=("unit",),
        required_tests_by_tier={},
        required_nodeids_by_tier={},
    )

    assert [result.mode for result in results] == ["source", "cython"]
    assert [call["python_executable"] for call in calls] == [source_python, cython_python]


def test_single_runtime_gate_keeps_environment_and_tiers_explicit(tmp_path, monkeypatch):
    observed = {"modes": [], "tier_modes": []}

    def fake_build_environment(*args, runtime_mode):
        del args
        observed["modes"].append(runtime_mode)
        return {"runtime_mode": runtime_mode}

    def fake_runtime_problems(_project_root, runtime, *, expected_mode):
        assert runtime["pyboy_mode"] == expected_mode
        return []

    def fake_run_tier(**kwargs):
        observed["tier_modes"].append(kwargs["environment"]["runtime_mode"])
        return gate.TierResult(
            name=kwargs["name"],
            description=kwargs["name"],
            expression=kwargs["name"],
            required=True,
            status="PASS",
            counts=gate.Counts(total=1, passed=1),
        )

    monkeypatch.setattr(gate, "build_test_environment", fake_build_environment)
    monkeypatch.setattr(gate, "probe_runtime", lambda *_args: {"pyboy_mode": "source"})
    monkeypatch.setattr(gate, "runtime_problems", fake_runtime_problems)
    monkeypatch.setattr(gate, "environment_policy_problems", lambda **_kwargs: [])
    monkeypatch.setattr(
        gate,
        "run_collection_preflight",
        lambda **_kwargs: [gate.CollectionResult("python-module", [], "PASS", 0)],
    )
    monkeypatch.setattr(
        gate,
        "run_fixture_manifest_validation",
        lambda **_kwargs: {"status": "PASS", "mode": "schema"},
    )
    monkeypatch.setattr(gate, "run_matrix_collection_audit", lambda **_kwargs: {"status": "PASS"})
    monkeypatch.setattr(gate, "run_tier", fake_run_tier)

    result = gate.run_runtime_gate(
        mode="source",
        project_root=tmp_path,
        python_executable=Path("python"),
        rom_root=tmp_path / "rom",
        fixture_root=tmp_path / "fixtures",
        expected_sha1={},
        assets=[],
        selected=("unit", "timing"),
        required_tests_by_tier={},
        required_nodeids_by_tier={},
    )

    assert result.mode == "source"
    assert observed["modes"] == ["source"]
    assert observed["tier_modes"] == ["source", "source"]
    assert gate.runtime_gate_passes(result) is True


def test_runtime_gate_aggregation_is_fail_closed_for_any_runtime_failure():
    source = _runtime_result("source")
    cython_failure = _runtime_result("cython", gate_problems=("probe failed",))

    assert gate.runtime_gates_pass((source, cython_failure)) is False
    assert gate.runtime_gates_pass((source,)) is True
    assert gate.runtime_gates_pass(()) is False
    assert gate.runtime_gates_pass((_runtime_result("cython", collection_status="FAIL"),)) is False
    assert gate.runtime_gates_pass((_runtime_result("cython", tier_status="FAIL"),)) is False
    inconsistent = _runtime_result("cython")
    inconsistent.runtime["pyboy_mode"] = "source"
    assert gate.runtime_gates_pass((inconsistent,)) is False


def test_dual_evidence_retains_both_explicit_runtime_results(tmp_path):
    payload = gate.build_dual_evidence_payload(
        project_root=tmp_path / "checkout",
        rom_root=tmp_path / "rom",
        fixture_root=tmp_path / "fixtures",
        assets=[],
        runtime_results=(_runtime_result("source"), _runtime_result("cython")),
        overall="PASS",
    )

    assert payload["runtime_mode"] == "both"
    assert [item["mode"] for item in payload["runtimes"]] == ["source", "cython"]
    rendered = gate.render_evidence_text(payload)
    assert "runtime-mode=both" in rendered
    assert "source: PASS" in rendered
    assert "cython: PASS" in rendered


def test_dual_evidence_bundle_retains_failure_diagnostics_per_runtime(tmp_path):
    source = _runtime_result("source")
    cython = _runtime_result("cython", gate_problems=("probe failed",))
    payload = gate.build_dual_evidence_payload(
        project_root=tmp_path / "checkout",
        rom_root=tmp_path / "rom",
        fixture_root=tmp_path / "fixtures",
        assets=[],
        runtime_results=(source, cython),
        overall="FAIL",
        generated_at="2026-09-03T00:00:00+00:00",
    )

    assert gate.runtime_gates_pass((source, cython)) is False
    assert [item["overall"] for item in payload["runtimes"]] == ["PASS", "FAIL"]
    assert payload["runtimes"][1]["gate_problems"] == ["probe failed"]

    paths = gate.write_evidence_bundle(tmp_path / "evidence", payload)
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    text = paths["text"].read_text(encoding="utf-8")
    assert report["overall"] == "FAIL"
    assert [item["mode"] for item in report["runtimes"]] == ["source", "cython"]
    assert report["runtimes"][1]["gate_problems"] == ["probe failed"]
    assert "cython: FAIL" in text
    assert "probe failed" in text
    gate.verify_evidence_bundle(tmp_path / "evidence")


def test_main_preserves_single_runtime_json_schema(tmp_path, monkeypatch, capsys):
    result = _runtime_result("source")
    monkeypatch.setattr(gate, "run_runtime_gates", lambda **_kwargs: (result,))

    exit_code = gate.main(_patch_main_inputs(monkeypatch, tmp_path))

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["runtime"] == result.runtime
    assert "runtime_mode" not in payload
    assert "runtimes" not in payload
    assert {
        "runtime",
        "collections",
        "assets",
        "tiers",
        "gate_problems",
        "overall",
        "fixture_manifest",
        "matrix_audit",
    } <= payload.keys()


@pytest.mark.parametrize("runtime_mode", ("source", "cython"))
def test_main_rejects_cython_interpreter_outside_both(runtime_mode, capsys):
    with pytest.raises(SystemExit) as error:
        gate.main(
            [
                "--runtime-mode",
                runtime_mode,
                "--cython-python",
                ".venv-cython/bin/python",
            ]
        )

    assert error.value.code == 2
    assert "--cython-python requires --runtime-mode both" in capsys.readouterr().err


def test_main_dual_alias_reports_each_runtime_and_fails_closed(tmp_path, monkeypatch, capsys):
    source = _runtime_result("source")
    cython = _runtime_result("cython", gate_problems=("cython probe failed",))
    observed = {}

    def fake_run_runtime_gates(**kwargs):
        observed.update(kwargs)
        return source, cython

    monkeypatch.setattr(gate, "run_runtime_gates", fake_run_runtime_gates)
    args = _patch_main_inputs(monkeypatch, tmp_path)
    args[args.index("--format") + 1] = "json"
    cython_python = tmp_path / ".venv-cython" / "bin" / "python"
    args.extend(("--runtime-mode", "dual", "--cython-python", str(cython_python)))

    exit_code = gate.main(args)

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert observed["runtime_mode"] == "both"
    assert observed["cython_python_executable"] == cython_python
    assert payload["runtime_mode"] == "both"
    assert [item["mode"] for item in payload["runtimes"]] == ["source", "cython"]
    assert payload["runtimes"][1]["overall"] == "FAIL"
    assert payload["gate_problems"] == ["cython: cython probe failed"]
    assert payload["overall"] == "FAIL"
