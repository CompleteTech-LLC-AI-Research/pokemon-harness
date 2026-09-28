# Independent round 2 review: PR #393 / issue #367

**Verdict: REQUEST CHANGES.** The multi-hop settlement helper is repaired, but the binding resolver still certifies a swallowed assert as ENFORCED after a conditional rebind that did not run. This occurs in the same function and data path this PR changes. The #397 split leaves the damaging polarity hole in the candidate.

**SHA reviewed:** `f1c1038b6c00a2d104255632ae5c7335018f50a5`; base and local `origin/master`: `ed9d9b0daadefca4ce3082d49068890d4949ddc3`. The checkout was clean before this report. `gh pr view 393 --json headRefOid,state,url,baseRefName` failed with `error connecting to api.github.com`; `git ls-remote origin refs/pull/393/head` failed because `github.com` could not be resolved. The live PR head therefore **could not be verified**. This review is of the explicitly pinned SHA only; it does not assert that the live PR still points there.

## Required commands and independent results

| Command | Terminal result / JUnit XML result |
|---|---|
| `/workspace/poke-harness/pokemon/.venv/bin/python -m pytest tests/test_timed_menu_milestone_sentinels.py tests/test_timed_menu_milestones.py -q --junitxml=/tmp/rev367_r2.xml` | exit 0; XML `tests=393 failures=0 errors=0 skipped=0` |
| `/workspace/poke-harness/pokemon/.venv/bin/python -m ruff check tests/_timed_menu_milestone_sentinel_support.py tests/test_timed_menu_milestone_sentinels.py` | exit 0, `All checks passed!` |
| `/workspace/poke-harness/pokemon/.venv/bin/python -m ruff format --check tests/_timed_menu_milestone_sentinel_support.py tests/test_timed_menu_milestone_sentinels.py` | exit 0, `2 files already formatted` |
| `git diff --check ed9d9b0..f1c1038` and `git diff --check` | exit 0, no output |

CPython is the supplied 3.12.14 venv. The hosted CI rollup is empty/skipped and is not evidence of a pass. Release status stays **PARTIAL**. No ROM, native, timing, or performance claim is made; the host's load and free space preclude a meaningful timing claim.

## F1: helper repaired, verdict still unsafe

I called `_runs_before_end_of` directly on newly constructed ASTs. It returned `True` for a direct `with` body and `False` for a store under `if`, `for`, `while`, `try` body, `except`, or `finally` inside a `with` body. A `for` else without an own-loop `break` returned `True`; with one it returned `False`. This verifies the new ancestry bit and the ordinary settled case. Python has no `with` else; a `with` nested in a loop else was also exercised. The helper returned `True` when the loop could not break, as intended by the new loop rule.

That fix is **not an end-to-end F1 repair**. The following fresh shape, with `flag=False`, returns normally in CPython because the suppressor remains bound, yet `_is_enforced` returns `True`:

```python
def f(flag):
    import contextlib
    cs = contextlib.suppress(AssertionError)
    with contextlib.nullcontext():
        if flag:
            with contextlib.nullcontext():
                cs = contextlib.nullcontext()
    with cs:
        assert False
```

`flag=True` raises `AssertionError`. The analyzer answers ENFORCED for the whole shape. Replacing the first binding with `nullcontext()` makes both runtime inputs LIVE and is correctly certified ENFORCED. The same damaging result occurs for a plain `if` outside `with`, and for several other branch forms below. The old base also answers ENFORCED for these shapes. Its age does not make the current certification correct. The PR edits `_resolve_bindings` and its conditional tie policy, and the release gate asks whether this touched path still has damaging polarity.

## Fresh CPython oracle table

`S/L` means `flag=False` is SUPPRESSED and `flag=True` is LIVE. `L/S` is the reverse. `L/L` is LIVE on both inputs. `U/L` is unreachable entry on false and LIVE on true. `S/U` is suppressed on false and unreachable on true. Every shape has a non-suppressor control made by replacing the initial `suppress(AssertionError)` with `nullcontext()`. The analyzer verdict is one verdict per AST, independent of the input. For an input-dependent shape, only DEFEATED is safe. These sources and the direct calls were executed in `/tmp/rev367_adversarial.py` and `/tmp/rev367_scope_probe.py` after mutation restoration.

