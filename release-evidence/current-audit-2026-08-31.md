# Current audit: 2026-08-31

Status: `PARTIAL` — not production-ready.

This record describes the isolated candidate's functional boundary at
`d8198ef` (`d8198ef` transport/runtime hardening commit; this documentation
update follows). No ROM, symbol, fixture, save-state, or
secret is included here. External assets are supplied through the documented
ROM and fixture roots.

## Passing evidence

- `scripts/production_gate.py --unit-only --repeat-timing 5` passed both
  collection paths, unit `477/477`, and timing `35/35` in all five repeats
  (604 collected tests).
- With explicit ROM/SYM/SHA-1 inputs, MCP stdio and golden-path smoke checks
  exited successfully for Red stock, Red color, Blue stock, Blue color, and
  Yellow.
- The selected real-ROM local tier passed `46/46` in `878.66s`; the selected
  remote tier passed `13/13` in `48.54s`; strict trade passed `3/3` in
  `576.47s`; strict battle passed `3/3` in `646.94s`. These selected results
  were recorded at the prior functional boundary `db72be6` and require rerun
  after the current transport hardening.
- The fixture manifest schema and all 10 external fixture byte records passed
  validation. Canonical color Red, color Blue, and Yellow ordinary/battle
  bytes are recorded; vanilla source provenance remains partial.
- `uv lock --check`, `uv pip check`, `ruff check .`, compilation, and focused
  lifecycle/link tests passed. A clean exported editable install and wheel
  install also passed `pip check`, source-runtime bootstrap, and MCP stdio
  smoke (`3/3`).
- Native link selection now fails closed for a real PyBoy that lacks the
  bit-accurate serial contract. Network sends, public listener waits, and
  worker teardown use bounded deadlines; remote MCP callers can require an
  expected peer ROM label.

## Not signed off

- The strict declaration covers only three local/remote trade cases and three
  local/remote battle cases; the matrix audit reports 15 undeclared cases per
  operation. The collection-only audit is structural and does not run ROMs.
- The current-candidate local, remote, trade, and battle real-ROM tiers have
  not yet been rerun after `d8198ef`; the prior selected results are retained
  as historical evidence, not current sign-off.
- The full broad suite, native Windows/Cython attachment, load-stability
  reruns, and independent review remain incomplete.
- Vanilla ordinary fixture source provenance remains partial, and complete
  sanitized per-tier gate output is not retained in-tree.
- Cython mode does not expose the Python-visible serial contract required by
  the current link layer; source mode is the only verified production mode.
- TCP remains loopback-only and unauthenticated/unencrypted. Cross-host use is
  unsupported.

The candidate must remain `PARTIAL` until these boundaries are either closed
with evidence or explicitly excluded from the advertised product scope.
