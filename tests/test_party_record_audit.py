"""ROM-free tests for the read-only party-record digest/export audit.

These tests build synthetic ``wPartyMons`` memory only.  They prove the
source-defined 44-byte record identity, digest stability, the exact-swap
audit (including two same-species members), explicit unknown/failure results,
and the observational ``pokered://party-records`` resource shape.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading

import mcp.types as mcp_types
import pytest

from pokered_harness.mcp_server import LinkState, build_server, read_resource
from pokered_harness.session import Session, SessionClosedError, SessionCloseTimeout
from pokered_harness.state.party import (
    PARTY_STRUCT_SIZE,
    PartyRecord,
    PartyRecords,
    audit_exact_party_exchange,
    parse_party_records,
)
from pokered_harness.symbols.loader import SymbolTable, load_sym_text
from tests.conftest import CANONICAL_SYM, DictMemory
from tests.fakes import FakePyBoy


def _symbols():
    return load_sym_text(CANONICAL_SYM)


def _record(species: int, serial: int, *, level: int = 5) -> bytes:
    raw = bytearray(((serial * 7) + index) % 256 for index in range(PARTY_STRUCT_SIZE))
    raw[0] = species
    raw[33] = level
    return bytes(raw)


def _load_party(mem, symbols, records: list[bytes]) -> None:
    mem[symbols.addr_of("wPartyCount")] = len(records)
    base = symbols.addr_of("wPartyMons")
    for slot, record in enumerate(records):
        for offset, value in enumerate(record):
            mem[base + slot * PARTY_STRUCT_SIZE + offset] = value


def _view(records: list[bytes], symbols=None) -> PartyRecords:
    mem = DictMemory()
    symbols = symbols or _symbols()
    _load_party(mem, symbols, records)
    return parse_party_records(mem, symbols)


def _session() -> Session:
    mem = DictMemory()
    pb = FakePyBoy(mem)
    session = Session(pyboy=pb, symbols=_symbols())
    return session


# -- raw record extraction and digest stability ------------------------------


def test_parse_party_records_exact_44_byte_identity_and_stable_digest():
    symbols = _symbols()
    records = [_record(0x99, 1), _record(0x25, 2)]
    view = _view(records, symbols)

    assert view.valid is True
    assert view.count == 2
    assert [record.raw for record in view.records] == records
    assert all(record.size == PARTY_STRUCT_SIZE for record in view.records)
    assert view.digests() == (
        hashlib.sha256(records[0]).hexdigest(),
        hashlib.sha256(records[1]).hexdigest(),
    )
    assert view.records[0].species == 0x99
    assert view.records[0].level == 5


def test_digest_is_stable_across_independent_reads():
    symbols = _symbols()
    records = [_record(0x99, 1)]
    first = _view(records, symbols)
    second = _view(records, symbols)
    assert first.digests() == second.digests()
    assert first.records[0].raw == second.records[0].raw


def test_record_at_is_bounded():
    view = _view([_record(0x99, 1), _record(0x25, 2)])
    assert view.record_at(0) is view.records[0]
    assert view.record_at(1) is view.records[1]
    assert view.record_at(-1) is None
    assert view.record_at(2) is None


def test_parse_party_records_clamps_and_flags_corrupt_count():
    symbols = _symbols()
    mem = DictMemory()
    mem[symbols.addr_of("wPartyCount")] = 200
    view = parse_party_records(mem, symbols)
    assert view.count == 6
    assert view.count_valid is False
    assert view.valid is False
    assert len(view.records) == 6


# -- exact exchange audit ----------------------------------------------------


def test_audit_exact_swap_between_intended_slots():
    a0 = _record(0x99, 1)
    a1 = _record(0x01, 11)
    b0 = _record(0x25, 2)
    b1 = _record(0x02, 12)
    audit = audit_exact_party_exchange(
        owner_a_before=_view([a0, a1]),
        owner_a_after=_view([a1, b0]),
        owner_b_before=_view([b0, b1]),
        owner_b_after=_view([b1, a0]),
        slot_a=0,
        slot_b=0,
    )
    assert audit.valid is True
    assert audit.status == "exact"
    assert audit.check("record_size_44") is True
    assert audit.check("intended_slots_swapped") is True
    assert audit.check("unrelated_records_unchanged") is True


def test_audit_rejects_changed_unrelated_record():
    a0 = _record(0x99, 1)
    a1 = _record(0x01, 11)
    b0 = _record(0x25, 2)
    b1 = _record(0x02, 12)
    audit = audit_exact_party_exchange(
        owner_a_before=_view([a0, a1]),
        owner_a_after=_view([_record(0x77, 99), b0]),
        owner_b_before=_view([b0, b1]),
        owner_b_after=_view([b1, a0]),
        slot_a=0,
        slot_b=0,
    )
    assert audit.valid is False
    assert audit.status == "mismatch"
    assert audit.check("intended_slots_swapped") is True
    assert audit.check("unrelated_records_unchanged") is False


def test_two_same_species_members_distinguished_by_slot():
    a0 = _record(0x99, 1)
    b0 = _record(0x99, 2)
    assert a0[0] == b0[0]
    assert a0 != b0
    audit = audit_exact_party_exchange(
        owner_a_before=_view([a0]),
        owner_a_after=_view([b0]),
        owner_b_before=_view([b0]),
        owner_b_after=_view([a0]),
        slot_a=0,
        slot_b=0,
    )
    assert audit.valid is True
    assert audit.check("same_species_distinguished") is True


@pytest.mark.parametrize("slot_a,slot_b", [(0, 0), (1, 0), (2, 1)])
def test_trade_compacts_unequal_parties_and_appends_same_species_records(slot_a, slot_b):
    a = [_record(0x99, index) for index in (1, 2, 3)]
    b = [_record(0x99, index) for index in (4, 5)]
    audit = audit_exact_party_exchange(
        owner_a_before=_view(a),
        owner_a_after=_view(a[:slot_a] + a[slot_a + 1:] + [b[slot_b]]),
        owner_b_before=_view(b),
        owner_b_after=_view(b[:slot_b] + b[slot_b + 1:] + [a[slot_a]]),
        slot_a=slot_a,
        slot_b=slot_b,
    )
    assert audit.is_exact
    assert audit.check("same_species_distinguished") is True


def test_trade_rejects_in_place_replacement_of_nonfinal_outgoing_slot():
    a = [_record(0x99, 1), _record(0x99, 2)]
    b = [_record(0x99, 3), _record(0x99, 4)]
    audit = audit_exact_party_exchange(
        owner_a_before=_view(a),
        owner_a_after=_view([b[0], a[1]]),
        owner_b_before=_view(b),
        owner_b_after=_view([a[0], b[1]]),
        slot_a=0,
        slot_b=0,
    )
    assert audit.valid is False
    assert audit.check("intended_slots_swapped") is False
    assert audit.check("unrelated_records_unchanged") is False


@pytest.mark.parametrize("survivors", [(2, 1), (1,), (1, 2, 2)])
def test_trade_rejects_reordered_missing_or_extra_survivors(survivors):
    a = [_record(0x99, index) for index in (1, 2, 3)]
    b = [_record(0x25, 4)]
    audit = audit_exact_party_exchange(
        owner_a_before=_view(a),
        owner_a_after=_view([a[index] for index in survivors] + b),
        owner_b_before=_view(b),
        owner_b_after=_view([a[0]]),
        slot_a=0,
        slot_b=0,
    )
    assert audit.valid is False
    assert audit.check("intended_slots_swapped") is True
    assert audit.check("unrelated_records_unchanged") is False


def test_audit_rejects_species_only_match():
    a0 = _record(0x99, 1)
    b0 = _record(0x99, 2)
    wrong = bytearray(b0)
    wrong[10] ^= 0xFF  # correct species, corrupt interior bytes
    audit = audit_exact_party_exchange(
        owner_a_before=_view([a0]),
        owner_a_after=_view([bytes(wrong)]),
        owner_b_before=_view([b0]),
        owner_b_after=_view([a0]),
        slot_a=0,
        slot_b=0,
    )
    assert wrong[0] == b0[0]
    assert audit.valid is False
    assert audit.status == "mismatch"
    assert audit.check("intended_slots_swapped") is False
    assert audit.check("same_species_distinguished") is False


def test_audit_rejects_wrong_slot_copy():
    a0 = _record(0x01, 1)
    a1 = _record(0x02, 2)
    b0 = _record(0x03, 3)
    audit = audit_exact_party_exchange(
        owner_a_before=_view([a0, a1]),
        owner_a_after=_view([a1, a1]),  # copied its own other slot
        owner_b_before=_view([b0]),
        owner_b_after=_view([a0]),
        slot_a=0,
        slot_b=0,
    )
    assert audit.valid is False
    assert audit.status == "mismatch"
    assert audit.check("intended_slots_swapped") is False


def test_audit_rejects_record_that_is_not_44_bytes():
    def mismatched(raw: bytes) -> PartyRecords:
        return PartyRecords(
            records=(
                PartyRecord(
                    slot=0,
                    raw=raw,
                    digest=hashlib.sha256(raw).hexdigest(),
                    species=raw[0] if raw else 0,
                    level=raw[33] if len(raw) > 33 else 0,
                ),
            ),
            count=1,
            count_raw=1,
            count_valid=True,
            valid=True,
        )

    short = _record(0x99, 1)[:43]
    audit = audit_exact_party_exchange(
        owner_a_before=mismatched(short),
        owner_a_after=mismatched(_record(0x25, 2)),
        owner_b_before=mismatched(_record(0x25, 2)),
        owner_b_after=mismatched(short),
        slot_a=0,
        slot_b=0,
    )
    assert audit.valid is False
    assert audit.status == "mismatch"
    assert audit.check("record_size_44") is False


# -- explicit unknown and failure -------------------------------------------


def test_absent_party_mons_symbol_is_unknown():
    symbols = load_sym_text("00:C001 wPartyCount\n")
    mem = DictMemory({0xC001: 1})
    view = parse_party_records(mem, symbols)
    assert view.valid is None
    assert view.is_unknown is True
    assert view.missing_symbols == ("wPartyMons",)
    assert view.records == ()

    audit = audit_exact_party_exchange(
        owner_a_before=view,
        owner_a_after=view,
        owner_b_before=view,
        owner_b_after=view,
        slot_a=0,
        slot_b=0,
    )
    assert audit.valid is None
    assert audit.status == "unknown"
    assert audit.is_unknown is True


def test_known_count_marks_out_of_range_slots_without_record_bytes():
    symbols = load_sym_text("00:C001 wPartyCount\n")
    mem = DictMemory({0xC001: 1})
    view = parse_party_records(mem, symbols)
    assert view.valid is None  # record bytes unavailable, but the count is known
    assert view.count == 1 and view.count_valid is True

    for slot in (1, 6, 999):
        audit = audit_exact_party_exchange(
            owner_a_before=view,
            owner_a_after=view,
            owner_b_before=view,
            owner_b_after=view,
            slot_a=slot,
            slot_b=0,
        )
        assert audit.valid is False, slot
        assert audit.status == "out_of_range", slot

    # A slot that could still be valid remains explicitly unknown rather than
    # being reported as a failure.
    audit = audit_exact_party_exchange(
        owner_a_before=view,
        owner_a_after=view,
        owner_b_before=view,
        owner_b_after=view,
        slot_a=0,
        slot_b=0,
    )
    assert audit.valid is None
    assert audit.status == "unknown"


def test_absent_party_count_symbol_is_unknown():
    symbols = load_sym_text("00:C100 wPartyMons\n")
    view = parse_party_records(DictMemory(), symbols)
    assert view.valid is None
    assert view.missing_symbols == ("wPartyCount",)


def test_out_of_range_slot_is_explicit_failure():
    records = [_record(0x99, 1)]
    audit = audit_exact_party_exchange(
        owner_a_before=_view(records),
        owner_a_after=_view(records),
        owner_b_before=_view(records),
        owner_b_after=_view(records),
        slot_a=1,
        slot_b=0,
    )
    assert audit.valid is False
    assert audit.status == "out_of_range"


# -- MCP resource shape and observational behavior ---------------------------


def test_party_records_resource_json_shape():
    mem = DictMemory()
    pb = FakePyBoy(mem)
    symbols = _symbols()
    session = Session(pyboy=pb, symbols=symbols)
    record = _record(0x99, 1)
    _load_party(mem, symbols, [record])

    body = read_resource(session, "pokered://party-records")
    payload = json.loads(body)

    assert set(payload) == {
        "source",
        "digest_algorithm",
        "record_size",
        "count",
        "count_raw",
        "count_valid",
        "valid",
        "missing_symbols",
        "records",
    }
    assert payload["source"] == "party-records"
    assert payload["record_size"] == PARTY_STRUCT_SIZE
    assert payload["digest_algorithm"] == "sha256"
    assert payload["count"] == 1
    assert payload["count_raw"] == 1
    assert payload["count_valid"] is True
    assert payload["valid"] is True
    assert payload["missing_symbols"] == []
    assert payload["records"] == [
        {
            "slot": 0,
            "digest": hashlib.sha256(record).hexdigest(),
            "record_size": 44,
            "species": 0x99,
            "level": 5,
        }
    ]
    # Never expose raw record bytes or machine-local absolute paths.
    assert "raw" not in payload["records"][0]
    assert set(payload["records"][0]) == {"slot", "digest", "record_size", "species", "level"}
    assert "/home/" not in body


def test_party_records_resource_reports_unknown_without_fabricating():
    mem = DictMemory()
    pb = FakePyBoy(mem)
    symbols = load_sym_text("00:C001 wPartyCount\n")
    session = Session(pyboy=pb, symbols=symbols)
    mem[0xC001] = 1

    payload = json.loads(read_resource(session, "pokered://party-records"))
    assert payload["valid"] is None
    assert payload["records"] == []
    assert payload["missing_symbols"] == ["wPartyMons"]


def test_party_records_read_is_observational_only():
    mem = DictMemory()
    pb = FakePyBoy(mem)
    symbols = _symbols()
    session = Session(pyboy=pb, symbols=symbols)
    _load_party(mem, symbols, [_record(0x99, 1), _record(0x25, 2)])
    base = symbols.addr_of("wPartyMons")
    span = range(base, base + 2 * PARTY_STRUCT_SIZE)
    before = {address: mem[address] for address in span}

    read_resource(session, "pokered://party-records")

    assert pb.tick_calls == []
    assert pb.button_calls == []
    assert pb.button_press_calls == []
    assert pb.button_release_calls == []
    assert {address: mem[address] for address in span} == before


def test_party_records_resource_is_advertised():
    session = _session()
    server = build_server(session)
    handler = server.request_handlers[mcp_types.ListResourcesRequest]
    response = asyncio.run(handler(mcp_types.ListResourcesRequest()))

    names = {resource.name for resource in response.root.resources}
    uris = {str(resource.uri) for resource in response.root.resources}
    assert "Party Records" in names
    assert "pokered://party-records" in uris


# -- peer owner observation, freshness, and ownership coverage ---------------


def _session_with_memory(
    records: list[bytes] | None = None,
    symbols: SymbolTable | None = None,
) -> tuple[Session, DictMemory, FakePyBoy, SymbolTable]:
    symbols = symbols or _symbols()
    mem = DictMemory()
    pyboy = FakePyBoy(mem)
    session = Session(pyboy=pyboy, symbols=symbols)
    if records is not None:
        _load_party(mem, symbols, records)
    return session, mem, pyboy, symbols


def _list_resources(server) -> set[str]:
    handler = server.request_handlers[mcp_types.ListResourcesRequest]
    response = asyncio.run(handler(mcp_types.ListResourcesRequest()))
    return {str(resource.uri) for resource in response.root.resources}


class _ImmediateTimedOwner:
    """Minimal persistent-owner double that runs submissions synchronously."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.submissions = 0
        self.owned_sessions: list[Session] = []

    def submit(self, operation, *args, **kwargs):
        owner = self
        self.submissions += 1

        class _Result:
            def result(self):
                owner.owned_sessions.append(owner.session)
                return operation(owner.session, *args, **kwargs)

        return _Result()


