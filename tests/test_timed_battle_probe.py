"""ROM-free mock contracts only; synthetic snapshots are NOT gameplay proof.

The fakes never instantiate PyBoy, load assets, or advance an emulated CPU.
Fixture builders populate private dictionaries, not emulator RAM. Only the
external test harness may fire an observation hook or change synthetic state.
"""

import copy
import importlib
import json
import threading
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

PHASES = (
    "party_qualified",
    "link_menu_colosseum_ready",
    "colosseum_reached",
    "battle_menu_ready",
    "party_inspected",
    "party_cancelled",
    "move_ready",
    "settled_turn",
    "room_returned",
)


@pytest.fixture
def probe():
    return importlib.import_module("scripts._timed_battle_probe")


class ReadOnlyMemory:
    def __init__(self):
        self.values = {}
        self.reads = []
        self.forbidden = []

    def __getitem__(self, address):
        if isinstance(address, slice):
            return [self[i] for i in range(address.start, address.stop, address.step or 1)]
        if isinstance(address, tuple) and isinstance(address[1], slice):
            bank, span = address
            return [self[bank, i] for i in range(span.start, span.stop, span.step or 1)]
        self.reads.append(address)
        return self.values.get(address, 0)

    def __setitem__(self, address, value):
        self.forbidden.append((address, value))
        raise AssertionError("probe must never write RAM or ROM")


class ReadOnlyRegisters:
    PC = SP = HL = A = B = C = D = E = F = 0

    def __setattr__(self, name, value):
        raise AssertionError("probe must never change CPU registers")


def record(species=84, hp=80, move=1, pp=20, status=0):
    raw = bytearray(44)
    raw[0] = species
    raw[1:3] = hp.to_bytes(2, "big")
    raw[3] = raw[33] = 20
    raw[4] = status
    raw[8] = move
    raw[29] = pp
    raw[34:36] = (100).to_bytes(2, "big")
    for offset in (36, 38, 40, 42):
        raw[offset : offset + 2] = (30).to_bytes(2, "big")
    return raw


class Readiness:
    def __init__(self, phases):
        self.phases = set(phases)
        self.local = set()
        self.peer = set(phases)

    def publish_ready(self, phase):
        assert phase in self.phases
        self.local.add(phase)

    def peer_ready(self, phase):
        assert phase in self.phases
        return phase in self.peer


class FakeSymbols:
    def __init__(self, probe, version):
        # Party/battle records use canonical Red offsets and aliases. Menu-only
        # scalars use isolated fake addresses; ROM continuations vary by version.
        self.locations = {
            "wPartyCount": (0, 0xD163),
            "wPartySpecies": (0, 0xD164),
            "wPartyMons": (0, 0xD16B),
            "wPlayerMonNumber": (0, 0xCCD5),
            "wEnemyMonPartyPos": (0, 0xCFE8),
            "wPlayerSelectedMove": (0, 0xCCDC),
            "wEnemySelectedMove": (0, 0xCCDD),
            "wPlayerMoveListIndex": (0, 0xCC2E),
            "wEnemyMoveListIndex": (0, 0xCCE2),
            "wSerialExchangeNybbleSendData": (0, 0xCC42),
            "wSerialExchangeNybbleReceiveData": (0, 0xCC3E),
            "Moves": (14, 0x4000),
            "SelectEnemyMove": (15, 0x56D6 if version == "yellow" else 0x5564),
            "LinkBattleExchangeData": (15, 0x5777 if version == "yellow" else 0x5605),
            "LoadScreenTilesFromBuffer1": (0, 0x371B if version == "yellow" else 0x3725),
            "PrintText": (0, 0x3C36 if version == "yellow" else 0x3C49),
            "FullyParalyzedText": (15, 0x5BBE if version == "yellow" else 0x5A4C),
            "CheckPlayerStatusConditions.MonHurtItselfOrFullyParalysed": (
                15,
                0x5AC4 if version == "yellow" else 0x5952,
            ),
            "CheckEnemyStatusConditions.monHurtItselfOrFullyParalysed": (
                15,
                0x6B59 if version == "yellow" else 0x69D3,
            ),
        }
        for index, name in enumerate(probe.MENU_FIELDS):
            self.locations.setdefault(name, (0, 0xC100 + index))
        for index, name in enumerate(probe.OBSERVATION_SYMBOLS):
            self.locations.setdefault(name, (15, 0x6000 + index * 16))
        for prefix, base in (("wBattleMon", 0xD014), ("wEnemyMon", 0xCFE5)):
            self.locations[prefix] = (0, base)
            for suffix, offset in {
                "Species": 0,
                "HP": 1,
                "PartyPos": 3,
                "Status": 4,
                "Moves": 8,
                "Level": 14,
                "MaxHP": 15,
                "PP": 25,
            }.items():
                self.locations[prefix + suffix] = (0, base + offset)
        self.lookups = []

    def __contains__(self, name):
        return name in self.locations

    def bank_addr(self, name):
        self.lookups.append(name)
        return self.locations[name]

    def addr_of(self, name):
        return self.bank_addr(name)[1]


