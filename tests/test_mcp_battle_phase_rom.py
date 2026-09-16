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

Forced replacement and a terminal return are not reachable from these
fixtures within a reasonable finite bound (six full-HP party mons, same-type
reduced damage and limited lead-move PP); the test therefore asserts the
documented fail-closed behavior instead of fabricating a terminal result: the
battle stays active and ``terminal_result`` stays ``None``.  A normal FIGHT
move also leaves ``wActionResultOrTookBattleTurn`` at zero (``ExecutePlayerMoveDone``
resets it), so ``ACTION_RESOLUTION`` is correctly never derived for this turn;
the test asserts that fail-closed outcome and records every observed phase.

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
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from scripts.probe_timed_rom_pair import resolve_assets
from tests._rom_assets import fixture_path, rom_path, sym_path
from tests.test_mcp_timed_rom import PIPE_CAP, RomClient
from tests.test_mcp_timed_stdio import CALL_BOUND

ROOT = Path(__file__).resolve().parents[1]
FAMILIES = ("red_color", "blue_color", "yellow")

# Finite frame budgets.  Each is a hard cap on emulated frames for one phase;
# exceeding it fails the phase instead of spinning forever.
STEP_CHUNK = 20
RECEPTIONIST_WALK_FRAMES = 60
LINK_MENU_BUDGET = 2400
LINK_MENU_CURSOR_BUDGET = 400
COLOSSEUM_WARP_BUDGET = 2400
BATTLE_ENTRY_BUDGET = 2400
BATTLE_MENU_BUDGET = 2400
# Move settlement is driven through the public link tools only.  The turn
# itself (move text, animation, damage, return to the next command menu) is a
# bounded number of paired frames; the boundary wait below is a separate finite
# cap so a settled turn is not mistaken for a stalled transport.
SETTLEMENT_BUDGET = 2400
SETTLEMENT_STEP = 4
# One A press per side at most this often; the ROM's HandleMenuInput needs a
# fresh joypad sample and a single queued event can be lost to a transition.
SETTLEMENT_INPUT_SPACING = 8
BOUNDARY_BUDGET = 1200
# The battle command menu stores wMaxMenuItem == 1; the move menu stores
# wNumMovesMinusOne + 2 (>= 2 for any legal moveset).
COMMAND_MENU_MAX_ITEM = 1
TERMINAL_RETURN_PHASE = 5
# An outstanding paired request is large enough to still be in flight after a
# short cancellation delay (measured ~0.22 s/frame on this host) yet small
# enough that the server's eventual response is drained within CALL_BOUND.
OUTSTANDING_STEP_FRAMES = 20
OUTSTANDING_CANCEL_DELAY = 0.2
# A concurrent resource read is issued while a long finite ``link_step`` is
# still outstanding.  The read serializes on the session emulator lock, so it
# returns the coherent post-step snapshot (or the pre-step snapshot if it wins
# the race); the tick is one of the two boundary values, never torn.
CONCURRENT_STEP_FRAMES = 40
# A server-visible cancellation reuses the public MCP cancellation
# notification with the outstanding request id.  The count is large enough to
# guarantee the request is still in flight when the notification arrives, yet
# bounded so the shielded emulator worker (and the read that follows it)
# settles within the transport deadline.
SERVER_CANCEL_STEP_FRAMES = 40
CANCEL_REASON = "battle-state cancellation coverage"

LINK_MENU_MAX_ITEMS = (2, 3)
MENU_WATCHED_A = 0x01
CABLE_CLUB_MAP_ID = 64
COLOSSEUM_MAP_ID = 0xF0
BATTLE_KINDS = (1, 2)
# A short staggered A burst advances the receptionist dialog; a longer
# input-free window then lets the LinkMenu appear without a pending A press
# selecting its default (TRADE_CENTER) entry.
LINK_MENU_BURST_ATTEMPTS = 1
LINK_MENU_QUIET_FRAMES = 100

