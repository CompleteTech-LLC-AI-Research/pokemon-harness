# Current audit: 2026-08-31

Status: `PARTIAL` — not production-ready.

This record describes the isolated candidate's functional boundary at
`034e34e` (`d8198ef` transport/runtime hardening, Cython-safe serial control
typing, native cross-family startup clock-role negotiation, and fail-closed
cross-family battle-warp rendezvous). No ROM,
symbol, fixture, save-state, or secret is included here. External assets are
supplied through the documented ROM and fixture roots.

## Passing evidence

- `scripts/production_gate.py --unit-only --repeat-timing 5` passed both
  collection paths, unit `483/483`, and timing `35/35` in all five repeats
  (624 collected tests).
- With explicit ROM/SYM/SHA-1 inputs, MCP stdio and golden-path smoke checks
  exited successfully for Red stock, Red color, Blue stock, Blue color, and
  Yellow.
- The current local canonical battle matrix completed `9/9`. Exact-head spot
  checks passed Yellow-listener to Red-color and Blue-color remote trade in
  `240.32s` and `228.00s`. At `034e34e`, Yellow-listener to Blue-color remote
  battle completed a turn in `224.64s`; Yellow-listener to Red-color remote
  battle diverged at the pre-battle warp, with Red reaching Colosseum while
  Yellow remained in Cable Club. Repeated cross-family runs remain timing-
  sensitive, and the complete local/remote strict matrices remain pending.
- The fixture manifest schema and all 10 external fixture byte records passed
  validation. Canonical color Red, color Blue, and Yellow ordinary/battle
  bytes are recorded; vanilla source provenance remains partial.
- `uv lock --check`, `uv pip check`, `ruff check .`, compilation, and focused
  lifecycle/link tests passed. A clean exported editable install and wheel
  install also passed `pip check`, source-runtime bootstrap, and MCP stdio
  smoke (`3/3`).
- In a disposable Python 3.12 environment, the pinned PyBoy Cython wheel built
  successfully and `scripts/bootstrap_pyboy.py --mode cython --check` plus
  `pip check` passed. Cython mode still does not expose the Python-side
  motherboard serial attachment used by link acceptance.
- Native link selection now fails closed for a real PyBoy that lacks the
  bit-accurate serial contract. Network sends, public listener waits, and
  worker teardown use bounded deadlines; remote MCP callers can require an
  expected peer ROM label.

## Not signed off

- The strict declaration has 19 local/remote trade entrypoints and 19
  local/remote battle entrypoints, covering every ordered canonical
  Red/Blue/Yellow pair. The collection-only audit is structural and does not
  run ROMs; current gameplay execution of the complete matrix remains
  pending.
- The selected exact-head remote results above are retained as current spot
  evidence, not full sign-off. Repeated cross-family battle runs expose an
  open pre-battle warp/phase issue (the current driver now fails closed at the
  map boundary); it requires diagnosis or explicit product-scope treatment.
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
