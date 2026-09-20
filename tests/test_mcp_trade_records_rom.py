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

Coverage limits are explicit.  This file implements the real-ROM MCP stdio
*local* pair across all nine ordered canonical orientations (every ordered
combination of Red/Blue/Yellow, including the same-family rows), the
cancel-before-commitment case, the multi-member sender/receiver slot rows, and
the EOF/disconnect cases during setup and during the live trade flow.  It does
not implement the TCP/remote transport rows or a native runtime run; those
remain separate declared work (see the production-gate manifest).  The
trade-centre navigator reads only public ``menu``/``overworld`` fields and
derives every button from the mask and bounds the ROM itself published
(engine/link/cable_club.asm), so no private hook, RAM read, or RAM write is used
by the acceptance client.  Nothing here writes RAM, bypasses a ROM hash,
installs a hook, or manufactures a fixture; the server child only preloads the
immutable peer state that the public tool surface cannot load, exactly as the
battle-phase template does.

The ordinary fixture is a single-member party (``party_count == 1``), where the
ROM's ``.playerMonMenu`` bounds ``wMaxMenuItem`` by ``wPartyCount`` and no
second slot can be selected.  The nonzero sender/receiver *slot* rows are
therefore driven from the registered ``cable_club-battle.state`` fixture: the
manifest pins that row as ``kind: battle``, but its observable state is the same
pre-connection Cable Club attendant tile as the ordinary fixture
(``map_id == 64``, ``(11, 3)``, ``wIsInBattle == 0``) carrying a six-member
party with independently pinned 44-byte records.  Using an admitted immutable
fixture keeps the row real; no party is fabricated at runtime.  Those rows
assert both the cursor slot the client actually selected (observed on the ROM
menu before it confirmed) and the ROM's remove/compact/append receiving slot.

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
# Issue #105 requires all nine *ordered* canonical local orientations, not only
# a representative pair: every ordered combination of the three families,
# including the same-family rows.  ``SAME_SPECIES_LEADS`` names the families
# whose lead records are pinned to the same species (Red and Blue both lead with
# species 154); a row whose two owners are both in that set proves the exchange by
# raw 44-byte record identity rather than by a species change.
ORIENTATIONS = tuple((primary, peer) for primary in FAMILIES for peer in FAMILIES)
SAME_SPECIES_FAMILIES = frozenset({"red_color", "blue_color"})


def _orientation_id(orientation):
    return f"{orientation[0]}-{orientation[1]}"


def _same_species_leads(primary, peer):
    """Whether this orientation's two leads are the same species by fixture."""

    return primary in SAME_SPECIES_FAMILIES and peer in SAME_SPECIES_FAMILIES


# Manifest-registered fixture (``kind: battle``) that starts on the same
# pre-connection Cable Club attendant tile as the ordinary trade fixture while
# carrying a six-member party, so the issue's nonzero sender/receiver slot rows
# are driven from an admitted immutable fixture instead of an authored double.
# The pins are still checked by ``resolve_assets``: only the unique verified
# ``battle`` row for each family whose basename matches is admitted.
MULTI_MEMBER_FIXTURE = "cable_club-battle.state"
MULTI_MEMBER_PARTY_COUNT = 6
# Both ordered slot roles: in the first row the primary owner offers a nonzero
# slot and receives at the appended slot, in the second the roles are reversed.
# The outgoing slots differ from each other and from the receiving slot.
MULTI_MEMBER_ORIENTATIONS = (
    ("red_color", "blue_color", 2, 0),
    ("blue_color", "red_color", 0, 3),
)


def _multi_member_id(row):
    primary, peer, primary_slot, peer_slot = row
    return f"{primary}-{peer}-out{primary_slot}-peerout{peer_slot}"


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
# Completion budget *after* the paired record copy.  The copy precedes a
# 100-frame delay, the trade animation, a forced evolution check
# (``TryEvolvingMon``), a final serial synchronization, ``SavePartyAndDexData``,
# and the return to the trade-center selection loop
# (engine/link/cable_club.asm ``.doTrade``/``CableClub_DoBattleOrTradeAgain``),
# so the post-copy phase needs its own finite budget.
POST_TRADE_BUDGET = 6000
# ROM-owned execution-hook name registered from ``TryEvolvingMon`` in
# ``DEFAULT_HOOKS``.  It fires after the trade animation as part of the
# source-defined completion sequence, so it distinguishes real completion from
# a game frozen at the record copy.
EVOLUTION_EVENT = "evolution_check"
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
    runtime_mode = sys.argv[9]
    with contextlib.redirect_stdout(sys.stderr):
        sys.path.insert(0, str(repo / "src"))
        import pyboy
        import pyboy.core.serial

        pyboy_origin = pyboy.__file__
        serial_origin = pyboy.core.serial.__file__
        origins = {"pyboy": pyboy_origin, "serial": serial_origin}
        print(f"MCP_TRADE_RECORDS_CHILD_ORIGINS {runtime_mode} {origins}", flush=True)
        # The runtime selection is not cosmetic: a native (cython) run must
        # import the installed compiled extensions, while a source run must use
        # the vendored Python package.  Asserting this here makes a shadowed
        # PYTHONPATH fail loudly instead of silently changing the runtime.
        # ``pyboy`` is a package, so its source origin is ``pyboy/__init__.py``
        # rather than a top-level ``pyboy.py``; anchor the source check to the
        # vendored tree instead of a filename suffix.
        vendored = str((repo / "vendor" / "pyboy-src").resolve(strict=False))
        compiled_extensions = (".so", ".pyd")
        if runtime_mode == "cython":
            assert pyboy_origin.endswith(compiled_extensions), origins
            assert serial_origin.endswith(compiled_extensions), origins
            assert not pyboy_origin.startswith(vendored), origins
        else:
            assert pyboy_origin.startswith(vendored), origins
            assert serial_origin.startswith(vendored), origins
            assert pyboy_origin.endswith(".py"), origins
            assert serial_origin.endswith(".py"), origins
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


