# Version and asset pins

This file records the candidate runtime and ROM inputs used by the harness. A
pin identifies bytes or a dependency version; it is not, by itself, a release
certification. The repository does not distribute ROMs, symbol files, save
states, or other ROM-derived artifacts.

Status: `PARTIAL` current audit candidate through `ab89c39` (2026-08-31).
The fast gate is unit 414/414 and timing 35/35 across five repetitions. The
stateful evidence was collected at the code-equivalent `2ac09fb` boundary:
local 46/46 and remote 11/11 passed for the canonical color-Red listener and
color-Blue connector roles. An isolated remote Red/Blue trade passed 1/1 in
229.37 seconds, but the same trade timed out under concurrent stateful load.
The historical complete real-ROM snapshot at
`1046a541e0003923aec6000b6b383c6eaafeaa48` remains separate evidence, not
current sign-off. Uncommitted worktree changes are excluded.

The historical full-gate snapshot is unit 372/372, timing 35/35 across five
repetitions, local 46/46, remote 11/11, strict trade 2/2, and strict battle
2/2. Current local Red/Yellow trade and battle pass, while the remote battle
case is only a controlled native-serial diagnostic because its driver selects
LinkMenu through `_install_linkmenu_autoselect` and
`_force_linkmenu_selection`. Other link rows remain unsupported or unverified.

Symbol hashes and audited generator provenance for the inputs are recorded
below. The remaining release decision is `PARTIAL` because the complete
implementation-revision gate output is not retained as an evidence bundle,
repository-wide lint and the broad matrix are not clean, per-ROM and
reversed-role coverage is incomplete, and independent review/native-platform
evidence is missing.

## Runtime

| Component | Pin | Source of truth |
|---|---|---|
| Python | `>=3.12` | `pyproject.toml` |
| PyBoy | `2.7.0` + fork `c565df66c3731fad2856169a90f6bbec99925915` | `vendor/pyboy-src/POKERED_HARNESS_PYBOY_REVISION` and `pyproject.toml` |
| MCP | `1.29.1` | `pyproject.toml` and the stdio acceptance test |

The project distribution bundles the pinned PyBoy source runtime. It exposes
the Python-accessible `mb.serial` backend used by the bit-accurate link
coordinator and remote TCP transport. A pre-existing standalone PyBoy wheel
must not be allowed to shadow this package; verify the runtime identity before
release. See the [production runbook](docs/PRODUCTION_RUNBOOK.md).

## ROM pins

ROMs are BYO inputs and belong under `rom/<version>/`, which is gitignored.
The SHA-1 rows below are the pins currently recorded for the candidate files.
`pokered_harness.config.load_versions()` indexes rows by their documented
`Path`, so a launch can be checked against the selected ROM rather than the
first row in this file. Explicit `POKERED_ROM_SHA1` values remain preferred
for release evidence.

### Pokémon Red (UE)

| Field | Value |
|---|---|
| SHA-1 | `ea9bcae617fdf159b045185467ae58b2e4a48b9a` |
| Size | 1,048,576 bytes |
| Path | `rom/red/pokemon-red.gb` |
| Symbols | `rom/red/pokemon-red.sym` |
| Symbol SHA-1 | `03783c86a42588bd77f73bd7814cf8d70e590118` |
| Role | Hash-pinned single-session input; selected checks only; link gameplay not claimed |

### Pokémon Red color variant

| Field | Value |
|---|---|
| SHA-1 | `e1deed63080bc24cad5fba18ecb3184f905d16d4` |
| Size | 1,048,576 bytes |
| Path | `rom/red/pokemon-red-color.gb` |
| Symbols | `rom/red/pokemon-red.sym` only when verified against this variant |
| Symbol SHA-1 | `03783c86a42588bd77f73bd7814cf8d70e590118` |
| Role | Color-Red input for the certified local Red/Yellow pair and remote listener role |

### Pokémon Blue (UE)

| Field | Value |
|---|---|
| SHA-1 | `d7037c83e1ae5b39bde3c30787637ba1d4c48ce2` |
| Size | 1,048,576 bytes |
| Path | `rom/blue/pokemon-blue.gb` |
| Symbols | `rom/blue/pokemon-blue.sym` |
| Symbol SHA-1 | `c779a0628cfc97cc9ac9db2520a1e23a2d8b7ed6` |
| Role | Hash-pinned single-session input; selected checks only; link gameplay not claimed |

### Pokémon Blue color variant

| Field | Value |
|---|---|
| SHA-1 | `5f4b05725a860e04077045462176d3e2771c5022` |
| Size | 1,048,576 bytes |
| Path | `rom/blue/pokemon-blue-color.gb` |
| Symbols | `rom/blue/pokemon-blue.sym` only when verified against this variant |
| Symbol SHA-1 | `c779a0628cfc97cc9ac9db2520a1e23a2d8b7ed6` |
| Role | Color-Blue input for the certified remote connector role |

### Pokémon Yellow (UE)

| Field | Value |
|---|---|
| SHA-1 | `cc7d03262ebfaf2f06772c1a480c7d9d5f4a38e1` |
| Size | 1,048,576 bytes |
| Path | `rom/yellow/pokemon-yellow.gbc` |
| Symbols | `rom/yellow/pokemon-yellow.sym` |
| Symbol SHA-1 | `7c4205723943e7722230dcf014e5e8a2012474aa` |
| Role | Yellow session input and certified local Red/Yellow acceptance peer |

Other localisations, hacks, and variants are out of scope unless they receive
their own ROM hash, matching symbols, fixture provenance, and acceptance
result. File size alone does not establish compatibility.

## Symbol-file contract

