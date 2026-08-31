# Current audit: 2026-08-31

Status: `PARTIAL` — not production-ready.

This record describes the isolated candidate with functional source at
`25e231c`. The exact-head fast, local, and remote gates were rerun at that
boundary. The exact-head no-hook Red/Blue remote battle smoke also passed.
The exact-head trade tier finished 1/2 because its TCP subprocess exceeded its
720-second child bound. Intervening functional commits harden runtime
bootstrap, MCP lifecycle/cancellation, production-gate coverage, lint scope,
and evidence verification. No ROM, symbol, fixture, save-state, or secret
is included here.

## Passing evidence

- The locked exact-head unit gate passed `445/445` tests.
- Timing passed `35/35` cases across five repetitions.
- The gate preflight validated all five pinned ROM inputs, three symbol files,
  and three ordinary link fixtures by SHA-1.
- The exact-head stateful local tier passed `46/46` with real ROMs and matching
  fixtures.
- The exact-head canonical remote transport tier passed `11/11` for color Red as the
  listener/internal-clock and color Blue as the connector/external-clock.
- The local strict Red/Yellow trade and battle paths passed. The trade compares
  complete game-owned party records and the battle reaches a real move turn.
- The exact-head no-hook Red/Blue remote battle smoke passed with ordinary menu
  input and native bit-level move exchange.
- The latest exact-head trade tier finished `1/2`; its local trade passed, but
  the TCP subprocess exceeded its 720-second child bound. An earlier isolated
  run passed, so load stability is not established.
- A wheel built from the candidate installed in a clean environment outside
  the checkout. The MCP stdio smoke passed `3/3` for an explicit color-Red
  ROM/SYM pair without checkout-local `PYTHONPATH` or `VERSIONS.md`.
- `uv lock --check`, `uv pip check`, source compilation, and the focused
  configuration/session/MCP/link test set passed.

## Not signed off

- The latest exact-head remote trade run timed out its 720-second child bound;
  the tier finished 1/2 after local trade passed. An earlier isolated run
  passed, but production load stability and no-flake behavior are not
  established.
- The broader battle matrix is not a clean release gate; unrun/reversed rows
  remain outside the exact-head evidence.
- The broad suite is not a clean release gate. Product Ruff leaves `101`
  findings under the locked scope, while the explicitly audited vendored
  runtime has `227` findings.
- Reversed listener/connector roles, the complete Red/Blue/Yellow stateful
  matrix, native Windows/Cython certification, and independent review remain
  incomplete.
- Battle-fixture hashes and source-state provenance were not retained in the
  release record.
- The complete current gate output is not retained as one in-tree evidence
  bundle; sanitized per-tier bundles exist outside the checkout.
- TCP remains loopback-only and unauthenticated/unencrypted. Cross-host use is
  unsupported.

The candidate must remain `PARTIAL` until these boundaries are either closed
with evidence or explicitly excluded from the advertised product scope.
