#!/usr/bin/env python3
"""Declare every admitted boundary fixture in the battle-scenario catalog.

``release-evidence/battle-scenarios.json`` is the public contract mechanics
issues declare a replayable scenario in, and the catalog validator fails closed
on any fixture-manifest row with no scenario
(``catalog is missing manifest fixtures``).  Once
``scripts/merge_fixture_manifest_rows.py`` admits the ``kind: boundary`` rows,
those 18 fixtures must be declared here too, or the release schema gate refuses
the catalog.

The rows are derived, never hand-typed:

* every digest, size, ROM/SYM pin, and provenance field is copied from
  ``release-evidence/fixture-manifest.json`` (the admission contract);
* ``party.count``, ``party.active_slot``, ``capture_boundary.map_id``, and the
  link state are measured from the admitted bytes through a real ROM session,
  so a row cannot describe a state the fixture does not hold;
* the consumer replay bounds are the drive geometry the real-ROM boundary
  tests actually use, not an invented envelope.

Assets are required: the measurement opens each fixture with the pinned
ROM/SYM pair, and the tool refuses to write a catalog it could not measure.
The document keeps its two-space indentation, key order, and trailing newline,
so ``json.dumps(document, indent=2) + "\n"`` round-trips exactly and re-running
the merge is idempotent.

Example::

    python scripts/merge_battle_boundary_scenarios.py \\
        --fixture-root <fixture-root> --rom-root <rom-root> [--write]
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

from scripts import validate_battle_scenarios as scenario_validator
from scripts import validate_fixture_manifest as fixture_manifest

_DEFAULT_MANIFEST = _REPO / "release-evidence" / "fixture-manifest.json"
_DEFAULT_CATALOG = _REPO / "release-evidence" / "battle-scenarios.json"

# The boundary kinds the producer emits, and the scenario metadata each one
# carries.  ``when`` names the ROM phase the bytes sit in: a faint and the
# pre-terminal boundary are both at a live battle command menu, the terminal
# return is after ``EndOfBattle``.
_BOUNDARY_WHEN = {
    "forced-replacement": "pre_command",
    "pre-terminal": "pre_command",
    "terminal": "post_battle",
}
# A distinct battle type keeps the settled one-turn ``link_battle`` scenarios
# the canonical fixture for each version: the coverage dimension resolves its
# cases from those, and a boundary state is not a settled turn.
_BOUNDARY_BATTLE_TYPE = "link_battle_boundary"
_BOUNDARY_REGION = "colosseum"
# ``LINK_STATE_*`` values from the pinned pret sources
# (``constants/serial_constants.asm``): 1 is IN_CABLE_CLUB, 4 is BATTLING.
_LINK_STATE_NAMES = {1: "in_cable_club", 4: "battling"}
_BATTLE_KINDS = (1, 2)
# ``provenance.source_state`` records the driven input as
# ``external <path>; SHA-1 <sha1>; SHA-256 <sha256>; ...``.  The declared
# capture identity is read from that record, never re-derived from the manifest
# lookup, so the catalog restates only what the capture actually names.
_SOURCE_STATE_RE = re.compile(
    r"^external (?P<path>\S+); SHA-1 (?P<sha1>[0-9a-f]{40}); "
    r"SHA-256 (?P<sha256>[0-9a-f]{64});"
)

# The consumer replay geometry, read from the real-ROM boundary tests that
# drive these admitted bytes (``tests/test_mcp_battle_phase_rom.py``), and the
# wall bound of the production-gate tier that runs them.  The catalog declares
# the tests' own constants rather than a guess, so the two cannot drift.
_BOUNDARY_OWNERS = 2
_BOUNDARY_TIER = "local"

# The tests that actually exercise each boundary kind through the public MCP
# surface against the admitted bytes.
_AUTHORED_TESTS = {
    "forced-replacement": [
        "tests/test_mcp_battle_phase_rom.py::test_real_rom_mcp_boundary_reads_fail_closed",
    ],
    "pre-terminal": [
        "tests/test_mcp_battle_phase_rom.py::test_real_rom_mcp_terminal_return_drive_reads",
    ],
    "terminal": [
        "tests/test_mcp_battle_phase_rom.py::test_real_rom_mcp_boundary_reads_fail_closed",
        "tests/test_mcp_battle_phase_rom.py::test_real_rom_mcp_terminal_return_drive_reads",
    ],
}
# A captured row records the runtime that drove the bytes; the catalog echoes
# that identity rather than restating a version by hand.
_RUNTIME_PYTHON_RE = re.compile(r"Python (?P<version>\d+\.\d+\.\d+)")
_RUNTIME_PYBOY_RE = re.compile(r"PyBoy (?P<version>\S+?) (?:\w+ )?runtime")
_RUNTIME_FORK_RE = re.compile(r"fork (?P<revision>[0-9a-f]{40})")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _sha1(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bounds() -> dict[str, Any]:
    """The finite consumer replay bounds for one boundary scenario.

    The frame and input bounds are the boundary tests' own drive geometry, and
    the wall bound is the production-gate tier timeout, so a scenario can never
    declare an envelope the real replay does not run inside.
    """
    from scripts import production_gate
    from tests import test_mcp_battle_phase_rom as boundary_tests

    budget_frames = boundary_tests.BOUNDARY_DRIVE_BUDGET
    spacing = boundary_tests.BOUNDARY_INPUT_SPACING
    per_owner = budget_frames // spacing + 1
    return {
        "max_frames": budget_frames,
        "max_wall_seconds": production_gate.DEFAULT_TIMEOUT_SECONDS[_BOUNDARY_TIER],
        "max_inputs": per_owner * _BOUNDARY_OWNERS,
    }


def _runtime_from_identity(identity: str, fixture_id: str) -> dict[str, Any]:
    """Parse the pinned runtime out of a captured row's recorded identity."""
    python = _RUNTIME_PYTHON_RE.search(identity)
    pyboy = _RUNTIME_PYBOY_RE.search(identity)
    fork = _RUNTIME_FORK_RE.search(identity)
    _require(python is not None, f"{fixture_id} runtime identity names no Python version")
    _require(pyboy is not None, f"{fixture_id} runtime identity names no PyBoy version")
    _require(fork is not None, f"{fixture_id} runtime identity names no fork revision")
    return {
        "python": python.group("version"),
        "pyboy_version": pyboy.group("version"),
        "pyboy_revision": fork.group("revision"),
        "role": None,
    }


