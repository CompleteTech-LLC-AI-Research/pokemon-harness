# Native unit validation for the vendored pinball split

The pinball split is already on `master`. Issue #138 remains open until the
native unit lane has a terminal passing result and the exact candidate has
its required independent review. This recipe supplies a runnable lane, not
that result. It does not change the release decision from **PARTIAL**.

## Execute on an eligible host

Use a clean, committed checkout, a normal non-root Linux account, Python 3.11
or 3.12 with development headers, a C compiler, SDL2 development libraries,
writable shared memory, and an operator-provided quiet allocation. Installing
packages requires network access. No ROM, symbol, or save-state input is needed.

```bash
export PYTHON=python3.12
export NATIVE_UNIT_OUTPUT="$(mktemp -d)/native-unit"
bash scripts/run_native_unit_ci.sh
```

The output must be a **new absolute directory outside the checkout**. The
runner refuses existing outputs, dirty/uncommitted checkouts, root execution,
missing build prerequisites, and environment overrides that could shadow the
runtime or filter tests. Unset the named conflicting variables and rerun with
a new output directory; there is no bypass switch. Existing evidence is never
overwritten or deleted by this recipe.

The GitHub workflow uses the same script on a standard `ubuntu-latest` public
runner. Its explicit public-visibility guard follows `CI_POLICY.md`. It does
not enable a disabled workflow, change repository visibility, use a paid
runner, upload assets, or modify branch protection. A skipped or disabled job
is not a passing gate.

## What the lane does

1. Records the clean harness commit, interpreter, compiler, headers, effective
   affinity, available load/PSI observations, and a real shared-memory semaphore
   check. Missing CPU observations remain unknown. These facts are **not** an
   operator allocation attestation and do not complete #85/#86.
2. Creates a new virtual environment and installs the existing development
   dependencies. Runs lint/format checks for this lane's Python changes.
3. Delegates the complete clean native build to `bootstrap_pyboy.py --mode
   cython --build-evidence ...`, preserving the bootstrap's own staged-input
   digest and build record. Runs `pip check` and the native bootstrap check.
4. Verifies the current pin, the compiled-runtime flag, both pinball modules'
   extension origins, their 1000-line limit, explicit data exports, facade/data
   object identity, and the compiled plugin manager's typed Pinball wrapper slot.
5. Runs the existing `production_gate.py --runtime-mode cython --tier unit`
   unchanged, retaining the gate report and raw output. No selected test is
   omitted, no assertion or timeout is relaxed, and there is no retry-until-green
   loop. Build and import proof never substitute for this full unit run.

## Evidence and acceptance

`evidence/` contains prerequisite and import-proof JSON, the bootstrap's build
record, command logs, gate/raw output, the final worktree status and head, and
an exit-code record when setup reaches the execution phase. Prerequisite
failure records `BLOCKED`; interrupted jobs or missing terminal reports remain
unqualified. A final exit code alone is not a release verdict. Review all
records, matching the preflight and final commits to the exact reviewed head,
and require the unchanged gate's clean unit-tier PASS with no skipped, xfailed,
failed, errored, or timed-out selected test.

`work/` contains the disposable virtual environment and temporary files. It is
not uploaded by the workflow. Keep all generated output outside Git, and do
not upload the entire output root. The workflow retains only `evidence/`, also
on failure; check logs before sharing an artifact beyond repository readers.

The new contract tests are in the already registered unit module
`tests/test_vendored_provenance_wording.py`. Existing tests and classifications
are unchanged. Run the focused tooling controls with:

```bash
python -m pytest tests/test_vendored_provenance_wording.py -k native_ci
```

Those controls use synthetic modules to exercise the verifier's negative paths.
They are not execution of compiled PyBoy or the full unit suite. Neither the
workflow definition nor passing controls close issue #138. Preserve the
historical evidence and runtime pin; real-ROM re-qualification remains owned
by #235, and full release qualification remains separate.
