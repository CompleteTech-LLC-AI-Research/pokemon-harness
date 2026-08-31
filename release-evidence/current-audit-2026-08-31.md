# Current audit: 2026-08-31

Status: `PARTIAL` — not production-ready.

This record describes the isolated candidate at
`52eac342c6709d8a8cc6bd90caa90c8343d25813` (`52eac34` documentation commit;
functional source boundary `db72be6`). No ROM, symbol, fixture, save-state, or
secret is included here. External assets are supplied through the documented
ROM and fixture roots.

## Passing evidence

- `scripts/production_gate.py --unit-only --repeat-timing 5` passed both
  collection paths, unit `472/472`, and timing `35/35` in all five repeats.
- The selected real-ROM local tier passed `46/46` in `878.66s`; the selected
  remote tier passed `13/13` in `48.54s`.
- Strict trade passed `3/3` in `576.47s`; strict battle passed `3/3` in
  `646.94s`. Both remote listener/connector directions passed for color Red
  and color Blue using native bit-level serial traffic.
- The fixture manifest schema and all 10 external fixture byte records passed
  validation. Canonical color Red, color Blue, and Yellow ordinary/battle
  bytes are recorded; vanilla source provenance remains partial.
- `uv lock --check`, `uv pip check`, `ruff check .`, compilation, and focused
  lifecycle/link tests passed. A clean exported editable install and wheel
  install also passed `pip check`, source-runtime bootstrap, and MCP stdio
  smoke (`3/3`).

## Not signed off

- The strict declaration covers only three local/remote trade cases and three
  local/remote battle cases; the matrix audit reports 15 undeclared cases per
  operation. The collection-only audit is structural and does not run ROMs.
- The full broad suite, all advertised single-session inputs, native
  Windows/Cython attachment, load-stability reruns, and independent review
  remain incomplete.
- Vanilla ordinary fixture source provenance remains partial, and complete
  sanitized per-tier gate output is not retained in-tree.
- Cython mode does not expose the Python-visible serial contract required by
  the current link layer; source mode is the only verified production mode.
- TCP remains loopback-only and unauthenticated/unencrypted. Cross-host use is
  unsupported.

The candidate must remain `PARTIAL` until these boundaries are either closed
with evidence or explicitly excluded from the advertised product scope.
