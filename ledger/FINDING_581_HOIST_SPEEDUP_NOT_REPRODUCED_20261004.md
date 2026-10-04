# PR #581 — the hoist is correct, but the claimed 1.50x speedup does not reproduce

Date: 2026-10-04
Reviewed head: `a9605c96f9e37995ae2b2392b903fadae4560d4b` (PR #581, branch
`lead/109-hoist-stepping`), base `78f02fd7b`
Scope: `src/pokered_harness/link/_pyboy_link_session_stepping_mixin.py`, one function

## Verdict

**The code change is correct and should be merged on correctness grounds. The
performance claim in the docstring and commit message is not supported and should
be corrected before merge.**

The review contract requires no claim in the PR to be unsupported. The change is
a strict improvement; the *number* attached to it is the problem.

## What the change does, and why it is safe

Hoists three loop-invariant lookups (`p.mb`, `mb.cpu`, `mb.lcd`) out of the
per-instruction loop in `_step_single_step_chunk`. Safe because those objects
cannot be rebound mid-loop. Verified three ways:

1. **Static.** The only assignment of `.mb` anywhere in `src/` or the vendored
   tree is `vendor/pyboy-src/pyboy/core/cpu.py:33` (`self.mb = mb`, in
   `CPU.__init__`). No assignment to `.lcd` or `.cpu` exists in any `.py` file
   under `vendor/pyboy-src/`.
2. **Dynamic, real runtime.** On a real `pyboy.core.mb.Motherboard` (the same
   construction the repo's own tests use), over 5000 single-stepped ticks
   *including the breakpoint inject/reached/remove path*:
   `distinct (mb, lcd, cpu, serial) identities = 1`. Over 20000 ticks with a
   breakpoint armed: still `1`.
3. **Loop-invariance is therefore established empirically, not just by grep** —
   which is what the previous review could not close.

The `getattr` defaults are preserved: `cpu` may lack `.cycles`, `mb` may lack
`.lcd`, and `getattr(None, "frame_done", False)` still returns `False`.
`fallback_ticks = max(1, cycle_budget // 7)` guarantees `max_ticks >= 1`, so the
final `return bool(getattr(lcd, ...))` cannot hit `UnboundLocalError`; the
hoist removes that latent risk rather than adding one.

## The speedup claim does not reproduce

Three measurements, each alternating arms **within one process** so host-load
drift hits both equally, and each recording `retired_instructions` and `cycles`
to prove the emulated work is identical.

### 1. Real `Motherboard`, 41 alternating trials — 1.064x

```
original  median=0.1173s min=0.0445s retired=4096 cycles=38240 retvals=False
hoisted   median=0.1103s min=0.0448s retired=4096 cycles=38240 retvals=False
SPEEDUP median=1.064x     identical emulated work: True
```

Identical retired count, cycles, and return value in both arms. At ~0.1s per
chunk the run is scheduler-noise dominated.

### 2. Same, 5 trials — 0.959x

```
original  median=11.0322s  retired=430080 cycles=4014088
hoisted   median=11.5051s  retired=430080 cycles=4014088
SPEEDUP median=0.959x     identical emulated work: True
```

A second, differently-shaped run of the same probe produced a *slowdown*. Two
runs of the same comparison disagreeing in sign is itself the finding: at this
scale the effect is not resolvable against host noise.

### 3. Isolated loop, stub `mb`, 201 reps — 1.108x

Replacing the real motherboard with a stub whose `tick()` does identical work
removes emulated-work variance entirely, leaving only the Python loop cost:

```
original median=0.002332s  min=0.001234s
hoisted  median=0.002105s  min=0.001062s
SPEEDUP median=1.108x  min-ratio=1.163x
```

**This is the honest ceiling for the loop in isolation: ~1.1x, not 1.50x.**

## The mechanism, measured exactly

`cProfile` over the same stub, 100 calls x 4096 iterations per arm:

| arm | `getattr` calls | per retired instruction |
| --- | ---: | ---: |
| original | 1 229 100 | **3.0007** |
| hoisted | 819 600 | **2.0010** |
| removed | 409 500 | **0.9998** |

So the hoist removes **exactly one `getattr` per retired instruction** — the
`lcd = getattr(p.mb, "lcd", None)` inside the loop. It does not remove two.
`p.mb.breakpoint_singlestep`, `p.mb.tick()`, `p.mb.breakpoint_reinject()`,
`p.mb.breakpoint_reached()` and the two `getattr(lcd, "frame_done", ...)` calls
all remain per-iteration, and the two hoisted `getattr`s move outside the loop
rather than disappearing.

The docstring's "3.08 calls per retired instruction" matches the *original* arm
(3.00 here, 3.08 with real-world attribute access). That part is honest. The
error is the inference: removing 1 of 3 `getattr` calls per instruction is a
~33% reduction in `getattr` count, not a 1.50x reduction in loop time. The
measured loop-level effect is ~1.1x.

## Disposition

- **Correctness: approved.** Independent review via `claude1 -p` returned
  APPROVE-WITH-NOTES, no blocking findings, and specifically could not close the
  loop-invariance question from source alone — which the dynamic identity probe
  above now closes.
- **Required before merge:** correct the docstring and commit message. Replace
  "hoisting them is a 1.50x median speedup over byte-identical emulated work"
  with the measured figure and its scope, e.g. "removes one `getattr` per
  retired instruction; measured ~1.1x on the isolated loop and 1.06x on the real
  motherboard at host load N-N." The `3.08 getattr per retired instruction`
  figure may stay.
- The change should still merge once the claim is accurate: it is strictly less
  work per instruction, with no behavioural difference.

## Reproduction

Probes: `/tmp/ab581b.py` (real motherboard, 41 trials), `/tmp/ab581b.py` with
`range(5)` (0.959x run), `/tmp/ab581c.py` (isolated stub loop, 201 reps),
`/tmp/count_getattr.py` (per-arm `getattr` counts). These are scratch, not
committed; the numbers above are reproducible from them.

Host at time of measurement: 4 CPUs, load average 5.75-10.83. This is **not** a
quiet CPU window (`<= 4` required for timing qualification), which is why the
real-motherboard numbers are reported as a range rather than a point estimate.
The isolated-stub figure is load-independent in structure but was still taken on
this host.