class FakeSession:
    def __init__(self, probe, *, version="red_color", records=None):
        self.symbols = FakeSymbols(probe, version)
        self.memory = ReadOnlyMemory()
        self.hooks = {}
        self.installs = []
        self.removals = []
        self.forbidden = []
        self._pyboy = SimpleNamespace(
            memory=self.memory,
            register_file=ReadOnlyRegisters(),
            frame_count=0,
            hook_register=self.register,
            hook_deregister=self.deregister,
            tick=self.forbid,
            step=self.forbid,
            send_input=self.forbid,
            button=self.forbid,
            button_press=self.forbid,
            button_release=self.forbid,
        )
        records = [record()] if records is None else records
        self.set_bytes("wPartyCount", [len(records)])
        self.set_bytes("wPartySpecies", [r[0] for r in records] + [255])
        self.set_bytes("wPartyMons", b"".join(records))
        self.set_combatant("wBattleMon", records[0])
        self.set_combatant("wEnemyMon", record(species=153, move=33))
        for name, value in (("hSerialConnectionStatus", 2), ("wCurMap", 0x40)):
            self.set_bytes(name, [value])
        # Exact CALL exchange; CALL restore-screen; LD A,[receive-buffer].
        # These are test bytes, not commercial ROM data or a PyBoy instance.
        bank, select = self.symbols.bank_addr("SelectEnemyMove")
        exchange = self.symbols.addr_of("LinkBattleExchangeData")
        restore = self.symbols.addr_of("LoadScreenTilesFromBuffer1")
        receive = self.symbols.addr_of("wSerialExchangeNybbleReceiveData")
        code = bytes(
            [
                0xCD,
                exchange & 255,
                exchange >> 8,
                0xCD,
                restore & 255,
                restore >> 8,
                0xFA,
                receive & 255,
                receive >> 8,
            ]
        )
        self.memory.values.update({(bank, select + 10 + i): b for i, b in enumerate(code)})
        text = self.symbols.addr_of("FullyParalyzedText")
        printer = self.symbols.addr_of("PrintText")
        code = bytes([0x21, text & 255, text >> 8, 0xCD, printer & 255, printer >> 8])
        for name in (
            "CheckPlayerStatusConditions.MonHurtItselfOrFullyParalysed",
            "CheckEnemyStatusConditions.monHurtItselfOrFullyParalysed",
        ):
            bank, target = self.symbols.bank_addr(name)
            self.memory.values.update({(bank, target - 6 + i): b for i, b in enumerate(code)})
        # Minimal six-byte Gen-I move rows for the fixture's populated moves.
        for move_id in (1, 24, 33):
            values = [move_id, 44 if move_id == 24 else 0, 40, 0, 255, 35]
            self.memory.values.update(
                {(14, 0x4000 + (move_id - 1) * 6 + i): b for i, b in enumerate(values)}
            )

    def set_bytes(self, name, values):
        start = self.symbols.addr_of(name)
        self.memory.values.update({start + i: b for i, b in enumerate(values)})

    def set_combatant(self, prefix, raw):
        for suffix, values in (
            ("Species", raw[:1]),
            ("HP", raw[1:3]),
            ("Status", raw[4:5]),
            ("Moves", raw[8:12]),
            ("PP", raw[29:33]),
            ("MaxHP", raw[34:36]),
        ):
            self.set_bytes(prefix + suffix, values)

    def register(self, bank, address, callback, context):
        location = bank, address
        if location in self.hooks:
            raise ValueError("existing hook")
        self.installs.append(location)
        self.hooks[location] = callback, context

    def deregister(self, bank, address):
        location = bank, address
        self.removals.append(location)
        del self.hooks[location]

    def forbid(self, *args, **kwargs):
        self.forbidden.append((args, kwargs))
        raise AssertionError("helper must not advance CPU or apply input")

    step = tick = press = send_input = close = forbid

    def current_tick(self):
        return 0

    def fire(self, name):
        callback, context = self.hooks[self.symbols.bank_addr(name)]
        callback(context)


def make_driver(probe, *, version="red_color", records=None, move_slot=0, session=None):
    session = session or FakeSession(probe, version=version, records=records)
    readiness = Readiness(PHASES)
    driver = probe.create_owner_driver(
        session=session,
        side="listen",
        peer_state=readiness,
        options={"version": version, "move_slot": move_slot, "checkpoint": "settled-turn"},
    )
    return session, readiness, driver


def exception_leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in exception_leaves(child)]
    return [error]


def adjudicate(probe, owners, checkpoint="settled-turn"):
    original = copy.deepcopy(owners)
    result = probe.adjudicate_pair(owners, checkpoint)
    assert owners == original, "adjudicator must not repair its input evidence"
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    assert {"complete", "errors", "status"} <= result.keys()
    assert result["full_authentic_acceptance"] is False
    assert "unknown" in result["provenance"].lower()
    assert "unverified" in result["provenance"].lower()
    return result


@pytest.mark.parametrize("owners", [None, [], {}, [None, None], [{}, {}], [{}, {}, {}]])
def test_malformed_pair_schema_fails_closed(probe, owners):
    result = adjudicate(probe, owners)
    assert result["complete"] is False
    assert result["errors"]


@pytest.mark.parametrize("checkpoint", ["settled-turn", "room-return"])
def test_flags_and_entry_hook_counts_are_never_settlement(probe, checkpoint):
    owners = [
        {
            "role": side,
            "version": version,
            "checkpoint": checkpoint,
            "checkpoint_reached": True,
            "complete": True,
            "turn_settled": True,
            "room_returned": True,
            "readiness": [1] * len(probe.READINESS_PHASES),
            "hook_counts": {
                "ExecutePlayerMove": 100,
                "ExecuteEnemyMove": 100,
                "PlayerCalcMoveDamage": 100,
                "LinkBattleExchangeData": 100,
            },
        }
        for side, version in (("listen", "red_color"), ("connect", "yellow"))
    ]
    result = adjudicate(probe, owners, checkpoint)
    assert result["complete"] is False
    assert result["errors"]


