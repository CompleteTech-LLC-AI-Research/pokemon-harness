"""ROM-free oracle for the pinned Gen I medicine HP-application ladder (#90.1).

The ordinary-medicine workstream (#90-#94) must agree with real cartridge
behavior, so the arithmetic that decides a Potion, Super Potion, Hyper Potion,
Max Potion and Full Restore result is pinned here *from the upstream sources*
instead of being restated from memory.  Two sources are pinned and checked
against each other:

===========  ================================  ===============================
source       repository                        ``engine/items/item_effects.asm``
===========  ================================  ===============================
Red/Blue     ``pret/pokered`` ``fbcf7d0e19a3``  ``76b570b5216bbbcf``
Yellow       ``pret/pokeyellow`` ``bfa7170107``  ``a6b464fe759683af``
===========  ================================  ===============================

``tests/data/battle_medicine/`` holds byte-exact excerpts of the pinned
regions.  ``HP_APPLICATION_EXCERPTS`` and ``ITEM_ID_EXCERPTS`` record the exact
line range, the digest of the whole upstream file and the digest of the
excerpt, so the committed text can be re-derived from a checkout with
``git cat-file``/``sed -n``.  Nothing here is retyped assembly: the tests
re-parse the committed bytes, so a hand-edited amount inside the excerpt cannot
stay green.

What the pinned routine does (``.notUsingSoftboiled2`` through the party
status write), in execution order:

1. ``cp SODA_POP`` / ``ld b, 60`` / ``jr z`` ... resolves the amount in ``b``
   by *comparing item IDs*, so the numeric ordering of the item constants is
   load-bearing rather than cosmetic.
2. ``add b`` on the low byte of the two-byte HP field, with the carry
   incremented into the high byte (``inc [hl]``): a real 16-bit add that wraps
   at ``0x10000`` rather than saturating.
3. ``sub``/``sbc`` against max HP: ``new_hp >= max_hp`` clamps to max HP.
4. ``cp HYPER_POTION`` / ``jr c`` and ``cp MAX_REVIVE`` / ``jr z`` clamp to max
   HP for Full Restore, Max Potion and Max Revive even when the add stayed
   below max HP.
5. ``cp REVIVE`` branches to the half-max HP write before any of that; Full
   Restore then writes ``0`` over the party status byte.

Points 3 and 4 are why the tests below assert *below* the cap, one HP past the
cap, at the ``0x11``/``0x12`` constant boundary and inside the wrap region: a
clamped-only assertion cannot tell ``min(max_hp, hp + amount)`` apart from a
correct 16-bit add, and a correct *amount* is not sufficient because the item
ID decides the branch.

The oracle is pure and deterministic.  It never reads an emulator, ROM, symbol
file, save state or runtime fixture, and it hard-codes no game address.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data" / "battle_medicine"

RED_BLUE = "red_blue"
YELLOW = "yellow"

RED_BLUE_REPOSITORY = "pret/pokered"
YELLOW_REPOSITORY = "pret/pokeyellow"
RED_BLUE_REVISION = "fbcf7d0e19a3a2db505440d3ccd3d40ca996c15c"
YELLOW_REVISION = "bfa7170107eea23b89febb60bfb2ce39173bf2e1"

ITEM_EFFECTS_PATH = "engine/items/item_effects.asm"
ITEM_CONSTANTS_PATH = "constants/item_constants.asm"

RED_BLUE_ITEM_EFFECTS_SHA256 = "76b570b5216bbbcf6d9528c4704d931744a3740cce88afdc4f4f8f700449dda1"
YELLOW_ITEM_EFFECTS_SHA256 = "a6b464fe759683af1e8ff6cf2a040d26975c1cd244550899da2342900cf63b76"
RED_BLUE_ITEM_CONSTANTS_SHA256 = "b5a1db015fbf0de637381eb39350f21f35a436a8088e2f6657f65c79f118f97f"
YELLOW_ITEM_CONSTANTS_SHA256 = "17837f53ed3a59bb54717068f0ecd9271bb52166dd694521695cbfc00132c5f1"

HP_APPLICATION_DATA_FILE = "pokered-item_effects-hp-application.asm"
YELLOW_HP_APPLICATION_DATA_FILE = "pokeyellow-item_effects-hp-application.asm"
ITEM_IDS_DATA_FILE = "item_constants-item-ids.asm"

HP_APPLICATION_EXCERPT_SHA256 = "84da30a475942420a0288c63350ca258db31882e3bf8c79e16c466493c60e63a"
YELLOW_HP_APPLICATION_EXCERPT_SHA256 = (
    "b8bdefec7522bea2296544dc7bf36dea389b81ea05f73717cd88af5a938420e8"
)
ITEM_IDS_EXCERPT_SHA256 = "9e477a97224a40f55af832967af41547bc7ec5e3373dbe0d79e84ce35410250d"

# Result labels.  They describe the branch the pinned routine took; the
# mapping onto the merged #89/#93 reason vocabulary lives in the tests.
BRANCH_REFUSED_FAINTED = "refused_fainted"
BRANCH_REFUSED_REVIVE_ON_LIVING = "refused_revive_on_living"
BRANCH_REFUSED_FULL_HP = "refused_full_hp"
BRANCH_FULL_RESTORE_STATUS_ONLY = "full_restore_status_only"
BRANCH_FIXED_HEAL = "fixed_heal"
BRANCH_CLAMPED_TO_MAX = "clamped_to_max"
BRANCH_REVIVE_HALF_MAX = "revive_half_max"
BRANCH_REVIVE_TO_MAX = "revive_to_max"

APPLIED_BRANCHES = frozenset(
    {
        BRANCH_FULL_RESTORE_STATUS_ONLY,
        BRANCH_FIXED_HEAL,
        BRANCH_CLAMPED_TO_MAX,
        BRANCH_REVIVE_HALF_MAX,
        BRANCH_REVIVE_TO_MAX,
    }
)

HP_FIELD_MASK = 0xFFFF
BYTE_MASK = 0xFF


@dataclass(frozen=True, slots=True)
class Excerpt:
    """One byte-exact slice of a pinned upstream file."""

    label: str
    source: str
    repository: str
    revision: str
    path: str
    first_line: int
    last_line: int
    file_sha256: str
    excerpt_sha256: str
    data_file: str


HP_APPLICATION_EXCERPTS = (
    Excerpt(
        label="red/blue HP application",
        source=RED_BLUE,
        repository=RED_BLUE_REPOSITORY,
        revision=RED_BLUE_REVISION,
        path=ITEM_EFFECTS_PATH,
        first_line=1075,
        last_line=1159,
        file_sha256=RED_BLUE_ITEM_EFFECTS_SHA256,
        excerpt_sha256=HP_APPLICATION_EXCERPT_SHA256,
        data_file=HP_APPLICATION_DATA_FILE,
    ),
    Excerpt(
        label="yellow HP application",
        source=YELLOW,
        repository=YELLOW_REPOSITORY,
        revision=YELLOW_REVISION,
        path=ITEM_EFFECTS_PATH,
        first_line=1193,
        last_line=1279,
        file_sha256=YELLOW_ITEM_EFFECTS_SHA256,
        excerpt_sha256=YELLOW_HP_APPLICATION_EXCERPT_SHA256,
        data_file=YELLOW_HP_APPLICATION_DATA_FILE,
    ),
)

# The item-ID table is shared with Yellow: the pinned slice is byte-identical
# in both checkouts even though the two files as a whole are not, which is
# exactly the agreement the constant-ordering argument depends on.
ITEM_ID_EXCERPTS = (
    Excerpt(
        label="red/blue item ids",
        source=RED_BLUE,
        repository=RED_BLUE_REPOSITORY,
        revision=RED_BLUE_REVISION,
        path=ITEM_CONSTANTS_PATH,
        first_line=8,
        last_line=74,
        file_sha256=RED_BLUE_ITEM_CONSTANTS_SHA256,
        excerpt_sha256=ITEM_IDS_EXCERPT_SHA256,
        data_file=ITEM_IDS_DATA_FILE,
    ),
    Excerpt(
        label="yellow item ids",
        source=YELLOW,
        repository=YELLOW_REPOSITORY,
        revision=YELLOW_REVISION,
        path=ITEM_CONSTANTS_PATH,
        first_line=8,
        last_line=74,
        file_sha256=YELLOW_ITEM_CONSTANTS_SHA256,
        excerpt_sha256=ITEM_IDS_EXCERPT_SHA256,
        data_file=ITEM_IDS_DATA_FILE,
    ),
)

ALL_EXCERPTS = HP_APPLICATION_EXCERPTS + ITEM_ID_EXCERPTS


def digest_text(text: str) -> str:
    """Return the sha256 of ``text`` as it is stored on disk (UTF-8)."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def excerpt_text(record: Excerpt) -> str:
    """Read one committed excerpt verbatim."""

    if type(record) is not Excerpt:
        raise TypeError("record is not an Excerpt")
    return (DATA_DIR / record.data_file).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Ladder parsing (the ``b`` register resolution in item_effects.asm)
