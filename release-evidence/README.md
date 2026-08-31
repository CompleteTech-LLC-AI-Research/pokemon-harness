# External fixture evidence

[`fixture-manifest.json`](fixture-manifest.json) records the 10 save-state
files observed in the operator-managed `tests/fixtures/link` root. It records
file size, SHA-1, SHA-256, the expected ROM/symbol pins, and the provenance
known at the time of the audit.

[`current-audit-2026-08-31.md`](current-audit-2026-08-31.md) records the
current isolated-candidate gate counts, runtime identity, explicit-ROM MCP
coverage, and known non-gates. The 2026-08-30 file is retained as a
superseded historical snapshot. These are sanitized evidence summaries, not
copies of emulator traces or ROM-derived artifacts.

The `.state` files are ROM-derived, ignored by Git, and intentionally not
distributed by this repository. The manifest is an evidence index, not a
license or a claim that the files are safely reproducible. The battle and
vanilla entries retain hashes but have `unknown` or `partial` provenance
because their source-state hashes, runtime identity, and capture commands were
not retained.

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
  --fixture-root /path/to/tests/fixtures/link
```

The byte-validation command fails closed on a missing file, size mismatch,
SHA-1 mismatch, or SHA-256 mismatch. A schema-only pass is not fixture
evidence and must not be used as a production acceptance result.