def paired_snapshots(*, paralyzed=False, multi_hit=False, room=False):
    """Hand-constructed contract witnesses, never ROM-produced evidence."""
    before = [
        {
            "slot": 0,
            "species": 84,
            "hp": 80,
            "status": 64 if paralyzed else 0,
            "moves": [1, 0, 0, 0],
            "pp": [20, 0, 0, 0],
        },
        {
            "slot": 0,
            "species": 153,
            "hp": 90,
            "status": 0,
            "moves": [24 if multi_hit else 33, 0, 0, 0],
            "pp": [30, 0, 0, 0],
        },
    ]
    after = copy.deepcopy(before)
    after[0]["hp"] = 60 if multi_hit else 70
    after[1]["hp"] = 90 if paralyzed else 65
    after[0]["pp"][0] -= 0 if paralyzed else 1
    after[1]["pp"][0] -= 1
    actions = [
        {
            "executed": not paralyzed,
            "done": True,
            "skip_reason": "fully_paralyzed" if paralyzed else None,
            "damage_done": 0 if paralyzed else 1,
        },
        {"executed": True, "done": True, "skip_reason": None, "damage_done": 2 if multi_hit else 1},
    ]
    for action in actions:
        action.update(move_missed=0, damage_samples=[])
    if not paralyzed:
        actions[0]["damage_samples"] = [
            {"before_hp": 90, "after_hp": 65, "damage": 25, "move_missed": 0},
        ]
    actions[1]["damage_samples"] = (
        [
            {"before_hp": 80, "after_hp": 72, "damage": 8, "move_missed": 0},
            {"before_hp": 72, "after_hp": 60, "damage": 12, "move_missed": 0},
        ]
        if multi_hit
        else [{"before_hp": 80, "after_hp": 70, "damage": 10, "move_missed": 0}]
    )
    reports = []
    for index, side in enumerate(("listen", "connect")):
        local, enemy = before[index], before[1 - index]
        effects = [0, 44 if multi_hit else 0]
        reports.append(
            {
                "schema_version": 1,
                "role": side,
                "version": ("red_color", "yellow")[index],
                "move_slot": 0,
                "chosen": {"slot": 0, "move_id": local["moves"][0], "effect": effects[index]},
                "checkpoint": "room-return" if room else "settled-turn",
                "phase": "room_returned" if room else "settled_turn",
                "readiness": [1] * (len(PHASES) - 1) + [int(room)],
                "checkpoint_reached": True,
                "gameplay_completed": True,
                "full_authentic_acceptance": False,
                "provenance": "unknown historical provenance; pristine acquisition unverified",
                "before_party": {
                    "count": 1,
                    "species": [local["species"], 255],
                    "records": [
                        record(
                            local["species"],
                            local["hp"],
                            local["moves"][0],
                            local["pp"][0],
                            local["status"],
                        ).hex()
                    ],
                },
                "baseline": {
                    "seq": 100,
                    "local": copy.deepcopy(local),
                    "enemy": copy.deepcopy(enemy),
                },
                "turn": {
                    "exchange_seq": 150,
                    "settled_seq": 200,
                    "send": 0,
                    "receive": 0,
                    "local_move_id": local["moves"][0],
                    "enemy_move_id": enemy["moves"][0],
                    "local_move_effect": effects[index],
                    "enemy_move_effect": effects[1 - index],
                    "local": copy.deepcopy(after[index]),
                    "enemy": copy.deepcopy(after[1 - index]),
                    "actions": {
                        "local": copy.deepcopy(actions[index]),
                        "enemy": copy.deepcopy(actions[1 - index]),
                    },
                    "pp": {
                        "before": list(local["pp"]),
                        "after": list(after[index]["pp"]),
                        "decrement_entries": int(actions[index]["executed"]),
                    },
                },
                "room_return": (
                    {
                        "end_seq": 250,
                        "return_seq": 260,
                        "settled_seq": 270,
                        "send": 15,
                        "receive": 15,
                        "result": 2,
                        "map": 240,
                        "link_state": 1,
                        "is_in_battle": 0,
                    }
                    if room
                    else None
                ),
                "unsupported_reason": None,
                "hook_counts": {},
                "recent_transitions": [],
            }
        )
        if room:
            reports[-1]["room_return"]["healed_party"] = healed_party(reports[-1]["before_party"])
    return reports


def healed_party(before):
    healed = copy.deepcopy(before)
    for slot, encoded in enumerate(healed["records"]):
        raw = bytearray.fromhex(encoded)
        raw[1:3] = raw[34:36]
        raw[4] = 0
        # This contract claims HP/status restoration, not PP restoration.
        healed["records"][slot] = raw.hex()
    return healed


@pytest.mark.parametrize("room", [False, True])
@pytest.mark.parametrize("paralyzed,multi_hit", [(False, False), (True, False), (False, True)])
def test_coherent_synthetic_pair_settles_with_unknown_provenance(probe, room, paralyzed, multi_hit):
    owners = paired_snapshots(room=room, paralyzed=paralyzed, multi_hit=multi_hit)
    result = adjudicate(probe, owners, "room-return" if room else "settled-turn")
    assert result["complete"] is True, result
    assert result["gameplay_completed"] is True
    assert result["errors"] == []


def change(value, path, replacement):
    for key in path[:-1]:
        value = value[key]
    value[path[-1]] = replacement


@pytest.mark.parametrize(
    "path,value",
    [
        (("schema_version",), 2),
        (("schema_version",), True),
        (("schema_version",), 1.0),
        (("move_slot",), False),
        (("move_slot",), 0.0),
        (("role",), "connect"),
        (("baseline",), None),
        (("baseline", "seq"), True),
        (("baseline", "seq"), 100.5),
        (("baseline", "local", "hp"), True),
        (("baseline", "local", "species"), 153),
        (("before_party", "count"), 7),
        (("turn",), None),
        (("turn", "exchange_seq"), 99),
        (("turn", "exchange_seq"), 150.5),
        (("turn", "settled_seq"), 150),
        (("turn", "settled_seq"), 200.5),
        (("turn", "send"), 1),
        (("turn", "receive"), 1),
        (("turn", "local_move_id"), 33),
        (("turn", "local_move_id"), True),
        (("turn", "enemy_move_id"), 1),
        (("turn", "local", "hp"), 69),
        (("turn", "enemy", "hp"), 64),
        (("turn", "local", "status"), 64),
        (("turn", "enemy", "status"), 64),
        (("turn", "actions", "local", "done"), False),
        (("turn", "actions", "enemy", "done"), False),
        (("turn", "pp", "after"), [20, 0, 0, 0]),
        (("turn", "pp", "after"), [18, 0, 0, 0]),
        (("turn", "pp", "after"), [19, 1, 0, 0]),
        (("unsupported_reason",), "lost observation"),
    ],
)
def test_single_field_corruption_cannot_pass_reciprocal_adjudication(probe, path, value):
    owners = paired_snapshots()
    change(owners[0], path, value)
    result = adjudicate(probe, owners)
    assert result["complete"] is False, (path, result)
    assert result["errors"]


def test_enemy_pp_is_not_a_reciprocal_mirror(probe):
    owners = paired_snapshots()
    for owner in owners:
        owner["baseline"]["enemy"]["pp"] = [7, 0, 0, 0]
        owner["turn"]["enemy"]["pp"] = [7, 0, 0, 0]
    assert adjudicate(probe, owners)["complete"] is True


