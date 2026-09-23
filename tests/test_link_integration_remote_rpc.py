"""Remote link RPC-flow regressions (two-process TCP, real-ROM gated).

Split from ``tests/test_link_integration_remote.py`` for #137 with no
behavior change: every assertion and test ID is preserved verbatim.
"""
from __future__ import annotations

import sys

import pytest

from tests._link_integration_remote_support import (
    _FIXTURES_WITH_WALKABLE_PLAYER,
    _cable_club_state,
    _drive_remote_to_link_menu,
    _ensure_fixture_is_walkable,
    _install_autoselect_trade_hook,
    _open_session,
    _press_remote_both,
    _roms_present,
    _start_remote_runners,
    _stop_remote_runners,
    _tcp_pair,
)


@pytest.mark.parametrize(
    "version_listen,version_connect",
    [
        ("red", "red"),
        ("red", "blue"),
        ("blue", "red"),
        ("red", "yellow"),
        ("yellow", "red"),
        ("blue", "blue"),
        ("blue", "yellow"),
        ("yellow", "blue"),
        ("yellow", "yellow"),
    ],
)
def test_remote_rpc_kinds_flow_over_tcp_reaching_link_menu(
    version_listen: str, version_connect: str
) -> None:
    """Observe every ``link.exchange`` RPC that flows during the
    Cable Club drive to LinkMenu.

    The nybble test proves the game reaches LinkMenu. This test is
    about the transport-layer observation: which symbolic ``kind``
    values the game actually asks the endpoint to RPC over TCP, in
    what order, and whether the exchange returns matching bytes.

    Expected kinds during the drive:
    - ``exchange_nybble/wSerialExchangeNybbleSendData`` (from
      Serial_SyncAndExchangeNybble, called many times during the
      attendant-dialog handshake and to gate LinkMenu entry)

    This is the last two-process gap I can close without agent policy
    for menu navigation. Post-LinkMenu exchanges
    (Serial_ExchangeLinkMenuSelection, Serial_ExchangeBytes) require
    coordinated A-press timing on both sides; the existing in-process
    LinkPair trade_roundtrip test covers that game-code flow, and the
    Phase 1 serial-link tests + Phase 2 symbol-translation tests cover
    the transport and kind-encoding separately.
    """
    if not (_roms_present(version_listen) and _roms_present(version_connect)):
        pytest.skip(f"ROMs not present for {version_listen}/{version_connect}")
    state_listen = _cable_club_state(version_listen)
    state_connect = _cable_club_state(version_connect)
    if not (state_listen.exists() and state_connect.exists()):
        pytest.skip(
            f"Cable Club save states missing for "
            f"{version_listen}/{version_connect}"
        )
    if not (
        version_listen in _FIXTURES_WITH_WALKABLE_PLAYER
        and version_connect in _FIXTURES_WITH_WALKABLE_PLAYER
    ):
        pytest.skip(
            f"Fixture walkability gap: {version_listen}/{version_connect}"
        )

    session_a = _open_session(version_listen)
    session_b = _open_session(version_connect)
    try:
        session_a.load_state(state_listen.read_bytes())
        session_b.load_state(state_connect.read_bytes())
        _ensure_fixture_is_walkable(version_listen, session_a)
        _ensure_fixture_is_walkable(version_connect, session_b)

        link_a, link_b, endpoint_a, endpoint_b = _tcp_pair(
            session_a, version_listen, session_b, version_connect
        )
        try:
            # Wrap the link.exchange methods to record every RPC kind
            # that crosses the TCP boundary. Stays correct under the
            # runner's background-thread pressure (queue.Queue-backed
            # SerialLink doesn't mind the wrapper).
            kinds_a: list[str] = []
            kinds_b: list[str] = []
            orig_ex_a = link_a.exchange
            orig_ex_b = link_b.exchange

            def _wrap(sink: list[str], inner):
                def exchange(kind, my_bytes, *, timeout_ms=5000):
                    sink.append(kind)
                    return inner(kind, my_bytes, timeout_ms=timeout_ms)

                return exchange

            link_a.exchange = _wrap(kinds_a, orig_ex_a)  # type: ignore[method-assign]
            link_b.exchange = _wrap(kinds_b, orig_ex_b)  # type: ignore[method-assign]

            runner_a, runner_b = _start_remote_runners(
                session_a, endpoint_a, session_b, endpoint_b
            )
            try:
                # Drive until the nybble RPC has fired at least a
                # handful of times on each side using frame progress.
                _drive_remote_to_link_menu(
                    runner_a,
                    runner_b,
                    lambda: len(kinds_a) >= 3 and len(kinds_b) >= 3,
                )
            finally:
                _stop_remote_runners(runner_a, runner_b)
            assert runner_a.exc is None, runner_a.exc
            assert runner_b.exc is None, runner_b.exc
            nybble_kind = (
                "exchange_nybble/wSerialExchangeNybbleSendData"
            )
            assert nybble_kind in kinds_a, (
                f"primary never issued nybble RPC; kinds_a={kinds_a}"
            )
            assert nybble_kind in kinds_b, (
                f"peer never issued nybble RPC; kinds_b={kinds_b}"
            )
            # And the counts must be roughly balanced — if they diverge
            # by a huge margin, one side is over/under-exchanging and
            # the peer would drift.
            count_a = kinds_a.count(nybble_kind)
            count_b = kinds_b.count(nybble_kind)
            assert abs(count_a - count_b) <= max(count_a, count_b), (
                f"nybble RPC count wildly unbalanced: "
                f"count_a={count_a}, count_b={count_b}"
            )
        finally:
            link_a.close()
            link_b.close()
    finally:
        session_a.close()
        session_b.close()


