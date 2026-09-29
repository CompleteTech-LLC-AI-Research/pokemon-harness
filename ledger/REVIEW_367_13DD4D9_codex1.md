# Independent review of PR #393 / issue #367

**Verdict: REQUEST CHANGES.** The new `_runs_before_end_of` rule certifies a suppressed assert as ENFORCED when an optional branch contains a nested `with`; all five new fixture rows also pass against the unrepaired base implementation.

**SHA reviewed:** code commit `13dd4d975c9523e5bc50ab6da8dcc8ee6e29180a`, base `ed9d9b0daadefca4ce3082d49068890d4949ddc3`, local review head `68eed2c441d3780855c4a2217901506b487fca8d`. `git rev-parse` verified these and `origin/master == ed9d9b0`. The three commits after the code change and before the local review head are brief/ledger only. `gh pr view 393 --json headRefOid` and `gh issue view 367 --comments` each failed repeatedly with `error connecting to api.github.com`; the live PR head and issue comments could not be independently verified. The supplied task text states that the issue author retracted the #360 attribution; I cannot independently confirm the comment.

## Commands and results

* `/workspace/poke-harness/pokemon/.venv/bin/python --version`: `Python 3.12.14`.
* `/workspace/poke-harness/pokemon/.venv/bin/python -m pytest tests/test_timed_menu_milestone_sentinels.py tests/test_timed_menu_milestones.py -q --junitxml=/tmp/rev367_cli.xml`: exit 0. JUnit XML `tests=391`, `failures=0`, `errors=0`, `skipped=0` (11 warnings).
* `/workspace/poke-harness/pokemon/.venv/bin/python -m ruff check tests/_timed_menu_milestone_sentinel_support.py tests/test_timed_menu_milestone_sentinels.py`: exit 0, `All checks passed!`.
* `/workspace/poke-harness/pokemon/.venv/bin/python -m ruff format --check tests/_timed_menu_milestone_sentinel_support.py tests/test_timed_menu_milestone_sentinels.py`: exit 0, `2 files already formatted`.
* `git diff --check ed9d9b0..13dd4d9` and `git diff --check`: exit 0, no output.
* `sha256sum` matches ledger: support `50aa7b9170e5d921cef1e504f87363e8b1f873599b53cbba088e91d52e611cdf`; sentinel test `3bb0756428ca1dce8c279fa5179e03f8b16a4f42506e26e6a3f79d14cd4d69d4`.

Hosted CI is skipped; no hosted pass is inferred. Release status remains PARTIAL. These are structural and CPython execution observations only; no ROM, native, timing, or performance qualification is claimed.

## Independent CPython execution

I built and executed each source shape in `/tmp/rev367_probe.py` with CPython 3.12.14, then called `_is_enforced` on its AST. `LIVE` means `AssertionError` escaped; `SUPPRESSED` means the function returned normally; `UNREACHABLE` means entry raised before the assert. Every suppressor shape has an adjacent non-suppressor control. The controls establish ordinary positive certification: the plain, nested-branch, nested-loop, and direct-rebind live asserts were all ENFORCED.

| Shape | True runtime | Analyzer verdict | Agree? |
|---|---|---|---|
| Plain alias to `suppress(AssertionError)` | SUPPRESSED | DEFEATED | yes |
| Plain `nullcontext()` control | LIVE | ENFORCED | yes |
| Outer `with`; `if flag` contains nested `with` and `cs = nullcontext()`; `flag=False` | SUPPRESSED | **ENFORCED** | **no, dangerous** |
| Same shape, `flag=True` control | LIVE | ENFORCED | yes |
| Outer `with`; empty `for` contains nested `with` and `cs = nullcontext()` | SUPPRESSED | **ENFORCED** | **no, dangerous** |
| Same shape, one iteration control | LIVE | ENFORCED | yes |
| Direct `with` body rebinds `cs` to `nullcontext()`; live control | LIVE | ENFORCED | yes |
| Direct `with` body rebinds `cs` to `suppress(AssertionError)` | SUPPRESSED | DEFEATED | yes |
| Mismatched `match [1, 2]: case [cs]` retains prior suppressor | SUPPRESSED | DEFEATED | yes |
| Matched `match [1]: case [cs]` binds int control | UNREACHABLE:TypeError | DEFEATED | yes |

Minimal dangerous reproduction (substitute `flag=False` to execute the suppressed path):