def _measure(version: str, path: Path) -> dict[str, Any]:
    """Read the declared party/map/link state from the admitted fixture bytes."""
    from tests.test_pyboy_link_session_roms import _open_session

    session = _open_session(version, state_path=path)
    try:
        symbols = session.symbols

        def read(name: str) -> int | None:
            if symbols.get(name) is None:
                return None
            return symbols.read_u8(session._pyboy.memory, name)

        party_count = read("wPartyCount")
        active_slot = read("wPlayerMonNumber")
        link_state = read("wLinkState")
        map_id = read("wCurMap")
    finally:
        session.close()
    _require(party_count is not None, f"{path} has no wPartyCount symbol")
    _require(link_state is not None, f"{path} has no wLinkState symbol")
    _require(map_id is not None, f"{path} has no wCurMap symbol")
    _require(
        link_state in _LINK_STATE_NAMES,
        f"{path} holds link state {link_state}, which is not a documented boundary state",
    )
    _require(
        active_slot is None or active_slot < party_count,
        f"{path} reports active slot {active_slot} outside its {party_count}-mon party",
    )
    return {
        "party_count": party_count,
        "active_slot": active_slot,
        "link_state": link_state,
        "map_id": map_id,
    }


def _source_fixture_id(row: dict[str, Any]) -> str:
    """The admitted battle fixture the boundary pair was driven from."""
    return f"{row['version']}-{row['variant']}-battle"


def _verified_capture_identity(row: dict[str, Any], source: dict[str, Any]) -> str:
    """Return the capture input's SHA-1 after proving it is the admitted source.

    The catalog declares the input a boundary pair was driven from.  That
    identity must come from the *capture*, not from a same-version lookup: if
    the builder simply took ``source["sha1"]``, a row whose recorded capture
    input had changed would still be declared as the canonical admitted input.
    The recorded ``provenance.source_state`` is therefore parsed and bound to
    the admitted source fixture (path and both digests) before it is published,
    so the catalog can only ever restate an identity the manifest admits.
    """
    fixture_id = row["id"]
    source_id = _source_fixture_id(row)
    source_state = row.get("provenance", {}).get("source_state")
    _require(
        isinstance(source_state, str),
        f"{fixture_id} has no recorded capture input",
    )
    match = _SOURCE_STATE_RE.match(source_state)
    _require(
        match is not None,
        f"{fixture_id} does not record its capture input path, SHA-1, and SHA-256",
    )
    _require(
        match.group("path") == source["path"],
        f"{fixture_id} was captured from {match.group('path')} but the admitted "
        f"{source_id} is {source['path']}",
    )
    _require(
        match.group("sha1") == source["sha1"],
        f"{fixture_id} records capture input SHA-1 {match.group('sha1')} but the "
        f"admitted {source_id} is {source['sha1']}",
    )
    _require(
        match.group("sha256") == source["sha256"],
        f"{fixture_id} records capture input SHA-256 {match.group('sha256')} but the "
        f"admitted {source_id} is {source['sha256']}",
    )
    return match.group("sha1")


