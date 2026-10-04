from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ._local_ci_policy_paths import ROOT, RUNNER, WORKFLOW
from ._local_ci_policy_ruff_effective import _BENCHMARK, _lane_covers
from ._local_ci_policy_ruff_selection import _main_lane, _ruff_invocations


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
    # Since #259 the two main lanes take the `tests` directory itself, and
    # since #567 they take the `scripts` directory itself too. So this floor
    # names those two directory tokens plus the narrower runtime/link lane's
    # explicit `tests/` entries, which stay enumerated. Individual main-lane
    # `scripts/` files are intentionally absent, exactly as individual
    # main-lane test files are: their coverage is enforced by measurement in
    # `test_main_ruff_lanes_cover_every_test_file` and
    # `test_main_ruff_lanes_cover_every_script_file` above, which fail if a
    # directory token is dropped or reintroduced as a partial list. Keeping the
    # literal per-file `scripts/` entries here would have re-pinned the
    # enumerated boundary that #567 exists to remove.
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
        "scripts",
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
        assert any(_lane_covers(tuple(arguments), _BENCHMARK) for arguments in matches), (
            f"the executed `ruff {subcommand}` lane does not cover {_BENCHMARK}"
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
    assert _lane_covers(tuple(recorded[stop]), _BENCHMARK), (
        "the failing lane executed was not the one covering the benchmark"
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
