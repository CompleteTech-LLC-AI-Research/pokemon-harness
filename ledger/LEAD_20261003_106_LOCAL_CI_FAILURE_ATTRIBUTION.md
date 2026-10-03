# Local CI failure attribution: the #106 benchmark PR causes no gate regression

Date: 2026-10-03
Issue: #106 (stays **open**)
Related open issue: #253 (stays **open**)
Released in: #564, merge commit `1325f139`, landed in `master`

## The claim under review

`bash scripts/run_local_ci.sh` exits **1** with `overall: FAIL` on this host.
That is recorded here as measured. **Local CI does not pass**, before or after
this work, and nothing in this document claims otherwise.

The narrower question is whether the matrix-concurrency benchmark caused any
of it. The answer is no, and this document now carries the full evidence
rather than a partial sample.

## Correction to an earlier version of this record

An earlier revision of this ledger claimed the failures were confined to six
modules. That was **wrong**, and an independent review caught it.

The six-module figure came from grepping the log for `FAILED tests/...` lines.
Those lines are truncated in this log. The gate's own structured
`iteration-failure` and `failure-detail` records name **thirteen** modules, and
seven of them never appear in a `FAILED` line at all:

```
tests/test_mcp_timed_remote_owner.py
tests/test_mcp_timed_remote_cached_failure.py
tests/test_probe_timed_trade_pair_driver.py
tests/test_probe_timed_trade_pair_proof.py
tests/test_timed_menu_milestone_sentinels.py
tests/test_timed_menu_milestones.py
tests/test_runtime_packaging_build_contract.py
```

Because the earlier comparison ran only the six visible modules, it did not
establish the claim it was making. The comparison below now covers all
thirteen.

## Gate totals, as measured on this host

```
unit   7377 total   7345 passed   29 failed
timing 2125 total   2031 passed   94 failed
capacity-policy: unavailable
  reason: no --capacity-policy supplied; capacity admission is not enforced
overall: FAIL
```

These counts are the gate's own, taken from the full local-CI run recorded
above. They have **not** been re-measured as whole-tier totals on unmodified
master — the master comparison below covers the failing modules directly rather
than re-running the entire ~30 minute gate on a second tree. What is
demonstrated on unmodified master is the per-test failure set, not these
aggregate totals.

## Full comparison: all thirteen failing modules, master vs this change

Two checkouts were used, each with its own virtualenv built by
`pip install -e ".[dev]"`: a **master baseline** at the merged `1325f139` tree,
and this **candidate** tree. Identical invocation on both, no selection, no
skips, no xfail, no synthetic gameplay:

Provenance limit, stated rather than glossed: the retained comparison logs
(`/tmp/base13*.txt`, `/tmp/head13*.txt`) record the pytest output but **not**
the baseline checkout's SHA. So what the logs substantiate is the *failure
set* — 19 versus 22, the 19 identical, and the three named extras — and the
claim that the baseline was the merged master tree rests on the recorded
checkout rather than on anything inside those files.

```bash
python -m pytest -p no:randomly -q \
  tests/test_probe_timed_rom_pair_process.py \
  tests/test_probe_owner_phases.py \
  tests/test_probe_timed_rom_pair_startup.py \
  tests/test_probe_timed_rom_pair_menu.py \
  tests/test_session_timed_execution.py \
  tests/test_mcp_timed_remote_owner.py \
  tests/test_mcp_timed_remote_cached_failure.py \
  tests/test_probe_timed_trade_pair_driver.py \
  tests/test_probe_timed_trade_pair_proof.py \
  tests/test_timed_menu_milestone_sentinels.py \
  tests/test_timed_menu_milestones.py \
  tests/test_runtime_packaging_build_contract.py \
  tests/test_mcp_timed_stdio.py
```

Both exit 1. The master baseline fails **19** distinct tests; this candidate
fails **22**. The 19 are identical on both sides. The candidate additionally
showed these three:

```
tests/test_mcp_timed_remote_owner.py::test_real_partial_progress_active_interrupt_is_terminal
tests/test_session_timed_execution.py::test_paired_authored_full_frame_calls_preserve_count_render_buttons_and_events
tests/test_session_timed_execution.py::test_real_partial_public_tick_failure_counts_only_completed_frames
```

These were **not** assumed to be noise. Each was rechecked in isolation on
unmodified `master`, and each fails there too:

