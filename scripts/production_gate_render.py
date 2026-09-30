"""Human-readable gate and evidence report rendering.

Split from ``scripts/production_gate.py`` for issue #124 with no behavior
change: the code below is copied verbatim except that calls to facade-owned,
monkeypatch-patched entry points resolve through ``_entry`` so attribute patches
on the loaded gate module stay visible.
"""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Direct names this module calls; cyclic back-edges go through _entry.
from scripts.production_gate_capacity import _capacity_text_lines
from scripts.production_gate_model import (
    AssetRecord,
    CollectionResult,
    RuntimeGateResult,
    TierResult,
    _execution_plan_lines,
)
from scripts.production_gate_runtime_gates import runtime_gate_passes
from scripts.production_gate_text import (
    _failure_detail_lines,
    _format_counts,
    _jsonable_tier,
    _payload_counts_text,
)


def render_text(
    *,
    project_root: Path,
    rom_root: Path,
    fixture_root: Path,
    runtime: dict[str, Any],
    assets: list[AssetRecord],
    collections: list[CollectionResult],
    tiers: list[TierResult],
    gate_problems: list[str],
    overall: str,
    fixture_manifest: dict[str, Any] | None = None,
    matrix_audit: dict[str, Any] | None = None,
    execution_plan: dict[str, Any] | None = None,
    capacity: dict[str, Any] | None = None,
    cancellation: str = "",
) -> str:
    lines = [
        "Pokémon harness production gate",
        f"project={project_root}",
        f"rom_root={rom_root}",
        f"fixture_root={fixture_root}",
        "environment:",
    ]
    for key in (
        "python_executable",
        "python_version",
        "pytest_version",
        "pyboy_version",
        "pyboy_revision",
        "pyboy_kind",
        "pyboy_module",
        "serial_module",
        "serial_contract",
        "harness_module",
    ):
        if key in runtime:
            lines.append(f"  {key}={runtime[key]}")
    if runtime.get("probe_error"):
        lines.append(f"  probe_error={runtime['probe_error']}")

    lines.append("gate-policy:")
    if gate_problems:
        lines.extend(f"  FAIL: {problem}" for problem in gate_problems)
    else:
        lines.append("  PASS")
    if cancellation:
        lines.append("cancellation:")
        lines.append(f"  {cancellation}")

    lines.append("collection:")
    for collection in collections:
        command = " ".join(collection.command)
        lines.append(
            f"  {collection.status:4} {collection.name}: "
            f"returncode={collection.returncode} duration={collection.duration_seconds:.1f}s"
        )
        lines.append(f"    command: {command}")
        if collection.reason:
            lines.append(f"    reason: {collection.reason}")
        if collection.status == "FAIL" and collection.output_tail:
            lines.append("    output tail:")
            lines.extend(f"      {line}" for line in collection.output_tail.splitlines()[-60:])

    lines.append("assets:")
    for asset in assets:
        suffix = []
        if asset.expected_sha1:
            suffix.append(f"expected_sha1={asset.expected_sha1}")
        if asset.actual_sha1:
            suffix.append(f"actual_sha1={asset.actual_sha1}")
        if asset.size is not None:
            suffix.append(f"size={asset.size}")
        detail = " " + " ".join(suffix) if suffix else ""
        lines.append(
            f"  {asset.status.upper():13} {asset.kind:7} {asset.label}: {asset.path}{detail}"
        )

    if fixture_manifest is not None:
        lines.append("fixture-manifest:")
        lines.append(
            f"  {fixture_manifest.get('status', 'UNKNOWN')}: "
            f"mode={fixture_manifest.get('mode', 'unknown')} "
            f"entries={fixture_manifest.get('entries')} "
            f"returncode={fixture_manifest.get('returncode')}"
        )
        if fixture_manifest.get("reason"):
            lines.append(f"  reason: {fixture_manifest['reason']}")

    if matrix_audit is not None:
        lines.append("matrix-audit:")
        lines.append(
            f"  {matrix_audit.get('status', 'UNKNOWN')}: "
            f"collected={matrix_audit.get('collected', 0)} "
            f"structural={'PASS' if matrix_audit.get('structural_pass') else 'FAIL'} "
            f"acceptance-declaration="
            f"{'PASS' if matrix_audit.get('acceptance_matrix_complete') else 'FAIL'}"
        )
        groups = matrix_audit.get("groups", {})
        if isinstance(groups, dict):
            for name, group in groups.items():
                if isinstance(group, dict):
                    lines.append(
                        f"  {name}: {group.get('present', 0)}/{group.get('expected', 0)} collected"
                    )
        if matrix_audit.get("reason"):
            lines.append(f"  reason: {matrix_audit['reason']}")

    if execution_plan:
        lines.extend(_execution_plan_lines(execution_plan))
    lines.append("tiers:")
    for tier in tiers:
        lines.append(
            f"  {tier.status:8} {tier.name:7} {_format_counts(tier.counts)} "
            f"duration={tier.duration_seconds:.1f}s"
        )
        if tier.reason:
            lines.append(f"    reason: {tier.reason}")
        for reason, count in tier.skip_reasons.items():
            lines.append(f"    skip[{count}]: {reason}")
        for failure in tier.iteration_failures:
            lines.append(f"    iteration-failure: {failure}")
        lines.extend(
            _failure_detail_lines(
                (asdict(detail) for detail in tier.failure_details), tier.failure_details_omitted
            )
        )
        for case in tier.case_results:
            lines.append(
                f"    case {case.status:4} {case.nodeid}: "
                f"{_format_counts(case.counts)} "
                f"returncode={case.returncode} duration={case.duration_seconds:.1f}s"
            )
            if case.reason:
                lines.append(f"      reason: {case.reason}")
        if tier.status in {"FAIL", "BLOCKED", "INTERRUPTED"} and tier.output_tail:
            lines.append("    output tail:")
            lines.extend(f"      {line}" for line in tier.output_tail.splitlines()[-60:])
    if capacity is not None:
        lines.extend(_capacity_text_lines(capacity))
    lines.append(f"overall: {overall}")
    return "\n".join(lines)


