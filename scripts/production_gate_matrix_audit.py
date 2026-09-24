"""Strict matrix collection audit split out of ``production_gate_matrix``.

Moved verbatim for the #86 capacity admission feature (#115) so the matrix module
stays inside the repository's 1000-line file split bound (#122/#124).  Calls to
facade-owned, monkeypatch-patched entry points resolve through ``_entry``.
"""

from __future__ import annotations

import runpy
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Direct names this module calls; cyclic back-edges go through _entry.
from scripts.production_gate_model import (
    TIER_DESCRIPTIONS,
    TIER_EXPRESSIONS,
    CollectionResult,
    Counts,
    TierResult,
    _normalize_nodeid,
)


def run_matrix_collection_audit(
    *,
    project_root: Path,
    collections: Iterable[CollectionResult],
) -> dict[str, Any]:
    """Apply the repository matrix auditor to the gate's collection result."""

    collection_list = list(collections)
    matrix_path = project_root / "scripts" / "tcp_link_matrix.py"
    try:
        namespace = runpy.run_path(str(matrix_path))
        audit_collection = namespace["audit_collection"]
        audited_nodeids = _audited_matrix_nodeids(namespace)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        return {
            "status": "FAIL",
            "structural_pass": False,
            "acceptance_matrix_complete": False,
            "collected": 0,
            "groups": {},
            "acceptance_gaps": {},
            "runtime": "not-run",
            "reason": f"matrix auditor could not be loaded: {type(exc).__name__}: {exc}",
        }

    nodeids = collection_list[0].nodeids if collection_list else ()
    errors = tuple(
        collection.reason
        for collection in collection_list
        if collection.status != "PASS" and collection.reason
    )
    skips = tuple(
        collection.reason
        for collection in collection_list
        if collection.status == "PASS" and collection.reason
    )
    try:
        audit = audit_collection(
            nodeids,
            collection_errors=errors,
            collection_skips=skips,
        )
    except (TypeError, ValueError) as exc:
        return {
            "status": "FAIL",
            "structural_pass": False,
            "acceptance_matrix_complete": False,
            "collected": len(nodeids),
            "groups": {},
            "acceptance_gaps": {},
            "runtime": "not-run",
            "reason": f"matrix audit failed: {type(exc).__name__}: {exc}",
        }
    audit["status"] = (
        "PASS"
        if audit.get("structural_pass") and audit.get("acceptance_matrix_complete")
        else "FAIL"
    )
    audit["audited_nodeids"] = audited_nodeids
    return audit


def synthetic_optional_skip(
    name: str,
    reason: str,
) -> TierResult:
    return TierResult(
        name=name,
        description=TIER_DESCRIPTIONS[name],
        expression=TIER_EXPRESSIONS[name],
        required=False,
        status="SKIP",
        counts=Counts(total=1, skipped=1),
        skip_reasons={reason: 1},
        reason=reason,
    )


def _test_key_from_nodeid(nodeid: str) -> tuple[str, str] | None:
    if "::" not in nodeid:
        return None
    path, test_name = nodeid.split("::", 1)
    return Path(path).name, test_name.split("[", 1)[0]


def _audited_matrix_nodeids(namespace: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    """Return the exact strict rows used by the collection auditor."""

    raw = namespace.get("STRICT_ACCEPTANCE_NODEIDS")
    if not isinstance(raw, dict):
        raise TypeError("matrix auditor has no strict acceptance node IDs")

    result: dict[str, tuple[str, ...]] = {}
    for name in ("trade", "battle"):
        values = raw.get(name)
        if not isinstance(values, (set, frozenset, tuple, list)):
            raise TypeError(f"matrix auditor has no strict {name} node IDs")
        if any(not isinstance(nodeid, str) or "::" not in nodeid for nodeid in values):
            raise ValueError(f"matrix auditor has invalid strict {name} node ID")
        normalized = tuple(sorted({_normalize_nodeid(nodeid) for nodeid in values}))
        if not normalized:
            raise ValueError(f"matrix auditor has no strict {name} node IDs")
        result[name] = normalized
    return result


def _required_test_problems(
    nodeids: Iterable[str],
    required_test_keys: Iterable[tuple[str, str]],
) -> list[str]:
    actual = {key for nodeid in nodeids if (key := _test_key_from_nodeid(nodeid)) is not None}
    missing = sorted(set(required_test_keys) - actual)
    return [
        f"required acceptance test is absent from selected items: {module}::{name}"
        for module, name in missing
    ]


def _required_nodeid_problems(
    nodeids: Iterable[str],
    required_nodeids: Iterable[str],
) -> list[str]:
    actual = {_normalize_nodeid(nodeid) for nodeid in nodeids}
    missing = sorted({_normalize_nodeid(nodeid) for nodeid in required_nodeids} - actual)
    return [f"required matrix case is absent from selected items: {nodeid}" for nodeid in missing]
