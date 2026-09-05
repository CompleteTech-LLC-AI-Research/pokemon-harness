"""ROM-free trade evidence contracts; these tests are not gameplay proof.

Assembly basis (read, never executed): pokered engine/link/cable_club.asm
RemovePokemon -> AddEnemyMonToPlayerParty -> TryEvolvingMon -> save ->
CableClub_DoBattleOrTradeAgain; pokeyellow has the same ordering. In both,
engine/pokemon/remove_mon.asm compacts survivors and add_mon.asm appends
PARTYMON_STRUCT_LENGTH=44, NAME_LENGTH=11 OT and nickname bytes. Thus an
in-place selected-slot replacement is wrong for multi-member parties.
"""

import copy
import importlib
import json
from types import SimpleNamespace

import pytest

PHASES = (
    "party_qualified",
    "link_menu_trade_ready",
    "link_menu_a_applied",
    "trade_center_reached",
    "select_mon_ready",
    "outgoing_slot_ready",
    "pre_evolution_copy_validated",
    "post_save_cycle_returned",
)


@pytest.fixture
def probe():
    # Missing implementation is a failure, not a skip or a substitute mock.
    return importlib.import_module("scripts._timed_trade_probe")


def party(species):
    records, ots, names = [], [], []
    for index, mon in enumerate(species):
        record = bytearray((mon + index + offset) % 256 for offset in range(44))
        record[0] = mon
        record[1:3] = b"\x00\x14"
        record[33] = 20
        records.append(record.hex())
        ots.append(bytes([mon, index] + [0x80 + index] * 8 + [0x50]).hex())
        names.append(bytes([mon, index] + [0x90 + index] * 8 + [0x50]).hex())
    return {
        "count": len(species),
        "species": [*species, 255],
        "records": records,
        "ot_names": ots,
        "nicknames": names,
    }


def exchanged(local, slot, peer, peer_slot):
    result = {"count": local["count"]}
    result["species"] = (
        local["species"][:slot]
        + local["species"][slot + 1 : -1]
        + [peer["species"][peer_slot], 255]
    )
    for field in ("records", "ot_names", "nicknames"):
        result[field] = local[field][:slot] + local[field][slot + 1 :] + [peer[field][peer_slot]]
    return result


def owners(slot_a=0, slot_b=1, count_a=3, count_b=3):
    # Internal Gen-I species indices; no selected trade-evolution species.
    before = [
        party([153, 176, 177, 84, 85, 150][:count_a]),
        party([84, 85, 150, 153, 176, 177][:count_b]),
    ]
    slots = [slot_a, slot_b]
    result = []
    for index, role in enumerate(("listen", "connect")):
        copied = exchanged(before[index], slots[index], before[1 - index], slots[1 - index])
        result.append(
            {
                "role": role,
                "version": ("blue_color", "yellow")[index],
                "outgoing_slot": slots[index],
                "checkpoint": "reciprocal-exchange",
                "phase": "post_save_cycle_returned",
                "readiness": [1] * len(PHASES),
                "checkpoint_reached": True,
                "copy_complete": True,
                "trade_endpoint_observed": True,
                "before_party": before[index],
                "copied_party": copied,
                "final_party": copy.deepcopy(copied),
                "unsupported_reason": None,
            }
        )
    return result


def adjudicate(probe, evidence, checkpoint="reciprocal-exchange"):
    original = copy.deepcopy(evidence)
    result = probe.adjudicate_pair(evidence, checkpoint)
    assert evidence == original, "parent must not rewrite evidence to make it match"
    json.dumps(result, allow_nan=False)
    assert {"complete", "checkpoint_only", "errors", "status"} <= result.keys()
    return result


@pytest.mark.parametrize("slot_a,slot_b", [(0, 0), (0, 2), (1, 0), (1, 2), (2, 1)])
def test_reciprocal_copy_compacts_survivors_and_appends_incoming(probe, slot_a, slot_b):
    result = adjudicate(probe, owners(slot_a, slot_b))
    assert result["complete"] is True
    assert result["checkpoint_only"] is False
    assert result["status"] == "complete"
    assert result["errors"] == []


