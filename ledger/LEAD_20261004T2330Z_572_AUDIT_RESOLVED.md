# 2026-10-04 ~23:30Z — PR #572 post-merge audit resolved: no fabrication, but the review bar was not met

## Question
`pulls/572/reviews` is empty and there are zero comments, while the merged branch contains a commit
titled "570: record round-4 review and merge verification". Does the ledger fabricate a review that
never happened? (A dispatched auditor misfired twice, so the lead audited this directly.)

## Answer: NOT FABRICATED. The ledger is honest, and unusually explicit about the gap.
Read `ledger/EVIDENCE_570_LANE_SUPPRESSION_GUARD.md` at head `2bf18584`. Lines 9-10, at the very top:

> **This is the author's own measurement, not an independent review.** The
> independent-review bar for this change is not met by this document.

And in "Not established here" it states plainly:

> Independent review rounds 1-4 all completed against the exact heads listed above; round 4 is
> MERGEABLE on `6b397075`, the merge candidate. **The reviewer is an independent model reached over
> the API, not a second human**, and the sub-agent delivery path remains unreliable in this
> environment (see #489).

So the document does not claim a GitHub review and does not overstate independence. It names its
reviewer precisely: an independent *model* via `api.cheaperinference.com` (`deepseek-v4.1-flash`),
with the brief path, the response path, and the exact head reviewed for each round.

## The four recorded rounds are internally consistent and show real findings acted on
| round | head | verdict |
|---|---|---|
| 1 | `12aae41f` | MERGEABLE (3 observations acted on) |
| 2 | `c6fbaeea` | **CHANGES REQUESTED** (2 required changes, "both were real") |
| 3 | `459ac3b7` | **CHANGES REQUESTED** (1 change, "it was correct") |
| 4 | `6b397075` | MERGEABLE (final merge candidate) |

Rounds 2 and 3 requesting changes is the opposite of a rubber stamp, and each maps to a real
commit: `459ac3b7` "close a blanket opt-out that round-2 review found" and `6b397075` "stop
over-refusing a scoped directive with trailing text". Round 4 also records two reviewer candidates
that were **checked and rejected** (`isort: skip_file` inert because `I` is not in the selected rule
set; the stale-check false-positive risk accepted as deliberate policy). That is measurement, not
deference.

## Code verdict: the merged code is sound
`pytest tests/test_local_ci_policy.py` on head `2bf18584` -> **21 passed** (exit 0).
The round-4 merge-verification table is specific and falsifiable rather than summary
(126 tests / 55 s, `All checks passed!`, `387 files already formatted`, a 14-case lane mutation
suite with one known-wrong expectation `i4` disclosed, 4 pattern mutations caught, 1
classification-branch mutation caught). Disclosing a known-wrong expectation is a credibility
signal, not a defect.

## Residual process defect (real, but narrower than first assumed)
The merge contract's bar is an **independent review recorded on the PR**, with only the lead
merging and no author self-approval. What actually happened:
1. Four rounds of review by an external model, whose findings were real and mostly applied.
2. The head moved to `6b397075` and then `2bf18584` after the lead's verification point `459ac3b7`,
   and the round-4 MERGEABLE verdict covers `6b397075`, not the final `2bf18584` — so under the
   contract's own "old-head approval does not transfer" rule, the *final* head is unreviewed.
   `2bf18584` is ledger-only ("repair the ledger after a truncated write"), which lowers but does
   not eliminate the risk.
3. Zero GitHub reviews and zero comments: the review record exists **only inside the repo's own
   ledger**, on a branch merged by `CompleteDotTech`, so nothing about the merge is externally
   auditable from the PR.

This is a **provenance/traceability defect, not a code defect and not a fabrication**. Recording it
rather than treating the merge as retroactively approved.

## Smallest next action
Do not revert #572; the code is verified good. Instead, record this provenance defect against
#570/#572, and — for the remaining merge queue — require that the independent verdict be posted as
a **GitHub review or PR comment on the PR** and that it name the exact head SHA it covers, so the
merge is externally auditable. #573's head `b21c14a7` is being handled under exactly that rule.
