# Version and asset pins

This file records the candidate runtime and ROM inputs used by the harness. A
pin identifies bytes or a dependency version; it is not, by itself, a release
certification. The repository does not distribute ROMs, symbol files, save
states, or other ROM-derived artifacts.

Status: `PARTIAL` at merged head
`daa1d72f2cc559a6424067a9088dfae1b7b5f7bb` (2026-09-03). Earlier PR #35
introduced remote serial-edge dispatch at an explicit native instruction-batch
boundary. The current candidate also contains serial save-state, bootstrap
ownership and build-metadata cleanup, MCP teardown, gate-accounting, and
partial-initialization cleanup changes. This is a `PARTIAL` publication
candidate, not a `PRODUCTION-READY` release.

Current controlled evidence is scoped as follows. The focused source and Cython
transport/serial/PyBoy-link suite passes 110/110 in each runtime. An
asset-backed source trade gate recorded 19/19 ordered local and remote party
swaps. The historical native Cython strict-trade gate recorded 18/19. Exact-row
follow-ups have both passed and failed, including exact party-record exchange,
a party-record mismatch, and phase stalls, so native strict-trade reliability
is unproven. Timing-altered diagnostics are not acceptance evidence. The
latest native remote-battle acceptance lane exercised 3/9 direct strict rows
under a bounded 155-second pair deadline: one PASS for
Blue-color↔Blue-color, two FAIL results for Red-color↔Yellow and
Yellow↔Red-color, and six rows remain unrun. No test-only bypasses were used;
the full 19-entrypoint battle set remains unqualified.
The source run was a trade-tier run whose supervisor started before PR #35 was
published; its child runs loaded the current implementation, but it is not a
clean post-merge all-tier sign-off.

The latest verified candidate used the dual `--unit-only --repeat-timing 5`
gate on 2026-09-03 with separate source and Cython interpreters (`--python`
plus `--cython-python`). It collected 753 tests in each mode. Both passed unit
608/608 and timing 40/40 in all five repetitions; source reported
`python-source` and Cython reported `cython/native-extension`. The clone had no
ROM, symbol, or save-state assets, so fixture schema and matrix declaration
were checked but ROM gameplay was not run. An earlier environment-specific
585/586 ownership result is superseded for these isolated environments. Fresh
uv-managed source and native environments installed the candidate;
`uv pip check` passed in both,
and the native bootstrap verified both `pyboy` and `pokered-harness` package
owners. The vendored source PyBoy runtime remains the documented release
default; Cython is an optional diagnostic build. The host's bare `python3` still
lacks `ensurepip`,
so that alternate standard-library venv path remains open.

Prior integrated source and Cython transport slices passed remote 15/15 and
the Cython local/session slice passed 47/47. Those are scoped follow-up
results, not proof of current full native trade or battle parity. Vanilla
fixture provenance, real-ROM concurrent load, full native-platform
qualification, reproducible clean-install verification, independent review,
and authenticated/encrypted cross-host TCP remain open, so the release decision
remains `PARTIAL`. Remote TCP is enforced as loopback-only and is
unauthenticated and unencrypted; cross-host use is not supported until a secure
transport is added.

Historical PR #17 evidence collected 698 tests and passed unit 554/554, local
real-ROM 47/47, remote transport/MCP 15/15, strict trade 19/19, strict battle
19/19, and timing 40/40 in each of five repetitions with
`DEFAULT_MATRIX_WORKERS=1`. The PR #19 follow-up collected 699 tests and
passed the ROM-free 555/555 unit and 40/40 × 5 timing slice, focused
transport/MCP 167/167, bounded concurrency probe 8/8 × 5, and real-ROM remote
transport 15/15. These results are historical slices, not a full rerun of the
current merged implementation. Earlier post-PR #23 native failures at the
Cython `PyBoy.tick` ownership seam are superseded by PR #26/#27 and are not
current acceptance evidence.

PR #22 adds bounded MCP stdio unpair cleanup and fresh remote lifecycle
generation tracking. Its release-hygiene workflow passed; the full strict
post-change gameplay matrices remain open.

Symbol hashes and fixture byte/provenance records are recorded below and in
[`release-evidence/fixture-manifest.json`](release-evidence/fixture-manifest.json).
The remaining release decision is `PARTIAL` because a clean full current
strict matrix has not been recorded for both runtimes, vanilla source
provenance, broad-suite coverage, full native-platform qualification, real-ROM
load evidence, independent review, and authenticated / encrypted cross-host
TCP remain incomplete. `ruff check .` is clean only for the
configured product boundary; the broad legacy tree is not silently
reformatted.

