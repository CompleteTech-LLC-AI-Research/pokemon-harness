# Production runbook

This runbook defines how to prepare and evaluate a clean `pokered-harness`
checkout. The baseline at commit `e219fb5` was not certified; the committed
release-audit candidate adds the bundled runtime, native link lifecycle, and
tiered gate. The release decision is recorded in the [release checklist](RELEASE_CHECKLIST.md).

Current evidence boundary: the latest pinned-interpreter gate reports unit
363/363, timing 35/35 across five repetitions, local 46/46, remote 11/11,
trade 2/2, and battle 2/2. The strict local Red/Yellow trade and battle cases
pass with untouched legal fixtures. The independent-process Red/Blue trade and
battle cases pass through native bit-level serial traffic; trade compares both
full party records and battle advances through move exchange and execution on
both processes. These results establish the certified candidate scope. Symbol
hashes and generator provenance are recorded in `VERSIONS.md`. Overall release
status remains `PARTIAL` until the evidence bundle and independent review are
attached; failed or unrun ROM-pair rows remain unsupported.

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

Compare every ROM used by a test or launch to the corresponding SHA-1 in
[`VERSIONS.md`](../VERSIONS.md). For example:

```bash
sha1sum rom/red/pokemon-red.gb rom/red/pokemon-red-color.gb
sha1sum rom/blue/pokemon-blue.gb rom/blue/pokemon-blue-color.gb
sha1sum rom/yellow/pokemon-yellow.gbc
```

The matching `.sym` file must come from the matching game source/build. Record
the symbol-file hash, source commit, RGBDS version, and build flags in the
release evidence. A symbol file being readable is not proof that its labels
match the ROM.

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

The current candidate has not passed a repository-wide Ruff audit: `ruff check
.` reports 525 findings, including legacy and vendored-runtime code. Treat
lint cleanup as a remaining release task even when the production gate is
green.

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

Repeat with the appropriate path and hash for each release input. These tests
prove only the tested boot/state/MCP surface; they do not prove link gameplay.

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
scope. The current audit reached LinkMenu and completed the diagnostic trade
route for all nine R/B/Y version orderings. Seven of nine diagnostic battle
rows reached a complete turn; `blue↔blue` and the `blue→red` attach ordering
did not reach both move-exchange hooks. The strict local cases are:

```bash
python -m pytest -q \
  tests/test_pyboy_link_session_roms.py::test_red_yellow_trade_swaps_real_party_records \
  tests/test_pyboy_link_session_roms.py::test_red_yellow_battle_turn_is_resolved
```

They require the pinned source-compatible PyBoy runtime, ROM-specific Cable
Club fixtures, and matching symbols. The trade case compares the complete
game-owned party-mon records before and after the exchange; the battle case
uses a pre-generated legal three-mon fixture and requires both sides to reach
move exchange, execution, and damage calculation. Neither case writes party
or battle state to make the assertion pass. A skipped or partially
parameterized matrix is not full Red/Blue/Yellow coverage.

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
test requires both processes to reach move exchange and execution. Both pass
naturally with the bundled runtime. Their live-serial driver uses cooperative
phase rendezvous so a waiting process continues servicing serial IRQs; it does
not replace game-owned trade or battle bytes with a semantic shortcut. Record
both child traces and the exact deadline when investigating a regression.

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