def render_dual_text(
    *,
    project_root: Path,
    rom_root: Path,
    fixture_root: Path,
    assets: Sequence[AssetRecord],
    runtime_results: Sequence[RuntimeGateResult],
    overall: str,
    capacity: dict[str, Any] | None = None,
) -> str:
    """Render explicit source/Cython results without collapsing either run."""

    lines = [
        "Pokémon harness production gate",
        f"project={project_root}",
        f"rom_root={rom_root}",
        f"fixture_root={fixture_root}",
        "runtime-mode=both",
        "assets:",
    ]
    for asset in assets:
        suffix = []
        if asset.expected_sha1:
            suffix.append(f"expected_sha1={asset.expected_sha1}")
        if asset.actual_sha1:
            suffix.append(f"actual_sha1={asset.actual_sha1}")
        if asset.size is not None:
            suffix.append(f"size={asset.size}")
        detail = " " + " ".join(suffix) if suffix else ""
        lines.append(
            f"  {asset.status.upper():13} {asset.kind:7} {asset.label}: {asset.path}{detail}"
        )
    lines.append(
        "runtime-results:",
    )
    for result in runtime_results:
        status = "PASS" if runtime_gate_passes(result) else "FAIL"
        lines.append(f"  {result.mode}: {status}")
        detail = render_text(
            project_root=project_root,
            rom_root=rom_root,
            fixture_root=fixture_root,
            runtime=result.runtime,
            assets=[],
            collections=result.collections,
            tiers=result.tiers,
            gate_problems=result.gate_problems,
            overall=status,
            fixture_manifest=result.fixture_manifest,
            matrix_audit=result.matrix_audit,
            execution_plan=result.execution_plan,
            cancellation=result.cancellation,
        )
        lines.extend(f"    {line}" for line in detail.splitlines()[4:])
    if capacity is not None:
        lines.extend(_capacity_text_lines(capacity))
    lines.append(f"overall: {overall}")
    return "\n".join(lines)


def _runtime_result_json(result: RuntimeGateResult) -> dict[str, Any]:
    """Serialize one explicit runtime result for the dual JSON report."""

    return {
        "mode": result.mode,
        "runtime": result.runtime,
        "collections": [asdict(collection) for collection in result.collections],
        "fixture_manifest": result.fixture_manifest,
        "matrix_audit": result.matrix_audit,
        "tiers": [_jsonable_tier(tier) for tier in result.tiers],
        "gate_problems": result.gate_problems,
        "cancellation": result.cancellation,
        "overall": "PASS" if runtime_gate_passes(result) else "FAIL",
        **({"execution_plan": result.execution_plan} if result.execution_plan else {}),
    }


