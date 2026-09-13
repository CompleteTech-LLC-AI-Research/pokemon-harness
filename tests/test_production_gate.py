"""Focused, ROM-free tests for the acceptance-gate plumbing."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

import pytest

import scripts.tcp_link_matrix as matrix
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
    creation_kwargs: ClassVar[list[dict[str, object]]] = []

    def __init__(self, command, *, env, **kwargs):
        self.command = command
        self.returncode = None if self.mode == "hang" else 0
        self.pid = 999999999
        self.stdout = io.StringIO("")
        self.commands.append((command, env))
        self.instances.append(self)
        self.creation_kwargs.append(dict(kwargs))
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


# Independently reviewed bodies use fake memory/backends/results or ordinary
# Python children, never ROM peers. Keep this oracle independent of the tier
# implementation: deriving it from its allowlist would hide missing entries.
_SUBPROCESS_MODULE = "tests/test_pyboy_link_session_subprocess.py"
_REVIEWED_SUBPROCESS_UNIT_TESTS = (
    "test_link_menu_history_preserves_first_samples_across_buffer_reuse",
    "test_link_menu_history_validates_call_and_bank",
    "test_link_menu_history_rejects_call_to_wrong_target",
    "test_link_menu_history_additive_result_compatibility",
    "test_link_menu_history_reports_missing_symbols_and_registration_errors",
    "test_link_menu_history_bounds_callback_errors_and_keeps_partial_samples",
    "test_link_menu_history_decisive_directions_ignore_stale_second_bytes",
    "test_link_menu_history_received_candidate_follows_rom_order",
    "test_link_menu_history_failure_summary_survives_large_result_tail",
    "test_link_menu_history_missing_call_symbols_remains_observable",
    "test_link_menu_history_rejects_post_call_outside_bank",
    "test_peer_trace_watchdog",
    "test_setup_handshake_failure_returns_bounded_non_success_sentinels",
    "test_collect_pair_rejects_missing_or_partial_required_rows",
    "test_strict_acceptance_rejects_link_menu_only_result",
    "test_strict_acceptance_rejects_missing_native_edge_req",
    "test_peer_shutdown_drains_live_serial_work_before_starting_teardown_marker",
    "test_peer_shutdown_protocol_propagates_backend_errors",
    "test_peer_shutdown_ready_marker_times_out_without_post_marker_ticks",
    "test_link_menu_finish_starts_teardown_only_through_draining_helper",
    "test_hold_at_sync_boundary_does_not_tick_past_ready_marker",
    "test_hold_at_sync_boundary_ticks_timed_rom_phase",
    "test_collect_pair_enforces_hard_deadline_without_waiting_for_peers",
    "test_partial_peer_sentinel_is_fatal_before_gameplay_assertions",
)
_REVIEWED_SUBPROCESS_REAL_TESTS = {
    "test_subprocess_pair_reaches_link_menu_over_tcp": {"real_rom", "remote_link"},
    "test_subprocess_pair_completes_trade_over_tcp": {
        "real_rom",
        "remote_link",
        "acceptance",
        "trade",
        "trade_acceptance",
    },
    "test_subprocess_pair_resolves_battle_turn_over_tcp": {
        "real_rom",
        "remote_link",
        "acceptance",
        "battle",
        "battle_acceptance",
    },
}


@pytest.mark.parametrize("test_name", _REVIEWED_SUBPROCESS_UNIT_TESTS)
def test_tier_classifier_subprocess_reviewed_fakes_are_exactly_unit(test_name):
    assert classify_test(_SUBPROCESS_MODULE, test_name) == {"unit"}


@pytest.mark.parametrize("test_name,expected", _REVIEWED_SUBPROCESS_REAL_TESTS.items())
def test_tier_classifier_subprocess_real_entrypoints_keep_exact_markers(test_name, expected):
    assert classify_test(_SUBPROCESS_MODULE, test_name) == expected


@pytest.mark.parametrize(
    "test_name",
    (
        "test_future_subprocess_case",
        "test_fake_only_future_case",
        "test_link_menu_history_future_case",
        "test_peer_shutdown_protocol_future_case",
        "test_collect_pair_future_case",
        "test_hold_at_sync_boundary_future_case",
        "test_strict_acceptance_future_case",
        "test_link_menu_history_validates_call_and_bank_future_case",
    ),
)
def test_tier_classifier_subprocess_unknown_names_remain_real_remote(test_name):
    assert classify_test(_SUBPROCESS_MODULE, test_name) == {"real_rom", "remote_link"}


def test_tier_classifier_subprocess_fake_exceptions_are_module_scoped():
    assert classify_test(
        "tests/test_link_integration_remote.py",
        "test_link_menu_history_validates_call_and_bank",
    ) == {
        "real_rom",
        "remote_link",
    }


def test_tier_classifier_subprocess_reviewed_inventory_matches_source():
    # Parse only: importing or collecting the live peer module is unnecessary.
    source = Path(__file__).resolve().parent / Path(_SUBPROCESS_MODULE).name
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    names = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]
    reviewed = set(_REVIEWED_SUBPROCESS_UNIT_TESTS) | set(_REVIEWED_SUBPROCESS_REAL_TESTS)
    assert len(_REVIEWED_SUBPROCESS_UNIT_TESTS) == len(set(_REVIEWED_SUBPROCESS_UNIT_TESTS))
    assert set(_REVIEWED_SUBPROCESS_UNIT_TESTS).isdisjoint(_REVIEWED_SUBPROCESS_REAL_TESTS)
    assert len(names) == len(set(names)), "duplicate test definitions hide reviewed coverage"
    assert set(names) == reviewed, "subprocess test inventory changed; review tier membership"


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


# Independent contract: do not derive these from the tier configuration.
_CANONICAL_BOOT_NODEIDS = frozenset(
    {
        "tests/test_rom_boot.py::test_canonical_rom_boot_state_roundtrip[red-color]",
        "tests/test_rom_boot.py::test_canonical_rom_boot_state_roundtrip[blue-color]",
        "tests/test_rom_boot.py::test_canonical_rom_boot_state_roundtrip[yellow]",
    }
)


def test_canonical_boot_nodes_are_required_and_classified_local():
    assert {
        nodeid
        for nodeid in TIER_REQUIRED_NODEIDS["local"]
        if nodeid.startswith("tests/test_rom_boot.py::")
    } == _CANONICAL_BOOT_NODEIDS
    assert classify_test("tests/test_rom_boot.py", "test_canonical_rom_boot_state_roundtrip") == {
        "real_rom"
    }
    assert gate.TIER_EXPRESSIONS["local"] == "real_rom and not remote_link and not acceptance"


def test_required_matrix_manifest_covers_ordered_versions_and_variants():
    assert SUPPORTED_VERSIONS == ("red", "blue", "yellow")
    legacy_remote = {
        "tests/test_link_integration_remote.py::"
        f"test_remote_handshake_writes_status_on_both_sides[{left}-{right}]"
        for left in ("red", "blue", "yellow")
        for right in ("red", "blue", "yellow")
    } | {
        "tests/test_mcp_real_link.py::test_mcp_remote_link_attaches_native_serial_backend",
        (
            "tests/test_pyboy_link_session_subprocess.py::"
            "test_subprocess_pair_reaches_link_menu_over_tcp"
        ),
    }
    timed_remote = {
        "tests/test_mcp_timed_rom.py::"
        f"test_timed_rom_stdio_pair[{listener}-listen-{connector}-connect]"
        for listener in ("red_color", "blue_color", "yellow")
        for connector in ("red_color", "blue_color", "yellow")
    }
    assert len(legacy_remote) == 11
    assert len(timed_remote) == 9
    assert legacy_remote.isdisjoint(timed_remote)
    assert matrix.TIMED_MCP_SMOKE_NODEIDS == timed_remote
    assert TIER_REQUIRED_NODEIDS["remote"] == legacy_remote | timed_remote
    assert len(TIER_REQUIRED_NODEIDS["local"]) == 21
    expected = required_matrix_nodeids()
    expected["local"] = expected["local"] | _CANONICAL_BOOT_NODEIDS
    assert TIER_REQUIRED_NODEIDS == expected
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


def test_standalone_matrix_collection_timeout_reaps_process_group(tmp_path, monkeypatch):
    class _HangingCollectionPopen:
        instances: ClassVar[list] = []

        def __init__(self, command, **kwargs):
            self.command = command
            self.kwargs = kwargs
            self.pid = 31337
            self.returncode = None
            self.stdout = io.StringIO("partial collection output")
            self.instances.append(self)

        def communicate(self, timeout=None):
            del timeout
            raise subprocess.TimeoutExpired(self.command, 1.0, output="partial collection output")

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            del timeout
            self.returncode = -signal.SIGTERM
            return self.returncode

        def terminate(self):
            self.returncode = -signal.SIGTERM

        def kill(self):
            self.returncode = -signal.SIGKILL

    killed_groups = []
    monkeypatch.setattr(matrix.subprocess, "Popen", _HangingCollectionPopen)
    monkeypatch.setattr(matrix.os, "killpg", lambda pid, sig: killed_groups.append((pid, sig)))

    audit, command = matrix.collect_nodeids(tmp_path, Path("python"), timeout_seconds=1.0)

    assert command[:3] == ["python", "-m", "pytest"]
    assert audit["structural_pass"] is False
    assert any("timed out after 1.0s" in error for error in audit["collection_errors"])
    assert killed_groups == [(31337, signal.SIGTERM)]
    process = _HangingCollectionPopen.instances[0]
    assert process.kwargs["start_new_session"] is True
    assert process.poll() is not None


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


def _diagnostic_evidence_payload(roots, diagnostics, *, dual=False):
    """Exercise diagnostic fan-out without unrelated asset-path resolutions."""
    result = _runtime_result("source", gate_problems=tuple(diagnostics))
    result.runtime.update({f"diagnostic_{i}": value for i, value in enumerate(diagnostics)})
    collection = result.collections[0]
    collection.nodeids = tuple(diagnostics)
    collection.output_tail = diagnostics[0]
    collection.reason = diagnostics[0]
    tier = result.tiers[0]
    tier.command = ["python", diagnostics[0]]
    tier.output_tail = diagnostics[0]
    tier.reason = diagnostics[0]
    tier.iteration_failures = list(diagnostics)
    tier.selected_nodeids = list(diagnostics)
    tier.skip_reasons = dict.fromkeys(diagnostics, 1)
    tier.case_results = [
        gate.MatrixCaseResult(
            nodeid=value,
            status="FAIL",
            returncode=1,
            duration_seconds=0.0,
            reason=value,
            output_tail=value,
        )
        for value in diagnostics
    ]
    common = {
        "project_root": roots[0],
        "rom_root": roots[1],
        "fixture_root": roots[2],
        "assets": [],
        "overall": "FAIL",
        "generated_at": "2026-09-05T00:00:00+00:00",
        "evidence_error": diagnostics[0],
    }
    if dual:
        other = _runtime_result("cython", gate_problems=tuple(diagnostics))
        other.runtime = dict(result.runtime, pyboy_mode="cython")
        other.collections = result.collections
        other.tiers = result.tiers
        return gate.build_dual_evidence_payload(**common, runtime_results=[result, other])
    return gate.build_evidence_payload(
        **common,
        runtime=result.runtime,
        collections=result.collections,
        tiers=result.tiers,
        gate_problems=result.gate_problems,
    )


@pytest.mark.parametrize("dual", [False, True], ids=["single", "dual"])
def test_evidence_prepares_roots_once_independent_of_diagnostic_count(tmp_path, monkeypatch, dual):
    roots = (tmp_path / "checkout", tmp_path / "checkout/rom", tmp_path / "fixtures")
    original_resolve = Path.resolve
    calls = []

    def counted_resolve(path, *args, **kwargs):
        calls.append(path)
        return original_resolve(path, *args, **kwargs)

    for count in (1, 128):
        diagnostics = [
            f"row {i}: {roots[0]} | {roots[1]} | {roots[2]} password=hidden" for i in range(count)
        ]
        calls.clear()
        with monkeypatch.context() as patch:
            patch.setattr(Path, "resolve", counted_resolve)
            payload = _diagnostic_evidence_payload(roots, diagnostics, dual=dual)
        assert len(calls) == len(roots)
        assert set(calls) == set(roots)
        expected = [
            f"row {i}: <project-root> | <rom-root> | <fixture-root> [CREDENTIAL REDACTED]"
            for i in range(count)
        ]
        results = payload["runtimes"] if dual else [payload]
        for result in results:
            assert result["gate_problems"] == expected
            assert result["collections"][0]["nodeids"] == expected
            assert result["tiers"][0]["iteration_failures"] == expected
            assert [case["reason"] for case in result["tiers"][0]["case_results"]] == expected
        assert payload["evidence_error"] == expected[0]


@pytest.mark.parametrize("dual", [False, True], ids=["single", "dual"])
@pytest.mark.parametrize("context", ["symlink", "home-expansion"])
def test_evidence_rebuild_refreshes_root_context(tmp_path, monkeypatch, dual, context):
    # Model a retargeted symlink / changed expansion without mutating host paths or HOME.
    roots = tuple(Path("~") / name for name in ("checkout", "rom", "fixtures"))
    state = {"target": tmp_path / "first"}
    calls = []

    def expanduser(path):
        if path in roots and context == "home-expansion":
            return state["target"] / path.name
        return path

    def resolve(path, *args, **kwargs):
        calls.append(path)
        return state["target"] / path.name

    with monkeypatch.context() as patch:
        patch.setattr(Path, "expanduser", expanduser)
        patch.setattr(Path, "resolve", resolve)
        for target in ("first", "second", "first"):
            state["target"] = tmp_path / target
            diagnostics = [
                "roots: " + " | ".join(str(state["target"] / root.name) for root in roots)
            ]
            calls.clear()
            payload = _diagnostic_evidence_payload(roots, diagnostics, dual=dual)
            assert len(calls) == len(roots)
            assert payload["evidence_error"] == (
                "roots: <project-root> | <rom-root> | <fixture-root>"
            )


@pytest.mark.parametrize("case", ["whitespace", "single-quote", "double-quote"])
def test_sanitizer_adversarial_inputs_complete_in_bounded_subprocess(case):
    # A generous process bound catches quadratic/exponential regressions without
    # depending on millisecond timings or hanging the pytest worker on old regexes.
    code = r"""
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("bounded_production_gate", sys.argv[1])
gate = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gate
spec.loader.exec_module(gate)
case = sys.argv[2]
if case == "whitespace":
    for suffix in ("", ",", ";"):
        value = "password" + " " * 200_000 + suffix
        assert gate._safe_text(value, limit=len(value)) == value
    assert gate._safe_text("password" + " " * 200_000 + "=value") == "[CREDENTIAL REDACTED]"
