# Audit: were #534 and #547 closed correctly?

Date: 2026-10-02
Refs: `origin/master` `b15575c`
Status: **#534's closure is justified. #547's was not — reopened.**

## #534 (closed 2026-10-02T13:09:19Z)

Closed after five close/reopen cycles during the day (09:13 closed, 09:21
reopened, 09:25 closed, 09:27 reopened, 09:42 closed, 10:07 reopened, 10:30
closed, 12:25 reopened, 13:09 closed). All by `CompleteDotTech`.

I checked whether that was premature. The four acceptance criteria on
`origin/master`:

| AC | Verdict on master | Evidence |
|---|---|---|
| 1. CI cannot import from another tree | met | `run_import_origin_preflight` at `production_gate_execution.py:262`, inside `run_collection_preflight` |
| 2. Fails loudly before the tier runs | met | same call site, ahead of pytest collection |
| 3. `run_local_ci.sh` + `production_gate.py` covered | met | `run_local_ci.sh:306` invokes `production_gate.py --unit-only`, which reaches the preflight |
| 4. Fail on mis-pointed, pass on repaired | met | see below |

Live, on a fresh worktree at `origin/master`, using the flag combination
`run_local_ci.sh` actually uses:

```
$ production_gate.py --runtime-mode source --unit-only --repeat-timing 5
  PASS import-origins: returncode=0 duration=0.3s
```

And the failure direction against a root the interpreter does not import is the
same FAIL already demonstrated on the #558 merge tree.

**So #534's guard work is genuinely on trunk and the closure is correct.** The
seven open PRs refine that guard further; they are not the difference between
"fixed" and "not fixed" for the issue's stated criteria. Master is simply a
weaker, earlier version of the same guard.

## #547 (closed 2026-10-02T22:07:17Z)

Closed at 22:07:17Z — 53 seconds before my comment on this issue asking for it
to be left open, and 53 seconds after `gh issue list` had already shown it open.
Reopened with evidence.

The issue's own suggested repair came with a one-line acceptance test:

> assert `check_origins(tmp_path, ())["status"] == "FAIL"`

Run verbatim against `origin/master`:

```
check_origins(tmp_path, ())['status'] == PASS
#547's suggested test: FAILS on origin/master
```

`git grep "no packages were selected" origin/master` returns nothing, so the
fail-closed repair has never landed on trunk. The vacuous PASS is reachable
through the public function today.

The closure had a defensible basis — the defect was reported against already-
merged PR #546, so closing it was not unreasonable — but the defect itself was
never repaired there. **This is the first functional difference between the
#534 branches**: #558 and #557 carry the fix; master, #551, #548 and #549 do
not.

## Consequence

Merging #551, #548 or #549 would land without this repair. #558 is the only
reviewed-equivalent path that carries it, which strengthens the case for
consolidating onto #558 — but it remains gated on independent review.

## Disposition

- #534: closed, closure verified correct, left closed.
- #547: **reopened**, defect reproduced on trunk with the issue's own test.
- #554: open, awaiting an operator decision.
- Nothing merged. Release remains `PARTIAL`.