def _render_dual_evidence_text(payload: dict[str, Any]) -> str:
    """Render a dual-runtime evidence payload with each run clearly scoped."""

    lines = [
        "Pokémon harness production gate evidence bundle",
        f"generated_at={payload.get('generated_at', '')}",
        f"overall: {payload.get('overall', 'UNKNOWN')}",
        f"runtime-mode={payload.get('runtime_mode', 'both')}",
        "assets:",
    ]
    for asset in payload.get("assets", []):
        details = []
        for key in ("expected_sha1", "actual_sha1", "size"):
            if asset.get(key) is not None:
                details.append(f"{key}={asset[key]}")
        suffix = f" {' '.join(details)}" if details else ""
        lines.append(
            f"  {str(asset.get('status', 'unknown')).upper():13} "
            f"{asset.get('kind', ''):7} {asset.get('label', '')}: "
            f"{asset.get('path', '')}{suffix}"
        )

    lines.append("runtimes:")
    for result in payload.get("runtimes", []):
        if not isinstance(result, dict):
            continue
        child_payload = {
            "generated_at": payload.get("generated_at", ""),
            "overall": result.get("overall", "UNKNOWN"),
            "runtime": result.get("runtime", {}),
            "collections": result.get("collections", []),
            "assets": [],
            "fixture_manifest": result.get("fixture_manifest"),
            "matrix_audit": result.get("matrix_audit"),
            "tiers": result.get("tiers", []),
            "gate_problems": result.get("gate_problems", []),
            "cancellation": result.get("cancellation", ""),
            "execution_plan": result.get("execution_plan", {}),
            "safety": {},
        }
        child_lines = render_evidence_text(child_payload).rstrip("\n").splitlines()
        lines.append(f"  {result.get('mode', 'unknown')}: {result.get('overall', 'UNKNOWN')}")
        # Omit the child's title, timestamp, and duplicate overall line; the
        # parent already identifies the combined evidence bundle.
        lines.extend(f"    {line}" for line in child_lines[3:])

    if isinstance(payload.get("capacity"), dict):
        lines.extend(_capacity_text_lines(payload["capacity"]))
    if payload.get("evidence_error"):
        lines.append(f"evidence-error: {payload['evidence_error']}")
    lines.append("safety:")
    for key, value in payload.get("safety", {}).items():
        lines.append(f"  {key}: {value}")
    return "\n".join(lines) + "\n"


