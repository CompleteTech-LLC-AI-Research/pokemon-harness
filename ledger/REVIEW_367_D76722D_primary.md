# Independent round-3 review of `d76722d9ac727ae39c0d947a7f5c35466a72243e`

**Verdict: REQUEST CHANGES.** The reported false-ENFORCED defect is fixed for the three requested shapes, but the new guard causes a fresh false-DEFEATED result for an assert that raises on every input. The repair is too broad: it considers a suppressor that an unconditional store has already overwritten. The 399-test lane does not cover that regression.

## Identity and scope

In the head worktree, `git rev-parse HEAD` printed `d76722d9ac727ae39c0d947a7f5c35466a72243e`; `git rev-parse 4796c641119a4db2a73deef494eb6902beca70a7` printed `4796c641119a4db2a73deef494eb6902beca70a7`. In the base worktree, `git rev-parse HEAD` printed `4796c641119a4db2a73deef494eb6902beca70a7`. The initial and final `git status --porcelain` in head printed nothing. `git diff 4796c64..d76722d` covered four files: the support module, its sentinel test, and two ledger files (403 insertions, 1 deletion). This review uses static AST analysis and real CPython execution only. Release status remains **PARTIAL**; no hosted CI result is inferred.

## Finding 1 — high severity: false DEFEATED from a stale historical suppressor

At `tests/_timed_menu_milestone_sentinel_support.py:1456-1463`, `any(_is_readable_suppressor(entry[1], bound) for entry in entries)` searches **all** prior stores. It includes suppressors older than the latest unconditional store, although the resolver's own `latest` and `competing` calculation correctly treats those older stores as overwritten. Consequently the new branch returns `AMBIGUOUS_SUPPRESSOR` when both possible current values are non-suppressors.

Independent counterexample:

```python
def outer(x, flag):
    import contextlib
    cs = contextlib.suppress(AssertionError)
    cs = contextlib.nullcontext()  # unconditional overwrite
    if flag:
        cs = contextlib.nullcontext()
    with cs:
        assert x != 1
```

With `x=1`, CPython raised `AssertionError` for both `flag=False` and `flag=True`. Head's `_is_enforced` returned **DEFEATED**; base returned **ENFORCED**. This is a regression in positive certification, not a recurrence of the original false-ENFORCED polarity. A correct guard must consider only suppressors that can still reach this header, including the latest unconditional binding and any later competing bindings, rather than an overwritten historical binding. The current non-suppressor controls contain no historical suppressor, so they do not catch this case.

Command in each worktree: `/workspace/poke-harness/pokemon/.venv/bin/python /tmp/rev367r3/overbroad_primary.py`. The script inserted its current working directory at `sys.path[0]`, printed the loaded support path, asserted the path belonged to that worktree, executed both inputs, and called `_is_enforced`. Output:

| Worktree and loaded support path | Analyzer | `flag=False` | `flag=True` |
|---|---|---|---|
| head `/workspace/poke-harness/.scratch/fix367/tests/_timed_menu_milestone_sentinel_support.py` | DEFEATED | LIVE | LIVE |
| base `/workspace/poke-harness/.scratch/mst/tests/_timed_menu_milestone_sentinel_support.py` | ENFORCED | LIVE | LIVE |

## Requested independent execution

Command, separately after `cd` into each worktree: `/workspace/poke-harness/pokemon/.venv/bin/python /tmp/rev367r3/probe_primary.py`. The script inserted the current worktree at `sys.path[0]`, printed and asserted the loaded `tests._timed_menu_milestone_sentinel_support.__file__`, compiled and ran each source on both inputs, and called `_is_enforced` on the parsed AST. Printed paths were respectively `/workspace/poke-harness/.scratch/fix367/tests/_timed_menu_milestone_sentinel_support.py` and `/workspace/poke-harness/.scratch/mst/tests/_timed_menu_milestone_sentinel_support.py`. `control=yes` replaces only the initial `suppress(AssertionError)` with `nullcontext()`.

| Shape | Input `(flag, rows)` | Control | Runtime | Head verdict | Base verdict |
|---|---|---|---|---|---|
| plain `if` | `(False, ())` | no | SUPPRESSED | DEFEATED | ENFORCED |
| plain `if` | `(True, ())` | no | LIVE | DEFEATED | ENFORCED |
| plain `if` | `(False, ())` | yes | LIVE | ENFORCED | ENFORCED |
| plain `if` | `(True, ())` | yes | LIVE | ENFORCED | ENFORCED |
| nested `with` inside `if` | `(False, ())` | no | SUPPRESSED | DEFEATED | ENFORCED |
| nested `with` inside `if` | `(True, ())` | no | LIVE | DEFEATED | ENFORCED |
| nested `with` inside `if` | `(False, ())` | yes | LIVE | ENFORCED | ENFORCED |
| nested `with` inside `if` | `(True, ())` | yes | LIVE | ENFORCED | ENFORCED |
| loop body | `(True, ())` | no | SUPPRESSED | DEFEATED | ENFORCED |
| loop body | `(True, (1,))` | no | LIVE | DEFEATED | ENFORCED |
| loop body | `(True, ())` | yes | LIVE | ENFORCED | ENFORCED |
| loop body | `(True, (1,))` | yes | LIVE | ENFORCED | ENFORCED |

