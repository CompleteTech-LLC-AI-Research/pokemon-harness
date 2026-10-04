from __future__ import annotations

import json
import re
import subprocess
import sys

from ._local_ci_policy_paths import ROOT, RUNNER
from ._local_ci_policy_ruff_effective import _comment_tokens, _diagnostics
from ._local_ci_policy_ruff_selection import (
    _main_lane,
    _ruff_invocations,
    _ruff_lint_resolved_files,
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
    r"[ \t]*(?P<trailing>#+.*|[^:#,][^:]*)?[ \t]*$"
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
            # A `noqa` followed by anything other than a colon-separated code
            # list is a *blanket* opt-out, not a selective one. Measured:
            # `noqa F401`, `noqa whatever` and `noqa F401 F841 E501` each
            # silence the entire file including an unrelated F821, while
            # `noqa :`, `noqa, F401` and `noqa:` disable nothing. Treating a
            # stray trailing token as codes would hand out the one thing this
            # row exists to refuse, so the codes count only when the colon
            # form actually parsed.
            #
            # Trailing text *after* a parsed code list is not blanket, and is
            # not treated as one: measured, `noqa: F401  # why` and
            # `noqa: F401 E501` both still suppress F401 and still report an
            # unrelated F821. Round-3 review flagged this branch, and an
            # earlier version wrongly reclassified codes-plus-trailing as
            # blanket. It is scoped in Ruff, so it is scoped here; no current
            # lane file uses that spelling, so nothing depends on it today,
            # but over-refusing a legitimately scoped directive would block a
            # valid file.
            parsed = lint.group("codes") or ""
            codes = (
                tuple(code.strip().upper() for code in parsed.split(",") if code.strip())
                if parsed
                else ()
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
    # A `noqa` followed by a space-separated token rather than a colon is a
    # blanket opt-out: measured, it silences unrelated codes too, so Ruff is
    # reading it as "suppress everything", not as a code list.
    ("trailing_space_token", "# ruff: noqa F401", True),
    ("trailing_space_word", "# ruff: noqa  trailing", True),
    ("trailing_space_codes", "# ruff: noqa F401 F841 E501", True),
    ("trailing_space_semicolon", "# ruff: noqa F401;", True),
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
    ("colon_then_space", "# ruff: noqa :", False),
    ("comma_space_code", "# ruff: noqa, F401", False),
    ("colon_eol", "# ruff: noqa:", False),
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
    # A parsed code list followed by trailing text. Measured: these still
    # suppress the code they name and still report an unrelated F821, so they
    # are scoped, not blanket. This is the branch round-3 review asked to have
    # asserted rather than reasoned about.
    (
        "selective_then_comment",
        "# ruff: noqa: F401  # why",
        _PROBE_UNDEFINED_AND_UNUSED,
        frozenset({"F401"}),
    ),
    (
        "selective_then_code",
        "# ruff: noqa: F401 E501",
        _PROBE_UNDEFINED_AND_UNUSED,
        frozenset({"F401"}),
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

    That strictness is deliberate and it does bite: a code suppressed
    independently, say by a line-level `noqa` on the offending line, would stay
    absent with the file-level directive removed and be reported as stale. All
    34 current allowances satisfy the strict form (measured), so nothing trips
    today, and a future file that leans on a redundant directive should be made
    to justify itself rather than inherit the exemption.

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
