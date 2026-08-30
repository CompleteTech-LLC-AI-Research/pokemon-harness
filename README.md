# pokered-harness

`pokered-harness` is a memory-first automation harness for Pokémon Red,
Blue, and Yellow. It loads a user-supplied ROM and symbol file, exposes a
typed `Session` API, and can serve that session over MCP.

This repository is a source tree. It does not distribute commercial ROMs,
symbol files, save states, or other ROM-derived artifacts.

## Release status

This repository is an audited candidate, not a production release. The
historical implementation snapshot `1046a541e0003923aec6000b6b383c6eaafeaa48`
passed the complete real-ROM gate once; the current candidate has since
changed the link scheduler, transport lifecycle, battle driver, and fixture
evidence. Those historical counts are retained as a boundary, not as current
sign-off. The baseline at `e219fb5` was not production-certified.

The counts below are therefore an evidence snapshot with an explicit commit
boundary, not a claim that every listed capability is a finished product:

| Capability | Current status | Evidence boundary |
|---|---|---|
| Single-session loading, input, state parsing, and save/load | Candidate-tested for selected Red/Blue/Yellow inputs | The gate checks the pinned runtime, hashes, symbols, and real-ROM paths; the environment-driven boot/MCP test is not a five-row single-session certification. |
| MCP stdio server for one session | Certified for the tested explicit-ROM smoke path | `tests/test_mcp_stdio_integration.py` and the fresh wheel smoke use the constrained MCP 1.x dependency; repeat the test for each ROM before making a per-ROM claim. |
| In-process `LinkPair` | Candidate-tested; Red/Yellow strict trade and battle pass | The current candidate passes the strict color-Red/Yellow party swap and one complete battle turn with the normalized scheduler; the broader Red/Blue/Yellow matrix remains diagnostic. |
| Remote TCP transport and MCP lifecycle | Current candidate 11/11 remote gate pass for canonical localhost roles | The current gate passed native MCP attach/HELLO, two-process LinkMenu, and native serial paths for color Red listener + color Blue connector. The LinkMenu choice is controlled by the acceptance driver. |
| Remote full trade | Current controlled native-serial acceptance: 2/2 | The current independent-process color Red/Blue test compares both full 44-byte party-mon records; it uses a test-driver LinkMenu selection hook, not full user-driven gameplay. |
| Link battle | Current Red/Yellow + Red/Blue controlled acceptance; broader matrix incomplete | The current candidate resolves one strict local Red/Yellow move turn and one strict remote Red/Blue move turn. Blue↔Blue, Blue→Red, reversed roles, and other rows are not certified. |
| Boot-to-Boulder-Badge walkthroughs | Experimental diagnostics | The scripts contain fallback RAM writes and are not a release acceptance suite. |

The candidate includes the explicit `tests/__init__.py` package boundary and
the bundled PyBoy source tree. Symbol hashes and audited generator provenance
are recorded in [`VERSIONS.md`](VERSIONS.md). Full release sign-off remains
`PARTIAL`: the complete gate output and independent review are not attached,
the broad lint and diagnostic matrix are not clean, and several matrix,
fixture-provenance, role, and security boundaries remain open.

The historical local diagnostic matrix reached LinkMenu and completed the trade
route for all nine ordered Red/Blue/Yellow version pairs. Seven of nine battle
rows reached a complete turn at that historical boundary; `blue↔blue` and the
`blue→red` attach ordering stalled before both sides entered move exchange.
Those rows are not claimed as supported. In the current candidate, the
normalized hardware-time scheduler passes the strict Red↔Yellow trade and
battle cases with `POKERED_LINK_CHUNK_CYCLES=64`; targeted Blue↔Blue and
Blue→Red checks reach LinkMenu, but their complete battle behavior and reversed
roles remain unverified.

Current release blockers are explicit:

- the current full local and remote gates are still being rerun for the
  scheduler change; the strict Red↔Yellow cases are green, but the full matrix
  is not certified;
- the repository-wide Ruff audit reports 546 findings and the broad suite has
  not become a clean production gate;
- the audited implementation gate output is not retained in this tree as a
  complete evidence bundle;
- per-ROM single-session coverage, reversed listener/connector roles, native
  platform coverage, and independent review remain incomplete; and
- TCP is deliberately localhost-only because it has no authentication or
  encryption. Cross-host use is unsupported until an authenticated encrypted
  channel exists.

The required setup, test tiers, evidence format, and sign-off rules are in
[`docs/PRODUCTION_RUNBOOK.md`](docs/PRODUCTION_RUNBOOK.md) and
[`docs/RELEASE_CHECKLIST.md`](docs/RELEASE_CHECKLIST.md).

## Supported input formats

The intended release inputs are the exact ROM variants listed in
[`VERSIONS.md`](VERSIONS.md):

| Game | Input | Status |
|---|---|---|
| Pokémon Red (UE) | Stock `.gb` plus `pokered.sym` | Hash-pinned input; selected single-session checks only; link gameplay not claimed |
| Pokémon Red (UE) color variant | `pokemon-red-color.gb` plus the matching Red symbols | Certified in the local Red/Yellow and remote Red/Blue acceptance roles |
| Pokémon Blue (UE) | Stock `.gb` plus `pokeblue.sym` | Hash-pinned input; selected single-session checks only; link gameplay not claimed |
| Pokémon Blue (UE) color variant | `pokemon-blue-color.gb` plus the matching Blue symbols | Certified as the remote Red/Blue acceptance connector |
| Pokémon Yellow (UE) | Native CGB `.gbc` plus `pokeyellow.sym` | Certified as the local Red/Yellow acceptance peer |
| Other localisations and ROM hacks | — | Out of scope |

