# LEAD — supersession audit of the eight open #534 PRs

Date: 2026-10-03

```
audited against : master = origin/master = 40aa71027b1437bf944b45b00fb61d6f8699a505
method          : git cherry (patch-id), then row-level and behaviour-level
                  confirmation. Not closure by commit count.
```

The objective file's PR list (#164, #112, #115, #121, #113, #166, #170, #174,
#179/#128 and the other file splits) is a 2026-09-23 snapshot. Live state at
this audit is eight open PRs, all #534, and 31 open issues. The old list is
closed.

## Result

| PR | branch | in master | unmerged | disposition |
|---|---|---|---|---|
| #559 | `lead/534-final-candidate` | 30 | 0 | superseded |
| #558 | `lead/556-const-collision` | 24 | 4 | superseded (combined into `bf92007f`) |
| #557 | `fix/556-compare-consts` | 9 | 2 | superseded, stronger on master |
| #556 | `lead/555-forge-proof-cofilename` | 10 | 0 | superseded |
| #555 | `fix/551-553-close-mutation-gaps` | 7 | 0 | superseded |
| #551 | `fix/534-regular-package-path-portion` | 6 | 11 | superseded, stronger on master |
| #548 | `lead/534-combine-545-546` | 0 | 3 | superseded |
| #549 | `fix/534-package-help-wording` | 0 | 1 | **NOT superseded — genuine gap** |

## Why each superseded PR is genuinely covered

`git cherry` alone was not sufficient. For #558, #557, #551 and #555 it reports
unmerged commits whose *behaviour* is already on master under different names.
Each was confirmed by reading the master implementation, not by counting.

**#559.** Master's `scripts/check_import_origins.py` differs from #559's head
`254c5339` in exactly one way: master *adds* the `check_origins` /
`_check_origins` boundary that #559 lacks. Master is a strict superset of the
guard. On tests, the only row master has that #559 lacks is
`test_the_guard_refuses_rather_than_traceback_on_any_escape`. Superseded.

**#558.** Its four unmerged commits are the boundary work plus two
intermediate repairs and a ledger note. The boundary landed via `bf92007f`.
Of the 37 rows #558 adds, 32 are present on master under the same names; the
other five originated on #559's line and are covered there. Superseded.

**#557.** Claims a full bypass: `co_code` is byte-identical for two functions
differing only in constants. Master compares `co_consts` recursively via
`_code_signature`, which folds nested code objects and compares
`co_name`/`co_code`/`co_names`/`co_varnames`/`co_flags`/`co_argcount`/
`co_posonlyargcount`/`co_kwonlyargcount`/`co_nlocals`/`co_freevars`/
`co_cellvars` at every level. #557's second commit used a shallower
`_constant_signature` and compared `co_flags` naively, which produced a false
red on a real installer; master instead compiles with `dont_inherit=True`,
which stops `CO_FUTURE_ANNOTATIONS` being stamped onto the recompiled copy.
Master's rows `test_a_same_shape_twin_in_site_packages_cannot_corroborate_a_forged_finder`
and `test_a_nested_code_twin_with_equal_constants_is_still_refused` pin both
bypasses and pass (verified: 5 passed, 129 deselected). Superseded by a
strictly stronger implementation.

**#551.** Eleven unmerged commits, but its substantive behaviour is on master:
`test_regular_package_refuses_a_foreign_path_portion` and its namespace
siblings pass (verified: 5 passed), the latin-1 finder row is present, and
stdlib trust is identity-based in `_trusted_stdlib_finders()` guarded against a
hostile `__hash__` — which is #551's own `ed050ec2` requirement. Superseded.

**#555, #556, #548.** Zero unmerged commits for #555 and #556; all rows present
on master. #548's rows are all present on master under the same names, and
master's `_is_this_checkout` / `_allowed_roots` are stronger (they re-raise
`KeyboardInterrupt`/`SystemExit` and catch `BaseException` rather than
`(OSError, ValueError, RuntimeError)`).

## #549 is a real gap, and the prior triage was wrong about it

A triage comment on #549 claimed its row "passes on unmodified master:
2 tests, 0 failures". That run used **#549's own copy** of the row, not
master's. Master's copy cannot fail, because it makes no such assertion:

```
git show master:tests/test_import_origin_guard.py | grep -c "not adds to"
0
```

Confirmed by mutation on master: reverting the help string to the pre-#546
misleading wording leaves the suite **green** (2 passed). The mutant survives.
The gap is real. See the follow-up ledger for the repair.

## #253 unit tier — genuine, re-confirmed on current master

Two rows reproduce on `40aa7102` and are not `/dev/shm`-related:

```
FAILED tests/test_mcp_timed_remote_owner.py::test_duplicate_connection_preserves_existing_epoch_and_execution[connect]
FAILED tests/test_mcp_timed_remote_owner.py::test_mcp_tools_and_resource_share_persistent_native_owner
2 failed, 96 passed
```

The failure is `TimedOwnerError: request deadline expired` against a
wall-clock deadline in `mcp_timed_owner._wait`. The host has 4 CPUs; an
independent review dispatched during this measurement was itself using about
two of them, so these numbers are contended and must be re-measured on a quiet
host before being called a defect. Recorded as unmeasured-under-quiet-CPU, not
as a fixable defect.

`/dev/shm` is confirmed read-only (`tmpfs ... (ro,...)`) and `/tmp` was at
100% earlier in the session. Both remain external blockers for the timing tier.