# The only execution hooks the server installs for battle observation; the
# test never registers any hook of its own.
OBSERVATIONAL_MENU_HOOKS = frozenset(
    {
        "SelectMenuItem",
        "DisplayBattleMenu.handleBattleMenuInput",
        "MainInBattleLoop",
        "MainInBattleLoop.selectEnemyMove",
        "LoadScreenTilesFromBuffer1",
    }
)
PHASE_EVIDENCE_SYMBOLS = frozenset(
    {
        "wIsInBattle",
        "wBattleResult",
        "wInHandlePlayerMonFainted",
        "wMoveMenuType",
        "wPlayerMoveListIndex",
        "wActionResultOrTookBattleTurn",
    }
)
# Public MCP tools the test is permitted to call.  There is no memory, RAM,
# register, hook-registration, or hash-bypass tool in the surface.
PUBLIC_TOOLS = frozenset(
    {
        "load_state",
        "save_state",
        "press",
        "release",
        "step",
        "link_pair",
        "link_unpair",
        "link_step",
        "link_peer_press",
    }
)
BATTLE_TRANSIENT_FIELDS = (
    "move_menu_type",
    "player_move_list_index",
    "current_menu_item",
    "player_selected_move",
    "enemy_selected_move",
    "action_result_or_took_turn",
    "in_handle_player_mon_fainted",
)

CHILD = r"""
import asyncio
import contextlib
import sys
from pathlib import Path


async def _serve():
    repo = Path(sys.argv[1])
    family = sys.argv[2]
    primary_rom = Path(sys.argv[3])
    primary_sym = Path(sys.argv[4])
    peer_rom = Path(sys.argv[5])
    peer_sym = Path(sys.argv[6])
    peer_state = Path(sys.argv[7])
    with contextlib.redirect_stdout(sys.stderr):
        sys.path.insert(0, str(repo / "src"))
        from pokered_harness.config import load_versions
        from pokered_harness.mcp_server import register_default_hooks, serve_stdio
        from pokered_harness.session import Session

        pins = load_versions(repo / "VERSIONS.md")

        def _open(rom, sym):
            return Session.from_files(
                rom,
                sym,
                expected_rom_sha1=pins.sha1_for_path(rom),
                expected_symbol_sha1=pins.symbol_sha1_for_path(sym),
                expected_pyboy_version=pins.pyboy_version,
                expected_pyboy_revision=pins.pyboy_revision,
            )

        primary = _open(primary_rom, primary_sym)
        peer = _open(peer_rom, peer_sym)
        register_default_hooks(primary)
        primary.enable_battle_menu_observation()
        register_default_hooks(peer)
        peer.enable_battle_menu_observation()
        peer.load_state(peer_state.read_bytes())
    try:
        await serve_stdio(
            primary,
            peer_session=peer,
            primary_version=family,
            peer_version=family,
        )
    finally:
        with contextlib.redirect_stdout(sys.stderr):
            primary.close()
            peer.close()


asyncio.run(_serve())
"""


def _battle_assets(version):
    """Return validated pinned assets plus the immutable battle fixture bytes."""
    family = version.split("_")[0]
    paths = [
        rom_path(family, color=family != "yellow", project_root=ROOT),
        sym_path(family, ROOT),
        fixture_path(family, "cable_club.state", project_root=ROOT),
        fixture_path(family, "cable_club-battle.state", project_root=ROOT),
    ]
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        pytest.skip("missing BYO battle assets: " + ", ".join(missing))
    # resolve_assets validates the pinned ROM/SYM and the ordinary fixture
    # against the canonical registry; the caller only needs the battle bytes.
    assets = resolve_assets(version, ROOT)
    assets["state"] = fixture_path(
        family, "cable_club-battle.state", project_root=ROOT
    ).read_bytes()
    return assets


@asynccontextmanager
async def _stdio_server(tmp_path, asset):
    """Launch one stdio server with an in-process peer on the battle fixture."""
    family = asset["family"]
    directory = tmp_path / "server"
    directory.mkdir()
    copied = {}
    for name in ("rom", "sym"):
        copied[name] = directory / asset[name].name
        shutil.copyfile(asset[name], copied[name])
    peer_state = directory / "peer-cable_club-battle.state"
    peer_state.write_bytes(asset["state"])
    env = {key: value for key, value in os.environ.items() if not key.startswith("POKERED_")}
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT / "vendor/pyboy-src")))
    env["PYTHONUNBUFFERED"] = "1"
    client = None
    try:
        process = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-u",
                "-c",
                CHILD,
                str(ROOT),
                family,
                str(copied["rom"]),
                str(copied["sym"]),
                str(copied["rom"]),
                str(copied["sym"]),
                str(peer_state),
                cwd=ROOT,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=PIPE_CAP,
                start_new_session=True,
            ),
            timeout=30.0,
        )
        client = RomClient(process)
        yield client
    finally:
        if client is not None:
            await client.cleanup()


