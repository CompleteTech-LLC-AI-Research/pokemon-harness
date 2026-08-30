# Production runbook

This runbook defines how to prepare and evaluate a clean `pokered-harness`
checkout. The baseline at `e219fb5` was not certified. The historical audited
implementation revision is
`1046a541e0003923aec6000b6b383c6eaafeaa48`; its complete real-ROM gate was
rerun once at that exact clean revision. The current candidate has since
changed the link scheduler, transport lifecycle, battle driver, and fixture
evidence, so the historical counts below are evidence boundaries rather than
current sign-off. Uncommitted worktree changes are excluded from the candidate
record.

The historical full-gate snapshot is unit 372/372, timing 35/35 across five
repetitions, local 46/46, remote 11/11, strict trade 2/2, and strict battle
2/2. Current candidate evidence on the scheduler-fix boundary is 392/392 unit
tests, 35/35 timing cases across five repetitions, and current strict local
Red/Yellow trade plus battle passes at the library's default scheduler slice
(the tighter `POKERED_LINK_CHUNK_CYCLES=64` rerun also passes); the broader
local rerun must still be recorded before release sign-off. The current remote
tier has now passed
11/11 tests for the canonical color-Red listener/internal-clock and color-Blue
connector/external-clock roles. Strict local evidence covers color Red +
Yellow; strict remote evidence covers color Red as listener/internal-clock and
color Blue as connector/external-clock. The current strict trade and battle
tiers each pass 2/2 (local Red/Yellow plus controlled remote Red/Blue).
The remote subprocess driver controls the LinkMenu choice with a test hook, so
the remote result is controlled native-serial acceptance, not full user-driven
menu gameplay. Symbol hashes and audited generator provenance are in
[`VERSIONS.md`](../VERSIONS.md). Overall status is `PARTIAL`; the exact open
items are listed in [the release checklist](RELEASE_CHECKLIST.md).

## 1. Start from a clean checkout

Use a fresh clone or an isolated worktree. Do not use a dirty development
checkout as release evidence.

```bash
git clone <repository-url> poke-harness
cd poke-harness
git status --short
git rev-parse HEAD

python3 --version
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pip check
```

For a historical reproduction, revision
`1046a541e0003923aec6000b6b383c6eaafeaa48` must be present before collecting
the snapshot described above. For a current release candidate, record the
actual candidate commit and rerun every affected tier; do not layer new
runtime or test changes over the historical result. The working tree should
be clean; ignored BYO assets may be present outside the tracked source. Use
the same activated interpreter for installation, tests, the gate, and MCP.

The required Python version is 3.11 or newer. On Windows PowerShell, create
the same environment with `py -3 -m venv .venv`, activate with
`.venv\Scripts\Activate.ps1`, and use `python -m pip` for the remaining
commands. Run the tests and the MCP server with the same interpreter.

The repository is source-only. Obtain ROMs and symbols legally and keep them
outside version control. The `.gitignore` intentionally excludes ROMs, symbol
files, save states, and walkthrough output.

## 2. Install matching ROM and symbol inputs

Create this layout:

```text
rom/
├── red/
│   ├── pokemon-red.gb
│   ├── pokemon-red-color.gb       # optional candidate variant
│   └── pokemon-red.sym
├── blue/
│   ├── pokemon-blue.gb
│   ├── pokemon-blue-color.gb      # optional candidate variant
│   └── pokemon-blue.sym
└── yellow/
    ├── pokemon-yellow.gbc
    └── pokemon-yellow.sym
```

The real-ROM acceptance tiers additionally require these ignored save-state
files:

```text
tests/fixtures/link/
├── red/
│   ├── cable_club.state
│   └── cable_club-battle.state
├── blue/
│   ├── cable_club.state
│   └── cable_club-battle.state
└── yellow/
    ├── cable_club.state
    └── cable_club-battle.state
```

The default `cable_club.state` files support the ordinary Cable Club path;
the `cable_club-battle.state` files are separate legal battle-start states.
The production gate preflight lists the three default files, while the strict
battle tests independently require the three battle files. A missing battle
file therefore produces a required-tier skip/failure rather than a valid
battle result.

Compare every ROM used by a test or launch to the corresponding SHA-1 in
[`VERSIONS.md`](../VERSIONS.md). For example:

