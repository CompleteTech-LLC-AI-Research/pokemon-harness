# LEAD final sync — 2026-10-02T22:40Z

## Trunk

```
master        = e1218c9f547ad670cf0caf5e8ffa43064bdcaf5b
origin/master = e1218c9f547ad670cf0caf5e8ffa43064bdcaf5b
origin/master...master = 0 0
```

Trunk was already identical at the start of this run, so **no fast-forward was
required**. The only movement is the two ledger commits made here:
`e1218c9` is the only new commit on `master`, pushed and verified.

## Preserved, untouched

- 137 dirty/untracked entries — no reset, clean, stash, or broad stage.
- 659 registered worktrees — none removed except the one disposable
  measurement worktree this run created and then removed.
- `pokemon/.venv` `__editable__` finder untouched.
- `/workspace/poke-harness/.scratch/lead555venv` `rep557_paths.pth` verified
  still pointing at `/workspace/poke-harness/.scratch/rep557/src` and
  `.../vendor/pyboy-src`.

## Work completed this run

**#253 timing tier re-measured** — the one outstanding measurement.

425 rows (not the issue title's stale 201). 18 fail, 407 pass. The identical 18
fail on a second run. All 18 carry the read-only-`/dev/shm` signature
(`other=0`) and abort at `multiprocessing.RawValue` before any assertion.

Mechanism demonstrated two ways: an isolated arena redirect turns the
allocation green, and it then exposes a **second** host blocker underneath —
`_multiprocessing.SemLock` POSIX semaphores, which no pure-Python redirect can
reach. Two layers, both host-level, neither fixable from the repository.

Together with the earlier unit-tier decomposition: **20 unit + 18 timing = 38
rows blocked on the same read-only `/dev/shm`, zero deterministic defects in
either tier.** Posted to #253; the issue stays open with the exact missing
resource named (writable `/dev/shm` + pinned CPU allocation). Nothing waived,
nothing marked passing, xfail, or skipped.

Ledger: `LEAD_20261002_253_TIMING_TIER_REMEASURED.md`.

## #551 head moved a second time — force-push

`90deb71` -> `1723ec6` (observed 22:28Z) -> **`1d425ac`** (observed 22:40Z,
recorded by the fetch as `forced update` on both
`fix/534-regular-package-path-portion` and `lead/551-combine-553`).

The new head adds one commit:

```
1d425ac Require frozen code from a trusted finder, and refuse unparseable RECORD rows
```

The rest is the same content as `1723ec6`, re-based onto current `master` (its
history now contains `2cd4bb1` and `0539dc0`). So the movement is a rebase plus
one substantive fix, not a reversal of the round-5 finding.

**This does not change the consolidation recommendation.** #551 still has no
independent review — the prior REQUEST CHANGES was against `90deb71` and does
not transfer to a re-based head. The planted-`.pth` gap is still open on both
#551 and #558, for the same measured reason: `RECORD` attestation is forgeable
by an attacker inside the declared model, and it false-reds genuine
`_virtualenv` / `__editable__` shims. Consolidate onto **#558** `9084db8`,
which is unchanged and is the only head (with #557) that fixes #547.

## Open PRs (live, 22:40Z)

| PR | head | draft | note |
|---|---:|---|---|
| #558 | `9084db8` | no | primary candidate; unchanged all run |
| #557 | `075fff3` | no | unchanged |
| #556 | `187b44a` | no | unchanged |
| #555 | `89c7152` | no | unchanged |
| #551 | **`1d425ac`** | no | moved twice this run |
| #549 | `33cc0e5` | yes | unchanged |
| #548 | `1d4c9e1` | no | unchanged |

## #489 re-probed this run — still blocking

`list_agents` returned normally, and the child transcripts are themselves the
evidence. Nearly every child independently reports the same symptom:

> "Your message came through empty again — no request text attached."
> "I didn't get a task along with the environment details."

Children receive the environment and the `AGENTS.md` settings text but never
the task. Receive works — `wait_agent` returns full child reports — so this
remains a one-directional **delivery** failure, exactly as #489 describes.

Fourteen earlier probes plus this one. The failure is unchanged and not
workaroundable from inside the session.

## Consequence — nothing merged

No PR is merged, and none is marked ready. Every open PR is authored by
`CompleteDotTech`, the account this lead pushes from, so **every review on
them is self-review** and carries no approval weight. The merge contract
requires an independent verdict, and the lead cannot supply one.

- **#558 is not merged**, despite being verified ready: clean merge-tree,
  130/130 guard/provenance tests, hosted CI green at the exact head
  `9084db8`, and all four of #534's acceptance criteria met in both
  directions.
- Release status stays **PARTIAL**.

The complete independent-review brief for #558 remains staged at
`/workspace/poke-harness/.scratch/briefs/REVIEW_558_HEAD.md`, ready to hand to
a working sub-agent the moment delivery is repaired.

## Smallest next action

Repair sub-agent task delivery (#489), then hand `REVIEW_558_HEAD.md` to a
fresh independent reviewer. On a clean verdict, #558 `9084db8` merges
immediately — everything downstream of it is already verified.
