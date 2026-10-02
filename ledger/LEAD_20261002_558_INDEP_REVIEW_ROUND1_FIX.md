# LEAD — #558: independent review round 1 (INCOMPLETE) and the repair

Session: 2026-10-02T23:55Z. Trunk `master` = `origin/master` = `81b1474`.

## The blocker was worked around, not waived

`spawn_agent` now fails outright (`unsupported call`), as do `list_agents` and
`wait_agent`. #489 has hardened further, so in-session delegation is
unavailable.

An out-of-band path was found that does **not** depend on the broken
collaboration tools: a separate `codex exec` process with its own account
(`codex2`) is a genuinely independent session. It receives its prompt on stdin
rather than through the broken delivery channel, and it has no access to this
session's reasoning. The `codex` launcher routes on `argv[0]`, so it must be
invoked through a symlink named `codex2`.

This does not weaken the independence requirement — it satisfies it more
strictly. The reviewer is a separate process with a separate context.

## Round 1 verdict: INCOMPLETE, with a real defect

Reviewer dispatched against head `9084db8`, brief
`.scratch/briefs/REVIEW_558_HEAD.md`. Verdict: **INCOMPLETE**.

It declined to approve because required checks could not finish, and it was
right to. It found and reproduced one genuine defect:

> A finder with a metaclass whose `__module__` property raises a custom
> `BaseException` caused `check_origins()` to escape instead of returning a
> structured `FAIL`.
> ```
> ESCAPED: Hostile module lookup exploded
> ```

**Independently reproduced by me at head `9084db8`:**

```
scripts/check_import_origins.py:1086: in check_origins
    intruders = _untrusted_meta_path_finders()
scripts/check_import_origins.py:644: in _untrusted_meta_path_finders
    offenders.append((finder, _finder_code_file(finder) or _finder_source(finder)))
scripts/check_import_origins.py:178: in _finder_source
    name = finder.__module__ if isinstance(finder, type) else type(finder).__module__
Hostile: module lookup exploded
```

### Root cause

`_finder_source` guarded six attacker-controlled lookups with
`except Exception`. A metaclass is arbitrary code and need not raise inside
`Exception`, so a direct `BaseException` subclass bypassed every clause and
escaped at line 178.

This is a fail-closed violation, not a false pass: the process crashes instead
of reporting a finding. The module already establishes the opposite convention
elsewhere — `_resolve_path`, `_describe`, `_finder_module` and
`_is_this_checkout` all catch `BaseException` for exactly this reason.
`_describe` at the reporting site had the identical `except Exception` bug.

### Why the existing test missed it

`test_a_hostile_finder_metaclass_cannot_abort_the_guard` raises
`RuntimeError`, which `except Exception` already caught. The suite had no row
for a `BaseException`, which is the only case that got through.

## The repair — pushed as `9e494a7`

Widened the six clauses in `_finder_source` and the one in `_describe` to
`BaseException`, matching the module's existing convention. Added
`test_a_hostile_finder_raising_base_exception_cannot_abort_the_guard`.

**The new test was proven to fail on the unfixed code** before being accepted.
With the guard reverted to `9084db8` the new row reported:

```
FAILED tests/test_import_origin_guard.py::test_a_hostile_finder_raising_base_exception_cannot_abort_the_guard
```

With the guard restored to `9e494a7`, the hostile-finder rows reported:

```
2 passed  (-k hostile_finder)
```

Verification at the new head:

| check | result |
|---|---|
| escape reproduced | fixed — `STATUS: FAIL (no escape)` |
| full guard suite | **113 passed** |
| AC direction 1, correct root | `PASS`, rc=0, both packages PASS |
| AC direction 2, wrong root | `FAIL`, rc=1, both packages FAIL |
| `ruff format --check` (guard) | clean |
| `ruff check` | only a pre-existing import-sort finding, present on `9084db8` too |

PR #558 head is now `9e494a7fa76b1e5f945f2e4b76fe2dbdfaf8a90a`, pushed and
confirmed via both `git ls-remote` and the GitHub API.

## Round 1's other two findings — disposition

1. **Planted `.pth` can produce a false PASS.** The reviewer judged this
   **acceptable for #534's scope**: it requires write access to the
   interpreter's site-packages plus a startup `.pth`, which does not describe
   the stale editable install in the issue. This matches the retraction of
   round 8 at `14aa5b9` and the round-5 finding on #551. The gap remains open
   and recorded; it is not closed by this PR.

2. **`pytest_asyncio` missing from the review venv**, so a complete
   `production_gate.py` run could not finish in that interpreter. An
   environment limitation of the review sandbox, not a defect in the PR. A
   fresh review has been asked to attempt the gate with an interpreter that
   has the declared dev extra.

## State

Round 1 was **not** an approval, so nothing is merged and #558 is not marked
ready. A fresh independent review of head `9e494a7` is dispatched, asking
specifically to verify the fix and to audit for **any remaining** untrusted-data
path that still escapes. An old-head verdict does not transfer to a changed
head.

Release remains **PARTIAL**.
