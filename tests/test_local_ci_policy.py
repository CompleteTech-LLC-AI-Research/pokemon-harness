"""Static policy checks for the reproducible local CI runner.

Most rows inspect the runner and workflow text.  Two of them instead *execute*
the runner against a stub ``python`` that records argument vectors, so that
exit-status propagation is measured rather than inferred; the stub keeps that
run inside the unit tier instead of installing dependencies, building a wheel,
or running the bounded production gate.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tokenize
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


def _ruff_lint_resolved_files(
    paths: tuple[str, ...] = ("tests",), tree: str | None = "tests"
) -> set[str]:
    """Return the repo-relative files Ruff itself resolves for a lane.

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

    `paths` is the lane's own path list, because the main lanes mix directory
    tokens with enumerated files and both forms have to be measured the way the
    lane really passes them. `tree` narrows the answer to one subtree, so a
    `src` entry in the config cannot pull a config-relative file into a row
    whose contract is about `tests/`; `tree=None` measures the whole lane,
    including the enumerated non-`tests/` files.
    """

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            *paths,
            "--show-files",
            "--no-cache",
        ],
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
        # `src` entry; only the requested tree is this row's contract.
        if tree is None or relative.startswith(f"{tree.rstrip('/')}/"):
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
#   * `--diff`/`--diff-format` change what the formatter emits, so the
#     format probe's verdict would not reflect the lane's own.
#   * `--fix`/`--fix-only` *repair* the violation instead of reporting it, so
#     Ruff exits 0 on a file that was broken when CI read it. (Verified: an
#     unused import is fixed and the probe exits 0.) A gate that silently
#     rewrites the benchmark is not a gate.
#
# `--select`/`--extend-select`/`--ignore`/`--extend-ignore` are deliberately
# NOT rejected here: narrowing the rule set is a legitimate reviewable
# decision, and rejecting the option would be over-rejecting. Instead the
# check probe below asserts that the rules CI actually relies on survive the
# narrowing -- so `--select=F821` fails because it drops F401, not because the
# word "select" appeared.
#
# The `--range` family is matched by prefix because Ruff accepts `--range`,
# `--range=N`, and (in future) a range-like spelling; rejecting any option
# whose name starts with `--range` avoids re-opening this hole silently.
_WEAKENING_OPTIONS = (
    "--exit-zero",
    "--diff",
    "--diff-format",
    "--fix",
    "--fix-only",
)
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


# Each entry is a snippet that violates exactly one rule, plus that rule's
# code. Several are needed because a single probe only proves one rule
# survives. Two escapes were found in review precisely because they silenced
# that one rule:
#
#   * `--select=F821` narrowed a lane so F401 stopped firing; an F821-only
#     probe still passed while an unused import went unreported.
#   * A benchmark-specific `per-file-ignores = ["F821"]` did the same.
#
# Probing three independent rules means narrowing that drops one of them is
# caught, while a *selective* ignore of a different rule is not over-rejected.
# A blanket disable -- `--ignore=ALL` or `per-file-ignores = ["ALL"]` -- silences
# all of them at once, so it still fails on the first.
# Each rule was confirmed to fire through the stdin probe under this
# project's configuration. F841 was tried first and dropped: Ruff lists it as
# enabled in `--show-settings` but does not report it for this input, so a
# probe whose rule never fires would assert nothing.
# Spanning five rule codes is what makes a blanket disable distinguishable from
# a selective ignore. `per-file-ignores = ["ALL"]` silences every one of them;
# an entry naming a single rule leaves the other four firing. Three probes
# could not do this -- with `--select=F401,F811` two of three still fired and
# F821 was silently accepted.
#
# Two earlier probes were replaced after measuring that they were not doing
# what they claimed. F841 is listed as enabled by `--show-settings` but is not
# reported for a local assignment, and an `E711` snippet using an undefined
# name tripped F821 instead of E711 -- so one "probe" was silently re-testing
# another rule, and `--extend-ignore=E711` passed unnoticed. Every rule below
# was confirmed to fire on its own, and to still fire when a *different* rule
# is ignored.
_CHECK_PROBES = (
    ("F821", "def _probe():\n    return _pokered_undefined_probe_name\n"),
    ("F401", "import os\n"),
    ("F811", "def f():\n    pass\ndef f():\n    pass\n"),
    ("F632", "def _probe():\n    x = 1\n    if x is 1:\n        pass\n"),
    ("F541", "x = f'hello'\n"),
)

# Unformatted in two independent ways, so a lane cannot pass by rejecting only
# one kind of reformatting.
_FORMAT_PROBES = (
    ("extra spacing", "x  =  1\n"),
    ("statement joining", "x = 1; y = 2\n"),
)


# Code Ruff rejects before it applies any rule. Unlike a lint diagnostic this
# survives `--select`, `--ignore=ALL` and a blanket `per-file-ignores`, so it
# answers a different question from the rule probes: is Ruff looking at this
# file *at all*? It was needed because the control-filename probe below cannot
# see a `per-file-ignores = ["ALL"]` entry -- for the control file every rule
# still fires, which looks exactly like a selective ignore.
_UNPARSEABLE_SNIPPET = "def _probe(:\n"

# A path no `per-file-ignores` entry names, used to tell a rule that is
# selectively ignored *for the benchmark* from one that is disabled outright.
_CONTROL_FILENAME = "scripts/_policy_control_.py"


def _probe_check(
    options: list[str], snippet: str, filename: str = _BENCHMARK
) -> subprocess.CompletedProcess[str]:
    """Lint a snippet holding one real violation, through the lane's options.

    The probe deliberately feeds *bad* code rather than reading the benchmark:
    it answers "would this lane reject a violation in this file?", which is the
    question CI actually asks. A lane that silently drops the file, disables
    the relevant rule, or forces exit 0 all answer "no" here, without any of
    them needing to be enumerated in the test.

    `filename` defaults to the benchmark. Passing the control path instead
    re-asks the same question about a file that no per-file ignore names, which
    is what separates a selective ignore from a disabled rule.
    """

    return subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            *options,
            "--stdin-filename",
            filename,
            "-",
        ],
        cwd=ROOT,
        input=snippet,
        capture_output=True,
        text=True,
        check=False,
    )


def _probe_format(options: list[str], snippet: str) -> subprocess.CompletedProcess[str]:
    """Format a snippet that is known-unformatted, through the lane's options.

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
        input=snippet,
        capture_output=True,
        text=True,
        check=False,
    )


def _diagnostics(completed: subprocess.CompletedProcess[str]) -> str:
    """Return the probe's combined output for assertion messages."""

    return f"{completed.stdout.strip()} {completed.stderr.strip()}".strip()


