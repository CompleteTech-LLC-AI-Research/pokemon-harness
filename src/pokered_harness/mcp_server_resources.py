"""MCP resource catalogue and read handlers.

Split from ``pokered_harness.mcp_server`` for issue #125 with no behavior
change: the code below is copied verbatim except that calls to facade-owned,
monkeypatch-patched entry points resolve through ``_entry`` so attribute patches
on ``pokered_harness.mcp_server`` stay visible.
"""

from __future__ import annotations

import json
from typing import Any

import mcp.types as mcp_types

from pokered_harness.events.hooks import GameEvent

# Direct names this module calls; cyclic back-edges go through _entry.
from pokered_harness.mcp_server_model import (
    _URI_EVENT_LOG,
    _URI_GAME_STATE,
    _URI_LINK_STATUS,
    _URI_LINK_TRANSPORT,
    _URI_PARTY_RECORDS,
    _URI_PEER_EVENTS,
    _URI_PEER_GAME_STATE,
    _URI_PEER_PARTY_RECORDS,
    _URI_STATE_EPOCH,
    LinkState,
    McpHarnessError,
)
from pokered_harness.mcp_timed_owner import TimedOwner
from pokered_harness.serialize import to_jsonable
from pokered_harness.session import Session, SessionEpoch, StateSnapshot


def _epoch_payload(epoch: SessionEpoch) -> dict[str, int | str]:
    return {
        "tick": epoch.tick,
        "load_generation": epoch.load_generation,
        "reset_generation": epoch.reset_generation,
        "session_id": epoch.session_id,
    }


def _game_state_payload(snapshot: StateSnapshot) -> dict[str, Any]:
    # The epoch is embedded in the same JSON object as the parsed state, so a
    # client never has to correlate two independent resource reads.  The
    # standalone `pokered://state-epoch` resource is retained for polling.
    payload = to_jsonable(snapshot.state)
    payload["epoch"] = _epoch_payload(snapshot.epoch)
    return payload


def read_resource(
    session: Session,
    uri: str,
    link: LinkState | None = None,
    *,
    timed_owner: TimedOwner | None = None,
) -> str:
    """Resource reader — returns JSON text."""
    if timed_owner is not None:
        if timed_owner.session is not session:
            raise McpHarnessError(
                "invalid_timed_configuration", "timed owner belongs to another session"
            )
        if uri in {
            _URI_PEER_GAME_STATE,
            _URI_PEER_PARTY_RECORDS,
            _URI_PEER_EVENTS,
            _URI_LINK_TRANSPORT,
        }:
            raise McpHarnessError(
                "timed_unsupported_resource", "timed remote transport has no local peer"
            )
        if uri == _URI_LINK_STATUS:
            return json.dumps(_entry._timed_status(timed_owner))
        return timed_owner.submit(lambda owned: _entry.read_resource(owned, uri, link)).result()
    if uri == _URI_GAME_STATE:
        return json.dumps(_game_state_payload(session.read_state_snapshot()))
    if uri == _URI_STATE_EPOCH:
        # Read-only session bookkeeping: lets a client pair a game-state
        # snapshot with a monotonic tick and reject a stale observation after
        # a later step/load without touching emulator state.  The monotonic
        # load_generation advances on every load_state, which the tick does
        # not, so a rewound state is still detectable.  session_id
        # distinguishes a replacement session, and all three counters are
        # captured together (read_epoch) so the payload is never torn.
        return json.dumps(_epoch_payload(session.read_epoch()))
    if uri == _URI_PARTY_RECORDS:
        # Bounded, read-only projection: per-slot record SHA-256 digests plus
        # the sanitized species/level needed to interpret them.  Raw record
        # bytes and absolute paths are deliberately never serialized.
        return json.dumps(session.read_party_records().to_resource_payload())
    if uri == _URI_PEER_PARTY_RECORDS:
        # Owner-scoped peer observation: read the peer Session's own party
        # records under that owner's lock. Like peer-game-state, this is
        # strictly observational and never advances or repairs either owner.
        if link is None or link.peer_session is None:
            raise McpHarnessError("peer_not_configured", "peer session not configured")
        with link.state():
            peer = link.peer_session
        return json.dumps(
            peer.read_party_records().to_resource_payload(source="peer-party-records")
        )
    if uri == _URI_EVENT_LOG:
        events: list[GameEvent] = session.event_snapshot()
        return json.dumps([to_jsonable(e) for e in events])
    if uri == _URI_PEER_EVENTS:
        # Owner-scoped peer observation, mirroring ``peer-game-state`` and
        # ``peer-party-records``: read the peer Session's own latched event log
        # under that owner's lock. Observational only; no raw bytes, no paths.
        if link is None or link.peer_session is None:
            raise McpHarnessError("peer_not_configured", "peer session not configured")
        with link.state():
            peer = link.peer_session
        return json.dumps([to_jsonable(e) for e in peer.event_snapshot()])
    if uri == _URI_PEER_GAME_STATE:
        if link is None or link.peer_session is None:
            raise McpHarnessError("peer_not_configured", "peer session not configured")
        with link.state():
            peer = link.peer_session
        return json.dumps(_game_state_payload(peer.read_state_snapshot()))
    if uri == _URI_LINK_TRANSPORT:
        if link is None:
            return json.dumps({"a_to_b": [], "b_to_a": []})
        with link.state():
            pair = link.pair
        if pair is None:
            return json.dumps({"a_to_b": [], "b_to_a": []})
        # The pair object can be unpaired concurrently with a resource read.
        # Keep the snapshot serialized with pair stepping/unpairing, but do
        # not take the global operation lock: status/resources must remain
        # responsive while a remote operation is waiting on TCP.
        with link._pair_lock:
            return json.dumps(pair.transport.snapshot())
    if uri == _URI_LINK_STATUS:
        if link is None:
            link = LinkState()
        return json.dumps(_entry.dispatch_tool(session, "link_status", {}, link=link))
    raise McpHarnessError("invalid_resource", f"unknown resource: {uri!r}")