async def _request(client, uri):
    resource = await client.request("resources/read", {"uri": uri})
    return json.loads(resource["contents"][0]["text"])


async def _states(client):
    # One outstanding request per stdio client: read the primary and peer
    # resources sequentially rather than concurrently on the same stream.
    primary = await _request(client, "pokered://game-state")
    peer = await _request(client, "pokered://peer-game-state")
    return [primary, peer]


def _without_epoch(payload):
    return {key: value for key, value in payload.items() if key != "epoch"}


def _log(phase, detail):
    print(f"MCP_BATTLE_PHASE {phase} {detail}", flush=True)


def _menu(state):
    menu = state["menu"]
    assert isinstance(menu, dict), state
    return menu


def _battle(state):
    battle = state["battle"]
    assert isinstance(battle, dict), state
    return battle


def _menu_ready(state, max_items):
    menu = _menu(state)
    watched = menu["watched_keys"]
    current = menu["current_item"]
    return (
        type(watched) is int
        and watched & MENU_WATCHED_A == MENU_WATCHED_A
        and menu["max_item"] in max_items
        and type(current) is int
    )


def _link_menu_ready(state):
    return state["overworld"]["map_id"] == CABLE_CLUB_MAP_ID and _menu_ready(
        state, LINK_MENU_MAX_ITEMS
    )


async def _link_step(client, frames):
    """Step the local pair, keeping each RPC below the transport bound."""
    remaining = frames
    while remaining > 0:
        step = min(STEP_CHUNK, remaining)
        await client.tool("link_step", {"count": step})
        remaining -= step


async def _press(client, button, *, duration=4):
    await client.tool("press", {"button": button, "duration": duration})


async def _peer_press(client, button, *, duration=4):
    await client.tool("link_peer_press", {"button": button, "duration": duration})


async def _press_both(client, button, *, duration=4):
    await _press(client, button, duration=duration)
    await _peer_press(client, button, duration=duration)


async def _advance_until(client, predicate, *, budget, prompt):
    """Advance the pair in bounded chunks until ``predicate`` holds."""
    spent = 0
    while spent < budget:
        states = await _states(client)
        if all(predicate(state) for state in states):
            return states, spent
        await _link_step(client, min(STEP_CHUNK, budget - spent))
        spent += STEP_CHUNK
    states = await _states(client)
    raise AssertionError(
        f"{prompt}: phase budget {budget} frames exhausted; states={json.dumps(states)}"
    )


async def _mash_until(client, predicate, *, budget, prompt):
    """A-mash the pair while advancing, until ``predicate`` holds."""
    spent = 0
    while spent < budget:
        states = await _states(client)
        if all(predicate(state) for state in states):
            return states, spent
        await _press_both(client, "a")
        await _link_step(client, min(STEP_CHUNK, budget - spent))
        spent += STEP_CHUNK
    states = await _states(client)
    raise AssertionError(
        f"{prompt}: phase budget {budget} frames exhausted; states={json.dumps(states)}"
    )


async def _advance_pair_primary_until(client, predicate, *, budget, prompt):
    """Advance the still-paired link through ``link_step`` until the primary
    state satisfies ``predicate``.

    Ordinary paired stepping must preserve the battle/menu observations the
    execution hooks record: ``link_step`` advances bookkeeping without starting
    a new observation epoch.  This enters the FIGHT -> move menu through the
    ROM's own input handling while the pair remains connected.
    """
    spent = 0
    last_press = -100
    while spent < budget:
        state = await _request(client, "pokered://game-state")
        if predicate(state):
            return state, spent
        if spent - last_press >= 16:
            await _press(client, "a", duration=2)
            last_press = spent
        await _link_step(client, 4)
        spent += 4
    state = await _request(client, "pokered://game-state")
    raise AssertionError(f"{prompt}: budget {budget} frames; state={json.dumps(state)}")


def _active_mon(state):
    """Return the parsed ``party.active_mon`` dict, or ``None`` when absent."""
    party = state.get("party")
    if not isinstance(party, dict):
        return None
    mon = party.get("active_mon")
    return mon if isinstance(mon, dict) else None


