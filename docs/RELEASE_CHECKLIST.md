# Release checklist

Use this checklist for a proposed `pokered-harness` release. A source pin,
fixture hash, or passing unit test is not a substitute for the real-ROM and
runtime evidence required by the capability being advertised.

## Status semantics

- `PASS` is scoped to one named command or tier. It requires clean collection
  and no failure, error, skip, xfail, or timeout in that scope.
- `PARTIAL` means that controlled evidence exists but one or more release
  conditions remain open.
- `PRODUCTION-READY` requires a clean candidate, all required BYO assets, a
  complete strict acceptance declaration and runtime matrix, retained evidence,
  and no open blocker.

## Current audit snapshot

The current integration candidate is based on
`985d95ee380676e0e61cda2ae154abc18ac84a9d` on
`sync/20260907-poke-harness` plus uncommitted
runtime, packaging, lifecycle, and test changes. It remains a `PARTIAL`
publication candidate; the base commit and this checklist do not certify the
uncommitted worktree.

### Current candidate result

The latest local frame-boundary correction treats an already-completed peer
frame as unavailable for rearming, not as a fatal scheduler fault. It never
clears the peer's LCD latch from a serial callback. Regressions verify no extra
peer instruction, idle-high cable fallback, rejection of unowned callbacks,
and normal resumption on the next owner frame.

After this correction, `python scripts/production_gate.py --unit-only
--repeat-timing 5` passed **948 unit tests and 690 timing executions**, with
zero failures, errors, skips, xfails, or xpasses. The command selected the
shared checkout's source PyBoy and harness under CPython 3.12.3; all 14 asset
hashes matched. Unit/timing durations were 181.2/581.3 seconds. The corresponding
compiled-runtime gate (`--runtime-mode cython`) also passed 948 unit tests and
690 timing executions, with the same zero-failure/skip scope and all 14 hashes
matching; durations were 180.8/473.7 seconds. Its environment report confirmed
compiled PyBoy and serial extensions. Independent focused verification passed
132 tests in each runtime. These gates precede the new checkpoint helper tests.

The preceding real Red/Yellow trade attempt reached both trade rooms, then
failed on the completed-frame guard. The post-correction retry of
`tests/test_pyboy_link_session_roms.py::test_red_yellow_trade_swaps_real_party_records`
hit its external 600-second deadline (exit 124), without a test result or
phase checkpoint. It does not prove that trade succeeds or identify how far
the retry progressed. No strict trade/battle matrix is qualified. The new
inspection-only paired checkpoint helper and exact-frame trade driver pass
18 focused tests in each runtime. A native Red/Yellow one-frame probe captured
both 200546-byte states without changing either state's bytes, frame count,
or physical clock, then detached and closed both sessions. This qualifies
that capture operation, not linked replay or trade completion. The checkpointed
trade investigation is separate from the completed gates above.

The full post-checkpoint compiled-runtime gate passed 966 unit tests and 690
timing executions with no failures or skips. The corresponding source gate
failed: 965 unit tests passed and the old-runtime compatibility subprocess
import exceeded its 30-second deadline; all 690 timing executions passed.
This source failure remains unresolved. Full JSON reports are retained as
`shared-checkpoint-native-gate-20260909.json` and
`shared-checkpoint-source-gate-20260909.json` in the operator evidence directory.

The first checkpointed diagnostic stopped on its own 600-second wall deadline
at pair frame 880, with no cleanup errors. It retained frames 0, 20, 100, 200,
400, and 800; both games reached Trade Center, while only Red had entered
`CableClub_DoBattleOrTrade` at frame 800. Detached inspection showed Yellow's
clock, serial traffic, and map state advancing, so its repeated sampled PC is
not evidence of a freeze. Neither this run nor its snapshots prove a completed
trade. The longer diagnostic leaves acceptance limits unchanged.

The remaining paragraphs retain earlier named snapshots. Their counts and
capability descriptions do not supersede the result above.

The latest integrated runtime includes a monotonic physical-time scheduler
across CPU-speed transitions, precommit owner callbacks, and exclusive native
owner-pump claims with pre-CPU/HALT polling. For attached remote cores, only
the emulator owner samples, shifts, and delivers serial IRQs; transport workers
queue requests and enforce deadlines. No attached-core fallback payload is
fabricated. Fault-safe release preserves the first latched cause. Bootstrap
tests the behavior and rejects stale builds sharing the legacy revision marker.

The latest admission integration rejects stale or claimless attached cores
before emulator-backend writes or legacy promotion and removes the
`PyBoyLinkSession` role-status seeding path; the native constructor's role
keyword is inert. The default native MCP attachment therefore no longer writes
`hSerialConnectionStatus` to select a clock role. Legacy `SerialBridge` and
`RemoteLinkEndpoint` compatibility paths still contain semantic/RAM shortcuts
outside that default MCP path and require a reachability audit; this does not
claim that every public API is RAM-free.

Five local MCP guard tests use injected pairs and reject real/native-capable
endpoints before semantic pairing mutates state. This is local admission
evidence only; it is not a new socket guard or gameplay acceptance.

The pre-owner-post diagnostics source/native gates each passed 925 unit tests and
685 timing executions (137 cases repeated five times), with no failures,
errors, skips, xfails, or xpasses. All 14 asset hashes match. Source used
CPython 3.12.3 (32.286/142.790 seconds for unit/timing); native used CPython
3.12.13 (26.218/98.208 seconds). Retained reports are
`shared-diagnostics-source-gate.json` and `shared-diagnostics-native-gate.json`.
These runs follow a test-only descendant-readiness fix and improved bounded
gate failure diagnostics. An intervening source run failed the descendant
timeout test (919/920 unit, 670/670 timing); its retained report is
`shared-docs-source-gate.json`. The original traceback was not retained, so
the startup race found during review is not a proven cause of that failure.
No serial owner-post candidate changes are included in these passing gates.

The preceding source boot gate passed 920 unit tests and 670 timing executions
(134 cases repeated five times) under CPython 3.12.3 (129.961/479.402 seconds),
with zero failures, errors, skips, xfails, or xpasses and all 14 asset hashes
matched. The current native boot gate passes the same 920/670 scope under
CPython 3.12.13 (67.452/229.558 seconds); the prior source/native stdio 919/670
results remain retained as baselines. Both fresh separate Linux environments used the
exact 43-version dependency constraints, `pip check`, bootstrap, and no caller
`PYTHONPATH`; the gate's internal paths selected the shared harness. The native
gates reused the existing claim-fork wheel rather than making a new native
build. These are source/native gate evidence, not a clean committed-clone,
full trade/battle, intro-completion, or broad gameplay qualification. The
retained gate records are `shared-rom-boot-source-gate.json`,
`shared-rom-boot-native-gate.json`, `shared-stdio-source-gate.json`, and
`shared-stdio-native-gate.json`.

