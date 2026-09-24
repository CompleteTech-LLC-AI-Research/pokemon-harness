"""Versioned capacity policy, bounded admission, and low-overhead telemetry.

The production gate runs emulator pairs whose deadlines are sensitive to CPU
scheduling.  This module adds a declared, operator-owned capacity policy and a
deterministic slot controller that decides whether more work may be admitted.
It exists so that a host without the declared capacity is reported as
``blocked`` (or ``unsupported``) instead of being confused with a product test
failure.

Design rules:

* The policy is a versioned JSON document.  Every operating threshold is
  *declared* by the operator; nothing here derives a threshold from the
  exploratory #84 PSI/load condition.
* Host facts are collected once per observation by reusing
  :func:`scripts.qualification_runner.collect_facts`.  An unsupported or
  unavailable field is reported as ``unsupported``; it is never treated as a
  passing observation.
* System and cgroup CPU PSI ``some`` and load use measured, declared limits.
  ``full=0`` is never used as proof of available capacity.
* The admission controller has an injectable clock, never pauses, reprioritizes,
  or enlarges the deadline of an active owner, and never reinterprets a failed
  test as a pass.
* Telemetry collection failure is recorded explicitly and can never crash
  cleanup or erase an original failure.
"""

from __future__ import annotations

import sys as _sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

# Register the canonical name first so a split module's ``import
# scripts.gate_capacity`` resolves to this same object instead of a second
# copy.
_sys.modules.setdefault("scripts.gate_capacity", _sys.modules[__name__])


# The split modules' names are re-exported verbatim so the module surface
# (and every ``gate_capacity.<name>`` monkeypatch target) is unchanged.
from scripts.gate_capacity_admission import (
    AdmissionDecision,
    CapacityAdmission,
    CapacitySession,
    TelemetryRecorder,
)
from scripts.gate_capacity_policy import (  # noqa: F401
    _PRESSURE_METRIC,
    _REQUIRED_FACT_KEYS,
    ADMISSION_STATUSES,
    CAPACITY_OUTCOMES,
    LIFECYCLE_STATES,
    NOT_APPLICABLE,
    POLICY_SCHEMA_VERSION,
    CapacityPolicy,
    CapacitySample,
    _is_non_negative_int,
    _is_number,
    _load_qualification_runner,
    _utc_now,
    capacity_policy_from_dict,
    evaluate_capacity,
    facts_to_dict,
    load_capacity_policy,
    qualification_runner,
    sample_facts,
    validate_facts,
    validate_policy,
)
from scripts.gate_capacity_report import (
    not_applicable_capacity,
    overall_outcome,
    render_capacity_text,
    unavailable_capacity,
)

__all__ = [
    "ADMISSION_STATUSES",
    "CAPACITY_OUTCOMES",
    "LIFECYCLE_STATES",
    "NOT_APPLICABLE",
    "POLICY_SCHEMA_VERSION",
    "AdmissionDecision",
    "CapacityAdmission",
    "CapacityPolicy",
    "CapacitySample",
    "CapacitySession",
    "TelemetryRecorder",
    "capacity_policy_from_dict",
    "evaluate_capacity",
    "facts_to_dict",
    "load_capacity_policy",
    "not_applicable_capacity",
    "overall_outcome",
    "render_capacity_text",
    "sample_facts",
    "unavailable_capacity",
    "validate_facts",
    "validate_policy",
]
