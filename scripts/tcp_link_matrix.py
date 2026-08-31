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
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path

SUPPORTED_VERSIONS = ("red", "blue", "yellow")
SUPPORTED_VERSION_PAIRS = tuple(
    (left, right)
    for left in SUPPORTED_VERSIONS
    for right in SUPPORTED_VERSIONS
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

# Only these profile-specific remote rows have current, native-serial
# end-to-end trade and battle evidence.  The first profile is the listener
# (internal-clock master); the second is the connector (external-clock slave).
# The color suffix is intentional: the vanilla profiles use a different ROM
# menu contract and are not silently promoted by the version-level matrix.
REMOTE_STRICT_PROFILE_PAIRS = (
    ("red_color", "blue_color"),
    ("blue_color", "red_color"),
)


def _strict_profile_case_id(listener: str, connector: str) -> str:
    return f"{listener}-listen-{connector}-connect"


def _nodeid(test: tuple[str, str], parameter_id: str | None = None) -> str:
    module, name = test
    suffix = f"[{parameter_id}]" if parameter_id is not None else ""
    return f"{module}::{name}{suffix}"

STRICT_TRADE_NODEIDS = frozenset(
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
    _nodeid(LOCAL_VERSION_PAIR_TEST, f"{left}-{right}")
    for left, right in SUPPORTED_VERSION_PAIRS
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

# The strict set contains only current end-to-end evidence.  Every other
# logical Red/Blue/Yellow pair remains explicitly unverified below; diagnostic
# LinkMenu rows are never promoted to strict trade/battle acceptance.
STRICT_ACCEPTANCE_CASES = {
    "trade": frozenset(
        {
            ("local", "red", "yellow"),
            ("remote", "red", "blue"),
            ("remote", "blue", "red"),
        }
    ),
    "battle": frozenset(
        {
            ("local", "red", "yellow"),
            ("remote", "red", "blue"),
            ("remote", "blue", "red"),
        }
    ),
}

# The remote version-level tests cover the complete ordered 3x3 handshake
# matrix.  These are the current LinkMenu/strict results at the exact
# candidate boundary.  The strict color Red/Blue rows prove the two Red/Blue
# directions through trade and battle.  The other Red-involving rows are not
# exercised by the broad diagnostic because that test explicitly excludes its
# Red walk path; Yellow->Blue has a reproducible pre-LinkMenu failure; the
# remaining Blue/Yellow rows reach LinkMenu.  This is a classification, not a
# runtime claim made by collection alone.
REMOTE_LINK_MENU_CASE_CLASSIFICATIONS = {
    ("red", "red"): "diagnostic-driver-excluded-red-walk",
    ("red", "blue"): "strict-trade-battle-certified-color-profiles",
    ("blue", "red"): "strict-trade-battle-certified-color-profiles",
    ("red", "yellow"): "diagnostic-driver-excluded-red-walk",
    ("yellow", "red"): "diagnostic-driver-excluded-red-walk",
    ("blue", "blue"): "link-menu-certified",
    ("blue", "yellow"): "link-menu-certified",
    ("yellow", "blue"): "runtime-failed-before-link-menu",
    ("yellow", "yellow"): "link-menu-certified",
}


def acceptance_matrix_classifications() -> dict[
    str, dict[tuple[str, str, str], str]
]:
    """Classify every strict candidate row without treating gaps as green."""
    all_cases = {
        (transport, left, right)
        for transport in ("local", "remote")
        for left, right in SUPPORTED_VERSION_PAIRS
    }
    return {
        operation: {
            case: (
                "certified"
                if case in cases
                else "unverified-no-strict-entrypoint"
            )
            for case in sorted(all_cases)
        }
        for operation, cases in STRICT_ACCEPTANCE_CASES.items()
    }


def _json_case_classifications() -> dict[str, dict[str, str]]:
    """Return acceptance classifications with JSON-safe case keys."""
    return {
        operation: {
            _case_label(case): status for case, status in classifications.items()
        }
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
            "trade": "certified",
            "battle": "certified",
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
    return (
        f"{normalized_path}::{test_name}"
        if separator
        else normalized_path
    )


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
        operation: tuple(sorted(expected - cases))
        for operation, cases in declared_cases.items()
    }


def acceptance_matrix_gaps() -> dict[str, tuple[tuple[str, str, str], ...]]:
    """Return the current prospective full-matrix gap report.

    This is a declaration-level gap report, not runtime evidence.  A missing
    case means the current strict acceptance manifest has no dedicated test
    for that transport/version ordering.
    """
    return _acceptance_gaps_for(STRICT_ACCEPTANCE_CASES)


def acceptance_declaration_gaps() -> dict[
    str, tuple[tuple[str, str, str], ...]
]:
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
        errors
        or skips
        or duplicate_nodeids
        or any(group["missing"] for group in groups.values())
    )
    return {
        "structural_pass": structural_pass,
        "acceptance_matrix_complete": not any(
            acceptance_declaration_gaps().values()
        ),
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
        "-p",
        "tests._gate_report",
    ]
    with tempfile.TemporaryDirectory(prefix="pokered-matrix-") as directory:
        report_path = Path(directory) / "collection.json"
        environment = dict(os.environ)
        environment["POKERED_GATE_REPORT"] = str(report_path)
        try:
            completed = subprocess.run(
                command,
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            audit = audit_collection(
                (), collection_errors=(f"collection command: {type(exc).__name__}: {exc}",)
            )
            return audit, command

        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            audit = audit_collection(
                (),
                collection_errors=(
                    f"collection report: {type(exc).__name__}: {exc}",
                    f"pytest return code: {completed.returncode}",
                ),
            )
            return audit, command

        if not isinstance(payload, dict):
            audit = audit_collection(
                (), collection_errors=("collection report root is not an object",)
            )
            return audit, command

        raw_nodeids = payload.get("nodeids", [])
        nodeids = raw_nodeids if isinstance(raw_nodeids, list) else []
        raw_collected = payload.get("collected")
        errors = payload.get("collection_errors", [])
        skips = payload.get("collection_skips", [])
        error_text = [
            str(entry.get("reason", entry))
            if isinstance(entry, dict)
            else str(entry)
            for entry in (errors if isinstance(errors, list) else [errors])
        ]
        skip_text = [
            str(entry.get("reason", entry))
            if isinstance(entry, dict)
            else str(entry)
            for entry in (skips if isinstance(skips, list) else [skips])
        ]
        if completed.returncode != 0:
            error_text.append(f"pytest return code: {completed.returncode}")
        if payload.get("collection_only") is not True:
            error_text.append("collection report is not marked collection_only")
        if isinstance(raw_collected, bool) or not isinstance(raw_collected, int):
            error_text.append("collection report has an invalid collected count")
        elif raw_collected != len(nodeids):
            error_text.append(
                "collection report count does not match nodeids: "
                f"collected={raw_collected} nodeids={len(nodeids)}"
            )
        records = payload.get("tests")
        if payload.get("collection_only") is True and records != []:
            error_text.append(
                "collection-only report unexpectedly contains test outcomes"
            )
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
        lines.append(
            f"  {status:4} {name}: "
            f"{group['present']}/{group['expected']} collected"
        )
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
        lines.append(
            f"  {operation}: {len(cases)} unverified cases lack a strict entry point"
        )
        lines.extend(f"    - {_case_label(case)}" for case in cases)
    lines.append("remote ordered role classifications (runtime evidence recorded separately):")
    for case, status in sorted(
        audit["remote_link_menu_classifications"].items()
    ):
        lines.append(f"  {status}: {case}")
    lines.append("strict remote profile/role evidence rows:")
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
