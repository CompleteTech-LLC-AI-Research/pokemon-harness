"""Evidence payload construction, verification and bundle writing.

Split from ``scripts/production_gate.py`` for issue #124 with no behavior
change: the code below is copied verbatim except that calls to facade-owned,
monkeypatch-patched entry points resolve through ``_entry`` so attribute patches
on the loaded gate module stay visible.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Direct names this module calls; cyclic back-edges go through _entry.
from scripts.production_gate_capacity import (
    CAPACITY_STREAM_FILENAME,
    MAX_CAPACITY_SAMPLES,
    _safe_capacity,
)
from scripts.production_gate_model import (
    EVIDENCE_MANIFEST_FILENAME,
    EVIDENCE_REPORT_FILENAME,
    EVIDENCE_SCHEMA_VERSION,
    EVIDENCE_TEXT_FILENAME,
    MAX_FAILURE_DETAILS,
    MAX_FAILURE_DETAILS_CHARS,
    MAX_FAILURE_NODEID_CHARS,
    AssetRecord,
    CollectionResult,
    FailureDetail,
    RuntimeGateResult,
    TierResult,
    _safe_execution_plan,
)
from scripts.production_gate_render import _evidence_file_metadata, render_evidence_text
from scripts.production_gate_runtime_gates import runtime_gate_passes
from scripts.production_gate_text import _jsonable_tier, _retain_failure_detail, _safe_text


def _evidence_roots(
    project_root: Path,
    rom_root: Path,
    fixture_root: Path,
) -> tuple[tuple[str, Path], ...]:
    return (
        ("rom-root", rom_root),
        ("fixture-root", fixture_root),
        ("project-root", project_root),
    )


def _portable_path(
    value: str | Path,
    roots: tuple[tuple[str, Path], ...],
) -> str:
    """Represent a path without retaining machine-local absolute paths."""

    candidate = Path(value)
    try:
        resolved_candidate = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        resolved_candidate = candidate

    for label, root in roots:
        try:
            resolved_root = root.expanduser().resolve(strict=False)
            relative = resolved_candidate.relative_to(resolved_root)
        except (OSError, RuntimeError, ValueError):
            continue
        if not relative.parts:
            return f"<{label}>"
        return f"<{label}>/{relative.as_posix()}"

    return "<external-path>"


def _prepare_root_replacements(
    roots: tuple[tuple[str, Path], ...],
) -> tuple[tuple[str, str], ...]:
    """Resolve root variants once for one evidence payload, without a global cache."""

    replacements: dict[str, str] = {}
    for label, root in roots:
        for raw in (str(root), str(root.expanduser())):
            if raw:
                replacements[raw] = f"<{label}>"
        try:
            replacements[str(root.expanduser().resolve(strict=False))] = f"<{label}>"
        except (OSError, RuntimeError):
            pass
    return tuple(sorted(replacements.items(), key=lambda item: -len(item[0])))


def _safe_diagnostic(
    value: Any,
    roots: tuple[tuple[str, Path], ...],
    *,
    limit: int = 8000,
    replacements: tuple[tuple[str, str], ...] | None = None,
) -> str:
    text = "" if value is None else str(value)
    if replacements is None:
        replacements = _prepare_root_replacements(roots)
    for raw, replacement in replacements:
        text = text.replace(raw, replacement)
    return _safe_text(text, limit=limit)


def _safe_command(
    command: list[str] | None,
    roots: tuple[tuple[str, Path], ...],
    *,
    replacements: tuple[tuple[str, str], ...] | None = None,
) -> list[str] | None:
    if command is None:
        return None
    result: list[str] = []
    for argument in command:
        try:
            path = Path(argument)
            is_absolute = path.is_absolute()
        except (TypeError, ValueError):
            is_absolute = False
        if is_absolute:
            result.append(_portable_path(argument, roots))
        else:
            result.append(_safe_diagnostic(argument, roots, replacements=replacements, limit=1000))
    return result


def _safe_runtime(
    runtime: dict[str, Any],
    roots: tuple[tuple[str, Path], ...],
    *,
    replacements: tuple[tuple[str, str], ...] | None = None,
) -> dict[str, Any]:
    path_keys = frozenset({"python_executable", "pyboy_module", "serial_module", "harness_module"})
    result: dict[str, Any] = {}
    for key, value in runtime.items():
        if value is None or isinstance(value, (bool, int, float)):
            result[key] = value
        elif key in path_keys:
            result[key] = _portable_path(str(value), roots)
        else:
            result[key] = _safe_diagnostic(value, roots, replacements=replacements)
    return result


def _safe_asset(
    asset: AssetRecord,
    roots: tuple[tuple[str, Path], ...],
) -> dict[str, Any]:
    data = asdict(asset)
    data["path"] = _portable_path(asset.path, roots)
    return data


def _safe_collection(
    collection: CollectionResult,
    roots: tuple[tuple[str, Path], ...],
    *,
    replacements: tuple[tuple[str, str], ...] | None = None,
) -> dict[str, Any]:
    data = asdict(collection)
    data["command"] = _safe_command(collection.command, roots, replacements=replacements) or []
    data["nodeids"] = [
        _safe_diagnostic(nodeid, roots, replacements=replacements, limit=1000)
        for nodeid in collection.nodeids
    ]
    data["output_tail"] = _safe_diagnostic(collection.output_tail, roots, replacements=replacements)
    data["reason"] = _safe_diagnostic(
        collection.reason, roots, replacements=replacements, limit=2000
    )
    return data


def _safe_fixture_manifest(result: dict[str, Any]) -> dict[str, Any]:
    """Keep only bounded, non-path fields from manifest validation."""

    return {
        "status": result.get("status"),
        "mode": result.get("mode"),
        "returncode": result.get("returncode"),
        "entries": result.get("entries"),
        "reason": _safe_text(result.get("reason"), limit=4000),
    }


def _safe_matrix_audit(result: dict[str, Any]) -> dict[str, Any]:
    """Sanitize the matrix auditor while retaining exact coverage counts."""

    safe: dict[str, Any] = {
        "status": result.get("status"),
        "structural_pass": result.get("structural_pass"),
        "acceptance_matrix_complete": result.get("acceptance_matrix_complete"),
        "collected": result.get("collected"),
        "runtime": _safe_text(result.get("runtime"), limit=100),
        "reason": _safe_text(result.get("reason"), limit=2000),
        "groups": {},
        "acceptance_gaps": {},
    }
    raw_groups = result.get("groups", {})
    if isinstance(raw_groups, dict):
        groups: dict[str, Any] = {}
        for name, raw_group in raw_groups.items():
            if not isinstance(raw_group, dict):
                continue
            groups[str(name)] = {
                "expected": raw_group.get("expected"),
                "present": raw_group.get("present"),
                "missing": [
                    _safe_diagnostic(item, (), limit=1000) for item in raw_group.get("missing", ())
                ],
            }
        safe["groups"] = groups
    raw_gaps = result.get("acceptance_gaps", {})
    if isinstance(raw_gaps, dict):
        safe["acceptance_gaps"] = {
            str(name): [_safe_diagnostic(item, (), limit=1000) for item in entries]
            for name, entries in raw_gaps.items()
            if isinstance(entries, (tuple, list))
        }
    for key in (
        "acceptance_classifications",
        "remote_link_menu_classifications",
    ):
        raw_classifications = result.get(key)
        if isinstance(raw_classifications, dict):
            safe[key] = {
                str(name): _safe_diagnostic(status, (), limit=500)
                for name, status in raw_classifications.items()
            }
    raw_profile_pairs = result.get("remote_strict_profile_pairs")
    if isinstance(raw_profile_pairs, list):
        safe["remote_strict_profile_pairs"] = [
            {str(name): _safe_diagnostic(value, (), limit=500) for name, value in pair.items()}
            for pair in raw_profile_pairs
            if isinstance(pair, dict)
        ]
    raw_audited_nodeids = result.get("audited_nodeids")
    if isinstance(raw_audited_nodeids, dict):
        safe["audited_nodeids"] = {
            str(name): [
                _safe_diagnostic(nodeid, (), limit=1000)
                for nodeid in nodeids
                if isinstance(nodeid, str)
            ]
            for name, nodeids in raw_audited_nodeids.items()
            if isinstance(nodeids, (tuple, list))
        }
    return safe


def _safe_tier(
    tier: TierResult,
    roots: tuple[tuple[str, Path], ...],
    *,
    replacements: tuple[tuple[str, str], ...] | None = None,
) -> dict[str, Any]:
    data = _jsonable_tier(tier)
    data["command"] = _safe_command(tier.command, roots, replacements=replacements)
    data["output_tail"] = _safe_diagnostic(tier.output_tail, roots, replacements=replacements)
    data["reason"] = _safe_diagnostic(tier.reason, roots, replacements=replacements, limit=2000)
    data["iteration_failures"] = [
        _safe_diagnostic(failure, roots, replacements=replacements, limit=2000)
        for failure in tier.iteration_failures
    ]
    # Reapply the evidence boundary for directly constructed records as well
    # as runner results. Root replacement and redaction precede any clipping.
    # Preserve existing truncation metadata; only add newly omitted characters.
    safe_details: list[FailureDetail] = []
    remaining_chars = MAX_FAILURE_DETAILS_CHARS
    omitted_details = tier.failure_details_omitted
    for detail in tier.failure_details:
        if (
            len(safe_details) >= MAX_FAILURE_DETAILS
            or remaining_chars < MAX_FAILURE_NODEID_CHARS + 128
        ):
            omitted_details += 1
            continue
        record = {
            key: _safe_diagnostic(
                getattr(detail, key), roots, replacements=replacements, limit=sys.maxsize
            )
            for key in ("nodeid", "outcome", "reason")
        }
        remaining_chars, omitted = _retain_failure_detail(
            safe_details, record, iteration=detail.iteration, remaining_chars=remaining_chars
        )
        omitted_details += omitted
        if not omitted:
            safe_detail = safe_details[-1]
            if detail.omitted_chars:
                safe_detail.original_chars = detail.original_chars
                safe_detail.omitted_chars += detail.omitted_chars
            if detail.nodeid_omitted_chars:
                safe_detail.nodeid_original_chars = detail.nodeid_original_chars
                safe_detail.nodeid_omitted_chars += detail.nodeid_omitted_chars
            safe_detail.truncated = bool(
                safe_detail.omitted_chars or safe_detail.nodeid_omitted_chars
            )
    data["failure_details"] = [asdict(detail) for detail in safe_details]
    data["failure_details_omitted"] = omitted_details
    data["selected_nodeids"] = [
        _safe_diagnostic(nodeid, roots, replacements=replacements, limit=1000)
        for nodeid in tier.selected_nodeids
    ]
    data["skip_reasons"] = {
        _safe_diagnostic(reason, roots, replacements=replacements, limit=2000): count
        for reason, count in tier.skip_reasons.items()
    }
    data["case_results"] = []
    for case in tier.case_results:
        safe_case = asdict(case)
        safe_case["nodeid"] = _safe_diagnostic(
            case.nodeid, roots, replacements=replacements, limit=1000
        )
        safe_case["reason"] = _safe_diagnostic(
            case.reason, roots, replacements=replacements, limit=2000
        )
        safe_case["output_tail"] = _safe_diagnostic(
            case.output_tail, roots, replacements=replacements
        )
        data["case_results"].append(safe_case)
    return data


def build_evidence_payload(
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
    generated_at: str | None = None,
    evidence_error: str = "",
    fixture_manifest: dict[str, Any] | None = None,
    matrix_audit: dict[str, Any] | None = None,
    execution_plan: dict[str, Any] | None = None,
    capacity: dict[str, Any] | None = None,
    cancellation: str = "",
) -> dict[str, Any]:
    """Build the sanitized, metadata-only payload retained by the gate.

    The normal stdout JSON remains backward-compatible.  This separate
    payload is deliberately allow-listed and redacts free-form diagnostics so
    an evidence directory never becomes a copy of ROMs, save states, or the
    inherited process environment.
    """

    roots = _evidence_roots(project_root, rom_root, fixture_root)
    replacements = _prepare_root_replacements(roots)
    payload: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "project_root": "<project-root>",
        "rom_root": "<rom-root>",
        "fixture_root": "<fixture-root>",
        "runtime": _safe_runtime(runtime, roots, replacements=replacements),
        "collections": [
            _safe_collection(item, roots, replacements=replacements) for item in collections
        ],
        "assets": [_safe_asset(item, roots) for item in assets],
        "tiers": [_safe_tier(item, roots, replacements=replacements) for item in tiers],
        "gate_problems": [
            _safe_diagnostic(problem, roots, replacements=replacements, limit=2000)
            for problem in gate_problems
        ],
        "cancellation": _safe_diagnostic(cancellation, roots, replacements=replacements, limit=2000)
        if cancellation
        else "",
        "overall": overall,
        "safety": {
            "rom_bytes": "not included",
            "credentials": "environment is not captured; free-form diagnostics are redacted",
            "diagnostics": "bounded text tails only",
        },
    }
    if fixture_manifest is not None:
        payload["fixture_manifest"] = _safe_fixture_manifest(fixture_manifest)
    if matrix_audit is not None:
        payload["matrix_audit"] = _safe_matrix_audit(matrix_audit)
    if execution_plan:
        payload["execution_plan"] = _safe_execution_plan(execution_plan)
    payload["capacity"] = _safe_capacity(capacity, roots, replacements=replacements)
    if evidence_error:
        payload["evidence_error"] = _safe_diagnostic(
            evidence_error, roots, replacements=replacements, limit=2000
        )
    return payload


def build_dual_evidence_payload(
    *,
    project_root: Path,
    rom_root: Path,
    fixture_root: Path,
    assets: list[AssetRecord],
    runtime_results: Sequence[RuntimeGateResult],
    overall: str,
    requested_mode: str = "both",
    generated_at: str | None = None,
    evidence_error: str = "",
    capacity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a sanitized evidence bundle containing both runtime executions."""

    roots = _evidence_roots(project_root, rom_root, fixture_root)
    replacements = _prepare_root_replacements(roots)
    runtimes: list[dict[str, Any]] = []
    for result in runtime_results:
        runtimes.append(
            {
                "mode": result.mode,
                "runtime": _safe_runtime(result.runtime, roots, replacements=replacements),
                "collections": [
                    _safe_collection(collection, roots, replacements=replacements)
                    for collection in result.collections
                ],
                "fixture_manifest": _safe_fixture_manifest(result.fixture_manifest),
                "matrix_audit": _safe_matrix_audit(result.matrix_audit),
                "tiers": [
                    _safe_tier(tier, roots, replacements=replacements) for tier in result.tiers
                ],
                "gate_problems": [
                    _safe_diagnostic(problem, roots, replacements=replacements, limit=2000)
                    for problem in result.gate_problems
                ],
                "cancellation": _safe_diagnostic(
                    result.cancellation, roots, replacements=replacements, limit=2000
                )
                if result.cancellation
                else "",
                "overall": "PASS" if runtime_gate_passes(result) else "FAIL",
                **(
                    {"execution_plan": _safe_execution_plan(result.execution_plan)}
                    if result.execution_plan
                    else {}
                ),
            }
        )

    payload: dict[str, Any] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "generated_at": generated_at or datetime.now(UTC).isoformat(),
        "project_root": "<project-root>",
        "rom_root": "<rom-root>",
        "fixture_root": "<fixture-root>",
        "runtime_mode": requested_mode,
        "runtimes": runtimes,
        "assets": [_safe_asset(item, roots) for item in assets],
        "overall": overall,
        "capacity": _safe_capacity(capacity, roots, replacements=replacements),
        "safety": {
            "rom_bytes": "not included",
            "credentials": "environment is not captured; free-form diagnostics are redacted",
            "diagnostics": "bounded text tails only",
        },
    }
    if evidence_error:
        payload["evidence_error"] = _safe_diagnostic(
            evidence_error, roots, replacements=replacements, limit=2000
        )
    return payload


