"""Result model and collection/JUnit parsing for the coverage report (#130).

Split from ``scripts/coverage_report.py`` with no behavior change."""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts._coverage_report_schema import (
    _COLLECTION_PASS,
    _DEFAULT_ACCEPTED_OUTCOMES,
    _EFFECT_TEXT_KEYS,
    _ENCLOSING_PASS,
    _GATE_STATUS,
    _MOVE_EFFECT_RE,
    _VERSIONS,
    CoverageError,
    normalize_nodeid,
)


@dataclass
class Outcome:
    """One terminal test outcome for a single node ID and runtime."""

    nodeid: str
    status: str
    runtime: str
    reason: str = ""
    partial: bool = False
    commit: str | None = None
    run_id: str | None = None
    input_hashes: dict[str, str] = field(default_factory=dict)
    effects: tuple[int, ...] = ()


@dataclass
class CollectionEvidence:
    """One pytest collection record for a declared runtime."""

    runtime: str
    name: str
    status: str
    nodeids: tuple[str, ...] = ()
    reason: str = ""


@dataclass
class ResultSet:
    """Normalised collected/terminal records from one results source."""

    source_path: str | None = None
    source_kind: str = "unknown"
    run_id: str | None = None
    commit: str | None = None
    partial: bool = False
    runtimes: dict[str, dict[str, Any]] = field(default_factory=dict)
    outcomes: list[Outcome] = field(default_factory=list)
    collection_errors: list[str] = field(default_factory=list)
    collection_failures: list[str] = field(default_factory=list)
    collections: list[CollectionEvidence] = field(default_factory=list)
    block_run_ids: set[str] = field(default_factory=set)
    block_commits: set[str] = field(default_factory=set)
    missing_commit: bool = False
    notes: list[str] = field(default_factory=list)
    identity_conflicts: list[str] = field(default_factory=list)
    merge_conflict: bool = False

    def lookup(self, runtime: str, selector: str) -> list[Outcome]:
        target = normalize_nodeid(selector)
        return [
            outcome
            for outcome in self.outcomes
            if outcome.runtime == runtime and normalize_nodeid(outcome.nodeid) == target
        ]


def _status_from_gate(status: Any) -> str:
    if isinstance(status, str) and status in _GATE_STATUS:
        return _GATE_STATUS[status]
    if isinstance(status, str) and status:
        return status.lower()
    return "error"


def _runtime_identity(block: dict[str, Any]) -> dict[str, Any]:
    runtime = block.get("runtime")
    return runtime if isinstance(runtime, dict) else {}


def _effects_from_text(text: str) -> tuple[int, ...]:
    """Extract observed ``local``/``enemy`` move effects from retained output."""
    return tuple(int(match.group(1)) for match in _MOVE_EFFECT_RE.finditer(text))


def _record_effects(record: dict[str, Any]) -> tuple[int, ...]:
    """Return the observed move effects carried by a result record.

    A normalised record may declare ``effect_id``/``effect`` directly. Gate and
    JUnit evidence instead retain the strict battle test's printed settlement
    rows, so the same normalisation path also scans the retained output text.
    """
    explicit = record.get("effect_id", record.get("effect"))
    if isinstance(explicit, int) and not isinstance(explicit, bool):
        return (explicit,)
    effects: list[int] = []
    for key in _EFFECT_TEXT_KEYS:
        value = record.get(key)
        if isinstance(value, str) and value:
            effects.extend(_effects_from_text(value))
    return tuple(effects)


def _enclosing_partial(record: dict[str, Any]) -> bool:
    """Return whether an enclosing container marks every child non-terminal.

    An explicit ``partial`` marker or any enclosing status that is not a clean
    terminal pass propagates to every contained case so a timed-out or partial
    tier can never qualify a dimension.
    """
    if bool(record.get("partial", False)):
        return True
    status = record.get("status")
    if isinstance(status, str) and status.strip():
        return _status_from_gate(status) not in _ENCLOSING_PASS
    return False


