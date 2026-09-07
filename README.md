# pokered-harness

`pokered-harness` is a memory-first automation harness for Pokémon Red,
Blue, and Yellow. It loads a user-supplied ROM and symbol file, exposes a
typed `Session` API, and can serve that session over MCP.

This repository is a source tree. It does not distribute commercial ROMs,
symbol files, save states, or other ROM-derived artifacts.

## Release status

The release status is `PARTIAL`; this repository is not production-certified.
The integration candidate is based on `d8060be232dbbb75798fa1ff93e49c2f84747b9f`
and may contain uncommitted runtime, test, and packaging changes. An
uncommitted worktree is not a release artifact.

The latest bounded candidate unit check used the bundled source PyBoy runtime
and passed 360/360 unit tests plus 35/35 timing tests across five repetitions.
The current checkout's asset preflight also matched all five pinned ROMs, three
symbol files, and six required ordinary/battle Cable Club fixtures. This remains a scoped
check, not release sign-off: the strict trade gate timed out in its remote
Red-color↔Blue-color subprocess row at the 600-second bound, and no current
battle acceptance result is recorded here. Historical results retained below
are dated context, not current release sign-off.

A separate bounded remote diagnostic completed one Blue-color↔Blue-color trade
row (`1/1`) with balanced `6,528` serial edges per direction and no unknown
opcodes. Both peers used the same fixture, so this is a transport/game-flow
diagnostic, not strict party-swap acceptance or full-matrix evidence.

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
| Single-session loading, input, state parsing, and save/load | Implemented; scoped gate passes | The latest candidate source unit/timing gate passed 360/360 and 35/35 × 5 without ROM assets. Real-ROM checks require the pinned runtime plus local ROM and symbol files. |
| MCP stdio server for one session | Implemented; fixture-gated | `tests/test_mcp_stdio_integration.py` requires real-ROM assets. Current MCP behavior must be rerun with the pinned `mcp` dependency and matching BYO inputs. |
| In-process `LinkPair` | Implemented; acceptance unqualified | Focused and fixture-gated tests exercise the path; no current candidate full local trade/battle acceptance result is recorded. |
| Remote TCP transport and MCP lifecycle | Implemented; gameplay unqualified | Transport and lifecycle tests do not establish a real-game trade or battle. The remote API is loopback-only. |
| Remote full trade | Unqualified for this candidate | A release claim requires a current strict result with matching ROMs, fixtures, runtime identity, bounded deadlines, and clean teardown. |
| Link battle | Unqualified for this candidate | A release claim requires a current strict result for the advertised matrix; historical or representative rows are insufficient. |
| Boot-to-Boulder-Badge walkthroughs | Experimental diagnostics | The scripts contain fallback RAM writes and are not a release acceptance suite. |

The release commit must include the explicit `tests/__init__.py` package
boundary and the bundled PyBoy source tree. Until those files are committed
and a clean checkout reproduces the recorded gates, this remains a working
tree status rather than release sign-off.

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
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pip check
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1` and use
`python -m pip` for the remaining commands. Keep the environment used for
testing and the environment used to launch MCP identical.

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
the older semantic bridge remains only as a compatibility path for test
doubles that do not expose the native serial object.

### In-process pair

`link_pair` owns two sessions in one process and uses the native bit-accurate
serial coordinator for real PyBoy sessions. The canonical Red/Yellow local
trade and battle acceptance cases pass; other rows remain diagnostic until
their exact ROM, fixture, and runtime combination is separately certified.
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