```bash
sha1sum rom/red/pokemon-red.gb rom/red/pokemon-red-color.gb
sha1sum rom/blue/pokemon-blue.gb rom/blue/pokemon-blue-color.gb
sha1sum rom/yellow/pokemon-yellow.gbc
sha1sum rom/red/pokemon-red.sym rom/blue/pokemon-blue.sym rom/yellow/pokemon-yellow.sym
sha1sum tests/fixtures/link/*/*.state
```

The matching `.sym` file must come from the matching game source/build. Use the
source commits and RGBDS `v1.0.1` provenance recorded in `VERSIONS.md`, and
record the symbol-file hash, source commit, RGBDS version, and build flags in
the release evidence. A symbol file being readable is not proof that its
labels match the ROM.

## 3. Run the clean-tree gate

Run collection first so missing dependencies and import failures are visible:

```bash
python -m pytest --collect-only -q
python -m pytest -q -ra
```

The release gate requires collection to complete without errors. It also
requires no unexpected skips, xfails, failures, or timeouts in any tier marked
required below. Fixture-gated tests may be skipped during development, but a
skip is not a passing release result.

The complete gate command uses the same interpreter as the activated
environment and keeps its report outside the checkout:

```bash
POKERED_ROM_ROOT="$PWD/rom" \
POKERED_FIXTURE_ROOT="$PWD/tests/fixtures/link" \
python scripts/production_gate.py \
  --repo-root "$PWD" \
  --rom-root "$PWD/rom" \
  --fixture-root "$PWD/tests/fixtures/link" \
  --python "$(command -v python)" \
  --repeat-timing 5 \
  --format text
```

Use `--format json` and redirect to a file outside the checkout when a
machine-readable report must be retained. `--unit-only` proves only the
ROM-free and timing tiers; it is not a production sign-off. The default gate
checks five pinned ROM paths, three symbol paths, and three default fixture
paths before running all required tiers.

The current candidate has not passed a repository-wide Ruff audit: `ruff check
.` reports 546 findings, including legacy and vendored-runtime code. Treat
lint cleanup as a remaining release task even when the scoped production gate
is green. The current full-gate counts below must not be read as evidence that
the broad suite or lint gate is clean.

The release tree includes `tests/__init__.py`; otherwise environments that do
not treat `tests/` as a namespace package can fail collection. Run both
invocation forms for every release candidate.

For a machine-readable report, run the gate from the repository root:

```bash
python scripts/production_gate.py --format json > production-gate.json
```

The command fails closed when required ROMs, symbols, fixtures, or acceptance
tests are missing. Keep the report outside version control if it contains
local paths or ROM-derived details.

## 4. Run the evidence tiers

Run the tiers in order and save the complete output with the commit and
interpreter identity.

### Tier A: ROM-free behavior and runtime contract

This tier exercises configuration, state parsing, serial semantics, protocol,
transport, and symbol-loader behavior without commercial game assets:

```bash
python -m pytest -q \
  tests/test_agent_sync.py \
  tests/test_config.py \
  tests/test_game_state.py \
  tests/test_link_protocol.py \
  tests/test_link_symbols.py \
  tests/test_link_transport.py \
  tests/test_network_backend.py \
  tests/test_serial_core.py \
  tests/test_serial_coordinator.py \
  tests/test_serial_link.py \
  tests/test_state_bag.py \
  tests/test_state_battle.py \
  tests/test_state_menu.py \
  tests/test_state_overworld.py \
  tests/test_state_progress.py \
  tests/test_state_status.py \
  tests/test_state_text.py \
  tests/test_symbol_loader.py
```

These tests do not require commercial ROM bytes. The package bundles the
source PyBoy runtime pinned in `VERSIONS.md`; the gate prepends that runtime
when running from a checkout so a standalone PyBoy wheel cannot silently
change the serial contract. A green Tier A result does not establish
emulator, MCP, trade, or battle compatibility.

### Tier B: one real session and MCP stdio

Use one explicit ROM hash. This example uses stock Red:

```bash
POKERED_ROM_PATH=rom/red/pokemon-red.gb \
POKERED_SYM_PATH=rom/red/pokemon-red.sym \
POKERED_ROM_SHA1=ea9bcae617fdf159b045185467ae58b2e4a48b9a \
python -m pytest -q -ra \
  tests/test_golden_paths.py \
  tests/test_mcp_stdio_integration.py
```

