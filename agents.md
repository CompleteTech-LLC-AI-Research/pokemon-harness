# Agents: poke-harness

This repository is a source-only Pokémon Red/Blue/Yellow PyBoy MCP harness.
It does not contain ROMs, symbol files, save states, or other ROM-derived
assets. Keep those inputs outside version control and use only legally obtained
files whose hashes match [`VERSIONS.md`](VERSIONS.md).

## Portable setup

Run these commands from the repository root (the path can be anywhere):

```bash
python -m venv .venv
. .venv/bin/activate                 # WSL, Linux, or Git Bash
# PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pip check
python scripts/bootstrap_pyboy.py --mode source --check
```

The supported release contract is Python `>=3.11`, `mcp==1.29.1`, and the
bundled PyBoy source runtime at the revision recorded in `VERSIONS.md`. Use a
separate clean environment for a Cython build; do not mix a standalone PyBoy
wheel with the bundled runtime.

## BYO assets

Place matching inputs under the ignored `rom/` directory:

```text
rom/
├── red/
│   ├── pokemon-red.gb
│   ├── pokemon-red-color.gb
│   └── pokemon-red.sym
├── blue/
│   ├── pokemon-blue.gb
│   ├── pokemon-blue-color.gb
│   └── pokemon-blue.sym
└── yellow/
    ├── pokemon-yellow.gbc
    └── pokemon-yellow.sym
```

Real-ROM link checks also require operator-managed fixtures under
`tests/fixtures/link/<version>/`. Validate every supplied file against
the corresponding pin and record the exact runtime, fixture, and teardown
result.

## MCP launch

For a single Red color session, use the portable template in `.mcp.json`, or
launch explicitly:

```bash
POKERED_ROM_PATH=rom/red/pokemon-red-color.gb \
POKERED_SYM_PATH=rom/red/pokemon-red.sym \
POKERED_VERSIONS_PATH="$PWD/VERSIONS.md" \
POKERED_ROM_SHA1=e1deed63080bc24cad5fba18ecb3184f905d16d4 \
python -m pokered_harness.mcp_server
```

The MCP client must expand `${PWD}` to the workspace root and use the same
installed interpreter used for setup. The server is headless by default.
Peer variables (`POKERED_PEER_ROM_PATH`, `POKERED_PEER_SYM_PATH`, and
`POKERED_PEER_ROM_SHA1`) enable link-pair configuration; call the link tools
explicitly and disconnect/unpair during teardown.

## Verification boundary

Use `python scripts/production_gate.py --unit-only --repeat-timing 5` for the
asset-free check and the tiered commands in
[`docs/PRODUCTION_RUNBOOK.md`](docs/PRODUCTION_RUNBOOK.md) when BYO assets are
present. Unit, transport, or LinkMenu smoke tests do not establish a full
trade or battle. RAM-writing walkthrough shortcuts such as `--option-b` are
diagnostics only and must not be reported as human-valid gameplay.

Before calling a checkout release-ready, require a clean commit, a green
current gate, matching asset hashes, complete strict trade and battle rows,
bounded remote teardown, and retained evidence. The current integration
candidate is `PARTIAL`; see [`docs/RELEASE_CHECKLIST.md`](docs/RELEASE_CHECKLIST.md)
for open conditions.
