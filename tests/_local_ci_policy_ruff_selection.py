from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ._local_ci_policy_paths import ROOT, RUNNER, WORKFLOW

# The one script deliberately held outside the `scripts/` lint+format boundary.
# `release-evidence/fixture-manifest.json` records this tool's SHA-1 by value in
# each boundary row's `runtime_identity`, and
# `scripts/merge_fixture_manifest_rows._verify_producer_revision` recomputes that
# hash from the committed bytes and refuses any row that no longer matches.
# Reformatting it would invalidate the only record of which tool captured the
# boundary fixtures, and repairing that requires re-capturing them from a real
# ROM, so it is pinned here until that re-capture is possible.
_PRODUCER_PINNED_BY_MANIFEST = "scripts/produce_battle_state_fixtures.py"


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

    # A `str` here is unpacked by `*paths` into one argument per character, so
    # Ruff resolves nothing and returns an empty set. That fails silently: the
    # caller sees "no files are covered" rather than "you passed the wrong
    # shape", which is how `_ruff_lint_resolved_files("scripts")` survived two
    # review rounds while quietly measuring nothing.
    if isinstance(paths, str):
        raise TypeError(
            "paths must be a tuple of path tokens, not a bare string: "
            f"pass ({paths!r},), not {paths!r}"
        )

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


def test_main_ruff_lanes_cover_every_script_file() -> None:
    """Both main lanes must take the whole `scripts/` directory, not a subset.

    Issue #567 was filed because the lanes enumerated `scripts/` file by file:
    on master 51 of 114 scripts were listed and 63 were not, so a new script
    shipped outside the lint boundary until someone added it by hand. PR #564
    landed `scripts/benchmark_matrix_concurrency.py` -- 1409 lines -- that way.

    Assert the directory token is present, and that no enumerated
    `scripts/...` entry survives alongside it. A leftover entry would let a
    later edit shrink the effective set back toward the partial list without
    any other row noticing, which is the same failure mode
    `test_main_ruff_lanes_cover_every_test_file` pins for `tests/`.
    """

    invocations = _ruff_invocations(RUNNER.read_text(encoding="utf-8"))
    workflow_invocations = _ruff_invocations(WORKFLOW.read_text(encoding="utf-8"))

    for subcommand in ("check", "format"):
        for source, lanes in (("runner", invocations), ("workflow", workflow_invocations)):
            lane = _main_lane(lanes, subcommand)
            assert "scripts" in lane, (
                f"{source} `ruff {subcommand}` lane must pass the `scripts` "
                f"directory so a new script cannot escape the gate; got: {lane}"
            )
            assert not [token for token in lane if token.startswith("scripts/")], (
                f"{source} `ruff {subcommand}` lane still enumerates individual "
                f"scripts; the directory token must be the only `scripts` reference"
            )
            assert lane.count("scripts") == 1, (
                f"{source} `ruff {subcommand}` lane must name the scripts "
                f"directory exactly once, got {lane.count('scripts')}"
            )


def test_main_ruff_lane_scripts_directory_covers_every_script_file_on_disk() -> None:
    """The `scripts` directory token must reach every script except the pinned one.

    Asserting the token is present is necessary but not sufficient, exactly as
    for `tests/`: a future `extend-exclude` entry could silently drop scripts
    from the glob while the token stayed present. Resolve it the way Ruff does
    and require the covered set to be the whole tree minus exactly the one file
    the fixture-provenance guard pins, so a second silent exclusion fails here.
    """

    scripts_root = ROOT / "scripts"
    on_disk = {
        path.relative_to(ROOT).as_posix() for path in scripts_root.rglob("*.py") if path.is_file()
    }
    assert on_disk, "no script files found on disk"
    assert scripts_root.is_dir(), "the `scripts` directory the lanes pass must exist"

    covered = _ruff_lint_resolved_files(("scripts",), tree="scripts")
    uncovered = on_disk - covered
    assert uncovered == {_PRODUCER_PINNED_BY_MANIFEST}, (
        "scripts/ files are excluded from the Ruff lanes beyond the one pinned "
        f"by the fixture manifest: {sorted(uncovered)} "
        f"(extend-exclude={_ruff_excluded_patterns()!r})"
    )
    unexpected = covered - on_disk
    assert not unexpected, f"Ruff resolved unexpected scripts/ files: {sorted(unexpected)[:5]}"


def test_excluded_fixture_producer_still_matches_the_manifest_sha1() -> None:
    """The one script held outside the lint boundary must not drift.

    `scripts/produce_battle_state_fixtures.py` is excluded from the `scripts/`
    lanes because reformatting it changes the SHA-1 that
    `release-evidence/fixture-manifest.json` records as the capture tool's
    identity. Excluding it also removes the ordinary signal that it was
    modified, so this row re-establishes that signal independently: it reads
    the recorded hashes straight out of the manifest and compares them with the
    committed bytes.

    If this ever fails, the correct repair is to re-capture the boundary
    fixtures from a real ROM with the current producer and update the manifest
    -- not to relax the check or hand-edit the recorded hash.
    """

    import hashlib
    import re

    manifest = (ROOT / "release-evidence" / "fixture-manifest.json").read_text(encoding="utf-8")
    recorded = {
        match.group("path"): match.group("sha1")
        for match in re.finditer(
            r"producer (?P<path>[\w./-]+\.py) SHA-1 (?P<sha1>[0-9a-f]{40})", manifest
        )
    }
    assert recorded, "no producer SHA-1 recorded in the fixture manifest"
    unpinned = set(recorded) - {_PRODUCER_PINNED_BY_MANIFEST}
    assert set(recorded) == {_PRODUCER_PINNED_BY_MANIFEST}, (
        f"the fixture manifest names a producer this row does not pin: {sorted(unpinned)}"
    )

    producer = ROOT / _PRODUCER_PINNED_BY_MANIFEST
    assert producer.is_file(), f"pinned producer is missing: {_PRODUCER_PINNED_BY_MANIFEST}"
    actual = hashlib.sha1(producer.read_bytes()).hexdigest()
    assert actual == recorded[_PRODUCER_PINNED_BY_MANIFEST], (
        f"{_PRODUCER_PINNED_BY_MANIFEST} no longer matches the SHA-1 the fixture "
        f"manifest records ({recorded[_PRODUCER_PINNED_BY_MANIFEST]} -> {actual}); "
        "re-capture the boundary fixtures with the committed producer rather "
        "than editing the recorded hash"
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
