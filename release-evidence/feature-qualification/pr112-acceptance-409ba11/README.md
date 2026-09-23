# PR #112 qualification-runner acceptance evidence (issue #85)

Reviewed head: `409ba11d81707d95f9bfb542fe38b7ca98f40ad1`
Branch: `work/qualification-runner-85`
Runtime pin: PyBoy `2.7.0`, fork revision `c565df66c3731fad2856169a90f6bbec99925915`,
`mcp==1.29.1`, CPython `3.11`.

## What this bundle is

Retained, sanitized terminal evidence for the two acceptance runs issue #85
names: the unchanged dual-runtime comparison and the complete nine-orientation
timed MCP matrix, executed from this branch's head with the operator assets.

- `acceptance-evidence.json` — machine-readable bundle: runtime identity, input
  SHA-1s (ROMs, symbols, all 24 link fixtures, fixture-manifest SHA-256), the
  fixture-validator result, both runtimes' suite totals, every one of the nine
  ordered orientations with its frame count, terminal mode, per-process exit
  status and process-group-alive flag, and the three host capacity samples.
- `acceptance-summary.txt` — the same result in plain text.

## Observed terminal result

| Runtime | tests | failures | errors | skipped | timed orientations |
| --- | --- | --- | --- | --- | --- |
| source (`PYBOY_NO_CYTHON=1`) | 143 | 0 | 0 | 0 | 9/9 PASSED |
| cython (installed extensions) | 143 | 0 | 0 | 0 | 9/9 PASSED |

Every orientation completed its three public frames, ended in `mode=connected`,
reported `returncode=0` for both peers, and reported `group_alive=false`, so no
owned emulator or server process survived the run. Both suites contain
`tests/test_qualification_runner.py` as well, which is where the runner
prerequisite, reservation, and descendant-cleanup checks live.

Sanitized output only: no ROM bytes, symbol bytes, save states, credentials,
machine-local paths, or interpreter paths are present. Absolute paths are
absent by construction; the bundle records inputs by role, SHA-1, and size.

## What this bundle does NOT prove

**It is not a capacity-qualified result.** The host that produced these runs has
no independently verifiable exclusive allocation: the cgroup hierarchy is
mounted read-only, the job runs in the root cgroup (`0::/`) with
`cpu.max = max 100000`, affinity spans all 12 CPUs, user namespaces are blocked
with `EPERM`, and `CapEff=0`. The three host-wide samples taken around and
during these runs measured `9.44`, `10.72`, and `10.72` busy cores out of `12`,
so competing tenants were demonstrably active throughout.

Consequently the runner's own reservation checks (`cpuset-affinity`,
`cgroup-quota`, `dedicated-host`) correctly report `fail`/`unsupported` on this
host and refuse admission. These runs demonstrate that the unchanged comparison
and the full nine-orientation matrix pass in both runtimes from this head; they
do **not** demonstrate those results under an operator-provisioned reservation,
and issue #85's acceptance criterion "run under that allocation" therefore
remains BLOCKED rather than satisfied. Provisioning status, test status, and
release qualification status stay separate, exactly as the issue requires.

## How the runs were produced

From the worktree at the reviewed head, with the operator asset roots:

```sh
# source runtime
env -u POKERED_SKIP_SHA1 -u PYTEST_ADDOPTS PYBOY_NO_CYTHON=1 \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  PYTHONPATH="$PWD/vendor/pyboy-src:$PWD/src:$PWD" \
  POKERED_ROM_ROOT="$ROM_ROOT" POKERED_FIXTURE_ROOT="$FIXTURE_ROOT" \
  "$SOURCE_PYTHON" -m pytest -q -rA -p pytest_asyncio.plugin \
  --strict-config --strict-markers -o junit_logging=out-err \
  --junitxml=source-focused.xml \
  tests/test_mcp_timed_rom.py tests/test_qualification_runner.py

# cython runtime
env -u PYBOY_NO_CYTHON -u POKERED_SKIP_SHA1 -u PYTEST_ADDOPTS \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH="$PWD/src:$PWD" \
  POKERED_ROM_ROOT="$ROM_ROOT" POKERED_FIXTURE_ROOT="$FIXTURE_ROOT" \
  "$NATIVE_PYTHON" -m pytest -q -rA -p pytest_asyncio.plugin \
  --strict-config --strict-markers -o junit_logging=out-err \
  --junitxml=native-focused.xml \
  tests/test_mcp_timed_rom.py tests/test_qualification_runner.py
```

The fixture manifest validated at 10 entries before execution. Raw JUnit and
stdout/stderr remain outside version control in the run's evidence directory;
this bundle carries the sanitized normalized form.
