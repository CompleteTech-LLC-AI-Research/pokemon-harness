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
   allowed CPU count, held continuously for 60 s) before a comparison row is
   admitted (§5).
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
its definition unchanged:

- sample `/proc/loadavg` and the `some` line of `/proc/pressure/cpu` once per
  second;
- the window qualifies when `avg10 < 10%` **and** `avg60 < 10%` **and**
  one-minute load `< allowed CPU count`, all held continuously for 60 s;
- the observation is bounded at 1800 s and takes no emulator slot;
- a qualifying window is required **before** a comparison row is admitted, and
  capacity is sampled **during** the row as well.

Host load alone never establishes the cause. A row that fails outside a
qualifying window is not evidence of a product defect, and a row that passes
outside one is not evidence that capacity explains anything.

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
   processes, runnable-queue delay where the scheduler observer is active,
   owner progress (public tick, half-cycles, raw CPU clock, active episode,
   pending delivery/permit, debt), frame accounting (frame index, phase, role),
   and structured step results.
5. **Teardown** — per peer: return code, `forced`, `group_alive`, EOF/disconnect
   classification, and the phase in which cleanup ran.
6. **Capacity context** — the qualifying window record and the during-row
   samples.
7. **Digests** — SHA-256 of each retained output, so the record is
   self-checking.

The comparison consumes the runners already validated for this issue: the
single-case observer, its scheduler variant, and the nine-orientation matrix
runner that stops at the first failure. The matrix runner additionally asserts
9/9 non-skipped passes before it can report success, and asserts the final
commit equals the starting commit with a clean tracked status.

## 7. Pre-registered decision rules (leaf 84.3)

Run one unchanged ordinary Red/Red case in each runtime inside a qualifying
window. Then:

- **Both rows pass in a qualifying window** → the observed failures are
  capacity-attributed. Record the measured prerequisite, the qualifying window,
  and its limits explicitly; this is a resource prerequisite, not a product
  defect claim, and it does not erase the retained failures. Proceed to the
  nine orientations per runtime under the same admission rule.
- **A row fails in a qualifying window** → the cause is not capacity alone.
  Isolate owner, transport, and runtime overhead with a bounded diagnostic and
  land a failing regression that reproduces at the frozen candidate before any
  correction is attempted. Do not increase a deadline to make the row pass.
- **No qualifying window is obtainable** → report the comparison as blocked on
  the capacity prerequisite with the observer's terminal result. Do not run the
  comparison and do not infer a cause; a saturated host is not a result.

In every branch the six original failures, both Red/Red reproductions, the
scheduler diagnostic, and the capacity window remain retained and keep their
recorded scopes.

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

1. **Capacity.** A qualifying window must be observed (§5). The 2026-09-13
   attempt produced none, and the same observation and reported result are
   required again.
2. **Runner candidate pin.** The three observers currently assert the
   historical `7516f16` as `HEAD` and refuse to run otherwise. The pin must be
   re-aimed at the frozen candidate, and the re-aimed runner's own SHA-256
   recorded with the run, before any comparison executes. This is a declared
   prerequisite of the rerun, not a change to §3.
3. **Emulator admission.** The orchestrator owns the global CPU/emulator-pair
   budget; a comparison row runs only after it is granted a slot. The nine-row
   matrix runs with one worker, because each row is an emulator pair.
4. **Assets and runtimes.** Pinned ROM/symbol/fixture inputs via the documented
   external roots, separate bootstrapped source and native interpreters, and a
   native build whose installed-extension identity is proven rather than
   assumed.

## 10. Re-verifying this document

The identities in §2 can be re-checked without running an emulator: recompute
SHA-256 for each named artifact in the private evidence root and compare with
the table. The tier composition and the six outcomes in §2.1–§2.2 are
independently readable from the terminal pytest report and from the per-peer
JSON. No step of this verification executes gameplay, and none of it re-runs a
failed orientation.
