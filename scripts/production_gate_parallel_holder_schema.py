"""Strict terminal instance accounting for the authenticated per-job holder."""

from collections.abc import Mapping
from typing import Any


def instance(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and type(value[0]) is int
        and value[0] > 0
        and type(value[1]) is int
        and value[1] >= 0
    )


def terminal_schema_problems(body: Mapping[str, Any]) -> list[str]:
    """Reject incomplete accounting even when the producer authenticates its own claim.

    ECHILD proves exhausted child waiting. It cannot reconstruct a missing start identity,
    omitted receipt field, or unmatched adoption/reaping record.
    """

    expected = {
        "contained": True,
        "proof": True,
        "subreaper_start": True,
        "subreaper_end": True,
        "incomplete": False,
        "echild": True,
    }
    if any(body.get(key) is not value for key, value in expected.items()):
        return ["the holder sweep was not a completed, exhausted, fully reaped one"]
    if any(type(body.get(key)) is not bool for key in ("never_started", "guardian_killed")):
        return ["holder terminal receipt omits a required boolean field"]
    groups: dict[str, set[tuple[int, int]]] = {}
    for key in ("forks", "adopted", "killed", "reaped", "reaped_unheld", "unknown", "remaining"):
        values = body.get(key)
        if not isinstance(values, list) or any(not instance(value) for value in values):
            return [f"holder terminal {key} is missing or contains an invalid instance"]
        pairs = {tuple(value) for value in values}
        if len(pairs) != len(values):
            return [f"holder terminal {key} contains duplicate instances"]
        groups[key] = pairs
    if any(groups[key] for key in ("reaped_unheld", "unknown", "remaining")):
        return ["holder terminal contains unidentified reaping, unknown or remaining instances"]
    guardian, status = body.get("guardian"), body.get("guardian_status")
    if body["never_started"]:
        if (
            guardian is not None
            or status is not None
            or any(groups.values())
            or body["guardian_killed"]
        ):
            return ["never-started holder receipt contradicts its fork or instance accounting"]
        return []
    if not isinstance(guardian, dict) or not instance([guardian.get("pid"), guardian.get("start")]):
        return ["holder terminal lacks a valid guardian instance"]
    guardian_pair = (guardian["pid"], guardian["start"])
    if type(status) is not int or groups["forks"] != {guardian_pair}:
        return ["holder terminal guardian exit or fork accounting is incomplete"]
    if guardian_pair in groups["adopted"]:
        return ["holder terminal accounts for its guardian as an adopted orphan"]
    if groups["reaped"] != groups["adopted"] or not groups["killed"].issubset(groups["adopted"]):
        return ["holder terminal adoption, signal and reaping records do not reconcile"]
    return []
