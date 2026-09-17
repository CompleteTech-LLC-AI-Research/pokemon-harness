"""Real-ROM MCP stdio trade that verifies exact paired party-record exchange.

Finding 1 of the independent PR #118 review requires public stdio gameplay
evidence: a caller must enter the Trade Center through the public MCP server,
choose both owners' party members, complete the real exchange, and prove the
44-byte record swap through read-only public resources.  The pre-existing
strict trade tests drive the emulators through private hooks; this test drives
the *public* ``press``/``link_step``/``link_peer_press`` tool surface and reads
only ``pokered://game-state``/``pokered://peer-game-state`` plus the additive
``pokered://party-records``/``pokered://peer-party-records`` resources.

The test pairs a canonical primary game with a distinct canonical peer game so
that the two source records are *different bytes of the same species*.  That
makes the digest swap a byte-exact identity proof rather than a species match:
Red's lead and Blue's lead are both species 154 with independently pinned
records, while Yellow's lead is a different species.  A same-family pair would
receive its own identical record and could not demonstrate a digest change.

Coverage limits are explicit: this file implements the real-ROM MCP stdio
*local* pair only.  It does not implement the nine-orientation matrix, does not
implement the TCP/remote transport, and does not implement a native runtime
run.  The parametrization exists so each canonical source game can be selected
individually, but a run only validates the selected case.  The trade-centre
navigator reads only public ``menu``/``overworld`` fields and derives every
button from the mask and bounds the ROM itself published
(engine/link/cable_club.asm), so no private hook, RAM read, or RAM write is
used by the acceptance client.  Nothing here writes
RAM, bypasses a ROM hash, installs a hook, or manufactures a fixture; the
server child only preloads the immutable peer state that the public tool
surface cannot load, exactly as the battle-phase template does.

``POKERED_SKIP_SHA1`` is rejected: real-ROM evidence must validate the pinned
ROM/SYM and fixture bytes.  Missing assets skip so partial BYO-ROM checkouts
still collect the file.
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
# Each canonical source game is paired with a distinct peer.  Red<->Blue keeps
# the same species (154) with different records in both directions, which is the
# required same-species/byte-distinct case; Yellow's lead is a different species,
# so that row exercises a cross-species exchange instead.  ``SAME_SPECIES_LEADS``
# names the row ids whose leads are pinned to the same species, so the
# same-species assertion is only made where the fixtures actually guarantee it.
PEERS = {
    "red_color": "blue_color",
    "blue_color": "red_color",
    "yellow": "red_color",
}
SAME_SPECIES_LEADS = frozenset({"red_color", "blue_color"})

# Map ids per pokered/constants/map_constants.asm.
CABLE_CLUB_MAP_ID = 64
TRADE_CENTER_MAP_ID = 0xEF

# ``wMenuWatchedKeys`` masks written by the Trade Center code in
# pokered/engine/link/cable_club.asm, cross-checked against
# constants/hardware.inc (PAD_A=0x01, PAD_B=0x02, PAD_RIGHT=0x10,
# PAD_DOWN=0x80, PAD_LEFT=0x20):
#   .playerMonMenu            PAD_DOWN|PAD_RIGHT|PAD_A = 0x91
#   .selectStatsMenuItem      PAD_RIGHT|PAD_B|PAD_A    = 0x13
#   .selectTradeMenuItem      PAD_LEFT|PAD_B|PAD_A     = 0x23
#   trade-cancel two-option menu (TRADE_CANCEL_MENU)   = 0x03
# ``.playerMonMenu`` sets ``wMaxMenuItem = wPartyCount`` (not +1: the CANCEL box
# is drawn outside the menu list), and the STATS/TRADE sub-menus set it to 0.
PARTY_MENU_KEYS = 0x91
STATS_MENU_KEYS = 0x13
TRADE_MENU_KEYS = 0x23
CONFIRM_MENU_KEYS = 0x03
LINK_MENU_MAX_ITEMS = (2, 3)

# Finite frame budgets.  Every phase fails closed when its bound is exhausted
# instead of spinning.  ``link_step`` is bounded by the client's 20 s call
# deadline; a local pair runs at roughly 2.5-4 frames/s here, so a ten-frame
# chunk keeps each RPC at a few seconds even on a busy host.
STEP_CHUNK = 10
RECEPTIONIST_WALK_FRAMES = 60
LINK_MENU_BUDGET = 1600
LINK_MENU_QUIET_FRAMES = 100
LINK_MENU_BURST_ATTEMPTS = 1
WARP_BUDGET = 1200
WALK_ATTEMPTS = 4
WALK_STEP_FRAMES = 20
TRADE_BUDGET = 4000
# Two same-species members must be distinguishable by digest; the two records
# are pinned by the fixture manifest, so the test asserts the property from
# the live resources rather than hard-coding the digests.

CHILD = r"""
import asyncio
import contextlib
import sys
from pathlib import Path