@pytest.mark.parametrize(
    "count_a,count_b,slot_a,slot_b",
    [
        (1, 1, 0, 0),
        (1, 6, 0, 4),
        (6, 1, 5, 0),
        (6, 6, 2, 3),
    ],
)
def test_singleton_and_full_parties_use_actual_selected_slots(
    probe, count_a, count_b, slot_a, slot_b
):
    assert adjudicate(probe, owners(slot_a, slot_b, count_a, count_b))["complete"] is True


def test_in_place_replacement_is_not_a_trade(probe):
    evidence = owners(0, 1)
    for index in range(2):
        wrong = copy.deepcopy(evidence[index]["before_party"])
        peer = evidence[1 - index]
        slot = evidence[index]["outgoing_slot"]
        for field in ("species", "records", "ot_names", "nicknames"):
            wrong[field][slot] = peer["before_party"][field][peer["outgoing_slot"]]
        evidence[index]["copied_party"] = wrong
        evidence[index]["final_party"] = copy.deepcopy(wrong)
    assert adjudicate(probe, evidence)["complete"] is False


def test_two_reports_from_same_role_are_not_reciprocal_evidence(probe):
    evidence = owners()
    evidence[1]["role"] = "listen"
    assert adjudicate(probe, evidence)["complete"] is False


def test_select_mon_report_cannot_be_relabelled_full_exchange(probe):
    evidence = owners()
    evidence[0]["checkpoint"] = "select-mon"
    assert adjudicate(probe, evidence)["complete"] is False


@pytest.mark.parametrize("field,width", [("records", 44), ("ot_names", 11), ("nicknames", 11)])
@pytest.mark.parametrize("slot", [0, 1, 2])
def test_every_identity_byte_including_compacted_survivors_is_checked(probe, field, width, slot):
    for byte_index in range(width):
        evidence = owners()
        raw = bytearray.fromhex(evidence[0]["copied_party"][field][slot])
        raw[byte_index] ^= 1
        evidence[0]["copied_party"][field][slot] = raw.hex()
        evidence[0]["final_party"] = copy.deepcopy(evidence[0]["copied_party"])
        assert adjudicate(probe, evidence)["complete"] is False, (field, slot, byte_index)


def test_select_mon_is_only_a_partial_checkpoint(probe):
    evidence = owners()
    for owner in evidence:
        owner.update(
            checkpoint="select-mon",
            phase="select_mon_ready",
            copy_complete=False,
            trade_endpoint_observed=False,
            copied_party=None,
            final_party=None,
        )
    result = adjudicate(probe, evidence, "select-mon")
    assert result["complete"] is False
    assert result["checkpoint_only"] is True
    assert result["status"] == "partial"


@pytest.mark.parametrize("fault", ["missing_party", "bad_count", "slot", "bool_species"])
def test_select_mon_boolean_cannot_replace_party_and_slot_qualification(probe, fault):
    evidence = owners()
    for owner in evidence:
        owner.update(
            checkpoint="select-mon",
            phase="select_mon_ready",
            copy_complete=False,
            trade_endpoint_observed=False,
            copied_party=None,
            final_party=None,
        )
    if fault == "missing_party":
        evidence[0]["before_party"] = None
    elif fault == "bad_count":
        evidence[0]["before_party"]["count"] = 7
    elif fault == "slot":
        evidence[0]["outgoing_slot"] = 3
    else:
        evidence[0]["before_party"]["species"][0] = True
        raw = bytearray.fromhex(evidence[0]["before_party"]["records"][0])
        raw[0] = 1
        evidence[0]["before_party"]["records"][0] = raw.hex()
    result = adjudicate(probe, evidence, "select-mon")
    assert result["complete"] is False
    assert result["checkpoint_only"] is False
    assert result["errors"]


def test_species_bool_is_not_a_valid_species_integer(probe):
    evidence = owners()
    # Build otherwise internally consistent identity so integer coercion cannot hide the issue.
    for owner in evidence:
        for phase in ("before_party", "copied_party", "final_party"):
            data = owner[phase]
            for index in range(data["count"]):
                data["species"][index] = True
                raw = bytearray.fromhex(data["records"][index])
                raw[0] = 1
                data["records"][index] = raw.hex()
    assert adjudicate(probe, evidence)["complete"] is False