def verify_evidence_bundle(evidence_dir: Path) -> None:
    """Verify the metadata and content hashes of a retained evidence bundle."""

    evidence_dir = evidence_dir.expanduser().resolve(strict=False)
    expected_bundle_names = {
        EVIDENCE_REPORT_FILENAME,
        EVIDENCE_TEXT_FILENAME,
        EVIDENCE_MANIFEST_FILENAME,
    }
    try:
        actual_bundle_names = {path.name for path in evidence_dir.iterdir()}
    except OSError as exc:
        raise ValueError(
            f"could not enumerate evidence bundle: {type(exc).__name__}: {exc}"
        ) from exc
    if CAPACITY_STREAM_FILENAME in actual_bundle_names:
        expected_bundle_names.add(CAPACITY_STREAM_FILENAME)
    unexpected_files = sorted(actual_bundle_names - expected_bundle_names)
    if unexpected_files:
        raise ValueError(
            "evidence bundle contains unexpected files: " + ", ".join(unexpected_files)
        )
    for name in expected_bundle_names:
        path = evidence_dir / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"evidence bundle entry is not a regular file: {name!r}")
    manifest_path = evidence_dir / EVIDENCE_MANIFEST_FILENAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read evidence manifest: {type(exc).__name__}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise TypeError("evidence manifest root is not an object")
    if manifest.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise ValueError("evidence manifest schema version is unsupported")

    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise TypeError("evidence manifest files is not a list")
    expected_names = expected_bundle_names - {EVIDENCE_MANIFEST_FILENAME}
    actual_names: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise TypeError("evidence manifest file entry is not an object")
        relative_name = entry.get("path")
        if not isinstance(relative_name, str) or not relative_name:
            raise ValueError("evidence manifest file path is invalid")
        if relative_name in actual_names:
            raise ValueError(f"evidence manifest repeats file {relative_name!r}")
        actual_names.add(relative_name)
        relative_path = Path(relative_name)
        if (
            relative_path.is_absolute()
            or PureWindowsPath(relative_name).is_absolute()
            or "\\" in relative_name
            or ".." in relative_path.parts
        ):
            raise ValueError(f"evidence manifest file escapes its directory: {relative_name!r}")
        path = (evidence_dir / relative_path).resolve(strict=False)
        try:
            path.relative_to(evidence_dir)
        except ValueError as exc:
            raise ValueError(
                f"evidence manifest file escapes its directory: {relative_name!r}"
            ) from exc
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise ValueError(
                f"could not read evidence file {relative_name!r}: {type(exc).__name__}: {exc}"
            ) from exc
        if entry.get("size") != len(content):
            raise ValueError(f"evidence file size mismatch for {relative_name!r}")
        if entry.get("sha256") != hashlib.sha256(content).hexdigest():
            raise ValueError(f"evidence file sha256 mismatch for {relative_name!r}")

    if actual_names != expected_names:
        raise ValueError("evidence manifest must cover exactly the retained report files")
    try:
        report = json.loads((evidence_dir / EVIDENCE_REPORT_FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"could not read retained gate report: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(report, dict):
        raise TypeError("retained gate report root is not an object")
    if report.get("overall") != manifest.get("overall"):
        raise ValueError("evidence manifest overall status does not match gate report")
    capacity = report.get("capacity", {})
    reference = capacity.get("sample_reference", {}) if isinstance(capacity, dict) else {}
    if reference or CAPACITY_STREAM_FILENAME in actual_bundle_names:
        if reference.get("path") != CAPACITY_STREAM_FILENAME:
            raise ValueError("capacity stream reference is missing or invalid")
        content = (evidence_dir / CAPACITY_STREAM_FILENAME).read_bytes()
        if reference.get("sha256") != hashlib.sha256(content).hexdigest():
            raise ValueError("capacity stream reference hash mismatch")
        if reference.get("size") != len(content):
            raise ValueError("capacity stream reference size mismatch")
        samples = json.loads(content)
        if not isinstance(samples, list) or len(samples) != reference.get("sample_count"):
            raise ValueError("capacity stream reference count mismatch")
        if capacity.get("samples") != samples[:MAX_CAPACITY_SAMPLES]:
            raise ValueError("capacity summary does not match stream")
        if capacity.get("samples_omitted") != max(0, len(samples) - MAX_CAPACITY_SAMPLES):
            raise ValueError("capacity omitted count does not match stream")


def write_evidence_bundle(
    evidence_dir: Path,
    payload: dict[str, Any],
) -> dict[str, Path]:
    """Write a small, self-contained report bundle without copying inputs."""

    evidence_dir = evidence_dir.expanduser()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    report_path = evidence_dir / EVIDENCE_REPORT_FILENAME
    text_path = evidence_dir / EVIDENCE_TEXT_FILENAME
    manifest_path = evidence_dir / EVIDENCE_MANIFEST_FILENAME

    payload = dict(payload)
    stream_path = None
    capacity = payload.get("capacity")
    if isinstance(capacity, dict) and "sample_stream" in capacity:
        capacity = dict(capacity)
        payload["capacity"] = capacity
        samples = capacity.pop("sample_stream")
        encoded = (json.dumps(samples, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
        stream_path = evidence_dir / CAPACITY_STREAM_FILENAME
        stream_path.write_bytes(encoded)

    report_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    text_path.write_text(render_evidence_text(payload), encoding="utf-8")
    manifest = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "generated_at": payload.get("generated_at"),
        "overall": payload.get("overall"),
        "files": [
            _evidence_file_metadata(report_path, EVIDENCE_REPORT_FILENAME),
            _evidence_file_metadata(text_path, EVIDENCE_TEXT_FILENAME),
        ],
        "safety": payload.get("safety", {}),
    }
    if stream_path is not None:
        manifest["files"].append(_evidence_file_metadata(stream_path, CAPACITY_STREAM_FILENAME))
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    verify_evidence_bundle(evidence_dir)
    return {
        "report": report_path,
        "text": text_path,
        "manifest": manifest_path,
    }