def _require_assets(version, fixture="cable_club.state"):
    """Return validated pinned assets for one canonical game or skip."""
    family = version.split("_")[0]
    paths = [
        rom_path(family, color=family != "yellow", project_root=ROOT),
        sym_path(family, ROOT),
        fixture_path(family, fixture, project_root=ROOT),
    ]
    missing = [str(path) for path in paths if not path.exists()]
    if missing:
        pytest.skip("missing BYO trade assets: " + ", ".join(missing))
    # Present but mismatched inputs must fail, not skip.
    return resolve_assets(version, ROOT, fixture=fixture)


def _child_runtime_mode():
    """Return the runtime selection the child must inherit.

    Mirrors ``scripts/production_gate.py``: the gate sets ``PYBOY_NO_CYTHON=1``
    for the source runtime and leaves it unset for the cython runtime.  The
    invoking interpreter is inherited, so the child's PyBoy must resolve to
    the vendored package in source mode and to the installed compiled
    extensions in cython mode.  An explicit override is honored so a caller can
    pin the mode without changing the gate.
    """
    override = os.environ.get("POKERED_TRADE_RUNTIME_MODE")
    if override in {"source", "cython"}:
        return override
    if os.environ.get("PYBOY_NO_CYTHON") == "1":
        return "source"
    return "cython"


def _child_environment(runtime_mode):
    """Build the child env without shadowing the selected runtime.

    The previous launcher unconditionally prepended ``vendor/pyboy-src``, which
    forced source ``.py`` modules even under the native runtime.  This mirrors
    the gate: source mode prepends the vendored package and sets
    ``PYBOY_NO_CYTHON``; cython mode keeps only the harness paths and any
    inherited entries that are not the vendored package.
    """
    vendored_pyboy = (ROOT / "vendor" / "pyboy-src").resolve(strict=False)
    entries = [str(ROOT / "src"), str(ROOT)]
    if runtime_mode == "source":
        entries.insert(0, str(vendored_pyboy))
    for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        if not entry:
            continue
        entry_path = Path(entry).expanduser()
        if not entry_path.is_absolute():
            entry_path = ROOT / entry_path
        if runtime_mode == "cython" and entry_path.resolve(strict=False) == vendored_pyboy:
            continue
        resolved = str(entry_path)
        if resolved not in entries:
            entries.append(resolved)
    env = {key: value for key, value in os.environ.items() if not key.startswith("POKERED_")}
    env["PYTHONPATH"] = os.pathsep.join(entries)
    env["PYTHONUNBUFFERED"] = "1"
    if runtime_mode == "source":
        env["PYBOY_NO_CYTHON"] = "1"
    else:
        env.pop("PYBOY_NO_CYTHON", None)
    return env


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
    runtime_mode = _child_runtime_mode()
    env = _child_environment(runtime_mode)
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
                runtime_mode,
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


async def _event_names(client):
    """Return the public execution-hook event names observed this session."""
    events = await _request(client, "pokered://events")
    assert isinstance(events, list), events
    return [str(event.get("name")) for event in events]


def _selection_loop_ready(state, party_count):
    """Whether one owner is back on the trade-center party-selection loop."""
    if _overworld(state)["map_id"] != TRADE_CENTER_MAP_ID:
        return False
    menu = _menu(state)
    return (
        menu["watched_keys"] == PARTY_MENU_KEYS
        and menu["max_item"] == party_count
        and _menu_ready(state, (party_count,))
    )


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


