# Blocking finding on #557: a constants-only signature still admits a nested-code twin

Recorded 2026-10-02 by the lead integrator, before merge. Ledger-only: no code
is changed by this commit.

## Subject

PR #557 (`7fc614a`, "#534: corroborate a finder's constants, not just its
bytecode shape") correctly closes the finding recorded in
`LEAD_20261002_556_BLOCKING_CO_CONSTS_COLLISION.md`: `_code_matches_source` now
compares the constant table, not just `co_code`. That part of the fix is real
and is verified below.

The head is nevertheless still bypassable end to end, by the same class of
attack, moved one level down.

## The gap

`_constant_signature` in `scripts/check_import_origins.py` reduces a nested code
object to that object's *constants*:

```python
return tuple(
    _constant_signature(item)
    if isinstance(item, types.CodeType)
    else (type(item).__name__, repr(item))
    for item in getattr(code, "co_consts", ())
)
```

A nested body -- a lambda, comprehension or closure -- is itself a code object
sitting in the parent's `co_consts` at a fixed index. The parent's `co_code`
addresses it by index and cannot see what the nested body does, and the
signature above never looks at the nested object's own `co_code`, `co_names`
or `co_varnames` either.

So an attacker who keeps the *constants* identical while changing what the
nested body computes passes the whole comparison. The planted neighbour writes
its nested lambda as `_n + ''`; the hostile twin writes `'' + _n`. Same
constant `''`, same resulting string, same top-level bytecode, different
nested bytecode.

The served path is read from the environment, so it is not a constant in
either function and cannot be caught by any constant comparison.

## Measured on the exact head of #557 (`7fc614a`)

Shape, from the venv at `/workspace/poke-harness/.scratch/lead555venv`:

| Field | Result |
|---|---|
| top-level `co_code` identical | `True` |
| top-level `co_names` equal | `True` |
| top-level `co_varnames` equal | `True` |
| nested `co_code` identical | `False`  <- the discriminator that is not compared |
| nested `co_consts` equal | `True` |
| `_constant_signature` equal | `True`  <- the bypass |

End to end, `/workspace/poke-harness/.scratch/lead558_probe/nested_bypass.py`
against `/workspace/poke-harness/.scratch/fix556` (worktree at `7fc614a`):

```
guard status BEFORE attack : PASS
candidates matching #556 fields: 1
  nested code objects        : 1
    nested co_name/co_code len: <lambda> 12
hostile finder TRUSTED     : True
guard status under attack  : PASS
FOREIGN SUBMODULE LOADED   : foreign-submodule
  loaded from              : /tmp/.../foreign/pokered_harness/stolen.py

VERDICT: FALSE GREEN -- FULL BYPASS (nested-code gap)
```

The guard's own suite was green at 119 passed at the same time. A green suite
and a full bypass coexisted, which is why this is recorded as a blocking
finding rather than a nit.

## The repair

`lead/556-const-collision` folds each nested code object's own `co_name`,
`co_code`, `co_names` and `co_varnames` into its signature, and still recurses
so a nested body is covered to any depth:

```python
if isinstance(constant, types.CodeType):
    items.append(
        (
            constant.co_name,
            constant.co_code,
            constant.co_names,
            constant.co_varnames,
            _const_signature(constant),
        )
    )
```

## Verification of the repair

Dedicated venv `/workspace/poke-harness/.scratch/lead555venv`, worktree
`/workspace/poke-harness/.scratch/rep557`:

| Check | Result |
|---|---|
| guard + fixture provenance | 120 passed, 0 failed, 0 errors |
| genuine/editable/install rows | 27 passed |
| nested-code bypass | hostile finder `TRUSTED: False`, guard `FAIL` |
| const-collision bypass (original) | hostile finder `TRUSTED: False`, guard `FAIL` |
| forged `co_filename` (`exploit`) | `ATTACK DEFEATED` |
| forged `co_filename` (`exploit2`) | `ATTACK DEFEATED` |
| real install: `DistutilsMetaFinder` trusted | `True`, untrusted list empty |
| own checkout `check_origins` | `PASS` |
| CLI `--project-root .` | rc=0 |
| `ruff check` both files | clean |

## Mutation check

Removing the nested fold -- reducing the signature back to constants-only, the
shape #557 shipped -- was bypassable end to end (`TRUSTED: True`, guard `PASS`,
foreign submodule loaded). The suite initially did **not** catch that, which is
itself a hole in the evidence and is why
`test_a_nested_code_twin_with_equal_constants_is_still_refused` was added.

With the row in place the mutant is killed, and it is the only row that fails:

```
E  AssertionError: the signature must distinguish a nested body that differs
FAILED tests/test_import_origin_guard.py::test_a_nested_code_twin_with_equal_constants_is_still_refused
1 failed, 102 passed
```

Restoring the fold returns the suite to `120 passed`.

## Disposition

#557 must not merge as it stands. The nested-code fold is required on top of
the constant comparison it already contains. This finding is discharged only
when a head containing the fold has been independently reviewed and merged.