def _reports_code(completed: subprocess.CompletedProcess[str], code: str) -> bool:
    """Say whether a JSON-format Ruff run reported `code` as a diagnostic.

    Ruff's human output repeats the offending source line, so a directive that
    merely *mentions* a code ("`# ruff: noqa: F821, F401`") puts that code in
    the text even when nothing was reported. The JSON array carries each
    diagnostic's code separately, which is the only reliable way to ask.

    Anything that is not a parseable JSON array counts as *reporting*, not as
    silence: a probe that did not return Ruff's JSON has measured nothing, and
    treating that as "no diagnostic" would let a broken probe mark a real
    directive as harmless. Empty output counts as unmeasured too.

    "Reporting" is the safe answer at both call sites, but for opposite
    reasons, so read the polarity before changing this. A guard that wants the
    code *absent* fails loudly when a probe breaks, which is correct. A caller
    asking whether a directive silenced something would instead read the broken
    probe as "still reporting" and miss a real directive. That direction is
    quiet, and what bounds it is *not* the guard above -- that one lints the
    file on disk, not the stdin path these probes use. It is the
    `lint_directives` assertion in the same test: those ten probes go through
    this same stdin path, so a probe that had stopped emitting JSON would fail
    that equality loudly instead of quietly excusing a comment here.
    """

    if not completed.stdout.strip():
        return True
    try:
        diagnostics = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return True
    if not isinstance(diagnostics, list):
        # Parseable but not Ruff's documented array shape, so this run measured
        # nothing. Reporting is the safe answer; silence would let a broken
        # probe excuse a real directive.
        return True
    return any(isinstance(item, dict) and item.get("code") == code for item in diagnostics)


def _comment_tokens(source: str) -> list[tokenize.TokenInfo]:
    """Return only the real comments in `source`, as Python tokenizes them.

    A suppression directive is only honoured by Ruff when it is an actual
    comment. The same text inside a docstring or a string literal is inert, so
    matching it with `line.strip().startswith("#")` would refuse a file whose
    documentation merely quotes a directive. Tokenizing asks the same question
    Ruff does.

    A file this cannot tokenize yields no comments, which would make the
    caller pass vacuously -- so the caller pairs this with a real lint of the
    file, which rejects an unparseable module outright. `tokenize` reports that
    as `TokenError` or `IndentationError` depending on where it gives up; the
    broader `SyntaxError` is not caught here on purpose.
    """

    try:
        return [
            token
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type == tokenize.COMMENT
        ]
    except (tokenize.TokenError, IndentationError):
        return []


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


