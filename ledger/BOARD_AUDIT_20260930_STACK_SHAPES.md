# Board audit — every open PR's actual base-to-head shape

Date: 2026-09-30
Author: lead integrator.

Written because finding the #433 branch defect suggested a whole class: a PR
whose base-to-head diff is not the fix it claims. Each PR below was measured
the same way, from the live remote refs rather than from its body.

## Method

```
base_tip = the tip of the PR's declared base branch, on the remote
diff     = git diff --name-only/--shortstat $base_tip..$head_oid
```

The point is to compare what a reviewer would *read* against what would
*merge*, and to catch a PR that quietly carries another PR's code.

## Results

| PR | declared base | base tip | head | files | scope read | verdict |
|---|---|---|---|---|---|---|
| #419 | `master` | `6b72bf6` (master) | `318d9a2c` | 3 | 372 insertions | clean, on master. One `ledger/` brief file rides along. |
| #421 | `fix/418-starred-target-list` | `318d9a2c` (=#419 head) | `e058d96b` | 2 | 210 insertions | clean stack, sits on #419. |
| #424 | `fix/385-position-aware-loop-value` | `6f388cf` (=#412 head) | `590ca17f` | 2 | 569 insertions | clean stack, sits on #412. |
| #435 | `fix/385-position-aware-loop-value` | `6f388cf` (=#412 head) | `470951b` | 2 | 152 insertions | clean, this session's rebuild. |

No `SIMULATION` commit appears in any of these branches, and none of them
carries another PR's code. **#433 was the only contaminated branch on the
board**, and it is closed.

## The ordering this implies

The stack is a chain of branches, each sitting on the one before:

```
master 6b72bf6
  └─ #419  (base master)          -> master
       └─ #421 (base = #419 head) -> master
  └─ #412  (base master)          -> master
       ├─ #424 (base = #412 head) -> master
       └─ #435 (base = #412 head) -> master
```

So #412 and #419 are two independent roots off master, and #421/#424/#435
are leaves. #412's head is an ancestor of #424's head, which means **#424
already contains #412** — merging #424 subsumes #412's content, and merging
#412 afterwards is a no-op rather than a conflict. That is worth knowing
before anyone merges both and assumes a conflict will surface.

## What this does not establish

Shape is not correctness. Every one of these PRs still has **zero reviews**,
and the false-LIVE family in #434, plus the #423 / #425 / #417 residuals,
are open regardless of how clean the branch shape is. A tidy diff is a
precondition for review, never a substitute for it.
