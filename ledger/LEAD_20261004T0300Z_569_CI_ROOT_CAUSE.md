# #569's CI failure: root cause established and fixed (`25607fc`)

## The failure is this PR's fault, not inherited

`gh pr checks 569` reports
`Native build and complete unit tier (Python 3.12): fail`. Run `37148200858`
at head `11eb719`, step 6:

    FAIL  unit  total=8314 passed=8313 failed=1 skipped=0
    FAILED tests/test_stepping_loop_profile.py::test_probe_module_and_its_tier_are_part_of_the_ci_contract
      AssertionError: assert 0 >= 2
      +  where 0 = <built-in method count of str>('scripts/stepping_loop_profile.py')
      tests/test_stepping_loop_profile.py:519

That row counts literal occurrences of `scripts/stepping_loop_profile.py` in the
workflow and the runner. Replacing the 51-entry `scripts/` list with the
`scripts` token means the probe's path is no longer written out anywhere, so the
count is 0 while the coverage is still real. Step 2 is what breaks it.

This supersedes the earlier retraction, which correctly withdrew the *format-lane*
claim but wrongly left the failure unattributed. The format lane is green on this
head (`ruff format --check scripts tests` -> 448 files already formatted); the
failure is a stale coverage assertion elsewhere in the suite.

## Why the old row could not have caught this

Counting occurrences of a path is the right instrument only while the lane
enumerates paths. Under a directory token it can agree with reality by accident
and disagree for a real reason, with no way to tell the two apart. The repaired
row asks Ruff what the token resolves.

## Fix landed on the PR branch

`25607fc` on `fix/567-scripts-dir-lane`, ported from the already-verified
`adb6969` on `lead/src-dir-lane`:

    pytest tests/test_stepping_loop_profile.py                 -> 26 passed
    pytest tests/test_local_ci_policy.py + stepping profile    -> 39 passed
    ruff check scripts tests --no-cache                        -> All checks passed!
    ruff format --check scripts tests                          -> 448 files already formatted

Teeth, each mutation restored after measuring:

    drop the `scripts` token from the runner check lane
      -> row fails
    exclude scripts/stepping_loop_profile.py from the lanes
      -> row fails, naming extend-exclude=('vendor/pyboy-src',)

Pushed; the PR head is now `25607fc`. CI is the confirming evidence and has not
yet reported on it.

## Still not mergeable

Independent review is outstanding for every open PR, and #569 has not been
reviewed by anyone other than its author. Release remains **PARTIAL**.

## Confirmed by CI

    run 37157467035, head 25607fc
    Native build and complete unit tier (Python 3.12): pass  (8m1s)
      PASS  unit  total=8314 passed=8314 failed=0 skipped=0 xfailed=0 xpassed=0 errors=0

The same tier that reported `total=8314 passed=8313 failed=1` at `11eb719` is now
`total=8314 passed=8314 failed=0`. One test changed state and it is the one this
commit repairs; nothing else moved.

#569 is therefore green on its own head. It is still **not merged**: independent
review remains outstanding, and #568 must land with it (the producer exclusion
lives in #569).
