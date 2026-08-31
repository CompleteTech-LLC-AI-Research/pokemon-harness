"""Two-subprocess PyBoy trade and battle over TCP.

Spawns two child Python processes, each running
:mod:`tests._tcp_trade_peer` against its own PyBoy ROM. One child
is the listener, the other connects. Each child drives its side of
the trade flow and prints a JSON result. The parent (this test)
parses both results and asserts the agreed-upon milestone hooks
fired on both sides.

Why subprocesses
----------------

Two PyBoy instances running as Python threads share a single GIL
and OS scheduling tends to give one disproportionate CPU time,
causing one side's game code to race ahead of the peer's. Moving
each PyBoy into its own Python process gives them independent
GILs + real OS scheduling, which is enough to keep the TCP
NetworkBackend's REQ/RESP chatter in balance.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests._rom_assets import fixture_path, rom_path, sym_path

_REPO = Path(__file__).resolve().parents[1]


def _fixtures_ready() -> bool:
    rom = rom_path("yellow")
    sym = sym_path("yellow")
    state = fixture_path("yellow")
    return rom.is_file() and sym.is_file() and state.is_file()


def _trade_fixtures_ready() -> bool:
    """Return whether the strict Red/Blue subprocess fixtures exist."""
    return all(
        path.is_file()
        for path in (
            rom_path("red", color=True),
            sym_path("red"),
            fixture_path("red"),
            rom_path("blue", color=True),
            sym_path("blue"),
            fixture_path("blue"),
        )
    )


def _battle_fixtures_ready() -> bool:
    """Return whether the strict Red/Blue subprocess battle assets exist."""
    return all(
        path.is_file()
        for path in (
            rom_path("red", color=True),
            sym_path("red"),
            fixture_path("red").parent / "cable_club-battle.state",
            rom_path("blue", color=True),
            sym_path("blue"),
            fixture_path("blue").parent / "cable_club-battle.state",
        )
    )


def _non_cython_pyboy_available() -> bool:
    """Return whether the configured production subprocess interpreter exists."""
    return _noncython_python().is_file()


def _noncython_python() -> Path:
    configured = os.environ.get("POKERED_PYTHON")
    if configured:
        return Path(configured)
    candidates = (
        _REPO / ".venv-noncython" / "Scripts" / "python.exe",
        _REPO / ".venv-noncython" / "bin" / "python",
        _REPO / ".venv" / "Scripts" / "python.exe",
        _REPO / ".venv" / "bin" / "python",
    )
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[0])


pytestmark = pytest.mark.skipif(
    not (_fixtures_ready() and _non_cython_pyboy_available()),
    reason=(
        "Needs Yellow ROM + cable_club.state fixture + a production Python "
        "interpreter. Set POKERED_PYTHON if needed. See "
        "tests/test_pyboy_link_session_roms.py for setup recipe."
    ),
)


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    try:
        return s.getsockname()[1]
    finally:
        s.close()


def _spawn_peer(
    role: str,
    port: int,
    *,
    goal: str,
    deadline_seconds: float,
    version: str = "yellow",
):
    """Spawn one side as a subprocess. Returns the Popen handle."""
    script = _REPO / "tests" / "_tcp_trade_peer.py"
    cmd = [
        str(_noncython_python()),
        str(script),
        "--role", role,
        "--port", str(port),
        "--goal", goal,
        "--version", version,
        "--deadline-seconds", str(deadline_seconds),
        "--repo-root", str(_REPO),
    ]
    env = dict(os.environ)
    return subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )


def _collect_result(proc, timeout: float, *, label: str = "") -> dict:
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        pytest.fail(
            f"peer subprocess timed out after {timeout}s.\n"
            f"stdout:\n{stdout[-2000:]}\nstderr:\n{stderr[-2000:]}"
        )
    # Always print peer stderr trace for visibility. The peer script
    # logs progress to stderr; on test failure this shows where it
    # got stuck.
    if label and stderr:
        trace = [
            line for line in stderr.splitlines()
            if "[peer" in line or "EXCEPTION" in line or "sync:" in line
        ]
        if trace:
            print(f"\n[{label}] peer trace:")
            for line in trace[-40:]:
                print(f"  {line}")
    for line in stdout.splitlines():
        if line.startswith("__TCP_TRADE_RESULT__ "):
            return json.loads(line[len("__TCP_TRADE_RESULT__ "):])
    pytest.fail(
        f"peer subprocess exited without emitting __TCP_TRADE_RESULT__; "
        f"rc={proc.returncode}, "
        f"stdout tail:\n{stdout[-2000:]}\nstderr tail:\n{stderr[-2000:]}"
    )


def _collect_pair(listener, connector, *, timeout: float) -> tuple[dict, dict]:
    """Drain both child pipes concurrently.

    PyBoy emits a large amount of symbol-loader warning text. Waiting for
    the listener first lets the connector fill its stdout pipe before it can
    finish connecting, which deadlocks the acceptance test rather than the
    link implementation.
    """
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="tcp-peer") as pool:
        futures = (
            pool.submit(_collect_result, listener, timeout, label="listener"),
            pool.submit(_collect_result, connector, timeout, label="connector"),
        )
        try:
            return futures[0].result(), futures[1].result()
        except BaseException:
            # Do not leave the surviving peer emulating until its full
            # deadline after the other peer has already failed. This keeps a
            # broken remote test bounded and avoids a second misleading
            # timeout from the peer whose socket was just abandoned.
            for proc in (listener, connector):
                if proc.poll() is None:
                    proc.kill()
            raise


def test_subprocess_pair_reaches_link_menu_over_tcp():
    """Two-subprocess version of the LinkMenu-over-TCP milestone.

    Scheduling is no longer constrained by a single Python interpreter's
    GIL — each PyBoy runs in its own process. LinkMenu is the milestone
    that proves preamble handshake + nibble-sync converge through
    NetworkBackend for real ROM traffic.
    """
    port = _free_port()
    deadline = 240.0

    listener = _spawn_peer("listen", port, goal="link_menu", deadline_seconds=deadline)
    # Small delay to let listener bind before the connector tries.
    time.sleep(1.0)
    connector = _spawn_peer("connect", port, goal="link_menu", deadline_seconds=deadline)

    result_a, result_b = _collect_pair(
        listener, connector, timeout=deadline + 60.0
    )

    print("\nsubprocess TCP LinkMenu results:")
    print(f"  listener: {result_a}")
    print(f"  connector: {result_b}")

    assert result_a.get("LinkMenu", 0) > 0, (
        f"listener never reached LinkMenu; {result_a}"
    )
    assert result_b.get("LinkMenu", 0) > 0, (
        f"connector never reached LinkMenu; {result_b}"
    )


def test_subprocess_pair_completes_trade_over_tcp():
    """Two-subprocess Red/Blue trade with real party-record checks.

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

    NetworkBackend's ``_handle_edge_req`` carries the per-edge work:
    when the local SerialCore is armed as slave, apply the peer's
    bit and reply; when it's transiently unarmed (mid-IRQ re-arm
    window), briefly poll for re-arm before falling back to the
    SERIAL_NO_DATA_BYTE (0xFE) keep-alive stream. That wait-for-
    rearm is what lets each side's ROM finish the full fixed-length
    Serial_ExchangeBytes loops without desyncing on the missing
    bytes the peer skipped over while busy elsewhere.
    """
    if not _trade_fixtures_ready():
        pytest.skip("Red and Blue color Cable Club fixtures are required")

    port = _free_port()
    deadline = 720.0

    listener = _spawn_peer(
        "listen",
        port,
        goal="trade",
        deadline_seconds=deadline,
        version="red_color",
    )
    time.sleep(1.0)
    connector = _spawn_peer(
        "connect",
        port,
        goal="trade",
        deadline_seconds=deadline,
        version="blue_color",
    )

    result_a, result_b = _collect_pair(
        listener, connector, timeout=deadline + 60.0
    )

    print("\nsubprocess TCP full-trade results:")
    print(f"  listener: {result_a}")
    print(f"  connector: {result_b}")

    assert result_a.get("_AddEnemyMonToPlayerParty", 0) > 0, (
        f"listener never traded; {result_a}"
    )
    assert result_b.get("_AddEnemyMonToPlayerParty", 0) > 0, (
        f"connector never traded; {result_b}"
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
    assert record_a is not None and record_b is not None and record_a != record_b, (
        "trade fixtures must contain distinguishable lead records; "
        f"A={before_a} B={before_b}"
    )
    assert after_a.get("mon_records", [None])[0] == record_b, (
        f"listener did not receive connector's lead record; before={before_a} "
        f"after={after_a}"
    )
    assert after_a.get("species", [None])[0] == lead_b, (
        f"listener mon record is inconsistent; after={after_a}"
    )
    assert after_b.get("mon_records", [None])[0] == record_a, (
        f"connector did not receive listener's lead record; before={before_b} "
        f"after={after_b}"
    )
    assert after_b.get("species", [None])[0] == lead_a, (
        f"connector mon record is inconsistent; after={after_b}"
    )


def test_subprocess_pair_resolves_battle_turn_over_tcp():
    """Strict Red/Blue remote battle acceptance with native serial traffic.

    The peer processes load legal, ROM-matched battle fixtures and drive the
    real Cable Club battle path using ordinary directional/A input to select
    Battle in LinkMenu. No semantic byte/nibble exchange or test-only game
    state bypass is installed; all exchange traffic must pass through
    NetworkBackend's native bit-level serial transport.
    """
    if not _battle_fixtures_ready():
        pytest.skip("Red and Blue color battle fixtures are required")

    port = _free_port()
    deadline = 900.0

    listener = _spawn_peer(
        "listen",
        port,
        goal="battle",
        deadline_seconds=deadline,
        version="red_color",
    )
    time.sleep(1.0)
    connector = _spawn_peer(
        "connect",
        port,
        goal="battle",
        deadline_seconds=deadline,
        version="blue_color",
    )

    result_a, result_b = _collect_pair(
        listener, connector, timeout=deadline + 60.0
    )

    print("\nsubprocess TCP battle-turn results:")
    print(f"  listener: {result_a}")
    print(f"  connector: {result_b}")

    required_hooks = (
        "DisplayLinkBattleVersusTextBox",
        "MoveSelectionMenu",
        "LinkBattleExchangeData",
    )
    for result in (result_a, result_b):
        for symbol in required_hooks:
            assert result.get(symbol, 0) > 0, (
                f"{symbol} did not fire in remote battle; {result}"
            )
        backend_stats = result.get("_backend_stats", {})
        assert backend_stats.get("edge_req_sent", 0) + backend_stats.get(
            "edge_req_received", 0
        ) > 0, f"remote battle used no native serial edges; {result}"
        assert backend_stats.get("exchange_sent", 0) == 0, (
            f"remote battle used an out-of-band exchange; {result}"
        )
        assert backend_stats.get("exchange_received", 0) == 0, (
            f"remote battle used an out-of-band exchange; {result}"
        )

    assert (
        result_a.get("ExecutePlayerMove", 0)
        + result_a.get("ExecuteEnemyMove", 0)
        > 0
    ), f"listener battle turn did not advance; {result_a}"
    assert (
        result_b.get("ExecutePlayerMove", 0)
        + result_b.get("ExecuteEnemyMove", 0)
        > 0
    ), f"connector battle turn did not advance; {result_b}"
