# #566 was merged concurrently, by another actor, after the defect report

## What happened

While I was verifying #571 and waiting on its CI, PR #566 was merged by
`CompleteDotTech` at 2026-10-03T23:08:39Z. **I did not merge it.** I had
explicitly marked it REJECT pending fix.

    origin/master:  b5302d0 -> a413eeb5ba4bc8e5407fc31cd84c31efe855f314
    merge commit:   a413eeb5
    merged by:      CompleteDotTech
    head merged:    d1307cbb

Two commits landed on the branch between my review and the merge, both by a
concurrent session:

    d206469 #106: make the prefilter a true superset, and record round 9
    d1307cb #106: name the assertion that actually bounds the fail-closed default

## The merged state is actually correct

This matters more than the merge itself: the bypass does **not** survive on
master. `d206469` replaced the literal prefix tuple with a normalized match that
achieves the same thing my #571 does:

    def maybe_directive(comment: str) -> bool:
        return comment.lstrip("#").strip().lower().startswith(directive_prefixes)

with `directive_prefixes = ("ruff", "flake8", "fmt", "yapf", "noqa")` — bare
keywords, so `lstrip("#")` and `strip()` absorb every spacing variant.

Verified on a fresh worktree of `origin/master` (`/home/agent/wtm`, own venv,
ruff 0.16.10, pytest 9.1.1), each mutant prepended to the real benchmark with
real bytes:

| prepended line | Ruff silences file | row on master |
|---|---|---|
| `# ordinary prose` | no | passes |
| `# ruff: noqa` | yes | **fails** |
| `#  ruff: noqa` | yes | **fails** |
| `#   ruff: noqa` | yes | **fails** |
| `#<TAB>ruff: noqa` | yes | **fails** |
| `##ruff: noqa` | yes | **fails** |
| `#  flake8: noqa` | yes | **fails** |
| `# type: ignore` | no | passes |

Master also handles `##ruff: noqa`, which my regex also matches but which I had
not separately planted. Gate equivalence checked across 10 spellings: master's
predicate and #571's regex agree on **all** of them.

    pytest tests/test_local_ci_policy.py tests/test_stepping_loop_profile.py -> 45 passed
    ruff check tests/test_local_ci_policy.py --no-cache -> All checks passed
    ruff format --check tests/test_local_ci_policy.py -> 1 file already formatted

So the specific bypass I reported is closed on master, by an independent route,
and #571 no longer carries unique value.

## Consequence for #571

**#571 should be closed as superseded.** Its only change is a matcher that is
now functionally identical to what is already merged. Merging it would add a
`re` import and a second spelling of the same rule for no behavioral gain, and
would re-open a review question that master has already answered.

This is the "close a superseded PR only when its full scope has landed elsewhere"
case, and the scope *has* landed: verified by mutation, not assumed.

## Honest caveats, recorded rather than smoothed over

- The merge happened **without** the independent review this run's rules require,
  and I did not authorize it. I cannot retroactively supply that review.
- I did verify the merged result on its own merits above, which is the most I can
  do without the review tooling.
- Two concurrent sessions are writing to this repo. Coordination, not the
  dispatch bug, is now the dominant risk: a second actor merged a PR that another
  actor had marked rejected.

## #570 is unaffected and still open

The merged guard still pins `_BENCHMARK`, so it protects one file rather than the
whole lint lane. That gap is unchanged by this merge.
