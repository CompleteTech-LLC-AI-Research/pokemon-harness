#!/usr/bin/env python3
"""Audit the statically collected TCP/link acceptance matrix.

This command deliberately performs a pytest *collection* run only.  It proves
that the required ordered version/role cases and strict acceptance entry points
are present in the selected test tree; it does not claim that any ROM-backed
case passed.  Runtime pass/fail evidence still comes from the production gate.

The constants in this module are also the single source of truth used by
``tests._tier_config``.  Keeping the node-ID construction here makes a missing
parameter or a changed pytest ID fail the fast matrix audit instead of being
hidden by an aggregate test count.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path

SUPPORTED_VERSIONS = ("red", "blue", "yellow")
SUPPORTED_VERSION_PAIRS = tuple(
    (left, right) for left in SUPPORTED_VERSIONS for right in SUPPORTED_VERSIONS
)

ROM_VARIANTS = (
    ("red", ("vanilla", "color")),
    ("blue", ("vanilla", "color")),
    ("yellow", ("cgb",)),
)

LOCAL_VERSION_PAIR_TEST = (
    "tests/test_pyboy_link_session_roms.py",
    "test_pair_reaches_link_menu_via_pyboy_link_session",
)
REMOTE_ROLE_PAIR_TEST = (
    "tests/test_link_integration_remote.py",
    "test_remote_handshake_writes_status_on_both_sides",
)
LOCAL_VARIANT_TEST = (
    "tests/test_pyboy_link_session_roms.py",
    "test_same_version_variants_reach_link_menu",
)

# The strict subprocess driver uses the canonical color Red, color Blue, and
# Yellow profiles. The first profile is the listener (internal-clock master)
# and the second is the connector (external-clock slave), so every ordered
# version pair is a distinct acceptance row. Stock Red/Blue remain outside
# this matrix because their vanilla fixture provenance is still partial.
_CANONICAL_PROFILE = {
    "red": "red_color",
    "blue": "blue_color",
    "yellow": "yellow",
}
REMOTE_STRICT_PROFILE_PAIRS = tuple(
    (_CANONICAL_PROFILE[listener], _CANONICAL_PROFILE[connector])
    for listener, connector in SUPPORTED_VERSION_PAIRS
)


def _strict_profile_case_id(listener: str, connector: str) -> str:
    return f"{listener}-listen-{connector}-connect"


def _nodeid(test: tuple[str, str], parameter_id: str | None = None) -> str:
    module, name = test
    suffix = f"[{parameter_id}]" if parameter_id is not None else ""
    return f"{module}::{name}{suffix}"


STRICT_TRADE_NODEIDS = frozenset(
    _nodeid(
        (
            "tests/test_pyboy_link_session_roms.py",
            "test_pair_completes_trade_end_to_end",
        ),
        f"{left}-{right}",
    )
    for left, right in SUPPORTED_VERSION_PAIRS
)
STRICT_TRADE_NODEIDS |= frozenset(
    {
        _nodeid(
            (
                "tests/test_pyboy_link_session_roms.py",
                "test_red_yellow_trade_swaps_real_party_records",
            )
        ),
    }
    | frozenset(
        _nodeid(
            (
                "tests/test_pyboy_link_session_subprocess.py",
                "test_subprocess_pair_completes_trade_over_tcp",
            ),
            _strict_profile_case_id(listener, connector),
        )
        for listener, connector in REMOTE_STRICT_PROFILE_PAIRS
    )
)
STRICT_BATTLE_NODEIDS = frozenset(
    _nodeid(
        (
            "tests/test_pyboy_link_session_roms.py",
            "test_pair_completes_battle_turn",
        ),
        f"{left}-{right}",
    )
    for left, right in SUPPORTED_VERSION_PAIRS
)
STRICT_BATTLE_NODEIDS |= frozenset(
    {
        _nodeid(
            (
                "tests/test_pyboy_link_session_roms.py",
                "test_red_yellow_battle_turn_is_resolved",
            )
        ),
    }
    | frozenset(
        _nodeid(
            (
                "tests/test_pyboy_link_session_subprocess.py",
                "test_subprocess_pair_resolves_battle_turn_over_tcp",
            ),
            _strict_profile_case_id(listener, connector),
        )
        for listener, connector in REMOTE_STRICT_PROFILE_PAIRS
    )
)


LOCAL_VERSION_PAIR_NODEIDS = frozenset(
    _nodeid(LOCAL_VERSION_PAIR_TEST, f"{left}-{right}") for left, right in SUPPORTED_VERSION_PAIRS
)

REMOTE_VERSION_PAIR_NODEIDS = frozenset(
    _nodeid(REMOTE_ROLE_PAIR_TEST, f"{listener}-{connector}")
    for listener, connector in SUPPORTED_VERSION_PAIRS
)

LOCAL_VARIANT_NODEIDS = frozenset(
    _nodeid(LOCAL_VARIANT_TEST, f"{version}-{variant_a}-x-{variant_b}")
    for version, variants in ROM_VARIANTS
    for variant_a in variants
    for variant_b in variants
)

REMOTE_REVERSED_ROLE_NODEIDS = frozenset(
    _nodeid(REMOTE_ROLE_PAIR_TEST, f"{listener}-{connector}")
    for listener, connector in SUPPORTED_VERSION_PAIRS
    if listener != connector
)

# These are the strict entry points currently implemented by the repository.
# Their presence in collection is auditable here; their ROM behavior remains a
# separate runtime question and is intentionally not represented as PASS.
STRICT_ACCEPTANCE_NODEIDS = {
    "trade": STRICT_TRADE_NODEIDS,
    "battle": STRICT_BATTLE_NODEIDS,
}

# The strict set contains a dedicated end-to-end entry point for every
# canonical Red/Blue/Yellow version ordering on both transports. Runtime
# execution still has to prove each row; collection alone is never a pass.
STRICT_ACCEPTANCE_CASES = {
    "trade": frozenset(("local", left, right) for left, right in SUPPORTED_VERSION_PAIRS)
    | frozenset(("remote", left, right) for left, right in SUPPORTED_VERSION_PAIRS),
    "battle": frozenset(("local", left, right) for left, right in SUPPORTED_VERSION_PAIRS)
    | frozenset(("remote", left, right) for left, right in SUPPORTED_VERSION_PAIRS),
}

# These labels describe the required current-candidate runtime work, not a
# collection-time pass. The gate must execute and retain evidence for every
# ordered row before any label can be promoted to certified.
REMOTE_LINK_MENU_CASE_CLASSIFICATIONS = {
    pair: "strict-trade-battle-runtime-pending" for pair in SUPPORTED_VERSION_PAIRS
}

PYTEST_COLLECTION_ARGUMENTS = (
    "--strict-config",
    "--strict-markers",
    "-p",
    "pytest_asyncio.plugin",
    "-p",
    "tests._gate_report",
)
_COLLECTION_REPORT_FIELDS = (
    "total",
    "passed",
    "failed",
    "skipped",
    "xfailed",
    "xpassed",
    "errors",
)
COLLECTION_CLEANUP_TIMEOUT_SECONDS = 5.0


def acceptance_matrix_classifications() -> dict[str, dict[tuple[str, str, str], str]]:
    """Classify declaration coverage without treating it as runtime evidence."""
    all_cases = {
        (transport, left, right)
        for transport in ("local", "remote")
        for left, right in SUPPORTED_VERSION_PAIRS
    }
    return {
        operation: {
            case: ("declared" if case in cases else "unverified-no-strict-entrypoint")
            for case in sorted(all_cases)
        }
        for operation, cases in STRICT_ACCEPTANCE_CASES.items()
    }


def _json_case_classifications() -> dict[str, dict[str, str]]:
    """Return acceptance classifications with JSON-safe case keys."""
    return {
        operation: {_case_label(case): status for case, status in classifications.items()}
        for operation, classifications in acceptance_matrix_classifications().items()
    }


def _json_remote_link_menu_classifications() -> dict[str, str]:
    """Return remote role classifications with JSON-safe case keys."""
    return {
        f"listener={listener} connector={connector}": status
        for (listener, connector), status in REMOTE_LINK_MENU_CASE_CLASSIFICATIONS.items()
    }


def _json_strict_profile_pairs() -> list[dict[str, str]]:
    """Return the exact profile/role pairs behind strict remote rows."""
    return [
        {
            "listener": listener,
            "connector": connector,
            "trade": "declared",
            "battle": "declared",
        }
        for listener, connector in REMOTE_STRICT_PROFILE_PAIRS
    ]


def required_matrix_nodeids() -> dict[str, frozenset[str]]:
    """Return node IDs required by each production-gate tier.

    The MCP and subprocess smoke nodes are part of the remote tier but are not
    version-pair matrix rows.  They are included here so the gate manifest and
    the standalone audit cannot drift apart.
    """

    return {
        "local": LOCAL_VERSION_PAIR_NODEIDS | LOCAL_VARIANT_NODEIDS,
        "remote": REMOTE_VERSION_PAIR_NODEIDS
        | frozenset(
            {
                _nodeid(
                    (
                        "tests/test_mcp_real_link.py",
                        "test_mcp_remote_link_attaches_native_serial_backend",
                    )
                ),
                _nodeid(
                    (
                        "tests/test_pyboy_link_session_subprocess.py",
                        "test_subprocess_pair_reaches_link_menu_over_tcp",
                    )
                ),
            }
        ),
        "trade": STRICT_TRADE_NODEIDS,
        "battle": STRICT_BATTLE_NODEIDS,
    }


def _normalize_nodeid(nodeid: str) -> str:
    path, separator, test_name = nodeid.partition("::")
    normalized_path = path.replace("\\", "/")
    while normalized_path.startswith("./"):
        normalized_path = normalized_path[2:]
    return f"{normalized_path}::{test_name}" if separator else normalized_path


def _missing(expected: Iterable[str], actual: set[str]) -> tuple[str, ...]:
    return tuple(sorted(set(expected) - actual))


def _acceptance_gaps_for(
    declared_cases: dict[str, frozenset[tuple[str, str, str]]],
) -> dict[str, tuple[tuple[str, str, str], ...]]:
    expected = frozenset(
        (transport, left, right)
        for transport in ("local", "remote")
        for left, right in SUPPORTED_VERSION_PAIRS
    )
    return {
        operation: tuple(sorted(expected - cases)) for operation, cases in declared_cases.items()
    }


def acceptance_matrix_gaps() -> dict[str, tuple[tuple[str, str, str], ...]]:
    """Return the current prospective full-matrix gap report.

    This is a declaration-level gap report, not runtime evidence.  A missing
    case means the current strict acceptance manifest has no dedicated test
    for that transport/version ordering.
    """
    return _acceptance_gaps_for(STRICT_ACCEPTANCE_CASES)


def acceptance_declaration_gaps() -> dict[str, tuple[tuple[str, str, str], ...]]:
    """Return current strict rows without a dedicated acceptance entry point."""
    return acceptance_matrix_gaps()


def audit_collection(
    nodeids: Iterable[str],
    *,
    collection_errors: Iterable[str] = (),
    collection_skips: Iterable[str] = (),
) -> dict[str, object]:
    """Audit collected node IDs without executing any test body."""

    normalized = tuple(_normalize_nodeid(nodeid) for nodeid in nodeids)
    actual = set(normalized)
    duplicate_nodeids = tuple(
        sorted({nodeid for nodeid in normalized if normalized.count(nodeid) > 1})
    )
    groups = {
        name: {
            "expected": len(expected),
            "present": len(set(expected) & actual),
            "missing": _missing(expected, actual),
        }
        for name, expected in (
            ("local-version-pairs", LOCAL_VERSION_PAIR_NODEIDS),
            ("remote-role-pairs", REMOTE_VERSION_PAIR_NODEIDS),
            ("remote-reversed-roles", REMOTE_REVERSED_ROLE_NODEIDS),
            ("local-rom-variants", LOCAL_VARIANT_NODEIDS),
            ("strict-trade-entrypoints", STRICT_TRADE_NODEIDS),
            ("strict-battle-entrypoints", STRICT_BATTLE_NODEIDS),
        )
    }
    errors = tuple(str(error) for error in collection_errors)
    skips = tuple(str(skip) for skip in collection_skips)
    structural_pass = not (
        errors or skips or duplicate_nodeids or any(group["missing"] for group in groups.values())
    )
    return {
        "structural_pass": structural_pass,
        "acceptance_matrix_complete": not any(acceptance_declaration_gaps().values()),
        "collected": len(normalized),
        "duplicate_nodeids": duplicate_nodeids,
        "collection_errors": errors,
        "collection_skips": skips,
        "groups": groups,
        "acceptance_gaps": acceptance_declaration_gaps(),
        "acceptance_classifications": _json_case_classifications(),
        "remote_link_menu_classifications": _json_remote_link_menu_classifications(),
        "remote_strict_profile_pairs": _json_strict_profile_pairs(),
        "runtime": "not-run",
    }


def _resolve_python(value: Path, project_root: Path) -> Path:
    if value.is_absolute():
        return value
    return project_root / value


def _collection_environment(project_root: Path) -> dict[str, str]:
    """Make standalone collection auditing independent of ambient pytest state."""

    environment = dict(os.environ)
    for key in tuple(environment):
        if key.startswith("PYTEST_") or key in {
            "POKERED_GATE_REPORT",
            "POKERED_GATE_PROGRESS_REPORT",
            "POKERED_SKIP_SHA1",
        }:
            environment.pop(key, None)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    environment["PYBOY_NO_CYTHON"] = "1"

    entries = [
        str((project_root / "vendor" / "pyboy-src").resolve(strict=False)),
        str((project_root / "src").resolve(strict=False)),
        str(project_root),
    ]
    old_pythonpath = os.environ.get("PYTHONPATH")
    if old_pythonpath:
        entries.extend(item for item in old_pythonpath.split(os.pathsep) if item)
    environment["PYTHONPATH"] = os.pathsep.join(entries)
    return environment


def _process_creation_kwargs() -> dict[str, object]:
    """Return process-group options for the standalone collection child."""

    if os.name == "posix":
        return {"start_new_session": True}
    return {
        "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    }


def _terminate_collection_process(
    process: subprocess.Popen[str],
    *,
    deadline: float | None = None,
) -> bool:
    """Terminate and reap a collection child and its process group."""

    pid = getattr(process, "pid", None)
    if pid is None:
        return False
    cleanup_deadline = deadline
    if cleanup_deadline is None:
        cleanup_deadline = time.monotonic() + COLLECTION_CLEANUP_TIMEOUT_SECONDS

    def wait_for_exit() -> bool:
        if process.poll() is not None:
            return True
        remaining = cleanup_deadline - time.monotonic()
        if remaining <= 0:
            return process.poll() is not None
        try:
            process.wait(timeout=remaining)
        except (OSError, subprocess.TimeoutExpired):
            return process.poll() is not None
        return process.poll() is not None

    if os.name == "posix":
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            try:
                process.terminate()
            except OSError:
                pass
    else:
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    if not wait_for_exit():
        if os.name == "posix":
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                try:
                    process.kill()
                except OSError:
                    pass
        else:
            remaining = cleanup_deadline - time.monotonic()
            if remaining > 0:
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=remaining,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
            try:
                process.kill()
            except OSError:
                pass

    reaped = wait_for_exit()
    stream = getattr(process, "stdout", None)
    if stream is not None:
        try:
            stream.close()
        except (OSError, ValueError):
            pass
    return reaped


def _text_output(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    if isinstance(value, str):
        return value
    return "" if value is None else str(value)


def _collection_report_details(
    payload: object,
    *,
    returncode: int,
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Validate the gate-plugin collection report before auditing its nodes.

    Returning errors and skips separately lets :func:`audit_collection` keep
    its structural diagnostics, while malformed accounting is still a hard
    failure instead of being reduced to a plausible node list.
    """

    errors: list[str] = []
    skips: list[str] = []
    problems: list[str] = []
    if not isinstance(payload, dict):
        return [], [], [], ["collection report root is not an object"]

    exitstatus = payload.get("exitstatus")
    if isinstance(exitstatus, bool) or not isinstance(exitstatus, int):
        problems.append("collection report has an invalid exitstatus")
    elif exitstatus != returncode:
        problems.append(
            "collection report exitstatus does not match pytest return code: "
            f"{exitstatus} != {returncode}"
        )

    if payload.get("collection_only") is not True:
        problems.append("collection report is not marked collection_only")

    raw_counts = payload.get("counts")
    if not isinstance(raw_counts, dict):
        problems.append("collection report has no counts object")
    else:
        counts: dict[str, int] = {}
        for field in _COLLECTION_REPORT_FIELDS:
            value = raw_counts.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                problems.append(f"collection report count {field!r} is invalid")
            else:
                counts[field] = value
        if counts.get("total") not in (None, 0):
            problems.append("collection-only report contains test outcomes")
        for field in ("passed", "failed", "skipped", "xfailed", "xpassed"):
            if counts.get(field, 0) != 0:
                problems.append(f"collection-only report has nonzero {field} outcome count")

    records = payload.get("tests")
    if records != []:
        problems.append("collection-only report unexpectedly contains test records")

    raw_nodeids = payload.get("nodeids")
    if not isinstance(raw_nodeids, list) or any(
        not isinstance(nodeid, str) or not nodeid for nodeid in raw_nodeids
    ):
        problems.append("collection report nodeids is invalid")
        nodeids: list[str] = []
    else:
        nodeids = list(raw_nodeids)
        if len(set(nodeids)) != len(nodeids):
            problems.append("collection report contains duplicate nodeids")

    collected = payload.get("collected")
    if isinstance(collected, bool) or not isinstance(collected, int) or collected < 0:
        problems.append("collection report collected count is invalid")
    elif collected != len(nodeids):
        problems.append(
            "collection report count does not match nodeids: "
            f"collected={collected} nodeids={len(nodeids)}"
        )

    def read_collection_entries(name: str) -> list[str]:
        raw_entries = payload.get(name)
        if not isinstance(raw_entries, list):
            problems.append(f"collection report {name} is not a list")
            return []
        result: list[str] = []
        for entry in raw_entries:
            if not isinstance(entry, dict):
                problems.append(f"collection report {name} contains a non-object entry")
                continue
            nodeid = entry.get("nodeid")
            reason = entry.get("reason")
            if not isinstance(nodeid, str) or not isinstance(reason, str):
                problems.append(f"collection report {name} contains an invalid entry")
                continue
            result.append(f"{nodeid}: {reason}")
        return result

    errors = read_collection_entries("collection_errors")
    skips = read_collection_entries("collection_skips")
    if isinstance(raw_counts, dict):
        declared_errors = raw_counts.get("errors")
        if isinstance(declared_errors, int) and declared_errors != len(errors):
            problems.append(
                "collection report error count does not match collection_errors: "
                f"errors={declared_errors} entries={len(errors)}"
            )
    if returncode != 0:
        problems.append(f"pytest return code: {returncode}")
    return nodeids, errors, skips, problems


