# Version and asset pins

This file records the candidate runtime and ROM inputs used by the harness. A
pin identifies bytes or a dependency version; it is not, by itself, a release
certification. The repository does not distribute ROMs, symbol files, save
states, or other ROM-derived artifacts.

Status: audited 2026-08-29 against commit `e219fb5`.

## Runtime

| Component | Pin | Source of truth |
|---|---|---|
| Python | `>=3.11` | `pyproject.toml` |
| PyBoy | `2.7.0` | `pyproject.toml` and the `pyboy` dependency declaration |

The project dependency is the stock PyBoy wheel. The current link-session
prototype also needs Python-accessible `mb` and serial attributes, which the
stock Cython build does not expose in the observed environment. Until a
runtime lane packages or lands a compatible solution, link tests are not a
default-runtime release gate. See the
[production runbook](docs/PRODUCTION_RUNBOOK.md).

## ROM pins

ROMs are BYO inputs and belong under `rom/<version>/`, which is gitignored.
The SHA-1 rows below are the pins currently recorded for the candidate files.
The first SHA-1 row is intentionally the stock Red row because
`pokered_harness.config.load_versions()` currently returns the first matching
SHA-1 row for its legacy fallback behavior. Always pass
`POKERED_ROM_SHA1` explicitly when selecting any other row.

### Pokémon Red (UE)

| Field | Value |
|---|---|
| SHA-1 | `ea9bcae617fdf159b045185467ae58b2e4a48b9a` |
| Size | 1,048,576 bytes |
| Path | `rom/red/pokemon-red.gb` |
| Symbols | `rom/red/pokemon-red.sym` |
| Role | Candidate stock DMG Red input |

### Pokémon Red color variant

| Field | Value |
|---|---|
| SHA-1 | `e1deed63080bc24cad5fba18ecb3184f905d16d4` |
| Size | 1,048,576 bytes |
| Path | `rom/red/pokemon-red-color.gb` |
| Symbols | `rom/red/pokemon-red.sym` only when verified against this variant |
| Role | Candidate color-variant input; fixture-specific validation required |

### Pokémon Blue (UE)

| Field | Value |
|---|---|
| SHA-1 | `d7037c83e1ae5b39bde3c30787637ba1d4c48ce2` |
| Size | 1,048,576 bytes |
| Path | `rom/blue/pokemon-blue.gb` |
| Symbols | `rom/blue/pokemon-blue.sym` |
| Role | Candidate stock DMG Blue input |

### Pokémon Blue color variant

| Field | Value |
|---|---|
| SHA-1 | `5f4b05725a860e04077045462176d3e2771c5022` |
| Size | 1,048,576 bytes |
| Path | `rom/blue/pokemon-blue-color.gb` |
| Symbols | `rom/blue/pokemon-blue.sym` only when verified against this variant |
| Role | Candidate color-variant input; fixture-specific validation required |

### Pokémon Yellow (UE)

| Field | Value |
|---|---|
| SHA-1 | `cc7d03262ebfaf2f06772c1a480c7d9d5f4a38e1` |
| Size | 1,048,576 bytes |
| Path | `rom/yellow/pokemon-yellow.gbc` |
| Symbols | `rom/yellow/pokemon-yellow.sym` |
| Role | Candidate native CGB Yellow input; fixture and runtime gates required |

Other localisations, hacks, and variants are out of scope unless they receive
their own ROM hash, matching symbols, fixture provenance, and acceptance
result. File size alone does not establish compatibility.

## Symbol-file contract

Symbol files are generated inputs, not committed release assets. Generate them
from the matching `pret/pokered` or `pret/pokeyellow` source tree with debug
symbols enabled, then record the source commit, RGBDS/toolchain version, and
symbol-file SHA-1 in the release evidence. The harness loader checks that the
requested labels can be parsed; it does not establish the source provenance of
an arbitrary `.sym` file.

The link symbol registry contains required, optional, and reserved labels in
`src/pokered_harness/link/symbols.py`. A symbol-file audit or real-ROM test must
be run for each version before claiming link support. No blanket cross-version
symbol-coverage claim is made here.

## Hash and version enforcement

`Session.from_files(..., expected_rom_sha1=...)` hashes the ROM and raises
`VersionMismatch` on a mismatch. `expected_pyboy_version` performs the
corresponding PyBoy check when a caller supplies it. The MCP entry point:

1. uses `POKERED_ROM_SHA1` when set;
2. otherwise reads the first SHA-1 row from `VERSIONS.md` in the current
   working directory; and
3. skips the fallback if `POKERED_SKIP_SHA1` is set or `VERSIONS.md` is not
   found.

The last two behaviors are compatibility fallbacks, not release policy. A
release run must provide an explicit hash and must not set
`POKERED_SKIP_SHA1=1`.

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

The current repository has candidate local, remote, trade, and battle tests,
but their fixture/runtime gates can skip them. Consult the test-surface table
in the [README](README.md) and run the required tiers in the
[production runbook](docs/PRODUCTION_RUNBOOK.md) before using any of those
tests as release evidence.

## Performance

No machine-specific throughput floor is pinned here. Performance evidence must
record the exact commit, Python/PyBoy build, host, render mode, workload, warmup
policy, and sample distribution. A single local timing is not a release gate.