# ---------------------------------------------------------------------------

OP_COMPARE = "compare"
OP_LOAD = "load"
OP_BRANCH = "branch"

LADDER_START_LABEL = ".notUsingSoftboiled2"
LADDER_END_LABEL = ".addHealAmount"

_CP_RE = re.compile(r"^cp\s+(?P<symbol>[A-Z][A-Z0-9_]*)$")
_LD_B_RE = re.compile(r"^ld\s+b,\s*(?P<amount>\d+)$")
_JR_RE = re.compile(r"^jr\s+(?P<condition>z|nz|nc|c),\s*(?P<label>\.[A-Za-z0-9_]+)$")


@dataclass(frozen=True, slots=True)
class LadderOp:
    """One instruction of the item-ID ladder, kept in source order."""

    kind: str
    operand: str | int
    text: str


def parse_heal_ladder_ops(text: str) -> tuple[LadderOp, ...]:
    """Parse the pinned ladder into ordered ``compare``/``load``/``branch`` ops.

    Only the instructions the ladder actually uses are accepted, and the
    excerpt must contain the documented labels, so a truncated or reworded
    excerpt fails instead of silently producing a shorter program.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.startswith(LADDER_START_LABEL))
        end = next(i for i, line in enumerate(lines) if line.startswith(LADDER_END_LABEL))
    except StopIteration as exc:
        raise ValueError("excerpt does not carry the pinned ladder labels") from exc
    if end <= start:
        raise ValueError("ladder end label precedes its start label")

    ops: list[LadderOp] = []
    for raw in lines[start + 1 : end]:
        statement = raw.split(";", 1)[0].strip()
        if not statement:
            continue
        if statement == "ld a, [wCurItem]":
            continue
        match = _CP_RE.match(statement)
        if match:
            ops.append(LadderOp(OP_COMPARE, match.group("symbol"), statement))
            continue
        match = _LD_B_RE.match(statement)
        if match:
            amount = int(match.group("amount"))
            if not 0 <= amount <= BYTE_MASK:
                raise ValueError(f"ladder amount does not fit in b: {statement!r}")
            ops.append(LadderOp(OP_LOAD, amount, statement))
            continue
        match = _JR_RE.match(statement)
        if match:
            ops.append(LadderOp(OP_BRANCH, match.group("condition"), statement))
            continue
        raise ValueError(f"unexpected ladder instruction: {statement!r}")
    if not ops or ops[-1].kind != OP_LOAD:
        raise ValueError("ladder does not end in a default amount")
    return tuple(ops)


LADDER_CONDITIONS: Mapping[str, Callable[[int, int], bool]] = {
    "z": lambda expected, value: value == expected,
    "nc": lambda expected, value: value >= expected,
    "c": lambda expected, value: value < expected,
}


def run_heal_ladder(
    item_id: int,
    *,
    ops: tuple[LadderOp, ...],
    item_ids: Mapping[str, int],
) -> int:
    """Interpret the parsed ladder for ``item_id`` and return ``b``.

    The interpreter mimics the CPU: ``cp`` loads the comparison operand,
    ``ld b, N`` loads the amount, and the first taken conditional branch exits.
    A `jr nz`/`jr nz`-style condition the routine does not use is rejected
    rather than guessed at.
    """

    _u8(item_id, "item id")
    compared: int | None = None
    amount: int | None = None
    for op in ops:
        if op.kind == OP_COMPARE:
            symbol = str(op.operand)
            if symbol not in item_ids:
                raise ValueError(f"unknown item constant in ladder: {symbol!r}")
            compared = item_ids[symbol]
        elif op.kind == OP_LOAD:
            amount = int(op.operand)
        else:
            condition = str(op.operand)
            predicate = LADDER_CONDITIONS.get(condition)
            if predicate is None:
                raise ValueError(f"unsupported ladder condition: {condition!r}")
            if compared is None or amount is None:
                raise ValueError("ladder branch without a preceding cp/ld b pair")
            if predicate(compared, item_id):
                return amount
    if amount is None:
        raise ValueError("ladder produced no amount")
    return amount


def ladder_compare_symbols(ops: tuple[LadderOp, ...]) -> tuple[str, ...]:
    """Return the compared symbols in source order (the ordering evidence)."""

    return tuple(str(op.operand) for op in ops if op.kind == OP_COMPARE)


# ---------------------------------------------------------------------------
# Item-ID parsing (declaration order is the real assigned value)
# ---------------------------------------------------------------------------

_CONST_DEF_RE = re.compile(r"^const_def$")
_CONST_RE = re.compile(
    r"^const\s+(?P<name>[A-Z][A-Z0-9_]*)\s*(?:;\s*\$?(?P<value>[0-9A-Fa-f]+)(?:\b.*)?)?$"
)
_CONST_NEXT_RE = re.compile(r"^const_next\s+\$?(?P<value>[0-9A-Fa-f]+)$")
_CONST_SKIP_RE = re.compile(r"^const_skip(?:\s+(?P<count>\d+))?$")
_CONST_VALUE_RE = re.compile(r"^const_value\s+SET\s+\$?(?P<value>[0-9A-Fa-f]+)$")
_CONST_ALIAS_RE = re.compile(
    r"^DEF\s+(?P<name>[A-Z][A-Z0-9_]*)\s+EQU\s+(?P<target>[A-Z][A-Z0-9_]*)\s*(?:;.*)?$"
)

EXPECTED_ITEM_IDS = {
    "NO_ITEM": 0x00,
    "FULL_RESTORE": 0x10,
    "MAX_POTION": 0x11,
    "HYPER_POTION": 0x12,
    "SUPER_POTION": 0x13,
    "POTION": 0x14,
    "FULL_HEAL": 0x34,
    "REVIVE": 0x35,
    "MAX_REVIVE": 0x36,
    "FRESH_WATER": 0x3C,
    "SODA_POP": 0x3D,
    "LEMONADE": 0x3E,
}


def parse_item_id_annotations(text: str) -> dict[str, int]:
    """Return the ``; $XX`` annotations only, without trusting their order."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    annotated: dict[str, int] = {}
    for raw in text.splitlines():
        statement = raw.strip()
        match = _CONST_RE.match(statement)
        if not match or match.group("value") is None:
            continue
        name = match.group("name")
        if name in annotated:
            raise ValueError(f"duplicate item constant in excerpt: {name!r}")
        annotated[name] = int(match.group("value"), 16)
    if not annotated:
        raise ValueError("excerpt carries no annotated item constants")
    return annotated


