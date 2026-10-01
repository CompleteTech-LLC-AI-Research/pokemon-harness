# Ordinary Red six-party continuation

`scripts/produce_normal_club_journey.py` continues a pinned, operator-managed
normal Route2 checkpoint through Route3 and MtMoon to Cerulean Center,
map `64`, position `(11,3)`. It requires six coherent living party records,
normal Center healing, available moves, and bounded ordinary input. It does
not perform or qualify a trade, link battle, CPU benchmark, or full release.

The parent save state is explicitly loaded. This is a continuation, not a fresh
boot. SHA1 and SHA256 pins for the state and a SHA256 pin for its parent
receipt are mandatory. The checkpoint must appear exactly once in the closed
parent receipt under its actual filename. A failed boundary cannot be selected.
A verified earlier checkpoint from a failed phase may be selected; the new
receipt retains that parent status and starts a separate bounded identity.
These checks establish byte identity and receipt inclusion, not independently
certified human-valid ancestry. Retain and independently review the full parent
chain before fixture admission.

Use the supported environment configured by the repository portable setup:

```bash
python -m scripts.produce_normal_club_journey \
  --rom /operator/inputs/pokemon-red-color.gb \
  --symbols /operator/inputs/pokemon-red.sym \
  --game-source /operator/pinned-pret-source \
  --source-state /operator/prior/ordinary_six_party_captured.state \
  --parent-receipt /operator/prior/receipt.json \
  --state-sha1 EXPECTED_STATE_SHA1 \
  --state-sha256 EXPECTED_STATE_SHA256 \
  --receipt-sha256 EXPECTED_PARENT_RECEIPT_SHA256 \
  --output /operator/new-unique-attempt \
  --seconds 600
```

The expected parent geometry is Route2 map13 `(7,2)`. Every supplied input
must match the current VERSIONS ROM/SYM/runtime pins. Consulted pret files and
blocksets must match the producer's file hashes for the recorded pinned pret
revision. Do not supply a battery, RAM snapshot, or sibling state in the ROM
input directory. Put the explicit parent state in a separate operator folder.
The producer and its imported journey, foundation and tile helpers must equal their committed bytes; Git identity must be available before launch. The exact producer commit is recorded. Output must not already exist. Keep all ROMs, symbols, states, images and raw
journals outside version control.

## Guards and retained evidence

Regular move selection requires the public battle-menu observation, regular
move-menu type, watched keys, and a readable cursor. The controller selects a
PP-qualified move outside the currently disabled slot. Cave pathfinding uses
the game's pair-collision rules. Guardian victory and Helix Fossil acceptance
require their source-defined event bits, the actual fossil item and completed
map script. The final record requires six living full-HP/status-clear records
with an available move at the exact Club starting position.

Every ordinary button or idle action has an intent/completion journal pair.
Checkpoints include state hashes, source/runtime/input pins and readbacks.
Failures retain their boundary and receipt. Public-observation initialization
must succeed before input. Session teardown always requests `save=False`;
a teardown failure keeps the result failed. The wall deadline and per-phase
input bounds are enforced without RAM, PC, PP, or repel writes.

Historical external prototypes reached the normal Cerulean starting position
from the canonical `573e` fresh Red foundation. Their review covered byte
identity, action pairs and source scope; it did not independently rerun a ROM.
Those receipts remain attributed to their original prototype hashes, including
failed phases. They are not execution evidence for this portable producer.
A new committed producer run, independently reviewed lineage and separately
admitted scenario are required before using a new fixture for strict checks.
Existing immutable fixture pins remain unchanged. Blue/Yellow fresh ancestry,
strict local/TCP trade/battle matrices and dedicated quiet CPU qualification
remain separate open conditions.

Asset-free tests use authored states and execution controls. They establish
refusal/selection/teardown behavior, not genuine gameplay or fixture legality.