def test_each_check_probe_detects_its_own_rule() -> None:
    """Every probe must fire on its own rule, and be independent of the others.

    Two probes were wrong for a long time and neither showed up as a failure.
    F841 is listed as enabled by `--show-settings` but is never reported for a
    local assignment, so its probe asserted nothing. An `E711` snippet that used
    an undefined name tripped F821 instead, so one probe was silently
    re-testing another rule -- and `--extend-ignore=E711` then passed unnoticed.

    This row pins both properties directly, so a future probe cannot quietly
    stop testing what it claims: it must report its own named rule, and it must
    keep reporting that rule when any *other* probed rule is ignored. The
    second half is the independence check that caught the E711 mix-up.
    """

    options = _lane_options(
        _main_lane(_ruff_invocations(RUNNER.read_text(encoding="utf-8")), "check")
    )
    for rule, snippet in _CHECK_PROBES:
        probe = _probe_check(options, snippet)
        assert probe.returncode != 0 and rule in _diagnostics(probe), (
            f"the {rule} probe does not report {rule} on an unmodified lane: "
            f"{_diagnostics(probe)!r}"
        )
        # Ignore each *other* probed rule in turn; this one must survive.
        for other, _other_snippet in _CHECK_PROBES:
            if other == rule:
                continue
            isolated = _probe_check(options + [f"--ignore={other}"], snippet)
            assert isolated.returncode != 0 and rule in _diagnostics(isolated), (
                f"the {rule} probe stops reporting {rule} when {other} is ignored, "
                f"so it is not testing its own rule: {_diagnostics(isolated)!r}"
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
    2. Ruff actually *rejects* violations in it. A lane carrying
       `--exclude=<benchmark> --force-exclude` names the path and then drops
       it; a `per-file-ignores` entry of `["ALL"]` leaves it resolved but
       silently unlinted; a lane-level `--ignore=ALL` disables everything that
       would catch a violation. Each leaves Ruff reporting "All checks passed"
       for *anything*, including an undefined name.
    3. Every rule CI relies on survives any narrowing of the rule set. A
       single probe is not enough here: `--select=F821` keeps an undefined
       name fatal while an unused import passes unnoticed, and a
       `per-file-ignores = ["F821"]` entry does the same. Both were found green
       in review, so the lane is probed with several independent violations.

    Rather than model Ruff's rule resolution, each probe asks Ruff the question
    CI asks -- "would you reject this violation in this file, with these exact
    options?" -- by feeding a known-bad snippet through the lane's own option
    vector. An exclusion, a blanket ignore, a narrowed select, a per-file
    `ALL`, and `--exit-zero` all collapse to the same answer, and all of them
    fail here. Narrowing that drops *one* rule fails too, because a second
    probe covers that rule -- while a selective per-file ignore of an
    unrelated rule is not over-rejected, because every remaining probe still
    fires.

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
        is_check = "check" in label
        if is_check:
            probes = [(rule, _probe_check(options, snippet)) for rule, snippet in _CHECK_PROBES]
        else:
            probes = [(rule, _probe_format(options, snippet)) for rule, snippet in _FORMAT_PROBES]

        # A parse failure is not a lint verdict, so it is reported as such
        # rather than passing as "no violations".
        for rule, probe in probes:
            assert probe.returncode != 2, (
                f"{label} probe for {rule} failed to parse: {_diagnostics(probe)}"
            )

        # For `check`, the rule is required *and* the specific rule must be
        # reported. The combination matters: a lane narrowed to `--select=F821`
        # still fails F821, so exit status alone would pass while an unused
        # import went unreported. Requiring the named diagnostic closes that.
        #
        # Every probed rule must be reported, not a majority of them. A quorum
        # is exactly what `--select=F401,F811` walked through: two of the three
        # probes still fired while F821 -- an undefined name in the benchmark --
        # was silently accepted. Two would have to be dropped, and only a
        # blanket disable does that, which the rule-ignoring probe already
        # separates from a selective per-file ignore.
        #
        # `ruff format --check` has no equivalent signal -- it exits nonzero for
        # an unformatted file and prints nothing at all -- so the format lane is
        # judged on exit status alone, across two independent snippets.
        if is_check:
            rejected = [
                rule
                for rule, probe in probes
                if probe.returncode != 0 and rule in _diagnostics(probe)
            ]
        else:
            rejected = [rule for rule, probe in probes if probe.returncode != 0]
        # A selective `per-file-ignores` entry for one rule is a legitimate
        # reviewable decision, not an escape, so it must not fail here. The two
        # are told apart by re-running the same snippet under a filename the
        # ignore does not name: if the rule still fires there, the rule itself
        # is active and only this file's copy of it is silenced, which is
        # selective. If it fires for neither, the rule is disabled outright.
        selective: list[str] = []
        if is_check:
            # Is Ruff reading this file at all? A syntax error is reported
            # before any rule selection is applied, so this fails when the file
            # is excluded and passes when it is merely unlinted.
            parsed = _probe_check(options, _UNPARSEABLE_SNIPPET)
            assert "invalid-syntax" in _diagnostics(parsed), (
                f"{label} did not report an unparseable {_BENCHMARK}: Ruff is not "
                f"reading the file at all, so it is excluded from the gate "
                f"(options={options}); output={_diagnostics(parsed)!r}"
            )

            # Which missing rules are selective? Re-ask each one under a path no
            # per-file ignore names: if the rule still fires there, the rule is
            # active and only this file's copy of it is silenced.
            control = [
                _probe_check(options, snippet, _CONTROL_FILENAME)
                for _rule, snippet in _CHECK_PROBES
            ]
            selective = [
                rule
                for (rule, _probe), control_probe in zip(probes, control, strict=True)
                if rule not in rejected and control_probe.returncode != 0
            ]
        escaped = [rule for rule, _ in probes if rule not in rejected and rule not in selective]
        assert not escaped, (
            f"{label} would not reject {escaped} in {_BENCHMARK}, and the same "
            f"rules are not active for other files either, so the rule set is "
            f"narrowed or blanket-disabled (options={options})"
        )

        # A `per-file-ignores = ["ALL"]` entry is not selective at all: it
        # silences every rule for this file, which the control probe cannot see
        # because the control file keeps every rule. A majority of the probed
        # rules having to fire is what catches it while still tolerating an
        # entry that names one rule.
        assert len(rejected) * 2 > len(probes), (
            f"{label} rejects only {rejected} of "
            f"{[rule for rule, _ in probes]} for {_BENCHMARK}; a rule set where "
            f"most rules are silenced for this file is a blanket disable, not a "
            f"selective ignore (options={options})"
        )


def test_benchmark_cannot_opt_out_of_linting_with_in_file_suppression() -> None:
    """A file-level `noqa`/`fmt` directive must not silence the whole file.

    Every probe above feeds its snippet on *stdin*, which is what makes the
    probes immune to `exclude`, `per-file-ignores` and a narrowed rule set --
    Ruff resolves those from the filename, not the bytes. The cost of that
    design is that a suppression living in the file's own content is invisible
    to all of them.

    That is not hypothetical. Planting `# ruff: noqa` on the benchmark's first
    line, next to a real undefined name, leaves both lanes reporting
    `All checks passed!` and every other row in this file green -- the whole
    file becomes unlintable. An in-file directive is the one escape that no
    option-level probe can catch, because the snippet the probe lints has no
    directive to find.

    Matching those directives textually is what this row deliberately avoids.
    A bare `# ruff: noqa` is not the only form that silences a whole file:
    measured against Ruff, `# ruff: noqa: F821`, `#ruff:noqa` and a
    trailing-space variant each suppress F821 file-wide, while a bare
    `# noqa: E501` is not honoured at all. A string-literal or docstring line
    that merely *reads* like a directive is honoured by neither. So rather than
    enumerate spellings, ask Ruff directly. Two lists are involved and both are
    measured rather than assumed: a set of candidate spellings, each of which
    must still silence the file (so the row cannot go vacuous if Ruff changes),
    and the comments the benchmark actually carries, each of which is refused
    only if it really does silence the file.

    That second pass is behavioural on purpose, and it is deliberately
    asymmetric with the list above. The list holds whole-file opt-outs; the
    scan asks whether each comment stops *this* lane from catching *this*
    probe, which is stricter. A code-bearing directive scoped to some other
    code is left alone, and a `# fmt: off` region is surfaced even when a
    later `# fmt: on` balances it -- Ruff does not re-check the span between
    them, so an unformatted region there would still ship unchecked.
    """

    source = (ROOT / _BENCHMARK).read_text(encoding="utf-8")
    lines = source.splitlines()

    # `_comment_tokens` yields nothing for a file it cannot tokenize, which
    # would make the scan below pass vacuously. Require that the benchmark is
    # actually lintable, so that path stays unreachable while the file is fine.
    lintable = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--output-format=json", _BENCHMARK],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert not _reports_code(lintable, "invalid-syntax"), (
        f"{_BENCHMARK} no longer parses, so its comments cannot be inspected "
        f"and this row would pass for the wrong reason: "
        f"{_diagnostics(lintable)[:400]!r}"
    )

    # A violation Ruff certainly reports in a clean file, used as the probe:
    # if a directive silences the file, this stops being reported.
    violation = "\n\ndef _suppression_probe():\n    return _undefined_name_probe\n"
    check_options = _lane_options(
        _main_lane(_ruff_invocations(RUNNER.read_text(encoding="utf-8")), "check")
    )
    format_options = _lane_options(
        _main_lane(_ruff_invocations(RUNNER.read_text(encoding="utf-8")), "format")
    )
    reported = _probe_check(check_options, violation)
    assert reported.returncode != 0 and "F821" in _diagnostics(reported), (
        "the suppression probe no longer reports F821 on the real lane options, "
        f"so this row cannot measure anything: {_diagnostics(reported)[:400]!r}"
    )

    # Lint directives: each of these really does suppress F821 file-wide,
    # including the Flake8-compatible alias and the comma-separated code list.
    # Uppercase spellings are NOT honoured and are deliberately absent, which
    # is why the row measures each candidate instead of trusting this tuple to
    # stay exhaustive.
    lint_directives = (
        "# ruff: noqa",
        "# ruff:noqa",
        "#ruff:noqa",
        "#ruff: noqa",
        "# ruff: noqa   ",
        "# ruff: noqa: F821",
        "# ruff: noqa: F821, F401",
        "# flake8: noqa",
        "#flake8: noqa",
        "#flake8: noqa: F821",
    )
    # Format directives: `# fmt: off` disables the formatter for the rest of
    # the file *unless* a later `# fmt: on` balances it, so the escape needs
    # the unbalanced form below. They are invisible to `ruff check`, so this
    # half is measured through the format lane instead.
    format_directives = (
        "# fmt: off",
        "# fmt:off",
        "# yapf: disable",
        "# yapf:disable",
    )
    baseline = _probe_format(format_options, _FORMAT_PROBES[0][1])
    assert baseline.returncode != 0, (
        "the format probe no longer reports an unformatted file, so this row "
        f"cannot measure anything: {_diagnostics(baseline)[:400]!r}"
    )
    silencing: list[str] = []
    for directive in lint_directives:
        # Ask for F821 by *code*, not by matching text: the code-bearing
        # "F821, F401" spelling really does silence the whole file, but its
        # unused `F401` also trips RUF100, so the process still exits nonzero
        # and the echoed source line contains the string "F821" even when
        # nothing is reported. Either signal would call that a live escape.
        #
        # The directive is named only by its code list, never quoted verbatim.
        # Writing one of these opt-out lines into *this* file would silence
        # F821 here as well, which is the same escape this row exists to catch.
        probe = _probe_check(check_options + ["--output-format=json"], f"{directive}\n" + violation)
        if not _reports_code(probe, "F821"):
            silencing.append(directive)

    # Every one of those spellings really does suppress the file, so the row is
    # measuring Ruff and not a guess about it. If a future Ruff drops one, the
    # list shrinks and the row reports the drift instead of silently narrowing.
    assert silencing == list(lint_directives), (
        "these in-file lint directives no longer silence the benchmark, so "
        f"the escape they represent is gone and this row must be revisited "
        f"(silenced={silencing}, expected={list(lint_directives)})"
    )

    for directive in format_directives:
        probe = _probe_format(format_options, f"{directive}\n" + _FORMAT_PROBES[0][1])
        assert probe.returncode == 0, (
            f"{directive!r} no longer disables the formatter for the rest of "
            f"the file, so the format-lane escape it represents is gone and "
            f"this row must be revisited"
        )

    # Test each real comment the benchmark actually carries by asking Ruff
    # whether *that* comment silences the file. Matching text instead cannot
    # separate a code-bearing opt-out for one rule from one written for another
    # -- both silence the file -- and it would wrongly refuse a balanced
    # format-off/format-on region that only skips one span.
    #
    # Each comment is probed through the lane it could affect: a formatter
    # directive is invisible to `ruff check`, so asking the check lane would
    # wave every one of them through.
    #
    # A directive is refused when it silences *any* probe, which is the
    # honest reading of "the file opted out of the gate". Two measured cases
    # that look narrower than they are: a code-bearing directive silences its
    # own code file-wide but still reports every other rule, and a
    # format-off/format-on region leaves later code checked -- yet both stop
    # the specific probe this row runs, so both are worth surfacing.
    def silences(comment: str) -> bool:
        if not _reports_code(
            _probe_check(
                check_options + ["--output-format=json"],
                f"{comment}\n{violation}",
            ),
            "F821",
        ):
            return True
        return _probe_format(format_options, f"{comment}\n" + _FORMAT_PROBES[0][1]).returncode == 0

    # Probing all 131 comments through two lanes costs ~130 subprocesses, so
    # skip the ones Ruff's directive grammar cannot possibly match. A comment
    # that does not name one of these tools cannot be one of their directives.
    # This is a short-circuit on a necessary condition, not a reimplementation
    # of the rule -- `silences` still decides anything that gets this far.
    #
    # The comparison has to normalise the spacing Ruff itself ignores, or it
    # stops being a superset. Measured: extra spaces after the hash, a doubled
    # hash, and a tab all silence F821 exactly as the bare spelling does,
    # because Ruff strips leading `#` characters and the whitespace that
    # follows them. Matching the literal token instead of the normalised one
    # would let exactly those spellings through, so `lstrip("#")` and `strip()`
    # are load bearing here, not tidying. Case is deliberately *not* normalised
    # the other way: `.lower()` keeps an uppercase spelling in scope even
    # though Ruff honours no such casing, and over-inclusion only costs one
    # subprocess.
    directive_prefixes = (
        "ruff",
        "flake8",
        "fmt",
        "yapf",
        "noqa",
    )

    def maybe_directive(comment: str) -> bool:
        return comment.lstrip("#").strip().lower().startswith(directive_prefixes)

    file_level = [
        (token.start[0], token.string)
        for token in _comment_tokens(source)
        if maybe_directive(token.string) and silences(token.string)
    ]
    assert not file_level, (
        f"{_BENCHMARK} carries a file-level suppression at "
        f"{[f'{n}: {t}' for n, t in file_level]}; every probe in this file "
        f"lints stdin, so an in-file opt-out makes the whole file unlintable "
        f"while the suite stays green. Scope the directive to a line "
        f"(`# noqa: CODE`) or remove it."
    )

    # The benchmark is allowed -- and required -- to carry selective,
    # line-scoped ignores; assert one is still honoured, so a future edit
    # cannot satisfy this row by deleting all of them.
    selective = [line for line in lines if "# noqa:" in line]
    assert selective, (
        f"{_BENCHMARK} no longer carries any selective `# noqa: CODE`; this "
        f"row requires the file to stay genuinely lint-clean, not merely free "
        f"of file-level directives"
    )
    honoured = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select=BLE001",
            f"--stdin-filename={_BENCHMARK}",
            "-",
        ],
        cwd=ROOT,
        input="try:\n    pass\nexcept Exception:  # noqa: BLE001\n    pass\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert honoured.returncode == 0, (
        "a selective `# noqa: BLE001` no longer suppresses BLE001, so the "
        f"benchmark's own documented ignore is stale: "
        f"{_diagnostics(honoured)[:400]!r}"
    )