Symbol files are generated inputs, not committed release assets. The audited
files byte-match the generated outputs identified in the provenance table
below. The harness loader checks that the requested labels can be parsed; it
does not establish the source provenance of an arbitrary `.sym` file.

The link symbol registry contains required, optional, and reserved labels in
`src/pokered_harness/link/symbols.py`. A symbol-file audit or real-ROM test must
be run for each version before claiming link support. No blanket cross-version
symbol-coverage claim is made here.

## Verified symbol provenance

The symbol files used for the 2026-08-30 audit were compared byte-for-byte
with the matching generated files in the local source checkouts before their
hashes were recorded above. The source repositories and build inputs were:

| Game symbols | Source commit | Generator/toolchain | Build flags |
|---|---|---|---|
| Red `pokered.sym` and Blue `pokeblue.sym` | `pret/pokered` `fbcf7d0e19a3a2db505440d3ccd3d40ca996c15c` | `rgblink` from RGBDS `v1.0.1` | `make DEBUG=1 red blue`; default `RGBASMFLAGS` plus `-Q8 -P includes.asm -E`, with `_RED`/`_BLUE` targets |
| Yellow `pokeyellow.sym` | `pret/pokeyellow` `bfa7170107eea23b89febb60bfb2ce39173bf2e1` | `rgblink` from RGBDS `v1.0.1` | `make DEBUG=1 yellow`; default `RGBASMFLAGS` plus `-Q8 -P includes.asm -E` |

The source repositories are `https://github.com/pret/pokered` and
`https://github.com/pret/pokeyellow`. The color ROMs use the corresponding
base-game symbols because the color patch preserves the symbol-address ABI;
their ROM hashes remain independently pinned above.

## Hash and version enforcement

`Session.from_files(..., expected_rom_sha1=...)` hashes the ROM and raises
`VersionMismatch` on a mismatch. `expected_symbol_sha1` performs the matching
symbol-file check, and `expected_pyboy_version` plus
`expected_pyboy_revision` verify the bundled PyBoy version and exact nonempty
fork revision when a caller supplies them. The MCP entry point:

1. uses `POKERED_ROM_SHA1` when set;
2. otherwise selects the SHA-1 whose `Path` matches the configured ROM; and
3. enforces the matching symbol SHA-1 and exact PyBoy revision when
   `VERSIONS.md` is available; and
4. fails closed when no matching pin is available, unless
   `POKERED_SKIP_SHA1` is explicitly set for diagnostics.

An explicit hash is still required by release policy, and a release run must
not set `POKERED_SKIP_SHA1=1`.

## Link and fixture boundary

Save states are tied to the exact ROM bytes and symbol layout used to capture
them. A fixture named `cable_club.state` is therefore not interchangeable
between stock, color-variant, and Yellow inputs. Keep fixtures local under
`tests/fixtures/link/<version>/`; do not commit them.

The existing producer is
[`scripts/produce_cable_club_fixture.py`](scripts/produce_cable_club_fixture.py).
It requires a matching, user-generated `cerulean_pc.state` source and accepts
explicit `--version`, `--variant`, `--source`, `--rom`, `--sym`, and `--out`
arguments. There is no separate Yellow producer. The producer's successful
output only establishes a fixture at the expected map/tile; it does not prove
that a trade or battle works.

The strict acceptance fixtures are distinct from the default Cable Club
fixtures:

| Acceptance path | ROMs | Required fixture files |
|---|---|---|
| Local strict trade | color Red + Yellow | `red/cable_club.state`, `yellow/cable_club.state` |
| Local strict battle | color Red + Yellow | `red/cable_club-battle.state`, `yellow/cable_club-battle.state` |
| Remote strict trade | color Red listener + color Blue connector | `red/cable_club.state`, `blue/cable_club.state` |
| Remote strict battle | color Red listener + color Blue connector | `red/cable_club-battle.state`, `blue/cable_club-battle.state` |

The battle files are separately captured legal states; the existing producer
does not create them. Every state must be captured from the exact ROM bytes it
loads, and the release record must include each fixture hash, source-state
provenance, runtime identity, and capture command. The three default fixture
hashes observed in the full-gate audit were:

| Fixture | Observed SHA-1 | Evidence boundary |
|---|---|---|
| `red/cable_club.state` | `546d7edaf7c3a987f86ae86a97066c7d619eefbb` | External fixture used by the audit; not committed |
| `blue/cable_club.state` | `0809d2f8e514c7fb714a73a7b13f120b26a40c38` | External fixture used by the audit; not committed |
| `yellow/cable_club.state` | `37df4dbdb512cd3febdc2d536281683476291d3b` | External fixture used by the audit; not committed |

Battle-fixture hashes and complete source-state provenance were not retained in
this tree, so fixture provenance remains an open release item. Vanilla
variant fixtures (`cable_club-vanilla.state` and
`cable_club-battle-vanilla.state`) are also not part of the certified scope.

The current repository has diagnostic local/remote matrices plus strict local
Red/Yellow trade and battle acceptance tests and strict independent-process
Red/Blue trade and battle acceptance tests. The isolated remote trade passes
in the bundled source-compatible runtime, including full party-record equality,
but its concurrent-tier timeout leaves load stability open. The remote battle
case reaches native move exchange/execution, yet its deterministic LinkMenu
RAM/hook selector makes it controlled diagnostic evidence rather than a
user-driven acceptance. Consult the test-surface table in the
[README](README.md) and run the required tiers in the
[production runbook](docs/PRODUCTION_RUNBOOK.md) before using any other row as
release evidence.

## Performance

No machine-specific throughput floor is pinned here. Performance evidence must
record the exact commit, Python/PyBoy build, host, render mode, workload, warmup
policy, and sample distribution. A single local timing is not a release gate.