The integrated stdio helper passed four single-session cases in each runtime
with an actual child return code of zero under a 120-second deadline. The shared
stdio suite passed 6/6 in source (35.35 seconds) and native (28.83 seconds);
both Red/Blue remote directions used two child servers that exited zero and
reclaimed their ports. The retained records are
`isolated-stdio-teardown-source.xml`, `isolated-stdio-teardown-native.xml`,
`shared-stdio-teardown-source.xml`, `shared-stdio-teardown-native.xml`, and
`stdio-summary.json`. This is MCP lifecycle/input/save-restore evidence, not a
full trade, battle, or intro-completion qualification.

The integrated five-variant ROM boot scope passed 31/31 in source (116.64
seconds) and native (13.85 seconds), covering `red-stock`, `red-color`,
`blue-stock`, `blue-color`, and `yellow` boot/advance/replay cases plus 26 gate
checks. Each XML property records actual ROM and symbol hashes matching the
pinned values; `replay_sha256` equals `expected_replay_sha256` and is distinct
from `checkpoint_sha256`; and `teardown_status=closed`. This
qualifies only boot/advance/replay/save-load/close for the five inputs; it does
not claim intro completion, trade, battle, or broad gameplay. The owner pre/post
callback API and its cross-runtime regressions are now integrated.
The integrated source gate now passes 936 unit tests and 685 timing executions
(137 cases repeated five times), with zero failures, errors, skips, xfails, or
xpasses and all 14 supplied asset hashes matching. The five source ROM
boot/state/replay checks also pass. The retained gate summary is
`shared-owner-post-source-gate-summary.json` (a transcription of completed
stdout, not the full gate JSON); boot evidence is
`shared-owner-post-source-boot.xml` in the operator evidence directory.
The isolated installed-native gate also passes 936 unit tests and 685 timing
executions with zero failures, errors, skips, xfails, or xpasses; all 14 asset
hashes match. All five native ROM boot/state/replay checks pass, including
verified replay hashes and closed sessions. Native evidence is retained as
`shared-owner-post-native-gate.json` and
`shared-owner-post-native-boot.xml`. These results qualify the scoped Linux
source/native runtime checks, not a clean committed release or gameplay. This
API foundation does not implement authentic clock negotiation. The source and
native records are `shared-rom-boot-source.xml` and
`shared-rom-boot-native.xml`. The full source/native gate reports are
`shared-rom-boot-source-gate.json` and `shared-rom-boot-native-gate.json`.

Post-owner-integration MCP checks passed all four single-session cases and
both Red/Blue listener-role directions in the isolated native environment.
Evidence is `shared-owner-post-native-stdio-single.xml` and
`shared-owner-post-native-stdio-remote.xml`; the latter records four clean
server exits and successful socket reuse. The corresponding full source
file run was **5 passed, 1 failed**: the Blue-listener/Red-connector case
failed its final zero-exit-status assertion after disconnect and socket reuse
succeeded. Its XML is `shared-owner-post-source-stdio.xml`. The original
temporary stderr logs were unavailable on inspection, so the cause and exact
nonzero exit status are not established. A focused repeat passed (1 passed,
5 deselected), with retained logs showing only the SDL warning; that repeat
does not resolve the earlier failure. Source teardown qualification remains
open pending diagnosis and repeated verification.

The prior receiver/owner snapshot passed 906 unit tests and 660 timing
executions (132 cases times five), with 150 focused checks per runtime. Source
used CPython 3.12.3 and native used CPython 3.12.13 with the shared harness
selected through an explicit import path; all 14 inventoried ROM, symbol, and
fixture hashes matched. Those results are historical comparison, not the
latest admission result, and are not a clean committed-clone or gameplay
qualification. Independent review confirmed all 11 files in that tested
owner/guard scope byte-for-byte. Earlier bootstrap timeout evidence remains
retained; the unchanged-bound native recheck passed without establishing its
cause.

The prior focused command was `python -m pytest
tests/test_network_owner_execution.py tests/test_serial_owner_claim.py
tests/test_network_backend.py tests/test_bootstrap_pyboy.py
tests/test_runtime_packaging.py --capture=sys -o addopts= -q` (one shell
command). Operator-retained `shared-owner-focused-source.txt` and
`shared-owner-focused-native.txt` record the historical 150 passes, each with
one SDL2 warning.

The historical mounted-workspace native stdio revalidation failed all six
cases: four could not create
the child process because pytest's sys-capture stream lacks `fileno`; the two
remote cases closed during initialization, with child stderr reporting
`OSError: [Errno 12] Cannot allocate memory` while importing dependencies.
No MCP server process remained and all 14 input hashes still matched. The
retained `shared-owner-native-stdio.xml` records 6 failures in 40.15 seconds.
These failures neither establish a receiver regression nor qualify lifecycle
readiness; capture configuration and the unexplained resource errors must be
resolved before a new bounded run.

With capture disabled, the historical mounted-workspace source stdio
revalidation passed its four
single-session initialization/input/resource/save-replay cases, but both
remote cases failed during initialization (4 passed / 2 failed, 94.54 seconds).
Child stderr again reported dependency-directory `Cannot allocate memory`
errors. `shared-owner-source-stdio.xml` is retained; no MCP server remained
and all 14 input hashes matched afterward. Linux free disk space had fallen
to 39 MB, although an earlier memory check reported 19 GiB available. This
resource observation does not establish the allocation error's cause; further
heavy validation in that mounted environment was paused pending adequate
resources and diagnosis.

Stale-core and claimless admission behavior is integrated. The source/native
gates and scoped stdio lifecycle results described above are current
working-snapshot evidence; they remain separate from clean-clone, fresh-native-
build, full matrix, and gameplay qualification.
Native receiver confinement
and default no-role-RAM attachment do not qualify the separate semantic
compatibility path; its legacy RAM shortcuts still need reachability review.
Version-1 wire frames lack causal
timestamp/epoch/edge/phase grants. The clock-phase/save-v18 experiment remains
isolated and hardware-unqualified; its 290-test source result is not native
qualified. No fresh complete passing strict trade or battle matrix qualifies
the integrated runtime.

