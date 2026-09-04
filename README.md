# pokered-harness

`pokered-harness` is a memory-first automation harness for Pokémon Red,
Blue, and Yellow. It loads a user-supplied ROM and symbol file, exposes a
typed `Session` API, and can serve that session over MCP.

This repository is a source tree. It does not distribute commercial ROMs,
symbol files, save states, or other ROM-derived artifacts.

## Release status

This repository remains an audited production-readiness candidate, not a
production release. The live target is
[`CompleteDotTech/pokemon`](https://github.com/CompleteDotTech/pokemon). The
current evidence snapshot is for exact commit
`3399407aa04f6e5e496df628442597c03e0adcc6` (2026-09-04), on top of the merged
production-followup implementation.
The current integration candidate adds serial save-state restoration,
native bootstrap ownership checks and build-metadata cleanup, bounded MCP
teardown, fail-closed gate accounting, and safe cleanup for partially
initialized PyBoy objects. The
underlying remote-dispatch implementation is
`54a739be4a5f2d95a924c6f35c2aa695246ddd2f` (PR #35, merged 2026-09-03).
PR #35 moves remote serial-edge dispatch to an explicit native
instruction-batch boundary, outside serial register access. This removes the
reentrant callback path that had made source and compiled PyBoy behavior
diverge during remote linking.
This candidate is a `PARTIAL` publication candidate, not a `PRODUCTION-READY`
release, until the remaining gates and review conditions are closed.

The candidate includes the MCP lifecycle, runtime packaging, in-process
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

The latest complete all-tier source-runtime gate was collected on 2026-09-02
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

The latest merged-head source-runtime gate used managed Linux Python 3.12.13
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

The latest native remote battle acceptance lane exercised 3/9 direct strict
remote rows with a bounded 155-second pair deadline and no bypasses:
`Blue-color↔Blue-color` was `PASS`, while `Red-color↔Yellow` and
`Yellow↔Red-color` were `FAIL`. Six remote rows remain unrun, and the full
19-entrypoint native battle set remains unqualified.

A prior isolated source/Cython unit/timing gate on 2026-09-03 collected 745
tests in each runtime and passed unit 600/600 plus timing 40/40 in each of five
repetitions. It remains historical evidence; the separate-interpreter result
below is the latest verified gate. Fresh uv-managed source and native
environments installed the project, passed `uv pip check`, and the native
bootstrap completed with both `pyboy` and `pokered-harness` owners. The bare
host `python3` could not create a new standard-library virtual environment
because `ensurepip` was unavailable, so that alternate clean-install path
remains open.

The current separate-interpreter dual source/native unit/timing gate at the exact
commit above collected 1,094 tests in each runtime. Each runtime had 949 unit
tests pass and timing pass 50/50 in each of five repetitions. This unit/timing
scope does not establish ROM-backed gameplay, strict trade, or battle.

At the exact commit above, the remote asset tier verified 5/5 ROMs, 3/3
symbols, and 3/3 fixtures. Source remote was `FAIL`, 15/16, with the failure
at `tests/test_pyboy_link_session_subprocess.py::test_subprocess_pair_reaches_link_menu_over_tcp`.
Native remote was `PASS`, 16/16. The local asset tier remains `PENDING`; no
complete local-tier result is claimed.

An earlier `python -m pytest -q -ra` run completed 604 passed,
141 expected BYO-asset skips, and one SDL warning. It is a clean-checkout
diagnostic rather than a release result: the ROM-backed tests were skipped
because this isolated checkout intentionally contains no ROMs, symbols, or
save states.

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

The release status remains `PARTIAL`. The exact-commit unit/timing gate is
scoped and the remote asset tier is mixed (`FAIL` 15/16 in source, `PASS`
16/16 in native); the local asset tier is pending. Strict full trade and battle
acceptance for the current runtime remain unqualified. Fixture provenance,
especially vanilla provenance, concurrent real-ROM load, cross-platform native
qualification, standard-library clean-install verification, MCP live gameplay,
independent review, and authenticated/encrypted cross-host TCP also remain
open. TCP remains loopback-only and unauthenticated/unencrypted.

The pre-PR #27 source-runtime asset-backed broad run is diagnostic and bounded,
not a release sign-off: at the 5,400-second supervisor cutoff it had completed
418 of 703 collected tests (386 passed, 20 skipped, 12 failed, 285 not
started). The 12 failures were remote TCP integration cases ending in
`SerialLinkClosed` after the old 5-second exchange deadline; the recorded
`blue-blue` flow passes with PR #27's 30-second game-exchange deadline. The Red
color fixture now reproduces byte-for-byte and its four previously excluded
remote diagnostic matrices are enabled, but a complete post-fix broad rerun
remains pending.
Vanilla fixture provenance, real-ROM concurrent-load coverage, full native
platform qualification, independent review, and authenticated/encrypted
cross-host TCP remain open.

Status semantics are deliberately scoped:

- `PASS` means that the named command completed with clean collection and no
  failure, error, skip, xfail, or timeout in the selected scope. It is not a
  claim about unselected ROM-backed capabilities.
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

The current candidate’s evidence is:

- complete all-tier source-runtime baseline: `PASS`, collection 698, unit
  554/554, local real-ROM 47/47, remote transport/MCP 15/15, strict trade
  19/19, strict battle 19/19, and timing 40/40 across five repetitions, with
  no skips, xfails, failures, errors, or timeouts;
- pre-PR #35 integrated source-runtime gate: `PASS`, collection 730, unit
  586/586, and timing 40/40 in each of five repetitions;
- pre-PR #35 integrated Cython/native unit-runtime gate: `PASS`, collection
  730, unit 586/586, and timing 40/40 in each of five repetitions; all five
  probed PyBoy modules were native extensions and the serial contract passed;
- exact-commit separate-interpreter source/native dual unit-timing gate:
  `PASS` for the unit/timing scope, collection 1,094 in each runtime, 949 unit
  tests passed, and timing 50/50 in each of five repetitions; ROM-backed
  gameplay was not run;
- exact-commit remote asset tier: source `FAIL`, 15/16, with
  `tests/test_pyboy_link_session_subprocess.py::test_subprocess_pair_reaches_link_menu_over_tcp`
  as the failing test; native `PASS`, 16/16; asset preflight verified 5/5
  ROMs, 3/3 symbols, and 3/3 fixtures;
- exact-commit local asset tier: `PENDING`; no complete local-tier result is
  claimed;
- prior integrated source and Cython remote tier: `PASS`, 15/15 each;
- prior integrated source and Cython local/session tiers: `PASS`, 47/47 each;
- historical exact red-color local variant: `PASS` in source and Cython modes
  after the headless-audio optimization;
- historical source-runtime strict trade gate: `PASS` as a recorded trade-tier
  result, 19/19 ordered local and remote party-swap rows with the external
  hashed assets; its supervisor began before PR #35, so it is not a clean
  post-merge all-tier sign-off;
- historical source and Cython focused serial-link+network suite: `PASS`,
  78/78 in each runtime after the owner-boundary dispatch change;
- prior source and Cython packaging/runtime contract: `PASS`, 27/27
  packaging tests in each runtime, clean editable install, native bootstrap,
  matching package ownership, transient build-metadata cleanup, and `uv pip check`;
- prior MCP lifecycle hardening: `PASS` in the focused 159-test scoped
  suite, including real native local and remote lifecycle smokes; external
  trade fixtures were not part of that suite;
- historical Cython/native strict trade gate: `PARTIAL`, 18/19 in the complete
  matrix; follow-up exact-row retries have both passed and failed, including a
  party-record mismatch and phase stalls, so the row is not reliable
  acceptance evidence for the current runtime;
- historical Cython/native gameplay: `PARTIAL`; earlier representative Blue
  Color↔Yellow and Red Color↔Yellow trade/battle rows passed in scoped runs. The
  latest native remote battle lane exercised 3/9 direct strict rows with a
  bounded 155-second pair deadline and no bypasses: Blue-color↔Blue-color
  was `PASS`, while Red-color↔Yellow and Yellow↔Red-color were `FAIL`; six
  remote rows remain unrun and the full
  19-entrypoint native battle set remains unqualified;
- pre-PR #27 source-runtime broad diagnostic: `PARTIAL`, 418/703 tests
  completed before the 5,400-second bound (386 passed, 20 skipped, 12 failed,
  285 not started); no full-suite pass is claimed;
- post-PR #19 focused transport/MCP slice: `PASS`, 167/167;
- post-PR #19 bounded concurrency probe: `PASS`, 8/8 in each of five
  repetitions;
- post-PR #19 real-ROM remote transport slice: `PASS`, 15/15;
- fixture manifest schema: `PASS`, all 10 entries validated;
- source runtime/package checks: `PASS`, `uv pip check`, source bootstrap, and
  the selected production Ruff boundary;
- MCP lifecycle hardening: `PASS` in the focused local rerun (85 passed, four
  expected real-ROM skips without supplied assets); CI release-hygiene run
  #47 passed on PR #22;
- Windows native runtime checks: `PASS` for a fresh Windows install,
  `pip check`, source and Cython bootstrap contracts, selected runtime/fixture
  checks (23 passed, one unrelated skip), MCP stdio (4/4), and three-ROM
  Cython lifecycle smokes;
- Cython/native runtime: `PARTIAL` for the pinned build, semantic serial
  contract, corrected lockstep timing ABI, integrated unit/timing, remote, and
  local/session gates; representative strict trade/battle rows pass, but the
  full native strict matrices remain unqualified.

The historical full gate used native bit-level serial traffic, ordinary ROM
input, exact party-record and battle-hook assertions, bounded deadlines, and
clean teardown for every declared local and remote row. Exact-commit evidence
establishes the dual unit/timing gate and the scoped remote asset tier, while
the local asset tier is pending. It does not establish strict full trade or
battle acceptance for the current runtime, source broad-suite completion,
vanilla fixture provenance, platform, real-ROM load, MCP live gameplay, review,
or network-security claims.

| Capability | Status | Evidence boundary |
|---|---|---|
| ROM-free unit and timing regressions | `PASS` (unit/timing scoped; release remains `PARTIAL`) | The exact-commit dual source/native gate collected 1,094 tests in each runtime, had 949 unit tests pass, and passed timing 50/50 in each of five repetitions. No ROM-backed gameplay ran. |
| Runtime/package identity | `PASS` (source/native scoped) | The gate selects and reports vendored source or installed Cython PyBoy 2.7.0, fork `c565df66c3731fad2856169a90f6bbec99925915`, the packaged entry point, and the bit-accurate serial contract. |
| Canonical color Red/Blue/Yellow fixture evidence | `PASS` (asset preflight scoped; release remains `PARTIAL`) | At the exact commit, the remote asset tier verified 5/5 ROMs, 3/3 symbols, and 3/3 fixtures. This verifies supplied asset identity only; strict gameplay and vanilla provenance remain open. |
| Single-session/MCP | `PENDING` (current asset scope; release remains `PARTIAL`) | Historical PR #17 source-runtime local/session evidence passed 47/47 and Windows MCP stdio passed 4/4. No complete current local asset-tier result is claimed, and MCP lifecycle evidence does not establish live gameplay. |
| In-process link acceptance | `PENDING` (current local asset scope; release remains `PARTIAL`) | Historical source/Cython local/session evidence passed 47/47 and the historical focused serial-link+network suite passed 78/78 in each runtime. The current local asset tier remains pending; stock-ROM link support remains unclaimed. |
| Remote TCP and MCP lifecycle | `PARTIAL` (current asset scope; release remains `PARTIAL`) | At the exact commit, source remote was `FAIL` 15/16 because `tests/test_pyboy_link_session_subprocess.py::test_subprocess_pair_reaches_link_menu_over_tcp` failed; native remote was `PASS` 16/16. This is scoped remote transport/MCP evidence, not strict full trade or battle acceptance. TCP remains loopback-only and unauthenticated/unencrypted. |
| Synthetic concurrency/lifecycle boundary | `PASS` (localhost probe scope) | Eight bounded probes passed in each of five repetitions, including concurrent exchange load, shutdown overlap, receiver races, terminal timeouts, resolver rechecks, BYE ordering, and raw-socket boundaries. Real-ROM load remains unverified. |
| Cython/native serial runtime | `PARTIAL` (current scoped tiers) | The exact-commit native unit/timing gate collected 1,094 tests, had 949 unit tests pass, and passed timing 50/50 × 5. Historical native strict trade and battle rows are not a current full-runtime qualification; strict full trade and battle remain unqualified. |
| Windows native runtime | `PASS` (scoped) | A fresh Windows environment passed install, dependency, source/Cython bootstrap, selected runtime/fixture, MCP stdio, and three-ROM lifecycle checks. Full native gameplay, concurrent-load, remote trade/battle, and macOS coverage remain open. |
| Walkthroughs | Diagnostic only | Walkthrough scripts can use state writes or fallback paths and are not release acceptance. |

The historical complete real-ROM snapshot at
`1046a541e0003923aec6000b6b383c6eaafeaa48` is retained as historical evidence
only. The current product Ruff check is clean for the explicitly configured CI
files; the broad legacy tree still reports pre-existing style violations and is
not silently reformatted. The complete source-runtime gate is the PR #17
baseline; PR #19's focused source-runtime and remote slices are historical
evidence. Current strict full trade/battle acceptance, vanilla fixture
provenance, broad-suite and full native-platform coverage, real-ROM load
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
than current full-runtime sign-off. The current exact-commit remote asset tier
is scoped transport/MCP evidence, and the local asset tier is pending. Strict
full trade and battle acceptance for the current runtime remain unqualified.
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
environment. The exact-commit separate-interpreter dual gate collected 1,094
tests in each source and native runtime, had 949 unit tests pass, and passed
timing 50/50 in each of five repetitions. This unit/timing scope does not
establish ROM gameplay.

The default release path uses the source-compatible runtime. The pinned Cython
diagnostic build compiles with the checked-in serial ABI, passes its semantic
bit-accuracy probe, and passed real-ROM attach/step/close smokes for canonical
color Red, color Blue, and Yellow. PR #27's explicit `--runtime-mode` gate
verifies all five native PyBoy modules before running tests. The focused
source/native serial-link+network suite and the native strict trade/battle rows
listed below are historical scoped evidence; they do not qualify the current
full runtime. Source mode remains the documented release default.

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
contains ten state entries (ordinary and battle, including the two vanilla
rows), so `validate_fixture_manifest.py --fixture-root ...` requires all ten
files. The currently controlled gameplay scope uses the six canonical
color-Red, color-Blue, and Yellow states; vanilla rows remain `PARTIAL`
because their source-state provenance is not established.

The symbol files should be generated with `DEBUG=1` from the matching
[pret/pokered](https://github.com/pret/pokered) or
[pret/pokeyellow](https://github.com/pret/pokeyellow) source tree. A generic
build sequence is:

```bash
git clone https://github.com/pret/pokered.git
cd pokered
make DEBUG=1
make clean && make blue DEBUG=1
cd ..

git clone https://github.com/pret/pokeyellow.git
cd pokeyellow
make DEBUG=1
cd ..
```

Use the source commits and RGBDS toolchain recorded in [`VERSIONS.md`](VERSIONS.md)
when reproducing the audited symbols, then copy only the matching `.sym` files
into `rom/<version>/`. The repository records audited provenance but does not
contain the generated symbols or enforce a source-build lock.

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
is not paired automatically.

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
serial coordinator for real PyBoy sessions. The complete PR #17 source-runtime
baseline recorded 47/47 local tests and 19/19 strict trade and battle rows
against the pinned external assets. PR #19's post-change verification reran
the focused transport/lifecycle slice and the remote transport slice; it did
not silently present the baseline local gameplay rows as a full rerun.
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
negotiate the compatible startup role for a cross-family pair: the Red/Blue
endpoint provides the initial internal clock and Yellow waits as the external
endpoint. Same-family pairs and Red/Blue pairs retain the listener/connector
defaults. The ROM still owns its connection-status byte and any later role
changes. Poll `link_status` until it reports `remote_mode` as `connected`, then
call `link_disconnect` at teardown.

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
| `tests/test_pyboy_link_session_subprocess.py` | Parameterized two-process LinkMenu smoke plus canonical color Red/Blue/Yellow native-serial trade and battle acceptance entrypoints | At the exact commit, the remote asset tier is source `FAIL` 15/16 because `test_subprocess_pair_reaches_link_menu_over_tcp` failed and native `PASS` 16/16; strict full trade and battle remain unqualified |
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
evidence rather than current full-runtime sign-off. The exact-commit remote
asset tier is source `FAIL` 15/16 and native `PASS` 16/16; the local asset tier
is pending. Current strict full trade and battle acceptance, Cython gameplay,
vanilla fixture provenance, broad-suite/platform/load/review coverage, MCP live
gameplay, and secure cross-host networking remain outside the release result.

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
indefinitely. There is no separate Yellow-specific producer. A successful
producer run proves only the Cable Club map/position, not a trade or battle.

The tracked
[`scripts/prepare_battle_cable_club_fixtures.py`](scripts/prepare_battle_cable_club_fixtures.py)
creates separate immutable battle-start states from ordinary fixtures. It
validates the selected ROM, creates a legal multi-mon party, and the acceptance
runner loads the result without mutating party state at runtime. This is a
deterministic derived-fixture step, not evidence that a human captured a battle
state. Vanilla derived rows remain partial until their ordinary source state is
proven to match the vanilla ROM. The exact hashes and provenance statuses are
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
scoped results, not a substitute for the current native strict matrix. The
exact-commit dual source/native unit/timing gate collected 1,094 tests in each
runtime, had 949 unit tests pass, and passed timing 50/50 in each of five
repetitions. The exact-commit remote asset tier verified 5/5 ROMs, 3/3
symbols, and 3/3 fixtures; source was `FAIL` 15/16 at
`tests/test_pyboy_link_session_subprocess.py::test_subprocess_pair_reaches_link_menu_over_tcp`,
while native was `PASS` 16/16. The local asset tier remains pending. These
results do not establish strict full trade or battle, MCP live gameplay,
fixture provenance, broad-suite completion, full native-platform coverage,
real-ROM load evidence, security, or independent-review conditions. An
earlier environment-specific 585/586 ownership result is superseded.

Use the tiered commands in [`docs/PRODUCTION_RUNBOOK.md`](docs/PRODUCTION_RUNBOOK.md)
when ROMs, symbols, fixtures, or the bundled link runtime are present. The
current matrix declaration is complete, but its collection audit is not
runtime evidence. A green unit suite alone is not a production result; every
required tier must run with no unexpected failures, skips, xfails, or
timeouts.

## License

The harness code is LGPL-3.0-only. No game-derived assets are distributed.
