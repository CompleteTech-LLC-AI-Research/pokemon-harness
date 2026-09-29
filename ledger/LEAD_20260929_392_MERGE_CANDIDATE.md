# Lead note — #392 merge-based candidate, 2026-09-29

## Why a merge, not the rebase

The exploratory branch `rebase/392-onto-6b72bf6` (`392b576`) holds all 12 local
#392 commits replayed onto `6b72bf6`. It is correct but unusable: replaying
rewrites ancestry, and pushing it would require a force-push, which is forbidden.

So the candidate is built on the original branch `fix/375-local-carrier-stale`
(`2031d7d`) by merging `origin/master` (`6b72bf6`) with `--no-ff`, preserving
remote ancestry. Conflicts are resolved with the exploratory rebase used only as
a *reference* for the intended combined semantics.

## Conflict resolution

8 content conflicts, all in `tests/_timed_menu_milestone_sentinel_support.py`.
`tests/test_timed_menu_milestone_sentinels.py` auto-merged.

The auto-merged test file is **byte-identical** to the rebase tree's version
(`diff` = 0 lines). That is independent confirmation the rebase's replayed commits
and git's 3-way merge agree on the test surface, so the rebase tree is a sound
resolution reference and was adopted verbatim for the support file.

Resolutions preserve, together:

| Source | Rule kept |
|---|---|
| #367 | tied-store ambiguity, `_latest_write_in_block`, `_readable_store_value` |
| #375 | conditional-store-supersedes-carrier guard (declines, safe direction) |
| #400 | `_is_after_control_transfer` intra-block reachability |
| #392 | loop-target collapse into bodies, `_contains_any`, carrier guards |

`origin/master`'s `_store_is_settled_before` behaviour is kept for `decidable`.

## One real defect found and fixed in the merged tree

With the conflict resolved, the sentinel suite had exactly one failure:

```text
test_a_conditional_store_superseding_a_local_carrier_is_declined[
  a loop body rebinding the target to a manager
]
```

Fixture (CPython `outer(1, True, None)` raises, so the assert is LIVE):

```python
import os as cs
if flag:
    for cs in (None,):
        cs = nullcontext()
with cs:
    assert x != 1
```

### Root cause

`_collapse_loop_targets_into_bodies` correctly retires the `for` target — the
body's store is what survives — leaving **one** conditional candidate. But the
#367 tie-break then recomputed its own candidate set from raw `entries`:

```python
latest = max(orders[id(entry[0])] for entry in entries)      # <- `entries`
tied = [entry for entry in entries if orders[id(entry[0])] == latest]
```

The `for` target and the body store share order 1 (both are ordered by the
top-level `for` containing them), so `tied` regained the retired target, the tie
count went back to 2, and `_latest_write_in_block` returned `None` — two stores
inside one loop, neither settled ahead of the other. That returned
`AMBIGUOUS_SUPPRESSOR`, which `_is_suppressing_with` reads as a *possible*
suppressor, retiring a header CPython really enters.

This is a **merge-introduced** regression, not a pre-existing #392 bug: the
pre-rebase `2031d7d` resolved it with `candidates = competing if competing else
entries`, and the #367 tie-break that `6b72bf6` added sat *after* that line, so it
re-derived the set from `entries`. The non-tie branch had already been repaired to
use `candidates`; the tie branch had not.

### Fix

The tie-break now takes its maximum over the same surviving candidate set the
collapse produced, and the no-tie branch reuses that set rather than recomputing
it. Both branches then answer from one set, which is the invariant the collapse
comment already claimed.

Also removed one stray `    #` left by the 3-way merge (`PLR2044`).

## Validation

- `tests/test_timed_menu_milestone_sentinels.py`: **408 collected, exit 0**, 0 failures.
- Ruff `format --check` and `check`: clean.
- 20-fixture differential vs CPython `outer(2, True)`:

  | Revision | Correct | False-DEAD | False-LIVE |
  |---|---|---|---|
  | master `6b72bf6` | 17/20 | 3 | 0 |
  | PR #392 `fee41bf` | 15/20 | 1 | 4 |
  | local `2031d7d` | 18/20 | 1 | 1 |
  | **merge candidate** | **18/20** | **1** | **1** |

  The candidate matches the best local chain and does not regress against it. The
  one residual false-LIVE is fixture 19 (`if/else` both arms enterable), identical
  to `2031d7d` and `31e3b22` — a pre-existing, unchanged gap, not introduced here.

## Status

Not yet approved, not pushed, not merged. A fresh independent review of the exact
merge SHA is required before any push.