def collect_nodeids(
    project_root: Path,
    python_executable: Path,
    *,
    timeout_seconds: float = 300.0,
) -> tuple[dict[str, object], list[str]]:
    """Run the gate plugin's collection-only report in a temporary directory."""

    command = [
        str(python_executable),
        "-m",
        "pytest",
        "tests",
        "--collect-only",
        "-q",
        *PYTEST_COLLECTION_ARGUMENTS,
    ]
    with tempfile.TemporaryDirectory(prefix="pokered-matrix-") as directory:
        report_path = Path(directory) / "collection.json"
        environment = _collection_environment(project_root)
        environment["POKERED_GATE_REPORT"] = str(report_path)
        try:
            process = subprocess.Popen(
                command,
                cwd=project_root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                **_process_creation_kwargs(),
            )
            _, _ = process.communicate(timeout=timeout_seconds)
            raw_returncode = process.returncode
            returncode = int(raw_returncode) if raw_returncode is not None else 125
        except OSError as exc:
            audit = audit_collection(
                (), collection_errors=(f"collection command: {type(exc).__name__}: {exc}",)
            )
            return audit, command
        except subprocess.TimeoutExpired as exc:
            cleanup_ok = _terminate_collection_process(process)
            partial_output = _text_output(exc.output)
            timeout_error = f"collection command timed out after {timeout_seconds:.1f}s" + (
                "; collection process did not terminate after cleanup" if not cleanup_ok else ""
            )
            if partial_output:
                timeout_error += f"; output={partial_output[-4000:]}"
            audit = audit_collection((), collection_errors=(timeout_error,))
            return audit, command

        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            audit = audit_collection(
                (),
                collection_errors=(
                    f"collection report: {type(exc).__name__}: {exc}",
                    f"pytest return code: {returncode}",
                ),
            )
            return audit, command

        nodeids, error_text, skip_text, report_problems = _collection_report_details(
            payload,
            returncode=returncode,
        )
        error_text.extend(report_problems)
        return audit_collection(
            (str(nodeid) for nodeid in nodeids),
            collection_errors=error_text,
            collection_skips=skip_text,
        ), command


