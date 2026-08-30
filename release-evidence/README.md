# External fixture evidence

[`fixture-manifest.json`](fixture-manifest.json) records the 10 save-state
files observed in the operator-managed `tests/fixtures/link` root. It records
file size, SHA-1, SHA-256, the expected ROM/symbol pins, and the provenance
known at the time of the audit.

The `.state` files are ROM-derived, ignored by Git, and intentionally not
distributed by this repository. The manifest is an evidence index, not a
license or a claim that the files are safely reproducible. The battle and
vanilla entries retain hashes but have `unknown` or `partial` provenance
because their source-state hashes, runtime identity, and capture commands were
not retained.

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