def test_baseline_cannot_substitute_a_different_species_for_ordinary_first_living_member(probe):
    owners = paired_snapshots()
    party = owners[0]["before_party"]
    raw = bytearray.fromhex(party["records"][0])
    party["species"][0] = raw[0] = 100
    party["records"][0] = raw.hex()
    assert adjudicate(probe, owners)["complete"] is False


@pytest.mark.parametrize("side", [0, 1])
def test_nonfaint_contract_rejects_faint_even_when_peers_agree(probe, side):
    owners = paired_snapshots()
    owners[side]["turn"]["local"]["hp"] = 0
    owners[1 - side]["turn"]["enemy"]["hp"] = 0
    assert adjudicate(probe, owners)["complete"] is False


@pytest.mark.parametrize("wrong_pp", [19, 18])
def test_fully_paralyzed_action_must_not_consume_local_pp(probe, wrong_pp):
    owners = paired_snapshots(paralyzed=True)
    owners[0]["turn"]["local"]["pp"][0] = wrong_pp
    owners[0]["turn"]["pp"]["after"][0] = wrong_pp
    assert adjudicate(probe, owners)["complete"] is False


def test_paralysis_claim_without_status_is_not_a_valid_skipped_action(probe):
    owners = paired_snapshots(paralyzed=True)
    for owner in owners:
        for phase in ("baseline", "turn"):
            for side in ("local", "enemy"):
                owner[phase][side]["status"] = 0
    raw = bytearray.fromhex(owners[0]["before_party"]["records"][0])
    raw[4] = 0
    owners[0]["before_party"]["records"][0] = raw.hex()
    assert adjudicate(probe, owners)["complete"] is False


def test_multihit_consumes_one_pp_for_aggregate_settlement(probe):
    owners = paired_snapshots(multi_hit=True)
    assert adjudicate(probe, owners)["complete"] is True
    owners[1]["turn"]["local"]["pp"][0] = 28
    owners[1]["turn"]["pp"]["after"][0] = 28
    assert adjudicate(probe, owners)["complete"] is False


def zero_outcome_pair(*, missed=False):
    owners = paired_snapshots()
    for index, owner in enumerate(owners):
        for side in ("local", "enemy"):
            owner["turn"][side]["hp"] = owner["baseline"][side]["hp"]
        hp = owner["baseline"]["enemy"]["hp"]
        owner["turn"]["actions"]["local"].update(
            move_missed=int(missed),
            damage_done=0 if missed else 1,
            damage_samples=[]
            if missed
            else [
                {"before_hp": hp, "after_hp": hp, "damage": 0, "move_missed": 0},
            ],
        )
    for index, owner in enumerate(owners):
        owner["turn"]["actions"]["enemy"] = copy.deepcopy(
            owners[1 - index]["turn"]["actions"]["local"]
        )
    return owners


@pytest.mark.parametrize("missed", [False, True], ids=["completed-zero", "reported-miss"])
def test_no_hp_delta_can_settle_with_completed_application_or_observed_miss(probe, missed):
    # wMoveMissed records the ROM path; it does not independently classify immunity.
    owners = zero_outcome_pair(missed=missed)
    result = adjudicate(probe, owners)
    assert result["complete"] is True, result
    for owner in owners:
        assert owner["turn"]["local"]["hp"] == owner["baseline"]["local"]["hp"]
        assert owner["turn"]["pp"]["after"][0] == owner["turn"]["pp"]["before"][0] - 1


@pytest.mark.parametrize(
    "fault",
    [
        "missing_path",
        "missing_sample",
        "miss_with_application",
        "wrong_calculated_damage",
        "hp_increase",
        "residual_drop",
    ],
)
def test_fabricated_zero_and_mismatched_damage_paths_fail_even_when_peers_agree(probe, fault):
    owners = zero_outcome_pair()
    action = owners[0]["turn"]["actions"]["local"]
    if fault == "missing_path":
        action.update(damage_done=0, damage_samples=[])
    elif fault == "missing_sample":
        action["damage_samples"] = []
    elif fault == "miss_with_application":
        action["move_missed"] = 1
    elif fault == "wrong_calculated_damage":
        action["damage_samples"][0]["damage"] = 1
    else:
        changed_hp = 91 if fault == "hp_increase" else 89
        owners[0]["turn"]["enemy"]["hp"] = changed_hp
        owners[1]["turn"]["local"]["hp"] = changed_hp
    owners[1]["turn"]["actions"]["enemy"] = copy.deepcopy(action)
    assert adjudicate(probe, owners)["complete"] is False


@pytest.mark.parametrize("side", [0, 1])
def test_counter_is_explicitly_unsupported_despite_catalog_effect_zero(probe, side):
    owners = paired_snapshots()
    raw = bytearray.fromhex(owners[side]["before_party"]["records"][0])
    raw[8] = 68
    owners[side]["before_party"]["records"][0] = raw.hex()
    owners[side]["chosen"]["move_id"] = 68
    owners[side]["turn"]["local_move_id"] = 68
    owners[1 - side]["turn"]["enemy_move_id"] = 68
    for phase in ("baseline", "turn"):
        owners[side][phase]["local"]["moves"][0] = 68
        owners[1 - side][phase]["enemy"]["moves"][0] = 68
    result = adjudicate(probe, owners)
    assert result["complete"] is False
    assert "counter" in " ".join(result["errors"]).lower()


@pytest.mark.parametrize(
    "path,value",
    [
        (("room_return",), None),
        (("room_return", "return_seq"), 250),
        (("room_return", "settled_seq"), 260),
        (("room_return", "send"), 0),
        (("room_return", "receive"), 0),
        (("room_return", "map"), 239),
        (("room_return", "is_in_battle"), 1),
    ],
)
def test_room_return_requires_post_battle_reciprocal_evidence(probe, path, value):
    owners = paired_snapshots(room=True)
    change(owners[0], path, value)
    assert adjudicate(probe, owners, "room-return")["complete"] is False