def _case_label(case: tuple[str, str, str]) -> str:
    transport, left, right = case
    role = "listener" if transport == "remote" else "left"
    peer_role = "connector" if transport == "remote" else "right"
    return f"{transport} {role}={left} {peer_role}={right}"


def render_text(audit: dict[str, object], command: Iterable[str]) -> str:
    lines = [
        "Pokémon TCP/link acceptance-matrix audit",
        "scope: pytest collection only; ROM behavior is NOT RUN",
        f"collection: collected={audit['collected']} structural={'PASS' if audit['structural_pass'] else 'FAIL'}",
        f"acceptance declaration: {'PASS' if audit['acceptance_matrix_complete'] else 'FAIL'}",
    ]
    if command:
        lines.append(f"command: {' '.join(command)}")
    for name, group in audit["groups"].items():
        status = "PASS" if not group["missing"] else "FAIL"
        lines.append(f"  {status:4} {name}: {group['present']}/{group['expected']} collected")
        for nodeid in group["missing"]:
            lines.append(f"    missing: {nodeid}")
    for name, entries in (
        ("collection-errors", audit["collection_errors"]),
        ("collection-skips", audit["collection_skips"]),
        ("duplicate-nodeids", audit["duplicate_nodeids"]),
    ):
        if entries:
            lines.append(f"{name}:")
            lines.extend(f"  - {entry}" for entry in entries)

    lines.append("strict acceptance declaration gaps (not runtime results):")
    for operation, cases in audit["acceptance_gaps"].items():
        lines.append(f"  {operation}: {len(cases)} unverified cases lack a strict entry point")
        lines.extend(f"    - {_case_label(case)}" for case in cases)
    lines.append("remote ordered role classifications (runtime evidence recorded separately):")
    for case, status in sorted(audit["remote_link_menu_classifications"].items()):
        lines.append(f"  {status}: {case}")
    lines.append("strict remote profile/role declarations (runtime pending):")
    for pair in audit["remote_strict_profile_pairs"]:
        lines.append(
            f"  trade={pair['trade']} battle={pair['battle']}: "
            f"listener={pair['listener']} connector={pair['connector']}"
        )
    lines.append("runtime: NOT RUN")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
    )
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.timeout_seconds <= 0:
        raise SystemExit("--timeout-seconds must be positive")
    project_root = args.repo_root.expanduser().resolve()
    python_executable = _resolve_python(args.python.expanduser(), project_root)
    audit, command = collect_nodeids(
        project_root,
        python_executable,
        timeout_seconds=args.timeout_seconds,
    )
    if args.format == "json":
        print(json.dumps(audit, indent=2, sort_keys=True))
    else:
        print(render_text(audit, command))
    return 0 if audit["structural_pass"] and audit["acceptance_matrix_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
