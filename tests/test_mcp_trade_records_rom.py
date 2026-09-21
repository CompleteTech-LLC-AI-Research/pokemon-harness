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
"""

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
from tests._rom_assets import fixture_path, rom_path, sym_path
from tests.test_mcp_timed_rom import PIPE_CAP, RomClient

ROOT = Path(__file__).resolve().parents[1]
FAMILIES = ("red_color", "blue_color", "yellow")
# Issue #105 requires all nine *ordered* canonical orientations in both
# transports, not only a representative pair: every ordered combination of the
# three families, including the same-family rows.  The local rows drive one
# in-process pair through the public peer tools; the TCP rows run two separate
# installed servers joined by the public ``link_listen``/``link_connect`` pair,
# so each owner's records are that process's own read.
ORIENTATIONS = tuple((primary, peer) for primary in FAMILIES for peer in FAMILIES)


def _orientation_id(orientation):
    return f"{orientation[0]}-{orientation[1]}"


ORDINARY_FIXTURE = "cable_club.state"
# Manifest-registered battle-start fixture (``kind: battle``).  It starts on the
# same pre-connection tile as the ordinary fixture, and for Blue its own lead
# record differs from Blue's ordinary lead, so the Blue/Blue orientation pairs
# the two for a same-family byte-exact exchange proof.
BATTLE_FIXTURE = "cable_club-battle.state"
# Manifest-registered six-member fixture (``kind: slots``) that starts on the
# same pre-connection Cable Club attendant tile as the ordinary trade fixture
# while carrying a party whose six 44-byte records are pairwise distinct, so the
# issue's nonzero sender/receiver slot rows are driven from an admitted
# immutable fixture and the oracle can tell an intended slot from any other.
# The pins are still checked by ``resolve_assets``: only the unique verified
# ``slots`` row for each family whose basename matches is admitted.
MULTI_MEMBER_FIXTURE = "cable_club-slots.state"
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


def _orientation_fixtures(primary, peer):
    """Return the admitted fixture pair an orientation row offers.

    The manifest admits exactly one ``verified`` ordinary party, one ``verified``
    battle party, and one ``verified`` six-member ``slots`` party per family.
    Blue is the only family whose two *ordinary-sized* admitted rows hold
    different 44-byte records (its ordinary lead digest
    ``b23fd6f97c8ad67b5fa7c05002a32ace9e681cf610bb1aefe0a279dcf5b856c0`` against
    its battle record
    ``27d4b20751d2d852837c83c7c5950ea8abad9a043dad307c56249ca4dcda4bbd``), so
    pairing Blue's ordinary row with Blue's battle row is the one way a
    *same-family* orientation can offer two distinct admitted single-member
    records.  Every other orientation pairs the ordinary rows; the same-family
    Red/Red and Yellow/Yellow rows then offer byte-identical records and lean on
    the ROM-owned ``trade_received`` marker, as the module docstring describes.
    The nonzero-slot rows pull the pairwise-distinct ``slots`` party through
    ``MULTI_MEMBER_FIXTURE`` instead.
    """
    if primary == peer == "blue_color":
        return (ORDINARY_FIXTURE, BATTLE_FIXTURE)
    return (ORDINARY_FIXTURE, ORDINARY_FIXTURE)


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
# ``link_up`` form the pair through the public tools, and the listener's accept
# thread can still be booting its ROM when ``link_connect`` returns, so the
# client polls both ends for the published ``connected`` mode before walking.
LINK_UP_STATUS_TIMEOUT = 180.0
LINK_UP_STATUS_POLL_INTERVAL = 0.25
RECEPTIONIST_WALK_FRAMES = 60
LINK_MENU_BUDGET = 1600
LINK_MENU_QUIET_FRAMES = 100
LINK_MENU_BURST_ATTEMPTS = 1
WARP_BUDGET = 1200
WALK_ATTEMPTS = 4
WALK_STEP_FRAMES = 20
# Quiet trade-center rendezvous.  The repository's own two-process driver
# cannot walk onto the Cable Club trigger until the ROM has finished its entry
# script, so it drains the wire before walking
# (``tests/_tcp_trade_peer.py``: ``wait_for_wire_idle(stable_checks=4)`` plus a
# cooperative release rendezvous at the same boundary).  The public surface has
# no wire-idle reading, so this pair waits for the same condition the only way
# it can observe it: it keeps advancing both owners, without any input, until
# the ROM-owned trade-center state has stopped changing for
# ``WALK_STABLE_CHECKS`` consecutive slices and only then presses the facing
# direction.  Without this rendezvous a faster runtime can press while the
# entry script still owns input: the press is consumed, the ``ANY_FACING``
# hidden-event trigger never fires, and the row stalls at ``offered={}`` for
# its whole trade budget.
WALK_SETTLE_SLICE = 10
WALK_STABLE_CHECKS = 3
WALK_SETTLE_BUDGET = 400
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
# ROM-owned execution-hook name registered from ``_AddEnemyMonToPlayerParty``
# in ``DEFAULT_HOOKS``.  The Trade Center calls that routine exactly when it
# appends the peer's received 44-byte record to the player's own party, so the
# event is the copy itself rather than a menu or animation milestone.  It is
# required on *every* row, which is what keeps a byte-identical orientation
# falsifiable: a party that never copied a record never runs the routine.
TRADE_RECEIVED_EVENT = "trade_received"


class TradeRun(NamedTuple):
    """Every observation one completed trade row produced.

    Kept as a value object so the acceptance oracle is a pure function of
    observations: a regression can hand it a fabricated "menus and completion
    milestones, but no ROM-owned copy event" run and require rejection without
    running an emulator.
    """

    final_records: list
    copy_records: list
    copy_states: list
    final_states: list
    post_frames: int
    evolution: list
    offered: dict
    back_outs: int
    received: list



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


def _server_environment(runtime_mode):
    """Build the child env without shadowing the selected runtime.

    The launcher would otherwise unconditionally prepend ``vendor/pyboy-src``,
    forcing source ``.py`` modules even under the native runtime.  This mirrors
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


def _assert_invoking_runtime(runtime_mode):
    """Assert the runtime this acceptance process hands to the server.

    The server is launched with ``sys.executable`` and the environment built by
    :func:`_server_environment`, so the invoking interpreter's own PyBoy origin
    is exactly what the child resolves.  Asserting it here keeps the runtime
    claim honest without instrumenting the production entry point: a source row
    must import the vendored ``.py`` package and a native row must import the
    installed compiled extensions.
    """
    vendored = str((ROOT / "vendor" / "pyboy-src").resolve(strict=False))
    import pyboy
    import pyboy.core.serial

    origins = {"pyboy": pyboy.__file__, "serial": pyboy.core.serial.__file__}
    compiled_extensions = (".so", ".pyd")
    if runtime_mode == "cython":
        for origin in origins.values():
            assert origin.endswith(compiled_extensions), origins
            assert not origin.startswith(vendored), origins
    else:
        for origin in origins.values():
            assert origin.startswith(vendored), origins
            assert origin.endswith(".py"), origins
    return origins


def _server_env(primary_asset, peer_asset, runtime_mode, peer_state_path):
    """Build the documented ``POKERED_*`` launch contract for one server.

    Every input is named and pinned through the production entry point's own
    environment contract, including the peer fixture added by
    ``POKERED_PEER_STATE_PATH``/``POKERED_PEER_STATE_SHA1``.  No Session is
    constructed and no private ``load_state`` call is made by the acceptance
    client.
    """
    env = {
        key: value
        for key, value in _server_environment(runtime_mode).items()
        if not key.startswith("POKERED_")
    }
    env.update(
        {
            "POKERED_ROM_PATH": str(primary_asset["rom"]),
            "POKERED_ROM_VERSION": primary_asset["family"],
            "POKERED_ROM_SHA1": primary_asset["pins"]["expected_rom_sha1"],
            "POKERED_SYM_PATH": str(primary_asset["sym"]),
            "POKERED_SYM_SHA1": primary_asset["pins"]["expected_symbol_sha1"],
            "POKERED_VERSIONS_PATH": str(ROOT / "VERSIONS.md"),
        }
    )
    if peer_asset is not None:
        env.update(
            {
                "POKERED_PEER_ROM_PATH": str(peer_asset["rom"]),
                "POKERED_PEER_ROM_VERSION": peer_asset["family"],
                "POKERED_PEER_ROM_SHA1": peer_asset["pins"]["expected_rom_sha1"],
                "POKERED_PEER_SYM_PATH": str(peer_asset["sym"]),
                "POKERED_PEER_SYM_SHA1": peer_asset["pins"]["expected_symbol_sha1"],
                "POKERED_PEER_STATE_PATH": str(peer_state_path),
                "POKERED_PEER_STATE_SHA1": _fixture_sha1(peer_asset),
            }
        )
    return env


