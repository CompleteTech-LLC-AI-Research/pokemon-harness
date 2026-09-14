# Battle status attribution investigation, 2026-09-13

This records the bounded resolution of
[#80](https://github.com/CompleteDotTech/pokemon/issues/80), discovered during
[#72](https://github.com/CompleteDotTech/pokemon/issues/72). The complete dual
qualification remains a separate requirement; these focused results do not
establish a full gate or production readiness.

## Failure and cause

At `615f38b8e6aeca765b1ff53b38930b8fdedd1229`, the source gate's exact node
`tests/test_pyboy_link_session_roms.py::test_pair_completes_battle_turn[red-yellow]`
failed after `1195.00s`, inside the unchanged `1200s` case bound. Both observers
reported `status change lacks supported application evidence`. This was a
terminal pytest assertion failure, with exit status `1`, not a timeout.

`tests/_battle_turn_evidence.py::validate_turn` iterates over each attacker.
It checked damage against the opposing combatant but checked status against
the attacker. Consequently, a normal-move user receiving Thunderbolt's
paralysis could be rejected, while status incorrectly placed on Thunderbolt's
own user could pass. The existing timed validator already applies the current
attack's status effect to the opposing combatant.

The failed observer rejected the turn before retaining its post-turn snapshot.
Its baseline records and error do not establish its exact final status bytes.
The subsequent ordinary replays below independently capture the relevant
transition.

Before the failure, this source attempt passed unit `4554/4554`, local `55/55`,
remote `21/21`, strict trade `19/19`, and five strict battle cases. The gate was
deliberately interrupted after preserving the failure. Yellow/Blue was
interrupted; remaining source battle/timing and all native full-gate tiers
were unqualified. The supervisor exited `-15`; no owned processes survived.
There is no full-gate PASS bundle for that attempt.

The private failed log is
`full-gate-72c-raw-output/source/matrix-2bc2061bbcc189a73fe0.log`, with SHA-256
`3ac4116c0b5069779e071bb054ed1d16cf9a4b90ec00fffef047c9ba3fd3bcc2`.

## Correction and regression evidence

Status validation now reads the same target as damage validation. It retains
the supported `0 -> 64` transition, effect `6`, completed non-missed damage
application, HP accounting, local PP accounting, combatant identity, and
cross-peer agreement. ROMs, fixtures, move choice, runtime scheduling, and
acceptance deadlines are unchanged.

Twelve deterministic cases cover both peer orientations: valid incoming
paralysis; rejection of self-attributed status; and rejection of missed,
unfinished, unsupported, or peer-divergent application. The positive cases
also verify that validation leaves the snapshots unchanged.

With these regressions and the old validator, both source and native passed
`19/23` and failed exactly four cases: the two valid-paralysis cases and the
two self-attribution cases. Pytest exited `1` in each runtime. With the
correction, the final focused validator, observer, local-driver, and subprocess
helper slice passed `290/290` in each runtime, with exit `0` and no failures,
errors, or skips. Supervisor durations were source `3.767s`, native `4.033s`.

Portable focused command, repeated with each verified runtime environment:

```sh
"$PYTHON" -m pytest -q -rA -p pytest_asyncio.plugin \
  tests/test_battle_turn_evidence.py \
  tests/test_timed_battle_probe.py \
  tests/test_pyboy_link_session_roms.py \
  tests/test_pyboy_link_session_subprocess.py \
  -m 'not real_rom'
```

## Ordinary real-ROM replay

The exact failed node was replayed through the ordinary driver with the pinned
operator-managed assets. The local assertion now prints its existing immutable
settlement snapshots, allowing full logs to retain successful outcomes as the
TCP driver already does. This output does not alter observation or execution.

| Runtime | Result | Supervisor duration | Deadline / forced termination |
|---|---|---:|---|
| Bundled source | `1/1 PASS`, pytest exit `0` | `840.491s` | No timeout or forced termination |
| Complete native build | `1/1 PASS`, pytest exit `0` | `196.143s` | No timeout or forced termination |

Both runtimes captured the same agreed combatant outcomes:

| Combatant | Selected move | HP | Status |
|---|---|---|---|
| Red | Vine Whip (`22`) | `148 -> 130` | `0 -> 64` |
| Yellow | Thunderbolt (`85`, effect `6`) | `102 -> 66` | `0 -> 0` |

Each replay has two settled observer snapshots, matching completed action
evidence and PP accounting. An offline comparison against the exact old
validator rejects both retained replay snapshots with the original status
error; the corrected validator accepts them without mutation. This comparison
uses captured data and does not run or instrument an emulator.

The replayed source hashes remained unchanged from start to finish. Both
supervised processes exited normally; none remained active. The native runtime
is the complete 58-extension build from #68, with unchanged input digest
`680a5ad13aec5aa9bde9722a07342f60e75d6df8789f59eb823816ff63457ad4`.

Portable ordinary replay command, repeated under both verified environments:

```sh
"$PYTHON" -m pytest -q -s -rA -p pytest_asyncio.plugin \
  'tests/test_pyboy_link_session_roms.py::test_pair_completes_battle_turn[red-yellow]'
```

Private replay logs and their SHA-256 values:

- `source-battle-80-replay1.log`:
  `bc805f58e3df9385257b9b77ba3d414f3bf718a06ba8a01ddfdf53ec9b1d211b`
- `native-battle-80-replay1.log`:
  `68c909272c0abc27d9592cd678ddbfe23103e600ee434469a07ea4ba6df03aa0`

The private handoff also retains start/result JSON, JUnit results, both
settlement snapshots, and the offline comparison in
`battle-80-replay1-summary.json`. Raw evidence and game assets remain outside
version control. This correction qualifies status attribution for the existing
supported battle effects; it does not expand that effect catalog or replace
the complete source/native gate required by #72.
