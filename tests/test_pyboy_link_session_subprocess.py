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

import io
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
from tests._tcp_trade_peer import _TRADE_DIAG_SYMBOLS, _hold_at_sync_boundary

_REPO = Path(__file__).resolve().parents[1]
_RESULT_PREFIX = "__TCP_TRADE_RESULT__ "
_SUPERVISOR_RESULT_SOURCE = "supervisor"
_MAX_FAILURE_SENTINEL_ERROR = 512
_REQUIRED_RESULT_FIELDS = (
    "_role",
    "_version",
    "party_before",
    "party_after",
    "_final_state",
    "_final_cpu",
    "_shots",
    "_backend_stats",
    "_drive_status",
    "_drive_error",
    "_deadline_exceeded",
    "_supervisor_returncode",
)
_REQUIRED_PARTY_FIELDS = ("count", "species", "mon_species", "mon_records")
_REQUIRED_BACKEND_STATS = (
    "edge_req_sent",
    "edge_req_received",
    "edge_resp_sent",
    "edge_resp_received",
    "exchange_sent",
    "exchange_received",
    "pending_edge_requests",
)


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
        "--role",
        role,
        "--port",
        str(port),
        "--goal",
        goal,
        "--version",
        version,
        "--deadline-seconds",
        str(deadline_seconds),
        "--repo-root",
        str(_REPO),
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


def _bounded_failure_error(reason: str) -> str:
    """Keep a supervisor-owned failure sentinel safe to print and retain."""
    compact = " ".join(reason.split())
    if len(compact) <= _MAX_FAILURE_SENTINEL_ERROR:
        return compact
    return compact[: _MAX_FAILURE_SENTINEL_ERROR - 3] + "..."


def _failure_sentinel(
    proc,
    captured: dict[str, list[str]],
    *,
    label: str,
    reason: str,
) -> dict:
    """Represent a child that failed before it could emit a peer result row.

    The peer's JSON sentinel is emitted after setup and the drive-loop
    cleanup. A loader or HELLO failure therefore has no child-owned row to
    parse. The supervisor records that bounded failure explicitly so strict
    acceptance can fail closed without waiting for the surviving peer's full
    gameplay deadline.
    """
    stderr = _captured_text(captured, "stderr")
    detail = " ".join(stderr.split())
    if detail:
        reason = f"{reason}: {detail[-256:]}"
    return {
        "_result_source": _SUPERVISOR_RESULT_SOURCE,
        "_failure_phase": "setup/handshake",
        "_role": label,
        "_version": None,
        "_backend_stats": {},
        "_drive_status": "error",
        "_drive_error": _bounded_failure_error(reason),
        "_deadline_exceeded": False,
        "_supervisor_returncode": proc.returncode,
    }


def _has_result_row(captured: dict[str, list[str]]) -> bool:
    return any(
        line.startswith(_RESULT_PREFIX) for line in _captured_text(captured, "stdout").splitlines()
    )


