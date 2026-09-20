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
from tests._rom_assets import find_fixture_root, find_rom_root, fixture_path
from tests.test_mcp_timed_rom import PIPE_CAP, RomClient
from tests.test_mcp_timed_stdio import CALL_BOUND

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


# --- admitted battle-boundary scenarios ------------------------------------
#
# The boundary helpers below drive a *reloaded* pair.  A load resets the
# session's observational menu state to unknown, so the drive cannot use the
# hook-derived ``menu_open`` flag the way the live-entry scenarios do; it uses
# the ROM's own menu geometry (``wCurrentMenuItem``/``wMaxMenuItem``/
# ``wMenuWatchedKeys``) read through the public ``pokered://game-state``
# resource, which is the same ROM-owned evidence the capture producer used to
# snapshot the boundary.


def _menu_fields(state):
    """Return ``(current_item, max_item, watched_keys)`` from the public payload."""
    menu = _menu(state)
    current = menu.get("current_item")
    maximum = menu.get("max_item")
    watched = menu.get("watched_keys")
    if type(current) is int and type(maximum) is int and type(watched) is int:
        return current, maximum, watched
    return None


def _command_menu_ready(state):
    """True when the ROM's battle command menu geometry is live."""
    fields = _menu_fields(state)
    return (
        fields is not None
        and 0 <= fields[0] <= 1
        and fields[1] == COMMAND_MENU_MAX_ITEM
        and fields[2] in COMMAND_MENU_WATCHED_KEYS
    )


def _replacement_slot(state):
    """Index of the first living party slot while this owner is choosing one.

    ``None`` means the ROM is not asking this owner to replace a combatant:
    either its own combatant still has HP, or the battle party has no living
    member left to send out.
    """
    active = _active_mon(state)
    if active is None or active["hp"] != 0:
        return None
    living = [slot for slot, mon in enumerate(state["party"]["mons"]) if mon["hp"] > 0]
    return living[0] if living else None


def _replacement_button(state):
    """Return the next button for a live battle party menu, or ``None``.

    ``wMenuWatchedKeys`` is the key mask the ROM itself is watching, and it is
    what tells the two menus apart after a reload: ``3`` (A|B) is the battle
    party menu and ``195`` (UP|DOWN|A|B) is the move menu.  A party menu needs a
    *party* target rather than the move slot :func:`_boundary_button` steers to:
    confirming the move slot selects a fainted combatant, which the ROM refuses,
    and the side then never leaves the menu.  The living slot the capture
    producer used is derivable from public data alone (``party.mons[].hp``), so
    the drive stays on the public surface.
    """
    fields = _menu_fields(state)
    if fields is None or fields[2] != PARTY_MENU_WATCHED_KEYS:
        return None
    target = _replacement_slot(state)
    if target is None:
        return None
    current = fields[0]
    if current == target:
        return "a"
    return "down" if current < target else "up"


def _boundary_button(state, target_slot):
    """Return the next public button to inject for one owner, or ``None``.

    The cursor geometry is the ROM's own: FIGHT is entry 0 of the command menu
    (entry 1 is ITEM) and the move menu cursor is one past the move slot the
    ROM confirms.  The move branch additionally requires the move menu's own
    ``MOVE_MENU_WATCHED_KEYS`` mask, because the geometry above also matches the
    leftovers of a battle party menu that has already taken its input; without
    that witness the drive would press A on a closed menu and the stray input
    would be consumed by the next command menu.  The command branch requires
    the command menu's own mask (``COMMAND_MENU_WATCHED_KEYS``) for the same
    reason: a closed battle party menu of a two-mon party leaves command-menu
    geometry behind, and only the mask separates the two.  ``None`` means no
    menu is currently taking A input, so nothing is injected and the ROM keeps
    running its own animation or text.
    """
    fields = _menu_fields(state)
    if fields is None or not fields[2] & MENU_WATCHED_A:
        return None
    current, maximum, watched = fields
    if (
        0 <= current <= 1
        and maximum == COMMAND_MENU_MAX_ITEM
        and watched in COMMAND_MENU_WATCHED_KEYS
    ):
        # Never confirm ITEM: the admitted pair is replayed as a FIGHT turn.
        return "a" if current == 0 else "up"
    if watched == MOVE_MENU_WATCHED_KEYS and 1 <= current < maximum <= MOVE_MENU_MAX_ITEM:
        target = target_slot + 1
        if current == target:
            return "a"
        return "down" if current < target else "up"
    return None


