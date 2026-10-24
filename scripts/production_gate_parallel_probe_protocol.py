"""Root-side authentication and semantic checks for admitted probe-stage frames.

These checks do not establish kernel ownership. The future owner must independently
pin/validate peer and parent instances before calling registration checks or ACKing,
and retain those handles until exit and reaping obligations are resolved. This
module is not connected to measurements and grants no execution permission.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import signal
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from scripts.production_gate_parallel_holder_schema import instance
from scripts.production_gate_parallel_validation import loads_strict

MAX_PROBE_FRAME = 65536
MAX_PROBE_INSTANCES = 256
STAGE_ROLES = frozenset({"holder", "guardian", "payload"})
STAGE_PHASES = frozenset({"ready", "register", "final", "unproven"})


class ProbeProtocolError(ValueError):
    """Unauthenticated, unbound, malformed or contradictory probe evidence."""


@dataclass(frozen=True)
class ProbeContext:
    scope: str
    nonce: str
    secret: bytes
    source_sha256: str
    program_sha256: str
    job_deadline: float
    cleanup_deadline: float


def canonical_probe_frame(body: Mapping[str, Any]) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def decode_probe_frame(raw: bytes, context: ProbeContext) -> dict[str, Any]:
    if not raw or len(raw) > MAX_PROBE_FRAME:
        raise ProbeProtocolError("missing or oversized probe frame")
    try:
        body = loads_strict(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ProbeProtocolError("probe frame is not strict JSON") from error
    if not isinstance(body, dict):
        raise ProbeProtocolError("probe frame is not an object")
    tag = body.pop("mac", None)
    try:
        expected_mac = hmac.new(
            context.secret, canonical_probe_frame(body), hashlib.sha256
        ).hexdigest()
    except (TypeError, ValueError) as error:
        raise ProbeProtocolError("probe frame contains noncanonical values") from error
    if (
        not isinstance(tag, str)
        or len(tag) != 64
        or any(value not in "0123456789abcdef" for value in tag)
        or not hmac.compare_digest(tag, expected_mac)
    ):
        raise ProbeProtocolError("probe frame authentication failed")
    for name in ("scope", "nonce", "source_sha256", "program_sha256"):
        if body.get(name) != getattr(context, name):
            raise ProbeProtocolError(f"probe frame {name} binding differs")
    if (
        not isinstance(body.get("role"), str)
        or not isinstance(body.get("phase"), str)
        or body["role"] not in STAGE_ROLES
        or body["phase"] not in STAGE_PHASES
    ):
        raise ProbeProtocolError("probe stage role/phase differs")
    if not instance([body.get("pid"), body.get("start")]):
        raise ProbeProtocolError("probe frame instance is invalid")
    if type(body.get("sequence")) is not int or body["sequence"] < 1:
        raise ProbeProtocolError("probe sequence is invalid")
    return body


def registration_problems(
    body: Mapping[str, Any],
    context: ProbeContext,
    *,
    peer_instance: tuple[int, int],
    parent_instance: tuple[int, int],
    previous_sequence: int,
    cleanup_bound: float,
) -> list[str]:
    """Inputs are independently kernel-verified held instances, not sender assertions."""

    problems = []
    if (
        body.get("phase") != "register"
        or type(body.get("sequence")) is not int
        or body["sequence"] <= previous_sequence
    ):
        problems.append("registration phase/sequence is missing or replayed")
    if (body.get("pid"), body.get("start")) != peer_instance:
        problems.append("registration differs from the pinned peer")
    if (body.get("parent_pid"), body.get("parent_start")) != parent_instance:
        problems.append("registration differs from the pinned parent")
    expected_guard = signal.SIGKILL if body.get("role") == "payload" else signal.SIGTERM
    if body.get("guard") != expected_guard or body.get("subreaper") is not (
        body.get("role") != "payload"
    ):
        problems.append("registration guard/adoption readback differs")
    for name in ("job_deadline", "cleanup_deadline"):
        value = body.get(name)
        if type(value) not in (int, float) or not math.isfinite(value):
            problems.append(f"registration {name} is unknown")
        elif value != getattr(context, name):
            problems.append(f"registration {name} differs from the original budget")
    if not math.isfinite(cleanup_bound) or context.cleanup_deadline > cleanup_bound:
        problems.append("registration would outlive the tightened cleanup bound")
    return problems


def terminal_problems(
    body: Mapping[str, Any],
    *,
    stage_instance: tuple[int, int],
    child_instance: tuple[int, int] | None,
    previous_sequence: int,
) -> list[str]:
    """Authentication precedes this check; a final/PASS alone is never cleanup proof."""

    if body.get("phase") != "final" or body.get("role") not in ("holder", "guardian"):
        return ["required supervisor final is missing"]
    if (body.get("pid"), body.get("start")) != stage_instance:
        return ["supervisor final instance differs"]
    if type(body.get("sequence")) is not int or body["sequence"] <= previous_sequence:
        return ["supervisor final was replayed"]
    required = ("physical_empty", "echild", "subreaper_start", "subreaper_end", "proof")
    if any(body.get(name) is not True for name in required):
        return ["supervisor did not prove an exhausted cleanup"]
    if body.get("custody_errors") != [] or body.get("remaining") != []:
        return ["supervisor retains unknown/error/remaining custody"]
    groups: dict[str, set[tuple[int, int]]] = {}
    for name in ("forks", "adopted", "reaped", "killed"):
        values = body.get(name)
        if (
            not isinstance(values, list)
            or len(values) > MAX_PROBE_INSTANCES
            or any(not instance(value) for value in values)
        ):
            return [f"supervisor {name} accounting is invalid"]
        groups[name] = {tuple(value) for value in values}
        if len(groups[name]) != len(values):
            return [f"supervisor {name} accounting contains duplicates"]
    expected_forks = {child_instance} if child_instance is not None else set()
    if groups["forks"] != expected_forks or groups["forks"] & groups["adopted"]:
        return ["supervisor fork/adoption differs from independently registered child"]
    all_children = groups["forks"] | groups["adopted"]
    if groups["reaped"] != all_children or not groups["killed"].issubset(all_children):
        return ["supervisor signal/reaping accounting does not reconcile"]
    if (
        type(body.get("success")) is not bool
        or type(body.get("timely_cleanup")) is not bool
        or not isinstance(body.get("operation_errors"), list)
    ):
        return ["supervisor operation disposition is missing"]
    status = body.get("child_status")
    if (child_instance is None and status is not None) or (
        child_instance is not None and type(status) is not int
    ):
        return ["supervisor child status contradicts fork accounting"]
    if body["success"] and (status != 0 or body["operation_errors"] or not body["timely_cleanup"]):
        return ["supervisor success contradicts its original failure evidence"]
    return []
