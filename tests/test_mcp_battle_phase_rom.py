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
                    "primary_epoch": states[0]["epoch"],
                    "peer_epoch": states[1]["epoch"],
                },
                sort_keys=True,
            ),
            flush=True,
        )

        await client.eof()
