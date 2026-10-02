# LEAD — #558: independent review round 2 (REQUEST CHANGES) and the repair

Session: 2026-10-03T00:20Z. Trunk `master` = `origin/master` = `9ecea99`.

Round 2 reviewed head `9e494a7` — the round-1 repair — and returned
**REQUEST CHANGES**. Brief: `.scratch/briefs/REVIEW_558_DISPATCH2.txt`.
Verdict transcript: `/tmp/rev558_out2.txt`.

## Round 1's fix: confirmed real

> **FIX VERIFICATION: Confirmed.** At `9e494a7`, a meta-path finder whose
> metaclass raises a direct `BaseException` from `__module__` produces a
> structured `FAIL`, rather than escaping with a traceback.

I reproduced the confirmation independently before accepting it.

## Round 2's finding: the same bug class, one layer over

> `_resolve_origin()` catches exceptions reading `__file__`, but its
> namespace-package fallback reads `__path__` without protection.
> `_foreign_path_locations()` also reads `__path__` without protection.

**Independently reproduced at `9e494a7`:**

```
ESCAPED: Boom path boom
```

### Why this one matters more than it looks

`__path__` is the namespace-package search path. Reading it was sitting on the
line immediately below a carefully hardened `__file__` read, guarded by
nothing. Three sites in total were exposed:

1. `_resolve_origin()` — the namespace fallback at `__path__`
2. `_foreign_path_locations()` — the portion loop at `__path__`
3. `_foreign_path_locations()` — the `__file__` corroboration read

The portions in `__path__` are precisely the mechanism by which a package can
smuggle a foreign submodule into an allowed root, so an unreadable `__path__`
is the exact condition the guard exists to detect. Converting the raise into a
reported finding is the fail-closed direction; letting it out is a crash in a
preflight whose entire purpose is to never crash.

All three are now guarded. The `__file__` corroboration contributes no
portions when it cannot be read, which fails closed.

Added `test_a_hostile_path_getter_cannot_abort_the_guard`, proven to **FAIL on
`9e494a7`** before being accepted.

**Head is now `af4e2ecffb9bc5fc8e326d850e7c47840f446e6d`**, confirmed via the
GitHub API and `git ls-remote`.

## Verification at `af4e2ec`

| check | result |
|---|---|
| round-1 escape | fixed — structured `FAIL` |
| round-2 escape | fixed — `status: FAIL`, detail `path portion is not usable: __path__ could not be read: Boom: path boom` |
| full guard suite | **114 passed** |
| AC direction 1, correct root | rc=0 |
| AC direction 2, wrong root | rc=1 |
| `ruff check` | only the pre-existing import-sort finding (also present on `9084db8`) |
| `ruff format --check` | clean |

Round 2's verdict was posted to the PR as
`issuecomment-5963145677`.

## Round 2's remaining, unconfirmed items — carried into round 3

Round 2 flagged without confirming that `_finder_code_file()` and
`_is_installation_finder()` still hold `except Exception` over
attacker-controlled descriptor and code lookups. It observed no escape from
them, and failing to classify a finder as *trusted* is the fail-closed
direction, but it did not settle the question.

Round 3 is dispatched against `af4e2ec` with an instruction to audit
exhaustively for the whole defect class — every remaining unguarded or
`except Exception` lookup of attacker-controlled data — and to settle
definitively whether those two functions can drive an escape.

Early evidence from the running round-3 reviewer supports the fail-closed
reading: those sites return "untrusted", and `_untrusted_meta_path_finders`
then records the finder as an offender rather than trusting it.

## Planted `.pth` — scope judgement

Round 2 independently reached the same conclusion as rounds 1 and the earlier
#551 round 5: acceptable for #534's stated scope, because a writer to
site-packages can equally forge the `RECORD`, so `RECORD` attestation does not
distinguish a mis-pointed checkout from someone who already controls the
interpreter's install files. The gap stays recorded and **open**; it is not
closed by this PR.

## State

Two rounds, two real defects found and repaired, neither waived or argued
away. Nothing is merged and #558 is not marked ready: round 2 was not an
approval, and an old verdict does not transfer to a changed head.

Release remains **PARTIAL**.
