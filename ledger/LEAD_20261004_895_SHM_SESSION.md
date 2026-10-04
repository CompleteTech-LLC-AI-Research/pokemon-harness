# Session — 2026-10-04 — #89.5 audit merged, writable-/dev/shm record corrected

Start: `origin/master` `7d7c2160`. End: `origin/master` `20288570`.
Open PRs at end: **0**. Open issues at end: **30**. Release status: **PARTIAL**.

## Landed

### PR #576 -> `7d09ac7e` — #89.5 promotion-rule audit (no code change)

- Adds `ledger/EVIDENCE_89_5_PROMOTION_RULE_AUDIT.md` only. No source/test/config touched.
- Conclusion: the #89.5 **promotion** rule already exists and holds; the issue's own
  decomposition claim that it "is not implemented" was wrong.
- Four gates in `scripts/_coverage_report_build.py`, each confirmed to have a
  production caller: `_evaluate_case` (138), `_verified_mechanics_case_ids` (223),
  `_check_case_effect` (438), `_family_is_verified` (267).
- Measured through the public API (`result_set_from_document` + `build_report`) on the
  canonical catalog: exactly one combination promotes a family — terminal `passed` for
  every **scope-mandated** runtime with observed effect == declared `effect_id`.
  Refused: source-only, wrong effect, missing effect, skipped/failed/not_run/timed_out,
  the `"tested"` sentinel, and a catalog omitting a mandatory runtime.
- **67** families remain unverified; overall stays `INCOMPLETE`.
- Teeth: dropping the terminal-status conjunct turns **exactly 10** tests red (all named);
  emptying the `required` set turns **0** red.
- Independent review: **MERGEABLE**; its three findings were all corrected in `b0f22e31`.

### PR #577 -> `20288570` — writable-`/dev/shm` provisioning record corrected

- `docs/PRODUCTION_RUNBOOK.md` only.
- The runbook recorded `unshare` -> `EPERM` and concluded no writable shared-memory
  namespace was reachable. **False on this host.** Measured on `7d09ac7e`:
  `unshare --map-root-user -m --propagation private` succeeds, and a private tmpfs
  `/dev/shm` inside it is `rw`; `tempfile` writes and `mp.Queue` both work.
- Why it mattered: `production_gate.py`'s unit tier spawns `multiprocessing` workers
  whose arena allocation needs a writable `/dev/shm`, so the read-only mount turned
  environment failures into apparent product defects on #253.
- Also corrected a stale host fact: `affinity_cpus` claimed `0-11`/12 CPUs; this host
  has 4 (`nproc`=4, affinity `0-3`). Historical 12-CPU samples are retained verbatim
  with a drift note rather than rewritten.
- Independent review round 1: **NOT MERGEABLE** — caught my own unsupported causal
  claim ("remain only because they are deadline-bound"), which I had measured only as
  non-reproducibility. Corrected in `4416e1be`. Round 2: **MERGEABLE WITH MINOR
  NOTES**; both notes fixed in `3a1445c7`.

## Environment facts measured (no operator action available)

| fact | value |
|---|---|
| CPUs | 4 (`nproc`=4, affinity `0-3`) |
| `load1` during runs | 8.8–12.3 |
| `cpu.max` | `max 100000` (read-only cgroup, no writable leaf) |
| `CapEff` / `CapBnd` | `0` / `0`; uid 1000; no sudo |
| `unshare --map-root-user -m --propagation private` | **succeeds** |
| private `cgroup2` remount | `EPERM` |
| writable `/dev/shm` | **obtainable** (private tmpfs in a user namespace) |
| ROM/symbol/fixture assets | **absent** everywhere under the workspace |

## #253 re-measurement (issue stays open)

Seven failing unit-tier files, same interpreter and command:

| condition | failures |
|---|---|
| read-only `/dev/shm` (as previously recorded) | 17 |
| writable `/dev/shm` (corrected) | 9 |

`tests/test_probe_owner_phases.py` went **fully green** (deterministic).

The residual 9 are **not stable** across identical runs on an unchanged tree:
**9, then 6, then 5**. All surface as `timed_deadline`. That is consistent with a
load-induced deadline cause and equally consistent with a latent intermittent defect.
**Cause is not established** — do not read "9 remain" as "9 are environmental."
No deadline was enlarged, no test skipped or xfailed. Acceptance criterion 1 unmet.

## Blocked — unchanged, no action available in this environment

- **#84/#85/#86** — need an operator-declared CPU allocation. Private `cgroup2` is
  `EPERM`; `cpu.max` has no writable leaf.
- **#90–#105, #235** — need real ROM/symbol/fixture assets; none exist.
- **#89 (89.2, 89.3, substantive 89.5)** — need terminal real-ROM battle-mechanics
  evidence to promote the remaining 67 families.
- **#253** — residual 9 need a quiet host or real allocation to classify.
- **#489** — collaboration sub-agent task delivery still broken (see below).
- **#72, #108** — full source/native qualification needs #84/#85 resolution.
- **#110** — event-aware bounded batching: implementation needs #88 acceptance plus a
  declared allocation for the overhead measurement; both unmet.

## #489 re-check

Not re-exercised this session; the blocker recorded in the issue stands. Independent
review in this session used the external harness at `/tmp/rev-b3484c68/run_review.py`
(model `deepseek-v4.1-flash`), which is **not** the required multi-agent lane structure.

## Gates on the exact merge trees

```
PR #576 head b0f22e31: import-origins PASS; ruff check/format PASS;
  pytest (3 battle-coverage suites) 70 passed; hosted run 37189124921 pass (8m17s)
  post-merge on 7d09ac7e: 70 passed, ruff clean, tracked tree clean
PR #577 head 3a1445c7: docs only; git diff --check clean; ruff clean (no code touched)
  hosted run 37190556897 pass on head 3a1445c7
  post-merge on 20288570: tracked tree clean, byte-identical to master
```

## Notes for the next session

- The repo's import-origin guard refuses any venv whose `pokered_harness`/`pyboy`
  resolve outside the checkout. `pokemon/.venv` fails (`_virtualenv.py` is attested by
  no install RECORD). A venv must be `pip install -e ".[dev]"`-installed **against the
  checkout being tested**, one venv per worktree — shared venvs across worktrees will
  refuse to collect.
- Reproducing the #253 measurement requires the private-namespace wrapper described in
  PR #577; a bare run keeps the read-only `/dev/shm` and shows 17, not 9.