def _walk_item_constants(text: str) -> tuple[dict[str, int], dict[str, int]]:
    """Walk an excerpt once and return ``(item ids, resolved aliases)``.

    The assigned value comes from the sequence, not from the trailing comment:
    the annotations are cross-checked afterwards, so a wrong comment or a
    reordered declaration is a failure instead of a silent mis-derivation.
    ``DEF NAME EQU SYMBOL`` lines are overloads of a *previous* item: they name
    no new ID, so they must resolve to a constant already declared above them
    and they must not consume a slot in the sequence.
    """

    derived: dict[str, int] = {}
    aliases: dict[str, int] = {}
    when_defined: int | None = None
    next_value = 0
    for raw in text.splitlines():
        statement = raw.strip()
        if not statement or statement.startswith(";"):
            continue
        if _CONST_DEF_RE.match(statement):
            when_defined = next_value
            continue
        match = _CONST_NEXT_RE.match(statement)
        if match:
            next_value = int(match.group("value"), 16)
            continue
        match = _CONST_VALUE_RE.match(statement)
        if match:
            next_value = int(match.group("value"), 16)
            continue
        match = _CONST_SKIP_RE.match(statement)
        if match:
            next_value += int(match.group("count") or 1)
            continue
        match = _CONST_ALIAS_RE.match(statement)
        if match:
            name = match.group("name")
            target = match.group("target")
            if name in derived or name in aliases:
                raise ValueError(f"duplicate item constant in excerpt: {name!r}")
            if target not in derived:
                raise ValueError(
                    f"item alias {name!r} names {target!r}, which is not declared above it"
                )
            aliases[name] = derived[target]
            continue
        match = _CONST_RE.match(statement)
        if match:
            name = match.group("name")
            if name in derived or name in aliases:
                raise ValueError(f"duplicate item constant in excerpt: {name!r}")
            derived[name] = next_value
            annotated = match.group("value")
            if annotated is not None and int(annotated, 16) != next_value:
                raise ValueError(
                    f"item constant {name!r} is declared as {next_value:#04x} but "
                    f"annotated {int(annotated, 16):#04x}"
                )
            next_value += 1
            continue
        raise ValueError(f"unexpected item-constant directive: {statement!r}")
    if when_defined is None:
        raise ValueError("excerpt does not start a const_def block")
    if not derived:
        raise ValueError("excerpt declares no item constants")
    return derived, aliases


