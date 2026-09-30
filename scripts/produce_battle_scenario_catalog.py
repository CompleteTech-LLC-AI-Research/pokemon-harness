"""Catalog loading, metadata validation, and producer/asset verification.

Split out of ``scripts/produce_battle_scenario.py`` for the #122 file-size
contract with no behavior change: the code below is copied verbatim except that
calls to facade-owned, monkeypatch-patched entry points resolve through
``_entry`` so attribute patches on the loaded producer module stay visible.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.produce_battle_scenario_model import (
    _CAPTURE_RECORD_FIELDS,
    _LINK_STATE_CODES,
    _PINNED_FIXTURE_FIELDS,
    _PRODUCER_KINDS,
    _PROVENANCE_STATUSES,
    _RUNTIME_MEASUREMENTS,
    _RUNTIME_MODES,
    _STRICT_PROVENANCE_STATUSES,
    _UNMEASURED_RUNTIME,
    ScenarioBlocked,
    ScenarioRefusal,
    _require,
)


def load_catalog(path: str | Path) -> dict[str, Any]:
    """Load a scenario catalog and require a non-empty ``scenarios`` list."""
    target = Path(path)
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScenarioRefusal(f"catalog not found: {target}") from exc
    except json.JSONDecodeError as exc:
        raise ScenarioRefusal(f"catalog is not valid JSON: {target}: {exc}") from exc

    _require(isinstance(document, dict), "catalog root must be an object")
    scenarios = document.get("scenarios")
    _require(
        isinstance(scenarios, list) and scenarios, "catalog.scenarios must be a non-empty list"
    )
    for index, scenario in enumerate(scenarios):
        _require(isinstance(scenario, dict), f"catalog.scenarios[{index}] must be an object")
        scenario_id = scenario.get("scenario_id")
        _require(
            isinstance(scenario_id, str) and scenario_id.strip(),
            f"catalog.scenarios[{index}].scenario_id is required",
        )
    return document


def scenario_index(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return scenarios keyed by ID, refusing duplicate IDs."""
    index: dict[str, dict[str, Any]] = {}
    for scenario in catalog["scenarios"]:
        scenario_id = scenario["scenario_id"]
        if scenario_id in index:
            raise ScenarioRefusal(f"duplicate scenario id: {scenario_id}")
        index[scenario_id] = scenario
    return index


def find_scenario(catalog: dict[str, Any], scenario_id: str) -> dict[str, Any]:
    """Return the declared scenario or raise an explicit BLOCKED refusal."""
    index = scenario_index(catalog)
    if scenario_id not in index:
        raise ScenarioBlocked(f"required scenario {scenario_id!r} is not declared in the catalog")
    return index[scenario_id]


def scenario_status(catalog: dict[str, Any], scenario_id: str) -> str:
    """Return ``READY`` or ``BLOCKED``; ``BLOCKED`` is never a passing result."""
    try:
        find_scenario(catalog, scenario_id)
    except ScenarioBlocked:
        return "BLOCKED"
    return "READY"


def _positive_int(value: Any, name: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
        or float(value) != int(value)
    ):
        raise ScenarioRefusal(f"{name} must be a finite positive integer")
    return int(value)


def _is_digest(value: Any, length: int) -> bool:
    """Return whether ``value`` is a lowercase hex digest of ``length`` characters."""
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )


def validate_bounds(
    *,
    max_frames: Any,
    max_wall_seconds: Any,
    max_inputs: Any,
) -> dict[str, Any]:
    """Validate finite positive capture bounds and return normalized values."""
    frames = _positive_int(max_frames, "max_frames")
    inputs = _positive_int(max_inputs, "max_inputs")
    if (
        isinstance(max_wall_seconds, bool)
        or not isinstance(max_wall_seconds, (int, float))
        or not math.isfinite(float(max_wall_seconds))
        or float(max_wall_seconds) <= 0
    ):
        raise ScenarioRefusal("max_wall_seconds must be finite and greater than zero")
    return {
        "max_frames": frames,
        "max_wall_seconds": float(max_wall_seconds),
        "max_inputs": inputs,
    }