async def _drive_effect_turn(client, slots, *, budget=SETTLEMENT_BUDGET):
    """Drive both owners through one shared turn that selects ``slots``.

    The buttons and every decision come from the public ``press`` /
    ``link_peer_press`` / ``link_step`` tools and the public
    ``pokered://game-state`` resource, exactly like the boundary drive.  The
    loop stops when each owner's own read shows the selected slot's PP
    decremented *and* a live command menu, which is the ROM-owned return
    boundary the resolution observation must not contradict.
    """
    before = await _states(client)
    pp_before = [_active_pp(state) for state in before]
    hp_before = [_active_hp(state) for state in before]
    assert len(slots) == len(before), (slots, before)
    assert all(value is not None for value in pp_before), before
    assert all(value is not None for value in hp_before), before

    consumed = [False] * len(before)
    phases_seen = set()
    next_input = [0] * len(before)
    boundary = None
    frames = 0
    while frames < budget:
        states = await _states(client)
        for index, state in enumerate(states):
            battle = _battle(state)
            if battle["phase_valid"] is True:
                phases_seen.add(battle["phase"])
            if consumed[index]:
                continue
            after = _active_pp(state)
            if after is not None and after[slots[index]] < pp_before[index][slots[index]]:
                consumed[index] = True
        if all(consumed) and all(
            _command_menu_ready(state) and _battle(state)["menu_open"] is True for state in states
        ):
            boundary = states
            break
        for index, state in enumerate(states):
            if consumed[index] or frames < next_input[index]:
                continue
            button = _boundary_button(state, slots[index])
            if button is None:
                continue
            if index == 0:
                await _press(client, button)
            else:
                await _peer_press(client, button)
            next_input[index] = frames + BOUNDARY_INPUT_SPACING
        await _link_step(client, BOUNDARY_STEP)
        frames += BOUNDARY_STEP
    if boundary is None:
        states = await _states(client)
        raise AssertionError(
            "the selected turn never returned both owners to a command menu "
            f"within {budget} paired frames: " + json.dumps([state["battle"] for state in states])
        )
    return {
        "frames": frames,
        "pp_before": pp_before,
        "hp_before": hp_before,
        "phases_seen": tuple(sorted(value for value in phases_seen if value is not None)),
        "boundary": boundary,
    }


async def _drive_through_live_replacement(client, slots, *, budget=BOUNDARY_DRIVE_BUDGET):
    """Drive the admitted pair through one live knockout and its replacement.

    The drive stops as soon as every owner that opened the ROM's battle party
    menu has answered it and is back in the live battle with a healthy
    combatant, so the regression covers the nonterminal knockout path (the
    fainted combatant is replaced and the battle continues) instead of the
    whole terminal drive.  ``replacements`` holds one row per read of a live
    battle party menu; ``answered[index]`` is the first read after that owner
    answered its menu, and stays ``None`` for an owner that never opened one.
    """
    replacements = []
    answered = [None] * len(slots)
    opened = [False] * len(slots)
    next_input = [0] * len(slots)
    frames = 0
    while frames < budget:
        states = await _states(client)
        for index, state in enumerate(states):
            battle = _battle(state)
            live = battle["raw_is_in_battle"] in BATTLE_KINDS
            if live and _replacement_button(state) is not None:
                opened[index] = True
                replacements.append(
                    {
                        "side": index,
                        "frames": frames,
                        "tick": state["epoch"]["tick"],
                        "phase": battle["phase"],
                        "phase_valid": battle["phase_valid"],
                        "resolution_open": battle["resolution_open"],
                        "raw_is_in_battle": battle["raw_is_in_battle"],
                        "active_hp": _active_hp(state),
                        "party_hp": [mon["hp"] for mon in state["party"]["mons"]],
                        "menu_evidence": battle["menu_evidence"],
                        "phase_evidence": battle["phase_evidence"],
                        "resolution_evidence": battle["resolution_evidence"],
                    }
                )
            elif (
                opened[index] and answered[index] is None and live and (_active_hp(state) or 0) > 0
            ):
                answered[index] = {
                    "side": index,
                    "frames": frames,
                    "tick": state["epoch"]["tick"],
                    "phase": battle["phase"],
                    "phase_valid": battle["phase_valid"],
                    "resolution_open": battle["resolution_open"],
                    "raw_is_in_battle": battle["raw_is_in_battle"],
                    "active_hp": _active_hp(state),
                    "party_hp": [mon["hp"] for mon in state["party"]["mons"]],
                    "phase_evidence": battle["phase_evidence"],
                }
        if any(opened) and all(
            answered[index] is not None for index, flag in enumerate(opened) if flag
        ):
            break
        for index, state in enumerate(states):
            if opened[index] and answered[index] is not None:
                continue
            if frames < next_input[index]:
                continue
            button = _replacement_button(state)
            if button is None:
                button = _boundary_button(state, slots[index])
            if button is None:
                continue
            if index == 0:
                await _press(client, button)
            else:
                await _peer_press(client, button)
            next_input[index] = frames + BOUNDARY_INPUT_SPACING
        await _link_step(client, BOUNDARY_STEP)
        frames += BOUNDARY_STEP
    return {
        "frames": frames,
        "opened": tuple(opened),
        "replacements": replacements,
        "answered": answered,
    }


