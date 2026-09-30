"""Produce a declared battle-scenario fixture with a bounded, fail-closed contract.

The catalog in ``release-evidence/battle-scenarios.json`` declares each scenario,
its pinned ROM/SYM, its immutable input fixture, and finite capture bounds.  This
command validates a request against that contract and refuses unsafe or
inconsistent requests *before* touching the emulator:

* an unknown or duplicate scenario is refused (an unknown required scenario is
  reported as ``BLOCKED``, never as a passing skip);
* a ROM or symbol file that is not pinned in ``VERSIONS.md`` is refused;
* an ``--input-fixture`` whose SHA-1 disagrees with the declared input is refused;
* an existing ``--output`` is never overwritten, and ``--report`` may not alias
  the output, the ``--catalog`` itself, or any input file;
* non-finite or non-positive bounds are refused, including the effective bounds
  a caller supplies directly instead of taking the declared ones.

The capture path itself is bounded and fail-closed:

* it drives the pinned ROM with an explicit, replayable input sequence and no
  runtime RAM, party, PP, RNG, or serial mutation;
* it enforces the declared ``max_frames``, ``max_wall_seconds``, and
  ``max_inputs`` bounds and aborts as soon as any of them would be exceeded,
  including when a slow step crosses the wall-clock deadline before publication;
* it asserts the declared ``capture_boundary`` (map, the link-receptionist tile,
  link state, and any declared party shape) *before* anything is written;
* it measures the executing runtime and refuses a requested runtime or named
  interpreter the process does not actually provide — comparing interpreter
  *environments* rather than resolved binaries, so a second virtual environment
  whose ``bin/python`` resolves to the same file is still refused;
* it writes the state file with ``O_EXCL`` so an existing fixture can never be
  overwritten, retains the published inode identity independently of the staging
  entry, and therefore removes exactly its own publication if any later step —
  including the finalization that removes the staging entry and the requested
  report's own publication — fails, while a competing writer's file (even a
  symlink aimed at this capture's inode) is never deleted;
* it carries one absolute deadline through record preparation, staging,
  publication, and report finalization, so a capture that drifts past
  ``max_wall_seconds`` in those phases refuses and withdraws its own artifacts
  instead of recording a shorter duration and publishing anyway;
* it records a private report with the replayable input history, the measured
  runtime identity, the producer and declared-producer identities, the
  asset/output hashes, and the reproduction comparison against the pinned fixture
  hashes;
* it refuses incomplete or unverifiable declared metadata — a missing capture
  boundary or bounds block, a non-integer map, a link-state label with no verified
  encoding, a non-positive or non-integer verified fixture size, a verified entry
  whose producer is absent or whose declared producer digest disagrees, or a
  verified entry with no pinned fixture hashes — before the emulator is opened;
* it refuses a declared inventory, opponent, prior-action, or per-mon party
  precondition this bounded drive cannot observe, instead of recording an
  unchecked capture;
* it validates the capture record it is about to return and admits only records
  that carry the producer, asset, boundary, and output identities within the
  effective bounds;
* a cleanup failure fails the capture and prevents publication, and an
  interruption publishes nothing, leaves no staged file, and still closes the
  emulator session.

Callers still capture real fixtures from legal ROM/SYM inputs; the recorded
hashes are what consumers pin.
"""

from __future__ import annotations

import argparse
import contextlib  # noqa: F401  (retained facade attribute: producer.contextlib)
import functools  # noqa: F401  (retained facade attribute: producer.functools)
import hashlib  # noqa: F401  (retained facade attribute: producer.hashlib)
import json  # noqa: F401  (retained facade attribute: producer.json)
import math  # noqa: F401  (retained facade attribute: producer.math)
import os  # noqa: F401  (retained facade attribute: producer.os)
import subprocess  # noqa: F401  (retained facade attribute: producer.subprocess)
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass  # noqa: F401  (retained facade attribute: producer.dataclass)
from datetime import (  # noqa: F401  (retained facade attributes: producer.UTC, producer.datetime)
    UTC,
    datetime,
)
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Loaded by path (for example ``pokered_battle_scenario``) as well as by package
# name; register the loaded module under its canonical name first so the support
# modules' ``import scripts.produce_battle_scenario as _entry`` resolves to this
# same object instead of a second copy.
sys.modules.setdefault("scripts.produce_battle_scenario", sys.modules[__name__])

