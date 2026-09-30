"""Real-ROM MCP stdio trade that verifies exact paired party-record exchange.

Finding 1 of the independent PR #118 review requires public stdio gameplay
evidence: a caller must enter the Trade Center through the public MCP server,
choose both owners' party members, complete the real exchange, and prove the
44-byte record swap through read-only public resources.  The pre-existing
strict trade tests drive the emulators through private hooks; this file drives
the *public* ``press``/``link_step``/``link_peer_press`` tool surface -- and, for
the two-process rows, the public ``link_listen``/``link_connect`` pair -- and
reads only ``pokered://game-state``/``pokered://peer-game-state``,
``pokered://party-records``/``pokered://peer-party-records``, and the event-log
resources.

Coverage is complete rather than illustrative: the nine ordered canonical
*local* orientations, the nine ordered canonical *TCP* orientations, the two
multi-member sender/receiver slot rows, cancelling before commitment, EOF during
setup, EOF during a committed exchange, and EOF after commitment.  Every row
runs under either runtime; the client asserts which PyBoy modules the invoking
interpreter imports before launching the server with ``sys.executable``, so a
shadowed ``PYTHONPATH`` fails loudly instead of silently substituting the other
runtime.

Two admitted-immutable-fixture limits are handled explicitly rather than hidden.
The registry (``release-evidence/fixture-manifest.json``) admits, per family, one
``verified`` single-member ``ordinary`` party, one ``verified`` ``battle``
party, and one ``verified`` six-member ``slots`` party whose six 44-byte records
are pairwise distinct (they differ only in the two-byte OT id at record offset
12).  The ``battle`` row of Red and of Yellow reproduces that family's ordinary
44-byte record byte-for-byte, so a Red/Red or Yellow/Yellow row cannot offer two
different records from those two rows.  Those rows therefore rest on a ROM-owned
marker instead of a byte change: the produced server registers
``_AddEnemyMonToPlayerParty`` -- the Trade Center routine that appends the
received record to the player's own party -- as the ``trade_received`` event, and
*every* row, identical or distinct, requires that event on each owner before the
exchange is accepted.  A party that never copied a record never runs the routine,
so a skipped copy is rejected instead of passing on an unchanged-bytes
comparison.  Blue is the one family whose admitted ``battle`` row holds a
different record from its ``ordinary`` row; the Blue/Blue row pairs them so a
same-family row also carries a byte-exact exchange proof.  The nonzero-slot rows
are driven from the pairwise-distinct ``slots`` parties, so a copy that ignores
the selected cursor produces a different digest list and the acceptance oracle
rejects it instead of accepting a wrong-slot mutation on uniform bytes.

The cross-family rows pair two distinct canonical games, so the two source
records are *different bytes of the same species*.  That makes the digest swap a
byte-exact identity proof rather than a species match: Red's lead and Blue's lead
are both species 154 with independently pinned records, while Yellow's lead is a
different species.

The trade-centre navigator reads only public ``menu``/``overworld`` fields and
derives every button from the mask and bounds the ROM itself published
(engine/link/cable_club.asm), so no private hook, RAM read, or RAM write is used
by the acceptance client.  Nothing here writes RAM, bypasses a ROM hash, installs
a hook, or manufactures a fixture; the peer session is started through the
entry point's documented ``POKERED_PEER_STATE_PATH``/``POKERED_PEER_STATE_SHA1``
launch contract.

The ordinary fixture is a single-member party (``party_count == 1``), where the
ROM's ``.playerMonMenu`` bounds ``wMaxMenuItem`` by ``wPartyCount`` and no
second slot can be selected.  The nonzero sender/receiver *slot* rows are
therefore driven from the registered ``cable_club-slots.state`` fixture: the
manifest pins that row as ``kind: slots``, and its observable state is the same
pre-connection Cable Club attendant tile as the ordinary fixture
(``map_id == 64``, ``(11, 3)``, ``wIsInBattle == 0``) carrying a six-member
party whose six independently pinned 44-byte records are pairwise distinct.
Using an admitted immutable fixture keeps the row real; no party is fabricated
at runtime.  Those rows assert both the cursor slot the client actually selected
(observed on the ROM menu before it confirmed) and the ROM's remove/compact/append
receiving slot, and because the six admitted records differ a copy that ignores
the selected cursor is rejected rather than passing on uniform bytes.

``POKERED_SKIP_SHA1`` is rejected: real-ROM evidence must validate the pinned
ROM/SYM and fixture bytes.  Missing assets skip so partial BYO-ROM checkouts
still collect the file.

This module is the collection facade for the ``#208`` split.  The shared
helpers and drivers now live verbatim in the sibling non-collected
``tests/_mcp_trade_records_rom_{support,drivers_support,oracle_support}``
modules, and the ROM-free oracle and synthetic-TCP cases live in
``tests/_mcp_trade_records_rom_tests_support``; the facade re-exports the
support surface and every moved test so each
``tests/test_mcp_trade_records_rom.py::<test>`` node ID, its tier
classification, and the collected item count are preserved exactly.
"""

