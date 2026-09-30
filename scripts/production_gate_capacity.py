"""Capacity policy session plumbing, completion sinks and interrupt finalisation.

Added with the #86 capacity admission feature (#115): the new symbols live in
their own module so ``production_gate_tiers`` and ``production_gate_matrix`` stay
inside the repository's 1000-line file split bound (#122/#124).  Facade-owned,
monkeypatch-patched entry points resolve through ``_entry``.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import math
import runpy
import signal
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

try:
    from scripts import gate_capacity
except ImportError:  # pragma: no cover - executed as ``python scripts/production_gate.py``.
    import gate_capacity  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Direct names this module calls; cyclic back-edges go through _entry.
from scripts.production_gate_model import (
    REQUIRED_TIER_ASSETS,
    SMOKE_REQUIRED_NODEIDS,
    SMOKE_SELECTORS,
    TIER_EXPRESSIONS,
    CollectionResult,
    PreparedRuntimeGate,
    RuntimeGateResult,
    TierResult,
    _normalize_nodeid,
)


@functools.cache
def _tier_row_manifest(project_root: str) -> tuple[Any | None, Any | None, str]:
    """Resolve the repository's own classifier and tier membership rule.

    The classifier is the same manifest the collection auditor uses, so the
    planned row set for capacity accounting cannot drift from the marker
    expression the gate dispatches.  It is loaded once per project root.
    """

    config_path = Path(project_root) / "tests" / "_tier_config.py"
    if not config_path.is_file():
        return None, None, f"tier configuration is missing: {config_path}"
    try:
        namespace = runpy.run_path(str(config_path))
        classify = namespace["classify_test"]
        matches = namespace["tier_matches"]
    except Exception as exc:  # noqa: BLE001 - fail closed on any config-load error
        return None, None, f"tier configuration could not be loaded: {type(exc).__name__}: {exc}"
    if not callable(classify) or not callable(matches):
        return None, None, "tier configuration has no classify_test/tier_matches callables"
    return classify, matches, ""


def planned_tier_nodeids(
    collections: Iterable[CollectionResult],
    name: str,
    project_root: Path,
) -> tuple[tuple[str, ...], str]:
    """Return the exact rows one tier will dispatch, from its own collection.

    Required-nodeid manifests cover only the strict acceptance rows, while an
    ordinary real-ROM tier dispatches additional emulator rows.  Capacity
    accounting has to cover the whole planned set, so it is derived from the
    preflight collection with the same classifier the collection audit trusts.
    The second element is an explicit problem string; an empty row set with no
    problem means the tier genuinely selects nothing.
    """

    if name not in TIER_EXPRESSIONS:
        return (), f"unknown tier {name!r} has no planned-row rule"
    classifier, matches, error = _tier_row_manifest(str(project_root))
    if classifier is None or error:
        return (), error or "tier configuration is unavailable"
    nodeids = collections[0].nodeids if collections else ()
    selectors = SMOKE_SELECTORS if name == "smoke" else ()
    planned: set[str] = set()
    for nodeid in nodeids:
        if selectors and not nodeid.startswith(selectors):
            continue
        key = _entry._test_key_from_nodeid(nodeid)
        if key is None:
            continue
        try:
            markers = classifier(key[0], key[1])
        except ValueError:
            # An unclassified module is already a collection/config problem;
            # it must not silently join this tier's planned row set.
            continue
        if matches(name, markers):
            planned.add(_normalize_nodeid(nodeid))
    return tuple(sorted(planned)), ""


def register_blocked_rows(
    capacity_session: Any | None,
    *,
    name: str,
    collections: Iterable[CollectionResult],
    project_root: Path | None,
    reason: str,
    required_nodeids: Iterable[str] = (),
    status: str = "BLOCKED",
    scope: str = "",
) -> tuple[str, ...]:
    """Record every planned row a tier will never dispatch.

    A tier rejected before dispatch produces BLOCKED rows but, without an
    explicit decision, those rows vanish from admission and lifecycle
    accounting.  The planned set is derived from the tier's own collection so
    the recorded rows match what dispatch would have run, and the strict
    required manifest is unioned in so a collection that could not enumerate a
    required row still accounts for it.  Returns the recorded node IDs.

    Registration is idempotent per ``(scope, tier, nodeid)``: a cancellation
    that lands part-way through a multirow registration must be finalized by
    transitioning the rows it already recorded, never by allocating a second
    admission identity for the same planned row.
    """

    if capacity_session is None or name not in REQUIRED_TIER_ASSETS:
        return ()
    rows: set[str] = set()
    problem = ""
    if project_root is not None:
        planned, problem = planned_tier_nodeids(collections, name, project_root)
        rows.update(planned)
    rows.update(_normalize_nodeid(nodeid) for nodeid in required_nodeids)
    if name == "smoke":
        rows.update(SMOKE_REQUIRED_NODEIDS)
    detail = reason or "tier was not dispatched"
    if problem:
        detail = f"{detail}; planned rows could not be fully enumerated: {problem}"
    recorded: list[str] = []
    # A BLOCKED row never ran because declared capacity refused it, so its
    # terminal decision is a block; a row that merely never dispatched (or was
    # cancelled) is terminalized as an abandonment instead, and its lifecycle
    # state reflects the interruption.
    lifecycle_state = "interrupted" if status == "INTERRUPTED" else "not_started"

    def record(pair_id: str) -> bool:
        try:
            if status == "BLOCKED":
                capacity_session.admission.block(pair_id, detail)
            else:
                capacity_session.admission.abandon(pair_id, detail)
            capacity_session.telemetry.mark(pair_id, lifecycle_state)
        except Exception:  # noqa: BLE001 - accounting must never mask the block.
            return False
        return True

    for nodeid in sorted(rows):
        if record(capacity_session.blocked_row_id(name, nodeid, scope=scope)):
            recorded.append(nodeid)
    return tuple(recorded)


class TierInterruptedDuringCleanup(KeyboardInterrupt):
    """Cancellation raised while tearing a *completed* tier down.

    ``run_prepared_tier`` keeps its execution work and its observer/scratch
    cleanup in one call, so a ``SIGINT`` delivered during cleanup used to
    escape with the finished :class:`TierResult` unreachable.  The orchestrator
    then replaced the executed tier with an empty ``INTERRUPTED`` row, dropping
    the loaded outcomes and double-registering the same planned rows.

    This subclass still *is* a ``KeyboardInterrupt`` so every existing
    cancellation handler keeps treating it as cancellation, but it also carries
    the completed tier so the orchestrator can retain the real evidence and
    record the cancellation separately.
    """

    def __init__(self, tier: TierResult, reason: str) -> None:
        super().__init__(reason)
        self.tier = tier
        self.reason = reason


class _DeferredSigintGuard:
    """Defer a SIGINT delivered while final evidence is being assembled.

    ``run_matrix_tier`` loads every child report, count, and diagnostic into
    local containers, then assembles the returned :class:`TierResult`.  A
    cancellation delivered inside that short, in-memory window used to escape
    with the evidence unreachable, so the orchestrator replaced an executed
    failure with an empty ``INTERRUPTED`` row.  The window performs no I/O and
    no cleanup, so the signal is recorded and re-raised as a typed cancellation
    once the complete tier result exists.  Dispatch and teardown keep their
    ordinary immediate cancellation behavior.
    """

    def __init__(self) -> None:
        self.triggered = False
        self._previous: Any | None = None
        self._installed = False
        self._restored = False

    def _handler(self, _signum: int, _frame: Any) -> None:
        self.triggered = True

    def install(self) -> None:
        try:
            self._previous = signal.signal(signal.SIGINT, self._handler)
        except (ValueError, OSError):  # pragma: no cover - not the main thread.
            self._previous = None
            return
        self._installed = True

    def restore(self) -> bool:
        if self._installed and not self._restored:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signal.SIGINT, self._previous)
        self._restored = True
        return self.triggered


def _defer_sigint_while_finalizing() -> _DeferredSigintGuard:
    """Install the finalization guard; the caller must call ``restore()``."""

    guard = _DeferredSigintGuard()
    guard.install()
    return guard


def _capacity_execution(function):
    """Hold the capacity observation window open for every real-ROM tier.

    Admission is per emulator pair and lives in the tier supervisors, so a
    declared policy stops the *next* pair instead of holding one slot for a
    whole batch.  This wrapper only keeps the observer running across the
    entire tier, including the pre-dispatch blocked-row accounting.
    """

    @functools.wraps(function)
    def execute(**kwargs):
        session = kwargs.get("capacity_session")
        name = kwargs["name"]
        if session is None or name not in REQUIRED_TIER_ASSETS:
            return function(**kwargs)
        tier: TierResult | None = None
        try:
            with session.monitoring():
                tier = function(**kwargs)
        except KeyboardInterrupt:
            # A cancellation during execution leaves ``tier`` unset, so it keeps
            # the ordinary "unstarted" semantics.  One raised only by the
            # observer's shutdown happens after the tier was fully loaded: carry
            # it out so the evidence is not discarded.
            if tier is None:
                raise
            raise TierInterruptedDuringCleanup(
                tier,
                f"{name} completed but its capacity observer shutdown was interrupted",
            ) from None
        return tier

    return execute


def _recover_completed_tier(prepared: PreparedRuntimeGate, name: str) -> TierResult | None:
    """Return the durable evidence for an already-executed tier, if any.

    A cancellation can land between a tier finishing and its result being
    appended to ``prepared.result.tiers``.  Every such handoff consults this
    mapping so the executed counts, diagnostics, output, and lifecycle rows are
    preserved instead of being replaced by an empty ``INTERRUPTED`` row.
    """

    return prepared.result.completed_tiers.get(name)


def _completed_tier_sink(prepared: PreparedRuntimeGate) -> Callable[[TierResult], None]:
    """Return the durable publish sink for tiers executed under ``prepared``.

    The sink writes into the runtime's ``completed_tiers`` mapping, which the
    cancellation and finalization paths already consult.  Handing the mapping -
    rather than a per-call local sink - to the tier supervisors lets them
    publish a fully assembled tier while signal deferral is still installed, so
    a ``SIGINT`` delivered on the supervisor's own return path cannot erase it.
    """

    def publish(tier: TierResult) -> None:
        prepared.result.completed_tiers[tier.name] = tier

    return publish


def _record_completed_tier(
    prepared: PreparedRuntimeGate,
    tier: TierResult,
    reason: str,
) -> str:
    """Append an executed tier and mark the run cancelled; return the reason."""

    if not any(existing is tier for existing in prepared.result.tiers):
        prepared.result.tiers.append(tier)
    prepared.result.cancellation = reason
    return reason


def _dispatch_prepared_tier(
    *,
    prepared: PreparedRuntimeGate,
    name: str,
    python_executable: Path,
    arguments: dict[str, Any],
) -> tuple[TierResult, str]:
    """Run one prepared tier and retain its evidence across a cancellation.

    Returns the tier the orchestrator must record plus a sanitized cancellation
    reason (``""`` when the call completed normally).  The tier is produced by
    ``_entry.run_prepared_tier``, which records the completed result on the runtime
    before it returns; a ``KeyboardInterrupt`` delivered between that return
    and this frame's bookkeeping therefore still finds the executed evidence,
    instead of being indistinguishable from a tier that never ran.  The same
    mapping is consulted by every caller, because a signal can also land
    between this frame's return and the caller's append.  A cancellation that
    really arrived before the tier produced anything still propagates so the
    caller records an unstarted ``INTERRUPTED`` row.
    """

    try:
        tier = _entry.run_prepared_tier(
            prepared=prepared,
            name=name,
            python_executable=python_executable,
            **arguments,
        )
    except TierInterruptedDuringCleanup as interrupted:
        return interrupted.tier, interrupted.reason
    except KeyboardInterrupt:
        completed = _recover_completed_tier(prepared, name)
        if completed is not None:
            reason = (
                f"{prepared.result.mode} {name} completed but the run was cancelled "
                "before its result was recorded"
            )
            return completed, reason
        raise
    return tier, ""


def _capacity_tier_reason(capacity_session: Any | None, name: str) -> str:
    """Return a BLOCKED reason when declared capacity cannot admit ``name``.

    Only real-ROM tiers that spawn emulator pairs are capacity-gated; the
    ROM-free unit/timing tiers are unaffected.  When no policy is supplied the
    existing behavior is preserved exactly.
    """

    if capacity_session is None or name not in REQUIRED_TIER_ASSETS:
        return ""
    try:
        status = capacity_session.wait_until_available()
    except Exception as exc:  # noqa: BLE001 - fail closed, never crash.
        return f"capacity unsupported: {type(exc).__name__}: {exc}"
    if status == "ok":
        return ""
    return capacity_session.capacity_reason()


def _finalize_interrupted_results(
    *,
    accumulated: Sequence[RuntimeGateResult],
    modes: Sequence[str],
    plan: dict[str, Any],
    reason: str,
    required_nodeids_by_tier: dict[str, frozenset[str]],
    capacity_session: Any | None,
    project_root: Path,
) -> tuple[RuntimeGateResult, ...]:
    """Complete an interrupted run without erasing accumulated rows.

    A cancellation delivered while the outer plan is dispatching (for example
    during ``_entry._unrun_tier`` accounting for a row already stopped by fail-fast)
    used to be rebuilt from scratch by the CLI fallback, replacing every
    completed runtime and tier with blank interrupted rows.  Instead, every
    result already produced is kept exactly as it completed, the cancellation
    reason is recorded on it, and only genuinely unfinished rows are finalized
    as ``INTERRUPTED`` using the existing plan and row identities.
    """
    planned_by_mode: dict[str, list[str]] = {}
    for step in plan["steps"]:
        planned_by_mode.setdefault(step["mode"], []).append(step["tier"])
    present = {result.mode: result for result in accumulated}
    finalized: list[RuntimeGateResult] = []
    for mode in modes:
        result = present.get(mode)
        if result is None:
            result = _entry._unstarted_preparation(mode, reason, interrupted=True).result
            result.tiers = []
        result.execution_plan = plan
        if not result.cancellation:
            result.cancellation = reason
        existing = {tier.name for tier in result.tiers}
        for name in planned_by_mode.get(mode, []):
            if name in existing:
                continue
            completed = result.completed_tiers.get(name)
            if completed is not None:
                # The tier finished and its evidence was loaded, but the
                # cancellation landed before the caller could append it.  Keep
                # the executed tier - counts, diagnostics, output, rows - and
                # let the run carry the cancellation instead of replacing real
                # work with a blank INTERRUPTED row.
                result.tiers.append(completed)
                existing.add(name)
                continue
            # Reuse the admission generation the plan already opened for this
            # (runtime, tier) pair so finalizing a partially registered row
            # transitions its existing identity instead of adding a second one.
            result.tiers.append(
                _entry._unrun_tier(
                    name,
                    reason,
                    required_nodeids_by_tier.get(name, ()),
                    status="INTERRUPTED",
                    capacity_session=capacity_session,
                    collections=result.collections,
                    project_root=project_root,
                    scope=mode,
                )
            )
        finalized.append(result)
    return tuple(finalized)


def _capacity_text_lines(capacity: dict[str, Any] | None) -> list[str]:
    if not capacity:
        return ["capacity-policy: unavailable"]
    return gate_capacity.render_capacity_text(capacity).splitlines()


MAX_CAPACITY_SAMPLES = 256


CAPACITY_STREAM_FILENAME = "capacity-samples.json"


def _safe_capacity_sample(
    sample: Any,
    roots: tuple[tuple[str, Path], ...],
    *,
    replacements: tuple[tuple[str, str], ...] | None = None,
) -> dict[str, Any]:
    if not isinstance(sample, dict):
        return {"status": "invalid"}
    facts = sample.get("facts")
    safe_facts: dict[str, Any] = {}
    if isinstance(facts, dict):
        for key, value in facts.items():
            if value is None or isinstance(value, (bool, int, float)):
                safe_facts[key] = (
                    None if isinstance(value, float) and not math.isfinite(value) else value
                )
            elif isinstance(value, list):
                safe_facts[key] = [
                    _entry._safe_diagnostic(item, roots, replacements=replacements, limit=500)
                    for item in value
                ]
            else:
                safe_facts[key] = _entry._safe_diagnostic(
                    value, roots, replacements=replacements, limit=500
                )
    problems = sample.get("problems")
    return {
        "sequence": sample.get("sequence"),
        "monotonic_seconds": (
            sample.get("monotonic_seconds")
            if isinstance(sample.get("monotonic_seconds"), (int, float))
            and math.isfinite(sample["monotonic_seconds"])
            else None
        ),
        "utc_timestamp": sample.get("utc_timestamp")
        if isinstance(sample.get("utc_timestamp"), str)
        else None,
        "status": sample.get("status"),
        "problems": [
            _entry._safe_diagnostic(item, roots, replacements=replacements, limit=1000)
            for item in (problems if isinstance(problems, list) else [])
        ],
        "facts": safe_facts,
    }


def _safe_capacity(
    capacity: dict[str, Any] | None,
    roots: tuple[tuple[str, Path], ...] = (),
    *,
    replacements: tuple[tuple[str, str], ...] | None = None,
) -> dict[str, Any]:
    """Sanitize one capacity section without retaining machine-local paths."""

    if not isinstance(capacity, dict):
        return gate_capacity.unavailable_capacity("no capacity policy supplied")
    status = capacity.get("status")
    result: dict[str, Any] = {"status": status if isinstance(status, str) else "unavailable"}
    if isinstance(capacity.get("capacity_policy"), str):
        result["capacity_policy"] = capacity["capacity_policy"]
    policy = capacity.get("policy")
    if isinstance(policy, dict):
        result["policy"] = {
            key: value
            for key, value in policy.items()
            if value is None or isinstance(value, (str, int, float, bool))
        }
    if isinstance(capacity.get("pressure_metric"), str):
        result["pressure_metric"] = capacity["pressure_metric"]
    availability = capacity.get("availability")
    if isinstance(availability, dict):
        result["availability"] = {
            "status": availability.get("status"),
            "reasons": [
                _entry._safe_diagnostic(item, roots, replacements=replacements, limit=2000)
                for item in (
                    availability.get("reasons")
                    if isinstance(availability.get("reasons"), list)
                    else []
                )
            ],
        }
    assumptions = capacity.get("capability_assumptions")
    result["capability_assumptions"] = [
        _entry._safe_diagnostic(item, roots, replacements=replacements, limit=1000)
        for item in (assumptions if isinstance(assumptions, list) else [])
    ]
    admission = capacity.get("admission")
    if isinstance(admission, dict):
        decisions = []
        for decision in admission.get("decisions", []):
            if not isinstance(decision, dict):
                continue
            decisions.append(
                {
                    "pair_id": _entry._safe_diagnostic(
                        decision.get("pair_id"), roots, replacements=replacements, limit=500
                    ),
                    "status": decision.get("status"),
                    "reason": _entry._safe_diagnostic(
                        decision.get("reason"), roots, replacements=replacements, limit=1000
                    ),
                    "sequence": decision.get("sequence"),
                    "virtual_seconds": decision.get("virtual_seconds"),
                    "owner": bool(decision.get("owner")),
                    "promoted": bool(decision.get("promoted")),
                }
            )
        result["admission"] = {
            "max_concurrent_pairs": admission.get("max_concurrent_pairs"),
            "admitted": admission.get("admitted"),
            "active": [
                _entry._safe_diagnostic(item, roots, replacements=replacements, limit=500)
                for item in (admission.get("active") or [])
            ],
            "queued": [
                _entry._safe_diagnostic(item, roots, replacements=replacements, limit=500)
                for item in (admission.get("queued") or [])
            ],
            "decisions": decisions,
        }
    lifecycle = capacity.get("lifecycle")
    if isinstance(lifecycle, dict):
        result["lifecycle"] = {
            state: lifecycle.get(state, 0) for state in gate_capacity.LIFECYCLE_STATES
        }
    if isinstance(capacity.get("collection_failures"), int):
        result["collection_failures"] = capacity["collection_failures"]
    samples = capacity.get("samples")
    if isinstance(samples, list):
        complete = [
            _safe_capacity_sample(sample, roots, replacements=replacements) for sample in samples
        ]
        encoded = (json.dumps(complete, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
        result["samples"] = complete[:MAX_CAPACITY_SAMPLES]
        result["samples_omitted"] = max(0, len(complete) - MAX_CAPACITY_SAMPLES)
        result["sample_stream"] = complete
        result["sample_reference"] = {
            "algorithm": "sha256",
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "sample_count": len(complete),
            "size": len(encoded),
            "path": CAPACITY_STREAM_FILENAME,
        }
    return result


# Call-time indirection so facade-level monkeypatches stay visible here.
import scripts.production_gate as _entry
