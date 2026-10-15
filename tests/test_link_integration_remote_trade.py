"""Remote trade-center and agent-sync regressions (real-ROM gated).

Split from ``tests/test_link_integration_remote.py`` for #137 with no
behavior change: every assertion and test ID is preserved verbatim.
"""

from __future__ import annotations

import sys
import threading
import time

import pytest

from pokered_harness.link import AgentSync
from pokered_harness.link.remote import STATUS_EXTERNAL, STATUS_INTERNAL, RemoteLinkEndpoint
from pokered_harness.session import Session
from tests._link_integration_remote_support import (
    _FIXTURES_WITH_WALKABLE_PLAYER,
    _cable_club_state,
    _drive_remote_to_link_menu,
    _drive_remote_trade_to_rng,
    _ensure_fixture_is_walkable,
    _install_autoselect_trade_hook,
    _install_hook_counter,
    _open_session,
    _RemoteTradeInputSchedule,
    _roms_present,
    _SessionRunner,
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
def test_remote_trade_reaches_link_menu_via_tcp(version_listen: str, version_connect: str) -> None:
    """Full protocol drive over TCP up to the LinkMenu.

    Two sessions at the Cable Club attendant on opposite ends of a
    real localhost TCP SerialLink. Presses A/UP until both sides reach
    LinkMenu (the battle/trade/cancel picker). Reaching LinkMenu on
    *both* sides proves, in order:

    1. The handshake hook flipped the status byte on both sides.
    2. The attendant dialog advanced (SaveGameData fired).
    3. ``Serial_SyncAndExchangeNybble`` converged over TCP — this is
       the first real byte exchange in the trade/battle flow, and the
       game gates entry to the LinkMenu on the nybble sync completing.

    This is the two-process equivalent of the SaveGameData + LinkMenu
    milestones in
    :func:`test_link_integration.test_link_trade_roundtrip`. It stops
    at LinkMenu — past that, menu-selection is agent policy.

    Parametrized over the full listener × connector matrix; a row skips only
    when its pinned ROM, symbols, or Cable Club state is unavailable or does
    not load at the expected position.
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
        pytest.skip(
            f"Fixture walkability gap: {version_listen}/{version_connect} "
            f"cannot walk to Cable Club attendant. See the "
            f"_FIXTURES_WITH_WALKABLE_PLAYER comment for details."
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
            # Milestone hooks on both sides. Index 0 = listener/primary,
            # index 1 = connector/peer.
            save_game = [0, 0]
            link_menu = [0, 0]
            for idx, sess in enumerate((session_a, session_b)):
                _install_hook_counter(sess, "SaveGameData", save_game, idx)
                _install_hook_counter(sess, "LinkMenu", link_menu, idx)

            status_addr = session_a.symbols.addr_of("hSerialConnectionStatus")

            # Each endpoint owns an independent session/thread, as it does
            # in two separate MCP processes. The driver waits on emulator
            # frame progress and applies one input only after the previous
            # command has landed.
            runner_a, runner_b = _start_remote_runners(session_a, endpoint_a, session_b, endpoint_b)
            try:
                _drive_remote_to_link_menu(
                    runner_a,
                    runner_b,
                    lambda: link_menu[0] > 0 and link_menu[1] > 0,
                )
            finally:
                _stop_remote_runners(runner_a, runner_b)

            assert runner_a.exc is None, runner_a.exc
            assert runner_b.exc is None, runner_b.exc

            status_a = session_a._pyboy.memory[status_addr]
            status_b = session_b._pyboy.memory[status_addr]
            assert status_a == STATUS_INTERNAL, f"primary status=0x{status_a:02x}"
            assert status_b == STATUS_EXTERNAL, f"peer status=0x{status_b:02x}"
            assert save_game[0] > 0 and save_game[1] > 0, (
                f"SaveGameData never fired — attendant dialog stalled; save_game={save_game}"
            )
            assert link_menu[0] > 0 and link_menu[1] > 0, (
                f"LinkMenu never reached — Serial_SyncAndExchangeNybble "
                f"did not converge over TCP "
                f"({version_listen}↔{version_connect}); link_menu={link_menu}"
            )
        finally:
            link_a.close()
            link_b.close()
    finally:
        session_a.close()
        session_b.close()


# --- observe Serial_ExchangeLinkMenuSelection + Serial_ExchangeBytes ---
#
# Driving the LinkMenu past its A-press with the remote endpoint turns
# out to expose a PyBoy 2.7 limitation: `hook_register` at an address
# where a hook already exists raises ValueError silently-eaten by our
# test helpers. Since RemoteLinkEndpoint.install() registers on the
# Serial_* labels first, a naive counter hook on e.g.
# Serial_ExchangeLinkMenuSelection never lands and the signal looks
# like "0 calls" even when the RPC is flowing.
#
# Rather than fight that (the endpoint's single-hook-per-address
# registration is the right design), the test below observes the RPC
# layer directly by wrapping `link.exchange`. That way we see exactly
# which symbolic ``kind`` values crossed the TCP boundary — which is
# the end-to-end transport proof we actually care about.


def test_remote_exchange_bytes_fires_in_trade_center_blue_blue() -> None:
    """Observe the first post-menu data exchange over a real TCP pair.

    The test-only auto-select hook is a diagnostic shortcut, not natural
    gameplay qualification. Each session gets an independent owner-thread
    schedule with the original per-phase input caps, cadence, and 30s RPC
    timeout. Success requires both sides to be observed in Trade Center and
    to return a matching-length ``wSerialRandomNumberListBlock`` exchange.
    It does not establish completion of the later trade data exchanges.
    """
    if not _roms_present("blue"):
        pytest.skip("Blue ROM not present")
    state = _cable_club_state("blue")
    if not state.exists():
        pytest.skip(f"Blue Cable Club state missing: {state}")

    session_a = _open_session("blue")
    session_b = _open_session("blue")
    links = []
    runners = []
    input_lock = threading.Lock()
    stop_inputs = threading.Event()
    rng_done = {"listener": threading.Event(), "connector": threading.Event()}
    schedules = {}
    attempts = {"listener": [], "connector": []}
    returns = {"listener": [], "connector": []}
    rng_returns = {"listener": [], "connector": []}
    observed_maps = {}
    try:
        session_a.load_state(state.read_bytes())
        session_b.load_state(state.read_bytes())
        _ensure_fixture_is_walkable("blue", session_a)
        _ensure_fixture_is_walkable("blue", session_b)

        link_a, link_b, endpoint_a, endpoint_b = _tcp_pair(session_a, "blue", session_b, "blue")
        links = [link_a, link_b]
        try:
            schedules = {
                "listener": _RemoteTradeInputSchedule(
                    session_a, "listener", stop_inputs, input_lock, rng_done
                ),
                "connector": _RemoteTradeInputSchedule(
                    session_b, "connector", stop_inputs, input_lock, rng_done
                ),
            }
            rng_kind = "exchange_bytes/wSerialRandomNumberListBlock"

            def _wrap(role, inner):
                def exchange(kind, my_bytes, *, timeout_ms=30000):
                    attempts[role].append(kind)
                    response = inner(kind, my_bytes, timeout_ms=timeout_ms)
                    returns[role].append(kind)
                    if kind == rng_kind:
                        row = {
                            "send_length": len(my_bytes),
                            "receive_length": len(response),
                            "matched_length": len(my_bytes) == len(response),
                            "after_trade_center": schedules[role].trade_tick is not None,
                        }
                        rng_returns[role].append(row)
                        if row["matched_length"] and row["after_trade_center"]:
                            with input_lock:
                                rng_done[role].set()
                    return response

                return exchange

            link_a.exchange = _wrap("listener", link_a.exchange)  # type: ignore[method-assign]
            link_b.exchange = _wrap("connector", link_b.exchange)  # type: ignore[method-assign]
            _install_autoselect_trade_hook(session_a)
            _install_autoselect_trade_hook(session_b)
            runners = [
                _SessionRunner(session_a, endpoint_a, chunk=4),
                _SessionRunner(session_b, endpoint_b, chunk=4),
            ]
            _drive_remote_trade_to_rng(
                (runners[0], runners[1]),
                (schedules["listener"], schedules["connector"]),
                rng_done,
                stop_inputs,
                input_lock,
            )
        finally:
            with input_lock:
                stop_inputs.set()
            for runner in runners:
                runner.set_before_step(None)
                runner._stop.set()
            teardown_errors = []
            for link in links:
                try:
                    link.close()
                except Exception as exc:  # noqa: BLE001
                    teardown_errors.append(f"link close: {exc!r}")
            live = []
            for runner in runners:
                if runner._thread.ident is not None:
                    runner._thread.join(timeout=5.0)
                    if runner._thread.is_alive():
                        live.append(runner._thread.name)
                    if runner.exc is not None:
                        teardown_errors.append(f"{runner._thread.name}: {runner.exc!r}")
            if live:
                teardown_errors.append(f"owner runners still alive: {live}")
            else:
                observed_maps = {
                    "listener": session_a.read_game_state().overworld.map_id,
                    "connector": session_b.read_game_state().overworld.map_id,
                }
            if teardown_errors:
                raise RuntimeError("remote trade teardown failed: " + "; ".join(teardown_errors))
    finally:
        if not any(runner._thread.is_alive() for runner in runners):
            session_a.close()
            session_b.close()

    trade_center = 0xEF
    diag = (
        f"maps={observed_maps} trade_ticks="
        f"{{listener: {schedules['listener'].trade_tick}, "
        f"connector: {schedules['connector'].trade_tick}}} "
        f"rng_returns={rng_returns} attempts={attempts} returns={returns}"
    )
    assert all(schedule.trade_tick is not None for schedule in schedules.values()), (
        f"Trade Center was not observed independently on both sides; {diag}"
    )
    assert all(map_id == trade_center for map_id in observed_maps.values()), (
        f"both sessions did not end in Trade Center; {diag}"
    )
    for role in ("listener", "connector"):
        assert any(
            row["matched_length"] and row["after_trade_center"] for row in rng_returns[role]
        ), f"{role} did not return a matching RNG exchange after Trade Center; {diag}"
    sys.stderr.write(f"\n[trade-center exchange-bytes blue↔blue] {diag}\n")


# Investigation notes (continued): forced-cursor + AgentSync rendezvous
# — forcing wCurrentMenuItem = 1 on both sides before a coordinated
# A-press, expecting the natural menu exchange to converge on TRADE.
# Result: the menu did NOT converge. Diagnostics showed menu_selection
# RPC counts perfectly balanced at 5365/5365 on both sides — both
# sides were exchanging bytes FIFO-correctly — but neither side's
# vote ever agreed with the peer's. Forcing wCurrentMenuItem evidently
# doesn't persist through the menu's input handling, or the A-press
# didn't register within the menu's active frame window. Documented
# as another dead end.
#
# Summary of all attempted bypass strategies for full-trade completion:
#
#   | Approach                            | Result                            |
#   |-------------------------------------|-----------------------------------|
#   | auto-select-TRADE (T3)              | 2/1 exchanges; exchange #2 desyncs|
#   | frame-synced manual press           | Deadlock on menu vote mismatch    |
#   | hook rendezvous at CableClub entry  | Hook doesn't fire                 |
#   | force wLinkState = 5                | 0 exchanges (worse than baseline) |
#   | forced cursor + AgentSync press     | Menu never converges (5365 RPCs)  |
#
# The transport works end-to-end (T3 proves the first exchange_bytes
# round-trip); what stays unresolved is the cross-process game-state
# synchronization needed to complete all three post-menu exchanges.
# The README's "Deployment timing" section covers the two production
# paths (tick-broker collapse or agent-driven transport hijack).

# Investigation notes: forcing wLinkState = 5 (LINK_STATE_START_TRADE)
# on both sides after the TRADE_CENTER warp was attempted as a way to
# align divergent post-exchange branches in CableClub_DoBattleOrTradeAgain.
# Result: forcing the state STOPPED CableClub_DoBattleOrTradeAgain
# from running at all (0 exchanges vs. the 2/1 baseline). wLinkState
# evidently gates earlier in the code path than assumed, so writing
# it externally prevents the function from being reached. The natural
# wLinkState progression (whatever the auto-select bypass produces)
# is closer to "runnable" than any value we can force from outside.


# --- T4/T5: full trade and battle completion (KNOWN LIMITATION) ---
#
# Attempted but not reliably achievable two-process:
#
# - Frame-synchronized manual A-press (LockstepOrchestrator.press_both
#   + manual menu navigation): DEADLOCKS. Even with per-frame barrier
#   sync, the menu votes don't always match between the two sides
#   (sub-frame CPU state differs) and one side's
#   Serial_ExchangeLinkMenuSelection RPC blocks waiting for a matching
#   exchange that never comes.
#
# - Auto-select-TRADE hook (pre-plant 0xD4 in recv buffer): WORKS to
#   the first exchange only. Both sides warp to TRADE_CENTER and
#   CableClub_DoBattleOrTradeAgain fires, but the 428-byte
#   wSerialPlayerDataBlock exchange reliably desyncs between the two
#   daemon threads after the bypass leaves each side's wLinkState in
#   a subtly-different shape. Covered by T3 above.
#
# The in-process LinkPair avoids both failure modes because both
# Sessions are stepped by a single lockstep driver with zero timing
# variance — and test_link_integration.test_link_trade_roundtrip
# proves end-to-end trade UI reachability in that mode.
#
# The *uncoordinated* raw daemon drivers in this file are what stay
# limited: they step at their own pace and cannot align frame
# advancement, so they still deadlock or desync as described above.
# Option (a) — a shared tick broker — is now realized by the public
# MCP stdio path in tests/test_mcp_trade_records_rom.py, where the
# acceptance client drives ``link_step`` on two independent server
# processes in lockstep and completes the full paired record swap; the
# ``..._over_tcp`` rows do it with the two servers joined by the public
# ``link_listen``/``link_connect`` pair, so a two-process trade is no
# longer "first post-menu exchange" only. Trade completion over two
# autonomous, unsynchronized processes would still need option (b).


# --- T7+: AgentSync-coordinated trade setup -----------------------------
#
# The deployment-recommended pattern (T7 in the README): two
# independent agents exchange rendezvous messages over the SerialLink's
# agent_sync/* kind namespace to coordinate button timing.
#
# The test below runs two sessions as AUTONOMOUS agents (each in a
# separate thread stepping at its own pace — no lockstep orchestrator)
# and uses AgentSync to align A-press timing at the LinkMenu. This is
# closer to the real two-MCP-process deployment model than the
# orchestrator tests.


def test_remote_agent_sync_coordinates_link_menu_vote_blue_blue() -> None:
    """Two autonomous agents + AgentSync rendezvous → matched menu vote.

    Each "agent" runs its own session on its own thread with no shared
    clock. When each agent detects LinkMenu has been entered on its
    side (via a hook), it issues a rendezvous on
    ``agent_sync/about_to_press_a``. Both sides block in the
    rendezvous until the peer arrives. Then both press A
    simultaneously — wall-clock-synchronized, which is as close to
    frame-synced as two independent Python processes can get without
    a shared tick broker.

    Assertions: both sides complete the ``about_to_press_a`` rendezvous,
    receive the peer's offered tick, and issue a menu-selection RPC over
    TCP. This test does not prove that both sides reach the trade event or
    complete a post-menu ``exchange_bytes`` round-trip.

    This is the T7 deployment pattern in action — proof that two
    independent agents can complete a coordinated action over the
    existing SerialLink transport without a shared tick clock or a
    game-code bypass hook.
    """
    if not _roms_present("blue"):
        pytest.skip("Blue ROM not present")
    state = _cable_club_state("blue")
    if not state.exists():
        pytest.skip(f"Blue Cable Club state missing: {state}")

    session_a = _open_session("blue")
    session_b = _open_session("blue")
    try:
        session_a.load_state(state.read_bytes())
        session_b.load_state(state.read_bytes())
        _ensure_fixture_is_walkable("blue", session_a)
        _ensure_fixture_is_walkable("blue", session_b)

        link_a, link_b, endpoint_a, endpoint_b = _tcp_pair(session_a, "blue", session_b, "blue")
        try:
            # Observe the game-serial RPC stream.
            kinds_a: list[str] = []
            kinds_b: list[str] = []

            def _wrap(sink, inner):
                def exchange(kind, my_bytes, *, timeout_ms=30000):
                    sink.append(kind)
                    return inner(kind, my_bytes, timeout_ms=timeout_ms)

                return exchange

            link_a.exchange = _wrap(kinds_a, link_a.exchange)  # type: ignore[method-assign]
            link_b.exchange = _wrap(kinds_b, link_b.exchange)  # type: ignore[method-assign]

            # Per-side menu-input-ready detection. LinkMenu.waitForInputLoop
            # is the tight loop where the menu is actively reading
            # JoypadLowSensitivity each frame — hooking its first hit
            # tells each agent "my menu is ready for A-press NOW". That's
            # the precise rendezvous point we want.
            in_menu_a = threading.Event()
            in_menu_b = threading.Event()
            menu_loop_label = "LinkMenu.waitForInputLoop"
            for sess, ev in ((session_a, in_menu_a), (session_b, in_menu_b)):
                if menu_loop_label not in sess.symbols:
                    pytest.skip(f"{menu_loop_label} not in symbol table")
                bank, addr = sess.symbols.bank_addr(menu_loop_label)
                sess._pyboy.hook_register(bank, addr, lambda _c, _e=ev: _e.set(), None)

            sync_a = AgentSync(link_a)
            sync_b = AgentSync(link_b)

            # Per-side autonomous runner. Each agent:
            #   1. drives up + A until its LinkMenu event fires
            #   2. rendezvous("about_to_press_a", <my_tick>)
            #   3. presses A and steps enough for the exchange to flow
            def _agent(
                side: str,
                session: Session,
                endpoint: RemoteLinkEndpoint,
                menu_evt: threading.Event,
                sync: AgentSync,
                result: dict,
            ) -> None:
                try:
                    runner = _SessionRunner(session, endpoint)
                    runner.start()
                    try:
                        time.sleep(1.5)
                        for _ in range(3):
                            runner.press("up", duration=6)
                            time.sleep(0.4)
                        # Press A until LinkMenu entry hook fires.
                        # Very generous timeout — full-suite parallel
                        # load can cut PyBoy tick rate to ~1/4 normal.
                        deadline = time.time() + 90.0
                        while time.time() < deadline and not menu_evt.is_set():
                            runner.press("a", duration=4)
                            time.sleep(0.25)
                        if not menu_evt.is_set():
                            result["error"] = f"{side}: LinkMenu never reached"
                            return
                        # Rendezvous with peer — blocks until peer
                        # also reached its menu. Timeout generous
                        # enough to outlast peer's menu-reach deadline
                        # under heavy parallel-suite load.
                        offered_tick = session.current_tick()
                        tick_bytes = str(offered_tick).encode("ascii")
                        peer_tick_bytes = sync.rendezvous(
                            "about_to_press_a", tick_bytes, timeout_ms=120000
                        )
                        result["offered_tick"] = offered_tick
                        result["peer_tick"] = peer_tick_bytes.decode("ascii")
                        result["my_tick"] = session.current_tick()
                        # Coordinated action: burst of A-presses now.
                        # Both agents are wall-clock-synced post-rendezvous,
                        # so the first A on each side lands within a few
                        # ms of the peer's first A — close enough for the
                        # menu votes to match.
                        for _ in range(8):
                            runner.press("a", duration=4)
                            time.sleep(0.1)
                        # Give the game time to run the post-menu
                        # CableClub_DoBattleOrTradeAgain flow.
                        time.sleep(5.0)
                    finally:
                        runner.stop()
                    result["ok"] = True
                except Exception as exc:  # noqa: BLE001
                    result["error"] = f"{side}: {exc!r}"

            result_a: dict = {}
            result_b: dict = {}
            t_a = threading.Thread(
                target=_agent,
                args=("A", session_a, endpoint_a, in_menu_a, sync_a, result_a),
                daemon=True,
            )
            t_b = threading.Thread(
                target=_agent,
                args=("B", session_b, endpoint_b, in_menu_b, sync_b, result_b),
                daemon=True,
            )
            t_a.start()
            t_b.start()
            t_a.join(timeout=180.0)
            t_b.join(timeout=180.0)

            assert "error" not in result_a, result_a.get("error")
            assert "error" not in result_b, result_b.get("error")
            # Both agents completed the rendezvous successfully.
            assert "peer_tick" in result_a and "peer_tick" in result_b
            # Both sessions issued the menu-selection RPC over TCP.
            menu_kind = "menu_selection/wLinkMenuSelectionSendBuffer"
            diag = (
                f"result_a={result_a} result_b={result_b} "
                f"kinds_a_tail={kinds_a[-5:]} kinds_b_tail={kinds_b[-5:]}"
            )
            assert menu_kind in kinds_a and menu_kind in kinds_b, (
                f"menu RPC didn't flow on both sides: {diag}"
            )
            # Both agents received the tick value the peer offered before
            # entering rendezvous. The live clocks keep advancing while the
            # peer call returns, so compare against the offered value rather
            # than the post-rendezvous tick sample.
            assert result_a["peer_tick"] == str(result_b["offered_tick"])
            assert result_b["peer_tick"] == str(result_a["offered_tick"])
            map_a = session_a.read_game_state().overworld.map_id
            map_b = session_b.read_game_state().overworld.map_id
            sys.stderr.write(
                f"\n[agent-sync blue↔blue] map_a=0x{map_a:02x} "
                f"map_b=0x{map_b:02x} "
                f"result_a_tick={result_a.get('my_tick')} "
                f"result_b_tick={result_b.get('my_tick')}\n"
            )
            # NOTE: Reaching TRADE_CENTER / CableClub_DoBattleOrTrade
            # requires more than wall-clock-synchronized A-press.
            # Even with rendezvous, the menu-selection RPC FIFO drifts
            # because the two sides' game clocks advance at different
            # rates — side A might have issued 30 menu_selection RPCs
            # while side B issued 5, and FIFO pairing then matches
            # stale votes from earlier game-states. A complete fix
            # would need the agent sync to drive menu-selection
            # directly (agent votes bypass the game's exchange loop)
            # or a tick-broker that paces both sides. Documented in
            # the "Deployment timing" README section.
        finally:
            link_a.close()
            link_b.close()
    finally:
        session_a.close()
        session_b.close()


# --- T4+AgentSync attempted: hook-level rendezvous exploration ---------
#
# Investigation notes: installing an AgentSync.rendezvous hook at
# CableClub_DoBattleOrTrade entry (on top of the auto-select-TRADE
# hook that powers T3) was attempted to eliminate the game-clock
# drift between the two sides' CableClub_DoBattleOrTradeAgain runs.
# Empirically, the added hook caused the entry to not fire at all
# (cable_hits stayed 0 on both sides, no exchange_bytes RPCs
# flowed) — suggesting that the extra hook registration or its
# presence in the bank-01 hot path interferes with the sequence of
# post-menu code PyBoy 2.7 takes. Exact cause is unclear without
# deeper PyBoy-internals instrumentation.
#
# The combined T3 + rendezvous approach is therefore not a clean
# win in the current setup. What remains genuinely provable in the
# two-process model is what the tests above cover: the first
# post-menu exchange_bytes round-trips over TCP (T3) and the
# AgentSync rendezvous primitive itself works correctly on the
# transport. Completing all three post-menu exchanges reliably
# requires either the tick-broker collapse (effectively LinkPair)
# or agent-driven transport hijack (agents' AgentSync drives menu
# selection and CableClub data directly, bypassing the game's
# per-frame Serial_Exchange* loops).