@pytest.mark.parametrize("claimed", ["verified", "pristine", "", None])
def test_provenance_claims_cannot_upgrade_mock_gameplay_to_authentic_acceptance(probe, claimed):
    owners = paired_snapshots()
    for owner in owners:
        owner["provenance"] = claimed
        owner["full_authentic_acceptance"] = True
    result = adjudicate(probe, owners)
    assert result["full_authentic_acceptance"] is False


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize(
    "field,value",
    [
        ("slot", 1),
        ("move_id", 166),
        ("move_id", True),
        ("effect", 1),
        ("effect", 6),
    ],
)
def test_chosen_metadata_must_match_canonical_move_and_baseline(probe, side, field, value):
    owners = paired_snapshots()
    owners[side]["chosen"][field] = value
    assert adjudicate(probe, owners)["complete"] is False


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("effect", [1, 6, 44])
def test_forging_both_peers_effect_metadata_cannot_override_canonical_catalog(probe, side, effect):
    owners = paired_snapshots()
    owners[side]["chosen"]["effect"] = effect
    owners[side]["turn"]["local_move_effect"] = effect
    owners[1 - side]["turn"]["enemy_move_effect"] = effect
    assert adjudicate(probe, owners)["complete"] is False


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize("fault", ["missing", "hp", "status", "identity", "max_hp"])
def test_room_return_must_include_actual_healed_party_records(probe, side, fault):
    owners = paired_snapshots(room=True)
    room = owners[side]["room_return"]
    if fault == "missing":
        del room["healed_party"]
    else:
        party = room["healed_party"]
        raw = bytearray.fromhex(party["records"][0])
        if fault == "hp":
            raw[1:3] = (99).to_bytes(2, "big")
        elif fault == "status":
            raw[4] = 64
        elif fault == "identity":
            party["species"][0] = raw[0] = 100
        else:
            raw[1:3] = raw[34:36] = (99).to_bytes(2, "big")
        party["records"][0] = raw.hex()
    assert adjudicate(probe, owners, "room-return")["complete"] is False


def test_healed_room_proof_checks_nonactive_party_members_too(probe):
    owners = paired_snapshots(room=True)
    party = owners[0]["before_party"]
    party["count"] = 2
    party["species"].insert(1, 100)
    party["records"].append(record(species=100, hp=50, status=64).hex())
    owners[0]["room_return"]["healed_party"] = healed_party(party)
    assert adjudicate(probe, owners, "room-return")["complete"] is True
    owners[0]["room_return"]["healed_party"]["records"][1] = party["records"][1]
    assert adjudicate(probe, owners, "room-return")["complete"] is False


@pytest.mark.parametrize("version", ["red_color", "blue_color", "yellow"])
def test_constructor_accepts_one_living_ordinary_member_without_advancing(probe, version):
    session = FakeSession(probe, version=version)
    before = dict(session.memory.values)
    _, readiness, driver = make_driver(probe, version=version, session=session)
    try:
        snapshot = driver.snapshot()
        assert snapshot["before_party"]["count"] == 1
        assert snapshot["baseline"] is None and snapshot["turn"] is None
        assert snapshot["room_return"] is None
        assert snapshot["full_authentic_acceptance"] is False
        assert "unknown" in snapshot["provenance"]
        assert "party_qualified" in readiness.local
        assert driver.objective_complete() is False
        assert session._pyboy.frame_count == 0
        assert session.memory.values == before
        assert session.memory.forbidden == session.forbidden == []
        snapshot["before_party"]["records"].clear()
        assert len(driver.snapshot()["before_party"]["records"]) == 1
    finally:
        driver.close()
    assert session.hooks == {}


@pytest.mark.parametrize(
    "records,move_slot",
    [
        ([record(pp=0)], None),
        ([record(pp=0)], 0),
        ([record(move=0, pp=0)], None),
        ([record(hp=0)], 0),
        ([record()], 1),
        ([record()], 4),
        ([record()], -1),
        ([record()], True),
    ],
)
def test_unusable_initial_party_or_move_fails_closed_without_repair(probe, records, move_slot):
    session = FakeSession(probe, records=records)
    before = dict(session.memory.values)
    with pytest.raises((ValueError, RuntimeError)):
        make_driver(probe, session=session, move_slot=move_slot)
    assert session.memory.values == before
    assert session.memory.forbidden == session.forbidden == []
    assert session.hooks == {}


@pytest.mark.parametrize("fainted_prefix", [1, 2, 5])
def test_first_living_member_qualifies_even_when_fainted_leads_have_no_pp(probe, fainted_prefix):
    records = [record(hp=0, pp=0) for _ in range(fainted_prefix)] + [record(species=153)]
    session, _, driver = make_driver(probe, records=records)
    try:
        captured = driver.snapshot()["before_party"]
        assert captured["count"] == fainted_prefix + 1
        assert captured["records"] == [r.hex() for r in records]
        assert session.memory.forbidden == session.forbidden == []
    finally:
        driver.close()


def test_later_member_cannot_replace_first_living_member_with_no_legal_pp(probe):
    session = FakeSession(probe, records=[record(hp=0), record(pp=0), record(species=153)])
    before = dict(session.memory.values)
    with pytest.raises((ValueError, RuntimeError)):
        make_driver(probe, session=session)
    assert session.hooks == {}
    assert session.memory.values == before
    assert session.memory.forbidden == session.forbidden == []


@pytest.mark.parametrize("offset", range(9))
@pytest.mark.parametrize("version", ["red_color", "yellow"])
def test_damaged_exchange_continuation_bytes_fail_before_hook_install(probe, version, offset):
    session = FakeSession(probe, version=version)
    bank, start = session.symbols.bank_addr("SelectEnemyMove")
    session.memory.values[bank, start + 10 + offset] ^= 1
    with pytest.raises((ValueError, RuntimeError)):
        make_driver(probe, version=version, session=session)
    assert session.installs == []
    assert session.hooks == {}
    assert session.memory.forbidden == session.forbidden == []


@pytest.mark.parametrize(
    "label",
    [
        "CheckPlayerStatusConditions.MonHurtItselfOrFullyParalysed",
        "CheckEnemyStatusConditions.monHurtItselfOrFullyParalysed",
    ],
)
@pytest.mark.parametrize("offset", range(6))
def test_damaged_paralysis_branch_is_not_accepted_as_skip_proof(probe, label, offset):
    session = FakeSession(probe)
    bank, end = session.symbols.bank_addr(label)
    session.memory.values[bank, end - 6 + offset] ^= 1
    with pytest.raises((ValueError, RuntimeError)):
        make_driver(probe, session=session)
    assert session.installs == []
    assert session.hooks == {}


