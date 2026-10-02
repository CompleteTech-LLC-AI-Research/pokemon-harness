# LEAD — #558: independent review round 3 (REQUEST CHANGES) and the repair

Session: 2026-10-03T01:05Z. Trunk `master` = `origin/master` = `3a0d291`.

Round 3 reviewed head `af4e2ec` and returned **REQUEST CHANGES**. Verdict
transcript: `/tmp/rev558_out3.txt`.

## Both prior repairs confirmed real

> **FIX VERIFICATION: Both reported escapes are fixed at `af4e2ec`.** The
> `__module__` metaclass and `__path__` getter reproductions return structured
> `FAIL` reports.

Two rounds, three defects, all the same bug class, each reproduced and repaired
rather than argued away.

## Round 3's finding: the class recurs in two more places

### 1. `_finder_code_file` descriptor lookups

Reproduced by me at `af4e2ec`:

```
ESCAPED: Boom descriptor boom
```

`getattr(finder, "find_spec", None)`, `__func__`, and `__code__` were all guarded
with `except Exception`.

### 2. `_allowed_roots` — a site the review itself did not reach

Chasing my own reproduction produced a stack that pointed somewhere the review
had not flagged. I isolated it by wrapping every module-level function and
recording which one raises out:

```
ESCAPING FN: _allowed_roots Boom descriptor boom
ESCAPING FN: check_origins   Boom descriptor boom
```

`_allowed_roots` wrapped `importlib.metadata.packages_distributions()` in
`except Exception`. That call **imports helper modules**, so a hostile finder
raises straight out of it before a single distribution is examined. Confirmed
directly:

```
packages_distributions ESCAPED: Boom descriptor boom
  File "<frozen importlib._bootstrap>", line 1074, in _find_spec
    return _bootstrap._find_spec_legacy(finder, name, path)
```

This is the more interesting of the two. It is the same defect class reached by
a completely different route — not an attribute lookup on a hostile object, but
an ordinary library call that *transitively imports*, so anything on
`sys.meta_path` gets to run first. Any audit that only inspects direct
attribute reads will miss it.

Repaired: both descriptor sites and the metadata call now catch
`BaseException`. The metadata path returns no site-packages roots, which fails
closed — the packages are reported as foreign rather than silently admitted.

Added `test_a_hostile_finder_descriptor_cannot_abort_the_guard`, proven to
**FAIL on `af4e2ec`** before being accepted.

**Head is now `d58321fc252ccdb043e447285a80de5fd1444769`.**

## Verification at `d58321fc`

| check | result |
|---|---|
| round-1 escape (`__module__`) | fixed |
| round-2 escape (`__path__`) | fixed |
| round-3 escape (`find_spec` descriptor) | fixed — structured `FAIL` |
| round-3 escape (`packages_distributions`) | fixed — structured `FAIL` |
| full guard suite | **115 passed** |
| AC direction 1, correct root | rc=0 |
| AC direction 2, wrong root | rc=1 |
| `ruff check` | only the pre-existing import-sort finding (also on `9084db8`) |
| `ruff format --check` | clean |

## Round 3's AC1/AC3 reading — checked against the issue text

Round 3 returned **Not met** on AC1 and AC3, reasoning that
`scripts/run_local_ci.sh` "has no direct guard invocation" and "only invokes
it late". I checked this against #534's actual wording rather than accepting it:

> 3. `scripts/run_local_ci.sh` and `scripts/production_gate.py` are covered by
    that guard.

The criterion asks for **coverage**, not a direct call. `run_local_ci.sh`
invokes `production_gate.py` at lines 169, 223 and 306, and the gate's
collection preflight calls `check_import_origins.py` before collection and
tiers. That satisfies the wording. My earlier correction of this point — where
I initially read the absence of a literal `check_import_origins` string in
`run_local_ci.sh` as absence of coverage — stands.

Round 3 also returned **Not met** on AC4 because it did not complete the paired
mis-pointed/repaired install. It was right to refuse; its own simulated
metadata run printed `mispointed PASS`, which proves nothing. Round 4 is
instructed to build both installs for real and retain both outputs.

## The pattern worth noting

Two earlier verdicts each asserted a fix was complete; a later round found more
instances of the identical class. The reason is structural: the module defends
attacker-controlled data in ~15 places, and the audit has been finding them
roughly one layer per round. Round 4 is therefore instructed not merely to
check the known sites but to fix-point the audit — if a remaining instance is
found, keep going — and is told explicitly that earlier verdicts' claims of
completeness should not be trusted.

## State

Nothing merged, #558 not marked ready. Round 3 was not an approval and an old
verdict does not transfer to a changed head. Release remains **PARTIAL**.
