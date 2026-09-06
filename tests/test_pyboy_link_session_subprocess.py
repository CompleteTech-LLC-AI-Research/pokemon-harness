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

import ast
import codecs
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._rom_assets import fixture_path, rom_path, sym_path
from tests._tcp_trade_peer import _TRADE_DIAG_SYMBOLS, _hold_at_sync_boundary

_REPO = Path(__file__).resolve().parents[1]
_TCP_TRADE_PEER = Path(__file__).with_name("_tcp_trade_peer.py")
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


def _tcp_call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _is_tcp_confirmation_press(call: ast.Call) -> bool:
    """Return whether *call* queues a public Cable Club A confirmation."""
    return (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "session"
        and call.func.attr == "press"
        and bool(call.args)
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == "a"
    )


def _cable_club_entry_sync_contract(tree: ast.AST) -> tuple[bool, str]:
    """Check the native-entry, passive-rendezvous, confirmation ordering.

    Each subprocess observes its own ``CableClubNPC`` hook. A passive
    ready/release handshake made only after that local observation means both
    peers have reached the native entry boundary without advancing a ROM. The
    first subsequent A is the serial-driving save confirmation.
    """
    ready_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _tcp_call_name(node.func) == "_is_cable_club_save_choice_ready"
    ]
    if not ready_calls:
        return False, "driver never checks the native Cable Club save-choice predicate"

    ready_line = max(call.lineno for call in ready_calls)
    confirmations = sorted(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and node.lineno > ready_line
            and _is_tcp_confirmation_press(node)
        ),
        key=lambda call: call.lineno,
    )
    if not confirmations:
        return False, "no A confirmation follows the Cable Club readiness predicate"

    first_confirmation = confirmations[0]
    passive_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _tcp_call_name(node.func) == "passive_sync"
        and ready_line < node.lineno < first_confirmation.lineno
    ]
    if not passive_calls:
        return False, (
            "no passive control-plane sync occurs after CableClubNPC readiness "
            "and before the first save-confirmation A"
        )
    if not any(
        any(keyword.arg == "ready_sync_id" for keyword in call.keywords)
        and any(keyword.arg == "release_sync_id" for keyword in call.keywords)
        for call in passive_calls
    ):
        return False, "Cable Club passive sync must use ready and release markers"
    return True, ""


def test_cable_club_entry_sync_contract_recognizes_only_passive_preconfirmation_sync():
    """Keep the source contract specific to the two-marker passive protocol."""
    compliant = ast.parse(
        """
if _is_cable_club_save_choice_ready(counters, menu_snapshot()):
    passive_sync(ready_sync_id=127, release_sync_id=128, timeout=1.0)
    session.press("a", duration=1)
"""
    )
    cooperative = ast.parse(
        """
if _is_cable_club_save_choice_ready(counters, menu_snapshot()):
    cooperative_sync(sync_id=127, timeout=1.0)
    session.press("a", duration=1)
"""
    )

    assert _cable_club_entry_sync_contract(compliant) == (True, "")
    assert "no passive control-plane sync" in _cable_club_entry_sync_contract(cooperative)[1]


def test_tcp_peer_waits_for_both_cable_club_entries_before_save_confirmation():
    """The serial-driving A is barred until both peers reached CableClubNPC."""
    tree = ast.parse(_TCP_TRADE_PEER.read_text(encoding="utf-8"), filename=str(_TCP_TRADE_PEER))
    predicate = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_is_cable_club_save_choice_ready"
    )
    assert "CableClubNPC" in ast.unparse(predicate)

    valid, reason = _cable_club_entry_sync_contract(tree)
    assert valid, reason


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


class _HistoryMemory(dict):
    """Seed through dict.update; record every helper-originated write."""

    def __init__(self):
        super().__init__()
        self.writes = []
        self.reads = []

    def __getitem__(self, key):
        self.reads.append(key)
        return super().__getitem__(key)

    def __setitem__(self, key, value):
        self.writes.append((key, value))
        super().__setitem__(key, value)