@pytest.mark.parametrize("operation", ["before_step", "after_step", "snapshot", "close"])
def test_foreign_thread_cannot_observe_control_or_remove_hooks(probe, operation):
    session, _, driver = make_driver(probe)
    reads, lookups = len(session.memory.reads), len(session.symbols.lookups)
    hooks = dict(session.hooks)
    errors = []

    def foreign():
        try:
            if operation == "before_step":
                driver.before_step(frame_offset=0)
            elif operation == "after_step":
                driver.after_step(call={})
            else:
                getattr(driver, operation)()
        except BaseException as exc:  # noqa: BLE001 - inspect foreign-thread contract errors.
            errors.append(exc)

    try:
        worker = threading.Thread(target=foreign)
        worker.start()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
        assert "owner" in str(errors[0]).lower()
        assert len(session.memory.reads) == reads
        assert len(session.symbols.lookups) == lookups
        assert session.hooks == hooks
        assert session.forbidden == session.memory.forbidden == []
    finally:
        driver.close()


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_partial_registration_rolls_back_owned_hooks_and_preserves_both_errors(
    probe, cleanup_fails
):
    session = FakeSession(probe)
    collision = RuntimeError("test registration collision")
    cleanup = RuntimeError("test rollback failure")
    existing = (object(), object())
    failed = []
    attempted_removals = []

    def register(bank, address, callback, context):
        if len(session.installs) == 3:
            failed.append((bank, address))
            session.hooks[bank, address] = existing
            raise collision
        session.register(bank, address, callback, context)

    def deregister(bank, address):
        location = bank, address
        attempted_removals.append(location)
        if cleanup_fails and location == session.installs[-1]:
            raise cleanup
        session.deregister(bank, address)

    session._pyboy.hook_register = register
    session._pyboy.hook_deregister = deregister
    with pytest.raises((RuntimeError, BaseExceptionGroup)) as caught:
        make_driver(probe, session=session)
    assert len(failed) == 1
    assert attempted_removals == list(reversed(session.installs))
    assert session.hooks[failed[0]] is existing
    assert failed[0] not in attempted_removals
    leaves = exception_leaves(caught.value)
    assert collision in leaves
    assert (cleanup in leaves) is cleanup_fails
    expected = {failed[0], session.installs[-1]} if cleanup_fails else {failed[0]}
    assert set(session.hooks) == expected
    assert session.memory.forbidden == session.forbidden == []


def test_close_attempts_all_owned_hooks_even_when_one_removal_fails(probe):
    session, _, driver = make_driver(probe)
    installed = list(session.installs)
    sentinel = (object(), object())
    session.hooks[99, 0x4444] = sentinel
    attempted = []

    def deregister(bank, address):
        location = bank, address
        attempted.append(location)
        if location == installed[-1]:
            raise RuntimeError("one removal failed")
        session.deregister(bank, address)

    session._pyboy.hook_deregister = deregister
    try:
        with pytest.raises((RuntimeError, BaseExceptionGroup)):
            driver.close()
        assert attempted == list(reversed(installed))
        assert session.hooks[99, 0x4444] is sentinel
        # A native hook that could not be removed must be inert after close.
        callback, context = session.hooks[installed[-1]]
        reads = len(session.memory.reads)
        callback(context)
        assert len(session.memory.reads) == reads
    finally:
        session._pyboy.hook_deregister = session.deregister
        driver.close()
    assert session.hooks == {(99, 0x4444): sentinel}


def test_callbacks_and_reports_are_bounded_and_never_apply_input(probe):
    session, _, driver = make_driver(probe)
    before = dict(session.memory.values)
    try:
        for _ in range(100):
            session.fire("SaveGameData")
        snapshot = driver.snapshot()
        assert len(snapshot["recent_transitions"]) <= 32
        assert len(json.dumps(snapshot)) < 16000
        assert session._pyboy.frame_count == 0
        assert session.memory.values == before
        assert session.memory.forbidden == session.forbidden == []
    finally:
        driver.close()


def test_repeated_frame_and_nonprogressing_inputs_have_finite_quotas(probe):
    session, _, driver = make_driver(probe)
    actions = []
    try:
        for frame in range(0, 10000, 40):
            try:
                action = driver.before_step(frame_offset=frame)
            except RuntimeError:
                break
            if action is not None:
                assert isinstance(action, tuple) and len(action) == 2
                actions.append(action)
                assert driver.before_step(frame_offset=frame) is None
        assert actions, "test must exercise input proposals, not a permanently idle fake"
        assert len(actions) <= 64, "unbounded retries on a nonprogressing menu"
        assert session._pyboy.frame_count == 0
        assert session.memory.forbidden == session.forbidden == []
    finally:
        driver.close()


