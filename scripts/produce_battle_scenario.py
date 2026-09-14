"""Produce a declared battle-scenario fixture with a bounded, fail-closed contract.

The catalog in ``release-evidence/battle-scenarios.json`` declares each scenario,
its pinned ROM/SYM, its immutable input fixture, and finite capture bounds.  This
command validates a request against that contract and refuses unsafe or
inconsistent requests *before* touching the emulator:

* an unknown or duplicate scenario is refused (an unknown required scenario is
  reported as ``BLOCKED``, never as a passing skip);
* a ROM or symbol file that is not pinned in ``VERSIONS.md`` is refused;
* an ``--input-fixture`` whose SHA-1 disagrees with the declared input is refused;
* an existing ``--output`` is never overwritten;
* non-finite or non-positive bounds are refused.

The actual capture path is deliberately not implemented here: a real capture
requires a controlled ROM run with legal assets.  It exits non-zero with a clear
message and never writes a fixture, fabricates bytes, or disables hash
verification.  Callers capture externally and pin the resulting hashes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
_CATALOG_PATH = _REPO / "release-evidence" / "battle-scenarios.json"
_CAPTURE_MESSAGE = (
    "capture requires a controlled ROM run; no fixture bytes were written. "
    "Run the documented external capture procedure with legal ROM/SYM inputs, "
    "record the resulting hashes, and pin them in the scenario catalog."
)


class ScenarioRefusal(ValueError):
    """Raised when a declared scenario request is refused before capture."""


class ScenarioBlocked(ScenarioRefusal):
    """Raised when a required scenario is absent; callers must report BLOCKED."""


class CaptureNotAvailable(RuntimeError):
    """Raised by the bounded capture stub because no controlled ROM run is active."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ScenarioRefusal(message)


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
            raise ScenarioRefusal(
                f"{label} SHA-1 mismatch: expected {expected}, got {actual}"
            )


def sha1_file(path: str | Path) -> str:
    """Return the SHA-1 of a file using chunked reads."""
    digest = hashlib.sha1()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def write_report(path: str | Path, payload: Any) -> None:
    """Write a private capture/replay report; never called for the stub path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def capture_battle_scenario(**_kwargs: Any) -> None:
    """Bounded capture stub: refuse until a controlled ROM run is available."""
    raise CaptureNotAvailable(_CAPTURE_MESSAGE)


def run(
    *,
    scenario_id: str,
    catalog: dict[str, Any],
    rom: str | Path,
    sym: str | Path,
    output: str | Path,
    repo_root: str | Path = _REPO,
    input_fixture: str | Path | None = None,
    role: str | None = None,
    report: str | Path | None = None,
    max_frames: Any = None,
    max_wall_seconds: Any = None,
    max_inputs: Any = None,
    runtime: str = "source",
    python: str | Path | None = None,
    pins: Any = None,
    capture: Callable[..., None] = capture_battle_scenario,
) -> dict[str, Any]:
    """Validate a scenario request, then invoke the bounded capture path."""
    scenario = find_scenario(catalog, scenario_id)
    resolved_role = resolve_role(scenario, scenario_id, role)
    declared = scenario["capture_bounds"]
    bounds = validate_bounds(
        max_frames=declared["max_frames"] if max_frames is None else max_frames,
        max_wall_seconds=(
            declared["max_wall_seconds"] if max_wall_seconds is None else max_wall_seconds
        ),
        max_inputs=declared["max_inputs"] if max_inputs is None else max_inputs,
    )
    ensure_output_available(output)
    if pins is None:
        from pokered_harness.config import load_versions

        pins = load_versions(Path(repo_root) / "VERSIONS.md")
    rom_pin, sym_pin = resolve_pins(pins, rom, sym)
    validate_scenario_pins(scenario, rom_pin, sym_pin)
    verify_pinned_assets(rom, sym, rom_pin, sym_pin)
    provenance = scenario["provenance"]
    declared_input_sha1 = provenance.get("input_fixture_sha1")
    if provenance.get("source_fixture_id") is not None and input_fixture is None:
        raise ScenarioRefusal(
            "scenario declares a source fixture; an input fixture is required"
        )
    input_sha1: str | None = None
    if input_fixture is not None:
        input_sha1 = verify_input_fixture(input_fixture, declared_input_sha1)
    plan = {
        "scenario_id": scenario_id,
        "role": resolved_role,
        "runtime": runtime,
        "python": str(python) if python is not None else None,
        "rom_sha1": rom_pin,
        "sym_sha1": sym_pin,
        "input_fixture_sha1": input_sha1,
        "capture_bounds": bounds,
        "output": str(output),
    }
    capture(
        scenario=scenario,
        rom=rom,
        sym=sym,
        input_fixture=input_fixture,
        output=output,
        role=resolved_role,
        bounds=bounds,
        report=report,
        repo_root=repo_root,
        runtime=runtime,
        python=python,
        plan=plan,
    )
    if report is not None:
        write_report(report, plan)
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, help="declared scenario id")
    parser.add_argument("--catalog", type=Path, default=_CATALOG_PATH)
    parser.add_argument("--rom", type=Path, required=True)
    parser.add_argument("--sym", type=Path, required=True)
    parser.add_argument("--input-fixture", type=Path, default=None)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--role", choices=("listen", "connect"), default=None)
    parser.add_argument("--runtime", choices=("source", "cython"), default="source")
    parser.add_argument("--python", type=Path, default=None)
    parser.add_argument("--max-frames", type=float, default=None)
    parser.add_argument("--max-wall-seconds", type=float, default=None)
    parser.add_argument("--max-inputs", type=float, default=None)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--repo-root", type=Path, default=_REPO)
    args = parser.parse_args(argv)

    try:
        catalog = load_catalog(args.catalog)
        run(
            scenario_id=args.scenario,
            catalog=catalog,
            rom=args.rom,
            sym=args.sym,
            output=args.output,
            repo_root=args.repo_root,
            input_fixture=args.input_fixture,
            role=args.role,
            report=args.report,
            max_frames=args.max_frames,
            max_wall_seconds=args.max_wall_seconds,
            max_inputs=args.max_inputs,
            runtime=args.runtime,
            python=args.python,
        )
    except ScenarioBlocked as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 3
    except CaptureNotAvailable as exc:
        print(f"capture unavailable: {exc}", file=sys.stderr)
        return 4
    except (OSError, ScenarioRefusal) as exc:
        print(f"battle scenario refused: {exc}", file=sys.stderr)
        return 2

    print(f"scenario capture complete: {args.scenario}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