def _normalise_outcome(
    record: dict[str, Any] | None,
    *,
    nodeid: str,
    status: str,
    runtime: str,
    inherited_partial: bool,
    inherited_commit: str | None,
    inherited_run_id: str | None,
    inherited_hashes: dict[str, str],
) -> Outcome:
    """Build one :class:`Outcome`, preserving partial and identity provenance.

    Every supported input format funnels through this single normaliser so a
    record-level ``partial`` marker, commit, run identity, input hashes, and
    observed move effect behave the same for ``records``, ``tests``, gate
    ``tiers``/``case_results``, and JUnit testcases.
    """
    source = record if isinstance(record, dict) else {}
    commit = source.get("commit")
    if not (isinstance(commit, str) and commit):
        commit = inherited_commit
    run_id = source.get("run_id")
    if not (isinstance(run_id, str) and run_id):
        run_id = inherited_run_id
    input_hashes = dict(inherited_hashes)
    input_hashes.update(_record_input_hashes(source))
    return Outcome(
        nodeid=nodeid,
        status=status,
        runtime=runtime,
        reason=str(source.get("reason", "")),
        partial=bool(source.get("partial", False)) or inherited_partial,
        commit=commit,
        run_id=run_id,
        input_hashes=input_hashes,
        effects=_record_effects(source),
    )


def _mode_for(block: dict[str, Any]) -> str:
    mode = block.get("mode")
    if isinstance(mode, str) and mode:
        return mode
    identity = _runtime_identity(block)
    mode = identity.get("pyboy_mode")
    if isinstance(mode, str) and mode:
        return mode
    return "unknown"


def _collect_collection_errors(raw: Any, result: ResultSet) -> None:
    if not isinstance(raw, list):
        return
    for entry in raw:
        if isinstance(entry, dict):
            result.collection_errors.append(
                f"{entry.get('nodeid', '<collection>')}: {entry.get('reason', '')}"
            )
        elif isinstance(entry, str):
            result.collection_errors.append(entry)


def _collect_collections(raw: Any, runtime: str, result: ResultSet) -> None:
    if not isinstance(raw, list):
        return
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        name = name if isinstance(name, str) and name else "collection"
        status = entry.get("status")
        status = status if isinstance(status, str) and status else "error"
        raw_nodeids = entry.get("nodeids")
        nodeids = (
            tuple(item for item in raw_nodeids if isinstance(item, str))
            if isinstance(raw_nodeids, list)
            else ()
        )
        reason = entry.get("reason")
        reason = reason if isinstance(reason, str) else ""
        result.collections.append(
            CollectionEvidence(
                runtime=runtime,
                name=name,
                status=status,
                nodeids=nodeids,
                reason=reason,
            )
        )
        if status.lower() not in _COLLECTION_PASS:
            result.collection_failures.append(f"{name} ({runtime}): {reason or status}")


def result_set_from_document(
    document: Any,
    *,
    source_path: str | None = None,
) -> ResultSet:
    """Normalise a gate/evidence/normalised JSON document into a result set."""
    if not isinstance(document, dict):
        raise CoverageError("results root must be an object")
    result = ResultSet(source_path=source_path, source_kind="json")
    result.run_id = document.get("run_id") if isinstance(document.get("run_id"), str) else None
    result.commit = document.get("commit") if isinstance(document.get("commit"), str) else None
    result.partial = _enclosing_partial(document)
    document_input_hashes = _document_input_hashes(document)
    _collect_collection_errors(document.get("collection_errors"), result)

    raw_runtimes = document.get("runtimes")
    if isinstance(raw_runtimes, list):
        blocks = [block for block in raw_runtimes if isinstance(block, dict)]
        block_modes = {_mode_for(block) for block in blocks}
        default_mode = next(iter(block_modes)) if len(block_modes) == 1 else "unknown"
        for mode in sorted(block_modes):
            _collect_collections(document.get("collections"), mode, result)
        block_run_ids: set[str] = set()
        block_commits: set[str] = set()
        fingerprints: dict[str, set[tuple[Any, Any, str]]] = {}
        block_partial_by_mode: dict[str, bool] = {}
        any_partial = result.partial
        for block in blocks:
            mode = _mode_for(block)
            identity = _runtime_identity(block)
            block_run = block.get("run_id")
            block_run = block_run if isinstance(block_run, str) and block_run else None
            block_commit = block.get("commit")
            block_commit = block_commit if isinstance(block_commit, str) and block_commit else None
            block_partial = _enclosing_partial(block) or result.partial
            block_partial_by_mode[mode] = block_partial_by_mode.get(mode, False) or block_partial
            if block_run:
                block_run_ids.add(block_run)
            if block_commit:
                block_commits.add(block_commit)
            if block_partial:
                any_partial = True
            fingerprints.setdefault(mode, set()).add(
                (
                    block_run,
                    block_commit,
                    json.dumps(identity, sort_keys=True, default=str),
                )
            )
            if identity:
                result.runtimes[mode] = {**identity, "mode": mode}
            elif mode not in result.runtimes:
                result.runtimes[mode] = {"mode": mode}
            _collect_collection_errors(block.get("collection_errors"), result)
            _collect_collections(block.get("collections"), mode, result)
            _collect_outcomes(
                block,
                mode,
                result,
                block_run=block_run,
                block_commit=block_commit,
                block_partial=block_partial,
                default_input_hashes=document_input_hashes,
            )
        _record_block_conflicts(result, block_run_ids, block_commits, fingerprints, any_partial)
        raw_records = document.get("records")
        if isinstance(raw_records, list):
            _collect_outcome_records(
                raw_records,
                default_mode,
                result,
                block_run=result.run_id,
                block_commit=result.commit,
                block_partial=result.partial,
                default_input_hashes=document_input_hashes,
                runtime_partial=block_partial_by_mode,
            )
        _finalize_identity(result)
        return result

    if not (
        isinstance(document.get("tiers"), list)
        or isinstance(document.get("runtime"), dict)
        or isinstance(document.get("records"), list)
        or isinstance(document.get("tests"), list)
    ):
        result.notes.append("results source contains no runtime blocks")
        return result

    mode = _mode_for(document)
    identity = _runtime_identity(document)
    if identity:
        result.runtimes[mode] = {**identity, "mode": mode}
    elif mode not in result.runtimes:
        result.runtimes[mode] = {"mode": mode}
    _collect_collections(document.get("collections"), mode, result)
    _collect_outcomes(
        document,
        mode,
        result,
        block_run=result.run_id,
        block_commit=result.commit,
        block_partial=result.partial,
        default_input_hashes=document_input_hashes,
    )
    _finalize_identity(result)
    return result