def validate_scenario_metadata(scenario: dict[str, Any], scenario_id: str) -> None:
    """Refuse incomplete or unverifiable declared metadata before any emulator work.

    A capture is only as trustworthy as the metadata it asserts and records, so
    every field the capture later reads is screened here.  Reading these fields
    with ``[]`` deep inside the drive would surface a missing key as an opaque
    ``KeyError``, and reading them with ``.get`` would silently accept a
    ``None`` boundary as an unasserted precondition.
    """
    if not isinstance(scenario, dict):
        raise ScenarioRefusal(f"scenario {scenario_id!r} is not a declared object")
    game = scenario.get("game")
    if not isinstance(game, dict) or not _is_digest(game.get("rom_sha1"), 40):
        raise ScenarioRefusal(f"scenario {scenario_id!r} must declare a pinned ROM SHA-1")
    if not _is_digest(game.get("sym_sha1"), 40):
        raise ScenarioRefusal(f"scenario {scenario_id!r} must declare a pinned symbol SHA-1")

    bounds = scenario.get("capture_bounds")
    if not isinstance(bounds, dict):
        raise ScenarioRefusal(f"scenario {scenario_id!r} must declare capture_bounds")
    validate_bounds(
        max_frames=bounds.get("max_frames"),
        max_wall_seconds=bounds.get("max_wall_seconds"),
        max_inputs=bounds.get("max_inputs"),
    )

    provenance = scenario.get("provenance")
    if not isinstance(provenance, dict) or provenance.get("status") not in _PROVENANCE_STATUSES:
        raise ScenarioRefusal(
            f"scenario {scenario_id!r} must declare a provenance status in "
            f"{list(_PROVENANCE_STATUSES)}"
        )
    fixture = scenario.get("fixture")
    if not isinstance(fixture, dict):
        raise ScenarioRefusal(f"scenario {scenario_id!r} must declare its fixture block")
    if provenance["status"] == "verified":
        absent = [field for field in _PINNED_FIXTURE_FIELDS if not fixture.get(field)]
        if absent:
            raise ScenarioRefusal(
                f"scenario {scenario_id!r} claims verified provenance but does not declare "
                f"{', '.join(absent)}; an unverified byte comparison must not be admitted "
                "as verified provenance"
            )
        if not _is_digest(fixture.get("sha1"), 40) or not _is_digest(fixture.get("sha256"), 64):
            raise ScenarioRefusal(
                f"scenario {scenario_id!r} declares malformed fixture sha1/sha256 digests"
            )
        # ``size_bytes`` is compared against the produced length, so a boolean or
        # numeric-string value (``True == 1``) could admit a one-byte file as a
        # verified reproduction.  Require a real positive integer, exactly as the
        # public catalog validator does: an integral float such as ``24.0`` reads
        # as the same length here but is refused there, so admitting it here would
        # make the two screens disagree about the same declaration.
        declared_size = fixture.get("size_bytes")
        if isinstance(declared_size, bool) or not isinstance(declared_size, int):
            raise ScenarioRefusal(
                f"scenario {scenario_id!r} fixture.size_bytes must be a positive integer, "
                f"got {declared_size!r}"
            )
        _positive_int(declared_size, f"scenario {scenario_id!r} fixture.size_bytes")

    # ``verified`` and ``captured`` are the strict statuses: each claims a real
    # recorded run, so it must name the runtime it was produced on, when it was
    # recorded, and how it was verified.  The public catalog validator refuses a
    # strict row that omits any of these, so the two screens must agree here.
    if provenance["status"] in _STRICT_PROVENANCE_STATUSES:
        for key in ("runtime_identity", "captured_at_utc", "verification_method"):
            value = provenance.get(key)
            if not isinstance(value, str) or not value.strip():
                raise ScenarioRefusal(
                    f"scenario {scenario_id!r}.provenance.{key} is required for "
                    f"{provenance['status']} provenance"
                )

    # A ``captured`` row claims a real-play drive from a specific admitted input,
    # so it must name that input by id, pin it by SHA-1, and describe the input
    # sequence.  The public validator cross-references the pin against the
    # manifest; here the binding itself must at least be complete.
    if provenance["status"] == "captured":
        source_fixture_id = provenance.get("source_fixture_id")
        if not isinstance(source_fixture_id, str) or not source_fixture_id.strip():
            raise ScenarioRefusal(
                f"scenario {scenario_id!r}.provenance.source_fixture_id is required for "
                "captured provenance"
            )
        if not _is_digest(provenance.get("input_fixture_sha1"), 40):
            raise ScenarioRefusal(
                f"scenario {scenario_id!r}.provenance.input_fixture_sha1 is required for "
                "captured provenance"
            )
        input_sequence = provenance.get("input_sequence")
        if not (
            (isinstance(input_sequence, str) and input_sequence.strip())
            or (isinstance(input_sequence, list) and input_sequence)
        ):
            raise ScenarioRefusal(
                f"scenario {scenario_id!r}.provenance.input_sequence is required for "
                "captured provenance"
            )

    boundary = scenario.get("capture_boundary")
    if not isinstance(boundary, dict):
        raise ScenarioRefusal(
            f"scenario {scenario_id!r} must declare capture_boundary; a capture cannot "
            "assert an undeclared boundary"
        )
    for field in ("stage", "when"):
        declared = boundary.get(field)
        if not isinstance(declared, str) or not declared.strip():
            raise ScenarioRefusal(f"scenario {scenario_id!r} must declare capture_boundary.{field}")
    map_id = boundary.get("map_id")
    if isinstance(map_id, bool) or not isinstance(map_id, int) or map_id < 0:
        raise ScenarioRefusal(
            f"scenario {scenario_id!r} must declare a non-negative integer capture_boundary.map_id"
        )
    label = boundary.get("link_state")
    if not isinstance(label, str) or label not in _LINK_STATE_CODES:
        raise ScenarioRefusal(
            f"scenario {scenario_id!r} declares link_state {label!r}, which has no "
            "verified wLinkState encoding; refusing to assert an unverified value"
        )