| Shape | Suppressor runtime F/T | Analyzer | Correct? | Control runtime F/T; analyzer |
|---|---|---|---|---|
| Plain `if` rebind | S/L | ENFORCED | **No: input-dependent, dangerous** | L/L; ENFORCED |
| `with > if > with` rebind | S/L | ENFORCED | **No: input-dependent, dangerous** | L/L; ENFORCED |
| `with > for > with` rebind | S/L | ENFORCED | **No: input-dependent, dangerous** | L/L; ENFORCED |
| `with > while > with` rebind | S/L | ENFORCED | **No: input-dependent, dangerous** | L/L; ENFORCED |
| `with > try body > with` rebind | L/S | ENFORCED | **No: input-dependent, dangerous** | L/L; ENFORCED |
| `with > except > with` rebind | S/L | ENFORCED | **No: input-dependent, dangerous** | L/L; ENFORCED |
| `with > finally > with` rebind | L/L | ENFORCED | Yes: single runtime answer | L/L; ENFORCED |
| `for` else, no break | L/L | ENFORCED | Yes: single runtime answer | L/L; ENFORCED |
| `for` else, own break | L/S | ENFORCED | **No: input-dependent, dangerous** | L/L; ENFORCED |
| `while` else, no break | L/L | ENFORCED | Yes: single runtime answer | L/L; ENFORCED |
| outer `for` else, break only in inner loop | L/L | ENFORCED | Yes: single runtime answer | L/L; ENFORCED |
| walrus in short-circuit `if` | S/L | ENFORCED | **No: input-dependent, dangerous** | L/L; ENFORCED |
| `match` capture from input-dependent subject | U/L | DEFEATED | Safe decline: input-dependent | U/L; DEFEATED |
| conditional `del cs` | S/U | ENFORCED | **No: input-dependent, dangerous** | L/U; ENFORCED |
| decorator body binds its own `cs` | S/S | ENFORCED | **No: single suppressed answer** | L/L; ENFORCED |
| comprehension target named `cs` | S/S | DEFEATED | Yes: single suppressed answer | L/L; ENFORCED |
| lambda body uses `cs` spelling | S/S | DEFEATED | Yes: single suppressed answer | L/L; ENFORCED |
| class body binds its own `cs` | S/S | ENFORCED | **No: single suppressed answer** | L/L; ENFORCED |
| `async with` conditional rebind | S/L | ENFORCED | **No: input-dependent, dangerous** | L/L; ENFORCED |
| `async for` conditional rebind | S/L | ENFORCED | **No: input-dependent, dangerous** | L/L; ENFORCED |
| `global cs` conditional rebind | S/L | ENFORCED | **No: input-dependent, dangerous** | L/L; ENFORCED |
| `nonlocal cs` conditional rebind | S/L | ENFORCED | **No: input-dependent, dangerous** | L/L; ENFORCED |

The control run certified **21 of 22** controls ENFORCED; the exception was the `match` row, whose runtime itself differs across inputs and was safely declined. Four of four tested guaranteed live suppressor rebinds were also ENFORCED (`finally`, `for` else, `while` else, nested-loop break). Thus the tool still certifies ordinary positives; the finding is not based on an analyzer that declines everything. The class-body and decorator failures are additionally single-answer false certifications, but both reproduce on the base and are separate from the blocking branch-resolver issue. A flag-dependent DEFEATED result in the table is a safe decline, not a defect.

## F2: fixture non-vacuity on base

I loaded `git show ed9d9b0:tests/_timed_menu_milestone_sentinel_support.py` from `/tmp/rev367_base_support.py` into the test module name, without editing the worktree, then ran the seven `test_two_stores_sharing_one_statement_resolve_without_walk_order` parametrizations. The JUnit XML `/tmp/rev367_base_newrows.xml` says `tests=7 failures=2 errors=0 skipped=0`, exit 1. These two rows fail on base, both expected ENFORCED but base returned DEFEATED:

1. `a for/else pair whose else runs on every path settles the name`
2. `a break in a nested loop does not suppress the outer loop's else`

The other **five of seven** pass on base and do not individually detect the reverted implementation: the breakable `for` else decline, the two-conditional-store decline, the unconditional-walrus control, the carried-suppressor rebind, and the later-carrier row. The two failing rows prove the new fixture group is non-vacuous for the loop-else repair. They do not themselves establish coverage of every tied-store behavior claimed for #367.

