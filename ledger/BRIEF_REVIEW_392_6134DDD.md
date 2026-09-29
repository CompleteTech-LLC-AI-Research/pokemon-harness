# Independent review brief — #392 merge candidate

## What to review

**Exact SHA: `6134dddf17609e7703f19afcfb3484d4c171f308`**

Branch `fix/375-local-carrier-stale` in `/workspace/poke-harness/FIX-375r4`.

This is a merge commit. Parents are `2031d7d` (the #392 local repair chain) and
`6b72bf6` (current `origin/master`). Ancestry is preserved — no rebase, no
force-push, both tips remain reachable.

Reply with one of:

- `APPROVE 6134dddf17609e7703f19afcfb3484d4c171f308`
- `REQUEST CHANGES` with the specific finding and file/line

An approval applies to **that full SHA only**. Any further change to the tree
invalidates it.

## Why a merge rather than a rebase

The 12 local #392 commits were stacked on `ed9d9b0`, 12 commits behind master.
#367 and #400 rewrote both files #392 edits. A rebase resolves it but rewrites
the commits, and pushing a rebase would require a force-push. So the candidate
merges `6b72bf6` forward instead.

## Conflict resolution and how to check it

8 content conflicts, all in `tests/_timed_menu_milestone_sentinel_support.py`.
`tests/test_timed_menu_milestone_sentinels.py` auto-merged.

The strongest evidence the resolutions are right is independently checkable:

```bash
cd /workspace/poke-harness/.scratch/rebase392
git show HEAD:tests/test_timed_menu_milestone_sentinels.py > /tmp/rebase_tests.py
cd /workspace/poke-harness/FIX-375r4
git show 6134ddd:tests/test_timed_menu_milestone_sentinels.py | diff - /tmp/rebase_tests.py
```

That diff is empty. Git's 3-way merge and a hand-run rebase agree exactly on the
test surface, so the rebase tree was used as the resolution reference and
adopted verbatim for the support file.

Each resolution preserves one rule:

| Source | Rule |
|---|---|
| #367 | tied-store ambiguity, `_latest_write_in_block`, `_readable_store_value` |
| #375 | conditional-store-supersedes-carrier guard (declines — safe direction) |
| #400 | `_is_after_control_transfer` intra-block reachability |
| #392 | loop-target collapse, `_contains_any`, carrier guards |

`origin/master`'s `_store_is_settled_before` behaviour is retained for the
`decidable` filter.

## The one substantive logic change

With the conflicts resolved, the suite had exactly one failure:

```text
test_a_conditional_store_superseding_a_local_carrier_is_declined[
  a loop body rebinding the target to a manager
]
```

```python
import os as cs
if flag:
    for cs in (None,):
        cs = nullcontext()
with cs:
    assert x != 1
```

CPython `outer(1, True, None)` raises `AssertionError`, so the assert is LIVE.

**Root cause.** `_collapse_loop_targets_into_bodies` correctly retires the `for`
target — the body runs after the target on every pass, so the body store is what
survives — leaving one conditional candidate. But #367's tie-break, added by
master *after* the line that pre-rebase `2031d7d` used, recomputed its own set
from raw `entries`:

```python
latest = max(orders[id(entry[0])] for entry in entries)      # `entries`
tied = [entry for entry in entries if orders[id(entry[0])] == latest]
```

`_binding_order` keys a store by the top-level statement containing it, so the
target and the body store both carry order 1 and tie. `tied` regained the retired
target, the count returned to 2, and `_latest_write_in_block` — which cannot
separate two stores inside one loop — returned `None`. That produced
`AMBIGUOUS_SUPPRESSOR`, which `_is_suppressing_with` reads as a *possible*
suppressor, retiring a header CPython really enters.

This is merge-introduced, not a pre-existing #392 bug: the non-tie branch had
already been repaired to use `candidates`; the tie branch had not.

**Fix.** The tie-break now takes its maximum over the surviving candidate set,
and the no-tie branch reuses that set instead of recomputing it. Both branches
answer from one set — the invariant the collapse comment already claimed.

Also removed one stray `    #` the 3-way merge left behind (`PLR2044`).

## Validation already run on this exact SHA

```bash
cd /workspace/poke-harness/FIX-375r4
.venv/bin/python -m pytest tests/test_timed_menu_milestone_sentinels.py
# 443 passed          (408 before the merge; master's #367/#400 rows are included)

.venv/bin/python -m ruff format --check tests/_timed_menu_milestone_sentinel_support.py \
    tests/test_timed_menu_milestone_sentinels.py
.venv/bin/python -m ruff check tests/_timed_menu_milestone_sentinel_support.py \
    tests/test_timed_menu_milestone_sentinels.py
# clean
```

20-fixture differential against CPython `outer(2, True)`:

| Revision | Correct | False-DEAD | False-LIVE |
|---|---|---|---|
| master `6b72bf6` | 17/20 | 3 | 0 |
| PR #392 `fee41bf` (pushed head) | 15/20 | 1 | 4 |
| local `31e3b22` | 18/20 | 1 | 1 |
| local `2031d7d` | 18/20 | 1 | 1 |
| **this candidate** | **18/20** | **1** | **1** |

The candidate matches the best local chain and introduces no regression. The one
residual false-LIVE is fixture 19 (`if`/`else` both arms enterable), identical to
`2031d7d` and `31e3b22` — pre-existing and unchanged, not introduced here.

The wider `tests/` run shows 20 failures, all in `test_probe_*` spawn tests. These
are **pre-existing and environmental** — `/dev/shm` is read-only here, so
`tempfile` raises `OSError: [Errno 30]`. Verified identical on a clean `6b72bf6`
worktree:

```bash
git -C /workspace/poke-harness/FIX-375r4 worktree add /tmp/masterchk 6b72bf6
cd /tmp/masterchk && /workspace/poke-harness/FIX-375r4/.venv/bin/python \
    -m pytest tests/test_probe_owner_phases.py -q   # same failures
```

## Known scope limits — please judge these explicitly

1. **#396 and #391 are not superseded by this.** They are competing
   implementations of the same #388 rule. Nothing here closes them, and the
   merge is only justified if this candidate is the complete fix.
2. **Fixture 19 is still wrong** (false-LIVE), unchanged from the pre-merge
   chain. Out of scope for this merge, but it is a real gap.
3. **#388 stays open** until every acceptance criterion is terminal.

## Explicitly not done

No push, no merge, no PR status change, no issue closure. Those all wait on the
approval above.