def resolve_pins(pins: Any, rom: str | Path, sym: str | Path) -> tuple[str, str]:
    """Resolve ``rom``/``sym`` against VERSIONS.md, refusing unpinned inputs."""
    rom_pin = pins.sha1_for_path(rom)
    if rom_pin is None:
        raise ScenarioRefusal(f"ROM is not pinned in VERSIONS.md: {rom}")
    sym_pin = pins.symbol_sha1_for_path(sym)
    if sym_pin is None:
        raise ScenarioRefusal(f"symbol file is not pinned in VERSIONS.md: {sym}")
    return rom_pin, sym_pin


def validate_scenario_pins(scenario: dict[str, Any], rom_pin: str, sym_pin: str) -> None:
    """Refuse when the resolved pins disagree with the declared scenario."""
    game = scenario["game"]
    if game["rom_sha1"] != rom_pin:
        raise ScenarioRefusal(
            f"resolved ROM pin {rom_pin} disagrees with scenario {game['rom_sha1']}"
        )
    if game["sym_sha1"] != sym_pin:
        raise ScenarioRefusal(
            f"resolved symbol pin {sym_pin} disagrees with scenario {game['sym_sha1']}"
        )


def verify_pinned_assets(
    rom: str | Path,
    sym: str | Path,
    rom_pin: str,
    sym_pin: str,
) -> None:
    """Refuse when the on-disk ROM/symbol assets disagree with the resolved pins.

    Pin resolution matches a documented path suffix; it does not read the
    asset. Verify the actual bytes so a missing or substituted file cannot be
    recorded in the capture plan as if it were the pinned asset.
    """
    for path, expected, label in (
        (rom, rom_pin, "ROM"),
        (sym, sym_pin, "symbol file"),
    ):
        target = Path(path)
        if not target.is_file():
            raise ScenarioRefusal(f"{label} not found: {target}")
        actual = sha1_file(target)
        if actual != expected:
            raise ScenarioRefusal(f"{label} SHA-1 mismatch: expected {expected}, got {actual}")