# A file-level `ruff`/`flake8` opt-out, matched the way Ruff matches it. Each
# allowance below was measured against `ruff check` on stdin rather than
# assumed, because Ruff honours a much narrower grammar than "looks like a
# directive":
#   honoured -- a bare `noqa` directive, a doubled leading hash, a missing space
#              after the hash, spaces around the colon, leading indentation,
#              leading `-` characters, a trailing comment, and the `flake8`
#              alias.
#   inert    -- any trailing colon (Ruff splits on colons, so an empty code
#              list disables nothing), and an upper-cased value, since the
#              keyword comparison is case-sensitive.
# Measured over every spelling in `_DIRECTIVE_SPELLINGS`, which is asserted
# against the installed Ruff below, the pattern has no false negative.
# Matching more than Ruff honours would refuse files that are in fact linted;
# matching less would let a real opt-out through. So `\s` is deliberately not
# used in the separator classes: it would swallow a newline and let the pattern
# span two comment tokens, matching text Ruff never reads as a directive.
# The leading prefix is one repeated `#+` rather than a nested
# `#+` over an overlapping character class, for two independently measured
# reasons. A nested quantifier like `[#-]+(?:[ \t]*[#-]+)*` backtracks
# catastrophically on the ASCII-ruler comments this repository uses as section
# separators: it did not return in 3s on `# ` plus 75 dashes, a comment that
# appears in `tests/_battle_item_evidence.py`. And the dashes are unnecessary --
# Ruff strips leading `#` and whitespace but stops at a `-`, so `#--ruff: noqa`
# is inert even though the nested form matched it. One flat repeated group is
# both linear and accurate, and `test_directive_patterns_track_the_installed_ruff`
# is what keeps it accurate: tightening the prefix to `#+` alone looked tidier
# and silently stopped matching a doubled hash separated by whitespace, which
# does silence the file.
# The named codes are optional, and their absence is exactly what makes a
# directive *blanket* -- the case this row refuses.
#
# No honoured spelling is quoted in full here on purpose: a directive written
# into this file would silence F821 in the test file itself, which is the very
# escape this row exists to catch.
_FILE_LEVEL_LINT_DIRECTIVE = re.compile(
    r"^[ \t]*(?:[#-][ \t]*)+(?:ruff|flake8)[ \t]*:[ \t]*noqa"
    r"(?:[ \t]*:[ \t]*(?P<codes>[A-Za-z]+[0-9]+(?:[ \t]*,[ \t]*[A-Za-z]+[0-9]+)*))?"
    r"[ \t]*(?:#.*)?$"
)

