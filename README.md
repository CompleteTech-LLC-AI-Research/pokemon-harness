# Private game states and fixtures

This dedicated asset branch belongs only in the **private**
`CompleteDotTech/pokemon` repository. Do not publish this branch or make the
repository public while its history contains these assets.

The operator authorized private publication of saved game states and fixtures.
This is an exception to the source branches' source-only distribution policy;
do not merge this asset-only branch into a source or public release branch.

Contents preserve the original relative paths:

- `tests/fixtures/link/`: all ten pinned Red/Blue/Yellow link fixtures.
- `walkthrough_*/`: retained milestone, intermediate, and per-input save states.
- `artifacts/`: retained diagnostic and replay save states.

`rom/` includes the five pinned Red/Blue/Yellow ROMs (including both color
variants), their three matching symbol files, and retained adjacent save/RAM
files, explicitly authorized for private storage. Gold/Silver/Crystal inputs
are outside this harness's supported scope and are not included.
No screenshots, logs, or credentials are included.
Some saved states were produced with Option-B/RAM-writing diagnostics; their
presence is not proof of authentic gameplay or production readiness.

`fixture-manifest.historical.json` preserves the existing fixture hashes and
provenance. Its external/untracked distribution fields describe the original
source-only arrangement, before this private publication. Four vanilla-derived
fixture entries retain partial provenance; publication does not change that.

Clone this branch separately with authenticated Git access:

```sh
git clone --single-branch --branch private-game-states \
  git@github.com:CompleteDotTech/pokemon.git pokemon-private-states
```

Copy the required fixture paths into your source checkout without overwriting
existing files, then use that checkout's fixture validator. Copy the matching
`rom/` inputs as needed too. Keep these assets ignored in source
branches. This publication does not modify any Train checkout or its runner.
