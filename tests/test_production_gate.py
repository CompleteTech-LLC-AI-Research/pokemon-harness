"""Focused, ROM-free tests for the acceptance-gate plumbing."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from typing import ClassVar

import pytest

from scripts.tcp_link_matrix import (
    LOCAL_VARIANT_NODEIDS,
    LOCAL_VERSION_PAIR_NODEIDS,
    REMOTE_REVERSED_ROLE_NODEIDS,
    REMOTE_VERSION_PAIR_NODEIDS,
    SUPPORTED_VERSIONS,
    _collection_environment,
    _collection_report_details,
    acceptance_matrix_gaps,
    audit_collection,
    required_matrix_nodeids,
)
from tests._rom_assets import (
    find_fixture_root,
    find_rom_root,
    fixture_path,
    rom_path,
    sym_path,
)
from tests._tier_config import TIER_REQUIRED_NODEIDS, classify_test

_GATE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "production_gate.py"
_SPEC = importlib.util.spec_from_file_location("pokered_production_gate", _GATE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
gate = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = gate
_SPEC.loader.exec_module(gate)


def _matrix_report(nodeid: str, *, outcome: str = "passed") -> dict:
    counts = {
        "total": 1,
        "passed": int(outcome == "passed"),
        "failed": int(outcome == "failed"),
        "skipped": int(outcome == "skipped"),
        "xfailed": 0,
        "xpassed": 0,
        "errors": 0,
    }
    return {
        "collection_only": False,
        "counts": counts,
        "tests": [
            {
                "nodeid": nodeid,
                "outcome": outcome,
                "when": "call",
                "reason": "fixture missing" if outcome == "skipped" else "",
                "was_xfail": False,
            }
        ],
        "collection_errors": [],
        "collection_skips": [],
        "collected": 1,
        "nodeids": [nodeid],
        "exitstatus": 0,
    }


def _runtime_result(
    mode: str,
    *,
    gate_problems: tuple[str, ...] = (),
    collection_status: str = "PASS",
    tier_status: str = "PASS",
) -> gate.RuntimeGateResult:
    return gate.RuntimeGateResult(
        mode=mode,
        runtime={"pyboy_mode": mode},
        collections=[
            gate.CollectionResult(
                name="python-module",
                command=["python", "-m", "pytest"],
                status=collection_status,
                returncode=0 if collection_status == "PASS" else 1,
            )
        ],
        fixture_manifest={"status": "PASS", "mode": "schema"},
        matrix_audit={"status": "PASS"},
        tiers=[
            gate.TierResult(
                name="unit",
                description="unit",
                expression="unit",
                required=True,
                status=tier_status,
                counts=gate.Counts(total=1, passed=1 if tier_status == "PASS" else 0),
            )
        ],
        gate_problems=list(gate_problems),
    )


class _FakeMatrixPopen:
    mode = "pass"
    commands: ClassVar[list] = []
    instances: ClassVar[list] = []

    def __init__(self, command, *, env, **kwargs):
        del kwargs
        self.command = command
        self.returncode = None if self.mode == "hang" else 0
        self.pid = 999999999
        self.stdout = io.StringIO("")
        self.commands.append((command, env))
        self.instances.append(self)
        nodeid = command[3]
        if self.mode != "hang":
            outcome = "passed" if self.mode == "pass" else self.mode
            payload = _matrix_report(nodeid, outcome=outcome)
            Path(env["POKERED_GATE_REPORT"]).write_text(json.dumps(payload), encoding="utf-8")

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        del timeout
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_tier_classifier_rejects_unknown_test_modules():
    with pytest.raises(ValueError, match="not classified"):
        classify_test("tests/test_new_unit.py", "test_parser")


def test_tier_classifier_separates_required_remote_and_diagnostic_trade():
    remote = classify_test(
        "tests/test_link_integration_remote.py",
        "test_remote_handshake_writes_status_on_both_sides",
    )
    trade = classify_test(
        "tests/test_link_integration_remote.py",
        "test_remote_rpc_flow_past_link_menu_over_tcp",
    )
    assert {"real_rom", "remote_link"} <= remote
    assert "acceptance" not in remote
    assert {"real_rom", "remote_link", "acceptance", "trade"} <= trade


def test_tier_classifier_marks_late_rearm_as_timing_sensitive():
    marks = classify_test(
        "tests/test_network_backend.py",
        "test_on_edge_waits_for_late_rearm",
    )
    assert "unit" in marks
    assert "timing_sensitive" in marks


def test_relative_python_path_does_not_dereference_virtualenv_symlink(tmp_path):
    target = tmp_path / "system-python"
    target.write_text("placeholder", encoding="utf-8")
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    try:
        venv_python.symlink_to(target)
    except (NotImplementedError, OSError):
        pytest.skip("symlinks are unavailable on this platform")

    selected = gate._python_path_from_argument(Path(".venv/bin/python"), tmp_path)

    assert selected == venv_python
    assert selected.is_symlink()


def test_tier_classifier_reserves_strict_acceptance_markers_for_gate():
    trade = classify_test(
        "tests/test_pyboy_link_session_roms.py",
        "test_red_yellow_trade_swaps_real_party_records",
    )
    battle = classify_test(
        "tests/test_pyboy_link_session_roms.py",
        "test_red_yellow_battle_turn_is_resolved",
    )
    assert {"real_rom", "acceptance", "trade", "trade_acceptance"} <= trade
    assert {"real_rom", "acceptance", "battle", "battle_acceptance"} <= battle


def test_versions_sha_parser_pairs_each_rom_path(tmp_path):
    versions = tmp_path / "VERSIONS.md"
    versions.write_text(
        """
