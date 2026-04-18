# pokered-harness

Memory-first automation harness for **Pokémon Red (UE)** on **PyBoy 2.7.0**,
exposed over MCP. See [`plans/deep-research-report-glittery-rossum.md`](../.claude/plans/deep-research-report-glittery-rossum.md)
for the ADR that motivates this architecture.

## BYO-ROM

This repo does **not** contain a Pokémon Red ROM and does **not** contain any
game-derived build artifact (no `.gb`, no `.sym`, no `.map`, no save states
of commercial game content). You must provide your own legally obtained copy.

Place your ROM at `rom/pokemon-red.gb` (gitignored). Then record its SHA-1
in [`VERSIONS.md`](VERSIONS.md) — the session manager refuses to run without
a match.

## Prerequisites

1. **Python 3.11+**.
2. **PyBoy 2.7.0** — pinned in `pyproject.toml`.
3. **A `pokered.sym` file** generated from a local checkout of
   [`pret/pokered`](https://github.com/pret/pokered) built with `DEBUG=1`.
   Place it at `rom/pokemon-red.sym` (also gitignored).

### Generating `pokered.sym`

You need [RGBDS](https://rgbds.gbdev.io/) installed, then:

```bash
git clone https://github.com/pret/pokered.git
cd pokered
make DEBUG=1
# produces pokered.sym and pokered.map
```

Copy `pokered.sym` into this project's `rom/` directory and record the
`pret/pokered` commit SHA in [`VERSIONS.md`](VERSIONS.md).

## Install

```bash
python -m venv .venv
. .venv/Scripts/activate   # Windows bash; use .venv/bin/activate on Unix
pip install -e ".[dev]"
```

## Test

```bash
pytest
```

## Running

MCP server entry point is not wired yet — see the ADR's action items.
Early harness calls live under `src/pokered_harness/`.

## Scope (v1)

- Stock English Pokémon Red (UE) only.
- Blue, Yellow, JP Red, colorized forks, and ROM hacks are out of scope.

## License

The harness code in this repo is LGPL-3.0-only to match PyBoy's license.
No game-derived assets are distributed.