def build_scenario(
    row: dict[str, Any],
    measured: dict[str, Any],
    source: dict[str, Any],
) -> dict[str, Any]:
    """Build one boundary scenario from the manifest row and measured state."""
    fixture_id = row["id"]
    provenance = row["provenance"]
    boundary = provenance["boundary"]
    _require(boundary in _BOUNDARY_WHEN, f"{fixture_id} has unknown boundary {boundary!r}")
    source_id = _source_fixture_id(row)
    source_sha1 = _verified_capture_identity(row, source)
    bounds = _bounds()
    expectations = [
        "fixture size, SHA-1, and SHA-256 match release-evidence/fixture-manifest.json",
        (
            f"the pair was driven from the admitted {source_id} bytes "
            f"(SHA-1 {source_sha1}) and is not a byte-for-byte reproduction "
            "of an external state"
        ),
        (
            "party count, active slot, map id, and link state are measured from the "
            "admitted bytes through a pinned ROM session, not asserted from memory"
        ),
        (
            "the declared bounds are the real-ROM boundary drive geometry: "
            f"{bounds['max_frames']} paired frames, at most {bounds['max_inputs']} "
            f"owner inputs, and the {_BOUNDARY_TIER} tier wall cap of "
            f"{bounds['max_wall_seconds']:.0f}s"
        ),
        (
            "the scenario is declared for observation only; replay loads the "
            "immutable bytes and performs no runtime RAM, party, move, PP, or "
            "serial mutation"
        ),
    ]
    return {
        "scenario_id": fixture_id.replace("-", "_"),
        "game": {
            "version": row["version"],
            "variant": row["variant"],
            "rom_path": row["expected_rom"]["path"],
            "rom_sha1": row["expected_rom"]["sha1"],
            "sym_path": row["expected_symbols"]["path"],
            "sym_sha1": row["expected_symbols"]["sha1"],
        },
        "battle": {
            "type": _BOUNDARY_BATTLE_TYPE,
            "mode": "colosseum",
            "roles": ["listen", "connect"],
            "paths": [row["path"]],
        },
        "capture_boundary": {
            "stage": boundary,
            "when": _BOUNDARY_WHEN[boundary],
            "region": _BOUNDARY_REGION,
            "map_id": measured["map_id"],
            "link_state": _LINK_STATE_NAMES[measured["link_state"]],
        },
        "party": {
            "count": measured["party_count"],
            "active_slot": measured["active_slot"],
            "mons": [],
        },
        "inventory": None,
        "opponent": None,
        "runtime": _runtime_from_identity(provenance["runtime_identity"], fixture_id),
        "required_prior_actions": [
            "reload the admitted primary/peer pair through the public load_state tool",
            "install the serial bridge before driving any further ROM turn",
        ],
        "capture_bounds": bounds,
        "fixture": {
            "path": row["path"],
            "repository_distributed": False,
            "size_bytes": row["size_bytes"],
            "sha1": row["sha1"],
            "sha256": row["sha256"],
            "fixture_id": fixture_id,
        },
        "provenance": {
            "status": provenance["status"],
            "producer": provenance["producer"],
            "source_fixture_id": source_id,
            "input_fixture_sha1": source_sha1,
            "input_sequence": (
                f"real-play Cable Club link drive from {source_id} to the "
                f"{boundary.replace('-', ' ')} boundary (no RAM writes)"
            ),
            "capture_command_template": provenance["capture_command_template"],
            "runtime_identity": provenance["runtime_identity"],
            "captured_at_utc": provenance["captured_at_utc"],
            "verification_method": provenance["verification_method"],
        },
        "evidence_class": {
            "expectations": expectations,
            "authored_tests": list(_AUTHORED_TESTS[boundary]),
            "real_rom_execution": None,
        },
    }


