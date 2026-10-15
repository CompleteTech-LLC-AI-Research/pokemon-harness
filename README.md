# pokered-harness

`pokered-harness` is a memory-first automation harness for Pokémon Red,
Blue, and Yellow. It loads a user-supplied ROM and symbol file, exposes a
typed `Session` API, and can serve that session over MCP.

This repository is a source tree. It does not distribute commercial ROMs,
symbol files, save states, or other ROM-derived artifacts.

## Release status

The release status is `PARTIAL`; this repository is not production-certified.
The audited checkout is on `sync/20260907-poke-harness` at `7c207bd`
with uncommitted runtime, test, and packaging changes. An
uncommitted worktree is not a release artifact.

The [release checklist](docs/RELEASE_CHECKLIST.md) is the authority for the
latest integrated source/native validation and remaining blockers. Dated
results are kept separately from the latest working-snapshot gate.
MCP runtime admission, mode exclusion, locking, and cleanup checks establish
their tested lifecycle boundaries, not complete trade or battle acceptance.

The working snapshot integrates 512-T-cycle serial timing, latched backend
faults, physical-time scheduling across CPU-speed transitions, and common
emulator ownership. Native remote receive samples, shifts, and delivers IRQs
on the emulator owner, using exclusive pump claims and pre-CPU/HALT polling.
The latest admission integration rejects stale or claimless attached cores
before emulator-backend writes or legacy promotion and removes the
`PyBoyLinkSession` role-status seeding path; the native constructor's role
keyword is inert. Bootstrap checks the actual claim/poll behavior, not just the
revision marker.

The default native MCP attachment no longer selects a clock role by writing
`hSerialConnectionStatus` into game RAM. Legacy `SerialBridge` and
`RemoteLinkEndpoint` compatibility paths still contain semantic/RAM shortcuts
outside that default MCP path and require a reachability audit; this is not a
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
build. These are not clean-committed-clone, full trade/battle, intro-completion,
or broad gameplay qualification. The retained full-gate records are
`shared-rom-boot-source-gate.json`, `shared-rom-boot-native-gate.json`,
`shared-stdio-source-gate.json`, and `shared-stdio-native-gate.json`.

The integrated stdio helper also passed four single-session cases in each
runtime with an actual child return code of zero under a 120-second deadline.
The shared stdio suite passed 6/6 in source (35.35 seconds) and native
(28.83 seconds); both Red/Blue remote directions used two child servers that
exited zero and reclaimed their ports. The retained records are
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
native scopes are recorded in `shared-rom-boot-source.xml` and
`shared-rom-boot-native.xml`. The full source/native gate reports are
`shared-rom-boot-source-gate.json` and `shared-rom-boot-native-gate.json`.

The prior receiver/owner snapshot passed 906 unit tests and 660 timing
executions (132 cases repeated five times), with 150 focused checks per
runtime. Those results are retained as historical comparison, not the latest
admission result. The clock-phase/save-v18 experiment remains isolated: its
290-test source result is not native-qualified. See the checklist for remaining
native, negotiation, matrix, and clean-clone requirements.