def _history_session(*, target_bank=0, target_address=0x2247, opcode=0xCD):
    from pokered_harness.symbols.loader import load_sym_text
    from tests._tcp_trade_peer import _LINK_MENU_HISTORY_EVENTS

    locations = {name: (3, 0x5000 + index * 0x10) for index, name in enumerate(_TRADE_DIAG_SYMBOLS)}
    locations["Serial_ExchangeLinkMenuSelection"] = (target_bank, target_address)
    locations["LinkMenu.exchangeMenuSelectionLoop"] = (3, 0x5800)
    ram_names = (
        "wCurrentMenuItem",
        "wMaxMenuItem",
        "wCableClubDestinationMap",
        "wLinkState",
        "hSerialConnectionStatus",
        "wCurMap",
        "wLinkMenuSelectionSendBuffer",
        "wLinkMenuSelectionReceiveBuffer",
    )
    locations.update({name: (0, 0xC000 + i * 8) for i, name in enumerate(ram_names)})
    symbols = load_sym_text(
        "\n".join(f"{bank:02x}:{address:04x} {name}" for name, (bank, address) in locations.items())
    )
    memory = _HistoryMemory()
    for name in ram_names:
        memory.update({symbols.addr_of(name) + offset: 0 for offset in range(2)})
    bank, address = locations["LinkMenu.exchangeMenuSelectionLoop"]
    memory.update(
        {
            (bank, address): opcode,
            (bank, address + 1): target_address & 255,
            (bank, address + 2): target_address >> 8,
        }
    )
    # A different currently mapped bank must not be used to validate the CALL.
    memory.update({address: 0, address + 1: 0, address + 2: 0})
    hooks = {}
    registrations = []
    failures = set()

    def register(bank, address, callback, context):
        registrations.append((bank, address))
        if (bank, address) in failures:
            raise ValueError("registration unavailable")
        assert (bank, address) not in hooks, "duplicate hook registration"
        hooks[bank, address] = (callback, context)

    session = SimpleNamespace(
        symbols=symbols,
        tick=0,
        _pyboy=SimpleNamespace(memory=memory, hook_register=register),
    )
    session.current_tick = lambda: session.tick

    def fire(event):
        location = (bank, address + 3) if event == "LinkMenu.afterExchange" else locations[event]
        callback, context = hooks[location]
        callback(context)

    assert set(_LINK_MENU_HISTORY_EVENTS) <= set(locations) | {"LinkMenu.afterExchange"}
    return session, memory, hooks, registrations, failures, fire


def test_link_menu_history_preserves_first_samples_across_buffer_reuse():
    from tests._tcp_trade_peer import _LinkMenuHistory

    session, memory, _hooks, registrations, _failures, fire = _history_session()
    history = _LinkMenuHistory(session, role="listen", version="blue", limit=3)
    buckets = {name: [0] for name in _TRADE_DIAG_SYMBOLS}
    history.install(buckets)
    installed = list(registrations)
    history.install(buckets)
    assert registrations == installed
    send = session.symbols.addr_of("wLinkMenuSelectionSendBuffer")
    receive = session.symbols.addr_of("wLinkMenuSelectionReceiveBuffer")
    memory.update({send: 0xD4, send + 1: 0xFF, receive: 0xD4, receive + 1: 0xFF})
    events = [
        "LinkMenu.doneChoosingMenuSelection",
        "LinkMenu.afterExchange",
        "PrepareForSpecialWarp",
        "SpecialEnterMap",
    ]
    for tick, event in enumerate(events, 1):
        session.tick = tick
        fire(event)
    original = history.snapshot()
    memory.update({send: 0x12, receive: 0x34})
    for tick in range(5, 10):
        session.tick = tick
        fire("LinkMenu.afterExchange")
    result = history.snapshot()
    assert result["total"] == 9
    assert {event: count for event, count in result["counts"].items() if count} == {
        event: (6 if event == "LinkMenu.afterExchange" else 1) for event in events
    }
    assert [sample["tick"] for sample in result["recent"]] == [7, 8, 9]
    assert result["first"] == original["first"]
    for event in events:
        sample = result["first"][event]
        assert sample["event"] == event
        assert (sample["role"], sample["version"]) == ("listen", "blue")
        assert sample["wLinkMenuSelectionSendBuffer"] == [0xD4, 0xFF]
        assert sample["wLinkMenuSelectionReceiveBuffer"] == [0xD4, 0xFF]
    assert result["recent"][-1]["wLinkMenuSelectionReceiveBuffer"] == [0x34, 0xFF]
    assert buckets["SpecialEnterMap"] == [1]
    assert memory.writes == []
    # Neither caller mutation nor later ROM buffer reuse can alter retained evidence.
    result["first"][events[0]]["wLinkMenuSelectionSendBuffer"][0] = 0
    result["recent"].clear()
    assert history.snapshot()["first"] == original["first"]
    assert len(history.snapshot()["recent"]) == 3
    json.dumps(history.snapshot())


@pytest.mark.parametrize(
    "opcode,target_bank,target_address,valid",
    [
        (0xCD, 0, 0x2247, True),
        (0x00, 0, 0x2247, False),
        (0xCD, 3, 0x6247, True),
        (0xCD, 2, 0x6247, False),
        (0xCD, 2, 0x2247, False),
        (0xCD, 0, 0x6247, False),
    ],
)
def test_link_menu_history_validates_call_and_bank(opcode, target_bank, target_address, valid):
    from tests._tcp_trade_peer import _LinkMenuHistory

    session, memory, hooks, _registrations, _failures, _fire = _history_session(
        opcode=opcode,
        target_bank=target_bank,
        target_address=target_address,
    )
    history = _LinkMenuHistory(session, role="connect", version="yellow")
    history.install({name: [0] for name in _TRADE_DIAG_SYMBOLS})
    bank, address = session.symbols.bank_addr("LinkMenu.exchangeMenuSelectionLoop")
    assert ((bank, address + 3) in hooks) is valid
    assert history.snapshot()["hooks"]["LinkMenu.afterExchange"]["available"] is valid
    assert (bank, address) in memory.reads
    assert memory.writes == []


