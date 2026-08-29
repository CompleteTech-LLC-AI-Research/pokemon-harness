# pokered-harness

`pokered-harness` is a memory-first automation harness for Pokémon Red,
Blue, and Yellow. It loads a user-supplied ROM and symbol file, exposes a
typed `Session` API, and can serve that session over MCP.

This repository is a source tree. It does not distribute commercial ROMs,
symbol files, save states, or other ROM-derived artifacts.

## Release status

As audited on 2026-08-29 at commit `e219fb5`, this checkout is not certified
as a production release. The distinction matters:

| Capability | Current status | Evidence boundary |
|---|---|---|
| Single-session loading, input, state parsing, and save/load | Implemented; release gate pending | The API and unit tests exist. Real-ROM proof is gated by local ROM and symbol files. |
| MCP stdio server for one session | Implemented; release gate pending | `tests/test_mcp_stdio_integration.py` is real-ROM and dependency gated. |
| In-process `LinkPair` | Implemented in source; not release-certified | Local link tests require matching ROM fixtures and a PyBoy runtime whose serial internals are accessible to the link layer. |
| Remote TCP transport | Transport implementation present; gameplay not certified | `tests/test_link_integration_remote.py` is scoped to transport/serial milestones, not a complete user-driven trade. |
| Remote full trade | Not release-ready | The subprocess test is fixture- and non-Cython-runtime gated; it must pass with the documented production runtime before this capability can be called supported. |
| Link battle | Not release-ready | Current battle tests are local, fixture-gated diagnostics; no remote battle acceptance result is recorded. |
| Boot-to-Boulder-Badge walkthroughs | Experimental diagnostics | The scripts contain fallback RAM writes and are not a release acceptance suite. |

The clean committed tree also has a release determinism blocker that must be
fixed by the test lane: several tests import `tests.*`, but
`tests/__init__.py` is not committed. The `pytest` console-script invocation
fails eight modules during collection in this checkout. `python -m pytest` can
collect them through namespace-package behavior in some environments, but
that is not a portable substitute for fixing the package boundary.

The required setup, test tiers, evidence format, and sign-off rules are in
[`docs/PRODUCTION_RUNBOOK.md`](docs/PRODUCTION_RUNBOOK.md) and
[`docs/RELEASE_CHECKLIST.md`](docs/RELEASE_CHECKLIST.md).

## Supported input formats

The intended release inputs are the exact ROM variants listed in
[`VERSIONS.md`](VERSIONS.md):

| Game | Input | Status |
|---|---|---|
| Pokémon Red (UE) | Stock `.gb` plus `pokered.sym` | Candidate; hash and real-ROM gate required |
| Pokémon Red (UE) color variant | `pokemon-red-color.gb` plus the matching Red symbols | Candidate; use a matching fixture if testing links |
| Pokémon Blue (UE) | Stock `.gb` plus `pokeblue.sym` | Candidate; hash and real-ROM gate required |
| Pokémon Blue (UE) color variant | `pokemon-blue-color.gb` plus the matching Blue symbols | Candidate; use a matching fixture if testing links |
| Pokémon Yellow (UE) | Native CGB `.gbc` plus `pokeyellow.sym` | Candidate; hash and real-ROM gate required |
| Other localisations and ROM hacks | — | Out of scope |

“Candidate” means that the file layout and code paths exist. It does not mean
that the current checkout has a repeatable, green, release-gate result for
that variant.

## Requirements and clean install

Requirements:

- Python 3.11 or newer.
- The exact PyBoy version declared in `pyproject.toml` (`2.7.0`).
- `mcp` for the MCP server; it is an optional project extra.
- A legally obtained ROM and a matching debug symbol file for any real-ROM
  run.

From a clean checkout on Unix, WSL, or Git Bash:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev,mcp]"
python -m pip check
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1` and use
`python -m pip` for the remaining commands. Keep the environment used for
testing and the environment used to launch MCP identical.

The link implementation currently requires a separately verified PyBoy
runtime contract. The stock Cython wheel declared by the project does not
expose every serial object needed by the link layer, while the link tests
expect a Python-accessible serial implementation. Until the runtime lane
pins or packages one supported solution, treat single-session use as the
only declared default and do not label link support production-ready.

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

Before starting a session, compare the ROM's SHA-1 with
[`VERSIONS.md`](VERSIONS.md). Always set `POKERED_ROM_SHA1` explicitly for
the selected ROM. The current loader uses the first SHA-1 row in
`VERSIONS.md` as its fallback, so omitting the variable while selecting Blue,
Yellow, or a color variant can validate against the wrong pin. Never use
`POKERED_SKIP_SHA1=1` for a release run.

## Run one MCP server

The server requires the primary ROM and symbol paths:

```bash
POKERED_ROM_PATH=rom/red/pokemon-red-color.gb \
POKERED_SYM_PATH=rom/red/pokemon-red.sym \
POKERED_ROM_SHA1=e1deed63080bc24cad5fba18ecb3184f905d16d4 \
PYTHONPATH=src \
python -m pokered_harness.mcp_server
```

For Blue or Yellow, replace all three values with the matching row in
[`VERSIONS.md`](VERSIONS.md). The server starts headless. Use a script's
documented display option, or construct `Session(view=True)`, when a visible
session is needed.

The committed `.mcp.json` is not currently a valid default for the canonical
layout: it refers to flat `rom/pokemon-red-color.gb` and
`rom/pokemon-red.sym` paths, while the files belong under `rom/red/`. Treat
that configuration as a packaging-lane blocker and use the explicit command
above until it is corrected and tested.

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

PyBoy does not provide the hardware link model required by Gen I Pokémon.
The harness therefore bridges the game’s serial routines at either an
in-process or TCP transport boundary. These are implementation modes, not
claims of production gameplay support.

### In-process pair

`LinkPair` owns two sessions in one process and advances them in an
interleaved schedule. Configure the peer before launching the MCP server:

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

The current transport has no authentication or encryption. It is therefore
restricted to loopback or a trusted private network by release policy; it
must not be exposed to an untrusted LAN, the public internet, or a WAN until
the transport lane adds an authenticated encrypted channel.

The current evidence boundary is deliberately narrow:

| Test surface | What it can establish | What it cannot establish |
|---|---|---|
| `tests/test_link_protocol.py` | ROM-free Pokémon serial constants and synthetic exchange behavior | Emulator or game compatibility |
| `tests/test_link_transport.py` and `tests/test_network_backend.py` | In-process queues and TCP edge/response primitives | A real game trade or battle |
| `tests/test_link_symbols_real_roms.py` | Required labels resolve when local symbols are available | A complete gameplay flow |
| `tests/test_link_integration.py` | Fixture-gated in-process real-ROM milestones | Remote two-process behavior |
| `tests/test_link_integration_remote.py` | Fixture-gated remote transport/serial milestones | A full user-driven remote trade or battle |
| `tests/test_pyboy_link_session_subprocess.py` | Candidate two-process LinkMenu/trade paths when its fixture and runtime gates are satisfied | Readiness with the default runtime unless it is the tested runtime |
| `tests/test_pyboy_link_session_roms.py` | Candidate local link, trade, battle, and variant paths under their fixture/runtime gates | A release result when the tests skip, mutate fixture RAM, or use an unpinned runtime |

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
when ROMs, symbols, fixtures, or the non-Cython link runtime are present. A
green unit suite alone is not a production result; every required tier must
run with no unexpected failures, skips, xfails, or timeouts.

## License

The harness code is LGPL-3.0-only. No game-derived assets are distributed.
