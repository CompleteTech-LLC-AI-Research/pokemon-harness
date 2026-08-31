# pokered-harness

`pokered-harness` is a memory-first automation harness for Pokémon Red,
Blue, and Yellow. It loads a user-supplied ROM and symbol file, exposes a
typed `Session` API, and can serve that session over MCP.

This repository is a source tree. It does not distribute commercial ROMs,
symbol files, save states, or other ROM-derived artifacts.

## Release status

This repository is an audited candidate, not a production release. The
current functional source boundary is `25e231c`; the historical implementation
boundary at `1046a541e0003923aec6000b6b383c6eaafeaa48` is retained separately.
The baseline at
`e219fb5` was not production-certified.

The counts below are therefore an evidence snapshot with an explicit commit
boundary, not a claim that every listed capability is a finished product:

| Capability | Current status | Evidence boundary |
|---|---|---|
| Single-session loading, input, state parsing, and save/load | Clean wheel MCP smoke: 3/3 | On 2026-08-31, an installed wheel outside the checkout passed tool discovery, stepping, game-state parsing, and save/load with explicit Red color ROM/SYM hashes. The wider five-ROM smoke evidence remains historical. |
| MCP stdio server for one session | Current startup pinning is strict | MCP enforces ROM, symbol, PyBoy version, and exact vendored fork revision when `VERSIONS.md` is available; wheel launches also require explicit ROM/SYM pins. |
| In-process `LinkPair` | Exact-head local 46/46 | Strict Red/Yellow party swap and one complete battle turn pass against the release runtime; the broader Red/Blue/Yellow matrix remains diagnostic. |
| Remote TCP transport and MCP lifecycle | Exact-head remote 11/11; MCP lifecycle 74/74 | Native MCP attach/HELLO, two-process LinkMenu, and native serial paths pass for color-Red listener + color-Blue connector on localhost. The exact-head no-hook Red/Blue battle smoke also passes; reversed roles remain uncertified. |
| Remote full trade | Exact-head 1/2; unstable | The local strict trade passes, but the TCP subprocess trade exceeded its 720s child bound in the latest exact-head run (tier 1/2 at 881.7s). A previous isolated run passed, so stability is not signed off. |
| Link battle | Authentic local and remote smoke pass | The Red/Yellow local battle and Red/Blue subprocess battle select the mode with ordinary input and complete native move exchange; no test-driver RAM write or selection hook is used. Broader battle rows remain unverified. |
| Boot-to-Boulder-Badge walkthroughs | Experimental diagnostics | The scripts contain fallback RAM writes and are not a release acceptance suite. |

The candidate includes the explicit `tests/__init__.py` package boundary and
the bundled PyBoy source tree. Symbol hashes and audited generator provenance
are recorded in [`VERSIONS.md`](VERSIONS.md). The exact-head unit gate passes
445/445 and the timing tier passes 35/35 across five repetitions. Full release
sign-off remains
`PARTIAL`: per-tier evidence bundles are retained outside the checkout, but
the product lint/matrix gates, load-stable remote trade, reversed roles, fixture
provenance, and independent review remain open.

The historical local diagnostic matrix reached LinkMenu and completed the trade
route for all nine ordered Red/Blue/Yellow version pairs. Seven of nine battle
rows reached a complete turn at that historical boundary; `blue↔blue` and the
`blue→red` attach ordering stalled before both sides entered move exchange.
Those rows are not claimed as supported. In the current candidate, the
normalized hardware-time scheduler passes the strict Red↔Yellow trade and
battle cases at the library's default 256-cycle slice (the tighter 64-cycle
acceptance rerun also passes); targeted Blue↔Blue and Blue→Red checks reach
LinkMenu, but their complete battle behavior and reversed roles remain
unverified.

Current exact-head evidence and blockers are explicit:

- unit 445/445 and timing 35/35 across five repetitions pass at `25e231c`;
  exact-head stateful evidence is local 46/46 and remote 11/11, while the
  latest trade tier finished 1/2 after the TCP case exceeded its bound;
- the Lane G audit at `25e231c` reduces the default product Ruff surface to
  101 findings under locked Ruff; the explicit vendored-runtime audit remains
  227 findings, and the broad suite has
  not become a clean production gate;
- the exact-head no-hook Red/Blue remote battle smoke passes with ordinary
  menu input and native serial move exchange; broader battle matrix coverage
  and repeated stability remain open;
- per-ROM single-session coverage, reversed listener/connector roles, native
  platform coverage, battle-fixture provenance, load-stable remote trade, and
  independent review remain incomplete; and
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
| Pokémon Red (UE) | Stock `.gb` plus `pokered.sym` | Hash-pinned input; historical five-row MCP stdio evidence includes this row; link gameplay not claimed |
| Pokémon Red (UE) color variant | `pokemon-red-color.gb` plus the matching Red symbols | Passing local Red/Yellow role; canonical remote listener role tested, but overall release support remains partial |
| Pokémon Blue (UE) | Stock `.gb` plus `pokeblue.sym` | Hash-pinned input; historical five-row MCP stdio evidence includes this row; link gameplay not claimed |
| Pokémon Blue (UE) color variant | `pokemon-blue-color.gb` plus the matching Blue symbols | Canonical remote connector role tested; isolated trade evidence only, with full remote support not signed off |
| Pokémon Yellow (UE) | Native CGB `.gbc` plus `pokeyellow.sym` | Passing local Red/Yellow peer role; other pairings remain unverified |
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

- Python 3.12 or newer.
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

The standard-library `venv` module must include `ensurepip`. On Debian or
Ubuntu, install the matching OS package (for example, `python3.12-venv` or
`python3-venv`) first if `python3 -m venv` reports that `ensurepip` is
unavailable. The commands above assume the resulting environment provides
`python -m pip`; an environment created by another tool must provide the same
pip/install contract before it is used for the release gate.

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

Native attach only installs the serial backend and its transport callbacks. It
does not write Pokémon HRAM such as `hSerialConnectionStatus` or install
symbol-level exchange hooks: the ROM's own serial ISR must establish the role
from native serial traffic. The semantic bridge is therefore not production
evidence for `link_pair`, `link_listen`, or `link_connect`.

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
| `tests/test_pyboy_link_session_subprocess.py` | Two-process LinkMenu smoke plus color Red/Blue native-serial trade and no-hook battle acceptance | Repeated trade stability, reversed roles, and unclaimed ROM/variant rows |
| `tests/test_pyboy_link_session_roms.py` | Diagnostic matrix plus strict local Red/Yellow trade and battle acceptance | Full Red/Blue/Yellow coverage or a release result from a skipped, RAM-mutated, or unpinned path |

Do not describe a transport milestone as “trade complete.” A full trade or
battle needs an acceptance result from the actual release runtime, matching
ROMs, matching save-state fixtures, bounded deadlines, a clean teardown, and
an explicit statement about any test-driver menu control. The current remote
battle result uses ordinary menu input and proves native serial move exchange;
the trade result remains stability-sensitive and does not prove every remote
menu/role combination.

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
one. The producer creates only the ordinary Cable Club state. Battle-start
states must be captured manually for each exact ROM and remain local, ignored
inputs. See [`scripts/WALKTHROUGH_README.md`](scripts/WALKTHROUGH_README.md)
for the diagnostic walkthrough notes.

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
