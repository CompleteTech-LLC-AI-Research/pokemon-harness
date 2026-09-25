"""Static policy checks for the reproducible local CI runner.

These tests intentionally inspect the runner and workflow text only.  Running
the runner itself would install dependencies, build a wheel, and execute the
bounded production gate, which is outside the unit-test tier.
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_local_ci.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "release-hygiene.yml"


def _ruff_invocations(text: str) -> list[tuple[str, ...]]:
    """Return the full argument vector of each `python -m ruff` call, in order.

    The runner and the workflow are different languages (bash vs YAML), so the
    comparison is made over the *arguments* rather than the surrounding text.
    Comment lines are ignored; a `#` comment inside a shell list is not a path.

    The whole argument vector is compared, not only the ``.py`` tokens: an
    option such as ``--exclude`` combined with ``--force-exclude`` can drop a
    declared file from the effective lint set, and an option such as
    ``--line-length`` changes the verdict, so a flags-only edit to one lane is
    exactly the kind of divergence this contract must catch.
    """

    invocations: list[tuple[str, ...]] = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped.startswith("python -m ruff "):
            index += 1
            continue
        arguments = [token for token in stripped.rstrip("\\").split() if token]
        cursor = index + 1
        while cursor < len(lines):
            raw = lines[cursor]
            segment = raw.strip()
            continues = raw.rstrip().endswith("\\")
            if not segment.startswith("#"):
                for token in segment.rstrip("\\").split():
                    if token:
                        arguments.append(token)
            if not continues:
                break
            cursor += 1
        invocations.append(tuple(arguments))
        index = cursor + 1
    return invocations


def test_runner_ruff_file_lists_match_the_workflow_exactly() -> None:
    """Both lanes must run the same Ruff commands, file lists and flags alike.

    The workflow and the local runner intentionally duplicate their Ruff
    boundaries so a local run cannot lint a smaller (or stale) set.  A split
    that updates only one of them silently weakens the hosted lane, so the two
    invocations are asserted equal here rather than trusted to stay in sync.
    """

    workflow = _ruff_invocations(WORKFLOW.read_text(encoding="utf-8"))
    runner = _ruff_invocations(RUNNER.read_text(encoding="utf-8"))

    assert workflow, "workflow declares no Ruff invocations"
    assert runner, "local runner declares no Ruff invocations"
    assert len(workflow) == len(runner), (
        "workflow and local runner declare a different number of Ruff invocations"
    )
    for workflow_arguments, runner_arguments in zip(workflow, runner, strict=True):
        assert workflow_arguments == runner_arguments, (
            "Ruff invocation diverges: "
            f"workflow-only={sorted(set(workflow_arguments) - set(runner_arguments))} "
            f"runner-only={sorted(set(runner_arguments) - set(workflow_arguments))}"
        )


def test_ruff_invocation_parser_keeps_options_that_change_the_lint_set() -> None:
    """The lockstep comparison must cover Ruff options, not only `.py` tokens."""

    workflow_style = (
        "          python -m ruff check \\\n            src/a.py \\\n            src/b.py\n"
    )
    runner_style = "python -m ruff check \\\n    src/a.py \\\n    src/b.py\n"
    flags_only_drift = (
        "python -m ruff check --exclude=src/a.py --force-exclude \\\n"
        "    src/a.py \\\n"
        "    src/b.py\n"
    )

    # The same invocation written in the two languages still compares equal.
    assert _ruff_invocations(workflow_style) == _ruff_invocations(runner_style)

    # A change to the flags alone is a divergence even though the `.py` tokens
    # are untouched, because it can shrink or reshape the effective lint set.
    assert _ruff_invocations(flags_only_drift) != _ruff_invocations(runner_style)


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

    # Spot-check a declared core of the Ruff file boundaries, not only the
    # command prefixes, so a local run cannot silently lint a smaller set.
    #
    # This is a *subset floor*, not the full contract: every path it names is
    # in a lane, but the lanes carry more entries than it lists, and they did
    # before #242 too (38 lane paths were absent from it at 061fa15c; the two
    # #242 paths added here leave 40 absent at bfc2920). A path that matters is
    # still expected to be listed, so the two new #242 paths were added rather
    # than left out. The complete boundary is asserted by
    # `test_runner_ruff_file_lists_match_the_workflow_exactly` above, which
    # compares the workflow's and the runner's full argument vectors for every
    # `python -m ruff` invocation. The converse is *not* enforced: no assertion
    # here fails when a lane carries a path this tuple omits, which is why the
    # lane-placement row below exists to pin the one property the tuple cannot
    # see. Read this tuple as a selected membership floor, not as a claim that
    # it enumerates the boundary; a path missing here is not by itself evidence
    # that the boundary lost it.
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
        "src/pokered_harness/_mcp_facade_entry.py",
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
        "tests/test_mcp_server_import_order.py",
        "tests/test_serial_coordinator.py",
        "tests/test_serial_link.py",
    )
    for path in workflow_paths:
        assert path in workflow
        assert path in runner

    # The tuple above is membership-only, so it cannot see *which lane* holds a
    # path. That gap is what let `src/pokered_harness/_mcp_facade_entry.py` be
    # `ruff check`ed while no `ruff format --check` lane named it. Assert the
    # lane itself: a path that is linted but never format-checked can drift out
    # of format silently, which is exactly what this row pins.
    facade = "src/pokered_harness/_mcp_facade_entry.py"
    for label, text in (("workflow", workflow), ("local runner", runner)):
        format_lanes = [
            arguments
            for arguments in _ruff_invocations(text)
            if "--check" in arguments and "format" in arguments
        ]
        assert format_lanes, f"{label} declares no `ruff format --check` lane"
        covered = {token for lane in format_lanes for token in lane if token.endswith(".py")}
        assert facade in covered, f"{label} no longer format-checks {facade}"

        # Keep the documented reason honest: the rest of the runtime/link lane's
        # `src/` files are deliberately outside the format boundary because the
        # lane is not format-clean. If that ever changes, this row should be
        # revisited rather than silently kept.
        runtime_lane = [
            arguments
            for arguments in _ruff_invocations(text)
            if "check" in arguments and "format" not in arguments
        ][-1]
        runtime_src = {token for token in runtime_lane if token.startswith("src/")}
        assert facade in runtime_src, f"{label} no longer checks {facade}"
        assert runtime_src - covered, (
            f"{label} now format-checks the whole runtime/link `src/` set; "
            "the boundary comment in this file and in both CI files is stale"
        )

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