def _trade_action(state, party_count, target_slot=0):
    """Return the public button for one owner from its observed ROM menu context.

    The navigator is reactive rather than phase-counted: the ROM can escape
    back to an outer menu (A in the STATS/TRADE sub-menu displays stats and
    returns to the player-mon menu), so each owner is driven from the mask and
    bounds it currently publishes.  ``wMaxMenuItem`` is retained after a menu
    closes, so the map id gates every branch and only the trade-center masks
    below are treated as live menus.

    ``target_slot`` is the party slot this owner must offer.  On the
    player-mon menu the ROM sets ``wMaxMenuItem = wPartyCount``, so a
    multi-member party can walk the cursor with DOWN; a single-member party
    never issues a DOWN because the cursor is already on the only slot.
    """
    if _overworld(state)["map_id"] != TRADE_CENTER_MAP_ID:
        return "a"
    menu = _menu(state)
    keys = menu["watched_keys"]
    item = menu["current_item"]
    maximum = menu["max_item"]
    if keys == PARTY_MENU_KEYS and maximum == party_count and 0 <= item < party_count:
        if item < target_slot:
            # Player-mon menu: move down toward the intended slot.
            return "down"
        if item > target_slot:
            # Overshot (or the ROM clamped); walk back up.
            return "up"
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


def _compaction_expected_digests(before, outgoing_slot, incoming_digest):
    """Return the source-defined post-trade digest list for one owner.

    Gen I removes the selected outgoing record, compacts the survivors keeping
    their order, then appends the received 44-byte record (engine/link/
    cable_club.asm via the party compaction path).  The receiving slot is the
    final occupied slot, independent of the outgoing index, so a fixed-slot or
    in-place equality would be wrong for a multi-member party.
    """
    survivors = before[:outgoing_slot] + before[outgoing_slot + 1 :]
    return [record["digest"] for record in survivors] + [incoming_digest]


async def _drive_trade(
    client, *, party_count, primary_before, peer_before, primary_slot=0, peer_slot=0
):
    """Drive both owners through the ROM trade flow to full completion.

    Success is the exact paired 44-byte record exchange under the ROM's
    remove/compact/append semantics: each owner's final occupied slot must hold
    the peer's selected record after its other records are compacted in order.
    The copy milestone is only the first half: after it the ROM runs the trade
    animation, a forced evolution check, a final serial synchronization, the
    save, and the return to the selection loop, so this continues to that
    later, ROM-owned completion milestone before returning.  Every phase is
    bounded, and a bound that lapses fails closed with the observed state
    instead of spinning.
    """
    primary_incoming = peer_before[peer_slot]["digest"]
    peer_incoming = primary_before[primary_slot]["digest"]
    expected_primary = _compaction_expected_digests(primary_before, primary_slot, primary_incoming)
    expected_peer = _compaction_expected_digests(peer_before, peer_slot, peer_incoming)
    spent = 0
    milestones = []
    swap_records = None
    swap_states = None
    pre_evolution = 0
    # Cursor slot each owner was resting on when it confirmed the offer.  The
    # ROM's ``.playerMonMenu`` publishes ``wCurrentMenuItem``/``wMaxMenuItem``,
    # so the intended slot is observed through the public menu read rather than
    # inferred from the result.  The first time an owner's live player-mon menu
    # rests on its target slot is the offer this drive confirms.
    offered: dict[int, int] = {}
    targets = (primary_slot, peer_slot)
    while spent < TRADE_BUDGET:
        records = await _party_records(client)
        primary_after = _records_from_payload(records[0])
        peer_after = _records_from_payload(records[1])
        if (
            primary_after
            and peer_after
            and [record["digest"] for record in primary_after] == expected_primary
            and [record["digest"] for record in peer_after] == expected_peer
        ):
            swap_records = records
            swap_states = await _game_states(client)
            pre_evolution = (await _event_names(client)).count(EVOLUTION_EVENT)
            break
        states = await _game_states(client)
        observed = [
            f"{_overworld(state)['map_id']}:{_menu(state)['watched_keys']}"
            f":{_menu(state)['current_item']}/{_menu(state)['max_item']}"
            for state in states
        ]
        if not milestones or milestones[-1] != observed:
            milestones.append(observed)
        for owner, state in enumerate(states):
            if owner in offered:
                continue
            menu = _menu(state)
            if (
                _overworld(state)["map_id"] == TRADE_CENTER_MAP_ID
                and menu["watched_keys"] == PARTY_MENU_KEYS
                and type(menu["current_item"]) is int
                and type(menu["max_item"]) is int
                and menu["max_item"] == party_count
                and menu["current_item"] == targets[owner]
            ):
                offered[owner] = menu["current_item"]
        actions = [
            _trade_action(states[0], party_count, primary_slot),
            _trade_action(states[1], party_count, peer_slot),
        ]
        await _press(client, actions[0], duration=4)
        await _peer_press(client, actions[1], duration=4)
        await _link_step(client, STEP_CHUNK)
        spent += STEP_CHUNK
    if swap_records is None:
        records = await _party_records(client)
        states = await _game_states(client)
        raise AssertionError(
            "trade never produced the exact paired digest exchange within "
            f"{TRADE_BUDGET} frames; primary={json.dumps(records[0])} "
            f"peer={json.dumps(records[1])} states={json.dumps(states)}"
        )
    _log("trade_copy", f"frames={spent} milestones={milestones}")
    final_states, post_frames, evolution = await _drive_trade_completion(
        client,
        party_count=party_count,
        pre_evolution=pre_evolution,
        primary_slot=primary_slot,
        peer_slot=peer_slot,
    )
    assert offered.get(0) == primary_slot, (offered, primary_slot)
    assert offered.get(1) == peer_slot, (offered, peer_slot)
    _log("offers", f"primary={primary_slot} peer={peer_slot} observed={offered}")
    return swap_records, swap_states, final_states, post_frames, evolution, offered