# The formatter half, measured on the same footing with `ruff format --diff`:
# a leading `off` directive and `yapf: disable` both stop the reformat, extra
# whitespace changes nothing, and a trailing `on` restores it, so `on` is
# correctly not matched here. These are a plain opt-out with no codes to
# measure, which is why the stale-directive check below does not apply to them.
_FILE_LEVEL_FORMAT_DIRECTIVE = re.compile(
    r"^[ \t]*#+[ \t]*(?:fmt|yapf)[ \t]*:?[ \t]*(?:off|disable)\b"
)


def _reported_codes(completed: subprocess.CompletedProcess[str]) -> set[str] | None:
    """Return the codes a JSON Ruff run reported, or None when unreadable.

    Unlike `_reports_code`, which answers "was this code reported" and treats
    anything unmeasurable as *yes*, this returns the whole set, because the
    caller has to tell a file that is clean apart from one it could not read.
    An unmeasurable run is therefore None rather than an empty set.
    """

    if not completed.stdout.strip():
        return None
    try:
        diagnostics = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(diagnostics, list):
        return None
    return {
        item["code"]
        for item in diagnostics
        if isinstance(item, dict) and isinstance(item.get("code"), str)
    }


def _lane_file_level_directives(source: str) -> list[tuple[int, str, tuple[str, ...]]]:
    """Return `(line, text, named_codes)` for each file-level opt-out.

    Only real comment tokens count: the same text inside a docstring or a
    string literal is inert, and Ruff honours it as inert. An empty
    `named_codes` means a blanket opt-out.
    """

    found: list[tuple[int, str, tuple[str, ...]]] = []
    for token in _comment_tokens(source):
        text = token.string.strip()
        lint = _FILE_LEVEL_LINT_DIRECTIVE.match(text)
        if lint is not None:
            codes = tuple(
                code.strip().upper()
                for code in (lint.group("codes") or "").split(",")
                if code.strip()
            )
            found.append((token.start[0], token.string, codes))
        elif _FILE_LEVEL_FORMAT_DIRECTIVE.match(text):
            found.append((token.start[0], token.string, ()))
    return found


# Every spelling below was classified by running the *installed* Ruff, not by
# reading Ruff's source or assuming a grammar. `honoured` means a planted
# undefined name plus the directive leaves `ruff check` at exit 0; `inert`
# means Ruff still reports it. This table is the evidence that the patterns
# above track the real linter rather than a guess at it.
#
# A reviewer cannot confirm that claim from the prose, and it is the one
# load-bearing assumption in the row below, so it is asserted here: if a Ruff
# upgrade changes which spellings silence a file, this fails and names the
# spelling, instead of the guard quietly ceasing to detect an opt-out.
_PROBE_UNDEFINED_AND_UNUSED = "import os\n\n\ndef _probe():\n    return _undefined_zzz\n"

_DIRECTIVE_SPELLINGS: tuple[tuple[str, str, bool], ...] = (
    ("bare", "# ruff: noqa", True),
    ("doubled_hash", "## ruff: noqa", True),
    ("no_space_after_hash", "#ruff: noqa", True),
    ("spaces_around_colon", "# ruff : noqa", True),
    ("no_space_at_all", "#ruff:noqa", True),
    ("tab_separator", "#\truff:noqa", True),
    ("indented", "    # ruff: noqa", True),
    ("deeply_indented", "        # ruff: noqa", True),
    ("hash_space_hash", "# # ruff: noqa", True),
    ("hash_spaces_hash", "#   # ruff: noqa", True),
    ("doubled_hash_space_hash", "## # ruff: noqa", True),
    ("hash_dash_space_hash", "#- # ruff: noqa", True),
    ("flake8_alias", "# flake8: noqa", True),
    ("trailing_comment", "# ruff: noqa  # whole file", True),
    ("irregular_spacing", "#  ruff  :  noqa  ", True),
    # Inert: a trailing colon makes Ruff split on it and read an empty code
    # list, which disables nothing.
    ("trailing_colon", "# ruff: noqa:", False),
    ("trailing_colon_irregular", "##   ruff:   noqa:   ", False),
    # Inert: the keyword comparison is case-sensitive.
    ("upper_cased", "# RUFF: NOQA", False),
    ("mixed_cased", "# Ruff: NoQA", False),
    ("no_space_colon_inert", "# noqa: ruff", False),
    ("word_before_keyword", "# something ruff: noqa", False),
    ("word_after_hash", "## something: noqa", False),
    # Inert: Ruff strips leading `#` and whitespace but stops at a `-`, so the
    # keyword after a doubled dash is not the start of the directive.
    #
    # The guard deliberately still matches this one. Ruff's own stripping
    # accepts a single leading dash, but a *second* dash makes the directive
    # inert, and narrowing the prefix far enough to exclude it would also stop
    # matching a doubled hash separated by whitespace, which does silence the
    # file. Over-matching one
    # inert spelling refuses a file Ruff would have linted; under-matching any
    # honoured one lets a lane file opt out silently. The asymmetry decides it.
    ("leading_dashes", "#--ruff: noqa", False),
    # A bare line-level `noqa` comment, not a file-level directive. Spelled with
    # a space so this comment does not read as a real directive to Ruff, which
    # would otherwise warn about an invalid code list on this very line.
    ("line_level_noqa", "#noqa", False),
    ("empty", "", False),
)