def _assert_complete_peer_result(
    result: dict,
    *,
    label: str,
    expected_role: str | None = None,
    expected_version: str | None = None,
) -> None:
    """Reject missing or partial rows before any strict gameplay claim."""
    if not isinstance(result, dict):
        raise TypeError(f"{label} peer result is not an object")
    if result.get("_result_source") == _SUPERVISOR_RESULT_SOURCE:
        raise AssertionError(f"{label} supervisor failure sentinel is not a result row")

    missing = [key for key in _REQUIRED_RESULT_FIELDS if key not in result]
    missing.extend(f"{symbol} counter" for symbol in _TRADE_DIAG_SYMBOLS if symbol not in result)
    for party_name in ("party_before", "party_after"):
        party = result.get(party_name)
        if isinstance(party, dict):
            missing.extend(
                f"{party_name}.{field}" for field in _REQUIRED_PARTY_FIELDS if field not in party
            )
        elif party_name not in missing:
            missing.append(party_name)
    backend_stats = result.get("_backend_stats")
    if isinstance(backend_stats, dict):
        missing.extend(
            f"_backend_stats.{field}"
            for field in _REQUIRED_BACKEND_STATS
            if field not in backend_stats
        )
    if missing:
        raise AssertionError(
            f"{label} peer result is missing required result rows: "
            f"{', '.join(sorted(set(missing)))}; result={result}"
        )

    if expected_role is not None and result["_role"] != expected_role:
        raise AssertionError(
            f"{label} peer result role mismatch: expected={expected_role!r} "
            f"actual={result['_role']!r}; result={result}"
        )
    if expected_version is not None and result["_version"] != expected_version:
        raise AssertionError(
            f"{label} peer result version mismatch: expected={expected_version!r} "
            f"actual={result['_version']!r}; result={result}"
        )

    if not isinstance(result["_role"], str) or not isinstance(result["_version"], str):
        raise TypeError(f"{label} peer result has invalid role/version rows; result={result}")
    if not isinstance(result["_drive_status"], str):
        raise TypeError(f"{label} peer result has an invalid drive-status row; result={result}")
    if result["_drive_error"] is not None and not isinstance(result["_drive_error"], str):
        raise TypeError(f"{label} peer result has an invalid drive-error row; result={result}")
    if not isinstance(result["_deadline_exceeded"], bool):
        raise TypeError(f"{label} peer result has an invalid deadline row; result={result}")
    if isinstance(result["_supervisor_returncode"], bool) or not isinstance(
        result["_supervisor_returncode"], int
    ):
        raise TypeError(f"{label} peer result has an invalid return-code row; result={result}")

    for symbol in _TRADE_DIAG_SYMBOLS:
        value = result[symbol]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AssertionError(
                f"{label} peer result has an invalid {symbol} counter; result={result}"
            )

    for party_name in ("party_before", "party_after"):
        party = result[party_name]
        if not isinstance(party, dict):
            raise TypeError(f"{label} peer result has an invalid {party_name} row; result={result}")
        count = party["count"]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise AssertionError(
                f"{label} peer result has an invalid {party_name}.count row; result={result}"
            )
        for field in ("species", "mon_species", "mon_records"):
            if not isinstance(party[field], list):
                raise TypeError(
                    f"{label} peer result has an invalid {party_name}.{field} row; result={result}"
                )
        if (
            len(party["species"]) != count + 1
            or len(party["mon_species"]) != count
            or len(party["mon_records"]) != count
        ):
            raise AssertionError(
                f"{label} peer result has an incomplete {party_name} row; result={result}"
            )

    if not isinstance(result["_final_state"], dict) or not isinstance(result["_final_cpu"], dict):
        raise TypeError(f"{label} peer result has incomplete final-state rows; result={result}")
    if not isinstance(result["_shots"], list):
        raise TypeError(f"{label} peer result has an invalid shots row; result={result}")

    if not isinstance(backend_stats, dict):
        raise TypeError(f"{label} peer result has an invalid backend row; result={result}")
    for field in _REQUIRED_BACKEND_STATS:
        value = backend_stats[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AssertionError(
                f"{label} peer result has an invalid _backend_stats.{field} row; result={result}"
            )


def _assert_native_edge_transport(result: dict, *, label: str) -> None:
    """Require completed, balanced native EDGE_REQ/EDGE_RESP traffic."""
    backend_stats = result.get("_backend_stats")
    if not isinstance(backend_stats, dict):
        raise TypeError(f"{label} peer result has no backend stats; result={result}")
    missing = [field for field in _REQUIRED_BACKEND_STATS if field not in backend_stats]
    if missing:
        raise AssertionError(
            f"{label} peer result has incomplete backend stats: {missing}; result={result}"
        )
    request_count = backend_stats["edge_req_sent"] + backend_stats["edge_req_received"]
    response_count = backend_stats["edge_resp_sent"] + backend_stats["edge_resp_received"]
    if request_count <= 0:
        raise AssertionError(f"{label} peer result has no native EDGE_REQ traffic; result={result}")
    if request_count != response_count:
        raise AssertionError(
            f"{label} peer result has unbalanced native EDGE_REQ/EDGE_RESP traffic; result={result}"
        )
    if backend_stats["pending_edge_requests"] != 0:
        raise AssertionError(
            f"{label} peer result left native EDGE_REQ work pending; result={result}"
        )
    if backend_stats["exchange_sent"] != 0 or backend_stats["exchange_received"] != 0:
        raise AssertionError(f"{label} peer result used an out-of-band exchange; result={result}")


def _assert_strict_peer_result(
    result: dict,
    *,
    label: str,
    goal: str,
    expected_role: str | None = None,
    expected_version: str | None = None,
) -> None:
    """Apply the shared strict contract before trade/battle-specific checks."""
    _assert_peer_success(result, label=label)
    _assert_complete_peer_result(
        result,
        label=label,
        expected_role=expected_role,
        expected_version=expected_version,
    )
    _assert_native_edge_transport(result, label=label)

    if goal == "trade":
        if result["_AddEnemyMonToPlayerParty"] <= 0:
            raise AssertionError(
                f"{label} peer did not complete trade; LinkMenu alone is insufficient; "
                f"result={result}"
            )
        return
    if goal == "battle":
        required_hooks = (
            "DisplayLinkBattleVersusTextBox",
            "MoveSelectionMenu",
            "LinkBattleExchangeData",
        )
        missing_hooks = [symbol for symbol in required_hooks if result[symbol] <= 0]
        if missing_hooks:
            raise AssertionError(
                f"{label} peer did not complete battle hooks {missing_hooks}; "
                f"LinkMenu alone is insufficient; result={result}"
            )
        if result["ExecutePlayerMove"] + result["ExecuteEnemyMove"] <= 0:
            raise AssertionError(f"{label} peer did not complete a battle turn; result={result}")
        return
    raise ValueError(f"unsupported strict peer goal: {goal}")


def _parse_result(proc, captured: dict[str, list[str]], *, label: str) -> dict:
    stdout = _captured_text(captured, "stdout")
    stderr = _captured_text(captured, "stderr")
    _print_peer_trace(stderr, label=label)
    for line in stdout.splitlines():
        if line.startswith(_RESULT_PREFIX):
            result = json.loads(line[len(_RESULT_PREFIX) :])
            if not isinstance(result, dict):
                pytest.fail(
                    f"peer subprocess emitted a non-object result; type={type(result).__name__}"
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
    if status != "ok" or error is not None or deadline_exceeded is not False or returncode != 0:
        raise AssertionError(
            f"{label} peer did not complete successfully: "
            f"status={status!r} error={error!r} "
            f"deadline_exceeded={deadline_exceeded!r} "
            f"returncode={returncode!r}; result={result}"
        )


def _complete_peer_result(*, role: str = "listen", version: str = "yellow") -> dict:
    """Build a complete peer row for deterministic acceptance-contract tests."""
    if role == "listen":
        backend_stats = {
            "edge_req_sent": 1,
            "edge_req_received": 0,
            "edge_resp_sent": 0,
            "edge_resp_received": 1,
        }
    else:
        backend_stats = {
            "edge_req_sent": 0,
            "edge_req_received": 1,
            "edge_resp_sent": 1,
            "edge_resp_received": 0,
        }
    backend_stats.update(
        {
            "exchange_sent": 0,
            "exchange_received": 0,
            "pending_edge_requests": 0,
        }
    )
    party = {
        "count": 1,
        "species": [1, 255],
        "mon_species": [1],
        "mon_records": ["00" * 44],
    }
    result = {symbol: 0 for symbol in _TRADE_DIAG_SYMBOLS}
    result.update(
        {
            "_role": role,
            "_version": version,
            "party_before": party.copy(),
            "party_after": party.copy(),
            "_final_state": {},
            "_final_cpu": {},
            "_shots": [],
            "_backend_stats": backend_stats,
            "_drive_status": "ok",
            "_drive_error": None,
            "_deadline_exceeded": False,
            "_supervisor_returncode": 0,
        }
    )
    return result


class _CompletedPeer:
    """Small completed-process double for result-row collection tests."""

    def __init__(self, result: dict):
        self.stdout = io.StringIO(f"{_RESULT_PREFIX}{json.dumps(result)}\n")
        self.stderr = io.StringIO("")
        self.returncode = 0

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        del timeout
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_setup_handshake_failure_returns_bounded_non_success_sentinels():
    """A pre-result child failure is fail-closed without waiting for its peer."""
    failure_command = [
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('setup-failure-' + 'x' * 4096); sys.exit(17)",
    ]
    blocked_command = [sys.executable, "-c", "import time; time.sleep(30)"]
    listener = subprocess.Popen(
        failure_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    connector = subprocess.Popen(
        blocked_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    started = time.monotonic()
    try:
        result_a, result_b = _collect_pair(
            listener,
            connector,
            deadline_at=started + 5.0,
        )
        assert time.monotonic() - started < 2.0
        for result in (result_a, result_b):
            assert result["_result_source"] == _SUPERVISOR_RESULT_SOURCE
            assert result["_failure_phase"] == "setup/handshake"
            assert result["_drive_status"] == "error"
            assert result["_drive_error"]
            assert len(result["_drive_error"]) <= _MAX_FAILURE_SENTINEL_ERROR
            with pytest.raises(AssertionError, match="did not complete successfully"):
                _assert_peer_success(result, label="peer")
    finally:
        _kill_without_waiting((listener, connector))
        listener.wait(timeout=2.0)
        connector.wait(timeout=2.0)


@pytest.mark.parametrize("missing_row", ("LinkMenu", "party_after.mon_records"))
def test_collect_pair_rejects_missing_or_partial_required_rows(missing_row: str):
    """A cleanly exited child still cannot pass with an incomplete JSON row."""
    incomplete = _complete_peer_result()
    if missing_row == "LinkMenu":
        del incomplete[missing_row]
    else:
        del incomplete["party_after"]["mon_records"]
    listener = _CompletedPeer(incomplete)
    connector = _CompletedPeer(_complete_peer_result(role="connect"))

    with pytest.raises(AssertionError, match="missing required result rows"):
        _collect_pair(
            listener,
            connector,
            deadline_at=time.monotonic() + 1.0,
        )


@pytest.mark.parametrize("goal", ("trade", "battle"))
def test_strict_acceptance_rejects_link_menu_only_result(goal: str):
    """The LinkMenu milestone is not strict trade or battle completion."""
    result = _complete_peer_result()
    result["LinkMenu"] = 1

    with pytest.raises(AssertionError, match="LinkMenu alone is insufficient"):
        _assert_strict_peer_result(
            result,
            label="listener",
            goal=goal,
            expected_role="listen",
            expected_version="yellow",
        )


@pytest.mark.parametrize("goal", ("trade", "battle"))
def test_strict_acceptance_rejects_missing_native_edge_req(goal: str):
    """Gameplay-shaped counters without native EDGE_REQ traffic cannot pass."""
    result = _complete_peer_result()
    if goal == "trade":
        result["_AddEnemyMonToPlayerParty"] = 1
    else:
        for symbol in (
            "DisplayLinkBattleVersusTextBox",
            "MoveSelectionMenu",
            "LinkBattleExchangeData",
        ):
            result[symbol] = 1
        result["ExecutePlayerMove"] = 1
    for field in (
        "edge_req_sent",
        "edge_req_received",
        "edge_resp_sent",
        "edge_resp_received",
    ):
        result["_backend_stats"][field] = 0

    with pytest.raises(AssertionError, match="no native EDGE_REQ"):
        _assert_strict_peer_result(
            result,
            label="listener",
            goal=goal,
            expected_role="listen",
            expected_version="yellow",
        )


def test_hold_at_sync_boundary_does_not_tick_past_ready_marker():
    """The release rendezvous uses owner dispatch, never blind ROM ticks."""

    class FakeBackend:
        def __init__(self):
            self.announced: list[int] = []

        def announce_sync(self, *, sync_id: int) -> None:
            self.announced.append(sync_id)

        def poll_peer_sync(self, *, sync_id: int) -> bool:
            # A peer that has already reached each matching phase.
            return sync_id in (113, 114)

    service_calls = 0

    def service_pending_edges() -> int:
        nonlocal service_calls
        service_calls += 1
        return 0

    backend = FakeBackend()
    _hold_at_sync_boundary(
        backend,
        ready_sync_id=113,
        release_sync_id=114,
        timeout=1.0,
        service_pending_edges=service_pending_edges,
    )

    assert backend.announced == [113, 114]
    # Both peer markers were already available, so no ROM tick is possible
    # in this helper; the callback exists solely for any already-admitted
    # owner edge that may arrive at a real boundary.
    assert service_calls == 0


def test_hold_at_sync_boundary_ticks_timed_rom_phase():
    """Timed ROM work keeps the owner emulator advancing during rendezvous."""

    class FakeBackend:
        def __init__(self):
            self.announced: list[int] = []
            self.polls: dict[int, int] = {}

        def announce_sync(self, *, sync_id: int) -> None:
            self.announced.append(sync_id)

        def poll_peer_sync(self, *, sync_id: int) -> bool:
            polls = self.polls.get(sync_id, 0)
            self.polls[sync_id] = polls + 1
            return polls >= 1

    progress_calls = 0

    def progress() -> None:
        nonlocal progress_calls
        progress_calls += 1

    backend = FakeBackend()
    _hold_at_sync_boundary(
        backend,
        ready_sync_id=115,
        release_sync_id=116,
        timeout=1.0,
        service_pending_edges=lambda: 0,
        progress_callback=progress,
    )

    assert backend.announced == [115, 116]
    assert progress_calls == 2


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
    for label, proc, captured in zip(("listener", "connector"), procs, captures, strict=True):
        stdout = _captured_text(captured, "stdout")
        stderr = _captured_text(captured, "stderr")
        _print_peer_trace(stderr, label=label)
        tails.append(
            f"{label} rc={proc.returncode}\n"
            f"stdout tail:\n{stdout[-2000:]}\n"
            f"stderr tail:\n{stderr[-2000:]}"
        )
    return _PairDeadlineExceeded(
        f"peer subprocess pair exceeded hard deadline at {deadline_at:.6f};\n" + "\n".join(tails)
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
    reader_groups = []
    readers = []
    for proc in procs:
        captured, proc_readers = _start_pipe_drainers(proc)
        captures.append(captured)
        reader_groups.append(proc_readers)
        readers.extend(proc_readers)

    while True:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            _kill_without_waiting(procs)
            raise _deadline_error(procs, tuple(captures), deadline_at)
        for index, (proc, proc_readers) in enumerate(zip(procs, reader_groups, strict=True)):
            if proc.poll() is None or any(reader.is_alive() for reader in proc_readers):
                continue
            if not _has_result_row(captures[index]):
                label = ("listener", "connector")[index]
                _kill_without_waiting(procs)
                if proc.returncode != 0:
                    results = tuple(
                        _failure_sentinel(
                            peer,
                            capture,
                            label=peer_label,
                            reason=(
                                f"{label} peer exited during setup/handshake before "
                                "emitting a result sentinel"
                                if peer_index == index
                                else f"{peer_label} peer was aborted after {label} "
                                "failed during setup/handshake"
                            ),
                        )
                        for peer_index, (peer, capture, peer_label) in enumerate(
                            zip(procs, captures, ("listener", "connector"), strict=True)
                        )
                    )
                    return results
                pytest.fail(
                    f"{label} peer exited without emitting {_RESULT_PREFIX.strip()}; "
                    f"rc={proc.returncode}, "
                    f"stdout tail:\n{_captured_text(captures[index], 'stdout')[-2000:]}\n"
                    f"stderr tail:\n{_captured_text(captures[index], 'stderr')[-2000:]}"
                )
        if all(proc.poll() is not None for proc in procs) and all(
            not reader.is_alive() for reader in readers
        ):
            break
        time.sleep(min(0.01, remaining))

    results = (
        _parse_result(listener, captures[0], label="listener"),
        _parse_result(connector, captures[1], label="connector"),
    )
    for label, result in zip(("listener", "connector"), results, strict=True):
        _assert_complete_peer_result(result, label=label)
    return results


def test_collect_pair_enforces_hard_deadline_without_waiting_for_peers():
    """A blocked child cannot extend the supervisor's absolute deadline."""
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    listener = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    connector = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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
