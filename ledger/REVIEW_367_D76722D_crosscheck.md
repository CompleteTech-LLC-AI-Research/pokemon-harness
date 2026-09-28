# Independent round-3 cross-check — PR #393 / issue #367

**Verdict: REQUEST CHANGES.** The committed repair closes the direct conditional-rebind case, but an alias copied after that rebind still certifies a swallowed assertion as **ENFORCED**. This is the dangerous direction. Two all-path rebinding shapes also yield false **DEFEATED** verdicts.

## Provenance and method

- `git rev-parse HEAD` in the head worktree returned `d76722d9ac727ae39c0d947a7f5c35466a72243e`; the base worktree returned `4796c641119a4db2a73deef494eb6902beca70a7`. Both were clean initially.
- Every runtime result below was executed with CPython 3.12.14 using `/workspace/poke-harness/pokemon/.venv/bin/python`. Each verdict was calculated from a parsed AST with `_is_enforced(function, assert_node, tree)`.
- For every verdict, the process changed to the intended tree, inserted that tree at `sys.path[0]`, printed `tests._timed_menu_milestone_sentinel_support.__file__`, and asserted that it resolved to the intended file. The final complete head pass used `/tmp/rev367r3/head_snapshot`, a `git archive` of `d76722d`'s `tests` and `scripts`; its support file SHA-256 matched `git show d76722d:tests/_timed_menu_milestone_sentinel_support.py` (`692d0d4093dd717a56fd915b27cb1ba8bc60029d4f0064147b557034b7a136b4`). The printed import was `/tmp/rev367r3/head_snapshot/tests/_timed_menu_milestone_sentinel_support.py` for each row. Earlier head-worktree probes also printed `/workspace/poke-harness/.scratch/fix367/tests/_timed_menu_milestone_sentinel_support.py`; base comparisons printed `/workspace/poke-harness/.scratch/mst/tests/_timed_menu_milestone_sentinel_support.py`.
- Probe source and runner are at `/tmp/rev367r3/xcheck_probe.py`. The table below reports the committed head snapshot. `LIVE` means `AssertionError` escaped; `SUPPRESSED` means the function returned normally after `assert False`. An analyzer **DEFEATED** verdict is safe but imprecise on a live path; **ENFORCED** on a suppressed path is dangerous.

## A. Sibling shapes

Each shape begins with `cs = contextlib.suppress(AssertionError)` and ends with `with cs: assert False`, except the alias rows noted below. Rebinds use `contextlib.nullcontext()`.

| Shape | CPython runtime by input | Analyzer | Direction of error |
| --- | --- | --- | --- |
| Two separate `if` rebinds | both false: SUPPRESSED; either or both true: LIVE | DEFEATED | Agrees on skipped path; safe false DEFEATED on taken paths |
| `if/else`, both branches rebind | either branch: LIVE | DEFEATED | **False DEFEATED** on every path |
| `try/except ValueError`, both paths rebind | normal try and raised `ValueError`: LIVE | DEFEATED | **False DEFEATED** on every path |
| `while` rebind, zero or one iteration | zero: SUPPRESSED; one: LIVE | DEFEATED | Agrees on zero; safe false DEFEATED on one |
| `match` with `case 1` rebind | no match: SUPPRESSED; match: LIVE | DEFEATED | Agrees on no match; safe false DEFEATED on match |
| `for/else` rebind in `else`, loop can break | no break: LIVE; break: SUPPRESSED | DEFEATED | Safe false DEFEATED on no break; agrees on break |

There is **no false ENFORCED in these six direct shapes** on the committed head. For comparison, the base's isolated import returned ENFORCED for the `while`, `match`, and breakable `for/else` shapes, each of which suppresses on one input. The repair improves those direct cases.

## B. Over-breadth controls

| Shape | CPython runtime | Analyzer | Agreement |
| --- | --- | --- | --- |
| Unconditional `cs = nullcontext()` after suppressor | LIVE | ENFORCED | Yes |
| `cs = nullcontext()` directly in a completed `with nullcontext()` body | LIVE | ENFORCED | Yes |
| `for/else` store where loop has no break | LIVE with empty and nonempty loop | ENFORCED | Yes |
| Initial `cs = nullcontext()` and optional rebind; never a suppressor | LIVE on both inputs | ENFORCED | Yes |

## C. Findings and minimal reproducers

### 1. Alias copied after conditional rebind: false ENFORCED (blocking)