| test | isolated runs on unmodified master |
|---|---|
| `test_real_partial_progress_active_interrupt_is_terminal` | fails (run 1) |
| `test_paired_authored_full_frame_calls_preserve_count_render_buttons_and_events` | fails (run 1) |
| `test_real_partial_public_tick_failure_counts_only_completed_frames` | fails (run 3), passes (runs 4, 5) |

They are **load-dependent**, exactly the class #253's correction at `96c4c1ea`
identifies. They flip to passing on the same master tree within minutes, so the
difference between a 19-count and a 22-count on this host is contention, not a
code change. None of the three is in a module this candidate touches.

Two further host-dependent failures in the 19 are worth naming for the same
reason: `tests/test_mcp_timed_stdio.py::test_authored_timed_stdio_pair_frames_and_cleanup`
failed **3/3** isolated runs on unmodified master. That is a separate
three-run experiment from the 13-module comparison above, and it is recorded
in its own retained per-run logs rather than in the combined ones.

**The honest summary:** this candidate's failure set is a superset of master's,
and every member of the difference also fails on unmodified master. Nothing in
the candidate causes a gate regression.

## Why these fail at all

Two host-imposed causes, both tracked by **#253**:

1. `OSError: [Errno 30] Read-only file system` — the probe process family
   needs a POSIX shared-memory segment under `/dev/shm`. This host mounts
   `/dev/shm` read-only at 63 MB, so the segment cannot be created. The
   `Errno 30` appears 163 times in the retained gate log; the mount mode and
   size are a direct `df -h /dev/shm` observation on this host, not something
   the log itself records.
2. `TimedOwnerError: request deadline expired` and
   `AssertionError: paired owners did not complete semantic work within 48s
   capacity` — the measured single-frame cost on this 4-CPU host is ~11.48 s
   against a 5 s deadline. The CPU count is a direct `nproc` observation;
   the timings are the gate's own. #253's correction at `96c4c1ea`
   classifies the split as 2 deterministic and 7 load-dependent, not 3 and 6.

## The change touches none of it

`git diff` between the merged `master` tree and this candidate over
`src/`, `pokered_harness/`, `scripts/production_gate*.py` and
`tests/test_mcp_timed_stdio.py` is **empty**. The timed-owner implementation,
the shared-memory probe and the gate logic are untouched.

The complete candidate diff is three code files plus this ledger:

```
.github/workflows/release-hygiene.yml    (2 lines: one per Ruff lane)
scripts/run_local_ci.sh                   (2 lines: one per Ruff lane)
tests/test_local_ci_policy.py             (the lint-coverage test and helpers)
ledger/LEAD_20261003_106_LOCAL_CI_FAILURE_ATTRIBUTION.md
```

No ROM, `.gb`/`.gbc`/`.sym` file, save state, credential, cache or build
output is included, and the shipped `--matrix-workers` default remains `1`.

## A real defect this review did find

`scripts/` is enumerated explicitly in the Ruff lanes rather than globbed.
When #564 landed, `scripts/benchmark_matrix_concurrency.py` — 1409 lines of
new code — was added to neither the check lane nor the format lane in either
`scripts/run_local_ci.sh` or `.github/workflows/release-hygiene.yml`.

The existing lockstep test could not catch it: it only proves the runner and
the workflow list the *same* files, and both omitted the path together. This
candidate fixes the omission and adds tests that hold for both subcommands and
**both** files:

1. the path is **listed** in the lane;
2. Ruff actually **rejects violations** in it, probed through the lane's own
   option vector;
3. the runner actually **executes** those lanes, and a failure there actually
   stops it;
4. the hosted workflow actually runs the step that carries those lanes.

Point 2 is deliberately behavioural rather than a model of Ruff's rule
resolution. The probe feeds known-bad snippets to `ruff` on stdin under
`--stdin-filename <benchmark>` carrying the lane's real options, and asserts
the verdict is nonzero and not an argparse failure. That question absorbs
every way the file could escape linting: an `--exclude` that drops it, a
lane-level `--ignore=ALL`, a `per-file-ignores` entry of `["ALL"]`, and
`--exit-zero` all answer "no violation", and all of them fail here.