The historical stateful evidence scope is the local color-Red/Yellow pair and
the independent-process color-Red/color-Blue pair for the tested trade and
battle paths, using the bundled source-runtime build. The remote evidence
fixes the roles as Red listener/internal-clock and Blue connector/external-
clock. The current candidate is not certified: stock ROM link pairs,
Blue/Yellow pairs, reversed listener/connector roles, and other unlisted rows
remain unsupported or unverified until they receive fresh fixtures and
acceptance results.

## Requirements and clean install

Requirements:

- Python 3.11 or newer.
- The bundled PyBoy runtime (`2.7.0`, harness revision
  `c565df66c3731fad2856169a90f6bbec99925915`).
- `mcp==1.29.1`, the certified runtime API used by the server.
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
for diagnostic/platform validation only. A Cython build that hides `mb.serial`
has not been certified for Python-side link attachment and must not be used for
the link acceptance gate.

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

Use the source commits and RGBDS toolchain recorded in [`VERSIONS.md`](VERSIONS.md)
when reproducing the audited symbols, then copy only the matching `.sym` files
into `rom/<version>/`. The repository records audited provenance but does not
contain the generated symbols or enforce a source-build lock.

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
POKERED_VERSIONS_PATH="$PWD/VERSIONS.md" \
python -m pokered_harness.mcp_server
```

For Blue or Yellow, replace all three values with the matching row in
[`VERSIONS.md`](VERSIONS.md). The server starts headless. Use a script's
documented display option, or construct `Session(view=True)`, when a visible
session is needed.

The committed `.mcp.json` uses the canonical `rom/red/` layout, an explicit
color-ROM hash, an explicit `${PWD}/VERSIONS.md` pin file, and no machine-local
`PYTHONPATH`. It is suitable for a workspace whose MCP client expands `${PWD}`
and whose `python` command resolves to the installed package environment. An
installed wheel launched outside a checkout may omit `VERSIONS.md` when
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
doubles that do not expose the native serial object.

### In-process pair

`link_pair` owns two sessions in one process and uses the native bit-accurate
serial coordinator for real PyBoy sessions. The canonical Red/Yellow local
trade and battle acceptance cases pass; other rows remain diagnostic until
their exact ROM, fixture, and runtime combination is separately certified.
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
listener is the internal-clock side; the connector is the external-clock
side. Poll `link_status` until it reports `remote_mode` as `connected`, then
call `link_disconnect` at teardown.

The MCP remote-link API enforces localhost-only binding and connection
(`127.0.0.1`, `localhost`, or `::1`). The transport has no authentication or
encryption and must not be exposed to an untrusted LAN, the public internet,
or a WAN until an authenticated encrypted channel is added. Treat this as a
security boundary, not as cross-host support.

The current evidence boundary is deliberately narrow:

| Test surface | What it can establish | What it cannot establish |
|---|---|---|
| `tests/test_link_protocol.py` | ROM-free Pokémon serial constants and synthetic exchange behavior | Emulator or game compatibility |
| `tests/test_link_transport.py` and `tests/test_network_backend.py` | In-process queues and TCP edge/response primitives | A real game trade or battle |
| `tests/test_link_symbols_real_roms.py` | Required labels resolve when local symbols are available | A complete gameplay flow |
| `tests/test_link_integration.py` | Fixture-gated in-process real-ROM milestones | Remote two-process behavior |
| `tests/test_link_integration_remote.py` | Fixture-gated remote transport/serial milestones | A full user-driven remote trade or battle |
| `tests/test_pyboy_link_session_subprocess.py` | Two-process LinkMenu smoke plus controlled color Red/Blue native-serial trade and battle acceptance | User-driven menu input, reversed roles, and unclaimed ROM/variant rows |
| `tests/test_pyboy_link_session_roms.py` | Diagnostic matrix plus strict local Red/Yellow trade and battle acceptance | Full Red/Blue/Yellow coverage or a release result from a skipped, RAM-mutated, or unpinned path |

Do not describe a transport milestone as “trade complete.” A full trade or
battle needs an acceptance result from the actual release runtime, matching
ROMs, matching save-state fixtures, bounded deadlines, a clean teardown, and
an explicit statement about any test-driver menu control. The current remote
result proves native serial payload handling for a controlled LinkMenu setup;
it does not prove an end-user can drive both menus independently.

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

For link fixtures, use the existing producer
[`scripts/produce_cable_club_fixture.py`](scripts/produce_cable_club_fixture.py)
with a source state captured against the same ROM bytes. There is no separate
Yellow-specific producer in this repository; documentation must not point to
one.

## Development test command

For development diagnostics, the broad local command is:

```bash
python -m pytest -q -ra
```

Use the tiered commands in [`docs/PRODUCTION_RUNBOOK.md`](docs/PRODUCTION_RUNBOOK.md)
when ROMs, symbols, fixtures, or the bundled link runtime are present. The
current diagnostic matrix is known to stall on `blue↔blue` and `blue→red`
battle rows, so a broad run is not currently a production result. A green unit
suite alone is not a production result; every required tier must run with no
unexpected failures, skips, xfails, or timeouts.

## License

The harness code is LGPL-3.0-only. No game-derived assets are distributed.
