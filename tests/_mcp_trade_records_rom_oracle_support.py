"""Acceptance-oracle helpers for the MCP stdio trade module.

Split from ``tests/test_mcp_trade_records_rom.py`` (#208) with no behavior change:
``assert_paired_exchange`` and the record/digest construction helpers that feed
both the oracle tests and the real-ROM rows moved here verbatim."""

from __future__ import annotations

import json

from tests._mcp_trade_records_rom_drivers_support import (
    _compacted_records,
    _compaction_expected_digests,
    _digests,
    _prelink,
    _records_from_payload,
)
from tests._mcp_trade_records_rom_support import (
    MULTI_MEMBER_PARTY_COUNT,
    TRADE_RECEIVED_EVENT,
    TradeRun,
    _assert_invoking_runtime,
    _child_runtime_mode,
    _fixture_sha1,
)


def _assert_record_shape(record, *, label):
    assert set(record) == {"slot", "digest", "record_size", "species", "level"}, (label, record)
    assert record["record_size"] == 44, (label, record)
    assert type(record["slot"]) is int, (label, record)
    assert type(record["species"]) is int and record["species"] not in (0, 0xFF), (label, record)
    assert type(record["level"]) is int and record["level"] > 0, (label, record)
    assert isinstance(record["digest"], str) and len(record["digest"]) == 64, (label, record)


def _assert_exact_exchange(
    before, after, *, outgoing_slot, incoming_digest, incoming_species, label, distinct=True
):
    """Assert the source-defined remove/compact/append exchange for one owner.

    Gen I removes the selected outgoing record, compacts survivors in order,
    then appends the received record at the final occupied slot.  The receiving
    slot is therefore ``len(after) - 1`` rather than the outgoing index, so an
    in-place equality or a fixed-slot comparison would be wrong for a
    multi-member party.

    ``distinct`` states whether the two owners offered byte-different records.
    When they did, the compacted result must differ from the starting party.
    When they offered the same admitted bytes (a same-family orientation whose
    ``battle`` row reproduces the ``ordinary`` record), the digest list cannot
    change; that case is proven by the caller's required ROM-owned append
    marker instead, so the change assertion is skipped here.
    """
    assert len(before) == len(after), (label, before, after)
    expected = _compaction_expected_digests(before, outgoing_slot, incoming_digest)
    assert [record["digest"] for record in after] == expected, (label, before, after, expected)
    receiving = len(after) - 1
    assert after[receiving]["slot"] == receiving, (label, after)
    assert after[receiving]["digest"] == incoming_digest, (label, after)
    assert after[receiving]["species"] == incoming_species, (label, after)
    if distinct:
        assert _digests(before) != _digests(after), (label, before, after)


async def _fixture_pair(pair, primary_asset, peer_asset, *, fixture):
    """Load the fixtures publicly and return both owners' starting records."""
    before = await _prelink(pair, primary_asset, peer_asset)
    primary_before = _records_from_payload(before[0])
    peer_before = _records_from_payload(before[1])
    assert len(primary_before) >= 1 and len(peer_before) >= 1, before
    for record in (*primary_before, *peer_before):
        _assert_record_shape(record, label=f"fixture-{fixture}")
    return primary_before, peer_before


def _fixture_mismatch_control(before, outgoing_slot, incoming_digest):
    """Return the digests a *skipped* copy would leave behind for one owner.

    This is the negative control for the paired exchange oracle: if the trade
    completed its menus without moving any record, the owner would still hold
    its own pre-trade digests.  A correct oracle must reject that list, so the
    exchange rows cannot pass on a driver that never copies -- including the
    byte-identical orientations, where the digest list alone cannot tell the two
    apart and the ROM-owned ``trade_received`` marker does the work.
    """
    del outgoing_slot, incoming_digest
    return _digests(before)


def _record_payload(records, *, source="party-records"):
    """Wrap an observed record list in the resource payload shape."""
    return {
        "source": source,
        "record_size": 44,
        "valid": True,
        "digest_algorithm": "sha256",
        "records": list(records),
    }