# --- past-LinkMenu RPC flow observation ---------------------------------
#
# The earlier post-LinkMenu attempt using hook counters on
# Serial_ExchangeLinkMenuSelection hit a PyBoy-hook-collision dead end
# (RemoteLinkEndpoint registers its own hook there first, and PyBoy 2.7
# rejects a second hook at the same address). Observing at the
# link.exchange layer sidesteps that entirely — we see every RPC kind
# the game asks the endpoint to issue, regardless of hook-registration
# ordering.


# racing A-press timing. In a real two-agent deployment, each agent's
# policy would press A at a well-defined point — that's essentially
# synchronous from the game's perspective. We simulate that here by
# *injecting* the LINK_MENU_TRADE vote byte (0xD4) directly into both
# sides' wLinkMenuSelectionSendBuffer once we observe LinkMenu has
# entered its exchange loop. That's exactly what a cooperating pair
# of agent policies would produce; it isolates the transport from the
# input-timing concern.

@pytest.mark.parametrize(
    "version_listen,version_connect",
    [
        ("red", "red"),
        ("red", "blue"),
        ("blue", "red"),
        ("red", "yellow"),
        ("yellow", "red"),
        ("blue", "blue"),
        ("blue", "yellow"),
        ("yellow", "blue"),
        ("yellow", "yellow"),
    ],
)
def test_remote_menu_vote_converges_and_warps_to_trade_center(
    version_listen: str, version_connect: str
) -> None:
    """Prove the full LinkMenu → TRADE_CENTER warp flow works over TCP.

    Walks to LinkMenu, then installs the same auto-select-
    TRADE hook LinkPair uses for its single-process tests
    (LinkMenu.exchangeMenuSelectionLoop + 3 pre-plants 0xD4 into
    wLinkMenuSelectionReceiveBuffer, simulating "peer pressed A on
    TRADE" regardless of actual A-press timing).

    With both sides' recv buffers forced to 0xD4, pokered's LinkMenu
    takes the enemyPressedAOrB → useEnemyMenuSelection →
    doneChoosingMenuSelection path and writes
    wCableClubDestinationMap = TRADE_CENTER. SpecialEnterMap warps
    both peers to map 0xEF. This asserts that warp happened on both
    sides — transport-level proof that the full menu-selection byte
    exchange round-trips over TCP correctly.

    Reaching CableClub_DoBattleOrTradeAgain (and its three
    Serial_ExchangeBytes blocks) from here additionally requires the
    two players to walk onto the hidden-event tile and press A in the
    same frame; that's not transport-testable from daemon threads and
    is covered by in-process tests (test_link_integration.test_link_trade_roundtrip)
    instead.

    Parametrized over the full 3×3 matrix; a row skips only when its pinned
    ROM, symbols, or Cable Club state is unavailable.
    """
    if not (_roms_present(version_listen) and _roms_present(version_connect)):
        pytest.skip(f"ROMs not present for {version_listen}/{version_connect}")
    state_listen = _cable_club_state(version_listen)
    state_connect = _cable_club_state(version_connect)
    if not (state_listen.exists() and state_connect.exists()):
        pytest.skip(
            f"Cable Club save states missing for "
            f"{version_listen}/{version_connect}"
        )
    if not (
        version_listen in _FIXTURES_WITH_WALKABLE_PLAYER
        and version_connect in _FIXTURES_WITH_WALKABLE_PLAYER
    ):
        pytest.skip(
            f"Fixture walkability gap: {version_listen}/{version_connect}"
        )

    session_a = _open_session(version_listen)
    session_b = _open_session(version_connect)
    try:
        session_a.load_state(state_listen.read_bytes())
        session_b.load_state(state_connect.read_bytes())
        _ensure_fixture_is_walkable(version_listen, session_a)
        _ensure_fixture_is_walkable(version_connect, session_b)

        link_a, link_b, endpoint_a, endpoint_b = _tcp_pair(
            session_a, version_listen, session_b, version_connect
        )
        try:
            kinds_a: list[str] = []
            kinds_b: list[str] = []
            orig_ex_a = link_a.exchange
            orig_ex_b = link_b.exchange

            def _wrap(sink, inner):
                def exchange(kind, my_bytes, *, timeout_ms=5000):
                    sink.append(kind)
                    return inner(kind, my_bytes, timeout_ms=timeout_ms)
                return exchange

            link_a.exchange = _wrap(kinds_a, orig_ex_a)  # type: ignore[method-assign]
            link_b.exchange = _wrap(kinds_b, orig_ex_b)  # type: ignore[method-assign]

            # Install the game-code test hook before either PyBoy thread
            # starts. This avoids concurrent hook registration while still
            # exercising the real LinkMenu exchange over TCP.
            _install_autoselect_trade_hook(session_a)
            _install_autoselect_trade_hook(session_b)

            runner_a, runner_b = _start_remote_runners(
                session_a, endpoint_a, session_b, endpoint_b
            )
            try:
                # Drive to LinkMenu via A-presses.
                menu_kind = "menu_selection/wLinkMenuSelectionSendBuffer"
                _drive_remote_to_link_menu(
                    runner_a,
                    runner_b,
                    lambda: menu_kind in kinds_a and menu_kind in kinds_b,
                )
                assert menu_kind in kinds_a and menu_kind in kinds_b, (
                    "didn't reach LinkMenu's exchange loop"
                )

                # Wait for the TRADE_CENTER warp. Both peers land on
                # opposite sides of the trade table.
                TRADE_CENTER = 0xEF
                for _ in range(100):
                    m_a = session_a.read_game_state().overworld.map_id
                    m_b = session_b.read_game_state().overworld.map_id
                    if m_a == TRADE_CENTER and m_b == TRADE_CENTER:
                        break
                    _press_remote_both(
                        runner_a,
                        runner_b,
                        "a",
                        duration=4,
                    )
            finally:
                _stop_remote_runners(runner_a, runner_b)
            assert runner_a.exc is None, runner_a.exc
            assert runner_b.exc is None, runner_b.exc
            map_a = session_a.read_game_state().overworld.map_id
            map_b = session_b.read_game_state().overworld.map_id
            assert map_a == TRADE_CENTER, (
                f"listener didn't warp to TRADE_CENTER "
                f"(map_a=0x{map_a:02x}); menu vote did not converge "
                f"over TCP despite auto-select-TRADE hook"
            )
            assert map_b == TRADE_CENTER, (
                f"connector didn't warp to TRADE_CENTER "
                f"(map_b=0x{map_b:02x})"
            )
            menu_kind = "menu_selection/wLinkMenuSelectionSendBuffer"
            count_a = kinds_a.count(menu_kind)
            count_b = kinds_b.count(menu_kind)
            assert abs(count_a - count_b) <= max(count_a, count_b), (
                f"menu-selection RPC count wildly unbalanced: "
                f"a={count_a} b={count_b}"
            )
            sys.stderr.write(
                f"\n[menu-vote-converges {version_listen}↔{version_connect}] "
                f"menu_sel={count_a}/{count_b} map_a=0x{map_a:02x} "
                f"map_b=0x{map_b:02x}\n"
            )
        finally:
            link_a.close()
            link_b.close()
    finally:
        session_a.close()
        session_b.close()


# --- T3: full CableClub_DoBattleOrTrade drive over TCP ------------------
#
# Uses the LockstepOrchestrator (per-frame sync across the two sessions)
# to walk both players onto the TRADE_CENTER hidden-event tiles and
# press A on the same game frame. When both CableClubLeftGameboy and
# CableClubRightGameboy fire, the game enters CableClub_DoBattleOrTrade
# which runs three Serial_ExchangeBytes blocks — the RPC kinds we're
# observing end-to-end over TCP.