### Earlier integration snapshots

The following records predate the latest admission/no-role-RAM integration above.
Their present-tense descriptions apply only to their named snapshots; failures
and diagnostic passes are retained, not promoted to current acceptance.

Scheduler B+C2 and the common emulator-owner boundary are integrated. Raw
providers and managed Sessions share canonical emulator-lock ordering,
execution depth, close exclusion, and provider identity. Closed owners still
allow serialized unpair/detach cleanup. Independent Session step, run-until,
and load operations remain rejected while locally paired. The persistent
elapsed-cycle instruction scheduler is used across phases at fixed normal
CPU speed; unsupported clock changes or completed-frame rearm faults fail
closed instead of selecting a different phase-specific scheduler.

The owner-integration source and native gates on 2026-09-08 each passed 738
unit tests and 190 timing executions (38 cases repeated five times), with
zero failures, errors, skips, xfails, or xpasses. Native imports were verified
against the compiled runtime while selecting the shared harness through an
explicit checkout import path, not a new native installation. The subsequent
network-deadline patch is now integrated and passed 50 focused tests per
runtime. Its full source and native gates each passed 752 unit tests and 260
timing executions (52 cases repeated five times), with zero failures, errors,
skips, xfails, or xpasses. Native selected the shared harness and its compiled
runtime. These post-network gates are distinct from the earlier owner-baseline
results.

The network patch uses nonblocking sockets and one absolute edge-call budget
across lock admission, request-frame writes, and response waiting. Partial
writes are serialized and cancellation-aware. Control-frame writes have a
separate bound; public SYNC/exchange response timeouts still begin after
their frame write, so they are not end-to-end exchange deadlines. This
transport hardening does not repair remote owner-thread or IRQ semantics.

The local and remote runtime guards reject unsupported implicit MCP links
before semantic bridging or socket creation. Local admission rejects remote
ownership, cancelled attachment rolls back under the common locks, and main
shutdown attempts both sessions even when peer cleanup raises. Session close
serializes stop against raw and managed emulator operations, with retryable
cleanup-timeout errors and shared reentrant-close protection. Its deadline
bounds lock acquisition, not the runtime stop implementation itself. These
Python-side ownership guards do not relocate remote worker serial/IRQ
mutation to the emulator owner thread.

The post-network real-ROM stdio suites passed 6/6 in source mode
(99.711 seconds) and 6/6 with the shared harness and native runtime
(21.523 seconds), with zero failures, errors, or skips. Both include stronger
state-replay hash verification and both Red/Blue remote lifecycle directions,
reusable ports, and zero-exit peer teardown. Native used a checkout import
path without changing its environment installation. These are lifecycle
checks, not gameplay or complete remote-teardown qualification.

Initial post-network stdio commands omitted the required asset variables and
skipped all six tests in each runtime. Those configuration errors are not
passes. The corrected results above used explicit pinned asset paths and do
not convert either skipped run into acceptance evidence.

A fresh, asset-free copied owner snapshot passed documented editable source
installation, `pip check`, standalone installed-module origins, source
bootstrap, and the full 738-unit/190-timing gate on CPython 3.11.15. No
caller-supplied `PYTHONPATH` or `PYTHONHOME` was used, and no standalone PyBoy
distribution was installed. All 320 copied file hashes remained unchanged;
all 14 BYO assets were absent. This validates the minimum Python minor for
that Linux/WSL source snapshot, not later network changes, native or Windows
Python 3.11, a clean committed clone, or real-ROM acceptance.

At that earlier owner-integration snapshot, a native canonical local battle
matrix completed **0 passed /
9 failed / 0 skipped / 0 timeouts / 0 unrun** in 976.200483 seconds. All nine
original nodes ran once, without retries, changed assertions, RAM writes,
substitute inputs, or a sequential-controller proxy. Yellow/Yellow failed the
original both-peers-at-LinkMenu precondition after 833.69 seconds; the other
eight rows were rejected during second attachment by the fixed-normal-CPU-speed
guard. All children exited 1 naturally, their known process groups were absent,
and all seven code/runtime plus 14 asset hashes matched afterward. The tests
close both Sessions but do not independently report backend detach state.
A separate read-only source-runtime fixture probe observed Red/Blue
`double_speed=1`, `lcd_speed_shift=1`, `KEY1=128`; Yellow reported zero for all
three. This is fixture-state evidence, not a native guard bypass or repair.
The matrix blocks readiness despite the scoped 752-unit/260-timing gates and
6/6 stdio passes. The native owner-pump and physical-time-counter repair
candidates remain isolated and are not integrated. The earlier sequential
pilot below and historical 4-pass/5-fail matrix do not supersede this failure.

An isolated repaired-owner native pilot passed explicit real-ROM lifecycle
and one unchanged sequential-controller Yellow/Yellow strict battle
diagnostic. The battle took 374.888610 seconds wall/139.421429 seconds CPU;
settled next-turn assertions observed mirrored HP 73/72 versus 72/73,
species 84, status zero, battle-loop counters 2/2, and attack/damage counters
1/1. Both runs exited zero; provider detach and absent process trees were
verified, and all eight code/runtime plus 14 external asset hashes matched.
No new RAM writes, scheduler monkeypatch, retry, or matrix expansion was
introduced. This is an approved sequential-controller diagnostic, not an
unchanged simultaneous-input acceptance row or a complete matrix.

The integrated source runtime now uses 512 T-cycles per normal serial bit
(4096 per byte), repairs stale nonmaster save-state deadline caches, and
latches backend faults until an exception-capable owner boundary raises
`SerialBackendError`. A fault requires new emulator sessions; detach or load
is not recovery. Bootstrap checks actual timing and fault behavior in addition
to module kind, ownership, version and revision markers. The legacy fork
revision marker identifies the base, not a new commit containing these edits.
The earlier timing/fault, bootstrap, Session-guard, and pre-owner gate results
are retained in the historical section below. Their old ownership failures
motivated the integrated common boundary; the new scoped passes do not erase
failed gameplay records or qualify unrestricted concurrency.

The current runtime needs a new committed fork revision and a clean-clone
reproduction; its unchanged legacy marker cannot identify the dirty repair.
The historical failures below remain evidence, not current integration claims.