async def _serve():
    repo = Path(sys.argv[1])
    primary_version = sys.argv[2]
    peer_version = sys.argv[3]
    primary_rom = Path(sys.argv[4])
    primary_sym = Path(sys.argv[5])
    peer_rom = Path(sys.argv[6])
    peer_sym = Path(sys.argv[7])
    peer_state = Path(sys.argv[8])
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
        register_default_hooks(peer)
        # The public tool surface has no peer-load operation, so the harness
        # child preloads the immutable peer fixture before serving.  The
        # primary fixture is loaded by the acceptance client through the
        # public ``load_state`` tool.
        peer.load_state(peer_state.read_bytes())
    try:
        await serve_stdio(
            primary,
            peer_session=peer,
            primary_version=primary_version,
            peer_version=peer_version,
        )
    finally:
        with contextlib.redirect_stdout(sys.stderr):
            primary.close()
            peer.close()


asyncio.run(_serve())
"""


def _require_assets(version):
    """Return validated pinned assets for one canonical game or skip."""
    family = version.split("_")[0]
    paths = [
        rom_path(family, color=family != "yellow", project_root=ROOT),
        sym_path(family, ROOT),
        fixture_path(family, "cable_club.state", project_root=ROOT),
    ]
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        pytest.skip("missing BYO trade assets: " + ", ".join(missing))
    # Present but mismatched inputs must fail, not skip.
    return resolve_assets(version, ROOT)


@asynccontextmanager
async def _stdio_server(tmp_path, primary_asset, peer_asset):
    """Launch one stdio server with an in-process peer on immutable fixtures."""
    directory = tmp_path / "server"
    directory.mkdir()
    copied = {}
    for name, asset in (
        ("primary_rom", primary_asset),
        ("peer_rom", peer_asset),
    ):
        copied[name] = directory / asset["rom"].name
        shutil.copyfile(asset["rom"], copied[name])
    copied["primary_sym"] = directory / primary_asset["sym"].name
    copied["peer_sym"] = directory / peer_asset["sym"].name
    shutil.copyfile(primary_asset["sym"], copied["primary_sym"])
    shutil.copyfile(peer_asset["sym"], copied["peer_sym"])
    peer_state = directory / "peer-cable_club.state"
    peer_state.write_bytes(peer_asset["state"])
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
                primary_asset["family"],
                peer_asset["family"],
                str(copied["primary_rom"]),
                str(copied["primary_sym"]),
                str(copied["peer_rom"]),
                str(copied["peer_sym"]),
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


async def _game_states(client):
    # One outstanding request per stdio client: read the primary and peer
    # resources sequentially rather than concurrently on the same stream.
    primary = await _request(client, "pokered://game-state")
    peer = await _request(client, "pokered://peer-game-state")
    return [primary, peer]


async def _party_records(client):
    primary = await _request(client, "pokered://party-records")
    peer = await _request(client, "pokered://peer-party-records")
    return [primary, peer]


def _log(phase, detail):
    print(f"MCP_TRADE_RECORDS {phase} {detail}", flush=True)


def _overworld(state):
    overworld = state.get("overworld")
    assert isinstance(overworld, dict), state
    return overworld


def _menu(state):
    menu = state.get("menu")
    assert isinstance(menu, dict), state
    return menu


def _menu_ready(state, max_items):
    menu = _menu(state)
    watched = menu["watched_keys"]
    current = menu["current_item"]
    return (
        type(watched) is int
        and watched & 0x01 == 0x01
        and menu["max_item"] in max_items
        and type(current) is int
    )


def _link_menu_ready(state):
    return _overworld(state)["map_id"] == CABLE_CLUB_MAP_ID and _menu_ready(
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
        states = await _game_states(client)
        if all(predicate(state) for state in states):
            return states, spent
        await _link_step(client, min(STEP_CHUNK, budget - spent))
        spent += STEP_CHUNK
    states = await _game_states(client)
    raise AssertionError(
        f"{prompt}: phase budget {budget} frames exhausted; states={json.dumps(states)}"
    )


async def _mash_until(client, predicate, *, budget, prompt):
    """A-mash the pair while advancing until ``predicate`` holds."""
    spent = 0
    while spent < budget:
        states = await _game_states(client)
        if all(predicate(state) for state in states):
            return states, spent
        await _press_both(client, "a")
        await _link_step(client, min(STEP_CHUNK, budget - spent))
        spent += STEP_CHUNK
    states = await _game_states(client)
    raise AssertionError(
        f"{prompt}: phase budget {budget} frames exhausted; states={json.dumps(states)}"
    )


async def _pair(client):
    paired = await client.tool("link_pair")
    assert paired["paired"] is True, paired
    status = await client.tool("link_status")
    assert status["link_backend"] == "bit_accurate", status
    assert status["paired"] is True, status


async def _prelink(client, primary_asset):
    """Load the public primary fixture and prove the resource surface is live."""
    await client.initialize()
    listed = await client.request("tools/list", {})
    tool_names = {row["name"] for row in listed["tools"]}
    assert {
        "load_state",
        "press",
        "release",
        "step",
        "link_pair",
        "link_unpair",
        "link_step",
        "link_peer_press",
    } <= tool_names, tool_names
    # No RAM, register, hook-registration, or hash-bypass tool exists.
    assert not any(
        "hash" in name or "memory" in name or "register" in name or "hook" in name
        for name in tool_names
    ), tool_names
    resources = await client.request("resources/list", {})
    uris = {str(row["uri"]) for row in resources["resources"]}
    assert {
        "pokered://game-state",
        "pokered://peer-game-state",
        "pokered://party-records",
        "pokered://peer-party-records",
    } <= uris, uris
    loaded = await client.tool(
        "load_state", {"data": base64.b64encode(primary_asset["state"]).decode("ascii")}
    )
    assert loaded == {"ok": True}, loaded
    before = await _party_records(client)
    for payload, source in zip(before, ("party-records", "peer-party-records"), strict=True):
        assert payload["source"] == source, payload
        assert payload["digest_algorithm"] == "sha256", payload
        assert payload["record_size"] == 44, payload
        assert payload["valid"] is True, payload
    return before


async def _drive_to_link_menu(client):
    for _ in range(3):
        await _press_both(client, "up", duration=6)
        await _link_step(client, RECEPTIONIST_WALK_FRAMES)
    spent = 0
    while spent < LINK_MENU_BUDGET:
        states = await _game_states(client)
        if all(_link_menu_ready(state) for state in states):
            _log("link_menu", f"frames={spent}")
            return states
        # Stagger the ordinary public A input exactly as the proven local
        # driver does: primary A, four frames, peer A, sixteen frames.
        for _ in range(LINK_MENU_BURST_ATTEMPTS):
            await _press(client, "a", duration=4)
            await _link_step(client, 4)
            await _peer_press(client, "a", duration=4)
            await _link_step(client, 16)
            spent += 20
        # Quiet window: no public input, so a LinkMenu that finishes drawing
        # here cannot select its default entry before the explicit selection.
        for _ in range(LINK_MENU_QUIET_FRAMES // STEP_CHUNK):
            await _link_step(client, STEP_CHUNK)
            spent += STEP_CHUNK
            states = await _game_states(client)
            if all(_link_menu_ready(state) for state in states):
                _log("link_menu", f"frames={spent}")
                return states
    states = await _game_states(client)
    raise AssertionError(
        f"LinkMenu never reached on both owners: budget {LINK_MENU_BUDGET} "
        f"frames exhausted; states={json.dumps(states)}"
    )


async def _select_trade_center(client):
    """Confirm the default TRADE CENTER entry and wait for the 0xEF warp."""
    spent = 0
    while spent < 400:
        states = await _game_states(client)
        if all(_menu_ready(state, LINK_MENU_MAX_ITEMS) for state in states):
            break
        await _link_step(client, 16)
        spent += 16
    states, cursor_frames = await _advance_until(
        client,
        lambda state: _link_menu_ready(state) and _menu(state)["current_item"] == 0,
        budget=400,
        prompt="LinkMenu cursor never rested on TRADE CENTER on both owners",
    )
    # Staggered A confirms the default TRADE CENTER entry on both owners.
    await _press(client, "a", duration=4)
    await _link_step(client, 4)
    await _peer_press(client, "a", duration=4)
    await _link_step(client, 16)
    states, warp_frames = await _mash_until(
        client,
        lambda state: _overworld(state)["map_id"] == TRADE_CENTER_MAP_ID,
        budget=WARP_BUDGET,
        prompt="TRADE_CENTER warp never reached on both owners",
    )
    _log("trade_center", f"cursor={cursor_frames} warp={warp_frames}")
    return states


async def _walk_to_trade_trigger(client):
    """Turn each owner toward its hidden-event tile and hand off to the trade.

    The internal-clock owner spawns at (3, 4) and the external-clock owner at
    (6, 4); the two hidden-event triggers live at (4, 4) and (5, 4).  The
    public ``overworld.x`` identifies each owner without reading serial
    registers.  Facing is what the ``ANY_FACING`` trigger needs, so each
    attempt presses the direction on its own owner and then advances the pair
    one frame at a time: one owner can enter ``CableClub_DoBattleOrTrade``
    during this loop, and ticking twenty frames on that side before its peer
    advances would let the first serial transfers use stale handshake bytes.
    """
    states = await _game_states(client)
    directions = ["right" if _overworld(state)["x"] < 5 else "left" for state in states]
    for _ in range(WALK_ATTEMPTS):
        await _press(client, directions[0], duration=8)
        await _peer_press(client, directions[1], duration=8)
        for _ in range(WALK_STEP_FRAMES):
            await _link_step(client, 1)
    _log("walk", f"directions={directions}")


def _trade_action(state, party_count):
    """Return the public button for one owner from its observed ROM menu context.

    The navigator is reactive rather than phase-counted: the ROM can escape
    back to an outer menu (A in the STATS/TRADE sub-menu displays stats and
    returns to the player-mon menu), so each owner is driven from the mask and
    bounds it currently publishes.  ``wMaxMenuItem`` is retained after a menu
    closes, so the map id gates every branch and only the trade-center masks
    below are treated as live menus.
    """
    if _overworld(state)["map_id"] != TRADE_CENTER_MAP_ID:
        return "a"
    menu = _menu(state)
    keys = menu["watched_keys"]
    item = menu["current_item"]
    maximum = menu["max_item"]
    if keys == PARTY_MENU_KEYS and maximum == party_count and 0 <= item < party_count:
        # Player-mon menu: A selects this mon and opens the STATS/TRADE menu.
        return "a"
    if keys == STATS_MENU_KEYS and maximum == 0:
        # STATS/TRADE sub-menu: RIGHT moves the cursor from STATS to TRADE.
        return "right"
    if keys == TRADE_MENU_KEYS and maximum == 0:
        # TRADE entry: A commits the selection and starts the nibble exchange.
        return "a"
    if keys == CONFIRM_MENU_KEYS and maximum == 1 and item == 0:
        # "WILL TRADE X FOR Y?" defaults to YES.
        return "a"
    # Pre-menu dialogue, the waiting prompt, and the trade animation: A-mash.
    return "a"


def _records_from_payload(payload):
    records = payload["records"]
    assert isinstance(records, list), payload
    return records


async def _drive_trade(client, *, party_count, primary_before, peer_before):
    """Drive both owners through the ROM trade flow and return the final records.

    Success is the exact paired 44-byte record swap: each owner's traded slot
    must hold the peer's original digest.  Every phase is bounded, and a bound
    that lapses fails closed with the observed state instead of spinning.
    """
    spent = 0
    milestones = []
    while spent < TRADE_BUDGET:
        records = await _party_records(client)
        primary_after = _records_from_payload(records[0])
        peer_after = _records_from_payload(records[1])
        if (
            primary_after
            and peer_after
            and primary_after[0]["digest"] == peer_before[0]["digest"]
            and peer_after[0]["digest"] == primary_before[0]["digest"]
        ):
            _log("trade_complete", f"frames={spent} milestones={milestones}")
            return records, await _game_states(client)
        states = await _game_states(client)
        observed = [
            f"{_overworld(state)['map_id']}:{_menu(state)['watched_keys']}"
            f":{_menu(state)['current_item']}/{_menu(state)['max_item']}"
            for state in states
        ]
        if not milestones or milestones[-1] != observed:
            milestones.append(observed)
        actions = [_trade_action(state, party_count) for state in states]
        await _press(client, actions[0], duration=4)
        await _peer_press(client, actions[1], duration=4)
        await _link_step(client, STEP_CHUNK)
        spent += STEP_CHUNK
    records = await _party_records(client)
    states = await _game_states(client)
    raise AssertionError(
        "trade never produced the exact paired digest swap within "
        f"{TRADE_BUDGET} frames; primary={json.dumps(records[0])} "
        f"peer={json.dumps(records[1])} states={json.dumps(states)}"
    )


def _assert_record_shape(record, *, label):
    assert set(record) == {"slot", "digest", "record_size", "species", "level"}, (label, record)
    assert record["record_size"] == 44, (label, record)
    assert type(record["slot"]) is int, (label, record)
    assert type(record["species"]) is int and record["species"] not in (0, 0xFF), (label, record)
    assert type(record["level"]) is int and record["level"] > 0, (label, record)
    assert isinstance(record["digest"], str) and len(record["digest"]) == 64, (label, record)


def _assert_unrelated_unchanged(before, after, *, traded_slot, label):
    assert len(before) == len(after), (label, before, after)
    for slot, (old, new) in enumerate(zip(before, after, strict=True)):
        if slot == traded_slot:
            continue
        assert old["digest"] == new["digest"], (label, slot, old, new)


@pytest.mark.asyncio
@pytest.mark.parametrize("version", FAMILIES)
async def test_real_rom_mcp_trade_exchanges_party_records(tmp_path, version):
    if os.environ.get("POKERED_SKIP_SHA1"):
        pytest.fail("POKERED_SKIP_SHA1 must not be used for real-ROM evidence")
    peer_version = PEERS[version]
    primary_asset = _require_assets(version)
    peer_asset = _require_assets(peer_version)

    async with _stdio_server(tmp_path, primary_asset, peer_asset) as client:
        before = await _prelink(client, primary_asset)
        primary_before = _records_from_payload(before[0])
        peer_before = _records_from_payload(before[1])
        assert len(primary_before) >= 1 and len(peer_before) >= 1, before
        for record in (*primary_before, *peer_before):
            _assert_record_shape(record, label="fixture")

        await _pair(client)
        await _drive_to_link_menu(client)
        await _select_trade_center(client)
        await _walk_to_trade_trigger(client)
        party_count = len(primary_before)

        records, final_states = await _drive_trade(
            client,
            party_count=party_count,
            primary_before=primary_before,
            peer_before=peer_before,
        )
        primary_after = _records_from_payload(records[0])
        peer_after = _records_from_payload(records[1])

        # Exact 44-byte identity exchange at the intended (lead) slots.
        traded_slot = 0
        assert len(primary_after) == len(primary_before)
        assert len(peer_after) == len(peer_before)
        for record in (*primary_after, *peer_after):
            _assert_record_shape(record, label="post-trade")
        assert primary_after[traded_slot]["slot"] == traded_slot
        assert peer_after[traded_slot]["slot"] == traded_slot
        assert primary_after[traded_slot]["digest"] == peer_before[traded_slot]["digest"]
        assert peer_after[traded_slot]["digest"] == primary_before[traded_slot]["digest"]
        # The traded slots changed identity, and the same-species members are
        # distinguished by raw record digest rather than species alone.
        assert primary_after[traded_slot]["digest"] != primary_before[traded_slot]["digest"]
        assert peer_after[traded_slot]["digest"] != peer_before[traded_slot]["digest"]
        if version in SAME_SPECIES_LEADS:
            # Same species, different 44-byte records: the swap is proved by the
            # digest identity above rather than by a species change.
            assert primary_before[traded_slot]["species"] == peer_before[traded_slot]["species"], (
                version,
                primary_before[traded_slot],
                peer_before[traded_slot],
            )
        assert primary_before[traded_slot]["digest"] != peer_before[traded_slot]["digest"], (
            version,
            primary_before[traded_slot],
            peer_before[traded_slot],
        )
        assert primary_after[traded_slot]["species"] == peer_before[traded_slot]["species"]
        assert peer_after[traded_slot]["species"] == primary_before[traded_slot]["species"]
        # Unrelated records must be byte-identical; with these one-member
        # fixtures there are no other slots, so the check is explicit and
        # vacuous rather than omitted.
        _assert_unrelated_unchanged(
            primary_before, primary_after, traded_slot=traded_slot, label="primary"
        )
        _assert_unrelated_unchanged(peer_before, peer_after, traded_slot=traded_slot, label="peer")

        print(
            "MCP_TRADE_RECORDS_ROM "
            + json.dumps(
                {
                    "version": version,
                    "peer_version": peer_version,
                    "transport": "local_pair",
                    "traded_slot": traded_slot,
                    "party_count": party_count,
                    "primary_before": primary_before,
                    "primary_after": primary_after,
                    "peer_before": peer_before,
                    "peer_after": peer_after,
                    "primary_map": _overworld(final_states[0])["map_id"],
                    "peer_map": _overworld(final_states[1])["map_id"],
                    "step_chunk": STEP_CHUNK,
                },
                sort_keys=True,
            ),
            flush=True,
        )

        await client.eof()