## #397 split and findings

**The split is a deferral of a blocking defect.** The original `if` counterexample indeed reproduces on base, and changing the resolver may require updating the #324 expected verdict. That pinned row checks only an AST and can hide the `flag=False` runtime. An old fixture is not a valid reason to certify a swallowed assert. This PR changes `_resolve_bindings`, `_latest_write_in_block`, `_stores_of`, and `_runs_before_end_of`; it has a suitable place to make single conditional supersession ambiguous and update the conflicting test with both runtime inputs. Filing #397 preserves useful tracking but does not make the current PR safe to approve.

1. **Blocking, false ENFORCED** — `tests/_timed_menu_milestone_sentinel_support.py:1419-1538`. `_resolve_bindings` only declines multiple competing conditional stores (and selected captures). A *single* conditional `cs = nullcontext()` after an unconditional suppressor is chosen even when its branch can be skipped. The minimal `with > if > with` reproduction above gives CPython `SUPPRESSED/LIVE` and analyzer `ENFORCED`. A plain `if` has the same result. Fix the conditional resolution and add a two-input execution fixture with a live non-suppressor control; reconcile the #324 row with CPython.
2. **Additional pre-existing false certifications, outside the narrow #367 tie** — `tests/_timed_menu_milestone_sentinel_support.py:1220-1370`. The new independent decorator and class-body shapes run suppressed on both inputs but `_is_enforced` returns ENFORCED. They also fail identically on base, so they are not evidence that this PR introduced those gaps. They warrant separate follow-up after finding 1 is resolved.

## Mutations and ledger audit

I applied three text mutations sequentially to the support file, asserted each search matched **exactly once**, asserted the mutated bytes differed on disk, ran the seven tied-store rows, parsed JUnit XML, then restored the original bytes in a `finally` block and verified SHA-256 after each run. The worktree was clean after restoration.

| Mutation | Applied change | JUnit `tests/failures/errors/skipped`; killed by |
|---|---|---|
| B | `_store_is_settled_before` forced `False` | `7/4/0/0`; both no-break loop-else positives and both mixed-tie rebind rows |
| H | `_edge_is_guaranteed` loop-else edge forced `False` | `7/2/0/0`; the two no-break loop-else positives |
| J | nested-loop guard disabled in `_breaks_own_loop` | `7/1/0/0`; the nested-loop-break row |

All three mutation pytest commands exited 1 for actual assertion failures. The original support hash after every restoration was `79da06020ad9971cbed900ba0f3e4f9f25b31e455c3e45c510aa9d4b581a1a45`. This verifies **3/3 selected mutations**, not the whole matrix. The ledger itself claims 7/7 in its first round and 10/10 after H-J; I found no `13/13` claim in that file. The request's `13/13` statement therefore cannot be attributed to this ledger or independently confirmed by these three reruns.

The ledger's current suite count of 393 is reproduced. Its older 391 count is a historical run, not the current head. Its two recorded source hashes (`50aa7b...`, `3bb075...`) do **not** match this head; current hashes are `79da0602...` for support and `007d3cea18bbe01bda81cbaca3914cf408e710e3fc0ae35dba18b57c0fc7dfc9` for tests. They document an earlier snapshot. The old `/tmp/final367.xml` artifact exists and its claimed SHA-256 `cf87f48b2b602e1f626610a32d6ceb6ac645044f133e0475e575943a16b49d25` matches, but it is not evidence for this head. The ledger's `2 files, 853 insertions, 21 deletions` is historical: current code/test diff is **2 files, 972 insertions, 21 deletions**; the entire commit diff is **5 files, 1446 insertions, 21 deletions**. I did not reproduce its 630-combination reachability enumeration or unrun mutations.

**Unresolved risks:** live PR head unverified because GitHub DNS/API access failed; the remaining 7 or 10 ledger mutations were not rerun; scope and carrier false certifications outside the tie path need follow-up. No hosted CI pass or real-ROM qualification exists.

**Smallest next action:** Make `_resolve_bindings` decline a single conditional supersession when the earlier suppressor may still be live, replace the conflicting #324 AST-only expectation with a two-input CPython-backed fixture and non-suppressor control, rerun this lane, and obtain a live-head check before merge.
