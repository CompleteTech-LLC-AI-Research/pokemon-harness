# PR #557 head `075fff3` — nested-code repair, validated on the real merge tree

**Head:** `075fff388ddefba80df5c1a43b182ca72aa16004` (`fix/556-compare-consts`)
**Merge tree:** tree `391a3d2`, commit `850e64d`
**Worktrees:** `fix556` (branch), `mt557b` (merge tree)
**Status:** OPEN, **not merged** — awaiting independent review (#489).

## What this head repairs

Two defects in the previous head `7fc614a`, both found by measurement:

1. **Nested-code bypass** (false PASS). `_constant_signature` reduced a nested
   code object to its constants and never compared its bytecode, so a lambda or
   comprehension could differ while its constants matched.
2. **False RED** (introduced by this session's own repair). Folding `co_flags`
   in refused the genuine `_distutils_hack.DistutilsMetaFinder`: the installed
   `.pyc` carries `0x13`, recompiling the same source here yields `0x1000013`.
   The difference is `CO_FUTURE_ANNOTATIONS` (`0x1000000`), which describes how
   bytecode was produced rather than what it does. Now masked out.

Finding 1 was reported by `sim <sim@local>` in `b1e09a6`; the lead reproduced
it independently and confirms it.

## Verification on the merge tree `850e64d`

| Check | Result |
|---|---|
| guard + fixture provenance | **121 passed, 0 failed, 0 errors, 0 skipped** |
| guard suite alone | 104 passed / 0 failed |
| `ruff check` both files | clean |
| CLI `check_import_origins.py --project-root .` | rc=0 |
| nested-code bypass | hostile finder TRUSTED **False** -> refused |
| flat const-collision bypass | refused |
| original forged-`co_filename` attack | refused (status FAIL) |

## Mutation matrix

| Mutant | Result |
|---|---|
| baseline | 104 tests, 0 failures |
| M5 nested `co_code` fold removed | **KILLED** — 1 failure, `test_a_nested_code_twin_with_equal_constants_is_still_refused`, and no other row |
| M6 `CO_FUTURE_ANNOTATIONS` mask removed | **KILLED** — 1 failure |
| restored | 104 tests, 0 failures |

M1–M4 were killed on the previous head and are unaffected: the same lines are
untouched. M1-M3 kill counts as recorded in
`LEAD_20261002_557_CONSTS_REPAIR_VALIDATION.md`.

## False-red control

Real `pip install -e` of this tree, then:

    __editable___pokered_harness_0_1_0_finder.py   trusted=True
    _distutils_hack.DistutilsMetaFinder            trusted=True
    untrusted list                                 empty
    check_origins                                  PASS

## Errors of my own, recorded so they are not re-derived

1. **A stale `.pyc` shadowed the edited source.** `_code_matches_source`
   returned `False` for a genuine finder while an identical hand-replication of
   its own body returned `True`, and `dis.dis` showed the loaded function was
   the pre-edit code. Cost roughly fifteen minutes of misdiagnosis; the fix was
   to clear `__pycache__` and load with `-B`. Anyone comparing bytecode-derived
   behaviour on an edited module here should do the same first.
2. **The nested row's premise was initially wrong.** It asserted
   `co_consts ==` between two code objects, which is never true — identical
   source compiles to distinct objects. Rewritten to compare value constants and
   the nested `co_consts` explicitly, which is what the row is actually about.
3. **A first attempt applied the `co_flags` mask to the parent code instead of
   each nested item**, which would have masked nothing. Corrected before any
   result was recorded.

None of these changed a reported number; every table above is post-correction.

## Not verified here

- **No ROM present**, so no real-ROM source/native gate ran.
- **No timing tier**; the host is 4 cores under load, and timing failures are not
  distinguishable from contention here.
- **No full `bash scripts/run_local_ci.sh`.**
- **No independent review.** #489 blocks delegation (sixth attempt this session
  also returned `unsupported call`). An author may not discharge its own review,
  so this head stays open no matter how green it is.

**Release status: PARTIAL**, unchanged.

## Standing caution for the next repair

This is the fourth round on the meta-path finder's trust decision:

| Round | Head | Gap closed |
|---|---|---|
| 1 | #555 `89c7152` | meta-path finder guard restored at all |
| 2 | #556 `c34be0b` | `co_filename` borrowed from a real existing file |
| 3 | #557 `7fc614a` | flat constant collision |
| 4 | #557 `075fff3` | nested code object |

Each round closes one class by comparing *more* fields of recompiled bytecode.
A reviewer should ask whether folding further fields is still the right shape,
or whether the trust rule should instead be narrowed to the known editable
install shim by **name and location** — option 2 in
`LEAD_20261002_555_BLOCKING_FORGED_CODE_FILENAME.md`, never yet tried.