# A pattern that over-matches an inert spelling is tolerated, and only for the
# spellings named here. Each is a deliberate fail-closed choice, documented at
# its entry in `_DIRECTIVE_SPELLINGS`; anything else over-matching is a defect.
_TOLERATED_OVER_MATCHES: frozenset[str] = frozenset({"leading_dashes"})


# Selective directives name codes, so whether they scope the file as intended
# depends on those codes actually firing alongside an unnamed one. Each body
# below raises the named codes plus one the directive does not name; the test
# then requires the named ones to disappear and the unnamed one to survive.
_SELECTIVE_SPELLINGS: tuple[tuple[str, str, str, frozenset[str]], ...] = (
    (
        "selective_one_code",
        "# ruff: noqa: F401",
        _PROBE_UNDEFINED_AND_UNUSED,
        frozenset({"F401"}),
    ),
    (
        "selective_irregular",
        "##   ruff:   noqa:   F401   ",
        _PROBE_UNDEFINED_AND_UNUSED,
        frozenset({"F401"}),
    ),
    (
        "selective_two_codes",
        "# ruff: noqa: F401, F841",
        "import os\nfrom sys import path as _p\n\n\ndef _probe():\n    return _undefined_zzz\n",
        frozenset({"F401", "F841"}),
    ),
)


def test_directive_patterns_track_the_installed_ruff() -> None:
    """The suppression patterns must match what Ruff actually honours.

    A guard that stops detecting a real opt-out fails silently: the row below
    would keep passing while a lane file silenced itself. So the correspondence
    is asserted against the installed Ruff on every run rather than recorded as
    a one-off measurement.

    Blanket spellings are measured as "does this silence the whole file", which
    is exactly the condition the row refuses. Selective spellings are measured
    differently on purpose: a selective directive is *supposed* to silence the
    codes it names, so "the file went quiet" says nothing about whether it was
    recognised. For those the question is whether it is scoped -- the named
    codes disappear and an unnamed one survives -- which is also what makes the
    allowance measured rather than blanket. A selective directive that silenced
    everything would be a blanket opt-out wearing a code list, and this asserts
    it does not.

    The probe is deliberately minimal -- an unused import, a used-looking
    re-export, and an undefined name -- because the question is only how the
    *directive* scopes the file, not whether the file is otherwise clean.
    """

    missed: list[str] = []
    spurious: list[str] = []
    blanket_cases: tuple[tuple[str, str, bool], ...] = tuple(
        (label, directive, honoured)
        for label, directive, honoured in _DIRECTIVE_SPELLINGS
        if directive
    )
    for label, directive, honoured in blanket_cases:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "--output-format=json",
                "--no-cache",
                "--stdin-filename",
                "scripts/_directive_grammar_probe.py",
                "-",
            ],
            cwd=ROOT,
            input=_PROBE_UNDEFINED_AND_UNUSED + directive + "\n",
            capture_output=True,
            text=True,
            check=False,
        )
        silenced = completed.returncode == 0
        detected = bool(_lane_file_level_directives(directive))
        if honoured and not detected:
            missed.append(f"{label}: {directive!r} silences Ruff but the guard does not match it")
        if detected and not honoured and label not in _TOLERATED_OVER_MATCHES:
            spurious.append(
                f"{label}: {directive!r} does not silence Ruff but the "
                f"guard matches it, and it is not a documented exception"
            )
        if honoured and not silenced:
            missed.append(
                f"{label}: {directive!r} is classified as silencing but the "
                f"installed Ruff still reports it, so the table is stale"
            )
        if silenced and not honoured:
            spurious.append(
                f"{label}: {directive!r} is classified as inert but the "
                f"installed Ruff silences it, so the table is stale"
            )

    for label, directive, body, named in _SELECTIVE_SPELLINGS:
        detected = _lane_file_level_directives(directive)
        if not detected:
            missed.append(f"{label}: {directive!r} is not detected by the guard at all")
            continue
        if detected[0][2] != tuple(sorted(named)):
            spurious.append(
                f"{label}: the guard reads the named codes as "
                f"{detected[0][2]}, not {tuple(sorted(named))}"
            )
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "--output-format=json",
                "--no-cache",
                "--stdin-filename",
                "scripts/_directive_grammar_probe.py",
                "-",
            ],
            cwd=ROOT,
            input=body + directive + "\n",
            capture_output=True,
            text=True,
            check=False,
        )
        remaining = _reported_codes(completed)
        assert remaining is not None, f"{label}: could not read Ruff's diagnostics"
        if remaining & named:
            missed.append(
                f"{label}: {directive!r} is recognised but did not suppress "
                f"{sorted(remaining & named)}, so the probe body does not "
                f"exercise the codes it names"
            )
        if not remaining - named:
            spurious.append(
                f"{label}: {directive!r} suppressed every code in the file, so "
                f"it is a blanket opt-out rather than a selective one"
            )

    assert not missed, (
        "these spellings silence a file in the installed Ruff but the guard "
        "does not detect them, so a lane file could opt out unnoticed:\n  " + "\n  ".join(missed)
    )
    assert not spurious, (
        "these spellings do not silence a file in the installed Ruff but the "
        "guard flags them, which would refuse files that are actually linted:\n  "
        + "\n  ".join(spurious)
    )