def _record(slot, digest, *, species=154, level=54):
    return {
        "slot": slot,
        "digest": digest,
        "record_size": 44,
        "species": species,
        "level": level,
    }


def assert_paired_exchange(
    run, *, primary_before, peer_before, primary_slot=0, peer_slot=0, label="pair"
):
    """Assert one completed trade produced the source-defined paired exchange.

    Pure oracle over observations.  It is driven directly by the regression rows
    below, which is how a "menus and completion milestones but no record copy"
    observation is *required* to fail instead of being accepted on an
    unchanged-bytes comparison.  Three independent requirements do the work:
    each owner must publish the ROM-owned ``trade_received`` append marker, the
    terminal digests must still equal the copy snapshot (so a party mutated
    after the copy is rejected), and the terminal digests must equal the
    source-defined remove/compact/append result.
    """
    assert len(run.received) == 2, (label, run.received)
    assert len(run.final_records) == 2, (label, run.final_records)
    assert len(run.copy_records) == 2, (label, run.copy_records)
    for owner, count in enumerate(run.received):
        assert count >= 1, (
            (
                f"{label}: owner {owner} never emitted the ROM's "
                f"{TRADE_RECEIVED_EVENT} append marker, so no record was copied"
            ),
            run.received,
        )
    for owner, payload in enumerate(run.final_records):
        assert payload["valid"] is True, (label, owner, payload)
        assert payload["record_size"] == 44, (label, owner, payload)
        assert len(_records_from_payload(payload)) == len(
            _records_from_payload(run.copy_records[owner])
        ), (label, owner, payload)
        for record in _records_from_payload(payload):
            _assert_record_shape(record, label=f"{label}-final-{owner}")
    primary_after = _records_from_payload(run.final_records[0])
    peer_after = _records_from_payload(run.final_records[1])
    copy_digests = [_digests(_records_from_payload(payload)) for payload in run.copy_records]
    final_digests = [_digests(primary_after), _digests(peer_after)]
    assert final_digests == copy_digests, (
        f"{label}: a party changed between the record copy and the terminal milestone",
        copy_digests,
        final_digests,
    )
    primary_incoming = peer_before[peer_slot]["digest"]
    peer_incoming = primary_before[primary_slot]["digest"]
    distinct = primary_incoming != peer_incoming
    _assert_exact_exchange(
        primary_before,
        primary_after,
        outgoing_slot=primary_slot,
        incoming_digest=primary_incoming,
        incoming_species=peer_before[peer_slot]["species"],
        label=f"{label}-primary",
        distinct=distinct,
    )
    _assert_exact_exchange(
        peer_before,
        peer_after,
        outgoing_slot=peer_slot,
        incoming_digest=peer_incoming,
        incoming_species=primary_before[primary_slot]["species"],
        label=f"{label}-peer",
        distinct=distinct,
    )
    if distinct:
        assert _digests(primary_before) != _digests(primary_after), (label, primary_before)
    else:
        # Two admitted byte-identical records: the row cannot show a digest
        # change and does not pretend to.  The copy is proven by the required
        # ROM-owned append marker above; the identity is stated explicitly so a
        # reader can see the row rests on that marker.
        assert _digests(primary_after) == _digests(primary_before), (label, primary_after)
        assert _digests(peer_after) == _digests(peer_before), (label, peer_after)
    return {
        "distinct_records": distinct,
        "primary_before": _digests(primary_before),
        "primary_after": _digests(primary_after),
        "peer_before": _digests(peer_before),
        "peer_after": _digests(peer_after),
        "trade_received": list(run.received),
    }