@pytest.mark.parametrize("side", [0, 1])
@pytest.mark.parametrize(
    "missing", ["copy_complete", "trade_endpoint_observed", "checkpoint_reached"]
)
def test_one_sided_or_copy_only_evidence_never_completes(probe, side, missing):
    evidence = owners()
    evidence[side][missing] = False
    assert adjudicate(probe, evidence)["complete"] is False


@pytest.mark.parametrize("field,width", [("records", 44), ("ot_names", 11), ("nicknames", 11)])
def test_unexplained_final_transform_fails_closed_and_preserves_raw_copy(probe, field, width):
    for offset in range(width):
        evidence = owners()
        raw = bytearray.fromhex(evidence[0]["final_party"][field][-1])
        raw[offset] ^= 1
        evidence[0]["final_party"][field][-1] = raw.hex()
        result = adjudicate(probe, evidence)
        assert result["complete"] is False
        assert result["status"] in ("unsupported", "mismatch")


def test_evolution_label_cannot_authorize_arbitrary_record_changes(probe):
    evidence = owners()
    evidence[0]["unsupported_reason"] = "trade evolution transformation not validated"
    evidence[0]["final_party"]["records"][-1] = "ff" * 44
    result = adjudicate(probe, evidence)
    assert result["complete"] is False
    assert result["status"] in ("unsupported", "mismatch")


@pytest.mark.parametrize("slot", [-1, 3, 6, True])
def test_invalid_outgoing_slot_never_becomes_proof(probe, slot):
    evidence = owners()
    evidence[0]["outgoing_slot"] = slot
    assert adjudicate(probe, evidence)["complete"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("count", 0),
        ("count", 7),
        ("species", [153, 176, 177, 0]),
        ("records", ["00"]),
        ("ot_names", ["00"]),
        ("nicknames", ["00"]),
    ],
)
def test_malformed_party_evidence_fails_closed(probe, field, value):
    evidence = owners()
    evidence[0]["copied_party"][field] = value
    assert adjudicate(probe, evidence)["complete"] is False


class FakeSymbols:
    def __init__(self):
        self.addresses = {}

    def addr_of(self, name):
        if name not in self.addresses:
            self.addresses[name] = 0x8000 + 0x180 * len(self.addresses)
        return self.addresses[name]

    def bank_addr(self, name):
        return (1, self.addr_of(name))


class ReadOnlyMemory:
    def __init__(self):
        self.values = {}
        self.reads = 0

    def __getitem__(self, address):
        if isinstance(address, slice):
            return [self[i] for i in range(address.start, address.stop, address.step or 1)]
        self.reads += 1
        return self.values.get(address, 0)

    def __setitem__(self, address, value):
        raise AssertionError("driver/hook must never write RAM")


class FakeSession:
    def __init__(self):
        self.symbols = FakeSymbols()
        self.memory = ReadOnlyMemory()
        self.hooks = {}
        self.external_steps = 0
        self._pyboy = SimpleNamespace(
            memory=self.memory,
            hook_register=self.register,
            hook_deregister=self.deregister,
            frame_count=0,
            register_file=SimpleNamespace(PC=0, HL=0, A=0, B=0, C=0),
        )
        initial = party([153, 176, 177])
        self.set_bytes("wPartyCount", [initial["count"]])
        self.set_bytes("wPartySpecies", initial["species"])
        for symbol, field in (
            ("wPartyMons", "records"),
            ("wPartyMonOT", "ot_names"),
            ("wPartyMonNicks", "nicknames"),
        ):
            self.set_bytes(symbol, bytes.fromhex("".join(initial[field])))

    def set_bytes(self, symbol, values):
        address = self.symbols.addr_of(symbol)
        self.memory.values.update({address + i: value for i, value in enumerate(values)})

    def register(self, bank, address, callback, context):
        assert (bank, address) not in self.hooks
        self.hooks[bank, address] = callback, context

    def deregister(self, bank, address):
        del self.hooks[bank, address]

    def current_tick(self):
        return self.external_steps

    def step(self, *args, **kwargs):
        raise AssertionError("only the external owner loop may tick")

    def press(self, *args, **kwargs):
        raise AssertionError("driver returns input; caller owns applying it")

    def fire(self, name):
        callback, context = self.hooks[self.symbols.bank_addr(name)]
        callback(context)


