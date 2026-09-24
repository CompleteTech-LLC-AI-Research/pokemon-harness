"""Shared constants and generic helpers for the real-ROM battle modules.

Split from ``tests/test_mcp_battle_phase_rom.py`` (#122) with no behavior
change: the pinned constants, the sparse-``CHILD`` stdio launcher, the admitted
fixture admission helpers, and the menu primitives moved here verbatim.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from scripts.probe_timed_rom_pair import resolve_assets
from tests._rom_assets import find_rom_root, fixture_path
from tests.test_mcp_timed_rom import PIPE_CAP, RomClient

ROOT = Path(__file__).resolve().parents[1]
FAMILIES = ("red_color", "blue_color", "yellow")
MANIFEST_PATH = ROOT / "release-evidence" / "fixture-manifest.json"
# Fixture-manifest variant for the ROM a boundary pair was captured on: the
# color-patched Red/Blue ROM and the canonical Yellow CGB ROM, i.e. the same
# ROM/SYM pair ``tests/test_pyboy_link_session_roms._ROM_PATHS`` opens.
VARIANT_BY_FAMILY = {"red": "color", "blue": "color", "yellow": "cgb"}
FORCED_REPLACEMENT_PHASE = 4
# Boundary-drive geometry and finite budget.  The drive stops as soon as both
# reads show the battle ended; the budget only bounds a stalled scenario.  The
# deciding knockout is one ordinary ROM turn, but the drive that reaches it
# from the admitted boundary can span a few of them, because the knockout may
# open a forced replacement first, so the cap covers the whole drive.
BOUNDARY_STEP = 4
BOUNDARY_INPUT_SPACING = 8
BOUNDARY_DRIVE_BUDGET = 10000
MOVE_MENU_MAX_ITEM = 5
# ``ResidualEffects1``/``ResidualEffects2`` moves execute by jumping to
# ``JumpMoveEffect`` from ``ExecutePlayerMove``/``ExecuteEnemyMove``; the
# handler returns straight to the caller, so those turns never reach
# ``Execute*MoveDone``.  Growl (45) is exposed by the admitted Blue and Yellow
# pre-terminal pairs at the same slot on both owners, so one real shared turn
# can be settled without any RAM write: the move lowering the attacker's stat
# stage and the bracket closing again are the ROM-owned proof.
STATUS_MOVE_ID = 45

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
# Key masks the ROM watches in the battle command menu, one per column
# (``engine/battle/core.asm:2177`` PAD_RIGHT|PAD_A, ``:2210`` PAD_LEFT|PAD_A),
# each written with ``wMaxMenuItem == 1``.  A battle party menu that has
# already taken its input leaves its own ``A|B`` mask and
# ``wMaxMenuItem == wPartyCount - 1`` behind, which for a two-mon party is the
# command menu's geometry exactly, so the mask is what separates the two for a
# consumer that only has the public payload after a reload.
COMMAND_MENU_WATCHED_KEYS = (0x11, 0x21)
# Key mask the ROM watches in the battle party menu (A|B).  Together with the
# move menu's mask (UP|DOWN|A|B) it is what separates the two menus for a
# consumer that only has the public payload after a reload.
PARTY_MENU_WATCHED_KEYS = 0x03
# Key mask the ROM watches in the battle move menu (UP|DOWN|A|B).  The move
# menu's cursor geometry is not enough on its own: a battle party menu that has
# just confirmed a replacement leaves ``current=1, max=5`` behind, which is
# exactly the geometry the move branch below steers, so a stale party menu would
# otherwise be driven at as if it were a live move menu.
MOVE_MENU_WATCHED_KEYS = 0xC3
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
        # Move-execution brackets: the ROM-owned proof that an ordinary FIGHT
        # turn is being resolved right now (``wActionResultOrTookBattleTurn``
        # is cleared by ExecutePlayerMoveDone, so RAM alone cannot report it).
        "ExecutePlayerMove",
        "ExecuteEnemyMove",
        "ExecutePlayerMoveDone",
        "ExecuteEnemyMoveDone",
        # ``Execute*MoveDone`` is not the only return from either move routine:
        # a status move (``ResidualEffects1``/``2``) jumps to ``JumpMoveEffect``
        # and returns from there, and a lethal hit returns beside the faint
        # check, so the bracket also closes on the post-move continuations those
        # returns land on -- ``HandlePoisonBurnLeechSeed`` (called after each
        # move returns), the faint handlers (which run before ``ChooseNextMon``
        # opens the replacement menu), ``MainInBattleLoop`` (the per-turn entry
        # a finished turn jumps back to) and ``EndOfBattle`` (the escape/run
        # tail).  These are observations of the same session, not new inputs.
        "HandlePoisonBurnLeechSeed",
        "HandlePlayerMonFainted",
        "HandleEnemyMonFainted",
        "EndOfBattle",
    }
)
# The close labels a move routine can reach *without* ever touching
# ``Execute*MoveDone``.  ``HandlePoisonBurnLeechSeed`` is the call every move
# return lands in, the two faint handlers are the lethal-hit continuations
# that run ``ChooseNextMon`` before the replacement menu appears,
# ``MainInBattleLoop`` is the per-turn entry a finished turn jumps back to, and
# ``EndOfBattle`` is the escape/run tail.  The whole set is asserted as a
# subset of the session's registered evidence so that dropping any one of them
# fails here instead of silently falling back on the parser's menu
# reconciliation, which is what let a real status move or knockout report a
# contradictory phase before.
_RESOLUTION_CONTINUATION_LABELS = frozenset(
    {
        "HandlePoisonBurnLeechSeed",
        "HandlePlayerMonFainted",
        "HandleEnemyMonFainted",
        "MainInBattleLoop",
        "EndOfBattle",
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
        # Consulted only by the forced-replacement guard, which requires the
        # ROM's live battle party menu (the only positive proof that
        # ChooseNextMon is awaiting input) and a living party member.
        "wPartyMenuTypeOrMessageID",
        "wPartyMenuAnimMonEnabled",
        "wPartyCount",
        "wPartyMon1HP",
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
import os
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
    # The acceptance evidence claims which PyBoy runtime produced it, so the
    # child proves its own runtime instead of trusting the parent's label: the
    # native runtime must load the compiled extension and the source runtime
    # must load the vendored ``.py`` tree from *this* checkout.
    import pyboy
    import pyboy.core.serial as _serial

    expected = os.environ.get("POKERED_EXPECT_PYBOY_KIND")
    vendor = repo / "vendor" / "pyboy-src"
    for label, module in (("pyboy", pyboy), ("pyboy.core.serial", _serial)):
        origin = Path(module.__file__).resolve()
        if expected == "compiled":
            assert origin.suffix == ".so", (label, origin)
            assert not origin.is_relative_to(repo), (label, origin)
        elif expected == "source":
            assert origin.suffix == ".py", (label, origin)
            assert origin.is_relative_to(vendor), (label, origin)
        else:
            raise SystemExit(f"missing POKERED_EXPECT_PYBOY_KIND for {label}")
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
        assert primary.enable_battle_menu_observation()
        # The ACTION_RESOLUTION phase is only observable through the session's
        # move-execution hooks, so the child must install them exactly like
        # ``mcp_server.main`` does; otherwise the phase could never be read
        # over the public surface even while the ROM executes a move.
        assert primary.enable_battle_resolution_observation()
        # The terminal result is only promoted from the bytes the ROM itself
        # held at ``EndOfBattle`` entry, so the harness must install that hook
        # exactly like ``mcp_server.main`` does.
        assert primary.enable_battle_end_observation()
        register_default_hooks(peer)
        assert peer.enable_battle_menu_observation()
        assert peer.enable_battle_resolution_observation()
        assert peer.enable_battle_end_observation()
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
    """Return validated pinned assets plus the immutable battle fixture bytes.

    The battle fixture is admitted through its fixture-manifest row exactly
    like a boundary pair member: the row records the size, SHA-1, and SHA-256
    of the immutable bytes and the ROM/symbol pair it was captured on, and the
    bytes on disk must match.  Validating only the ordinary ``cable_club``
    fixture and then substituting different battle bytes would let a modified
    fixture into the session unchecked.
    """
    family = version.split("_")[0]
    variant = VARIANT_BY_FAMILY[family]
    rows = _manifest_rows()
    row = rows.get(f"{family}-{variant}-battle")
    assert row is not None, f"no admitted battle fixture row for {family}"
    assert row["kind"] == "battle", row

    assets = resolve_assets(version, ROOT)
    rom = assets["rom"]
    sym = assets["sym"]
    rom_relative, sym_relative = _pinned_relatives(rom, sym)
    battle_path = fixture_path(family, Path(row["path"]).name, project_root=ROOT)
    assert Path(row["path"]) == Path(family) / battle_path.name, row["path"]
    missing = [str(path) for path in (rom, sym, battle_path) if not path.is_file()]
    if missing:
        pytest.skip("missing BYO battle assets: " + ", ".join(missing))
    assets["state"] = _admit_fixture(row, battle_path, rom_relative, sym_relative, rom, sym)
    return assets


def _pinned_relatives(rom, sym):
    """Return the manifest-relative ROM and symbol paths for a pinned pair."""
    rom_root = find_rom_root(ROOT)
    return (
        (Path("rom") / rom.relative_to(rom_root)).as_posix(),
        (Path("rom") / sym.relative_to(rom_root)).as_posix(),
    )


def _admit_fixture(row, path, rom_relative, sym_relative, rom, sym):
    """Validate one immutable fixture against its manifest row; return bytes.

    The row is the admission contract: size, SHA-1, SHA-256, and the exact
    ROM/symbol bindings must all match the bytes and pinned assets on disk.
    A mismatch is an input-contract violation (tampered or stale fixture
    bytes), so it raises ``ValueError`` rather than asserting.
    """
    payload = path.read_bytes()
    if len(payload) != row["size_bytes"]:
        raise ValueError(
            f"fixture size mismatch for {path}: "
            f"{len(payload)} bytes on disk, manifest declares {row['size_bytes']}"
        )
    digest_sha1 = hashlib.sha1(payload).hexdigest()
    if digest_sha1 != row["sha1"]:
        raise ValueError(
            f"fixture sha1 mismatch for {path}: "
            f"{digest_sha1} on disk, manifest declares {row['sha1']}"
        )
    digest_sha256 = hashlib.sha256(payload).hexdigest()
    if digest_sha256 != row["sha256"]:
        raise ValueError(
            f"fixture sha256 mismatch for {path}: "
            f"{digest_sha256} on disk, manifest declares {row['sha256']}"
        )
    rom_sha1 = hashlib.sha1(rom.read_bytes()).hexdigest()
    sym_sha1 = hashlib.sha1(sym.read_bytes()).hexdigest()
    if row["expected_rom"] != {"path": rom_relative, "sha1": rom_sha1}:
        raise ValueError(
            f"fixture ROM binding mismatch for {path}: "
            f"manifest declares {row['expected_rom']}, "
            f"resolved {rom_relative} ({rom_sha1})"
        )
    if row["expected_symbols"] != {"path": sym_relative, "sha1": sym_sha1}:
        raise ValueError(
            f"fixture symbol binding mismatch for {path}: "
            f"manifest declares {row['expected_symbols']}, "
            f"resolved {sym_relative} ({sym_sha1})"
        )
    return payload


def _manifest_rows():
    """Return the fixture-manifest rows keyed by id."""
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {row["id"]: row for row in document["fixtures"]}


def _pyboy_runtime_kind() -> str:
    """Which PyBoy runtime this test process loaded: ``compiled`` or ``source``.

    The acceptance evidence records the runtime that produced it, so the
    parent must not label the pair from the interpreter path alone: it asks the
    import system what ``pyboy`` actually resolves to.  A compiled extension
    (``.so``) is the native runtime; a vendored ``.py`` tree is the source
    runtime.  The child re-checks the same fact so a mislabelled run fails
    instead of publishing mislabelled evidence.
    """
    origin = Path(importlib.util.find_spec("pyboy").origin).resolve()
    if origin.suffix == ".so":
        return "compiled"
    if origin.suffix == ".py":
        return "source"
    raise AssertionError(f"unexpected PyBoy origin: {origin}")


def _boundary_assets(version, slug):
    """Return the pinned ROM/SYM plus one admitted boundary fixture pair.

    The fixture-manifest row is the admission contract: the row records the
    size, SHA-1, and SHA-256 of every immutable pair member and the exact
    ROM/symbol pair the pair was captured on.  The bytes on disk must match the
    row, and the resolved ROM/symbol file must be the pinned pair the row names
    (``resolve_assets`` validates that pair against ``VERSIONS.md``); a
    mismatch fails the test instead of loading unverified bytes.
    """
    family = version.split("_")[0]
    variant = VARIANT_BY_FAMILY[family]
    rows = _manifest_rows()
    primary_row = rows.get(f"{family}-{variant}-{slug}")
    peer_row = rows.get(f"{family}-{variant}-{slug}-peer")
    assert primary_row is not None, f"no admitted boundary fixture row for {family} {slug}"
    assert peer_row is not None, f"no admitted boundary peer row for {family} {slug}"
    assert primary_row["kind"] == peer_row["kind"] == "boundary", (primary_row, peer_row)

    assets = resolve_assets(version, ROOT)
    rom = assets["rom"]
    sym = assets["sym"]
    rom_relative, sym_relative = _pinned_relatives(rom, sym)
    primary_path = fixture_path(family, Path(primary_row["path"]).name, project_root=ROOT)
    peer_path = fixture_path(family, Path(peer_row["path"]).name, project_root=ROOT)
    assert Path(primary_row["path"]) == Path(family) / primary_path.name, primary_row["path"]
    assert Path(peer_row["path"]) == Path(family) / peer_path.name, peer_row["path"]

    paths = [rom, sym, primary_path, peer_path]
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        pytest.skip("missing admitted boundary assets: " + ", ".join(missing))

    primary = _admit_fixture(primary_row, primary_path, rom_relative, sym_relative, rom, sym)
    peer = _admit_fixture(peer_row, peer_path, rom_relative, sym_relative, rom, sym)
    return {
        "family": family,
        "rom": rom,
        "sym": sym,
        "primary": primary,
        "peer": peer,
        "row": primary_row,
        "peer_row": peer_row,
    }


@asynccontextmanager
async def _stdio_server(tmp_path, asset, *, name="server"):
    """Launch one stdio server whose child preloads the peer fixture bytes.

    The public MCP tool surface has no peer-load operation, so the peer state
    is preloaded by the child before it serves.  The primary fixture is always
    loaded through the public ``load_state`` tool by the test itself.
    """
    family = asset["family"]
    directory = tmp_path / name
    directory.mkdir()
    copied = {}
    for key in ("rom", "sym"):
        copied[key] = directory / asset[key].name
        shutil.copyfile(asset[key], copied[key])
    peer_state = directory / "peer-cable_club-battle.state"
    peer_state.write_bytes(asset.get("peer", asset.get("state")))
    env = {key: value for key, value in os.environ.items() if not key.startswith("POKERED_")}
    # PyBoy runtime selection follows the interpreter, not a hard-coded
    # PYTHONPATH: the native runtime must load its compiled extension, and the
    # source runtime must load this checkout's vendored ``.py`` tree.  Pinning
    # ``vendor/pyboy-src`` unconditionally made the "native" acceptance run
    # silently execute source PyBoy, so the path is added only for the source
    # runtime and the child re-verifies the origin of every PyBoy module.
    runtime_kind = _pyboy_runtime_kind()
    pythonpath = [str(ROOT / "src")]
    if runtime_kind == "source":
        pythonpath.insert(0, str(ROOT / "vendor" / "pyboy-src"))
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    env["POKERED_EXPECT_PYBOY_KIND"] = runtime_kind
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


