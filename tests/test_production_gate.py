"""Focused, ROM-free tests for the acceptance-gate plumbing."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from tests._rom_assets import find_fixture_root, find_rom_root, fixture_path, rom_path, sym_path
from tests._tier_config import classify_test


_GATE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "production_gate.py"
_SPEC = importlib.util.spec_from_file_location("pokered_production_gate", _GATE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
gate = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = gate
_SPEC.loader.exec_module(gate)

_MATRIX_PATH = Path(__file__).resolve().parents[1] / "scripts" / "tcp_link_matrix.py"
_MATRIX_SPEC = importlib.util.spec_from_file_location("pokered_tcp_matrix", _MATRIX_PATH)
assert _MATRIX_SPEC is not None and _MATRIX_SPEC.loader is not None
matrix = importlib.util.module_from_spec(_MATRIX_SPEC)
sys.modules[_MATRIX_SPEC.name] = matrix
_MATRIX_SPEC.loader.exec_module(matrix)


def test_tier_classifier_keeps_unknown_tests_rom_free():
    marks = classify_test("tests/test_new_unit.py", "test_parser")
    assert marks == frozenset({"unit"})


def test_tier_classifier_keeps_boot_matrix_in_required_real_rom_scope():
    marks = classify_test(
        "tests/test_rom_boot_matrix.py", "test_pinned_rom_boot_save_load_replay"
    )
    assert marks == frozenset({"real_rom"})


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


def test_tier_classifier_marks_bootstrap_descendant_timeout_as_timing_sensitive():
    marks = classify_test(
        "tests/test_bootstrap_pyboy.py",
        "test_timeout_stops_build_descendants",
    )
    assert {"unit", "timing_sensitive"} <= marks


def test_timing_repeats_retain_first_failure_output_and_aggregate_counts(tmp_path, monkeypatch):
    calls = []

    def fake_run(**kwargs):
        calls.append(kwargs["report_path"].name)
        if len(calls) == 1:
            return (
                1, gate.Counts(total=7, passed=6, failed=1), {},
                "x" * 10000 + "\nFAILED test_late_rearm: early repeat failure", ["pytest"],
            )
        return 0, gate.Counts(total=7, passed=7), {}, "7 passed on later repeat", ["pytest"]

    monkeypatch.setattr(gate, "run_pytest_once", fake_run)
    result = gate.run_tier(
        name="timing", project_root=tmp_path, python_executable=Path(sys.executable),
        environment={}, required_problems=[], repeat=5, timeout_override=10,
        report_directory=tmp_path,
    )
    assert calls == [f"timing-{iteration}.json" for iteration in range(1, 6)]
    assert result.status == "FAIL"
    assert result.returncodes == [1, 0, 0, 0, 0]
    assert result.counts == gate.Counts(total=35, passed=34, failed=1)
    assert result.output_tail.startswith("pytest iteration 1/5, returncode=1\n")
    assert "FAILED test_late_rearm: early repeat failure" in result.output_tail
    assert "7 passed on later repeat" not in result.output_tail
    assert len(result.output_tail) <= 8000


def test_timing_repeats_retain_zero_exit_unexpected_outcome(tmp_path, monkeypatch):
    runs = iter([
        (0, gate.Counts(total=1, xfailed=1), {"known issue": 1}, "XFAIL original issue", ["pytest"]),
        *[(0, gate.Counts(total=1, passed=1), {}, "1 passed", ["pytest"])] * 4,
    ])
    monkeypatch.setattr(gate, "run_pytest_once", lambda **kwargs: next(runs))
    result = gate.run_tier(
        name="timing", project_root=tmp_path, python_executable=Path(sys.executable),
        environment={}, required_problems=[], repeat=5, timeout_override=10,
        report_directory=tmp_path,
    )
    assert result.status == "FAIL"
    assert result.returncodes == [0] * 5
    assert result.counts == gate.Counts(total=5, passed=4, xfailed=1)
    assert result.skip_reasons == {"known issue": 1}
    assert "XFAIL original issue" in result.output_tail


def test_run_pytest_once_report_failure_survives_long_pass_tail_and_later_passes(
    tmp_path, monkeypatch
):
    first_report = {
        "counts": {
            "total": 1,
            "passed": 0,
            "failed": 1,
            "skipped": 0,
            "xfailed": 0,
            "xpassed": 0,
            "errors": 0,
        },
        "tests": [
            {
                "nodeid": "tests/test_timing.py::test_first_iteration",
                "outcome": "failed",
                "when": "call",
                "reason": "first timing iteration lost the peer frontier",
            }
        ],
        "collection_errors": [],
    }
    later_report = {
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
                "nodeid": "tests/test_timing.py::test_first_iteration",
                "outcome": "passed",
                "when": "call",
                "reason": "",
            }
        ],
        "collection_errors": [],
    }
    processes = []

    class FakeProcess:
        def __init__(self, environment, iteration):
            self.environment = environment
            self.iteration = iteration
            self.returncode = 1 if iteration == 0 else 0

        def communicate(self, timeout=None):
            del timeout
            report = first_report if self.iteration == 0 else later_report
            Path(self.environment["POKERED_GATE_REPORT"]).write_text(
                json.dumps(report), encoding="utf-8"
            )
            marker = "FIRST_PASS_SUMMARY" if self.iteration == 0 else "LATER_PASS_MARKER"
            return ((marker + "\n") * 3000, "")

        def poll(self):
            return self.returncode

    def fake_popen(command, *, env, **kwargs):
        del command, kwargs
        process = FakeProcess(env, len(processes))
        processes.append(process)
        return process

    monkeypatch.setattr(gate.subprocess, "Popen", fake_popen)
    result = gate.run_tier(
        name="timing",
        project_root=tmp_path,
        python_executable=Path(sys.executable),
        environment={},
        required_problems=[],
        repeat=5,
        timeout_override=10,
        report_directory=tmp_path,
    )

    assert len(processes) == 5
    assert (tmp_path / "timing-1.json").is_file()
    assert result.status == "FAIL"
    assert result.returncodes == [1, 0, 0, 0, 0]
    assert result.counts == gate.Counts(total=5, passed=4, failed=1)
    assert result.output_tail.startswith("pytest iteration 1/5, returncode=1\n")
    assert "test_first_iteration" in result.output_tail
    assert "when='call'" in result.output_tail
    assert "first timing iteration lost the peer frontier" in result.output_tail
    assert "LATER_PASS_MARKER" not in result.output_tail
    assert len(result.output_tail) <= gate.MAX_OUTPUT_TAIL


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


def test_remote_battle_menu_setup_is_diagnostic_not_acceptance():
    marks = classify_test(
        "tests/test_pyboy_link_session_subprocess.py",
        "test_subprocess_pair_resolves_battle_turn_over_tcp",
    )
    assert {"real_rom", "remote_link", "battle", "battle_diagnostic"} <= marks
    assert "acceptance" not in marks
    assert "battle_acceptance" not in marks


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


def test_versions_symbol_sha_parser_pairs_each_symbol_path(tmp_path):
    versions = tmp_path / "VERSIONS.md"
    versions.write_text(
        """
