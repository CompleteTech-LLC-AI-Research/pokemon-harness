# Review of #551 (`lead/551-combine-553`) — findings reported, NOT an independent review

Date: 2026-10-02
Reviewed head: `90deb71` "#534: decode RECORD with the CSV parser, not a
hand-rolled split"
Review worktree: `/workspace/poke-harness/.scratch/rev551` (detached at `90deb71`)
Verdict of the measurements: **one blocking bypass survives**
Status: **does not count as a review. #551 remains unreviewed.**

## Correction to the record

An earlier ledger entry in this round described a review of `#551` as
*independent*, on the grounds that `#551` was authored by someone other than
me. **That is wrong, and this entry retracts it.**

```
$ gh api user --jq .login          ->  CompleteDotTech
$ gh pr view 551 --json author     ->  CompleteDotTech
$ gh pr view 558 --json author     ->  CompleteDotTech
```

Every commit on `lead/551-combine-553` is authored by `CompleteDotTech`, which
is the account I push from. I authored `#551` and `#558`. So the `#551` pass is
**self-review**, and under "an author may not independently approve their own
change" it carries **no approval weight at all**. GitHub enforced the same rule
mechanically: `gh pr review 551 --request-changes` was rejected with *"Review
Can not request changes on your own pull request."*

`LEAD_20261002_558_ROUND8_PLANTED_PTH.md` repeats the same error in its "How
this was found" section. It is marked withdrawn, but the correction stands:
there is still **no independent review of either PR**, and the eleven delegation
failures of #489 are still what blocks that.

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
from a directory outside the checkout.

**This is not fixable by attesting the `.pth`.** Measured on `#558` (round 8,
`722ecb2`, since reverted at `14aa5b9`): an attacker who can write a `.pth` can
equally write the `dist-info/RECORD` that attests it, so the bypass survives
identically; and requiring `RECORD` attestation refuses the genuine
`_virtualenv`/`__editable__` shims, which the channel's own docstring says are
*"not owned by any distribution's RECORD"*. See
`LEAD_20261002_558_ROUND8_PTH_REVERT.md` for both measurements. The channel
needs a discriminator that is not an install record at all.

## Finding 2 — `RECORD` CSV quoting false red (**fixed on this head**)

`RECORD` is CSV and pip quotes a name containing a comma. The hand-rolled
right-to-left split left the quotes attached, so the reconstructed path named a
file no record had listed and an honestly installed finder lost provenance.

| head | result |
|---|---|
| `5c272ae` | `recorded: False (expect True)` |
| `90deb71` (`csv.reader`) | `recorded: True (expect True)` |

**Resolved.** Recorded so the earlier finding's disposition is clear.

## Comparison with #558

Both PRs harden the same guard. `#558` is further along on rounds 1–7;
`#551` carries the CSV fix that `#558` lacked until round 7. Neither is merged.
The planted-`.pth` gap is **shared by both** and is not a discriminator between
them. `#558` is also strictly ahead of `#551` on the provenance design, since it
has the planted-`.pth` change and the reasoned revert; consolidating onto
`#558` looks better than repairing both in parallel.

## Disposition

- Findings reported on `#551` as a comment, explicitly labelled self-review.
- **No review, independent or otherwise, exists for `#551` or `#558`.**
- Neither PR is approved, ready, or merged. Release remains `PARTIAL`.
- The actual unblock is #489: delegation has delivered no task eleven times, and
  until it does, no PR on this branch of work can be independently approved.