def test_link_menu_history_rejects_call_to_wrong_target():
    from tests._tcp_trade_peer import _LinkMenuHistory

    session, memory, hooks, _registrations, _failures, _fire = _history_session()
    bank, address = session.symbols.bank_addr("LinkMenu.exchangeMenuSelectionLoop")
    memory.update({(bank, address + 1): 0x48})
    history = _LinkMenuHistory(session, role="listen", version="red")
    history.install({name: [0] for name in _TRADE_DIAG_SYMBOLS})
    assert (bank, address + 3) not in hooks
    assert history.snapshot()["hooks"]["LinkMenu.afterExchange"]["available"] is False
    assert memory.writes == []


def test_link_menu_history_additive_result_compatibility():
    from tests._tcp_trade_peer import _LinkMenuHistory

    session, _memory, _hooks, _registrations, _failures, fire = _history_session()
    history = _LinkMenuHistory(session, role="listen", version="yellow")
    history.install({name: [0] for name in _TRADE_DIAG_SYMBOLS})
    fire("LinkMenu")
    legacy = _complete_peer_result(role="connect")
    enriched = _complete_peer_result()
    enriched["_link_menu_history"] = history.snapshot()
    first, second = _collect_pair(
        _CompletedPeer(enriched), _CompletedPeer(legacy), deadline_at=time.monotonic() + 1.0
    )
    assert first["_link_menu_history"] == enriched["_link_menu_history"]
    assert "_link_menu_history" not in second
    for result in (first, second):
        _assert_peer_success(result, label="fake")


def test_link_menu_history_reports_missing_symbols_and_registration_errors():
    from tests._tcp_trade_peer import _LinkMenuHistory

    session, memory, _hooks, _registrations, failures, fire = _history_session()
    symbols = session.symbols

    def bank_addr(name):
        if name == "LinkMenu.choseCancel":
            raise KeyError(name)
        return symbols.bank_addr(name)

    session.symbols = SimpleNamespace(bank_addr=bank_addr, addr_of=symbols.addr_of)
    failures.add(symbols.bank_addr("PrepareForSpecialWarp"))
    history = _LinkMenuHistory(session, role="listen", version="red")
    buckets = {name: [0] for name in _TRADE_DIAG_SYMBOLS}
    history.install(buckets)
    fire("LinkMenu")
    result = history.snapshot()
    assert result["hooks"]["LinkMenu.choseCancel"]["available"] is False
    assert result["hooks"]["PrepareForSpecialWarp"]["available"] is False
    assert result["hooks"]["LinkMenu"]["available"] is True
    assert {(error["event"], error["stage"]) for error in result["errors"]} == {
        ("LinkMenu.choseCancel", "resolve"),
        ("PrepareForSpecialWarp", "register"),
    }
    assert result["error_count"] == 2
    assert result["total"] == 1
    assert buckets["LinkMenu"] == [1]
    assert buckets["PrepareForSpecialWarp"] == [0]
    assert memory.writes == []


def test_link_menu_history_bounds_callback_errors_and_keeps_partial_samples():
    from tests._tcp_trade_peer import _LinkMenuHistory

    session, memory, _hooks, _registrations, _failures, fire = _history_session()
    history = _LinkMenuHistory(session, role="connect", version="yellow", limit=2)
    buckets = {name: [0] for name in _TRADE_DIAG_SYMBOLS}
    history.install(buckets)
    symbols = session.symbols

    def addr_of(name):
        if name == "wCurrentMenuItem":
            raise KeyError("missing symbol " + "x" * 400)
        return symbols.addr_of(name)

    def broken_tick():
        raise RuntimeError("tick unavailable " + "x" * 400)

    session.symbols = SimpleNamespace(addr_of=addr_of)
    session.current_tick = broken_tick
    del memory[symbols.addr_of("wLinkMenuSelectionReceiveBuffer") + 1]
    for _ in range(4):
        fire("LinkMenu")
    result = history.snapshot()
    assert result["total"] == result["counts"]["LinkMenu"] == 4
    assert result["error_count"] == 12
    assert result["errors_dropped"] == 10
    assert result["errors_truncated"] is True
    assert len(result["errors"]) == len(result["recent"]) == 2
    assert [error["stage"] for error in result["errors"]] == [
        "wCurrentMenuItem",
        "wLinkMenuSelectionReceiveBuffer",
    ]
    assert all(len(error["error"]) <= 256 for error in result["errors"])
    for sample in [result["first"]["LinkMenu"], *result["recent"]]:
        assert sample["tick"] is None
        assert "wCurrentMenuItem" not in sample
        assert "wLinkMenuSelectionReceiveBuffer" not in sample
        assert sample["wLinkMenuSelectionSendBuffer"] == [0, 0]
    assert buckets["LinkMenu"] == [4]
    assert memory.writes == []
    result["errors"][0]["error"] = "caller mutation"
    assert history.snapshot()["errors"][0]["error"] != "caller mutation"
    json.dumps(result)


