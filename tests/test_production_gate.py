"""Focused, ROM-free tests for the acceptance-gate plumbing."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._rom_assets import find_fixture_root, find_rom_root, fixture_path, rom_path, sym_path
from tests._tier_config import classify_test


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
    assert rom_path("yellow", project_root=tmp_path) == configured_rom / "yellow" / "pokemon-yellow.gbc"
    assert sym_path("blue", project_root=tmp_path) == configured_rom / "blue" / "pokemon-blue.sym"
    assert fixture_path("red", project_root=tmp_path) == configured_fixture / "red" / "cable_club.state"


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
        [str(python), "-m", "pytest", "--collect-only", "-q"],
        [str(console), "--collect-only", "-q"],
    ]
