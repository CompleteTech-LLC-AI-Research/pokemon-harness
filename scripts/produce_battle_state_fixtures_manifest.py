"""Fixture writing, provenance, and manifest-row helpers for the producer.

Split out of ``scripts/produce_battle_state_fixtures.py`` for the #122
file-size contract with no behavior change.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from scripts.produce_battle_state_fixtures_drive import (
    _party_summary,
)
from scripts.produce_battle_state_fixtures_model import (
    BATTLE_PARTY_MENU,
    CAPTURE_COMMAND_TEMPLATE,
    PRODUCER_PATH,
    _atomic_write,
    _runtime_label,
    _sha1,
    _sha256_of_bytes,
)


def _fixture_path(fixture_root: Path, version: str, stem: str, peer: bool) -> Path:
    suffix = "-peer" if peer else ""
    return fixture_root / version / f"{stem}{suffix}.state"


def _write_fixture(
    path: Path,
    session,
    *,
    provenance: dict,
    boundary: str,
    assets: dict | None = None,
) -> dict:
    payload = session.save_state()
    _atomic_write(path, payload)
    record = {
        "path": str(path),
        "sha1": hashlib.sha1(payload).hexdigest(),
        "sha256": _sha256_of_bytes(payload),
        "size": len(payload),
        "boundary": boundary,
        "captured_at_utc": provenance.get("captured_at_utc"),
        "party": _party_summary(session),
    }
    sidecar = path.with_suffix(".provenance.json")
    _atomic_write(
        sidecar,
        json.dumps(
            {"provenance": provenance, "fixture": record, "assets": assets or {}},
            indent=2,
            sort_keys=True,
        ).encode(),
    )
    print(
        f"  saved {boundary} fixture {path} sha1={record['sha1']} "
        f"is_in_battle={record['party']['is_in_battle']} "
        f"battle_result={record['party']['battle_result']} "
        f"party_hp={record['party']['party_hp']}",
        flush=True,
    )
    return record


def _capture_provenance(
    *,
    source_relative: str,
    source_sha1: str,
    source_sha256: str,
    version: str,
    variant: str,
    pyboy_version: str,
    pyboy_revision: str,
    max_turns: int,
    boundary: str,
    captured_at_utc: str,
    facts: dict,
) -> dict:
    """Build the ``captured`` provenance every derived boundary row shares."""

    return {
        "status": "captured",
        "producer": PRODUCER_PATH,
        "source_state": (
            f"external {source_relative}; SHA-1 {source_sha1}; "
            f"SHA-256 {source_sha256}; real-play Cable Club link drive from "
            "the admitted battle boundary (no byte-reproduction claim)"
        ),
        "capture_command_template": CAPTURE_COMMAND_TEMPLATE.format(
            version=version, max_turns=max_turns
        ),
        "runtime_identity": (
            f"Python {sys.version.split()[0]}; PyBoy {pyboy_version} "
            f"{_runtime_label()}; fork "
            f"{pyboy_revision}; producer {PRODUCER_PATH} SHA-1 "
            f"{_sha1(Path(__file__).resolve())}"
        ),
        "captured_at_utc": captured_at_utc,
        "verification_method": (
            "real-play Cable Club link drive from the admitted "
            f"{source_relative} bytes (SHA-1 {source_sha1}); the produced pair "
            "is reload-validated against the boundary invariants before this "
            "producer exits"
        ),
        "evidence_note": (
            "operator-managed external input; the fixture is a driven "
            "save-state pair, not a deterministic byte reproduction of an "
            "external state"
        ),
        "version": version,
        "variant": variant,
        "boundary": boundary,
        **facts,
    }


def _manifest_row(
    *,
    slug: str,
    record: dict,
    provenance: dict,
    fixture_root: Path,
    version: str,
    variant: str,
    rom_relative: str,
    rom_sha1: str,
    sym_relative: str,
    sym_sha1: str,
    capture: dict | None = None,
) -> dict:
    """Build one ``kind: boundary`` fixture-manifest row for a captured file."""

    path = Path(record["path"]).resolve().relative_to(fixture_root.resolve())
    row = {
        "id": f"{version}-{variant}-{slug}",
        "path": path.as_posix(),
        "version": version,
        "variant": variant,
        "kind": "boundary",
        "size_bytes": record["size"],
        "sha1": record["sha1"],
        "sha256": record["sha256"],
        "expected_rom": {"path": rom_relative, "sha1": rom_sha1},
        "expected_symbols": {"path": sym_relative, "sha1": sym_sha1},
        "repository_distributed": False,
        "provenance": provenance,
    }
    if capture is not None:
        row["capture"] = capture
    return row


def _validate_fixture(version: str, path: Path, boundary: str) -> dict:
    from tests.test_pyboy_link_session_roms import _open_session

    session = _open_session(version, state_path=path)
    try:
        summary = _party_summary(session)
    finally:
        session.close()
    if boundary == "faint":
        assert summary["is_in_battle"] != 0, f"{path} is not in battle: {summary}"
        assert summary["in_handle_player_mon_fainted"] != 0, (
            f"{path} is not in a forced-replacement boundary: {summary}"
        )
        assert summary["party_menu_type"] == BATTLE_PARTY_MENU, (
            f"{path} not at the battle party menu: {summary}"
        )
        assert any(value == 0 for value in summary["party_hp"]), (
            f"{path} has no fainted party mon: {summary}"
        )
        assert any(value > 0 for value in summary["party_hp"]), (
            f"{path} has no living replacement: {summary}"
        )
    elif boundary == "pre-terminal":
        assert summary["is_in_battle"] != 0, f"{path} must reload into a live battle: {summary}"
    else:
        assert summary["is_in_battle"] == 0, f"{path} did not end the battle: {summary}"
    print(
        f"  reload OK {path} boundary={boundary} "
        f"is_in_battle={summary['is_in_battle']} "
        f"battle_result={summary['battle_result']} "
        f"in_handle_fainted={summary['in_handle_player_mon_fainted']} "
        f"party_hp={summary['party_hp']} active_hp={summary['active_hp']}",
        flush=True,
    )
    return summary


# ---------------------------------------------------------------------------
# Driver entry point
# ---------------------------------------------------------------------------


def _resolve_root(value, repo_root: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else repo_root / path


