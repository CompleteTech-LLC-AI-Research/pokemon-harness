# Timed MCP frame-deadline comparison protocol — 2026-09-21

[Issue #84](https://github.com/CompleteTech-LLC-AI-Research/pokemon-harness/issues/84),
leaf **84.1** ("Freeze a comparison protocol"). This document freezes the
comparison *before* any rerun: it fixes the retained failure evidence, the
conditions that may not change, the capacity measurement, the terminal
artifacts each run must produce, and the decision rules that leaf 84.3 will be
judged against.

It is a specification and an evidence index. It records no acceptance result.
No timed orientation is claimed passing, no deadline, policy value, or
assertion is relaxed, and the cause of the observed failures remains
unresolved. Parent acceptance for #84 (nine ordinary orientations in both
runtimes, then the fresh #72 gate) stays open until the runs below exist.

**Revision 2 (2026-09-21).** Independent round-1 review of revision 1
(`1ff3713`) returned CHANGES_REQUESTED with five findings; revision 2 addressed
all five, anchored to source artifacts rather than to prose:

| Finding | Correction in revision 2 |
|---|---|
| F1 (P1) causal rule promoted two passes to a conclusion | §7 pre-registers an explicit inconclusive outcome and lists the prerequisites for causal attribution; §5/§5.1 supply the during-row capacity control it depends on. |
| F2 mandated cadence contradicts the frozen watcher | §5 states the frozen watcher's real cadence, defines the permitted observation gaps, and keeps the historical samples labelled with the cadence they actually have; §2.6 is relabelled accordingly. |
| F3 admission is not executable as frozen | §5.1 is a capacity/admission state table with one defined outcome per state, and carries #85's allocation requirement forward as a named prerequisite. |
| F4 terminal outputs omit mandatory fields on the success path | §6.1 freezes a field-to-output mapping for both outcomes and labels every value captured, derived, or prerequisite-bound. |
| F5 the runner configuration is not bound to the candidate | §9.2 requires a candidate-bound launch plan, an import-origin check, and the recorded effective configuration; re-aiming the asserted SHA alone is declared insufficient. |

**Revision 3 (2026-09-21) — response to the round-2 review.** Independent round-2
review of revision 2 (`codex2`, head `6043357`) confirmed **F2** and **F5**
closed, but returned CHANGES_REQUESTED because **F1**, **F3**, and **F4** were
only partially corrected, and recorded two defects revision 2 itself
introduced (an overlapping `S2`/`S3` validity rule, and success-path output
provenance that no output can supply). Revision 3 is the response to that
review; it does **not** claim that all findings are closed:

| Round-2 finding | Revision-3 correction |
|---|---|
| F1 (P1) the failure arm still excluded capacity-only causation automatically | §7's failure arm is re-pre-registered as **"controlled failure; cause not established"**; excluding capacity-only causation now requires a defined, reviewed discriminator over the failed request's own scheduler/progress evidence (§7). |
| F3 (P2) `S2`/`S3` assign incompatible outcomes to the same breach | §5.1 makes `S2` and `S3` mutually exclusive, binds admission expiry, maximum launch delay, during-row coverage, and allocation loss, and requires a named, pinned gap/admission validator that the unchanged watcher does not itself provide. |
| F4 (P2) required fields map to outputs that cannot supply them | §6.1 maps every field to an output/assertion that actually produces it, distinguishes sampled CPU, sampled lifetime bounds, and whole-row wall time, and declares observation-only prerequisites for genuinely missing values. |

Revision 2 and revision 3 record no execution and no acceptance result. Every
identity in §2 is unchanged and was re-verified statically, without an emulator.

## 1. Frozen decisions

1. The six failed orientations, both failed single-case Red/Red reproductions,
   the scheduler diagnostic, and the capacity-control window are **retained as
   evidence** and are never relabelled as passes or overwritten (§2).
2. The request deadline, pair bound, owner policy, and result assertions stay
   **exactly as recorded** (§3).
3. Every comparison row runs against a **frozen candidate identity** that is
   recorded with the run; the historical `7516f16` evidence is never merged
   with a new candidate's results (§4).
4. Capacity is judged by the **pre-existing read-only control definition**
   (CPU-pressure `some` avg10 and avg60 below 10%, one-minute load below the
   allowed CPU count, held continuously for 60 s) **at the frozen watcher's own
   sampling cadence**, before a comparison row is admitted, and with the
   admission, control-loss and sample-gap outcomes of §5.1 (§5).
5. Each row must produce the **terminal artifacts in §6**; a missing or
   non-terminal artifact is not a result.
6. The 84.3 outcome is selected by the **pre-registered rules in §7**, not by
   the convenience of whichever evidence survives.
7. The actions in §8 are prohibited regardless of outcome.

## 2. Retained evidence (frozen; identities re-verified 2026-09-21)

All identities below were re-computed on 2026-09-21 from the private evidence
root retained for this issue and match the values recorded when the runs were
made. The evidence root is operator-managed and untracked; only names and
digests appear here.

| Artifact | Bytes | SHA-256 | Establishes |
|---|---:|---|---|
| `full-gate-72f-raw-output/cython/remote-1.log` | 789334 | `6021b9ae934d64b687f23fb060db9fda3dd6adbe0caa1ed0fa0a0658bbb3eb57` | Complete native remote-tier output of attempt `72f` |
| `full-gate-72f-reports/pokered-gate-cython-fd_i1bt5/remote-1.json` | 214956 | `30cce4df784f2d6035284f264feb723d749f1f51933001dcf40a594bbc62da13` | Terminal pytest report: per-node outcomes and counts |
| `native-timed-mcp-84-before-peers.json` | 418565 | `81179c44fb3e3572f72cf82f065ce12ebb21274e0c51cb7ee9a9edea1b0970f6` | Per-peer failure, prior/cached status, RPC result, and teardown records; also records `raw_sha256` = the log above |
| `native-timed-mcp-84-baseline1.log` | 11692 | `b5a21f0db8fe72599f094f936a515d4699e32bade5333f8027574c9afcc1a65d` | Native Red/Red ordinary reproduction output |
| `source-timed-mcp-84-baseline1.log` | 11707 | `505ea4f7f119b8e08d8891d81a2d6bacabe48248edc3122b6405fb29bba23378` | Source Red/Red ordinary reproduction output |
| `native-timed-mcp-84-schedstat1.log` | 11693 | `6147688eedabaecc818becd4aa253732cdea7473a3a70185a78a67e2e67933fe` | Bounded native scheduler diagnostic output |
| `timed-mcp-84-scheduler-verification.json` | 33605 | `a45a770f7fe53dd954b0495ee80defa6d5a3cc950cde231946a037018789ddda` | Per-thread runtime/runqueue deltas and build-identity check |
| `native-timed-mcp-84-schedstat1-host-samples.json` | 210189 | `8f32a4a4680f3865e3ed3498d8e51072d97fd34715f4c1c8ba297608ad162ffe` | Sampled host pressure and per-thread counters |
| `cpu-capacity-84-window1-samples.jsonl` | 116448 | `9abbecdf1da446b9b7f750727702ee10ad95d67d11033874ca9c2248f6b92bc8` | 359 capacity samples |
| `cpu-capacity-84-window1-result.json` | 634 | `040bb76bbeaff54d4d2d6ebde36d4da7c863390248feb1310562ac37e643e805` | Terminal capacity-window result |
| `run_timed_mcp_84.py` | 5847 | `aa4ba995113ab30f2b52d29bf18d428b761fd42ac017f54a111fce9023e24d14` | Single-case observer actually used for both baseline rows |
| `run_timed_mcp_84_schedstat.py` | 7167 | `ef78eb21959c2b4c23a12d332b239a75fc6f39f22100d6b8b2422aee0fe6c396` | Scheduler-diagnostic observer actually used |

The two supported runner sources that have **not** yet produced a comparison
result are frozen here as the declared tools for the rerun, with the identities
of the copies on disk:

| Runner | Bytes | SHA-256 |
|---|---:|---|
| `run_timed_mcp_84_matrix.py` | 6293 | `fa064cbe5600f6e562265cb1bc5f1454ac4d69f97a1f0a73c2a16643281fa6cd` |
| `watch_cpu_capacity_84.py` | 3419 | `24d719bb4d1d0fad8b8cfcf79f14fc59fdb37e7a78cc7bfd31401acf3caa15bf` |

§5.1's runner prerequisite is discharged by two tracked components, added
under issue #84. They are declared here with the identities of their sources at
this revision, so a row record can name the exact admission machinery that
admitted it:

| Component | Bytes | SHA-256 | Establishes |
|---|---:|---|---|
| `scripts/timed_frame_window.py` | 14760 | `9869ca252701705594c1c91eb1e65a1cd6432260dc662e8f817ea0e9763eb70f` | The named, pinned gap/admission validator (§5, §9.1) and the read-only §5 sample |
| `scripts/timed_frame_admission.py` | 32586 | `adee9a952dad3b9ed0f1aeb7cd4bb742447473a5f68b7d7d4849c990d1c59659` | The declared per-row admission wrapper: fresh window per row, during-row sampling, `S0`–`S6` classification, `S5` stop |
| `scripts/timed_frame_runner.py` | 5240 | `914071fb120cef2a6633cae7281d05424cdc2368442665f1f19712fac70c3517` | The wrapper's executable half: runs each row's unchanged command and captures its terminal result |

The validator is pinned by name as well as by digest:
`timed-frame-gap-admission-validator`. A run records
`validator_identity()`, which reports the validator's name, source file, byte
count, SHA-256, and the procedure it applies, so the admission decision for a
row is reproducible from the retained record alone. These byte counts and
digests are **not** the row's own `§6` output digests; a row that runs must
re-compute both from the retained sources and record the values it actually
loaded.

### 2.1 Original full-gate failure — attempt `72f` at `7516f16`

The complete native remote tier collected 21 tests and exited `1`:
**15 passed / 6 failed / 0 errors / 0 skipped / 0 xfailed / 0 xpassed**, in
`231.61s`. The tier composition, read from the terminal report, is:

| Group | Node | Outcome |
|---|---|---|
| Remote handshake, 9 ordered pairs | `tests/test_link_integration_remote.py::test_remote_handshake_writes_status_on_both_sides[...]` | 9 passed |
| Real-link backend attach | `tests/test_mcp_real_link.py::test_mcp_remote_link_attaches_native_serial_backend` | passed |
| Real-link backend attach | `tests/test_mcp_real_link.py::test_mcp_local_link_attaches_native_serial_backend` | passed |
| Timed MCP stdio pair, 9 orientations | `tests/test_mcp_timed_rom.py::test_timed_rom_stdio_pair[...]` | 3 passed / 6 failed |
| Subprocess link-menu over TCP | `tests/test_pyboy_link_session_subprocess.py::test_subprocess_pair_reaches_link_menu_over_tcp` | passed |

The three timed orientations that passed are
`blue_color-listen-yellow-connect`, `yellow-listen-red_color-connect`, and
`yellow-listen-yellow-connect`. The six failures are §2.2. Handshake,
direct-dispatch, and subprocess rows passing means the tier failure is specific
to the timed stdio pair path, not to asset loading or process startup.

### 2.2 The six failures: first rejected paired frame

Read from `native-timed-mcp-84-before-peers.json` (`frame` on each peer's
failure record). All six requested `count=1` per frame:

| Orientation | First rejected paired frame |
|---|---:|
| `red_color-listen-red_color-connect` | 3 |
| `red_color-listen-blue_color-connect` | 1 |
| `red_color-listen-yellow-connect` | 1 |
| `blue_color-listen-red_color-connect` | 1 |
| `blue_color-listen-blue_color-connect` | 1 |
| `yellow-listen-blue_color-connect` | 2 |

Observed peer errors: `TimedOwnerError: request deadline expired` on the peer
that owned the expired request, and on the other peer either
`TimedOwnerError: request cancelled` (Red-listener/Blue-connector) or
`ChannelClosed: peer closed connection` (Blue/Red, Blue/Blue, Yellow/Blue).
The retained per-peer record therefore shows **three** peers with
`ChannelClosed: peer closed connection`, plus one orientation where both peers
report `request cancelled`. Historical prose describing "two cases" with the
other peer's channel closure undercounts the retained record by one; the
per-peer JSON is authoritative.

### 2.3 Partial progress and post-failure teardown (distinct evidence)

Partial progress is recorded, not a completed frame and not a proven protocol
deadlock:

- Red/Red: both peers had completed two paired frames, public `current_tick` 4,
  no active episode, no pending delivery/permit, zero debt, no terminal
  failure; the connector's captured accounting reached local half-cycles
  `401772`, peer half-cycles `401344`, raw CPU clock `20683191988`.
- Blue/Blue first-frame failure: captured connector accounting reached local
  half-cycles `124780`, peer half-cycles `124328`, public tick `2`.

Teardown succeeded and does not negate the failures. Across the six cases all
**12 peers** record `returncode 0`, `forced false`, `group_alive false`, and
`eof: spontaneous_zero_exit` in phase `paired_step_failure_cleanup`. Peers
whose status was cached after automatic cleanup report null accounting; missing
intermediate state must not be inferred from that.

### 2.4 Source/native Red/Red ordinary reproductions

Both ran the single node
`test_timed_rom_stdio_pair[red_color-listen-red_color-connect]` on unchanged
clean `7516f16`, with the ordinary entrypoint and no code, policy, or deadline
change. Each failed on **frame 1** with one failure and no skips/errors:

| Runtime | pytest exit | Elapsed | Peak CPU of the two MCP processes |
|---|---:|---:|---|
| native | 1 | 27.446455 s | 2.19 s and 2.08 s |
| source | 1 | 22.989090 s | 3.41 s and 3.41 s |

Both rows ended with `final_commit` `7516f16` and an empty tracked status: the
initial full-gate failure is **not** demonstrated to be a native-only defect.
Low process CPU relative to elapsed time is suggestive but also includes
legitimate protocol waits, so it alone does not prove scheduling starvation.

### 2.5 Scheduler diagnostic

One external, read-only per-thread observation of the same native node on
`7516f16`, with the ordinary MCP child entrypoint and no ROM hook, wrapper,
policy, or deadline change. It reproduced the frame-1 failure
(`exit_status 1`, 1 failed, no skips/errors) and confirmed severe runnable
delay inside a `9.264482 s` sampled interval:

| Peer | Busiest thread runtime | Runnable wait on run queue |
|---|---:|---:|
| listener | 1.578329 s | 6.553236 s |
| connector | 1.544002 s | 6.952642 s |

The record also confirms all 58 installed native extension hashes still match
the recorded complete build. These are kernel scheduler counter deltas, not an
inference from low CPU usage. They establish delay **during this
reproduction**; they do not by themselves distinguish starvation from
owner/transport/runtime overhead, and they are scoped to `7516f16`.

### 2.6 Capacity-control window

A read-only observation from `19:48:09` to `20:18:11` UTC on 2026-09-13,
`1801.336221 s`, 359 samples, no emulator or gate execution. The planned
control was CPU-pressure `some` avg10 < 10%, avg60 < 10%, one-minute load below
the 12 allowed CPUs, continuously for 60 s, within an 1800 s bound.

Observed: one-minute load `51.37`–`97.56`; avg10 `46.91`–`84.45`; avg60
`56.54`–`79.03`. **Zero samples met the control**, so the observer exited `2`
with `NO_QUIET_CONTROL_WINDOW`. That is an observation result, not a test
failure, and these thresholds select a conservative comparison condition rather
than a measured universal product performance floor.

These 359 samples were taken at the frozen watcher's **five-second** cadence
(§5), not at one second: consecutive gaps run from `5.000317 s` to `5.256798 s`,
median `5.002217 s`. The retained description here and the cadence rule in §5
must agree; the samples are never re-described as a one-second series.

## 3. Conditions that must not change

These are the exact values in the code at the frozen candidate. A comparison
that alters any of them is not the frozen comparison and its result does not
count:

| Condition | Value | Source |
|---|---|---|
| JSON-RPC request bound (timed stdio client) | `CALL_BOUND = 20.0` | `tests/test_mcp_timed_stdio.py` |
| Test process-exit bound | `EXIT_BOUND = 12.0` | `tests/test_mcp_timed_stdio.py` |
| Whole-pair bound | `PAIR_BOUND = 180.0` | `tests/test_mcp_timed_rom.py` |
| Owner policy | `POLICY = ALIGNED_DIAGNOSTIC_PROFILE` | `tests/test_mcp_timed_rom.py` |
| Frames requested per row | 3 paired `step(count=1)` | `tests/test_mcp_timed_rom.py` |
| Orientation matrix | the 3×3 `families × families` product, 9 ordered rows | `tests/test_mcp_timed_rom.py` |

The aligned diagnostic profile is the explicitly non-default profile already in
use, and it is unchanged:

```text
quantum_cycles=256
rearm_budget=4096
rearm_instruction_cap=1024
max_edge_lateness=4096
operation_timeout=5.0
request_timeout=10.0
lock_timeout=2.0
close_timeout=5.0
max_wait_attempts=64
inbound_capacity=256
queue_capacity=16
```

The matrix runner's `1800 s` value is a **whole-nine-case supervisor bound**,
not a test bound; the per-pair and per-request bounds above remain in force
inside it. Deadlines, assertions, and the selected profile are not tuning
inputs, and an observed failure is never converted into a pass by a longer
wait.

## 4. Candidate identity

The retained evidence in §2 is scoped to `7516f1655fce3682a6b668bad5f13502290d7775`
and stays scoped to it. The comparison must name its own frozen candidate:
exact commit SHA, clean tracked status, separate source and native
interpreters, native build identity (installed-extension hashes, not a
source-shadowed import), ROM/symbol/fixture SHA-1 pins from `VERSIONS.md` and
`release-evidence/fixture-manifest.json`, and the host conditions at execution.
Evidence from different candidates is never combined into one result.

The supported Red/Red inputs are pinned as: Red color ROM
`e1deed63080bc24cad5fba18ecb3184f905d16d4`, Red symbols
`03783c86a42588bd77f73bd7814cf8d70e590118`, Red ordinary fixture
`546d7edaf7c3a987f86ae86a97066c7d619eefbb`. ROM, symbol, state, and raw-log
bytes remain private and are never committed or published.

## 5. Capacity measurement

Capacity is measured by the read-only control already used for this issue, with
its qualification thresholds unchanged. Two things are now stated explicitly
because revision 1 left them ambiguous: the **cadence the frozen watcher
actually uses**, and the **allocation** the control is supposed to describe.

**Cadence (F2).** The frozen watcher is `watch_cpu_capacity_84.py`, pinned in §2
at 3419 bytes / SHA-256
`24d719bb4d1d0fad8b8cfcf79f14fc59fdb37e7a78cc7bfd31401acf3caa15bf`. It samples
`/proc/loadavg` and the `some` line of `/proc/pressure/cpu` and then sleeps
**five** seconds (`time.sleep(5)`, line 85), so the pre-admission observer takes
a sample every ~5 s. Revision 1's "once per second" described the separate
in-test observer loop, not this watcher; the two are not interchangeable. The
359 retained samples of §2.6 have gaps of `5.000317 s`–`5.256798 s`, median
`5.002217 s` — the frozen watcher's own cadence.

- the window qualifies when `avg10 < 10%` **and** `avg60 < 10%` **and**
  one-minute load `< allowed CPU count`, all held continuously for 60 s;
- the observation is bounded at 1800 s and takes no emulator slot;
- **permitted sample gaps.** A continuity run stays valid only while every
  consecutive sample gap is at most `6.0 s` — the largest gap actually observed
  (`5.256798 s`) plus slack for a single scheduling delay. A longer gap breaks
  continuity: the 60 s interval restarts from the next sample, and the gap is
  recorded in the window record. An unrecorded or unbounded gap is not a
  qualifying window;
- a qualifying window is required **before** each comparison row is admitted,
  and capacity is sampled **during** the row as well (§5.1).

**The frozen watcher does not enforce the gap rule (F3).** The pinned watcher
updates its quiet run from quiet *threshold* samples only and exits when the
quiet run reaches 60 s; it applies no maximum-gap reset, because the `6.0 s`
continuity rule above was introduced by this protocol after the watcher was
frozen. The unchanged watcher therefore cannot itself enforce the gap rule,
admission expiry, or per-row admission. A **named, pinned gap/admission
validator** — or an explicit read-only validation/restart procedure over the
retained sample series — is required before the first controlled row and before
each later matrix row (§5.1, §9.2), and the unchanged watcher is never described
as applying the reset rule.

A one-second watcher may be used **only** as an explicitly declared
substitution: named, pinned by source and SHA-256 in the §2 table, and recorded
as a rerun prerequisite of §9 before it observes anything. A faster cadence
must never be reported as if it were the frozen observer's own.

**Allocation (F3).** Leaf 84.2 requires the comparison to run under #85's
verified allocation. #85 distinguishes an actual operator-controlled
reservation from CPU affinity or a cgroup quota, and requires the allocation
facts to be retained. Aggregate `/proc` averages and a granted emulator slot do
**not** identify an allocation and do not by themselves demonstrate that
capacity was available to the peers:

- the rerun records which allocation was held, its extent, its span, and the
  fact that it is a reservation rather than affinity or a quota;
- the allocation identity is retained with the row record (§6, item 6);
- when no allocation is held the row is not admitted, whatever `/proc` shows
  (§5.1, states `S0`/`S5`).

Host load alone never establishes the cause. A row that fails outside a
qualifying window is not evidence of a product defect, and a row that passes
outside one is not evidence that capacity explains anything.

### 5.1 Capacity/admission state table (F3)

One row has exactly one state, decided from that row's own pre-admission
window, its during-row samples, and its allocation record — never from the
overall host history.

| State | Evidence | Row outcome | Remaining rows |
|---|---|---|---|
| `S0` not admitted | no qualifying window, or no allocation held, before the row | the row is **not dispatched**; nothing is recorded as a pass or a failure | wait for a fresh window under §9.1 |
| `S1` admitted, control held | qualifying window before the row, allocation recorded, every during-row sample inside the qualifying bounds with gaps within the permitted maximum | a **valid controlled observation**: a pass or a failure is read by §7 | the next row re-enters at `S0`; admission is never inherited |
| `S2` admitted, pressure rose but stayed within every bound | as `S1`, except a during-row sample was strictly above the highest sample of its admission window while **every** during-row sample stayed inside every qualifying bound and gaps stayed within the permitted maximum | a **valid controlled observation**, retained and flagged `during_row_pressure_rise`; a failure here is a failure and a pass here is not evidence that capacity explains anything | the next row re-enters at `S0` |
| `S3` control lost | admitted, then a during-row sample **breached** a qualifying bound (`avg10 ≥ 10%` **or** `avg60 ≥ 10%` **or** one-minute load ≥ the allowed CPU count), **or** the recorded allocation was lost during the row | retained and marked **`inadmissible`**: it counts as **neither pass nor failure** for §7 and must not be reported as a product failure | stop dispatching; remaining rows wait for a fresh window under §9.1 |
| `S4` sample gap | as `S1`/`S2`, but a during-row gap exceeded the permitted maximum | as `S3` (`inadmissible`), with the gap recorded | as `S3` |
| `S5` admission unavailable | no fresh qualifying window (or no allocation) when the next row is due | no further row is dispatched; remaining rows are reported **`not run — blocked on the capacity prerequisite`** | the run stops; the matrix does not report success |
| `S6` failed inside a valid window | the row failed in state `S1`/`S2` | §7's failure arm applies: bounded diagnostic, then a failing regression at the frozen candidate | that row is **never** re-run for admission reasons |

`S2` and `S3` are disjoint by construction: `S2` requires every during-row
sample to stay inside every qualifying bound, and `S3` requires at least one
bound to be breached (or the allocation to be lost). No single sample can
satisfy both, so a breach is never read as a benign rise or vice versa.

**Admission bounds (F3).** These bound how a qualifying window may be used:

- **Admission expiry.** The qualifying window ends at its final quiet sample.
  Admission is valid only if that sample is quiet and the window's gaps were
  within the permitted maximum.
- **Maximum launch delay.** Dispatch of the admitted row must begin within one
  permitted gap (`6.0 s`) of that final sample. A longer recorded delay expires
  the admission, and the row re-enters at `S0` rather than being dispatched.
- **During-row coverage.** The row is sampled at the frozen cadence (§5) across
  its whole duration, plus one final sample at or after the row terminates. A
  missing or over-long interior gap makes the row `S4`; an absent final sample
  leaves the row non-terminal.
- **Allocation loss.** Losing the recorded allocation at any point during the
  row makes the row `S3`.

Three rules make the table unambiguous:

1. **Re-running an `inadmissible` row is not "retrying until green."** `S3`/`S4`
   discard the *observation*, because the control was invalid; the replacement
   observation is judged on its own merits. A row that *failed* in `S1` or `S2`
   is never re-run to obtain a different result, and no `S3`/`S4` row may be
   silently counted as either outcome.
2. **Capacity loss is not a product defect**, and a failure outside a valid
   window is not evidence of one.
3. **Admission is per row.** The nine orientations are nine rows; each
   re-enters at `S0` and requires its own fresh window. A first row's window
   never admits the second.

**Worked timeline (authored illustration, not a run).** A row is admitted in
`S1` from a fresh window, and dispatch begins `2 s` later — within the `6.0 s`
launch bound. 40 s in, a sample shows avg10 `6%`: higher than during admission,
but still inside every qualifying bound, so the row stays a valid controlled
observation, flagged `during_row_pressure_rise` (`S2`). The next row re-enters
at `S0`. If instead a during-row sample breaches a qualifying bound (for
example avg10 `18%`), the row is `S3` (`inadmissible`): it counts as neither
outcome and dispatch stops. If the observer then finds no fresh window, the
remaining rows are `S5` — reported `not run — blocked on the capacity
prerequisite` — and the matrix does not report success. A `7.4 s` gap in one
row's samples makes that row `S4` (`inadmissible`), with the gap recorded. If
dispatch were delayed `9 s` after the window closed, the admission would have
expired and the row would be `S0`, not dispatched. No sequence of these states
turns an `S3`/`S4` row into a pass or a failure, and no `S3`/`S4` row is re-run
to obtain a different result (§7).

**Runner prerequisite.** Per-row admission is not implementable with the frozen
matrix runner as it stands. `run_timed_mcp_84_matrix.py` (pinned in §2 at 6293
bytes / SHA-256
`fa064cbe5600f6e562265cb1bc5f1454ac4d69f97a1f0a73c2a16643281fa6cd`) launches
**one** pytest selection containing all nine orientations (`:35-40`) and then
samples the host globally while it runs (`:86-90`); it has no inter-row
admission check and no admission pause, so a successful first row can be
followed automatically by an inadmissible second row. The nine-orientation
expansion therefore requires a declared, hashed admission wrapper that waits
for a fresh qualifying window before each of the nine rows, samples during each
row, classifies each row by this table, and stops dispatching in `S5`. Until
that wrapper is named and pinned, the nine-orientation expansion does not start.
The **first controlled row** needs the same machinery: because the unchanged
watcher enforces no gap reset (§5), a named, pinned gap/admission validator — or
an explicit read-only validation/restart procedure over the retained sample
series — must exist and be recorded with the row before even the first row is
dispatched. The frozen runner's own `1800 s` value remains only the supervisor
bound of §3.

The wrapper and validator are tracked as `scripts/timed_frame_admission.py` and
`scripts/timed_frame_window.py`, pinned in §2. The frozen runner is **not**
modified and is never described as applying this table: it remains the tool that
dispatches one row, and the wrapper is what decides whether that row may be
dispatched and whether its result is admissible.

The wrapper is a prerequisite, not a substitute for the others. In this table,
`S1`/`S2` still require a *qualifying window* under §5 and a *recorded
allocation*, so the wrapper alone cannot admit a row: with no verified
allocation it stops at `S0`/`S5`, and the per-row window is never inherited from
an earlier row. Its own tests fail if a row could be dispatched without either.

## 6. Required terminal artifacts

Every row must retain all of the following; a run that cannot produce them is
not a terminal result:

1. **Identity** — candidate commit, clean-status check, runtime mode, the
   interpreter path used, native extension count/hash match when native, and
   the ROM/symbol/fixture SHA-1 pins.
2. **Command and bounds** — the exact pytest invocation and the unchanged
   bounds/policy from §3.
3. **Terminal pytest result** — exit status plus counts (`passed`, `failed`,
   `errors`, `skipped`, `xfailed`, `xpassed`, `total`) and per-node outcomes;
   JUnit XML where the run collects it.
4. **Per-row observations** — CPU execution and wall time for the MCP
   processes, runnable-queue delay **from the scheduler observer of §2.5** (it
   is required for the controlled rows, not optional — see §6.1), owner progress
   (public tick, half-cycles, raw CPU clock, active episode, pending
   delivery/permit, debt), frame accounting (frame index, phase, role), and
   structured step results.
5. **Teardown** — per peer: return code, `forced`, `group_alive`, EOF/disconnect
   classification, and the phase in which cleanup ran.
6. **Capacity context** — the qualifying window record, the during-row samples,
   and the allocation record (which allocation was held, its extent, its span,
   and the fact that it is a reservation rather than affinity or a cgroup quota,
   §5).
7. **Digests** — SHA-256 of each retained output, so the record is
   self-checking.

The comparison consumes the runners already validated for this issue: the
single-case observer, its scheduler variant, and the nine-orientation matrix
runner that stops at the first failure. The matrix runner additionally asserts
9/9 non-skipped passes before it can report success, and asserts the final
commit equals the starting commit with a clean tracked status.

### 6.1 Field-to-output mapping (F4)

Fields 1–3 and 6–7 of §6 come from the runner and the pytest process without any
observation change. Field 4's frame accounting and step results, and field 5's
EOF/disconnect classification and cleanup phase, are **not** serialized on the
success path: the successful ordinary row prints a single `TIMED_ROM_SMOKE`
report (`tests/test_mcp_timed_rom.py:419-443` — `profile`, `policy`, `listener`,
`connector`, `completed_frames`, `prelink_press_release_restored`,
`prelink_input_drain_frames`, `baseline_statuses`, `final_statuses`, and one
`processes[]` entry per peer with exactly `pid`, `returncode`, `forced`,
`group_alive` — `:430-437`). That report carries **no** per-step results, **no**
CPU time, and no `phase` or `eof` field; the richer
`structured_step_result`/`phase`/`eof` records exist only when the paired-step
helper `_step_frame` raises (`:273-339`), and neither the external observers nor
the `_gate_report` plugin fills the missing success fields. Revision 1 therefore
required artifacts the success path does not emit, and revision 2 mapped some
fields to outputs that cannot supply them. Each value below is labelled by how
it is actually obtained:

- **captured** — emitted by the run itself, at the path named;
- **derived** — not serialized by the run, but unambiguously reconstructible
  from the terminal pytest result plus the fixed source assertions at the pinned
  candidate; a reconstruction, never presented as a raw capture;
- **prerequisite-bound** — obtainable only while the named declared prerequisite
  is present; without it the record is not terminal;
- **observation-only prerequisite** — a required value that no current output or
  assertion can supply; it must be captured by a declared extra observation
  before the row is terminal, and must never be invented from a PASS.

| §6 field | Success outcome | Failure outcome | Label |
|---|---|---|---|
| 1 identity (candidate/runtime/interpreter/native/ROM pins) | runner identity record | runner identity record | captured |
| 2 command and bounds | exact invocation + §3 bounds | same | captured |
| 3 terminal pytest result | exit status, counts, JUnit XML | exit status, counts, JUnit XML | captured |
| 4 owner progress — public tick, active episode, pending delivery/permit, debt | `TIMED_ROM_SMOKE` `baseline_statuses`/`final_statuses` (`captured`); exact mid-row half-cycle/raw-CPU-clock values are **not** emitted — the row asserts only that they strictly increase (`:393-399`), which does not reconstruct the numbers | exception-path records | captured (endpoints) / observation-only prerequisite (intermediate values, if required) |
| 4 frame accounting — frame index, phase, role | `completed_frames` `=3` plus the assertion that each expected tick was requested in order | `structured_step_result`/`phase`/`eof` on the exception path | derived from the fixed assertions (success) / captured (failure) |
| 4 structured step results | reconstructed as `{"tick": baseline[index]["tick"] + frame}` for frames 1,2,3 (`:382-390`) | exception-path records | derived (success) / captured (failure) |
| 4 CPU execution | **not** in `processes[]`; taken from the §2.5 scheduler observer — `sampled_process_cpu[pid:start_ticks].cpu_seconds` in `-result.json` and `samples[].processes[pid].cpu_seconds` in `-host-samples.json` (`run_timed_mcp_84_schedstat.py:62-65,112-124,148-160`), joined by `pid:start_ticks` | same | prerequisite-bound (scheduler observer) — a **sampled** CPU value, a lower bound, never an exact process lifetime |
| 4 wall time | the observer's `elapsed_seconds` is **whole-row observer** wall time, not each MCP process's lifetime; per-process sampled lifetime bounds come from the first/last `samples[]` occurrence of each `pid:start_ticks` | same | prerequisite-bound (scheduler observer) — whole-row wall time and sampled per-process bounds are reported separately |
| 4 runnable-queue delay | captured only while the §2.5 scheduler observer runs the row | same | prerequisite-bound (scheduler observer) |
| 5 teardown: return code, `forced`, `group_alive` | each `processes[]` entry | captured | captured |
| 5 teardown: EOF/disconnect classification, cleanup phase | reconstructed from the passing assertions that each `link_disconnect` returned `mode: idle`/`transport: timed` and that `client.eof()` completed (`:414-418`, `RomClient.eof` `:114-125`) | exception-path records | derived (success) / captured (failure) |
| 6 capacity window + during-row samples | captured only while the §5 watcher admission holds and the §5.1 validator admits the row | same | prerequisite-bound (capacity watcher + gap/admission validator) |
| 6 allocation identity | recorded only while #85's verified allocation is held | same | prerequisite-bound (#85 allocation) |
| 7 output digests | SHA-256 of each retained output | SHA-256 of each retained output | derived |

Four consequences are mandatory:

- **The scheduler observer is selected, not optional.** Leaf 84.2 requires the
  runnable-delay record, so every controlled row runs under the scheduler
  observer of §2.5; a row that omits it is missing a field-4 artifact and is not
  terminal. Field 4's earlier "where the scheduler observer is active" no longer
  leaves that requirement optional.
- **A derived value is never described as captured.** A record that reconstructs
  frame, step, or teardown detail from the terminal result and the fixed source
  assertions must label that detail `derived`, and must not be read as if the run
  had serialized it. A derived value is only ever a fact the assertions actually
  establish (an ordered, expected tick sequence; a completed clean `eof()`), never
  an exact intermediate measurement.
- **A value no output can supply is an observation-only prerequisite, not a
  derivation.** If mid-row owner accounting (exact half-cycle or raw-CPU-clock
  values) is required, it must be captured by a declared extra observation; it
  cannot be reconstructed from a PASS, because the row asserts only strictly
  increasing values (`:393-399`).
- **Failure records are not universal.** The
  `structured_step_result`/`phase`/`eof` records exist only when `_step_frame`
  raises (`:273-339`); a failure in a status assertion, in prelink, or in the
  final `eof()` outside that helper emits none of them. Such a row is a
  **missing-observation** case: report the fields it did produce and record the
  rest as *not observed*, never as captured or derived.

**Authored record sketch (schema illustration only, not a real-ROM result).** A
success row's retained record carries the identity/interpreter/ROM pins
(`captured`), the exact invocation (`captured`), the pytest counts plus JUnit
XML (`captured`), `TIMED_ROM_SMOKE`'s `baseline_statuses`/`final_statuses`/
`processes[]` (`captured`), the frame/step and teardown detail labelled `derived`
together with the assertion each was reconstructed from, the scheduler
observer's sampled CPU per `pid:start_ticks`, its whole-row `elapsed_seconds`,
and its runnable-delay sample (`captured`, prerequisite-bound), the window,
during-row samples, and the §5.1 admission-validator identity (`captured`,
prerequisite-bound), the #85 allocation record (`prerequisite-bound`), and the
output digests (`derived`). Any required mid-row owner value no output supplies
is listed as an observation-only prerequisite. A failure row substitutes the
exception-path `structured_step_result`/`phase`/`eof` records, labelled
`captured`, when `_step_frame` raised; otherwise it is recorded as a
missing-observation row. This sketch is schema only and must not be presented as
real-ROM acceptance.

## 7. Pre-registered decision rules (leaf 84.3)

Run one unchanged ordinary Red/Red case in each runtime inside a qualifying
window. Observations, invalid controls, and causal conclusions are three
separate categories and stay separate below:

- an **observation** is a pass or a failure read in a valid controlled state
  (`S1`/`S2`/`S6`, §5.1);
- an **invalid control** (`S0`/`S3`/`S4`/`S5`, §5.1) is neither a pass nor a
  failure and supports no conclusion;
- a **causal conclusion** requires the attribution prerequisites below and is
  never read off the pass/fail outcome alone.

Then:

- **Both rows pass in a qualifying window** → pre-registered outcome
  **"controlled rows passed; cause not established."** Record the measured
  prerequisite, the qualifying window, and its limits explicitly, and record
  that **no cause is attributed**. Two non-reproductions do not by themselves
  establish that the retained failures were caused by capacity: the frozen
  candidate need not be the historical revision the failures were observed on
  (§4), and two non-reproductions on any one revision do not exclude an
  intermittent implementation problem. Proceed to the nine orientations per
  runtime under the same admission rule. This outcome is a resource
  prerequisite, not a product defect claim, and it does not erase the retained
  failures.
- **Causal attribution, when required, has its own prerequisites.** The
  "cause not established" outcome is upgraded to a capacity attribution only
  when **all** of the following hold and each is recorded:
  1. **Comparable identity** — the code, build, and pinned inputs of the passing
     rows and of the failing observations are the same, or their differences are
     enumerated (§4), so like states are compared;
  2. **Verified allocation** — the rows ran under #85's verified operator
     reservation (§5), not affinity or a cgroup quota;
  3. **During-request scheduler evidence** — the §2.5 observer recorded
     runnable-queue delay *during the failed request itself*, not only a host
     average taken before or between rows; and
  4. **The extra discriminator** — the retained scheduler diagnostic (§2.5) and
     the during-row control evidence together tell capacity starvation apart
     from owner, transport, or native-execution overhead.
  A **newly selected candidate's pass is never treated as explaining an older
  candidate's failure**: when the passing rows and the failures are not the same
  state under prerequisite 1, the outcome is "cause not established" by
  construction.
- **A row fails in a qualifying window** → pre-registered outcome
  **"controlled failure; cause not established."** A qualifying pre-admission
  window does **not** rule capacity out for the failed request: it records
  nothing about scheduling service *during* that request, and a near-deadline
  request can be delayed enough to fail while the aggregate host averages stay
  inside their bounds. The row is read as `S6` only when its during-row samples
  also stayed valid (`S1`/`S2`, §5.1). Record the observation, then run a
  bounded diagnostic and land a failing regression that reproduces at the
  frozen candidate **before** any correction is attempted — never a raised
  deadline. A controlled failure is not by itself read as "not capacity."
- **Excluding capacity-only causation carries the same burden as attributing
  it.** The "cause not established" outcome is upgraded to "capacity excluded"
  only when a **defined, reviewed discriminator** shows the failed request
  received adequate scheduling service and a bounded share of runnable-queue
  delay across its own duration — that request's §2.5 per-thread runnable-delay
  and progress counters read through a named predicate, not merely present.
  Until that discriminator exists and is reviewed, no causal conclusion about
  the failed row is drawn in either direction.
- **No qualifying window is obtainable** → report the comparison as blocked on
  the capacity prerequisite with the observer's terminal result (`S5`, §5.1).
  Do not run the comparison and do not infer a cause; a saturated host is not a
  result.

In every branch the six original failures, both Red/Red reproductions, the
scheduler diagnostic, and the capacity window remain retained and keep their
recorded scopes.

**Worked examples (authored, not execution).**

- *Changed candidate, both rows green.* The frozen candidate is a later revision
  than `7516f16` and its source diff is nonempty (§4). Both ordinary rows pass in
  a valid window (`S1`). Prerequisite 1 is **not** met — the passing state is not
  the failing state — so the outcome is **"controlled rows passed; cause not
  established."** The rules neither say nor imply that the older failures were
  caused by capacity.
- *Same revision, intermittent.* Two rows pass on one revision, but a later run
  of the same unchanged row fails intermittently. Passes do not exclude an
  intermittent implementation problem, so no attribution follows either.
- *Failure inside a valid window.* A row fails while its during-row samples stay
  valid (`S6`). The pre-registered outcome is **"controlled failure; cause not
  established"**: the pre-admission window alone does not rule capacity out, and
  excluding it needs the reviewed per-request discriminator above. A bounded
  diagnostic and a regression that reproduces at the frozen candidate come
  before any correction — never a raised deadline.

## 8. Prohibited actions

- Raising `CALL_BOUND`, `EXIT_BOUND`, `PAIR_BOUND`, or any request/policy
  timeout to obtain a pass.
- Changing the selected profile, the requested frame count, or the result
  assertion.
- Skipping, xfailing, deselecting, or timing out a row to avoid a failure.
- Retrying selected failures until they pass, or presenting a later pass as
  erasing an earlier failure.
- Combining partial runs from different candidates into a single PASS.
- Treating an in-progress, missing, cancelled, or stale-head artifact as a
  terminal result.
- Committing or publishing ROMs, symbols, save states, screenshots, raw logs,
  or absolute local paths.
- Modifying or terminating other users' processes to free capacity.

## 9. Prerequisites before the first rerun

### 9.1 Capacity

A qualifying window must be observed (§5) before each row, and re-observed
whenever admission is lost (§5.1). The 2026-09-13 attempt produced none, and the
same observation and reported result are required again. A lost, expired, or
inherited window is not carried forward: each row waits for a fresh window under
this section before it is dispatched. Because the unchanged watcher enforces no
gap reset (§5), a named, pinned gap/admission validator — or an explicit
read-only validation/restart procedure over the retained sample series — is
required and recorded before the first controlled row and before each later
matrix row.

### 9.2 Runner candidate pin (F5)

The three observers derive `ROOT` from their own physical location, load an
undeclared sibling donor record (`{source,native}-trade-79-after1-start.json`),
and take their first eight command arguments and their environment from it. That
donor record pins absolute `PYTHONPATH` and interpreter paths that target a
**different checkout**, and it is absent from §2's retained-tool table. Re-aiming
the hard-coded `HEAD` assertion (currently the historical `7516f16`) therefore
does **not** bind a row to the candidate it records: the runner can assert one
checkout's commit while the server imports another checkout's `pokered_harness`.
An import-origin check performed at the proposed new checkout confirms this —
with the inherited donor `PYTHONPATH`, both modes resolve `pokered_harness`
outside the reviewed tree, and the donor checkout's own head is not the reviewed
SHA. Re-aiming the asserted SHA alone is **not** sufficient preparation.

Before any comparison executes, all of the following must be met and recorded.
This is a declared prerequisite of the rerun, not a change to §3.

1. **Candidate-bound launch plan.** Document the observers' CLI and their
   physical layout, and record the exact invocation and environment actually
   used for each runtime (source and native) in the run record.
2. **Import-origin check.** Using a path-lookup check that requires no emulator,
   prove independently of the runner's own assertions that the harness and the
   test module resolve inside the selected candidate — not inside a stale donor
   checkout — for both runtimes, and that the PyBoy runtime matches the declared
   mode: the vendored source package for the source runtime, the declared
   installed compiled extension for the native runtime. The check must fail when
   a donor path is still in effect.
3. **Replace or explicitly reconstruct the donor configuration.** Either replace
   the donor record with one written for the selected candidate, or pin it by
   content and reconstruct it explicitly. An undeclared, stale sibling file is
   not acceptable, and a second engineer must be able to reconstruct it from the
   protocol alone.
4. **Record the effective configuration.** Retain the effective launch plan, the
   digests of the runner, donor record, and interpreter actually used, and the
   resulting CLI/physical layout with the row record.

External configuration keeps real host paths; the tracked protocol states the
shape and the identities, never absolute local paths.

### 9.3 Emulator admission

The orchestrator owns the global CPU/emulator-pair budget; a comparison row runs
only after it is granted a slot. The nine-row matrix runs with one worker,
because each row is an emulator pair, and each row requires its own §9.1 window.

### 9.4 Assets and runtimes

Pinned ROM/symbol/fixture inputs via the documented external roots, separate
bootstrapped source and native interpreters, and a native build whose
installed-extension identity is proven rather than assumed.

## 10. Re-verifying this document

The identities in §2 can be re-checked without running an emulator: recompute
SHA-256 for each named artifact in the private evidence root and compare with
the table. The tier composition and the six outcomes in §2.1–§2.2 are
independently readable from the terminal pytest report and from the per-peer
JSON. No step of this verification executes gameplay, and none of it re-runs a
failed orientation.