class Readiness:
    def __init__(self):
        self.local = set()
        self.peer = set()

    def publish_ready(self, phase):
        assert phase in PHASES
        self.local.add(phase)

    def peer_ready(self, phase):
        assert phase in PHASES
        return phase in self.peer


@pytest.mark.parametrize("count", [1, 3, 6])
def test_party_capture_is_bounded_read_only_and_detached(probe, count):
    session = FakeSession()
    expected = party([153, 176, 177, 84, 85, 150][:count])
    session.set_bytes("wPartyCount", [count])
    session.set_bytes("wPartySpecies", expected["species"])
    for symbol, field in (
        ("wPartyMons", "records"),
        ("wPartyMonOT", "ot_names"),
        ("wPartyMonNicks", "nicknames"),
    ):
        session.set_bytes(symbol, bytes.fromhex("".join(expected[field])))
    before_reads = session.memory.reads
    observed = probe.read_party(session)
    assert observed == expected
    # Count + species/sentinel + 44+11+11 bytes per occupied member.
    assert session.memory.reads - before_reads <= 2 + 67 * count
    session.set_bytes("wPartyMons", [0])
    assert observed == expected
    assert session.external_steps == 0


@pytest.mark.parametrize("count", [0, 7, 255])
def test_party_capture_rejects_bad_count_before_unbounded_reads(probe, count):
    session = FakeSession()
    session.set_bytes("wPartyCount", [count])
    with pytest.raises(ValueError):
        probe.read_party(session)
    assert session.memory.reads == 1


def test_fixed_readiness_contract(probe):
    assert tuple(probe.READINESS_PHASES) == PHASES


@pytest.mark.parametrize("slot", [-1, 3, 6, True])
def test_owner_rejects_unavailable_slot_before_any_input(probe, slot):
    session = FakeSession()
    with pytest.raises((ValueError, RuntimeError)):
        driver = probe.create_owner_driver(
            session=session,
            side="listen",
            options={
                "version": "blue_color",
                "outgoing_slot": slot,
                "checkpoint": "reciprocal-exchange",
            },
            peer_state=Readiness(),
        )
        driver.before_step(frame_offset=0)
    assert session.external_steps == 0


@pytest.mark.parametrize(
    "fields",
    [
        {
            "wMaxMenuItem": 3,
            "wWhichTradeMonSelectionMenu": 0,
            "wMenuWatchedKeys": 0x91,
            "wTopMenuItemX": 1,
            "wTopMenuItemY": 1,
        },
        {"wMaxMenuItem": 0, "wMenuWatchedKeys": 0x13, "wTopMenuItemX": 1, "wTopMenuItemY": 16},
        {"wMaxMenuItem": 0, "wMenuWatchedKeys": 0x23, "wTopMenuItemX": 11, "wTopMenuItemY": 16},
        {"wMaxMenuItem": 1, "wMenuWatchedKeys": 3, "wTwoOptionMenuID": 5},
    ],
)
def test_stale_trade_menu_fields_do_not_authorize_input_or_stop_external_ticks(probe, fields):
    session, readiness = FakeSession(), Readiness()
    # Matching fields from an earlier menu must not replace fresh phase evidence.
    for symbol, value in {
        "wCurMap": 239,
        "wLinkState": 1,
        "hSerialConnectionStatus": 2,
        "wCurrentMenuItem": 0,
        **fields,
    }.items():
        session.set_bytes(symbol, [value])
    driver = probe.create_owner_driver(
        session=session,
        side="listen",
        options={"version": "blue_color", "outgoing_slot": 0, "checkpoint": "reciprocal-exchange"},
        peer_state=readiness,
    )
    # Peer history does not authenticate this owner's current menu.
    readiness.peer.update(PHASES)
    for offset in range(8):
        action = driver.before_step(frame_offset=offset)
        assert action is None or action[0] != "a"
        # The mandatory CPU step belongs to this harness, not the callback.
        session.external_steps += 1
        session._pyboy.frame_count += 1
        driver.after_step(
            call={
                "status": "completed",
                "actual_completed_frames": 1,
                "frame_offset": offset,
                "requested_frames": 1,
            }
        )
    assert session.external_steps == 8
    assert driver.objective_complete() is False
    json.dumps(driver.snapshot(), allow_nan=False)


