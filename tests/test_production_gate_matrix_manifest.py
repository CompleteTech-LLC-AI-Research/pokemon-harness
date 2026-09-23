"""Tier classification, matrix manifest and collection auditing (#129).

Split from ``tests/test_production_gate.py`` for #129 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module.
"""

from __future__ import annotations

import ast
import hashlib
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
from tests._production_gate_support import (
    _CANONICAL_BOOT_NODEIDS,
    gate,
)
from tests._tier_config import (
    TIER_REQUIRED_NODEIDS,
    classify_test,
)


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
        "tests/test_network_backend_rearm.py",
        "test_on_edge_waits_for_late_rearm",
    )
    assert "unit" in marks
    assert "timing_sensitive" in marks


_SUBPROCESS_MODULE = "tests/test_pyboy_link_session_subprocess.py"
_SUBPROCESS_LINK_MENU_MODULE = "tests/test_pyboy_link_session_subprocess_link_menu_history.py"
_SUBPROCESS_PEER_LIFECYCLE_MODULE = "tests/test_pyboy_link_session_subprocess_peer_lifecycle.py"


# The ROM-free cases were split out of the real-ROM module for #131.  They now
# live in dedicated unit modules and are pinned per module below, so a test that
# drifts into the wrong file is caught rather than hidden by a shared inventory.
_REVIEWED_SUBPROCESS_UNIT_TESTS = {
    _SUBPROCESS_LINK_MENU_MODULE: (
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
    ),
    _SUBPROCESS_PEER_LIFECYCLE_MODULE: (
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
    ),
}

_SUBPROCESS_UNIT_TEST_MODULES = {
    test_name: module
    for module, names in _REVIEWED_SUBPROCESS_UNIT_TESTS.items()
    for test_name in names
}


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


@pytest.mark.parametrize("test_name", _SUBPROCESS_UNIT_TEST_MODULES)
def test_tier_classifier_subprocess_reviewed_fakes_are_exactly_unit(test_name):
    assert classify_test(_SUBPROCESS_UNIT_TEST_MODULES[test_name], test_name) == {"unit"}


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
    base = Path(__file__).resolve().parent

    def source_tests(module: str) -> list[str]:
        source = base / Path(module).name
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        return [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        ]

    unit_names = [name for names in _REVIEWED_SUBPROCESS_UNIT_TESTS.values() for name in names]
    reviewed = set(unit_names) | set(_REVIEWED_SUBPROCESS_REAL_TESTS)
    assert len(unit_names) == len(set(unit_names))
    assert set(unit_names).isdisjoint(_REVIEWED_SUBPROCESS_REAL_TESTS)
    # Each reviewed unit name is pinned to exactly one split module, and that
    # module must define exactly the reviewed names and nothing else.
    for module, reviewed_names in _REVIEWED_SUBPROCESS_UNIT_TESTS.items():
        defined = source_tests(module)
        assert len(defined) == len(set(defined)), (
            f"duplicate test definitions in {module} hide reviewed coverage"
        )
        assert set(defined) == set(reviewed_names), (
            f"{module} inventory changed; review tier membership"
        )
    assert set(source_tests(_SUBPROCESS_MODULE)) == set(_REVIEWED_SUBPROCESS_REAL_TESTS), (
        "subprocess real-ROM inventory changed; review tier membership"
    )
    total_defined = len(source_tests(_SUBPROCESS_MODULE)) + sum(
        len(source_tests(module)) for module in _REVIEWED_SUBPROCESS_UNIT_TESTS
    )
    assert total_defined == len(reviewed)


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
    assert result["entries"] == 13


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
    # 19 pre-existing strict trade rows plus the 24 real-ROM MCP stdio rows
    # declared by tests/test_mcp_trade_records_rom.py (nine local orientations,
    # nine TCP orientations, two multi-member slot rows, the cancel row, and
    # the three EOF/disconnect rows).
    assert len(TIER_REQUIRED_NODEIDS["trade"]) == 43
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


@pytest.mark.parametrize(
    ("operation", "marker"),
    (("trade", "trade_acceptance"), ("battle", "battle_acceptance")),
)
def test_strict_matrix_rows_are_selected_by_their_tier_expression(operation, marker):
    """Every declared strict row must survive the tier marker expression.

    ``run_matrix_tier`` runs each required node ID as an explicit selector
    *together with* the tier marker expression, so a declared row whose base
    test is missing from the acceptance marker set is deselected and the row
    fails closed.  Classify every declared node ID's original test name here so
    a dropped ``TRADE_ACCEPTANCE_TESTS``/``BATTLE_ACCEPTANCE_TESTS`` entry
    cannot hide behind the aggregate node-ID manifest.
    """

    assert gate.TIER_EXPRESSIONS[operation] == f"real_rom and {marker}"
    declared = sorted(TIER_REQUIRED_NODEIDS[operation])
    assert declared, operation
    for nodeid in declared:
        path, separator, test_name = nodeid.partition("::")
        assert separator, nodeid
        marks = classify_test(path, test_name.split("[", 1)[0])
        assert "real_rom" in marks, (nodeid, marks)
        assert marker in marks, (nodeid, marks)


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