The historical pre-clock-repair native local nine-pair battle run (128
T-cycles per bit) completed in 562.00 seconds with
four passes, five failures, and no skips. Red/Yellow and Blue/Yellow passed
in both orders. Yellow/Yellow, Blue/Blue, Red/Red, Red/Blue, and Blue/Red
failed through missing exchange/boundary evidence or invalid/nonmatching
combatant state. The exact native Red/Yellow party-record trade row passed
separately in 20.50 seconds. Neither result qualifies the full release matrix.

Remote battle verification now requires same-exchange settled snapshots,
mirrored combatants, and terminal cleanup when applicable. The earlier
335-second remote result below predates this stronger verifier.

A fresh historical role-seeded Red-color listener/Blue-color connector source
row passed the stronger verifier in 455.93 seconds, within its 600-second
deadline. Both peers captured
exchange 1 at the next battle loop with mirrored HP 148/154 and 147/152,
species 154, and status zero. Both exited zero, reported closed, exchanged
6,032 balanced edges, and reported no synthetic keepalive or out-of-band data
exchange. This historical role-seeded row is a settled-turn diagnostic, not
authentic full acceptance of the current no-role-RAM path; off-owner serial
mutation also remained unchanged in that snapshot.
The implemented local fault latch does not qualify swallowed remote-worker
IRQ errors or remote teardown; owner-thread error delivery remains open.

The source real-ROM MCP link dispatch lifecycle smoke also passed 1/1:
listen, connect, status, and disconnect. This is not a stdio link gameplay test.

Before changing any checkbox below, rerun the relevant command against the
exact candidate commit from a clean install using Python `>=3.11`, the pinned
`mcp==1.29.1`, and the bundled PyBoy runtime. Record the command, interpreter,
runtime mode, asset hashes, deadlines, and teardown outcome.

### Historical evidence retained for comparison