def _skipped_copy_run(primary_before, peer_before, *, received=(0, 0)):
    """Fabricate the observation a no-copy trade leaves behind.

    The menus *were* driven and the ROM did return to the trade-center selection
    loop, but neither party ever appended a received record, so both parties
    still hold exactly the fixture bytes they started with.  That is the shape
    the independent review's response-model control produced, and the oracle has
    to reject it.
    """
    return TradeRun(
        final_records=[
            _record_payload(primary_before),
            _record_payload(peer_before, source="peer-party-records"),
        ],
        copy_records=[
            _record_payload(primary_before),
            _record_payload(peer_before, source="peer-party-records"),
        ],
        copy_states=[],
        final_states=[],
        post_frames=0,
        evolution=[1, 1],
        offered={0: 0, 1: 0},
        back_outs=0,
        received=list(received),
    )


def _wrong_slot_run(
    primary_before,
    peer_before,
    *,
    copied_primary_slot,
    copied_peer_slot,
    claimed_offered,
    received=(1, 1),
):
    """Fabricate the observation a driver that copied the *wrong* slots leaves.

    ``copied_*_slot`` are the slots the response actually moved; the run claims
    ``claimed_offered``.  This expresses the independent review's diagnostic
    mutation -- copying slots ``(0, 1)`` while claiming ``(2, 0)`` -- as an
    observation, so the acceptance oracle can be *required* to reject it.  With
    six pairwise-distinct records the wrong-slot compaction produces a different
    digest list than the claimed-slot compaction, so the copy snapshot and the
    exact-exchange assertions no longer both hold.
    """
    primary_after = _compacted_records(
        primary_before, copied_primary_slot, peer_before[copied_peer_slot]
    )
    peer_after = _compacted_records(
        peer_before, copied_peer_slot, primary_before[copied_primary_slot]
    )
    return TradeRun(
        final_records=[
            _record_payload(primary_after),
            _record_payload(peer_after, source="peer-party-records"),
        ],
        copy_records=[
            _record_payload(primary_after),
            _record_payload(peer_after, source="peer-party-records"),
        ],
        copy_states=[],
        final_states=[],
        post_frames=0,
        evolution=[1, 1],
        offered=dict(claimed_offered),
        back_outs=0,
        received=list(received),
    )


def _distinct_party(prefix):
    """Six same-species records whose 44-byte digests are pairwise distinct."""
    return [
        _record(slot, (prefix + str(slot)).ljust(64, "0"))
        for slot in range(MULTI_MEMBER_PARTY_COUNT)
    ]


def _survivor_run(primary_after, peer_before, primary_before, *, received=(1, 1)):
    """Wrap one mutated primary digest list in an otherwise-complete run."""
    peer_after = _compacted_records(peer_before, 0, primary_before[2])
    primary_after = [{**record, "slot": index} for index, record in enumerate(primary_after)]
    return TradeRun(
        final_records=[
            _record_payload(primary_after),
            _record_payload(peer_after, source="peer-party-records"),
        ],
        copy_records=[
            _record_payload(primary_after),
            _record_payload(peer_after, source="peer-party-records"),
        ],
        copy_states=[],
        final_states=[],
        post_frames=0,
        evolution=[1, 1],
        offered={0: 2, 1: 0},
        back_outs=0,
        received=list(received),
    )


def _emit_row(runtime_mode, payload):
    """Emit one acceptance row: invoking runtime origins, then the payload.

    The origins line is what the evidence builder records as the resolved
    runtime identity of the cell, so a row cannot claim a runtime it did not
    import.
    """
    origins = _assert_invoking_runtime(runtime_mode)
    print(
        f"MCP_TRADE_RECORDS_CHILD_ORIGINS {runtime_mode} {origins['pyboy']} {origins['serial']}",
        flush=True,
    )
    print("MCP_TRADE_RECORDS_ROM " + json.dumps(payload, sort_keys=True), flush=True)


def _base_row_payload(
    version, peer_version, primary_asset, peer_asset, primary_fixture, peer_fixture
):
    return {
        "version": version,
        "peer_version": peer_version,
        "runtime_mode": _child_runtime_mode(),
        "primary_fixture": primary_fixture,
        "peer_fixture": peer_fixture,
        "fixture_sha1": _fixture_sha1(primary_asset),
        "peer_fixture_sha1": _fixture_sha1(peer_asset),
    }
