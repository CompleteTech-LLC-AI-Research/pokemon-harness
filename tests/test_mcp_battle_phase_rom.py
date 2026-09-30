"""Real-ROM MCP stdio link battle that reads ``pokered://game-state``.

This is actual stdio evidence for the additive battle observations from
issue #88.  A real ``pokered_harness.mcp_server`` stdio server is launched
with an in-process peer on the immutable ``cable_club-battle.state`` fixture.
The public MCP surface then loads the primary fixture (``load_state``), pairs
the two ROMs (``link_pair``), drives them through the Cable Club receptionist,
the LinkMenu (COLOSSEUM), the Colosseum warp, the battle intro and the battle
command/move menu, and reads the battle fields through the public MCP
``resources/read`` protocol only.

The test asserts:

* the additive enemy-combatant identity with its documented validity;
* ``raw_battle_result`` is exposed and ``terminal_result`` stays ``None``
  because no confirmed terminal outcome is observed;
* the derived ``phase``/``phase_valid``/``phase_evidence`` and the
  menu ``menu_open``/``menu_evidence`` fields are present with their
  documented validity, including ``command_selection`` derived only from the
  session's observational battle-menu hooks;
* the transient fields are present;
* no RAM writes, no hash bypass, and no hooks beyond the server's own
  battle-menu observation hooks are used.

The public MCP tool surface has no peer-load operation, so the harness child
preloads the immutable peer fixture before serving; the primary fixture is
loaded through the public ``load_state`` tool.  Every asset is validated
against the pinned ROM/SYM SHA-1 values (``POKERED_SKIP_SHA1`` is rejected).

The paired additive observation is read while the link is up, and the primary
is then driven into a ``FIGHT`` -> move-menu entry with the pair STILL
connected through the public ``link_step`` tool: ordinary paired stepping
advances bookkeeping without starting a new observation epoch, so the
``command_selection`` hook evidence survives and is observed without unpairing.

Beyond menu entry the scenario also settles a real move: both sides select the
highlighted move through the ROM's menu handling, the paired link is advanced
through the move exchange and execution, and the test asserts the settled
``player_selected_move``, the matching PP decrement, the opponent HP drop and
the return to the next command boundary from the public
``pokered://game-state`` resource.  It then issues and cancels an outstanding
``link_step`` request, proving a bounded client-side ``CancelledError``, a
still-responsive server and clean owned-process teardown at ``client.eof()``.

The same active, still-paired battle is then used to exercise the two
server-visible concurrency paths the public stdio surface exposes:

* **Concurrent resource read.**  A public ``tools/call link_step`` with a large
  finite count is written, and a public ``resources/read pokered://game-state``
  is written immediately after it, before either reply is read.  The read
  serializes on the session's emulator lock, so it observes a coherent
  pre- or post-step snapshot (the ``epoch.tick`` is exactly one of those two
  boundary values, never a torn mid-step value) with the documented battle
  fields and validity.
* **Server-visible cancellation.**  A long public ``link_step`` is written and
  then cancelled with the MCP cancellation notification
  ``{"jsonrpc": "2.0", "method": "notifications/cancelled", "params":
  {"requestId": <id>, "reason": ...}}``.  The MCP SDK's receive loop marks the
  in-flight ``_call_tool`` task cancelled and answers with the JSON-RPC error
  ``{"code": 0, "message": "Request cancelled"}``; the server's
  ``_await_blocking_task`` shield then leaves the emulator worker alive until
  it settles.  The test asserts the bounded error terminal, that the full
  finite count still completed (so no owned emulator work was orphaned), that
  a later ``resources/read`` and ``link_status`` remain responsive, that the
  link is still paired and steppable, and that ``client.eof()`` still tears
  every owned process down cleanly.

Forced replacement and the terminal return are exercised by two further
scenarios over their own admitted immutable fixtures (``kind: boundary`` rows
of ``release-evidence/fixture-manifest.json``, produced by
``scripts/produce_battle_state_fixtures.py``).  ``cable_club-battle-faint`` is
a real forced-replacement boundary (the owner's own combatant is at zero HP
with ``wInHandlePlayerMonFainted`` set and the battle party menu open) and
``cable_club-terminal`` is a real terminal return, and both are read through
``resources/read`` after a public ``load_state``; ``cable_club-pre-terminal``
is the last ROM command boundary before the deciding knockout, which the test
drives into ``EndOfBattle`` with public input only, folding in the forced
replacement the deciding knockout can open from the party HP list the payload
exposes, so the terminal return is observed as a live falling edge rather than
asserted from a loaded byte.  This
scenario's own fixture is a plain battle state, so a settled FIGHT turn
legitimately leaves the battle live with ``terminal_result`` unset; a normal
FIGHT move also leaves ``wActionResultOrTookBattleTurn`` at zero
(``ExecutePlayerMoveDone`` resets it), so ``ACTION_RESOLUTION`` is correctly
never derived for this turn and the test records every observed phase.

Runs inside the harness child with a preloaded peer; the child reuses the
existing peer-preload harness approach.

Gated with ``skipif`` on the canonical ROM/SYM/fixture assets.  The parameter
ids match the canonical games (``red_color``, ``blue_color``, ``yellow``).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil

import pytest

from tests._mcp_battle_phase_rom_drive_support import (
    _assert_battle_observations,
    _assert_terminal_return,
    _command_menu_ready,
    _drive_boundary_turn,
    _drive_effect_turn,
    _drive_through_live_replacement,
    _drive_to_link_menu,
    _enter_battle,
    _pair,
    _prelink,
    _read_response_for,
    _read_state_during_outstanding_step,
    _select_colosseum,
    _send_raw_request,
    _server_visible_cancel,
    _settle_selected_move,
)
from tests._mcp_battle_phase_rom_support import (
    _RESOLUTION_CONTINUATION_LABELS,
    BATTLE_KINDS,
    BATTLE_MENU_BUDGET,
    BOUNDARY_DRIVE_BUDGET,
    BOUNDARY_INPUT_SPACING,  # noqa: F401  (retained facade attribute: boundary_tests.BOUNDARY_INPUT_SPACING)
    CONCURRENT_STEP_FRAMES,
    FAMILIES,
    FORCED_REPLACEMENT_PHASE,
    OBSERVATIONAL_MENU_HOOKS,
    OUTSTANDING_CANCEL_DELAY,
    OUTSTANDING_STEP_FRAMES,
    ROOT,
    SERVER_CANCEL_STEP_FRAMES,
    STATUS_MOVE_ID,
    STEP_CHUNK,
    TERMINAL_RETURN_PHASE,
    _active_hp,
    _active_mon,
    _active_moves,
    _active_pp,
    _advance_pair_primary_until,
    _advance_until,
    _at_command_boundary,
    _battle,
    _battle_assets,
    _boundary_assets,
    _log,
    _request,
    _states,
    _stdio_server,
)
from tests._rom_assets import find_fixture_root
from tests.test_mcp_timed_stdio import CALL_BOUND


@pytest.mark.parametrize("version", FAMILIES)
def test_battle_fixture_is_admitted_against_its_manifest_row(tmp_path, monkeypatch, version):
    """A tampered battle fixture must be rejected, not loaded.

    ``_battle_assets`` validates the actual battle bytes against the manifest
    row (size, SHA-1, SHA-256, ROM/symbol bindings) instead of validating the
    ordinary ``cable_club`` fixture and substituting unchecked battle bytes.
    The tampering is done in a temporary fixture root so no repository or
    private fixture byte is touched.
    """
    if os.environ.get("POKERED_SKIP_SHA1"):
        pytest.fail("POKERED_SKIP_SHA1 must not be used for real-ROM evidence")
    original_root = find_fixture_root(ROOT)

    # The untampered bytes pass admission.
    assets = _battle_assets(version)
    assert assets["state"] != b""

    family = version.split("_")[0]
    if not (original_root / family / "cable_club-battle.state").is_file():
        pytest.skip(f"missing BYO battle fixture for {family}")
    tampered_root = tmp_path / "fixtures"
    shutil.copytree(original_root, tampered_root)
    battle = tampered_root / family / "cable_club-battle.state"
    battle.write_bytes(battle.read_bytes() + b"\x00")
    monkeypatch.setenv("POKERED_FIXTURE_ROOT", str(tampered_root))

    # An appended byte no longer matches the row's size and digests, so the
    # loader must refuse the bytes rather than hand them to a session.
    with pytest.raises(ValueError, match="mismatch"):
        _battle_assets(version)


@pytest.mark.asyncio
@pytest.mark.parametrize("version", FAMILIES)
async def test_real_rom_mcp_link_battle_reads_additive_state(tmp_path, version):
    if os.environ.get("POKERED_SKIP_SHA1"):
        pytest.fail("POKERED_SKIP_SHA1 must not be used for real-ROM evidence")
    asset = _battle_assets(version)
    async with _stdio_server(tmp_path, asset) as client:
        await _prelink(client, asset)
        await _pair(client)

        await _drive_to_link_menu(client)
        await _select_colosseum(client)
        await _enter_battle(client)

        # The battle flag can rise a few frames before the enemy combatant
        # buffer is populated; wait for the ROM-owned record to become valid.
        states, enemy_frames = await _advance_until(
            client,
            lambda state: _battle(state)["enemy_mon_valid"] is True,
            budget=BATTLE_MENU_BUDGET,
            prompt="enemy combatant never became valid on both peers",
        )
        _log("enemy_valid", f"frames={enemy_frames}")

        # Observe the additive battle state on both peers while still paired.
        labels = (f"{version}-primary", f"{version}-peer")
        states = await _states(client)
        for state, label in zip(states, labels, strict=True):
            _assert_battle_observations(state, label=label)
            assert _battle(state)["terminal_result"] is None, (label, state)

        # Select FIGHT on the primary and observe the ROM move menu open while
        # the pair is STILL stepped through the public link_step tool.  Ordinary
        # paired stepping advances bookkeeping without invalidating the menu
        # observation the execution hooks recorded.
        single, menu_frames = await _advance_pair_primary_until(
            client,
            lambda state: _battle(state)["menu_open"] is True,
            budget=BATTLE_MENU_BUDGET,
            prompt="battle command/move menu never opened on the primary while paired",
        )
        _log("battle_menu", f"frames={menu_frames}")
        _assert_battle_observations(single, label=labels[0])
        battle = _battle(single)
        assert battle["menu_open"] is True, battle
        assert battle["phase"] == 2 and battle["phase_valid"] is True, battle
        assert battle["menu_evidence"], battle
        assert battle["terminal_result"] is None, battle

        # -- move settlement -------------------------------------------------
        # Capture the pre-turn PP/HP before either side commits a move.  The
        # cursor is still on FIGHT (current_menu_item == 0) and no move has been
        # selected yet, so a later change is a genuine ROM-driven settlement.
        turn_start = await _states(client)
        for state, label in zip(turn_start, labels, strict=True):
            _assert_battle_observations(state, label=label)
        assert _battle(turn_start[0])["player_selected_move"] == 0, turn_start[0]
        enemy_hp_before = _battle(turn_start[0])["enemy_mon"]["hp"]
        player_hp_before = _active_hp(turn_start[0])
        assert player_hp_before is not None, turn_start[0]

        settlement = await _settle_selected_move(client, before=turn_start)
        boundary = settlement["boundary"]
        assert settlement["decremented"][0] is not None, settlement
        assert settlement["decremented"][1] is not None, settlement
        for index, label in enumerate(labels):
            changed = settlement["decremented"][index]
            before_pp = settlement["pp_before"][index]
            after_pp = settlement["pp_after"][index]
            assert len(changed) == 1, (label, changed, settlement)
            assert before_pp is not None and after_pp is not None, (label, settlement)
            slot = changed[0]
            assert before_pp[slot] - after_pp[slot] == 1, (label, slot, settlement)

        primary_state = boundary[0]
        primary_battle = _battle(primary_state)
        primary_moves = _active_moves(primary_state)
        primary_slot = settlement["decremented"][0][0]
        assert primary_moves is not None and primary_moves[primary_slot] != 0, primary_state
        assert primary_battle["player_selected_move"] == primary_moves[primary_slot], (
            primary_battle,
            primary_moves,
        )
        # The opponent's HP dropped from the executed move and the player's
        # active mon never healed; both sides returned to a live battle.
        enemy_hp_after = primary_battle["enemy_mon"]["hp"]
        player_hp_after = _active_hp(primary_state)
        assert 0 <= enemy_hp_after < enemy_hp_before, (enemy_hp_before, enemy_hp_after)
        assert player_hp_after is not None and player_hp_after <= player_hp_before, (
            player_hp_before,
            player_hp_after,
        )
        assert primary_battle["raw_is_in_battle"] in BATTLE_KINDS, primary_battle
        assert primary_battle["terminal_result"] is None, primary_battle
        assert _at_command_boundary(primary_state), primary_battle
        assert primary_battle["phase"] == 2 and primary_battle["phase_valid"] is True, (
            primary_battle
        )
        _log(
            "move_settlement",
            f"frames={settlement['frames']} "
            f"boundary_frames={settlement['boundary_frames']} "
            f"phases={settlement['phases_seen']} actions={settlement['actions_seen']}",
        )

        # Action resolution must be *observed*, not assumed.  A regular FIGHT
        # move resets wActionResultOrTookBattleTurn to zero in
        # ExecutePlayerMoveDone, so the flag alone can never report this turn;
        # the session's move-execution hook is the ROM-owned proof, and the
        # drive samples states while the move is executing.  Issue #88 requires
        # the phase distinction, so a drive that never observes
        # ACTION_RESOLUTION is a failure rather than an accepted outcome.
        assert 3 in settlement["phases_seen"], settlement
        resolution_evidence = settlement["resolution_evidence"]
        assert resolution_evidence, settlement
        assert set(resolution_evidence) <= OBSERVATIONAL_MENU_HOOKS, settlement

        # -- outstanding request / asyncio cancellation ----------------------
        # Issue a paired link request, cancel it while it is still outstanding,
        # then prove the server stayed responsive and completed its own bounded
        # operation.  The cancel is a client-side asyncio task cancellation (the
        # documented terminal outcome is asyncio.CancelledError); the server
        # never sees it, so its response is drained before any later request.
        responsive_before = await _request(client, "pokered://game-state")
        request_id = await _send_raw_request(
            client,
            "tools/call",
            {"name": "link_step", "arguments": {"count": OUTSTANDING_STEP_FRAMES}},
        )
        reader = asyncio.create_task(_read_response_for(client, request_id, bound=CALL_BOUND))
        await asyncio.sleep(OUTSTANDING_CANCEL_DELAY)
        assert not reader.done(), "outstanding link_step completed before cancellation"
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
        cancelled_response = await _read_response_for(client, request_id, bound=CALL_BOUND)
        assert "result" in cancelled_response, cancelled_response
        responsive_after = await _request(client, "pokered://game-state")
        assert responsive_after["epoch"]["tick"] > responsive_before["epoch"]["tick"], (
            responsive_before["epoch"],
            responsive_after["epoch"],
        )
        _log(
            "cancellation",
            f"request_id={request_id} step_frames={OUTSTANDING_STEP_FRAMES} "
            f"tick={responsive_before['epoch']['tick']}->{responsive_after['epoch']['tick']}",
        )

        # -- resource read during an outstanding operation -------------------
        # A public game-state read is written while a long finite ``link_step``
        # is still outstanding.  The read serializes on the session emulator
        # lock, so it returns a coherent pre-/post-step snapshot (never a torn
        # mid-step tick) with the documented battle fields and validity.
        concurrent = await _read_state_during_outstanding_step(
            client, frames=CONCURRENT_STEP_FRAMES, label=labels[0]
        )
        # The following state is still coherent and monotonic.
        coherent_after = await _request(client, "pokered://game-state")
        _assert_battle_observations(coherent_after, label=labels[0])
        assert coherent_after["epoch"]["tick"] >= concurrent["tick"], (
            concurrent["tick"],
            coherent_after["epoch"],
        )

        # -- server-visible cancellation -------------------------------------
        # Canonical MCP cancellation: ``notifications/cancelled`` naming the
        # outstanding request id.  The server cancels its ``_call_tool`` task,
        # returns the bounded "Request cancelled" error, and its
        # ``_await_blocking_task`` shield lets the worker settle the full
        # finite step, so no owned emulator work is orphaned.
        server_cancel = await _server_visible_cancel(
            client, frames=SERVER_CANCEL_STEP_FRAMES, label=labels[0]
        )
        # The server is still responsive and the link is still owned/usable:
        # a subsequent resource read, status poll and paired step all succeed.
        responsive_after_cancel = await _request(client, "pokered://game-state")
        _assert_battle_observations(responsive_after_cancel, label=labels[0])
        assert responsive_after_cancel["epoch"]["tick"] == server_cancel["after_tick"], (
            server_cancel["after_tick"],
            responsive_after_cancel["epoch"],
        )
        link_status = await client.tool("link_status")
        assert link_status["paired"] is True, link_status
        assert link_status["link_backend"] == "bit_accurate", link_status
        stepped_after_cancel = await client.tool("link_step", {"count": 4})
        assert stepped_after_cancel["primary_tick"] == server_cancel["after_tick"] + 4, (
            server_cancel["after_tick"],
            stepped_after_cancel,
        )
        # A structured tool error remains preserved on the post-cancel link.
        # The error body is not asserted to be JSON: only that the server still
        # answers a public request with a bounded tool error (never a crash).
        preserved = await client.request(
            "tools/call", {"name": "link_step", "arguments": {"count": 0}}
        )
        assert preserved.get("isError") is True, preserved
        responsive_after = responsive_after_cancel

        # -- forced replacement / terminal return ----------------------------
        # This scenario's fixture is a plain battle state, so it legitimately
        # ends the settled turn still in a live battle.  The forced-replacement
        # and terminal-return boundaries are separate admitted immutable
        # fixtures exercised end to end by
        # ``test_real_rom_mcp_boundary_reads_fail_closed`` (loaded pairs) and
        # ``test_real_rom_mcp_terminal_return_drive_reads`` (the deciding turn
        # driven into EndOfBattle).  Here only the live-battle contract is
        # asserted: an active battle never reports a terminal outcome.
        final_battle = _battle(responsive_after)
        assert final_battle["raw_is_in_battle"] in BATTLE_KINDS, final_battle
        assert final_battle["terminal_result"] is None, final_battle
        assert final_battle["phase"] != TERMINAL_RETURN_PHASE, final_battle
        # A transient TERMINAL_RETURN during the turn is only valid as the
        # documented unconfirmed falling edge: wIsInBattle read as zero with no
        # surviving non-zero outcome, so terminal_result must stay None.
        for observation in settlement["terminal_observations"]:
            assert observation["raw_is_in_battle"] == 0, observation
            assert observation["terminal_result"] is None, observation

        print(
            "MCP_BATTLE_PHASE_ROM "
            + json.dumps(
                {
                    "version": version,
                    "transport": "local_pair",
                    "step_chunk": STEP_CHUNK,
                    "primary_battle": states[0]["battle"],
                    "peer_battle": states[1]["battle"],
                    "primary_selection": single["battle"],
                    "settlement": {
                        "frames": settlement["frames"],
                        "boundary_frames": settlement["boundary_frames"],
                        "pp_before": settlement["pp_before"],
                        "pp_after": settlement["pp_after"],
                        "decremented": settlement["decremented"],
                        "selected": settlement["selected"],
                        "phases_seen": settlement["phases_seen"],
                        "actions_seen": settlement["actions_seen"],
                        "raw_values": settlement["raw_values"],
                        "terminal_observations": settlement["terminal_observations"],
                        "enemy_hp": [enemy_hp_before, enemy_hp_after],
                        "player_hp": [player_hp_before, player_hp_after],
                    },
                    "cancellation": {
                        "step_frames": OUTSTANDING_STEP_FRAMES,
                        "terminal": "asyncio.CancelledError",
                        "response_result": True,
                    },
                    "concurrent_read": {
                        "step_frames": CONCURRENT_STEP_FRAMES,
                        "before_tick": concurrent["before_tick"],
                        "observed_tick": concurrent["tick"],
                    },
                    "server_cancellation": {
                        "step_frames": SERVER_CANCEL_STEP_FRAMES,
                        "request_id": server_cancel["request_id"],
                        "error_code": server_cancel["error"]["code"],
                        "error_message": server_cancel["error"]["message"],
                        "before_tick": server_cancel["before_tick"],
                        "after_tick": server_cancel["after_tick"],
                    },
                    "settled_live_battle": {
                        "raw_is_in_battle": final_battle["raw_is_in_battle"],
                        "terminal_result": final_battle["terminal_result"],
                        "phase": final_battle["phase"],
                    },
                    "primary_epoch": states[0]["epoch"],
                    "peer_epoch": states[1]["epoch"],
                },
                sort_keys=True,
            ),
            flush=True,
        )

        await client.eof()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", FAMILIES)
async def test_real_rom_mcp_status_move_returns_to_command_selection(tmp_path, version):
    """A real status move must close the bracket and reach the command menu.

    ``ResidualEffects1`` moves (Growl here) jump to ``JumpMoveEffect`` from
    ``ExecutePlayerMove``/``ExecuteEnemyMove`` and return from the effect
    handler, so the turn never reaches ``Execute*MoveDone``.  Before the
    observation covered those returns the bracket stayed open and the next
    command menu was reported as a contradiction
    (``phase=null``/``phase_valid=false``).  This regression settles one real
    shared Growl turn on the admitted pre-terminal pair with public input only
    and requires the ROM's own return boundary to be reported: the selected
    move, the PP decrement on that slot, unchanged HP, the lowered attack
    stage, a live command menu, ``ACTION_RESOLUTION`` observed while the move
    executed, and a valid ``COMMAND_SELECTION`` phase with the bracket closed.

    The admitted Red pair exposes no status move at any slot, so that family
    skips with the observed move list instead of claiming coverage it does not
    have; Blue and Yellow expose Growl at the same slot on both owners.
    """
    if os.environ.get("POKERED_SKIP_SHA1"):
        pytest.fail("POKERED_SKIP_SHA1 must not be used for real-ROM evidence")

    assets = _boundary_assets(version, "battle-pre-terminal")
    async with _stdio_server(tmp_path, assets, name="status-move") as client:
        await client.initialize()
        payload = base64.b64encode(assets["primary"]).decode("ascii")
        assert await client.tool("load_state", {"data": payload}) == {"ok": True}
        await _pair(client)
        boundary = await _states(client)
        slots = []
        for state in boundary:
            moves = _active_moves(state) or ()
            slots.append(
                next((slot for slot, move in enumerate(moves) if move == STATUS_MOVE_ID), None)
            )
        if any(slot is None for slot in slots):
            pytest.skip(
                "the admitted pre-terminal pair exposes no status move "
                f"{STATUS_MOVE_ID}: "
                + json.dumps([list(_active_moves(state) or ()) for state in boundary])
            )

        drive = await _drive_effect_turn(client, slots)
        observed = []
        for index, state in enumerate(drive["boundary"]):
            label = f"{version}-status-{index}"
            battle = _battle(state)
            assert _active_pp(state)[slots[index]] < drive["pp_before"][index][slots[index]], (
                label,
                state,
            )
            assert battle["player_selected_move"] == STATUS_MOVE_ID, (label, battle)
            assert battle["menu_open"] is True, (label, battle)
            assert battle["resolution_open"] is False, (label, battle)
            assert battle["phase"] == 2 and battle["phase_valid"] is True, (label, battle)
            assert "SelectMenuItem" in battle["phase_evidence"], (label, battle)
            # Growl deals no damage, so the HP the ROM reported before the turn
            # must be exactly what the post-turn read reports.
            assert _active_hp(state) == drive["hp_before"][index], (label, state)
            stages = battle["player_stat_stages"]
            assert isinstance(stages, dict) and stages.get("attack") == -1, (label, battle)
            # The early return is covered by real close labels, not only by the
            # menu reconciliation the parser applies on top of them.
            assert _RESOLUTION_CONTINUATION_LABELS <= set(battle["resolution_evidence"]), (
                label,
                battle,
            )
            observed.append(battle)
        # Positive coverage: the same turn really did report the resolution
        # phase while the move was executing, so closing it is an observation
        # and not a phase that was never derived at all.
        assert 3 in drive["phases_seen"], drive["phases_seen"]

        print(
            "MCP_BATTLE_BOUNDARY "
            + json.dumps(
                _boundary_payload(
                    "status_move",
                    version,
                    fixture=assets["row"]["path"],
                    fixture_sha1=assets["row"]["sha1"],
                    move=STATUS_MOVE_ID,
                    slots=list(slots),
                    drive_frames=drive["frames"],
                    phases_seen=list(drive["phases_seen"]),
                    observed=observed,
                ),
                sort_keys=True,
            ),
            flush=True,
        )

        await client.eof()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", FAMILIES)
async def test_real_rom_mcp_nonterminal_knockout_keeps_valid_replacement(tmp_path, version):
    """A live knockout must report a valid replacement with the bracket closed.

    A lethal hit returns from ``Execute*Move`` beside the faint check and jumps
    straight to ``Handle*MonFainted``, which runs ``ChooseNextMon`` before
    ``Execute*MoveDone`` would ever be reached.  This regression drives the
    admitted pre-terminal pair through the first knockout that opens the ROM's
    battle party menu, answers it with public input, and requires both sides of
    that nonterminal knockout to be reported from ROM-owned evidence: the live
    menu read must be ``FORCED_REPLACEMENT`` with ``phase_valid=true`` and the
    bracket closed, and the read after the replacement must show a healthy
    combatant still in the live battle with a valid phase.

    A family whose fixture never opens a replacement menu within the drive
    budget skips with that observation instead of reporting an empty set as
    coverage.
    """
    if os.environ.get("POKERED_SKIP_SHA1"):
        pytest.fail("POKERED_SKIP_SHA1 must not be used for real-ROM evidence")

    assets = _boundary_assets(version, "battle-pre-terminal")
    capture = assets["row"].get("capture")
    assert isinstance(capture, dict), assets["row"]
    plan = capture.get("terminal_turn_plan")
    assert isinstance(plan, list) and len(plan) == 2, plan
    slots = [entry["slot"] for entry in plan]
    assert all(type(slot) is int for slot in slots), plan

    async with _stdio_server(tmp_path, assets, name="nonterminal-knockout") as client:
        await client.initialize()
        payload = base64.b64encode(assets["primary"]).decode("ascii")
        assert await client.tool("load_state", {"data": payload}) == {"ok": True}
        await _pair(client)
        boundary = await _states(client)
        assert all(_battle(state)["raw_is_in_battle"] in BATTLE_KINDS for state in boundary), (
            boundary
        )

        drive = await _drive_through_live_replacement(client, slots)
        if not any(drive["opened"]):
            pytest.skip(
                "the admitted pre-terminal pair opened no live battle party menu "
                f"within {BOUNDARY_DRIVE_BUDGET} paired frames: "
                + json.dumps([state["battle"] for state in boundary])
            )

        assert drive["replacements"], drive
        for row in drive["replacements"]:
            assert row["raw_is_in_battle"] in BATTLE_KINDS, row
            assert row["phase"] == FORCED_REPLACEMENT_PHASE, row
            assert row["phase_valid"] is True, row
            assert row["resolution_open"] is False, row
            assert row["active_hp"] == 0, row
            assert any(hp > 0 for hp in row["party_hp"]), row
            assert "wPartyMenuTypeOrMessageID" in row["phase_evidence"], row
            assert _RESOLUTION_CONTINUATION_LABELS <= set(row["resolution_evidence"]), row

        answered = []
        for index, opened in enumerate(drive["opened"]):
            if not opened:
                continue
            row = drive["answered"][index]
            assert row is not None, (index, drive)
            # The knockout was nonterminal: the same owner is still in the
            # live battle with the replacement it sent out.
            assert row["raw_is_in_battle"] in BATTLE_KINDS, row
            assert (row["active_hp"] or 0) > 0, row
            assert row["phase_valid"] is True, row
            assert row["resolution_open"] is False, row
            answered.append(row)
        assert answered, drive

        print(
            "MCP_BATTLE_BOUNDARY "
            + json.dumps(
                _boundary_payload(
                    "nonterminal_knockout",
                    version,
                    fixture=assets["row"]["path"],
                    fixture_sha1=assets["row"]["sha1"],
                    plan=plan,
                    drive_frames=drive["frames"],
                    opened=list(drive["opened"]),
                    replacements=drive["replacements"],
                    answered=answered,
                ),
                sort_keys=True,
            ),
            flush=True,
        )

        await client.eof()


def _boundary_payload(kind, version, **fields):
    """Build one boundary evidence line payload."""
    return {"kind": kind, "version": version, "transport": "stdio_local_pair", **fields}


@pytest.mark.asyncio
@pytest.mark.parametrize("version", FAMILIES)
async def test_real_rom_mcp_boundary_reads_fail_closed(tmp_path, version):
    """Read the admitted forced-replacement and terminal pairs over stdio.

    ``cable_club-battle-faint.state`` is a real forced-replacement boundary:
    the owner's own combatant is at zero HP with living replacements left in
    the party, ``wInHandlePlayerMonFainted`` is set and the battle party menu
    is open.  ``cable_club-terminal.state`` is a real terminal return:
    ``wIsInBattle`` is zero after ``EndOfBattle`` while the pair still carries
    a non-zero ``wBattleResult`` on the owner whose combatant fainted.  Both
    pairs are admitted through fail-closed manifest rows and *loaded* with the
    public ``load_state`` tool, so neither session has observed a battle end
    here: the boundary evidence is reported (forced-replacement phase, inactive
    phase) and the leading non-zero outcome byte is never promoted to
    ``terminal_result``.
    """
    if os.environ.get("POKERED_SKIP_SHA1"):
        pytest.fail("POKERED_SKIP_SHA1 must not be used for real-ROM evidence")

    faint = _boundary_assets(version, "battle-faint")
    async with _stdio_server(tmp_path, faint, name="faint") as client:
        await client.initialize()
        payload = base64.b64encode(faint["primary"]).decode("ascii")
        assert await client.tool("load_state", {"data": payload}) == {"ok": True}
        primary, peer = await _states(client)
        _assert_battle_observations(primary, label=f"{version}-faint-primary")
        battle = _battle(primary)
        # A load cannot fabricate menu evidence, and the forced-replacement
        # signal itself is ROM-owned.
        assert battle["menu_open"] is None, battle
        assert battle["in_handle_player_mon_fainted"] != 0, battle
        assert "wInHandlePlayerMonFainted" in battle["phase_evidence"], battle
        # The derivation either names the documented forced-replacement phase
        # or fails closed on contradictory co-signals (a persisting action
        # flag); it must never name an unrelated phase.
        assert (battle["phase"], battle["phase_valid"]) in (
            (FORCED_REPLACEMENT_PHASE, True),
            (None, False),
        ), battle
        assert battle["raw_battle_result"] in (1, 2), battle
        assert battle["terminal_result"] is None, battle
        active = _active_mon(primary)
        assert active is not None and active["hp"] == 0, active
        party_hp = [mon["hp"] for mon in primary["party"]["mons"]]
        assert 0 in party_hp and any(hp > 0 for hp in party_hp), party_hp
        # The paired owner is still in the same live battle.
        peer_battle = _battle(peer)
        assert peer_battle["raw_is_in_battle"] in BATTLE_KINDS, peer_battle
        assert peer_battle["terminal_result"] is None, peer_battle
        print(
            "MCP_BATTLE_BOUNDARY "
            + json.dumps(
                _boundary_payload(
                    "forced_replacement",
                    version,
                    fixture=faint["row"]["path"],
                    fixture_sha1=faint["row"]["sha1"],
                    primary=battle,
                    peer=peer_battle,
                    party_hp=party_hp,
                ),
                sort_keys=True,
            ),
            flush=True,
        )

    terminal = _boundary_assets(version, "battle-terminal-return")
    async with _stdio_server(tmp_path, terminal, name="terminal") as client:
        await client.initialize()
        payload = base64.b64encode(terminal["primary"]).decode("ascii")
        assert await client.tool("load_state", {"data": payload}) == {"ok": True}
        primary, peer = await _states(client)
        observed = []
        for index, state in enumerate((primary, peer)):
            label = f"{version}-terminal-{index}"
            battle = _battle(state)
            assert battle["raw_is_in_battle"] == 0, (label, battle)
            assert battle["kind"] == 0, (label, battle)
            assert battle["phase"] == 0 and battle["phase_valid"] is True, (label, battle)
            assert "wIsInBattle" in battle["phase_evidence"], (label, battle)
            assert battle["menu_open"] is None, (label, battle)
            assert battle["enemy_mon"] is None, (label, battle)
            assert type(battle["raw_battle_result"]) is int, (label, battle)
            assert battle["terminal_result"] is None, (label, battle)
            observed.append(battle)
        # The fainted owner's outcome byte survived teardown, and the harness
        # still refuses to promote it: promotion requires an observed battle
        # end in this session, which a load cannot provide.
        raw_results = [battle["raw_battle_result"] for battle in observed]
        assert any(value != 0 for value in raw_results), raw_results
        print(
            "MCP_BATTLE_BOUNDARY "
            + json.dumps(
                _boundary_payload(
                    "terminal_loaded",
                    version,
                    fixture=terminal["row"]["path"],
                    fixture_sha1=terminal["row"]["sha1"],
                    primary=observed[0],
                    peer=observed[1],
                    raw_results=raw_results,
                ),
                sort_keys=True,
            ),
            flush=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("version", FAMILIES)
async def test_real_rom_mcp_terminal_return_drive_reads(tmp_path, version):
    """Drive the admitted pre-terminal pair into the terminal return.

    ``cable_club-pre-terminal.state`` is captured at the last ROM command
    boundary before the deciding knockout.  The test loads the pair through the
    public surface and drives it to ``EndOfBattle`` with
    ``press``/``link_peer_press``/``link_step`` calls only, taking every
    decision from a ``pokered://game-state`` read: both owners are folded back
    into their own command/move menus using the move slots the capture
    recorded, and the forced replacement the deciding knockout can open is
    answered from the party HP list the payload exposes.  The drive therefore
    covers the ROM turns between that boundary and the terminal return rather
    than a single exchange, and its bounded frame count is reported.  The first
    post-battle resource read for each owner must report ``TERMINAL_RETURN``
    with its documented validity and the ``wIsInBattle`` + ``wBattleResult``
    evidence, promote a surviving non-zero outcome byte to ``terminal_result``,
    and the next read must report the plain ``INACTIVE`` phase with no terminal
    result (the documented single-shot falling edge).
    """
    if os.environ.get("POKERED_SKIP_SHA1"):
        pytest.fail("POKERED_SKIP_SHA1 must not be used for real-ROM evidence")

    assets = _boundary_assets(version, "battle-pre-terminal")
    capture = assets["row"].get("capture")
    assert isinstance(capture, dict), assets["row"]
    plan = capture.get("terminal_turn_plan")
    assert isinstance(plan, list) and len(plan) == 2, plan
    assert all(type(entry["slot"]) is int for entry in plan), plan

    async with _stdio_server(tmp_path, assets, name="pre-terminal") as client:
        await client.initialize()
        payload = base64.b64encode(assets["primary"]).decode("ascii")
        assert await client.tool("load_state", {"data": payload}) == {"ok": True}
        # The pair is reloaded into an in-progress Cable Club battle, so the
        # serial bridge must be installed before the deciding turn is driven:
        # the two ROMs exchange the turn over the link cable, exactly as the
        # capture did.
        await _pair(client)
        boundary = await _states(client)
        for index, state in enumerate(boundary):
            label = f"{version}-boundary-{index}"
            battle = _battle(state)
            assert battle["raw_is_in_battle"] in BATTLE_KINDS, (label, battle)
            assert battle["terminal_result"] is None, (label, battle)
            assert battle["menu_open"] is None, (label, battle)
            # The pair really is at a ROM command boundary (FIGHT/ITEM), which
            # is what makes the recorded move plan replayable.
            assert _command_menu_ready(state), (label, state["menu"])
            active = _active_mon(state)
            assert active is not None and active["hp"] > 0, (label, active)
            # A healthy combatant at the command menu must not be reported as a
            # forced replacement.  The ROM leaves
            # ``wInHandlePlayerMonFainted`` set after ``ChooseNextMon`` returns
            # to the battle loop, so a derivation that trusted that byte alone
            # named phase 4 here for all three games (Red 35 HP, Blue 9 HP,
            # Yellow 102 HP).
            assert battle["phase"] != FORCED_REPLACEMENT_PHASE, (label, battle)
        _log(
            "boundary_reload",
            f"version={version} fixture={assets['row']['path']} plan={plan}",
        )

        drive = await _drive_boundary_turn(client, plan)
        terminal = [entry["state"] for entry in drive["terminal"]]
        observed = [
            _assert_terminal_return(state, label=f"{version}-terminal-{index}")
            for index, state in enumerate(terminal)
        ]
        raw_results = [battle["raw_battle_result"] for battle in observed]
        # At least one owner's non-zero outcome byte survived the teardown, so
        # this is a genuine confirmed outcome and not an ambiguous zero.
        assert any(value != 0 for value in raw_results), raw_results

        # The falling edge is single-shot: the next read of each owner is the
        # documented plain INACTIVE phase with no terminal result.
        follow_up = await _states(client)
        follow_up_battles = []
        for index, state in enumerate(follow_up):
            label = f"{version}-follow-up-{index}"
            battle = _battle(state)
            assert battle["raw_is_in_battle"] == 0, (label, battle)
            assert battle["kind"] == 0, (label, battle)
            assert battle["phase"] == 0 and battle["phase_valid"] is True, (label, battle)
            assert battle["terminal_result"] is None, (label, battle)
            follow_up_battles.append(battle)

        print(
            "MCP_BATTLE_BOUNDARY "
            + json.dumps(
                _boundary_payload(
                    "terminal_drive",
                    version,
                    fixture=assets["row"]["path"],
                    fixture_sha1=assets["row"]["sha1"],
                    boundary_turn=capture.get("boundary_turn"),
                    terminal_turn=capture.get("terminal_turn"),
                    plan=plan,
                    drive_frames=drive["frames"],
                    injected={
                        str(index): list(buttons) for index, buttons in enumerate(drive["injected"])
                    },
                    boundary=[state["battle"] for state in boundary],
                    terminal=[
                        dict(battle, frames=drive["terminal"][index]["frames"])
                        for index, battle in enumerate(observed)
                    ],
                    follow_up=follow_up_battles,
                ),
                sort_keys=True,
            ),
            flush=True,
        )

        await client.eof()