async def _launch_server(env):
    """Launch the installed ``pokered_harness.mcp_server`` entry point."""
    process = await asyncio.wait_for(
        asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pokered_harness.mcp_server",
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
    return RomClient(process)


@asynccontextmanager
async def _stdio_server(tmp_path, primary_asset, peer_asset):
    """Launch one stdio server with a pinned in-process peer fixture.

    The server is the production ``pokered_harness.mcp_server`` entry point,
    configured entirely through its documented launch contract: the primary
    ROM/SYM pins, the peer ROM/SYM pins, and the pinned peer fixture.  The
    primary fixture is still loaded through the public ``load_state`` tool by
    the acceptance client.
    """
    directory = tmp_path / "server"
    directory.mkdir()
    copied = {}
    for name in ("rom", "sym"):
        copied[f"primary_{name}"] = directory / primary_asset[name].name
        shutil.copyfile(primary_asset[name], copied[f"primary_{name}"])
        copied[f"peer_{name}"] = directory / peer_asset[name].name
        shutil.copyfile(peer_asset[name], copied[f"peer_{name}"])
    peer_state = directory / "peer-cable_club.state"
    peer_state.write_bytes(peer_asset["state"])
    runtime_mode = _child_runtime_mode()
    _assert_invoking_runtime(runtime_mode)
    primary = dict(primary_asset)
    primary["rom"] = copied["primary_rom"]
    primary["sym"] = copied["primary_sym"]
    peer = dict(peer_asset)
    peer["rom"] = copied["peer_rom"]
    peer["sym"] = copied["peer_sym"]
    env = _server_env(primary, peer, runtime_mode, peer_state)
    client = None
    original_failure = None
    try:
        client = await _launch_server(env)
        yield client
    except BaseException as exc:
        original_failure = exc
        raise
    finally:
        if client is not None:
            # A cleanup failure is a secondary diagnostic: the gameplay
            # failure that triggered the teardown stays the primary error, and
            # the cleanup problem is attached as a note.  Only a body that
            # succeeded may be replaced by an unconditional cleanup failure.
            try:
                await client.cleanup()
            except BaseException as cleanup_error:
                if original_failure is None:
                    raise
                original_failure.add_note(
                    f"MCP_TRADE_RECORDS_CLEANUP_ERROR {cleanup_error!r}"
                )


async def _read_resource(client, uri):
    resource = await client.request("resources/read", {"uri": uri})
    return json.loads(resource["contents"][0]["text"])


class LocalPair:
    """One MCP stdio server that owns both sessions as an in-process pair.

    The primary fixture is loaded through the public ``load_state`` tool and the
    peer fixture through the entry point's documented ``POKERED_PEER_STATE_*``
    launch contract.  Every drive action still goes through the public
    ``press``/``release``/``link_step``/``link_peer_press`` tools and every
    observation through the read-only resources.
    """

    transport = "local_pair"
    owners = 2

    def __init__(self, client, runtime_mode):
        self.client = client
        self.runtime_mode = runtime_mode
        # Set by :func:`_walk_to_trade_trigger`; reported in the row payload and
        # in the captured walk log so the evidence records how long the
        # ROM-owned trade-center rendezvous actually took.
        self.walk_settle_frames = None

    async def initialize(self):
        await self.client.initialize()

    async def assert_surface(self):
        listed = await self.client.request("tools/list", {})
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
        resources = await self.client.request("resources/list", {})
        uris = {str(row["uri"]) for row in resources["resources"]}
        assert {
            "pokered://game-state",
            "pokered://peer-game-state",
            "pokered://party-records",
            "pokered://peer-party-records",
            "pokered://events",
            "pokered://peer-events",
        } <= uris, uris

    async def load_fixture(self, asset, *, owner):
        assert owner == 0, "the peer fixture is loaded by the launch contract"
        loaded = await self.client.tool(
            "load_state", {"data": base64.b64encode(asset["state"]).decode("ascii")}
        )
        assert loaded == {"ok": True}, loaded

    async def link_up(self, *, arm_barrier=None):
        """Pair the two in-process sessions.

        ``arm_barrier`` is the remote-transport pacing knob.  This pair runs on
        the bit-accurate backend with no remote edge transport, so there is no
        network frame barrier to arm and the argument is ignored.
        """
        del arm_barrier
        paired = await self.client.tool("link_pair")
        assert paired["paired"] is True, paired
        status = await self.client.tool("link_status")
        assert status["link_backend"] == "bit_accurate", status
        assert status["paired"] is True, status

    async def press(self, owner, button, *, duration=4):
        if owner == 0:
            await self.client.tool("press", {"button": button, "duration": duration})
        else:
            await self.client.tool(
                "link_peer_press", {"button": button, "duration": duration}
            )

    async def step(self, frames):
        remaining = frames
        while remaining > 0:
            chunk = min(STEP_CHUNK, remaining)
            await self.client.tool("link_step", {"count": chunk})
            remaining -= chunk

    async def state(self, owner):
        uri = "pokered://game-state" if owner == 0 else "pokered://peer-game-state"
        return await _read_resource(self.client, uri)

    async def records(self, owner):
        source = "party-records" if owner == 0 else "peer-party-records"
        payload = await _read_resource(self.client, f"pokered://{source}")
        assert payload["source"] == source, payload
        return payload

    async def rom_events(self, owner):
        """One owner's own latched ROM event log, read from that owner's view.

        The primary session's log is ``pokered://events``; the second session of
        the in-process pair publishes its own log as ``pokered://peer-events``,
        which is what makes the peer's ``trade_received`` marker observable
        instead of assumed.
        """
        uri = "pokered://events" if owner == 0 else "pokered://peer-events"
        events = await _read_resource(self.client, uri)
        assert isinstance(events, list), events
        return events

    async def evolution_counts(self):
        counts = []
        for owner in range(self.owners):
            events = await self.rom_events(owner)
            names = [str(event.get("name")) for event in events]
            counts.append(names.count(EVOLUTION_EVENT))
        return counts

    async def eof(self):
        await self.client.eof()

    async def cleanup(self):
        await self.client.cleanup()


# Every tool this file's TCP client invokes on a single-session server.  A
# single-session process publishes ``_tool_specs(has_peer=False)``, which drops
# ``_LOCAL_LINK_TOOL_NAMES`` because there is no second in-process session to
# drive; the advertised equivalent of the two-session ``link_step`` is ``step``.
# ``TcpPair.assert_surface`` requires the process it drives to publish every
# name in this set and ``TcpPair._call`` refuses any other name, so a row cannot
# exercise a private or undiscovered operation while reporting that it drove the
# documented public surface.
_TCP_DRIVER_TOOL_NAMES = frozenset(
    {
        "load_state",
        "step",
        "press",
        "link_listen",
        "link_connect",
        "link_status",
        "link_frame_barrier",
    }
)


class TcpPair:
    """Two MCP stdio servers linked over a localhost TCP serial cable.

    Each process owns exactly one session and is driven only through its own
    public tool surface, so every observation is that owner's own read: the
    records a process reports are the records its own emulator holds after the
    exchange, not a peer session published by its neighbour.
    """

    transport = "tcp_pair"
    owners = 2

    def __init__(self, clients, versions, runtime_mode):
        assert len(clients) == 2, clients
        self.clients = list(clients)
        self.versions = tuple(versions)
        self.runtime_mode = runtime_mode
        # Per-owner ``tools/list`` records captured by :meth:`assert_surface`.
        # Empty until then, so a driver call made before discovery fails closed.
        self._advertised = ()
        # Set by :meth:`arm_network_frame_barrier`; reported in the row
        # payload so the terminal evidence records that this client armed the
        # documented pacing control rather than inferring it from a pass.
        self.network_frame_barrier_armed = False
        # Set by :func:`_walk_to_trade_trigger`; same reporting contract.
        self.walk_settle_frames = None

    async def _call(self, owner, name, arguments=None):
        """Invoke one public tool on ``self.clients[owner]``.

        The MCP SDK dispatches an unlisted ``tools/call`` and explicitly skips
        its schema validation, so a driver that reached for a name its own
        server never advertised would still look like it drove the public
        surface.  Every driver call therefore goes through the per-owner
        ``tools/list`` record captured by :meth:`assert_surface` and fails
        loudly when the name is absent.
        """
        advertised = self._advertised[owner] if self._advertised else frozenset()
        if name not in advertised:
            raise AssertionError(
                f"TCP driver called undiscovered tool {name!r} on owner {owner}; "
                f"advertised={sorted(advertised)}"
            )
        return await self.clients[owner].tool(name, arguments)

    async def arm_network_frame_barrier(self):
        """Arm the documented network frame barrier on both owners.

        HELLO negotiation deliberately leaves Yellow's input-sensitive
        preamble (and every cross-family pair) on native edge pacing, which
        requires the peer to answer each edge from inside its own tick.  This
        client advances exactly one frame per owner per turn, so that
        unpaced exchange lapses: the waiting endpoint blocks on a peer response
        that the paced step never delivers, the calling process latches the
        fatal ``serial_backend_error``, and the row can never reach the
        LinkMenu.  The unarmed row is exercised by
        ``test_real_rom_mcp_trade_over_tcp_yellow_needs_the_frame_barrier``.

        The repository's own two-process trade driver arms the same control at
        the Cable Club attendant, before the first ``A`` press
        (``tests/_tcp_trade_peer.py``).  This client cannot arm at that
        boundary because its first two moves are still inside the same
        preamble it has to protect, so it arms at the earliest documented
        boundary it can observe: both ends already report ``connected``.  That
        is the public-tool equivalent, so the arm is a documented rendezvous,
        not a private hook, RAM read, or RAM write.  Both owners are armed
        while no frame is in flight, and the ROM keeps ownership of every
        serial register and role change.
        """
        for owner in range(self.owners):
            payload = await self._call(owner, "link_frame_barrier", {"enabled": True})
            assert payload["enabled"] is True, payload
            assert payload["network_frame_barrier"] is True, payload
            assert payload["remote_mode"] == "connected", payload
        self.network_frame_barrier_armed = True

    def needs_network_frame_barrier(self):
        """Whether this pair is one HELLO leaves on native edge pacing.

        A pair that involves Yellow keeps native edge transport until the
        coordinator arms the barrier, so it must be armed before the first
        link-menu frame.  A Red/Blue pair already negotiates the pacing it
        needs, so this row leaves it exactly as negotiated rather than
        overriding a working setting.
        """
        return "yellow" in self.versions

    async def initialize(self):
        for client in self.clients:
            await client.initialize()

    async def assert_surface(self):
        advertised = []
        for role, client in zip(("listener", "connector"), self.clients, strict=True):
            listed = await client.request("tools/list", {})
            tool_names = {row["name"] for row in listed["tools"]}
            assert {
                "load_state",
                "press",
                "release",
                "step",
                "link_listen",
                "link_connect",
                "link_status",
                "link_disconnect",
            } <= tool_names, (role, tool_names)
            # Discovery requirement: every tool this driver invokes (including
            # the pacing control it arms) must be published by the very
            # process it drives.  ``link_step`` is the in-process pair's
            # two-session tool and is deliberately absent from a single-session
            # process, so requiring it here would be a contradiction rather
            # than a contract.
            missing = _TCP_DRIVER_TOOL_NAMES - tool_names
            assert not missing, (role, sorted(missing), tool_names)
            assert "link_step" not in tool_names, (role, tool_names)
            # A single-session process must not advertise the in-process peer
            # tools: there is no second session for it to drive.
            assert "link_peer_press" not in tool_names, (role, tool_names)
            assert not any(
                "hash" in name or "memory" in name or "register" in name or "hook" in name
                for name in tool_names
            ), (role, tool_names)
            resources = await client.request("resources/list", {})
            uris = {str(row["uri"]) for row in resources["resources"]}
            assert {
                "pokered://game-state",
                "pokered://party-records",
                "pokered://events",
            } <= uris, (role, uris)
            assert "pokered://peer-party-records" not in uris, (role, uris)
            assert "pokered://peer-events" not in uris, (role, uris)
            advertised.append(frozenset(tool_names))
        self._advertised = tuple(advertised)

    async def load_fixture(self, asset, *, owner):
        loaded = await self._call(
            owner, "load_state", {"data": base64.b64encode(asset["state"]).decode("ascii")}
        )
        assert loaded == {"ok": True}, loaded

    async def link_up(self, *, arm_barrier=None):
        """Form the TCP pair, then arm the barrier this pair needs.

        ``arm_barrier=None`` applies :meth:`needs_network_frame_barrier`; an
        explicit boolean overrides it so the negative control can prove the
        arm is load-bearing.
        """
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        listening = await self._call(
            0,
            "link_listen",
            {
                "host": "127.0.0.1",
                "port": port,
                "rom_version": self.versions[0],
                "peer_rom_version": self.versions[1],
                "timeout_s": 30,
            },
        )
        assert listening["remote_mode"] == "listening", listening
        connected = await self._call(
            1,
            "link_connect",
            {
                "host": "127.0.0.1",
                "port": port,
                "rom_version": self.versions[1],
                "peer_rom_version": self.versions[0],
                "timeout_s": 30,
            },
        )
        assert connected["remote_mode"] == "connected", connected
        # ``link_connect`` reports the connector's own view.  The listener's
        # accept thread can still be inside its first ROM boot when that
        # returns, so poll both ends until each publishes ``connected``; a pair
        # that never links fails closed on the deadline instead of walking a
        # half-linked transport.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + LINK_UP_STATUS_TIMEOUT
        while True:
            statuses = [await self._call(owner, "link_status") for owner in range(self.owners)]
            modes = [status.get("remote_mode") or status.get("mode") for status in statuses]
            if all(mode == "connected" for mode in modes):
                break
            if loop.time() > deadline:
                raise AssertionError(("link_up never reported connected", modes))
            await asyncio.sleep(LINK_UP_STATUS_POLL_INTERVAL)
        # Both ends now agree on a connected remote link, so this is the
        # ROM-owned boundary at which the repository's own two-process driver
        # arms the same pacing control.  Arming here keeps every edge-paced row
        # on one documented rendezvous instead of letting it lapse into the
        # transport's ten-second ``EDGE_RESP`` deadline.
        wanted = self.needs_network_frame_barrier() if arm_barrier is None else arm_barrier
        if wanted:
            await self.arm_network_frame_barrier()

    async def press(self, owner, button, *, duration=4):
        await self._call(owner, "press", {"button": button, "duration": duration})

    async def step(self, frames):
        """Advance one frame on each owner per turn.

        The two processes have independent clocks, so the pair is stepped one
        frame per owner per turn.  Requests run concurrently: the remote
        transport paces turns between the listener and the connector, and a
        serial step on one side can require its peer to advance before either
        call returns.  Each owner is stepped through its own advertised
        ``step`` tool: a single-session process publishes no ``link_step``.
        """
        for _ in range(frames):
            await asyncio.gather(
                *(self._call(owner, "step", {"count": 1}) for owner in range(self.owners))
            )

    async def state(self, owner):
        return await _read_resource(self.clients[owner], "pokered://game-state")

    async def records(self, owner):
        payload = await _read_resource(self.clients[owner], "pokered://party-records")
        assert payload["source"] == "party-records", payload
        return payload

    async def evolution_counts(self):
        counts = []
        for owner in range(self.owners):
            events = await self.rom_events(owner)
            names = [str(event.get("name")) for event in events]
            counts.append(names.count(EVOLUTION_EVENT))
        return counts

    async def rom_events(self, owner):
        """This process's own latched ROM event log (never its neighbour's)."""
        events = await _read_resource(self.clients[owner], "pokered://events")
        assert isinstance(events, list), events
        return events

    async def eof(self):
        await asyncio.gather(*(client.eof() for client in self.clients))

    async def cleanup(self):
        await asyncio.gather(
            *(client.cleanup() for client in self.clients), return_exceptions=False
        )


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


def _log(phase, detail):
    print(f"MCP_TRADE_RECORDS {phase} {detail}", flush=True)


async def _all_states(pair):
    return [await pair.state(owner) for owner in range(pair.owners)]


async def _all_records(pair):
    return [await pair.records(owner) for owner in range(pair.owners)]


async def _trade_received_counts(pair):
    """Per-owner count of the ROM-owned ``trade_received`` append marker.

    Read from each owner's *own* event log, so the peer's copy is observed
    rather than inferred from the primary's view.
    """
    counts = []
    for owner in range(pair.owners):
        events = await pair.rom_events(owner)
        names = [str(event.get("name")) for event in events]
        counts.append(names.count(TRADE_RECEIVED_EVENT))
    return counts


async def _link_step(pair, frames):
    await pair.step(frames)


async def _press(pair, button, *, duration=4):
    await pair.press(0, button, duration=duration)


async def _peer_press(pair, button, *, duration=4):
    await pair.press(1, button, duration=duration)


async def _press_both(pair, button, *, duration=4):
    await _press(pair, button, duration=duration)
    await _peer_press(pair, button, duration=duration)


async def _press_actions(pair, primary_action, peer_action, *, duration=4):
    """Press each owner's button, treating ``None`` as "send no input".

    The completion driver must leave an owner that has already reached the
    restored trade-center selection loop untouched: ``_trade_action`` would
    return ``"a"`` there and start a *second* trade, which desynchronises the
    pair before its peer arrives at the same milestone.

    An action is either a button name or a ``(button, hold)`` pair.  The hold
    length matters while a menu can still open under the keystroke: the ROM's
    menu loop reads the key state of its first frames, so an ``A`` that is still
    held when the trade party menu appears is consumed as that menu's own
    selection and the offer is confirmed without ever being observed.  Dialogue
    presses therefore use a one-frame hold while menu navigation keeps the
    proven four-frame hold.
    """
    for owner, action in ((0, primary_action), (1, peer_action)):
        if action is None:
            continue
        button, hold = action if isinstance(action, tuple) else (action, duration)
        await pair.press(owner, button, duration=hold)


def _approach_action(state, party_count):
    """Return one owner's approach action on the way to the trade party menu.

    An owner resting on the live ``.playerMonMenu`` receives no input, so its
    peer can catch up.  An owner that is already inside the STATS/TRADE sub-menu
    was driven there by an input the caller aimed at the preceding dialogue (the
    held ``A`` is consumed by the freshly opened menu); ``B`` is the ROM's
    return key for that sub-menu, so backing out restores the live menu the
    caller has to observe instead of pressing on and committing unobserved.
    Everything else is the pre-menu dialogue and the waiting prompt, which
    advance on ``A``; the one-frame hold keeps that press from reaching a menu
    that opens underneath it.
    """
    if _player_mon_menu_live(state, party_count):
        return None
    if _overworld(state)["map_id"] != TRADE_CENTER_MAP_ID:
        return ("a", 1)
    if _menu(state)["watched_keys"] in (STATS_MENU_KEYS, TRADE_MENU_KEYS):
        return ("b", 4)
    return ("a", 1)


async def _owner_trade_action(pair, owner, state, *, party_count, target_slot, confirmed):
    """Return one owner's ``(button, hold)`` action and any freshly read offer.

    The ROM's ``.playerMonMenu`` publishes ``wCurrentMenuItem``/``wMaxMenuItem``
    and closes as soon as its entry is confirmed, so an offer is only accepted
    from a *fresh* read taken after the cursor has settled on the intended slot
    and before the confirming press.  A sample that already showed the intended
    slot is not enough on its own: the cursor can move between the sample and
    the press, which is exactly the gap that let an earlier revision of this
    driver confirm owner 0 without ever reading its live menu.

    An owner that reaches the STATS/TRADE sub-menu without a recorded offer is
    returned to the live menu through the ROM's sub-menu back key rather than
    being confirmed unobserved.
    """
    if _overworld(state)["map_id"] != TRADE_CENTER_MAP_ID:
        # Outside the trade center the drive is still in the Cable Club walk.
        return ("a", 1), None
    menu = _menu(state)
    if not confirmed and _player_mon_menu_live(state, party_count):
        item = menu["current_item"]
        if item != target_slot:
            # Player-mon menu: walk the published cursor to the intended slot.
            return (("down" if item < target_slot else "up"), 4), None
        fresh = await pair.state(owner)
        if (
            _player_mon_menu_live(fresh, party_count)
            and _menu(fresh)["current_item"] == target_slot
        ):
            return ("a", 4), item
        # The menu moved between the sample and the confirming press; keep
        # driving from the fresh reading instead of confirming what was not read.
        return _trade_action(fresh, party_count, target_slot), None
    if not confirmed and menu["watched_keys"] in (STATS_MENU_KEYS, TRADE_MENU_KEYS):
        return ("b", 4), None
    return _trade_action(state, party_count, target_slot), None


async def _advance_until(pair, predicate, *, budget, prompt):
    """Advance the pair in bounded chunks until ``predicate`` holds."""
    spent = 0
    while spent < budget:
        states = await _all_states(pair)
        if all(predicate(state) for state in states):
            return states, spent
        await _link_step(pair, min(STEP_CHUNK, budget - spent))
        spent += STEP_CHUNK
    states = await _all_states(pair)
    raise AssertionError(
        f"{prompt}: phase budget {budget} frames exhausted; states={json.dumps(states)}"
    )


async def _mash_until(pair, predicate, *, budget, prompt):
    """A-mash the pair while advancing until ``predicate`` holds."""
    spent = 0
    while spent < budget:
        states = await _all_states(pair)
        if all(predicate(state) for state in states):
            return states, spent
        await _press_both(pair, "a")
        await _link_step(pair, min(STEP_CHUNK, budget - spent))
        spent += STEP_CHUNK
    states = await _all_states(pair)
    raise AssertionError(
        f"{prompt}: phase budget {budget} frames exhausted; states={json.dumps(states)}"
    )


async def _prelink(pair, primary_asset, peer_asset):
    """Load the admitted fixtures through the public surface and verify it."""
    await pair.initialize()
    await pair.assert_surface()
    await pair.load_fixture(primary_asset, owner=0)
    if pair.transport == "tcp_pair":
        await pair.load_fixture(peer_asset, owner=1)
    before = await _all_records(pair)
    for payload, owner in zip(before, range(pair.owners), strict=True):
        assert payload["digest_algorithm"] == "sha256", payload
        assert payload["record_size"] == 44, payload
        assert payload["valid"] is True, payload
        assert len(_records_from_payload(payload)) >= 1, payload
        del owner
    return before


async def _drive_to_link_menu(pair):
    for _ in range(3):
        await _press_both(pair, "up", duration=6)
        await _link_step(pair, RECEPTIONIST_WALK_FRAMES)
    spent = 0
    while spent < LINK_MENU_BUDGET:
        states = await _all_states(pair)
        if all(_link_menu_ready(state) for state in states):
            _log("link_menu", f"frames={spent}")
            return states
        # Stagger the ordinary public A input exactly as the proven local
        # driver does: primary A, four frames, peer A, sixteen frames.
        for _ in range(LINK_MENU_BURST_ATTEMPTS):
            await _press(pair, "a", duration=4)
            await _link_step(pair, 4)
            await _peer_press(pair, "a", duration=4)
            await _link_step(pair, 16)
            spent += 20
        # Quiet window: no public input, so a LinkMenu that finishes drawing
        # here cannot select its default entry before the explicit selection.
        for _ in range(LINK_MENU_QUIET_FRAMES // STEP_CHUNK):
            await _link_step(pair, STEP_CHUNK)
            spent += STEP_CHUNK
            states = await _all_states(pair)
            if all(_link_menu_ready(state) for state in states):
                _log("link_menu", f"frames={spent}")
                return states
    states = await _all_states(pair)
    raise AssertionError(
        f"LinkMenu never reached on both owners: budget {LINK_MENU_BUDGET} "
        f"frames exhausted; states={json.dumps(states)}"
    )


async def _select_trade_center(pair):
    """Confirm the default TRADE CENTER entry and wait for the 0xEF warp."""
    spent = 0
    while spent < 400:
        states = await _all_states(pair)
        if all(_menu_ready(state, LINK_MENU_MAX_ITEMS) for state in states):
            break
        await _link_step(pair, 16)
        spent += 16
    states, cursor_frames = await _advance_until(
        pair,
        lambda state: _link_menu_ready(state) and _menu(state)["current_item"] == 0,
        budget=400,
        prompt="LinkMenu cursor never rested on TRADE CENTER on both owners",
    )
    # Staggered A confirms the default TRADE CENTER entry on both owners.
    await _press(pair, "a", duration=4)
    await _link_step(pair, 4)
    await _peer_press(pair, "a", duration=4)
    await _link_step(pair, 16)
    states, warp_frames = await _mash_until(
        pair,
        lambda state: _overworld(state)["map_id"] == TRADE_CENTER_MAP_ID,
        budget=WARP_BUDGET,
        prompt="TRADE_CENTER warp never reached on both owners",
    )
    _log("trade_center", f"cursor={cursor_frames} warp={warp_frames}")
    return states


async def _walk_to_trade_trigger(pair):
    """Turn each owner toward its hidden-event tile and hand off to the trade.

    The internal-clock owner spawns at (3, 4) and the external-clock owner at
    (6, 4); the two hidden-event triggers live at (4, 4) and (5, 4).  The
    public ``overworld.x`` identifies each owner without reading serial
    registers.  Facing is what the ``ANY_FACING`` trigger needs, so each
    attempt presses the direction on its own owner and then advances the pair
    one frame at a time: one owner can enter ``CableClub_DoBattleOrTrade``
    during this loop, and ticking twenty frames on that side before its peer
    advances would let the first serial transfers use stale handshake bytes.

    Before the first press both owners are advanced quietly until the
    ROM-owned trade-center state has stopped changing (see
    ``WALK_STABLE_CHECKS``): the Cable Club entry script keeps the overworld
    input for its own handshake, so a press offered during it is consumed
    instead of walking onto the trigger.  The quiet rendezvous is bounded, so
    an unsettled pair still walks and fails its own budget rather than hanging.
    """
    settle_frames = await _settle_trade_center(pair)
    states = await _all_states(pair)
    directions = ["right" if _overworld(state)["x"] < 5 else "left" for state in states]
    for _ in range(WALK_ATTEMPTS):
        await _press(pair, directions[0], duration=8)
        await _peer_press(pair, directions[1], duration=8)
        for _ in range(WALK_STEP_FRAMES):
            await _link_step(pair, 1)
    pair.walk_settle_frames = settle_frames
    _log("walk", f"directions={directions} settle_frames={settle_frames}")


def _trade_center_fingerprint(state):
    """Return the public trade-center observation used for quiescence.

    Every field is read from a resource the acceptance surface already
    publishes, so the rendezvous is a statement about ROM-owned state rather
    than about host scheduling.
    """
    overworld = _overworld(state)
    menu = _menu(state)
    text = state["text"]
    return (
        overworld["map_id"],
        overworld["x"],
        overworld["y"],
        overworld["direction"],
        overworld["walk_counter"],
        overworld["current_map_script"],
        overworld["map_script_flags"],
        menu["watched_keys"],
        menu["max_item"],
        text["suppress_prompt_wait"],
    )


async def _settle_trade_center(pair):
    """Advance both owners quietly until their trade-center state is stable.

    Returns the number of frames spent.  ``WALK_STABLE_CHECKS`` identical
    consecutive readings of :func:`_trade_center_fingerprint` are required, and
    the whole wait is bounded by ``WALK_SETTLE_BUDGET`` so a pair that never
    settles still walks and fails closed on its own budget.
    """
    spent = 0
    previous = None
    stable = 0
    while spent < WALK_SETTLE_BUDGET and stable < WALK_STABLE_CHECKS:
        states = await _all_states(pair)
        current = tuple(_trade_center_fingerprint(state) for state in states)
        stable = stable + 1 if current == previous else 0
        previous = current
        if stable >= WALK_STABLE_CHECKS:
            break
        await _link_step(pair, WALK_SETTLE_SLICE)
        spent += WALK_SETTLE_SLICE
    return spent


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


def _digests(records):
    return [record["digest"] for record in records]


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


def _compacted_records(before, outgoing_slot, incoming_record):
    """Return the source-defined post-trade record list for one owner.

    Same remove/compact/append model as ``_compaction_expected_digests`` but at
    the record level, so a fabricated observation carries the received record's
    species and level (which the oracle also checks) instead of a digest alone.
    """
    survivors = before[:outgoing_slot] + before[outgoing_slot + 1 :]
    renumbered = [{**record, "slot": index} for index, record in enumerate(survivors)]
    receiving = len(before) - 1
    return renumbered + [{**incoming_record, "slot": receiving}]


async def _drive_trade(
    pair, *, party_counts, primary_before, peer_before, primary_slot=0, peer_slot=0
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

    The copy is only accepted when *both* signals agree: each owner's party
    digests equal the source-defined compaction prediction *and* each owner's
    own event log holds the ROM's ``trade_received`` marker from
    ``_AddEnemyMonToPlayerParty``.  The digest comparison alone is vacuous for
    an orientation whose two admitted records happen to be byte-identical (Red
    and Yellow cannot supply distinct admitted records, see the module
    docstring), because it already holds before a single input is sent; the
    ROM-owned marker is what makes those rows falsifiable, and a party that
    never appended a received record never emits it.  The records are reread
    after the terminal milestone and required to equal the copy snapshot, so a
    party mutated between the copy and the room return fails this row instead
    of being validated from a stale read.
    """
    primary_incoming = peer_before[peer_slot]["digest"]
    peer_incoming = primary_before[primary_slot]["digest"]
    expected_primary = _compaction_expected_digests(primary_before, primary_slot, primary_incoming)
    expected_peer = _compaction_expected_digests(peer_before, peer_slot, peer_incoming)
    spent = 0
    milestones = []
    swap_records = None
    swap_states = None
    pre_evolution = None
    received: list[int] = []
    back_outs = 0
    # Cursor slot each owner was resting on when it confirmed the offer.  The
    # ROM's ``.playerMonMenu`` publishes ``wCurrentMenuItem``/``wMaxMenuItem``,
    # so the intended slot is observed through the public menu read rather than
    # inferred from the result.  The offer is recorded from the fresh read that
    # immediately precedes the confirming press (``_owner_trade_action``).
    offered: dict[int, int] = {}
    targets = (primary_slot, peer_slot)
    while spent < TRADE_BUDGET:
        records = await _all_records(pair)
        primary_after = _records_from_payload(records[0])
        peer_after = _records_from_payload(records[1])
        received = await _trade_received_counts(pair)
        if (
            primary_after
            and peer_after
            and _digests(primary_after) == expected_primary
            and _digests(peer_after) == expected_peer
            and all(count >= 1 for count in received)
        ):
            swap_records = records
            swap_states = await _all_states(pair)
            pre_evolution = await pair.evolution_counts()
            break
        states = await _all_states(pair)
        observed = [
            f"{_overworld(state)['map_id']}:{_menu(state)['watched_keys']}"
            f":{_menu(state)['current_item']}/{_menu(state)['max_item']}"
            for state in states
        ]
        if not milestones or milestones[-1] != observed:
            milestones.append(observed)
        actions = []
        for owner, state in enumerate(states):
            action, observation = await _owner_trade_action(
                pair,
                owner,
                state,
                party_count=party_counts[owner],
                target_slot=targets[owner],
                confirmed=owner in offered,
            )
            if action is not None and action[0] == "b":
                back_outs += 1
            if observation is not None:
                offered[owner] = observation
            actions.append(action)
        await _press_actions(pair, actions[0], actions[1])
        await _link_step(pair, STEP_CHUNK)
        spent += STEP_CHUNK
    if swap_records is None:
        records = await _all_records(pair)
        states = await _all_states(pair)
        raise AssertionError(
            "trade never produced the exact paired digest exchange within "
            f"{TRADE_BUDGET} frames; offered={offered} "
            f"trade_received={received} "
            f"primary={json.dumps(records[0])} "
            f"peer={json.dumps(records[1])} states={json.dumps(states)}"
        )
    _log("trade_copy", f"frames={spent} trade_received={received} milestones={milestones}")
    final_states, post_frames, evolution = await _drive_trade_completion(
        pair,
        party_counts=party_counts,
        pre_evolution=pre_evolution,
        primary_slot=primary_slot,
        peer_slot=peer_slot,
    )
    assert offered.get(0) == primary_slot, (offered, primary_slot)
    assert offered.get(1) == peer_slot, (offered, peer_slot)
    # Reread both owners after the terminal milestone: the exchange has to hold
    # in the state the ROM actually returned to, not only in the copy snapshot.
    final_records = await _all_records(pair)
    copy_digests = [_digests(_records_from_payload(payload)) for payload in swap_records]
    final_digests = [_digests(_records_from_payload(payload)) for payload in final_records]
    assert final_digests == copy_digests, (
        "a party changed between the record copy and the terminal milestone",
        copy_digests,
        final_digests,
    )
    # The append marker must still be present in the terminal state; a session
    # that lost (or never latched) the ROM's own copy event is not accepted even
    # if the digests happen to agree.
    final_received = await _trade_received_counts(pair)
    assert all(count >= 1 for count in final_received), (final_received, received)
    _log(
        "offers",
        f"primary={primary_slot} peer={peer_slot} observed={offered} "
        f"submenu_back_outs={back_outs} trade_received={final_received}",
    )
    return TradeRun(
        final_records=final_records,
        copy_records=swap_records,
        copy_states=swap_states,
        final_states=final_states,
        post_frames=post_frames,
        evolution=evolution,
        offered=offered,
        back_outs=back_outs,
        received=final_received,
    )


async def _drive_trade_completion(
    pair, *, party_counts, pre_evolution, primary_slot=0, peer_slot=0
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
    assert pre_evolution is not None, pre_evolution
    spent = 0
    milestones = []
    while spent < POST_TRADE_BUDGET:
        states = await _all_states(pair)
        evolution = await pair.evolution_counts()
        advanced = all(
            previous is None or current is None or current > previous
            for previous, current in zip(pre_evolution, evolution, strict=True)
        )
        restored = all(
            _selection_loop_ready(state, party_counts[owner])
            for owner, state in enumerate(states)
        )
        if advanced and restored:
            _log("trade_completion", f"frames={spent} evolution={evolution}")
            return states, spent, evolution
        observed = [
            f"{_overworld(state)['map_id']}:{_menu(state)['watched_keys']}"
            f":{_menu(state)['current_item']}/{_menu(state)['max_item']}:{evolution[owner]}"
            for owner, state in enumerate(states)
        ]
        if not milestones or milestones[-1] != observed:
            milestones.append(observed)
        # An owner that has already reached the restored selection loop must
        # receive no further input: ``_trade_action`` returns ``"a"`` there and
        # would start a *second* trade, desynchronising it from a peer that is
        # still finishing the first one.  Holding the settled owner still lets
        # its peer arrive at the same milestone.
        actions = [
            None
            if _selection_loop_ready(state, party_counts[owner])
            else _trade_action(state, party_counts[owner], slot)
            for owner, (state, slot) in enumerate(
                zip(states, (primary_slot, peer_slot), strict=True)
            )
        ]
        await _press_actions(pair, actions[0], actions[1], duration=4)
        await _link_step(pair, STEP_CHUNK)
        spent += STEP_CHUNK
    states = await _all_states(pair)
    evolution = await pair.evolution_counts()
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


async def _enter_trade_flow(pair, *, arm_barrier=None):
    """Drive the public flow from the loaded fixture into the Trade Center.

    ``arm_barrier`` is forwarded to :meth:`TcpPair.link_up` so a caller can
    override the pair's own pacing decision (the negative control does).
    """
    await pair.link_up(arm_barrier=arm_barrier)
    await _drive_to_link_menu(pair)
    await _select_trade_center(pair)
    await _walk_to_trade_trigger(pair)


async def _await_party_menu(pair, party_counts, *, budget=TRADE_BUDGET):
    """Advance until both owners publish the live trade party menu.

    An owner that already publishes the live menu receives no further input, and
    an owner that was driven into the STATS/TRADE sub-menu by an input aimed at
    the preceding dialogue is backed out of it (the ROM's ``B`` return key)
    instead of being pressed on: ``_trade_action`` would confirm the offer from
    the sub-menu, which leaves the state the caller is waiting for.  Because the
    two owners reach the menu a few frames apart, pressing both keeps them
    alternating between the menu and the sub-menu and the predicate never holds
    for the pair; holding the settled owner still lets its peer catch up.
    """
    spent = 0
    back_outs = 0
    while spent < budget:
        states = await _all_states(pair)
        if all(
            _player_mon_menu_live(state, party_counts[owner])
            for owner, state in enumerate(states)
        ):
            _log("party_menu", f"frames={spent} submenu_back_outs={back_outs}")
            return states, spent
        actions = []
        for owner, state in enumerate(states):
            action = _approach_action(state, party_counts[owner])
            if action is not None and action[0] == "b":
                back_outs += 1
            actions.append(action)
        await _press_actions(pair, actions[0], actions[1])
        await _link_step(pair, STEP_CHUNK)
        spent += STEP_CHUNK
    states = await _all_states(pair)
    raise AssertionError(
        f"trade party menu never became live within {budget} frames; states={json.dumps(states)}"
    )


async def _fixture_pair(pair, primary_asset, peer_asset, *, fixture):
    """Load the fixtures publicly and return both owners' starting records."""
    before = await _prelink(pair, primary_asset, peer_asset)
    primary_before = _records_from_payload(before[0])
    peer_before = _records_from_payload(before[1])
    assert len(primary_before) >= 1 and len(peer_before) >= 1, before
    for record in (*primary_before, *peer_before):
        _assert_record_shape(record, label=f"fixture-{fixture}")
    return primary_before, peer_before


def _fixture_sha1(asset):
    return asset["provenance"]["registry"]["sha1"]



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


def assert_paired_exchange(run, *, primary_before, peer_before, primary_slot=0, peer_slot=0, label="pair"):
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


def test_paired_exchange_oracle_rejects_identical_skipped_copy():
    """The byte-identical orientation must not pass on unchanged records.

    This is the review's control verbatim: menus and completion milestones are
    present, both parties are byte-identical to the fixture read, and the
    ROM-owned copy marker never fired.  Before the marker was required this
    observation passed, because a Red/Red exchange is a no-op at the byte level.
    """
    primary_before = [_record(0, "a" * 64)]
    peer_before = [_record(0, "a" * 64)]
    run = _skipped_copy_run(primary_before, peer_before)
    with pytest.raises(AssertionError):
        assert_paired_exchange(
            run, primary_before=primary_before, peer_before=peer_before, label="identical-skip"
        )


def test_paired_exchange_oracle_rejects_distinct_skipped_copy():
    """A distinct-record orientation must reject a run that copied nothing."""
    primary_before = [_record(0, "a" * 64)]
    peer_before = [_record(0, "b" * 64)]
    run = _skipped_copy_run(primary_before, peer_before)
    with pytest.raises(AssertionError):
        assert_paired_exchange(
            run, primary_before=primary_before, peer_before=peer_before, label="distinct-skip"
        )


def test_paired_exchange_oracle_rejects_faked_marker_without_exchange():
    """A fired marker is not enough: the records must match the copy result."""
    primary_before = [_record(0, "a" * 64)]
    peer_before = [_record(0, "b" * 64)]
    run = _skipped_copy_run(primary_before, peer_before, received=(1, 1))
    with pytest.raises(AssertionError):
        assert_paired_exchange(
            run, primary_before=primary_before, peer_before=peer_before, label="faked-marker"
        )


def test_paired_exchange_oracle_rejects_late_corruption():
    """A correct copy snapshot does not excuse a party that changed later."""
    primary_before = [_record(0, "a" * 64)]
    peer_before = [_record(0, "b" * 64)]
    run = TradeRun(
        final_records=[
            _record_payload([_record(0, "c" * 64)]),
            _record_payload([_record(0, "a" * 64)], source="peer-party-records"),
        ],
        copy_records=[
            _record_payload([_record(0, "b" * 64)]),
            _record_payload([_record(0, "a" * 64)], source="peer-party-records"),
        ],
        copy_states=[],
        final_states=[],
        post_frames=0,
        evolution=[1, 1],
        offered={0: 0, 1: 0},
        back_outs=0,
        received=[1, 1],
    )
    with pytest.raises(AssertionError):
        assert_paired_exchange(
            run, primary_before=primary_before, peer_before=peer_before, label="late-corruption"
        )


def test_paired_exchange_oracle_accepts_identical_completion():
    """Positive control: the identical-record oracle is not vacuous in reverse.

    The same byte-identical observation that the skipped-copy control must
    reject has to be *accepted* once the ROM-owned append marker is present.
    Without this control a marker requirement that simply failed closed could not
    be told apart from an oracle that always raises.
    """
    primary_before = [_record(0, "a" * 64)]
    peer_before = [_record(0, "a" * 64)]
    run = _skipped_copy_run(primary_before, peer_before, received=(1, 1))
    verdict = assert_paired_exchange(
        run, primary_before=primary_before, peer_before=peer_before, label="identical-accept"
    )
    assert verdict["distinct_records"] is False, verdict


def _distinct_party(prefix):
    """Six same-species records whose 44-byte digests are pairwise distinct."""
    return [
        _record(slot, (prefix + str(slot)).ljust(64, "0"))
        for slot in range(MULTI_MEMBER_PARTY_COUNT)
    ]


def test_paired_exchange_oracle_rejects_wrong_slot_copy():
    """The review's wrong-slot mutation must fail on pairwise-distinct records.

    Six same-species records per owner make every slot digest distinct, so a
    response that copied slots ``(0, 1)`` yields a different compaction result
    than the required ``(2, 0)`` and the exact-exchange assertion rejects it.
    With the earlier six *identical* records per owner this mutation satisfied
    every assertion; the control fails closed only once the records differ.
    """
    primary_before = _distinct_party("p")
    peer_before = _distinct_party("q")
    run = _wrong_slot_run(
        primary_before,
        peer_before,
        copied_primary_slot=0,
        copied_peer_slot=1,
        claimed_offered={0: 2, 1: 0},
    )
    assert run.offered == {0: 2, 1: 0}, run.offered
    with pytest.raises(AssertionError):
        assert_paired_exchange(
            run,
            primary_before=primary_before,
            peer_before=peer_before,
            primary_slot=2,
            peer_slot=0,
            label="wrong-slot",
        )


def test_paired_exchange_oracle_accepts_the_required_slot_copy():
    """Positive control: the oracle accepts the *required* slot copy.

    Without this control a wrong-slot test that raised for every input could not
    be told apart from an oracle that also accepts the correct exchange.
    """
    primary_before = _distinct_party("p")
    peer_before = _distinct_party("q")
    run = _wrong_slot_run(
        primary_before,
        peer_before,
        copied_primary_slot=2,
        copied_peer_slot=0,
        claimed_offered={0: 2, 1: 0},
    )
    verdict = assert_paired_exchange(
        run,
        primary_before=primary_before,
        peer_before=peer_before,
        primary_slot=2,
        peer_slot=0,
        label="required-slot",
    )
    assert verdict["distinct_records"] is True, verdict


def _survivor_run(primary_after, peer_before, primary_before, *, received=(1, 1)):
    """Wrap one mutated primary digest list in an otherwise-complete run."""
    peer_after = _compacted_records(peer_before, 0, primary_before[2])
    primary_after = [
        {**record, "slot": index} for index, record in enumerate(primary_after)
    ]
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


def test_paired_exchange_oracle_rejects_reordered_or_substituted_survivors():
    """Survivor mutations must fail once the six records are pairwise distinct.

    With six identical records per owner the compaction oracle could not see a
    reordering or a substitution among the survivors: every permutation produced
    the same digest list.  Distinct records make each survivor identifiable, so
    the exact list comparison rejects both mutations while the ROM-owned append
    marker and the copy-snapshot equality still hold.
    """
    primary_before = _distinct_party("p")
    peer_before = _distinct_party("q")
    required = _compacted_records(primary_before, 2, peer_before[0])
    swapped = [required[0], required[1], required[3], required[2], required[4], required[5]]
    substituted = [required[0], required[1], required[2], required[0], required[4], required[5]]
    for label, mutated in (("reordered", swapped), ("substituted", substituted)):
        run = _survivor_run(mutated, peer_before, primary_before)
        with pytest.raises(AssertionError):
            assert_paired_exchange(
                run,
                primary_before=primary_before,
                peer_before=peer_before,
                primary_slot=2,
                peer_slot=0,
                label=f"survivor-{label}",
            )
    # Positive control: the unmutated required list is accepted, so the two
    # rejections above are not an oracle that simply raises for every input.
    verdict = assert_paired_exchange(
        _survivor_run(required, peer_before, primary_before),
        primary_before=primary_before,
        peer_before=peer_before,
        primary_slot=2,
        peer_slot=0,
        label="survivor-required",
    )
    assert verdict["distinct_records"] is True, verdict


def _stage_assets(directory, primary_asset, peer_asset):
    """Copy the pinned ROM/SYM pairs into ``directory`` under unique names."""
    staged = {}
    for prefix, asset in (("primary", primary_asset), ("peer", peer_asset)):
        for name in ("rom", "sym"):
            target = directory / f"{prefix}-{asset[name].name}"
            shutil.copyfile(asset[name], target)
            staged[f"{prefix}_{name}"] = target
    primary = dict(primary_asset)
    primary["rom"] = staged["primary_rom"]
    primary["sym"] = staged["primary_sym"]
    peer = dict(peer_asset)
    peer["rom"] = staged["peer_rom"]
    peer["sym"] = staged["peer_sym"]
    return primary, peer


@asynccontextmanager
async def _tcp_pair_servers(tmp_path, primary_asset, peer_asset):
    """Launch two independent installed servers for one TCP trade row.

    Each process owns exactly one session and is configured through the same
    documented ``POKERED_*`` launch contract as the local rows, but with no peer
    session: the pair is formed later through the public
    ``link_listen``/``link_connect`` tools.  A failed body keeps its own
    exception as the primary error and attaches any cleanup failure as a note.
    The ``rom_version``/``peer_rom_version`` handshake arguments use each
    asset's canonical family (``red``/``blue``/``yellow``), the value the entry
    point validates, rather than the color-profile parameter id.
    """
    directory = tmp_path / "servers"
    directory.mkdir()
    primary, peer = _stage_assets(directory, primary_asset, peer_asset)
    runtime_mode = _child_runtime_mode()
    _assert_invoking_runtime(runtime_mode)
    clients = []
    original_failure = None
    try:
        for asset in (primary, peer):
            clients.append(await _launch_server(_server_env(asset, None, runtime_mode, None)))
        assert len(clients) == 2, clients
        yield TcpPair(clients, (primary["family"], peer["family"]), runtime_mode)
    except BaseException as exc:
        original_failure = exc
        raise
    finally:
        # Attempt every owned client's cleanup exactly once.  A cleanup failure
        # must never leave a sibling process running: when the body already
        # failed, that failure stays primary and each cleanup error is attached
        # to it as a note; when the body succeeded, the cleanup errors are
        # aggregated and raised only after every attempt has been made.
        cleanup_errors = []
        for client in clients:
            try:
                await client.cleanup()
            # A cleanup attempt must also survive cancellation, so the blind
            # catch is deliberate here.
            except BaseException as cleanup_error:  # noqa: BLE001
                cleanup_errors.append(cleanup_error)
        if original_failure is not None:
            for cleanup_error in cleanup_errors:
                original_failure.add_note(f"MCP_TRADE_RECORDS_CLEANUP_ERROR {cleanup_error!r}")
        elif cleanup_errors:
            primary = cleanup_errors[0]
            for extra in cleanup_errors[1:]:
                primary.add_note(f"MCP_TRADE_RECORDS_CLEANUP_ERROR {extra!r}")
            raise primary


# The transfer contract's stable failure code, read from the client's own
# latched structured error rather than from a child's stderr ring.
_MCP_ERROR_CODE = re.compile(r'"code":\s*"([A-Za-z_]+)"')


def _latched_error_code(failure_text):
    """Return the structured MCP error code one tool failure latched.

    The negative control reads the client's own observation: the structured
    error the calling process latched when the unpaced transport failed.  An
    arm that is not load-bearing fails here, because the unarmed pair would
    either complete the trade or fail with some unrelated code.
    """
    match = _MCP_ERROR_CODE.search(failure_text)
    if match is None:
        raise AssertionError(
            "unarmed pair did not latch a structured MCP error: "
            + failure_text[:400]
        )
    return match.group(1)


def _emit_row(runtime_mode, payload):
    """Emit one acceptance row: invoking runtime origins, then the payload.

    The origins line is what the evidence builder records as the resolved
    runtime identity of the cell, so a row cannot claim a runtime it did not
    import.
    """
    origins = _assert_invoking_runtime(runtime_mode)
    print(
        "MCP_TRADE_RECORDS_CHILD_ORIGINS "
        f"{runtime_mode} {origins['pyboy']} {origins['serial']}",
        flush=True,
    )
    print("MCP_TRADE_RECORDS_ROM " + json.dumps(payload, sort_keys=True), flush=True)


def _base_row_payload(version, peer_version, primary_asset, peer_asset, primary_fixture, peer_fixture):
    return {
        "version": version,
        "peer_version": peer_version,
        "runtime_mode": _child_runtime_mode(),
        "primary_fixture": primary_fixture,
        "peer_fixture": peer_fixture,
        "fixture_sha1": _fixture_sha1(primary_asset),
        "peer_fixture_sha1": _fixture_sha1(peer_asset),
    }


class _SyntheticTcpClient:
    """Minimal single-session MCP surface for the TCP driver's own controls.

    Answers ``tools/list``/``resources/list`` from an explicit surface, records
    every ``tools/call`` it receives, and refuses a name that surface never
    published -- the fail-closed behaviour an installed client relies on.  No
    emulator, ROM, or child process is involved, so these controls cost nothing
    and can probe failure paths the real rows cannot be made to hit.
    """

    _RESOURCE_URIS = (
        "pokered://game-state",
        "pokered://party-records",
        "pokered://events",
    )

    def __init__(self, tools, *, cleanup_failure=None):
        self.tools = frozenset(tools)
        self.calls = []
        self.cleanups = 0
        self.cleanup_failure = cleanup_failure

    async def request(self, method, params=None):
        if method == "tools/list":
            return {"tools": [{"name": name} for name in sorted(self.tools)]}
        if method == "resources/list":
            return {"resources": [{"uri": uri} for uri in self._RESOURCE_URIS]}
        raise AssertionError(f"unexpected request {method!r}")

    async def tool(self, name, arguments=None):
        self.calls.append((name, dict(arguments or {})))
        assert name in self.tools, f"undiscovered tool {name!r} in {sorted(self.tools)}"
        return self._reply(name)

    @staticmethod
    def _reply(name):
        if name == "load_state":
            return {"ok": True}
        if name == "link_listen":
            return {"remote_mode": "listening"}
        if name in {"link_connect", "link_status"}:
            return {"remote_mode": "connected"}
        if name == "link_frame_barrier":
            return {
                "enabled": True,
                "network_frame_barrier": True,
                "remote_mode": "connected",
            }
        return {"ok": True}

    def called(self):
        return {name for name, _ in self.calls}

    async def cleanup(self):
        self.cleanups += 1
        if self.cleanup_failure is not None:
            raise self.cleanup_failure


def _patch_synthetic_tcp_launch(monkeypatch, clients):
    """Replace the two real subprocess launches with synthetic clients."""
    launches = list(clients)

    monkeypatch.setitem(
        globals(), "_stage_assets", lambda directory, primary, peer: (primary, peer)
    )
    monkeypatch.setitem(globals(), "_assert_invoking_runtime", lambda runtime_mode: None)

    async def launch(env):
        assert env, env
        return launches.pop(0)

    monkeypatch.setitem(globals(), "_launch_server", launch)


def _synthetic_assets():
    pins = {"expected_rom_sha1": "0" * 40, "expected_symbol_sha1": "1" * 40}
    return [
        {
            "rom": Path("primary.gb"),
            "sym": Path("primary.sym"),
            "family": family,
            "pins": dict(pins),
            "state": b"\x00\x01",
        }
        for family in ("red", "blue")
    ]


@pytest.mark.asyncio
async def test_tcp_driver_invokes_only_tools_its_own_server_advertises():
    """Finding 1: every tool the TCP driver calls must be in that process's list.

    ``_tool_specs(has_peer=False)`` is the surface a single-session installed
    server publishes; it drops the in-process ``link_step`` because there is no
    second session to drive, so the advertised equivalent is ``step``.  Reading
    the production surface rather than restating it keeps the control honest if
    the surface changes later.  The clients refuse any name they did not
    publish, so a driver that reached for ``link_step`` -- as the reviewed head
    did -- fails here instead of passing on permissive SDK dispatch.
    """
    from pokered_harness.mcp_server import _tool_specs

    advertised = sorted(spec.name for spec in _tool_specs(has_peer=False))
    assert "link_step" not in advertised, advertised
    clients = [_SyntheticTcpClient(advertised) for _ in range(2)]
    pair = TcpPair(clients, ("red", "blue"), "source")

    await pair.assert_surface()
    await pair.load_fixture({"state": b"\x00\x01"}, owner=0)
    await pair.link_up()
    await pair.arm_network_frame_barrier()
    await pair.press(0, "a", duration=1)
    await pair.step(2)

    invoked = {name for client in clients for name in client.called()}
    assert invoked == {
        "load_state",
        "step",
        "press",
        "link_listen",
        "link_connect",
        "link_status",
        "link_frame_barrier",
    }, sorted(invoked)
    assert invoked <= set(advertised), sorted(invoked)
    assert set(_TCP_DRIVER_TOOL_NAMES) == invoked, sorted(_TCP_DRIVER_TOOL_NAMES)
    # Each owner was stepped twice, once per requested frame, through the
    # advertised tool the pair actually selected.
    assert [sum(1 for name, _ in client.calls if name == "step") for client in clients] == [2, 2]


@pytest.mark.asyncio
async def test_tcp_surface_requirement_rejects_a_process_missing_an_invoked_tool():
    """Finding 1: the discovery requirement is load-bearing, not decorative.

    The declared driver set is only a contract if a surface that omits one of
    its names is rejected.  Here every published name but the pacing control is
    advertised, so ``assert_surface`` must fail on the missing
    ``link_frame_barrier`` rather than let a row arm a control its own process
    never offered.
    """
    surface = sorted(
        (set(_TCP_DRIVER_TOOL_NAMES) | {"release", "link_disconnect"}) - {"link_frame_barrier"}
    )
    clients = [_SyntheticTcpClient(surface) for _ in range(2)]
    pair = TcpPair(clients, ("red", "blue"), "source")

    with pytest.raises(AssertionError) as failure:
        await pair.assert_surface()
    assert "link_frame_barrier" in str(failure.value), failure.value


@pytest.mark.asyncio
async def test_tcp_pair_servers_attempt_every_cleanup_when_the_body_succeeds(monkeypatch, tmp_path):
    """Finding 3: one failing cleanup must not strand the sibling server.

    The unarmed-barrier control ends its expected-failure body without
    ``pair.eof()`` and relies on this finalizer to terminate both live servers,
    so every owned client has to be attempted even after an earlier attempt
    raises.  Both synthetic cleanups raise here: both must still be attempted,
    and the first failure must surface only after the loop, carrying the second
    as a note.
    """
    clients = [
        _SyntheticTcpClient(_TCP_DRIVER_TOOL_NAMES, cleanup_failure=TimeoutError("first")),
        _SyntheticTcpClient(_TCP_DRIVER_TOOL_NAMES, cleanup_failure=TimeoutError("second")),
    ]
    primary, peer = _synthetic_assets()
    _patch_synthetic_tcp_launch(monkeypatch, clients)

    with pytest.raises(TimeoutError) as failure:
        async with _tcp_pair_servers(tmp_path, primary, peer) as pair:
            assert isinstance(pair, TcpPair), pair

    assert [client.cleanups for client in clients] == [1, 1]
    assert "first" in str(failure.value), failure.value
    notes = getattr(failure.value, "__notes__", [])
    assert any("MCP_TRADE_RECORDS_CLEANUP_ERROR" in note and "second" in note for note in notes), (
        notes
    )


@pytest.mark.asyncio
async def test_tcp_pair_servers_keep_the_body_failure_and_note_every_cleanup_failure(
    monkeypatch, tmp_path
):
    """Finding 3: cleanup errors must not replace a failed body.

    The body failure stays primary and both cleanup failures are attached to it,
    so a row that already failed for its own reason still reports why the owned
    processes could not be settled.  This pins the behaviour the aggregation
    above must preserve (it already held before the correction).
    """
    clients = [
        _SyntheticTcpClient(_TCP_DRIVER_TOOL_NAMES, cleanup_failure=TimeoutError("first")),
        _SyntheticTcpClient(_TCP_DRIVER_TOOL_NAMES, cleanup_failure=TimeoutError("second")),
    ]
    primary, peer = _synthetic_assets()
    _patch_synthetic_tcp_launch(monkeypatch, clients)
    body_failure = RuntimeError("trade row failed")

    with pytest.raises(RuntimeError) as failure:
        async with _tcp_pair_servers(tmp_path, primary, peer):
            raise body_failure

    assert failure.value is body_failure, failure.value
    assert [client.cleanups for client in clients] == [1, 1]
    notes = getattr(failure.value, "__notes__", [])
    assert sum(1 for note in notes if "MCP_TRADE_RECORDS_CLEANUP_ERROR" in note) == 2, notes


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
                "offered_slots": {
                    str(owner): slot for owner, slot in sorted(run.offered.items())
                },
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
                "offered_slots": {
                    str(owner): slot for owner, slot in sorted(run.offered.items())
                },
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


async def _enter_stats_trade_submenu(pair, party_count):
    """Advance from the live party menu into each owner's STATS/TRADE sub-menu."""
    spent = 0
    while spent < 800:
        states = await _all_states(pair)
        opened = [
            _menu(state)["watched_keys"] in (STATS_MENU_KEYS, TRADE_MENU_KEYS)
            for state in states
        ]
        if all(opened):
            _log("submenu", f"frames={spent}")
            return states, spent
        # Press A only on an owner whose live party menu is up and which has not
        # opened the sub-menu yet: A on the TRADE entry would confirm the offer,
        # so a blind mash would commit instead of cancelling.
        for owner, state in enumerate(states):
            if not opened[owner] and _player_mon_menu_live(state, party_count):
                await pair.press(owner, "a", duration=4)
        await _link_step(pair, STEP_CHUNK)
        spent += STEP_CHUNK
    states = await _all_states(pair)
    raise AssertionError(
        f"STATS/TRADE sub-menu never opened on both owners; states={json.dumps(states)}"
    )


async def _back_out_of_submenu(pair, party_count):
    """Press B on both owners and require the live party menu to return."""
    await _press_both(pair, "b", duration=4)
    return await _advance_until(
        pair,
        lambda state: _player_mon_menu_live(state, party_count),
        budget=400,
        prompt="B did not return both owners to the trade party menu",
    )


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


async def _drive_to_commitment(
    pair, *, party_counts, primary_before, peer_before, primary_slot=0, peer_slot=0
):
    """Drive the pair to the first observed committed record copy, then stop.

    The caller interrupts *here* on purpose: the ROM has confirmed both offers
    and appended each received record, but the trade animation, the forced
    evolution check, the save and the room return are all still outstanding, so
    the disconnect happens with committed work in flight.  Returns the records,
    states, ROM-owned copy counts and frames spent at that observation.
    """
    expected_primary = _compaction_expected_digests(
        primary_before, primary_slot, peer_before[peer_slot]["digest"]
    )
    expected_peer = _compaction_expected_digests(
        peer_before, peer_slot, primary_before[primary_slot]["digest"]
    )
    targets = (primary_slot, peer_slot)
    offered: dict[int, int] = {}
    spent = 0
    received: list[int] = []
    while spent < TRADE_BUDGET:
        records = await _all_records(pair)
        received = await _trade_received_counts(pair)
        if all(count >= 1 for count in received):
            primary_after = _records_from_payload(records[0])
            peer_after = _records_from_payload(records[1])
            if (
                _digests(primary_after) == expected_primary
                and _digests(peer_after) == expected_peer
            ):
                states = await _all_states(pair)
                _log(
                    "trade_copy",
                    f"frames={spent} trade_received={received} interrupted_at_commitment",
                )
                assert offered == {0: primary_slot, 1: peer_slot}, offered
                return records, states, received, spent
        states = await _all_states(pair)
        actions = []
        for owner, state in enumerate(states):
            action, observation = await _owner_trade_action(
                pair,
                owner,
                state,
                party_count=party_counts[owner],
                target_slot=targets[owner],
                confirmed=owner in offered,
            )
            if observation is not None:
                offered[owner] = observation
            actions.append(action)
        await _press_actions(pair, actions[0], actions[1])
        await _link_step(pair, STEP_CHUNK)
        spent += STEP_CHUNK
    raise AssertionError(
        "trade never reached an observed committed copy within "
        f"{TRADE_BUDGET} frames; trade_received={received} offered={offered}"
    )


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
        expected_primary = _compaction_expected_digests(
            primary_before, 0, peer_before[0]["digest"]
        )
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