def sha1_file(path: str | Path) -> str:
    """Return the SHA-1 of a file using chunked reads."""
    digest = hashlib.sha1()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _repository_producer_locator(label: str) -> bool:
    """Return whether a producer locator names a readable in-tree script path."""
    if " " in label or not label.endswith(".py"):
        return False
    path = Path(label)
    return not path.is_absolute() and ".." not in path.parts


def verify_declared_producer(
    scenario: dict[str, Any],
    scenario_id: str,
    repo_root: str | Path,
) -> dict[str, Any]:
    """Resolve the declared producer identity, refusing what cannot be verified.

    A fixture's identity has three parts: the declared locator, its kind, and
    the digest actually observed here.  A declaration that pins an expected
    ``producer_sha1`` is compared against the observed bytes, so a changed or
    substituted producer is refused instead of being fingerprinted and accepted.
    A verified entry with no pinned digest is recorded with its identity
    explicitly unqualified rather than described as verified lineage, and its
    repository producer must still be readable so the capture can be replayed.
    ``partial``/``derived``/``unknown`` entries may name an external or operator
    producer that need not exist in this tree.
    """
    provenance = scenario["provenance"]
    status = provenance.get("status")
    named = provenance.get("producer")
    declared_sha1 = provenance.get("producer_sha1")
    if declared_sha1 is not None and not _is_digest(declared_sha1, 40):
        raise ScenarioRefusal(
            f"scenario {scenario_id!r} declares a malformed producer_sha1 "
            f"{declared_sha1!r}; expected a 40-character lowercase hex digest"
        )
    identity: dict[str, Any] = {
        "locator": None,
        "kind": None,
        "declared_sha1": declared_sha1,
        "observed_sha1": None,
        "identity_verified": False,
    }
    if named is None:
        if status == "verified":
            raise ScenarioRefusal(
                f"scenario {scenario_id!r} claims verified provenance but names no producer"
            )
        return identity
    if not isinstance(named, str) or not named.strip():
        raise ScenarioRefusal(
            f"scenario {scenario_id!r} declares a non-textual provenance producer: {named!r}"
        )
    label = named.strip()
    declared_kind = provenance.get("producer_kind")
    if declared_kind is None:
        declared_kind = "repository" if _repository_producer_locator(label) else "derived"
    if declared_kind not in _PRODUCER_KINDS:
        raise ScenarioRefusal(
            f"scenario {scenario_id!r} declares producer_kind {declared_kind!r}, which is not "
            f"one of {list(_PRODUCER_KINDS)}"
        )
    identity["locator"] = label
    identity["kind"] = declared_kind
    if declared_kind == "repository":
        target = Path(repo_root) / label
        if target.is_file():
            identity["observed_sha1"] = sha1_file(target)
        elif status == "verified":
            raise ScenarioRefusal(
                f"scenario {scenario_id!r} names producer {label!r}, which is not present at "
                f"{target}; the declared lineage cannot be reproduced from this tree"
            )
    if status == "verified":
        if declared_kind == "derived":
            raise ScenarioRefusal(
                f"scenario {scenario_id!r} claims verified provenance from a derived "
                f"transformation {label!r}; derived bytes must be declared as derived provenance"
            )
        if declared_kind == "external" and declared_sha1 is None:
            raise ScenarioRefusal(
                f"scenario {scenario_id!r} claims verified provenance from an external producer "
                f"{label!r} without pinning producer_sha1; the identity cannot be verified here"
            )
    observed = identity["observed_sha1"]
    if declared_sha1 is not None and observed is not None and observed != declared_sha1:
        raise ScenarioRefusal(
            f"scenario {scenario_id!r} declares producer_sha1 {declared_sha1} but {label!r} "
            f"hashes to {observed}; a changed producer cannot back a verified fixture"
        )
    identity["identity_verified"] = (
        status == "verified" and declared_sha1 is not None and observed == declared_sha1
    )
    return identity


