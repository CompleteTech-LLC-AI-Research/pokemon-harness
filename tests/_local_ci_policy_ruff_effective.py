from __future__ import annotations

import io
import json
import subprocess
import sys
import tokenize
from pathlib import Path

from ._local_ci_policy_paths import ROOT, RUNNER, WORKFLOW
from ._local_ci_policy_ruff_selection import _main_lane, _ruff_invocations

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


def _lane_covers(lane: tuple[str, ...], relative: str) -> bool:
    """Return whether a `ruff` lane really lints `relative`, as Ruff sees it.

    A literal membership test answers a narrower question than the one these
    rows are asking. The main lanes used to name every `scripts/` path
    individually; they now pass the `scripts` directory token, so
    `relative in lane` is False even though the lane covers the file exactly as
    before. Asserting the literal there would fail a correct lane and, worse,
    invite someone to "fix" it by re-expanding the path list -- undoing the
    change that stops a new script from shipping unlinted.

    So ask Ruff instead. Each path token is handed to `ruff check` on its own
    and the file is resolved by Ruff's own exclusion rules, which is the same
    question the CI lane answers. A token that is a directory resolves to every
    file beneath it; an explicit file resolves to itself; anything Ruff does
    not resolve cannot be covering the file.
    """

    # The parsed lane starts with `python`, but a vector recorded from the
    # executed runner starts at `-m ruff`. Locate the subcommand rather than
    # assuming a fixed offset, so the same helper serves both shapes.
    subcommand = next(
        (index for index, token in enumerate(lane) if token in {"check", "format"}),
        None,
    )
    assert subcommand is not None, f"{lane!r} carries no ruff subcommand"
    paths = [token for token in lane[subcommand + 1 :] if not token.startswith("-")]
    assert paths, f"the {lane[subcommand]} lane names no paths, so it cannot cover anything"
    for token in paths:
        resolved = subprocess.run(
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "--no-cache",
                "--force-exclude",
                token,
                "--show-files",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if relative in {
            Path(line.strip()).relative_to(ROOT).as_posix()
            for line in resolved.stdout.splitlines()
            if line.strip()
        }:
            return True
    return False


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
        assert _lane_covers(lane, _BENCHMARK), (
            f"{label} lane does not cover {_BENCHMARK}; the benchmark would ship unlinted"
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