def _active_pp(state):
    mon = _active_mon(state)
    if mon is None:
        return None
    pp = mon.get("pp")
    return tuple(pp) if isinstance(pp, (list, tuple)) else None


def _active_moves(state):
    mon = _active_mon(state)
    if mon is None:
        return None
    moves = mon.get("moves")
    return tuple(moves) if isinstance(moves, (list, tuple)) else None


def _active_hp(state):
    mon = _active_mon(state)
    if mon is None:
        return None
    hp = mon.get("hp")
    return hp if type(hp) is int else None


def _pp_decrements(before, after):
    """Return the move slots whose PP strictly decreased, or ``None`` if the
    before/after PP tuples are not both available."""
    if before is None or after is None or len(before) != len(after):
        return None
    return tuple(index for index in range(len(before)) if after[index] < before[index])


def _menu_awaiting_input(state):
    """True when the ROM's command or move menu is open and its cursor bytes
    are integer-shaped (a live HandleMenuInput loop, not a stale draw)."""
    battle = _battle(state)
    if battle.get("menu_open") is not True:
        return False
    menu = state.get("menu")
    if not isinstance(menu, dict):
        return False
    return type(menu.get("current_item")) is int and type(menu.get("max_item")) is int


def _at_command_boundary(state):
    """True when the primary is back at the FIGHT/ITEM command menu."""
    battle = _battle(state)
    if battle.get("menu_open") is not True:
        return False
    menu = state.get("menu")
    return (
        isinstance(menu, dict)
        and type(menu.get("max_item")) is int
        and menu["max_item"] <= COMMAND_MENU_MAX_ITEM
    )


async def _settle_selected_move(client, *, before, budget=SETTLEMENT_BUDGET):
    """Drive the paired ROMs through one move selection -> resolution cycle.

    Both combatants must select a move before the link exchange can complete,
    so each side is advanced toward its own menu with at most one A press per
    ``SETTLEMENT_INPUT_SPACING`` frames.  The loop stops as soon as *both*
    active mons show a PP decrement (the ROM executes ``DecrementPP`` before
    damage, so this is a strict settlement signal) and then waits, input-free,
    for the primary to return to a command/next input boundary.

    Returns the pre/post PP tuples, the decremented slots, the selected move
    bytes, every valid phase and action-flag value observed during the turn,
    and both states captured at the final boundary.
    """
    labels = len(before)
    if labels != 2:
        raise ValueError("settlement expects the primary and peer states")
    pp_before = tuple(_active_pp(state) for state in before)
    decremented = [None] * labels
    selected = [None] * labels
    phases_seen = set()
    actions_seen = set()
    terminal_observations = []
    raw_values = set()
    spent = 0
    next_input = [-SETTLEMENT_INPUT_SPACING] * labels
    while spent < budget:
        states = await _states(client)
        for index, state in enumerate(states):
            battle = _battle(state)
            raw_values.add(battle.get("raw_is_in_battle"))
            if battle.get("phase_valid") is True:
                phases_seen.add(battle.get("phase"))
            if battle.get("phase") == TERMINAL_RETURN_PHASE and not terminal_observations:
                terminal_observations.append(
                    {
                        "side": index,
                        "raw_is_in_battle": battle.get("raw_is_in_battle"),
                        "raw_battle_result": battle.get("raw_battle_result"),
                        "terminal_result": battle.get("terminal_result"),
                        "tick": state["epoch"]["tick"],
                    }
                )
            actions_seen.add(battle.get("action_result_or_took_turn"))
            if decremented[index] is None:
                changed = _pp_decrements(pp_before[index], _active_pp(state))
                if changed:
                    decremented[index] = changed
                    selected[index] = battle.get("player_selected_move")
        if all(changed is not None for changed in decremented):
            break
        for index, state in enumerate(states):
            if decremented[index] is not None:
                continue
            if not _menu_awaiting_input(state):
                continue
            if spent < next_input[index]:
                continue
            if index == 0:
                await _press(client, "a", duration=4)
            else:
                await _peer_press(client, "a", duration=4)
            next_input[index] = spent + SETTLEMENT_INPUT_SPACING
        await _link_step(client, SETTLEMENT_STEP)
        spent += SETTLEMENT_STEP
    if not all(changed is not None for changed in decremented):
        raise AssertionError(
            "selected move never settled (PP decrement not observed): "
            f"before={pp_before} frames={spent}"
        )

    boundary = None
    boundary_frames = 0
    while boundary_frames < BOUNDARY_BUDGET:
        states = await _states(client)
        if _battle(states[0]).get("raw_is_in_battle") not in BATTLE_KINDS:
            boundary = states
            break
        if _at_command_boundary(states[0]):
            boundary = states
            break
        await _link_step(client, SETTLEMENT_STEP)
        boundary_frames += SETTLEMENT_STEP
    if boundary is None:
        states = await _states(client)
        raise AssertionError(
            "settled turn never returned to a command/next boundary: "
            f"{json.dumps([_battle(state) for state in states])}"
        )
    return {
        "frames": spent,
        "boundary_frames": boundary_frames,
        "pp_before": pp_before,
        "pp_after": tuple(_active_pp(state) for state in boundary),
        "decremented": tuple(decremented),
        "selected": tuple(selected),
        "phases_seen": tuple(sorted(value for value in phases_seen if value is not None)),
        "actions_seen": tuple(sorted(value for value in actions_seen if value is not None)),
        "raw_values": tuple(sorted(value for value in raw_values if value is not None)),
        "terminal_observations": terminal_observations,
        "boundary": boundary,
    }


