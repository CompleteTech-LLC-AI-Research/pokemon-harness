"""Live file-owner checks for the selected runtime modules.

This is separate from the location-only ``check_origins`` CLI. Distribution
``direct_url.json`` and wheel ``RECORD`` are local installation evidence, not
signatures or import-time protection; callers run this bounded check only
after importing the modules they intend to use.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import url2pathname

OwnerApi = dict[str, Any]


def _normal_owner(value: object, *, api: OwnerApi) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return api["_normalise_distribution_name"](value)


def _editable_install_matches(owner: str, project_root: Path, *, api: OwnerApi) -> bool:
    try:
        distribution = api["_distribution"](owner)
        content = distribution.read_text("direct_url.json")
        data = json.loads(content) if content else None
        if not isinstance(data, dict) or data.get("dir_info", {}).get("editable") is not True:
            return False
        url = data.get("url")
        if not isinstance(url, str):
            return False
        parsed = urlparse(url)
        if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
            return False
        url_path = url2pathname(parsed.path)
        recorded_root = Path(url_path).resolve()
        return recorded_root == project_root.resolve()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - untrusted metadata fails closed
        return False


def _module_result(
    name: str,
    module: object,
    expected_owner: object,
    project_root: Path,
    editable_path: Path | None,
    claims: dict[str, list[dict[str, str | None]]],
    *,
    api: OwnerApi,
) -> dict[str, object]:
    result: dict[str, object] = {
        "actual_path": None,
        "expected_owner": expected_owner if isinstance(expected_owner, str) else None,
        "basis": None,
        "record_owner": None,
        "record_path": None,
        "algorithm": None,
        "record_digest": None,
        "live_digest": None,
        "matches": False,
        "issues": [],
    }
    issues = result["issues"]
    if not isinstance(issues, list):
        return result

    owner = _normal_owner(expected_owner, api=api)
    if owner is None:
        issues.append("expected distribution owner is missing or invalid")
        return result
    try:
        raw_path = getattr(module, "__file__", None)
        path = api["_safe_resolve"](Path(raw_path)) if isinstance(raw_path, str) else None
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - the imported module is untrusted
        path = None
    if path is None or not path.is_file():
        issues.append("selected module has no readable file path")
        return result
    result["actual_path"] = str(path)

    matching_claims = claims.get(str(path), [])
    if matching_claims:
        result["basis"] = "record"
        if len(matching_claims) != 1:
            issues.append("module file has ambiguous RECORD claims")
            return result
        claim = matching_claims[0]
        record_owner = _normal_owner(claim.get("owner"), api=api)
        result["record_owner"] = claim.get("owner")
        result["record_path"] = claim.get("record")
        result["algorithm"] = claim.get("algorithm")
        result["record_digest"] = claim.get("digest")
        if record_owner != owner:
            issues.append("module file is recorded by the wrong distribution")
        algorithm = claim.get("algorithm")
        digest = claim.get("digest")
        if not algorithm or not digest:
            issues.append("module file has a hashless or malformed RECORD claim")
            return result
        length = api["_decoded_digest_length"](digest)
        if length is None:
            issues.append("module file has an invalid RECORD digest")
            return result
        live_digest = api["_file_digest"](path, algorithm, length)
        result["live_digest"] = live_digest
        if live_digest is None:
            issues.append("RECORD digest algorithm is unsupported or file cannot be read")
        elif live_digest != digest:
            issues.append("module bytes differ from the expected RECORD digest")
        elif record_owner == owner:
            result["matches"] = True
        return result

    result["basis"] = "editable-source-path"
    if editable_path is None:
        issues.append("module has no RECORD claim or declared editable source path")
        return result
    try:
        expected_path = api["_safe_resolve"](editable_path)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - caller-provided path is untrusted
        expected_path = None
    if expected_path is None or expected_path != path:
        issues.append("module path does not equal its declared editable source path")
        return result
    if not _editable_install_matches(owner, project_root, api=api):
        issues.append("distribution direct_url does not identify this editable checkout")
        return result
    result["matches"] = True
    return result


def attest_selected_module_owners(
    modules: object,
    expected_owners: object,
    *,
    project_root: Path,
    editable_module_paths: object = None,
    api: OwnerApi,
) -> dict[str, object]:
    """Attest actual selected module files to editable roots or RECORD bytes.

    A path claimed by any RECORD is never allowed to fall back to the editable
    rule. This prevents a malformed or competing claim from being hidden by a
    weaker location explanation.
    """

    report: dict[str, object] = {"ok": False, "modules": {}, "errors": []}
    try:
        if not isinstance(modules, Mapping) or not isinstance(expected_owners, Mapping):
            return {**report, "errors": ["modules and expected_owners must be mappings"]}
        modules = dict(modules)
        expected_owners = dict(expected_owners)
        if not modules or not expected_owners:
            return {**report, "errors": ["selected module and owner mappings must be nonempty"]}
        if set(modules) != set(expected_owners):
            return {
                **report,
                "errors": ["selected modules and expected owners must have exact matching keys"],
            }
        if any(not isinstance(name, str) or not name for name in modules):
            return {**report, "errors": ["selected module names must be nonempty text"]}
        if any(not isinstance(owner, str) or not owner for owner in expected_owners.values()):
            return {**report, "errors": ["expected distribution owners must be nonempty text"]}
        if editable_module_paths is None:
            editable_paths: dict[str, object] = {}
        elif isinstance(editable_module_paths, Mapping):
            editable_paths = dict(editable_module_paths)
        else:
            return {**report, "errors": ["editable_module_paths must be a mapping or None"]}
        if any(not isinstance(name, str) or not name for name in editable_paths):
            return {**report, "errors": ["editable module names must be nonempty text"]}
        if not set(editable_paths).issubset(modules):
            return {**report, "errors": ["editable source paths name an unselected module"]}
        roots = api["_site_packages_roots"]()
        claims: dict[str, list[dict[str, str | None]]] = {}
        for root in roots:
            for path, rows in api["_record_claim_rows"](root).items():
                claims.setdefault(path, []).extend(rows)

        results: dict[str, object] = {}
        errors: list[str] = []
        for name in sorted(modules):
            if not isinstance(name, str):
                errors.append("selected module name is not text")
                continue
            expected_owner = expected_owners.get(name)
            editable_path = editable_paths.get(name)
            if editable_path is not None:
                editable_path = Path(editable_path)
                resolved_editable = api["_safe_resolve"](editable_path)
                allowed_source_roots = (
                    Path(project_root) / "src" / "pokered_harness",
                    Path(project_root) / "vendor" / "pyboy-src" / "pyboy",
                )
                if resolved_editable is None or not any(
                    api["_is_within"](resolved_editable, source_root)
                    for source_root in allowed_source_roots
                ):
                    results[name] = {
                        "actual_path": None,
                        "expected_owner": expected_owner,
                        "basis": "editable-source-path",
                        "record_owner": None,
                        "record_path": None,
                        "algorithm": None,
                        "record_digest": None,
                        "live_digest": None,
                        "matches": False,
                        "issues": ["declared editable path is outside the approved source trees"],
                    }
                    errors.append(
                        f"{name}: declared editable path is outside the approved source trees"
                    )
                    continue
            result = _module_result(
                name,
                modules[name],
                expected_owner,
                project_root,
                editable_path,
                claims,
                api=api,
            )
            results[name] = result
            if not result["matches"]:
                issues = result["issues"]
                errors.extend(f"{name}: {issue}" for issue in issues)
        missing = sorted(set(expected_owners) - set(modules))
        errors.extend(f"{name}: selected module is missing" for name in missing)
        report.update({"ok": not errors, "modules": results, "errors": errors})
        return report
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - public verifier must refuse, not traceback
        report["errors"] = ["selected module owner attestation could not complete"]
        return report