```python
def f(flag=False):
    import contextlib
    cs = contextlib.suppress(AssertionError)
    with contextlib.nullcontext():
        if flag:
            with contextlib.nullcontext():
                cs = contextlib.nullcontext()
    with cs:
        assert False
```

CPython returns normally at `flag=False`, whereas `_is_enforced` returns `True`. At `flag=True`, CPython raises `AssertionError` and the analyzer returns `True`. Replacing `if` with `for _ in rows` produces the same disagreement for `rows=()` and agreement for `rows=(1,)`.

## Findings

1. **Blocking — dangerous polarity.** [support.py](/workspace/poke-harness/.scratch/rev367_cli/wt/tests/_timed_menu_milestone_sentinel_support.py:3210): `_runs_before_end_of` traverses every `body`, `orelse`, `finalbody`, and handler without preserving whether earlier edges were conditional. At line 3218 it tests only the *immediate parent* of the store. A store inside `if -> with -> assignment` is therefore called settled just because the last edge is a `with` body. `_store_is_settled_before`, `_stores_of`, and the forwarding pass then treat the skipped `nullcontext()` write as certain, retire the prior suppressor, and report an actually swallowed assert ENFORCED. The execution table reproduces this for both `if` and `for`.
2. **Blocking — new fixtures do not detect the original implementation.** [sentinel test](/workspace/poke-harness/.scratch/rev367_cli/wt/tests/test_timed_menu_milestone_sentinels.py:3136): I copied the pristine support file, replaced it temporarily with `git show ed9d9b0:tests/_timed_menu_milestone_sentinel_support.py`, and ran `pytest ... -k two_stores_sharing_one_statement --junitxml=/tmp/rev367_base_newrows.xml`. The base implementation returned exit 0; XML: `tests=5`, `failures=0`, `errors=0`, `skipped=0`. **All five newly shipped rows pass with the implementation reverted.** In particular, the named `for/else` row's expected `False` can result from selecting the walk-first suppressor, so it does not prove ambiguity was recognized. Add a row that fails the old walk-order policy while preserving a non-suppressor control.
3. **Non-blocking — source-semantic claim overstates the implementation.** [support.py](/workspace/poke-harness/.scratch/rev367_cli/wt/tests/_timed_menu_milestone_sentinel_support.py:3204): The comment says every ancestry hop must be a `with` body, but the code checks only the final hop. This is the direct cause of finding 1. I found no separately demonstrable dead branch in the reviewed code; the earlier all-conditional tie branch described in the ledger is absent.

## Mutation and ledger audit

I re-ran ledger mutations **B** (`_store_is_settled_before` returns `False`) and **G** (disable the `_NOT_A_SUPPRESSOR` forwarding loop). Each textual replacement matched exactly once and changed the file (`+17` and `-21` bytes respectively); each was verified on disk before test execution. The five new fixture rows under each mutation produced XML `tests=5`, `failures=2`, `errors=0`, `skipped=0`, exit 1. Both were killed by the `CONTROL an unconditional walrus beside a later rebind in one block` and `a carried suppressor walked before its in-block rebind` rows: expected `[False, True]`, observed `[False, False]`. After each run I restored from my own pristine copy. `diff -q tests/_timed_menu_milestone_sentinel_support.py /tmp/rev367_support_pristine.py` was silent and exit 0; `git status --short` was empty before writing this report.

The two measured mutations support 2/2, **not** independent verification of the ledger's 7/7 claim; the other five were not re-run. The ledger's hash claims match. Its assertion that the new fixtures close the previously uncovered tie gap is not reproduced: 5/5 pass on the base code. Its cited `/tmp/final367.xml` hash was not reproduced because that author-side artifact is not available here. Issue comments and live PR head could not be fetched because GitHub API access failed. The author-side 630-combination reachability calculation was not independently repeated.

**Unresolved risks:** Other ancestry shapes under `try`, `else`, and handlers may trigger the same settlement error. The live PR head remains unverified because `gh` cannot connect. No timing-sensitive inference is valid on this loaded host.

**Smallest next action:** Make `_runs_before_end_of` reject a store if *any* edge from the top statement to that store passes through a branch, and add an executed false-branch suppressor case with its live true-branch control. Add a fixture that fails against `ed9d9b0` for the actual tie defect; rerun this lane and have an independent reviewer verify the live PR head.