def parse_item_ids(text: str) -> dict[str, int]:
    """Derive item IDs from ``const_def``/``const`` declaration order."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    return _walk_item_constants(text)[0]


def parse_item_aliases(text: str) -> dict[str, int]:
    """Resolve the excerpt's ``; overload`` aliases to the IDs they name."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    return _walk_item_constants(text)[1]


@lru_cache(maxsize=1)
def pinned_item_ids() -> dict[str, int]:
    """Item IDs parsed from the committed Red/Blue ``const`` excerpt."""

    return parse_item_ids(excerpt_text(ITEM_ID_EXCERPTS[0]))


@lru_cache(maxsize=1)
def pinned_item_aliases() -> dict[str, int]:
    """Overload aliases parsed from the committed Red/Blue ``const`` excerpt."""

    return parse_item_aliases(excerpt_text(ITEM_ID_EXCERPTS[0]))


def _heal_ladder_ops_for(record: Excerpt) -> tuple[LadderOp, ...]:
    return parse_heal_ladder_ops(excerpt_text(record))


@lru_cache(maxsize=1)
def pinned_heal_ladder_ops() -> tuple[LadderOp, ...]:
    """Ladder ops parsed from the committed Red/Blue excerpt."""

    return _heal_ladder_ops_for(HP_APPLICATION_EXCERPTS[0])


