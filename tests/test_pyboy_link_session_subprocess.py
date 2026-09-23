"""Two-subprocess PyBoy trade and battle over TCP (real-ROM integration).

Split from ``tests/test_pyboy_link_session_subprocess.py`` for #131 with no behavior
change: every assertion and test ID is preserved verbatim. These cases spawn two
``tests._tcp_trade_peer`` children against real ROMs and are skipped unless the
canonical fixtures are present.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from tests._battle_turn_evidence import verify_battle_turns
from tests._pyboy_link_session_subprocess_support import (
    _assert_complete_peer_result,
    _assert_peer_success,
    _assert_strict_peer_result,
    _collect_pair,
    _fixtures_ready,
    _free_port,
    _noncython_python,
    _spawn_peer,
)
from tests._rom_assets import fixture_path, rom_path, sym_path

_REMOTE_STRICT_PROFILE_CASES = (
    pytest.param(
        "red_color",
        "red_color",
        id="red_color-listen-red_color-connect",
    ),
    pytest.param(
        "red_color",
        "blue_color",
        id="red_color-listen-blue_color-connect",
    ),
    pytest.param(
        "blue_color",
        "red_color",
        id="blue_color-listen-red_color-connect",
    ),
    pytest.param(
        "red_color",
        "yellow",
        id="red_color-listen-yellow-connect",
    ),
    pytest.param(
        "blue_color",
        "blue_color",
        id="blue_color-listen-blue_color-connect",
    ),
    pytest.param(
        "blue_color",
        "yellow",
        id="blue_color-listen-yellow-connect",
    ),
    pytest.param(
        "yellow",
        "red_color",
        id="yellow-listen-red_color-connect",
    ),
    pytest.param(
        "yellow",
        "blue_color",
        id="yellow-listen-blue_color-connect",
    ),
    pytest.param(
        "yellow",
        "yellow",
        id="yellow-listen-yellow-connect",
    ),
)


def _strict_fixture_path(version: str, *, battle: bool) -> Path:
    """Return the exact ignored fixture selected by a subprocess profile."""
    fixture_version = {
        "red_color": "red",
        "blue_color": "blue",
        "yellow": "yellow",
    }.get(version)
    if fixture_version is None:
        raise ValueError(f"unsupported strict subprocess profile: {version}")
    name = "cable_club-battle.state" if battle else "cable_club.state"
    return fixture_path(fixture_version, name)


def _strict_fixtures_ready(
    listener_version: str,
    connector_version: str,
    *,
    battle: bool,
) -> bool:
    """Check assets for one strict, profile-specific subprocess row."""
    required = []
    for version in (listener_version, connector_version):
        rom_version = {
            "red_color": "red",
            "blue_color": "blue",
            "yellow": "yellow",
        }[version]
        required.extend(
            (
                rom_path(rom_version, color=version.endswith("_color")),
                sym_path(rom_version),
                _strict_fixture_path(version, battle=battle),
            )
        )
    return all(path.is_file() for path in required)


def _non_cython_pyboy_available() -> bool:
    """Return whether the configured production subprocess interpreter exists."""
    return _noncython_python().is_file()


_REMOTE_SKIP_REASON = (
    "Needs Yellow ROM + cable_club.state fixture + a production Python "
    "interpreter. Set POKERED_PYTHON if needed. See "
    "tests/test_pyboy_link_session_roms.py for setup recipe."
)


_REMOTE_INTEGRATION = pytest.mark.skipif(
    not (_fixtures_ready() and _non_cython_pyboy_available()),
    reason=_REMOTE_SKIP_REASON,
)


@_REMOTE_INTEGRATION
def test_subprocess_pair_reaches_link_menu_over_tcp():
    """Two-subprocess version of the LinkMenu-over-TCP milestone.

    Scheduling is no longer constrained by a single Python interpreter's
    GIL — each PyBoy runs in its own process. LinkMenu is the milestone
    that proves preamble handshake + nibble-sync converge through
    NetworkBackend for real ROM traffic.
    """
    port = _free_port()
    deadline = 240.0
    pair_deadline = time.monotonic() + deadline + 60.0

    listener = _spawn_peer("listen", port, goal="link_menu", deadline_seconds=deadline)
    # Small delay to let listener bind before the connector tries.
    time.sleep(1.0)
    connector = _spawn_peer("connect", port, goal="link_menu", deadline_seconds=deadline)

    result_a, result_b = _collect_pair(listener, connector, deadline_at=pair_deadline)

    print("\nsubprocess TCP LinkMenu results:")
    print(f"  listener: {result_a}")
    print(f"  connector: {result_b}")
    _assert_peer_success(result_a, label="listener")
    _assert_peer_success(result_b, label="connector")
    _assert_complete_peer_result(
        result_a,
        label="listener",
        expected_role="listen",
        expected_version="yellow",
    )
    _assert_complete_peer_result(
        result_b,
        label="connector",
        expected_role="connect",
        expected_version="yellow",
    )

    assert result_a.get("LinkMenu", 0) > 0, f"listener never reached LinkMenu; {result_a}"
    assert result_b.get("LinkMenu", 0) > 0, f"connector never reached LinkMenu; {result_b}"


@pytest.mark.parametrize(
    ("listener_version", "connector_version"),
    _REMOTE_STRICT_PROFILE_CASES,
)
@_REMOTE_INTEGRATION
def test_subprocess_pair_completes_trade_over_tcp(listener_version: str, connector_version: str):
    """Two-subprocess canonical Red/Blue/Yellow trade with real party-record checks.

    End-to-end proof that the NetworkBackend transport carries a
    complete Pokemon trade between two independent PyBoy processes.
    The flow uses four OP_SYNC barriers to keep the subprocesses'
    game-state in step at phase boundaries:

      1. ``link_menu`` — both sides have reached LinkMenu and are
         about to vote Trade Center.
      2. ``warp`` — both sides have warped to map 0xEF (TRADE_CENTER)
         and are about to walk onto the hidden-event trigger tiles.
      3. ``select_mon`` — both sides' big trainer/party-data block
         exchange has completed and TradeCenter_SelectMon is up.
      4. ``post_trade`` — both sides' ``_AddEnemyMonToPlayerParty``
         has fired (i.e. received the peer's mon). Without this
         rendezvous, whichever side finished first would tear down
         its SerialCore and leave the peer's ``on_edge`` timing out
         on the few final bytes of the trade's mon-data exchange.

    Live PyBoy peers queue each inbound edge for their emulator owner.
    Completing a byte latches SB and raises the serial interrupt; normal
    CPU execution must still run the handler and consume the ROM mailbox.
    An owner waiting at a frame barrier can execute a recovery frame after
    byte completion or for an unarmed deferred edge. The response worker
    does not establish ROM-consumption ordering. Exact party-record checks
    therefore remain necessary even when both trade hooks have fired.
    """
    if not _strict_fixtures_ready(
        listener_version,
        connector_version,
        battle=False,
    ):
        pytest.skip(
            "canonical Red/Blue/Yellow Cable Club fixtures are required for "
            f"{listener_version}/{connector_version}"
        )

    port = _free_port()
    deadline = 720.0
    pair_deadline = time.monotonic() + deadline + 60.0

    listener = _spawn_peer(
        "listen",
        port,
        goal="trade",
        deadline_seconds=deadline,
        version=listener_version,
    )
    time.sleep(1.0)
    connector = _spawn_peer(
        "connect",
        port,
        goal="trade",
        deadline_seconds=deadline,
        version=connector_version,
    )

    result_a, result_b = _collect_pair(listener, connector, deadline_at=pair_deadline)

    print("\nsubprocess TCP full-trade results:")
    print(f"  listener: {result_a}")
    print(f"  connector: {result_b}")
    _assert_peer_success(result_a, label="listener")
    _assert_peer_success(result_b, label="connector")
    _assert_strict_peer_result(
        result_a,
        label="listener",
        goal="trade",
        expected_role="listen",
        expected_version=listener_version,
    )
    _assert_strict_peer_result(
        result_b,
        label="connector",
        goal="trade",
        expected_role="connect",
        expected_version=connector_version,
    )
    assert result_a.get("_AddEnemyMonToPlayerParty", 0) > 0, f"listener never traded; {result_a}"
    assert result_b.get("_AddEnemyMonToPlayerParty", 0) > 0, f"connector never traded; {result_b}"
    for result in (result_a, result_b):
        backend_stats = result.get("_backend_stats", {})
        assert (
            backend_stats.get("edge_req_sent", 0) + backend_stats.get("edge_req_received", 0) > 0
        ), f"remote trade used no native serial edges; {result}"
        assert backend_stats.get("exchange_sent", 0) == 0, (
            f"remote trade used an out-of-band exchange; {result}"
        )
        assert backend_stats.get("exchange_received", 0) == 0, (
            f"remote trade used an out-of-band exchange; {result}"
        )
        assert backend_stats.get("pending_edge_requests", 0) == 0, (
            f"remote trade left admitted EDGE_REQ work pending at shutdown; {result}"
        )
    before_a = result_a.get("party_before", {})
    before_b = result_b.get("party_before", {})
    after_a = result_a.get("party_after", {})
    after_b = result_b.get("party_after", {})
    lead_a = before_a.get("mon_species", [None])[0]
    lead_b = before_b.get("mon_species", [None])[0]
    record_a = before_a.get("mon_records", [None])[0]
    record_b = before_b.get("mon_records", [None])[0]
    assert lead_a is not None and lead_b is not None, (
        f"trade fixtures must contain a lead; A={before_a} B={before_b}"
    )
    assert record_a is not None and record_b is not None, (
        f"trade fixtures must contain lead records; A={before_a} B={before_b}"
    )
    assert after_a.get("count") == before_a.get("count"), (
        f"listener party count changed unexpectedly; before={before_a} after={after_a}"
    )
    assert after_b.get("count") == before_b.get("count"), (
        f"connector party count changed unexpectedly; before={before_b} after={after_b}"
    )
    assert after_a.get("mon_records", [None])[0] == record_b, (
        f"listener did not receive connector's lead record; before={before_a} after={after_a}"
    )
    assert after_a.get("species", [None])[0] == lead_b, (
        f"listener mon record is inconsistent; after={after_a}"
    )
    assert after_a.get("mon_species", [None])[0] == lead_b, (
        f"listener mon species is inconsistent; after={after_a}"
    )
    assert after_b.get("mon_records", [None])[0] == record_a, (
        f"connector did not receive listener's lead record; before={before_b} after={after_b}"
    )
    assert after_b.get("species", [None])[0] == lead_a, (
        f"connector mon record is inconsistent; after={after_b}"
    )
    assert after_b.get("mon_species", [None])[0] == lead_a, (
        f"connector mon species is inconsistent; after={after_b}"
    )


@pytest.mark.parametrize(
    ("listener_version", "connector_version"),
    _REMOTE_STRICT_PROFILE_CASES,
)
@_REMOTE_INTEGRATION
def test_subprocess_pair_resolves_battle_turn_over_tcp(
    listener_version: str, connector_version: str
):
    """Strict canonical Red/Blue/Yellow remote battle acceptance with native serial traffic.

    The peer processes load legal, ROM-matched battle fixtures and drive the
    real Cable Club battle path using ordinary directional/A input to select
    Battle in LinkMenu. No semantic byte/nibble exchange or test-only game
    state bypass is installed; all exchange traffic must pass through
    NetworkBackend's native bit-level serial transport.
    """
    if not _strict_fixtures_ready(
        listener_version,
        connector_version,
        battle=True,
    ):
        pytest.skip(
            "canonical Red/Blue/Yellow battle fixtures are required for "
            f"{listener_version}/{connector_version}"
        )

    port = _free_port()
    deadline = 900.0
    pair_deadline = time.monotonic() + deadline + 60.0

    listener = _spawn_peer(
        "listen",
        port,
        goal="battle",
        deadline_seconds=deadline,
        version=listener_version,
    )
    time.sleep(1.0)
    connector = _spawn_peer(
        "connect",
        port,
        goal="battle",
        deadline_seconds=deadline,
        version=connector_version,
    )

    result_a, result_b = _collect_pair(listener, connector, deadline_at=pair_deadline)

    print("\nsubprocess TCP battle-turn results:")
    print(f"  listener: {result_a}")
    print(f"  connector: {result_b}")
    _assert_peer_success(result_a, label="listener")
    _assert_peer_success(result_b, label="connector")
    _assert_strict_peer_result(
        result_a,
        label="listener",
        goal="battle",
        expected_role="listen",
        expected_version=listener_version,
    )
    _assert_strict_peer_result(
        result_b,
        label="connector",
        goal="battle",
        expected_role="connect",
        expected_version=connector_version,
    )
    evidence_errors = verify_battle_turns([result_a, result_b])
    assert not evidence_errors, (
        "remote battle peers disagreed on settled application evidence: "
        f"{evidence_errors}; listener={result_a}; connector={result_b}"
    )

    required_hooks = (
        "DisplayLinkBattleVersusTextBox",
        "MoveSelectionMenu",
        "LinkBattleExchangeData",
    )
    for result in (result_a, result_b):
        for symbol in required_hooks:
            assert result.get(symbol, 0) > 0, f"{symbol} did not fire in remote battle; {result}"
        backend_stats = result.get("_backend_stats", {})
        assert (
            backend_stats.get("edge_req_sent", 0) + backend_stats.get("edge_req_received", 0) > 0
        ), f"remote battle used no native serial edges; {result}"
        assert backend_stats.get("exchange_sent", 0) == 0, (
            f"remote battle used an out-of-band exchange; {result}"
        )
        assert backend_stats.get("exchange_received", 0) == 0, (
            f"remote battle used an out-of-band exchange; {result}"
        )

    assert result_a.get("ExecutePlayerMove", 0) + result_a.get("ExecuteEnemyMove", 0) > 0, (
        f"listener battle turn did not advance; {result_a}"
    )
    assert result_b.get("ExecutePlayerMove", 0) + result_b.get("ExecuteEnemyMove", 0) > 0, (
        f"connector battle turn did not advance; {result_b}"
    )