## Runtime

| Component | Pin | Source of truth |
|---|---|---|
| Python | `>=3.12` | `pyproject.toml` |
| PyBoy | `2.7.0` + fork `c565df66c3731fad2856169a90f6bbec99925915` | `vendor/pyboy-src/POKERED_HARNESS_PYBOY_REVISION` and `vendor/pyboy-src/pyboy/__init__.py` |
| MCP | `1.29.1` | `pyproject.toml` and `uv.lock` |

The project distribution bundles the pinned PyBoy source runtime. It exposes
the Python-accessible `mb.serial` backend used by the bit-accurate link
coordinator and remote TCP transport. The optional Cython/native build passes
its compiled serial-contract probe and the focused 110/110 serial-link suite,
but the historical native strict-trade result is 18/19 and mixed exact-row
follow-ups leave its reliability unproven; the full current native battle
matrix is unqualified. A pre-existing standalone PyBoy wheel must not be
allowed to shadow this package; verify the runtime identity and selected mode
before release. See the
[production runbook](docs/PRODUCTION_RUNBOOK.md).

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
| Role | Canonical color-Red input; selected source/Cython transport checks are recorded, but strict trade/battle qualification remains incomplete |

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
| Role | Canonical color-Blue input; selected source/Cython transport checks are recorded, but strict trade/battle qualification remains incomplete |

### Pokémon Yellow (UE)

| Field | Value |
|---|---|
| SHA-1 | `cc7d03262ebfaf2f06772c1a480c7d9d5f4a38e1` |
| Size | 1,048,576 bytes |
| Path | `rom/yellow/pokemon-yellow.gbc` |
| Symbols | `rom/yellow/pokemon-yellow.sym` |
| Symbol SHA-1 | `7c4205723943e7722230dcf014e5e8a2012474aa` |
| Role | Canonical Yellow input; selected source/Cython transport checks and mixed native exact-row diagnostics are recorded, but strict trade/battle qualification remains incomplete |

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
distinct from the default Cable Club fixtures. The current canonical runtime
matrix uses the color Red, color Blue, and Yellow ordinary/battle rows below:

| Path | ROMs | Required fixture files | Evidence boundary |
|---|---|---|---|
| Local strict trade matrix | color Red, color Blue, or Yellow in every ordered pair | matching `cable_club.state` for each side | Recorded source gate: 9/9 local rows. The native strict-trade matrix has a historical 18/19 result; mixed exact-row follow-ups leave reliability unproven |
| Local strict battle matrix | color Red, color Blue, or Yellow in every ordered pair | matching `cable_club-battle.state` for each side | Historical source baseline passed 9/9 local rows; current full source rerun is not recorded, and native full battle coverage remains unqualified |
| Remote strict trade matrix | canonical color Red, color Blue, or Yellow listener/connector in every ordered pair | matching ordinary fixture for each side | Recorded source gate: 9/9 remote rows. Native follow-ups for the historical 18/19 matrix both passed and failed, including party mismatch and phase stalls |
| Remote strict battle matrix | canonical color Red, color Blue, or Yellow listener/connector in every ordered pair | matching battle fixture for each side | Historical source baseline passed 9/9 remote rows; latest native direct lane exercised 3/9 under a bounded 155-second pair deadline (one pass, two fail, six unrun), with no test-only bypasses; the full 19-entrypoint battle set remains unqualified |

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

The repository has strict local and remote entry points for every canonical
Red/Blue/Yellow ordered pair. The current source trade gate recorded 19/19
rows (9 local and 9 remote, plus the dedicated assertion). The native strict-
trade matrix has a historical 18/19 result, but exact-row follow-ups both
passed and failed, including a party-record mismatch and phase stalls, so its
reliability is unproven. The current native battle matrix has not been
qualified, and the current source battle matrix was not rerun as a complete
post-PR #35 gate. The PR #17 source-runtime 19/19 trade and 19/19 battle
results remain historical baseline evidence. Consult the test-surface table in
the [README](README.md) and run the required tiers in the [production runbook](docs/PRODUCTION_RUNBOOK.md)
before using any row as release evidence.

## Performance

No machine-specific throughput floor is pinned here. Performance evidence must
record the exact commit, Python/PyBoy build, host, render mode, workload, warmup
policy, and sample distribution. A single local timing is not a release gate.
