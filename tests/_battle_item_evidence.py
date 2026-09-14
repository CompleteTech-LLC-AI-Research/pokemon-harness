"""Shared, ROM-free contract for ordinary battle-item use and turn accounting.

This module encodes the reusable inventory/target and action-timeline
assertions for the ordinary medicine workstream (#90-#94).  It is pure and
deterministic: it never imports emulator state, ROM/symbol bytes, save states,
or fixtures.  Every effect rule is an explicit parameter so a wrong amount,
status mask, or continuation rule is testable instead of being hidden inside a
captured fixture.

Pinned Gen I behavior is cited from the pre-decompilation sources:

* Red/Blue ``engine/items/item_effects.asm`` at
  ``fbcf7d0e19a3a2db505440d3ccd3d40ca996c15c``.
* Yellow ``engine/items/item_effects.asm`` at
  ``bfa7170107eea23b89febb60bfb2ce39173bf2e1``.
* Red/Blue ``engine/battle/core.asm`` at the same Red/Blue revision for the
  ``wActionResultOrTookBattleTurn`` turn hand-off and the opponent action.

No ROM address or struct offset is hard-coded.  ``MedicineRule`` and
``ContinuationRule`` values are declared inputs; the helpers below validate an
observed snapshot/timeline against them but never read the game themselves.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

# Bag layout constants mirror ``src/pokered_harness/state/bag.py``; they are
# engine invariants, not addresses.
NO_ITEM = 0x00
BAG_TERMINATOR = 0xFF
MAX_BAG_STACKS = 20
MAX_ITEM_QUANTITY = 99

# Non-volatile status byte bits, per pokered
# ``constants/battle_constants.asm``: SLP_MASK is the low three sleep-counter
# bits; PSN/BRN/FRZ/PAR are bits 3/4/5/6 and Full Heal clears $ff.
STATUS_SLEEP_MASK = 0x07
STATUS_POISON = 0x08
STATUS_BURN = 0x10
STATUS_FREEZE = 0x20
STATUS_PARALYSIS = 0x40
STATUS_ALL = 0xFF

# Fixed HP-restore amounts.  ``item_effects.asm`` (``.notUsingSoftboiled2``)
# loads 20 for Potion, 50 for Super Potion, and 200 for Hyper Potion.  Max
# Potion and Full Restore take the ``.setCurrentHPToMaxHp`` branch instead.
POTION_HEAL = 20
SUPER_POTION_HEAL = 50
HYPER_POTION_HEAL = 200

# Reason labels.  ``INVALID_TARGET_REASONS`` are items the pinned routine
# refuses to apply at all; ``VALID_NO_EFFECT_REASONS`` are eligible targets
# whose state makes the item a no-op (``.healingItemNoEffect``).
REASON_APPLIED = "applied"
REASON_FULL_HP = "full_hp"
REASON_FULL_HP_NO_STATUS = "full_hp_no_status"
REASON_NO_STATUS = "no_status"
REASON_WRONG_STATUS = "wrong_status"
REASON_REVIVE_ON_LIVING = "revive_on_living"
REASON_FAINTED_NON_REVIVE = "fainted_non_revive"
INVALID_TARGET_REASONS = frozenset({REASON_REVIVE_ON_LIVING, REASON_FAINTED_NON_REVIVE})
VALID_NO_EFFECT_REASONS = frozenset(
    {REASON_FULL_HP, REASON_FULL_HP_NO_STATUS, REASON_NO_STATUS, REASON_WRONG_STATUS}
)

# Ordered action-timeline event kinds (#94).
EVENT_COMMAND_SELECTION = "command_selection"
EVENT_ITEM_SELECTION = "item_selection"
EVENT_APPLICATION = "application"
EVENT_REJECTION = "rejection"
EVENT_OPPONENT_ACTION = "opponent_action"
EVENT_PLAYER_MOVE = "player_move"
EVENT_RESIDUAL = "residual"
EVENT_REPLACEMENT = "replacement"
EVENT_NEXT_COMMAND = "next_command"
EVENT_TERMINAL = "terminal"

EVENT_KINDS = frozenset(
    {
        EVENT_COMMAND_SELECTION,
        EVENT_ITEM_SELECTION,
        EVENT_APPLICATION,
        EVENT_REJECTION,
        EVENT_OPPONENT_ACTION,
        EVENT_PLAYER_MOVE,
        EVENT_RESIDUAL,
        EVENT_REPLACEMENT,
        EVENT_NEXT_COMMAND,
        EVENT_TERMINAL,
    }
)
OUTCOME_KINDS = frozenset({EVENT_APPLICATION, EVENT_REJECTION})
BOUNDARY_KINDS = frozenset({EVENT_NEXT_COMMAND, EVENT_REPLACEMENT, EVENT_TERMINAL})
LATER_EFFECT_KINDS = frozenset({EVENT_OPPONENT_ACTION, EVENT_RESIDUAL})


def _integer(value: Any, low: int, high: int, label: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _tuple4(value: Any, label: str) -> tuple[int, int, int, int]:
    if not isinstance(value, tuple) or len(value) != 4:
        raise ValueError(f"invalid {label}: {value!r}")
    for item in value:
        _integer(item, 0, 255, label)
    return value


# ---------------------------------------------------------------------------
# Inventory / target snapshots (#93)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BagStack:
    """One ordered bag stack: item ID and remaining quantity."""

    item_id: int
    quantity: int


@dataclass(frozen=True, slots=True)
class BagSnapshot:
    """Ordered, compacted bag stacks plus the observed bag metadata.

    Inventory acceptance requires the ROM-observed ``raw_count``, the
    ``terminator_position`` (index of the ``$FF`` terminator), and a ``valid``
    tri-state copied from the production parser.  A snapshot with
    ``valid`` not ``True`` or with missing metadata is rejected before any
    consumption assertion can trust it.
    """

    stacks: tuple[BagStack, ...]
    terminator: int = BAG_TERMINATOR
    raw_count: int | None = None
    terminator_position: int | None = None
    valid: bool | None = None

    def quantity_of(self, item_id: int) -> int:
        return sum(stack.quantity for stack in self.stacks if stack.item_id == item_id)

    def has(self, item_id: int) -> bool:
        return any(stack.item_id == item_id for stack in self.stacks)


@dataclass(frozen=True, slots=True)
class PartyMonSnapshot:
    """One party record, identified by its stable party slot."""

    slot: int
    species: int
    level: int
    hp: int
    max_hp: int
    status: int
    moves: tuple[int, int, int, int]
    pp: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class ActiveBattleSnapshot:
    """The active ``wBattleMon`` view; ``player_slot`` names its party member."""

    player_slot: int
    species: int
    level: int
    hp: int
    max_hp: int
    status: int
    moves: tuple[int, int, int, int]
    pp: tuple[int, int, int, int]
    turn_consumed: bool = False


@dataclass(frozen=True, slots=True)
class ItemUseSnapshot:
    """A before/after observation keyed by used item and selected party slot."""

    item_id: int
    target_slot: int
    bag: BagSnapshot
    party: tuple[PartyMonSnapshot, ...]
    active: ActiveBattleSnapshot

    def mon(self, slot: int) -> PartyMonSnapshot:
        for mon in self.party:
            if mon.slot == slot:
                return mon
        raise KeyError(f"party has no slot {slot}")


def validate_snapshot(snapshot: ItemUseSnapshot) -> None:
    """Reject malformed observations before an assertion can trust them."""
    if type(snapshot) is not ItemUseSnapshot:
        raise TypeError("snapshot is not an ItemUseSnapshot")
    _integer(snapshot.item_id, 1, 254, "item id")
    _integer(snapshot.target_slot, 0, 5, "target slot")
    _validate_bag(snapshot.bag)
    _validate_party(snapshot.party)
    _validate_active(snapshot.active)


def _validate_bag(bag: BagSnapshot) -> None:
    if type(bag) is not BagSnapshot:
        raise TypeError("bag is not a BagSnapshot")
    if bag.terminator != BAG_TERMINATOR:
        raise ValueError(f"invalid bag terminator: {bag.terminator!r}")
    if bag.valid is not True:
        raise ValueError(f"bag observation is not valid: {bag.valid!r}")
    if bag.raw_count is None:
        raise ValueError("bag raw count is required for inventory acceptance")
    if bag.terminator_position is None:
        raise ValueError("bag terminator position is required for inventory acceptance")
    if len(bag.stacks) > MAX_BAG_STACKS:
        raise ValueError("bag stack count exceeds capacity")
    if bag.raw_count != len(bag.stacks):
        raise ValueError(
            "bag raw count does not match the compacted stack count "
            f"({bag.raw_count} != {len(bag.stacks)})"
        )
    if bag.terminator_position != len(bag.stacks):
        raise ValueError(
            "bag terminator does not immediately follow the last stack "
            f"({bag.terminator_position} != {len(bag.stacks)})"
        )
    seen: set[int] = set()
    for stack in bag.stacks:
        _integer(stack.item_id, NO_ITEM + 1, BAG_TERMINATOR - 1, "bag item id")
        _integer(stack.quantity, 1, MAX_ITEM_QUANTITY, "bag quantity")
        if stack.item_id in seen:
            raise ValueError("duplicate bag item stack")
        seen.add(stack.item_id)


def _validate_party(party: tuple[PartyMonSnapshot, ...]) -> None:
    if not isinstance(party, tuple) or not party:
        raise ValueError("party snapshot is empty")
    if tuple(mon.slot for mon in party) != tuple(range(len(party))):
        raise ValueError("party slots must be contiguous from zero")
    for mon in party:
        _validate_mon(mon, label="party")


def _validate_active(active: ActiveBattleSnapshot) -> None:
    if type(active) is not ActiveBattleSnapshot:
        raise TypeError("active is not an ActiveBattleSnapshot")
    _integer(active.player_slot, 0, 5, "active player slot")
    if type(active.turn_consumed) is not bool:
        raise ValueError("invalid active turn_consumed flag")
    _validate_mon(active, label="active")


def _validate_mon(mon: PartyMonSnapshot | ActiveBattleSnapshot, *, label: str) -> None:
    _integer(mon.species, 1, 190, f"{label} species")
    _integer(mon.level, 1, 100, f"{label} level")
    max_hp = _integer(mon.max_hp, 1, 65535, f"{label} max HP")
    hp = _integer(mon.hp, 0, 65535, f"{label} HP")
    if hp > max_hp:
        raise ValueError(f"{label} HP exceeds max HP")
    _integer(mon.status, 0, 255, f"{label} status")
    _tuple4(mon.moves, f"{label} moves")
    _tuple4(mon.pp, f"{label} PP")


def _consume_stack(
    stacks: tuple[BagStack, ...], item_id: int, consumed: int
) -> tuple[BagStack, ...]:
    """Return the expected post-use stacks: decrement then compact at zero."""
    result: list[BagStack] = []
    for stack in stacks:
        if stack.item_id != item_id:
            result.append(stack)
            continue
        remainder = stack.quantity - consumed
        if remainder < 0:
            raise AssertionError("bag quantity underflow while consuming item")
        if remainder > 0:
            result.append(BagStack(stack.item_id, remainder))
    return tuple(result)


def assert_single_consumption(
    before: ItemUseSnapshot,
    after: ItemUseSnapshot,
    item_id: int,
    *,
    expected_consumed: int,
) -> None:
    """Assert the observed inventory delta equals an explicit expectation.

    ``expected_consumed`` is supplied by the caller from the pinned item
    routine; it is never inferred from a menu closing, an HP change, or text
    advancement.  A wrong expectation (including a duplicated consumption)
    fails here.  The comparison also proves last-unit compaction, terminator
    correctness, unchanged remaining stacks, and no underflow.
    """
    validate_snapshot(before)
    validate_snapshot(after)
    _integer(item_id, 1, 254, "item id")
    if expected_consumed not in (0, 1):
        raise ValueError("expected_consumed must be exactly 0 or 1")
    if before.item_id != item_id:
        raise AssertionError("before snapshot is for a different item")
    if after.item_id != item_id:
        raise AssertionError("after snapshot is for a different item")
    before_quantity = before.bag.quantity_of(item_id)
    after_quantity = after.bag.quantity_of(item_id)
    if expected_consumed and before_quantity < expected_consumed:
        raise AssertionError(
            f"inventory underflow: {before_quantity} units available, {expected_consumed} expected"
        )
    observed = before_quantity - after_quantity
    if observed != expected_consumed:
        raise AssertionError(f"expected {expected_consumed} unit(s) consumed, observed {observed}")
    expected_stacks = _consume_stack(before.bag.stacks, item_id, expected_consumed)
    if after.bag.stacks != expected_stacks:
        raise AssertionError("bag stacks changed beyond the single consumption")
    if after.bag.terminator != before.bag.terminator:
        raise AssertionError("bag terminator changed during item use")


def assert_last_unit_compaction(
    before: ItemUseSnapshot, after: ItemUseSnapshot, item_id: int
) -> None:
    """Assert the final unit is consumed and its empty entry is removed."""
    if before.bag.quantity_of(item_id) != 1:
        raise AssertionError("before snapshot does not hold the last unit")
    assert_single_consumption(before, after, item_id, expected_consumed=1)
    if after.bag.has(item_id):
        raise AssertionError("empty stack survived the last unit's removal")


def assert_continuation_idempotent(
    before: ItemUseSnapshot,
    after: ItemUseSnapshot,
    continued: ItemUseSnapshot,
    item_id: int,
) -> None:
    """Advancing text over the settled snapshot must not decrement again.

    ``after`` is the settled post-use snapshot and ``continued`` is a distinct
    snapshot taken after the text advance.  The first consumption is verified
    against ``before``; the second comparison is between ``after`` and the
    separate ``continued`` snapshot, so it can detect a duplicated decrement
    rather than comparing a value with itself.
    """
    assert_single_consumption(before, after, item_id, expected_consumed=1)
    assert_single_consumption(after, continued, item_id, expected_consumed=0)


def assert_selected_target_changes_only(before: ItemUseSnapshot, after: ItemUseSnapshot) -> None:
    """Only the selected slot may change state; its identity must be stable."""
    if len(before.party) != len(after.party):
        raise AssertionError("party size changed during item use")
    target_before = before.mon(before.target_slot)
    target_after = after.mon(after.target_slot)
    identity = ("slot", "species", "level", "max_hp", "moves", "pp")
    for field in identity:
        if getattr(target_before, field) != getattr(target_after, field):
            raise AssertionError(f"target {field} changed during item use")
    for mon_before in before.party:
        if mon_before.slot == before.target_slot:
            continue
        if after.mon(mon_before.slot) != mon_before:
            raise AssertionError(f"unselected party slot {mon_before.slot} changed during item use")


def assert_active_battle_refresh(before: ItemUseSnapshot, after: ItemUseSnapshot) -> None:
    """Active data mirrors the selected target, and only then.

    When the item targets the active mon, the ROM copies the resolved party HP
    (and Full Restore's status) into ``wBattleMon`` while leaving the active
    identity (species/level/moves/PP) untouched.  When the target is benched,
    the active battle record must not be overwritten with the benched member's
    identity, HP, status, or moves.
    """
    target_is_active = before.active.player_slot == before.target_slot
    if not target_is_active:
        if after.active != before.active:
            raise AssertionError("benched item target refreshed active battle data")
        return
    target = after.mon(after.target_slot)
    if after.active.hp != target.hp or after.active.status != target.status:
        raise AssertionError("active battle HP/status did not refresh from the target")
    if after.active.max_hp != target.max_hp:
        raise AssertionError("active battle max HP does not mirror the selected target")
    for field in ("player_slot", "species", "level", "max_hp", "moves", "pp"):
        if getattr(before.active, field) != getattr(after.active, field):
            raise AssertionError(f"active battle {field} changed during item use")


# ---------------------------------------------------------------------------
# Medicine math and application (#93)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MedicineRule:
    """Declared medicine behavior; no value is read from a real ROM.

    ``fixed_heal`` is the fixed restore amount, ``heal_to_max`` selects the
    Max Potion/Max Revive branch, ``revive`` selects the Revive branch, and
    ``cure_mask`` is the status bitmask the item clears.
    """

    item_id: int
    fixed_heal: int = 0
    heal_to_max: bool = False
    revive: bool = False
    cure_mask: int = 0


@dataclass(frozen=True, slots=True)
class MedicineOutcome:
    applied: bool
    hp: int
    status: int
    reason: str


def _validate_rule(rule: MedicineRule) -> None:
    if type(rule) is not MedicineRule:
        raise TypeError("rule is not a MedicineRule")
    _integer(rule.item_id, 1, 254, "medicine item id")
    _integer(rule.fixed_heal, 0, 65535, "fixed heal amount")
    _integer(rule.cure_mask, 0, 255, "cure mask")
    if type(rule.heal_to_max) is not bool or type(rule.revive) is not bool:
        raise ValueError("invalid medicine branch flag")
    if rule.fixed_heal and rule.heal_to_max:
        raise ValueError("fixed and to-max healing are mutually exclusive")


def capped_heal(before_hp: int, max_hp: int, amount: int) -> int:
    """``min(max_hp, before_hp + amount)`` for a fixed-heal medicine."""
    _integer(before_hp, 0, 65535, "before HP")
    _integer(max_hp, 1, 65535, "max HP")
    _integer(amount, 0, 65535, "heal amount")
    if before_hp > max_hp:
        raise ValueError("before HP exceeds max HP")
    return min(max_hp, before_hp + amount)


def revive_hp(max_hp: int, *, to_max: bool) -> int:
    """``max_hp`` for Max Revive, else ``floor(max_hp / 2)``.

    ``item_effects.asm`` shifts the high byte then rotates the low byte,
    which is an exact 16-bit floor division by two.
    """
    _integer(max_hp, 1, 65535, "max HP")
    if type(to_max) is not bool:
        raise ValueError("to_max must be a bool")
    return max_hp if to_max else max_hp // 2


def status_matches_mask(status: int, mask: int) -> bool:
    """Eligibility only: does the status byte hold a bit the item can cure?

    ``item_effects.asm`` computes ``ld a, [hl]; and c`` where ``c`` is the
    item's mask and branches to ``.healingItemNoEffect`` when the result is
    zero.  It never consults current HP here.
    """
    _integer(status, 0, 255, "status")
    _integer(mask, 0, 255, "cure mask")
    return (status & mask) != 0


def cure_status(status: int, mask: int) -> int:
    """Status write: the pinned routine zeroes the ENTIRE byte once eligible.

    The mask is used only for eligibility (see ``status_matches_mask``); the
    write itself is ``xor a; ld [hl], a``, so a matching mask clears every
    status bit, not just the masked ones.  A non-matching status is unchanged.
    """
    _integer(status, 0, 255, "status")
    _integer(mask, 0, 255, "cure mask")
    return 0 if status_matches_mask(status, mask) else status


def medicine_outcome(rule: MedicineRule, mon: PartyMonSnapshot) -> MedicineOutcome:
    """Resolve the pinned Gen I branch for one target record."""
    _validate_rule(rule)
    _validate_mon(mon, label="party")
    if rule.revive:
        if mon.hp != 0:
            return MedicineOutcome(False, mon.hp, mon.status, REASON_REVIVE_ON_LIVING)
        return MedicineOutcome(
            True,
            revive_hp(mon.max_hp, to_max=rule.heal_to_max),
            mon.status,
            REASON_APPLIED,
        )
    is_hp_medicine = rule.fixed_heal > 0 or rule.heal_to_max
    if not is_hp_medicine:
        # Status-only medicine: the pinned ``.cureStatusAilment`` path masks
        # eligibility but performs no HP check, so a fainted target is still
        # eligible; a matching mask zeroes the entire status byte.
        if not status_matches_mask(mon.status, rule.cure_mask):
            return MedicineOutcome(False, mon.hp, mon.status, REASON_WRONG_STATUS)
        return MedicineOutcome(
            True, mon.hp, cure_status(mon.status, rule.cure_mask), REASON_APPLIED
        )
    if mon.hp == 0:
        return MedicineOutcome(False, mon.hp, mon.status, REASON_FAINTED_NON_REVIVE)
    if mon.hp >= mon.max_hp:
        if not rule.cure_mask:
            return MedicineOutcome(False, mon.hp, mon.status, REASON_FULL_HP)
        if mon.status == 0:
            return MedicineOutcome(False, mon.hp, mon.status, REASON_FULL_HP_NO_STATUS)
        return MedicineOutcome(
            True, mon.hp, cure_status(mon.status, rule.cure_mask), REASON_APPLIED
        )
    if rule.heal_to_max:
        healed = mon.max_hp
    else:
        healed = capped_heal(mon.hp, mon.max_hp, rule.fixed_heal)
    status = cure_status(mon.status, rule.cure_mask) if rule.cure_mask else mon.status
    return MedicineOutcome(True, healed, status, REASON_APPLIED)


def assert_medicine_application(
    before: ItemUseSnapshot,
    after: ItemUseSnapshot,
    rule: MedicineRule,
    *,
    expected_consumed: int,
) -> MedicineOutcome:
    """Validate target math, isolated target change, active refresh, and delta."""
    validate_snapshot(before)
    validate_snapshot(after)
    if rule.item_id != before.item_id:
        raise AssertionError("medicine rule does not describe the used item")
    if after.item_id != before.item_id:
        raise AssertionError("after snapshot is for a different item")
    if before.target_slot != after.target_slot:
        raise AssertionError("selected target slot changed during item use")
    outcome = medicine_outcome(rule, before.mon(before.target_slot))
    target_after = after.mon(after.target_slot)
    if (target_after.hp, target_after.status) != (outcome.hp, outcome.status):
        raise AssertionError("target HP/status does not match the declared medicine outcome")
    assert_selected_target_changes_only(before, after)
    assert_active_battle_refresh(before, after)
    assert_single_consumption(before, after, before.item_id, expected_consumed=expected_consumed)
    return outcome


# ---------------------------------------------------------------------------
# Ordered action timeline and turn accounting (#94)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    """One ordered observation at a battle boundary."""

    kind: str
    item_id: int | None = None
    target_slot: int | None = None
    consumed: int = 0
    consumes_action: bool = False
    pp_decrements: int = 0
    hp_delta: int = 0
    reason: str = ""

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"unknown timeline event kind: {self.kind!r}")
        if self.item_id is not None:
            _integer(self.item_id, 1, 254, "event item id")
        if self.target_slot is not None:
            _integer(self.target_slot, 0, 5, "event target slot")
        _integer(self.consumed, 0, 1, "event consumed count")
        _integer(self.pp_decrements, 0, 255, "event PP decrements")
        if type(self.consumes_action) is not bool:
            raise ValueError("event consumes_action must be a bool")


@dataclass(frozen=True, slots=True)
class ContinuationRule:
    """Declared continuation after an item attempt.

    The pinned Red/Blue routine initialises ``wActionResultOrTookBattleTurn``
    to success and clears it on ``ItemUseFailed``/``.canceledItemUse``; the
    battle loop then skips the move menu and lets the opponent act only when
    the flag stays set.  Those exact values are the caller's declaration, not
    a universal assumption: a different game/branch can declare another rule.
    """

    label: str
    consumes_player_action: bool
    consumes_item: int
    opponent_action_due: bool


SUCCESSFUL_ITEM_CONTINUATION = ContinuationRule(
    "successful_item", consumes_player_action=True, consumes_item=1, opponent_action_due=True
)
NO_EFFECT_CONTINUATION = ContinuationRule(
    "no_effect", consumes_player_action=False, consumes_item=0, opponent_action_due=False
)
CANCELLATION_CONTINUATION = ContinuationRule(
    "cancellation", consumes_player_action=False, consumes_item=0, opponent_action_due=False
)
UNAVAILABLE_ITEM_CONTINUATION = ContinuationRule(
    "unavailable_item", consumes_player_action=False, consumes_item=0, opponent_action_due=False
)
# Declared alternative used only to prove the continuation is an input rather
# than a hard-coded universal: a branch that fails yet still spends the turn.
TURN_CONSUMING_FAILURE_CONTINUATION = ContinuationRule(
    "turn_consuming_failure",
    consumes_player_action=True,
    consumes_item=0,
    opponent_action_due=True,
)


@dataclass(frozen=True, slots=True)
class TurnAccount:
    consumes_player_action: bool
    item_consumed: int
    opponent_actions: int
    player_moves: int
    pp_decrements: int
    applied: bool
    reason: str


class ActionTimeline:
    """Ordered ordinary-battle observations for a single item attempt."""

    def __init__(self) -> None:
        self._events: list[TimelineEvent] = []

    def record(self, kind: str, **fields: Any) -> TimelineEvent:
        event = TimelineEvent(kind, **fields)
        self._events.append(event)
        return event

    @property
    def events(self) -> tuple[TimelineEvent, ...]:
        return tuple(self._events)

    def count(self, kind: str) -> int:
        return sum(1 for event in self._events if event.kind == kind)

    def kinds(self) -> tuple[str, ...]:
        return tuple(event.kind for event in self._events)

    def snapshot(self) -> dict[str, Any]:
        return {"events": [asdict(event) for event in self._events]}


def account_timeline(timeline: ActionTimeline, *, continuation: ContinuationRule) -> TurnAccount:
    """Validate ordering against the declared rule and return account totals."""
    events = timeline.events
    if not events:
        raise AssertionError("timeline is empty")
    if events[0].kind != EVENT_COMMAND_SELECTION:
        raise AssertionError("timeline must begin with command selection")

    selections = [index for index, event in enumerate(events) if event.kind == EVENT_ITEM_SELECTION]
    if len(selections) != 1:
        raise AssertionError("timeline must contain exactly one item/target selection")
    selection = selections[0]

    outcomes = [index for index, event in enumerate(events) if event.kind in OUTCOME_KINDS]
    if len(outcomes) != 1:
        raise AssertionError("timeline must contain exactly one application or rejection")
    outcome_index = outcomes[0]
    if outcome_index <= selection:
        raise AssertionError("item outcome must follow item/target selection")
    outcome = events[outcome_index]
    selection_event = events[selection]
    if (
        selection_event.item_id != outcome.item_id
        or selection_event.target_slot != outcome.target_slot
    ):
        raise AssertionError("item outcome identity does not match the selected item/target")

    expected_kind = EVENT_APPLICATION if continuation.consumes_item else EVENT_REJECTION
    if outcome.kind != expected_kind:
        raise AssertionError(f"declared rule expects {expected_kind}, observed {outcome.kind}")

    boundaries = [index for index, event in enumerate(events) if event.kind in BOUNDARY_KINDS]
    if len(boundaries) != 1:
        raise AssertionError("timeline must reach exactly one boundary")
    if boundaries[0] != len(events) - 1:
        raise AssertionError("the terminal boundary must be the final event")

    opponents = [index for index, event in enumerate(events) if event.kind == EVENT_OPPONENT_ACTION]
    expected_opponents = 1 if continuation.opponent_action_due else 0
    if len(opponents) != expected_opponents:
        raise AssertionError(
            f"declared rule expects {expected_opponents} opponent action(s), "
            f"observed {len(opponents)}"
        )
    if any(index < outcome_index for index in opponents):
        raise AssertionError("opponent action precedes the item application boundary")

    moves = timeline.count(EVENT_PLAYER_MOVE)
    if moves:
        raise AssertionError("an item action must not contain a hidden player move")
    if any(event.pp_decrements for event in events):
        raise AssertionError("an item action must not decrement move PP")

    if any(event.consumed for event in events if event.kind not in OUTCOME_KINDS):
        raise AssertionError("only the item outcome may account for consumption")
    consumed = sum(event.consumed for event in events)
    if consumed != continuation.consumes_item:
        raise AssertionError(
            f"declared rule consumes {continuation.consumes_item} item(s), observed {consumed}"
        )

    actions = sum(1 for event in events if event.consumes_action)
    expected_actions = 1 if continuation.consumes_player_action else 0
    if actions != expected_actions:
        raise AssertionError(
            f"declared rule consumes {expected_actions} player action(s), observed {actions}"
        )
    if continuation.consumes_player_action and timeline.count(EVENT_COMMAND_SELECTION) != 1:
        raise AssertionError("a consumed item action must not return to command selection")

    return TurnAccount(
        consumes_player_action=continuation.consumes_player_action,
        item_consumed=consumed,
        opponent_actions=len(opponents),
        player_moves=moves,
        pp_decrements=0,
        applied=outcome.kind == EVENT_APPLICATION,
        reason=outcome.reason,
    )


def assert_successful_item_turn(
    timeline: ActionTimeline,
    *,
    item_id: int,
    target_slot: int,
    expected_hp_delta: int | None = None,
) -> TurnAccount:
    """Assert a successful eligible item spends the turn exactly once."""
    account = account_timeline(timeline, continuation=SUCCESSFUL_ITEM_CONTINUATION)
    application = _only_outcome(timeline, EVENT_APPLICATION)
    if application.item_id != item_id or application.target_slot != target_slot:
        raise AssertionError("application does not name the selected item/target")
    if application.consumed != 1:
        raise AssertionError("a completed application must consume exactly one unit")
    if expected_hp_delta is not None and application.hp_delta != expected_hp_delta:
        raise AssertionError(
            f"application HP delta {application.hp_delta} != expected {expected_hp_delta}"
        )
    return account


def assert_no_duplicate_consumption(timeline: ActionTimeline) -> None:
    """A delayed/repeated input must not produce a second consumption."""
    if timeline.count(EVENT_APPLICATION) > 1:
        raise AssertionError("repeated input produced a duplicate application event")
    total = sum(event.consumed for event in timeline.events)
    if total > 1:
        raise AssertionError("repeated input consumed the same unit more than once")


def item_effect_hp_delta(timeline: ActionTimeline) -> int:
    """HP change recorded exactly at the item application boundary."""
    return sum(event.hp_delta for event in timeline.events if event.kind == EVENT_APPLICATION)


def later_hp_delta(timeline: ActionTimeline) -> int:
    """HP change from later opponent/residual events, accounted separately."""
    return sum(event.hp_delta for event in timeline.events if event.kind in LATER_EFFECT_KINDS)


def _only_outcome(timeline: ActionTimeline, kind: str) -> TimelineEvent:
    matches = [event for event in timeline.events if event.kind == kind]
    if len(matches) != 1:
        raise AssertionError(f"expected exactly one {kind} event, found {len(matches)}")
    return matches[0]


__all__ = [
    "BAG_TERMINATOR",
    "CANCELLATION_CONTINUATION",
    "EVENT_APPLICATION",
    "EVENT_COMMAND_SELECTION",
    "EVENT_ITEM_SELECTION",
    "EVENT_NEXT_COMMAND",
    "EVENT_OPPONENT_ACTION",
    "EVENT_PLAYER_MOVE",
    "EVENT_REJECTION",
    "EVENT_REPLACEMENT",
    "EVENT_RESIDUAL",
    "EVENT_TERMINAL",
    "HYPER_POTION_HEAL",
    "INVALID_TARGET_REASONS",
    "MAX_BAG_STACKS",
    "MAX_ITEM_QUANTITY",
    "NO_EFFECT_CONTINUATION",
    "POTION_HEAL",
    "REASON_APPLIED",
    "REASON_FAINTED_NON_REVIVE",
    "REASON_FULL_HP",
    "REASON_FULL_HP_NO_STATUS",
    "REASON_NO_STATUS",
    "REASON_REVIVE_ON_LIVING",
    "REASON_WRONG_STATUS",
    "STATUS_ALL",
    "STATUS_BURN",
    "STATUS_FREEZE",
    "STATUS_PARALYSIS",
    "STATUS_POISON",
    "STATUS_SLEEP_MASK",
    "SUCCESSFUL_ITEM_CONTINUATION",
    "SUPER_POTION_HEAL",
    "TURN_CONSUMING_FAILURE_CONTINUATION",
    "UNAVAILABLE_ITEM_CONTINUATION",
    "VALID_NO_EFFECT_REASONS",
    "ActionTimeline",
    "ActiveBattleSnapshot",
    "BagSnapshot",
    "BagStack",
    "ContinuationRule",
    "ItemUseSnapshot",
    "MedicineOutcome",
    "MedicineRule",
    "PartyMonSnapshot",
    "TimelineEvent",
    "TurnAccount",
    "account_timeline",
    "assert_active_battle_refresh",
    "assert_continuation_idempotent",
    "assert_last_unit_compaction",
    "assert_medicine_application",
    "assert_no_duplicate_consumption",
    "assert_selected_target_changes_only",
    "assert_single_consumption",
    "assert_successful_item_turn",
    "capped_heal",
    "cure_status",
    "item_effect_hp_delta",
    "later_hp_delta",
    "medicine_outcome",
    "revive_hp",
    "status_matches_mask",
    "validate_snapshot",
]