async def _drive_trade_completion(
    client, *, party_count, pre_evolution, primary_slot=0, peer_slot=0
):
    """Drive past the record copy to the source-defined trade completion.

    After the copy the ROM runs a 100-frame delay, the trade animation, a
    forced evolution check (``TryEvolvingMon`` → the ``evolution_check`` hook),
    a final serial synchronization, ``SavePartyAndDexData``, and then returns
    to the trade-center selection loop (engine/link/cable_club.asm
    ``.doTrade`` → ``CableClub_DoBattleOrTradeAgain``).  Completion requires
    the later, ROM-owned evolution milestone *and* the restored menu, both
    reachable only by a game that actually ran the full sequence; a game frozen
    at the copy can satisfy neither.
    """
    spent = 0
    milestones = []
    while spent < POST_TRADE_BUDGET:
        states = await _game_states(client)
        evolution = (await _event_names(client)).count(EVOLUTION_EVENT)
        restored = all(_selection_loop_ready(state, party_count) for state in states)
        if evolution > pre_evolution and restored:
            _log("trade_completion", f"frames={spent} evolution={evolution}")
            return states, spent, evolution
        observed = [
            f"{_overworld(state)['map_id']}:{_menu(state)['watched_keys']}"
            f":{_menu(state)['current_item']}/{_menu(state)['max_item']}:{evolution}"
            for state in states
        ]
        if not milestones or milestones[-1] != observed:
            milestones.append(observed)
        actions = [
            _trade_action(states[0], party_count, primary_slot),
            _trade_action(states[1], party_count, peer_slot),
        ]
        await _press(client, actions[0], duration=4)
        await _peer_press(client, actions[1], duration=4)
        await _link_step(client, STEP_CHUNK)
        spent += STEP_CHUNK
    states = await _game_states(client)
    evolution = (await _event_names(client)).count(EVOLUTION_EVENT)
    raise AssertionError(
        "trade never reached the post-copy completion milestone within "
        f"{POST_TRADE_BUDGET} frames; evolution_events={evolution} "
        f"(pre={pre_evolution}) states={json.dumps(states)} "
        f"milestones={json.dumps(milestones)}"
    )


def _assert_record_shape(record, *, label):
    assert set(record) == {"slot", "digest", "record_size", "species", "level"}, (label, record)
    assert record["record_size"] == 44, (label, record)
    assert type(record["slot"]) is int, (label, record)
    assert type(record["species"]) is int and record["species"] not in (0, 0xFF), (label, record)
    assert type(record["level"]) is int and record["level"] > 0, (label, record)
    assert isinstance(record["digest"], str) and len(record["digest"]) == 64, (label, record)


def _assert_exact_exchange(
    before, after, *, outgoing_slot, incoming_digest, incoming_species, label
):
    """Assert the source-defined remove/compact/append exchange for one owner.

    Gen I removes the selected outgoing record, compacts survivors in order,
    then appends the received record at the final occupied slot.  The receiving
    slot is therefore ``len(after) - 1`` rather than the outgoing index, so an
    in-place equality or a fixed-slot comparison would be wrong for a
    multi-member party.
    """
    assert len(before) == len(after), (label, before, after)
    expected = _compaction_expected_digests(before, outgoing_slot, incoming_digest)
    assert [record["digest"] for record in after] == expected, (label, before, after, expected)
    receiving = len(after) - 1
    assert after[receiving]["slot"] == receiving, (label, after)
    assert after[receiving]["digest"] == incoming_digest, (label, after)
    assert after[receiving]["species"] == incoming_species, (label, after)
    # The received record is byte-distinct from the outgoing record it replaces
    # in the same owner's party, which the compaction-aware digest list above
    # already proves; assert it explicitly for the receiving slot.
    assert before[outgoing_slot]["digest"] != incoming_digest, (label, before, after)
    for record in after:
        _assert_record_shape(record, label=f"{label}-after")