Repeat with the appropriate path, symbol file, and hash for each release input
that will be advertised. The current evidence used one environment-driven
primary-ROM selection for this test module, so it does not by itself certify
all five ROM rows. These tests prove only the tested boot/state/MCP surface;
they do not prove link gameplay.

### Tier C: real symbol and local-link smoke

With all candidate ROMs and matching symbols available:

```bash
python -m pytest -q -ra \
  tests/test_link_symbols_real_roms.py \
  tests/test_link_integration.py
```

The local integration module's active matrix is fixture-gated and covers
transport milestones. Its pair-smoke test proves construction, hook
installation, a short step, and teardown; its trade test is a fixture-gated
round-trip. Neither result should be generalized to every ROM variant or to
remote play.

### Tier D: local link session, trade, and battle acceptance

The more extensive candidate suite is:

```bash
python -m pytest -q -ra \
  tests/test_pyboy_link_session_roms.py
```

The diagnostic matrix in this module is broader than the release acceptance
scope. The historical audit reached LinkMenu and completed the diagnostic
trade route for all nine R/B/Y version orderings. Seven of nine diagnostic
battle rows reached a complete turn at that historical boundary; `blue↔blue`
and the `blue→red` attach ordering did not reach both move-exchange hooks. A
current targeted re-audit reaches LinkMenu for Blue↔Blue and Blue→Red in 520
frames. The current strict Red↔Yellow trade and battle cases pass at the
library's default scheduler slice; the tighter 64-cycle rerun also passes.
The remaining matrix is not certified. The strict local cases are:

```bash
python -m pytest -q \
  tests/test_pyboy_link_session_roms.py::test_red_yellow_trade_swaps_real_party_records \
  tests/test_pyboy_link_session_roms.py::test_red_yellow_battle_turn_is_resolved
```

They require the pinned source-compatible PyBoy runtime, ROM-specific Cable
Club fixtures, and matching symbols. The trade case compares the complete
game-owned party-mon records before and after the exchange; the battle case
uses a pre-generated legal three-mon fixture and requires both sides to reach
move exchange and turn execution. It does not require the optional damage
calculation hook. Neither case writes party or battle state to make the
assertion pass. A skipped or partially parameterized matrix is not full
Red/Blue/Yellow coverage.

### Tier E: remote transport and subprocess acceptance

First run the transport/serial milestones:

```bash
python -m pytest -q -ra \
  tests/test_link_integration_remote.py
```

This module exercises remote TCP plumbing and selected real-ROM milestones.
Some cases stop at LinkMenu or use controlled menu/fixture setup; that is not
the same as a user-driven full trade or battle.

The separate-process acceptance uses the same bundled runtime as the normal
package and explicit color Red/Blue fixtures:

```bash
python -m pytest -q -ra \
  tests/test_pyboy_link_session_subprocess.py
```

The LinkMenu test is a transport smoke test. The strict subprocess trade test
also compares complete party-mon records, and the strict subprocess battle
test requires both processes to reach move exchange and execution. The current
tests use a hook in each test driver to select the same LinkMenu mode; that
controlled setup is not a user-driven menu acceptance. Once setup is complete,
the trade and battle payloads travel through native bit-level serial traffic,
and the tests reject the out-of-band exchange counter. Their live-serial driver
uses cooperative phase rendezvous so a waiting process continues servicing
serial IRQs. Record both child traces and the exact deadline when investigating
a regression.

## 5. Generate link fixtures safely

Save states are emulator artifacts and are tied to the exact ROM bytes. Keep
them local under `tests/fixtures/link/<version>/` and never commit them.

The producer is:

```bash
python scripts/produce_cable_club_fixture.py --help
```

Pass a source state captured against the same ROM variant, rather than relying
on automatic sibling-worktree discovery. Example:

```bash
python scripts/produce_cable_club_fixture.py \
  --version yellow \
  --variant cgb \
  --source <yellow-cerulean-pc.state> \
  --rom rom/yellow/pokemon-yellow.gbc \
  --sym rom/yellow/pokemon-yellow.sym \
  --out tests/fixtures/link/yellow/cable_club.state
```

