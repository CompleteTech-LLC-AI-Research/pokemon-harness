"""Shared fakes, fixtures and builders for the timed battle probe tests (#147).

Split from ``tests/test_timed_battle_probe.py`` for #147 with no behavior
change. Synthetic snapshots are NOT gameplay proof: these fakes never
instantiate PyBoy, load assets, or advance an emulated CPU. Fixture builders
populate private dictionaries, not emulator RAM. Only the external test harness
may fire an observation hook or change synthetic state.
"""

import copy
import json
from types import SimpleNamespace

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


def change(value, path, replacement):
    for key in path[:-1]:
        value = value[key]
    value[path[-1]] = replacement


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