def make_driver(probe, *, slot=0, checkpoint="reciprocal-exchange"):
    session, readiness = FakeSession(), Readiness()
    readiness.peer.update(PHASES)
    driver = probe.create_owner_driver(
        session=session,
        side="listen",
        options={"version": "blue_color", "outgoing_slot": slot, "checkpoint": checkpoint},
        peer_state=readiness,
    )
    return session, readiness, driver


def set_menu(session, **overrides):
    fields = {
        "wCurMap": 239,
        "wLinkState": 1,
        "hSerialConnectionStatus": 2,
        "wWhichTradeMonSelectionMenu": 0,
        "wCurrentMenuItem": 0,
        "wMaxMenuItem": 3,
        "wMenuWatchedKeys": 0x91,
        "wTopMenuItemX": 1,
        "wTopMenuItemY": 1,
    }
    fields.update(overrides)
    for name, value in fields.items():
        session.set_bytes(name, [value])


@pytest.mark.parametrize(
    "field,value",
    [
        ("wMaxMenuItem", 2),
        ("wMenuWatchedKeys", 1),
        ("wWhichTradeMonSelectionMenu", 1),
        ("wTopMenuItemX", 11),
        ("wTopMenuItemY", 16),
    ],
)
def test_fresh_party_hook_still_requires_all_live_menu_fields(probe, field, value):
    session, _, driver = make_driver(probe)
    set_menu(session, **{field: value})
    session.fire("TradeCenter_SelectMon")
    session.fire("TradeCenter_SelectMon.playerMonMenu_HandleInput")
    session.fire("HandleMenuInput")
    assert driver.before_step(frame_offset=0) is None


def test_party_entry_needs_actual_poll_then_targets_explicit_slot(probe):
    session, _, driver = make_driver(probe, slot=1)
    set_menu(session)
    session.fire("TradeCenter_SelectMon")
    session.fire("TradeCenter_SelectMon.playerMonMenu_HandleInput")
    assert driver.before_step(frame_offset=0) is None
    session.fire("HandleMenuInput")
    assert driver.before_step(frame_offset=1)[0] == "down"
    set_menu(session, wCurrentMenuItem=1)
    session.fire("HandleMenuInput")
    assert driver.before_step(frame_offset=10)[0] == "a"
    assert driver.before_step(frame_offset=11) is None


def test_confirmation_accepts_rom_cleared_id_only_after_fresh_trade_menu_entry(probe):
    session, _, driver = make_driver(probe)
    set_menu(session, wTwoOptionMenuID=5, wMaxMenuItem=1, wMenuWatchedKeys=3)
    session.fire("DisplayTwoOptionMenu")
    session.fire("HandleMenuInput")
    assert driver.before_step(frame_offset=0) != ("a", 4)
    session.fire("TradeCenter_Trade")
    session.fire("DisplayTwoOptionMenu")
    # pokered text_box.asm:307 and pokeyellow text_box.asm:279 clear ID before polling.
    session.set_bytes("wTwoOptionMenuID", [0])
    assert driver.before_step(frame_offset=100) is None
    session.fire("HandleMenuInput")
    assert driver.before_step(frame_offset=101) == ("a", 4)


def test_cleared_yes_no_id_without_trade_menu_entry_cannot_confirm(probe):
    session, _, driver = make_driver(probe)
    set_menu(session, wTwoOptionMenuID=0, wMaxMenuItem=1, wMenuWatchedKeys=3)
    session.fire("TradeCenter_Trade")
    # This is a generic yes/no construction, not observed TRADE_CANCEL_MENU=5.
    session.fire("DisplayTwoOptionMenu")
    session.fire("HandleMenuInput")
    assert driver.before_step(frame_offset=0) is None


def test_select_mon_local_goal_does_not_stop_external_owner_cpu(probe):
    session, _, driver = make_driver(probe, checkpoint="select-mon")
    set_menu(session)
    session.fire("TradeCenter_SelectMon")
    session.fire("TradeCenter_SelectMon.playerMonMenu_HandleInput")
    session.fire("HandleMenuInput")
    assert driver.before_step(frame_offset=0) is None
    assert driver.objective_complete() is True
    for offset in range(1, 12):
        assert driver.before_step(frame_offset=offset) is None
        session.external_steps += 1
        driver.after_step(call={"status": "completed", "actual_completed_frames": 1})
    assert session.external_steps == 11
    assert driver.snapshot()["trade_endpoint_observed"] is False


