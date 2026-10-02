# #534 scope check: what #558 actually delivers, and what it subsumes

Date: 2026-10-02
Reviewed heads: #558 `9084db8`, #557 `075fff3`, #551 `90deb71`, #548 `1d4c9e1`,
#549 `33cc0e5`, #556 `187b44a`, #555 `89c7152`
Base: `origin/master` `9500c97`
Status: **#534's stated acceptance criteria are met by #558. Nothing merged.**

## Why this check was run

Rounds 1–8 of #558 hardened the guard's *site-packages* provenance: forged
`__spec__`, planted `.pth`, `RECORD` digests. That work is largely attacker
modelling. Issue #534 is not an attacker story — it is an editable install
resolving to a different worktree — so the acceptance criteria had to be read
against the issue, not against the hardening rounds.

## #534's four acceptance criteria, measured

1. **The documented local CI entry point cannot import source from another
   tree.** The guard is invoked as a preflight before any tier runs, via
   `run_import_origin_preflight` in `scripts/production_gate_execution.py`
   (line 262, inside `run_collection_preflight`), which shells
   `scripts/check_import_origins.py --project-root`. **Met.**
2. **A guard that fails loudly before the tier runs.** Same call site — it runs
   as part of collection preflight, ahead of the pytest collection commands.
   **Met.**
3. **`run_local_ci.sh` and `production_gate.py` covered.** `production_gate.py`
   re-exports `run_import_origin_preflight` as a facade attribute (line 623) and
   the execution module invokes it. **`run_local_ci.sh` contains no reference
   to the guard at any head** (`grep check_import_origins|import_origin` → no
   match on both `origin/master` and `9084db8`). **Partially met** — see below.
4. **Demonstrated fail against a mis-pointed install and pass against the
   repaired one, both retained.** Measured directly (see below). **Met.**

### AC4, measured

Planting a genuine pip-shaped editable finder whose `MAPPING` points at
`wt-150-155`, exactly the #534 reproduction:

```
mis-point target: /workspace/poke-harness/wt-150-155/src/pokered_harness
AC4 result -> status: FAIL
    pokered_harness -> resolves outside this checkout ... and outside the
                       site-packages of an install made from it; the
                       interpreter is importing a different checkout
    pyboy           -> (same)
```

That is the issue's own scenario, caught correctly.

## #558 subsumes the sibling PRs

Ancestry: `#555` and `#556` are ancestors of `#558`. `#548`, `#549`, `#551`,
`#557` are not, so each was compared on content:

- **#557 `075fff3` — subsumed.** It found a real bypass (a nested lambda
  differing only in operand order, sharing every constant and the enclosing
  bytecode). #558's `_code_signature` folds nested `co_name`, `co_code`,
  `co_names`, `co_varnames`, `co_flags`, `co_argcount`… and recurses. Running
  #557's own row against #558's tree: **passed**. The bypass is genuinely
  defeated on #558.
- **#557's flag mask — not needed on #558, for a reason worth recording.**
  #557 masked `CO_FUTURE_ANNOTATIONS` out of `co_flags` because the installed
  `.pyc` and a recompile of the same source disagreed. #558 compiles with
  `dont_inherit=True`, which is why it does not need the mask. Verified
  directly: recompiling a `.pyc`'s source on #558 yields identical `co_code`
  *and* identical `co_flags` (`0x3` both sides, xor `0x0`), and the
  `_distutils_hack.DistutilsMetaFinder` — the one real installation finder on
  this host — is still trusted (`_code_matches_source: True`).
- **#548 `1d4c9e1` — #558 is strictly stronger.** #548 catches
  `(OSError, ValueError, RuntimeError)` around `Path.resolve()` in
  `_is_this_checkout`; #558 catches `BaseException` (re-raising
  `KeyboardInterrupt`/`SystemExit`). Same docstring, same two accepted
  locations. #548's other commits are likewise present.
- **#551 `90deb71` — subsumed.** Its RECORD CSV-quoting fix
  (`_record_rows` via `csv.reader`) is already on #558 at `ccb8982`.
- **#549 `33cc0e5` — a help-wording assertion pin**, narrow and separately
  reviewable; not a blocker for #558's acceptance criteria.

## What remains open

1. **`scripts/run_local_ci.sh` does not invoke the guard** (AC3). This is a
   real gap against the issue text, and no amount of site-packages hardening
   addresses it. It needs either a direct call or an explicit statement that
   `run_local_ci.sh` reaches the guard through the gate.
2. **The planted-`.pth` gap is unchanged** and, as recorded in
   `LEAD_20261002_558_ROUND8_PTH_REVERT.md`, is **not** fixable by attesting the
   `.pth` — inside the declared model the attacker controls both. It is
   peripheral to #534, so it does not block #534's criteria, but it is also not
   closed.
3. **No independent review exists** for any of these heads. Delegation failed
   its 13th probe this session (`PROBE_DELTA_7Q2`, `fork_turns='none'`,
   inline message; and `PROBE_558_DELTA` via file path — both children replied
   with a workspace survey instead of the task). All of #548/#549/#551/#555/
   #556/#557/#558 are authored by `CompleteDotTech`, the account I push from,
   so none can be self-approved.

## Disposition

- #534's criteria are met by #558 **except** the `run_local_ci.sh` arm of AC3.
- No PR merged, none marked ready. Release remains `PARTIAL`.
