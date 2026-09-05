# pokered-harness

`pokered-harness` is a memory-first automation harness for Pokémon Red,
Blue, and Yellow. It loads a user-supplied ROM and symbol file, exposes a
typed `Session` API, and can serve that session over MCP.

This repository is a source tree. It does not distribute commercial ROMs,
symbol files, save states, or other ROM-derived artifacts.

## Release status

**Status: `PARTIAL` — experimental branch, not production-ready.**

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
remains pending. The current dual gate (`pokemon-trade-dual-f458fc7-20260905`)
and older full dual-runtime gate are still running at this snapshot. Actual full
trade remains unverified; no terminal gate outcome is claimed.

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
unit/timing snapshot above or current experimental gameplay acceptance.

| Capability | Recorded result | Evidence boundary |
|---|---|---|
| Source unit/timing | `PASS` — 980/980 unit and 55/55 timing cases | Recorded source runtime, Python 3.12.13, PyBoy 2.7.0 fork `c565df66c3731fad2856169a90f6bbec99925915`; 1,132 tests collected and all five ROM/SYM pins validated. |
| Strict trade | `FAIL` — 18/19 | Recorded source runtime; all 19 rows declared and executed with `workers=4`. The sole failure is `red_color-listen-blue_color-connect` at the Trade Center warp after 722.6s; 18 other real-ROM local/TCP rows passed. |
| Strict battle | `PASS` — 19/19 | Recorded source runtime; all 19 local/TCP real-ROM rows passed in 1,237.8s with no skips, errors, or test-only protocol bypasses. |
| Native/Cython build | `PASS` — wheel built | Build evidence only. The separate full native gate at `f4fddfc` failed remote, trade, and battle; compiled gameplay remains unqualified. |
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
| `tests/test_pyboy_link_session_subprocess.py` | Parameterized two-process LinkMenu smoke plus canonical color Red/Blue/Yellow native-serial trade and battle acceptance entrypoints | A full source release, compiled gameplay parity, or MCP-facing starter/trade/battle gameplay; recorded source strict battle passed 19/19, while source strict trade failed 18/19 |
| `tests/test_pyboy_link_session_roms.py` | Diagnostic matrix plus parameterized canonical Red/Blue/Yellow local trade and battle acceptance entrypoints | Stock-variant coverage, or a release result from a skipped, RAM-mutated, or unpinned path |

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