from scripts.produce_battle_scenario_capture import (
    _assert_boundary,  # noqa: F401  (retained facade attribute: producer._assert_boundary)
    _assert_supported_conditions,  # noqa: F401  (retained facade attribute: producer._assert_supported_conditions)
    _CaptureBudget,  # noqa: F401  (retained facade attribute: producer._CaptureBudget)
    _drive_to_link_reception,  # noqa: F401  (retained facade attribute: producer._drive_to_link_reception)
    _entry_identity,  # noqa: F401  (retained facade attribute: producer._entry_identity)
    _InputStep,  # noqa: F401  (retained facade attribute: producer._InputStep)
    _observe_boundary,  # noqa: F401  (retained facade attribute: producer._observe_boundary)
    _OwnedPublication,
    _remove_staged,  # noqa: F401  (retained facade attribute: producer._remove_staged)
    _symbol_byte,  # noqa: F401  (retained facade attribute: producer._symbol_byte)
    _withdraw_all,
    _write_bytes_atomically,  # noqa: F401  (retained facade attribute: producer._write_bytes_atomically)
    _write_fixture_exclusive,  # noqa: F401  (retained facade attribute: producer._write_fixture_exclusive)
    capture_battle_scenario,
    refuse_report_alias,
    write_report,
)
from scripts.produce_battle_scenario_catalog import (
    _is_digest,  # noqa: F401  (retained facade attribute: producer._is_digest)
    _positive_int,  # noqa: F401  (retained facade attribute: producer._positive_int)
    _repository_producer_locator,  # noqa: F401  (retained facade attribute: producer._repository_producer_locator)
    ensure_output_available,
    find_scenario,
    load_catalog,
    resolve_pins,
    resolve_role,
    scenario_index,  # noqa: F401  (retained facade attribute: producer.scenario_index)
    scenario_status,  # noqa: F401  (retained facade attribute: producer.scenario_status)
    sha1_file,  # noqa: F401  (retained facade attribute: producer.sha1_file)
    validate_bounds,
    validate_capture_record,  # noqa: F401  (retained facade attribute: producer.validate_capture_record)
    validate_scenario_metadata,
    validate_scenario_pins,
    verify_declared_producer,
    verify_input_fixture,
    verify_pinned_assets,
)
from scripts.produce_battle_scenario_model import (
    _BUTTON_NAMES,  # noqa: F401  (retained facade attribute: producer._BUTTON_NAMES)
    _CAPTURE_MESSAGE,  # noqa: F401  (retained facade attribute: producer._CAPTURE_MESSAGE)
    _CAPTURE_PRODUCER,  # noqa: F401  (retained facade attribute: producer._CAPTURE_PRODUCER)
    _CAPTURE_RECORD_FIELDS,  # noqa: F401  (retained facade attribute: producer._CAPTURE_RECORD_FIELDS)
    _CATALOG_PATH,
    _DEFAULT_PRESS_DURATION,  # noqa: F401  (retained facade attribute: producer._DEFAULT_PRESS_DURATION)
    _DEFAULT_STEP_FRAMES,  # noqa: F401  (retained facade attribute: producer._DEFAULT_STEP_FRAMES)
    _LINK_RECEPTION_TILE,  # noqa: F401  (retained facade attribute: producer._LINK_RECEPTION_TILE)
    _LINK_STATE_CODES,  # noqa: F401  (retained facade attribute: producer._LINK_STATE_CODES)
    _PINNED_FIXTURE_FIELDS,  # noqa: F401  (retained facade attribute: producer._PINNED_FIXTURE_FIELDS)
    _PRODUCER_KINDS,  # noqa: F401  (retained facade attribute: producer._PRODUCER_KINDS)
    _PROVENANCE_STATUSES,  # noqa: F401  (retained facade attribute: producer._PROVENANCE_STATUSES)
    _REPO,
    _RUNTIME_MEASUREMENTS,  # noqa: F401  (retained facade attribute: producer._RUNTIME_MEASUREMENTS)
    _RUNTIME_MODES,  # noqa: F401  (retained facade attribute: producer._RUNTIME_MODES)
    _SUPPORTED_BOUNDARY,  # noqa: F401  (retained facade attribute: producer._SUPPORTED_BOUNDARY)
    _UNMEASURED_RUNTIME,  # noqa: F401  (retained facade attribute: producer._UNMEASURED_RUNTIME)
    CaptureBoundsExceeded,
    CaptureNotAvailable,
    CapturePreconditionFailed,  # noqa: F401  (retained facade attribute: producer.CapturePreconditionFailed)
    ScenarioBlocked,
    ScenarioRefusal,
    _check_deadline,  # noqa: F401  (retained facade attribute: producer._check_deadline)
    _deadline_guard,
    _note,  # noqa: F401  (retained facade attribute: producer._note)
    _require,  # noqa: F401  (retained facade attribute: producer._require)
)
from scripts.produce_battle_scenario_runtime import (
    _default_session_factory,  # noqa: F401  (retained facade attribute: producer._default_session_factory)
    _interpreter_environment,  # noqa: F401  (retained facade attribute: producer._interpreter_environment)
    _producer_revision,  # noqa: F401  (retained facade attribute: producer._producer_revision)
    _pyboy_cython_flag,  # noqa: F401  (retained facade attribute: producer._pyboy_cython_flag)
    _pyboy_module_revision,  # noqa: F401  (retained facade attribute: producer._pyboy_module_revision)
    _pyboy_module_version,  # noqa: F401  (retained facade attribute: producer._pyboy_module_version)
    measure_runtime_identity,  # noqa: F401  (retained facade attribute: producer.measure_runtime_identity)
)


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
    catalog_path: str | Path | None = None,
    capture: Callable[..., None] = capture_battle_scenario,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Validate a scenario request, then invoke the bounded capture path."""
    scenario = find_scenario(catalog, scenario_id)
    validate_scenario_metadata(scenario, scenario_id)
    resolved_role = resolve_role(scenario, scenario_id, role)
    declared = scenario["capture_bounds"]
    bounds = validate_bounds(
        max_frames=declared["max_frames"] if max_frames is None else max_frames,
        max_wall_seconds=(
            declared["max_wall_seconds"] if max_wall_seconds is None else max_wall_seconds
        ),
        max_inputs=declared["max_inputs"] if max_inputs is None else max_inputs,
    )
    if report is not None:
        # The catalog is an input as well: a report written over it would replace
        # the very declarations this capture was validated against.
        refuse_report_alias(report, catalog_path, output, rom, sym, input_fixture)
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
        raise ScenarioRefusal("scenario declares a source fixture; an input fixture is required")
    producer_identity = verify_declared_producer(scenario, scenario_id, repo_root)
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
        "declared_producer": provenance.get("producer"),
        "producer_identity": producer_identity,
        "capture_bounds": bounds,
        "output": str(output),
    }
    deadline = clock() + bounds["max_wall_seconds"]
    ownership: dict[str, Any] = {}
    capture_record = capture(
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
        deadline=deadline,
        ownership=ownership,
        clock=clock,
    )
    if isinstance(capture_record, dict):
        plan = {**plan, "capture": capture_record}
    fixture_publication: _OwnedPublication | None = ownership.get("publication")
    if report is not None:
        guard = _deadline_guard(deadline, clock)
        report_publication: _OwnedPublication | None = None
        try:
            guard("report preparation")
            report_publication = write_report(report, plan)
            guard("report finalization")
        except BaseException as exc:
            # A requested report that could not be written must not leave the
            # fixture behind as if the capture had been fully recorded, and a
            # report this capture published must not survive a rolled-back
            # fixture.  Only entries this capture owns are ever withdrawn.
            _withdraw_all(exc, report_publication, fixture_publication)
            raise
        if clock() >= deadline:
            refusal = CaptureBoundsExceeded(
                f"capture exceeded max_wall_seconds ({bounds['max_wall_seconds']}) while "
                "finalizing the requested report; refusing to admit the pair"
            )
            _withdraw_all(refusal, report_publication, fixture_publication)
            raise refusal
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
            catalog_path=args.catalog,
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