def test_party_records_resource_reread_reflects_changed_memory():
    session, mem, _pyboy, symbols = _session_with_memory([_record(0x99, 1)])
    first = json.loads(read_resource(session, "pokered://party-records"))
    assert first["count"] == 1
    assert first["records"][0]["species"] == 0x99

    replacement = _record(0x25, 2)
    base = symbols.addr_of("wPartyMons")
    for offset, value in enumerate(replacement):
        mem[base + offset] = value

    second = json.loads(read_resource(session, "pokered://party-records"))
    assert second["records"][0]["species"] == 0x25
    assert second["records"][0]["digest"] == hashlib.sha256(replacement).hexdigest()
    assert second["records"][0]["digest"] != first["records"][0]["digest"]


def test_party_records_resource_fails_closed_after_close():
    session, _mem, pyboy, _symbols = _session_with_memory([_record(0x99, 1)])
    session.close()
    assert session.closed and pyboy.stopped

    with pytest.raises(SessionClosedError):
        read_resource(session, "pokered://party-records")


def test_party_records_read_serializes_with_competing_owner():
    session, _mem, _pyboy, _symbols = _session_with_memory([_record(0x99, 1)])
    entered, release = threading.Event(), threading.Event()

    def hold():
        with session.locked(timeout_s=1.0):
            entered.set()
            assert release.wait(timeout=2.0)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert entered.wait(timeout=1.0)

    bodies: list[str] = []
    errors: list[BaseException] = []

    def read():
        try:
            bodies.append(read_resource(session, "pokered://party-records"))
        except BaseException as exc:  # noqa: BLE001 - collect worker failures
            errors.append(exc)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    # A competing owner scope must keep the read out until it releases.
    assert not reader.join(timeout=0.2), "read did not serialize behind the owner lock"
    assert bodies == [] and errors == []

    release.set()
    holder.join(timeout=2.0)
    reader.join(timeout=2.0)
    assert not reader.is_alive()
    assert errors == []
    assert json.loads(bodies[0])["count"] == 1


