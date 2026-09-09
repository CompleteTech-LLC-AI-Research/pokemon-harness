# poke-harness meta prompt

## Operating model
- Follow strict execution order: confirm project intent, set up environment, validate assets, then start/drive MCP.
- Prefer deterministic, reproducible commands rooted at the active checkout; do not embed machine-specific paths.
- Preserve all safety and policy boundaries from README (no ROM redistribution, no test artifacts committed).

## Collaboration behavior
- When blocked by missing ROM/SYM files, stop and ask for explicit user input instead of fabricating values.
- If a command or tool fails, report the exact command, error text, and next remediation step.
- Keep prompts actionable and short; avoid speculative claims about simulation outcomes.

## Verification policy
- Treat `README.md` as the operational source-of-truth for supported targets and run modes.
- Treat `VERSIONS.md` as the canonical map for SHA-1 values.
- Treat `.mcp.json` as the canonical MCP launcher contract.

## Completion checks
- Mark a run complete only when all of the following are true:
  - Environment is initialized (`.venv`, dependencies installed).
  - All required BYO files are present for the selected game mode.
  - MCP server or MCP tool flow is invoked according to mode selection.
  - Output includes mode-specific success or explicit blocker.
