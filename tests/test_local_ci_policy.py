"""Static policy checks for the reproducible local CI runner.

These tests intentionally inspect the runner and workflow text only.  Running
the runner itself would install dependencies, build a wheel, and execute the
bounded production gate, which is outside the unit-test tier.
"""

from __future__ import annotations

import os
import subprocess
import sys
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


def _ruff_lint_resolved_files() -> set[str]:
    """Return the repo-relative files Ruff itself resolves for the main lane.

    Re-implementing `extend-exclude` in the test is what made this row lie:
    the hand-rolled matcher crashed on any tree that declared an
    `extend-exclude` entry, because `PurePath.match()` takes a *string* pattern
    and the matcher passed a `PurePosixPath`, so the coverage assertion died
    with `TypeError` instead of reporting coverage. It also had no way to stay
    faithful to Ruff's glob dialect, which `pathlib` does not implement the
    same way.

    Ask Ruff instead. `ruff check <path> --show-files` prints the exact file set
    it would lint after honouring `extend-exclude`, its default excludes and its
    directory recursion, so this measures the gate rather than a model of it.
    """

    completed = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "tests", "--show-files", "--no-cache"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    resolved = set()
    for line in completed.stdout.splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        relative = Path(candidate).resolve().relative_to(ROOT).as_posix()
        # `--show-files` may also report files Ruff pulled in from a config
        # `src` entry; only the `tests` tree is this row's contract.
        if relative.startswith("tests/"):
            resolved.add(relative)
    return resolved


def _ruff_excluded_patterns() -> tuple[str, ...]:
    """Return `extend-exclude` for the failure message only.

    Exclusion itself is decided by Ruff in ``_ruff_lint_resolved_files``; this
    is read purely so a failure names the pattern that caused it.
    """

    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    raw = config.get("tool", {}).get("ruff", {}).get("extend-exclude", [])
    return tuple(raw if isinstance(raw, list) else [raw])


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

    # Resolve the `tests` directory token through Ruff itself, then require the
    # resolved set to still be the whole tree. An `extend-exclude` entry that
    # drops a test file now fails here with the offending file, instead of
    # crashing the row or silently shrinking the gate.
    covered = _ruff_lint_resolved_files()
    uncovered = on_disk - covered
    assert not uncovered, (
        "tests/ files are excluded from the Ruff lanes: "
        f"{sorted(uncovered)[:5]} (extend-exclude={_ruff_excluded_patterns()!r})"
    )
    # The directory token must also not *gain* files Ruff would never lint,
    # which would mean the measured set and the real gate disagree.
    unexpected = covered - on_disk
    assert not unexpected, f"Ruff resolved unexpected tests/ files: {sorted(unexpected)[:5]}"
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
        "scripts/benchmark_matrix_concurrency.py",
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


_BENCHMARK = "scripts/benchmark_matrix_concurrency.py"

# Ruff options that take a separate operand token. A lane may spell an exclude
# either `--exclude=path` or `--exclude path`, and both forms are honoured by
# Ruff, so a probe must carry the operand through rather than dropping it as if
# it were a path. `--config` matters here for the same reason: a lane-level
# `--config` can point Ruff at a settings file that disables the file.
_RUFF_OPTIONS_WITH_OPERAND = frozenset(
    {
        "--exclude",
        "--extend-exclude",
        "--config",
        "--line-length",
        "--target-version",
        "--output-format",
        "--range",
    }
)

# Options that make a lane *appear* to lint while covering less than it
# declares. These are rejected outright rather than probed, because each one
# makes a narrowed gate indistinguishable from a whole-file gate:
#
#   * `--exit-zero` keeps printing diagnostics but forces exit 0, so a real
#     violation no longer fails CI.
#   * `--range`/`--range=...` format-checks only a line span, so an
#     unformatted region outside the span is never checked. (Verified: with
#     `--range=1-1` an unformatted line 2 passes, and a full `--check` on the
#     same input fails.)
#   * `--diff`/`--diff-format`/`-` change what the formatter emits, so the
#     formatted-stdout comparison would not reflect the lane's own verdict.
#
# The `--range` family is matched by prefix because Ruff accepts `--range`,
# `--range=N`, and (in future) a range-like spelling; rejecting any option
# whose name starts with `--range` avoids re-opening this hole silently.
_WEAKENING_OPTIONS = ("--exit-zero", "--diff", "--diff-format")
_WEAKENING_OPTION_PREFIXES = ("--range",)