def test_party_records_read_fails_closed_when_closed_during_contention():
    session, _mem, _pyboy, _symbols = _session_with_memory([_record(0x99, 1)])
    entered, release = threading.Event(), threading.Event()

    def hold():
        with session.locked(timeout_s=1.0):
            entered.set()
            assert release.wait(timeout=2.0)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert entered.wait(timeout=1.0)

    errors: list[BaseException] = []

    def read():
        try:
            read_resource(session, "pokered://party-records")
        except BaseException as exc:  # noqa: BLE001 - collect worker failures
            errors.append(exc)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    assert not reader.join(timeout=0.2)

    # Publish the closed lifecycle while the reader waits on the owner lock.
    with pytest.raises(SessionCloseTimeout):
        session.close(timeout_s=0.01)
    assert session.closed

    release.set()
    holder.join(timeout=2.0)
    reader.join(timeout=2.0)
    assert not reader.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], SessionClosedError)


def test_distinct_owners_party_records_are_not_conflated():
    owner_a, _mem_a, _pyboy_a, symbols = _session_with_memory([_record(0x99, 1)])
    owner_b, _mem_b, _pyboy_b, _symbols = _session_with_memory([_record(0x25, 2)], symbols)

    payload_a = json.loads(read_resource(owner_a, "pokered://party-records"))
    payload_b = json.loads(read_resource(owner_b, "pokered://party-records"))

    assert payload_a["records"][0]["species"] == 0x99
    assert payload_b["records"][0]["species"] == 0x25
    assert payload_a["records"][0]["digest"] != payload_b["records"][0]["digest"]


