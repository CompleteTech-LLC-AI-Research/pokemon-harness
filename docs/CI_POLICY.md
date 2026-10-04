# Free hosted CI and local checks

Hosted CI is restricted to **public repositories on standard `ubuntu-latest`
runners**. GitHub provides standard hosted runner usage free for public
repositories. Private repositories have metered allowances instead; this
project does not rely on those allowances or authorize paid runner usage.

The repository's visibility is not changed by this policy. While a copy is
private, the hosted job is skipped, including manual dispatch. A skipped job
does **not** mean that its checks passed. Do not publish a private repository
merely to enable CI without its owner's explicit approval.

For the current private upstream, the workflow is also disabled in GitHub's
repository settings to prevent the older default-branch definition from
running before this policy is merged. Disabling it does not delete the file.
When operating an eligible public copy, its owner can enable **Release
hygiene** in the Actions page; the public-only job guard still applies.

The complete reusable workflow stays in
[`release-hygiene.yml`](../.github/workflows/release-hygiene.yml). It runs on
public pull requests, pushes to `main`/`master`, and manual dispatch. It uses
no larger runners or paid scanning service. Evidence artifact names include
the Python version so matrix jobs do not overwrite each other.

## Run checks locally

Use Python 3.11 or 3.12 and Bash (Linux, WSL, or Git Bash), with a dedicated
virtual environment activated. Run from the repository root:

```bash
python -m venv .venv-ci
. .venv-ci/bin/activate
bash scripts/run_local_ci.sh
```

On Windows Git Bash, activate `.venv-ci/Scripts/activate` instead. Run the
same command in separate environments for both supported Python versions
when qualifying the complete matrix.

Two authored analyzer cases contain Python 3.12 generic-class syntax. On
Python 3.11, those same pytest cases run in an explicitly configured, isolated
Python >=3.12 environment installed from the same checkout. The primary
runtime and the supported Python >=3.11 floor remain unchanged. Each child
must collect exactly its original case and pass setup, call, and teardown;
missing prerequisites, foreign imports, skips, or failures fail the primary
case. The complete child terminal output and pytest receipt are retained
beside the gate bundle, whose manifest remains closed to extra files.
Optimized Python execution fails explicitly in the delegating parent and
child. Python >=3.12 executes the original bodies directly.

Before a Python 3.11 local run, prepare the auxiliary environment outside the
checkout with a real Python >=3.12 executable, then export its venv entrypoint:

```bash
python scripts/prepare_grammar_runtime.py \
  --python /absolute/path/to/python3.12 --output /tmp/pokered-grammar-runtime
export POKERED_GRAMMAR_TEST_PYTHON=/tmp/pokered-grammar-runtime/venv/bin/python
bash scripts/run_local_ci.sh
```

Use a fresh output directory for each preparation. On Windows, use the
created `venv/Scripts/python.exe` entrypoint. Both hosted matrix jobs prepare
the same auxiliary environment explicitly before running the unchanged
strict production gate. The primary interpreter stays at the matrix version;
the Python 3.12 job executes these cases directly.
The hosted artifact contains `pokered-unit-evidence/` for the sanitized gate
bundle and `pokered-grammar-evidence/` for these two source-only cases' child
logs and receipts. Both are retained on failure.

The local runner executes the workflow's shell checks: tracked-asset policy,
development installation, lint/format, fixture schema, declared matrix,
network concurrency probe, ROM-free production gate with five timing
repetitions, wheel contents, and clean wheel installation. It retains its
temporary evidence and wheel directory locally, including on failure. It
does not require GitHub Actions, a runner registration, or a paid service.

Any additional scanner that is not demonstrably free for public open-source
use must remain local. Keep its runnable commands/configuration in this
repository so other users can reproduce the scan; do not silently add a
metered hosted job. No additional commercial/code-scanning service is
configured by this change.

Local unit/scan success does not establish real-ROM trade or battle
readiness. Run the asset-dependent tiers in the
[production runbook](PRODUCTION_RUNBOOK.md) separately, retaining their
results and asset hashes. Never upload ROMs, saves, symbols, or other
ROM-derived inputs to hosted CI.

Reference: [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
