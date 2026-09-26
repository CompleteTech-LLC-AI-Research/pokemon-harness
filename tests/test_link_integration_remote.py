"""End-to-end integration tests for the TWO-PROCESS remote link cable.

The single-process equivalent is :mod:`test_link_integration` — it pairs
two :class:`Session` objects with an in-process :class:`LinkPair`. That
exercise proved the bridge logic + game-logic sequencing end-to-end on
real ROMs through the full trade handshake.

This module's job is narrower: prove that when the same two sessions
are wired through a :class:`TcpSerialLink` + :class:`RemoteLinkEndpoint`
pair (as they would be in production with two separate MCP servers),
the handshake and the hardware-serial-tick still work. We do NOT
re-exercise the trade UI flow; that would just be testing the game
logic a second time.

All tests are skipped unless the real ROMs + ``cable_club.state``
fixtures are present.
"""

from __future__ import annotations

import sys

import pytest

from pokered_harness.link.remote import STATUS_EXTERNAL, STATUS_INTERNAL
from tests._link_integration_remote_support import (
    _FIXTURES_WITH_WALKABLE_PLAYER,
    _advance_remote_runners,
    _cable_club_state,
    _drive_remote_to_link_menu,
    _ensure_fixture_is_walkable,
    _install_autoselect_trade_hook,
    _open_session,
    _roms_present,
    _start_remote_runners,
    _stop_remote_runners,
    _tcp_pair,
)

# --- scaffold test --------------------------------------------------------