def test_no_lane_file_opts_out_of_linting_with_a_blanket_directive() -> None:
    """No file the main lanes read may opt out of the gate wholesale.

    Every probe in this file lints a snippet on *stdin*, which is what makes
    them immune to `extend-exclude`, `per-file-ignores` and a narrowed rule
    set -- Ruff resolves all three from the filename. The mirror-image cost is
    that a directive living in a file's *own content* is invisible to all of
    them, because the linted snippet carries no directive to find.

    That is a property of any file Ruff reads, not of one file. Measured on
    `scripts/production_gate.py`, which both the runner and the workflow name:
    a blanket opt-out planted beside a real undefined name leaves `ruff check`
    at exit 0 with `F821 reported: False`, while every row in this file stays
    green. The row above closes that for the benchmark alone; this one covers
    the lane, so a new file cannot opt out by carrying the same line.

    A *selective* file-level directive (`# ruff: noqa: F821`) is a different
    thing, and Ruff scopes it to the codes it names: measured, `# ruff: noqa:
    F401` suppresses F401 and still reports an unrelated F821. Those
    directives are load-bearing here. 34 lane files carry one, and stripping
    it from `tests/_sentinel_support_part1.py` surfaces 105 F821s that the
    assembled module supplies, so a blanket "no file-level directive" rule
    would be wrong here rather than merely strict.

    The allowance is measured rather than asserted. Each selective directive is
    re-linted with that one comment removed, and every code it named must be
    genuinely absent afterwards: a directive that has quietly stopped covering
    its code is dead weight, and one that still leaves its code reported is not
    scoped the way it reads. Both fail here instead of sitting in the lane
    unexamined.

    A formatter opt-out is refused whether or not it is later re-enabled.
    Measured with `ruff format --diff`: given a file that already ends in
    `fmt: off`, code appended after that line is left unformatted. So a
    trailing `off` with no matching `on` is not a harmless bookkeeping line,
    it is an opt-out that extends over whatever comes next, and refusing it is
    the point of the row.
    """

    lane = _main_lane(_ruff_invocations(RUNNER.read_text(encoding="utf-8")), "check")
    lane_paths = tuple(token for token in lane[4:] if not token.startswith("-"))
    assert lane_paths, "the main `ruff check` lane names no paths"

    resolved = _ruff_lint_resolved_files(lane_paths, tree=None)
    assert resolved, (
        "Ruff resolved no files for the main check lane "
        f"(paths={lane_paths!r}); this row would pass without measuring anything"
    )

    blanket: list[str] = []
    selective: list[tuple[str, int, str, tuple[str, ...]]] = []
    for relative in sorted(resolved):
        source = (ROOT / relative).read_text(encoding="utf-8")
        for line, text, codes in _lane_file_level_directives(source):
            if codes:
                selective.append((relative, line, text, codes))
            else:
                blanket.append(f"{relative}:{line}: {text}")

    assert not blanket, (
        "these lane files opt out of the Ruff lanes wholesale, so the gate "
        "reports success for code it never reads while every probe in this "
        "file stays green:\n  "
        + "\n  ".join(blanket)
        + "\nScope the directive to a line (`# noqa: CODE`), name the codes it "
        "genuinely needs (`# ruff: noqa: CODE`), or remove it."
    )

    stale: list[str] = []
    for relative, line, text, codes in selective:
        original = (ROOT / relative).read_text(encoding="utf-8")
        stripped = "".join(
            entry
            for index, entry in enumerate(original.splitlines(keepends=True), start=1)
            if index != line
        )
        probe = subprocess.run(
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "--output-format=json",
                "--no-cache",
                "--stdin-filename",
                relative,
                "-",
            ],
            cwd=ROOT,
            input=stripped,
            capture_output=True,
            text=True,
            check=False,
        )
        remaining = _reported_codes(probe)
        assert remaining is not None, (
            f"{relative}:{line} could not be re-linted with its directive "
            f"removed, so its allowance cannot be measured: "
            f"{_diagnostics(probe)[:400]!r}"
        )
        # Removing the directive must *reveal* every code it names: that is
        # what makes the suppression load-bearing rather than decorative. A
        # named code that stays absent was suppressed by something else, or by
        # nothing at all, and the directive is then not doing what it reads.
        # Codes revealed *alongside* the named ones are fine -- a selective
        # directive is expected to leave other rules alone.
        unrevealed = sorted(set(codes) - remaining)
        if unrevealed:
            stale.append(
                f"{relative}:{line}: {text} names {unrevealed}, which the file "
                f"does not report even with the directive removed"
            )

    assert not stale, (
        "these selective directives no longer suppress the codes they name, so "
        "they are dead weight and should be removed or narrowed:\n  " + "\n  ".join(stale)
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
    "    sys.exit(0)",
    "# When asked, fail exactly like a lint violation would: nonzero, after",
    "# recording the call. This is what makes failure propagation measurable.",
    "if os.environ.get('POKERED_POLICY_STUB_FAIL_MAIN_CHECK') and arguments[:3] == [",
    '    "-m", "ruff", "check",',
    '] and "tests" in arguments:',
    "    # This is the main lane: the only `ruff check` that covers all of tests/.",
    '    print("pokered-policy-stub: simulated lint failure", file=sys.stderr)',
    "    sys.exit(1)",
    "sys.exit(0)",
)


def _python_stub_text() -> str:
    """Return the stub program, headed by a shebang that surely resolves."""

    return chr(10).join(_PYTHON_STUB_LINES).replace("{python}", sys.executable, 1)


