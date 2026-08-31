"""Focused, ROM-free tests for the acceptance-gate plumbing."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from scripts.tcp_link_matrix import (
    LOCAL_VARIANT_NODEIDS,
    LOCAL_VERSION_PAIR_NODEIDS,
    REMOTE_REVERSED_ROLE_NODEIDS,
    REMOTE_VERSION_PAIR_NODEIDS,
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


def test_required_matrix_manifest_covers_ordered_versions_and_variants():
    assert len(TIER_REQUIRED_NODEIDS["remote"]) == 11
    assert len(TIER_REQUIRED_NODEIDS["local"]) == 18
    assert TIER_REQUIRED_NODEIDS == required_matrix_nodeids()
    assert len(LOCAL_VERSION_PAIR_NODEIDS) == 9
    assert len(REMOTE_VERSION_PAIR_NODEIDS) == 9
    assert len(REMOTE_REVERSED_ROLE_NODEIDS) == 6
    assert len(LOCAL_VARIANT_NODEIDS) == 9
    assert any("[red-blue]" in nodeid for nodeid in TIER_REQUIRED_NODEIDS["remote"])
    assert any("[blue-red]" in nodeid for nodeid in TIER_REQUIRED_NODEIDS["remote"])
    assert any("[red-vanilla-x-color]" in nodeid for nodeid in TIER_REQUIRED_NODEIDS["local"])
    assert any("[blue-color-x-vanilla]" in nodeid for nodeid in TIER_REQUIRED_NODEIDS["local"])


def test_matrix_audit_fails_closed_on_missing_cases_and_reports_unrun_runtime():
    expected = set(required_matrix_nodeids()["remote"])
    missing = next(iter(REMOTE_VERSION_PAIR_NODEIDS))

    audit = audit_collection(expected - {missing})

    assert audit["structural_pass"] is False
    assert missing in audit["groups"]["remote-role-pairs"]["missing"]
    assert audit["acceptance_matrix_complete"] is False
    assert audit["runtime"] == "not-run"


def test_matrix_audit_surfaces_collection_skips_even_when_they_are_described():
    audit = audit_collection(
        required_matrix_nodeids()["local"],
        collection_skips=("optional dependency unavailable",),
    )

    assert audit["structural_pass"] is False
    assert audit["collection_skips"] == ("optional dependency unavailable",)


def test_strict_acceptance_gap_report_keeps_uncovered_ordered_cases_explicit():
    gaps = acceptance_matrix_gaps()

    assert len(gaps["trade"]) == 15
    assert len(gaps["battle"]) == 15
    assert ("remote", "blue", "red") not in gaps["trade"]
    assert ("remote", "blue", "red") not in gaps["battle"]
    assert ("remote", "yellow", "blue") in gaps["trade"]
    assert ("remote", "yellow", "blue") in gaps["battle"]


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
    environment = gate.build_test_environment(
        tmp_path,
        rom_root,
        tmp_path / "fixtures",
        {Path("red/pokemon-red.gb"): hashlib.sha1(b"rom").hexdigest()},
    )
    assert environment["POKERED_ROM_ROOT"] == str(rom_root)
    assert environment["POKERED_ROM_PATH"] == explicit
    assert environment["PYTHONPATH"].split(os.pathsep)[:3] == [
        str(tmp_path / "vendor" / "pyboy-src"),
        str(tmp_path / "src"),
        str(tmp_path),
    ]


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


def test_optional_skip_is_explicit_and_non_required():
    result = gate.synthetic_optional_skip("trade", "optional trade unavailable: no fixtures")
    assert result.status == "SKIP"
    assert result.required is False
    assert result.counts.skipped == 1
    assert result.skip_reasons == {"optional trade unavailable: no fixtures": 1}


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
            "-p",
            "tests._gate_report",
        ],
        [
            str(console),
            "tests",
            "--collect-only",
            "-q",
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
        "pyboy_import_error": "Authorization: Bearer topsecret b'ROM_BYTES'",
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


def test_parser_accepts_evidence_directory():
    args = gate.build_parser().parse_args(["--evidence-dir", "retained-evidence"])
    assert args.evidence_dir == Path("retained-evidence")