| SHA-1 | `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa` |
| Path | `rom/red/pokemon-red.gb` |

| SHA-1 | `BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB` |
| Path | `rom/yellow/pokemon-yellow.gbc` |
""",
        encoding="utf-8",
    )
    assert gate.parse_expected_sha1(versions) == {
        Path("red/pokemon-red.gb"): "a" * 40,
        Path("yellow/pokemon-yellow.gbc"): "b" * 40,
    }


def test_pyboy_version_parser_accepts_revision_annotation(tmp_path):
    versions = tmp_path / "VERSIONS.md"
    versions.write_text(
        "| PyBoy | `2.7.0` + fork `c565df66c3731fad2856169a90f6bbec99925915` |\n",
        encoding="utf-8",
    )
    assert gate.parse_expected_pyboy_version(versions) == "2.7.0"
    assert gate.parse_expected_pyboy_revision(versions) == (
        "c565df66c3731fad2856169a90f6bbec99925915"
    )


def test_fixture_manifest_schema_validation_uses_selected_interpreter(tmp_path):
    result = gate.run_fixture_manifest_validation(
        project_root=Path(__file__).resolve().parents[1],
        python_executable=Path(sys.executable),
        environment={},
        fixture_root=tmp_path / "fixtures",
        validate_bytes=False,
    )

    assert result["status"] == "PASS"
    assert result["mode"] == "schema"
    assert result["entries"] == 10


def test_fixture_manifest_provenance_requires_certified_entries(tmp_path):
    manifest = tmp_path / "fixture-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "fixtures": [
                    {
                        "id": "red-color-ordinary",
                        "provenance": {"status": "partial"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    problems = gate.fixture_manifest_provenance_problems(
        manifest,
        required_ids=("red-color-ordinary", "blue-color-ordinary"),
    )

    assert problems == [
        "certified fixture is absent from manifest: blue-color-ordinary",
        "certified fixture provenance is not verified: red-color-ordinary ('partial')",
    ]


def test_fixture_manifest_input_pins_match_versions_and_inspected_bytes(tmp_path):
    rom_root = tmp_path / "rom"
    fixture_root = tmp_path / "fixtures"
    red_root = rom_root / "red"
    red_root.mkdir(parents=True)
    rom = red_root / "pokemon-red.gb"
    symbols = red_root / "pokemon-red.sym"
    rom.write_bytes(b"rom")
    symbols.write_bytes(b"symbols")
    rom_sha1 = hashlib.sha1(b"rom").hexdigest()
    symbol_sha1 = hashlib.sha1(b"symbols").hexdigest()
    manifest = tmp_path / "fixture-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "fixtures": [
                    {
                        "id": "red-color-ordinary",
                        "expected_rom": {
                            "path": "rom/red/pokemon-red.gb",
                            "sha1": rom_sha1,
                        },
                        "expected_symbols": {
                            "path": "rom/red/pokemon-red.sym",
                            "sha1": symbol_sha1,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    assets = gate.inspect_assets(
        rom_root,
        fixture_root,
        {
            Path("red/pokemon-red.gb"): rom_sha1,
            Path("red/pokemon-red.sym"): symbol_sha1,
        },
    )

    assert (
        gate.fixture_manifest_input_problems(
            manifest,
            rom_root=rom_root,
            assets=assets,
            expected_sha1={
                Path("red/pokemon-red.gb"): rom_sha1,
                Path("red/pokemon-red.sym"): symbol_sha1,
            },
        )
        == []
    )

    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["fixtures"][0]["expected_rom"]["sha1"] = "0" * 40
    manifest.write_text(json.dumps(document), encoding="utf-8")
    problems = gate.fixture_manifest_input_problems(
        manifest,
        rom_root=rom_root,
        assets=assets,
        expected_sha1={
            Path("red/pokemon-red.gb"): rom_sha1,
            Path("red/pokemon-red.sym"): symbol_sha1,
        },
    )
    assert any("disagrees with VERSIONS.md" in problem for problem in problems)
    assert any("does not match inspected bytes" in problem for problem in problems)


def test_required_matrix_manifest_covers_ordered_versions_and_variants():
    assert SUPPORTED_VERSIONS == ("red", "blue", "yellow")
    assert len(TIER_REQUIRED_NODEIDS["remote"]) == 11
    assert len(TIER_REQUIRED_NODEIDS["local"]) == 18
    assert TIER_REQUIRED_NODEIDS == required_matrix_nodeids()
    assert len(LOCAL_VERSION_PAIR_NODEIDS) == 9
    assert len(REMOTE_VERSION_PAIR_NODEIDS) == 9
    assert len(REMOTE_REVERSED_ROLE_NODEIDS) == 6
    assert len(LOCAL_VARIANT_NODEIDS) == 9
    assert len(TIER_REQUIRED_NODEIDS["trade"]) == 19
    assert len(TIER_REQUIRED_NODEIDS["battle"]) == 19
    assert any("[red-blue]" in nodeid for nodeid in TIER_REQUIRED_NODEIDS["remote"])
    assert any("[blue-red]" in nodeid for nodeid in TIER_REQUIRED_NODEIDS["remote"])
    assert any("[red-vanilla-x-color]" in nodeid for nodeid in TIER_REQUIRED_NODEIDS["local"])
    assert any("[blue-color-x-vanilla]" in nodeid for nodeid in TIER_REQUIRED_NODEIDS["local"])

    expected_pairs = {
        f"[{left}-{right}]" for left in SUPPORTED_VERSIONS for right in SUPPORTED_VERSIONS
    }
    assert {nodeid[nodeid.index("[") :] for nodeid in REMOTE_VERSION_PAIR_NODEIDS} == expected_pairs
    assert {nodeid[nodeid.index("[") :] for nodeid in LOCAL_VERSION_PAIR_NODEIDS} == expected_pairs
    for operation, test_name in (
        ("trade", "test_subprocess_pair_completes_trade_over_tcp"),
        ("battle", "test_subprocess_pair_resolves_battle_turn_over_tcp"),
    ):
        strict_remote = {
            nodeid[nodeid.index("[") :]
            for nodeid in TIER_REQUIRED_NODEIDS[operation]
            if test_name in nodeid
        }
        assert strict_remote == {
            f"[{listener}-listen-{connector}-connect]"
            for listener in ("red_color", "blue_color", "yellow")
            for connector in ("red_color", "blue_color", "yellow")
        }


def test_matrix_audit_fails_closed_on_missing_cases_and_reports_unrun_runtime():
    expected = set(required_matrix_nodeids()["remote"])
    missing = next(iter(REMOTE_VERSION_PAIR_NODEIDS))

    audit = audit_collection(expected - {missing})

    assert audit["structural_pass"] is False
    assert missing in audit["groups"]["remote-role-pairs"]["missing"]
    assert audit["acceptance_matrix_complete"] is True
    assert audit["runtime"] == "not-run"


def test_matrix_audit_surfaces_collection_skips_even_when_they_are_described():
    audit = audit_collection(
        required_matrix_nodeids()["local"],
        collection_skips=("optional dependency unavailable",),
    )

    assert audit["structural_pass"] is False
    assert audit["collection_skips"] == ("optional dependency unavailable",)


def test_standalone_matrix_collection_report_rejects_inconsistent_accounting():
    nodeids, errors, skips, problems = _collection_report_details(
        {
            "collection_only": True,
            "counts": {
                "total": 1,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "xfailed": 0,
                "xpassed": 0,
                "errors": 0,
            },
            "tests": [],
            "collection_errors": [],
            "collection_skips": [],
            "collected": 1,
            "nodeids": ["tests/test_one.py::test_one"],
            "exitstatus": 0,
        },
        returncode=0,
    )

    assert nodeids == ["tests/test_one.py::test_one"]
    assert errors == []
    assert skips == []
    assert "collection-only report contains test outcomes" in problems


def test_standalone_matrix_collection_environment_is_controlled(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k hidden")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "leaked")
    monkeypatch.setenv("POKERED_SKIP_SHA1", "1")
    monkeypatch.setenv("PYTHONPATH", "/ambient")

    environment = _collection_environment(tmp_path)

    assert environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert environment["PYBOY_NO_CYTHON"] == "1"
    assert "PYTEST_ADDOPTS" not in environment
    assert "PYTEST_CURRENT_TEST" not in environment
    assert "POKERED_SKIP_SHA1" not in environment
    assert environment["PYTHONPATH"].split(os.pathsep)[:3] == [
        str(tmp_path / "vendor" / "pyboy-src"),
        str(tmp_path / "src"),
        str(tmp_path),
    ]


def test_strict_acceptance_gap_report_has_an_entrypoint_for_each_ordered_case():
    gaps = acceptance_matrix_gaps()

    assert gaps["trade"] == ()
    assert gaps["battle"] == ()


def test_required_nodeid_checker_preserves_parameterized_case_identity():
    required = ("tests/test_matrix.py::test_pair[red-blue]",)
    assert gate._required_nodeid_problems(required, required) == []
    problems = gate._required_nodeid_problems(
        ("tests/test_matrix.py::test_pair[red-red]",), required
    )
    assert problems == [
        (
            "required matrix case is absent from selected items: "
            "tests/test_matrix.py::test_pair[red-blue]"
        )
    ]


def test_run_tier_fails_when_a_required_matrix_case_is_missing(tmp_path, monkeypatch):
    def fake_run_pytest_once(**kwargs):
        return (
            0,
            gate.GateReport(
                counts=gate.Counts(total=1, passed=1),
                nodeids=("tests/test_matrix.py::test_pair[red-red]",),
            ),
            "",
            ["python", "-m", "pytest"],
        )

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
        required_nodeids=("tests/test_matrix.py::test_pair[red-blue]",),
    )

    assert result.status == "FAIL"
    assert any("red-blue" in failure for failure in result.iteration_failures)


def test_run_tier_repeats_timing_cases_at_least_five_times(tmp_path, monkeypatch):
    calls = []

    def fake_run_pytest_once(**kwargs):
        calls.append(kwargs)
        return (
            0,
            gate.GateReport(
                counts=gate.Counts(total=1, passed=1),
                nodeids=("tests/test_timing.py::test_rearm",),
            ),
            "",
            ["python", "-m", "pytest"],
        )

    monkeypatch.setattr(gate, "run_pytest_once", fake_run_pytest_once)
    result = gate.run_tier(
        name="timing",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        repeat=1,
        timeout_override=1,
        report_directory=tmp_path,
    )

    assert result.status == "PASS"
    assert len(calls) == 5


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


def _patch_main_inputs(monkeypatch, tmp_path):
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
        "--python",
        str(tmp_path / "bin" / "python"),
        "--unit-only",
        "--format",
        "json",
    ]


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


def test_environment_pins_selected_symbol_file_when_available(tmp_path, monkeypatch):
    rom_root = tmp_path / "rom"
    red = rom_root / "red"
    red.mkdir(parents=True)
    rom_path = red / "pokemon-red.gb"
    sym_path = red / "pokemon-red.sym"
    rom_path.write_bytes(b"rom")
    sym_path.write_text("sym", encoding="utf-8")
    monkeypatch.setenv("POKERED_SYM_PATH", str(sym_path))
    # The production gate intentionally forwards an explicitly configured
    # symbol digest to every child process.  This unit test exercises the
    # auto-pin path, so make the absence of both selected-input digests part
    # of its fixture instead of depending on the caller's environment.  The
    # ROM digest is asserted below as a guard against the analogous leak.
    for name in ("POKERED_ROM_PATH", "POKERED_ROM_SHA1", "POKERED_SYM_SHA1"):
        monkeypatch.delenv(name, raising=False)

    symbol_sha = hashlib.sha1(b"sym").hexdigest()
    environment = gate.build_test_environment(
        tmp_path,
        rom_root,
        tmp_path / "fixtures",
        {
            Path("red/pokemon-red.gb"): hashlib.sha1(b"rom").hexdigest(),
            Path("red/pokemon-red.sym"): symbol_sha,
        },
    )

    assert environment["POKERED_ROM_SHA1"] == hashlib.sha1(b"rom").hexdigest()
    assert environment["POKERED_SYM_SHA1"] == symbol_sha


def test_gate_report_loader_counts_xfail_and_skip_reasons(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "counts": {
                    "total": 3,
                    "passed": 1,
                    "failed": 0,
                    "skipped": 1,
                    "xfailed": 1,
                    "xpassed": 0,
                    "errors": 0,
                },
                "tests": [
                    {
                        "nodeid": "tests/test_gate.py::test_pass",
                        "outcome": "passed",
                        "when": "call",
                        "reason": "",
                        "was_xfail": False,
                    },
                    {
                        "nodeid": "tests/test_gate.py::test_skip",
                        "outcome": "skipped",
                        "when": "call",
                        "reason": "missing ROM",
                        "was_xfail": False,
                    },
                    {
                        "nodeid": "tests/test_gate.py::test_xfail",
                        "outcome": "skipped",
                        "when": "call",
                        "reason": "known issue",
                        "was_xfail": True,
                    },
                ],
                "collection_errors": [],
                "collection_skips": [],
                "collected": 3,
                "nodeids": [
                    "tests/test_gate.py::test_pass",
                    "tests/test_gate.py::test_skip",
                    "tests/test_gate.py::test_xfail",
                ],
                "exitstatus": 0,
            }
        ),
        encoding="utf-8",
    )
    counts, reasons, error = gate.load_gate_report(report)
    assert error == ""
    assert counts.total == 3
    assert counts.skipped == 1
    assert counts.xfailed == 1
    assert reasons == {"known issue": 1, "missing ROM": 1}


def test_gate_report_loader_counts_collection_skip_reasons(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "counts": {
                    "total": 1,
                    "passed": 1,
                    "failed": 0,
                    "skipped": 0,
                    "xfailed": 0,
                    "xpassed": 0,
                    "errors": 0,
                },
                "tests": [
                    {
                        "nodeid": "tests/test_gate.py::test_pass",
                        "outcome": "passed",
                        "when": "call",
                        "reason": "",
                        "was_xfail": False,
                    }
                ],
                "collection_errors": [],
                "collection_skips": [
                    {
                        "nodeid": "tests/test_optional.py",
                        "reason": "optional fixture unavailable",
                    }
                ],
                "collected": 1,
                "nodeids": ["tests/test_gate.py::test_pass"],
                "exitstatus": 0,
            }
        ),
        encoding="utf-8",
    )

    _counts, reasons, error = gate.load_gate_report(report)

    assert error == ""
    assert reasons == {"optional fixture unavailable": 1}


def test_optional_skip_is_explicit_and_non_required():
    result = gate.synthetic_optional_skip("trade", "optional trade unavailable: no fixtures")
    assert result.status == "SKIP"
    assert result.required is False
    assert result.counts.skipped == 1
    assert result.skip_reasons == {"optional trade unavailable: no fixtures": 1}


def test_optional_tier_with_partial_skips_is_not_reported_as_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "OPTIONAL_TIERS", frozenset({"unit"}))

    def fake_run_pytest_once(**kwargs):
        return (
            0,
            gate.GateReport(
                counts=gate.Counts(total=2, passed=1, skipped=1),
                skip_reasons={"optional fixture unavailable": 1},
                nodeids=(
                    "tests/test_optional.py::test_available",
                    "tests/test_optional.py::test_missing_fixture",
                ),
            ),
            "",
            ["python", "-m", "pytest"],
        )

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

    assert result.status == "SKIP"
    assert result.counts == gate.Counts(total=2, passed=1, skipped=1)
    assert result.skip_reasons == {"optional fixture unavailable": 1}


def test_rom_helper_honors_explicit_roots(tmp_path, monkeypatch):
    configured_rom = tmp_path / "external-rom"
    configured_fixture = tmp_path / "external-fixtures"
    monkeypatch.setenv("POKERED_ROM_ROOT", str(configured_rom))
    monkeypatch.setenv("POKERED_FIXTURE_ROOT", str(configured_fixture))
    assert find_rom_root(tmp_path) == configured_rom
    assert find_fixture_root(tmp_path) == configured_fixture
    assert rom_path("yellow", project_root=tmp_path) == (
        configured_rom / "yellow" / "pokemon-yellow.gbc"
    )
    assert sym_path("blue", project_root=tmp_path) == configured_rom / "blue" / "pokemon-blue.sym"
    assert fixture_path("red", project_root=tmp_path) == (
        configured_fixture / "red" / "cable_club.state"
    )


def test_relative_roots_are_anchored_to_the_inspected_project(tmp_path, monkeypatch):
    monkeypatch.setenv("POKERED_ROM_ROOT", "external-rom")
    monkeypatch.setenv("POKERED_FIXTURE_ROOT", "external-fixtures")

    assert gate.find_rom_root(tmp_path) == tmp_path / "external-rom"
    assert gate.find_fixture_root(tmp_path) == tmp_path / "external-fixtures"
    assert find_rom_root(tmp_path) == tmp_path / "external-rom"
    assert find_fixture_root(tmp_path) == tmp_path / "external-fixtures"
    assert gate.find_rom_root(tmp_path, Path("explicit-rom")) == tmp_path / "explicit-rom"
    assert gate.find_fixture_root(tmp_path, Path("explicit-fixtures")) == (
        tmp_path / "explicit-fixtures"
    )


def test_collection_preflight_requires_the_selected_environment_console_script(tmp_path):
    python = tmp_path / "bin" / "python"
    python.parent.mkdir()
    python.write_text("", encoding="utf-8")

    results = gate.run_collection_preflight(
        project_root=tmp_path,
        python_executable=python,
        environment={},
        timeout_seconds=1,
    )

    assert {result.name for result in results} == {"python-module", "pytest-console"}
    console = next(result for result in results if result.name == "pytest-console")
    assert console.status == "FAIL"
    assert "not found beside" in console.reason


def test_collection_preflight_runs_module_and_console_commands(tmp_path, monkeypatch):
    python = tmp_path / "bin" / "python"
    python.parent.mkdir()
    python.write_text("", encoding="utf-8")
    console = python.parent / "pytest"
    console.write_text("", encoding="utf-8")

    calls = []

    def fake_run_collection_command(**kwargs):
        calls.append(kwargs)
        return gate.CollectionResult(
            name=kwargs["name"],
            command=kwargs["command"],
            status="PASS",
            returncode=0,
        )

    monkeypatch.setattr(gate, "_run_collection_command", fake_run_collection_command)
    results = gate.run_collection_preflight(
        project_root=tmp_path,
        python_executable=python,
        environment={},
    )

    assert [result.name for result in results] == ["python-module", "pytest-console"]
    assert [call["command"] for call in calls] == [
        [
            str(python),
            "-m",
            "pytest",
            "tests",
            "--collect-only",
            "-q",
            "--strict-config",
            "--strict-markers",
            "-p",
            "pytest_asyncio.plugin",
            "-p",
            "tests._gate_report",
        ],
        [
            str(console),
            "tests",
            "--collect-only",
            "-q",
            "--strict-config",
            "--strict-markers",
            "-p",
            "pytest_asyncio.plugin",
            "-p",
            "tests._gate_report",
        ],
    ]
    assert all(call["report_path"].is_absolute() for call in calls)


def test_collection_preflight_rejects_different_entry_point_test_trees(tmp_path, monkeypatch):
    python = tmp_path / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    (python.parent / "pytest").write_text("", encoding="utf-8")

    def fake_run_collection_command(**kwargs):
        nodeids = (
            "tests/test_one.py::test_one",
            "tests/test_two.py::test_two",
        )
        if kwargs["name"] == "pytest-console":
            nodeids = nodeids[:1]
        return gate.CollectionResult(
            name=kwargs["name"],
            command=kwargs["command"],
            status="PASS",
            returncode=0,
            nodeids=nodeids,
        )

    monkeypatch.setattr(gate, "_run_collection_command", fake_run_collection_command)
    results = gate.run_collection_preflight(
        project_root=tmp_path,
        python_executable=python,
        environment={},
    )

    assert [result.status for result in results] == ["FAIL", "FAIL"]
    assert all("different test trees" in result.reason for result in results)


def test_gate_report_loader_accepts_collection_only_inventory(tmp_path):
    report = tmp_path / "collection.json"
    report.write_text(
        json.dumps(
            {
                "collection_only": True,
                "counts": {
                    "total": 0,
                    "passed": 0,
                    "failed": 0,
                    "skipped": 0,
                    "xfailed": 0,
                    "xpassed": 0,
                    "errors": 0,
                },
                "tests": [],
                "collection_errors": [],
                "collection_skips": [],
                "collected": 1,
                "nodeids": ["tests/test_one.py::test_one"],
                "exitstatus": 0,
            }
        ),
        encoding="utf-8",
    )

    loaded = gate._load_gate_report(report, expected_returncode=0)
    assert loaded.error == ""
    assert loaded.collection_only is True
    assert loaded.nodeids == ("tests/test_one.py::test_one",)


def test_gate_report_loader_accepts_only_explicit_partial_progress(tmp_path):
    report = tmp_path / "progress.json"
    report.write_text(
        json.dumps(
            {
                "collection_only": False,
                "counts": {
                    "total": 1,
                    "passed": 1,
                    "failed": 0,
                    "skipped": 0,
                    "xfailed": 0,
                    "xpassed": 0,
                    "errors": 0,
                },
                "tests": [
                    {
                        "nodeid": "tests/test_one.py::test_one",
                        "outcome": "passed",
                        "when": "call",
                        "reason": "",
                        "was_xfail": False,
                    }
                ],
                "collection_errors": [],
                "collection_skips": [],
                "collected": 2,
                "nodeids": [
                    "tests/test_one.py::test_one",
                    "tests/test_two.py::test_two",
                ],
                "exitstatus": -1,
            }
        ),
        encoding="utf-8",
    )

    partial = gate._load_gate_report(report, allow_partial=True)
    assert partial.error == ""
    assert partial.counts == gate.Counts(total=1, passed=1)
    assert len(partial.nodeids) == 2

    complete = gate._load_gate_report(report)
    assert complete.error.startswith("invalid pytest report:")


def test_runtime_problems_reject_a_manifest_revision_mismatch(tmp_path):
    versions = tmp_path / "VERSIONS.md"
    versions.write_text(
        "| PyBoy | `2.7.0` + fork `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa` |\n",
        encoding="utf-8",
    )
    vendor = tmp_path / "vendor" / "pyboy-src"
    vendor.mkdir(parents=True)
    (vendor / "POKERED_HARNESS_PYBOY_REVISION").write_text(
        "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n",
        encoding="ascii",
    )
    runtime = {
        "pyboy_version": "2.7.0",
        "pyboy_revision": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "serial_contract": "bit-accurate-backend",
        "pyboy_module": "pyboy",
        "harness_module": "pokered_harness",
    }

    problems = gate.runtime_problems(tmp_path, runtime)

    assert any("does not match VERSIONS.md" in problem for problem in problems)


def test_evidence_bundle_is_portable_sanitized_and_diagnostic(tmp_path):
    project_root = tmp_path / "checkout"
    rom_root = project_root / "rom"
    fixture_root = project_root / "tests" / "fixtures" / "link"
    asset_path = rom_root / "red" / "pokemon-red.gb"
    asset_path.parent.mkdir(parents=True)

    runtime = {
        "python_executable": str(project_root / ".venv" / "bin" / "python"),
        "python_version": "3.12.13",
        "pyboy_module": str(project_root / "vendor" / "pyboy-src" / "pyboy"),
        "pyboy_import_error": (
            "Authorization: Bearer topsecret b'ROM_BYTES' "
            r"path=C:\agent work\private\secret.gb"
        ),
    }
    collections = [
        gate.CollectionResult(
            name="python-module",
            command=[str(project_root / ".venv" / "bin" / "python"), "-m", "pytest"],
            status="PASS",
            returncode=0,
            nodeids=(
                "tests/test_gate.py::test_case[password=topsecret]",
                str(project_root / "tests" / "test_gate.py") + "::test_path",
            ),
        )
    ]
    tiers = [
        gate.TierResult(
            name="remote",
            description="remote",
            expression="remote",
            required=True,
            status="FAIL",
            counts=gate.Counts(total=1, failed=1),
            returncodes=[1],
            output_tail="password=topsecret b'ROM_BYTES'\nassertion failed",
            reason="peer failed at /sensitive/path",
            iteration_failures=["iteration 1: token=topsecret"],
        )
    ]
    payload = gate.build_evidence_payload(
        project_root=project_root,
        rom_root=rom_root,
        fixture_root=fixture_root,
        runtime=runtime,
        assets=[
            gate.AssetRecord(
                label="red-stock",
                kind="rom",
                path=str(asset_path),
                status="ok",
                size=123,
                actual_sha1="a" * 40,
            )
        ],
        collections=collections,
        tiers=tiers,
        gate_problems=["credential=topsecret at " + str(project_root)],
        overall="FAIL",
    )

    serialized = json.dumps(payload, sort_keys=True)
    assert "topsecret" not in serialized
    assert "ROM_BYTES" not in serialized
    assert "secret.gb" not in serialized
    assert str(tmp_path) not in serialized
    assert "/sensitive/path" not in serialized
    assert payload["assets"][0]["path"] == "<rom-root>/red/pokemon-red.gb"
    assert all("topsecret" not in nodeid for nodeid in payload["collections"][0]["nodeids"])
    assert all(str(tmp_path) not in nodeid for nodeid in payload["collections"][0]["nodeids"])
    assert payload["tiers"][0]["status"] == "FAIL"
    assert "assertion failed" in payload["tiers"][0]["output_tail"]

    evidence_dir = tmp_path / "evidence" / "partial-run"
    paths = gate.write_evidence_bundle(evidence_dir, payload)
    assert set(paths) == {"report", "text", "manifest"}
    assert {path.name for path in paths.values()} == {
        "gate-report.json",
        "gate-report.txt",
        "evidence-manifest.json",
    }
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert report["overall"] == "FAIL"
    assert report["tiers"][0]["iteration_failures"]
    assert manifest["overall"] == "FAIL"
    assert {entry["path"] for entry in manifest["files"]} == {
        "gate-report.json",
        "gate-report.txt",
    }
    report_hash = hashlib.sha256(paths["report"].read_bytes()).hexdigest()
    assert (
        next(entry for entry in manifest["files"] if entry["path"] == "gate-report.json")["sha256"]
        == report_hash
    )
    text = paths["text"].read_text(encoding="utf-8")
    assert "overall: FAIL" in text
    assert "assertion failed" in text
    assert "topsecret" not in text
    assert "ROM_BYTES" not in text
    gate.verify_evidence_bundle(evidence_dir)

    paths["report"].write_text(
        paths["report"].read_text(encoding="utf-8") + "tampered\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="(?:size|sha256) mismatch"):
        gate.verify_evidence_bundle(evidence_dir)


def test_evidence_bundle_rejects_unlisted_files(tmp_path):
    payload = gate.build_evidence_payload(
        project_root=tmp_path / "checkout",
        rom_root=tmp_path / "rom",
        fixture_root=tmp_path / "fixtures",
        runtime={},
        assets=[],
        collections=[],
        tiers=[],
        gate_problems=[],
        overall="PASS",
    )
    evidence_dir = tmp_path / "evidence"
    gate.write_evidence_bundle(evidence_dir, payload)
    (evidence_dir / "raw-debug.log").write_text("unredacted", encoding="utf-8")

    with pytest.raises(ValueError, match="unexpected files"):
        gate.verify_evidence_bundle(evidence_dir)


def test_parser_accepts_evidence_directory():
    args = gate.build_parser().parse_args(["--evidence-dir", "retained-evidence"])
    assert args.evidence_dir == Path("retained-evidence")