async def _drive_boundary_turn(client, plan, *, budget=BOUNDARY_DRIVE_BUDGET):
    """Drive the reloaded pre-terminal pair until both owners leave the battle.

    Both owners are folded back into their own ROM command/move menus with the
    public ``press`` / ``link_peer_press`` / ``link_step`` tools only, and every
    decision is taken from a public ``pokered://game-state`` read.  An owner
    stops being driven as soon as its own read reports that ``wIsInBattle``
    left the live battle kinds, so that read stays the observed falling edge of
    the battle the session tracks; the other owner keeps being driven until its
    own read shows the same.

    A knockout that opens a forced replacement is folded in rather than
    abandoned: while the ROM is watching its party menu, the cursor is steered
    onto the first living slot (``_replacement_button``), which is the same
    target the capture producer drove the recorded turn with.  The drive
    therefore covers every ROM turn between the admitted boundary and the
    terminal return, bounded by ``budget``.
    """
    slots = [entry["slot"] for entry in plan]
    terminal = [None, None]
    injected = [[], []]
    frames = 0
    realign = [0, 0]
    while frames < budget and any(entry is None for entry in terminal):
        states = await _states(client)
        for index, state in enumerate(states):
            if terminal[index] is not None:
                continue
            if _battle(state)["raw_is_in_battle"] not in BATTLE_KINDS:
                terminal[index] = {
                    "frames": frames,
                    "state": state,
                    "buttons": tuple(injected[index]),
                }
        for index, state in enumerate(states):
            if terminal[index] is not None or frames < realign[index]:
                continue
            button = _replacement_button(state)
            if button is None:
                button = _boundary_button(state, slots[index])
            if button is None:
                continue
            if index == 0:
                await _press(client, button)
            else:
                await _peer_press(client, button)
            injected[index].append(button)
            realign[index] = frames + BOUNDARY_INPUT_SPACING
        if all(entry is not None for entry in terminal):
            break
        await _link_step(client, BOUNDARY_STEP)
        frames += BOUNDARY_STEP
    if any(entry is None for entry in terminal):
        states = await _states(client)
        raise AssertionError(
            "the admitted pre-terminal pair never reached the terminal return "
            f"within {budget} paired frames: " + json.dumps([state["battle"] for state in states])
        )
    return {"terminal": terminal, "frames": frames, "injected": injected}


def _assert_terminal_return(state, *, label):
    """Assert the documented terminal-return derivation for one owner."""
    battle = _battle(state)
    assert battle["raw_is_in_battle"] == 0, (label, battle)
    assert battle["kind"] == 0, (label, battle)
    assert battle["phase"] == TERMINAL_RETURN_PHASE, (label, battle)
    assert battle["phase_valid"] is True, (label, battle)
    assert {"wIsInBattle", "wBattleResult"} <= set(battle["phase_evidence"]), (label, battle)
    raw = battle["raw_battle_result"]
    assert type(raw) is int, (label, battle)
    expected = raw if raw in (1, 2) else None
    assert battle["terminal_result"] == expected, (label, battle)
    return battle


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
    resolution_evidence = set()
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
            resolution_evidence.update(battle.get("resolution_evidence") or ())
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
        "resolution_evidence": tuple(sorted(resolution_evidence)),
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
