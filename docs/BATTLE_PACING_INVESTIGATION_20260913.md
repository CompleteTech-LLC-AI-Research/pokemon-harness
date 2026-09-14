# Cross-family TCP battle pacing investigation — 2026-09-13

[Issue #82](https://github.com/CompleteDotTech/pokemon/issues/82) tracks this
correction. The full qualification remains
[issue #72](https://github.com/CompleteDotTech/pokemon/issues/72).

## Ordinary failure evidence

Full attempt `72e`, on clean
`7fc1b2901c73e94627f4a8d4910560493593f424`, failed
`test_subprocess_pair_resolves_battle_turn_over_tcp[yellow-listen-blue_color-connect]`
in source mode after 340.82 seconds. Yellow exhausted its 300-second
pre-battle window before the VS-text hook. Blue subsequently reported a serial
backend error and could not capture its final party snapshot. The parent then
rejected the incomplete peer result. The result-schema failure is a consequence,
not evidence that the result requirements should be weakened.

An ordinary source replay passed in 178.274 seconds. An ordinary native replay
failed at the 960-second pair-collection bound after both peers entered battle.
Blue's observed enemy data was already inconsistent with Yellow's fixture:
PP `[40, 30, 77, 0]` and maximum HP 109, instead of `[12, 40, 30, 30]`
and 102. That peer reached an enemy-fainted path and did not settle a valid
turn. The listener was still live at the collection deadline and was killed
by the existing collector; clean teardown is not claimed for that failure.
The two failures occurred at different stages. Passing retries do not erase
either, and the later diagnostic does not establish every event in the
uninstrumented failed runs.

## Bounded causal observation

An owner-thread observer read serial IRQ entry, block entry/store, receive
return, and nybble entry at nonadjacent pinned ROM hook addresses. It changed
host scheduling and is diagnostic evidence. The observer did not write RAM
or state, add emulator frames, or change deadlines inside the ordinary driver.
The separate outer observation bound was 450 seconds.

The native trace captured all 424 stores in each party-data block. Yellow
returned from its receive routine 74 times with its new-data flag clear,
serial-only interrupts enabled, and both timeout-counter bytes zero. Some
returns occurred while external serial bits were still outstanding. The pinned
[Yellow serial routine](https://github.com/pret/pokeyellow/blob/bfa7170107eea23b89febb60bfb2ce39173bf2e1/home/serial.asm)
returns the existing mailbox after this timeout path.

A captured Yellow sequence proves an actual stale store:

| Event | Local CPU cycles after prior store | Observed result |
|---|---:|---|
| Prior normal store | 0 | Store `0x00`; destination advances |
| Receive timeout | 5,000,804 | 71 frames later; five bits remain; mailbox still `0x00` |
| Next destination store | 5,001,780 | Store stale `0x00`; four bits remain |
| Next serial IRQ | 5,335,444 | Actual next received byte is `0x16` |

These are local cycles and frames, not synchronized cross-peer timestamps.
Both native party blocks contain only one intact copy of the expected opposite
lead record, from six identical fixture records. The source observation passed
in 183.893 seconds and retained all six expected records in each direction,
with no timeout return in its party-data block. Its sole timeout return was
in final patch-list padding, so a zero new-data flag by itself is insufficient
to classify an application failure.

The native observation was stopped by SIGTERM at its 450-second outer bound,
with supervisor exit `-15`; it has no terminal pytest verdict or observer error
totals. Its complete captured party blocks and witness sequence remain in the
partial JSONL traces. An offline summary initially assumed every repeated
native record was corrupted; that check failed and the summary was corrected
to report the measured one-of-six count in both directions.

## Driver correction and limits

Startup negotiation intentionally leaves Yellow's input-sensitive preamble
without the existing frame barrier. The battle driver previously re-enabled
it at Colosseum only for Yellow/Yellow. It now enables it for every battle
pair after both processes have observed the ROM-owned Colosseum boundary.
Frame waits retain the existing native-edge service and owner continuation.
This bounds the observed frame overrun during the subsequent party exchange;
it does not introduce a common physical clock or qualify arbitrary ROM timing.
The VS-text success log now occurs only after its hook counter is verified.

An initial diagnostic overlay failed before the changed boundary: Yellow
remained in LinkMenu while Blue reached Colosseum. Neither enabled frame
pacing; all frame counters were zero. It exited `1` after 124.137 seconds,
with both peer exits `1`. Thus it does not adjudicate the pacing candidate.
The next overlay, without optional serial hooks, exercised the boundary and
passed in 22.417 seconds: both peer exits `0`, verified settled turns, 15,808
balanced edges, 5,930 complete frame turns, and no pending work. Both are
supplemental diagnostics; neither is ordinary acceptance of committed code.
The no-hook overlay's launcher retained a generic observation-scope string;
its `observer_enabled=false` record and empty trace set identify the actual
configuration.

## Ordinary correction verification

The first ordinary correction replay passed source in 143.972890 seconds but
failed native after a valid first turn during teardown. That separate defect
is recorded in [issue #83](FRAME_SHUTDOWN_INVESTIGATION_20260913.md); the failed
native result is not acceptance of this pacing change.

| Runtime | Result | Supervisor elapsed | Peer exits | Balanced edges | Completed frame turns |
|---|---|---:|---|---:|---:|
| Source | PASS, 1/1 | 144.375999 s | 0 / 0 | 6,440 | 2,639 |
| Native | PASS, 1/1 | 22.142327 s | 0 / 0 | 17,424 | 5,460 |

Both runs execute the ordinary entrypoint with both #82 and #83 corrections,
without optional serial hooks, a driver overlay, or the controlled old helper.
Both strict settled-turn checks and independent offline peer checks pass.
Every endpoint has zero pending edges, no held final-bit response, and no
reader, owner, or IRQ error before close. All six directional frame counters
balance. No timeout or forced termination occurred.

The fixed driver SHA-256 is
`e4912e43c499ac2426e397e8200e646a3bd1f7740330e75ad98c966f1dc1a9b8`.
The runs retain the base Git commit and exact working-diff/source hashes;
those inputs match at start and finish. They are ordinary working-tree replay
evidence, pending complete qualification of the final clean commit.

The focused ownership, grouped-frame, mailbox, peer-driver, and battle-evidence
slice passes 369/369 tests per runtime, with zero failures, errors, or skips.
The changed file is a Python driver; no native source/layout input changed.
The existing complete 58-extension build remains subject to the full-gate
identity and input checks. No ROM, fixture, native hardware register, deadline,
wire payload, or strict result/settlement requirement was changed.

## Reproduction and retained evidence

Select one explicitly verified source/native interpreter, supply legal pinned
ROM/symbol/fixture roots, and retain the full output:

```sh
POKERED_ROM_ROOT="$ROM_ROOT" POKERED_FIXTURE_ROOT="$FIXTURE_ROOT" \
POKERED_PYTHON="$RUNTIME_PYTHON" PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
"$RUNTIME_PYTHON" -m pytest -q -s -rA -p pytest_asyncio.plugin \
  --strict-config --strict-markers \
  'tests/test_pyboy_link_session_subprocess.py::test_subprocess_pair_resolves_battle_turn_over_tcp[yellow-listen-blue_color-connect]'
```

For source, set `PYBOY_NO_CYTHON=1` and use the bundled vendor, `src`, and
repository roots in `PYTHONPATH`. For native, unset `PYBOY_NO_CYTHON` and omit
the vendor path. Unset diagnostic/hash-bypass variables. A writable `/dev/shm`
is required on this host. Ordinary bounds remain 300 seconds for pre-battle,
900 seconds for the peer drive, 960 seconds for collection, and 1,200 seconds
for the outer case. The peer's later rendezvous can outlast its drive bound;
the collection bound remains authoritative.

| Input | SHA-1 |
|---|---|
| Yellow ROM | `cc7d03262ebfaf2f06772c1a480c7d9d5f4a38e1` |
| Yellow symbols | `7c4205723943e7722230dcf014e5e8a2012474aa` |
| Blue-color ROM | `5f4b05725a860e04077045462176d3e2771c5022` |
| Blue symbols | `c779a0628cfc97cc9ac9db2520a1e23a2d8b7ed6` |
| Yellow battle fixture | `78c7d0b32006b11baa9ac9c71efc73b0a8d807b9` |
| Blue-color battle fixture | `6aac3aabe0f6ad662dec218682c2254b3954a10a` |

Artifacts under `target/link-issues-20260912/` retain commands, selected
interpreters, source/diff hashes, bounds, PIDs, logs, XML, and terminal records:
`*-battle-82-baseline1-*`, `*-battle-82-diagnostic-*`,
`battle-82-receive-timeout-summary.json`, `serial-order-82-*`,
`*-battle-82-focused-after1-*`, `*-battle-82-replay-after1-*`, and the final combined
`*-shutdown-83-replay-after1-*` results.

| Artifact | SHA-256 |
|---|---|
| Failed full source case, `matrix-ac59fa440c6c14c4f324.log` | `140f90058d04d587374947b5c2ef9db2370c10852b43a90b4b7496a7e1ca3061` |
| Ordinary native baseline log | `f5a06f7ef0441ca546b792f998efdb9f12a2ebd1d0ec7e34429a0ce036b04a96` |
| Native diagnostic listener JSONL | `fb7ce8e6ae5dcfd00dcebf6a1504d0542aecade36dc47c9eb23bf0813c9b805c` |
| Native diagnostic connector JSONL | `85b5def604b8eea9e71b440903b91e53f109ef4a9533e880d5a03c17526b8cb4` |
| No-hook candidate overlay log | `4fc0b4d14a8c659ee1eb6cb3a45ca745767eab082c8fae16e50f7a046e3e4136` |
| Final ordinary combined replay log, source | `8c3ce4b3a844d2e23dd205e1d5436b440d7f39c5603df0ebb8e883ec5c65a86f` |
| Final ordinary combined replay log, native | `01e42eb03e9d60dacc6499ad9a4349ed4abb585f615863ead612a3ed18bc26f9` |

The complete source/native qualification must still run on the final clean
code state. Focused checks and representative battles do not qualify the
remaining matrix, MCP gameplay, cross-host use, or other native platforms.