def render_evidence_text(payload: dict[str, Any]) -> str:
    """Render the already-sanitized payload for human inspection."""

    if isinstance(payload.get("runtimes"), list):
        return _render_dual_evidence_text(payload)

    lines = [
        "Pokémon harness production gate evidence bundle",
        f"generated_at={payload.get('generated_at', '')}",
        f"overall: {payload.get('overall', 'UNKNOWN')}",
        "environment:",
    ]
    runtime = payload.get("runtime", {})
    if isinstance(runtime, dict):
        for key in (
            "python_executable",
            "python_version",
            "pytest_version",
            "pyboy_version",
            "pyboy_revision",
            "pyboy_kind",
            "pyboy_module",
            "serial_module",
            "serial_contract",
            "harness_module",
        ):
            if key in runtime:
                lines.append(f"  {key}={runtime[key]}")

    problems = payload.get("gate_problems", [])
    lines.append("gate-policy:")
    if problems:
        lines.extend(f"  FAIL: {problem}" for problem in problems)
    else:
        lines.append("  PASS")
    cancellation = payload.get("cancellation", "")
    if cancellation:
        lines.append("cancellation:")
        lines.append(f"  {cancellation}")

    lines.append("collection:")
    for collection in payload.get("collections", []):
        lines.append(
            f"  {collection.get('status', 'UNKNOWN'):4} {collection.get('name', '')}: "
            f"returncode={collection.get('returncode')} "
            f"duration={collection.get('duration_seconds', 0.0):.1f}s"
        )
        command = collection.get("command", [])
        lines.append(f"    command: {' '.join(command)}")
        if collection.get("reason"):
            lines.append(f"    reason: {collection['reason']}")
        if collection.get("status") != "PASS" and collection.get("output_tail"):
            lines.append("    output tail:")
            lines.extend(f"      {line}" for line in collection["output_tail"].splitlines())

    lines.append("assets:")
    for asset in payload.get("assets", []):
        details = []
        for key in ("expected_sha1", "actual_sha1", "size"):
            if asset.get(key) is not None:
                details.append(f"{key}={asset[key]}")
        suffix = f" {' '.join(details)}" if details else ""
        lines.append(
            f"  {str(asset.get('status', 'unknown')).upper():13} "
            f"{asset.get('kind', ''):7} {asset.get('label', '')}: "
            f"{asset.get('path', '')}{suffix}"
        )

    fixture_manifest = payload.get("fixture_manifest")
    if isinstance(fixture_manifest, dict):
        lines.append("fixture-manifest:")
        lines.append(
            f"  {fixture_manifest.get('status', 'UNKNOWN')}: "
            f"mode={fixture_manifest.get('mode', 'unknown')} "
            f"entries={fixture_manifest.get('entries')} "
            f"returncode={fixture_manifest.get('returncode')}"
        )
        if fixture_manifest.get("reason"):
            lines.append(f"  reason: {fixture_manifest['reason']}")

    matrix_audit = payload.get("matrix_audit")
    if isinstance(matrix_audit, dict):
        lines.append("matrix-audit:")
        lines.append(
            f"  {matrix_audit.get('status', 'UNKNOWN')}: "
            f"collected={matrix_audit.get('collected', 0)} "
            f"structural={'PASS' if matrix_audit.get('structural_pass') else 'FAIL'} "
            f"acceptance-declaration="
            f"{'PASS' if matrix_audit.get('acceptance_matrix_complete') else 'FAIL'}"
        )
        groups = matrix_audit.get("groups", {})
        if isinstance(groups, dict):
            for name, group in groups.items():
                if isinstance(group, dict):
                    lines.append(
                        f"  {name}: {group.get('present', 0)}/{group.get('expected', 0)} collected"
                    )

    if payload.get("execution_plan"):
        lines.extend(_execution_plan_lines(payload["execution_plan"]))
    lines.append("tiers:")
    for tier in payload.get("tiers", []):
        lines.append(
            f"  {tier.get('status', 'UNKNOWN'):8} {tier.get('name', ''):7} "
            f"{_payload_counts_text(tier.get('counts', {}))} "
            f"duration={tier.get('duration_seconds', 0.0):.1f}s"
        )
        if tier.get("reason"):
            lines.append(f"    reason: {tier['reason']}")
        for reason, count in tier.get("skip_reasons", {}).items():
            lines.append(f"    skip[{count}]: {reason}")
        for failure in tier.get("iteration_failures", []):
            lines.append(f"    iteration-failure: {failure}")
        lines.extend(
            _failure_detail_lines(
                tier.get("failure_details", []), tier.get("failure_details_omitted", 0)
            )
        )
        for case in tier.get("case_results", []):
            if not isinstance(case, dict):
                continue
            counts = case.get("counts", {})
            lines.append(
                f"    case {case.get('status', 'UNKNOWN'):4} "
                f"{case.get('nodeid', '')}: {_payload_counts_text(counts)} "
                f"returncode={case.get('returncode')} "
                f"duration={case.get('duration_seconds', 0.0):.1f}s"
            )
            if case.get("reason"):
                lines.append(f"      reason: {case['reason']}")
        if tier.get("status") in {"FAIL", "BLOCKED"} and tier.get("output_tail"):
            lines.append("    output tail:")
            lines.extend(f"      {line}" for line in tier["output_tail"].splitlines())

    if isinstance(payload.get("capacity"), dict):
        lines.extend(_capacity_text_lines(payload["capacity"]))
    if payload.get("evidence_error"):
        lines.append(f"evidence-error: {payload['evidence_error']}")
    lines.append("safety:")
    for key, value in payload.get("safety", {}).items():
        lines.append(f"  {key}: {value}")
    return "\n".join(lines) + "\n"


def _evidence_file_metadata(path: Path, relative_name: str) -> dict[str, Any]:
    content = path.read_bytes()
    return {
        "path": relative_name,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