async def _send_raw_request(client, method, params):
    """Send one JSON-RPC request and return its id without awaiting a reply."""
    client.sequence += 1
    request_id = client.sequence
    await client.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
    return request_id


async def _read_response_for(client, request_id, *, bound):
    """Read stdout until the response for ``request_id`` arrives (or fail)."""
    async with asyncio.timeout(bound):
        while True:
            line = await client.process.stdout.readline()
            assert line, f"server EOF while awaiting response {request_id}"
            text = line.decode("utf-8", "replace").strip()
            if not text or not text.startswith("{"):
                # Ignore blank or non-protocol diagnostic lines; JSON-RPC is
                # line-delimited and only objects carry responses.
                continue
            response = json.loads(text)
            assert response.get("jsonrpc") == "2.0", response
            if "id" not in response:
                continue
            if response["id"] == request_id:
                return response


async def _read_responses_for(client, request_ids, *, bound):
    """Read stdout until every request id in ``request_ids`` has a reply.

    Requests are written before any reply is read, so the server handles them
    concurrently; replies are matched by id rather than arrival order.
    """
    pending = set(request_ids)
    responses = {}
    async with asyncio.timeout(bound):
        while pending:
            line = await client.process.stdout.readline()
            assert line, f"server EOF while awaiting responses {sorted(pending)}"
            text = line.decode("utf-8", "replace").strip()
            if not text or not text.startswith("{"):
                continue
            response = json.loads(text)
            assert response.get("jsonrpc") == "2.0", response
            if "id" not in response or response["id"] not in pending:
                continue
            pending.discard(response["id"])
            responses[response["id"]] = response
    return responses


async def _read_state_during_outstanding_step(client, *, frames, label):
    """Read ``pokered://game-state`` while a long ``link_step`` is outstanding.

    Both public requests are written before either reply is read.  The resource
    read serializes on the same session emulator lock the paired step holds, so
    it observes a coherent pre- or post-step snapshot.  The returned ``tick`` is
    asserted to be exactly one of those two boundary values, never a torn
    mid-step value, and the documented battle fields are validated.
    """
    before = await _request(client, "pokered://game-state")
    before_tick = before["epoch"]["tick"]
    step_id = await _send_raw_request(
        client, "tools/call", {"name": "link_step", "arguments": {"count": frames}}
    )
    read_id = await _send_raw_request(client, "resources/read", {"uri": "pokered://game-state"})
    responses = await _read_responses_for(client, (step_id, read_id), bound=CALL_BOUND)
    step_response = responses[step_id]
    read_response = responses[read_id]
    assert "error" not in step_response, step_response
    assert "error" not in read_response, read_response
    step_result = json.loads(step_response["result"]["content"][0]["text"])
    snapshot = json.loads(read_response["result"]["contents"][0]["text"])
    assert isinstance(step_result, dict), step_result
    assert isinstance(snapshot, dict), snapshot
    tick = snapshot["epoch"]["tick"]
    assert tick in (before_tick, before_tick + frames), (before_tick, tick, frames, snapshot)
    _assert_battle_observations(snapshot, label=label)
    _log(
        "concurrent_read",
        f"step_id={step_id} read_id={read_id} frames={frames} tick={before_tick}->{tick}",
    )
    return {
        "step_id": step_id,
        "read_id": read_id,
        "before_tick": before_tick,
        "tick": tick,
        "step_result": step_result,
        "snapshot": snapshot,
    }