One probe rule is not enough, though, and that was found in review twice. A
lane narrowed to `--select=F821` still fails an undefined name while an unused
import goes unreported, and a `per-file-ignores = ["F821"]` entry does the
same. Three probes then looked sufficient — until `--select=F401,F811` kept
two of the three firing while F821 was silently accepted. So the check lane is
probed with **five** independent violations spanning five rule codes, every one
of which must be reported, with a majority additionally required. Measured
behaviour of that probe set:

| lane configuration | F821 | F401 | F811 | F632 | F541 |
|---|---|---|---|---|---|
| unmodified | rejected | rejected | rejected | rejected | rejected |
| `--select=F821` | rejected | **silent** | **silent** | **silent** | **silent** |
| `--select=F401,F811` | **silent** | rejected | rejected | **silent** | **silent** |
| `--extend-ignore=<any one rule>` | silent | 4 of 5 still rejected |
| `per-file-ignores = ["F821"]` | silent | rejected | rejected | rejected | rejected |
| `per-file-ignores = ["ALL"]` | silent | silent | silent | silent | silent |
| `--ignore=ALL` | silent | silent | silent | silent | silent |

Two further signals keep that honest, because the majority rule alone cannot
separate a blanket disable from a selective one:

* A **syntax error** is reported before any rule selection applies, so probing
  an unparseable snippet proves Ruff is reading the file at all. It fails when
  the file is excluded and passes when it is merely unlinted.
* A **control filename** that no `per-file-ignores` entry names is probed with
  the same snippets. If a rule still fires there, the rule is active and only
  this file's copy of it is silenced — which is selective, and legitimate. A
  blanket entry leaves every rule firing for the control file and failing for
  the benchmark, so the width of the probe set is what tells the two apart.

Two of those five probes were themselves wrong for a while, and neither
showed up as a failure. F841 is listed as enabled in `--show-settings` but is
never reported for a local assignment, so its probe asserted nothing. An E711
snippet that used an undefined name tripped F821 instead, so that probe was
silently re-testing a different rule — and `--extend-ignore=E711` then passed
because ignoring E711 drops none of the rules actually being watched. Both
were replaced after measuring that the new rules fire on their own, and a
dedicated row now pins the property that had been missing: every probe must
report its own named rule, and must keep reporting it when any *other* probed
rule is ignored. Reintroducing either broken probe fails that row.

`ruff format --check` has no diagnostic to match at all — it exits nonzero and
prints nothing — so the format lane is judged on exit status across two
independent unformatted snippets, requiring both.

Four lanes of escape cannot be detected by probing and are rejected by name
instead. `--exit-zero` keeps printing diagnostics while forcing exit 0, so a
real violation no longer fails CI; `--fix` repairs the violation and also exits
0, so a gate that silently rewrites the benchmark is not a gate; `--range`
format-checks only a line span, so an unformatted region outside the span is
never checked — measured directly, with `--range=1-1` an unformatted line 2
passes while a full `--check` on the same input fails; and `--diff` changes
what the formatter emits.

`--select` and `--ignore` themselves are deliberately *not* rejected. Narrowing
a rule set is a reviewable decision, and refusing the option would be
over-rejecting; the probe above already fails when the narrowing drops a rule
CI depends on.

An earlier revision of this probe injected `--force-exclude`, which changed the
lane's own behaviour rather than measuring it: with an `extend-exclude` naming
the benchmark, the real command still resolved the file while the probe
reported it as excluded. That produced a false failure, so the probe now
passes the lane's options verbatim and adds nothing.

Point 3 exists because every other check in this file reads the runner as
*text*, so a lane wrapped in `if false; then ... fi` still parses under
`bash -n`, still contains the path, and still leaves all tests green while CI
executes neither command. The runner is now executed for real against a stub
`python` that records its argument vectors and does nothing else; the recorded
trace, not the text, decides whether a lane runs. The execution is hermetic —
no dependency install, no gate, no wheel build, no network — and redirects
`TMPDIR` into a scratch directory that is removed afterwards. It costs about
4 seconds.

That trace alone was still not enough, because the stub exits 0: wrapping the
lanes in `set +e` left every test green, since the trace showed the lanes
running but never showed a nonzero exit mattering. So the runner is executed a
second time with the stub failing the main `ruff check` lane exactly as a real
violation would, and the trace must end there. Point 4 covers the hosted path,
which the trace cannot reach: `if: ${{ false }}` on the lint/format step also
left every test green, because GitHub replaces the implicit `success()` with a
step-level condition.

### What is deliberately *not* rejected

