"""Support helpers for the real-ROM MCP stdio trade acceptance module.

Split from ``tests/test_mcp_trade_records_rom.py`` (#208) with no behavior
change: the ordered-orientation constants, the fixture registrations, the
session/launch harness, and the ``LocalPair``/``TcpPair`` drivers moved here
verbatim so the facade stays a small collection home for the node IDs that
``scripts/tcp_link_matrix.py`` and ``tests/_tier_config.py`` reference."""

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
                original_failure.add_note(f"MCP_TRADE_RECORDS_CLEANUP_ERROR {cleanup_error!r}")


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
            await self.client.tool("link_peer_press", {"button": button, "duration": duration})

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
            "unarmed pair did not latch a structured MCP error: " + failure_text[:400]
        )
    return match.group(1)


def _fixture_sha1(asset):
    return asset["provenance"]["registry"]["sha1"]