def test_peer_party_records_resource_reads_peer_owner():
    primary, _mem, _pyboy, symbols = _session_with_memory([_record(0x99, 1)])
    peer_record = _record(0x25, 2)
    peer, _peer_mem, _peer_pyboy, _peer_symbols = _session_with_memory([peer_record], symbols)
    link = LinkState(peer_session=peer)

    primary_payload = json.loads(read_resource(primary, "pokered://party-records", link=link))
    peer_payload = json.loads(read_resource(primary, "pokered://peer-party-records", link=link))

    assert primary_payload["source"] == "party-records"
    assert primary_payload["records"][0]["species"] == 0x99
    assert peer_payload["source"] == "peer-party-records"
    assert peer_payload["records"][0]["species"] == 0x25
    assert peer_payload["records"][0]["digest"] == hashlib.sha256(peer_record).hexdigest()
    assert peer_payload["records"][0]["digest"] != primary_payload["records"][0]["digest"]


def test_peer_party_records_read_is_observational_only():
    primary, _mem, _pyboy, symbols = _session_with_memory([_record(0x99, 1)])
    peer_record = _record(0x25, 2)
    peer, peer_mem, peer_pyboy, _peer_symbols = _session_with_memory([peer_record], symbols)
    link = LinkState(peer_session=peer)
    base = symbols.addr_of("wPartyMons")
    span = range(base, base + PARTY_STRUCT_SIZE)
    before = {address: peer_mem[address] for address in span}

    read_resource(primary, "pokered://peer-party-records", link=link)

    assert peer_pyboy.tick_calls == []
    assert peer_pyboy.button_calls == []
    assert peer_pyboy.button_press_calls == []
    assert peer_pyboy.button_release_calls == []
    assert {address: peer_mem[address] for address in span} == before


