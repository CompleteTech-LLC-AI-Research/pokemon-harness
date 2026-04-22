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


def test_subprocess_pair_completes_trade_over_tcp():
    """Two-subprocess full trade: both sides run
    ``_AddEnemyMonToPlayerParty``.

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
    port = _free_port()
    deadline = 720.0

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
