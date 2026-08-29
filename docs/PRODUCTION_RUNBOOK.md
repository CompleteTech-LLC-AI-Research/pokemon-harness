# Production runbook

This runbook defines how to prepare and evaluate a clean `pokered-harness`
checkout. It is an evidence gate, not a claim that the current checkout has
passed it. The audit status for commit `e219fb5` is recorded in the
[release checklist](RELEASE_CHECKLIST.md).

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
python -m pip install -e ".[dev,mcp]"
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
PYTHONPATH=src python -m pytest --collect-only -q
PYTHONPATH=src python -m pytest -q -ra
```

The release gate requires collection to complete without errors. It also
requires no unexpected skips, xfails, failures, or timeouts in any tier marked
required below. Fixture-gated tests may be skipped during development, but a
skip is not a passing release result.

The audited committed tree has a collection determinism blocker because several
tests import `tests.*` while `tests/__init__.py` is not committed. The
`pytest` console-script invocation fails eight modules during collection in
this checkout; `python -m pytest` may collect them through namespace-package
behavior in some environments. Fix the package boundary in the test lane and
rerun both invocation forms before using any downstream result.

## 4. Run the evidence tiers

Run the tiers in order and save the complete output with the commit and
interpreter identity.

### Tier A: ROM-free behavior and runtime contract

This tier exercises configuration, state parsing, serial semantics, protocol,
transport, and symbol-loader behavior without commercial game assets:

```bash
PYTHONPATH=src python -m pytest -q \
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

These tests do not require commercial ROM bytes, but the current
`pokered_harness.link.serial_core` module re-exports PyBoy's native serial
implementation when PyBoy is installed. Link-related Tier A tests therefore
still require the compatible runtime described in `VERSIONS.md`; the stock
Cython wheel may fail them before any ROM is involved. A green Tier A result
does not establish emulator, MCP, trade, or battle compatibility.

### Tier B: one real session and MCP stdio

Use one explicit ROM hash. This example uses stock Red:

```bash
POKERED_ROM_PATH=rom/red/pokemon-red.gb \
POKERED_SYM_PATH=rom/red/pokemon-red.sym \
POKERED_ROM_SHA1=ea9bcae617fdf159b045185467ae58b2e4a48b9a \
PYTHONPATH=src python -m pytest -q -ra \
  tests/test_golden_paths.py \
  tests/test_mcp_stdio_integration.py
```

Repeat with the appropriate path and hash for each release input. These tests
prove only the tested boot/state/MCP surface; they do not prove link gameplay.

### Tier C: real symbol and local-link smoke

With all candidate ROMs and matching symbols available:

```bash
PYTHONPATH=src python -m pytest -q -ra \
  tests/test_link_symbols_real_roms.py \
  tests/test_link_integration.py
```

The local integration module's active matrix is fixture-gated and currently
covers selected Blue/Yellow pairings. Its pair-smoke test proves construction,
hook installation, a short step, and teardown; its trade test is the only
fixture-gated local round-trip in that module. Neither result should be
generalized to every ROM variant or to remote play.

### Tier D: candidate local link session, trade, and battle

The more extensive candidate suite is:

```bash
PYTHONPATH=src python -m pytest -q -ra \
  tests/test_pyboy_link_session_roms.py
```

It requires ROM-specific Cable Club states and a PyBoy build whose `mb` and
`mb.serial` objects can be swapped from Python. The stock Cython wheel may
cause the module to skip. The tests also contain diagnostic setup, including
fixture-dependent party preparation; review the exact test and fixture
provenance before promoting a result to a product acceptance claim. A skipped
or partially parameterized matrix is not full Red/Blue/Yellow coverage.

### Tier E: remote transport and subprocess candidate

First run the transport/serial milestones:

```bash
PYTHONPATH=src python -m pytest -q -ra \
  tests/test_link_integration_remote.py
```

This module exercises remote TCP plumbing and selected real-ROM milestones.
Some cases stop at LinkMenu or use controlled menu/fixture setup; that is not
the same as a user-driven full trade or battle.

The separate-process candidate requires an additional non-Cython PyBoy
environment in `.venv-noncython/` and a Yellow Cable Club fixture:

```bash
PYTHONPATH=src python -m pytest -q -ra \
  tests/test_pyboy_link_session_subprocess.py
```

This is the candidate path for two-process LinkMenu and full-trade assertions.
It is release evidence only when the exact runtime, ROM, symbols, fixtures,
deadlines, and teardown are recorded and both tests complete without skips.

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

Use explicit paths and the matching hash. This avoids the current fallback
behavior in which `mcp_server` reads only the first SHA-1 row in `VERSIONS.md`:

```bash
POKERED_ROM_PATH=rom/red/pokemon-red-color.gb \
POKERED_SYM_PATH=rom/red/pokemon-red.sym \
POKERED_ROM_SHA1=e1deed63080bc24cad5fba18ecb3184f905d16d4 \
PYTHONPATH=src python -m pokered_harness.mcp_server
```

For an in-process peer, add all three `POKERED_PEER_*` variables with the
peer's matching paths and hash. The peer is created at startup but must be
paired explicitly with `link_pair`; it is not proof of a working game flow.

The checked-in `.mcp.json` currently points at flat `rom/` paths rather than
the canonical `rom/red/`, `rom/blue/`, and `rom/yellow/` layout. Do not use it
for a release run until the packaging lane corrects and tests it.

Remote TCP link tools currently provide no authentication or encryption. Limit
them to loopback or a trusted private network. Do not expose them to a public
address, untrusted LAN, or WAN.

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
