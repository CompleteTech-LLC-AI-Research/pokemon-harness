# Supplied #523 timing diagnostic: repaired admission and journal compatibility

The original two-commit user patch remains immutable in its supplied archive.
It applied cleanly at `421cebbc84f54fd59e9f1d7681984c1324a4217b`; original authors
are retained in local `git am` commits. These are source-only diagnostic tools:
the source LOCAL battle timeout is not repaired or causally diagnosed.

## Repaired behavior

Pass `--diagnostic-timing` to `python -m scripts.qualify_normal_red_link` to
explicitly enable the wall-time ledger. Without that flag, constructor defaults,
operation journal bytes and receipt fields retain their previous untimed form.
The 1200-second row deadline remains unchanged in both modes.

Phases stay in the bounded `receipt.json` timing summary (64 retained, further
marks counted). They are not mixed into `pair-operations.jsonl`, preserving its
intent/completion sequencing. Opted-in completion rows have the additive
`seconds` field; readers with exact key schemas must accept that explicit mode.
The original archive README/RERUN describe the supplied always-on candidate,
not these repaired default and phase-journal semantics. Preserve them unchanged.

Timing covers high-level awaited pair operations, operation-journal work,
release RPCs, and observation stream write/flush. Sequential wrapped intervals
can be summed; detected concurrency sets `overlap_detected=true` and
`buckets_additive=false`. Calls are never serialized to manufacture exclusivity.
Unmeasured startup/cleanup, serialization outside those stages, and observer
overhead remain outside aggregate coverage. An interruption identifies the last
exception caught within a stage, not every possible timeout location. Receipt
failure retains an original row error; after an otherwise successful row,
receipt failure still fails the command.

## Comparison with the independent opt-in profiler

The reviewed separate profiler (`04360d63` frozen source tree) observes actual
per-owner module/class origins, build fingerprints, per-RPC wall/process CPU,
returned ticks, and optional main-thread Python cProfile in a separately admitted
fixed-history run. This supplied diagnostic instead measures existing controller
awaits and journal stages. Its wall totals establish no emulation/dispatch CPU
split, native instruction attribution, speedup, or root cause. Both approaches
add observation overhead; neither establishes #109/#110 acceptance. No file in
the reviewed profiler worktree was changed by this repair.

No new ROM, profile, or gameplay run was performed. Prior signed421 seven PASS
rows and the source LOCAL timeout remain historical421 evidence, not results of
this changed source. Operator allocation under #85/#86 and new qualified actual
rows remain required. Keep assets, raw journals, profiles, and receipts outside
Git; Studio and other controllers remain untouched.