def test_link_menu_history_decisive_directions_ignore_stale_second_bytes():
    from tests._tcp_trade_peer import _LinkMenuHistory

    session, memory, _hooks, _registrations, _failures, fire = _history_session()
    history = _LinkMenuHistory(session, role="listen", version="blue")
    history.install({name: [0] for name in _TRADE_DIAG_SYMBOLS})
    assert history.snapshot()["limit"] == 8
    event = "LinkMenu.afterExchange"
    send = session.symbols.addr_of("wLinkMenuSelectionSendBuffer")
    receive = session.symbols.addr_of("wLinkMenuSelectionReceiveBuffer")
    memory.update({send + 1: 0xD4, receive: 0xD0, receive + 1: 0xD8})
    for tick in range(1, 21):
        session.tick = tick
        fire(event)
    idle = history.snapshot()
    assert idle["first_decisive"] == {}
    session.tick = 21
    memory.update({send: 0xD4})
    fire(event)
    sent = history.snapshot()
    assert sent["first_decisive"]["sent"]["tick"] == 21
    assert "received" not in sent["first_decisive"]
    session.tick = 22
    memory.update({send: 0, receive: 0xD8})
    fire(event)
    for tick in range(23, 40):
        session.tick = tick
        memory.update({send: 0x12, receive: 0x34})
        fire(event)
    result = history.snapshot()
    assert result["first"][event]["tick"] == 1
    assert result["first_decisive"]["sent"] == sent["first_decisive"]["sent"]
    for direction, tick, symbol, value in (
        ("sent", 21, "wLinkMenuSelectionSendBuffer", 0xD4),
        ("received", 22, "wLinkMenuSelectionReceiveBuffer", 0xD8),
    ):
        sample = result["first_decisive"][direction]
        assert sample["tick"] == sample["seq"] == tick
        assert sample[symbol][0] == value
        assert sample["wCurMap"] == 0
    assert [sample["tick"] for sample in result["recent"]] == list(range(32, 40))
    assert result["total"] == result["counts"][event] == 39
    assert result["recent_dropped"] == 31
    assert result["recent_truncated"] is True
    assert result["errors_dropped"] == 0
    assert result["errors_truncated"] is False
    result["first_decisive"]["received"]["wLinkMenuSelectionReceiveBuffer"][0] = 0
    assert (
        history.snapshot()["first_decisive"]["received"]["wLinkMenuSelectionReceiveBuffer"][0]
        == 0xD8
    )
    assert memory.writes == []


@pytest.mark.parametrize(
    "first,second,candidate,index",
    [
        (0x12, 0xD4, 0xD4, 1),
        (0xD0, 0xD4, None, None),
        (0x12, 0x34, None, None),
        (0xD8, 0xD4, 0xD8, 0),
    ],
)
def test_link_menu_history_received_candidate_follows_rom_order(first, second, candidate, index):
    from tests._tcp_trade_peer import _LinkMenuHistory

    session, memory, _hooks, _registrations, _failures, fire = _history_session()
    history = _LinkMenuHistory(session, role="connect", version="yellow")
    history.install({name: [0] for name in _TRADE_DIAG_SYMBOLS})
    address = session.symbols.addr_of("wLinkMenuSelectionReceiveBuffer")
    memory.update({address: first, address + 1: second})
    fire("LinkMenu.afterExchange")
    decisive = history.snapshot()["first_decisive"]
    if first == 0xD0:
        assert history.snapshot()["recent"][-1]["recv_candidate"] == {"value": 0xD0, "index": 0}
    if candidate is None:
        assert "received" not in decisive
    else:
        assert decisive["received"]["recv_candidate"] == {"value": candidate, "index": index}
        assert decisive["received"]["wLinkMenuSelectionReceiveBuffer"] == [first, second]
    assert memory.writes == []