def install_party(session, data):
    session.set_bytes("wPartyCount", [data["count"]])
    session.set_bytes("wPartySpecies", data["species"])
    for symbol, field in (
        ("wPartyMons", "records"),
        ("wPartyMonOT", "ot_names"),
        ("wPartyMonNicks", "nicknames"),
    ):
        session.set_bytes(symbol, bytes.fromhex("".join(data[field])))


def test_pre_evolution_capture_and_post_save_endpoint_are_distinct(probe):
    session, _, driver = make_driver(probe, slot=1)
    session.fire("CableClub_DoBattleOrTradeAgain")  # initial entry proves nothing
    session.fire("SavePartyAndDexData")  # unrelated earlier save proves nothing
    session.fire("TryEvolvingMon")
    assert driver.snapshot()["copied_party"] is None
    session.fire("TradeCenter_Trade")
    session.fire("TradeCenter_Trade.tradeConfirmed")
    session.fire("_AddEnemyMonToPlayerParty")
    assert driver.snapshot()["copy_complete"] is False
    incoming = exchanged(party([153, 176, 177]), 1, party([84]), 0)
    install_party(session, incoming)
    session.fire("TryEvolvingMon")
    assert driver.snapshot()["copied_party"] == incoming
    assert driver.snapshot()["copy_complete"] is True
    assert driver.objective_complete() is False
    session.fire("CableClub_DoBattleOrTradeAgain")
    assert driver.objective_complete() is False
    session.fire("SavePartyAndDexData")
    assert driver.objective_complete() is False
    session.fire("CableClub_DoBattleOrTradeAgain")
    assert driver.objective_complete() is True
    assert driver.snapshot()["final_party"] == incoming
    for offset in range(600, 605):
        assert driver.before_step(frame_offset=offset) is None
        session.external_steps += 1
        driver.after_step(call={"status": "completed", "actual_completed_frames": 1})
    assert session.external_steps == 5


def test_departed_menu_phase_cannot_reuse_old_poll_permission(probe):
    session, _, driver = make_driver(probe)
    set_menu(session)
    session.fire("TradeCenter_SelectMon.playerMonMenu_HandleInput")
    session.fire("HandleMenuInput")
    session.fire("TradeCenter_SelectMon.choseTrade")
    assert driver.before_step(frame_offset=0) is None


def test_input_cadence_does_not_allow_repeated_requests_at_same_frame(probe):
    session, _, driver = make_driver(probe, slot=1)
    set_menu(session)
    session.fire("TradeCenter_SelectMon.playerMonMenu_HandleInput")
    session.fire("HandleMenuInput")
    assert driver.before_step(frame_offset=0)[0] == "down"
    for _ in range(100):
        assert driver.before_step(frame_offset=0) is None
    assert session.external_steps == 0


def test_owner_retains_exact_copy_but_rejects_unexplained_final_change(probe):
    session, _, driver = make_driver(probe, slot=1)
    session.fire("TradeCenter_Trade")
    session.fire("TradeCenter_Trade.tradeConfirmed")
    session.fire("_AddEnemyMonToPlayerParty")
    incoming = exchanged(party([153, 176, 177]), 1, party([84]), 0)
    install_party(session, incoming)
    session.fire("TryEvolvingMon")
    mutated = copy.deepcopy(incoming)
    raw = bytearray.fromhex(mutated["records"][-1])
    raw[29] ^= 1  # PP changes are not automatically justified by evolution.
    mutated["records"][-1] = raw.hex()
    install_party(session, mutated)
    session.fire("SavePartyAndDexData")
    session.fire("CableClub_DoBattleOrTradeAgain")
    assert driver.objective_complete() is False
    assert driver.snapshot()["copied_party"] == incoming
    assert driver.snapshot()["unsupported_reason"]
    with pytest.raises(RuntimeError):
        driver.before_step(frame_offset=600)


def test_hook_evidence_ring_is_bounded_and_hooks_do_not_select_or_tick(probe):
    session, _, driver = make_driver(probe)
    for _ in range(100):
        session.fire("TradeCenter_SelectMon")
        session.fire("TradeCenter_SelectMon.playerMonMenu_HandleInput")
    snapshot = driver.snapshot()
    assert len(snapshot["recent_transitions"]) <= 32
    assert session.external_steps == 0
    snapshot["before_party"]["records"].clear()
    assert len(driver.snapshot()["before_party"]["records"]) == 3
    driver.close()
    assert session.hooks == {}


