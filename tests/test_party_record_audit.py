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

import mcp.types as mcp_types

from pokered_harness.mcp_server import build_server, read_resource
from pokered_harness.session import Session
from pokered_harness.state.party import (
    PARTY_STRUCT_SIZE,
    PartyRecord,
    PartyRecords,
    audit_exact_party_exchange,
    parse_party_records,
)
from pokered_harness.symbols.loader import load_sym_text
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
        owner_a_after=_view([b0, a1]),
        owner_b_before=_view([b0, b1]),
        owner_b_after=_view([a0, b1]),
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
        owner_a_after=_view([b0, _record(0x77, 99)]),
        owner_b_before=_view([b0, b1]),
        owner_b_after=_view([a0, b1]),
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
