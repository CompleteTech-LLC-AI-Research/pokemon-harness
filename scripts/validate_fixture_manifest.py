"""Validate external save-state bytes against the release fixture manifest.

The repository intentionally does not distribute ROM-derived ``.state``
files.  Schema-only validation is suitable for an asset-free checkout; a
release or acceptance run must pass ``--fixture-root`` so every listed file is
present and its size, SHA-1, and SHA-256 match exactly.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path, PureWindowsPath
from typing import Any

_HASH_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# ``verified`` means the exact bytes were reproduced from the recorded source;
# ``captured`` means the bytes are a real-play drive recorded with a full
# provenance chain (same required fields) but not a reproduction.  Both are
# strict: only the incomplete ``partial``/``unknown`` statuses may omit the
# runtime/capture/verification fields.
_PROVENANCE_STATUSES = {"partial", "unknown", "verified", "captured"}
_STRICT_PROVENANCE_STATUSES = {"verified", "captured"}
_FIXTURE_KINDS = {"ordinary", "battle", "slots", "boundary"}
_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "release-evidence" / "fixture-manifest.json"


def _error(message: str) -> ValueError:
    return ValueError(message)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise _error(message)


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise _error(f"manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise _error(f"manifest is not valid JSON: {path}: {exc}") from exc

    _require(isinstance(document, dict), "manifest root must be an object")
    return document


def _validate_relative_path(value: Any, field: str) -> None:
    _require(isinstance(value, str) and value, f"{field} must be a non-empty string")
    path = Path(value)
    windows_path = PureWindowsPath(value)
    _require(
        not path.is_absolute() and not windows_path.is_absolute() and not windows_path.drive,
        f"{field} must be relative: {value!r}",
    )
    _require("\\" not in value, f"{field} must use POSIX separators: {value!r}")
    _require(".." not in path.parts, f"{field} must not escape its root: {value!r}")


def _validate_text(value: Any, field: str) -> None:
    _require(isinstance(value, str) and value.strip(), f"{field} must be a non-empty string")


def _validate_input_pin(value: Any, field: str) -> None:
    _require(isinstance(value, dict), f"{field} must be an object")
    _validate_relative_path(value.get("path"), f"{field}.path")
    sha1 = value.get("sha1")
    _require(isinstance(sha1, str) and _HASH_RE.fullmatch(sha1), f"{field}.sha1 is invalid")


def _validate_schema(document: dict[str, Any]) -> list[dict[str, Any]]:
    _require(
        document.get("manifest_id") == "pokered-harness.external-link-fixtures",
        "unexpected manifest_id",
    )
    _require(document.get("manifest_version") == 1, "unsupported manifest_version")
    _validate_relative_path(document.get("fixture_root"), "fixture_root")

    policy = document.get("asset_policy")
    _require(isinstance(policy, dict), "asset_policy must be an object")
    _require(policy.get("repository_distributed") is False, "state assets must remain external")
    _require(
        policy.get("validation_policy")
        == "fail closed on missing, size, SHA-1, or SHA-256 mismatch",
        "asset_policy.validation_policy must be fail-closed",
    )

    fixtures = document.get("fixtures")
    _require(isinstance(fixtures, list) and fixtures, "fixtures must be a non-empty list")
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    for index, fixture in enumerate(fixtures):
        prefix = f"fixtures[{index}]"
        _require(isinstance(fixture, dict), f"{prefix} must be an object")
        fixture_id = fixture.get("id")
        _require(isinstance(fixture_id, str) and fixture_id, f"{prefix}.id is required")
        _require(fixture_id not in seen_ids, f"duplicate fixture id: {fixture_id}")
        seen_ids.add(fixture_id)

        relative_path = fixture.get("path")
        _validate_relative_path(relative_path, f"{prefix}.path")
        _require(relative_path.endswith(".state"), f"{prefix}.path must name a .state file")
        _require(relative_path not in seen_paths, f"duplicate fixture path: {relative_path}")
        seen_paths.add(relative_path)

        _require(fixture.get("kind") in _FIXTURE_KINDS, f"{prefix}.kind is invalid")
        _validate_text(fixture.get("version"), f"{prefix}.version")
        _validate_text(fixture.get("variant"), f"{prefix}.variant")
        size = fixture.get("size_bytes")
        _require(
            isinstance(size, int) and not isinstance(size, bool) and size >= 0,
            f"{prefix}.size_bytes is invalid",
        )
        sha1 = fixture.get("sha1")
        _require(isinstance(sha1, str) and _HASH_RE.fullmatch(sha1), f"{prefix}.sha1 is invalid")
        sha256 = fixture.get("sha256")
        _require(
            isinstance(sha256, str) and _SHA256_RE.fullmatch(sha256),
            f"{prefix}.sha256 is invalid",
        )
        _validate_input_pin(fixture.get("expected_rom"), f"{prefix}.expected_rom")
        _validate_input_pin(fixture.get("expected_symbols"), f"{prefix}.expected_symbols")
        _require(
            fixture.get("repository_distributed") is False,
            f"{prefix}.repository_distributed must be false",
        )

        provenance = fixture.get("provenance")
        _require(isinstance(provenance, dict), f"{prefix}.provenance must be an object")
        _require(
            provenance.get("status") in _PROVENANCE_STATUSES,
            f"{prefix}.provenance.status is invalid",
        )
        for field in ("source_state", "capture_command_template"):
            _require(field in provenance, f"{prefix}.provenance.{field} is required")
            _validate_text(provenance[field], f"{prefix}.provenance.{field}")
        for field in ("runtime_identity", "captured_at_utc", "verification_method"):
            _require(field in provenance, f"{prefix}.provenance.{field} is required")
            value = provenance[field]
            _require(
                value is None or (isinstance(value, str) and value.strip()),
                f"{prefix}.provenance.{field} must be null or a non-empty string",
            )
        if provenance["status"] in _STRICT_PROVENANCE_STATUSES:
            for field in ("runtime_identity", "captured_at_utc", "verification_method"):
                _validate_text(provenance[field], f"{prefix}.provenance.{field}")

    return fixtures


def _hash_file(path: Path) -> tuple[str, str]:
    sha1 = hashlib.sha1()
    sha256 = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha1.update(chunk)
            sha256.update(chunk)
    return sha1.hexdigest(), sha256.hexdigest()


def _validate_assets(fixtures: list[dict[str, Any]], fixture_root: Path) -> None:
    try:
        resolved_root = fixture_root.expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise _error(f"fixture root could not be resolved: {fixture_root}: {exc}") from exc
    if not resolved_root.is_dir():
        raise _error(f"fixture root not found: {fixture_root}")

    for fixture in fixtures:
        relative_path = Path(fixture["path"])
        candidate = resolved_root / relative_path
        try:
            path = candidate.resolve(strict=False)
            path.relative_to(resolved_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise _error(f"fixture path escapes fixture root: {fixture['path']!r}") from exc
        if candidate.is_symlink():
            raise _error(f"fixture must be a regular file, not a symlink: {fixture['path']}")
        if not path.is_file():
            raise _error(f"fixture missing: {path}")
        actual_size = path.stat().st_size
        if actual_size != fixture["size_bytes"]:
            raise _error(
                f"fixture size mismatch for {fixture['path']}: "
                f"expected {fixture['size_bytes']}, got {actual_size}"
            )
        actual_sha1, actual_sha256 = _hash_file(path)
        if actual_sha1 != fixture["sha1"]:
            raise _error(
                f"fixture SHA-1 mismatch for {fixture['path']}: "
                f"expected {fixture['sha1']}, got {actual_sha1}"
            )
        if actual_sha256 != fixture["sha256"]:
            raise _error(
                f"fixture SHA-256 mismatch for {fixture['path']}: "
                f"expected {fixture['sha256']}, got {actual_sha256}"
            )


def _default_fixture_root() -> Path:
    configured = os.environ.get("POKERED_FIXTURE_ROOT")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "link"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=_MANIFEST_PATH)
    parser.add_argument(
        "--fixture-root",
        type=Path,
        default=None,
        help="external tests/fixtures/link directory; required for byte validation",
    )
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="validate the manifest structure without accepting missing external assets",
    )
    args = parser.parse_args(argv)

    try:
        document = _load_manifest(args.manifest)
        fixtures = _validate_schema(document)
        if not args.schema_only:
            _validate_assets(fixtures, (args.fixture_root or _default_fixture_root()).resolve())
    except (OSError, ValueError) as exc:
        print(f"fixture manifest validation failed: {exc}", file=sys.stderr)
        return 2

    mode = "schema" if args.schema_only else "byte"
    print(f"fixture manifest {mode} validation passed: {len(fixtures)} entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