For stock Red or Blue use `--variant vanilla`, a vanilla-ROM-captured source,
and an output named `cable_club-vanilla.state`. For the default Red/Blue color
variant use `--variant color` and `cable_club.state`. There is no separate
Yellow producer. Successful fixture generation proves only that the state
lands at the producer's expected map/tile; it does not prove a trade or battle.

The producer does not create `cable_club-battle.state`. Capture a separate
legal three-mon battle-start state for each exact ROM and place it beside the
ordinary fixture with that filename. Do not repair a fixture by writing party,
battle, or link state after capture; if a state was captured against another
ROM variant, regenerate it. The producer currently enables its own diagnostic
SHA-1 bypass while creating a state; never carry `POKERED_SKIP_SHA1=1` into
the acceptance or release commands.

## 6. Launch MCP explicitly

Use explicit paths and the matching hash. The server also indexes the
per-ROM `Path`/`SHA-1` rows in `VERSIONS.md` and fails closed when no matching
pin exists:

```bash
POKERED_ROM_PATH=rom/red/pokemon-red-color.gb \
POKERED_SYM_PATH=rom/red/pokemon-red.sym \
POKERED_ROM_SHA1=e1deed63080bc24cad5fba18ecb3184f905d16d4 \
python -m pokered_harness.mcp_server
```

For an in-process peer, add all three `POKERED_PEER_*` variables with the
peer's matching paths and hash. The peer is created at startup but must be
paired explicitly with `link_pair`; it is not proof of a working game flow.

For a normal MCP lifecycle, initialize the server, list tools, and then use
`step`, `press`, state resources, and save/load as needed. For a local pair,
call `link_pair`, use `link_step`, and finish with `link_unpair`. For TCP,
call `link_listen` on the internal-clock side or `link_connect` on the
external-clock side, poll `link_status` until `remote_mode=connected`, and
call `link_disconnect` before closing either server. A teardown is complete
only when status is idle and no child peer, worker thread, socket, or callback
remains.

The checked-in `.mcp.json` uses the canonical `rom/red/` layout, an explicit
color-ROM hash, and no machine-local `PYTHONPATH`. It is suitable for a
workspace whose MCP client expands `${PWD}` and whose installed interpreter
is the package environment. A wheel launched outside a checkout can omit
`VERSIONS.md` when it supplies explicit primary and peer SHA-1 values; the
bundled PyBoy runtime identity is still checked.

The MCP remote TCP tools enforce localhost-only hosts (`127.0.0.1`,
`localhost`, or `::1`). They provide no authentication or encryption; do not
expose the raw transport or server to a public address, untrusted LAN, or WAN.

## 7. Record release evidence

For every required result, record:

- repository commit and clean-worktree status;
- Python executable/version and PyBoy package/version/build mode;
- operating system and CPU/GPU details when timing is relevant;
- exact ROM and symbol paths plus SHA-1 values;
- fixture filenames, hashes, source-ROM identity, and generation command;
- exact pytest commands, full output, duration, skips, xfails, and timeouts;
- MCP launch environment with secrets and personal paths removed;
- remote listener/connector roles, address scope, and teardown result; and
- known limitations or deviations from the checklist.

Do not summarize a skipped matrix as “all versions passed,” and do not report a
transport or LinkMenu milestone as a completed trade or battle.

## 8. Current sign-off blockers

The candidate remains `PARTIAL`, not `PRODUCTION-READY`. The observed blockers
are:

1. The strict Red↔Yellow local trade and battle cases now pass with the
   normalized scheduler, but the full current local/remote gate and the
   broader Blue/Red/Yellow battle matrix are not yet certified. Blue↔Blue and
   Blue→Red remain diagnostic only.
2. The full real-ROM gate passed at the audited implementation revision, but
   its complete output is not retained in this repository as a release
   evidence bundle.
3. `ruff check .` reported 546 findings, and the broad suite is not a clean
   production gate.
4. The five-row single-session matrix, reversed listener/connector roles,
   native-platform builds, battle-fixture provenance, and an independent review
   remain incomplete.
5. Remote TCP has no authentication or encryption. Loopback-only operation is
   enforced and is the only supported network boundary; cross-host operation is
   blocked until secure transport is added.

The smallest next actions are to finish the current local/remote reruns,
capture and retain complete implementation-revision gate output plus
battle-fixture provenance, decide and test the supported per-ROM/role matrix,
resolve or
explicitly scope the lint gate, and obtain independent/native-platform review.