# Full listener × connector matrix. Red combinations auto-skip when
# the red cable_club.state fixture is absent (Mt. Moon → Cerulean is
# not yet scripted in the Red harness). Role matters: listener is
# internal-clock master, connector is external-clock slave — the
# reversed pair (e.g. yellow-listens-blue-connects) is a distinct
# wire configuration from its inverse.
@pytest.mark.parametrize(
    "version_a,version_b",
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
def test_remote_handshake_writes_status_on_both_sides(version_a: str, version_b: str) -> None:
    """Two-process equivalent of the single-process handshake check.

    On the Cerulean Pokemon Center map the game's overworld script calls
    ``Serial_TryEstablishingExternallyClockedConnection`` every frame
    (pret/pokered scripts/CeruleanPokecenter.asm). The bridge's
    handshake hook writes a role-specific byte to
    ``hSerialConnectionStatus`` — 0x02 on the listener (internal clock)
    and 0x01 on the connector (external clock).

    In the *remote* model each endpoint independently sets its own
    local status byte — no peer exchange is required for the handshake
    itself. Successfully running both sessions in parallel over a TCP
    link and observing both status bytes flip away from the default
    0xFF proves: sessions load + step + endpoint install works end-to-
    end against real ROM code.
    """
    if not (_roms_present(version_a) and _roms_present(version_b)):
        pytest.skip(f"ROMs not present for {version_a}/{version_b}")
    state_a = _cable_club_state(version_a)
    state_b = _cable_club_state(version_b)
    if not (state_a.exists() and state_b.exists()):
        pytest.skip(
            f"Cable Club save states missing; see README for how to produce {state_a} and {state_b}"
        )

    session_a = _open_session(version_a)
    session_b = _open_session(version_b)
    try:
        session_a.load_state(state_a.read_bytes())
        session_b.load_state(state_b.read_bytes())

        link_a, link_b, endpoint_a, endpoint_b = _tcp_pair(
            session_a, version_a, session_b, version_b
        )
        try:
            status_addr = session_a.symbols.addr_of("hSerialConnectionStatus")

            runner_a, runner_b = _start_remote_runners(session_a, endpoint_a, session_b, endpoint_b)
            try:
                # The map script fires the hook every frame. Waiting on
                # runner progress keeps this bounded without polling sleeps.
                # The fixture may enter a serial routine after the
                # handshake hook has fired; that routine can intentionally
                # block while waiting for a matching peer. Only advance
                # enough frames to exercise the per-frame handshake, then
                # inspect the latched bytes.
                _advance_remote_runners(runner_a, runner_b, 16, timeout_s=6.0)
            finally:
                _stop_remote_runners(runner_a, runner_b)

            assert runner_a.exc is None, runner_a.exc
            assert runner_b.exc is None, runner_b.exc

            status_a = session_a._pyboy.memory[status_addr]
            status_b = session_b._pyboy.memory[status_addr]
            assert status_a == STATUS_INTERNAL, (
                f"listener side (primary) expected clock-role byte "
                f"0x{STATUS_INTERNAL:02x}, got 0x{status_a:02x}"
            )
            assert status_b == STATUS_EXTERNAL, (
                f"connector side (peer) expected clock-role byte "
                f"0x{STATUS_EXTERNAL:02x}, got 0x{status_b:02x}"
            )
        finally:
            link_a.close()
            link_b.close()
    finally:
        session_a.close()
        session_b.close()


# --- TCP-pair setup helper ----------------------------------------------


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
def test_remote_rpc_flow_past_link_menu_over_tcp(version_listen: str, version_connect: str) -> None:
    """Drive past the LinkMenu A-press and observe the
    Serial_ExchangeLinkMenuSelection RPC flowing over TCP.

    Pokered's LinkMenu cursor starts on BATTLE (index 0). Pressing A
    commits that vote. The game then calls
    ``Serial_ExchangeLinkMenuSelection`` each frame to exchange both
    sides' votes; once both agree, the game warps to COLOSSEUM and
    runs ``CableClub_DoBattleOrTradeAgain``'s three
    ``Serial_ExchangeBytes`` blocks (RNG list + player data + patch
    list).

    We observe at the RPC layer — the set of ``kind`` values that
    crossed the TCP boundary. A successful past-LinkMenu run shows::

        exchange_nybble/wSerialExchangeNybbleSendData     (required)
        menu_selection/wLinkMenuSelectionSendBuffer       (required)

    on both sides. The ``exchange_bytes/*`` kinds (from
    CableClub_DoBattleOrTradeAgain) are a bonus — they only appear if
    the menu-selection vote converged across the two threads and the
    game actually ran the three post-menu buffer exchanges. We record
    whether that happened but don't require it. The diagnostic installs a
    test-only common TRADE vote before starting the independent runners so
    sub-frame A-press timing cannot obscure the menu RPC itself.

    Parametrized over the full 3×3 matrix; a row skips only when its pinned
    ROM, symbols, or Cable Club state is unavailable.
    """
    if not (_roms_present(version_listen) and _roms_present(version_connect)):
        pytest.skip(f"ROMs not present for {version_listen}/{version_connect}")
    state_listen = _cable_club_state(version_listen)
    state_connect = _cable_club_state(version_connect)
    if not (state_listen.exists() and state_connect.exists()):
        pytest.skip(f"Cable Club save states missing for {version_listen}/{version_connect}")
    if not (
        version_listen in _FIXTURES_WITH_WALKABLE_PLAYER
        and version_connect in _FIXTURES_WITH_WALKABLE_PLAYER
    ):
        pytest.skip(f"Fixture walkability gap: {version_listen}/{version_connect}")

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
                    # Nybble exchanges are retried by the game and need a
                    # short timeout to let independently paced runners
                    # recover. The menu exchange is the milestone under
                    # observation and can carry the slower cross-version
                    # branch once both sides reach it.
                    effective_timeout_ms = (
                        30000
                        if kind == "menu_selection/wLinkMenuSelectionSendBuffer"
                        else timeout_ms
                    )
                    return inner(
                        kind,
                        my_bytes,
                        timeout_ms=effective_timeout_ms,
                    )

                return exchange

            link_a.exchange = _wrap(kinds_a, orig_ex_a)  # type: ignore[method-assign]
            link_b.exchange = _wrap(kinds_b, orig_ex_b)  # type: ignore[method-assign]

            # Keep the diagnostic focused on the TCP menu-selection
            # exchange. A shared test-only TRADE vote prevents one side's
            # independently timed A press from entering a different branch
            # and timing out before the RPC we want to observe.
            _install_autoselect_trade_hook(session_a)
            _install_autoselect_trade_hook(session_b)

            runner_a, runner_b = _start_remote_runners(session_a, endpoint_a, session_b, endpoint_b)
            try:
                menu_kind = "menu_selection/wLinkMenuSelectionSendBuffer"
                _drive_remote_to_link_menu(
                    runner_a,
                    runner_b,
                    lambda: menu_kind in kinds_a and menu_kind in kinds_b,
                )
            finally:
                _stop_remote_runners(runner_a, runner_b)
            assert runner_a.exc is None, runner_a.exc
            assert runner_b.exc is None, runner_b.exc
            # Nybble sync (getting us to LinkMenu) must have flowed.
            nybble_kind = "exchange_nybble/wSerialExchangeNybbleSendData"
            assert nybble_kind in kinds_a and nybble_kind in kinds_b, (
                f"nybble RPC never flowed; kinds_a={kinds_a}, kinds_b={kinds_b}"
            )
            # The past-LinkMenu milestone: menu-selection RPC flowed on
            # both sides over TCP.
            menu_kind = "menu_selection/wLinkMenuSelectionSendBuffer"
            assert menu_kind in kinds_a, (
                f"listener never issued menu-selection RPC — "
                f"LinkMenu didn't reach its exchange loop. "
                f"kinds_a tail={kinds_a[-10:]}"
            )
            assert menu_kind in kinds_b, (
                f"connector never issued menu-selection RPC. kinds_b tail={kinds_b[-10:]}"
            )
            # Bonus: if the menu vote converged, the three post-menu
            # Serial_ExchangeBytes blocks would show up as
            # exchange_bytes/* kinds. Record whether we got that
            # deep — not required, because sub-frame A-press timing
            # between independent threads is racey.
            reached_post_menu = any(k.startswith("exchange_bytes/") for k in kinds_a)
            # Stash on the test for pytest-level reporting via -rP.
            sys.stderr.write(
                f"\n[past-LinkMenu {version_listen}↔{version_connect}] "
                f"nybble={kinds_a.count(nybble_kind)}/{kinds_b.count(nybble_kind)} "
                f"menu_sel={kinds_a.count(menu_kind)}/{kinds_b.count(menu_kind)} "
                f"exchange_bytes_seen={reached_post_menu}\n"
            )
        finally:
            link_a.close()
            link_b.close()
    finally:
        session_a.close()
        session_b.close()


# --- menu-vote convergence: drive Serial_ExchangeBytes over TCP ---------
#
# The past-LinkMenu test above proves menu_selection/* RPC flows, but
# the vote rarely converges across two independent daemon threads