# ruff: noqa: F401

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import socket
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import NamedTuple

import pytest

from scripts.probe_timed_rom_pair import resolve_assets
from tests._mcp_trade_records_rom_drivers_support import (
    _advance_until,
    _all_records,
    _all_states,
    _approach_action,
    _await_party_menu,
    _back_out_of_submenu,
    _compacted_records,
    _compaction_expected_digests,
    _digests,
    _drive_to_commitment,
    _drive_to_link_menu,
    _drive_trade,
    _drive_trade_completion,
    _enter_stats_trade_submenu,
    _enter_trade_flow,
    _link_menu_ready,
    _link_step,
    _log,
    _mash_until,
    _menu,
    _menu_ready,
    _overworld,
    _owner_trade_action,
    _peer_press,
    _player_mon_menu_live,
    _prelink,
    _press,
    _press_actions,
    _press_both,
    _records_from_payload,
    _select_trade_center,
    _selection_loop_ready,
    _settle_trade_center,
    _trade_action,
    _trade_center_fingerprint,
    _trade_received_counts,
    _walk_to_trade_trigger,
)
from tests._mcp_trade_records_rom_oracle_support import (
    _assert_exact_exchange,
    _assert_record_shape,
    _base_row_payload,
    _distinct_party,
    _emit_row,
    _fixture_mismatch_control,
    _fixture_pair,
    _record,
    _record_payload,
    _skipped_copy_run,
    _survivor_run,
    _wrong_slot_run,
    assert_paired_exchange,
)
from tests._mcp_trade_records_rom_support import (
    _MCP_ERROR_CODE,
    _TCP_DRIVER_TOOL_NAMES,
    BATTLE_FIXTURE,
    CABLE_CLUB_MAP_ID,
    CONFIRM_MENU_KEYS,
    EVOLUTION_EVENT,
    FAMILIES,
    LINK_MENU_BUDGET,
    LINK_MENU_BURST_ATTEMPTS,
    LINK_MENU_MAX_ITEMS,
    LINK_MENU_QUIET_FRAMES,
    LINK_UP_STATUS_POLL_INTERVAL,
    LINK_UP_STATUS_TIMEOUT,
    MULTI_MEMBER_FIXTURE,
    MULTI_MEMBER_ORIENTATIONS,
    MULTI_MEMBER_PARTY_COUNT,
    ORDINARY_FIXTURE,
    ORIENTATIONS,
    PARTY_MENU_KEYS,
    POST_TRADE_BUDGET,
    RECEPTIONIST_WALK_FRAMES,
    ROOT,
    STATS_MENU_KEYS,
    STEP_CHUNK,
    TRADE_BUDGET,
    TRADE_CENTER_MAP_ID,
    TRADE_MENU_KEYS,
    TRADE_RECEIVED_EVENT,
    WALK_ATTEMPTS,
    WALK_SETTLE_BUDGET,
    WALK_SETTLE_SLICE,
    WALK_STABLE_CHECKS,
    WALK_STEP_FRAMES,
    WARP_BUDGET,
    LocalPair,
    TcpPair,
    TradeRun,
    _assert_invoking_runtime,
    _child_runtime_mode,
    _fixture_sha1,
    _latched_error_code,
    _launch_server,
    _multi_member_id,
    _orientation_fixtures,
    _orientation_id,
    _read_resource,
    _require_assets,
    _server_env,
    _server_environment,
    _stage_assets,
    _stdio_server,
)
from tests._mcp_trade_records_rom_tests_support import (
    _patch_synthetic_tcp_launch,
    _synthetic_assets,
    _SyntheticTcpClient,
    _tcp_pair_servers,
    test_paired_exchange_oracle_accepts_identical_completion,
    test_paired_exchange_oracle_accepts_the_required_slot_copy,
    test_paired_exchange_oracle_rejects_distinct_skipped_copy,
    test_paired_exchange_oracle_rejects_faked_marker_without_exchange,
    test_paired_exchange_oracle_rejects_identical_skipped_copy,
    test_paired_exchange_oracle_rejects_late_corruption,
    test_paired_exchange_oracle_rejects_reordered_or_substituted_survivors,
    test_paired_exchange_oracle_rejects_wrong_slot_copy,
    test_tcp_driver_invokes_only_tools_its_own_server_advertises,
    test_tcp_pair_servers_attempt_every_cleanup_when_the_body_succeeds,
    test_tcp_pair_servers_keep_the_body_failure_and_note_every_cleanup_failure,
    test_tcp_surface_requirement_rejects_a_process_missing_an_invoked_tool,
)
from tests._rom_assets import fixture_path, rom_path, sym_path
from tests.test_mcp_timed_rom import PIPE_CAP, RomClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "peer_version"),
    ORIENTATIONS,
    ids=[_orientation_id(orientation) for orientation in ORIENTATIONS],
)
async def test_real_rom_mcp_trade_exchanges_party_records(tmp_path, version, peer_version):
    """Every ordered canonical orientation over the public in-process pair."""
    if os.environ.get("POKERED_SKIP_SHA1"):
        pytest.fail("POKERED_SKIP_SHA1 must not be used for real-ROM evidence")
    primary_fixture, peer_fixture = _orientation_fixtures(version, peer_version)
    primary_asset = _require_assets(version, primary_fixture)
    peer_asset = _require_assets(peer_version, peer_fixture)

    async with _stdio_server(tmp_path, primary_asset, peer_asset) as client:
        pair = LocalPair(client, _child_runtime_mode())
        primary_before, peer_before = await _fixture_pair(
            pair, primary_asset, peer_asset, fixture=primary_fixture
        )
        await _enter_trade_flow(pair)
        run = await _drive_trade(
            pair,
            party_counts=(len(primary_before), len(peer_before)),
            primary_before=primary_before,
            peer_before=peer_before,
        )
        verdict = assert_paired_exchange(
            run,
            primary_before=primary_before,
            peer_before=peer_before,
            label=f"{version}-{peer_version}",
        )
        payload = _base_row_payload(
            version, peer_version, primary_asset, peer_asset, primary_fixture, peer_fixture
        )
        payload.update(
            {
                "transport": "local_pair",
                "scenario": "ordered_orientation",
                # Frames spent in the quiet trade-center rendezvous before the
                # first facing press, straight from the pair, so the row shows
                # the ROM-owned state it actually waited on.
                "walk_settle_frames": pair.walk_settle_frames,
                "party_count": len(primary_before),
                "peer_party_count": len(peer_before),
                "outgoing_slot": 0,
                "peer_outgoing_slot": 0,
                "receiving_slot": len(_records_from_payload(run.final_records[0])) - 1,
                "offered_slots": {str(owner): slot for owner, slot in sorted(run.offered.items())},
                "submenu_back_outs": run.back_outs,
                "copy_milestone_observed": True,
                "copy_primary_map": _overworld(run.copy_states[0])["map_id"],
                "copy_peer_map": _overworld(run.copy_states[1])["map_id"],
                "primary_map": _overworld(run.final_states[0])["map_id"],
                "peer_map": _overworld(run.final_states[1])["map_id"],
                "post_trade_frames": run.post_frames,
                "evolution_events": run.evolution,
                "completion_menu_keys": [
                    _menu(state)["watched_keys"] for state in run.final_states
                ],
                "step_chunk": STEP_CHUNK,
            }
        )
        payload.update(verdict)
        _emit_row(payload["runtime_mode"], payload)
        await client.eof()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "peer_version"),
    ORIENTATIONS,
    ids=[_orientation_id(orientation) for orientation in ORIENTATIONS],
)
async def test_real_rom_mcp_trade_exchanges_party_records_over_tcp(tmp_path, version, peer_version):
    """Every ordered canonical orientation over two public TCP-linked servers.

    The two processes are independent: each one loads its own fixture through
    the public ``load_state`` tool and reports only its own records and its own
    ROM event log, so the paired exchange is observed from both ends rather than
    published by one in-process neighbour.
    """
    if os.environ.get("POKERED_SKIP_SHA1"):
        pytest.fail("POKERED_SKIP_SHA1 must not be used for real-ROM evidence")
    primary_fixture, peer_fixture = _orientation_fixtures(version, peer_version)
    primary_asset = _require_assets(version, primary_fixture)
    peer_asset = _require_assets(peer_version, peer_fixture)

    async with _tcp_pair_servers(tmp_path, primary_asset, peer_asset) as pair:
        primary_before, peer_before = await _fixture_pair(
            pair, primary_asset, peer_asset, fixture=primary_fixture
        )
        await _enter_trade_flow(pair)
        run = await _drive_trade(
            pair,
            party_counts=(len(primary_before), len(peer_before)),
            primary_before=primary_before,
            peer_before=peer_before,
        )
        verdict = assert_paired_exchange(
            run,
            primary_before=primary_before,
            peer_before=peer_before,
            label=f"tcp-{version}-{peer_version}",
        )
        payload = _base_row_payload(
            version, peer_version, primary_asset, peer_asset, primary_fixture, peer_fixture
        )
        payload.update(
            {
                "transport": "tcp_pair",
                "scenario": "ordered_orientation",
                # Recorded straight from the pair, so the row proves it armed
                # the documented pacing control on both owners rather than
                # leaving a reader to infer it from the row passing.
                "network_frame_barrier": pair.network_frame_barrier_armed,
                # Same contract for the walk rendezvous: the frames this row
                # actually spent waiting for the ROM-owned trade-center state
                # to stop changing before it pressed the facing direction.
                "walk_settle_frames": pair.walk_settle_frames,
                "party_count": len(primary_before),
                "peer_party_count": len(peer_before),
                "outgoing_slot": 0,
                "peer_outgoing_slot": 0,
                "receiving_slot": len(_records_from_payload(run.final_records[0])) - 1,
                "offered_slots": {str(owner): slot for owner, slot in sorted(run.offered.items())},
                "submenu_back_outs": run.back_outs,
                "copy_milestone_observed": True,
                "copy_primary_map": _overworld(run.copy_states[0])["map_id"],
                "copy_peer_map": _overworld(run.copy_states[1])["map_id"],
                "primary_map": _overworld(run.final_states[0])["map_id"],
                "peer_map": _overworld(run.final_states[1])["map_id"],
                "post_trade_frames": run.post_frames,
                "evolution_events": run.evolution,
                "completion_menu_keys": [
                    _menu(state)["watched_keys"] for state in run.final_states
                ],
                "step_chunk": STEP_CHUNK,
            }
        )
        payload.update(verdict)
        _emit_row(payload["runtime_mode"], payload)
        await pair.eof()


