"""Phase barrier for the two additional early smokes (#107.4, #107.6).

The sequential gate runs both runtimes' additional smokes before any long tier and a
smoke failure never hides the other runtime's smoke.  Concurrent jobs keep that
contract: every job of a phase reaches a terminal state before the next phase is
considered, a failed smoke never cancels its sibling, and ``fail_fast`` only decides
whether the selected-tier phase starts afterwards.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

SMOKE_PHASE = "smoke"
SELECTED_PHASE = "selected"
PHASE_ORDER = (SMOKE_PHASE, SELECTED_PHASE)


def phase_names(plan_body: Mapping[str, Any]) -> list[str]:
    present = {job["phase"] for job in plan_body["jobs"]}
    return [phase for phase in PHASE_ORDER if phase in present]


def jobs_in_phase(plan_body: Mapping[str, Any], phase: str) -> list[dict[str, Any]]:
    return [job for job in plan_body["jobs"] if job["phase"] == phase]


def may_cancel_siblings(phase: str, fail_fast: bool) -> bool:
    """A failed smoke never cancels the other smoke; selected-tier failures may."""

    return bool(fail_fast) and phase != SMOKE_PHASE


def barrier_decision(finished: Sequence[Mapping[str, Any]], *, fail_fast: bool) -> tuple[bool, str]:
    """Return (start the next phase, reason) once every job of the prior phase is terminal."""

    failed = [item for item in finished if item.get("state") != "passed"]
    if not failed:
        return True, ""
    names = ", ".join(f"{item.get('job_id')}:{item.get('state')}" for item in failed)
    if fail_fast:
        return False, f"smoke barrier failed ({names}); fail_fast stops the selected tiers"
    return True, ""