def _record_runner_python_invocations(
    *, fail_main_check: bool = False
) -> tuple[list[list[str]], int]:
    """Execute the local runner; return its `python` trace and exit status.

    The runner is run for real, but with a stub `python` first on `PATH`. The
    stub records its arguments and exits 0, so the runner walks its whole
    script -- every command, in order -- without installing dependencies,
    running the gate, building a wheel or touching the network. That trace is
    the authoritative answer to "which commands does CI actually execute?",
    which a text search cannot give: a lane inside `if false; then ... fi`
    still parses under `bash -n` and still contains the path.

    With `fail_main_check` the stub instead exits 1 on the main `ruff check`
    lane, the way a real violation would. The runner is supposed to stop
    there, so the trace must end at that point *and* the runner's own exit
    status must be nonzero. Both halves are needed: plain `set +e` shows up as
    extra commands in the trace, while `set +e` followed by `exit 0` leaves the
    trace short but still reports success to whoever invoked the runner.

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
        if fail_main_check:
            environment["POKERED_POLICY_STUB_FAIL_MAIN_CHECK"] = "1"

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
        return recorded, completed.returncode


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

    recorded, _returncode = _record_runner_python_invocations()
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


def test_main_ruff_lanes_stop_the_local_runner_on_failure() -> None:
    """A Ruff failure must stop the runner, not be swallowed and continue.

    The execution trace proves the lanes *run*. It cannot by itself prove a
    nonzero exit *matters*: the stub normally exits 0, so wrapping the lanes in
    `set +e` -- or `|| true` -- left every test green while CI carried on past
    a failing gate and reported success. That was found green in review.

    So the runner is executed a second time with the stub failing the main
    `ruff check` lane exactly as a real violation would. The runner must stop
    there: no command after that lane may appear in the trace.
    """

    recorded, returncode = _record_runner_python_invocations(fail_main_check=True)

    # Locate the failing call the runner is required to stop at. It is the
    # `ruff check` lane that covers all of `tests/`, which is what the stub
    # makes fail.
    failing = [
        index
        for index, arguments in enumerate(recorded)
        if arguments[:3] == ["-m", "ruff", "check"] and "tests" in arguments
    ]
    assert failing, (
        "the main `ruff check` lane was never executed, so its failure could not stop the runner"
    )
    stop = failing[0]
    assert _BENCHMARK in recorded[stop], (
        "the failing lane executed was not the one carrying the benchmark"
    )

    # Everything after the failing lane is proof the failure was swallowed.
    trailing = [arguments for arguments in recorded[stop + 1 :] if arguments[:1] not in (["-c"],)]
    assert not trailing, (
        "the local runner continued past a failing `ruff check` lane "
        f"(exit status was ignored); it then ran {trailing[:3]}"
    )

    # Stopping the trace is not sufficient on its own: `set +e` followed by an
    # explicit `exit 0` also ends the trace, while reporting success to
    # whatever invoked the runner. The failure has to reach the caller.
    assert returncode != 0, (
        "the local runner exited 0 after its `ruff check` lane failed; the "
        "failure never reached the caller"
    )


def _workflow_lint_step() -> tuple[list[str], list[str]]:
    """Return the lint/format step's `run:` body and its sibling keys.

    The body is de-indented so it can be inspected as the shell script GitHub
    will actually execute, which is what distinguishes a command that runs
    from one that is merely present in the file.
    """

    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    step = next(
        index
        for index, line in enumerate(lines)
        if line.strip() == "- name: Check packaging and gate lint/format"
    )
    keys: list[str] = []
    body: list[str] = []
    run_at = None
    for index in range(step + 1, len(lines)):
        line = lines[index]
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if stripped.startswith("- ") and indent <= 6:
            break
        if not stripped:
            continue
        if stripped.startswith("run:"):
            run_at = index
            block_indent = indent
            continue
        if run_at is None:
            keys.append(stripped)
            continue
        if indent <= block_indent:
            run_at = None
            keys.append(stripped)
            continue
        body.append(line)
    assert run_at is not None or body, "the lint/format step has no run block"
    return body, keys


def test_workflow_lint_step_has_no_disabled_shell_control_flow() -> None:
    """The hosted run block must execute its Ruff lanes unconditionally.

    Asserting that the step carries no `if:` is not enough: GitHub runs the
    `run:` body through bash, so wrapping the commands in `if false; then ...
    fi` inside the block skips them while every YAML key still looks right.
    That was found green in review, and the same trick that was already caught
    in the local runner had no hosted counterpart.

    The block is inspected as shell text rather than executed, because running
    a hosted step is not something a unit test can do. Only the constructs
    that can skip or neuter a command are rejected; ordinary shell -- pipes,
    redirections, comments -- is left alone.
    """

    body, _keys = _workflow_lint_step()
    offenders: list[str] = []
    for line in body:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped in ("fi", "done", "esac"):
            # A closing keyword is fine on its own; what matters is that a
            # matching conditional opener was not seen above.
            continue
        head = stripped.split(" ", 1)[0] if stripped else ""
        if head in ("if", "unless", "elif", "while", "until"):
            offenders.append(stripped)
            continue
        if stripped.startswith(("!", "time ", "exec ")) and "ruff" in stripped:
            offenders.append(stripped)
            continue
        # `cmd || true` / `cmd &` / `cmd ; true` disarm a failure's effect.
        if "ruff" in stripped and (stripped.endswith(("|| true", "&")) or " || true" in stripped):
            offenders.append(stripped)
    assert not offenders, (
        "the hosted lint/format run block wraps its Ruff lanes in shell "
        f"constructs that skip or disarm them: {offenders}"
    )


def test_workflow_lint_step_cannot_tolerate_a_failure() -> None:
    """A Ruff failure in hosted CI must fail the step and the job.

    `continue-on-error: true` on the step, or `|| true` in its body, keeps a
    real lint violation from failing the workflow while every other assertion
    still passes. Both were found green in review.

    `continue-on-error` is also refused at job level: a job-level `true` is
    what makes the whole `unit-and-manifest` job advisory, and a step-level one
    cannot be relied on to be the only spelling.
    """

    workflow = WORKFLOW.read_text(encoding="utf-8")
    _body, keys = _workflow_lint_step()

    assert not [key for key in keys if key.startswith("continue-on-error")], (
        f"the lint/format step tolerates its own failure: {keys}"
    )
    assert "continue-on-error" not in workflow, (
        "`continue-on-error` appears in the workflow; the lint/format job would "
        "report success despite a failing Ruff lane"
    )
    assert "fail-fast: false" not in workflow, (
        "`fail-fast: false` would let a matrix sibling mask this job's failure"
    )


def test_workflow_runs_the_lint_step_unconditionally() -> None:
    """The hosted lint/format step must not be skippable.

    The execution trace covers the *local* runner only, so the hosted path had
    no equivalent: adding `if: ${{ false }}` to the lint/format step left all
    policy tests green while GitHub Actions skipped the job that runs both
    main lanes. That was found green in review.

    GitHub's step-level `if` replaces the implicit `success()`, so the step has
    to carry no conditional of its own. The single `if: always()` in this
    workflow belongs to the artifact upload and is asserted as such, so a new
    conditional on the lint step cannot hide among them.
    """

    workflow = WORKFLOW.read_text(encoding="utf-8")
    lines = workflow.splitlines()

    # Locate the step that runs the main lanes.
    step = None
    for index, line in enumerate(lines):
        if line.strip() == "- name: Check packaging and gate lint/format":
            step = index
            break
    assert step is not None, "the lint/format step is missing from the workflow"

    # Its keys run from the step's `- name:` line to the next step or job.
    indent = len(lines[step]) - len(lines[step].lstrip())
    conditional = None
    for line in lines[step + 1 :]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        current = len(line) - len(line.lstrip())
        if current <= indent and stripped.startswith("-"):
            break
        if stripped.startswith("if:"):
            conditional = stripped
    assert conditional is None, (
        f"the lint/format step is conditional ({conditional}); GitHub would "
        "replace the implicit success() and could skip both main lanes"
    )

    # The job that contains it must run for this repository's own pushes.
    assert (
        "if: ${{ github.event.repository.private == false && "
        "github.event.repository.visibility == 'public' }}"
    ) in workflow, (
        "the unit-and-manifest job lost its explicit visibility condition; the "
        "job is expected to run for this public repository"
    )

    # And the only `if:` left in the workflow is the artifact upload's.
    conditionals = [line.strip() for line in lines if line.strip().startswith("if:")]
    expected = (
        "if: ${{ github.event.repository.private == false && "
        "github.event.repository.visibility == 'public' }}"
    )
    assert conditionals == [expected, "if: always()"], (
        f"unexpected step/job conditions in the workflow: {conditionals}"
    )
