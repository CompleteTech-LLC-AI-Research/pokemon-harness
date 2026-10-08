# Native unit and timing validation for the vendored pinball split

The pinball split is already on `master`. The public hosted native CI
workflow is configured to run the complete unit tier and five timing repeats.
Configuration alone is not a passing result or independent review, and it
does not change the release decision from **PARTIAL**.

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
5. Runs `production_gate.py` with `--runtime-mode cython --tier unit --tier timing`
   and `--repeat-timing 5`, retaining the gate report and raw output.
   No selected test is omitted, no assertion or timeout is relaxed, and there
   is no retry-until-green loop. Build and import proof never substitute for
   this complete unit-and-timing run.

## Evidence and acceptance

`evidence/` contains prerequisite and import-proof JSON, the bootstrap's build
record, command logs, gate/raw output, the final worktree status and head, and
an exit-code record when setup reaches the execution phase. Prerequisite
failure records `BLOCKED`; interrupted jobs or missing terminal reports remain
unqualified. A final exit code alone is not a release verdict. Review all
records, matching the preflight and final commits to the exact reviewed head,
and require a clean unit-tier PASS plus five successful timing repeats, with no
skipped, xfailed, failed, errored, or timed-out selected test.

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

## Pip-less uv bootstrap qualification (hosted job `native-uv-bootstrap`)

The pip lane above is unchanged and still owns the full unit and five-repeat
timing gate. It never exercises the bootstrap's uv fallback. A separate public
standard-runner job, `native-uv-bootstrap`, declares `needs: native-unit` and
runs `scripts/run_native_uv_ci.sh`. It does not repeat the full gate. If the
pip job fails, is skipped, or is disabled, the uv job is skipped, which is not
a pass.

What it does, in order:

1. Installs the pinned tool `uv==0.12.17` (hash-checked wheel
   `sha256:9e25bb39e1674799c408345a6397ebc2c7c719d498be0ce9d935466d36ceacf5`)
   into the hosted base environment only, never into the target environment.
2. Creates a fresh no-seed uv environment and records that pip is absent.
3. Injects a fault **inside that owned ephemeral environment only**: a `.pth`
   line makes `python -m ensurepip` fail, plus a benign audit hook that logs
   the argv of each subprocess to `evidence/uv-audit.jsonl` (no environment
   or credentials are serialized). This **simulates a Python without
   ensurepip**. It is not native platform absence and does not claim that any
   supported platform lacks ensurepip.
4. Runs the real, unchanged `scripts/bootstrap_pyboy.py --mode cython` build
   and `--check` with no patched functions, fake installer, or substituted
   return codes, so the bootstrap reaches its own uv fallback.
5. Runs the native import and pinball proof, then re-checks that pip is still
   absent, and validates the audit with a pure validator.

Acceptance requires every record below to validate; missing, corrupt, foreign
or contradictory evidence fails closed and is never treated as PASS:

- Typed pip/ensurepip state for `before`, `faulted` and `after`: matching phase
  labels, the verifier's own target interpreter, recorded integer return codes
  (booleans are rejected), pip absent throughout and ensurepip failing once the
  fault is in place.
- The audit log: the unchanged bootstrap process's exact sequence of pip probe,
  `ensurepip --upgrade`, build-requirement install, editable install of this
  checkout, native install of the staged `pyboy-src` with the pinned Cython,
  `uv pip check`, then the runtime probe. Each argv must match the bootstrap's
  own constants and flags, name the pinned uv binary and the owned target, and
  carry an integer `pid`, an integer `ppid` and an `executable` equal to `argv[0]`.
  Both bootstrap processes must be direct children of the one shell that runs the
  script (the same `ppid`, which never appears as an audited process); the
  `--check` process is neither the build process's child nor a probe child. Counting
  every foreign runtime probe, exactly one is allowed, after the build probe, and
  the `--check` process runs nothing else. Importing pyboy also loads pysdl2, whose
  library search runs `ldconfig -p`, `gcc -Wl,-t -o <tmp> -lSDL2...`, `ld -t -o
  /dev/null -lSDL2...` and `objdump -p -j .dynamic <file>` even with the bundled SDL
  libraries. Only those exact argv shapes (SDL2, SDL2_image and SDL2_ttf names;
  objdump only for `libSDL2[_image|_ttf][-2.0][d].so[.N...]`; system tool
  directories) are accepted. Build-process and build-probe-child discovery must end
  before the `--check` probe; the `--check` probe child's discovery follows that probe.
  Other build-tool subprocesses are tolerated only inside the uv install window; any
  other stray, extra, duplicate, misplaced or reordered call fails. These shapes were
  checked against records captured on Linux/Python 3.11 only, not a hosted run.
- The bootstrap's own `native-build.json` (complete cython record bound to the
  checked-in producer script, the staged-input digest recomputed from the vendored
  source, this interpreter, and the installed runtime fingerprint) and a full
  `pinball-native.json` (PASS, this head, the pinned revision, both native
  extension origins inside the target environment whose bytes match the build
  identity), never only a status field. Extension filenames follow the bootstrap's
  own runtime contract on every supported platform: untagged `.so`/`.pyd`,
  `.cpython-<tag>.so`, `.abi3.so`/`.pyd` and Windows `.cp312-win_amd64.pyd`, not the
  reviewing interpreter's ABI. The four required modules stay compiled; optional
  `pyboy` and `pyboy.link` may be source or compiled.
- The retained `uv-verify-commands.json` with the argv, raw stdout and stderr,
  digests and terminal return code of `uv --version` (must be 0.12.17 and exit
  0), `uv pip list --format json` (no pip; the harness distribution present) and
  `uv pip check`; its sha256 and those of every other evidence file are bound
  into `uv-qualification.json`.
- Unchanged HEAD and a clean worktree at the end, bounded raw logs and terminal
  codes.

The pure validators are exercised only with authored fixtures; those controls are
not hosted or native proof. The UV fixtures live in the portable
`tests/_bootstrap_stage_test_support.py` so the provenance tests collect where the
Unix-only reservation helpers (`fcntl`) do not import; an isolated-interpreter
control simulates that absence and claims no Windows run. The one instrumentation
control that builds a venv uses the POSIX `bin/python` layout and is POSIX-specific.
The focused counts are 8 old and 67 new native-module tests at the third repair,
13 more at the fourth, then the fifth repair's cases.

The job uploads its evidence with `if: always()` and 14-day retention, and is
bounded to 45 minutes. A failed, skipped, disabled or preflight-blocked run is
BLOCKED, not PASS. Until a hosted run of an approved head is read, this
qualification is **unfinished**; authoring the job and passing the pure
validator tests do not qualify the uv path.

## Historical native failure boundary

Local native `85d66e784cad314f2aed44e2362cd914a6623e39` remains
**FAIL (exit 1)**: the unit tier passed 8525 tests, the timing tier passed
2228 of 2230 with two repeat-2 failures, and the original return codes were
`[0, 1, 0, 0, 0]`. The cause is **unproven**, and there was no retry. Hosted
or current-head success neither explains nor waives it. The 40 transferred
original failure members and 2 hand-offs authenticate, but they do not prove
the broader 36-input semantic map. Bootstrap installer or dependency-check
changes are not evidence for, or a repair of, these timing failures.