| Symbols | `rom/red/pokemon-red.sym` |
| Symbol SHA-1 | `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa` |

| Symbols | `rom/yellow/pokemon-yellow.sym` |
| Symbol SHA-1 | `BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB` |
""",
        encoding="utf-8",
    )
    assert gate.parse_expected_symbol_sha1(versions) == {
        Path("red/pokemon-red.sym"): "a" * 40,
        Path("yellow/pokemon-yellow.sym"): "b" * 40,
    }


def test_versions_fixture_sha_parser_normalizes_fixture_paths(tmp_path):
    versions = tmp_path / "VERSIONS.md"
    versions.write_text(
        """
| Fixture | Observed SHA-1 | Provenance boundary |
|---|---|---|
| `red/cable_club.state` | `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa` | `verified` |
| `tests/fixtures/link/blue/cable_club-battle.state` | `BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB` | `verified` |
""",
        encoding="utf-8",
    )
    assert gate.parse_expected_fixture_sha1(versions) == {
        Path("red/cable_club.state"): "a" * 40,
        Path("blue/cable_club-battle.state"): "b" * 40,
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
        {Path("red/pokemon-red.gb"): "0" * 40},
    )
    by_label = {record.label: record for record in records}
    assert by_label["red-stock"].status == "sha1-mismatch"
    assert by_label["red-stock"].actual_sha1 == hashlib.sha1(b"wrong bytes").hexdigest()
    assert by_label["red"].status == "ok"
    assert by_label["blue"].status == "missing"
    assert any("blue-stock" in problem for problem in gate.required_asset_problems(records))


def test_asset_inspection_fails_closed_when_symbol_pin_is_missing(tmp_path):
    rom_root = tmp_path / "rom"
    fixture_root = tmp_path / "fixtures"
    symbol = rom_root / "red" / "pokemon-red.sym"
    symbol.parent.mkdir(parents=True)
    symbol.write_text("symbols", encoding="utf-8")

    records = gate.inspect_assets(
        rom_root,
        fixture_root,
        {},
        {Path("blue/pokemon-blue.sym"): "0" * 40},
    )
    red = next(record for record in records if record.label == "red")
    assert red.status == "missing-pin"
    assert any("symbol red" in problem for problem in gate.required_asset_problems(records))


def test_asset_inspection_fails_closed_when_rom_pin_is_missing(tmp_path):
    rom_root = tmp_path / "rom"
    fixture_root = tmp_path / "fixtures"
    rom = rom_root / "red" / "pokemon-red.gb"
    rom.parent.mkdir(parents=True)
    rom.write_bytes(b"rom without a documented pin")

    records = gate.inspect_assets(rom_root, fixture_root, {})
    red = next(record for record in records if record.label == "red-stock")
    assert red.status == "missing-pin"
    assert red.actual_sha1 == hashlib.sha1(rom.read_bytes()).hexdigest()
    assert any("rom red-stock" in problem for problem in gate.required_asset_problems(records))


def test_asset_inspection_validates_fixture_hash_when_pin_is_available(tmp_path):
    rom_root = tmp_path / "rom"
    fixture_root = tmp_path / "fixtures"
    fixture = fixture_root / "red" / "cable_club.state"
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(b"fixture bytes")

    records = gate.inspect_assets(
        rom_root,
        fixture_root,
        {},
        expected_fixture_sha1={Path("red/cable_club.state"): "0" * 40},
    )
    red = next(record for record in records if record.label == "red cable-club")
    assert red.status == "sha1-mismatch"
    assert red.expected_sha1 == "0" * 40
    assert red.actual_sha1 == hashlib.sha1(fixture.read_bytes()).hexdigest()
    assert any("fixture red cable-club" in problem for problem in gate.required_asset_problems(records))


def test_gate_entrypoint_rejects_fixtures_when_pin_table_is_absent(tmp_path, monkeypatch, capsys):
    fixture_root = tmp_path / "tests" / "fixtures" / "link"
    fixture = fixture_root / "red" / "cable_club.state"
    fixture.parent.mkdir(parents=True)
    fixture.write_bytes(b"unverified fixture")
    monkeypatch.setattr(
        gate, "probe_runtime", lambda *args: {"pyboy_serial_kind": "python-source"}
    )
    result = gate.main([
        "--repo-root", str(tmp_path),
        "--rom-root", str(tmp_path / "rom"),
        "--fixture-root", str(fixture_root),
        "--tier", "battle", "--format", "json",
    ])
    report = json.loads(capsys.readouterr().out)
    red = next(asset for asset in report["assets"] if asset["label"] == "red cable-club")
    assert red["status"] == "missing-pin"
    assert report["tiers"][0]["status"] == "BLOCKED"
    assert result == 1


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


def test_environment_cython_mode_does_not_shadow_native_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("PYBOY_NO_CYTHON", "1")
    environment = gate.build_test_environment(
        tmp_path,
        tmp_path / "rom",
        tmp_path / "fixtures",
        {},
        runtime_mode="cython",
    )
    assert environment["PYTHONPATH"].split(os.pathsep)[:2] == [
        str(tmp_path / "src"),
        str(tmp_path),
    ]
    assert "PYBOY_NO_CYTHON" not in environment


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
                    {"outcome": "passed", "reason": "", "was_xfail": False},
                    {"outcome": "skipped", "reason": "missing ROM", "was_xfail": False},
                    {"outcome": "skipped", "reason": "known issue", "was_xfail": True},
                ],
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


def test_structured_failure_diagnostics_survive_a_large_pass_summary(tmp_path):
    report = {
        "counts": {"total": 4, "passed": 0, "failed": 3, "skipped": 0},
        "tests": [
            {
                "nodeid": "tests/test_link.py::test_setup_failure",
                "outcome": "failed",
                "when": "setup",
                "reason": "fixture could not acquire the owner lock",
            },
            {
                "nodeid": "tests/test_link.py::test_call_failure",
                "outcome": "failed",
                "when": "call",
                "reason": "peer never reached the negotiated frontier",
            },
            {
                "nodeid": "tests/test_link.py::test_teardown_failure",
                "outcome": "failed",
                "when": "teardown",
                "reason": "peer teardown did not close the socket",
            },
        ],
        "collection_errors": [
            {
                "nodeid": "tests/test_remote.py",
                "reason": "ImportError: optional transport is unavailable",
            }
        ],
    }
    diagnostics = gate._structured_failure_diagnostics(report)
    bounded = gate._bounded_output_with_diagnostics(
        "PASSED test_%04d\n" * 2000,
        diagnostics,
    )
    assert len(bounded) <= gate.MAX_OUTPUT_TAIL
    assert "test_setup_failure" in bounded
    assert "when='setup'" in bounded
    assert "fixture could not acquire the owner lock" in bounded
    assert "test_call_failure" in bounded
    assert "when='call'" in bounded
    assert "peer never reached the negotiated frontier" in bounded
    assert "test_teardown_failure" in bounded
    assert "when='teardown'" in bounded
    assert "peer teardown did not close the socket" in bounded
    assert "tests/test_remote.py" in bounded
    assert "optional transport is unavailable" in bounded


def test_structured_failure_reason_is_bounded_without_losing_both_ends():
    reason = "TRACEBACK-START\n" + ("x" * 12000) + "\nTRACEBACK-END"
    diagnostics = gate._structured_failure_diagnostics(
        {
            "tests": [
                {
                    "nodeid": "tests/test_large_reason.py::test_failure",
                    "outcome": "failed",
                    "when": "call",
                    "reason": reason,
                }
            ],
            "collection_errors": [],
        }
    )
    assert len(diagnostics) <= gate.MAX_OUTPUT_TAIL - 128
    assert "TRACEBACK-START" in diagnostics
    assert "TRACEBACK-END" in diagnostics
    assert "[report reason truncated]" in diagnostics


def test_gate_report_preserves_module_collection_skips():
    """A module-level importorskip must count against a required tier."""
    report_plugin_path = Path(__file__).resolve().parents[1] / "tests" / "_gate_report.py"
    report_spec = importlib.util.spec_from_file_location("pokered_gate_report_test", report_plugin_path)
    assert report_spec is not None and report_spec.loader is not None
    report_plugin = importlib.util.module_from_spec(report_spec)
    report_spec.loader.exec_module(report_plugin)
    report_plugin._ACTIVE_RECORDS.clear()
    report_plugin._ACTIVE_COLLECTION_ERRORS.clear()
    try:
        report_plugin.pytest_collectreport(
            SimpleNamespace(
                skipped=True,
                failed=False,
                nodeid="tests/test_mcp_stdio_integration.py",
                longrepr=("file", 1, "mcp dependency missing"),
            )
        )
        records = report_plugin._ACTIVE_RECORDS
        assert len(records) == 1
        record = next(iter(records.values()))
        assert record["outcome"] == "skipped"
        assert record["when"] == "collection"
        assert record["reason"] == "mcp dependency missing"
    finally:
        report_plugin._ACTIVE_RECORDS.clear()
        report_plugin._ACTIVE_COLLECTION_ERRORS.clear()


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


def test_matrix_fingerprint_changes_when_an_asset_changes(tmp_path, monkeypatch):
    rom_root = tmp_path / "rom"
    fixture_root = tmp_path / "fixtures"
    monkeypatch.setenv("POKERED_ROM_ROOT", str(rom_root))
    monkeypatch.setenv("POKERED_FIXTURE_ROOT", str(fixture_root))
    first = matrix._matrix_fingerprint(
        repo_root=tmp_path,
        modes=["trade"],
        versions=["red_color"],
        deadline_seconds=30.0,
        python="python",
    )
    path = rom_root / "red" / "pokemon-red-color.gb"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"asset-v1")
    second = matrix._matrix_fingerprint(
        repo_root=tmp_path,
        modes=["trade"],
        versions=["red_color"],
        deadline_seconds=30.0,
        python="python",
    )
    assert first != second


def test_matrix_result_parser_rejects_malformed_or_non_mapping_payloads():
    assert matrix._parse_result(
        f"{matrix.RESULT_MARKER}not-json\n"
        f"{matrix.RESULT_MARKER}[1, 2]\n"
    ) is None
    assert matrix._parse_result(
        f"{matrix.RESULT_MARKER}{{\"ok\": true}}\n"
    ) == {"ok": True}


def _party_record_snapshot(species):
    return {
        "count": 1,
        "species": [species, 0xFF],
        "mon_records": [(bytes([species]) + bytes(43)).hex()],
    }


def test_matrix_requires_completed_distinguishable_party_swap():
    a = _party_record_snapshot(25)
    b = _party_record_snapshot(54)
    assert matrix._verify_party_swap([
        {"party_before": a, "party_after": b},
        {"party_before": b, "party_after": a},
    ]) == []
    assert matrix._verify_party_swap([
        {"party_before": a, "party_after": a},
        {"party_before": b, "party_after": b},
    ])
    assert matrix._verify_party_swap([
        {"party_before": a, "party_after": a},
        {"party_before": a, "party_after": a},
    ]) == ["trade lead records are indistinguishable; swap is unproven"]


def test_matrix_rejects_missing_or_inconsistent_party_data():
    assert matrix._verify_party_swap([{}, {}]) == ["trade party snapshot is missing"]
    a = _party_record_snapshot(25)
    b = _party_record_snapshot(54)
    b["species"][0] = 26
    assert matrix._verify_party_swap([
        {"party_before": a, "party_after": b},
        {"party_before": b, "party_after": a},
    ]) == ["trade party records disagree with species list"]