def merge(
    document: dict[str, Any],
    manifest: dict[str, Any],
    *,
    fixture_root: Path,
) -> tuple[int, int]:
    """Declare a scenario for every admitted boundary fixture.

    A row this tool owns is refreshed in place from the current manifest row
    and measured bytes, so re-running after a re-capture or a bounds change
    updates the declaration instead of silently keeping a stale one.  Rows the
    tool does not own (the committed ordinary/battle scenarios) are never
    touched.  Returns the ``(refreshed, inserted)`` row counts.
    """
    rows = {row["id"]: row for row in manifest["fixtures"]}
    owned = {
        scenario["fixture"]["fixture_id"]
        for scenario in document["scenarios"]
        if scenario.get("battle", {}).get("type") == _BOUNDARY_BATTLE_TYPE
    }
    generated: dict[str, dict[str, Any]] = {}
    for row in manifest["fixtures"]:
        if row.get("kind") != "boundary":
            continue
        fixture_id = row["id"]
        source_id = _source_fixture_id(row)
        _require(source_id in rows, f"{fixture_id} names no admitted source fixture {source_id}")
        path = fixture_root / row["path"]
        _require(path.is_file(), f"{fixture_id} bytes are not present at {row['path']}")
        _require(
            _sha1(path) == row["sha1"],
            f"{fixture_id} bytes do not match the admitted SHA-1",
        )
        measured = _measure(row["version"], path)
        generated[fixture_id] = build_scenario(row, measured, rows[source_id])
        print(
            f"declared {fixture_id}: map={measured['map_id']} "
            f"link_state={_LINK_STATE_NAMES[measured['link_state']]} "
            f"party={measured['party_count']} active_slot={measured['active_slot']}",
            flush=True,
        )

    # Walk the committed scenarios, refreshing the owned rows in place and
    # recording where each version's block ends so new rows append to it.
    ordered: list[dict[str, Any]] = []
    last_index: dict[str, int] = {}
    refreshed = 0
    for scenario in document["scenarios"]:
        fixture_id = scenario["fixture"]["fixture_id"]
        if fixture_id in generated:
            ordered.append(generated[fixture_id])
            refreshed += 1
        else:
            ordered.append(scenario)
        last_index[scenario["game"]["version"]] = len(ordered)

    # Insert any newly admitted rows after the last scenario of their version,
    # keeping the file grouped by game.
    inserted = 0
    pending: dict[str, list[dict[str, Any]]] = {}
    for fixture_id, scenario in generated.items():
        if fixture_id in owned:
            continue
        pending.setdefault(scenario["game"]["version"], []).append(scenario)
        inserted += 1
    for version, items in pending.items():
        position = last_index.get(version, len(ordered))
        ordered[position:position] = items
        last_index = {
            key: value + len(items) if value >= position else value
            for key, value in last_index.items()
        }
        last_index[version] = position + len(items)

    document["scenarios"] = ordered
    return refreshed, inserted


def _apply_policy(document: dict[str, Any]) -> None:
    """Restate the catalog provenance policy for the strict statuses."""
    document["asset_policy"]["provenance_policy"] = (
        "verified or captured requires runtime_identity, captured_at_utc, and "
        "verification_method; captured and derived additionally require "
        "source_fixture_id and input_sequence; partial/unknown never feed a PASS"
    )


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=_DEFAULT_MANIFEST)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--rom-root", type=Path, required=True)
    parser.add_argument(
        "--write",
        action="store_true",
        help="write the extended catalog; without it the run is a dry run",
    )
    args = parser.parse_args(argv)

    catalog_path = args.catalog.expanduser()
    try:
        raw = catalog_path.read_text(encoding="utf-8")
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise TypeError("catalog root must be an object")
        if raw != json.dumps(document, indent=2, ensure_ascii=False) + "\n":
            raise ValueError(f"catalog is not canonically formatted: {catalog_path}")
        manifest = json.loads(args.manifest.expanduser().read_text(encoding="utf-8"))
        fixture_manifest._validate_schema(manifest)

        # The measurement opens the pinned ROM through the canonical test
        # helpers, which read these roots from the environment at import time.
        os.environ["POKERED_ROM_ROOT"] = str(args.rom_root.expanduser())
        os.environ["POKERED_FIXTURE_ROOT"] = str(args.fixture_root.expanduser())
        if os.environ.get("POKERED_SKIP_SHA1"):
            raise ValueError("POKERED_SKIP_SHA1 must not be set while declaring scenarios")

        refreshed, inserted = merge(
            document,
            manifest,
            fixture_root=args.fixture_root.expanduser(),
        )
        if refreshed or inserted:
            _apply_policy(document)
        scenario_validator._validate_schema(document, manifest)
        payload = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        if args.write:
            _atomic_write(catalog_path, payload.encode("utf-8"))
    except (OSError, TypeError, ValueError, KeyError) as exc:
        print(f"battle-scenario merge failed: {exc}", file=sys.stderr)
        return 2

    total = len(document["scenarios"])
    verb = "wrote" if args.write else "dry run:"
    print(
        f"{verb} {total} scenarios to {catalog_path} ({refreshed} refreshed, {inserted} inserted)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
