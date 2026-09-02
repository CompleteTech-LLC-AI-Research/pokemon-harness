# Fixture provenance and portability audit

Audit scope: the published `master` at
`73c0b8cd98da073d66157c0b7a5b24f302beb5bb` and only the tracked manifest,
producer/generator sources, packaging metadata, and platform-facing
configuration. ROMs, symbol files, save states, and machine-local evidence
were not copied into the audit checkout.

## Decision

The repository is not production-ready for the remaining gameplay, provenance,
load, and full native-platform requirements.

The tracked record is internally consistent, but it does not establish the
missing vanilla source history or full native-platform qualification. The
clean-install evidence below includes Linux/Python 3.12 source-runtime
evidence and a scoped Windows/Python 3.12.10 validation; neither establishes
the complete native gameplay and matrix contract.

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
- A fresh Windows Python 3.12.10 environment in an isolated checkout passed
  editable installation, `pip check`, source bootstrap, the pinned Cython
  build/check, scoped runtime/fixture checks (23 passed, one unrelated skip),
  MCP stdio integration (4/4), and three canonical color-Red, color-Blue, and
  Yellow Cython attach/step/close smokes (3/3). This is scoped platform
  evidence; it does not prove strict Cython gameplay, concurrent real-ROM
  load, remote trade/battle acceptance, or macOS support.

## Portability and clean-install findings

The following bounded checks passed:

```text
git ls-remote <repository> refs/heads/master                  -> exact published head 73c0b8c...
python3 scripts/validate_fixture_manifest.py --schema-only  -> PASS, 10 entries
uv lock --check                                             -> PASS
python3 -m compileall <scoped-files>                        -> PASS
scripts/network_concurrency_probe.py                        -> PASS, 8 probes × 5 repetitions
wheel archive inspection                                    -> PASS, 100 entries; no ROM/state/symbol/native or absolute entries
fresh wheel installation                                    -> PASS, dependency check and bundled PyBoy serial contract
module and console entrypoint without assets                 -> fail closed as expected, missing ROM configuration
```

The wheel is `py3-none-any` for the documented source runtime. The GitHub
workflow still runs on `ubuntu-latest` with Python 3.12 and has no Windows or
macOS job. The fresh Windows run independently executes install, MCP startup,
Cython build, and three-ROM lifecycle checks, but not the complete strict
gameplay or load matrix. `uv.lock` contains platform resolution markers, but
lock metadata is not native-platform execution evidence.

`.mcp.json` uses `python` and `${PWD}`-relative asset paths. This is portable
only when the MCP client expands `${PWD}` to the checkout root and selects the
same interpreter used for installation. That client behavior is documented,
not established by the Windows run, which supplied explicit paths and
environment variables, or by a macOS run. The optional Cython path additionally
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
2. Choose the supported native platform set. Windows now has scoped
   clean-install, bootstrap, MCP, and Cython lifecycle evidence, but the full
   gameplay/load matrix is still required before retaining a production
   Windows claim. Either narrow the advertised contract to the platforms
   actually tested, or add complete checks for every retained target, including
   any Unix/macOS target intended for release.
3. Keep external asset validation and native-platform results as separate
   evidence bundles; neither a manifest hash nor a Linux wheel install proves
   gameplay or portability on another platform.

Smallest next action: obtain one provenance-complete vanilla Red or Blue source
state and its capture record, then rerun the bounded producer and byte
validator. The native-platform decision can proceed independently.
