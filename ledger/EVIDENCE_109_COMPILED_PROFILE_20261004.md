# #109 — compiled-runtime instruction-stepping profile (evidence, decision)

Date: 2026-10-04
Base / tested commit: `368d8262` ("ledger: retract the mechanism claim in FINDING_580")
Branch: `lead/109-stepping-profile`
Worktree: `/workspace/poke-harness/wt-109-profile`
Runtime: `pyboy 2.7.0` revision `fd765b1808ac9cb192b42ae971987158ff36ae48`, Cython build,
CPython 3.12.14. `pyboy/__init__.cpython-312-x86_64-linux-gnu.so` — verified compiled, 60
extension modules present.

No Nintendo asset was used. `scripts/stepping_loop_profile.py` authors its own 32 KiB ROM-only
cartridge in a temporary directory. Nothing was written outside that temporary directory and no
symbol table, save state or BYO ROM was opened.

## Runtime build

The compiled runtime required a rebuild that earlier attempts had failed to complete. The failure
cause was not the code: `scripts/bootstrap_pyboy.py` hardcodes `INSTALL_TIMEOUT_SECONDS = 1800`
with no environment override, and the wheel build of the vendored PyBoy needs ~30 minutes on this
4-CPU host while other tenants hold load in the 12-18 range.

Resolved by running the build against a Python 3.12 interpreter that ships headers (CPython 3.11
in the system environment has no `Python.h`):

    .venv-cy/bin/python   # CPython 3.12.14, Cython 3.0.12

Build evidence (`status: complete`, `mode: cython`), installed fingerprint
`80a699794ee795d5e32d74693ecf6697d25db16c2e1b0006bfbd9018578fed10`.

    sha256  2cff0645b1fa53350bcc7f53c3592663a572822b5956d6013868217881607adc  native-build.json
    sha256  87ad02d989c01fafc6505431b462c55e07a0444c82a3cef306392303b47aa902  profile-368d8262.json
    sha256  c8e6d79309242b3463db8a5f3c3fd8d758ebcd577cc0d3652eab1237d15b7f2c  profile-368d8262.txt

## Measurement 1 — five declared stepping paths

`--cycles 4000000 --repeats 5`, host load1 12.30 on 4 CPUs. Every path reported
`deterministic_counts_agree=True`: the retired instruction count and cycle count matched the
cartridge's own declared instruction mix, so the comparison is over equal emulated work.

### `reg-only` (6 instructions / 40 cycles per loop)

| path | med s | best s | med instr/s | py calls/instr |
|---|---:|---:|---:|---:|
| production_chunk_loop | 3.911 | 2.469 | 153 433 | 0.02564 |
| optimized_chunk_loop | 2.516 | 1.262 | 238 461 | 0.02564 |
| minimal_singlestep_loop | 1.621 | 0.970 | 370 088 | 1.00000 |
| compiled_frame_loop | 0.333 | 0.199 | 1 804 240 | 0.00009 |
| compiled_batched_loop | 0.585 | 0.369 | 2 015 914 | 0.00000 |

Adaptive batch resolved to 56 frames per call.

### `hram-io` (5 instructions / 44 cycles per loop, one HRAM write)

| path | med s | best s | med instr/s | py calls/instr |
|---|---:|---:|---:|---:|
| production_chunk_loop | 2.993 | 2.441 | 151 869 | 0.03333 |
| optimized_chunk_loop | 2.820 | 1.590 | 161 167 | 0.03333 |
| minimal_singlestep_loop | 2.244 | 1.986 | 202 603 | 1.00000 |
| compiled_frame_loop | 0.204 | 0.118 | 2 231 081 | 0.00013 |
| compiled_batched_loop | 0.893 | 0.258 | 1 001 100 | 0.00000 |

## Attribution — the bottleneck is measured, not hypothetical

`cProfile` over the production loop, 600 015 retired instructions:

| function | calls | calls per retired instruction |
|---|---:|---:|
| `builtins.getattr` | 1 846 200 | **3.07692** |
| `builtins.max` | 30 770 | 0.05128 |
| `builtins.min` | 15 385 | 0.02564 |
| `_step_single_step_chunk` | 15 385 | 0.02564 |

