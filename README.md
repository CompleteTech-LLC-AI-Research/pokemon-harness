# pokered-harness

`pokered-harness` is a memory-first automation harness for Pokémon Red,
Blue, and Yellow. It loads a user-supplied ROM and symbol file, exposes a
typed `Session` API, and can serve that session over MCP.

This repository is a source tree. It does not distribute commercial ROMs,
symbol files, save states, or other ROM-derived artifacts.

## Release status

**Status: `PARTIAL` — not production-ready.** This is the current
2026-09-04 candidate audit on branch `codex/production-next-20260904`, based on
the public `master` head `51d60a056178fc922ffaac49291365ce409d4221`. The
implementation hardens bit-accurate serial timing, save-state migration,
native callback error propagation, session close races, and the real-ROM
battle driver. Current battle acceptance passes; the complete current trade
matrix still has one mixed-version TCP failure.

Current evidence, using the operator-supplied assets pinned in
[`VERSIONS.md`](VERSIONS.md):

- **Source unit/timing gate:** `980/980` unit tests and `55/55` timing cases
  passed across five timing repetitions. Collection found `1,132` tests, all
  `19/19` strict trade and `19/19` strict battle entrypoints, and the fixture
  manifest passed schema validation. The focused serial, network, session,
  lifecycle, and packaging regression set passed `155/155`.
- **Native build:** the vendored PyBoy 2.7.0 fork
  (`c565df66c3731fad2856169a90f6bbec99925915`) built a CPython 3.12 Linux
  wheel successfully in an isolated temporary copy. A complete strict
  gameplay run using that compiled wheel is not claimed here; the current
  acceptance results below use the bundled source runtime.
- **Strict battle acceptance:** the source-runtime gate passed `19/19` local
  and TCP real-ROM entrypoints in `1,237.8s`, with no skips, errors, or
  test-only protocol bypasses. All five ROM hashes, three symbol hashes, and
  ten fixture entries were validated in the same gate.
- **Strict trade acceptance:** the source-runtime gate is `FAIL` at `18/19`
  after `1,093.9s`. The sole failure is
  `red_color-listen-blue_color-connect`, which timed out at `722.6s` during
  Trade Center warp. Its final transport snapshot had `1,080` applied owner
  edges, `9/10` sync counters, zero owner-edge errors, zero IRQ callback
  errors, and no pending requests; this is an unresolved protocol/ROM
  rendezvous failure, not a backend crash. The other 18 rows passed.
- **Quality checks:** Ruff lint passes, and the release-hygiene formatter check
  passes for the touched packaging test. The repository-wide formatter check
  still reports unrelated legacy files and was not used to rewrite them.