async def _server_visible_cancel(client, *, frames, label):
    """Cancel an outstanding public ``link_step`` with ``notifications/cancelled``.

    Protocol messages used (exact JSON-RPC):

    * request: ``{"jsonrpc": "2.0", "id": <id>, "method": "tools/call",
      "params": {"name": "link_step", "arguments": {"count": frames}}}``
    * cancel: ``{"jsonrpc": "2.0", "method": "notifications/cancelled",
      "params": {"requestId": <id>, "reason": <reason>}}``

    The SDK receive loop marks the in-flight request cancelled, cancels the
    ``_call_tool`` task and answers with the bounded JSON-RPC error
    ``{"code": 0, "message": "Request cancelled"}``.  The server's
    ``_await_blocking_task`` shield keeps the thread-backed emulator worker
    alive, so the full finite count still completes; the follow-up read
    serializes behind it and proves the tick advanced by exactly ``frames``.
    """
    before = await _request(client, "pokered://game-state")
    before_tick = before["epoch"]["tick"]
    request_id = await _send_raw_request(
        client, "tools/call", {"name": "link_step", "arguments": {"count": frames}}
    )
    # Let the server admit the request (its SDK request-task is created) before
    # the cancellation notification arrives; the count keeps it outstanding.
    await asyncio.sleep(OUTSTANDING_CANCEL_DELAY)
    await client.send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": request_id, "reason": CANCEL_REASON},
        }
    )
    response = await _read_response_for(client, request_id, bound=CALL_BOUND)
    assert "error" in response, response
    error = response["error"]
    assert error["code"] == 0, error
    assert error["message"] == "Request cancelled", error
    # The read below serializes behind the cancelled-but-shielded worker, so
    # its coherent snapshot proves the worker was not orphaned mid-step.
    after = await _request(client, "pokered://game-state")
    after_tick = after["epoch"]["tick"]
    assert after_tick == before_tick + frames, (before_tick, after_tick, frames)
    _assert_battle_observations(after, label=label)
    _log(
        "server_cancellation",
        f"request_id={request_id} frames={frames} "
        f"tick={before_tick}->{after_tick} error={error['message']}",
    )
    return {
        "request_id": request_id,
        "error": error,
        "before_tick": before_tick,
        "after_tick": after_tick,
        "state": after,
    }


async def _pair(client):
    paired = await client.tool("link_pair")
    assert paired["paired"] is True, paired
    status = await client.tool("link_status")
    assert status["link_backend"] == "bit_accurate", status
    assert status["paired"] is True, status