These records describe their named snapshots and retain failed evidence;
the [current candidate result](#current-candidate-result) alone defines the
latest integration status. Historical runtime markers are not new fork pins.

An earlier Linux-filesystem source-only run was recorded as 914 unit tests and
670 timing executions with all 14 asset hashes matched; its 120.88/310.20-
second durations are historical, and its temporary report is not retained in
the current evidence directory. A separate console-only 919/670 run selected
source mode under CPython 3.12.13; it is not native evidence and no report
artifact is retained. The prior mounted-workspace 909/914 + 650/670 failures and
the compatibility subprocess's 30-second deadline remain historical; its
90-second diagnostic exited 0 after 42.96 seconds while loading MCP, numpy,
and SDL2 through `importlib`/`_path_stat`, with no harness lock wait. This
records startup-load latency without establishing an OS/filesystem root cause.

After owner integration but before network-deadline hardening, source and
shared-native real-ROM stdio each passed 6/6 in 125.539 and 41.778 seconds,
respectively, with stronger replay hashes, both Red/Blue remote directions,
reusable ports, and zero-exit teardown. This was not a new native install or
link gameplay acceptance.

Before common-owner integration, the source and shared-native gates each
passed 683 unit tests and 50 timing executions. Native module origins were
verified using an explicit checkout import path, not a new clean install.
The earlier affected MCP/close suite passed 154 tests. Native
Red/Blue/Yellow boot/save/load/close smokes passed 3/3 with unchanged assets.
The six-case source stdio suite passed in 132.399 seconds and the
shared-native suite in 36.254 seconds, including stronger replay hashes,
both Red/Blue remote lifecycle directions, reusable ports, and zero-exit
teardown. The preceding source suite passed 5/5 in 101.879 seconds; before
integration, the new replay case passed separately in source (37.57 seconds)
and native (5.72 seconds). The post-test source gate remained 683 unit/50
timing green. These records are superseded for owner integration by the
scoped results above, not converted into gameplay acceptance.

The timing/fault runtime patch passed 115 focused tests and a
644-unit/50-timing gate; bootstrap then passed 30 focused tests and a
657-unit/50-timing gate. The optional Session backend ownership guard passed
112 affected tests before the 683-unit/50-timing gate. All 14 inventoried
asset hashes matched. That runtime source snapshot also passed an isolated
fresh native 644-unit/50-timing gate and source/native three-ROM lifecycle
checks; none was a clean committed-clone or complete gameplay result.

Before scheduler/owner integration, independent probes reproduced a
raw-provider/managed-Session lock-order deadlock, close stopping an emulator
during raw stepping, and a closed-session unpair cleanup failure. The
optional Session guard alone did not solve these paths. The common owner
boundary now has regressions for these failures; the historical probes
remain evidence of the earlier implementation rather than current blockers.

An unintegrated owner-dispatch/native-error prototype passed 73 affected tests
in each runtime. Its no-role-seed native Red-color/Blue-color LinkMenu probe
failed closed in 15.03 seconds: both peers sent an initial master request,
received no response, and exited with errors; neither reached LinkMenu.
Owner dispatch and authentic negotiation remain unqualified. Both peers had
an outgoing request but no owner-handled request; blocked owner progress
versus electrical arbitration was not isolated by this probe. A coherent new
fork pin also remains open. The prototype does not change shared evidence.

An earlier source gate passed 431 unit tests. Timing passed 34/35 executions
from seven cases repeated five times, so the gate failed. Source bootstrap
ownership validation passes. Preflight matched five pinned ROMs, three symbols,
and six required fixtures; the unit-only tiers do not exercise emulation.
After timing synchronization and gate-reporting fixes, the 2026-09-07 source
gate snapshot passed 473/473 unit tests and 35/35 timing executions (seven
cases repeated five times), with zero failures, skips, errors, or xfails.
Dependency checks passed. This qualifies the selected working-snapshot tiers.
The earlier failing timing case identity was not retained. A controlled
injected 50 ms pause reproduced a timing assumption; the synchronization
fix passed 20/20 focused repetitions and an isolated 35/35 timing tier.
Failure-tail reporting now retains the earliest failure for future runs.

Fresh source editable and wheel installations from a copied snapshot each
passed dependency checks, source bootstrap ownership validation, and 73/73
focused bootstrap/runtime/serial tests with CPython 3.12.13 and MCP 1.29.1.
The isolated native build passed dependency and Cython bootstrap checks for
module types, serial API, revision, and RECORD ownership, plus the same 73
focused tests. Native canonical Red-color, Blue-color, and Yellow 120-frame
boot/save/load/step/idempotent-close smokes passed 3/3, with all three ROM,
three symbol, and six fixture hashes unchanged. Clean committed-clone
reproduction remains open.

The later bounded stale-role coordinator fix passed 59 affected checks on
source and unchanged native runtimes. Its integrated source gate passed
478 unit tests and 50 timing executions (10 cases repeated five times), with
no failures or skips. It does not repair the observed Yellow/Yellow party
data loss; the older counts below describe their earlier snapshots.

On 2026-09-07 the expanded real-ROM MCP stdio suite passed 5/5 after
integration in source mode (98.08 seconds); the isolated native snapshot
passed the same 5/5 checks (25.90 seconds). Coverage includes
initialize/list_tools, queued press and step, game-state and link-status
resources, save/load, and remote listen/connect/status/disconnect in both
Red-color/Blue-color directions. Both remote rows verified socket reuse and
zero-exit server teardown with pinned ROM/symbol hashes. The source gate at that
earlier snapshot passed 473 unit tests and 35 timing executions. These results
establish the tested MCP lifecycle, not trade or battle gameplay.

A later five-case real-ROM stdio run passed 5/5 in 71.651 seconds, including
both Red/Blue remote lifecycle directions and zero-exit subprocess cleanup.
The expanded six-case results above add stronger state-replay verification;
neither suite establishes link gameplay acceptance.

A serial-clock candidate, isolated at the time, passed 494 source unit tests
and 35 timing executions, plus 94 focused checks per runtime (92 together and
two partial-save regressions separately). Its native nine-pair battle run
timed out at 600 seconds: Yellow/Yellow, Blue/Blue, Red/Red, and Red/Blue
failed; Blue/Red was incomplete; the four Yellow-cross-version rows were
unrun. There were no completed passes or finalized pytest failure details.
Its source Red/Yellow battle test timed out at 360 seconds without an outcome.
Both gameplay process trees were absent afterward and all 12 BYO asset hashes
were unchanged. At that snapshot the 512-T-cycle period and large-clock
deadline repairs were isolated. They are now integrated as described above;
that does not supersede these gameplay failures or qualify a full matrix.

A separate unintegrated owner-dispatch trace at the old 128-cycle period
showed the receiver advancing 111,588 and 125,400 cycles while the sender
advanced 888 over matched edge spans 17–24 and 25–32. Receiver ROM writes
reset SC mid-byte, supporting a receiver-run-ahead failure diagnosis. The
sender's first-byte trace was not retained. The 21.81-second diagnostic kept
six asset hashes unchanged, but Red required termination after the short
join grace despite both close paths returning. It is not clean-teardown
evidence and does not qualify the shared runtime.

One remote Red-color listener/Blue-color connector trade row passed in
239.8576 seconds with 6,144 balanced edges. An independently reapplied exact
44-byte lead party-record swap check returned no mismatches. A later battle
row using normal DOWN/A menu input passed its hook-based verifier in 335.0
seconds with 5,976 balanced edges, both peers exiting zero and reporting
closed, and zero keepalive bits. This does not prove a settled battle outcome.
At that snapshot, the role-seeded diagnostic still used production role-RAM
seeding, off-owner serial/IRQ mutation, and native callback-error propagation;
those were blockers. The integrated local
latched fault boundary is now implemented; remote worker/IRQ error delivery
and ownership are not qualified by that local repair. The local
`test_red_yellow_battle_turn_is_resolved` run exited 124 at a 360-second
supervisor bound without a result; its 60-second stack sample was in LinkMenu
navigation. The historical native local battle passed 1/1 in
21.14 seconds within a
300-second bound, using normal inputs and mirrored settled state. Complete
current matrices are unqualified. The following older records are historical or scoped
evidence and must not be copied into a current release report.

A separate bounded local-tier run selected 46 broad real-ROM diagnostics and
exceeded a 600-second supervisor bound without a pytest report; it remains
unqualified.

A separate bounded historical role-seeded remote diagnostic completed one
Blue-color↔Blue-color trade
row (`1/1`) with balanced `6,528` serial edges per direction and no unknown
opcodes. Both peers used the same fixture, so it is diagnostic evidence only;
it does not satisfy strict party-swap acceptance or the full matrix.

The complete all-tier source-runtime baseline ran from isolated source head
`df0e7424c87c812a57f257286b0dc00e87c498f4`, whose implementation tree is the
PR #17 parent. Its historical gate invocation and sanitized report are kept
outside this checkout; that older CLI had options not present in the current
`scripts/production_gate.py`.

It returned `PASS`: collection 698, unit 554/554, local real-ROM 47/47,
remote transport/MCP 15/15, strict trade 19/19, strict battle 19/19, and
timing 40/40 in each of five repetitions. All ten external fixture-manifest
entries validated; no selected test skipped, xfailed, failed, errored, or
timed out. The sanitized evidence bundle is retained outside version control
because ROMs, symbols, and save states are external BYO assets.

The current clean asset-free command is:

```bash
python scripts/production_gate.py \
  --repo-root "$PWD" \
  --python "$(command -v python)" \
  --unit-only \
  --repeat-timing 5 \
  --format text
```

Repeat the same command with `--runtime-mode cython` and the separately
bootstrapped Cython interpreter if native qualification is in scope. The
current CLI accepts one interpreter and one runtime mode per invocation; retain
separate reports for separate environments.

At the dated 2026-09-03 separate-interpreter dual-gate check, source (`--python`)
and Cython (`--cython-python`) each collected 771 tests, passed unit 626/626, and
passed timing 50/50 in each of five repetitions. Source reported
`python-source`; Cython reported `cython/native-extension`. The clone
contained no ROM, symbol, or save-state assets, so no ROM-backed tier ran and
the check is not a production sign-off. Both uv-managed environments passed
`uv pip check --python <interpreter>`, and native bootstrap verified the
`pyboy` and `pokered-harness` owners. An earlier environment-specific 585/586
ownership result is superseded for these isolated environments. The host's
bare `python3` lacked `ensurepip` during that historical check; this does not
describe the later successful CPython 3.12.13 snapshot installation checks.

A dated source and Cython packaging/runtime contract passed 27/27 in each
runtime, including native bootstrap ownership and transient metadata cleanup.

After the pinned fork is built with `scripts/bootstrap_pyboy.py --mode cython`,
the optional native path can be checked explicitly. A historical focused source
and Cython serial-link+network scope passed 78/78 in each runtime.
An asset-backed source trade gate recorded 19/19 ordered rows. The historical
native strict-trade gate recorded 18/19; exact-row follow-ups have both passed
and failed, including exact party-record exchange, a party-record mismatch,
and phase stalls, so native strict-trade reliability is unproven. The full
current native battle matrix is not qualified. The source run's supervisor
started before PR #35 was published, so it is historical scoped evidence, not a
current sign-off. That historical acceptance lane exercised 3/9
direct native remote battle rows under a bounded 155-second pair deadline:
`Blue-color↔Blue-color` passed; `Red-color↔Yellow` and
`Yellow↔Red-color` failed. The historical report recorded no bypasses; six rows were unrun, and
the full 19-entrypoint battle set remains unqualified.

Prior integrated source and Cython remote tiers pass 15/15, and the prior
integrated Cython local/session tier passes 47/47. These are scoped follow-ups,
not current full native trade/battle acceptance.

The same collection audit found all nine ordered local pairs, all nine ordered
remote listener/connector pairs, six reversed-role rows, all nine local variant
rows, and 19 strict trade plus 19 strict battle entrypoints. Structural and
declaration checks passed; those collection counts are not gameplay results.

The PR #19 focused transport/MCP slice passed 167/167, the bounded concurrency
probe passed 8/8 in each of five repetitions, and the post-change real-ROM
remote transport slice passed 15/15. The PR #17 complete source-runtime
baseline passed local 47/47, strict trade 19/19, and strict battle 19/19;
those full strict rows were not silently relabeled as a PR #19 rerun.
Historical 18/19 trade and 17/19 battle snapshots are retained only as
historical context.

A historical fresh Windows Python 3.12.10 environment passed editable installation,
`pip check`, source bootstrap, and the post-PR #23 pinned Cython build/check.
The post-PR #23 Cython runtime exposed `PyBoy.mb.serial` and passed direct
attach/tick/close smokes for canonical color Red, color Blue, and Yellow
(3/3). Earlier scoped source/fixture checks passed 23 tests with one unrelated
WSL-worktree skip, and MCP stdio integration passed 4/4. This narrows the
native-platform gap but does not complete strict Cython gameplay, real-ROM
concurrent load, the remote trade/battle matrix, or macOS coverage.

PR #22 adds bounded MCP stdio unpair cleanup and fresh remote lifecycle
generation tracking. Its release-hygiene workflow passed. PR #23 fixes native
lockstep timing; PR #26 fixes native `PyBoy.tick` instance ownership; PR #27
adds explicit runtime selection and raises game-driven remote exchange
timeouts to a bounded 30 seconds. PR #35 recorded remote serial-edge dispatch at
the native instruction-batch boundary in that historical revision; the current
checkout still uses off-owner worker dispatch. The historical native strict-trade
result is 18/19, but exact-row follow-ups have both passed and failed,
including a party-record mismatch and phase stalls, so reliability remains
unproven. Native battle remains unqualified.

The dated prior separate-interpreter dual-gate evidence uses managed Linux
Python 3.12.13 and Pytest 9.1.1: collection 771 in each mode, unit 626/626, and
timing 50/50 in each of five repetitions. The historical native probe/build and
focused 78/78 serial-link+network scope are separate scoped checks. These gates do
not establish ROM-backed gameplay coverage.

The historical post-hardening asset-backed evidence used pinned ROM/SYM/fixtures
and separate source/native interpreters: local 47/47 source/native
(758.1s/38.1s) and remote 16/16 source/native (75.0s/28.8s). This does not
satisfy the strict trade/battle matrix, provenance, security, platform, or
clean-checkout release items.

The pre-PR #27 source broad diagnostic remains incomplete: 418/703 tests
completed before a 5,400-second supervisor bound (386 passed, 20 skipped, 12
failed, and 285 not started). The failed cases were remote TCP timing cases
from before PR #27's 30-second game-exchange timeout; this is not a full-suite
pass.

An earlier asset-free `python -m pytest -q -ra` diagnostic completed 604 passed,
141 expected BYO-asset skips, and one SDL warning after the partial-
initialization destructor guard and before the dual-runtime gate tests were added. This is a completed clean-checkout diagnostic, not a release
gate; ROM-backed trade, battle, and MCP cases were intentionally skipped
because their external assets were absent.

**Release decision: `PARTIAL`.** The canonical color Red, color Blue, and
Yellow fixture bytes have recorded reproduction evidence, the bounded producer
and tracked battle-fixture generator are present, and a historical source trade gate
recorded 19/19. The historical native strict-trade result is 18/19, but
exact-row follow-ups have both passed and failed, including a party-record
mismatch and phase stalls, so native reliability is unproven; native battle
remains unqualified: a historical direct native remote battle lane exercised
only 3/9 rows (one pass and two bounded failures), leaving six unrun and the
full 19-entrypoint set open. Full sign-off still requires reproducible clean-install
evidence for the documented source/native environments, reliable current
native trade and battle matrices, a complete current source battle matrix, a
completed broad suite, vanilla source provenance, full native-platform and
real-ROM load evidence, independent review, and secure cross-host networking.

## Source and artifact hygiene

- [ ] The current candidate is a clean release commit. It is based on
  `7c207bd` on `sync/20260907-poke-harness` plus uncommitted serial,
  bootstrap, lifecycle, gate-accounting, cleanup, and packaging changes; it
  must be committed and rerun before release evidence can be signed off.
- [x] The checkout is source-only: ROMs, symbols, save states, screenshots,
  logs, caches, and virtual environments are not tracked.
- [x] Documentation keeps BYO assets and external evidence outside the source
  tree and uses relative paths or placeholders rather than machine paths.
- [x] Candidate evidence runs use isolated worktrees and record the exact
  runtime, asset hashes, deadlines, and outcome in the README and runbook;
  external evidence remains outside the source tree.

## Runtime and dependency identity

- [x] Python requirement is `>=3.11`.
- [x] A fresh copied owner-integration snapshot passes editable source
  installation, dependency/bootstrap checks, and 738 unit/190 timing checks
  on CPython 3.11.15 without a caller-supplied import-path override. This is
  Linux/WSL source-only evidence; later network changes and clean-clone,
  native, Windows, and gameplay qualification remain separate.
- [x] The distribution bundles source PyBoy `2.7.0` at fork revision
  `c565df66c3731fad2856169a90f6bbec99925915`.
- [x] `mcp==1.29.1` is pinned in `pyproject.toml`.
- [x] The bundled source runtime resolves and exposes the bit-accurate serial
  contract, including integrated timing and latched faults; behavioral bootstrap
  validation passes. See the [current result](#current-candidate-result) and
  [historical failures](#historical-evidence-retained-for-comparison).
- [x] Fresh source editable and wheel environments from a copied snapshot pass
  `pip check`, source bootstrap ownership validation, and 73 focused tests each
  with CPython 3.12.13 and `mcp==1.29.1`. Clean committed-clone reproduction
  remains a separate open release condition.
- [x] The copied snapshot's native build, ownership/API check, dependency check,
  and 73 focused tests pass. Three canonical ROM lifecycle smokes pass with
  before/after ROM, symbol, and fixture hashes unchanged.
- [x] Historical Windows install, bootstrap, MCP, and Cython lifecycle checks
  are retained below as scoped evidence; they do not certify this candidate.
- [ ] The integrated Cython build passes the full real-ROM strict gameplay
  matrix. A historical focused serial-link+network scope passed 78/78. The historical native
  strict-trade result is 18/19, but exact-row follow-ups have both passed and
  failed, including a party-record mismatch and phase stalls, so reliability
  remains unproven. A historical direct native remote battle lane exercised 3/9
  rows under a bounded 155-second pair deadline: one passed and two failed
  reportedly without bypasses; six rows were unrun and the full 19-entrypoint battle
  matrix is open.
- [ ] Full Cython trade/battle acceptance is complete; source mode remains the
  documented release default while strict acceptance is unqualified.

## ROM, symbol, and BYO asset identity

- [x] `VERSIONS.md` records SHA-1 pins for stock/color Red, stock/color Blue,
  Yellow, and the three matching symbol files.
- [ ] The operator attests that every supplied ROM and symbol file is legally
  obtained and matches the documented pin.
- [x] Release commands require explicit `POKERED_ROM_SHA1` and do not use
  `POKERED_SKIP_SHA1=1`.
- [x] The full real-ROM gate preflights five ROMs, three symbols, and six
  canonical ordinary/battle Cable Club fixtures, validating each documented
  SHA-1 pin. It does not validate the operator-managed ten-entry manifest;
  that remains a separate release evidence task.
- [x] The historical PR #17 full gate with those assets recorded exact hashes,
  sizes, deadlines, skips, xfails, failures, errors, and bounded diagnostics in
  a sanitized external evidence bundle; this does not certify the current head.

## Test gates

- [x] Historical post-network source and native working-snapshot gates each
  passed 752 unit tests and 260 timing executions. The preceding
  owner-integration gates each passed 738 unit/190 timing. See the
  [current candidate result](#current-candidate-result) for the current
  source/native PASS and exact runtime scope; these historical
  results are not clean committed-clone evidence.
- [x] Historical scoped unit evidence records 586/586 in integrated source and
  Cython gates (the complete PR #17 baseline passed 554/554).
- [ ] A clean committed clone reproduces the documented installation and
  complete gate. Fresh CPython 3.12.13 source editable and wheel snapshot
  installations passed scoped checks, and a fresh CPython 3.11.15 editable
  owner snapshot passed its full asset-free gate; the earlier
  system-interpreter `ensurepip` failure remains historical context.
- [x] The current Linux-filesystem source boot gate passes 920 unit tests and 670
  timing executions (134 cases repeated five times), with zero failures,
  errors, skips, xfails, or xpasses. Unit took 129.961 seconds and timing
  479.402 seconds under CPython 3.12.3; all 14 asset hashes matched.
- [x] The current explicit `--runtime-mode cython` native boot gate passes 920 unit
  tests and 670 timing executions with zero failures, errors, skips, xfails, or
  xpasses. Unit took 67.452 seconds and timing 229.558 seconds under CPython
  3.12.13; all 14 asset hashes matched. Fresh separate environments used the
  exact 43-version constraints, `pip check`, bootstrap, and no caller
  `PYTHONPATH`; the gate selected internal shared-harness paths. The existing
  claim-fork wheel was reused, so this is not a fresh native build or clean
  committed clone. Current records are `shared-rom-boot-source-gate.json`,
  `shared-rom-boot-native-gate.json`, `shared-stdio-source-gate.json`, and
  `shared-stdio-native-gate.json`.
- [x] Five local MCP guard tests use injected pairs and reject real/native-
  capable endpoints before semantic pairing mutates state. This is local
  admission evidence, not a new socket guard or gameplay acceptance.
- [x] The integrated stdio helper passed four single-session cases in each
  runtime with an actual child return code of zero under a 120-second deadline.
  The shared suite passed 6/6 in source (35.35 seconds) and native (28.83
  seconds); both Red/Blue remote directions used two zero-exit children and
  reclaimed their ports. Records are `isolated-stdio-teardown-source.xml`,
  `isolated-stdio-teardown-native.xml`, `shared-stdio-teardown-source.xml`,
  `shared-stdio-teardown-native.xml`, and `stdio-summary.json`. This remains
  lifecycle evidence, not full trade/battle or intro-completion qualification.
- [x] The integrated five-variant ROM boot scope passed 31/31 in source
  (116.64 seconds) and native (13.85 seconds), covering `red-stock`,
  `red-color`, `blue-stock`, `blue-color`, and `yellow` boot/advance/replay
  cases plus 26 gate checks. XML properties record matching
  pinned ROM/symbol hashes, separate checkpoint and replay SHA-256 values (with
  replay equal to its expected value and distinct from the checkpoint), and
  closed teardown.
  This qualifies only boot/advance/replay/save-load/close for the five inputs;
  it does not establish intro completion, trade, battle, or broad gameplay.
  Records are `shared-rom-boot-source.xml` and `shared-rom-boot-native.xml`.
- [x] Timing tests pass 40/40 in each of five repetitions in the PR #19
  follow-up gate; the post-PR #23 gate also passed 40/40 × 5.
- [ ] A reproducible Ruff command with an explicit production-file boundary is
  clean. This checkout has no release workflow or checked-in path allowlist;
  broad scans include legacy files and are not release evidence.
- [ ] `python -m pytest -q -ra` completes with no unexpected failure, skip,
  xfail, or timeout on the merged head. The pre-PR #27 bounded source
  diagnostic completed 418/703 tests before its 5,400-second supervisor bound:
  386 passed, 20 skipped, 12 failed, and 285 were not started.
- [x] The PR #17 baseline asset-backed local/session tier passes 47/47 for the
  pinned advertised ROM inputs, with no fixture or runtime skips.
- [x] The PR #17 baseline controlled local evidence covers the complete
  canonical strict trade and battle matrix: 9/9 local ordered rows plus the
  dedicated Red/Yellow assertions passed for each operation.
- [x] The PR #19 post-change remote transport/MCP slice passes 15/15; the PR
  #17 baseline strict trade and battle remote rows passed 9/9 each.
- [x] The strict acceptance declaration has an entrypoint for every canonical
  ordered local and remote Red/Blue/Yellow pair (19 trade and 19 battle node
  IDs).
- [x] The PR #17 baseline local and remote strict runtime rows pass in both
  listener/connector directions; strict trade and strict battle each passed
  19/19 with bounded teardown.
- [ ] A clean full strict rerun of the integrated candidate remains open. A
  prior source trade gate recorded 19/19; the historical native strict-trade
  result is 18/19, but exact-row follow-ups have both passed and failed,
  including a party-record mismatch and phase stalls, so reliability remains
  unproven. A historical direct native remote battle lane exercised 3/9 rows
  under a bounded 155-second pair deadline, with one pass and two failures;
  six rows were unrun, and the current full source battle rerun plus the
  complete 19-entrypoint native strict battle matrix remain unqualified.
- [x] The PR #19 bounded localhost concurrency/lifecycle probe passes 8/8 in
  each of five repetitions.
- [ ] Real-ROM concurrent-load stability is complete on the merged runtime.
- [ ] Full native-platform/build coverage and an independent release review
  are complete; the current Windows evidence is scoped and macOS remains
  untested.

## Fixture provenance and generation

- [ ] An operator-managed fixture manifest (kept outside this source tree)
  records every external state entry with sizes, SHA-1/SHA-256 values,
  expected ROM/SYM pins, source-state records, runtime identity, and command
  templates for the current candidate.
- [x] Canonical color-Red, color-Blue, and Yellow ordinary and derived battle
  fixture bytes have deterministic reproduction evidence.
- [ ] `scripts/produce_cable_club_fixture.py` accepts explicit version/variant,
  source, ROM, symbol, and output paths, but it has no built-in hash or timeout
  validation. Operators must validate pins and bound the diagnostic externally
  before retaining a fixture for release evidence.
- [x] `scripts/prepare_battle_cable_club_fixtures.py` is tracked and produces
  immutable derived battle fixtures; acceptance does not prepare party state
  in emulator RAM.
- [ ] Vanilla ordinary source provenance is verified. The retained Red/Blue
  vanilla source states are not proven to match the vanilla ROM, so vanilla
  ordinary and derived battle rows remain `PARTIAL`.
- [ ] The operator validates all ten external manifest entries against the
  supplied fixture files and keeps the external fixture root available to the
  release runner. This repository does not ship a manifest-validator script.
- [ ] A retained evidence bundle includes the exact source-state hashes,
  capture/generation commands, runtime identity, and original-vs-verification
  timestamp distinction.

## MCP and network operation

- [x] MCP launch examples use explicit ROM and symbol paths plus the ROM
  SHA-1 pin; symbol-file hashes remain an operator verification task.
- [x] The bundled runtime is the default path and no machine-local
  `PYTHONPATH` is required by `.mcp.json`.
- [x] The portable `.mcp.json` contract is documented: the client expands
  `${PWD}` (or an equivalent workspace variable), launches the installed
  environment's `python`, and resolves the selected BYO assets from the
  workspace root.
- [x] Five local MCP guard tests use injected pairs and reject real/native-
  capable endpoints before semantic pairing mutates state. This is local
  admission evidence only; it is not a new socket guard or gameplay acceptance.
- [x] MCP stdout remains the JSON-RPC channel; diagnostics are kept off the
  protocol stream.
- [x] TCP binding and connection are restricted to loopback addresses.
- [x] Lifecycle code has controlled pair/listen/connect/disconnect/close
  coverage, but final real-ROM teardown must still be recorded in the full
  evidence bundle.
- [x] The integrated real-ROM stdio helper passed four single-session cases in
  each runtime with an actual child return code of zero under a 120-second
  deadline. The shared suite passed 6/6 in source and native, including both
  Red/Blue remote directions, two zero-exit children per direction, and port
  reuse. Current records are `isolated-stdio-teardown-source.xml`,
  `isolated-stdio-teardown-native.xml`, `shared-stdio-teardown-source.xml`,
  `shared-stdio-teardown-native.xml`, and `stdio-summary.json`. This does not
  certify link gameplay, full trade/battle, intro completion, or a new native
  installation.
- [ ] Cross-host TCP is not enabled without adding authentication and
  encryption; the current transport is unauthenticated and unencrypted.

## Required release commands

Run from a clean checkout with the same interpreter used for installation and
MCP:

```bash
python -m pytest --collect-only -q
python -m pytest -q -ra
python scripts/tcp_link_matrix.py \
  --repo-root "$PWD" \
  --python "$(command -v python)" \
  --modes trade battle \
  --deadline-seconds 210 \
  --outdir "$(mktemp -d)"
python scripts/production_gate.py \
  --repo-root "$PWD" \
  --rom-root "$PWD/rom" \
  --fixture-root "$PWD/tests/fixtures/link" \
  --python "$(command -v python)" \
  --repeat-timing 5 \
  --format text
```

The production-gate command above uses one explicit interpreter and writes its
report to stdout. Repeat it with `--runtime-mode cython` and a separately
bootstrapped Cython interpreter if native qualification is in scope; there is
no dual-runtime switch.

The matrix command executes real subprocess pairs and its manifest is
diagnostic evidence, not a collection-only declaration. The asset-free gate
may return `PASS` without ROMs because `--unit-only` selects only unit and
timing. A full gate must return `PASS` only after assets, strict matrix,
fixture, and all required real-ROM tiers pass. Redirect stdout to a report
outside version control when retaining evidence.

Do not describe a skipped, xfailed, timed-out, synthetic, hook-only,
RAM-mutated, or LinkMenu-only result as a completed trade or battle.
