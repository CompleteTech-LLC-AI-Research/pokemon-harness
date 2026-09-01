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
import sys
import threading
import time
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
    """Return whether the canonical Red/Blue/Yellow trade fixtures exist."""
    return all(
        path.is_file()
        for version in ("red", "blue", "yellow")
        for path in (
            rom_path(version, color=version in ("red", "blue")),
            sym_path(version),
            fixture_path(version),
        )
    )


def _battle_fixtures_ready() -> bool:
    """Return whether the canonical Red/Blue/Yellow battle assets exist."""
    return all(
        path.is_file()
        for version in ("red", "blue", "yellow")
        for path in (
            rom_path(version, color=version in ("red", "blue")),
            sym_path(version),
            fixture_path(version, "cable_club-battle.state"),
        )
    )


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


_REMOTE_SKIP_REASON = (
    "Needs Yellow ROM + cable_club.state fixture + a production Python "
    "interpreter. Set POKERED_PYTHON if needed. See "
    "tests/test_pyboy_link_session_roms.py for setup recipe."
)

_REMOTE_INTEGRATION = pytest.mark.skipif(
    not (_fixtures_ready() and _non_cython_pyboy_available()),
    reason=_REMOTE_SKIP_REASON,
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


class _PairDeadlineExceeded(RuntimeError):
    """The supervisor reached the pair's hard wall-clock deadline."""


def _drain_stream(stream, chunks: list[str]) -> None:
    """Drain one child pipe without making the supervisor wait on it.

    A daemon thread is used instead of ``communicate()`` futures so the
    parent can keep one authoritative deadline for both processes.  The
    reader only owns its stream; the supervisor never joins it after the
    hard cutoff.
    """
    try:
        while True:
            chunk = stream.read(8192)
            if not chunk:
                return
            chunks.append(chunk)
    except (OSError, ValueError):
        # The supervisor may close a pipe immediately after killing a child.
        return
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _start_pipe_drainers(proc):
    captured: dict[str, list[str]] = {"stdout": [], "stderr": []}
    readers: list[threading.Thread] = []
    for name in ("stdout", "stderr"):
        stream = getattr(proc, name)
        reader = threading.Thread(
            target=_drain_stream,
            args=(stream, captured[name]),
            name=f"tcp-peer-{name}-drainer",
            daemon=True,
        )
        reader.start()
        readers.append(reader)
    return captured, readers


def _captured_text(captured: dict[str, list[str]], name: str) -> str:
    return "".join(captured[name])


def _print_peer_trace(stderr: str, *, label: str) -> None:
    """Print the useful tail of a peer trace without hiding raw failures."""
    trace = [
        line
        for line in stderr.splitlines()
        if "[peer" in line or "EXCEPTION" in line or "sync:" in line
    ]
    if trace:
        print(f"\n[{label}] peer trace:")
        for line in trace[-40:]:
            print(f"  {line}")


def _parse_result(proc, captured: dict[str, list[str]], *, label: str) -> dict:
    stdout = _captured_text(captured, "stdout")
    stderr = _captured_text(captured, "stderr")
    _print_peer_trace(stderr, label=label)
    for line in stdout.splitlines():
        if line.startswith("__TCP_TRADE_RESULT__ "):
            result = json.loads(line[len("__TCP_TRADE_RESULT__ "):])
            if not isinstance(result, dict):
                pytest.fail(
                    f"peer subprocess emitted a non-object result; "
                    f"type={type(result).__name__}"
                )
            # Keep the child-owned status fields separate from the parent
            # observation of its process exit code.
            result["_supervisor_returncode"] = proc.returncode
            return result
    pytest.fail(
        f"peer subprocess exited without emitting __TCP_TRADE_RESULT__; "
        f"rc={proc.returncode}, "
        f"stdout tail:\n{stdout[-2000:]}\n"
        f"stderr tail:\n{stderr[-2000:]}"
    )


def _assert_peer_success(result: dict, *, label: str) -> None:
    """Reject partial or failed child sentinels before gameplay assertions."""
    status = result.get("_drive_status")
    error = result.get("_drive_error")
    deadline_exceeded = result.get("_deadline_exceeded")
    returncode = result.get("_supervisor_returncode")
    if (
        status != "ok"
        or error is not None
        or deadline_exceeded is not False
        or returncode != 0
    ):
        raise AssertionError(
            f"{label} peer did not complete successfully: "
            f"status={status!r} error={error!r} "
            f"deadline_exceeded={deadline_exceeded!r} "
            f"returncode={returncode!r}; result={result}"
        )


def _kill_without_waiting(procs) -> None:
    """Kill and reap only when the OS reports immediate completion."""
    for proc in procs:
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        # ``wait(timeout=0)`` is a non-blocking reap.  Never wait for a
        # blocked PyBoy tick after the pair deadline has expired.
        try:
            proc.wait(timeout=0)
        except (subprocess.TimeoutExpired, OSError):
            pass


def _deadline_error(
    procs, captures: tuple[dict[str, list[str]], dict[str, list[str]]], deadline_at: float
) -> _PairDeadlineExceeded:
    tails = []
    for label, proc, captured in zip(
        ("listener", "connector"), procs, captures, strict=True
    ):
        stdout = _captured_text(captured, "stdout")
        stderr = _captured_text(captured, "stderr")
        _print_peer_trace(stderr, label=label)
        tails.append(
            f"{label} rc={proc.returncode}\n"
            f"stdout tail:\n{stdout[-2000:]}\n"
            f"stderr tail:\n{stderr[-2000:]}"
        )
    return _PairDeadlineExceeded(
        f"peer subprocess pair exceeded hard deadline at {deadline_at:.6f};\n"
        + "\n".join(tails)
    )


def _collect_pair(listener, connector, *, deadline_at: float) -> tuple[dict, dict]:
    """Collect both peers under one absolute, non-extendable deadline.

    The pipe drainers prevent a verbose PyBoy child from blocking on a full
    stdout/stderr pipe.  The supervisor polls both processes and the drainer
    state from one monotonic cutoff.  If that cutoff expires, both processes
    are killed immediately; there is deliberately no executor context,
    ``communicate()`` call, or thread join that can extend the bound.
    """
    procs = (listener, connector)
    captures = []
    readers = []
    for proc in procs:
        captured, proc_readers = _start_pipe_drainers(proc)
        captures.append(captured)
        readers.extend(proc_readers)

    while True:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            _kill_without_waiting(procs)
            raise _deadline_error(procs, tuple(captures), deadline_at)
        if all(proc.poll() is not None for proc in procs) and all(
            not reader.is_alive() for reader in readers
        ):
            break
        time.sleep(min(0.01, remaining))

    return (
        _parse_result(listener, captures[0], label="listener"),
        _parse_result(connector, captures[1], label="connector"),
    )


def test_collect_pair_enforces_hard_deadline_without_waiting_for_peers():
    """A blocked child cannot extend the supervisor's absolute deadline."""
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    listener = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    connector = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    started = time.monotonic()
    try:
        with pytest.raises(_PairDeadlineExceeded, match="hard deadline"):
            _collect_pair(
                listener,
                connector,
                deadline_at=started + 0.25,
            )
        elapsed = time.monotonic() - started
        assert elapsed < 2.0
    finally:
        _kill_without_waiting((listener, connector))
        listener.wait(timeout=2.0)
        connector.wait(timeout=2.0)
    assert listener.returncode is not None
    assert connector.returncode is not None


def test_partial_peer_sentinel_is_fatal_before_gameplay_assertions():
    """A deadline/error sentinel cannot pass on counters alone."""
    partial = {
        "LinkMenu": 1,
        "_drive_status": "deadline",
        "_drive_error": "trade did not complete before deadline",
        "_deadline_exceeded": True,
        "_supervisor_returncode": 1,
    }
    with pytest.raises(AssertionError, match="did not complete successfully"):
        _assert_peer_success(partial, label="listener")


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

    result_a, result_b = _collect_pair(
        listener, connector, deadline_at=pair_deadline
    )

    print("\nsubprocess TCP LinkMenu results:")
    print(f"  listener: {result_a}")
    print(f"  connector: {result_b}")
    _assert_peer_success(result_a, label="listener")
    _assert_peer_success(result_b, label="connector")

    assert result_a.get("LinkMenu", 0) > 0, (
        f"listener never reached LinkMenu; {result_a}"
    )
    assert result_b.get("LinkMenu", 0) > 0, (
        f"connector never reached LinkMenu; {result_b}"
    )


@pytest.mark.parametrize(
    ("listener_version", "connector_version"),
    _REMOTE_STRICT_PROFILE_CASES,
)
@_REMOTE_INTEGRATION
def test_subprocess_pair_completes_trade_over_tcp(
    listener_version: str, connector_version: str
):
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

    NetworkBackend's ``_handle_edge_req`` carries the per-edge work:
    when the local SerialCore is armed as slave, apply the peer's
    bit and reply; when it's transiently unarmed (mid-IRQ re-arm
    window), briefly poll for re-arm before falling back to the
    SERIAL_NO_DATA_BYTE (0xFE) keep-alive stream. That wait-for-
    rearm is what lets each side's ROM finish the full fixed-length
    Serial_ExchangeBytes loops without desyncing on the missing
    bytes the peer skipped over while busy elsewhere.
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

    result_a, result_b = _collect_pair(
        listener, connector, deadline_at=pair_deadline
    )

    print("\nsubprocess TCP full-trade results:")
    print(f"  listener: {result_a}")
    print(f"  connector: {result_b}")
    _assert_peer_success(result_a, label="listener")
    _assert_peer_success(result_b, label="connector")

    assert result_a.get("_AddEnemyMonToPlayerParty", 0) > 0, (
        f"listener never traded; {result_a}"
    )
    assert result_b.get("_AddEnemyMonToPlayerParty", 0) > 0, (
        f"connector never traded; {result_b}"
    )
    for result in (result_a, result_b):
        backend_stats = result.get("_backend_stats", {})
        assert backend_stats.get("edge_req_sent", 0) + backend_stats.get(
            "edge_req_received", 0
        ) > 0, f"remote trade used no native serial edges; {result}"
        assert backend_stats.get("exchange_sent", 0) == 0, (
            f"remote trade used an out-of-band exchange; {result}"
        )
        assert backend_stats.get("exchange_received", 0) == 0, (
            f"remote trade used an out-of-band exchange; {result}"
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
        "trade fixtures must contain lead records; "
        f"A={before_a} B={before_b}"
    )
    assert after_a.get("count") == before_a.get("count"), (
        f"listener party count changed unexpectedly; before={before_a} "
        f"after={after_a}"
    )
    assert after_b.get("count") == before_b.get("count"), (
        f"connector party count changed unexpectedly; before={before_b} "
        f"after={after_b}"
    )
    assert after_a.get("mon_records", [None])[0] == record_b, (
        f"listener did not receive connector's lead record; before={before_a} "
        f"after={after_a}"
    )
    assert after_a.get("species", [None])[0] == lead_b, (
        f"listener mon record is inconsistent; after={after_a}"
    )
    assert after_a.get("mon_species", [None])[0] == lead_b, (
        f"listener mon species is inconsistent; after={after_a}"
    )
    assert after_b.get("mon_records", [None])[0] == record_a, (
        f"connector did not receive listener's lead record; before={before_b} "
        f"after={after_b}"
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

    result_a, result_b = _collect_pair(
        listener, connector, deadline_at=pair_deadline
    )

    print("\nsubprocess TCP battle-turn results:")
    print(f"  listener: {result_a}")
    print(f"  connector: {result_b}")
    _assert_peer_success(result_a, label="listener")
    _assert_peer_success(result_b, label="connector")

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