def _lane_options(lane: tuple[str, ...]) -> list[str]:
    """Return the lane's Ruff options, with its path list dropped.

    Every option is preserved -- an `--exclude` hiding the benchmark, an
    `--ignore=ALL` disabling it, a `--config` re-pointing Ruff, or a
    `--range` narrowing the format check all live in exactly this part of the
    lane. Both spellings of an option and its operand are carried through, so
    `--exclude <path>` is not mistaken for two path arguments.

    `lane[3]` is the subcommand and is not returned; the caller rebuilds the
    command around it. The path list is dropped because the probes feed Ruff a
    single file on stdin instead, so any path in the lane would be a second,
    unrelated input.
    """

    tokens = list(lane[4:])
    options: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _RUFF_OPTIONS_WITH_OPERAND:
            options.append(token)
            index += 1
            if index < len(tokens):
                options.append(tokens[index])
                index += 1
            continue
        if token.startswith("-"):
            # Both `--name=value` and bare `--name` are kept: an option that
            # silently disables rules (`--ignore=ALL`) hides in either form,
            # and dropping it would make the probe report a false pass.
            options.append(token)
            index += 1
            continue
        index += 1

    # `--check`/`--show-files` are supplied by the caller, and Ruff rejects a
    # repeated flag, so an argparse error here would mask the real result.
    return [option for option in options if option not in {"--check", "--show-files"}]


def _option_names(lane: tuple[str, ...]) -> set[str]:
    """Return the lane's option names, normalised to their `--name` form."""

    names: set[str] = set()
    for option in _lane_options(lane):
        name = option.split("=", 1)[0]
        names.add(name)
    return names


def _weakening_options(lane: tuple[str, ...]) -> list[str]:
    """Return the lane options that would narrow or void its own verdict."""

    offenders: list[str] = []
    for option in _lane_options(lane):
        name = option.split("=", 1)[0]
        if name in _WEAKENING_OPTIONS or name.startswith(_WEAKENING_OPTION_PREFIXES):
            offenders.append(option)
    return offenders


def _probe_check(options: list[str]) -> subprocess.CompletedProcess[str]:
    """Lint stdin code that has a real violation, through the lane's options.

    The probe deliberately feeds *bad* code rather than reading the benchmark:
    it answers "would this lane reject a violation in this file?", which is the
    question CI actually asks. A lane that silently drops the file, disables
    every rule, or forces exit 0 all answer "no" here, without any of them
    needing to be enumerated in the test.
    """

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            *options,
            "--stdin-filename",
            _BENCHMARK,
            "-",
        ],
        cwd=ROOT,
        input="def _probe():\n    return _pokered_undefined_probe_name\n",
        capture_output=True,
        text=True,
        check=False,
    )


def _probe_format(options: list[str]) -> subprocess.CompletedProcess[str]:
    """Format stdin code that is known-unformatted, through the lane's options.

    `--check` exits nonzero exactly when the input is not already formatted, so
    a nonzero exit here means "this lane would reject an unformatted file".
    """

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "format",
            "--check",
            *options,
            "--stdin-filename",
            _BENCHMARK,
            "-",
        ],
        cwd=ROOT,
        input="x  =  1\ndef  _probe( a ):\n    return   a\n",
        capture_output=True,
        text=True,
        check=False,
    )