def validate_capture_record(record: Any, scenario_id: str) -> None:
    """Refuse a capture record that cannot be admitted as validated metadata.

    The record is the only machine-readable evidence a consumer receives, so a
    record that omits the producer, asset, boundary, or output identity is
    refused *before* the produced bytes are published: a fixture whose metadata
    cannot be admitted must not exist on disk as if it had been admitted.
    """
    if not isinstance(record, dict):
        raise ScenarioRefusal(f"capture record for {scenario_id!r} is not an object")
    absent = [field for field in _CAPTURE_RECORD_FIELDS if record.get(field) is None]
    if absent:
        raise ScenarioRefusal(f"capture record for {scenario_id!r} is missing {', '.join(absent)}")
    producer = record.get("producer")
    if not isinstance(producer, str) or not producer.strip():
        raise ScenarioRefusal(f"capture record for {scenario_id!r} does not name its producer")
    if not _is_digest(record.get("producer_sha1"), 40):
        raise ScenarioRefusal(
            f"capture record for {scenario_id!r} does not record a producer digest"
        )
    for field, label in (("rom_sha1", "ROM"), ("sym_sha1", "symbol")):
        if not _is_digest(record.get(field), 40):
            raise ScenarioRefusal(
                f"capture record for {scenario_id!r} does not record the pinned {label} digest"
            )

    output = record.get("output")
    if not isinstance(output, dict):
        raise ScenarioRefusal(f"capture record for {scenario_id!r} has no output block")
    if not isinstance(output.get("path"), str) or not output["path"].strip():
        raise ScenarioRefusal(f"capture record for {scenario_id!r} does not record the output path")
    size = output.get("size_bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise ScenarioRefusal(
            f"capture record for {scenario_id!r} does not record a positive output size"
        )
    if not _is_digest(output.get("sha1"), 40) or not _is_digest(output.get("sha256"), 64):
        raise ScenarioRefusal(
            f"capture record for {scenario_id!r} does not record output sha1/sha256 digests"
        )

    sequence = record.get("input_sequence")
    if not isinstance(sequence, list) or any(not isinstance(step, str) for step in sequence):
        raise ScenarioRefusal(
            f"capture record for {scenario_id!r} does not record a replayable input history"
        )
    observed = record.get("observed_boundary")
    if (
        not isinstance(observed, dict)
        or not isinstance(observed.get("map_id"), int)
        or not isinstance(observed.get("link_state_raw"), int)
    ):
        raise ScenarioRefusal(
            f"capture record for {scenario_id!r} does not record the observed boundary"
        )
    reproduction = record.get("reproduction")
    if not isinstance(reproduction, dict) or not isinstance(reproduction.get("matches"), bool):
        raise ScenarioRefusal(
            f"capture record for {scenario_id!r} does not record the reproduction comparison"
        )
    for field in ("inputs_used", "frames_used"):
        value = record.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ScenarioRefusal(f"capture record for {scenario_id!r} does not record {field}")
    wall_seconds = record.get("wall_seconds")
    if (
        isinstance(wall_seconds, bool)
        or not isinstance(wall_seconds, (int, float))
        or not math.isfinite(float(wall_seconds))
        or float(wall_seconds) < 0
    ):
        raise ScenarioRefusal(f"capture record for {scenario_id!r} does not record wall_seconds")
    declared_bounds = record.get("declared_bounds")
    if not isinstance(declared_bounds, dict):
        raise ScenarioRefusal(
            f"capture record for {scenario_id!r} does not record the effective bounds"
        )
    try:
        effective = validate_bounds(
            max_frames=declared_bounds.get("max_frames"),
            max_wall_seconds=declared_bounds.get("max_wall_seconds"),
            max_inputs=declared_bounds.get("max_inputs"),
        )
    except ScenarioRefusal as exc:
        raise ScenarioRefusal(
            f"capture record for {scenario_id!r} records invalid effective bounds: {exc}"
        ) from exc
    for field, limit in (("frames_used", "max_frames"), ("inputs_used", "max_inputs")):
        if record[field] > effective[limit]:
            raise ScenarioRefusal(
                f"capture record for {scenario_id!r} records {field}={record[field]}, which "
                f"exceeds the effective {limit}={effective[limit]}"
            )
    if float(wall_seconds) > effective["max_wall_seconds"]:
        raise ScenarioRefusal(
            f"capture record for {scenario_id!r} records wall_seconds={wall_seconds}, which "
            f"exceeds the effective max_wall_seconds={effective['max_wall_seconds']}"
        )
    runtime = record.get("runtime")
    measurement = record.get("runtime_measurement")
    runtime_identity = record.get("runtime_identity")
    if measurement not in _RUNTIME_MEASUREMENTS:
        raise ScenarioRefusal(
            f"capture record for {scenario_id!r} does not record whether its runtime "
            "identity was measured"
        )
    if measurement == "not_measured":
        # An injected session never inspected the capturing process, so the
        # record may carry no mode, interpreter, or identity for one: a record
        # that keeps the caller's request as if it were an observation is
        # exactly the unqualified claim this refusal exists to stop.
        if runtime != _UNMEASURED_RUNTIME:
            raise ScenarioRefusal(
                f"capture record for {scenario_id!r} records runtime {runtime!r} without "
                "measuring it"
            )
        if record.get("python") is not None:
            raise ScenarioRefusal(
                f"capture record for {scenario_id!r} records an interpreter it did not run"
            )
        if runtime_identity is not None:
            raise ScenarioRefusal(
                f"capture record for {scenario_id!r} records a runtime identity it did not measure"
            )
    else:
        # A measured capture must label itself with the mode it measured and
        # carry that same identity: a label that disagrees with the identity
        # describes a capture environment that was never observed.
        if runtime not in _RUNTIME_MODES:
            raise ScenarioRefusal(
                f"capture record for {scenario_id!r} claims a measured runtime {runtime!r}, "
                f"which is outside {list(_RUNTIME_MODES)}"
            )
        if (
            not isinstance(runtime_identity, dict)
            or runtime_identity.get("mode") != runtime
            or runtime_identity.get("measured") is not True
            or not isinstance(runtime_identity.get("executable"), str)
            or not runtime_identity["executable"].strip()
        ):
            raise ScenarioRefusal(
                f"capture record for {scenario_id!r} records an unmeasurable runtime identity"
            )


