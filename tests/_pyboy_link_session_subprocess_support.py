"""Shared helpers for the two-subprocess PyBoy TCP tests.

Split from ``tests/test_pyboy_link_session_subprocess.py`` for #131 with no behavior
change. Holds the result-parsing, sentinel, pair-collection and peer-plumbing
helpers shared by the split test modules.
"""

from __future__ import annotations

import codecs
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
from tests._tcp_trade_peer import _TRADE_DIAG_SYMBOLS

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


def _watchdog_dumps_ready(data: str, count: int) -> bool:
    """Require a completed frame in each of the first ``count`` dumps."""
    segments = data.split("Timeout (")[1:]
    return len(segments) >= count and all(
        'File "<string>"' in segment for segment in segments[:count]
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
        read1 = getattr(getattr(stream, "buffer", None), "read1", None)
        if callable(read1):
            # These fresh pipes have no prior text reads. Publish available
            # bytes without waiting to fill a text read or reach EOF.
            decoder = io.IncrementalNewlineDecoder(
                codecs.getincrementaldecoder(stream.encoding)(errors=stream.errors),
                translate=True,
            )
            while True:
                raw = read1(8192)
                chunk = decoder.decode(raw, final=not raw)
                if chunk:
                    chunks.append(chunk)
                if not raw:
                    return
        # StringIO and other text-only process doubles have no binary buffer.
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
        evidence = result.get("battle_turn")
        if not isinstance(evidence, dict) or evidence.get("settled") is not True:
            raise AssertionError(f"{label} peer lacks a settled battle snapshot; result={result}")
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


def _link_menu_failure_summary(result: dict, *, label: str) -> str:
    """Keep endpoint evidence visible after pytest truncates a large result."""
    history = result.get("_link_menu_history") or {}
    if not isinstance(history, dict):
        history = {}
    events = {
        "choose": "LinkMenu.doneChoosingMenuSelection",
        "exchange": "LinkMenu.afterExchange",
        "prepare": "PrepareForSpecialWarp",
        "warp": "SpecialEnterMap",
    }
    first = history.get("first", {})
    hooks = history.get("hooks", {})
    summary = {
        "endpoint": str(label)[:32],
        "role": str(result.get("_role"))[:16],
        "version": str(result.get("_version"))[:24],
        "votes": {
            direction: {
                "seq": sample.get("seq"),
                "candidate": (
                    {"index": 0, "value": sample["wLinkMenuSelectionSendBuffer"][0]}
                    if direction == "sent"
                    else sample.get("recv_candidate")
                ),
            }
            for direction, sample in history.get("first_decisive", {}).items()
            if direction in ("sent", "received")
        },
        "milestones": {
            alias: {
                "seq": first.get(event, {}).get("seq"),
                "available": hooks.get(event, {}).get("available"),
                "count": history.get("counts", {}).get(event, 0),
            }
            for alias, event in events.items()
        },
        "total": history.get("total"),
        "recent_dropped": history.get("recent_dropped"),
        "recent_truncated": history.get("recent_truncated"),
        "errors_dropped": history.get("errors_dropped"),
        "errors_truncated": history.get("errors_truncated"),
    }
    return "link-menu-summary=" + json.dumps(summary, separators=(",", ":"))


def _assert_peer_success(result: dict, *, label: str) -> None:
    """Reject partial or failed child sentinels before gameplay assertions."""
    status = result.get("_drive_status")
    error = result.get("_drive_error")
    deadline_exceeded = result.get("_deadline_exceeded")
    returncode = result.get("_supervisor_returncode")
    if status != "ok" or error is not None or deadline_exceeded is not False or returncode != 0:
        summary = _link_menu_failure_summary(result, label=label)
        print(summary, file=sys.stderr)
        raise AssertionError(
            f"{label} peer did not complete successfully: "
            f"status={status!r} error={error!r} "
            f"deadline_exceeded={deadline_exceeded!r} "
            f"returncode={returncode!r}; result={result}\n"
            f"{summary}"
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