def _record_block_conflicts(
    result: ResultSet,
    block_run_ids: set[str],
    block_commits: set[str],
    fingerprints: dict[str, set[tuple[Any, Any, str]]],
    any_partial: bool,
) -> None:
    result.block_run_ids = set(block_run_ids)
    result.block_commits = set(block_commits)
    if any_partial:
        result.identity_conflicts.append(
            "results source contains partial blocks; refusing to merge partial runs"
        )
    for mode, prints in sorted(fingerprints.items()):
        if len(prints) > 1:
            result.identity_conflicts.append(
                f"results source merges conflicting block identities for runtime {mode!r}"
            )


def _finalize_identity(result: ResultSet) -> None:
    record_commits = {outcome.commit for outcome in result.outcomes if outcome.commit}
    record_run_ids = {outcome.run_id for outcome in result.outcomes if outcome.run_id}
    all_commits = set(record_commits) | set(result.block_commits)
    if result.commit:
        all_commits.add(result.commit)
    all_run_ids = set(record_run_ids) | set(result.block_run_ids)
    if result.run_id:
        all_run_ids.add(result.run_id)
    if len(all_commits) > 1:
        result.identity_conflicts.append(
            "results source mixes "
            f"{len(all_commits)} commit identities across document, blocks, and records"
        )
    if len(all_run_ids) > 1:
        result.identity_conflicts.append(
            "results source mixes "
            f"{len(all_run_ids)} run identities across document, blocks, and records"
        )
    if result.outcomes and not all_commits:
        result.missing_commit = True
    if result.identity_conflicts:
        result.merge_conflict = True


def _record_input_hashes(record: dict[str, Any]) -> dict[str, str]:
    """Extract observed input hashes from a record or document, if declared."""
    hashes: dict[str, str] = {}
    raw = record.get("input_hashes")
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(key, str) and key and isinstance(value, str) and value:
                hashes[key.lower()] = value.lower()
    fixture_sha1 = record.get("fixture_sha1")
    if isinstance(fixture_sha1, str) and fixture_sha1:
        hashes.setdefault("fixture", fixture_sha1.lower())
    return hashes


def _asset_version(label: str) -> str | None:
    """Return the game version encoded in a gate asset label, if any."""
    head = re.split(r"[\s\-_]+", label.strip().lower(), maxsplit=1)[0]
    return head if head in _VERSIONS else None