@pytest.mark.parametrize(
    "side,fault",
    [
        ("local", None),
        *[
            (side, fault)
            for side in ("local", "enemy")
            for fault in ("unsupported_effect", "corrupt_id", "table_id", "zero_power")
        ],
    ],
)
def test_observer_requires_valid_moves_completed_actions_and_a_later_boundary(probe, side, fault):
    session, _, driver = make_driver(probe)

    def menu(**fields):
        for name, value in fields.items():
            session.set_bytes(name, [value])

    try:
        session.fire("MainInBattleLoop")
        menu(
            wCurrentMenuItem=0,
            wMaxMenuItem=1,
            wMenuWatchedKeys=1,
            wTopMenuItemY=14,
            wTopMenuItemX=15,
        )
        session.fire("DisplayBattleMenu")
        session.fire("HandleMenuInput")
        assert driver.before_step(frame_offset=0)[0] == "a"  # Inspect party.
        session.fire("HandlePartyMenuInput")
        menu(wPartyMenuTypeOrMessageID=0, wMenuWatchedKeys=3, wMaxMenuItem=0)
        assert driver.before_step(frame_offset=20)[0] == "b"
        menu(wTopMenuItemX=9, wMenuWatchedKeys=1, wMaxMenuItem=1)
        session.fire("DisplayBattleMenu")
        session.fire("HandleMenuInput")
        assert driver.before_step(frame_offset=40)[0] == "a"  # FIGHT.
        menu(wCurrentMenuItem=1, wMaxMenuItem=2)
        session.fire("MoveSelectionMenu")
        session.fire("HandleMenuInput")
        assert driver.before_step(frame_offset=60)[0] == "a"
        menu(
            wPlayerSelectedMove=1,
            wEnemySelectedMove=33,
            wSerialExchangeNybbleSendData=0,
            wSerialExchangeNybbleReceiveData=0,
        )
        move_id = 1 if side == "local" else 33
        table = 0x4000 + (move_id - 1) * 6
        if fault == "unsupported_effect":
            session.memory.values[14, table + 1] = 1
        elif fault == "corrupt_id":
            if side == "local":
                menu(wPlayerSelectedMove=166)
            else:
                session.set_bytes("wEnemyMonMoves", [166, 0, 0, 0])
        elif fault == "table_id":
            session.memory.values[14, table] = move_id + 1
        elif fault == "zero_power":
            session.memory.values[14, table + 2] = 0
        bank, start = session.symbols.bank_addr("SelectEnemyMove")
        callback, context = session.hooks[bank, start + 13]
        callback(context)
        if fault:
            with pytest.raises((ValueError, RuntimeError)):
                driver.after_step(call={})
            assert driver.snapshot()["turn"] is None
            assert driver.objective_complete() is False
            assert session.memory.forbidden == session.forbidden == []
            return
        for label in ("ExecutePlayerMove", "ExecuteEnemyMove"):
            session.fire(label)
        assert driver.snapshot()["turn"] is None
        assert driver.objective_complete() is False
        session.fire("PlayerCanExecuteMove")
        session.set_bytes("wBattleMonPP", [19, 0, 0, 0])
        menu(hWhoseTurn=0)
        session.fire("DecrementPP")
        session.set_bytes("wDamage", [0, 15])
        session.fire("ApplyDamageToEnemyPokemon")
        session.set_bytes("wEnemyMonHP", [0, 65])
        session.fire("ApplyAttackToEnemyPokemonDone")
        session.fire("ExecutePlayerMoveDone")
        session.fire("EnemyCanExecuteMove")
        menu(hWhoseTurn=1)
        session.fire("DecrementPP")  # Enemy-side entry must not count as local PP use.
        session.set_bytes("wDamage", [0, 10])
        session.fire("ApplyDamageToPlayerPokemon")
        session.set_bytes("wBattleMonHP", [0, 70])
        session.fire("ApplyAttackToPlayerPokemonDone")
        session.fire("ExecuteEnemyMoveDone")
        assert driver.snapshot()["turn"] is None, "Done entries are not the settled boundary"
        session.fire("MainInBattleLoop")
        snapshot = driver.snapshot()
        assert snapshot["unsupported_reason"] is None
        assert snapshot["turn"]["local"]["hp"] == 70
        assert snapshot["turn"]["enemy"]["hp"] == 65
        assert snapshot["turn"]["pp"]["after"] == [19, 0, 0, 0]
        assert snapshot["turn"]["pp"]["decrement_entries"] == 1
        assert snapshot["baseline"]["seq"] < snapshot["turn"]["exchange_seq"]
        assert snapshot["turn"]["exchange_seq"] < snapshot["turn"]["settled_seq"]
        assert driver.objective_complete() is True
        assert driver.before_step(frame_offset=80) is None
        assert session._pyboy.frame_count == 0
        assert session.memory.forbidden == session.forbidden == []
    finally:
        driver.close()


def terminal_driver(probe, *, role=2, context=None, version="red_color", facing=0):
    session, readiness, driver = make_driver(probe, version=version)
    fields = {
        "wCurMap": 0xF0,
        "wLinkState": 1,
        "hSerialConnectionStatus": role,
        "wXCoord": 3 if role == 2 else 6,
        "wYCoord": 4,
        "wSpritePlayerStateData1FacingDirection": facing,
        "wJoyIgnore": 0,
        "wWalkCounter": 0,
        "wStatusFlags5": 0,
    }
    for name, value in fields.items():
        session.set_bytes(name, [value])
    if context == "warp":
        session.fire("PrepareForSpecialWarp")
    return session, readiness, driver


@pytest.mark.parametrize("version", ["red_color", "blue_color", "yellow"])
@pytest.mark.parametrize("context", [None, "warp"])
@pytest.mark.parametrize("role,direction,facing", [(2, "right", 0x0C), (1, "left", 0x08)])
def test_terminal_faces_then_uses_a_until_actual_context_advance(
    probe, version, context, role, direction, facing
):
    session, _, driver = terminal_driver(probe, role=role, context=context, version=version)
    try:
        before = dict(session.memory.values)
        assert driver.before_step(frame_offset=0) == (direction, 8)
        snapshot = driver.snapshot()
        assert snapshot["interaction_observation"] == {
            "frame_offset": 0,
            "map": 0xF0,
            "link_state": 1,
            "serial_role": role,
            "x": 3 if role == 2 else 6,
            "y": 4,
            "facing": 0,
            "joy_ignore": 0,
            "walk_counter": 0,
            "status_flags5": 0,
            "admission_reason": "orient",
        }
        assert snapshot["last_action"] == {
            "frame_offset": 0,
            "button": direction,
            "duration": 8,
            "cadence": 30,
            "phase": context,
        }
        assert snapshot["hidden_event_quota_used"] == 1
        assert driver.before_step(frame_offset=29) is None
        assert session.memory.values == before
        # Only the test harness changes this synthetic observation; no CPU runs.
        session.set_bytes("wSpritePlayerStateData1FacingDirection", [facing])
        before = dict(session.memory.values)
        assert driver.before_step(frame_offset=30) == ("a", 4)
        assert driver.before_step(frame_offset=49) is None
        assert driver.before_step(frame_offset=50) == ("a", 4)
        assert session.memory.values == before
        snapshot = driver.snapshot()
        assert snapshot["interaction_observation"]["facing"] == facing
        assert snapshot["interaction_observation"]["admission_reason"] == "interact"
        assert snapshot["last_action"] == {
            "frame_offset": 50,
            "button": "a",
            "duration": 4,
            "cadence": 20,
            "phase": context,
        }
        assert (
            snapshot["hidden_event_quota_used"] == snapshot["input_attempts"]["hidden_event"] == 3
        )
        session.fire("CableClubLeftGameboy" if role == 2 else "CableClubRightGameboy")
        assert driver.snapshot()["phase"] == "dialogue"
        driver.before_step(frame_offset=70)
        assert driver.snapshot()["hidden_event_quota_used"] == 3
        session.fire("CableClub_DoBattleOrTrade")
        assert driver.before_step(frame_offset=100) is None
        assert driver.objective_complete() is False  # Hook is not gameplay proof.
        assert session._pyboy.frame_count == 0
        assert session.memory.forbidden == session.forbidden == []
    finally:
        driver.close()