def _main_lane_sources() -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return `(source, lane)` for both subcommands of both CI files."""

    pairs: list[tuple[str, tuple[str, ...]]] = []
    for source, text in (
        ("runner", RUNNER.read_text(encoding="utf-8")),
        ("workflow", WORKFLOW.read_text(encoding="utf-8")),
    ):
        lanes = _ruff_invocations(text)
        for subcommand in ("check", "format"):
            pairs.append((f"{source} `ruff {subcommand}`", _main_lane(lanes, subcommand)))
    return tuple(pairs)


def test_main_ruff_lanes_do_not_narrow_their_own_verdict() -> None:
    """No main lane may carry an option that shrinks or voids its gate.

    These options are rejected by name because they cannot be detected by
    probing: `--range` narrows a *real* format check to a line span, so the
    probe still passes while an unformatted region outside the span goes
    unchecked; and `--exit-zero` keeps the diagnostics but forces exit 0, so a
    genuine violation no longer fails CI. Both were found green in review.
    """

    for label, lane in _main_lane_sources():
        offenders = _weakening_options(lane)
        assert not offenders, (
            f"{label} lane carries option(s) {offenders} that narrow or void its "
            "own verdict; the benchmark (and every other lane path) would stop "
            "being enforced"
        )


def test_matrix_benchmark_is_linted_by_every_main_ruff_lane() -> None:
    """The #106 benchmark must be really linted, in both lanes of both files.

    `scripts/` is enumerated explicitly in these lanes rather than globbed, so
    a new script ships outside the lint boundary until someone lists it. When
    PR #564 landed, `scripts/benchmark_matrix_concurrency.py` -- 1409 lines of
    new code -- was added to neither the check lane nor the format lane, in
    neither the local runner nor the hosted workflow.

    Three separate things have to hold, and each has been refuted in review:

    1. The path is *listed*. The lockstep test cannot see this, because it
       only proves the runner and the workflow agree -- they omitted it
       together.
    2. Ruff actually *applies* to it. A lane carrying
       `--exclude=<benchmark> --force-exclude` names the path and then drops
       it; a `per-file-ignores` entry of `["ALL"]` leaves it resolved but
       silently unlinted; a lane-level `--ignore=ALL` or `--select=E501`
       disables everything that would catch a violation. Each leaves Ruff
       reporting "All checks passed" for *anything*, including an undefined
       name.

    Rather than model Ruff's rule resolution, each probe asks Ruff the question
    CI asks -- "would you reject a violation in this file, with these exact
    options?" -- by feeding a known-bad snippet through the lane's own option
    vector. An exclusion, a blanket ignore, a narrowed select, a per-file
    `ALL`, and `--exit-zero` all collapse to the same answer, and all of them
    fail here. A *selective* per-file ignore such as `["F401"]` is not
    over-rejected, because the probe still sees `F821`.

    The probes pass the lane's options verbatim. An earlier version appended
    `--force-exclude`, which changed the lane's behaviour: with an
    `extend-exclude` naming the benchmark, the real command still resolved it
    while the probe reported it as excluded. That was a false failure, so the
    probe must not inject options the lane does not carry.
    """

    for label, lane in _main_lane_sources():
        assert _BENCHMARK in lane, (
            f"{label} lane does not list {_BENCHMARK}; the benchmark would ship unlinted"
        )

        options = _lane_options(lane)
        if "check" in label:
            probe = _probe_check(options)
        else:
            probe = _probe_format(options)

        # Exit 2 is Ruff's argparse/usage failure, not a lint verdict. It must
        # be distinguished so a malformed lane is reported as such instead of
        # passing as "no violations".
        assert probe.returncode != 2, (
            f"{label} probe failed to parse: {probe.stderr.strip() or probe.stdout.strip()}"
        )
        assert probe.returncode != 0, (
            f"{label} would not reject a real violation in {_BENCHMARK}: "
            f"the file is excluded, every rule is disabled, or the verdict is void "
            f"(options={options}); output={probe.stdout.strip()!r}"
        )


# The stub records one JSON argument vector per line. It is assembled from
# `chr(10)` rather than an escaped `\n` so the literal below stays a plain,
# readable Python program with no nested escaping to get wrong.
_PYTHON_STUB_LINES = (
    "#!" + "{python}",
    "import json",
    "import os",
    "import sys",
    "",
    "arguments = sys.argv[1:]",
    'log = os.environ["POKERED_POLICY_STUB_LOG"]',
    'with open(log, "a", encoding="utf-8") as handle:',
    "    handle.write(json.dumps(arguments) + chr(10))",
    'if arguments[:1] == ["-c"] and "version_info" in " ".join(arguments):',
    "    # The runner refuses to continue unless it believes it is on 3.11/3.12.",
    '    print("3.11")',
    "sys.exit(0)",
)


def _python_stub_text() -> str:
    """Return the stub program, headed by a shebang that surely resolves."""

    return chr(10).join(_PYTHON_STUB_LINES).replace("{python}", sys.executable, 1)


def _record_runner_python_invocations() -> list[list[str]]:
    """Execute the local runner and return every `python` argument vector.

    The runner is run for real, but with a stub `python` first on `PATH`. The
    stub records its arguments and exits 0, so the runner walks its whole
    script -- every command, in order -- without installing dependencies,
    running the gate, building a wheel or touching the network. That trace is
    the authoritative answer to "which commands does CI actually execute?",
    which a text search cannot give: a lane inside `if false; then ... fi`
    still parses under `bash -n` and still contains the path.

    `TMPDIR` is redirected so the runner's `mktemp -d` lands in a scratch
    directory that is removed afterwards; `VIRTUAL_ENV` is pointed at this
    interpreter's own prefix so the runner's virtualenv precondition holds.
    """

    import json
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory(prefix="pokered-policy-stub.") as scratch:
        stub_dir = Path(scratch) / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "python"
        stub.write_text(_python_stub_text(), encoding="utf-8")
        stub.chmod(0o755)

        temp_root = Path(scratch) / "tmp"
        temp_root.mkdir()
        log = Path(scratch) / "invocations.jsonl"

        environment = dict(os.environ)
        environment["PATH"] = os.pathsep.join([str(stub_dir), environment.get("PATH", "")]).rstrip(
            os.pathsep
        )
        environment["TMPDIR"] = str(temp_root)
        environment["POKERED_POLICY_STUB_LOG"] = str(log)
        environment["VIRTUAL_ENV"] = sys.prefix

        completed = subprocess.run(
            ["bash", str(RUNNER)],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        assert not log.exists() or log.stat().st_size >= 0
        if not log.exists():
            # The stub never ran, so nothing about the runner was measured.
            assert False, (
                "the stub `python` was never invoked by the runner "
                f"(exit={completed.returncode}): {completed.stderr.strip()[:500]}"
            )
        recorded: list[list[str]] = []
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                recorded.append(json.loads(line))

        shutil.rmtree(temp_root, ignore_errors=True)
        return recorded


def test_main_ruff_lanes_run_in_executable_control_flow() -> None:
    """The main lanes must be *run* by CI, not merely present in the text.

    Every other check in this file reads the runner and workflow as text, so
    a lane can be wrapped in dead shell control flow -- `if false; then ... fi`
    -- and still parse (`bash -n` passes) and still contain the path, while
    CI executes neither command. That was found green in review.

    So the runner is actually executed against a stub `python` that records
    its argument vector and does nothing else. The recorded trace is the
    authoritative list of commands CI runs; the main lanes are required to
    appear in it. The stub keeps the run fast, offline and side-effect free:
    no dependency install, no gate, no wheel build.
    """

    recorded = _record_runner_python_invocations()
    executed = [arguments for arguments in recorded if arguments[:2] == ["-m", "ruff"]]

    runner_lanes = [
        _main_lane(_ruff_invocations(RUNNER.read_text(encoding="utf-8")), subcommand)
        for subcommand in ("check", "format")
    ]

    for subcommand, lane in zip(("check", "format"), runner_lanes, strict=True):
        # The parser keeps the leading `python` token; the stub *is* python, so
        # its recorded vector starts at `-m`. Drop that one token from the
        # parsed lane so both sides describe the same Ruff invocation.
        expected = list(lane[1:])
        matches = [
            arguments for arguments in executed if arguments == expected and "tests" in arguments
        ]
        assert matches, (
            f"the local runner declares a `ruff {subcommand}` lane covering tests/, "
            f"but executing it never invokes that command; the lane is inside "
            f"disabled control flow and CI would not run it"
        )
        # The main lane must actually carry the benchmark when executed.
        assert any(_BENCHMARK in arguments for arguments in matches), (
            f"the executed `ruff {subcommand}` lane does not include {_BENCHMARK}"
        )
