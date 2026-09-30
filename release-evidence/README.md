# External fixture evidence

[`fixture-manifest.json`](fixture-manifest.json) records the 28 save-state
files observed in the operator-managed `tests/fixtures/link` root. It records
file size, SHA-1, SHA-256, the expected ROM/symbol pins, and the provenance
known at the time of the audit: six `verified` canonical rows, four `partial`
vanilla rows, and eighteen `captured` boundary rows (a forced-replacement
party menu, the last command boundary before the deciding knockout, and the
terminal return, each with a peer sibling) driven from the admitted battle
fixtures.

The 2026-09-24 integration of the battle-state branch relocated that producer's
entry point (`scripts/produce_battle_state_fixtures.py`) into a thin facade plus
`scripts/produce_battle_state_fixtures_{model,drive,manifest}.py` to satisfy the
per-file size bound tracked by #122. The relocation is behavior-preserving
(identical public attribute surface and identical collected test node IDs), so
the eighteen `captured` boundary rows keep their recorded fixture bytes. Their
`runtime_identity` producer SHA-1 was updated from `a59ce9e9...` to the relocated
entry point's `e2e96765...`. No fixture was re-captured: re-capture needs the
operator-managed ROM/SYM assets, which are not present in this environment.

[`current-audit-2026-08-31.md`](current-audit-2026-08-31.md) records the
current isolated-candidate gate counts, runtime identity, explicit-ROM MCP
coverage, and known non-gates. The 2026-08-30 file is retained as a
superseded historical snapshot. These are sanitized evidence summaries, not
copies of emulator traces or ROM-derived artifacts.

The PR #19 follow-up is recorded in the repository README and runbook rather
than retroactively changing that baseline report. Its post-change evidence
includes the ROM-free 555/555 and timing 40/40 × 5 gate, focused transport/MCP
167/167, the 8/8 × 5 bounded localhost concurrency probe, and a 15/15
real-ROM remote transport slice. The full 19-row trade and battle results
remain the PR #17 source-runtime baseline until rerun against a later runtime.

The `.state` files are ROM-derived, ignored by Git, and intentionally not
distributed by this repository. The manifest is an evidence index, not a
license or a claim that the files are safely reproducible. The canonical color
Red, color Blue, and Yellow rows retain source-state hashes, runtime identity,
and reproduction commands. The four vanilla rows remain `partial`: the
ordinary source states are not proven to have been captured against the
vanilla ROMs, while the vanilla battle rows inherit that open provenance
boundary from their ordinary inputs.

## Production gate evidence bundles

The production gate can retain a portable, sanitized result bundle without
copying ROMs, save states, or environment variables:

```bash
python scripts/production_gate.py \
  --evidence-dir /tmp/pokered-gate-evidence \
  --format text
```

The directory contains `gate-report.json`, `gate-report.txt`, and
`evidence-manifest.json`. The manifest records the report files' sizes and
SHA-256 values. Reports retain runtime identity, asset hashes and sizes,
collection/tier outcomes, and bounded failure diagnostics. Absolute local
paths are replaced with placeholders, and free-form diagnostics are bounded
and redacted for credential and binary-looking data. A `FAIL` or `BLOCKED`
run is retained as such; it must not be reinterpreted as a pass. Keep the
evidence directory outside the checkout unless its generated files are
explicitly intended for review.

Validate only the checked-in manifest in an asset-free checkout:

```bash
python scripts/validate_fixture_manifest.py --schema-only
```

Validate the actual external bytes for a release or acceptance run:

```bash
python scripts/validate_fixture_manifest.py \
  --fixture-root <fixture-root>
```

The byte-validation command fails closed on a missing file, size mismatch,
SHA-1 mismatch, or SHA-256 mismatch. A schema-only pass is not fixture
evidence and must not be used as a production acceptance result.
