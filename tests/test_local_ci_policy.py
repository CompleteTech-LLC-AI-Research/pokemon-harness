"""Static policy checks for the reproducible local CI runner.

These tests intentionally inspect the runner and workflow text only.  Running
the runner itself would install dependencies, build a wheel, and execute the
bounded production gate, which is outside the unit-test tier.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

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


def _ruff_excluded_prefixes() -> tuple[str, ...]:
    """Read `extend-exclude` from the Ruff config so coverage mirrors the gate."""

    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    raw = config.get("tool", {}).get("ruff", {}).get("extend-exclude", [])
    return tuple(raw if isinstance(raw, list) else [raw])


def _is_excluded(relative_path: str, excluded: tuple[str, ...]) -> bool:
    """Return True when Ruff's `extend-exclude` would drop this path.

    `extend-exclude` entries are glob patterns, not just literal prefixes, so
    match them the way Ruff does.  A prefix-only test would pass for
    `extend-exclude = ["tests/legacy"]` while silently ignoring
    `["tests/test_*.py"]`, which drops the great majority of the test suite:
    measured on this tree, that glob removes 209 of 267 test files from the
    lanes.  The coverage assertion below is the last line of defence against a
    silent exclusion, so it must use the same resolution Ruff does.
    """

    path = PurePosixPath(relative_path)
    for entry in excluded:
        pattern = PurePosixPath(entry)
        if path.match(pattern) or path.match(f"{pattern}/**"):
            return True
        # Ruff also treats a bare directory entry as covering its contents.
        if relative_path == entry or relative_path.startswith(f"{entry}/"):
            return True
    return False


def _main_lane(invocations: list[tuple[str, ...]], subcommand: str) -> tuple[str, ...]:
    """Return the single main `tests/`-wide lane for `subcommand`.

    There are three Ruff lanes: two main ones that cover all of `tests/` plus
    an explicit `scripts/` list, and one narrower runtime/link lane that keeps
    a deliberately small explicit boundary. Only the main lanes glob `tests/`,
    so this selects the lane by that marker rather than by position, which keeps
    the test honest if a lane is ever reordered.
    """

    # `ruff check <paths>` puts paths straight after the subcommand, while
    # `ruff format --check <paths>` carries an extra flag, so match the mode
    # token itself and the path list that follows it.
    mode = ("check",) if subcommand == "check" else ("format", "--check")
    candidates = [
        invocation
        for invocation in invocations
        if invocation[:3] == ("python", "-m", "ruff")
        and invocation[3 : 3 + len(mode)] == mode
        and "tests" in invocation
    ]
    assert len(candidates) == 1, (
        f"expected exactly one `ruff {subcommand}` lane covering all of tests/, "
        f"found {len(candidates)}"
    )
    return candidates[0]


def test_main_ruff_lanes_cover_every_test_file() -> None:
    """Both main lanes must take the whole `tests/` directory, not a subset.

    Issue #259 was filed because the lanes enumerated 56 of 264 test files, so
    a newly added test could ship lint or format violations that no gate would
    catch. Widening the lanes to the directory itself is only meaningful if the
    directory token is actually present, so assert the token rather than trust
    the comment that describes the boundary.
    """

    invocations = _ruff_invocations(RUNNER.read_text(encoding="utf-8"))
    workflow_invocations = _ruff_invocations(WORKFLOW.read_text(encoding="utf-8"))

    for subcommand in ("check", "format"):
        for source, lanes in (("runner", invocations), ("workflow", workflow_invocations)):
            lane = _main_lane(lanes, subcommand)
            assert "tests" in lane, (
                f"{source} `ruff {subcommand}` lane must pass the `tests` directory "
                f"so a new test file cannot escape the gate; got: {lane}"
            )
            # A leftover enumerated `tests/...` entry alongside the directory
            # token would let a future edit shrink the effective set, so the
            # glob must be the only `tests` reference in the lane.
            assert not [token for token in lane if token.startswith("tests/")], (
                f"{source} `ruff {subcommand}` lane still enumerates individual test files"
            )


def test_main_ruff_lane_tests_directory_covers_every_test_file_on_disk() -> None:
    """The `tests` directory token must actually match every test file.

    Asserting the token is present is necessary but not sufficient: a future
    edit could add an `extend-exclude` entry that silently drops test files
    from the directory glob. Resolve the token the way Ruff does and confirm no
    test file is excluded, so the coverage claim is measured rather than
    assumed. This is the assertion that fails if someone later excludes, say,
    `tests/legacy` without noticing the gate stopped checking it.
    """

    tests_root = ROOT / "tests"
    on_disk = {
        path.relative_to(ROOT).as_posix() for path in tests_root.rglob("*.py") if path.is_file()
    }
    assert on_disk, "no test files found on disk"

    excluded = _ruff_excluded_prefixes()
    # Recurse the `tests` directory the way Ruff does, then drop anything
    # `extend-exclude` would skip. The result must still be the whole tree.
    covered = {path for path in on_disk if not _is_excluded(path, excluded)}
    uncovered = on_disk - covered
    assert not uncovered, (
        "tests/ files are excluded from the Ruff lanes by extend-exclude "
        f"({sorted(excluded)!r}): {sorted(uncovered)[:5]}"
    )
    # Guard against the directory token being satisfied by a stray file named
    # `tests` rather than the directory.
    assert tests_root.is_dir(), "the `tests` directory the lanes pass must exist"

    invocations = _ruff_invocations(RUNNER.read_text(encoding="utf-8"))
    for subcommand in ("check", "format"):
        assert _main_lane(invocations, subcommand).count("tests") == 1, (
            f"`ruff {subcommand}` lane must name the tests directory exactly once"
        )


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
    # Since #259 the two main lanes take the `tests` directory itself, so this
    # floor names that token plus the narrower runtime/link lane's explicit
    # `tests/` entries, which stay enumerated. Individual main-lane test files
    # are intentionally absent: their coverage is now enforced by
    # `test_main_ruff_lanes_cover_every_test_file` above, which fails if the
    # directory token is dropped or reintroduced as a partial list.
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
        "tests",
        "tests/test_fixture_provenance.py",
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
