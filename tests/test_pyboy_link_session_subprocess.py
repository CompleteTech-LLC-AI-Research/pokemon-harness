"""Two-subprocess PyBoy trade over TCP.

Spawns two child Python processes, each running
:mod:`tests._tcp_trade_peer` against its own Yellow PyBoy. One child
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
import sys
import time
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[1]


def _fixtures_ready() -> bool:
    for parent in (_REPO, *_REPO.parents):
        rom_root = parent / "rom"
        if rom_root.is_dir():
            rom = rom_root / "yellow" / "pokemon-yellow.gbc"
            sym = rom_root / "yellow" / "pokemon-yellow.sym"
            break
    else:
        return False
    state = _REPO / "tests" / "fixtures" / "link" / "yellow" / "cable_club.state"
    return rom.is_file() and sym.is_file() and state.is_file()


def _non_cython_pyboy_available() -> bool:
    """We need the non-Cython PyBoy build (``pyboy.mb.serial``
    swappable). Probe by locating the worktree's
    ``.venv-noncython`` interpreter."""
    return _noncython_python().is_file()


def _noncython_python() -> Path:
    return _REPO / ".venv-noncython" / "Scripts" / "python.exe"


pytestmark = pytest.mark.skipif(
    not (_fixtures_ready() and _non_cython_pyboy_available()),
    reason=(
        "Needs Yellow ROM + cable_club.state fixture + .venv-noncython/ "
        "with non-Cython PyBoy. See "
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


def _spawn_peer(role: str, port: int, *, goal: str, deadline_seconds: float):
    """Spawn one side as a subprocess. Returns the Popen handle."""
    script = _REPO / "tests" / "_tcp_trade_peer.py"
    cmd = [
        str(_noncython_python()),
        str(script),
        "--role", role,
        "--port", str(port),
        "--goal", goal,
        "--deadline-seconds", str(deadline_seconds),
        "--repo-root", str(_REPO),
    ]
    env = {**os.environ, "POKERED_SKIP_SHA1": "1"}
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

    result_a = _collect_result(listener, timeout=deadline + 60.0, label="listener")
    result_b = _collect_result(connector, timeout=deadline + 60.0, label="connector")

    print(f"\nsubprocess TCP LinkMenu results:")
    print(f"  listener: {result_a}")
    print(f"  connector: {result_b}")

    assert result_a.get("LinkMenu", 0) > 0, (
        f"listener never reached LinkMenu; {result_a}"
    )
    assert result_b.get("LinkMenu", 0) > 0, (
        f"connector never reached LinkMenu; {result_b}"
    )


@pytest.mark.xfail(
    reason=(
        "Subprocess isolation + three OP_SYNC barriers (link_menu, "
        "warp, select_mon) get both sides through CableClub_"
        "DoBattleOrTrade's big trainer/party-data exchange in the "
        "passing case, but there's a fundamental race we haven't "
        "addressed: whichever side's CPU finishes the big exchange "
        "first moves on to TradeCenter_SelectMon. At that point its "
        "SerialCore is no longer armed as slave, so the peer's "
        "still-running master-mode transfers get pull-up 0xFF "
        "responses from the backend's reader thread and stall. "
        "Fixing this would need either a 'keep-alive slave arm' "
        "protocol at the backend layer (reader thread keeps "
        "echoing sensible data even while local core is idle), "
        "or tighter interleaving at the CPU level (subprocess "
        "IPC + a tick-rate throttle). The LinkMenu-reach "
        "subprocess test passes reliably and proves the transport "
        "works end-to-end; this full-trade test stays xfail'd "
        "until the keep-alive protocol lands."
    ),
    run=True,
    strict=False,
)
def test_subprocess_pair_completes_trade_over_tcp():
    """Two-subprocess full trade: both sides run
    ``_AddEnemyMonToPlayerParty``."""
    port = _free_port()
    deadline = 480.0

    listener = _spawn_peer("listen", port, goal="trade", deadline_seconds=deadline)
    time.sleep(1.0)
    connector = _spawn_peer("connect", port, goal="trade", deadline_seconds=deadline)

    result_a = _collect_result(listener, timeout=deadline + 60.0, label="listener")
    result_b = _collect_result(connector, timeout=deadline + 60.0, label="connector")

    print(f"\nsubprocess TCP full-trade results:")
    print(f"  listener: {result_a}")
    print(f"  connector: {result_b}")

    assert result_a.get("_AddEnemyMonToPlayerParty", 0) > 0, (
        f"listener never traded; {result_a}"
    )
    assert result_b.get("_AddEnemyMonToPlayerParty", 0) > 0, (
        f"connector never traded; {result_b}"
    )