def _player_mon_menu_live(state, party_count):
    """Whether one owner is resting on the ROM's live trade party menu."""
    menu = _menu(state)
    return (
        _overworld(state)["map_id"] == TRADE_CENTER_MAP_ID
        and menu["watched_keys"] == PARTY_MENU_KEYS
        and menu["max_item"] == party_count
        and type(menu["current_item"]) is int
        and 0 <= menu["current_item"] < party_count
    )


async def _enter_trade_flow(client):
    """Drive the public flow from the loaded fixture into the Trade Center."""
    await _pair(client)
    await _drive_to_link_menu(client)
    await _select_trade_center(client)
    await _walk_to_trade_trigger(client)


async def _await_party_menu(client, party_count, *, budget=TRADE_BUDGET):
    """Advance until both owners publish the live trade party menu."""
    spent = 0
    while spent < budget:
        states = await _game_states(client)
        if all(_player_mon_menu_live(state, party_count) for state in states):
            _log("party_menu", f"frames={spent}")
            return states, spent
        actions = [_trade_action(state, party_count, 0) for state in states]
        await _press(client, actions[0], duration=4)
        await _peer_press(client, actions[1], duration=4)
        await _link_step(client, STEP_CHUNK)
        spent += STEP_CHUNK
    states = await _game_states(client)
    raise AssertionError(
        f"trade party menu never became live within {budget} frames; states={json.dumps(states)}"
    )


async def _fixture_pair(client, primary_asset, *, fixture):
    """Load the primary fixture publicly and return both owners' records."""
    before = await _prelink(client, primary_asset)
    primary_before = _records_from_payload(before[0])
    peer_before = _records_from_payload(before[1])
    assert len(primary_before) == len(peer_before) >= 1, before
    for record in (*primary_before, *peer_before):
        _assert_record_shape(record, label=f"fixture-{fixture}")
    return primary_before, peer_before


