# Fixture provenance and portability audit

Audit scope: the published `master` at
`dfc0b2a9a2273f61ed82a737bb644f7d82216189` and only the tracked manifest,
producer/generator sources, packaging metadata, and platform-facing
configuration. ROMs, symbol files, save states, and machine-local evidence
were not copied into the audit checkout.

## Decision

The repository is not production-ready for the remaining gameplay, provenance,
load, and native-platform requirements.

The tracked record is internally consistent, but it does not establish the
missing vanilla source history or native-platform execution. The clean-install
evidence below is Linux/Python 3.12 source-runtime evidence only.

## Evidence classification

### Confirmed from the current tracked tree

- `release-evidence/fixture-manifest.json` contains 10 external entries: six
  `verified` canonical color-Red, color-Blue, and Yellow entries, and four
  `partial` vanilla entries. No entry is marked `unknown`.
- Every manifest ROM pin and symbol pin matches the path-keyed record in
  `VERSIONS.md` (`10/10` ROM references and `10/10` symbol references).
- Every manifest producer path exists in the tracked tree, and the ordinary
  and battle recipe tables align with all 10 manifest entries (`10/10`).
- The vanilla ordinary rows have no retained runtime identity, capture time, or
  verification method. The vanilla battle rows retain derivation metadata but
  remain `partial` because their ordinary inputs remain `partial`.
- The manifest and provenance strings contain no machine-specific absolute
  paths. The clean checkout contains no tracked ROM, symbol, save-state, or
  generated native payload.

### Source-derived, not independently reproducible here

- The manifest records fixture sizes and SHA-1/SHA-256 values, and the
  historical audit records a byte-validation result, but the external state
  files are absent from this clean checkout. A schema pass cannot confirm the
  bytes. Byte validation therefore fails closed until an operator supplies the
  external fixture root.
- `VERSIONS.md` records the Red/Blue and Yellow source commits, RGBDS version,
  build flags, and symbol hashes. Those source trees and generated symbol
  outputs are not tracked here, so the symbol provenance is a recorded audit
  claim rather than a reconstruction performed in this checkout.
- The manifest's producer revisions match the commits that introduced the
  tracked ordinary and battle producer files. This establishes repository
  lineage, not that an external source state was captured with the claimed
  ROM.

## Portability and clean-install findings

The following bounded checks passed:

```text
git ls-remote <repository> refs/heads/master                  -> exact published head dfc0b2a...
python3 scripts/validate_fixture_manifest.py --schema-only  -> PASS, 10 entries
uv lock --check                                             -> PASS
python3 -m compileall <scoped-files>                        -> PASS
scripts/network_concurrency_probe.py                        -> PASS, 8 probes × 5 repetitions
wheel archive inspection                                    -> PASS, 100 entries; no ROM/state/symbol/native or absolute entries
fresh wheel installation                                    -> PASS, dependency check and bundled PyBoy serial contract
module and console entrypoint without assets                 -> fail closed as expected, missing ROM configuration
```

The wheel is `py3-none-any` for the documented source runtime. The GitHub
workflow runs on `ubuntu-latest` with Python 3.12; no Windows or macOS runner
executes the install, MCP startup, Cython build, or link tests. `uv.lock`
contains platform resolution markers, but lock metadata is not native-platform
execution evidence. The Cython build/contract and three-ROM lifecycle smoke
are recorded separately; they do not establish strict Cython gameplay.

`.mcp.json` uses `python` and `${PWD}`-relative asset paths. This is portable
only when the MCP client expands `${PWD}` to the checkout root and selects the
same interpreter used for installation. That client behavior is documented,
not established by a Windows/macOS run. The optional Cython path additionally
requires a platform compiler and headers; its build/contract checks do not
establish native real-ROM gameplay.

## Exact external evidence still required

1. For each stock Red and stock Blue ordinary fixture, provide a
   `cerulean_pc.state` captured from the exact pinned vanilla ROM, together
   with its source-state hashes, capture command, runtime/PyBoy identity, and
   capture or verification timestamp. Run the bounded producer with explicit
   `--variant vanilla`, compare the output hashes with the manifest, and retain
   the sanitized reproduction record. Regenerate the two vanilla battle states
   only from those verified ordinary inputs.
2. Choose the supported native platform set. Either narrow the advertised
   contract to the platforms actually tested, or run clean-install, bootstrap,
   MCP startup, and relevant source/Cython checks on every retained native
   target (at minimum the current Windows/PowerShell claim and any Unix/macOS
   target intended for release).
3. Keep external asset validation and native-platform results as separate
   evidence bundles; neither a manifest hash nor a Linux wheel install proves
   gameplay or portability on another platform.

Smallest next action: obtain one provenance-complete vanilla Red or Blue source
state and its capture record, then rerun the bounded producer and byte
validator. The native-platform decision can proceed independently.