The dominant per-instruction Python cost is `getattr`. The source confirms the mechanism:
`_pyboy_link_session_stepping_mixin.py::_step_single_step_chunk` re-reads `p.mb.lcd`, then
`lcd.frame_done`, then `cpu.cycles` on **every single-stepped instruction**, even though `mb`,
`cpu` and `lcd` are loop invariants. `start_cycles` is already hoisted; the remaining three lookups
are not.

## Measurement 2 — controlled A/B, identical emulated work

This isolates the per-instruction Python overhead from everything else. Both variants run the same
cartridge to the same cycle target, and both were validated to retire the identical instruction
count and the identical cycle count.

| variant | median s | best s | retired | cycles |
|---|---:|---:|---:|---:|
| production_chunk_loop | 4.0118 | 2.5678 | 600 015 | 4 000 096 |
| hoisted_chunk_loop | 2.6828 | 1.7800 | 600 015 | 4 000 096 |

**Speedup from hoisting the invariant lookups only: 1.495x median, 1.443x best-of-repeat.**
Identical retired instructions and identical cycles confirm no emulated work was skipped.

## Decision

**Implement, and the primitive is a hoisted Python loop — not a new Cython stepping primitive.**

The evidence supports the issue's central question in the affirmative: the per-instruction Python
scheduler loop is a measured cost centre, not a plausible one. But it also redirects where the win
is. Three findings shape the conclusion:

1. **The measured cost is Python attribute lookup, not PyBoy emulation.** The emulator core is
   already compiled — `compiled_frame_loop` retires instructions ~11.8x faster than the production
   loop on `reg-only` with 0.00009 Python calls per instruction. The gap is the Python loop
   surrounding it, not the emulation.
2. **The realistic gain is ~1.5x, not ~10x.** That is the honest ceiling for removing Python
   per-instruction overhead while preserving the scheduler's contract. The `compiled_frame_loop`
   and `compiled_batched_loop` ratios must not be claimed as achievable gains: those paths run a
   whole emulated frame inside one Python call and therefore **do not** preserve the serial-edge,
   LCD-marker, hook, owner-cancellation and partial-progress semantics the scheduler exists to
   maintain. They are upper bounds, not candidates.
3. **A new compiled primitive is not justified.** #109 asks for a bounded compiled stepping
   primitive "only for a measured bottleneck". The bottleneck is 3 `getattr` calls per instruction
   in one small method. Hoisting them is a local, low-risk change that captures the majority of the
   available win. Adding a new compiled stepping path would add a second stepping
   implementation to keep semantically aligned with the first — the exact divergence risk #72 and
   #84 exist — for a marginal gain over hoisting.

### Scope the implementation must preserve

The change is confined to `_step_single_step_chunk` in
`src/pokered_harness/link/_pyboy_link_session_stepping_mixin.py`: hoist `mb`, `cpu`, `lcd` out of
the loop, read `frame_done` and `cycles` directly. The `getattr(..., default)` forms exist for
lightweight legacy test doubles that lack `lcd` or `cpu.cycles`, so the hoisted lookups must keep
the same `None`/default semantics rather than assuming the attributes exist.

Semantics that must not change: CPU/timer/LCD/serial order, internal/external clock roles, IRQ
visibility, hook firing via `breakpoint_reinject`/`breakpoint_reached`, frame counts, transfer
limits, owner cancellation, load/reset epochs, partial-progress accounting, the
`MAX_STALLED_INSTRUCTIONS` guard, and the `stop_on_frame` frame-boundary contract. No emulated
time may be jumped and no event skipped to claim acceleration.

### Not yet done

- The hoisting change is **not implemented and not merged.** It has no patch, no tests, and no
  independent review.
- Independent review cannot be obtained: `spawn_agent` task bodies are not delivered and
  `followup_task` returns `unsupported call` (see #489). The merge contract requires independent
  review before a PR is marked ready, so this work is blocked at that step regardless of merit.
- Wall-clock numbers above were measured at host load1 12.3-14.0 on 4 CPUs and are contended. The
  instruction and cycle counts are exact and load-independent; the timings are not. A quiet-CPU
  re-measurement is required before any before/after claim is published.

## Reproduce

    scripts/bootstrap_pyboy.py --mode cython --build-evidence native-build.json
    scripts/stepping_loop_profile.py --cycles 4000000 --repeats 5 --profile-mix reg-only --json profile.json

Focused tests for the probe itself: `tests/test_stepping_loop_profile.py` — 26 passed.

#109 stays **open**. Release status stays **PARTIAL**.