@lru_cache(maxsize=1)
def yellow_heal_ladder_ops() -> tuple[LadderOp, ...]:
    """Ladder ops parsed from the committed Yellow excerpt."""

    return _heal_ladder_ops_for(HP_APPLICATION_EXCERPTS[1])


# ---------------------------------------------------------------------------
# The two-byte add, the clamp rules and the resulting outcome
# ---------------------------------------------------------------------------


def _u8(value: int, label: str) -> int:
    if type(value) is not int or not 0 <= value <= BYTE_MASK:
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def _u16(value: int, label: str) -> int:
    if type(value) is not int or not 0 <= value <= HP_FIELD_MASK:
        raise ValueError(f"invalid {label}: {value!r}")
    return value


def add_heal_amount(hp: int, amount: int) -> int:
    """Replicate ``ld a, [hl]; add b; ld [hld], a`` plus ``inc [hl]``.

    ``b`` is a single register, so ``amount`` must fit in a byte.  The carry is
    incremented into the high byte of the two-byte HP field and an overflow out
    of that byte is lost, which is a 16-bit wraparound rather than a clamp.
    Real party HP stays far below the wrap region (see the tests), but the
    arithmetic is pinned as written so a ``min``-shaped shortcut is detectable.
    """

    _u16(hp, "hp")
    _u8(amount, "heal amount")
    low = (hp & BYTE_MASK) + amount
    high = (hp >> 8) + (low >> 8)
    return ((high & BYTE_MASK) << 8) | (low & BYTE_MASK)


def clamps_to_max_hp(item_id: int, item_ids: Mapping[str, int]) -> bool:
    """``cp HYPER_POTION`` / ``jr c`` and ``cp MAX_REVIVE`` / ``jr z``."""

    return item_id < item_ids["HYPER_POTION"] or item_id == item_ids["MAX_REVIVE"]


def revive_half_max_hp(max_hp: int) -> int:
    """``srl a``/``rr a`` on the two-byte max HP: an exact floor halving."""

    _u16(max_hp, "max HP")
    return max_hp >> 1


@dataclass(frozen=True, slots=True)
class HealOutcome:
    """Resolved outcome for one medicine application."""

    applied: bool
    hp: int
    status: int
    branch: str


