# BLOCKING finding — PR #557 head `7fc614a`: nested-code twin passes `_constant_signature`

**Verdict: REQUEST CHANGES** against the lead's own repair. Do not merge `7fc614a`.

This finding was first reported on the PR by `sim <sim@local>` in commit
`b1e09a6`. The lead reproduced it independently and confirms it, with its own
end-to-end exploit. Recording it here because the lead authored the code being
found wanting, and an author may not discharge its own review — so this is a
second confirmation of the *defect*, not an approval.

## The defect

`_constant_signature` (added by #557) reduces each entry of `co_consts` to
either a recursive signature (nested code objects) or `(type, repr)`. For a
**nested code object** it recurses into only that object's *constants* and never
compares that object's **bytecode**.

A nested body — lambda, comprehension, closure — lives in the parent's
`co_consts` at a fixed index, so the parent's `co_code` cannot see what it does.
An attacker who keeps the constants identical while changing the nested
computation therefore passes the entire comparison.

## Reproduction (lead's own probe)

Probe: `/workspace/poke-harness/.scratch/lead556_probe/nested_bypass.py`
Signature-level: `.../nested_gap.py`
Tree: the #557 merge tree `b0ea779` (tree `5ee4852`), worktree
`/workspace/poke-harness/.scratch/mt557`.

    $ .scratch/lead556venv/bin/python .scratch/lead556_probe/nested_bypass.py \
        /workspace/poke-harness/.scratch/mt557

    top-level co_code identical: True
    _constant_signature equal  : True  <-- BYPASS

    *** hostile finder TRUSTED: True
    guard status under attack: PASS
    FOREIGN SUBMODULE LOADED: foreign-submodule
      outside checkout: True
    VERDICT: FALSE GREEN -- NESTED-CODE BYPASS

Signature-level confirmation of *why*:

    top-level co_code identical : True
    nested co_code identical    : False   <- never compared
    nested co_name identical    : True
    nested co_consts equal      : True
    _constant_signature equal   : True    <-- the bypass
    nested-folded signature eq  : False   <-- closes it

The hostile finder serves its path from `os.environ`, assembled inside a nested
lambda, so **no constant comparison can see it**. The planted neighbour uses
`(lambda a, b: a + b)` and the hostile twin `(lambda a, b: b + a)`: same
constants, same result, same top-level bytecode, different nested bytecode.

## Status of #557's existing verification

Unchanged and still valid — this finding waives none of it:

- 119 passed / 0 failed / 0 errors / 0 skipped on the merge tree.
- Hosted CI run `37056207330` on head `7fc614a`: `PASS import-origins`,
  `PASS unit total=8146 failed=0`, `overall: PASS`.
- M1–M4 killed, both original attacks refused, real `pip install -e` PASS.

The lesson is the important part: **M4 (the `co_consts` comparison) was killed by
exactly one row**, and that row only covered a *flat* constant collision. The
suite was green and the hosted gate was green while a full false PASS remained
reachable, because the mutation that mattered most was never written. A pinned
mutant proves the line is load-bearing; it does not prove the line is *sufficient*.

## Repair direction

Fold each nested code object's own identity into its signature and keep
recursing, rather than recursing into constants alone:

```python
def _constant_signature(code):
    out = []
    for item in getattr(code, "co_consts", ()):
        if isinstance(item, types.CodeType):
            out.append(
                ("<code>", item.co_name, item.co_code, item.co_names,
                 item.co_varnames, item.co_argcount, item.co_flags,
                 _constant_signature(item))
            )
        else:
            out.append((type(item).__name__, repr(item)))
    return tuple(out)
```

Note that folding `co_code` in is what defeats this shape, because the nested
bytecode is the discriminator — not the constants.

A row must then pin the nested case specifically, and — the real lesson — a
mutant that removes the nested fold must be shown to be end-to-end bypassable
*without* the row, so the row is known to be load-bearing rather than merely
present.

## Honest limit

This is the third round on this trust decision, and the pattern is worth stating
plainly for the next repair: comparing recompiled bytecode structurally keeps
admitting twins that differ in whatever the comparison does not cover. Each round
has closed one class (borrowed name → borrowed existing file → flat constant →
nested bytecode). A reviewer should ask whether folding more fields is still the
right shape of fix, or whether the trust rule should instead be narrowed to the
known editable-install shim by **name and location**, which is option 2 in
`LEAD_20261002_555_BLOCKING_FORGED_CODE_FILENAME.md` and has never been tried.

## Constraints

Same as the previous two findings: the repair lands on a new head, and that head
needs its own independent review. Delegation remains blocked by #489.
