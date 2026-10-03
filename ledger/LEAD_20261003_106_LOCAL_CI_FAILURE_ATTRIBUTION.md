# Local CI failure attribution for PR #564 (no regression on master)

Date: 2026-10-03
PR: #564 branch `fix/106-matrix-concurrency-benchmark`
Head: `ce2ce72a` (`#106: add the benchmark script to both local CI ruff lanes`)
Base: `origin/master` `09c84f14`
Worktrees: PR head `/workspace/poke-harness/.scratch/wt106` on `.venv-wt106`;
master baseline `/workspace/poke-harness/.scratch/wt106-mastercmp` on branch
`cmp/106-master-baseline` at `09c84f14`, with its own `.venv-base`
(`pip install -e ".[dev]"`). Both interpreters are CPython 3.11.2, ruff 0.16.5.
Full baseline log: `/tmp/wt106_localci.log`.

## The claim under review

`bash scripts/run_local_ci.sh` on the PR head exits **1** with
`overall: FAIL`. That is recorded here as-is. **Local CI does not pass on this
branch**, and this document does not claim otherwise.

The question this document answers is narrower: whether the PR *caused* any of
it. It does not.

## Gate totals as measured on the PR head

```
unit   7377 total   7345 passed   29 failed
timing 2125 total   2031 passed   94 failed
capacity-policy: unavailable
  reason: no --capacity-policy supplied; capacity admission is not enforced
overall: FAIL
```

## Every failure is in 6 modules, all environmental

`grep -oE "FAILED tests/[a-z_0-9]+.py" /tmp/wt106_localci.log | sort | uniq -c`:

```
16 tests/test_probe_timed_rom_pair_process.py
10 tests/test_probe_owner_phases.py
 6 tests/test_probe_timed_rom_pair_startup.py
 4 tests/test_probe_timed_rom_pair_menu.py
 1 tests/test_session_timed_execution.py
 1 tests/test_mcp_timed_stdio.py
```

Two root causes, both host-imposed and both tracked by **#253**:

1. `OSError: [Errno 30] Read-only file system` — the probe process family
   needs a POSIX shared-memory segment under `/dev/shm`. This host mounts
   `/dev/shm` read-only at 63 MB, so the segment cannot be created.
2. `TimedOwnerError: request deadline expired` and
   `AssertionError: paired owners did not complete semantic work within 48s
   capacity` — the measured single-frame cost on a 4-CPU host is ~11.48 s
   against a 5 s deadline. These are load-dependent, not deterministic
   (#253's correction at `96c4c1ea`: 2 deterministic / 7 load-dependent).

## Direct comparison against unmodified master

The failing modules were run directly on both trees with identical
invocation (`-p no:randomly`, no selection, no skips, no synthetic gameplay):

```bash
python -m pytest -p no:randomly -q \
  tests/test_probe_timed_rom_pair_process.py \
  tests/test_probe_timed_rom_pair_startup.py \
  tests/test_probe_owner_phases.py \
  tests/test_probe_timed_rom_pair_menu.py \
  tests/test_mcp_timed_stdio.py \
  tests/test_session_timed_execution.py
```

Master baseline `09c84f14`: exit 1, **14 distinct failing tests**.
PR head `ce2ce72a`: exit 1, the **same 14**, plus
`test_mcp_timed_stdio.py::test_authored_timed_stdio_pair_frames_and_cleanup`
in one run.

That extra test was checked for flakiness rather than assumed benign. On
**master**, in isolation, three consecutive runs:

```
MASTER run1 exit=1 FAILED tests/test_mcp_timed_stdio.py::test_authored_timed_stdio_pair_frames_and_cleanup
MASTER run2 exit=1 FAILED tests/test_mcp_timed_stdio.py::test_authored_timed_stdio_pair_frames_and_cleanup
MASTER run3 exit=1 FAILED tests/test_mcp_timed_stdio.py::test_authored_timed_stdio_pair_frames_and_cleanup
```

It is a pre-existing, host-dependent failure that also reproduces on the PR
head. It is not attributable to this PR.

Supporting fact: `git diff --stat origin/master...HEAD -- tests/test_mcp_timed_stdio.py
src/ pokered_harness/ scripts/production_gate*.py` is **empty**. The PR does not
touch the timed-owner implementation, the shared-memory probe, or any gate
logic. Its entire diff is 8 files: the benchmark script and its tests, the
runbook section, the tier registration, the two CI lane lists, and this ledger.

## Focused suites on the PR head, after rebasing onto the review commit

```
python -m pytest -p no:randomly -q tests/test_matrix_concurrency_policy.py tests/test_local_ci_policy.py
-> 115 passed
```

(105 in `test_matrix_concurrency_policy.py`, 10 in `test_local_ci_policy.py`.)

## Disposition

The PR introduces **no** local-CI regression. The 123 failures are the
already-open #253 condition reproducing on an unmodified master tree on this
host.

Local CI remains **FAIL** and release remains **PARTIAL**. #253 stays open, and
#106 stays open. This document records attribution only; it is not a waiver,
and merging #564 does not make any gate green.