Thus the specified defect exists on base and is fixed on head for all three shapes. The single static DEFEATED verdict on a branch-taking input is conservative because the same AST also has a suppressed branch-skipping input. The non-suppressor controls remain certified ENFORCED, so the fix does not merely decline every shape.

`AMBIGUOUS_SUPPRESSOR` is handled safely in the suppression path: `_resolve_bindings` produces it; `_assigned_suppressors` records any non-`None` result; `_aliased_suppressions` carries the recorded value into `entered`; `_is_suppressing_with` explicitly returns `True` for it, making `_is_enforced` return `False`. The alias dereference path also checks the marker before `_is_readable_suppressor` and appends it. The marker's other production in `_deref_alias` is handled the same way. This handling is safe against false ENFORCED for the requested shapes, but it makes the overbroad condition a false DEFEATED regression. No unhandled marker use was found in the grep and caller walk.

## Repository gates

Run from head with `/workspace/poke-harness/pokemon/.venv/bin/python`:

| Exact command | Exit and output |
|---|---|
| `python -m pytest tests/test_timed_menu_milestone_sentinels.py tests/test_timed_menu_milestones.py -q --junitxml=/tmp/indep_review.xml` | exit 0; JUnit XML `tests=399 failures=0 errors=0 skipped=0` |
| `python -m ruff check tests/_timed_menu_milestone_sentinel_support.py tests/test_timed_menu_milestone_sentinels.py` | exit 0; `All checks passed!` |
| `python -m ruff format --check tests/_timed_menu_milestone_sentinel_support.py tests/test_timed_menu_milestone_sentinels.py` | exit 0; `2 files already formatted` |
| `git diff --check 4796c64..d76722d` | exit 0; no output |

The `python` in the displayed commands was the supplied absolute venv interpreter; the full path is `/workspace/poke-harness/pokemon/.venv/bin/python`. Test numbers above were parsed from `/tmp/indep_review.xml`, not inferred from pytest's progress line.

## Mutations

Command from head: `/workspace/poke-harness/pokemon/.venv/bin/python /tmp/rev367r3/mutate_primary.py`. For each mutation the driver asserted exactly one target match, asserted and printed that on-disk bytes changed before launching the **full** `python -m pytest tests/test_timed_menu_milestone_sentinels.py tests/test_timed_menu_milestones.py -q --junitxml=<per-mutation XML>` lane, restored the original backup in a `finally` block, and ran `diff -q` with exit 0. Baseline and restored support SHA-256: `692d0d4093dd717a56fd915b27cb1ba8bc60029d4f0064147b557034b7a136b4`.

| Mutation | Applied changed SHA-256 | Pytest exit | JUnit `tests/failures/errors/skipped` | Result | Restore `diff -q` |
|---|---|---:|---|---|---:|
| M1 remove whole new `if` block | `2984429ed50f19f3c9f01c3c0ba60dbd3a0fa584929c1af9788b3d6c4215809a` | 1 | `399/4/0/0` | killed | 0 |
| M2 remove only readable-suppressor conjunct | `bf4a3784fb4487635a3fa45381dbb2c5bcb30dbdcc928060c45f1e3656ead16a` | 1 | `399/3/0/0` | killed | 0 |
| M3 insert immediate `return True` in `_store_is_settled_before` | `c44d5d48ee75cee14353829e6c7975a01e835e92fe9111f0921f5e3706a2d610` | 1 | `399/13/0/0` | killed | 0 |
| narrow removal of only `and not _store_is_settled_before(...)` | `c66b4d2300cd421472d634539c70b79950ab673474ef21383e2ccaace7183c8f` | 0 | `399/0/0/0` | **survived** | 0 |

M1's four failures were the three new conditional rows plus the pinned walrus row. M2's three failures were the three new conditional rows. M3's 13 failures included those three and other settled-store rows. The narrow variant survived as the brief anticipated; the predicate's other call sites mask that removal. The surviving narrow mutation is a coverage observation, separate from Finding 1.

Final verification command in head: `git status --porcelain && sha256sum tests/_timed_menu_milestone_sentinel_support.py && diff -q /tmp/rev367r3/support_primary_backup.py tests/_timed_menu_milestone_sentinel_support.py`. Status and `diff -q` produced no output; SHA-256 matched the backup above. No repository change, commit, or push remains.

## Other correctness concern — low severity

The old comment immediately following the new guard still says a single non-capture conditional store is deliberately not ambiguous and describes the formerly shipped walrus expectation. The code and updated test now contradict that explanation. It should be reconciled while fixing Finding 1 so future edits do not reintroduce the false-ENFORCED behavior.
