# Normal-input Red foundations

`scripts.produce_normal_journey` captures a fresh Red-color journey through the
Boulder Badge. It uses ordinary buttons, ROM execution, read-only observations,
and the public battle execution observers. It does not load a previous state or
battery, write emulator RAM or the program counter, replenish PP by a shortcut,
or call the diagnostic grinding scripts.

This is a fixture producer, not a release or strict trade/battle gate. A captured
Brock state still has one Pokémon. Strict battle fixtures require at least three
living party members with usable moves and PP; catching those members and
reaching the Cable Club remain separate work. A badge, clean teardown, or matching
fixture hash does not qualify those cases.

## Inputs and execution

Use the supported Python/MCP environment and the PyBoy fork in `VERSIONS.md`.
Select source or native explicitly and verify its bootstrap/import identity.
Keep all legally obtained ROM/SYM files, game-source blocksets, output states,
and raw journals outside version control.

The operator game-source root must contain `gfx/blocksets/{overworld,reds_house,
pokecenter,forest,gym}.bst`. The producer checks every file's SHA-256 against the
bytes at `pret/pokered` commit `fbcf7d0e19a3a2db505440d3ccd3d40ca996c15c`.
These are file-level input pins, not a claim that the operator's entire checkout
has that revision. The canonical Red-color ROM and matching Red symbol hashes
must match `VERSIONS.md`.

From the repository root, after portable setup:

```sh
python -m scripts.produce_normal_journey \
  --rom "$ROM_ROOT/red/pokemon-red-color.gb" \
  --sym "$ROM_ROOT/red/pokemon-red.sym" \
  --game-source-root "$OPERATOR_POKERED_SOURCE" \
  --out "$OPERATOR_EVIDENCE_ROOT/red-fresh-brock-attempt-1" \
  --wall-seconds 600
```

The ROM's directory must not contain `.sav`, `.ram`, or `.state` inputs. Output
must be a new directory outside the checkout. An earlier attempt is never
overwritten or resumed. The chosen deadline belongs to that attempt; a later
attempt needs a separate identity and retains the earlier terminal result.

The producer bounds navigation, wild encounters, combat input, and healing.
Training uses at most 64 actual wild battles and requires observed level 13;
move selection uses readable PP, the opponent's type, and ROM-owned seed status.
Center healing requires valid full HP and full PP for the known ordinary moves.
Unavailable public battle observers, unknown menu state, fainting, insufficient
PP, a blocked path, or a reached deadline fail the attempt. Success requires the
observed Boulder Badge; the session always closes with `save=False`.

## Evidence and admission

Each button/idle operation records a durable intent before execution and a
completion afterward. Every checkpoint has a unique filename and SHA-1/SHA-256.
`receipt.json` records the exact producer/dependency hashes, ROM/SYM pins,
blockset pins, Python/runtime origin, observer initialization, action count,
deadline, elapsed time, terminal result, and teardown result. Checkpoint or close
errors retain an earlier primary failure.

Before admitting a new fixture, independently review its producer and complete
receipt chain, verify every checkpoint hash and input/completion pair, record the
actual committed producer/runtime identity, and execute the required gameplay
row. Use a new scenario identity. Do not replace historical immutable fixture
pins or describe a RAM-seeded ancestor as normal gameplay.

The earlier external Red prototype observed the Boulder Badge, but it is not an
execution of this versioned producer. Its historical failures, source identity,
and receipts remain separate. Source/native gameplay matrices, nonterminal
replacement, public MCP trade/battle completion, and quiet allocated native CPU
qualification remain open until their exact criteria have terminal evidence.