def test_link_menu_history_failure_summary_survives_large_result_tail(tmp_path):
    from tests._tcp_trade_peer import _LinkMenuHistory

    session, memory, _hooks, _registrations, _failures, fire = _history_session()
    history = _LinkMenuHistory(session, role="connect", version="yellow", limit=1)
    history.install({name: [0] for name in _TRADE_DIAG_SYMBOLS})
    address = session.symbols.addr_of("wLinkMenuSelectionReceiveBuffer")
    memory.update({address: 0x12, address + 1: 0xD4})
    memory.update({session.symbols.addr_of("wLinkMenuSelectionSendBuffer"): 0xD8})
    for event in (
        "LinkMenu.doneChoosingMenuSelection",
        "LinkMenu.afterExchange",
        "PrepareForSpecialWarp",
        "SpecialEnterMap",
    ):
        fire(event)
    result = _complete_peer_result(role="connect")
    result.update(
        _drive_status="error",
        _drive_error="warp timeout",
        _link_menu_history=history.snapshot(),
        large_trace="x" * 10000,
    )
    with pytest.raises(AssertionError) as raised:
        _assert_peer_success(result, label="connector")
    message = str(raised.value)
    assert len(message) > 10000
    summary = message.rsplit("\n", 1)[1]
    assert len(summary) <= 1000
    assert summary in message[-4000:]
    decoded = json.loads(summary.removeprefix("link-menu-summary="))
    assert (decoded["endpoint"], decoded["role"], decoded["version"]) == (
        "connector",
        "connect",
        "yellow",
    )
    assert decoded["votes"]["received"]["candidate"] == {"value": 0xD4, "index": 1}
    assert decoded["votes"]["sent"]["candidate"] == {"value": 0xD8, "index": 0}
    assert decoded["milestones"]["warp"] == {
        "seq": 4,
        "available": True,
        "count": 1,
    }
    assert decoded["recent_dropped"] == 3
    assert decoded["recent_truncated"] is True
    # Pytest renders captured stdout after the traceback; stderr must carry
    # the same bounded summary after that large output as well.
    source = tmp_path / "test_fake_failure.py"
    source.write_text(
        "from tests.test_pyboy_link_session_subprocess import _assert_peer_success\n"
        "def test_fake_failure():\n"
        "    print('captured-large-output-' + 'x' * 10000)\n"
        f"    _assert_peer_success({result!r}, label='connector')\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "pytest_asyncio.plugin", "-q", str(source)],
        cwd=_REPO,
        env={
            **os.environ,
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join(
                (str(_REPO), str(_REPO / "src"), str(_REPO / "vendor/pyboy-src"))
            ),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 1
    assert "Captured stdout" in completed.stdout
    assert "Captured stderr" in completed.stdout
    assert summary in completed.stdout[-4000:]


@pytest.mark.parametrize(
    "missing",
    [
        "LinkMenu.exchangeMenuSelectionLoop",
        "Serial_ExchangeLinkMenuSelection",
    ],
)
def test_link_menu_history_missing_call_symbols_remains_observable(missing):
    from tests._tcp_trade_peer import _LinkMenuHistory

    session, memory, hooks, _registrations, _failures, fire = _history_session()
    symbols = session.symbols

    def bank_addr(name):
        if name == missing:
            raise KeyError(name)
        return symbols.bank_addr(name)

    session.symbols = SimpleNamespace(bank_addr=bank_addr, addr_of=symbols.addr_of)
    history = _LinkMenuHistory(session, role="listen", version="blue")
    history.install({name: [0] for name in _TRADE_DIAG_SYMBOLS})
    bank, address = symbols.bank_addr("LinkMenu.exchangeMenuSelectionLoop")
    assert (bank, address + 3) not in hooks
    fire("SpecialEnterMap")
    result = history.snapshot()
    assert result["hooks"]["LinkMenu.afterExchange"]["available"] is False
    assert result["counts"]["SpecialEnterMap"] == 1
    assert any(
        error["event"] == "LinkMenu.afterExchange" and error["stage"] == "resolve"
        for error in result["errors"]
    )
    assert memory.writes == []


@pytest.mark.parametrize("bank,address", [(0, 0x3FFD), (3, 0x7FFD)])
def test_link_menu_history_rejects_post_call_outside_bank(bank, address):
    from tests._tcp_trade_peer import _LinkMenuHistory

    session, memory, hooks, _registrations, _failures, _fire = _history_session()
    symbols = session.symbols
    session.symbols = SimpleNamespace(
        bank_addr=lambda name: (
            (bank, address)
            if name == "LinkMenu.exchangeMenuSelectionLoop"
            else symbols.bank_addr(name)
        ),
        addr_of=symbols.addr_of,
    )
    memory.update({(bank, address): 0xCD, (bank, address + 1): 0x47, (bank, address + 2): 0x22})
    history = _LinkMenuHistory(session, role="listen", version="blue")
    history.install({name: [0] for name in _TRADE_DIAG_SYMBOLS})
    assert (bank, address + 3) not in hooks
    assert history.snapshot()["hooks"]["LinkMenu.afterExchange"]["available"] is False
    assert memory.writes == []


@pytest.mark.parametrize(
    "watchdog_case",
    [
        "invalid-optin", "destinations", "wrapper", "real-output",
        "lifecycle", "arm-error", "cancel-error", "close-error",
    ],
)
def test_peer_trace_watchdog(watchdog_case, monkeypatch):
    """Opt-in peer stack diagnostics remain bounded and preserve peer outcomes."""
    from tests import _tcp_trade_peer as peer

    delay_env = "POKERED_PEER_TRACE_AFTER_SECONDS"
    dir_env = "POKERED_PEER_TRACE_DIR"
    monkeypatch.delenv(dir_env, raising=False)
    if watchdog_case == "invalid-optin":
        def unexpected(*args, **kwargs):
            pytest.fail("disabled watchdog must not open files or arm diagnostics")

        monkeypatch.setattr(peer.faulthandler, "dump_traceback_later", unexpected)
        monkeypatch.setattr(peer.tempfile, "mkstemp", unexpected)
        for value in (None, "", "garbage", "0", "-1", "nan", "inf", "-inf"):
            if value is None:
                monkeypatch.delenv(delay_env, raising=False)
            else:
                monkeypatch.setenv(delay_env, value)
            assert peer._PeerTraceWatchdog.from_env() is None
        return

    if watchdog_case == "destinations":
        monkeypatch.setenv(delay_env, "0.05")
        trace = peer._PeerTraceWatchdog.from_env()
        assert trace.destination is peer.sys.stderr
        assert not trace.owned
        trace.close()
        with tempfile.TemporaryDirectory(prefix="peer-watchdog-paths-") as directory:
            root = Path(directory)
            missing = root / "missing"
            for configured in ("", str(missing), __file__):
                monkeypatch.setenv(dir_env, configured)
                trace = peer._PeerTraceWatchdog.from_env()
                assert trace.destination is peer.sys.stderr
                assert not trace.owned
                trace.close()
            assert not missing.exists()
            assert list(root.iterdir()) == []
            monkeypatch.setenv(dir_env, directory)
            first = peer._PeerTraceWatchdog.from_env()
            try:
                first_path, = root.iterdir()
                os.write(first.destination, b"preserve existing artifact")
                second = peer._PeerTraceWatchdog.from_env()
                try:
                    paths = list(root.iterdir())
                    assert len(paths) == 2
                    assert all(path.name.startswith(f"peer-trace-{os.getpid()}-") for path in paths)
                    assert first_path.read_bytes() == b"preserve existing artifact"
                    if os.name == "posix":
                        assert all(path.stat().st_mode & 0o777 == 0o600 for path in paths)
                finally:
                    second.close()
            finally:
                first.close()

            def fail_open(**kwargs):
                raise OSError("diagnostic artifact open failed")

            monkeypatch.setattr(peer.tempfile, "mkstemp", fail_open)
            trace = peer._PeerTraceWatchdog.from_env()
            assert trace.destination is peer.sys.stderr
            assert not trace.owned
            trace.close()
        return

    if watchdog_case == "wrapper":
        events = []

        class Trace:
            def start(self):
                events.append("start")

            def close(self):
                events.append("close")

        trace = Trace()
        monkeypatch.setattr(peer._PeerTraceWatchdog, "from_env", lambda: trace)
        failure = RuntimeError("original peer failure")
        for outcome in (17, failure):
            events.clear()

            def run_peer(*, trace, outcome=outcome):
                assert trace is not None
                events.extend(("cleanup", "emit_result"))
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome

            monkeypatch.setattr(peer, "_run_peer", run_peer)
            if isinstance(outcome, Exception):
                with pytest.raises(RuntimeError) as raised:
                    peer.main()
                assert raised.value is failure
            else:
                assert peer.main() == outcome
            assert events == ["start", "cleanup", "emit_result", "close"]
        monkeypatch.setattr(peer._PeerTraceWatchdog, "from_env", lambda: None)
        monkeypatch.setattr(peer, "_run_peer", lambda *, trace: 23 if trace is None else 99)
        assert peer.main() == 23

        def disabled_failure(*, trace):
            assert trace is None
            raise failure

        monkeypatch.setattr(peer, "_run_peer", disabled_failure)
        with pytest.raises(RuntimeError) as raised:
            peer.main()
        assert raised.value is failure
        return

    if watchdog_case == "real-output":
        # Real faulthandler runs only in this disposable Python child. No ROM
        # imports/loads, emulator, TCP connection, or repository artifacts.
        script = """
import json, os, sys, time
from pathlib import Path
from tests import _tcp_trade_peer as peer

directory = Path(sys.argv[1])
os.environ['POKERED_PEER_TRACE_AFTER_SECONDS'] = '0.05'
os.environ['POKERED_PEER_TRACE_DIR'] = str(directory)
trace = peer._PeerTraceWatchdog.from_env()
assert trace is not None
try:
    trace.start()
    files = list(directory.iterdir())
    assert len(files) == 1, files
    path = files[0]
    assert path.name.startswith(f'peer-trace-{os.getpid()}-')
    def wait_for_dumps(count):
        cutoff = time.monotonic() + 5.0
        while time.monotonic() < cutoff:
            data = path.read_text(errors='replace')
            if data.count('Timeout (') >= count:
                return data
            time.sleep(0.01)
        raise AssertionError('watchdog did not emit expected dump: ' + data)
    first = wait_for_dumps(1)
    trace.cleanup(time.monotonic() + 5.0)
    second = wait_for_dumps(2)
    assert 'File "<string>"' in first
    assert second.count('Timeout (') == 2
finally:
    trace.close()
# Cancel a fresh pending timer, then stay alive past its deadline. Inspecting
# the artifact after child exit alone could hide an uncancelled timer.
os.environ['POKERED_PEER_TRACE_AFTER_SECONDS'] = '0.1'
cancelled = peer._PeerTraceWatchdog.from_env()
cancelled_path, = set(directory.iterdir()) - {path}
try:
    cancelled.start()
finally:
    cancelled.close()
before = cancelled_path.read_bytes()
assert before == b''
time.sleep(0.25)
assert cancelled_path.read_bytes() == before
assert path.read_text(errors='replace').count('Timeout (') == 2
print(json.dumps({'pid': os.getpid(), 'name': path.name, 'dumps': 2,
                  'cancelled_empty': True}), flush=True)
"""
        with tempfile.TemporaryDirectory(prefix="peer-watchdog-test-") as directory:
            completed = subprocess.run(
                [sys.executable, "-c", script, directory],
                capture_output=True,
                text=True,
                timeout=15.0,
                check=False,
            )
            assert completed.returncode == 0, completed.stderr
            result = json.loads(completed.stdout)
            assert result["dumps"] == 2
            assert result["cancelled_empty"] is True
            assert result["name"].startswith(f"peer-trace-{result['pid']}-")
            assert len(list(Path(directory).iterdir())) == 2
        return

    events = []
    clock = [100.0]
    monkeypatch.setattr(peer.time, "monotonic", lambda: clock[0])

    destination = 987654
    closed = []
    arms = []

    def arm(delay, **kwargs):
        events.append("arm")
        arms.append((delay, kwargs))
        assert not closed
        if watchdog_case == "arm-error":
            raise OSError("diagnostic arm failed")

    def cancel():
        events.append("cancel")
        assert not closed
        if watchdog_case == "cancel-error":
            raise OSError("diagnostic cancellation failed")

    monkeypatch.setattr(peer.faulthandler, "dump_traceback_later", arm)
    monkeypatch.setattr(peer.faulthandler, "cancel_dump_traceback_later", cancel)
    original_close = os.close

    def close(fd):
        if fd != destination:
            return original_close(fd)
        events.append("close")
        closed.append(fd)
        if watchdog_case == "close-error":
            raise OSError("diagnostic close failed")

    monkeypatch.setattr(peer.os, "close", close)
    trace = peer._PeerTraceWatchdog(10.0, destination, owned=True)
    failure = RuntimeError("original peer failure")

    def run_peer(*, trace):
        # Cleanup before initial due must not replace/postpone its timer.
        clock[0] = 105.0
        trace.cleanup(130.0)
        assert len(arms) == 1
        clock[0] = 111.0
        trace.cleanup(113.0)
        trace.cleanup(140.0)
        assert len(arms) == 2
        assert arms[1][0] == 2.0
        events.append("emit_result")
        assert not closed
        raise failure

    monkeypatch.setattr(peer._PeerTraceWatchdog, "from_env", lambda: trace)
    monkeypatch.setattr(peer, "_run_peer", run_peer)
    with pytest.raises(RuntimeError) as raised:
        peer.main()
    assert raised.value is failure
    assert arms[0][0] == 10.0
    for _delay, kwargs in arms:
        assert kwargs["file"] == destination
        assert kwargs.get("repeat", False) is False
        assert kwargs.get("exit", False) is False
    assert events.index("emit_result") < events.index("cancel")
    if watchdog_case == "cancel-error":
        assert not closed
    else:
        assert closed == [destination]
        assert events.index("cancel") < events.index("close")
        trace.close()
        assert closed == [destination]


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


class _ShutdownBackend:
    """Delay each ack until owner work is serviced, including the final ack."""

    def __init__(self, *, fail_at=None, missing_done=False):
        from pokered_harness.link.network_backend import NetworkBackendError

        self.trace = []
        self.marker = None
        self.polls = {}
        self.drains = 0
        self.fail_at = fail_at
        self.failure = NetworkBackendError("backend shutdown failure")
        self.missing_done = missing_done

    def record(self, *event):
        self.trace.append(event)
        if event == self.fail_at:
            raise self.failure

    def announce_sync(self, *, sync_id):
        self.record("announce", sync_id)
        self.marker = sync_id

    def poll_peer_sync(self, *, sync_id):
        self.record("poll", sync_id)
        self.polls[sync_id] = self.polls.get(sync_id, 0) + 1
        if sync_id == 126 and self.missing_done:
            return False
        # Completion is deliberately later than release and final drain ack.
        return self.polls[sync_id] > (3 if sync_id == 126 else 1)

    def service_pending_edges(self, *, max_edges):
        assert max_edges == 1
        self.record("service", self.marker)
        return 1

    def wait_for_wire_idle(self, *, timeout, progress_callback, stable_checks):
        assert timeout == 10.0
        assert stable_checks == 4
        self.drains += 1
        self.record("drain", self.drains)
        progress_callback()
        self.record("idle", self.drains)

    def step(self, frames):
        # Release is the hard boundary: even a late done ack cannot tick.
        assert self.marker is None
        assert frames == 1
        self.record("step", frames)

    def cooperative_sync(self, *, sync_id, timeout, step_frames):
        assert (sync_id, timeout, step_frames) == (123, 10.0, 1)
        self.record("ready", sync_id)
        self.step(step_frames)


def _run_fake_shutdown(backend):
    from tests._tcp_trade_peer import _peer_shutdown_sync

    clock = iter(range(1000))
    _peer_shutdown_sync(
        backend,
        ready_sync_id=123,
        release_sync_id=124,
        timeout=10.0,
        cooperative_sync=backend.cooperative_sync,
        step=backend.step,
        backend_snapshot=dict,
        monotonic=lambda: next(clock) * 0.1,
        sleep=lambda seconds: backend.record("sleep", seconds),
    )


def test_peer_shutdown_protocol_drains_and_waits_for_late_done_without_ticks():
    backend = _ShutdownBackend()
    _run_fake_shutdown(backend)
    assert [
        event
        for event in backend.trace
        if event[0] in ("ready", "step", "drain", "idle", "announce")
    ] == [
        ("ready", 123),
        ("step", 1),
        ("drain", 1),
        ("step", 1),
        ("idle", 1),
        ("announce", 124),
        ("drain", 2),
        ("idle", 2),
        ("announce", 125),
        ("drain", 3),
        ("idle", 3),
        ("announce", 126),
    ]
    assert backend.polls == {124: 2, 125: 2, 126: 4}
    assert backend.trace[-1] == ("poll", 126)
    assert [event for event in backend.trace if event[0] == "service"] == [
        ("service", 124),
        ("service", 124),
        ("service", 125),
        ("service", 125),
        ("service", 126),
        ("service", 126),
        ("service", 126),
    ]


@pytest.mark.parametrize(
    "failure",
    [
        ("drain", 1),
        ("announce", 124),
        ("poll", 124),
        ("service", 124),
        ("drain", 2),
        ("drain", 3),
        ("service", 126),
    ],
)
def test_peer_shutdown_protocol_propagates_backend_errors(failure):
    from pokered_harness.link.network_backend import NetworkBackendError

    backend = _ShutdownBackend(fail_at=failure)
    with pytest.raises(NetworkBackendError) as raised:
        _run_fake_shutdown(backend)
    assert raised.value is backend.failure
    assert backend.trace[-1] == failure


def test_peer_shutdown_protocol_missing_done_times_out_without_ticks():
    backend = _ShutdownBackend(missing_done=True)
    with pytest.raises(RuntimeError, match="peer shutdown sync 126 did not converge"):
        _run_fake_shutdown(backend)
    assert backend.drains == 3
    assert backend.marker == 126
    assert 90 <= backend.polls[126] <= 100
    release = backend.trace.index(("announce", 124))
    assert not any(event[0] == "step" for event in backend.trace[release:])
    assert ("service", 126) in backend.trace[release:]


@pytest.mark.parametrize("goal", ["link_menu", "trade", "battle"])
def test_peer_shutdown_protocol_phase_dispatch_preserves_continued_gameplay(goal):
    from tests._tcp_trade_peer import _finish_link_menu_phase, _peer_shutdown_sync

    backend = _ShutdownBackend()
    shutdown_calls = []

    def shutdown(**kwargs):
        shutdown_calls.append(kwargs)
        clock = iter(range(1000))
        _peer_shutdown_sync(
            backend,
            **kwargs,
            cooperative_sync=backend.cooperative_sync,
            step=backend.step,
            backend_snapshot=dict,
            monotonic=lambda: next(clock) * 0.01,
            sleep=lambda _seconds: None,
        )

    _finish_link_menu_phase(
        goal,
        cooperative_sync=backend.cooperative_sync,
        peer_shutdown_sync=shutdown,
    )
    if goal == "link_menu":
        assert shutdown_calls == [{"ready_sync_id": 123, "release_sync_id": 124, "timeout": 10.0}]
        assert backend.trace[-1] == ("poll", 126)
        assert backend.drains == 3
    else:
        assert shutdown_calls == []
        assert backend.trace == [("ready", 123), ("step", 1)]
        backend.step(1)  # The continuation still owns an active emulator.


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


@pytest.mark.parametrize(
    ("encoding", "errors", "fragments", "expected"),
    [
        ("utf-8", "strict", [b"\xe2", b"\x82", b"\xac\r", b"\nend\r"], "€\nend\n"),
        ("utf-8", "replace", [b"prefix", b"\xe2"], "prefix�"),
        ("latin-1", "strict", [b"\xe9\r", b"\n"], "é\n"),
    ],
)
def test_collect_pair_enforces_hard_deadline_without_waiting_for_peers(
    monkeypatch, encoding, errors, fragments, expected
):
    """Capture live partial output without extending the pair deadline."""
    pending = iter([*fragments, b""])
    stream = SimpleNamespace(
        buffer=SimpleNamespace(read1=lambda size: next(pending)),
        encoding=encoding,
        errors=errors,
        close=lambda: None,
    )
    chunks = []
    _drain_stream(stream, chunks)
    assert "".join(chunks) == expected
    chunks = []
    _drain_stream(io.StringIO(expected), chunks)
    assert "".join(chunks) == expected

    markers = {"stdout": "short-live-stdout", "stderr": "short-live-stderr"}
    command = [
        sys.executable,
        "-c",
        (
            "import sys, time; "
            "print('short-live-stdout', end='', flush=True); "
            "print('short-live-stderr', end='', file=sys.stderr, flush=True); "
            "time.sleep(30)"
        ),
    ]
    procs = []
    try:
        readiness_deadline = time.monotonic() + 5.0
        for _ in range(2):
            procs.append(
                subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            )
        listener, connector = procs
        drainers = {proc: _start_pipe_drainers(proc) for proc in procs}
        while time.monotonic() < readiness_deadline:
            if all(
                _captured_text(captured, name) == marker
                for captured, _readers in drainers.values()
                for name, marker in markers.items()
            ):
                break
            time.sleep(0.001)
        # Assert before any collector kill/EOF, using the very same captures
        # returned to the collector. A kill-triggered drain cannot pass this.
        for proc, (captured, readers) in drainers.items():
            assert proc.poll() is None
            assert all(reader.is_alive() for reader in readers)
            for name, marker in markers.items():
                assert _captured_text(captured, name) == marker
        monkeypatch.setattr(
            sys.modules[__name__], "_start_pipe_drainers", lambda proc: drainers[proc]
        )
        started = time.monotonic()
        deadline_at = started + 0.25
        with pytest.raises(_PairDeadlineExceeded, match="hard deadline") as raised:
            _collect_pair(
                listener,
                connector,
                deadline_at=deadline_at,
            )
        elapsed = time.monotonic() - started
        assert elapsed < 2.0
        for marker in markers.values():
            assert str(raised.value).count(marker) == 2
    finally:
        _kill_without_waiting(procs)
        for proc in procs:
            proc.wait(timeout=2.0)
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
