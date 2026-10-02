# Independent review of #551 (`lead/551-combine-553`) — REQUEST CHANGES

Date: 2026-10-02
Reviewer: lead delivery agent (**not** the author of this branch; author is
`CompleteDotTech`)
Reviewed head: `90deb71` "#534: decode RECORD with the CSV parser, not a
hand-rolled split"
Review worktree: `/workspace/poke-harness/.scratch/rev551` (detached at `90deb71`)
Verdict: **REQUEST CHANGES**

## Independence

`#551` is authored by `CompleteDotTech`, so reviewing it is legitimate: an
author may not approve their own change, and I am not its author. (`#558` is
mine, so my review of it would not count and I have not tried to make it.)

The branch was force-pushed during this review. The first pass was against
`5c272ae`; findings below are re-measured against the current head `90deb71`.

## Suite

| Check | Result |
|---|---|
| `pytest tests/test_import_origin_guard.py tests/test_fixture_provenance.py` | 135 tests, rc=0, no failures |

The suite is green. The finding below is a bypass the suite does not cover.

## Finding 1 — planted `.pth` gives a full false PASS (**blocking**)

`_finder_was_imported_from` accepts a finder as installed when some `.pth` in
the same site-packages directory contains `import <stem>`. A `.pth` is plain
text, so that test is satisfied by *writing the file* — no install, no
distribution, no hash.

Measured end to end against `90deb71`, planting one file into site-packages:

```
guard status: PASS
foreign submodule loaded: foreign
*** FULL BYPASS via attacker-written .pth ***
```

A finder with no provenance at all is certified, and a submodule is then served
from a directory outside the checkout. That is the whole point of the guard.

**This one is not fixable by attesting the `.pth`.** I tried that on `#558`
(round 8, `722ecb2`) and measured it failing twice over: an attacker who can
write a `.pth` can write the `dist-info/RECORD` that attests it, so the bypass
survives identically; and requiring `RECORD` attestation refuses the genuine
`_virtualenv`/`__editable__` shims, which the channel's own docstring says are
*"not owned by any distribution's RECORD"*. Both measurements are in
`LEAD_20261002_558_ROUND8_PTH_REVERT.md`. The honest position is that the
`.pth` channel needs a discriminator that is not an install record at all, and
I do not have one to propose.

## Finding 2 — `RECORD` CSV quoting false red (**fixed on this head**)

`RECORD` is CSV and pip quotes a name containing a comma. The hand-rolled
right-to-left split left the quotes attached, so the reconstructed path named a
file no record had listed and an honestly installed finder lost provenance.

On the previous head `5c272ae`:

```
RECORD: b'"rev551,comma_row_module.py",sha256=9sJVXU4kDdGTXZh51ZsrdxHqdGfNSz2slW3gGcL8fmQ,10\n'
recorded: False (expect True)
```

On the current head `90deb71`, which routes through `csv.reader`:

```
RECORD: b'"rev551,comma_row_module.py",sha256=9sJVXU4kDdGTXZh51ZsrdxHqdGfNSz2slW3gGcL8fmQ,10\n'
recorded: True (expect True)
```

**Resolved.** Recorded here so the disposition of the earlier finding is clear
and the fix is not re-litigated.

## Comparison with #558

Both PRs harden the same guard. `#558` is further along on rounds 1–7
(raising-`__file__`, hostile metaclass, forged `__spec__`, RECORD byte
mutation, conflicting claims); `#551` carries the CSV fix that `#558` lacked
until round 7. Neither is merged. The planted-`.pth` gap is **shared by both**
and is not a discriminator between them.

## Disposition

**REQUEST CHANGES on `90deb71`** — Finding 1 is blocking.

I am not the author, so this review is a legitimate independent one, but it is
also not sufficient on its own: merging is the lead's action and `#551` still
carries the open planted-`.pth` gap with no known fix. Nothing here should be
read as approval to merge. Release remains `PARTIAL`.
