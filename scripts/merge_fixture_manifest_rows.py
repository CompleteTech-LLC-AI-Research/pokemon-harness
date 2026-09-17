#!/usr/bin/env python3
"""Merge captured boundary fixture rows into the release fixture manifest.

``scripts/produce_battle_state_fixtures.py --emit-manifest-rows`` writes the
manifest rows for every boundary fixture it produced.  This utility folds those
rows into ``release-evidence/fixture-manifest.json`` deterministically:

* every input row is validated with the same schema the release validator
  applies (``scripts/validate_fixture_manifest._validate_schema``) before it is
  allowed anywhere near the manifest, and must be a ``kind: boundary`` row;
* a row whose ``id`` is already present replaces that row in place, so
  re-running the merge is idempotent and never reshuffles existing entries;
* a new row is inserted immediately after the last row of the same ``version``
  (or appended when the version is new), keeping the file grouped by game;
* the document keeps its two-space indentation, insertion order, and trailing
  newline, so ``json.dumps(document, indent=2) + "\n"`` round-trips exactly.

The merge never invents digests: sizes, SHA-1/SHA-256, ROM/SYM pins, and
provenance come from the producer that captured the bytes.

Example::

    python scripts/merge_fixture_manifest_rows.py \\
        --manifest release-evidence/fixture-manifest.json \\
        rows-red.json rows-blue.json rows-yellow.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts import validate_fixture_manifest as fixture_manifest

_DEFAULT_MANIFEST = _REPO / "release-evidence" / "fixture-manifest.json"
# ``captured`` is the strict status for a real-play drive: the bytes were
# recorded from an admitted input with a full provenance chain, but no
# byte-reproduction of an external state is claimed.
_CAPTURED_POLICY = (
    "captured means the exact bytes are a real-play drive from the recorded "
    "admitted input, bound to it by SHA-1 and SHA-256, with the same required "
    "provenance fields as verified; no byte-reproduction of an external state "
    "is claimed"
)
# A captured row records the revision of the producer that wrote the bytes.
# Admission refuses a row whose recorded revision is not the committed tool, so
# the manifest can never claim a producer digest the repository does not have.
_RUNTIME_PRODUCER_RE = re.compile(r"producer (?P<path>\S+) SHA-1 (?P<sha1>[0-9a-f]{40})")


def _load_rows(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"rows file not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"rows file is not valid JSON: {path}: {exc}") from exc
        if not isinstance(payload, list) or not payload:
            raise ValueError(f"rows file must contain a non-empty list: {path}")
        for row in payload:
            if not isinstance(row, dict):
                raise TypeError(f"rows file must contain objects: {path}")
            row_id = row.get("id")
            if not isinstance(row_id, str) or not row_id:
                raise ValueError(f"row without an id in {path}")
            if row_id in seen:
                raise ValueError(f"duplicate row id across inputs: {row_id}")
            seen.add(row_id)
            rows.append(row)
    return rows


def _validate_row(document: dict[str, Any], row: dict[str, Any]) -> None:
    """Validate one row with the release schema before it is merged."""

    if row.get("kind") != "boundary":
        raise ValueError(f"only kind=boundary rows may be merged: {row.get('id')}")
    _verify_producer_revision(row)
    synthetic = {
        "manifest_id": document["manifest_id"],
        "manifest_version": document["manifest_version"],
        "fixture_root": document["fixture_root"],
        "asset_policy": document["asset_policy"],
        "fixtures": [row],
    }
    fixture_manifest._validate_schema(synthetic)


def _verify_producer_revision(row: dict[str, Any]) -> None:
    """Refuse a row whose recorded producer revision is not the committed tool."""

    identity = row.get("provenance", {}).get("runtime_identity")
    if not isinstance(identity, str):
        raise TypeError(f"boundary row has no runtime identity: {row.get('id')}")
    match = _RUNTIME_PRODUCER_RE.search(identity)
    if match is None:
        raise ValueError(f"boundary row does not name its producer revision: {row.get('id')}")
    producer = _REPO / match.group("path")
    if not producer.is_file():
        raise ValueError(f"boundary row names a missing producer: {match.group('path')}")
    actual = hashlib.sha1(producer.read_bytes()).hexdigest()
    if actual != match.group("sha1"):
        raise ValueError(
            f"boundary row {row.get('id')} records producer {match.group('path')} "
            f"SHA-1 {match.group('sha1')} but the committed producer is {actual}; "
            "re-capture the fixtures with the committed producer"
        )


def merge(document: dict[str, Any], rows: list[dict[str, Any]]) -> int:
    """Merge ``rows`` into ``document`` in place; return the inserted count."""

    fixtures: list[dict[str, Any]] = document["fixtures"]
    for row in rows:
        _validate_row(document, row)

    inserted = 0
    for row in sorted(rows, key=lambda entry: entry["id"]):
        index = next(
            (position for position, entry in enumerate(fixtures) if entry["id"] == row["id"]),
            None,
        )
        if index is not None:
            fixtures[index] = row
            continue
        anchors = [
            position
            for position, entry in enumerate(fixtures)
            if entry["version"] == row["version"]
        ]
        target = anchors[-1] + 1 if anchors else len(fixtures)
        fixtures.insert(target, row)
        inserted += 1
    return inserted


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _apply_policy(document: dict[str, Any]) -> None:
    policy = document["asset_policy"]
    existing = policy["provenance_policy"]
    if _CAPTURED_POLICY in existing:
        return
    policy["provenance_policy"] = f"{existing}; {_CAPTURED_POLICY}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=_DEFAULT_MANIFEST,
        help="fixture manifest to merge into",
    )
    parser.add_argument(
        "--rows",
        type=Path,
        action="append",
        default=None,
        help="producer --emit-manifest-rows output; repeat per version",
    )
    parser.add_argument("files", type=Path, nargs="*", help="additional rows files")
    args = parser.parse_args(argv)

    sources = list(args.rows or []) + list(args.files)
    if not sources:
        print("no rows files given", file=sys.stderr)
        return 2

    manifest_path = args.manifest.expanduser()
    try:
        raw = manifest_path.read_text(encoding="utf-8")
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise TypeError("manifest root must be an object")
        if raw != json.dumps(document, indent=2) + "\n":
            raise ValueError(f"manifest is not canonically formatted: {manifest_path}")
        fixtures = fixture_manifest._validate_schema(document)
        if len(fixtures) != len(document["fixtures"]):
            raise ValueError("manifest validator dropped rows")
        rows = _load_rows([path.expanduser() for path in sources])
        inserted = merge(document, rows)
        _apply_policy(document)
        fixture_manifest._validate_schema(document)
        payload = json.dumps(document, indent=2) + "\n"
        _atomic_write(manifest_path, payload.encode("utf-8"))
    except (OSError, TypeError, ValueError, KeyError) as exc:
        print(f"fixture manifest merge failed: {exc}", file=sys.stderr)
        return 2

    print(
        f"merged {len(rows)} boundary rows into {manifest_path} "
        f"({inserted} inserted, {len(rows) - inserted} replaced); "
        f"{len(document['fixtures'])} entries total"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