async def _drive_to_link_menu(client):
    for _ in range(3):
        await _press_both(client, "up", duration=6)
        await _link_step(client, RECEPTIONIST_WALK_FRAMES)
    spent = 0
    while spent < LINK_MENU_BUDGET:
        states = await _states(client)
        if all(_link_menu_ready(state) for state in states):
            _log("link_menu", f"frames={spent}")
            return states
        # Burst: stagger the ordinary public A input exactly as the proven
        # local driver does (primary A, four frames, peer A, sixteen frames).
        for _ in range(LINK_MENU_BURST_ATTEMPTS):
            await _press(client, "a", duration=4)
            await _link_step(client, 4)
            await _peer_press(client, "a", duration=4)
            await _link_step(client, 16)
            spent += 20
        # Quiet window: no public input, so a LinkMenu that finishes drawing
        # here cannot select its default entry.
        for _ in range(LINK_MENU_QUIET_FRAMES // STEP_CHUNK):
            await _link_step(client, STEP_CHUNK)
            spent += STEP_CHUNK
            states = await _states(client)
            if all(_link_menu_ready(state) for state in states):
                _log("link_menu", f"frames={spent}")
                return states
    states = await _states(client)
    raise AssertionError(
        f"LinkMenu never reached on both peers: budget {LINK_MENU_BUDGET} "
        f"frames exhausted; states={json.dumps(states)}"
    )


async def _select_colosseum(client):
    # Let the pair settle on the freshly drawn LinkMenu before moving cursors.
    await _link_step(client, 60)
    spent = 60
    while spent < LINK_MENU_CURSOR_BUDGET:
        states = await _states(client)
        if all(_menu(state)["current_item"] == 1 for state in states):
            break
        primary_cursor = _menu(states[0])["current_item"]
        peer_cursor = _menu(states[1])["current_item"]
        if primary_cursor != 1:
            await _press(client, "down" if primary_cursor < 1 else "up", duration=12)
        if peer_cursor != 1:
            await _peer_press(client, "down" if peer_cursor < 1 else "up", duration=12)
        await _link_step(client, 16)
        spent += 16
    states = await _states(client)
    assert all(_menu(state)["current_item"] == 1 for state in states), states
    await _press_both(client, "a")
    states, spent = await _mash_until(
        client,
        lambda state: state["overworld"]["map_id"] == COLOSSEUM_MAP_ID,
        budget=COLOSSEUM_WARP_BUDGET,
        prompt="COLOSSEUM warp never reached on both peers",
    )
    _log("colosseum", f"frames={spent}")
    return states


async def _enter_battle(client):
    # The first-attached primary is the local-pair clock master, so it walks
    # right onto the hidden-event tile; the peer follows left.  Step one frame
    # at a time across the trigger so the paired CPUs stay aligned.
    for _ in range(4):
        states = await _states(client)
        if all(_battle(state)["raw_is_in_battle"] in BATTLE_KINDS for state in states):
            break
        await _press(client, "right", duration=8)
        await _peer_press(client, "left", duration=8)
        await _link_step(client, 8)
        await _link_step(client, 12)
    states, spent = await _mash_until(
        client,
        lambda state: _battle(state)["raw_is_in_battle"] in BATTLE_KINDS,
        budget=BATTLE_ENTRY_BUDGET,
        prompt="link battle never became active on both peers",
    )
    _log("battle_active", f"frames={spent}")
    return states


def _assert_battle_observations(state, *, label):
    """Assert the documented additive schema and validity for one peer."""
    battle = _battle(state)
    menu = _menu(state)

    # Identity/enemy: the ROM-owned enemy combatant must be present and valid.
    assert battle["raw_is_in_battle"] in BATTLE_KINDS, (label, battle)
    assert battle["kind"] in BATTLE_KINDS, (label, battle)
    enemy = battle["enemy_mon"]
    assert isinstance(enemy, dict), (label, battle)
    assert enemy["species"] not in (0, 0xFF), (label, enemy)
    assert enemy["max_hp"] > 0, (label, enemy)
    assert 0 < enemy["hp"] <= enemy["max_hp"], (label, enemy)
    assert 0 <= enemy["slot"] < 6, (label, enemy)
    assert battle["enemy_mon_valid"] is True, (label, battle)

    # Raw result is always exposed; nothing here proves a terminal outcome.
    assert type(battle["raw_battle_result"]) is int, (label, battle)
    assert battle["terminal_result"] is None, (label, battle)

    # Derived phase: documented validity, and command_selection only from the
    # session's observational menu hooks.
    assert battle["phase"] in (0, 1, 2, 3, 4, 5, 6), (label, battle)
    assert type(battle["phase_valid"]) is bool, (label, battle)
    evidence = battle["phase_evidence"]
    assert isinstance(evidence, list) and evidence, (label, battle)
    assert set(evidence) <= OBSERVATIONAL_MENU_HOOKS | PHASE_EVIDENCE_SYMBOLS, (
        label,
        battle,
    )

    # Menu/transient fields are present with their documented validity.
    assert battle["menu_open"] in (True, False, None), (label, battle)
    assert set(battle["menu_evidence"]) <= OBSERVATIONAL_MENU_HOOKS, (label, battle)
    for field in BATTLE_TRANSIENT_FIELDS:
        assert field in battle, (label, field, battle)
    assert menu["current_item"] is not None, (label, menu)
    assert menu["max_item"] is not None, (label, menu)
    assert isinstance(state["epoch"], dict), (label, state)
    assert state["epoch"]["session_id"], (label, state)


async def _prelink(client, asset):
    """Load the primary battle fixture and prove the public surface is live."""
    await client.initialize()
    listed = await client.request("tools/list", {})
    tool_names = {row["name"] for row in listed["tools"]}
    assert PUBLIC_TOOLS <= tool_names, tool_names
    # No RAM/hash/hook tool exists in the public surface.
    assert not any("hash" in name or "memory" in name or "hook" in name for name in tool_names), (
        tool_names
    )
    resources = await client.request("resources/list", {})
    assert {
        "pokered://game-state",
        "pokered://peer-game-state",
        "pokered://link-status",
    } <= {row["uri"] for row in resources["resources"]}
    loaded = await client.tool(
        "load_state", {"data": base64.b64encode(asset["state"]).decode("ascii")}
    )
    assert loaded == {"ok": True}, loaded
    before = await _states(client)
    for state, label in zip(before, ("primary", "peer"), strict=True):
        battle = _battle(state)
        # The pristine fixture is out of battle; the live enemy-mon bytes are
        # stale and must not be reported as live state.
        assert battle["raw_is_in_battle"] == 0, (label, battle)
        assert battle["kind"] == 0, (label, battle)
        assert battle["enemy_mon"] is None, (label, battle)
        assert battle["phase"] == 0 and battle["phase_valid"] is True, (label, battle)
        assert battle["terminal_result"] is None, (label, battle)
        # A load resets the observational menu state to unknown, never
        # fabricated.
        assert battle["menu_open"] is None, (label, battle)
        assert set(battle["menu_evidence"]) <= OBSERVATIONAL_MENU_HOOKS, (label, battle)
    saved = await client.tool("save_state")
    assert set(saved) == {"data"} and isinstance(saved["data"], str), saved
    assert base64.b64decode(saved["data"], validate=True)
    assert await client.tool("press", {"button": "a", "duration": 1}) == {"ok": True}
    assert await client.tool("release", {"button": "a"}) == {"ok": True}
    await client.tool("step", {"count": 2})
    assert await client.tool("load_state", saved) == {"ok": True}
    restored = await _request(client, "pokered://game-state")
    # The emulated state is restored exactly; the epoch is session
    # bookkeeping, so only the load generation advances (the external tick is
    # deliberately not rewound by load_state).
    assert _without_epoch(restored) == _without_epoch(before[0]), (restored, before[0])
    assert restored["epoch"]["load_generation"] == before[0]["epoch"]["load_generation"] + 1, (
        restored["epoch"],
        before[0]["epoch"],
    )


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

        # Action/phase fail-closed.  A regular FIGHT move resets
        # wActionResultOrTookBattleTurn to zero in ExecutePlayerMoveDone and no
        # item/switch/run path runs here, so the derived ACTION_RESOLUTION phase
        # is correctly absent.  If a non-zero flag or ACTION_RESOLUTION *were*
        # observed we require the documented value to be present; otherwise the
        # observed all-zero contract is asserted rather than fabricating
        # resolution evidence.
        if 3 in settlement["phases_seen"] or any(
            value != 0 for value in settlement["actions_seen"]
        ):
            assert any(value != 0 for value in settlement["actions_seen"]), settlement
        else:
            assert all(value == 0 for value in settlement["actions_seen"]), settlement
            assert 3 not in settlement["phases_seen"], settlement

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

        # -- forced replacement / terminal return (documented absent) --------
        # Reaching a knockout/terminal in these fixtures needs ~130 further
        # turns: six full-HP level-53/54 party mons, same-type matchups that
        # reduce the observed per-turn damage to ~7-31 HP, and a lead move with
        # as little as 2 PP.  That is not a reasonable finite path for this
        # scenario, so the documented fail-closed contract is asserted instead:
        # the battle remains active and terminal_result stays None.
        final_battle = _battle(responsive_after)
        assert final_battle["raw_is_in_battle"] in BATTLE_KINDS, final_battle
        assert final_battle["terminal_result"] is None, final_battle
        assert final_battle["phase"] != TERMINAL_RETURN_PHASE, final_battle
        assert final_battle["in_handle_player_mon_fainted"] == 0, final_battle
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
                    "terminal_absent": {
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