def _canonical_asset_version(kind: Any, label: str) -> str | None:
    """Map a gate asset to its canonical version without stock shadowing.

    A strict battle row uses the canonical ``red-color``/``blue-color``/
    ``yellow`` ROM and fixture, not the stock or vanilla ROM that may share a
    version prefix. Stock and vanilla assets must not override the canonical
    pin, so they are ignored here.
    """
    normalized = label.strip().lower()
    if kind == "rom":
        if normalized == "yellow":
            return "yellow"
        for version in ("red", "blue"):
            if normalized == f"{version}-color":
                return version
        return None
    if kind == "symbol":
        return normalized if normalized in _VERSIONS else None
    if kind == "fixture":
        return _asset_version(normalized)
    return None


def _gate_input_hashes(document: dict[str, Any]) -> dict[str, str]:
    """Extract version-scoped observed input hashes from a gate report.

    A production gate records the operator ROM, symbol, and fixture assets it
    actually opened. Those hashes describe one process, so they are exposed as
    document defaults keyed by version and consumed by every contained record.
    """
    hashes: dict[str, str] = {}
    assets = document.get("assets")
    if isinstance(assets, list):
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            status = asset.get("status")
            if isinstance(status, str) and status.lower() not in ("ok", "pass", "passed"):
                continue
            sha1 = asset.get("actual_sha1", asset.get("expected_sha1"))
            label = asset.get("label")
            if not (isinstance(sha1, str) and sha1 and isinstance(label, str) and label):
                continue
            kind = asset.get("kind")
            version = _canonical_asset_version(kind, label)
            if version is None:
                continue
            if kind == "rom":
                hashes.setdefault(f"rom:{version}", sha1.lower())
            elif kind == "symbol":
                hashes.setdefault(f"sym:{version}", sha1.lower())
            elif kind == "fixture":
                hashes.setdefault(f"input_fixture:{version}", sha1.lower())
    manifest = document.get("fixture_manifest")
    if isinstance(manifest, dict):
        entries = manifest.get("fixtures", manifest.get("entries"))
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                fixture_id = entry.get("id", entry.get("fixture_id"))
                sha1 = entry.get("sha1")
                if isinstance(fixture_id, str) and fixture_id and isinstance(sha1, str) and sha1:
                    hashes.setdefault(f"fixture:{fixture_id.lower()}", sha1.lower())
    return hashes


def _document_input_hashes(document: dict[str, Any]) -> dict[str, str]:
    """Merge document-level gate assets and any explicit input hash declaration."""
    merged = _gate_input_hashes(document)
    merged.update(_record_input_hashes(document))
    return merged


def _collect_outcome_records(
    records: list[Any],
    default_mode: str,
    result: ResultSet,
    *,
    block_run: str | None = None,
    block_commit: str | None = None,
    block_partial: bool = False,
    default_input_hashes: dict[str, str] | None = None,
    runtime_partial: dict[str, bool] | None = None,
) -> None:
    for record in records:
        if not isinstance(record, dict):
            continue
        nodeid = record.get("nodeid")
        if not isinstance(nodeid, str) or not nodeid:
            continue
        runtime = record.get("runtime")
        runtime = runtime if isinstance(runtime, str) and runtime else default_mode
        inherited_partial = block_partial or bool((runtime_partial or {}).get(runtime, False))
        result.outcomes.append(
            _normalise_outcome(
                record,
                nodeid=nodeid,
                status=str(record.get("status", "error")).lower(),
                runtime=runtime,
                inherited_partial=inherited_partial,
                inherited_commit=block_commit,
                inherited_run_id=block_run,
                inherited_hashes=dict(default_input_hashes or {}),
            )
        )


