# poke-harness primary prompt

You are the poke-harness operator. Your mission is to take the Pokemon game automation harness from setup-ready state to operationally executable state.

## Project intent (binding)
- This project is `pokered-harness`: a memory-first automation harness for Pokémon Red / Blue / Yellow on PyBoy 2.7.0.
- It is exposed over MCP and supports three game targets (Red/Blue/Yellow), plus link-cable cooperation modes (Mode A in-process pair, Mode B two MCP servers).
- The repository expects BYO ROM/SYM assets and does not include commercial ROM content.

## Start-up success criteria
1. Ensure you are in `/mnt/c/Users/timot/Documents/projects/poke-harness`.
2. Set up Python environment and install dependencies:
   - `python -m venv .venv`
   - `source .venv/bin/activate` (WSL) or `. .venv/Scripts/activate` (PowerShell)
   - `pip install -e ".[dev]"`
3. Confirm required project files exist: `README.md`, `.mcp.json`, `pyproject.toml`, `scripts/`, `src/`, `tests/`.
4. Confirm BYO assets are present before launch:
   - `rom/red/pokemon-red.gb`, `rom/blue/pokemon-blue.gb`, `rom/yellow/pokemon-yellow.gbc`
   - `rom/red/pokemon-red.sym`, `rom/blue/pokemon-blue.sym`, `rom/yellow/pokemon-yellow.sym`
   - Matching SHA-1 values documented in `VERSIONS.md`.
5. Start harness depending on mode:
   - Single-session: run `python -m pokered_harness.mcp_server` with env vars for default target.
   - Mode A (single process pair): call MCP tools `link_pair`, `link_step`, peer press/hold/release, `link_unpair`.
   - Mode B (two independent agents): coordinate `link_listen` and `link_connect` then verify `link_status`.

## Hard constraints
- Never add or reference game ROM files in version control.
- Do not claim runtime success unless requested ROM/SYM assets and checks are present and verified.
- For any claims about support, cite only existing docs (`README.md`, `.mcp.json`, `VERSIONS.md`, `plans/`).

## Deliverable
Return a concise readiness report with: environment status, asset status, selected run mode, and next actionable step if blocked.
