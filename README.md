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
current implementation candidate is merged commit `b0b63c8` (PR #17), which
includes the MCP lifecycle, runtime packaging, in-process serial/lifecycle,
and remote TCP follow-ups from PRs #12-#17.

The latest complete production gate was collected on 2026-09-02 from isolated
source head `df0e7424c87c812a57f257286b0dc00e87c498f4`, whose implementation
tree is merged as `b0b63c8`, before this documentation-only update. It used
Python 3.12.13, Pytest 9.1.1, and vendored PyBoy 2.7.0 at fork revision
`c565df66c3731fad2856169a90f6bbec99925915`. It collected 698 tests and
passed every selected tier: unit 554/554, local real-ROM 47/47, remote
transport/MCP 15/15, strict trade 19/19, strict battle 19/19, and timing
40/40 in each of five repetitions. The ten-entry external fixture manifest
also validated, and the run had no skips, xfails, failures, errors, or
timeouts. Its sanitized evidence bundle is retained outside version control
because ROMs, symbols, and save states are operator-supplied assets.

The release status remains `PARTIAL`, despite the complete source-runtime
gate passing. The vendored source PyBoy build remains the documented release
runtime. The optional pinned Cython build and compiled serial-contract probe
now pass, but no Cython real-ROM attach/detach smoke or Cython trade/battle
matrix has been run. Vanilla fixture provenance, broad-suite coverage,
native-platform coverage, concurrent-load evidence, independent review, and
authenticated/encrypted cross-host TCP remain open. Earlier 18/19 trade and
17/19 battle snapshots are historical and are superseded by the current
19/19 gate result.

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

- complete production gate: `PASS`, collection 698, unit 554/554, local
  real-ROM 47/47, remote transport/MCP 15/15, strict trade 19/19, strict
  battle 19/19, and timing 40/40 across five repetitions, with no skips,
  xfails, failures, errors, or timeouts;
- focused post-change integration slice: `PASS`, 294/294;
- fixture manifest: `PASS`, all 10 external entries validated;
- source runtime/package checks: `PASS`, `pip check`, source bootstrap, and
  the selected production Ruff boundary;
- Cython/native serial build: `PASS` for the pinned build and semantic serial
  contract; real-ROM Cython gameplay remains `UNVERIFIED`.

The full gate used native bit-level serial traffic, ordinary ROM input, exact
party-record and battle-hook assertions, bounded deadlines, and clean teardown
for every declared local and remote row. This establishes the advertised
source-runtime scope; it does not establish the open Cython, provenance,
platform, load, review, or network-security claims.

| Capability | Status | Evidence boundary |
|---|---|---|
| ROM-free unit and timing regressions | `PASS` (scoped) | Current complete gate: collection 698, unit 554/554, and timing 40/40 across five repetitions. |
| Runtime/package identity | `PASS` (scoped) | The gate resolves vendored source PyBoy 2.7.0, fork `c565df66c3731fad2856169a90f6bbec99925915`, the packaged entry point, and the bit-accurate serial contract. |
| Canonical color Red/Blue/Yellow fixture evidence | `PASS` for recorded byte reproduction; release remains `PARTIAL` | The external manifest records verified ordinary and derived battle fixture bytes for color Red, color Blue, and Yellow. States remain BYO and untracked; hashes do not replace gameplay acceptance. |
| Single-session/MCP | `PASS` (tested scope) | The asset-backed local/session tier passed 47/47; the source-runtime strict local and remote gameplay matrices also passed. |
| In-process link acceptance | `PASS` (scoped) | The post-change local production tier passed 47/47 with the pinned external assets. Stock-ROM link support remains unclaimed. |
| Remote TCP and MCP lifecycle | `PASS` (tested source-runtime scope) | The remote transport/MCP tier passed 15/15 and every strict remote trade and battle row passed (9/9 each). TCP remains loopback-only and unauthenticated/unencrypted. |
| Cython/native serial runtime | `PASS` (build/contract scope) | The pinned Cython build and semantic serial probe pass; no Cython real-ROM lifecycle or gameplay acceptance is claimed. |
| Walkthroughs | Diagnostic only | Walkthrough scripts can use state writes or fallback paths and are not release acceptance. |

The historical complete real-ROM snapshot at
`1046a541e0003923aec6000b6b383c6eaafeaa48` is retained as historical evidence
only. The current product Ruff check is clean for the explicitly configured CI
files; the broad legacy tree still reports pre-existing style violations and is
not silently reformatted. The full source-runtime gate is now complete, while
full Cython gameplay coverage, vanilla fixture provenance, broad-suite and
native-platform coverage, concurrent-load evidence, independent review, and
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
| Pokémon Red (UE) color variant | `pokemon-red-color.gb` plus the matching Red symbols | Pinned BYO input; source-runtime strict trade and battle evidence passed |
| Pokémon Blue (UE) | Stock `.gb` plus `pokeblue.sym` | Hash-pinned BYO input; current stateful link support is not claimed |
| Pokémon Blue (UE) color variant | `pokemon-blue-color.gb` plus the matching Blue symbols | Pinned BYO input; source-runtime strict trade and battle evidence passed |
| Pokémon Yellow (UE) | Native CGB `.gbc` plus `pokeyellow.sym` | Pinned BYO input; source-runtime strict trade and battle evidence passed |
| Other localisations and ROM hacks | — | Out of scope |

The current controlled stateful evidence is recorded on the merged candidate.
The complete source-runtime strict trade and battle runs each passed 19/19,
including all local, dedicated, and remote listener/connector rows. Stock-ROM
link pairs remain outside the strict canonical matrix because their fixture
provenance is partial.

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

The default release path uses the source-compatible runtime. The pinned Cython
diagnostic build now compiles with the checked-in serial ABI and passes its
semantic bit-accuracy probe, but no Cython real-ROM lifecycle or gameplay
acceptance has been run; source mode remains the documented release default.

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
serial coordinator for real PyBoy sessions. The post-change local production
tier recorded 47/47 passing tests against the pinned external assets; the
strict local trade rows also passed 9/9, while the strict local battle rows are
part of the still-running battle gate. Earlier evidence passed the 10
local/dedicated trade rows and 10 local/dedicated battle rows, but that evidence
does not certify this follow-up candidate.
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
bounded deadlines.

The current evidence boundary is deliberately narrow:

| Test surface | What it can establish | What it cannot establish |
|---|---|---|
| `tests/test_link_protocol.py` | ROM-free Pokémon serial constants and synthetic exchange behavior | Emulator or game compatibility |
| `tests/test_link_transport.py` and `tests/test_network_backend.py` | In-process queues and TCP edge/response primitives | A real game trade or battle |
| `tests/test_link_symbols_real_roms.py` | Required labels resolve when local symbols are available | A complete gameplay flow |
| `tests/test_link_integration.py` | Fixture-gated in-process real-ROM milestones | Remote two-process behavior |
| `tests/test_link_integration_remote.py` | Fixture-gated remote transport/serial milestones | A full user-driven remote trade or battle |
| `tests/test_pyboy_link_session_subprocess.py` | Parameterized two-process LinkMenu smoke plus canonical color Red/Blue/Yellow native-serial trade and battle acceptance entrypoints | Concurrent-load stability; the current source-runtime strict remote trade and battle matrices passed 9/9 each |
| `tests/test_pyboy_link_session_roms.py` | Diagnostic matrix plus parameterized canonical Red/Blue/Yellow local trade and battle acceptance entrypoints | Stock-variant coverage, or a release result from a skipped, RAM-mutated, or unpinned path |

Do not describe a transport milestone as “trade complete.” A full trade or
battle needs an acceptance result from the actual release runtime, matching
ROMs, matching save-state fixtures, bounded deadlines, a clean teardown, and
an explicit statement about any test-driver menu control. The current strict
declaration contains 19 trade and 19 battle entrypoints: nine local ordered
rows, one dedicated Red/Yellow assertion, and nine remote ordered listener /
connector rows for each operation. The complete source-runtime candidate
passed all 19 trade and all 19 battle entrypoints. Cython gameplay, vanilla
fixture provenance, broad-suite/platform/load/review coverage, and secure
cross-host networking remain outside that result.

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
  --unit-only \
  --repeat-timing 5 \
  --evidence-dir "$EVIDENCE_DIR" \
  --format text
```

The current candidate’s clean-scope gate passes 554/554 unit tests and 40/40
timing cases across five repetitions, with 698 tests collected. The complete
asset-backed gate additionally passed local 47/47, remote 15/15, strict trade
19/19, and strict battle 19/19. The result is still not a production sign-off
because the Cython gameplay, fixture provenance, platform/load/security, and
independent-review conditions remain open.

Use the tiered commands in [`docs/PRODUCTION_RUNBOOK.md`](docs/PRODUCTION_RUNBOOK.md)
when ROMs, symbols, fixtures, or the bundled link runtime are present. The
current matrix declaration is complete, but its collection audit is not
runtime evidence. A green unit suite alone is not a production result; every
required tier must run with no unexpected failures, skips, xfails, or
timeouts.

## License

The harness code is LGPL-3.0-only. No game-derived assets are distributed.
