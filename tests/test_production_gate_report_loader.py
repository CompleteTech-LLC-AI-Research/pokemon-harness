"""Gate-report loading, preflight and local-gate accounting (#129).

Split from ``tests/test_production_gate.py`` for #129 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tests._production_gate_support import (
    _CANONICAL_BOOT_NODEIDS,
    gate,
)
from tests._rom_assets import (
    find_fixture_root,
    find_rom_root,
    fixture_path,
    rom_path,
    sym_path,
)
from tests._tier_config import TIER_REQUIRED_NODEIDS


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


@pytest.mark.parametrize("missing_nodeid", sorted(_CANONICAL_BOOT_NODEIDS))
def test_local_gate_rejects_each_missing_canonical_boot_result(
    tmp_path, monkeypatch, missing_nodeid
):
    complete = TIER_REQUIRED_NODEIDS["local"]
    assert _CANONICAL_BOOT_NODEIDS <= complete

    def fake_run_pytest_once(**kwargs):
        selected = tuple(sorted(complete - {missing_nodeid}))
        return (
            0,
            gate.GateReport(
                counts=gate.Counts(total=len(selected), passed=len(selected)),
                nodeids=selected,
            ),
            "",
            ["python", "-m", "pytest"],
        )

    monkeypatch.setattr(gate, "run_pytest_once", fake_run_pytest_once)
    result = gate.run_tier(
        name="local",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        repeat=1,
        timeout_override=1,
        report_directory=tmp_path,
        required_nodeids=complete,
    )
    assert result.status == "FAIL"
    assert any(
        f"required matrix case is absent from selected items: {missing_nodeid}" in failure
        for failure in result.iteration_failures
    )


@pytest.mark.parametrize(
    "relative",
    (
        "red/pokemon-red-color.gb",
        "blue/pokemon-blue-color.gb",
        "yellow/pokemon-yellow.gbc",
        "red/pokemon-red.sym",
        "blue/pokemon-blue.sym",
        "yellow/pokemon-yellow.sym",
    ),
)
def test_local_gate_preflight_blocks_each_missing_canonical_asset(tmp_path, monkeypatch, relative):
    records = gate.inspect_assets(tmp_path / "rom", tmp_path / "fixtures", {})
    # Isolate one real missing-file inspection result so unrelated missing
    # stock ROMs or fixtures cannot make this rejection pass accidentally.
    (record,) = [r for r in records if Path(r.path) == tmp_path / "rom" / relative]
    assert record.status == "missing"
    problems = gate.required_asset_problems([record])
    assert len(problems) == 1

    def unexpected_pytest(**kwargs):
        pytest.fail("asset preflight must block before spawning pytest")

    monkeypatch.setattr(gate, "run_pytest_once", unexpected_pytest)
    result = gate.run_tier(
        name="local",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=problems,
        repeat=1,
        timeout_override=1,
        report_directory=tmp_path,
        required_nodeids=_CANONICAL_BOOT_NODEIDS,
    )
    assert result.status == "BLOCKED"
    assert result.required
    assert problems[0] in result.reason


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
