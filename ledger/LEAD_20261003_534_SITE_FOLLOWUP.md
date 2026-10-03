# LEAD — two more escapes found on merged master, and their repair

Date: 2026-10-03

```
merged guard commit : bf92007f2ced607df72cd8299b24a8a25fa03e57
ledger commit       : ae7efda2bad584cc06badd775e5746637b1ec6a6   (master)
follow-up branch    : lead/534-site-followup
follow-up head      : 3d0d23b40ac505b1de9b7ef3d29855a97e0369c1
```

## How this was found

Before closing #558 and #559 I checked that every commit of #559 was actually
contained in the merge, by **stable patch-id** rather than SHA ancestry
(#559 had been rebased, so its SHAs differ and ancestry proves nothing).

Two of #559's commits had **not** been in the merge — they landed on that
branch after I merged:

```
b7d7a721  Record why _code_signature names constant types directly   (docs only)
254c5339  Guard the site module's attributes and the entries its getters return
```

The second is a substantive repair attributed to "Reviewer G", who reproduced
two escapes in `_site_packages_roots`.

## The escapes, reproduced on merged master `bf92007f`

Not read from the commit message — measured, with
`/workspace/poke-harness/.scratch/r558s/probe_sites.py`:

```
escape2/fspath ESCAPED: Boom: fspath boom
escape2/bool   ESCAPED: Boom: bool boom
```

A site entry whose `__fspath__` or `__bool__` raises a direct `BaseException`
subclass escaped `_site_packages_roots`, and therefore escaped the guard
entirely. The cause was the half-guard shape once more: the conversion was
guarded for `(OSError, ValueError, RuntimeError)` only, and the truth test sat
outside the `try` entirely.

The third claim in the commit message — that
`getattr(site, "getsitepackages", None)` sat outside any exception boundary —
is **unverified**. My probe for it failed on its own `__class__` assignment
before reaching the guard, so I am not claiming it either way. The follow-up
review was asked to settle it independently.

## The repair, cherry-picked onto master

```
b1c71a43  (was b7d7a721)  documentation only, +13 lines
3d0d23b4  (was 254c5339)  the site-module repair, cherry-picked cleanly
```

Same probe on the follow-up head:

```
escape2/fspath: returned [...] (no escape)
escape2/bool:   returned [...] (no escape)
```

Reading the repaired code confirms the shape is now right: the
`getattr(site, attribute, None)` read is inside its own boundary, and the
conversion **and** the truth test share a single `except BaseException`, so an
unusable site directory contributes no root and the guard fails closed.

## Evidence on `3d0d23b4`

```
guard suite : 134 passed, 0 failed, rc=0     (was 132 on the parent bf92007f)
ruff        : clean on both changed files
AC4 repaired: rc=0  PASS  pokered_harness, pyboy
AC4 mis-point: rc=1  FAIL  pokered_harness, pyboy
```

## Status

**Not merged yet.** The follow-up head needs its own independent review;
round 15's APPROVE covers `bf92007f` and does not transfer. The first dispatch
of that review crashed (`codex_core::tools::router: host exited with status
signal: 5 (SIGTRAP)`) after confirming the commit identity and before
producing a verdict, so it was re-dispatched. Its output is retained at
`/workspace/poke-harness/.scratch/r558s/out16.txt`.

#558 and #559 stay open until this follow-up lands. Release remains
**PARTIAL**.