```python
import contextlib

def probe(flag):
    cs = contextlib.suppress(AssertionError)
    if flag:
        cs = contextlib.nullcontext()
    alias = cs
    with alias:
        assert False
```

On CPython, `probe(False)` returns normally (**SUPPRESSED**); `probe(True)` raises `AssertionError` (**LIVE**). The committed-head analyzer returns **ENFORCED** for the assertion. Putting the conditional `if` inside a completed `with contextlib.nullcontext():` produces the same false ENFORCED result. These are two observed placements of the same defect. The base also returns ENFORCED for the direct alias example, so this is an incomplete repair rather than a new regression.

`_resolve_bindings` correctly marks the direct `cs` read ambiguous, but `_store_bindings` dereferences `alias = cs` through `_deref_alias` and `_last_store_before`. That raw alias walk selects the later conditional `nullcontext()` store as if it certainly ran. The resulting `alias` store resolves to no suppressor, and the final `with alias:` is certified. The ambiguity marker never reaches the final consumer. This is a downstream path that treats the unresolved conditional value as harmless.

### 2. `if/else` all-path replacement: false DEFEATED

```python
import contextlib

def probe(flag):
    cs = contextlib.suppress(AssertionError)
    if flag:
        cs = contextlib.nullcontext()
    else:
        cs = contextlib.nullcontext()
    with cs:
        assert False
```

Both CPython inputs are **LIVE**; the committed-head analyzer says **DEFEATED**. `_resolve_bindings` sees two conditional stores and declines despite their exhaustive branches and identical non-suppressing result. The base gives the same verdict.

### 3. `try/except` all-path replacement: false DEFEATED

```python
import contextlib

def probe(flag):
    cs = contextlib.suppress(AssertionError)
    try:
        if flag:
            raise ValueError()
        cs = contextlib.nullcontext()
    except ValueError:
        cs = contextlib.nullcontext()
    with cs:
        assert False
```

Both tested CPython inputs are **LIVE**; the committed-head analyzer says **DEFEATED**. The two conditional stores are treated as ambiguous despite both tested control paths ending with `nullcontext()`. The base gives the same verdict.

## Ambiguity caller audit

`rg -n AMBIGUOUS_SUPPRESSOR tests/_timed_menu_milestone_sentinel_support.py` found the constant at line 63, returns at 1463/1487/1572 and 2029, propagation at 2228–2229, and final handling at 3478. The direct `_resolve_bindings` callers are `_assigned_suppressors` at 1123 and 1188. Both preserve its non-`None` ambiguity marker in the assigned map; `_aliased_suppressions` appends that marker for a direct `with cs:`; `_is_suppressing_with` treats it as defeat at 3478. The walrus path checks and appends the marker at 2228–2229. Those direct consumers are safe.

**The complete caller chain is not safe.** An ordinary alias assignment goes through `_store_bindings` → `_deref_alias` → `_last_store_before`, not through `_resolve_bindings`' ambiguity decision for the source name. The minimal alias repro above demonstrates the marker's loss and the downstream false ENFORCED. The `AMBIGUOUS_SUPPRESSOR` use sites themselves do not accidentally treat the object as `None`; the gap is the parallel alias-resolution path.

## Gates and hygiene

- JUnit XML `/tmp/xcheck.xml`: **399 tests, 0 failures, 0 errors, 0 skipped** (read from the `<testsuite>` attributes). Pytest exit 0.
- `ruff check` on the two requested files: **0 errors**, exit 0.
- `ruff format --check` on the two requested files: **2 files already formatted**, exit 0.
- `git diff --check 4796c64..d76722d`: no output, exit 0.
- Gates finished before the shared head worktree changed: `/tmp/xcheck.xml` was written at 21:29:28 UTC; a later support-file modification was timestamped 21:30:34 UTC. The head worktree started clean but the final `git status --porcelain` reports ` M tests/_timed_menu_milestone_sentinel_support.py`. Its uncommitted diff changed more than once during this review (one observed edit removed the new ambiguity branch; a later one added `return True` to `_store_is_settled_before`). **I did not modify either worktree.** Another parallel process changed the shared head; therefore post-change head-worktree imports are excluded from the committed-head tables. The base worktree remained clean when checked.

Release remains **PARTIAL**. These findings use static AST analysis and real CPython execution only; no hosted CI result is inferred. The direct conditional-rebind repair is sound for the six requested shapes, but the alias-copy escape means it is not complete for its stated safety objective. The all-path `if/else` and `try/except` precision gaps remain outside the repair's direct target, but they are concrete false DEFEATED results.