No complete passing strict trade or battle matrix qualifies the current
runtime. The earlier native nine-row failure remains retained evidence, not
a fresh post-ownership-fix matrix; see the checklist's
[current result](docs/RELEASE_CHECKLIST.md#current-candidate-result).

The historical pre-clock-repair native local battle matrix (128 T-cycles per
bit) completed **4 passed / 5 failed / 0 skipped**:
Red/Yellow and Blue/Yellow passed both orders; all same-version pairs and
Red/Blue both orders failed. See the [release checklist](docs/RELEASE_CHECKLIST.md)
for the observed failure boundaries and scoped evidence.

A historical role-seeded source Red-color→Blue-color remote battle row passed
same-turn mirrored state verification in 455.93 seconds with clean teardown.
It remains a diagnostic and does not qualify the current no-role-RAM path.

Dated installation, gate, MCP, and gameplay results—including failed and
timed-out runs—are retained in the checklist's
[historical evidence](docs/RELEASE_CHECKLIST.md#historical-evidence-retained-for-comparison).
They are not acceptance evidence for later runtime changes.

Recent production-hardening changes make lifecycle failures bounded and
recoverable: serial cleanup ownership is explicit; remote disconnects and
failed teardown fail closed; `LinkPair` setup is transactional; borrowed
PyBoy callbacks are restored; `NetworkBackend` bounds partial-frame reads;
and the bootstrap uses bounded timeouts and validates import-path ownership.
Focused fixture, link-pair, and network regression tests cover these seams.
They do not expand the current trade/battle evidence or establish authentic
gameplay semantics.

| Capability | Current status | Evidence boundary |
|---|---|---|
| Single-session loading, input, state parsing, and save/load | Implemented; scoped checks pass | See the checklist's [current candidate result](docs/RELEASE_CHECKLIST.md#current-candidate-result) for integrated source/native gates and scoped lifecycle checks; this is not clean-install or gameplay certification. |
| MCP stdio server and remote lifecycle | Source/native gates and scoped stdio lifecycle pass; gameplay unqualified | The current reports cover four single-session cases in each runtime plus both Red/Blue remote directions with zero-exit child teardown and port reuse. They do not replace a full remote trade/battle or intro-completion run. See the checklist for retained failures and environment boundaries. |
| In-process `LinkPair` | Implemented; full matrix unqualified | Historical narrow passes and timed-out/failed battle rows are retained in the checklist's [historical evidence](docs/RELEASE_CHECKLIST.md#historical-evidence-retained-for-comparison); the integrated full matrix remains unqualified. |
| Remote TCP transport and MCP lifecycle | Implemented; gameplay unqualified | Transport and lifecycle tests do not establish a real-game trade or battle. The remote API is loopback-only. |
| Remote full trade | Unqualified for this candidate | A release claim requires a current strict result with matching ROMs, fixtures, runtime identity, bounded deadlines, and clean teardown. |
| Link battle | Unqualified for this candidate | A release claim requires a current strict result for the advertised matrix; historical or representative rows are insufficient. |
| Pinned ROM boot/advance/replay/save-load/close | Scoped 5/5 source/native evidence | The integrated XML records five pinned variants, matching hashes, deterministic replay, and closed teardown; it does not establish intro completion, trade, battle, or broad gameplay. |
| Boot-to-Boulder-Badge walkthroughs | Experimental diagnostics | The scripts contain fallback RAM writes and are not a release acceptance suite. |

The release commit must include the explicit `tests/__init__.py` package
boundary and the bundled PyBoy source tree. Until those files are committed
and a clean checkout reproduces the recorded gates, this remains a working
tree status rather than release sign-off.
The source tree is a submodule; initialize it with
`git submodule update --init --depth 1 vendor/pyboy-src` after cloning.

The required setup, test tiers, evidence format, and sign-off rules are in
[`docs/PRODUCTION_RUNBOOK.md`](docs/PRODUCTION_RUNBOOK.md) and
[`docs/RELEASE_CHECKLIST.md`](docs/RELEASE_CHECKLIST.md).

## Supported input formats

The intended release inputs are the exact ROM variants listed in
[`VERSIONS.md`](VERSIONS.md):

| Game | Input | Status |
|---|---|---|
| Pokémon Red (UE) | Stock `.gb` plus `pokered.sym` | Candidate; hash and real-ROM gate required |
| Pokémon Red (UE) color variant | `pokemon-red-color.gb` plus the matching Red symbols | Candidate input; current strict link acceptance remains unqualified |
| Pokémon Blue (UE) | Stock `.gb` plus `pokeblue.sym` | Candidate; hash and real-ROM gate required |
| Pokémon Blue (UE) color variant | `pokemon-blue-color.gb` plus the matching Blue symbols | Candidate input; current strict link acceptance remains unqualified |
| Pokémon Yellow (UE) | Native CGB `.gbc` plus `pokeyellow.sym` | Candidate input; current strict link acceptance remains unqualified |
| Other localisations and ROM hacks | — | Out of scope |

“Candidate” means that the file layout and code paths exist. The current
stateful tests include a local Red/Yellow pair and independent-process Red/Blue
diagnostics, but their strict trade/battle results are not qualified for this
candidate. It does not imply that every listed variant or remote pairing has a
repeatable, green, release-gate result.

## Requirements and clean install

Requirements:

- Python 3.11 or newer.
- The bundled PyBoy runtime (`2.7.0`, harness revision
  `c565df66c3731fad2856169a90f6bbec99925915`).
- `mcp==1.29.1`, which is a runtime dependency of the package.
- A legally obtained ROM and a matching debug symbol file for any real-ROM
  run.

From a clean checkout on Unix, WSL, or Git Bash:

```bash
git submodule update --init --depth 1 vendor/pyboy-src
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pip check
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1` and use
`python -m pip` for the remaining commands. Keep the environment used for
testing and the environment used to launch MCP identical.

For WSL, if dependency imports or startup are blocked by Windows-mounted
filesystem I/O, use a virtual environment and, when practical, a checkout on
the Linux filesystem. Keep the commands relative and portable; this is an
operational workaround, not proof that a particular OS or filesystem is the
underlying cause.

The distribution bundles the pinned PyBoy source runtime used by the link
layer. It exposes the Python-accessible serial backend contract and is marked
with the revision above. If an older standalone `pyboy` wheel is already
installed in an environment, remove it and reinstall this project before
testing; `Session.from_files` rejects an unmarked runtime when the package
pin is enforced.

The default release path uses that source-compatible runtime. The optional
`scripts/bootstrap_pyboy.py --mode cython` path builds the native accelerator
for platform validation, but a Cython build that hides `mb.serial` is not a
valid runtime for the Python-side link attachment tests.

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

Copy only the matching `.sym` files into `rom/<version>/`. Record the source
repository commits and toolchain used for a release; the current repository
does not contain those generated files or a complete source-commit lock.

Before starting a session, compare the ROM's SHA-1 with its matching path row
in [`VERSIONS.md`](VERSIONS.md). Always set `POKERED_ROM_SHA1` explicitly for
release evidence; the loader also selects a matching documented path when the
variable is omitted and fails closed when no pin exists. Never use
`POKERED_SKIP_SHA1=1` for a release run.

## Run one MCP server

The server requires the primary ROM and symbol paths:

```bash
POKERED_ROM_PATH=rom/red/pokemon-red-color.gb \
POKERED_SYM_PATH=rom/red/pokemon-red.sym \
POKERED_ROM_SHA1=e1deed63080bc24cad5fba18ecb3184f905d16d4 \
python -m pokered_harness.mcp_server
```

For Blue or Yellow, replace all three values with the matching row in
[`VERSIONS.md`](VERSIONS.md). The server starts headless. Use a script's
documented display option, or construct `Session(view=True)`, when a visible
session is needed.

The committed `.mcp.json` uses the canonical `rom/red/` layout, an explicit
color-ROM hash, `${PWD}/VERSIONS.md`, and no machine-local `PYTHONPATH`. It is
portable when the MCP client expands `${PWD}` to the workspace root and the
selected `python` is the environment where this package is installed. The
template does not include ROMs or symbols; those remain BYO files.

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
the older semantic bridge remains only for explicitly injected local test
pairs. Remote test doubles must provide the serial contract too. Unsupported
MCP runtimes fail closed rather than selecting a semantic fallback.

### In-process pair

`link_pair` owns two sessions in one process and uses the native bit-accurate
serial coordinator for real PyBoy sessions. In a historical diagnostic run, a
narrow canonical Red/Yellow trade assertion passed; native
local battle passed 1/1 in 21.14 seconds with normal inputs and mirrored
settled state. Source battle exited 124 at a
360-second supervisor bound without a result, after a historical 1,000-second timeout.
Those results do not qualify the current runtime or any complete matrix.
Each advertised ROM, fixture, and runtime combination requires fresh strict
acceptance evidence.
Pair setup is transactional: failed installation rolls back pair-owned state,
and unpair restores borrowed serial and PyBoy callback state.
Configure the peer before launching the MCP server:

```bash
export POKERED_PEER_ROM_PATH=rom/blue/pokemon-blue-color.gb
export POKERED_PEER_SYM_PATH=rom/blue/pokemon-blue.sym
export POKERED_PEER_ROM_SHA1=5f4b05725a860e04077045462176d3e2771c5022
```

Then call `link_pair`, use `link_step`, and call `link_unpair` when finished.
The exact fixture, runtime, and game-flow requirements are in the runbook.

### Remote TCP pair

Two independent MCP servers can use `link_listen` and `link_connect`. The
listener is the internal-clock side; the connector is the external-clock
side. Poll `link_status` until it reports `remote_mode` as `connected`, then
call `link_disconnect` at teardown.

The MCP remote-link API enforces localhost-only binding and connection
(`127.0.0.1`, `localhost`, or `::1`). The transport has no authentication or
encryption and must not be exposed to an untrusted LAN, the public internet,
or a WAN until an authenticated encrypted channel is added. Remote
disconnects are surfaced as link failure; `NetworkBackend` bounds partial-frame
reads and fails closed during teardown.

The current evidence boundary is deliberately narrow:

| Test surface | What it can establish | What it cannot establish |
|---|---|---|
| `tests/test_link_protocol.py` | ROM-free Pokémon serial constants and synthetic exchange behavior | Emulator or game compatibility |
| `tests/test_link_transport.py` and `tests/test_network_backend.py` | In-process queues and TCP edge/response primitives | A real game trade or battle |
| `tests/test_link_symbols_real_roms.py` | Required labels resolve when local symbols are available | A complete gameplay flow |
| `tests/test_link_integration.py` | Fixture-gated in-process real-ROM milestones | Remote two-process behavior or a current acceptance result |
| `tests/test_link_integration_remote.py` | Fixture-gated remote transport/serial milestones | A full user-driven remote trade or battle |
| `tests/test_pyboy_link_session_subprocess.py` | Two-process and strict acceptance entry points | A release result until the current candidate runs the rows with pinned assets |
| `tests/test_pyboy_link_session_roms.py` | Diagnostic matrix and strict acceptance entry points | Full Red/Blue/Yellow coverage or a result from a skipped, RAM-mutated, or unpinned path |

Do not describe a transport milestone as “trade complete.” A full trade or
battle needs an acceptance result from the actual release runtime, matching
ROMs, matching save-state fixtures, bounded deadlines, and a clean teardown.

## Walkthrough scripts

The scripts are useful diagnostics and fixture producers, not a substitute
for the release gate:

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

For link fixtures, use the existing producer
[`scripts/produce_cable_club_fixture.py`](scripts/produce_cable_club_fixture.py)
with a source state captured against the same ROM bytes. There is no separate
Yellow-specific producer in this repository; documentation must not point to
one.

## Development test command

After the collection blocker and runtime contract are fixed, the broad local
command is:

```bash
python -m pytest -q -ra
```

Use the tiered commands in [`docs/PRODUCTION_RUNBOOK.md`](docs/PRODUCTION_RUNBOOK.md)
when ROMs, symbols, fixtures, or the bundled link runtime are present. A
green unit suite alone is not a production result; every required tier must
run with no unexpected failures, skips, xfails, or timeouts.

## License

The harness code is LGPL-3.0-only. No game-derived assets are distributed.
