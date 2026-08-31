# Version and asset pins

This file records the candidate runtime and ROM inputs used by the harness. A
pin identifies bytes or a dependency version; it is not, by itself, a release
certification. The repository does not distribute ROMs, symbol files, save
states, or other ROM-derived artifacts.

Status: `PARTIAL` current audit candidate; the current source boundary is
`c3c1d8e` (2026-08-31). Its asset-free production gate collected 594 tests,
passed unit 469/469, and passed timing 35/35 across five repetitions. The
gate's scoped result is `PASS`; it does not run ROM-backed tiers. Its matrix
audit collected all nine ordered pairs, six reversed-role rows, nine local
variant rows, and two strict trade plus two strict battle entry points, but
the strict acceptance declaration is incomplete and matrix runtime was not
run. Lane B's final matrix report is still required.

A previous controlled real-ROM snapshot at `3aad196` recorded local 46/46,
remote 13/13, strict trade 2/2, strict battle 2/2, and the canonical
color-Red/Yellow and color-Red/color-Blue roles. Those counts are historical
evidence and are not a current `c3c1d8e` full-gate sign-off. The historical
complete real-ROM snapshot at
`1046a541e0003923aec6000b6b383c6eaafeaa48` is also separate evidence.

Symbol hashes and fixture byte/provenance records are recorded below and in
[`release-evidence/fixture-manifest.json`](release-evidence/fixture-manifest.json).
The remaining release decision is `PARTIAL` because strict matrix declaration
and runtime coverage, vanilla source provenance, broad-suite/product-lint
closure, reversed-role and native-platform coverage, retained complete
evidence, and independent review are incomplete. The current product Ruff
check reports seven findings under the configured product boundary.

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
| Role | Color-Red input for the passing local Red/Yellow role and canonical remote listener role; overall release support remains partial |

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
| Role | Color-Blue input for the canonical remote connector role; isolated trade evidence only |

### Pokémon Yellow (UE)

| Field | Value |
|---|---|
| SHA-1 | `cc7d03262ebfaf2f06772c1a480c7d9d5f4a38e1` |
| Size | 1,048,576 bytes |
| Path | `rom/yellow/pokemon-yellow.gbc` |
| Symbols | `rom/yellow/pokemon-yellow.sym` |
| Symbol SHA-1 | `7c4205723943e7722230dcf014e5e8a2012474aa` |
| Role | Yellow session input and passing local Red/Yellow acceptance peer |

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

The bounded, pinned ordinary-fixture producer is
[`scripts/produce_cable_club_fixture.py`](scripts/produce_cable_club_fixture.py).
It requires a matching, user-generated `cerulean_pc.state` source and accepts
explicit `--version`, `--variant`, `--source`, `--rom`, `--sym`, and `--out`
arguments. It validates ROM and symbol hashes plus the bundled PyBoy version
and fork revision from this file. Its default wall-clock budget is 180 seconds
and its default movement budget is 64 directional inputs; either budget can be
overridden explicitly. There is no separate Yellow producer. A successful
output only establishes a fixture at the expected map/tile; it does not prove
that a trade or battle works.

The strict local acceptance fixtures and remote acceptance/diagnostic fixtures are
distinct from the default Cable Club fixtures:

| Path | ROMs | Required fixture files |
|---|---|---|
| Local strict trade | color Red + Yellow | `red/cable_club.state`, `yellow/cable_club.state` |
| Local strict battle | color Red + Yellow | `red/cable_club-battle.state`, `yellow/cable_club-battle.state` |
| Remote isolated trade diagnostic | color Red listener + color Blue connector | `red/cable_club.state`, `blue/cable_club.state` |
| Remote no-hook battle smoke | color Red listener + color Blue connector | `red/cable_club-battle.state`, `blue/cable_club-battle.state` |

The tracked
[`scripts/prepare_battle_cable_club_fixtures.py`](scripts/prepare_battle_cable_club_fixtures.py)
creates a separate derived battle state from an ordinary state. It validates
the selected ROM, creates a legal multi-mon party, and the acceptance runner
loads the resulting immutable bytes without preparing or mutating party state
at runtime. This is deterministic fixture preparation, not proof of a human
captured battle state.

The tracked
[`release-evidence/fixture-manifest.json`](release-evidence/fixture-manifest.json)
is the source of truth for fixture sizes, SHA-1/SHA-256 values, expected ROM
and symbol pins, source-state hashes, runtime identity, and capture command
templates. The states remain operator-managed, ignored, and outside the
repository. Its current entries are:

| Fixture | Observed SHA-1 | Provenance boundary |
|---|---|---|
| `red/cable_club.state` | `546d7edaf7c3a987f86ae86a97066c7d619eefbb` | `verified`, canonical color-Red ordinary reproduction |
| `red/cable_club-vanilla.state` | `affd77c20bf4600b8057ea33683ed58a2ac86157` | `partial`, retained source is not proven vanilla-ROM captured |
| `red/cable_club-battle.state` | `4343278b018187ed4bf7c4eda3b202097456f048` | `verified`, derived from canonical color-Red ordinary state |
| `red/cable_club-battle-vanilla.state` | `63d00b469b07e3969bcd82be1047031265be0b3a` | `partial`, derived from partial vanilla ordinary provenance |
| `blue/cable_club.state` | `0809d2f8e514c7fb714a73a7b13f120b26a40c38` | `verified`, canonical color-Blue ordinary reproduction |
| `blue/cable_club-vanilla.state` | `cff5349b55f8fdf47a5af63d674977103aeb44d6` | `partial`, retained source is not proven vanilla-ROM captured |
| `blue/cable_club-battle.state` | `6aac3aabe0f6ad662dec218682c2254b3954a10a` | `verified`, derived from canonical color-Blue ordinary state |
| `blue/cable_club-battle-vanilla.state` | `442c1497c0398f7e54ceade9739ddd1b5b9456fc` | `partial`, derived from partial vanilla ordinary provenance |
| `yellow/cable_club.state` | `37df4dbdb512cd3febdc2d536281683476291d3b` | `verified`, canonical Yellow ordinary reproduction |
| `yellow/cable_club-battle.state` | `78c7d0b32006b11baa9ac9c71efc73b0a8d807b9` | `verified`, derived from canonical Yellow ordinary state |

The canonical color Red, color Blue, and Yellow fixture rows have byte-level
reproduction evidence. The vanilla ordinary reproduction attempt was bounded
and did not establish that the retained source was captured against the
vanilla ROM; vanilla rows therefore remain `PARTIAL` and are not supported
release rows. Manifest byte validation still requires every listed entry when
the manifest is checked with `--fixture-root`.

The repository has strict local Red/Yellow trade and battle entry points and
strict remote color-Red/color-Blue trade and battle entry points, plus broader
diagnostic matrices. Their prior passing snapshots do not certify the
uncovered ordered pairs or reversed roles. Consult the test-surface table in
the [README](README.md) and run the required tiers in the
[production runbook](docs/PRODUCTION_RUNBOOK.md) before using any row as
release evidence.

## Performance

No machine-specific throughput floor is pinned here. Performance evidence must
record the exact commit, Python/PyBoy build, host, render mode, workload, warmup
policy, and sample distribution. A single local timing is not a release gate.