def _fixture_sha1(asset):
    return asset["provenance"]["registry"]["sha1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "peer_version"),
    ORIENTATIONS,
    ids=[_orientation_id(orientation) for orientation in ORIENTATIONS],
)
async def test_real_rom_mcp_trade_exchanges_party_records(tmp_path, version, peer_version):
    if os.environ.get("POKERED_SKIP_SHA1"):
        pytest.fail("POKERED_SKIP_SHA1 must not be used for real-ROM evidence")
    primary_asset = _require_assets(version)
    peer_asset = _require_assets(peer_version)

    async with _stdio_server(tmp_path, primary_asset, peer_asset) as client:
        primary_before, peer_before = await _fixture_pair(
            client, primary_asset, fixture="cable_club.state"
        )
        await _enter_trade_flow(client)
        party_count = len(primary_before)

        (
            records,
            swap_states,
            final_states,
            post_frames,
            evolution,
            offered,
        ) = await _drive_trade(
            client,
            party_count=party_count,
            primary_before=primary_before,
            peer_before=peer_before,
        )
        primary_after = _records_from_payload(records[0])
        peer_after = _records_from_payload(records[1])

        # Byte-exact remove/compact/append exchange under the ROM's own
        # semantics; the receiving slot is the final occupied slot.
        assert primary_before[0]["digest"] != peer_before[0]["digest"], (
            version,
            primary_before[0],
            peer_before[0],
        )
        if _same_species_leads(version, peer_version):
            # Same species, different 44-byte records: the exchange is proved by
            # raw record identity rather than by a species change.
            assert primary_before[0]["species"] == peer_before[0]["species"], (
                version,
                primary_before[0],
                peer_before[0],
            )
        _assert_exact_exchange(
            primary_before,
            primary_after,
            outgoing_slot=0,
            incoming_digest=peer_before[0]["digest"],
            incoming_species=peer_before[0]["species"],
            label="primary",
        )
        _assert_exact_exchange(
            peer_before,
            peer_after,
            outgoing_slot=0,
            incoming_digest=primary_before[0]["digest"],
            incoming_species=primary_before[0]["species"],
            label="peer",
        )

        print(
            "MCP_TRADE_RECORDS_ROM "
            + json.dumps(
                {
                    "version": version,
                    "peer_version": peer_version,
                    "transport": "local_pair",
                    "runtime_mode": _child_runtime_mode(),
                    "fixture": "cable_club.state",
                    "fixture_sha1": _fixture_sha1(primary_asset),
                    "peer_fixture_sha1": _fixture_sha1(peer_asset),
                    "outgoing_slot": 0,
                    "offered_slots": {str(owner): slot for owner, slot in sorted(offered.items())},
                    "receiving_slot": len(primary_after) - 1,
                    "party_count": party_count,
                    "primary_before": primary_before,
                    "primary_after": primary_after,
                    "peer_before": peer_before,
                    "peer_after": peer_after,
                    "copy_primary_map": _overworld(swap_states[0])["map_id"],
                    "copy_peer_map": _overworld(swap_states[1])["map_id"],
                    "primary_map": _overworld(final_states[0])["map_id"],
                    "peer_map": _overworld(final_states[1])["map_id"],
                    "post_trade_frames": post_frames,
                    "evolution_events": evolution,
                    "completion_menu_keys": [
                        _menu(state)["watched_keys"] for state in final_states
                    ],
                    "step_chunk": STEP_CHUNK,
                },
                sort_keys=True,
            ),
            flush=True,
        )

        await client.eof()


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

    This row is driven from the registered ``cable_club-battle.state`` fixture,
    whose observable state is the same pre-connection Cable Club attendant tile
    as the ordinary fixture with a six-member party.  Both claims are checked
    separately and against ROM-owned reads: the cursor slot each owner actually
    rested on when it confirmed the offer is observed on the live
    ``.playerMonMenu`` before the press, and the post-trade digest list must
    equal the source-defined compaction result.  The naive in-place model is
    computed in the test and required to *disagree*, so the oracle cannot pass
    by accident on a party whose records happen to be uniform.
    """
    if os.environ.get("POKERED_SKIP_SHA1"):
        pytest.fail("POKERED_SKIP_SHA1 must not be used for real-ROM evidence")

    primary_asset = _require_assets(version, MULTI_MEMBER_FIXTURE)
    peer_asset = _require_assets(peer_version, MULTI_MEMBER_FIXTURE)

    async with _stdio_server(tmp_path, primary_asset, peer_asset) as client:
        primary_before, peer_before = await _fixture_pair(
            client, primary_asset, fixture=MULTI_MEMBER_FIXTURE
        )
        assert len(primary_before) == MULTI_MEMBER_PARTY_COUNT, primary_before
        assert len(peer_before) == MULTI_MEMBER_PARTY_COUNT, peer_before
        # The two orientations are ordered roles: in row 1 the primary drives
        # its cursor to a moved slot while the peer confirms the default slot 0,
        # and row 2 swaps which owner moves.  Each row therefore proves a
        # non-default cursor was read from the live ROM menu for one owner, and
        # the two outgoing roles stay distinct from each other and from the
        # appended receiving slot.
        assert 0 <= primary_slot < len(primary_before), primary_slot
        assert 0 <= peer_slot < len(peer_before), peer_slot
        assert primary_slot != peer_slot, (primary_slot, peer_slot)
        assert primary_before[primary_slot]["digest"] != peer_before[peer_slot]["digest"], (
            version,
            peer_version,
        )

        await _enter_trade_flow(client)
        party_count = len(primary_before)

        (
            records,
            swap_states,
            final_states,
            post_frames,
            evolution,
            offered,
        ) = await _drive_trade(
            client,
            party_count=party_count,
            primary_before=primary_before,
            peer_before=peer_before,
            primary_slot=primary_slot,
            peer_slot=peer_slot,
        )
        primary_after = _records_from_payload(records[0])
        peer_after = _records_from_payload(records[1])

        # The intended slot was observed on the ROM menu, not inferred from the
        # result: ``offered`` is recorded before the confirming press.
        assert offered == {0: primary_slot, 1: peer_slot}, offered

        _assert_exact_exchange(
            primary_before,
            primary_after,
            outgoing_slot=primary_slot,
            incoming_digest=peer_before[peer_slot]["digest"],
            incoming_species=peer_before[peer_slot]["species"],
            label="primary-multi",
        )
        _assert_exact_exchange(
            peer_before,
            peer_after,
            outgoing_slot=peer_slot,
            incoming_digest=primary_before[primary_slot]["digest"],
            incoming_species=primary_before[primary_slot]["species"],
            label="peer-multi",
        )
        # The compaction model must be falsifiable on this very fixture: an
        # in-place replacement at the outgoing index would produce a different
        # digest list, so require the observed list to differ from it.
        in_place_primary = [record["digest"] for record in primary_before]
        in_place_primary[primary_slot] = peer_before[peer_slot]["digest"]
        assert [record["digest"] for record in primary_after] != in_place_primary, (
            "compaction oracle is not falsifiable on this fixture",
            primary_after,
        )
        assert len(primary_after) == len(primary_before), primary_after
        assert len(peer_after) == len(peer_before), peer_after

        print(
            "MCP_TRADE_RECORDS_ROM "
            + json.dumps(
                {
                    "version": version,
                    "peer_version": peer_version,
                    "transport": "local_pair",
                    "runtime_mode": _child_runtime_mode(),
                    "fixture": MULTI_MEMBER_FIXTURE,
                    "fixture_sha1": _fixture_sha1(primary_asset),
                    "peer_fixture_sha1": _fixture_sha1(peer_asset),
                    "party_count": party_count,
                    "outgoing_slot": primary_slot,
                    "peer_outgoing_slot": peer_slot,
                    "receiving_slot": len(primary_after) - 1,
                    "offered_slots": {str(owner): slot for owner, slot in sorted(offered.items())},
                    "primary_before": primary_before,
                    "primary_after": primary_after,
                    "peer_before": peer_before,
                    "peer_after": peer_after,
                    "copy_primary_map": _overworld(swap_states[0])["map_id"],
                    "copy_peer_map": _overworld(swap_states[1])["map_id"],
                    "primary_map": _overworld(final_states[0])["map_id"],
                    "peer_map": _overworld(final_states[1])["map_id"],
                    "post_trade_frames": post_frames,
                    "evolution_events": evolution,
                    "completion_menu_keys": [
                        _menu(state)["watched_keys"] for state in final_states
                    ],
                    "step_chunk": STEP_CHUNK,
                },
                sort_keys=True,
            ),
            flush=True,
        )

        await client.eof()


async def _enter_stats_trade_submenu(client, party_count):
    """Advance from the live party menu into each owner's STATS/TRADE sub-menu."""
    spent = 0
    while spent < 800:
        states = await _game_states(client)
        opened = [
            _menu(state)["watched_keys"] in (STATS_MENU_KEYS, TRADE_MENU_KEYS) for state in states
        ]
        if all(opened):
            _log("submenu", f"frames={spent}")
            return states, spent
        # Press A only on an owner whose live party menu is up and which has not
        # opened the sub-menu yet: A on the TRADE entry would confirm the offer,
        # so a blind mash would commit instead of cancelling.
        if not opened[0] and _player_mon_menu_live(states[0], party_count):
            await _press(client, "a", duration=4)
        if not opened[1] and _player_mon_menu_live(states[1], party_count):
            await _peer_press(client, "a", duration=4)
        await _link_step(client, STEP_CHUNK)
        spent += STEP_CHUNK
    states = await _game_states(client)
    raise AssertionError(
        f"STATS/TRADE sub-menu never opened on both owners; states={json.dumps(states)}"
    )