def test_peer_party_records_resource_errors_when_not_configured():
    session, _mem, _pyboy, _symbols = _session_with_memory([_record(0x99, 1)])
    link = LinkState(peer_session=None)

    with pytest.raises(ValueError, match="peer session not configured"):
        read_resource(session, "pokered://peer-party-records", link=link)


def test_peer_party_records_resource_is_advertised_only_with_peer():
    primary, _mem, _pyboy, symbols = _session_with_memory([_record(0x99, 1)])
    assert "pokered://peer-party-records" not in _list_resources(build_server(primary))

    peer, _peer_mem, _peer_pyboy, _peer_symbols = _session_with_memory([], symbols)
    server = build_server(primary, peer_session=peer)
    handler = server.request_handlers[mcp_types.ListResourcesRequest]
    response = asyncio.run(handler(mcp_types.ListResourcesRequest()))
    names = {resource.name for resource in response.root.resources}
    uris = {str(resource.uri) for resource in response.root.resources}
    assert "Peer Party Records" in names
    assert "pokered://peer-party-records" in uris


def test_party_records_resource_routes_through_timed_owner():
    session, _mem, _pyboy, _symbols = _session_with_memory([_record(0x99, 1)])
    owner = _ImmediateTimedOwner(session)

    payload = json.loads(read_resource(session, "pokered://party-records", timed_owner=owner))

    assert payload["source"] == "party-records"
    assert payload["count"] == 1
    assert owner.submissions == 1
    assert owner.owned_sessions == [session]


def test_peer_party_records_rejected_in_timed_owner_mode():
    session, _mem, _pyboy, _symbols = _session_with_memory([_record(0x99, 1)])
    owner = _ImmediateTimedOwner(session)

    with pytest.raises(ValueError, match="no local peer"):
        read_resource(session, "pokered://peer-party-records", timed_owner=owner)
    assert owner.submissions == 0
