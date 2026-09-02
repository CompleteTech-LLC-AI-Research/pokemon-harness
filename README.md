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
current audited repository head is `8727779` (PR #28 documentation merge), and
the latest implementation commit is `b2a4016` (PR #27, merge `b96ee77`), built on
the native Cython ABI fix in `94f103e` (PR #26), MCP lifecycle hardening in
`7f3c2f4` (PR #22), and the earlier runtime candidate in `dfc0b2a` (PR #19).
It includes the MCP lifecycle, runtime packaging, in-process serial/lifecycle,
remote TCP follow-ups from PRs #12-#17, concurrency/lifecycle hardening from
PR #19, bounded MCP shutdown and remote-generation hardening from PR #22,
native Cython tick/timing compatibility from PR #26, explicit source/Cython
gate selection, and bounded remote game-exchange pacing from PR #27.

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
timeouts. The runtime probe resolved the vendored PyBoy 2.7.0 source modules
and the pinned bit-accurate serial contract. ROM-backed tiers were not run in
this asset-free command.

A fresh `python -m pytest -q -ra` run at the current head completed 566 passed,
140 expected BYO-asset skips, and 2 warnings. It is a clean-checkout
diagnostic rather than a release result: the ROM-backed tests were skipped
because this isolated checkout intentionally contains no ROMs, symbols, or
save states.

Fresh Windows validation on 2026-09-02 used Windows Python 3.12.10 and new
virtual environments in isolated checkouts. The editable install, `pip check`,
source bootstrap, and the post-PR #23 pinned Cython build/check passed. The
post-PR #23 Cython runtime exposed `PyBoy.mb.serial` and passed direct
attach/tick/close smokes for canonical color Red, color Blue, and Yellow
(3/3). Earlier scoped source/MCP checks passed 23 tests with one unrelated
WSL-worktree skip and MCP stdio 4/4. This is scoped Windows execution
evidence, not full native qualification: strict Cython gameplay, real-ROM
concurrent load, the remote trade/battle matrix, and macOS coverage remain
open.

The release status remains `PARTIAL`, despite the source-runtime gate and the
native runtime contract gate passing. PR #26 makes per-instance native
`PyBoy.tick` ownership writable; PR #27 makes the gate select the requested
runtime instead of accidentally shadowing Cython extensions with vendored
source. The merged-head Cython unit/timing gate passes. Recorded post-PR #27
source and Cython `blue-blue` remote LinkMenu smokes pass. The post-PR #27 native strict trade
qualification run returned `FAIL`: 16/19 rows passed and three color-variant
subprocess rows failed in the trade-center/rendezvous path. The native strict
battle rerun has not been run. These results keep the release at `PARTIAL`.

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

The current candidate’s evidence is:

- complete all-tier source-runtime baseline: `PASS`, collection 698, unit
  554/554, local real-ROM 47/47, remote transport/MCP 15/15, strict trade
  19/19, strict battle 19/19, and timing 40/40 across five repetitions, with
  no skips, xfails, failures, errors, or timeouts;
- merged-head source-runtime gate: `PASS`, collection 706, unit 562/562, and
  timing 40/40 in each of five repetitions;
- merged-head Cython/native unit-runtime gate: `PASS`, collection 706, unit
  562/562, and timing 40/40 in each of five repetitions; all five probed PyBoy
  modules were native extensions and the serial contract passed;
- recorded post-PR #27 remote gameplay smoke: `PASS` for the real-ROM
  `blue-blue` LinkMenu flow in both source and Cython modes;
- post-PR #27 native strict trade qualification: `FAIL`, 16/19 rows passed and
  three color-variant subprocess rows failed in the trade-center/rendezvous
  path;
  the native strict battle rerun was not run;
- pre-PR #27 source-runtime broad diagnostic: `PARTIAL`, 418/703 tests
  completed before the 5,400-second bound (386 passed, 20 skipped, 12 failed,
  285 not started); no full-suite pass is claimed;
- post-PR #19 focused transport/MCP slice: `PASS`, 167/167;
- post-PR #19 bounded concurrency probe: `PASS`, 8/8 in each of five
  repetitions;
- post-PR #19 real-ROM remote transport slice: `PASS`, 15/15;
- fixture manifest schema: `PASS`, all 10 entries validated;
- source runtime/package checks: `PASS`, `pip check`, source bootstrap, and
  the selected production Ruff boundary;
- MCP lifecycle hardening: `PASS` in the focused local rerun (85 passed, four
  expected real-ROM skips without supplied assets); CI release-hygiene run
  #47 passed on PR #22;
- Windows native runtime checks: `PASS` for a fresh Windows install,
  `pip check`, source and Cython bootstrap contracts, selected runtime/fixture
  checks (23 passed, one unrelated skip), MCP stdio (4/4), and three-ROM
  Cython lifecycle smokes;
- Cython/native runtime: `PARTIAL` for the pinned build, semantic serial
  contract, corrected lockstep timing ABI, native unit/timing gate, and
  representative real-ROM remote smoke; native strict trade is `FAIL` at
  16/19 and native strict battle is not yet run.

The historical full gate used native bit-level serial traffic, ordinary ROM
input, exact party-record and battle-hook assertions, bounded deadlines, and
clean teardown for every declared local and remote row. Current PR #27
evidence establishes the source/native runtime contracts and representative
remote behavior; it does not establish the open full Cython strict matrix,
source broad-suite completion, fixture provenance, platform, real-ROM load,
review, or network-security claims.

| Capability | Status | Evidence boundary |
|---|---|---|
| ROM-free unit and timing regressions | `PASS` (merged-head scoped) | Source and Cython gates collected 706 tests and passed unit 562/562 plus timing 40/40 across five repetitions under managed Python 3.12.13. |
| Runtime/package identity | `PASS` (source/native scoped) | The gate selects and reports vendored source or installed Cython PyBoy 2.7.0, fork `c565df66c3731fad2856169a90f6bbec99925915`, the packaged entry point, and the bit-accurate serial contract. |
| Canonical color Red/Blue/Yellow fixture evidence | `PASS` for recorded byte reproduction; release remains `PARTIAL` | The external manifest records verified ordinary and derived battle fixture bytes for color Red, color Blue, and Yellow. The Red color fixture is now enabled for remote diagnostics. States remain BYO and untracked; hashes do not replace gameplay acceptance. |
| Single-session/MCP | `PASS` (baseline scope) | The PR #17 source-runtime baseline asset-backed local/session tier passed 47/47; PR #19 did not rerun the full local matrix. |
| In-process link acceptance | `PASS` (baseline scope) | The PR #17 source-runtime baseline passed 47/47 local rows and the declared strict local rows; PR #19 adds focused lifecycle coverage. Stock-ROM link support remains unclaimed. |
| Remote TCP and MCP lifecycle | `PARTIAL` (representative gameplay scope) | The PR #19 real-ROM remote transport slice passed 15/15 and the focused transport/MCP suite passed 167/167. Recorded PR #27 source and Cython `blue-blue` LinkMenu smokes pass with a bounded 30-second game-exchange timeout; full post-fix remote trade/battle coverage is pending. TCP remains loopback-only and unauthenticated/unencrypted. |
| Synthetic concurrency/lifecycle boundary | `PASS` (localhost probe scope) | Eight bounded probes passed in each of five repetitions, including concurrent exchange load, shutdown overlap, receiver races, terminal timeouts, resolver rechecks, BYE ordering, and raw-socket boundaries. Real-ROM load remains unverified. |
| Cython/native serial runtime | `PARTIAL` (contract/representative gameplay scope) | PR #26 fixes native `PyBoy.tick` ownership and PR #27 verifies the installed extension modules, 562/562 unit, 40/40 × 5 timing, and a real-ROM `blue-blue` remote smoke. The post-PR #27 native strict trade run is `FAIL` at 16/19, with three color-variant subprocess failures; native strict battle has not been run. |
| Windows native runtime | `PASS` (scoped) | A fresh Windows environment passed install, dependency, source/Cython bootstrap, selected runtime/fixture, MCP stdio, and three-ROM lifecycle checks. Full native gameplay, concurrent-load, remote trade/battle, and macOS coverage remain open. |
| Walkthroughs | Diagnostic only | Walkthrough scripts can use state writes or fallback paths and are not release acceptance. |

The historical complete real-ROM snapshot at
`1046a541e0003923aec6000b6b383c6eaafeaa48` is retained as historical evidence
only. The current product Ruff check is clean for the explicitly configured CI
files; the broad legacy tree still reports pre-existing style violations and is
not silently reformatted. The complete source-runtime gate is the PR #17
baseline; PR #19's focused source-runtime and remote slices are green, while
full Cython gameplay, vanilla fixture provenance, broad-suite and full
native-platform coverage, real-ROM load evidence, independent review, and
secure cross-host networking remain open. TCP is deliberately localhost-only
because it has no authentication or encryption.

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

The complete source-runtime strict trade and battle runs each passed 19/19 in
the historical PR #17 baseline, including all local, dedicated, and remote
listener/connector rows. PR #27's native strict trade qualification passed
16/19 and failed three color-variant subprocess rows in the trade-center/
rendezvous path; its native battle
qualification has not been run, and a complete post-fix source rerun is also
open. Stock-ROM link pairs remain outside the strict canonical matrix because
their fixture provenance is partial.

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

The default release path uses the source-compatible runtime. The pinned Cython
diagnostic build compiles with the checked-in serial ABI, passes its semantic
bit-accuracy probe, and passed real-ROM attach/step/close smokes for canonical
color Red, color Blue, and Yellow. PR #26 makes native `PyBoy.tick` instance
ownership writable; PR #27's explicit `--runtime-mode` gate verifies all five
native PyBoy modules before running tests. The native unit/timing gate and a
recorded real-ROM `blue-blue` remote LinkMenu smoke pass. The post-PR #27 native strict
trade qualification is `FAIL` at 16/19, with three color-variant subprocess
failures; native strict battle has not been run. Source mode remains the
documented release default.

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
| `tests/test_pyboy_link_session_subprocess.py` | Parameterized two-process LinkMenu smoke plus canonical color Red/Blue/Yellow native-serial trade and battle acceptance entrypoints | Concurrent-load stability; the PR #17 source-runtime strict remote trade and battle matrices passed 9/9 each, while PR #19's post-change remote slice was the 15-row transport tier |
| `tests/test_pyboy_link_session_roms.py` | Diagnostic matrix plus parameterized canonical Red/Blue/Yellow local trade and battle acceptance entrypoints | Stock-variant coverage, or a release result from a skipped, RAM-mutated, or unpinned path |

Do not describe a transport milestone as “trade complete.” A full trade or
battle needs an acceptance result from the actual release runtime, matching
ROMs, matching save-state fixtures, bounded deadlines, a clean teardown, and
an explicit statement about any test-driver menu control. The current strict
declaration contains 19 trade and 19 battle entrypoints: nine local ordered
rows, one dedicated Red/Yellow assertion, and nine remote ordered listener /
connector rows for each operation. The PR #17 source-runtime baseline passed
all 19 trade and all 19 battle entrypoints. PR #19's post-change evidence is
limited to the explicitly listed follow-up slices; Cython gameplay, vanilla
fixture provenance, broad-suite/platform/load/review coverage, and secure
cross-host networking remain outside the release result.

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

The merged-head source-runtime gate passes 562/562 unit tests and 40/40 timing
cases across five repetitions, with 706 tests collected. The same unit/timing
scope passes under the explicitly selected Cython runtime, and its probe
reports native extensions for all five PyBoy modules. The bounded localhost
concurrency probe is also part of the release-hygiene workflow:

```bash
python scripts/network_concurrency_probe.py
```

It passed 8/8 probes in each of five repetitions on the PR #19 candidate. The
complete PR #17 asset-backed baseline additionally passed local 47/47, remote
15/15, strict trade 19/19, and strict battle 19/19; PR #19's post-change
real-ROM remote transport slice passed 15/15. PR #27 additionally passes one
real-ROM remote LinkMenu smoke in both source and Cython modes. The result is
still not a production sign-off because native strict trade is `FAIL` at 16/19,
native strict battle and the post-fix source rerun are unrun, fixture
provenance, broad-suite completion, full native-platform coverage, real-ROM
load evidence, security, and independent-review conditions remain open.

Use the tiered commands in [`docs/PRODUCTION_RUNBOOK.md`](docs/PRODUCTION_RUNBOOK.md)
when ROMs, symbols, fixtures, or the bundled link runtime are present. The
current matrix declaration is complete, but its collection audit is not
runtime evidence. A green unit suite alone is not a production result; every
required tier must run with no unexpected failures, skips, xfails, or
timeouts.

## License

The harness code is LGPL-3.0-only. No game-derived assets are distributed.
