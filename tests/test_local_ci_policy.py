"""Static policy checks for the reproducible local CI runner.

These tests intentionally inspect the runner and workflow text only.  Running
the runner itself would install dependencies, build a wheel, and execute the
bounded production gate, which is outside the unit-test tier.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_local_ci.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "release-hygiene.yml"


def test_hosted_job_requires_explicit_public_visibility() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert (
        "if: ${{ github.event.repository.private == false && "
        "github.event.repository.visibility == 'public' }}"
    ) in workflow
    runners = [line.strip() for line in workflow.splitlines() if "runs-on:" in line]
    assert runners == ["runs-on: ubuntu-latest"]
    assert "workflow_dispatch:" in workflow
    assert "pokered-unit-gate-evidence-${{ matrix.python-version }}" in workflow


def test_local_runner_is_a_bash_script() -> None:
    source = RUNNER.read_text(encoding="utf-8")
    assert source.startswith("#!/usr/bin/env bash\n")
    assert "set -euo pipefail" in source
    assert os.access(RUNNER, os.X_OK)


def test_local_runner_copies_every_workflow_check_command() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    runner = RUNNER.read_text(encoding="utf-8")

    # Keep this list in lockstep with the workflow's run blocks.  It is a
    # command-level contract rather than a YAML parser so the unit tier has no
    # PyYAML dependency and cannot accidentally run the expensive checks.
    commands = (
        'python -m pip install -e ".[dev]"',
        "python -m ruff check",
        "python -m ruff format --check",
        "python scripts/validate_fixture_manifest.py --schema-only",
        "python scripts/validate_battle_scenarios.py --schema-only",
        "python scripts/tcp_link_matrix.py --format text",
        "python scripts/network_concurrency_probe.py",
        "python scripts/production_gate.py --runtime-mode source --unit-only --repeat-timing 5",
        '--evidence-dir "$RUNNER_TEMP/pokered-unit-evidence"',
        'python -m pip wheel --no-deps --wheel-dir "$RUNNER_TEMP/pokered-wheels" .',
        "python - \"$RUNNER_TEMP/pokered-wheels\" <<'PY'",
        'clean_venv="$RUNNER_TEMP/pokered-clean-venv"',
        'python -m venv "$clean_venv"',
        '"$clean_python" -m pip install --no-cache-dir "$RUNNER_TEMP"/pokered-wheels/*.whl',
        '"$clean_python" -m pip check',
        '"$clean_python" scripts/bootstrap_pyboy.py --mode source --check',
    )
    for command in commands:
        assert command in workflow, f"workflow lost expected command: {command}"
        assert command in runner, f"local runner lost expected command: {command}"
    assert '["git", "ls-files", "-z"]' in workflow
    assert '["git", "ls-files", "-z"]' in runner

    # Assert the complete explicit Ruff file boundaries, not only the command
    # prefixes, so a local run cannot silently lint a smaller set.
    workflow_paths = (
        "scripts/bootstrap_pyboy.py",
        "scripts/coverage_report.py",
        "scripts/gate_capacity.py",
        "scripts/gate_capacity_admission.py",
        "scripts/gate_capacity_policy.py",
        "scripts/gate_capacity_report.py",
        "scripts/network_concurrency_probe.py",
        "scripts/produce_battle_scenario.py",
        "scripts/production_gate.py",
        "scripts/production_gate_capacity.py",
        "scripts/production_gate_matrix_audit.py",
        "scripts/production_gate_runtime_gates.py",
        "scripts/qualification_runner.py",
        "scripts/qualification_runner_allocation.py",
        "scripts/qualification_runner_assets.py",
        "scripts/qualification_runner_cgroup.py",
        "scripts/qualification_runner_cli.py",
        "scripts/qualification_runner_command.py",
        "scripts/qualification_runner_declaration.py",
        "scripts/qualification_runner_facts.py",
        "scripts/qualification_runner_host.py",
        "scripts/qualification_runner_model.py",
        "scripts/qualification_runner_report.py",
        "scripts/qualification_runner_reservation.py",
        "scripts/tcp_link_matrix.py",
        "scripts/validate_battle_scenarios.py",
        "scripts/validate_fixture_manifest.py",
        "tests/_battle_item_evidence.py",
        "tests/_battle_item_evidence_factories.py",
        "tests/_gate_capacity_support.py",
        "tests/_gate_report.py",
        "tests/_qualification_runner_support.py",
        "tests/_rom_assets.py",
        "tests/_tier_config.py",
        "tests/conftest.py",
        "tests/test_battle_coverage_accounting.py",
        "tests/test_battle_coverage_catalog.py",
        "tests/test_battle_coverage_gate_assets.py",
        "tests/test_battle_coverage_identity.py",
        "tests/test_battle_coverage_mechanics.py",
        "tests/test_battle_item_evidence_inventory.py",
        "tests/test_battle_item_evidence_medicine.py",
        "tests/test_battle_item_evidence_targets.py",
        "tests/test_battle_item_evidence_timeline.py",
        "tests/test_battle_scenario_catalog.py",
        "tests/test_battle_scenario_producer_capture.py",
        "tests/test_battle_scenario_producer_run.py",
        "tests/test_battle_scenario_producer_runtime.py",
        "tests/test_battle_scenario_producer_screening.py",
        "tests/test_battle_scenario_validator.py",
        "tests/test_fixture_provenance.py",
        "tests/test_gate_capacity_boundaries.py",
        "tests/test_gate_capacity_interrupts.py",
        "tests/test_gate_capacity_main.py",
        "tests/test_gate_capacity_policy.py",
        "tests/test_party_record_audit.py",
        "tests/test_production_gate_diagnostics.py",
        "tests/test_production_gate_matrix_manifest.py",
        "tests/test_production_gate_report_loader.py",
        "tests/test_production_gate_run_tier_failures.py",
        "tests/test_production_gate_strict_matrix.py",
        "tests/test_qualification_runner.py",
        "tests/test_qualification_runner_allocation.py",
        "tests/test_qualification_runner_assets.py",
        "tests/test_qualification_runner_command.py",
        "tests/test_qualification_runner_containment.py",
        "tests/test_qualification_runner_lockstate.py",
        "tests/test_qualification_runner_native.py",
        "tests/test_qualification_runner_release.py",
        "tests/test_runtime_packaging_bootstrap.py",
        "tests/test_runtime_packaging_build_contract.py",
        "tests/test_runtime_packaging_dependency_pins.py",
        "tests/test_runtime_packaging_hygiene.py",
        "src/pokered_harness/link/network_backend.py",
        "src/pokered_harness/link/pyboy_link_session.py",
        "src/pokered_harness/link/pair.py",
        "src/pokered_harness/link/serial_bridge.py",
        "src/pokered_harness/link/serial_coordinator.py",
        "src/pokered_harness/link/serial_link.py",
        "tests/test_network_backend_dispatch.py",
        "tests/test_network_backend_rearm.py",
        "tests/test_network_backend_serial_transcript.py",
        "tests/test_network_backend_transport.py",
        "tests/test_network_backend_wire_idle.py",
        "tests/test_pyboy_link_session.py",
        "tests/test_link_pair.py",
        "tests/test_link_serial_bridge.py",
        "tests/test_serial_coordinator.py",
        "tests/test_serial_link.py",
    )
    for path in workflow_paths:
        assert path in workflow
        assert path in runner

    # The embedded Python verifiers are also part of the workflow contract;
    # command-prefix checks alone would permit a local runner to omit them.
    verifier_fragments = (
        "tracked artifact policy verified:",
        "windows_absolute = re.compile",
        'forbidden_parts = {"rom", "roms", "fixtures", "release-evidence", "artifacts"}',
        'assert pyboy.__version__ == "2.7.0"',
        "assert pyboy.__pokered_harness_revision__ == (",
        "assert utils.cython_compiled is False",
        '"backend", "apply_external_edge", "peek_out_bit"',
        '"POKERED_SKIP_SHA1",',
        '"set POKERED_ROM_PATH and POKERED_SYM_PATH"',
    )
    for fragment in verifier_fragments:
        assert fragment in workflow
        assert fragment in runner
    assert '"pokered-harness.exe" if os.name == "nt"' in runner


def _ruff_lane_paths(source: str) -> list[tuple[str, tuple[str, ...]]]:
    """Return each ``ruff`` invocation as ``(mode, explicit file paths)``.

    The workflow and the local runner must lint the *same* explicit file set;
    a path present in only one of them silently shrinks one lane.  Parsing the
    backslash-continued commands keeps the unit tier free of a YAML dependency.
    """

    lanes: list[tuple[str, tuple[str, ...]]] = []
    for match in re.finditer(r"python -m ruff (check|format --check)", source):
        lines = source[match.start() :].splitlines()
        block = [lines[0]]
        for line in lines[1:]:
            block.append(line)
            if not line.rstrip().endswith("\\"):
                break
        paths = tuple(
            token
            for token in (line.strip().rstrip("\\").strip() for line in block[1:])
            if token.endswith((".py", ".sh", ".yml"))
        )
        lanes.append((match.group(1), paths))
    return lanes


def test_local_runner_lints_exactly_the_workflow_file_set() -> None:
    workflow_lanes = _ruff_lane_paths(WORKFLOW.read_text(encoding="utf-8"))
    runner_lanes = _ruff_lane_paths(RUNNER.read_text(encoding="utf-8"))

    assert [mode for mode, _ in workflow_lanes] == [mode for mode, _ in runner_lanes]
    assert workflow_lanes, "no ruff lanes were discovered"
    for (mode, workflow_paths), (_, runner_paths) in zip(workflow_lanes, runner_lanes):
        assert set(workflow_paths) == set(runner_paths), (
            f"ruff {mode} lanes disagree: "
            f"only in workflow: {sorted(set(workflow_paths) - set(runner_paths))}; "
            f"only in local runner: {sorted(set(runner_paths) - set(workflow_paths))}"
        )
        assert len(workflow_paths) == len(runner_paths), f"ruff {mode} lane lists a duplicated path"


def test_local_runner_retains_external_evidence() -> None:
    runner = RUNNER.read_text(encoding="utf-8")

    assert 'RUNNER_TEMP="$(mktemp -d ' in runner
    assert "export RUNNER_TEMP" in runner
    assert "trap report_retained EXIT" in runner
    assert '"$repo_root"/*' in runner
    assert "Local CI temporary evidence retained at:" in runner
    assert "--dry-run" in runner
    assert "Dry run requested; checks were not executed." in runner
    assert "rm -" not in runner
    assert "rmdir" not in runner
    assert "shutil.rmtree" not in runner


def test_local_runner_requires_supported_active_virtualenv() -> None:
    runner = RUNNER.read_text(encoding="utf-8")

    assert "VIRTUAL_ENV:-" in runner
    assert "sys.prefix == sys.base_prefix" in runner
    assert "3.11|3.12" in runner
    assert "command -v python" in runner


def test_local_runner_has_no_hosted_or_paid_service_dependency() -> None:
    runner = RUNNER.read_text(encoding="utf-8").lower()

    assert "uses:" not in runner
    assert "actions/" not in runner
    assert "docker" not in runner
    assert "pyyaml" not in runner
    assert "upload-artifact" not in runner
    assert "download-artifact" not in runner