def heal_application(
    item_id: int,
    hp: int,
    max_hp: int,
    status: int,
    *,
    item_ids: Mapping[str, int] | None = None,
    ops: tuple[LadderOp, ...] | None = None,
) -> HealOutcome:
    """Resolve the pinned branch for one target, in the routine's own order.

    ``item_ids`` and ``ops`` default to the committed excerpts; tests override
    them to show that the outcome really is driven by the parsed ID table and
    ladder instead of by item names.
    """

    _u8(item_id, "item id")
    _u16(hp, "hp")
    _u16(max_hp, "max HP")
    _u8(status, "status")
    if max_hp == 0:
        raise ValueError("max HP must be non-zero")
    if hp > max_hp:
        raise ValueError("current HP exceeds max HP")
    table = pinned_item_ids() if item_ids is None else item_ids
    ladder = pinned_heal_ladder_ops() if ops is None else ops

    revive = item_id == table["REVIVE"]
    to_max = item_id == table["MAX_REVIVE"]
    full_restore = item_id == table["FULL_RESTORE"]

    if hp == 0:
        # ``or b`` detects a fainted target; only the two revive items continue.
        if not (revive or to_max):
            return HealOutcome(False, hp, status, BRANCH_REFUSED_FAINTED)
    elif revive or to_max:
        return HealOutcome(False, hp, status, BRANCH_REFUSED_REVIVE_ON_LIVING)

    if hp == max_hp:
        # ``.compareCurrentHPToMaxHP`` sends every other item to no-effect; Full
        # Restore is retargeted at Full Heal and never touches HP here.
        if not full_restore or status == 0:
            return HealOutcome(False, hp, status, BRANCH_REFUSED_FULL_HP)
        return HealOutcome(True, hp, 0, BRANCH_FULL_RESTORE_STATUS_ONLY)

    amount = run_heal_ladder(item_id, ops=ladder, item_ids=table)
    new_hp = add_heal_amount(hp, amount)
    if revive:
        # ``jr .doneHealingPartyHP`` from the half-max path still flows into the
        # party-status write, so an ID that is both a revive and Full Restore
        # would clear the status byte here.
        return HealOutcome(
            True,
            revive_half_max_hp(max_hp),
            0 if full_restore else status,
            BRANCH_REVIVE_HALF_MAX,
        )
    if to_max or new_hp >= max_hp or clamps_to_max_hp(item_id, table):
        resolved, branch = max_hp, BRANCH_CLAMPED_TO_MAX
        if to_max:
            branch = BRANCH_REVIVE_TO_MAX
        # Every clamp path (.setCurrentHPToMaxHp and the revive half-max path)
        # falls through .doneHealingPartyHP, which is where the status write
        # lives.
        clears_status = full_restore
    else:
        resolved, branch = new_hp, BRANCH_FIXED_HEAL
        # A fixed-amount heal jumps straight from the ladder to
        # ``.updateInBattleData``, skipping ``.doneHealingPartyHP`` entirely, so
        # it never writes the party status byte - even for a Full Restore ID
        # whose clamp was not taken (for example once the ID is remapped above
        # Hyper Potion).
        clears_status = False
    return HealOutcome(True, resolved, 0 if clears_status else status, branch)


def raw_loaded_amount(
    item_id: int,
    *,
    item_ids: Mapping[str, int] | None = None,
    ops: tuple[LadderOp, ...] | None = None,
) -> int:
    """Return the amount the ladder loads into ``b`` before any clamp."""

    table = pinned_item_ids() if item_ids is None else item_ids
    ladder = pinned_heal_ladder_ops() if ops is None else ops
    return run_heal_ladder(_u8(item_id, "item id"), ops=ladder, item_ids=table)


__all__ = [
    "ALL_EXCERPTS",
    "APPLIED_BRANCHES",
    "BRANCH_CLAMPED_TO_MAX",
    "BRANCH_FIXED_HEAL",
    "BRANCH_FULL_RESTORE_STATUS_ONLY",
    "BRANCH_REFUSED_FAINTED",
    "BRANCH_REFUSED_FULL_HP",
    "BRANCH_REFUSED_REVIVE_ON_LIVING",
    "BRANCH_REVIVE_HALF_MAX",
    "BRANCH_REVIVE_TO_MAX",
    "DATA_DIR",
    "EXPECTED_ITEM_IDS",
    "HP_APPLICATION_EXCERPTS",
    "ITEM_ID_EXCERPTS",
    "LADDER_CONDITIONS",
    "RED_BLUE",
    "RED_BLUE_ITEM_EFFECTS_SHA256",
    "RED_BLUE_REVISION",
    "YELLOW",
    "YELLOW_ITEM_EFFECTS_SHA256",
    "YELLOW_REVISION",
    "Excerpt",
    "LadderOp",
    "add_heal_amount",
    "clamps_to_max_hp",
    "digest_text",
    "excerpt_text",
    "heal_application",
    "ladder_compare_symbols",
    "parse_heal_ladder_ops",
    "parse_item_aliases",
    "parse_item_id_annotations",
    "parse_item_ids",
    "pinned_heal_ladder_ops",
    "pinned_item_aliases",
    "pinned_item_ids",
    "raw_loaded_amount",
    "revive_half_max_hp",
    "run_heal_ladder",
    "yellow_heal_ladder_ops",
]
