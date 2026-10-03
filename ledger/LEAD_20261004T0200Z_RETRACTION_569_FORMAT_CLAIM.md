# Retraction: the `full_to_brock.py` and E402 findings against #569 were wrong

## What I claimed

In `LEAD_20261004T0030Z_567_STACK_VERIFICATION.md`, and in public comments on
#568 and #569, I reported:

1. `scripts/full_to_brock.py` is missed by both #568 and #569 — not excluded,
   clean under `ruff check`, but still needing reformat, so the format lane
   stays red.
2. #569's red `ruff check scripts tests` (163 findings) is pre-existing on
   master (285), the difference being two `E402`s in
   `scripts/_grind_support.py`.

## Both are refuted

Re-measured on a fresh worktree at the same commit, `11eb719`, tree clean:

    ruff format --check scripts tests  -> 448 files already formatted
    ruff check scripts tests           -> All checks passed!

`scripts/full_to_brock.py` was already reformatted by #568
(`git diff --stat origin/master HEAD -- scripts/full_to_brock.py` shows
170 insertions / 130 deletions), which is why it is formatted. I reported the
opposite.

## Cause: a polluted `.ruff_cache`

The measurements that produced the wrong numbers were run in a worktree whose
`.ruff_cache` was being shared with a concurrently running full pytest suite,
which executes Ruff in-process. Interleaved writes left stale entries, and Ruff
reported files as needing reformatting that were already clean — and vice versa
for the check lane.

The tell was visible in the original output and I read past it: the run that
produced "2 files would be reformatted" also reported
"445 files already formatted" for a `scripts`+`tests` pair that is 448 files
now, and a *separate* invocation of the same command minutes later reported
different numbers. Same Ruff (`0.16.10`), same commit, opposite verdict. That
is a cache or environment artifact, never a code finding.

`--no-cache` was not applied to the format lane in those runs; where I did use
it, results were stable and correct.

## Correction

- **#569's lanes are green on its own head.** The earlier "CI agrees the pair is
  not done / #569 FAILURE" reading attributed a real CI failure to this stack
  without establishing its cause. That failure is not reproduced locally at
  this head and is not explained by the code as merged here.
- **#568's `full_to_brock.py` reformat is present and correct**, not missing.
- The AST-identity result for #568 (39 of 40 files AST-identical) and the
  producer SHA-1 result were obtained by methods that do not depend on Ruff's
  cache — `ast.dump` comparison and `sha1sum` — and both re-confirmed here. They
  stand.

## Lesson recorded

Ruff lane verdicts are only evidence when the cache is not shared with a
concurrently running suite. Measure with `--no-cache`, or in a worktree whose
cache nothing else is writing, and treat two runs of the same command on the
same commit disagreeing as an environment fault rather than a finding.

Public comments on #568 and #569 carry a correction pointing here.
