# pokered-harness

`pokered-harness` is a memory-first automation harness for Pokémon Red,
Blue, and Yellow. It loads a user-supplied ROM and symbol file, exposes a
typed `Session` API, and can serve that session over MCP.

This repository is a source tree. It does not distribute commercial ROMs,
symbol files, save states, or other ROM-derived artifacts.

Hosted checks run only for public repositories on standard free runners.
Private copies run the same checks locally; the workflow remains available
for reuse. See [CI policy and local commands](docs/CI_POLICY.md).

## Release status

**Status: `PARTIAL` — the current candidate is not production-ready.**

On 2026-09-11, candidate `3c96537` passed the isolated dual-runtime unit-only
gate: source and native/Cython each passed `4,508/4,508` unit tests and
`1,910/1,910` timing cases over five repetitions, with zero failures, skips,
xfails, xpasses, or errors. The same two runtimes each passed six real-ROM MCP
stdio/lifecycle checks and four Red golden-path checks. Native verification ran
inside an unprivileged Linux mount namespace with a private writable
`/dev/shm`, because this host's shared-memory mount is read-only.

This is current runtime and lifecycle evidence, not a gameplay qualification.
The two required color ROMs and all ten Cable Club fixtures are absent from the
candidate environment, so the fixture validator and full source/native local,
TCP, strict-trade, and strict-battle tiers have not run. See the
[current Linux evidence record](VERSIONS.md#current-linux-isolated-runtime-evidence)
and [release checklist](docs/RELEASE_CHECKLIST.md#current-audit-snapshot).

At `766eaa6aafecad039d5b10934ec36f8ec933645e`, the hosted
[release-hygiene workflow](https://github.com/CompleteDotTech/pokemon/actions/runs/34028627290)
passed. It verifies packaging and a clean wheel-install/runtime contract; it
does not run the real-ROM remote trade or battle acceptance matrix. The
documented default is the bundled PyBoy source runtime, not a separately
installed stock PyBoy wheel. The optional Cython build remains a distinct
explicit runtime mode.

Remote TCP transport and diagnostics are implemented, loopback-only, and
unauthenticated. The historical source-runtime validation below passed all nine
ordered TCP trade pairs; compiled-runtime, MCP-facing, cross-host, and strict
battle acceptance remain unqualified.

### Historical working-tree validation (2026-09-06)

The recorded isolated working tree had source-runtime real-ROM evidence using
the pinned
vendored PyBoy source runtime (`PYBOY_NO_CYTHON=1`) with the operator-supplied
ROM, symbol, and fixture roots:

- The final four-worker source-runtime strict trade tier passed `19/19` rows
  in `914.3s`. This covers all nine local version pairs, all nine TCP role
  pairs, and the additional strict Red/Yellow party-record trade assertion.
- Blue-color ↔ Blue-color completed LinkMenu, a full trade, and one battle
  turn. Native edge traffic was balanced, with the negotiated frame barrier
  completing without owner or reader errors.
- Blue-color ↔ Yellow completed LinkMenu, a full party-record trade, and one
  battle turn. The affected ordered topology uses a short bounded frame-paced
  rendezvous at the Trade Center walk boundary, then returns to native edge
  pacing for the ROM-owned serial exchange; the reverse ordering remains on
  its native path.
- Focused non-ROM subprocess-helper checks passed `54` tests, and Ruff passed
  for the touched driver.

That strict trade tier completed for the recorded source runtime, but the
strict battle tier, compiled-runtime gameplay, MCP-facing starter/trade/battle
workflows, and broader host/platform coverage remain open before a full
production claim.

### Historical experimental evidence

At historical revision `656917ba95843decf4a33d98158be0049ef7a22e`, a hosted
gate failed: unit tests recorded `3,913` passes and `1` failure; five timing
repetitions recorded `1,100` passes and `5` failures. Both tiers recorded
zero errors, skips, xfails, or xpasses. The failing case in both tiers was
`test_paired_authored_full_frame_calls_preserve_count_render_buttons_and_events`
with `_queue.Empty`. See the
[historical gate report](https://github.com/CompleteDotTech/pokemon/actions/runs/34001368361).
That result neither qualifies nor, by itself, diagnoses the current master
revision. The historical observations below are retained for investigation,
not as current acceptance evidence.

At `74e8db3`, the dual unit/timing gate completed `PASS`, collecting `3,594`
tests per runtime. Source passed `3,441` unit tests in `151.4s` and `1,075`
timing checks in `319.8s`; native passed `3,441` unit tests in `174.0s` and
`1,075` timing checks in `349.7s`. Both recorded zero failures, errors, skips,
xfails, or xpasses. The local, unpublished report is
`pokemon-batch-dual-74e8db3-20260905/gate-report.json`. This scoped pass does
not erase the historical `f458fc7` native failures below or qualify gameplay.

The separate `74e8db3` narrow-policy actual-MCP smoke matrix passed all `18`
runs (nine ordered pairs per runtime), with zero failures, errors, skips, or
leftover processes and unchanged checked files. Its local, unpublished
manifest is `poke-narrow-matrix18-74e8db3-rfww1gsd/manifest.json`; this is new
smoke evidence, distinct from the earlier `f458fc7` report, not trade completion.

The unbatched full-trade attempt failed to complete: its terminal report has
`status=incomplete`, `complete=false`, and a deadline stop after `3600.186s`.
Both owners completed `274` frames, interrupted the next call, and remained
in approach without trade goals. Both detached and closed with exit `0` and
no surviving owner, but recorded cancellation errors and missing goal/party
evidence. The local, unpublished report is
`poke-timed-trade-full-20260905-AeExoS/report.json`. The batched trade attempt
also failed to complete after `965.494s` (`status=incomplete`,
`complete=false`): Blue exhausted the `hidden_event` input quota in the warp
phase and Yellow was cancelled. Blue completed `929` whole frames and Yellow
`928`, with one interrupted call each and neither trade goal reached. Both
detached, closed their drivers and sessions, and exited `0`, without forced
termination or surviving owners, threads, or report readers. These clean
process exits do not negate the recorded driver/cancellation errors. Its
local, unpublished report is
`poke-batched-trade-74e8db3-20260905-r8Huo6/report.json`; no completed trade
or default-policy qualification is claimed.

At harness commit `4275957`, the source `--unit-only` gate passed `2,924`
unit tests and `955` timing checks (`191` cases in each of five repetitions),
with zero failures, errors, skips, xfails, or xpasses. The local, unpublished
report is `pokemon-mcp-source-4275957-20260905/gate-report.json`. The native
gate also passed `2,924` unit tests in `133.8s` and `955` timing checks
(`191` cases across five repetitions) in `287.2s`, with zero failures, errors,
skips, xfails, or xpasses. Its local, unpublished report is
`pokemon-mcp-native-4275957-20260905/gate-report.json`.
These checks do not establish real-ROM gameplay or release readiness.

A fresh native build from `4275957` verified unchanged hashes for `280`
tracked files, including `116` vendor files, and all eleven required native
extension imports. Hook, speed-state, and import regressions passed `16/16`;
the native counter probe passed `28/28`. Dependency and Cython bootstrap
checks passed. The local, unpublished build record is
`poke-native-4275957-20260905-JRdYYV/BUILD.md`; this is build/runtime evidence.
The separately reported clean source installation at `4275957` passed
dependency checks, source bootstrap, imports outside the checkout, and
`120` import/MCP tests in `26.19s` (exit `0`). Its local, unpublished evidence
is associated with `poke-clean-source-4275957-20260905-PqvMoP`; it does not
qualify real-ROM MCP operation.

Explicit opt-in timed MCP routing is implemented at `4275957`. Authored tests
cover this path, but are not real-ROM acceptance. An actual-ROM MCP policy
failure remains under investigation; no validated gameplay policy is claimed.

The actual-MCP smoke matrix passed in source and native: `10/10` each,
comprising all nine ordered canonical ROM pairs plus one asset-free redaction
test, with zero failures, errors, or skips. Reported pytest durations were
`56.32s` and `50.10s` (XML suite times `56.309s` and `50.086s`). Local,
unpublished reports are `poke-mcp-matrix-4275957-source-20260905.xml` and
`poke-mcp-matrix-4275957-native-20260905.xml`. These runs used `4275957` code
with the frozen, uncommitted `tests/test_mcp_timed_rom.py`, SHA-256
`77dc9d092b27f36cc879ca2f29752647defde5a549a237f407ac732b06af955e`.
The smoke policy used quantum `256`, rearm budget `4096`, instruction cap
`1024`, edge lateness `4096`, and a five-second operation deadline. This is
scoped MCP smoke evidence, not gameplay or a qualified default policy.
The narrower `32/16/32` policy failures remain
unresolved in source (`2.51s`) and native (`2.18s`); the larger-policy smoke
does not supersede those failure artifacts or the gate results below.

A separate native milestone diagnostic on `4fca3a3` ran Blue-color listener
to Yellow connector for `623.635s`. Each owner completed all `600` one-frame
calls (`600` actual frames), with no interrupted or partial calls. Both
recorded save-request, Yes/No, save-game, and LinkMenu milestones and reached
Trade Center map `0xEF`. Endpoint detach, hook removal, and session close
completed with no errors, forced termination, or surviving owners/readers.
The local, unpublished report is
`poke-milestone-600-20260905-ZZVHBS/report.json`. It used the same explicit
diagnostic policy: rearm `4096`, instruction cap `1024`, lateness `4096`,
quantum `256`, operation deadline `5s`. This single orientation establishes
milestone progression, not a completed trade exchange, gameplay matrix,
graceful protocol shutdown, or default-policy qualification. The full dual-runtime
gate on `4fca3a3` and unit gates on `079aea1` remain pending at this snapshot;
no outcome is claimed.

At the later `f458fc7` snapshot, the `4ee075b` timing correction addresses
CPU overshoot without widening the bound. The lead reported scoped
regressions passing `430` tests in source (`45.08s`) and native (`40.87s`);
these are implementation checks, not real-game proof. The atomic DMA gap
remains: a `412`-half-cycle operation can exceed the `64`-half-cycle limit.

The local, unpublished `poke-narrow-ten-repeats-2l3xvr4b/manifest.json`
records the narrow-policy Blue-color-listener/Yellow-connector actual-MCP
smoke passing five repetitions per runtime, with zero failures, errors,
skips, supervisor failures, or leftover processes. Numeric policy evidence
records rearm `32`, instruction cap `16`, lateness `32`, quantum `256`, and
operation deadline `5s`; the log's aligned-profile label is stale. Its recorded
HEAD is `81737bd`, with frozen working-file changes later committed in
`4ee075b` and checked hashes unchanged during the run; it is not an
exact-`f458fc7` gate. These targeted passes follow earlier failed runs, whose
artifacts remain retained; they do not qualify trade, a full matrix, or a
default policy.

The lead also reported one `079aea1` native unit setup-race failure, followed
by a correction and ten passing targeted repetitions; full-gate acceptance
remains pending. The dual unit/timing gate
(`pokemon-trade-dual-f458fc7-20260905`) ended `FAIL`: source passed `3,308`
unit and `995` timing checks; native passed `3,307` unit checks with one
failure and `994` timing checks with one failure. Native unit failed
`test_bounded_incoming_queue_fails_closed[socketpair]`; timing failed
`test_cancel_reaches_active_real_credit_wait_without_session_lock` in
iteration three (`cpu88 > 88`). No skips or errors were recorded. The
separately reported `18/18` narrow-policy ordered matrix smokes are not
gameplay acceptance. The older full dual-runtime gate has no terminal outcome
claimed here; actual full trade remains unverified.

### Earlier experimental gates

The `4e1e801` dual gate failed timing: source `844` passed / `1` failed;
native `843` passed / `2` failed. Both passed `2,394` unit tests. This retained
flaky-gate result (`pokemon-process-dual-4e1e801-20260905/gate-report.json`,
local and unpublished) is not erased by later scoped passes.

At harness commit `e8db87e`, the dual-runtime `--unit-only` gate collected
`2,512` tests per runtime. Source and native each passed `2,368` unit tests
and `785` timing checks (`157` cases in each of five repetitions), with zero
failures, errors, skips, xfails, or xpasses. The retained evidence bundle is
`pokemon-routing-dual-e8db87e-20260905` (`gate-report.json`), local and
unpublished. This is unit/timing evidence only, not real-ROM gameplay
or a full release gate.

The native runtime in that gate was built from the vendored code at `013b386`;
the source runtime at `e8db87e` includes the subsequent boolean save-state
restore correction. This combination is tested evidence, not an exact combined
native release build of `e8db87e`.

The earlier timed routing used `TimedRemoteEndpoint` and
`Session.bind_timed_execution`. Threaded Blue-color/Yellow diagnostics
advanced only five to six frames before bounded cancellation. They do not
prove trade, battle, rendezvous liveness, or graceful shutdown. Completed
whole calls in authored tests do not qualify real-ROM gameplay. Earlier full
real-ROM gates remain failed; the scoped passes below do not supersede them.

### Historical source candidate evidence

The historically recorded source battle
matrix passed `19/19`; the recorded four-worker source trade matrix failed
at `18/19`. The historical full source gate at
`2eb21a5b45e67f47bb89697daeed76509adf4b13` passed unit `980/980` and
local `47/47`, but its remote tier failed at `22/23`: the Yellow/Yellow TCP
LinkMenu test reached the listener's menu and then reported
`NetworkBackendError: backend closed`. Its terminal report is `FAIL`: trade
passed `18/19`, battle `19/19`, and timing `55/55`. The separate full native
gate at `f4fddfc` also ended `FAIL`: unit `1,060/1,060`, local `47/47`, remote
`11/12`, trade `14/19`, battle `15/19`, and timing `55/55`. Both terminal
reports recorded zero skips; neither qualifies the release.

### Historical origin/master evidence at `1f19707`

The following records describe the earlier origin/master candidate, not the
then-current experimental branch. At `1f19707`, source and
native each passed `1,176/1,176` unit tests and `65/65` total timing checks
(`13` cases in each of five repetitions), with zero failures, skips, xfails,
xpasses, or errors. Each collected `1,320` tests; `19/19` trade and `19/19`
battle entrypoints were declared but not executed by this unit-only gate.
The external report is `pokemon-qualification-dual-unit-1f19707-20260905/gate-report.json`.
Canonical Red/Blue/Yellow boot checks at the same head passed source `3/3`
in `6.74s` and native `3/3` in `1.89s`, with zero failures, skips, or errors
and one SDL warning per runtime. These checks run 120 frames plus
state/save/load assertions; they do not establish gameplay qualification.

That historical native build is documented in the external bundle
`poke-serial-native-20260905-V7dHOU/QUALIFICATION.md`: its base is `e1686ca`
with a serial overlay whose hash matches `1f19707`. The later dual gate above
tests the candidate harness with that runtime; the build alone is not a clean
full gameplay qualification.

The native Blue-color listener / Yellow connector trade replay at `02a8e85`
(runtime identical to `1f19707`) failed after `721.67s`. Both peers exited
cleanly with return code `1`. Blue recorded zero LinkMenu hits and one
`CloseLinkConnection` at local tick `1332`; Yellow reached LinkMenu at local
tick `628` and input at `660`. Transport recorded `6,248` balanced native
edges, zero errors, and zero keepalive exchanges. Local ticks are not a common
clock: this supports investigating time coordination, not a precise root-cause
claim. Raw evidence is retained in
`pokemon-native-blue-yellow-02a8e85-LMtfZm/output.log` and `result.json`.

That candidate fixed serial idle/external-clock hints beyond `2^31`, dispatch
lock ordering and admission deadlines, failure-first gate evidence retention,
and partial pipe capture. Peer stack diagnostics are opt-in through a positive,
finite `POKERED_PEER_TRACE_AFTER_SECONDS`; optional `POKERED_PEER_TRACE_DIR`
must already exist. At most two one-shot dumps are scheduled, with no ROM
changes. At that historical snapshot, experimental time coordinator `344aa95`
was on another branch and was not included in `1f19707`; this exclusion does
not describe the then-current experimental branch. The original `02a8e85` dual gate's native timing
failure (`64/65`) and lost failed test identity and iteration output remain historical failures;
subsequent test-race and gate-report fixes precede the verified `1f19707` pass.
No clean full source/native gameplay gate is established.

### Earlier full-gate snapshots retained from origin/master

The recorded source battle matrix passed `19/19`; the recorded four-worker
source trade matrix failed at `18/19`. The historical full source snapshot at
`2eb21a5b45e67f47bb89697daeed76509adf4b13` passed unit `980/980` and
local `47/47`, but its remote tier failed at `22/23`: the Yellow/Yellow TCP
LinkMenu test reached the listener's menu and then reported
`NetworkBackendError: backend closed`. Its later trade snapshot failed at
`18/19`, with battle still ongoing when recorded; this is historical status,
not a claim that the run remains active. The historical native full-gate
snapshot at `f4fddfc` passed unit `1,060/1,060` and local `47/47`, failed
remote `11/12`, and had two trade failures while that tier was ongoing.
Neither failed gate qualifies the release.
The follow-up LinkMenu-only shutdown change passed five consecutive
Yellow/Yellow real-ROM replays (`24.48s`, `22.20s`, `22.67s`, `23.08s`,
`21.41s`). These targeted checks do not replace a full gate on the new candidate.

Historical recorded evidence, using the operator-supplied assets pinned in
[`VERSIONS.md`](VERSIONS.md):

- **Clean editable install:** an exact-commit archive of `2eb21a5` passed
  standard-library `venv` creation on Python 3.12.13, pip installation of
  `.[dev]`, `pip check`, and the source bootstrap identity check. This verifies
  the documented editable-install path, not wheel completeness or gameplay.
- **Wheel install and launch:** the same `2eb21a5` source built a wheel that
  installed in a separate fresh Python 3.12.13 environment. Dependency checks
  and ten public imports passed outside the checkout. With pinned Red-color
  ROM/SYM inputs, MCP launched and exited cleanly on stdin EOF within a
  30-second bound. This is install/startup/cleanup evidence, not MCP request
  or gameplay acceptance. Optional Pillow image support was not installed.
- **Source unit/timing gate:** `980/980` unit tests and `55/55` timing cases
  passed across five timing repetitions. Collection found `1,132` tests, all
  `19/19` strict trade and `19/19` strict battle entrypoints, and the fixture
  manifest passed schema validation. The focused serial, network, session,
  lifecycle, and packaging regression set passed `155/155`.
- **Native build:** the vendored PyBoy 2.7.0 fork
  (`c565df66c3731fad2856169a90f6bbec99925915`) built a CPython 3.12 Linux
  wheel successfully in an isolated temporary copy. That build alone does
  not qualify gameplay. The separate historical full native gate at `f4fddfc`
  failed as recorded above; the acceptance results below use source mode.
- **Strict battle acceptance:** the source-runtime gate passed `19/19` local
  and TCP real-ROM entrypoints in `1,237.8s`, with no skips, errors, or
  test-only protocol bypasses. All five ROM hashes, three symbol hashes, and
  ten fixture entries were validated in the same gate.
- **Strict trade acceptance:** the source-runtime gate is `FAIL` at `18/19`
  after `1,093.9s` with `workers=4`. The sole failure is
  `red_color-listen-blue_color-connect`, which timed out at `722.6s` during
  Trade Center warp. Its final transport snapshot had `1,080` applied owner
  edges, `9/10` sync counters, zero owner-edge errors, zero IRQ callback
  errors, and no pending requests; this is an unresolved protocol/ROM
  rendezvous failure, not a backend crash. The other 18 rows passed. A
  targeted rerun of that exact row passed once, and a four-worker stress replay
  passed `4/4`; those are diagnostic replays and do not change the failed
  four-worker matrix result. The subprocess peer now also reports the ROM-owned
  LinkMenu selection buffers and warp-transition counters on future failures.
- **Quality checks:** Recorded Ruff lint passed, and the release-hygiene formatter check
  passed for the touched packaging test. The repository-wide formatter check
  still reports unrelated legacy files and was not used to rewrite them.

The serial protocol and battle-input work was informed by the
[`pret` Pokémon Yellow serial disassembly](https://raw.githubusercontent.com/pret/pokeyellow/master/home/serial.asm)
and the
[`pret` battle-core disassembly](https://raw.githubusercontent.com/pret/pokeyellow/master/engine/battle/core.asm).
These disassemblies informed game protocol/control flow and the one-based
move-menu cursor/input contract. Hardware-cycle timing requires separate
emulator or hardware evidence; disassembly does not establish it or replace
real-ROM acceptance evidence.

Release status remains `PARTIAL`: recorded source trade and remote LinkMenu
failures prevent source qualification, and compiled-runtime gameplay
qualification remains incomplete. The standard-library editable-install path
has the scoped passing evidence above. Supported inputs remain limited to the
declared canonical fixture-backed matrix, and TCP is enforced loopback-only,
unauthenticated, and unencrypted. Stock-ROM pairs, secure cross-host operation,
additional platforms, and MCP-facing starter/trade/battle workflows are separate
coverage limits, not promises made by this release scope. Required MCP
startup/state/action/lifecycle checks still need current-candidate evidence.

Status semantics are deliberately scoped:

- `PASS` means that the named command completed with clean collection and no
  failure, error, skip, xfail, or timeout in the selected scope. It is not a
  claim about unselected ROM-backed capabilities.
- `PENDING` means that the required current evidence is not available; it is
  not a passing result or permission to infer coverage from declarations,
  milestones, or historical rows.
- `PARTIAL` means that controlled evidence passes but one or more required
  release conditions remain open, such as BYO assets, fixture provenance,
  matrix coverage, broad-suite results, or platform/security review.
- `PRODUCTION-READY` is reserved for a clean full gate with all required BYO
  assets, complete strict acceptance coverage, retained evidence, and no open
  release blockers.

The state parser now has an additive validity contract: `GameState.validity`
reports `valid`, `partial`, or `unknown` status with exact missing-symbol,
unknown-field, and invalid-field metadata. Legacy component fields remain
available; callers can identify missing symbols and unrecognized values as
unknown instead of treating legacy zero or `False` values as authoritative.
This is state-observation evidence, not live MCP gameplay evidence.

### Recorded candidate matrix

The following matrix retains historical results; it is not the `e8db87e`
unit/timing snapshot above or then-current experimental gameplay acceptance.

| Capability | Recorded result | Evidence boundary |
|---|---|---|
| Source unit/timing | `PASS` — 980/980 unit and 55/55 timing cases | Recorded source runtime, Python 3.12.13, PyBoy 2.7.0 fork `c565df66c3731fad2856169a90f6bbec99925915`; 1,132 tests collected and all five ROM/SYM pins validated. |
| Strict trade | `FAIL` — 18/19 | Recorded source runtime; all 19 rows declared and executed with `workers=4`. The sole failure is `red_color-listen-blue_color-connect` at the Trade Center warp after 722.6s; 18 other real-ROM local/TCP rows passed. |
| Strict battle | `PASS` — 19/19 | Recorded source runtime; all 19 local/TCP real-ROM rows passed in 1,237.8s with no skips, errors, or test-only protocol bypasses. |
| Native/Cython build | `PASS` — wheel built | Build evidence only. The separate full native gate at `f4fddfc` failed remote, trade, and battle; compiled gameplay remains unqualified. |
| Source/native unit and timing | `PASS` — each 1,176/1,176 unit and 65/65 total timing | `1f19707`; 1,320 collected per runtime, five repetitions of 13 timing cases. Strict trade/battle declared, not executed. |
| Source/native canonical boot | `PASS` — each 3/3 | `1f19707`; source 6.74s, native 1.89s. 120 frames plus state/save/load; not gameplay. |
| Prior source strict trade | `FAIL` — 18/19 | Historical source runtime; all 19 rows declared and executed with `workers=4`. The sole failure is `red_color-listen-blue_color-connect` at the Trade Center warp after 722.6s; 18 other real-ROM local/TCP rows passed. |
| Prior source strict battle | `PASS` — 19/19 | Historical source runtime; all 19 local/TCP real-ROM rows passed in 1,237.8s with no skips, errors, or test-only protocol bypasses. |
| Native Blue/Yellow trade replay | `FAIL` — 721.67s | `02a8e85`, runtime identical to `1f19707`; clean failed peer exits do not establish gameplay success. |
| Native/Cython build | `PASS` — fresh build and identity checks | Base `e1686ca` plus matching serial overlay; candidate dual unit and boot passes above do not qualify compiled gameplay. |
| Release readiness | `PARTIAL` | Source gate failed; compiled-runtime gameplay qualification remains open. Clean install and MCP startup/state/action/lifecycle must be verified for the declared scope. Unadvertised extensions remain separate coverage limits. |

### Historical evidence boundary

| Retained result | Scope |
|---|---|
| Pre-PR #52 serialized source trade `FAIL`, 18/19 | Separate older run with `matrix-workers=1` and a Blue/Blue warp failure; not the four-worker Red-listener/Blue-connector result above. |
| Pre-fix source-local battle 9/9 and representative trade/battle rows | Historical source evidence; the recorded source battle tier above covers all 19 local/TCP entrypoints. |
| Pre-fix native/Cython trade 16/19 and battle 17/19 | Historical failed gameplay gate at `a8576b5ecb8e7039eefe0e02865b0bfc031387a7`; the newer wheel build does not qualify compiled gameplay. |
| Windows MCP stdio 4/4; separate pre-fix MCP integration 6/6 and dispatch 104/104 | Historical transport, observation, navigation, and lifecycle checks; MCP starter/trade/battle gameplay remains unproven. |
| Prior source/native unit, timing, install, and asset tiers | Historical scoped evidence, not a completed current full gate or verified standard-library install. |

The required setup, test tiers, evidence format, and sign-off rules are in
[`docs/PRODUCTION_RUNBOOK.md`](docs/PRODUCTION_RUNBOOK.md) and
[`docs/RELEASE_CHECKLIST.md`](docs/RELEASE_CHECKLIST.md).

## Supported input formats

The intended release inputs are the exact ROM variants listed in
[`VERSIONS.md`](VERSIONS.md):

| Game | Input | Status |
|---|---|---|
| Pokémon Red (UE) | Stock `.gb` plus `pokered.sym` | Hash-pinned BYO input; current stateful link support is not claimed |
| Pokémon Red (UE) color variant | `pokemon-red-color.gb` plus the matching Red symbols | Pinned BYO input; historical PR #17 source-runtime strict trade and battle evidence passed |
| Pokémon Blue (UE) | Stock `.gb` plus `pokeblue.sym` | Hash-pinned BYO input; current stateful link support is not claimed |
| Pokémon Blue (UE) color variant | `pokemon-blue-color.gb` plus the matching Blue symbols | Pinned BYO input; historical PR #17 source-runtime strict trade and battle evidence passed |
| Pokémon Yellow (UE) | Native CGB `.gbc` plus `pokeyellow.sym` | Pinned BYO input; historical PR #17 source-runtime strict trade and battle evidence passed |
| Other localisations and ROM hacks | — | Out of scope |

Stock-ROM link pairs remain outside the strict canonical matrix because their
fixture provenance is partial.

## Requirements and clean install

Requirements:

- Python 3.11 or newer.
- The bundled PyBoy runtime (`2.7.0`, harness-local divergence revision
  `b94bf5dfb042c502ff4bc1bcd417599b02a9419b`; pre-divergence harness fork revision
  `c565df66c3731fad2856169a90f6bbec99925915`). Earlier recorded runs below name
  that fork revision because they predate the in-fork divergence.
- `mcp==1.29.1`, the certified runtime API used by the server.
- A legally obtained ROM and a matching debug symbol file for any real-ROM
  run.

From a clean checkout on Unix, WSL, or Git Bash, use the same interpreter for
installation, tests, the gate, and MCP:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pip check
python scripts/bootstrap_pyboy.py --mode source --check
```

The standard-library `venv` module must include `ensurepip`. On Debian or
Ubuntu, install the matching OS package (for example, `python3.12-venv` or
`python3-venv`) first if `python3 -m venv` reports that `ensurepip` is
unavailable. The commands above assume the resulting environment provides
`python -m pip`; an environment created by another tool must provide the same
pip/install contract before it is used for the release gate.

On Windows PowerShell, create the environment with `py -3 -m venv .venv`,
activate with `.venv\Scripts\Activate.ps1`, and use `python -m pip` for the
remaining commands. Keep the environment used for testing and the environment
used to launch MCP identical.

If `uv` is the environment manager, the equivalent lockfile-resolved setup is:

```bash
uv venv --seed .venv
uv sync --locked --extra dev
uv pip check
uv run python scripts/bootstrap_pyboy.py --mode source --check
```

The standard-library path is the portable baseline; the `uv` path additionally
uses the checked-in `uv.lock` for transitive dependency resolution. Both paths
install the project distribution, which bundles the pinned PyBoy source tree.

The distribution bundles the pinned PyBoy source runtime used by the link
layer. It exposes the Python-accessible serial backend contract and is marked
with the revision above. If an older standalone `pyboy` wheel is already
installed in an environment, remove it and reinstall this project before
testing; `Session.from_files` rejects an unmarked runtime when the package
pin is enforced.

For a runtime identity check after installation:

```bash
python scripts/bootstrap_pyboy.py --mode source --check
python -c 'import pyboy; print(pyboy.__version__, pyboy.__pokered_harness_revision__)'
```

The production gate defaults to the vendored source runtime. To qualify the
optional native path, build the pinned fork in the same environment and select
it explicitly; the gate then fails if any required PyBoy module resolves to
vendored source instead of an installed extension:

```bash
python scripts/bootstrap_pyboy.py --mode cython \
  --build-evidence "$EVIDENCE_DIR/native-build-evidence.json"
python scripts/bootstrap_pyboy.py --mode cython --check
python scripts/production_gate.py --runtime-mode cython --unit-only \
  --repeat-timing 5 --evidence-dir "$EVIDENCE_DIR"
```

Native bootstrap stages the complete vendored source and resources in a fresh
directory under ignored `build/`, excluding generated C, objects, and extension
binaries. It prints a SHA-256 fingerprint of the staged inputs and removes the
staging directory after installation or failure. With `--build-evidence PATH`
the procedure that performed the build also writes its own record at `PATH`,
tying the staged input bytes, the completed install, and the resulting
extension identities together. `--build-evidence` is rejected with `--check`
and is not written when the build fails, so an absent record means no
successful build ran. Pin the emitted record with
`interpreters.native_build_evidence` and
`interpreters.native_build_evidence_sha256`; the qualification runner
recomputes the staged-input digest, the extension hashes, and the fingerprint
from the live runtime, so a hand-written record does not satisfy the check. A
change to a shared `.pxd` requires rebuilding all dependent extensions
together; copying a single rebuilt extension into an older installation can
leave incompatible method tables. The `--check` command verifies imports,
module origins, and the runtime contract; it does not prove that manually
replaced binaries share a build. Run a complete bootstrap after vendor changes
or binary replacement.

To run the dual-runtime gate with independently installed environments, pass
the source interpreter with `--python` and the native interpreter with
`--cython-python`:

```bash
python scripts/production_gate.py --runtime-mode both \
  --python .venv-source/bin/python \
  --cython-python .venv-cython/bin/python \
  --unit-only --repeat-timing 5 --evidence-dir "$EVIDENCE_DIR"
```

`--cython-python` is valid only with `--runtime-mode both`; if omitted, the
dual gate falls back to `--python` and does not exercise a separate Cython
environment. The retained prior-candidate separate-interpreter dual gate
collected 1,094 tests in each source and native runtime, had 949 unit tests pass,
and passed timing 50/50 in each of five repetitions. This unit/timing scope does
not establish ROM gameplay.

For complete captured pytest output, add `--raw-output-dir "$PRIVATE_OUTPUT_DIR"`
using a new private directory outside the sanitized `--evidence-dir` bundle.
This retains every attempted matrix row and each regular-tier iteration under
separate runtime directories, including passing output. These unredacted logs
are private diagnostic artifacts; see the [production runbook](docs/PRODUCTION_RUNBOOK.md)
for filenames and capture-failure behavior.

The complete gate now starts with an additional runtime/import and MCP smoke
in every requested runtime. With `--runtime-mode both`, source and native each
run the public MCP startup/state/save-load/disconnect checks and all nine
ordered canonical timed-ROM pairs before the long qualification tiers start.
The smoke uses their existing deadlines. Its passes do not replace any of the
six original tiers or the five timing repetitions.

For this ROM-backed developer scope alone, use separate bootstrapped
interpreters and a fresh `RUN_ID`:

```bash
"$SOURCE_PYTHON" scripts/production_gate.py \
  --repo-root "$PWD" --runtime-mode both \
  --python "$SOURCE_PYTHON" --cython-python "$NATIVE_PYTHON" \
  --rom-root "$ROM_ROOT" --fixture-root "$FIXTURE_ROOT" \
  --smoke-only \
  --evidence-dir "$PWD/target/gate-$RUN_ID/report" \
  --raw-output-dir "$PWD/target/gate-$RUN_ID/raw"
```

`ROM_ROOT` and `FIXTURE_ROOT` must contain the pinned legal inputs described
below. A smoke-only pass is labeled `execution-scope: smoke-only`; it does not
qualify a release. Omit `--smoke-only` to run the full gate. Full gates stop
queued tiers after a failure by default and retain each unrun tier as
`NOT_STARTED` with its stopping reason. An ordinary source smoke failure still
allows the native smoke to report its own result. `--keep-going` continues the
remaining tiers while preserving failures; `--fail-fast` explicitly enables
stopping for a selected scope. `--unit-only` remains the asset-free unit/timing
scope. See the runbook for cancellation and report details.

The source runtime is the documented release default. Native module identity,
unit/timing checks, and attach/step/close smokes do not qualify compiled
trade/battle gameplay; see the recorded matrix above.

## BYO-ROM and symbols

Place the files below under the repository root. The directories are
gitignored by design:

```text
rom/
├── red/
│   ├── pokemon-red.gb
│   ├── pokemon-red-color.gb       # optional color variant
│   └── pokemon-red.sym
├── blue/
│   ├── pokemon-blue.gb
│   ├── pokemon-blue-color.gb      # optional color variant
│   └── pokemon-blue.sym
└── yellow/
    ├── pokemon-yellow.gbc
    └── pokemon-yellow.sym
```

Asset requirements are tiered. The unit/timing gate needs no ROM or save-state
assets. A single-session test needs one pinned ROM and its matching symbol
file. The default real-ROM gate needs all five pinned ROM files, all three
symbol files, and the external fixture root. The tracked fixture manifest
contains ten state entries (ordinary and battle, including four
vanilla-derived rows), so `validate_fixture_manifest.py --fixture-root ...`
requires all ten files. The retained prior-candidate manifest validation
passed; six canonical fixtures have verified provenance and four
vanilla-derived fixtures remain `PARTIAL`. The stock ROM/SYM pins and existing
vanilla fixture bytes validate, but vanilla ordinary capture provenance cannot
be established: replay against the retained source failed at the 64-step
movement bound, and the manifest's ordinary producer revision `25e231c` is
historical. No reproducibility of those vanilla fixture bytes at the
implementation/runtime snapshot is claimed.

The symbol files should be generated with `DEBUG=1` from the matching
`pret/pokered` or `pret/pokeyellow` source tree. Obtain those source trees
separately from their authoritative upstreams. A generic build sequence is:

```bash
git clone <authoritative-pokered-source> pokered
cd pokered
make DEBUG=1
make clean && make blue DEBUG=1
cd ..

git clone <authoritative-pokeyellow-source> pokeyellow
cd pokeyellow
make DEBUG=1
cd ..
```

Use the source commits and RGBDS toolchain recorded in [`VERSIONS.md`](VERSIONS.md)
when reproducing the audited symbols, then copy only the matching `.sym` files
into `rom/<version>/`. The repository records the available audited provenance
but does not contain the generated symbols or enforce a source-build lock.

Before starting a session, compare the ROM's SHA-1 with its matching path row
in [`VERSIONS.md`](VERSIONS.md). Always set `POKERED_ROM_SHA1` explicitly for
release evidence; the loader also selects a matching documented path when the
variable is omitted and fails closed when no pin exists. Never use
`POKERED_SKIP_SHA1=1` for a release run. The MCP production entry point
rejects that variable outright; use a separate explicitly diagnostic driver
for non-production experiments.

## Run one MCP server

The server requires the primary ROM and symbol paths:

```bash
POKERED_ROM_PATH=rom/red/pokemon-red-color.gb \
POKERED_SYM_PATH=rom/red/pokemon-red.sym \
POKERED_ROM_SHA1=e1deed63080bc24cad5fba18ecb3184f905d16d4 \
POKERED_VERSIONS_PATH="$PWD/VERSIONS.md" \
python -m pokered_harness.mcp_server
```

For Blue or Yellow, replace all three values with the matching row in
[`VERSIONS.md`](VERSIONS.md). The server starts headless. Use a script's
documented display option, or construct `Session(view=True)`, when a visible
session is needed.

The committed `.mcp.json` is a portable configuration template, not a
self-installing launcher. Its contract is:

- the MCP client must expand `${PWD}` to the checkout/workspace root (or the
  operator must replace that placeholder with the client's documented workspace
  variable);
- `python` must resolve to the environment created by the clean-install command;
  the config does not search for or create a virtual environment; and
- the selected ROM, symbols, and `VERSIONS.md` must exist at the expanded paths.

Clients that do not expand `${PWD}` should use the explicit shell launch above
from the repository root, or configure an equivalent client-specific working
directory and variable expansion. No machine-local `PYTHONPATH` is required.
An installed wheel launched outside a checkout may omit `VERSIONS.md` when
explicit primary and peer ROM SHA-1 values are provided; the bundled PyBoy
runtime identity is still enforced.

## MCP surface

The single-session server exposes tools for:

- stepping frames and pressing, holding, or releasing buttons;
- saving and loading base64-encoded emulator state;
- waiting for named hook events; and
- reading parsed game state, the event log, and read-only per-slot 44-byte
  party-record SHA-256 digests (`pokered://party-records`) as resources. The
  digest resource is observational only and does not assert a completed trade.

When a peer session is configured with `POKERED_PEER_*` variables, the
link-related tools are also exposed. The peer is constructed at startup but
is not paired automatically. The paired server additionally advertises
`pokered://peer-party-records`, an owner-scoped, observational view of the
peer's `wPartyMons` records. It reports only per-slot slot/digest/
record_size/species/level plus the count and missing-symbol provenance, and
it never exposes raw record or ROM bytes or absolute paths. Because the
primary (`pokered://party-records`) and peer views are read under each
owner's own lock, a public client can compare both owners' intended receiving
slots and unrelated records without conflating owners. Timed remote mode has
no local peer, so the peer resource is rejected there.

The exact-exchange audit takes outgoing slot indexes. Gen I removes each
selected record, compacts survivors in order, and appends the incoming record
to the final occupied slot. The audit checks those receiving positions and
all survivor bytes; an in-place replacement of a nonfinal slot is rejected.
This is a record observation contract, not proof of animation or room return.

Evidence from the pre-fix implementation/runtime snapshot
includes six real MCP integration checks in 13.11 seconds with one SDL warning
and 104 MCP dispatch tests. Ordinary MCP calls drove a real Red session from
bedroom map 38 at `(3,7)` through the house exit, Pallet Town map 0 at `(5,5)`,
Oak's Lab map 40, and lab movement. A bounded starter attempt ended at map 40
`(5,3)` with `party.count=0`; no memory/state bypass was used. These results
establish MCP control and observation plus lifecycle behavior, but not starter
acquisition or MCP-facing trade/battle; those remain unproven.

### Battle state schema

`pokered://game-state` (and `pokered://peer-game-state` for an in-process
peer) carries a `battle` object plus an `epoch` object. The schema is
additive. Every optional field is `null` when its backing symbol is absent
from the loaded `.sym`; a value is never guessed.

Identity and combatants:

- `battle.kind`, `battle.raw_is_in_battle`, `battle.battle_type`,
  `battle.engaged_trainer_class`, `battle.engaged_trainer_set`,
  `battle.player_mon_slot`.
- `battle.enemy_mon` (species/level/HP/max HP/status/types/moves/PP and slot)
  and `battle.enemy_mon_valid`: `true`/`false` where a trainer slot can be
  checked, `null` for a wild battle with no meaningful slot; `null` whenever
  the `wEnemyMon*` symbols are absent.
  `false` is reserved for a value the harness actually read and rejected
  (a party slot outside `0..5`, or a combatant whose own validity check
  failed); unavailable evidence is always `null`, never `false`.
- `battle.player_stat_stages` / `battle.enemy_stat_stages`: the six
  `w*MonStatMods` bytes decoded as Gen-1 stages in `-6..+6` (`raw - 7`,
  where `7` is neutral), with `valid` `true` when all six are present and in
  range, `false` when a present byte is outside `1..13` (no engine writer),
  and `null` when the family is only partially present. These are exposed
  only while a wild/trainer battle is active; out of battle the bytes are
  stale and report `null`.

Phase and terminal state:

- `battle.phase` is a candidate derived from several ROM-owned observations;
  there is no single sub-phase byte. `battle.phase_valid` is `true` only when
  exactly one surviving signal supports the phase, `false` when evidence is
  missing, ambiguous, or contradictory, and `battle.phase_evidence` lists the
  symbols consulted. `intro` is retained for schema compatibility but is
  never derived.
- `battle.raw_battle_result` is always the raw `wBattleResult` byte.
  `battle.terminal_result` is set only for a non-zero outcome byte that
  survived an observed active-to-inactive battle transition; an ambiguous
  zero (win, blackout, and escape all leave or clear zero) stays `null`.
  `battle.escaped_from_battle` is the raw escape byte.
- `battle.menu_open` / `battle.menu_evidence`: the session-maintained
  execution-hook state for the battle command/move menu. `wMoveMenuType` is a
  mode selector, not an open/closed flag, so `command_selection` is reported
  only when `menu_open` is `true`; the evidence names the hooked ROM labels.
  `menu_open` is `null` when observation is unavailable and after a
  `load_state`/`reset_tick` until the next hook event.
- `battle.resolution_open` / `battle.resolution_evidence`: the
  session-maintained execution-hook state for ROM move execution
  (`ExecutePlayerMove`/`ExecuteEnemyMove` entered and their matching `*Done`
  exit not yet reached). An ordinary FIGHT turn is otherwise unobservable:
  `ExecutePlayerMoveDone` clears `wActionResultOrTookBattleTurn` to zero on
  the way out, so a client polling at any interval only ever sees that flag
  set for the item/switch/run turns that never execute a move.
  `action_resolution` is reported when the flag is non-zero *or* the hook
  shows the engine is resolving a move; `resolution_open` is `null` when
  observation is unavailable and after a `load_state`/`reset_tick` until the
  next hook event.

Forced replacement is reported from the ROM's own live replacement-menu
signal: `ChooseNextMon` writes `BATTLE_PARTY_MENU` to
`wPartyMenuTypeOrMessageID` and `DisplayPartyMenu`'s input loop raises
`wPartyMenuAnimMonEnabled` to `$40` while it awaits input, clearing it on
exit. Both must hold, and the party must have a living member
(`AnyPartyAlive` corroboration). The faint flag
`wInHandlePlayerMonFainted` is *not* sufficient and not required: it is
cleared on the enemy-faint path before that path calls `ChooseNextMon` (so a
genuine replacement can have it at zero), it can read stale after the menu
closes, and the final-faint path sets it while jumping to blackout or victory
without ever opening a menu.

Transient mechanics:

- `battle.move_menu_type` (raw `wMoveMenuType`), `battle.player_move_list_index`,
  `battle.current_menu_item`, `battle.player_selected_move`,
  `battle.enemy_selected_move`, `battle.action_result_or_took_turn`, and
  `battle.in_handle_player_mon_fainted`.

Per-mode availability: the primary MCP server enables the menu, move-execution,
and battle-end observations on its session and its configured peer at startup.
The menu observation is installed only when `SelectMenuItem`,
`DisplayBattleMenu.handleBattleMenuInput`, `MainInBattleLoop`, and
`MainInBattleLoop.selectEnemyMove` all exist in that session's symbol table
(they do in the pinned Red/Blue/Yellow `.sym` files); otherwise `menu_open` is
`null` and `command_selection` is never emitted. The move-execution
observation needs `ExecutePlayerMove`, `ExecuteEnemyMove`,
`ExecutePlayerMoveDone`, and `ExecuteEnemyMoveDone`; otherwise
`resolution_open` is `null` and `action_resolution` can only come from a
non-zero `wActionResultOrTookBattleTurn`. Each
session observes only its own emulator, so `pokered://peer-game-state`
reflects the peer's hooks. Timed remote mode has no local peer resource; its
`pokered://game-state` is read through the timed owner.

## Link cable modes

The bundled PyBoy fork provides the bit-accurate serial backend required by
Gen I Pokémon. Real sessions use that backend for in-process and TCP links;
the older semantic bridge remains only as a compatibility path for test
doubles that intentionally do not model a PyBoy motherboard. A real PyBoy
session with an incomplete serial contract fails closed instead of silently
switching to semantic exchange.

Native attach only installs the serial backend and its transport callbacks. It
does not write native serial registers (`SB`/`SC`, `FF01`/`FF02`), Pokémon HRAM
such as `hSerialConnectionStatus`, or install symbol-level exchange hooks. The
ROM controls its clock source and establishes its connection status from native
serial traffic. The semantic bridge is therefore not production
evidence for `link_pair`, `link_listen`, or `link_connect`.

### In-process pair

`link_pair` owns two sessions in one process and uses the native bit-accurate
serial coordinator for real PyBoy sessions. The historical PR #17
source-runtime baseline recorded 47/47 local tests and 19/19 strict trade and
battle rows against the pinned external assets. PR #19's post-change
verification reran the focused transport/lifecycle slice and the remote
transport slice; it did not silently present the baseline local gameplay rows
as a full rerun.
Configure the peer before launching the MCP server:

```bash
export POKERED_PEER_ROM_PATH=rom/yellow/pokemon-yellow.gbc
export POKERED_PEER_SYM_PATH=rom/yellow/pokemon-yellow.sym
export POKERED_PEER_ROM_SHA1=cc7d03262ebfaf2f06772c1a480c7d9d5f4a38e1
```

Then call `link_pair`, use `link_step`, and call `link_unpair` when finished.
The exact fixture, runtime, and game-flow requirements are in the runbook.

The public tool surface can load only the *primary* session's state, so a peer
that must start from an admitted save-state fixture uses the entry point's
documented launch contract instead of a private call:

```bash
export POKERED_PEER_STATE_PATH=tests/fixtures/link/blue/cable_club.state
export POKERED_PEER_STATE_SHA1=<40-character SHA-1 of that fixture>
```

`POKERED_PEER_STATE_PATH` and `POKERED_PEER_STATE_SHA1` are required together
and need a configured peer session (`POKERED_PEER_ROM_PATH` and
`POKERED_PEER_SYM_PATH`). The digest and size are verified before the bytes
reach the emulator, a mismatch fails closed at startup, and the pair is still
linked explicitly with `link_pair`.

### Remote TCP pair

Two independent MCP servers can use `link_listen` and `link_connect`. The
listener defaults to the frame-pacing leader and the connector to the follower.
After the versioned HELLO, Red leads a differing Red/Blue pair and the non-Yellow
endpoint leads a Yellow cross-family pair. Same-version pairs retain their
caller-provided pacing orientation. These roles are transport metadata, not an
assignment of the cartridges' hardware clocks: attach and negotiation leave
native serial registers and ROM-owned connection status untouched. Pacing
negotiation alone does not establish full TCP trade or battle acceptance. Poll
`link_status` until it reports `remote_mode` as `connected`, then call
`link_disconnect` at teardown.

The MCP remote-link API enforces localhost-only binding and connection
(`127.0.0.1`, `localhost`, or `::1`). The transport has no authentication or
encryption and must not be exposed to an untrusted LAN, the public internet,
or a WAN until an authenticated encrypted channel is added. Treat this as a
security boundary, not as cross-host support. `link_listen` and `link_connect`
accept an optional `peer_rom_version` expectation; a mismatched HELLO is
rejected. Socket writes, public listener waits, and worker teardown all have
bounded deadlines. Game-driven remote exchanges use a bounded 30-second
deadline to accommodate independently paced emulator runners; the lower-level
transport API retains its 5-second default for general callers.

The current evidence boundary is deliberately narrow:

| Test surface | What it can establish | What it cannot establish |
|---|---|---|
| `tests/test_link_protocol.py` | ROM-free Pokémon serial constants and synthetic exchange behavior | Emulator or game compatibility |
| `tests/test_link_transport.py` and the `tests/test_network_backend_*.py` modules | In-process queues and TCP edge/response primitives | A real game trade or battle |
| `tests/test_link_symbols_real_roms.py` | Required labels resolve when local symbols are available | A complete gameplay flow |
| `tests/test_link_integration.py` | Fixture-gated in-process real-ROM milestones | Remote two-process behavior |
| `tests/test_link_integration_remote.py` | Fixture-gated remote transport/serial handshake and link-menu-pass milestones (entry module) | A full user-driven remote trade or battle |
| `tests/test_link_integration_remote_rpc.py` and `tests/test_link_integration_remote_trade.py` | Fixture-gated remote RPC-flow, menu-vote, trade-center-exchange and agent-sync milestones | A full user-driven remote trade or battle |
| `tests/test_pyboy_link_session_subprocess.py` | Canonical color Red/Blue/Yellow native-serial trade and battle acceptance entrypoints (two-subprocess TCP) | A full source release, compiled gameplay parity, or MCP-facing starter/trade/battle gameplay; recorded source strict battle passed 19/19, while source strict trade failed 18/19 |
| `tests/test_pyboy_link_session_subprocess_link_menu_history.py` and `tests/test_pyboy_link_session_subprocess_peer_lifecycle.py` | ROM-free link-menu history recorder plus peer supervision, shutdown, sync-boundary, trace-watchdog and result-validation regressions | Emulator, game, or real-ROM behavior |
| `tests/test_pyboy_link_session_roms.py` | Diagnostic matrix plus parameterized canonical Red/Blue/Yellow local trade and battle acceptance entrypoints | Stock-variant coverage, or a release result from a skipped, RAM-mutated, or unpinned path |
| `tests/test_pyboy_link_session_roms_serial.py` and `tests/test_pyboy_link_session_roms_diagnostics.py` | Yellow two-session serial-core and byte-exchange smoke plus link-menu warp (trade center, colosseum) and link-battle-start milestones | A completed trade or battle, or a release result from an unpinned path |

Do not describe a transport milestone as “trade complete.” A full trade or
battle needs an acceptance result from the actual release runtime, matching
ROMs, matching save-state fixtures, bounded deadlines, a clean teardown, and
an explicit statement about any test-driver menu control. The strict
declaration contains 19 trade and 19 battle entrypoints: nine local ordered
rows, one dedicated Red/Yellow assertion, and nine remote ordered listener /
connector rows for each operation. See the recorded candidate matrix above for
source trade/battle outcomes and the separate native and MCP evidence boundaries.

## Walkthrough scripts

The scripts are useful diagnostics and fixture producers, not a substitute
for the release gate. The `--option-b` paths and any direct game-memory writes
are intentionally outside the supported gameplay claim:

- [`scripts/full_to_brock.py`](scripts/full_to_brock.py) drives the Red
  pipeline and can fall back to a RAM-based party top-up when the honest
  grind does not reach its target.
- [`scripts/blue_forest_to_brock.py`](scripts/blue_forest_to_brock.py)
  defaults to a Route 2 grind and retains `--option-b` as a diagnostic RAM
  boost.
- [`scripts/yellow_to_brock.py`](scripts/yellow_to_brock.py) has the same
  distinction between its honest-grind path and the `--option-b` diagnostic.

Any run that writes party, event, repel, or other game state directly is a
plumbing diagnostic. It must not be reported as an untouched, human-valid
playthrough. Outputs should go to an ignored directory such as
`walkthrough_output/`; do not commit ROM-derived states, screenshots, or
logs.

For ordinary link fixtures, use the bounded, pinned producer
[`scripts/produce_cable_club_fixture.py`](scripts/produce_cable_club_fixture.py)
with a `cerulean_pc.state` captured against the same ROM bytes. It validates
the ROM, symbols, PyBoy version, and fork revision from `VERSIONS.md`, then
fails after its 180-second or 64-movement default budget instead of waiting
indefinitely. The retained vanilla source does not meet the same-ROM capture
condition: replay failed at the 64-step movement bound, and the manifest's
ordinary producer revision `25e231c` is historical. There is no
implementation-snapshot reproducibility claim for the vanilla fixture bytes and no separate
Yellow-specific producer. A successful producer run proves only the Cable Club
map/position, not a trade or battle.

The tracked
[`scripts/prepare_battle_cable_club_fixtures.py`](scripts/prepare_battle_cable_club_fixtures.py)
creates separate immutable battle-start states from ordinary fixtures. It
validates the selected ROM, creates a legal multi-mon party, and the acceptance
runner loads the result without mutating party state at runtime. This is a
deterministic derived-fixture step, not evidence that a human captured a battle
state. Vanilla derived rows remain partial until their ordinary source state is
proven to match the vanilla ROM; their existing bytes do not establish current-
head ordinary capture provenance. The exact hashes and provenance statuses are
in [`release-evidence/fixture-manifest.json`](release-evidence/fixture-manifest.json).

Validate the manifest before a real-ROM run:

```bash
python scripts/validate_fixture_manifest.py --schema-only
python scripts/validate_fixture_manifest.py \
  --fixture-root "$PWD/tests/fixtures/link"
```

See [`scripts/WALKTHROUGH_README.md`](scripts/WALKTHROUGH_README.md) for
diagnostic walkthrough notes. Never set `POKERED_SKIP_SHA1=1` for acceptance
or release evidence.

## Development test command

For development diagnostics, the broad local command is:

```bash
python -m pytest -q -ra
```

The asset-free release smoke is narrower and reproducible without ROMs:

```bash
EVIDENCE_DIR="$(mktemp -d)"
python scripts/production_gate.py \
  --repo-root "$PWD" \
  --python "$(command -v python)" \
  --runtime-mode source \
  --unit-only \
  --repeat-timing 5 \
  --evidence-dir "$EVIDENCE_DIR" \
  --format text
```

The bounded localhost concurrency diagnostic is:

```bash
python scripts/network_concurrency_probe.py
```

Historical unit/timing, asset-tier, and synthetic concurrency results do not
establish full source-gate completion, compiled gameplay, or live MCP gameplay.

Use the tiered commands in [`docs/PRODUCTION_RUNBOOK.md`](docs/PRODUCTION_RUNBOOK.md)
when ROMs, symbols, fixtures, or the bundled link runtime are present. The
current matrix declaration is complete, but its collection audit is not
runtime evidence. A green unit suite alone is not a production result; every
required tier must run with no unexpected failures, skips, xfails, or
timeouts.

## License

The harness code is LGPL-3.0-only. No game-derived assets are distributed.