def _collect_outcomes(
    block: dict[str, Any],
    mode: str,
    result: ResultSet,
    *,
    block_run: str | None = None,
    block_commit: str | None = None,
    block_partial: bool = False,
    default_input_hashes: dict[str, str] | None = None,
) -> None:
    seen_nodeids: set[str] = set()
    inherited_hashes = dict(default_input_hashes or {})
    inherited_hashes.update(_record_input_hashes(block))
    records = block.get("records")
    if isinstance(records, list):
        _collect_outcome_records(
            records,
            mode,
            result,
            block_run=block_run,
            block_commit=block_commit,
            block_partial=block_partial,
            default_input_hashes=inherited_hashes,
        )
        seen_nodeids.update(
            normalize_nodeid(record["nodeid"])
            for record in records
            if isinstance(record, dict) and isinstance(record.get("nodeid"), str)
        )
    tests = block.get("tests")
    if isinstance(tests, list):
        for record in tests:
            if not isinstance(record, dict):
                continue
            nodeid = record.get("nodeid")
            if not isinstance(nodeid, str) or not nodeid:
                continue
            status = str(record.get("outcome", "error")).lower()
            if record.get("was_xfail") and status == "skipped":
                status = "xfailed"
            elif record.get("was_xfail") and status == "passed":
                status = "xpassed"
            result.outcomes.append(
                _normalise_outcome(
                    record,
                    nodeid=nodeid,
                    status=status,
                    runtime=mode,
                    inherited_partial=block_partial,
                    inherited_commit=block_commit,
                    inherited_run_id=block_run,
                    inherited_hashes=inherited_hashes,
                )
            )
            seen_nodeids.add(normalize_nodeid(nodeid))
    tiers = block.get("tiers")
    if isinstance(tiers, list):
        for tier in tiers:
            if not isinstance(tier, dict):
                continue
            tier_partial = _enclosing_partial(tier)
            if tier_partial:
                result.identity_conflicts.append(
                    "results source contains a partial or non-passing tier; "
                    "every contained case is treated as partial"
                )
            for case in tier.get("case_results", []):
                if not isinstance(case, dict):
                    continue
                nodeid = case.get("nodeid")
                if not isinstance(nodeid, str) or not nodeid:
                    continue
                result.outcomes.append(
                    _normalise_outcome(
                        case,
                        nodeid=nodeid,
                        status=_status_from_gate(case.get("status")),
                        runtime=mode,
                        inherited_partial=block_partial or tier_partial,
                        inherited_commit=block_commit,
                        inherited_run_id=block_run,
                        inherited_hashes=inherited_hashes,
                    )
                )
                seen_nodeids.add(normalize_nodeid(nodeid))
            for detail in tier.get("failure_details", []):
                if not isinstance(detail, dict):
                    continue
                nodeid = detail.get("nodeid")
                if not isinstance(nodeid, str) or not nodeid:
                    continue
                if normalize_nodeid(nodeid) in seen_nodeids:
                    continue
                outcome = str(detail.get("outcome", "failed")).lower()
                if outcome not in ("xfailed", "xpassed"):
                    outcome = outcome or "failed"
                result.outcomes.append(
                    _normalise_outcome(
                        detail,
                        nodeid=nodeid,
                        status=outcome,
                        runtime=mode,
                        inherited_partial=block_partial or tier_partial,
                        inherited_commit=block_commit,
                        inherited_run_id=block_run,
                        inherited_hashes=inherited_hashes,
                    )
                )


def _parse_junit(text: str, source_path: str) -> ResultSet:
    result = ResultSet(source_path=source_path, source_kind="junit-xml")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise CoverageError(f"invalid JUnit XML: {exc}") from exc
    for testcase in root.iter("testcase"):
        classname = testcase.get("classname") or ""
        name = testcase.get("name") or ""
        if classname:
            module = classname.replace(".", "/")
            if not module.endswith(".py"):
                module = f"{module}.py"
            nodeid = f"{module}::{name}"
        else:
            nodeid = name
        status = _DEFAULT_ACCEPTED_OUTCOMES[0]
        reason = ""
        failure = testcase.find("failure")
        error = testcase.find("error")
        skipped = testcase.find("skipped")
        if failure is not None:
            status = "failed"
            reason = failure.get("message") or ""
        elif error is not None:
            status = "error"
            reason = error.get("message") or ""
        elif skipped is not None:
            marker = f"{skipped.get('type', '')} {skipped.get('message', '')}".lower()
            status = "xfailed" if "xfail" in marker else "skipped"
            reason = skipped.get("message") or skipped.get("type") or ""
        system_out = testcase.findtext("system-out") or ""
        result.outcomes.append(
            _normalise_outcome(
                {"reason": reason, "output_tail": system_out},
                nodeid=nodeid,
                status=status,
                runtime="unknown",
                inherited_partial=False,
                inherited_commit=None,
                inherited_run_id=None,
                inherited_hashes={},
            )
        )
    result.notes.append("JUnit results carry no runtime/build identity")
    _finalize_identity(result)
    return result


def load_results(path: str | Path) -> ResultSet:
    """Load a gate report, JUnit XML, or normalised JSON results file."""
    target = Path(path)
    if not target.is_file():
        raise CoverageError(f"results source not found: {target}")
    text = target.read_text(encoding="utf-8")
    if target.suffix.lower() == ".xml" or text.lstrip().startswith("<"):
        return _parse_junit(text, str(target))
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CoverageError(f"results source is not valid JSON: {target}: {exc}") from exc
    return result_set_from_document(document, source_path=str(target))