@pytest.mark.asyncio
async def test_real_rom_mcp_trade_over_tcp_yellow_needs_the_frame_barrier(tmp_path):
    """Negative control: the Yellow TCP row dies without the documented arm.

    The passing ``over_tcp`` rows above arm ``link_frame_barrier`` right after
    the pair reports ``connected``.  Without that arm the same pair, the same
    fixtures, and the same driver never leave the link-menu preamble: one owner
    blocks waiting for a peer response that this client's frame-paced step
    never delivers, and the calling process latches the transfer contract's
    fatal ``serial_backend_error`` before either ROM appends a received record.

    This test is the control that makes the arm falsifiable.  It withholds
    exactly one thing -- the arm -- and requires the row to fail *and* the
    client to observe that stable error code with no ROM-owned copy, so a row
    that passed above cannot also pass here for an unrelated reason, and an arm
    that is not load-bearing makes this control fail instead of silently
    passing.
    """
    if os.environ.get("POKERED_SKIP_SHA1"):
        pytest.fail("POKERED_SKIP_SHA1 must not be used for real-ROM evidence")

    version = peer_version = "yellow"
    primary_fixture, peer_fixture = _orientation_fixtures(version, peer_version)
    primary_asset = _require_assets(version, primary_fixture)
    peer_asset = _require_assets(peer_version, peer_fixture)

    async with _tcp_pair_servers(tmp_path, primary_asset, peer_asset) as pair:
        assert pair.needs_network_frame_barrier() is True, pair.versions
        await _fixture_pair(pair, primary_asset, peer_asset, fixture=primary_fixture)
        with pytest.raises(AssertionError) as exc_info:
            await _enter_trade_flow(pair, arm_barrier=False)
        assert pair.network_frame_barrier_armed is False, pair.versions
        # The failing call is the remote serial step, so the client's own
        # observation is already the latched backend failure: the structured
        # MCP error carries the stable code plus the transport's message.
        failure_text = str(exc_info.value)
        observed = _latched_error_code(failure_text)
        assert observed == "serial_backend_error", failure_text
        # Withholding the arm must also withhold every ROM-owned copy: the row
        # died in the link-menu preamble, so neither owner appended a received
        # record.
        assert await _trade_received_counts(pair) == [0, 0], pair.versions
        payload = _base_row_payload(
            version, peer_version, primary_asset, peer_asset, primary_fixture, peer_fixture
        )
        payload.update(
            {
                "transport": "tcp_pair",
                "scenario": "unarmed_barrier_negative_control",
                "network_frame_barrier_armed": False,
                "expected_failure": "latched serial_backend_error before any copy",
                "observed_failure": observed,
                "trade_received": await _trade_received_counts(pair),
            }
        )
        _emit_row(payload["runtime_mode"], payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "peer_version", "primary_slot", "peer_slot"),
    MULTI_MEMBER_ORIENTATIONS,
    ids=[_multi_member_id(row) for row in MULTI_MEMBER_ORIENTATIONS],
)
async def test_real_rom_mcp_trade_exchanges_multi_member_slot_records(
    tmp_path, version, peer_version, primary_slot, peer_slot
):
    """A six-member party must offer the intended slot and compact the rest.

    Issue #105 requires the sender/receiver *slot* roles, and finding 4 of the
    independent PR #118 review requires a compaction-aware oracle: Gen I removes
    the offered record, keeps the survivors in order, and appends the received
    44-byte record at the final occupied slot, so an in-place replacement model
    is wrong for any party larger than one.

    This row is driven from the registered ``cable_club-slots.state`` fixture,
    whose observable state is the same pre-connection Cable Club attendant tile
    as the ordinary fixture with a six-member party of pairwise-distinct records.
    Both claims are checked separately and against ROM-owned reads: the cursor
    slot each owner actually rested on when it confirmed the offer is observed
    on the live ``.playerMonMenu`` before the press, and the post-trade digest
    list must equal the source-defined compaction result.  Three independent
    controls make the row falsifiable rather than incidental: before a single
    input the six admitted digests per owner are required to be pairwise
    distinct; the naive in-place model is computed in the test and required to
    *disagree*; and the review's wrong-slot mutation -- copying slots ``(0, 1)``
    while claiming ``(2, 0)`` -- is replayed against these real digests and
    required to fail the acceptance oracle.
    """
    if os.environ.get("POKERED_SKIP_SHA1"):
        pytest.fail("POKERED_SKIP_SHA1 must not be used for real-ROM evidence")

    primary_asset = _require_assets(version, MULTI_MEMBER_FIXTURE)
    peer_asset = _require_assets(peer_version, MULTI_MEMBER_FIXTURE)

    async with _stdio_server(tmp_path, primary_asset, peer_asset) as client:
        pair = LocalPair(client, _child_runtime_mode())
        primary_before, peer_before = await _fixture_pair(
            pair, primary_asset, peer_asset, fixture=MULTI_MEMBER_FIXTURE
        )
        assert len(primary_before) == MULTI_MEMBER_PARTY_COUNT, primary_before
        assert len(peer_before) == MULTI_MEMBER_PARTY_COUNT, peer_before
        assert 0 <= primary_slot < len(primary_before), primary_slot
        assert 0 <= peer_slot < len(peer_before), peer_slot
        assert primary_slot != peer_slot, (primary_slot, peer_slot)
        assert primary_before[primary_slot]["digest"] != peer_before[peer_slot]["digest"], (
            version,
            peer_version,
        )
        # The fixture's own reason for existing: six same-species records whose
        # digests are pairwise distinct, so any copy that ignores the selected
        # cursor changes the digest list instead of matching the required one.
        for owner, records in enumerate((primary_before, peer_before)):
            digests = _digests(records)
            assert len(set(digests)) == MULTI_MEMBER_PARTY_COUNT, (owner, digests)
            assert {record["species"] for record in records} == {records[0]["species"]}, (
                owner,
                records,
            )
        # Replay the independent review's diagnostic mutation on these actual
        # digests: a response that copied slots (0, 1) must not satisfy the
        # oracle invoked with the required slots (2, 0).
        wrong_slot = _wrong_slot_run(
            primary_before,
            peer_before,
            copied_primary_slot=0,
            copied_peer_slot=1,
            claimed_offered={0: 2, 1: 0},
        )
        with pytest.raises(AssertionError):
            assert_paired_exchange(
                wrong_slot,
                primary_before=primary_before,
                peer_before=peer_before,
                primary_slot=2,
                peer_slot=0,
                label=f"wrong-slot-{version}-{peer_version}",
            )

        await _enter_trade_flow(pair)
        run = await _drive_trade(
            pair,
            party_counts=(len(primary_before), len(peer_before)),
            primary_before=primary_before,
            peer_before=peer_before,
            primary_slot=primary_slot,
            peer_slot=peer_slot,
        )
        verdict = assert_paired_exchange(
            run,
            primary_before=primary_before,
            peer_before=peer_before,
            primary_slot=primary_slot,
            peer_slot=peer_slot,
            label=f"multi-{version}-{peer_version}",
        )
        primary_after = _records_from_payload(run.final_records[0])
        # The intended slot was observed on the ROM menu, not inferred from the
        # result: ``offered`` is recorded before the confirming press.
        assert run.offered == {0: primary_slot, 1: peer_slot}, run.offered
        # The compaction model must be falsifiable on this very fixture: an
        # in-place replacement at the outgoing index would produce a different
        # digest list, so require the observed list to differ from it.
        in_place_primary = [record["digest"] for record in primary_before]
        in_place_primary[primary_slot] = peer_before[peer_slot]["digest"]
        assert [record["digest"] for record in primary_after] != in_place_primary, (
            "compaction oracle is not falsifiable on this fixture",
            primary_after,
        )
        payload = _base_row_payload(
            version,
            peer_version,
            primary_asset,
            peer_asset,
            MULTI_MEMBER_FIXTURE,
            MULTI_MEMBER_FIXTURE,
        )
        payload.update(
            {
                "transport": "local_pair",
                "scenario": "multi_member_slots",
                "party_count": len(primary_before),
                "peer_party_count": len(peer_before),
                "outgoing_slot": primary_slot,
                "peer_outgoing_slot": peer_slot,
                "receiving_slot": len(primary_after) - 1,
                "offered_slots": {str(owner): slot for owner, slot in sorted(run.offered.items())},
                "submenu_back_outs": run.back_outs,
                "in_place_model_differs": True,
                "wrong_slot_control_rejected": True,
                "distinct_slot_digests": True,
                "copy_primary_map": _overworld(run.copy_states[0])["map_id"],
                "copy_peer_map": _overworld(run.copy_states[1])["map_id"],
                "primary_map": _overworld(run.final_states[0])["map_id"],
                "peer_map": _overworld(run.final_states[1])["map_id"],
                "post_trade_frames": run.post_frames,
                "evolution_events": run.evolution,
                "step_chunk": STEP_CHUNK,
            }
        )
        payload.update(verdict)
        _emit_row(payload["runtime_mode"], payload)
        await client.eof()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "peer_version"),
    (("red_color", "blue_color"),),
    ids=["red_color-blue_color"],
)
async def test_real_rom_mcp_trade_cancel_before_commitment_keeps_records(
    tmp_path, version, peer_version
):
    """Backing out of the trade sub-menu must not swap or partially copy records.

    Issue #105 requires a cancel before commitment with proof that no partial
    record swap happened.  Both owners are driven to the live trade party menu
    through public input, each opens its STATS/TRADE sub-menu, and then each
    presses B: the ROM treats B as the return key for that sub-menu, so no offer
    is confirmed and neither owner commits a record.

    The assertion is a byte-exact comparison of every occupied slot against the
    fixture read, plus an unchanged completion-event count and an un-fired
    ROM-owned copy marker.  The flow must also remain usable: the same session
    then completes the real exchange from the restored menu, which is the
    source-defined recovery the issue asks for in place of an unsupported
    rollback guarantee.
    """
    if os.environ.get("POKERED_SKIP_SHA1"):
        pytest.fail("POKERED_SKIP_SHA1 must not be used for real-ROM evidence")

    primary_asset = _require_assets(version, ORDINARY_FIXTURE)
    peer_asset = _require_assets(peer_version, ORDINARY_FIXTURE)

    async with _stdio_server(tmp_path, primary_asset, peer_asset) as client:
        pair = LocalPair(client, _child_runtime_mode())
        primary_before, peer_before = await _fixture_pair(
            pair, primary_asset, peer_asset, fixture=ORDINARY_FIXTURE
        )
        await _enter_trade_flow(pair)
        party_count = len(primary_before)
        assert len(peer_before) == party_count, (primary_before, peer_before)
        _, menu_frames = await _await_party_menu(pair, (party_count, party_count))
        _, submenu_frames = await _enter_stats_trade_submenu(pair, party_count)
        _, back_frames = await _back_out_of_submenu(pair, party_count)

        cancelled = await _all_records(pair)
        primary_cancelled = _records_from_payload(cancelled[0])
        peer_cancelled = _records_from_payload(cancelled[1])
        assert _digests(primary_cancelled) == _digests(primary_before), primary_cancelled
        assert _digests(peer_cancelled) == _digests(peer_before), peer_cancelled
        assert await _trade_received_counts(pair) == [0, 0], "a cancel fired the copy marker"

        run = await _drive_trade(
            pair,
            party_counts=(party_count, party_count),
            primary_before=primary_before,
            peer_before=peer_before,
        )
        verdict = assert_paired_exchange(
            run,
            primary_before=primary_before,
            peer_before=peer_before,
            label="cancel-then-trade",
        )
        payload = _base_row_payload(
            version, peer_version, primary_asset, peer_asset, ORDINARY_FIXTURE, ORDINARY_FIXTURE
        )
        payload.update(
            {
                "transport": "local_pair",
                "scenario": "cancel_before_commitment",
                "party_count": party_count,
                "menu_frames": menu_frames,
                "submenu_frames": submenu_frames,
                "back_frames": back_frames,
                "cancelled_primary": [record["digest"] for record in primary_cancelled],
                "cancelled_peer": [record["digest"] for record in peer_cancelled],
                "cancelled_copy_events": 0,
                "offered_slots": {str(owner): slot for owner, slot in sorted(run.offered.items())},
                "submenu_back_outs": run.back_outs,
                "copy_primary_map": _overworld(run.copy_states[0])["map_id"],
                "copy_peer_map": _overworld(run.copy_states[1])["map_id"],
                "primary_map": _overworld(run.final_states[0])["map_id"],
                "peer_map": _overworld(run.final_states[1])["map_id"],
                "post_trade_frames": run.post_frames,
                "evolution_events": run.evolution,
                "step_chunk": STEP_CHUNK,
            }
        )
        payload.update(verdict)
        _emit_row(payload["runtime_mode"], payload)
        await client.eof()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "peer_version"),
    (("red_color", "blue_color"),),
    ids=["red_color-blue_color"],
)
async def test_real_rom_mcp_trade_eof_during_setup_exits_cleanly(tmp_path, version, peer_version):
    """EOF before the trade flow starts must exit bounded and clean.

    Issue #105 requires the disconnect path to be exercised rather than assumed.
    The client loads the fixture, pairs, and then closes its stdin without any
    shutdown request.  ``RomClient.eof`` is the assertion: the server must exit
    with status 0 inside its exit bound, emit no trailing frame, and leave no
    surviving process group.  A wedged or forked server fails this instead of
    being reported as a clean shutdown.
    """
    if os.environ.get("POKERED_SKIP_SHA1"):
        pytest.fail("POKERED_SKIP_SHA1 must not be used for real-ROM evidence")

    primary_asset = _require_assets(version, ORDINARY_FIXTURE)
    peer_asset = _require_assets(peer_version, ORDINARY_FIXTURE)

    async with _stdio_server(tmp_path, primary_asset, peer_asset) as client:
        pair = LocalPair(client, _child_runtime_mode())
        primary_before, _ = await _fixture_pair(
            pair, primary_asset, peer_asset, fixture=ORDINARY_FIXTURE
        )
        await pair.link_up()
        status = await client.tool("link_status")
        assert status["paired"] is True, status
        payload = _base_row_payload(
            version, peer_version, primary_asset, peer_asset, ORDINARY_FIXTURE, ORDINARY_FIXTURE
        )
        payload.update(
            {
                "transport": "local_pair",
                "scenario": "eof_during_setup",
                "party_count": len(primary_before),
                "paired": True,
                "cleanup": "stdin_eof_bounded_exit_0_no_live_group",
            }
        )
        _emit_row(payload["runtime_mode"], payload)
        await client.eof()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "peer_version"),
    (("red_color", "blue_color"),),
    ids=["red_color-blue_color"],
)
async def test_real_rom_mcp_trade_eof_during_active_trade_exits_cleanly(
    tmp_path, version, peer_version
):
    """EOF before commitment must exit bounded, clean, and unswapped.

    Both owners have entered the Trade Center, walked to the hidden trade
    trigger, and opened the STATS/TRADE sub-menu, but neither has confirmed an
    offer.  The client reads both parties (proving no partial record swap has
    happened at the interruption point) and the ROM-owned copy marker (proving
    nothing was committed), then closes its stdin.  ``RomClient.eof`` requires
    the server to exit 0 inside the exit bound with no trailing frame and no
    surviving process group, which is the bounded owned cleanup the issue asks
    for.
    """
    if os.environ.get("POKERED_SKIP_SHA1"):
        pytest.fail("POKERED_SKIP_SHA1 must not be used for real-ROM evidence")

    primary_asset = _require_assets(version, ORDINARY_FIXTURE)
    peer_asset = _require_assets(peer_version, ORDINARY_FIXTURE)

    async with _stdio_server(tmp_path, primary_asset, peer_asset) as client:
        pair = LocalPair(client, _child_runtime_mode())
        primary_before, peer_before = await _fixture_pair(
            pair, primary_asset, peer_asset, fixture=ORDINARY_FIXTURE
        )
        await _enter_trade_flow(pair)
        party_count = len(primary_before)
        _, menu_frames = await _await_party_menu(pair, (party_count, party_count))
        _, submenu_frames = await _enter_stats_trade_submenu(pair, party_count)
        interrupted = await _all_records(pair)
        primary_interrupted = _records_from_payload(interrupted[0])
        peer_interrupted = _records_from_payload(interrupted[1])
        assert _digests(primary_interrupted) == _digests(primary_before), primary_interrupted
        assert _digests(peer_interrupted) == _digests(peer_before), peer_interrupted
        assert await _trade_received_counts(pair) == [0, 0], "EOF point was already committed"
        payload = _base_row_payload(
            version, peer_version, primary_asset, peer_asset, ORDINARY_FIXTURE, ORDINARY_FIXTURE
        )
        payload.update(
            {
                "transport": "local_pair",
                "scenario": "eof_before_commitment",
                "party_count": party_count,
                "menu_frames": menu_frames,
                "submenu_frames": submenu_frames,
                "interrupted_primary": [record["digest"] for record in primary_interrupted],
                "interrupted_peer": [record["digest"] for record in peer_interrupted],
                "interrupted_copy_events": 0,
                "cleanup": "stdin_eof_bounded_exit_0_no_live_group",
            }
        )
        _emit_row(payload["runtime_mode"], payload)
        await client.eof()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "peer_version"),
    (("red_color", "blue_color"),),
    ids=["red_color-blue_color"],
)
async def test_real_rom_mcp_trade_eof_after_commitment_exits_cleanly(
    tmp_path, version, peer_version
):
    """EOF with a committed, still-running exchange must clean up bounded.

    Finding 5 of the independent PR #118 review requires the interruption to
    land *after* commitment with real work outstanding, not before it.  The pair
    is driven until each owner's own ROM event log publishes the
    ``trade_received`` append marker and both parties hold the copied records;
    at that point the copy is committed but the animation, evolution check, save
    and room return have not run.  The client then closes its stdin.

    ``RomClient.eof`` is the assertion: exit status 0 inside the exit bound, no
    trailing stdout frame, and no surviving process group.  No rollback is
    claimed or promised: the parties at the interruption point are recorded
    exactly as the ROM left them.
    """
    if os.environ.get("POKERED_SKIP_SHA1"):
        pytest.fail("POKERED_SKIP_SHA1 must not be used for real-ROM evidence")

    primary_asset = _require_assets(version, ORDINARY_FIXTURE)
    peer_asset = _require_assets(peer_version, ORDINARY_FIXTURE)

    async with _stdio_server(tmp_path, primary_asset, peer_asset) as client:
        pair = LocalPair(client, _child_runtime_mode())
        primary_before, peer_before = await _fixture_pair(
            pair, primary_asset, peer_asset, fixture=ORDINARY_FIXTURE
        )
        await _enter_trade_flow(pair)
        party_count = len(primary_before)
        records, states, received, frames = await _drive_to_commitment(
            pair,
            party_counts=(party_count, party_count),
            primary_before=primary_before,
            peer_before=peer_before,
        )
        committed_primary = _records_from_payload(records[0])
        committed_peer = _records_from_payload(records[1])
        expected_primary = _compaction_expected_digests(primary_before, 0, peer_before[0]["digest"])
        expected_peer = _compaction_expected_digests(peer_before, 0, primary_before[0]["digest"])
        assert [record["digest"] for record in committed_primary] == expected_primary, records[0]
        assert [record["digest"] for record in committed_peer] == expected_peer, records[1]
        assert all(count >= 1 for count in received), received
        payload = _base_row_payload(
            version, peer_version, primary_asset, peer_asset, ORDINARY_FIXTURE, ORDINARY_FIXTURE
        )
        payload.update(
            {
                "transport": "local_pair",
                "scenario": "eof_after_commitment",
                "party_count": party_count,
                "commitment_frames": frames,
                "committed_trade_received": received,
                "committed_primary": [record["digest"] for record in committed_primary],
                "committed_peer": [record["digest"] for record in committed_peer],
                "committed_primary_map": _overworld(states[0])["map_id"],
                "committed_peer_map": _overworld(states[1])["map_id"],
                "outstanding_after_commitment": [
                    "trade_animation",
                    "evolution_check",
                    "save_party_and_dex",
                    "room_return",
                ],
                "rollback_claimed": False,
                "cleanup": "stdin_eof_bounded_exit_0_no_live_group",
            }
        )
        _emit_row(payload["runtime_mode"], payload)
        await client.eof()
