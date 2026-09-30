"""Capacity report rendering and explicit outcome helpers.

Split from ``scripts/gate_capacity.py`` for issue #122 with no behavior
change: the functions below are copied verbatim from that module and are
re-exported by ``scripts.gate_capacity``.
"""

from __future__ import annotations

import hashlib
from typing import Any

from scripts.gate_capacity_policy import (
    _PRESSURE_METRIC,
    CAPACITY_OUTCOMES,
    LIFECYCLE_STATES,
    NOT_APPLICABLE,
    CapacityPolicy,
)


def overall_outcome(
    *,
    availability: str = "ok",
    admitted: int = 0,
    failed: int = 0,
    collection_failures: int = 0,
    interrupted: int = 0,
    not_started: int = 0,
) -> str:
    """Aggregate a capacity outcome; zero admitted tests can never be ``ok``."""

    if failed > 0:
        return "failed"
    if admitted <= 0:
        return "blocked"
    if availability != "ok":
        return availability if availability in CAPACITY_OUTCOMES else "blocked"
    if collection_failures > 0:
        return "unsupported"
    if interrupted > 0 or not_started > 0:
        return "blocked"
    return "ok"


def unavailable_capacity(reason: str) -> dict[str, Any]:
    """Return the explicit marker recorded when no policy is supplied."""

    return {
        "status": "unavailable",
        "capacity_policy": "unavailable",
        "availability": {"status": "unavailable", "reasons": [reason]},
        "admission": {"max_concurrent_pairs": 0, "admitted": 0, "active": [], "queued": []},
        "lifecycle": {state: 0 for state in LIFECYCLE_STATES},
        "samples": [],
        "collection_failures": 0,
    }


def not_applicable_capacity(policy: CapacityPolicy, reason: str) -> dict[str, Any]:
    """Return an explicit marker for an asset-free selection that is not gated."""

    return {
        "status": NOT_APPLICABLE,
        "capacity_policy": policy.runner_id,
        "policy": policy.as_dict(),
        "pressure_metric": _PRESSURE_METRIC,
        "availability": {"status": NOT_APPLICABLE, "reasons": [reason]},
        "capability_assumptions": [],
        "admission": {
            "max_concurrent_pairs": policy.max_concurrent_pairs,
            "admitted": 0,
            "active": [],
            "queued": [],
            "decisions": [],
        },
        "lifecycle": {state: 0 for state in LIFECYCLE_STATES},
        "collection_failures": 0,
        "samples": [],
        "sample_reference": {
            "algorithm": "sha256",
            "sha256": hashlib.sha256(b"[]").hexdigest(),
            "sample_count": 0,
            "collection_failures": 0,
            "first_monotonic_seconds": None,
            "last_monotonic_seconds": None,
        },
    }


def render_capacity_text(payload: dict[str, Any]) -> str:
    """Render the capacity section for human-readable gate reports."""

    lines = [f"capacity-policy: {payload.get('status', 'unavailable')}"]
    policy = payload.get("policy")
    if isinstance(policy, dict):
        lines.append(
            "  policy: "
            f"version={policy.get('policy_version')} "
            f"runner={policy.get('runner_id')} "
            f"effective_cpus={policy.get('effective_cpus')} "
            f"max_concurrent_pairs={policy.get('max_concurrent_pairs')}"
        )
        lines.append(
            "  thresholds: "
            f"memory_bytes_min={policy.get('memory_bytes_min')} "
            f"disk_free_bytes_min={policy.get('disk_free_bytes_min')} "
            f"observation_seconds={policy.get('observation_seconds')} "
            f"admission_deadline_seconds={policy.get('admission_deadline_seconds')}"
        )
    availability = payload.get("availability")
    if isinstance(availability, dict):
        lines.append(f"  availability: {availability.get('status', 'unknown')}")
        for reason in availability.get("reasons", []):
            lines.append(f"    reason: {reason}")
    assumptions = payload.get("capability_assumptions") or []
    if assumptions:
        lines.append("  capability-assumptions: " + ", ".join(str(item) for item in assumptions))
    admission = payload.get("admission")
    if isinstance(admission, dict):
        lines.append(
            "  admission: "
            f"max_concurrent_pairs={admission.get('max_concurrent_pairs')} "
            f"admitted={admission.get('admitted')} "
            f"active={admission.get('active')} "
            f"queued={admission.get('queued')}"
        )
    lifecycle = payload.get("lifecycle")
    if isinstance(lifecycle, dict):
        lines.append(
            "  lifecycle: "
            + " ".join(f"{state}={lifecycle.get(state, 0)}" for state in LIFECYCLE_STATES)
        )
    if payload.get("collection_failures"):
        lines.append(f"  collection-failures: {payload['collection_failures']}")
    reference = payload.get("sample_reference")
    if isinstance(reference, dict):
        lines.append(
            "  sample-reference: "
            f"sha256={reference.get('sha256')} samples={reference.get('sample_count')}"
        )
    return "\n".join(lines)