async def _back_out_of_submenu(client, party_count):
    """Press B on both owners and require the live party menu to return."""
    await _press_both(client, "b", duration=4)
    return await _advance_until(
        client,
        lambda state: _player_mon_menu_live(state, party_count),
        budget=400,
        prompt="B did not return both owners to the trade party menu",
    )


def _digests(records):
    return [record["digest"] for record in records]


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
    fixture read, plus an unchanged completion-event count.  The flow must also
    remain usable: the same session then completes the real exchange from the
    restored menu, which is the source-defined recovery the issue asks for in
    place of an unsupported rollback guarantee.
    """
    if os.environ.get("POKERED_SKIP_SHA1"):
        pytest.fail("POKERED_SKIP_SHA1 must not be used for real-ROM evidence")

    primary_asset = _require_assets(version)
    peer_asset = _require_assets(peer_version)

    async with _stdio_server(tmp_path, primary_asset, peer_asset) as client:
        primary_before, peer_before = await _fixture_pair(
            client, primary_asset, fixture="cable_club.state"
        )
        await _enter_trade_flow(client)
        party_count = len(primary_before)
        _, menu_frames = await _await_party_menu(client, party_count)
        _, submenu_frames = await _enter_stats_trade_submenu(client, party_count)
        _, back_frames = await _back_out_of_submenu(client, party_count)

        cancelled = await _party_records(client)
        primary_cancelled = _records_from_payload(cancelled[0])
        peer_cancelled = _records_from_payload(cancelled[1])
        assert _digests(primary_cancelled) == _digests(primary_before), primary_cancelled
        assert _digests(peer_cancelled) == _digests(peer_before), peer_cancelled
        assert (await _event_names(client)).count(EVOLUTION_EVENT) == 0

        (
            records,
            swap_states,
            final_states,
            post_frames,
            evolution,
            offered,
        ) = await _drive_trade(
            client,
            party_count=party_count,
            primary_before=primary_before,
            peer_before=peer_before,
        )
        primary_after = _records_from_payload(records[0])
        peer_after = _records_from_payload(records[1])
        _assert_exact_exchange(
            primary_before,
            primary_after,
            outgoing_slot=0,
            incoming_digest=peer_before[0]["digest"],
            incoming_species=peer_before[0]["species"],
            label="primary-after-cancel",
        )
        _assert_exact_exchange(
            peer_before,
            peer_after,
            outgoing_slot=0,
            incoming_digest=primary_before[0]["digest"],
            incoming_species=primary_before[0]["species"],
            label="peer-after-cancel",
        )

        print(
            "MCP_TRADE_RECORDS_ROM "
            + json.dumps(
                {
                    "version": version,
                    "peer_version": peer_version,
                    "transport": "local_pair",
                    "scenario": "cancel_before_commitment",
                    "runtime_mode": _child_runtime_mode(),
                    "fixture": "cable_club.state",
                    "fixture_sha1": _fixture_sha1(primary_asset),
                    "peer_fixture_sha1": _fixture_sha1(peer_asset),
                    "party_count": party_count,
                    "menu_frames": menu_frames,
                    "submenu_frames": submenu_frames,
                    "back_frames": back_frames,
                    "cancelled_primary": [record["digest"] for record in primary_cancelled],
                    "cancelled_peer": [record["digest"] for record in peer_cancelled],
                    "primary_before": primary_before,
                    "primary_after": primary_after,
                    "peer_before": peer_before,
                    "peer_after": peer_after,
                    "offered_slots": {str(owner): slot for owner, slot in sorted(offered.items())},
                    "copy_primary_map": _overworld(swap_states[0])["map_id"],
                    "copy_peer_map": _overworld(swap_states[1])["map_id"],
                    "primary_map": _overworld(final_states[0])["map_id"],
                    "peer_map": _overworld(final_states[1])["map_id"],
                    "post_trade_frames": post_frames,
                    "evolution_events": evolution,
                    "step_chunk": STEP_CHUNK,
                },
                sort_keys=True,
            ),
            flush=True,
        )

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

    primary_asset = _require_assets(version)
    peer_asset = _require_assets(peer_version)

    async with _stdio_server(tmp_path, primary_asset, peer_asset) as client:
        primary_before, _ = await _fixture_pair(client, primary_asset, fixture="cable_club.state")
        await _pair(client)
        status = await client.tool("link_status")
        assert status["paired"] is True, status
        print(
            "MCP_TRADE_RECORDS_ROM "
            + json.dumps(
                {
                    "version": version,
                    "peer_version": peer_version,
                    "scenario": "eof_during_setup",
                    "runtime_mode": _child_runtime_mode(),
                    "fixture": "cable_club.state",
                    "fixture_sha1": _fixture_sha1(primary_asset),
                    "peer_fixture_sha1": _fixture_sha1(peer_asset),
                    "party_count": len(primary_before),
                    "paired": True,
                    "cleanup": "stdin_eof_bounded_exit_0_no_live_group",
                },
                sort_keys=True,
            ),
            flush=True,
        )
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
    """EOF inside the live trade sub-menu must exit bounded, clean, and unswapped.

    This is the mid-flow disconnect: both owners have entered the Trade Center,
    walked to the hidden trade trigger, and opened the STATS/TRADE sub-menu, but
    neither has confirmed an offer.  The client reads both parties (proving no
    partial record swap has happened at the interruption point), then closes its
    stdin.  ``RomClient.eof`` requires the server to exit 0 inside the exit bound
    with no trailing frame and no surviving process group, which is the bounded
    owned cleanup the issue asks for.

    The deeper post-commitment interruption (EOF after the record copy) is not
    claimed here: it stays declared work until a rerun proves it.
    """
    if os.environ.get("POKERED_SKIP_SHA1"):
        pytest.fail("POKERED_SKIP_SHA1 must not be used for real-ROM evidence")

    primary_asset = _require_assets(version)
    peer_asset = _require_assets(peer_version)

    async with _stdio_server(tmp_path, primary_asset, peer_asset) as client:
        primary_before, peer_before = await _fixture_pair(
            client, primary_asset, fixture="cable_club.state"
        )
        await _enter_trade_flow(client)
        party_count = len(primary_before)
        _, menu_frames = await _await_party_menu(client, party_count)
        _, submenu_frames = await _enter_stats_trade_submenu(client, party_count)
        interrupted = await _party_records(client)
        primary_interrupted = _records_from_payload(interrupted[0])
        peer_interrupted = _records_from_payload(interrupted[1])
        assert _digests(primary_interrupted) == _digests(primary_before), primary_interrupted
        assert _digests(peer_interrupted) == _digests(peer_before), peer_interrupted
        print(
            "MCP_TRADE_RECORDS_ROM "
            + json.dumps(
                {
                    "version": version,
                    "peer_version": peer_version,
                    "scenario": "eof_during_active_trade",
                    "runtime_mode": _child_runtime_mode(),
                    "fixture": "cable_club.state",
                    "fixture_sha1": _fixture_sha1(primary_asset),
                    "peer_fixture_sha1": _fixture_sha1(peer_asset),
                    "party_count": party_count,
                    "menu_frames": menu_frames,
                    "submenu_frames": submenu_frames,
                    "interrupted_primary": [record["digest"] for record in primary_interrupted],
                    "interrupted_peer": [record["digest"] for record in peer_interrupted],
                    "cleanup": "stdin_eof_bounded_exit_0_no_live_group",
                },
                sort_keys=True,
            ),
            flush=True,
        )
        await client.eof()