def exception_leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for child in error.exceptions for leaf in exception_leaves(child)]
    return [error]


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_constructor_registration_failure_rolls_back_only_owned_hooks(probe, cleanup_fails):
    session = FakeSession()
    locations = [session.symbols.bank_addr(name) for name in probe.OBSERVATION_SYMBOLS]
    failed_location = locations[3]
    existing = (object(), object())
    session.hooks[failed_location] = existing
    original_error = RuntimeError("registration collision")
    cleanup_error = RuntimeError("first cleanup failed")
    removals = []

    def register(bank, address, callback, context):
        if (bank, address) == failed_location:
            raise original_error
        session.register(bank, address, callback, context)

    def deregister(bank, address):
        location = bank, address
        removals.append(location)
        if cleanup_fails and location == locations[2]:
            raise cleanup_error
        session.deregister(bank, address)

    session._pyboy.hook_register = register
    session._pyboy.hook_deregister = deregister
    with pytest.raises((RuntimeError, BaseExceptionGroup)) as caught:
        probe.create_owner_driver(
            session=session,
            side="listen",
            options={
                "version": "blue_color",
                "outgoing_slot": 0,
                "checkpoint": "reciprocal-exchange",
            },
            peer_state=Readiness(),
        )
    assert removals == list(reversed(locations[:3]))
    assert session.hooks[failed_location] is existing
    assert failed_location not in removals
    leaves = exception_leaves(caught.value)
    assert original_error in leaves
    assert (cleanup_error in leaves) is cleanup_fails
    assert set(session.hooks) == (
        {failed_location, locations[2]} if cleanup_fails else {failed_location}
    )


@pytest.mark.parametrize(
    "phase,limit,button",
    [
        ("hidden_event", 6, "right"),
        ("cursor", 12, "down"),
        ("dialogue", 32, "a"),
    ],
)
def test_nonprogressing_input_attempts_have_fixed_fail_closed_quotas(probe, phase, limit, button):
    session, _, driver = make_driver(probe, slot=1)
    set_menu(session)
    if phase == "cursor":
        session.fire("TradeCenter_SelectMon.playerMonMenu_HandleInput")
        session.fire("HandleMenuInput")
    elif phase == "dialogue":
        session.fire("CableClubLeftGameboy")
    for index in range(limit):
        assert driver.before_step(frame_offset=index * 100)[0] == button
    with pytest.raises(RuntimeError, match="quota"):
        driver.before_step(frame_offset=limit * 100)
    assert driver.snapshot()["unsupported_reason"]
    assert session.external_steps == 0


def test_party_cursor_above_target_uses_up_before_watched_return_mask(probe):
    session, _, driver = make_driver(probe, slot=0)
    set_menu(session, wCurrentMenuItem=2)
    session.fire("TradeCenter_SelectMon.playerMonMenu_HandleInput")
    session.fire("HandleMenuInput")
    # Both home/window.asm implementations decrement before applying watched keys.
    assert driver.before_step(frame_offset=0)[0] == "up"
    assert driver.snapshot()["unsupported_reason"] is None


def test_native_hook_failure_is_sticky_and_raised_after_public_step(probe):
    session, _, driver = make_driver(probe)
    session.fire("TradeCenter_Trade")
    session.fire("TradeCenter_Trade.tradeConfirmed")
    session.fire("_AddEnemyMonToPlayerParty")
    session.set_bytes("wPartyCount", [0])
    # Native runtimes may swallow callback exceptions: nothing may escape here.
    session.fire("TryEvolvingMon")
    reason = driver.snapshot()["unsupported_reason"]
    assert reason and "count" in reason.lower()
    assert driver.snapshot()["copy_complete"] is False
    assert driver.objective_complete() is False
    with pytest.raises(RuntimeError):
        driver.after_step(call={"status": "completed", "actual_completed_frames": 1})
    assert driver.snapshot()["unsupported_reason"] == reason
    with pytest.raises(RuntimeError):
        driver.before_step(frame_offset=1)
    assert session.external_steps == 0
