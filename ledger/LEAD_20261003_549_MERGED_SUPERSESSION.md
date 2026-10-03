# #549 repair: independent review completed and merged to master

This supersedes the "not merged anyway" conclusion recorded in
`LEAD_20261003_549_REPAIR_AND_REVIEW_DELIVERY.md`, which was written *before* the
independent review ran. That file is left in place as the honest record of the
state at its authoring time; this file records what actually happened next.

## Why the repair was needed

PR #549's scope -- pinning the `--package` help wording the guard row is named
for -- was genuinely missing from master, so it was not superseded.

The original head `33cc0e52` was reviewed **REQUEST CHANGES** because its
assertion was a raw substring test for `"not adds to"`. `argparse` wraps help to
the terminal width and wraps *inside* that phrase, so at `COLUMNS=40` a correct
tree renders the phrase across four lines and the row fails on the wrapping
rather than on the wording.

## Repair

The repair normalises whitespace before asserting, which makes the test
independent of terminal width while still failing on the misleading pre-#546
wording:

    help_text = " ".join(capsys.readouterr().out.split())

    approved head : a35af956fbef72e0faad96732714d6127879b228
    branch        : lead/549-help-wording-on-master
    files changed : tests/test_import_origin_guard.py

## Independent review

Reviewer: `codex1`, dispatched separately from the author. Report preserved at
`.scratch/r558s/out549e.txt`.

    VERDICT: APPROVE
    WIDTHS_TESTED: 200, 100, 80, 60, 40, 30
    CORRECT_WORDING_RESULTS: 200: PASS, 100: PASS, 80: PASS, 60: PASS, 40: PASS, 30: PASS
    MUTANT_RESULTS: 200: FAIL, 100: FAIL, 80: FAIL, 60: FAIL, 40: FAIL, 30: FAIL
    MUTANT_RESTORED: yes
    COLLECTED_COUNT: 134
    FILES_CHANGED_BY_COMMIT: tests/test_import_origin_guard.py
    FINDINGS: none

The mutant column is the load-bearing part: on the pre-#546 wording every tested
width **fails**, so the row still discriminates. A test that only passes on the
correct tree proves nothing.

## Merge

    merge commit : 00daf280b49082cf644a3d126d634351656f4a12
    parents      : 7a94a19fb75f627917a6a3cc8c364e701ea5e3ea (master)
                   a35af956fbef72e0faad96732714d6127879b228 (repair head)
    message      : Merge #549's scope: pin the --package help wording, wrap-safely

## Verification run on the merge tree

- guard suite: `134 passed`, collected count unchanged at `134`
- ruff clean
- acceptance-criterion repaired variant: rc `0`
- acceptance criterion mis-pointed variant: rc `1` (must fail)
- correct help passes at widths `200`, `60`, `40`, `30`
- pre-#546 mutant fails at widths `200`, `60`, `40`, `30`
- real trunk guard: `status: PASS`, rc `0`
- `pokered_harness` -> `/workspace/poke-harness/pokemon/src/pokered_harness/__init__.py`
- `pyboy` -> `/workspace/poke-harness/pokemon/vendor/pyboy-src/pyboy/__init__.py`

The trunk guard resolving to the main checkout matters: a green guard run from an
interpreter bound to a different worktree would describe another tree, and this
guard refuses to run in that state by design.

## Preserved

The merge left `0` tracked modifications in the main checkout. Untracked entries
and the several hundred existing worktrees are untouched; no reset, clean, broad
stage, or force-push was performed.

Release status remains **PARTIAL**.
