# Current audit: 2026-08-31

Status: `PARTIAL` — not production-ready.

This record describes the isolated candidate through `ab89c39`. The stateful
real-ROM tiers were collected at the code-equivalent `2ac09fb` boundary;
`9db9bb2` fixes only production-gate parsing and `ab89c39` annotates deliberate
cleanup exception suppression. No ROM, symbol, fixture, save-state, or secret
is included here.

## Passing evidence

- The locked unit gate passed `414/414` tests.
- Timing passed `35/35` cases across five repetitions.
- The gate preflight validated all five pinned ROM inputs, three symbol files,
  and three ordinary link fixtures by SHA-1.
- The stateful local tier passed `46/46` with real ROMs and matching fixtures.
- The canonical remote transport tier passed `11/11` for color Red as the
  listener/internal-clock and color Blue as the connector/external-clock.
- The local strict Red/Yellow trade and battle paths passed. The trade compares
  complete game-owned party records and the battle reaches a real move turn.
- An isolated Red/Blue subprocess trade passed `1/1` in `229.37` seconds,
  including full party-record equality and native bit-level serial traffic.
- A wheel built from the candidate installed in a clean environment outside
  the checkout. The MCP stdio smoke passed `3/3` for an explicit color-Red
  ROM/SYM pair without checkout-local `PYTHONPATH` or `VERSIONS.md`.
- `uv lock --check`, `uv pip check`, source compilation, and the focused
  configuration/session/MCP/link test set passed.

## Not signed off

- The isolated remote trade timed out when the stateful local, remote, trade,
  and battle tiers were run concurrently. The standalone rerun passed, but
  production load stability and no-flake behavior are not established.
- The mechanical battle tier passed `2/2`, but its remote subprocess driver
  selects LinkMenu with `_install_linkmenu_autoselect` and
  `_force_linkmenu_selection`. It is controlled native-serial diagnostic
  evidence, not authentic user-driven remote battle acceptance.
- The broad suite is not a clean release gate, and locked repository-wide Ruff
  reports `528` findings, including legacy and vendored-runtime code.
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