The current serial timing and battle-input work was checked against the
[`pret` Pokémon Yellow serial disassembly](https://raw.githubusercontent.com/pret/pokeyellow/master/home/serial.asm)
and the
[`pret` battle-core disassembly](https://raw.githubusercontent.com/pret/pokeyellow/master/engine/battle/core.asm).
Those sources confirmed the hardware-cycle timing and the one-based move-menu
cursor/input contract; they do not replace real-ROM acceptance evidence.

Release status remains `PARTIAL` because the current full trade matrix is
failing, the compiled-runtime strict gameplay matrix has not been rerun, the
vanilla-derived fixture provenance is incomplete, and remote TCP remains
loopback-only, unauthenticated, and unencrypted. MCP-facing starter, trade,
and battle gameplay, broad platform/concurrency qualification, and secure
cross-host operation remain outside the evidence above.

### Historical evidence retained below

The retained pre-PR #52 build and unit verification supports:

- **Wheel/install:** `uv build --wheel` succeeded. A fresh Python 3.12.13
  environment installed the wheel, `uv pip check` passed, and the archive
  contained no ROM, symbol, fixture, generated-native, or path-traversal
  entries. The installed runtime identified PyBoy 2.7.0 at fork revision
  `c565df66c3731fad2856169a90f6bbec99925915`.
- **Launch:** with operator-supplied Red color ROM and symbol files matching
  [`VERSIONS.md`](VERSIONS.md) (`e1deed63080bc24cad5fba18ecb3184f905d16d4`
  and `03783c86a42588bd77f73bd7814cf8d70e590118`),
  `python -m pokered_harness.mcp_server` reached clean EOF and exited
  successfully. ROMs and symbols are not distributed in the wheel; without
  the required paths, launch fails closed.
- **Runtime matrix:** clean source and compiled-Cython environments each passed
  the current unit tier `970/970` and five timing repetitions (`55/55` total),
  with both collection paths and the bit-accurate serial contract passing.
  The CGB fast-serial regression slice passed `61/61` in both runtimes.
- **Collection/matrix audit:** the current candidate collected `1,122` tests;
  all declared local, remote-role, reversed-role, variant, strict-trade, and
  strict-battle entrypoint sets were present (`9/9`, `9/9`, `6/6`, `9/9`,
  `19/19`, and `19/19` respectively).
- **Asset-backed scoped checks:** with the operator-supplied ROMs, symbols, and
  states, the source local tier passed `47/47` in `530.0s`, the remote
  transport/MCP tier passed `23/23` in `39.1s`, and the ten-entry fixture
  manifest passed byte validation. These tiers cover LinkMenu, transport, and
  lifecycle behavior; they are not strict trade or battle acceptance.
- **Strict battle acceptance:** the pre-PR #52 source-runtime gate passed all
  `19/19` real-ROM local and TCP battle entrypoints in `1,675.4s` with four
  matrix workers, with no skips or errors. No post-PR #52 full battle rerun is
  claimed; this establishes only pre-PR #52 battle-turn acceptance and does
  not clear the unresolved trade gate.
- **MCP EOF reliability:** the real stdio remote-lifecycle test passed five
  consecutive isolated runs after the full local-tier rerun passed. The
  earlier `46/47` ordered-suite result was a non-reproducing failure and is not
  treated as a passing release gate.

Strict end-to-end trade readiness remains **not proven**. The pre-PR #52 source
full trade gate was `FAIL` at `17/19`, with Blue/Blue and Blue-listener →
Red-connector failures. After PR #52 role/test-driver work, one isolated
Blue-listener → Red-connector full trade passed with exchanged party records,
but Blue/Blue still failed at the trade-center warp; `984` native edges
matched and no backend errors were reported. No complete post-PR #52 trade
matrix is qualified. The retained pre-PR #52 serialized `18/19` result and
the native/Cython `16/19` trade and `17/19` battle result remain historical
scoped artifacts. Transport, LinkMenu, fixture, MCP lifecycle, or battle-turn
results do not substitute for a complete current-head real-ROM trade matrix.

The `passive_sync()` change is test-driver-only pre-drive coordination. It is
not a production MCP synchronization guarantee and does not establish
gameplay acceptance. The unresolved Blue/Blue result reinforces the `PARTIAL`
release status.

The follow-up native-backend audit also found a race when a slave core became
unarmed between two readiness checks. The backend now classifies that transition
and emits its bounded no-data fallback instead of crashing the worker; a focused
regression test covers the interleaving. This is a lifecycle fix only and does
not qualify the unresolved real-ROM trade or battle gates.

The detailed records below are retained historical or scoped evidence; they do
not supersede the current status above.

### Historical audit context

This repository remains an audited production-readiness candidate, not a
production release. The live target is `CompleteDotTech/pokemon`. The
prior/pre-reconciliation public documentation baseline is exact commit
`f048870bdbd4837b5494006ae49fe40510832a89` (PR #45, 2026-09-04); it is not the
current public head. The last
verified pre-fix implementation/runtime snapshot under audit is exact commit
`a8576b5ecb8e7039eefe0e02865b0bfc031387a7` (the code snapshot associated with
PR #44). Public changes after that snapshot are not documentation-only: the
integrated fix commit
`7b4b5b72ad373d2293d51e3d11717314606ee442` (cherry-picked as `7b4b5b7`)
moves stale `EDGE_RESP` closure outside `_edge_response_lock` and adds a
bounded regression test. Its focused post-fix network suite passed 38/38, and
Ruff and format checks were clean. The retained pre-PR #52 post-fix serialized
source-runtime strict-trade production gate documented below is `FAIL` at
18/19, with one
failed row. The retained native strict gate remains pre-fix evidence from the
`a8576b5` implementation / `f048870b` documentation snapshot, with trade
16/19 and battle 17/19. The source-runtime local strict battle result also
remains pre-fix `PASS` at 9/9. These scoped results do not make the release
production-ready; release status remains `PARTIAL`. The implementation baseline
for that snapshot is
`6d541b7867e82fa548c456008e6acd7fb1071586` (PR #42); the prior merged
implementation head was `71ca834d673c52eb74044089e92b09e7e3ae00a0`. The
retained exact prior-candidate evidence below was collected at
`3399407aa04f6e5e496df628442597c03e0adcc6` (2026-09-04); it is not a
full acceptance result for the current implementation/runtime snapshot.
The merged production-followup adds serial save-state restoration,
native bootstrap ownership checks and build-metadata cleanup, bounded MCP
teardown, fail-closed gate accounting, and safe cleanup for partially
initialized PyBoy objects. The
underlying remote-dispatch implementation is
`54a739be4a5f2d95a924c6f35c2aa695246ddd2f` (PR #35, merged 2026-09-03).
PR #35 moves remote serial-edge dispatch to an explicit native
instruction-batch boundary, outside serial register access. This removes the
reentrant callback path that had made source and compiled PyBoy behavior
diverge during remote linking.
The merged implementation head remains a `PARTIAL` publication candidate, not a
`PRODUCTION-READY` release, until the remaining gates and review conditions
are closed.

The acceptance update merged in PR #42 adds bounded production-
gate deadlines, runtime-provenance checks, fail-closed matrix accounting,
preserved network-close diagnostics, and strict subprocess result validation.
Its pre-fix source-runtime representative checks passed: in-process Red-color/Yellow
trade and battle, TCP Red-color/Yellow trade and battle, and the real MCP
integration suite (including save/load and TCP EOF cleanup). At the pre-fix
implementation/runtime snapshot under audit, six real MCP integration checks
passed in 13.11 seconds with one SDL warning, and 104 MCP dispatch tests
passed. The source-runtime local strict battle matrix also passed all nine
ordered canonical color pairs; the row-level snapshot is recorded below. The
pre-fix source unit/timing gate also passed 954/954 unit tests and 50/50 timing cases in
each of five repetitions. These are scoped pre-fix source-runtime results, not
the post-fix full trade result, a passing native/Cython strict gate, secure
cross-host operation, or MCP-driven starter/trade/battle gameplay; the release
decision remains `PARTIAL`.
A valid pre-fix native/Cython strict gate for the implementation/runtime
snapshot is retained in the operator-local artifact
`/tmp/poke-harness-native-strict-evidence-a8576b5`. It used Python 3.12.13 and
PyBoy 2.7.0 fork
`c565df66c3731fad2856169a90f6bbec99925915`. The trade and battle tiers each
declared and executed all 19 entrypoints; the trade result was 16/19 and the
battle result was 17/19. The gate outcome was `FAIL`; the terminal failures are
recorded below. No native owner/IRQ errors occurred, and every failure was a
strict real-ROM result rather than a bypass.

### Last verified pre-fix implementation/runtime snapshot: source-local battle

At the last verified pre-fix implementation/runtime snapshot
`a8576b5ecb8e7039eefe0e02865b0bfc031387a7` (the prior/pre-reconciliation public
documentation baseline is `f048870bdbd4837b5494006ae49fe40510832a89`; it is not
the current public head), the source-runtime strict
local battle matrix completed 9/9 ordered canonical color pairs. The first six
rows below were recorded earlier in the same source-local acceptance campaign;
the final three are fresh rows at this pre-fix implementation snapshot. Every row
reached Link Battle and recorded the move and damage hooks used by the
assertion.

| Pair | Result | Duration |
|---|---|---:|
| `yellow-yellow` | `PASS` | 125.397s |
| `blue-blue` | `PASS` | 303.395s |
| `red-red` | `PASS` | 231.966s |
| `red-blue` | `PASS` | 612.110s |
| `blue-red` | `PASS` | 629.876s |
| `red-yellow` | `PASS` | 179.162s |
| `yellow-red` | `PASS` | 182.89s |
| `blue-yellow` | `PASS` | 443.50s |
| `yellow-blue` | `PASS` | 480.31s |

This is pre-fix source-local battle evidence only. It does not change the
retained pre-PR #52 post-fix source-runtime strict-trade result (`FAIL`, 18/19, with the sole
failure detailed below), the retained pre-fix native/Cython strict gate (trade
16/19 and battle 17/19, with the terminal failures below), remote full-matrix
qualification,
MCP-driven gameplay, vanilla fixture provenance, platform/concurrency gates, or
secure cross-host TCP.

The scoped MCP run against the pre-fix implementation/runtime snapshot used
ordinary real-ROM calls without a memory or state bypass: it reached Red's
bedroom (map 38, `(3,7)`), exited the house, reached Pallet Town (map 0, `(5,5)`), reached
Oak's Lab (map 40), and moved in the lab. A bounded starter attempt ended at
map 40, `(5,3)`, with `party.count=0`. This proves MCP control and observation
over a real session, not starter acquisition or MCP-facing trade/battle; those
remain unproven.

The pre-fix implementation snapshot's source-local battle evidence is 9/9
ordered pairs: three fresh rows and six existing campaign rows all reached Link
Battle and exercised the move and damage hooks. This is scoped source-runtime local
evidence, not the 19-entrypoint strict battle acceptance, current remote
matrix, or native/Cython qualification.

### Native/Cython strict-gate snapshot

The valid pre-fix native/Cython strict gate for implementation/runtime snapshot
`a8576b5ecb8e7039eefe0e02865b0bfc031387a7` is retained at
`/tmp/poke-harness-native-strict-evidence-a8576b5`. It used Python 3.12.13 and
PyBoy 2.7.0 fork
`c565df66c3731fad2856169a90f6bbec99925915`. The trade and battle tiers each
declared and executed all 19 entrypoints.

- Strict trade: `16/19`. The `blue_color listener -> yellow connector` row
  failed at `83.39s` after the trade hooks with an incorrect/malformed party
  record. The `red_color listener -> yellow connector` row failed at `729.51s`
  with a LinkMenu rendezvous timeout after 2,197 balanced/applied edges and one
  pending request. The `yellow listener -> blue_color connector` row failed at
  `721.39s` with a LinkMenu rendezvous timeout after 6,248 balanced edges.
- Strict battle: `17/19`. The `blue_color listener -> yellow connector` row
  failed at `376.94s` because sync marker 113 did not converge after 18,840
  applied edges. The `yellow listener -> blue_color connector` row failed at
  `142.20s` because the battle Colosseum warp rendezvous did not converge after
  952 balanced edges.

No native owner/IRQ errors occurred. These five pre-fix failures are strict
real-ROM results, not bypasses, so this is a valid native/Cython gate with a
`FAIL` outcome rather than a passing production release result. The post-fix
serialized source-runtime strict-trade gate below is separate source evidence:
it completed all 19 trade entrypoints but failed at 18/19, so it is not a
passing full-trade release result and does not replace this retained native
gate. The post-fix focused 38/38 network result is transport/regression
evidence and does not qualify a post-fix battle matrix.

### Retained pre-PR #52 post-fix source-runtime strict-trade gate

The retained pre-PR #52 post-fix serialized source strict-trade production
gate is retained at
`/tmp/poke-harness-source-serial-acceptance-20260904/evidence`. It used source
Python 3.12.13, PyBoy 2.7.0 fork
`c565df66c3731fad2856169a90f6bbec99925915`, bit-accurate serial, and
`matrix-workers=1`. The complete matrix audit collected `1107`; all 19 trade
entrypoints were declared and executed. The overall result was `FAIL`: 18/19
passed, 1/19 failed, 0 skipped, 0 errors, 0 xfail, and 0 xpass, with a
duration of `2788.895s`.

The sole failure was
`tests/test_pyboy_link_session_subprocess.py::test_subprocess_pair_completes_trade_over_tcp[blue_color-listen-blue_color-connect]`
at `721.4846s`: the trade-center warp rendezvous did not converge. Pre-close
stats had 984 inbound/applied edges, 123 IRQ callbacks, zero owner-edge
errors, and no pending edge requests. The evidence hashes are
`gate-report.json`=`7de82390f52bdd4b8e3d569204ef8f3951113a8c1706f403e53a6f681e0db453`,
`gate-report.txt`=`90e5b17706ba87a095a7fa32c317d84d957e2c76550a7d06f2a38720ab451a97`,
and
`evidence-manifest.json`=`c6f07eb29dbd272ead265b628bd2d2c41e287ec02ab1229db90716ff95dde45a`.

This is pre-PR #52 post-fix source evidence after code fix `7b4b5b72` (cherry-picked as
`7b4b5b7`). It is a complete failed source strict-trade result, not a
production-ready result: the retained native/Cython strict gate remains
pre-fix at `a8576b5` with trade 16/19 and battle 17/19, and the source-local
strict battle remains pre-fix `PASS` at 9/9. Release status remains `PARTIAL`.

The merged head includes the MCP lifecycle, runtime packaging, in-process
serial/lifecycle, remote TCP follow-ups from PRs #12-#17, concurrency/lifecycle
hardening from PR #19, bounded MCP shutdown and remote-generation hardening from
PR #22, native Cython tick/timing compatibility from PR #26, explicit
source/Cython gate selection, bounded remote game-exchange pacing from PR #27,
remote synchronization/teardown hardening, MCP endpoint cleanup, controlled
gate accounting, and faster headless sessions.

The production-followup also hardens serial cleanup ownership and retry,
clean remote disconnect handling, transactional `LinkPair` setup, restoration
of borrowed PyBoy callback state, bounded `NetworkBackend` partial-frame reads
with fail-closed teardown, and bounded, isolated bootstrap checks. Focused
link-pair, network, runtime, and fixture regression coverage exercises these
boundaries; it adds no new gameplay acceptance.

A historical complete all-tier source-runtime gate was collected on 2026-09-02
from isolated source head `df0e7424c87c812a57f257286b0dc00e87c498f4`, whose
implementation tree is merged as the PR #17 parent `b0b63c8`. It used Python
3.12.13, Pytest 9.1.1, and vendored PyBoy 2.7.0 at fork revision
`c565df66c3731fad2856169a90f6bbec99925915`. It collected 698 tests and
passed every selected tier: unit 554/554, local real-ROM 47/47, remote
transport/MCP 15/15, strict trade 19/19, strict battle 19/19, and timing
40/40 in each of five repetitions. The ten-entry external fixture manifest
also validated, with no skips, xfails, failures, errors, or timeouts. This
remains the complete all-tier baseline; the PR #19 runtime changes were
verified with the separate follow-up slices below rather than silently
presented as a rerun of all 19 trade and battle rows.

The post-PR #19 source-runtime verification used Python 3.12.13 and collected
699 tests: the ROM-free gate passed unit 555/555 and timing 40/40 in each of
five repetitions; the focused transport/MCP suite passed 167/167; the bounded
localhost concurrency probe passed 8/8 in each of five repetitions; and the
real-ROM remote transport slice passed 15/15. The ten-entry fixture manifest
schema passed. Its sanitized evidence remains outside version control because
ROMs, symbols, and save states are operator-supplied assets.

A historical PR #28-base source-runtime gate used managed Linux Python 3.12.13
and Pytest 9.1.1. It collected 706 tests and passed unit 562/562 and timing
40/40 in each of five repetitions, with no skips, xfails, failures, errors, or
timeouts. This is historical PR #28-base evidence; ROM-backed tiers were not
run in that asset-free command.

On the prior integrated head `059bf9e5b5b868a81837eb58df0fea3f4b09f647`, the
focused source and Cython transport,
serial, and PyBoy-link suite passes 110/110 in each runtime. A source-runtime
asset-backed strict trade gate recorded all 19/19 ordered local and remote
acceptance rows. The corresponding native Cython trade gate completed 18/19:
the sole failure is the remote `yellow-listen-blue_color-connect` row, which
stalls before the party exchange at the configured 720-second bound. A valid isolated
native retry reproduced that stall; a timing-altered diagnostic could reach the
trade hooks but produced incorrect party records and is not acceptance
evidence. The full native battle matrix has not been qualified.
The source run was a trade-tier run whose supervisor started before PR #35 was
published; its child runs loaded the current implementation, but it is not a
clean post-merge all-tier sign-off.

Follow-up isolated diagnostics on 2026-09-03 did not establish reliable
cross-runtime behavior for that row. Two exact native retries completed in
114.86 seconds and 116.33 seconds with exact party-record exchange, while
another native run reached the trade path and failed party-record equality;
separate source/native probes also stalled at different ROM phase boundaries.
The focused diagnostic lanes passed their serial/backend checks (42/42 and
35/35), and no queue overflow, owner-dispatch error, or unbalanced edge count
was found. These differing outcomes are evidence of a scheduling-sensitive
protocol/phase defect, not a verified fix. No timing hack, synthetic bit, or
test-only game-state mutation was accepted.

A historical native remote battle acceptance lane exercised 3/9 direct strict
remote rows with a bounded 155-second pair deadline and no bypasses:
`Blue-color↔Blue-color` was `PASS`, while `Red-color↔Yellow` and
`Yellow↔Red-color` were `FAIL`. Six remote rows remain unrun, and the full
19-entrypoint native battle set remains unqualified.

A prior isolated source/Cython unit/timing gate on 2026-09-03 collected 745
tests in each runtime and passed unit 600/600 plus timing 40/40 in each of five
repetitions. It remains historical evidence; the retained prior-candidate
separate-interpreter result below is the latest recorded gate for that scope,
not evidence for the current implementation/runtime snapshot. Fresh uv-managed
source and native
environments installed the project, passed `uv pip check`, and the native
bootstrap completed with both `pyboy` and `pokered-harness` owners. The bare
host `python3` could not create a new standard-library virtual environment
because `ensurepip` was unavailable, so that alternate clean-install path
remains open.

The retained prior-candidate separate-interpreter dual source/native
unit/timing gate at `3399407aa04f6e5e496df628442597c03e0adcc6` collected 1,094
tests in each runtime. Each runtime had 949 unit tests pass and timing pass
50/50 in each of five repetitions. This unit/timing scope does not establish
ROM-backed gameplay, strict trade, or battle.

At that prior candidate, the local asset tier passed source 47/47 in
531.3477s and native 47/47 in 38.2974s. The remote asset tier passed source
16/16 in 35.6044s and native 16/16 in 30.8627s. The gate verified 5/5 ROM
hashes, 3/3 symbol hashes, and 3/3 fixture hashes, and the 10-entry fixture
manifest passed. These are scoped asset and transport/session results; the
implementation-snapshot MCP integration/navigation evidence above does not establish
starter acquisition, MCP-facing trade/battle, or the post-fix strict full
trade/battle result.

An earlier `python -m pytest -q -ra` run completed 604 passed,
141 expected BYO-asset skips, and one SDL warning. It is a clean-checkout
diagnostic rather than a release result: the ROM-backed tests were skipped
because this isolated checkout intentionally contains no ROMs, symbols, or
save states.

The integrated post-fix candidate's asset-free full suite passed 966 tests,
with 141 explicit BYO-asset skips and one SDL warning. This is a clean
package/test-surface regression result; the skipped ROM-backed rows remain
outside its evidence boundary.

Historical scoped Windows validation on 2026-09-02 used Windows Python 3.12.10 and new
virtual environments in isolated checkouts. The editable install, `pip check`,
source bootstrap, and the post-PR #23 pinned Cython build/check passed. The
post-PR #23 Cython runtime exposed `PyBoy.mb.serial` and passed direct
attach/tick/close smokes for canonical color Red, color Blue, and Yellow
(3/3). Earlier scoped source/MCP checks passed 23 tests with one unrelated
WSL-worktree skip and MCP stdio 4/4. This is scoped Windows execution
evidence, not full native qualification: strict Cython gameplay, real-ROM
concurrent load, the remote trade/battle matrix, and macOS coverage remain
open.

The release status recorded by the prior/pre-reconciliation public documentation
baseline `f048870bdbd4837b5494006ae49fe40510832a89` remains `PARTIAL`; it records the
last verified pre-fix implementation/runtime snapshot
`a8576b5ecb8e7039eefe0e02865b0bfc031387a7`. The post-fix serialized
source-runtime strict-trade production gate completed all 19 declared and
executed entrypoints and is `FAIL` at 18/19; its exact artifact boundary and
sole failure are recorded above. The source-runtime local strict battle matrix
is pre-fix `PASS` at 9/9, and the scoped MCP integration/navigation checks pass
in their pre-fix implementation-snapshot boundary.
The valid pre-fix native/Cython strict gate executed all 19 declared entrypoints
in each trade and battle tier, but is `FAIL` at 16/19 trade and 17/19 battle;
its strict real-ROM failures are detailed above. The focused post-fix network
suite passed 38/38, with Ruff and format checks clean; that focused result is
separate transport/regression evidence and does not qualify a post-fix battle
matrix. The post-fix source-trade result is a complete failed acceptance result,
not a production-ready pass. Current remote full battle-matrix qualification,
MCP-facing starter/trade/battle gameplay, fixture provenance, concurrent
real-ROM load, cross-platform native qualification, standard-library clean-install
verification, independent review, and authenticated/encrypted cross-host TCP
also remain open. TCP remains loopback-only and unauthenticated/unencrypted.

The pre-PR #27 source-runtime asset-backed broad run is diagnostic and bounded,
not a release sign-off: at the 5,400-second supervisor cutoff it had completed
418 of 703 collected tests (386 passed, 20 skipped, 12 failed, 285 not
started). The 12 failures were remote TCP integration cases ending in
`SerialLinkClosed` after the old 5-second exchange deadline; the recorded
`blue-blue` flow passes with PR #27's 30-second game-exchange deadline. The Red
color fixture now reproduces byte-for-byte and its four previously excluded
remote diagnostic matrices are enabled, but a complete post-fix broad rerun
remains pending.
Vanilla fixture provenance remains `PARTIAL`: six canonical entries have
verified provenance, while four vanilla-derived ordinary entries do not. The
retained replay failed at the 64-step movement bound and ordinary producer
revision `25e231c` is historical. Real-ROM concurrent-load coverage, full
native platform qualification, independent review, and authenticated/encrypted
cross-host TCP also remain open.

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

### Current candidate matrix

| Capability | Current result | Evidence boundary |
|---|---|---|
| Source unit/timing | `PASS` — 980/980 unit and 55/55 timing cases | Current source runtime, Python 3.12.13, PyBoy 2.7.0 fork `c565df66c3731fad2856169a90f6bbec99925915`; 1,132 tests collected and all five ROM/SYM pins validated. |
| Strict trade | `FAIL` — 18/19 | Current source runtime; all 19 rows declared and executed. The sole failure is `red_color-listen-blue_color-connect` at the Trade Center warp after 722.6s; 18 other real-ROM local/TCP rows passed. |
| Strict battle | `PASS` — 19/19 | Current source runtime; all 19 local/TCP real-ROM rows passed in 1,237.8s with no skips, errors, or test-only protocol bypasses. |
| Native/Cython build | `PASS` — wheel built | Vendored PyBoy native wheel build passed in an isolated temporary copy. No strict gameplay run using the compiled wheel is claimed. |
| Release readiness | `PARTIAL` | Trade reliability, compiled-runtime gameplay parity, vanilla fixture provenance, MCP-facing gameplay, broad platform/concurrency coverage, and secure cross-host TCP remain open. |

The retained evidence set is:

- historical complete all-tier source-runtime baseline: `PASS`, collection 698, unit
  554/554, local real-ROM 47/47, remote transport/MCP 15/15, strict trade
  19/19, strict battle 19/19, and timing 40/40 across five repetitions, with
  no skips, xfails, failures, errors, or timeouts;
- historical pre-PR #35 integrated source-runtime gate: `PASS`, collection 730, unit
  586/586, and timing 40/40 in each of five repetitions;
- historical pre-PR #35 integrated Cython/native unit-runtime gate: `PASS`, collection
  730, unit 586/586, and timing 40/40 in each of five repetitions; all five
  probed PyBoy modules were native extensions and the serial contract passed;
- retained prior-candidate separate-interpreter source/native dual unit-timing
  gate: `PASS` for the unit/timing scope, collection 1,094 in each runtime,
  949 unit tests passed, and timing 50/50 in each of five repetitions;
  ROM-backed gameplay was not run;
- retained prior-candidate local asset tier: source `PASS`, 47/47 in 531.3477s, and native
  `PASS`, 47/47 in 38.2974s;
- retained prior-candidate remote asset tier: source `PASS`, 16/16 in 35.6044s, and native
  `PASS`, 16/16 in 30.8627s; 5/5 ROM hashes, 3/3 symbol hashes, and 3/3
  fixture hashes verified, and the 10-entry fixture manifest passed;
- historical prior integrated source and Cython remote tier: `PASS`, 15/15 each;
- historical prior integrated source and Cython local/session tiers: `PASS`, 47/47 each;
- historical exact red-color local variant: `PASS` in source and Cython modes
  after the headless-audio optimization;
- historical source-runtime strict trade gate: `PASS` as a recorded trade-tier
  result, 19/19 ordered local and remote party-swap rows with the external
  hashed assets; its supervisor began before PR #35, so it is not a clean
  post-merge all-tier sign-off;
- historical source and Cython focused serial-link+network suite: `PASS`,
  78/78 in each runtime after the owner-boundary dispatch change;
- historical prior source and Cython packaging/runtime contract: `PASS`, 27/27
  packaging tests in each runtime, clean editable install, native bootstrap,
  matching package ownership, transient build-metadata cleanup, and `uv pip check`;
- historical prior MCP lifecycle hardening: `PASS` in the focused 159-test scoped
  suite, including real native local and remote lifecycle smokes; external
  trade fixtures were not part of that suite;
- historical Cython/native strict trade gate: `PARTIAL`, 18/19 in the complete
  matrix; follow-up exact-row retries have both passed and failed, including a
  party-record mismatch and phase stalls, so the row is not reliable
  acceptance evidence for the current runtime;
- pre-fix implementation/runtime snapshot source-runtime local strict battle
  matrix: `PASS`, 9/9 ordered canonical color pairs; each row reached Link
  Battle and recorded the required move and damage hooks. This is local
  source-runtime evidence only;
- pre-fix implementation/runtime snapshot native/Cython strict gate: `FAIL`,
  terminal artifact `/tmp/poke-harness-native-strict-evidence-a8576b5`; Python
  3.12.13, PyBoy 2.7.0 fork
  `c565df66c3731fad2856169a90f6bbec99925915`, and all 19 entrypoints declared
  and executed in each trade and battle tier. Trade was 16/19 and battle was
  17/19; no native owner/IRQ errors occurred, and the five failures were strict
  real-ROM results rather than bypasses;
- retained pre-PR #52 post-fix serialized source-runtime strict-trade production gate: `FAIL`,
  terminal evidence `/tmp/poke-harness-source-serial-acceptance-20260904/evidence`;
  source Python 3.12.13, PyBoy 2.7.0 fork
  `c565df66c3731fad2856169a90f6bbec99925915`, bit-accurate serial,
  `matrix-workers=1`, and complete matrix audit collection `1107`. All 19
  trade entrypoints were declared and executed: 18/19 passed and 1/19 failed,
  with 0 skipped, 0 errors, 0 xfail, and 0 xpass in `2788.895s`. The sole
  failure was the `blue_color-listen-blue_color-connect` subprocess trade row,
  which took `721.4846s` and did not converge at the trade-center warp
  rendezvous; pre-close stats had 984 inbound/applied edges, 123 IRQ callbacks,
  zero owner-edge errors, and no pending edge requests. Exact test identity,
  artifact hashes, and the post-fix/pre-fix evidence boundary are recorded in
  the dedicated section above;
- post-fix integrated network deadlock fix `7b4b5b72ad373d2293d51e3d11717314606ee442`
  (cherry-picked as `7b4b5b7`): `PASS` for the focused 38/38 network suite,
  with Ruff and format checks clean. This focused regression result is separate
  from the retained pre-PR #52 post-fix source strict-trade result; it does not replace the pre-fix
  native strict gate or qualify a post-fix battle matrix;
- historical Cython/native gameplay: `PARTIAL`; earlier representative Blue
  Color↔Yellow and Red Color↔Yellow trade/battle rows passed in scoped runs. A
  historical native remote battle lane exercised 3/9 direct strict rows with a
  bounded 155-second pair deadline and no bypasses: Blue-color↔Blue-color
  was `PASS`, while Red-color↔Yellow and Yellow↔Red-color were `FAIL`; six
  remote rows remain unrun and the full
  19-entrypoint native battle set remains unqualified;
- pre-PR #27 source-runtime broad diagnostic: `PARTIAL`, 418/703 tests
  completed before the 5,400-second bound (386 passed, 20 skipped, 12 failed,
  285 not started); no full-suite pass is claimed;
- historical post-PR #19 focused transport/MCP slice: `PASS`, 167/167;
- historical post-PR #19 bounded concurrency probe: `PASS`, 8/8 in each of five
  repetitions;
- historical post-PR #19 real-ROM remote transport slice: `PASS`, 15/15;
- historical fixture manifest schema: `PASS`, all 10 entries validated;
- historical source runtime/package checks: `PASS`, `uv pip check`, source bootstrap, and
  the selected production Ruff boundary;
- historical MCP lifecycle hardening: `PASS` in the focused local rerun (85 passed, four
  expected real-ROM skips without supplied assets); CI release-hygiene run
  #47 passed on PR #22;
- historical Windows native runtime checks: `PASS` for a fresh Windows install,
  `pip check`, source and Cython bootstrap contracts, selected runtime/fixture
  checks (23 passed, one unrelated skip), MCP stdio (4/4), and three-ROM
  Cython lifecycle smokes;
- historical Cython/native runtime: `PARTIAL` for the pinned build, semantic serial
  contract, corrected lockstep timing ABI, integrated unit/timing, remote, and
  local/session gates; representative strict trade/battle rows pass. The
  current native/Cython strict gate is the separate `FAIL` result recorded
  above.

The historical full gate used native bit-level serial traffic, ordinary ROM
input, exact party-record and battle-hook assertions, bounded deadlines, and
clean teardown for every declared local and remote row. Retained prior-candidate
evidence establishes the dual unit/timing gate and scoped local
and remote asset tiers. It does not establish a passing post-fix strict-trade
result (the complete source result is `FAIL` at 18/19), passing native or remote full battle acceptance for the current
implementation/runtime snapshot
`a8576b5ecb8e7039eefe0e02865b0bfc031387a7`, source broad-suite completion,
vanilla fixture provenance, platform, real-ROM load, actual MCP live gameplay,
review, or network-security claims.

| Capability | Status | Evidence boundary |
|---|---|---|
| ROM-free unit and timing regressions | `PASS` (prior-candidate unit/timing scope; release remains `PARTIAL`) | The retained prior-candidate dual source/native gate collected 1,094 tests in each runtime, had 949 unit tests pass, and passed timing 50/50 in each of five repetitions. No ROM-backed gameplay ran. |
| Runtime/package identity | `PASS` (retained source/native scope) | The retained prior-candidate gate selected and reported vendored source or installed Cython PyBoy 2.7.0, fork `c565df66c3731fad2856169a90f6bbec99925915`, the packaged entry point, and the bit-accurate serial contract. |
| Canonical color Red/Blue/Yellow fixture evidence | `PASS` (prior-candidate asset and fixture scope; release remains `PARTIAL`) | The prior-candidate checks validated 5/5 ROM hashes, 3/3 symbol hashes, 3/3 fixture hashes, and the 10-entry fixture manifest. Six canonical fixtures have verified provenance; four vanilla-derived fixtures remain `PARTIAL`. Existing vanilla bytes validate, but vanilla ordinary capture provenance cannot be established or reproduced at the implementation/runtime snapshot. |
| Single-session/MCP | `PASS` (pre-fix implementation-snapshot integration/navigation scope; release remains `PARTIAL`) | Six real MCP integration checks against the pre-fix implementation/runtime snapshot passed in 13.11s with one SDL warning, and 104 MCP dispatch tests passed. Ordinary MCP calls reached Red's bedroom (map 38, `(3,7)`), the house exit, Pallet Town (map 0, `(5,5)`), Oak's Lab (map 40), and lab movement. A bounded starter attempt ended at map 40, `(5,3)` with `party.count=0`; no memory/state bypass was used. MCP-facing starter acquisition and trade/battle remain unproven. |
| In-process link acceptance | `PARTIAL` (post-PR #52 full trade matrix unqualified; release remains `PARTIAL`) | The retained pre-PR #52 source strict-trade results are historical (`17/19` in the four-worker run and `18/19` in the serialized run). PR #52 adds targeted Red/Blue startup-role coverage; one isolated Blue-listener → Red-connector trade passed with exchanged party records, while Blue/Blue still failed at the trade-center warp. The pre-PR #52 source-local battle result remains `9/9`, and the native/Cython strict result remains `16/19` trade and `17/19` battle. |
| Remote TCP and MCP lifecycle | `PASS` (pre-fix implementation-snapshot scope plus post-fix focused regression; release remains `PARTIAL`) | The pre-fix source representative TCP Red/Yellow trade and battle passed. Six real MCP integration checks against the pre-fix implementation/runtime snapshot passed in 13.11s with one SDL warning and 104 MCP dispatch tests passed, including listen/connect and EOF cleanup. The post-fix focused network suite passed 38/38 with Ruff and format checks clean. These are scoped transport/MCP/regression results, not a passing current remote battle matrix or MCP-facing trade/battle acceptance. TCP remains loopback-only, unauthenticated, and unencrypted. |
| Strict full trade acceptance | `PARTIAL` (post-PR #52 full matrix unqualified; release remains `PARTIAL`) | No complete post-PR #52 strict-trade matrix has passed. The retained pre-PR #52 serialized source gate executed all 19 rows and passed `18/19`; the earlier four-worker source run passed `17/19`. After PR #52, one isolated Blue-listener → Red-connector real-ROM trade passed with party-record exchange, while Blue/Blue still failed at the Trade Center warp after `984` native edges matched with no backend errors. These targeted results do not replace a complete current-head matrix. |
| Strict full battle acceptance | `PARTIAL` (pre-fix source-local 9/9; native 17/19; release remains `PARTIAL`) | The pre-fix implementation/runtime snapshot passed all nine ordered source-local rows, each reaching Link Battle and the move/damage hooks. The valid pre-fix native/Cython strict gate executed all 19 battle entrypoints but failed at 17/19; its two strict real-ROM failures are detailed above. The post-fix focused 38/38 network result does not qualify a full battle matrix. Current remote battle-matrix qualification remains open; historical rows and LinkMenu milestones do not substitute for it. |
| Synthetic concurrency/lifecycle boundary | `PASS` (historical localhost probe scope) | Eight bounded probes passed in each of five repetitions, including concurrent exchange load, shutdown overlap, receiver races, terminal timeouts, resolver rechecks, BYE ordering, and raw-socket boundaries. Real-ROM load remains unverified. |
| Cython/native serial runtime | `PARTIAL` (pre-fix strict gate plus post-fix focused regression and retained scoped tiers) | The valid pre-fix native/Cython strict gate used Python 3.12.13 and PyBoy 2.7.0 fork `c565df66c3731fad2856169a90f6bbec99925915`, executed all 19 declared entrypoints in each trade and battle tier, and failed at trade 16/19 and battle 17/19. No native owner/IRQ errors occurred; all five failures were strict real-ROM results rather than bypasses. The post-fix focused network suite passed 38/38 with Ruff and format checks clean. The post-fix source strict-trade result is separate source-runtime evidence and does not update this retained pre-fix native/Cython boundary. The retained prior-candidate native unit/timing and asset tiers remain scoped historical evidence. |
| Windows native runtime | `PASS` (historical scoped) | A historical Windows environment passed install, dependency, source/Cython bootstrap, selected runtime/fixture, MCP stdio, and three-ROM lifecycle checks. Full native gameplay, concurrent-load, remote trade/battle, and macOS coverage remain open. |
| Walkthroughs | Diagnostic only | Walkthrough scripts can use state writes or fallback paths and are not release acceptance. |

The historical complete real-ROM snapshot at
`1046a541e0003923aec6000b6b383c6eaafeaa48` is retained as historical evidence
only. The retained product Ruff check is clean for the explicitly configured CI
files; the broad legacy tree still reports pre-existing style violations and is
not silently reformatted. The complete source-runtime gate is the PR #17
baseline; PR #19's focused source-runtime and remote slices are historical
evidence. The retained pre-PR #52 post-fix source strict full-trade result is
`FAIL` at 18/19, with
the sole failure recorded above; the retained native/Cython strict gate remains
pre-fix `FAIL` at 16/19 trade and 17/19 battle, and source-local strict battle
remains pre-fix `PASS` at 9/9. Release status remains `PARTIAL`. Vanilla fixture provenance, broad-suite and
full native-platform coverage, real-ROM load
evidence, MCP live gameplay, independent review, and secure cross-host
networking remain open. TCP is deliberately localhost-only because it has no
authentication or encryption.

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

The historical PR #17 source-runtime strict trade and battle runs each passed
19/19, including all local, dedicated, and remote listener/connector rows. A
separate recorded source-runtime trade gate also passed 19/19 with the
owner-boundary implementation, but it is historical trade-tier evidence rather
than current full-runtime sign-off. The retained prior-candidate remote asset
tier passed source 16/16 in 35.6044s and native 16/16 in 30.8627s; the local
asset tier passed source 47/47 in 531.3477s and native 47/47 in 38.2974s.
These are scoped transport/session results. The retained pre-PR #52 post-fix source-runtime
strict-trade production gate at
`/tmp/poke-harness-source-serial-acceptance-20260904/evidence` completed all 19
trade entrypoints and is `FAIL` at 18/19; its sole trade-center warp failure
and exact evidence hashes are recorded above. The valid pre-fix native/Cython
gate failed at 16/19 trade and 17/19 battle, while source-local strict battle
remains pre-fix `PASS` at 9/9. Current full strict trade acceptance is therefore
`UNQUALIFIED` after PR #52: the retained pre-PR #52 source gate is `FAIL` at
`18/19`, and no complete post-PR #52 matrix has been run. Release status
remains `PARTIAL`; strict battle acceptance remains `PARTIAL`.
Stock-ROM link pairs remain outside the strict canonical matrix because their
fixture provenance is partial.

## Requirements and clean install

Requirements:

- Python 3.12 or newer.
- The bundled PyBoy runtime (`2.7.0`, harness revision
  `c565df66c3731fad2856169a90f6bbec99925915`).
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
python scripts/bootstrap_pyboy.py --mode cython
python scripts/bootstrap_pyboy.py --mode cython --check
python scripts/production_gate.py --runtime-mode cython --unit-only \
  --repeat-timing 5 --evidence-dir "$EVIDENCE_DIR"
```

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

The default release path uses the source-compatible runtime. The pinned Cython
diagnostic build compiles with the checked-in serial ABI, passes its semantic
bit-accuracy probe, and passed real-ROM attach/step/close smokes for canonical
color Red, color Blue, and Yellow. PR #27's explicit `--runtime-mode` gate
verifies all five native PyBoy modules before running tests. The focused
source/native serial-link+network suite and the historical native strict
trade/battle rows listed below are scoped evidence; the current native/Cython
strict gate is recorded above with a `FAIL` outcome, and neither establishes a
passing full-runtime release. Source mode remains the documented release
default.

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

Clients that do not expand `${PWD}` should use the explicit shell launch below
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
- reading parsed game state and the event log as resources.

When a peer session is configured with `POKERED_PEER_*` variables, the
link-related tools are also exposed. The peer is constructed at startup but
is not paired automatically. Evidence from the pre-fix implementation/runtime snapshot
includes six real MCP integration checks in 13.11 seconds with one SDL warning
and 104 MCP dispatch tests. Ordinary MCP calls drove a real Red session from
bedroom map 38 at `(3,7)` through the house exit, Pallet Town map 0 at `(5,5)`,
Oak's Lab map 40, and lab movement. A bounded starter attempt ended at map 40
`(5,3)` with `party.count=0`; no memory/state bypass was used. These results
establish MCP control and observation plus lifecycle behavior, but not starter
acquisition or MCP-facing trade/battle; those remain unproven.

## Link cable modes

The bundled PyBoy fork provides the bit-accurate serial backend required by
Gen I Pokémon. Real sessions use that backend for in-process and TCP links;
the older semantic bridge remains only as a compatibility path for test
doubles that intentionally do not model a PyBoy motherboard. A real PyBoy
session with an incomplete serial contract fails closed instead of silently
switching to semantic exchange.

Native attach only installs the serial backend and its transport callbacks. It
does not write Pokémon HRAM such as `hSerialConnectionStatus` or install
symbol-level exchange hooks: the ROM's own serial ISR must establish the role
from native serial traffic. The semantic bridge is therefore not production
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

### Remote TCP pair

Two independent MCP servers can use `link_listen` and `link_connect`. The
listener starts with the internal-clock role and the connector starts with the
external-clock role. After the versioned HELLO, native PyBoy sessions
negotiate the compatible startup role: for Red/Blue pairs, Red provides the
initial internal clock and Blue waits as the external-clock endpoint; for
Yellow cross-family pairs, the non-Yellow endpoint provides the initial
internal clock. Same-family pairs retain their caller-provided orientation.
This register-level policy does not establish full TCP trade acceptance. The
ROM still owns its connection-status byte and any later role changes. Poll
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
| `tests/test_link_transport.py` and `tests/test_network_backend.py` | In-process queues and TCP edge/response primitives | A real game trade or battle |
| `tests/test_link_symbols_real_roms.py` | Required labels resolve when local symbols are available | A complete gameplay flow |
| `tests/test_link_integration.py` | Fixture-gated in-process real-ROM milestones | Remote two-process behavior |
| `tests/test_link_integration_remote.py` | Fixture-gated remote transport/serial milestones | A full user-driven remote trade or battle |
| `tests/test_pyboy_link_session_subprocess.py` | Parameterized two-process LinkMenu smoke plus canonical color Red/Blue/Yellow native-serial trade and battle acceptance entrypoints | The retained pre-PR #52 strict-trade gates are historical (`17/19` and `18/19`). PR #52 targeted Red/Blue startup roles and test-driver pre-drive coordination; Blue/Blue full trade remains unproven. MCP-facing starter/trade/battle gameplay and a passing current strict battle matrix remain unproven |
| `tests/test_pyboy_link_session_roms.py` | Diagnostic matrix plus parameterized canonical Red/Blue/Yellow local trade and battle acceptance entrypoints | Stock-variant coverage, or a release result from a skipped, RAM-mutated, or unpinned path |

Do not describe a transport milestone as “trade complete.” A full trade or
battle needs an acceptance result from the actual release runtime, matching
ROMs, matching save-state fixtures, bounded deadlines, a clean teardown, and
an explicit statement about any test-driver menu control. The strict
declaration contains 19 trade and 19 battle entrypoints: nine local ordered
rows, one dedicated Red/Yellow assertion, and nine remote ordered listener /
connector rows for each operation. The PR #17 source-runtime baseline passed
all 19 trade and all 19 battle entrypoints. The recorded source trade gate
records 19/19 after the owner-boundary change, but that is historical trade-tier
evidence rather than current full-runtime sign-off. The retained prior-candidate
remote asset tier passed source 16/16 in 35.6044s and native 16/16 in 30.8627s;
the local asset tier passed source 47/47 in 531.3477s and native 47/47 in
38.2974s. The retained pre-PR #52 post-fix source-runtime strict-trade production gate at
`/tmp/poke-harness-source-serial-acceptance-20260904/evidence` completed all 19
trade entrypoints and failed at 18/19; its sole trade-center warp failure and
exact evidence hashes are recorded above. This is pre-PR #52 post-fix source evidence
after `7b4b5b72` (cherry-picked as `7b4b5b7`), not a passing full-runtime sign-off.
The valid native/Cython strict gate remains pre-fix and failed at 16/19 trade
and 17/19 battle, while source-local strict battle remains pre-fix `PASS` at
9/9. Cython gameplay beyond that gate, vanilla fixture provenance,
current remote full-matrix coverage, broad-suite/platform/load/review coverage,
MCP-facing starter/trade/battle gameplay, and secure cross-host networking
remain outside the release result.

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

The pre-PR #35 integrated source-runtime gate recorded 586/586 unit tests and
40/40 timing cases across five repetitions, with 730 tests collected. The same
historical unit/timing scope passed under the explicitly selected Cython
runtime, and its probe reported native extensions for all five PyBoy modules.
After PR #35, the historical focused source/native serial-link+network suite
passed 78/78 in each runtime. The historical source strict trade gate recorded
19/19; the historical native strict trade gate recorded 18/19 because the
Yellow-listener/Blue-Color-connector row remained blocked. Native strict battle
was not qualified. The bounded localhost
concurrency probe is also part of the release-hygiene workflow:

```bash
python scripts/network_concurrency_probe.py
```

It passed 8/8 probes in each of five repetitions on the PR #19 candidate. The
integrated asset-backed candidate's source and Cython remote tier and prior
local/session tier passed 15/15 and 47/47 respectively; those are historical
scoped results, not a substitute for the current native/Cython strict-gate
result. The
retained prior-candidate dual source/native unit/timing gate collected 1,094
tests in each runtime, had 949 unit tests pass, and passed timing 50/50 in each
of five repetitions. Its local asset tier passed source 47/47 in 531.3477s and
native 47/47 in 38.2974s; its remote asset tier passed source 16/16 in 35.6044s
and native 16/16 in 30.8627s. The gate verified 5/5 ROM hashes, 3/3 symbol
hashes, and 3/3 fixture hashes, and the 10-entry fixture manifest passed.
These retained prior-candidate results do not establish current
implementation/runtime strict full trade or battle, MCP-facing
starter/trade/battle gameplay, vanilla fixture provenance,
broad-suite completion, full native-platform coverage, real-ROM load evidence,
security, or independent-review conditions. An earlier environment-specific
585/586 ownership result is superseded.

Use the tiered commands in [`docs/PRODUCTION_RUNBOOK.md`](docs/PRODUCTION_RUNBOOK.md)
when ROMs, symbols, fixtures, or the bundled link runtime are present. The
current matrix declaration is complete, but its collection audit is not
runtime evidence. A green unit suite alone is not a production result; every
required tier must run with no unexpected failures, skips, xfails, or
timeouts.

## License

The harness code is LGPL-3.0-only. No game-derived assets are distributed.