Five mutations leave the test green, and that is correct rather than a gap.
Verified directly against Ruff 0.16.5 by making the benchmark itself
contain a real `F821`:

| lane option | file resolved by Ruff? | F821 reported? |
|---|---|---|
| (none) | yes | yes |
| `--force-exclude` alone | yes | yes |
| `--exclude=scripts` alone | yes | yes |
| `--exclude=<benchmark>` alone | yes | yes |
| `--exclude <benchmark>` (operand form) alone | yes | yes |
| `--exclude=scripts --force-exclude` | **no** | no |
| `--exclude <benchmark> --force-exclude` | **no** | no |

Ruff's `--exclude` does not drop an explicitly-passed file unless
`--force-exclude` is also present, so the first five rows are true passes: the
real lane still lints the benchmark. Only the last two rows escape, and both
fail the probe. A `per-file-ignores = ["F821"]` entry is also a true pass,
since the other four probed rules still fire — but that one is selective rather
than blanket, and it is handled by the probe set described above rather than by
this table.

### Mutation matrix

Each mutation below was applied to the runner and workflow, one at a time, and
the policy suite re-run; every row marked `yes` was observed to fail. The rows
marked `n/a` were observed to pass, and are not escapes for the reasons given
above — listing them separately is the point: a green test is only meaningful
once you have checked it is green for the right reason.

An earlier revision of this table recorded a two-of-three quorum as sufficient.
It was not: `--select=F401,F811` satisfied it while F821 stopped being
enforced. The quorum is now a five-rule set in which every probed rule must be
reported.

| mutation | caught |
|---|---|
| entry removed from the check lane | yes |
| entry removed from the format lane | yes |
| `--force-exclude --exclude=<benchmark>` in the runner's lanes | yes |
| `--force-exclude --exclude=<benchmark>` in the workflow's lanes only | yes |
| `per-file-ignores = ["ALL"]` for the benchmark in `pyproject.toml` | yes |
| `--ignore=ALL` in a check lane | yes |
| `--select=E501` narrowing a check lane past every diagnostic rule | yes |
| `--select=F821` narrowing a check lane past the other rules | yes |
| `--select=F401,F811`, keeping a two-of-three quorum while dropping F821 | yes |
| `--extend-ignore=` of any single probed rule | yes |
| `per-file-ignores` naming two of the five probed rules | yes |
| `--fix-only` in a check lane | yes |
| `--diff` in a format lane | yes |
| `--config` pointing at a config with `lint.select = []` | yes |
| `--exit-zero` in both check lanes | yes |
| `--fix` in both check lanes | yes |
| `--range=1-1` in both format lanes | yes |
| both main lanes wrapped in `if false; then ... fi` | yes |
| `set +e` around both main lanes | yes |
| `if: ${{ false }}` on the workflow's lint/format step | yes |
| `if: always()` on the workflow's unit job | yes |
| `if false; then ... fi` inside the workflow's run block | yes |
| `continue-on-error: true` on the lint step or its job | yes |
| `set +e` followed by `exit 0` around the main check lane | yes |
| `--force-exclude` alone in the lanes | n/a — not an escape (see above) |
| `--exclude=scripts` alone in the lanes | n/a — not an escape (see above) |
| `--exclude <benchmark>` in the space-separated operand form | n/a — not an escape (see above) |
| `per-file-ignores = ["F821"]` only, for the benchmark | n/a for the coverage row — selective, not a blanket disable |
| `--extend-ignore=E711`, a rule no probe covers | n/a — drops no rule CI depends on |
| `--force-exclude` with `--exclude=<benchmark>`, both spellings | yes |

## Focused suites on this candidate

```
python -m pytest -p no:randomly -q tests/test_matrix_concurrency_policy.py tests/test_local_ci_policy.py
-> 123 passed
python -m ruff check <changed python files>      -> clean
python -m ruff format --check <changed files>    -> clean
bash -n scripts/run_local_ci.sh                  -> clean
```

## Disposition

The benchmark causes **no** local-CI regression. Every failure this candidate
shows is also a failure on unmodified `master` on this host, so the failures
belong to the already-open #253 condition rather than to this change.

Local CI remains **FAIL**. Release remains **PARTIAL**. #253 stays open and
#106 stays open: the harness ships, but the qualifying measurement still cannot
be run without real ROM and symbol assets and a capacity policy (#85). This
document is an attribution record, not a waiver.