def _resource_specs(has_peer: bool = False) -> list[mcp_types.Resource]:
    specs = [
        mcp_types.Resource(
            uri=_URI_GAME_STATE,  # type: ignore[arg-type]
            name="Game State",
            description=(
                "Current parsed game state snapshot (JSON). Includes an "
                "`epoch` object (`tick`, `load_generation`, "
                "`reset_generation`, `session_id`) captured in the same lock "
                "scope as the state, so a client can reject a stale snapshot "
                "without a second resource read. Battle fields expose the raw "
                "`wBattleResult` byte as `battle.raw_battle_result` and a "
                "`battle.terminal_result` that is set only for a non-zero "
                "outcome byte surviving teardown; an ambiguous zero at the "
                "falling edge stays null because win, blackout, and escape "
                "all leave or clear zero. Transient fields "
                "(`battle.move_menu_type`, `battle.player_move_list_index`, "
                "`battle.current_menu_item`, `battle.player_selected_move`, "
                "`battle.enemy_selected_move`, "
                "`battle.action_result_or_took_turn`, "
                "`battle.in_handle_player_mon_fainted`) and decoded "
                "`battle.player_stat_stages`/`battle.enemy_stat_stages` "
                "report null when their symbol is absent. `battle.phase` "
                "reports `command_selection` only from session "
                "execution-hook evidence (`battle.menu_open`; "
                "`battle.menu_evidence` names the hooked ROM labels), never "
                "from a persistent mode byte."
            ),
            mimeType="application/json",
        ),
        mcp_types.Resource(
            uri=_URI_STATE_EPOCH,  # type: ignore[arg-type]
            name="State Epoch",
            description=(
                "Monotonic session tick, load/reset generations, and session "
                "identity captured atomically for `pokered://game-state`. The "
                "tick advances with stepping; the load generation advances on "
                "every `load_state` and the reset generation on every "
                "`reset_tick`, so a rewound clock remains detectable; "
                "`session_id` distinguishes a replacement session, including "
                "across a server restart. Compare successive reads to reject "
                "stale snapshots."
            ),
            mimeType="application/json",
        ),
        mcp_types.Resource(
            uri=_URI_EVENT_LOG,  # type: ignore[arg-type]
            name="Event Log",
            description="All execution-hook events observed this session.",
            mimeType="application/json",
        ),
        mcp_types.Resource(
            uri=_URI_LINK_STATUS,  # type: ignore[arg-type]
            name="Link Status",
            description=(
                "Current link-cable state (in-process pair + remote TCP "
                "link). Equivalent to the `link_status` tool as a pollable "
                "resource."
            ),
            mimeType="application/json",
        ),
        mcp_types.Resource(
            uri=_URI_PARTY_RECORDS,  # type: ignore[arg-type]
            name="Party Records",
            description=(
                "Read-only per-slot SHA-256 digests of the raw 44-byte "
                "party_struct records, plus sanitized species/level fields "
                "for interpretation. Observational only; no raw bytes."
            ),
            mimeType="application/json",
        ),
    ]
    if has_peer:
        specs.append(
            mcp_types.Resource(
                uri=_URI_PEER_GAME_STATE,  # type: ignore[arg-type]
                name="Peer Game State",
                description=(
                    "Parsed game state of the peer session (JSON), including "
                    "the peer's atomically captured `epoch`."
                ),
                mimeType="application/json",
            )
        )
        specs.append(
            mcp_types.Resource(
                uri=_URI_PEER_PARTY_RECORDS,  # type: ignore[arg-type]
                name="Peer Party Records",
                description=(
                    "Read-only per-slot SHA-256 digests of the peer session's "
                    "raw 44-byte party_struct records, plus sanitized "
                    "species/level fields for interpretation. Observational "
                    "only; no raw bytes."
                ),
                mimeType="application/json",
            )
        )
        specs.append(
            mcp_types.Resource(
                uri=_URI_PEER_EVENTS,  # type: ignore[arg-type]
                name="Peer Event Log",
                description=(
                    "Read-only latched execution-hook event log of the peer "
                    "session (JSON). The peer's own ROM-owned milestone "
                    "observations, mirroring pokered://events for the primary. "
                    "Observational only; no raw bytes."
                ),
                mimeType="application/json",
            )
        )
        specs.append(
            mcp_types.Resource(
                uri=_URI_LINK_TRANSPORT,  # type: ignore[arg-type]
                name="Link Transport",
                description="Current LinkTransport byte-queue snapshot (JSON).",
                mimeType="application/json",
            )
        )
    return specs


# Call-time indirection so facade-level monkeypatches stay visible here.
# Deferred (issue #240) instead of an eager back-edge, so this module can
# be imported first in a fresh interpreter.
from pokered_harness._mcp_facade_entry import entry as _entry
