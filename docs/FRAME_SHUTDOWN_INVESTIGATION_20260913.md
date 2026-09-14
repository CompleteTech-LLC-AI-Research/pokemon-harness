# Frame-paced TCP shutdown investigation — 2026-09-13

[Issue #83](https://github.com/CompleteDotTech/pokemon/issues/83) records this
shutdown correction. It was exposed while verifying
[the battle-pacing correction in #82](BATTLE_PACING_INVESTIGATION_20260913.md).
Full source/native qualification remains issue #72.

## Failure after a valid first turn

The ordinary native Yellow-listener / Blue-color-connector battle on base
`7fc1b2901c73e94627f4a8d4910560493593f424`, with the #82 driver change,
failed after 33.244755 seconds. Both immutable battle snapshots pass the
existing turn verifier offline. Baselines agree on the exact party data;
Yellow finishes the first turn at HP 23 and Blue at HP 132, with completed
moves, matching opposite views, and one local PP decrement each.

The existing ten-second post-turn input drain can start another turn. At
shutdown Yellow logged completion and detached, but its pre-close snapshot
still contained a held final-bit response: 20,992 applied requests, only
20,991 responses sent, one pending edge, and 5,234 received frame ticks versus
5,233 completed frame DONE/ACK turns. Blue held the matching request and frame,
then reached the existing ten-second edge-response timeout. Its final party
snapshot was unavailable, and the parent correctly rejected the result.
A valid retained first turn is not successful teardown.

The ordinary source replay passed in 143.972890 seconds with 6,440 balanced
edges, both peer exits zero, valid settled turns, and no pending work. It does
not erase the native failure. No optional observer or driver overlay was
active in either ordinary replay.

## Ordering defect and correction

The old shutdown helper continued starting ordinary frame turns after its
readiness and release markers. Its final transport-only quiet observation
could precede a newly admitted peer frame. An incoming completed byte then
required the owner continuation from #79, while the retiring owner only
serviced its edge queue or had already detached. A quiet gap is not a fence
against future frame admission.

For a frame-paced caller, the corrected helper uses the existing negotiated
pacing direction and real frame accounting:

1. Both owners finish their initial quiet observation and publish readiness
   outside their frame calls.
2. The leader publishes release and admits no new frame turns.
3. The follower continues only an already-received `FRAME_TICK`; it never
   waits for the leader to invent another turn. This also covers a turn
   admitted just before the leader observed the follower's readiness.
4. Both observe release and perform final transport-only idle verification
   before detaching.

The frame ACK and control markers are ordered on their existing TCP stream.
The leader cannot release before its last frame ACK, and no later frame can
create work during the final quiet check. The helper uses one remaining-time
budget across its phases; ordinary frame and transport operations retain their
existing bounds. No native clock role, serial register, wire payload, emulator
frame implementation, or acceptance predicate changes.

The non-frame-paced helper remains distinct. Its blanket exception suppression
is removed: a queued expected marker cannot excuse an unrelated progress
exception. This correction does not claim a general physical-time protocol
or arbitrary-ROM timing support.

## Controlled regressions

The initial three regression cases failed in each runtime. Native recorded
an actual leader frame attempt after the peer completed shutdown; source
stopped earlier because the follower still required frame progress. Both
runtimes also swallowed an injected RuntimeError and KeyboardInterrupt when
a marker became available during the failed progress call.

The first corrected source run passed. The first corrected native run exposed
an overly blocking marker wait in the test harness, which prevented an already
admitted frame from completing. The test was corrected to delay only a marker
already sent. The final four cases cover both a late readiness marker and a
late release marker over real `NetworkBackend` sockets, including completion
of a turn after the follower has announced release, plus both exception types.
They verify successful owners and complete frame accounting.

The final four cases pass in source and native, with zero failures, errors,
or skips. A separate controlled comparison loads the retained `7fc1b29`
shutdown function into the same finalized unit tests, adding only an ignored
metadata keyword for signature compatibility. It fails all four cases in each
runtime. This is a unit comparison, not an ordinary ROM replay or a change to
tracked source. Neither old-helper frame case completes the required release
handshake; the original native before-run retains the direct post-release
admission event.

The broader ownership, frame, mailbox, peer-driver, and battle-evidence slice
passes 373/373 in each runtime. Ruff passes. The implementation and tests are
Python files; the complete native extension build inputs are unchanged.

## Ordinary verification with both corrections

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

## Retained evidence and reproduction

Artifacts are under `target/link-issues-20260912/`:
`native-battle-82-replay-after1-*`, its separately parsed failed peer records,
`*-shutdown-83-causal-before1-*`, `*-shutdown-83-causal-before2-*`,
`*-shutdown-83-causal-after1-*`, `*-shutdown-83-causal-after2-*`,
`*-shutdown-83-focused-after1-*`, and `*-shutdown-83-replay-after1-*`.
The records retain commands, runtime paths, source/diff hashes, bounds,
XML, full logs, and terminal status. The controlled old-helper comparison
also records its plugin hash and exact retained function source.

| Artifact | SHA-256 |
|---|---|
| Ordinary native failure log | `781302e410684212fc968b55cedc32d399fdaced6af2a5e94ac89dab426f94bf` |
| Final controlled old-helper comparison, source | `7e747107b8647bab5242cb80f76882d2c0c5105cb4ef19dacf16196642f859fc` |
| Final controlled old-helper comparison, native | `c1a229cd59ce6a7d39d22052e986605654f14e69e42b554729637856cec47e32` |
| Final corrected causal slice, source | `89e9ab16755b96715db474b57e8f8788d09a73a0823840b5cd15ecd117140f23` |
| Final corrected causal slice, native | `a00585ff9cd18d793a08316057d5f914aae70d10ce1a3b19e85faac2d3b0a9a1` |
| Final ordinary combined replay log, source | `8c3ce4b3a844d2e23dd205e1d5436b440d7f39c5603df0ebb8e883ec5c65a86f` |
| Final ordinary combined replay log, native | `01e42eb03e9d60dacc6499ad9a4349ed4abb585f615863ead612a3ed18bc26f9` |

Use the source/native environment selection and exact legal ROM/SYM/fixture
pins in [the battle-pacing investigation](BATTLE_PACING_INVESTIGATION_20260913.md).
The focused ROM-free command is:

```sh
python -m pytest -q -rA tests/test_peer_frame_shutdown.py
```

The ordinary failing node is
`tests/test_pyboy_link_session_subprocess.py::test_subprocess_pair_resolves_battle_turn_over_tcp[yellow-listen-blue_color-connect]`.
Run it without the controlled comparison plugin, optional observer, or driver
overlay. It retains the original peer, pair, and outer bounds. Require both
successful peer exits, matching settled turns, balanced frame and edge
accounting, no held response, and no reader/owner/IRQ error before close.
A fresh complete source/native qualification is still required for issue #72.