else:
    quote = "'" if case == "single-quote" else '"'
    value = "b" + quote + "\\" * 96
    assert gate._safe_text(value, limit=len(value)) == "[BINARY DATA REDACTED]"
    assert gate._safe_text(value + quote) == "[BINARY DATA REDACTED]"
    escaped_quote = "b" + quote + "left\\" + quote + "right" + quote
    assert gate._safe_text(escaped_quote) == "[BINARY DATA REDACTED]"
print("bounded sanitizer semantics passed")
"""
    completed = subprocess.run(
        [sys.executable, "-B", "-c", code, str(_GATE_PATH), case],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "bounded sanitizer semantics passed"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Authorization: Bearer hidden; done", "[CREDENTIAL REDACTED]; done"),
        ("API_KEY = hidden, done", "[CREDENTIAL REDACTED], done"),
        ("token\t hidden\nnext", "[CREDENTIAL REDACTED]\nnext"),
        ("password", "password"),
        ("password , next", "password , next"),
        ("ordinary diagnostic\nassertion failed", "ordinary diagnostic\nassertion failed"),
        ("b'payload'", "[BINARY DATA REDACTED]"),
        ('B"payload"', "[BINARY DATA REDACTED]"),
        (r"b'left\'right'", "[BINARY DATA REDACTED]"),
        ('b"left\\"right"', "[BINARY DATA REDACTED]"),
        ("b'unterminated", "[BINARY DATA REDACTED]"),
        ('b"unterminated', "[BINARY DATA REDACTED]"),
        ("b'ROM_BYTES\\", "[BINARY DATA REDACTED]"),
        ('b"ROM_BYTES\\', "[BINARY DATA REDACTED]"),
        ("prefix " + "x" * 128 + "; tail", "prefix [LONG TOKEN REDACTED]; tail"),
        ("\x00tail", r"\x00tail"),
    ],
)
def test_sanitizer_preserves_representative_diagnostic_semantics(value, expected):
    assert gate._safe_text(value) == expected


@pytest.mark.parametrize("kind", ["credential", "bytes"])
def test_safe_diagnostic_sanitizes_full_value_before_tail_trim(kind):
    private = "private-fragment-" * 700
    value = f"password={private}" if kind == "credential" else f"b'{private}'"
    expected = "[CREDENTIAL REDACTED]" if kind == "credential" else "[BINARY DATA REDACTED]"
    assert gate._safe_diagnostic(value + "\nassertion-tail", (), limit=64) == (
        expected + "\nassertion-tail"
    )


@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize("suffix", ["", "\\"])
def test_safe_diagnostic_redacts_unclosed_bytes_before_tail_trim(quote, suffix):
    # Spaces keep individual fragments below the generic long-token threshold.
    # Trimming first would lose the byte-literal prefix and expose ROM fragments.
    value = "b" + quote + "ROM-fragment " * 700 + suffix
    assert gate._safe_diagnostic(value, (), limit=64) == "[BINARY DATA REDACTED]"


def test_safe_diagnostic_legacy_and_prepared_context_have_identical_root_priority(
    tmp_path, monkeypatch
):
    project = tmp_path / "checkout"
    roots = gate._evidence_roots(project, project / "rom", project / "tests/fixtures")
    # Distinct canonical targets model symlink aliases for every named root.
    canonical = {root: tmp_path / "canonical" / label for label, root in roots}
    calls = []

    def resolve(path, *args, **kwargs):
        calls.append(path)
        return canonical[path]

    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", resolve)
        replacements = gate._prepare_root_replacements(roots)
        assert len(calls) == len(roots)
        for label, root in roots:
            for spelling in (root, canonical[root]):
                value = f"failed at {spelling}/case.py; password=hidden; b'payload'"
                expected = (
                    f"failed at <{label}>/case.py; [CREDENTIAL REDACTED]; [BINARY DATA REDACTED]"
                )
                calls.clear()
                assert gate._safe_diagnostic(value, roots, replacements=replacements) == expected
                assert calls == []
                assert gate._safe_diagnostic(value, roots) == expected
                assert len(calls) == len(roots)
        calls.clear()
        assert gate._safe_diagnostic("plain", roots, replacements=()) == "plain"
        assert calls == []


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


@pytest.fixture
def raw_pytest_output(monkeypatch):
    """Keep the real runner/report loader; replace only the pytest process."""

    outputs = {}

    class CompletedPytest:
        def __init__(self, command, *, env, **kwargs):
            report_path = Path(env["POKERED_GATE_REPORT"])
            collection = "--collect-only" in command
            failed = report_path.stem == "timing-2"
            self.returncode = int(failed)
            payload = _matrix_report(
                "tests/test_sample.py::test_one", outcome="failed" if failed else "passed"
            )
            payload["exitstatus"] = self.returncode
            if collection:
                payload.update(
                    collection_only=True,
                    counts={name: 0 for name in payload["counts"]},
                    tests=[],
                )
            report_path.write_text(json.dumps(payload), encoding="utf-8")
            self.output = (
                f"BEGIN {env.get('runtime_mode', 'source')} {report_path.stem}\n"
                "token=private-capture-secret\n"
                + "complete captured line\n" * 600
                + "END\n"
            )
            outputs[(env.get("runtime_mode", "source"), report_path.stem)] = self.output

        def communicate(self, timeout=None):
            return self.output, None

    monkeypatch.setattr(gate.subprocess, "Popen", CompletedPytest)
    return outputs


def test_dual_gate_retains_complete_private_logs_and_sanitized_evidence(
    tmp_path, monkeypatch, capsys, raw_pytest_output
):
    monkeypatch.setattr(
        gate, "build_test_environment",
        lambda *args, runtime_mode: {"runtime_mode": runtime_mode},
    )
    monkeypatch.setattr(
        gate, "probe_runtime",
        lambda _python, _root, env: {"pyboy_mode": env["runtime_mode"]},
    )
    monkeypatch.setattr(gate, "runtime_problems", lambda *args, **kwargs: [])
    monkeypatch.setattr(gate, "environment_policy_problems", lambda **kwargs: [])
    monkeypatch.setattr(gate, "_pytest_console_script", lambda path: path.parent / "pytest")
    monkeypatch.setattr(
        gate, "run_fixture_manifest_validation",
        lambda **kwargs: {"status": "PASS", "mode": "schema"},
    )
    monkeypatch.setattr(gate, "run_matrix_collection_audit", lambda **kwargs: {"status": "PASS"})
    raw_directory = tmp_path / "private-output"
    evidence_directory = tmp_path / "evidence"
    args = _patch_main_inputs(monkeypatch, tmp_path)
    args += [
        "--runtime-mode", "both",
        "--raw-output-dir", str(raw_directory),
        "--evidence-dir", str(evidence_directory),
    ]

    assert gate.main(args) == 1
    report = json.loads(capsys.readouterr().out)
    for runtime in report["runtimes"]:
        assert runtime["tiers"][0]["status"] == "PASS"
        timing = runtime["tiers"][1]
        assert timing["status"] == "FAIL"
        assert timing["counts"]["passed"] == 4
        assert timing["counts"]["failed"] == 1
        assert timing["returncodes"] == [0, 1, 0, 0, 0]
    for mode in ("source", "cython"):
        paths = list((raw_directory / mode).glob("*.log"))
        assert len(paths) == 8  # Both collection entrypoints, unit, five timing iterations.
        for path in paths:
            stem = {"collection-python-module": "0", "collection-pytest-console": "1"}.get(
                path.stem, path.stem
            )
            assert path.read_text(encoding="utf-8") == raw_pytest_output[(mode, stem)]
            if os.name == "posix":
                assert path.stat().st_mode & 0o777 == 0o600
        if os.name == "posix":
            assert (raw_directory / mode).stat().st_mode & 0o777 == 0o700
    if os.name == "posix":
        assert raw_directory.stat().st_mode & 0o777 == 0o700
    for filename in ("gate-report.json", "gate-report.txt"):
        text = (evidence_directory / filename).read_text(encoding="utf-8")
        assert "private-capture-secret" not in text
        assert str(raw_directory) not in text
    gate.verify_evidence_bundle(evidence_directory)


def test_raw_output_collision_preserves_old_log_and_fails_passing_tier(
    tmp_path, raw_pytest_output
):
    raw_directory = tmp_path / "private-output"
    raw_directory.mkdir()
    retained = raw_directory / "unit-1.log"
    retained.write_text("earlier evidence", encoding="utf-8")

    result = gate.run_tier(
        name="unit", project_root=tmp_path, python_executable=Path("python"),
        environment={}, required_problems=[], repeat=1, timeout_override=1,
        report_directory=tmp_path, raw_output_directory=raw_directory,
    )

    assert result.status == "FAIL"
    assert result.counts == gate.Counts(total=1, passed=1)
    assert result.returncodes == [0]
    assert any(
        "could not retain raw output unit-1.log: FileExistsError" in failure
        for failure in result.iteration_failures
    )
    assert retained.read_text(encoding="utf-8") == "earlier evidence"


@pytest.mark.parametrize("collision", (False, True))
def test_matrix_raw_output_keeps_complete_passed_row_or_reports_collision(
    tmp_path, monkeypatch, collision
):
    output = "BEGIN token=private-capture-secret\n" + "full trace\n" * 700 + "END\n"

    class VerboseMatrix(_FakeMatrixPopen):
        mode = "pass"

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.stdout = io.StringIO(output)

    monkeypatch.setattr(gate.subprocess, "Popen", VerboseMatrix)
    nodeid = "tests/test_matrix.py::test_pair[red-blue]"
    raw_directory = tmp_path / "private-output"
    filename = "matrix-" + hashlib.sha256(nodeid.encode()).hexdigest()[:20] + ".log"
    retained = raw_directory / filename
    if collision:
        raw_directory.mkdir()
        retained.write_text("earlier evidence", encoding="utf-8")
    result = gate.run_tier(
        name="trade", project_root=tmp_path, python_executable=Path("python"),
        environment={}, required_problems=[], repeat=1, timeout_override=1,
        report_directory=tmp_path, required_nodeids=(nodeid,),
        raw_output_directory=raw_directory,
    )

    assert result.status == ("FAIL" if collision else "PASS")
    assert result.counts == gate.Counts(total=1, passed=1)
    assert result.case_results[0].returncode == 0
    assert len(result.case_results[0].output_tail) <= 4000
    assert retained.read_text(encoding="utf-8") == ("earlier evidence" if collision else output)
    if collision:
        assert "could not retain raw output" in result.case_results[0].reason


def test_collection_timeout_retains_complete_captured_output(tmp_path, monkeypatch):
    output = "BEGIN\n" + "partial collection\n" * 700 + "END\n"

    class TimedOutCollection:
        def __init__(self, *args, **kwargs):
            pass

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired("pytest", timeout)

    monkeypatch.setattr(gate.subprocess, "Popen", TimedOutCollection)
    monkeypatch.setattr(gate, "_terminate_process", lambda process: None)
    monkeypatch.setattr(gate, "_communicate_after_termination", lambda process: output)
    result = gate._run_collection_command(
        name="python-module", command=["python", "-m", "pytest"],
        project_root=tmp_path, environment={}, timeout_seconds=1,
        report_path=tmp_path / "collection.json",
        raw_output_directory=tmp_path / "private-output",
    )

    assert result.status == "FAIL"
    assert result.returncode == 124
    assert result.reason == "collection timeout"
    retained = tmp_path / "private-output" / "collection-python-module.log"
    assert retained.read_text(encoding="utf-8") == output


@pytest.mark.parametrize("location", ("equal", "nested", "existing"))
def test_main_rejects_unsafe_raw_output_directory(tmp_path, capsys, location):
    evidence = tmp_path / "evidence"
    raw_directory = evidence / "raw" if location == "nested" else evidence
    if location == "existing":
        raw_directory = tmp_path / "earlier-output"
        raw_directory.mkdir()
        (raw_directory / "keep").write_text("earlier evidence", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        gate.main(["--evidence-dir", str(evidence), "--raw-output-dir", str(raw_directory)])

    assert error.value.code == 2
    assert "--raw-output-dir" in capsys.readouterr().err
    if location == "existing":
        assert (raw_directory / "keep").read_text(encoding="utf-8") == "earlier evidence"
    else:
        assert not evidence.exists()