@pytest.mark.parametrize("role,facing", [(2, 0x0C), (1, 0x08)])
@pytest.mark.parametrize(
    "blocked",
    [
        "map",
        "link",
        "event_tile",
        "y",
        "joy",
        "walk",
        "status04",
        "status80",
        "status84",
        "peer_room",
        "peer_party",
    ],
)
def test_terminal_invalid_admission_waits_without_spending_attempts(probe, role, facing, blocked):
    session, readiness, driver = terminal_driver(probe, role=role, context="warp", facing=facing)
    faults = {
        "map": ("wCurMap", 0xEF),
        "link": ("wLinkState", 0),
        "event_tile": ("wXCoord", 4 if role == 2 else 5),
        "y": ("wYCoord", 3),
        "joy": ("wJoyIgnore", 1),
        "walk": ("wWalkCounter", 1),
        "status04": ("wStatusFlags5", 0x04),
        "status80": ("wStatusFlags5", 0x80),
        "status84": ("wStatusFlags5", 0x84),
    }
    try:
        # Establish observed room readiness without permitting an input first.
        readiness.peer.remove("colosseum_reached")
        assert driver.before_step(frame_offset=0) is None
        readiness.peer.add("colosseum_reached")
        if blocked.startswith("peer_"):
            readiness.peer.remove(
                "colosseum_reached" if blocked == "peer_room" else "party_qualified"
            )
        else:
            name, value = faults[blocked]
            original = session.memory.values[session.symbols.addr_of(name)]
            session.set_bytes(name, [value])
        before = dict(session.memory.values)
        for frame in range(20, 420, 20):
            assert driver.before_step(frame_offset=frame) is None
        assert session.memory.values == before
        snapshot = driver.snapshot()
        assert snapshot["hidden_event_quota_used"] == 0
        assert snapshot["input_attempts"].get("hidden_event", 0) == 0
        assert snapshot["last_action"] is None
        reasons = {
            "map": "room_not_ready",
            "link": "room_not_ready",
            "event_tile": "unexpected_adjacent_coordinates",
            "y": "unexpected_adjacent_coordinates",
            "joy": "input_masked_or_walking",
            "walk": "input_masked_or_walking",
            "status04": "a_blocked_or_scripted_movement",
            "status80": "a_blocked_or_scripted_movement",
            "status84": "a_blocked_or_scripted_movement",
            "peer_room": "peer_room_not_ready",
        }
        if blocked in reasons:
            assert snapshot["interaction_observation"]["admission_reason"] == reasons[blocked]
        if blocked.startswith("peer_"):
            readiness.peer.update(PHASES)
        else:
            session.set_bytes(name, [original])
        # All six attempts must still be available after arbitrarily many waits.
        for frame in range(500, 620, 20):
            assert driver.before_step(frame_offset=frame) == ("a", 4)
        assert session._pyboy.frame_count == 0
        assert session.memory.forbidden == session.forbidden == []
    finally:
        driver.close()


@pytest.mark.parametrize("role,direction,facing", [(2, "right", 0x0C), (1, "left", 0x08)])
@pytest.mark.parametrize("context", [None, "warp"])
def test_terminal_direction_and_a_share_exactly_six_attempts(
    probe, role, direction, facing, context
):
    session, _, driver = terminal_driver(probe, role=role, context=context)
    try:
        for frame in (0, 30, 60):
            assert driver.before_step(frame_offset=frame) == (direction, 8)
        if context is None:
            session.fire(
                "PrepareForSpecialWarp"
            )  # Context change must not reset the shared budget.
        session.set_bytes("wSpritePlayerStateData1FacingDirection", [facing])
        for frame in (90, 110, 130):
            assert driver.before_step(frame_offset=frame) == ("a", 4)
        try:
            action = driver.before_step(frame_offset=150)
        except RuntimeError as exc:
            assert "quota" in str(exc).lower() or "budget" in str(exc).lower()
        else:
            assert action is None, "facing and A must not receive separate six-attempt budgets"
        assert driver.snapshot()["hidden_event_quota_used"] == 6
        assert driver.snapshot()["input_attempts"]["hidden_event"] == 6
        assert session.memory.forbidden == session.forbidden == []
    finally:
        driver.close()


@pytest.mark.parametrize("role,facing", [(2, 0x0C), (1, 0x08)])
@pytest.mark.parametrize("status", [0x01, 0x02, 0x08, 0x40, 0x7B])
def test_terminal_status_mask_does_not_block_unrelated_bits(probe, role, facing, status):
    session, _, driver = terminal_driver(probe, role=role, facing=facing)
    try:
        session.set_bytes("wStatusFlags5", [status])
        assert driver.before_step(frame_offset=0) == ("a", 4)
        observed = driver.snapshot()["interaction_observation"]
        assert observed["status_flags5"] == status
        assert observed["admission_reason"] == "interact"
        assert session.memory.forbidden == session.forbidden == []
    finally:
        driver.close()


@pytest.mark.parametrize("role", [0, 3])
def test_terminal_invalid_serial_role_fails_before_input_or_quota(probe, role):
    session, _, driver = terminal_driver(probe, role=role)
    try:
        with pytest.raises(ValueError, match="serial role"):
            driver.before_step(frame_offset=0)
        snapshot = driver.snapshot()
        assert snapshot["hidden_event_quota_used"] == 0
        assert snapshot["last_action"] is None
        assert snapshot["unsupported_reason"]
        assert session.memory.forbidden == session.forbidden == []
    finally:
        driver.close()
