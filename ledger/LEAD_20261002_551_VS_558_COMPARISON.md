# #551 and #558 solve the same decision two ways: comparison and disposition

Recorded 2026-10-02 by the lead integrator. Ledger-only.

## The collision

#551 (`fix/534-regular-package-path-portion`, head `0baf52e`, "require load
provenance, not just matching bytes") and #558
(`lead/556-const-collision`) both rewrite the same two files,
`scripts/check_import_origins.py` and `tests/test_import_origin_guard.py`, and
both decide the same question: whether a meta_path finder was installed by this
interpreter.

Neither includes the other:

```
git merge-base --is-ancestor 187b44a origin/fix/534-regular-package-path-portion
    -> does NOT include #556
git merge-base --is-ancestor 0b819d9 origin/fix/534-regular-package-path-portion
    -> does NOT include the #558 repair
```

So they cannot both merge as-is, and choosing by fiat would be a guess about
which is safer. Both were measured instead.

## Both defeat every attack that has been found

Run against `origin/fix/534-regular-package-path-portion` at `0baf52e`:

| Probe | #551 `0baf52e` | #558 `0b819d9` |
|---|---|---|
| nested-code bypass | TRUSTED False, guard FAIL | TRUSTED False, guard FAIL |
| const-collision bypass | TRUSTED False, guard FAIL | TRUSTED False, guard FAIL |
| forged `co_filename` (`exploit`) | ATTACK DEFEATED | ATTACK DEFEATED |
| forged `co_filename` (`exploit2`) | ATTACK DEFEATED | ATTACK DEFEATED |

Neither implementation is bypassable by any vector measured so far. They are
equivalent on the attacks; they differ on the shape of the defence.

## Where #551 is stronger

#551 compares a full fingerprint per code object:

```
co_argcount, co_kwonlyargcount, co_nlocals, co_flags, bytes(co_code),
co_consts (recursed), co_names, co_varnames, co_freevars, co_cellvars
```

That is strictly more than #558 compares. #558 folds
`co_name`/`co_code`/`co_names`/`co_varnames` and the constant signature into a
nested code object's signature, and does not compare `co_flags`,
`co_freevars`, `co_cellvars`, `co_argcount` or `co_nlocals`.

On the specific question of whether those omitted fields are load-bearing: a
corpus sweep of 28 `find_spec` shape variants produced 12 pairs the guard
treats as identical, and **none** of them differed in any omitted field. On
this evidence the omissions are not currently exploitable, and `co_freevars`
in particular is already visible in `co_code`, because a free variable compiles
to `LOAD_DEREF` where a local compiles to `LOAD_FAST`.

That is an argument that #558 is safe enough, not that it is better.

## Where #558 was weaker, and is now fixed

#551 already handled two things #558 got wrong. Both were found by comparing
the two implementations rather than by testing #558 alone.

**Decoding.** #558 read the named file with `read_text(encoding="utf-8")`. A
genuine module declaring a latin-1 encoding cookie cannot be decoded that way,
the call raises, and the blanket `except` converted it to a refusal:

```
read_text(utf-8)     -> RAISES UnicodeDecodeError
corroboration        -> False   (false FAIL on an honest finder)
```

`0baf52e` uses `tokenize.open`, the import system's own decoder, so the cookie
is honoured. Fixed on #558 in `ef61d3d`, with
`test_a_genuine_latin1_finder_is_still_corroborated` pinning it as a mutation
guard (reverting makes exactly that row fail).

**Flag inheritance.** `compile()` inherits the calling frame's `__future__`
flags. This module uses `from __future__ import annotations`, so inheriting
stamps `CO_FUTURE_ANNOTATIONS` onto every recompiled code object and no genuine
finder could match. #551 passes `dont_inherit=True`. Applied on #558 in the
same commit.

#558 also folds nested bytecode where #557, the head it grew out of, did not;
that is the finding recorded in
`LEAD_20261002_557_BLOCKING_NESTED_CODE_GAP.md`.

## Disposition

Both heads are unblocked on review grounds only. Neither has an independent
review, which is the single blocker on both.

Recommendation, on the evidence above:

- #558 should absorb #551 rather than compete with it. #551's fingerprint is
  the stronger shape, and folding `co_flags`, `co_freevars`, `co_cellvars`,
  `co_argcount` and `co_nlocals` into #558's signature costs nothing and closes
  the whole omitted-field question rather than arguing it empirically per
  corpus.
- #551's `__path__` containment scope for a regular package is not covered by
  #558 and must be carried across; it is the reason #551 exists.

Neither may merge before that consolidation and an independent review of the
resulting head. Doing this now, while both are unmerged and unapproved, is
cheaper than reconciling two merged PRs that both rewrote the same guard.

Release status stays PARTIAL.

