# LEAD — #549 repair at a35af956, and the review that did not arrive

Date: 2026-10-03

```
master : dfd4795a8f5587c04c0e195cc0c2ec8700c16e74
head   : a35af956fbef72e0faad96732714d6127879b228  (lead/549-help-wording-on-master)
file   : tests/test_import_origin_guard.py, +28 lines, one existing row
```

## What landed in the repair

The gap: `test_package_flag_replaces_the_defaults_and_says_so` is named for
asserting that `--help` says `--package` *replaces* the defaults, but on master
it read neither `--help` nor the wording. Reverting master's help string to the
pre-#546 misleading wording leaves the suite **green**; the mutant survives.

The repair appends the missing assertions, matched against whitespace-
normalised text. The first attempt used raw text, and an independent review of
the original PR #549 returned REQUEST CHANGES on exactly that point:

> non-blocking — the literal "not adds to" assertion fails when argparse wraps
> help text; reproduced with COLUMNS=40, argparse split the phrase across lines.

Confirmed independently on master:

```
COLUMNS=40  ...  replaces -- not adds
                 to --
```

so a raw substring test fails on a *correct* tree purely because of terminal
width. `" ".join(out.split())` fixes that without weakening the assertion: the
pre-#546 wording survives normalisation unchanged and is still refused.

## Verified on master dfd4795a + this head

```
guard suite        : 134 passed, 0 failed
collected count    : 134  (unchanged)
ruff               : All checks passed
correct wording    : row passes at COLUMNS 200 / 80 / 40 / 30
mutant (pre-#546)  : row FAILS at COLUMNS 200 / 80 / 40 / 30
AC4 repaired rc 0, mis-pointed rc 1
```

Non-vacuous in both directions at every width tested. Test-only: no guard
logic, tier config, deadline, marker, skip or xfail touched.

## The repair is NOT merged. Independent review has not landed.

Three dispatches were attempted for the repaired head. None produced a verdict.

1. `out549b.txt` — ran ~70 minutes. Did not review this commit. Its working
    directory was `/home/agent/cand534`, another agent's tree at `1712ba3f`
    ("Require an install to vouch for the .pth that activates a finder"), and
    it produced a mutation sweep of interrupt re-raise sites in
    `check_import_origins.py`. Wrong subject entirely. Killed.
2. `out549c.txt` — re-dispatched from a clean directory with explicit scope
    discipline. Ran ~50 minutes, executed real pytest runs in a scratch clone
    (`.scratch/rev549`, at master `40aa7102`, mutating only that clone), but
    emitted no report. Killed.
3. A third dispatch was not issued: a concurrent `run_local_ci.sh` from a
    different agent session (parent PID 128) was saturating the 4 available
    CPUs, and killing it would have destroyed another agent's work.

This is the same class of failure as issue **#489** ("collaboration sub-agent
task delivery is not working"), which is still open. The delivery mechanism,
not the change, is what failed.

## Why this is not merged anyway

The author may not approve their own change, and only the lead merges after
independent approval. The repair has **my** measurements and **no** independent
verdict. It stays on its branch, pushed and ready, unmerged.

## What is independently established about the underlying change

The wrap-sensitivity finding is not merely the reviewer's assertion; it was
reproduced on master directly (`COLUMNS=40` output above) and the repair is
measured non-vacuous in both directions at four widths. What is missing is a
second party's independent confirmation of that, not the evidence itself.

## Housekeeping

Two worktrees and three venvs were created for this and are retained:

```
/workspace/poke-harness/.scratch/fix549        lead/549-help-wording-wrap-safe
/workspace/poke-harness/.scratch/merge549      lead/549-help-wording-on-master
/workspace/poke-harness/.scratch/fix549venv
/workspace/poke-harness/.scratch/merge549venv
```

Main checkout and `merge549` verified clean; no tracked modification anywhere.