def verify_input_fixture(path: str | Path, expected_sha1: str | None) -> str:
    """Return the input SHA-1, refusing a mismatch against the declared value."""
    target = Path(path)
    if not target.is_file():
        raise ScenarioRefusal(f"input fixture not found: {target}")
    actual = sha1_file(target)
    if expected_sha1 is not None and actual != expected_sha1:
        raise ScenarioRefusal(
            f"input fixture SHA-1 mismatch: expected {expected_sha1}, got {actual}"
        )
    return actual


def ensure_output_available(output: str | Path) -> None:
    """Refuse to overwrite an existing output path."""
    if Path(output).exists():
        raise ScenarioRefusal(f"refusing to overwrite existing output: {output}")


def resolve_role(scenario: dict[str, Any], scenario_id: str, role: str | None) -> str:
    """Resolve and validate the production role for a scenario request."""
    allowed = scenario["battle"]["roles"]
    required_role: str | None = None
    if scenario_id.endswith("__listen"):
        required_role = "listen"
    elif scenario_id.endswith("__connect"):
        required_role = "connect"
    if role is None:
        role = required_role or allowed[0]
    if required_role is not None and role != required_role:
        raise ScenarioRefusal(f"scenario {scenario_id!r} requires role {required_role!r}")
    if role not in allowed:
        raise ScenarioRefusal(f"role {role!r} is not allowed for scenario {scenario_id!r}")
    return role
