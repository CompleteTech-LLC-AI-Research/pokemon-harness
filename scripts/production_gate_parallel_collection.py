"""Broker-backed origin and complete collection, using the gate's report validator.

The sequential execution helpers select this backend only with an explicit
measurement budget. Original stdout/stderr bytes are captured separately: their
concatenation is diagnostic text and does not claim stream interleaving. Report
reads, hashing and raw writes use the same success bound and sibling servicing.
This source remains under integration and is not an execution admission.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

from scripts.production_gate_model import CollectionResult
from scripts.production_gate_parallel_io import read_evidence_bytes
from scripts.production_gate_parallel_measurement_budget import IdentityDeadline, MeasurementBudget
from scripts.production_gate_parallel_probe_protocol import ProbeProtocolError
from scripts.production_gate_parallel_validation import loads_strict
from scripts.production_gate_runtime import _load_gate_report

RAW_WRITE_BYTES = 65536


def retain_raw(
    directory: Path | None, filename: str, raw: bytes, budget: MeasurementBudget
) -> None:
    if directory is None:
        return
    if not filename or Path(filename).name != filename or filename in (".", ".."):
        raise ValueError("raw capture filename must be one component")
    budget.checkpoint()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(
        directory / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "wb") as stream:
        for offset in range(0, len(raw), RAW_WRITE_BYTES):
            budget.checkpoint()
            stream.write(raw[offset : offset + RAW_WRITE_BYTES])
            budget.checkpoint()
        stream.flush()
    budget.checkpoint()


def measured_command(
    command: Sequence[str],
    *,
    project_root: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    budget: MeasurementBudget,
    raw_output_directory: Path | None,
    filename: str,
) -> tuple[int, str]:
    result = budget.run_probe(
        command, environment=environment, cwd=project_root.resolve(), max_seconds=timeout_seconds
    )
    retain_raw(raw_output_directory, filename + ".stdout.log", result.stdout, budget)
    retain_raw(raw_output_directory, filename + ".stderr.log", result.stderr, budget)
    output = result.stdout.decode("utf-8", "replace")
    if result.stderr:
        output += "\n[separate stderr stream]\n" + result.stderr.decode("utf-8", "replace")
    budget.checkpoint()
    return result.returncode, output


def refusal(
    name: str, command: Sequence[str], started: float, error: Exception
) -> CollectionResult:
    return CollectionResult(
        name=name,
        command=list(command),
        status="FAIL",
        returncode=124 if isinstance(error, IdentityDeadline) else None,
        duration_seconds=time.monotonic() - started,
        reason="broker-backed collection refused: " + type(error).__name__,
    )


def run_origin_probe(
    *,
    command: Sequence[str],
    project_root: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    budget: MeasurementBudget,
    raw_output_directory: Path | None,
) -> CollectionResult:
    started = time.monotonic()
    try:
        status, output = measured_command(
            command,
            project_root=project_root,
            environment=environment,
            timeout_seconds=timeout_seconds,
            budget=budget,
            raw_output_directory=raw_output_directory,
            filename="import-origins",
        )
    except (IdentityDeadline, ProbeProtocolError, OSError, ValueError, RecursionError) as error:
        return refusal("import-origins", command, started, error)
    return CollectionResult(
        name="import-origins",
        command=list(command),
        status="PASS" if status == 0 else "FAIL",
        returncode=status,
        duration_seconds=time.monotonic() - started,
        output_tail=output[-8000:],
        reason="" if status == 0 else "selected interpreter import-origin preflight failed",
    )


def run_collection_probe(
    *,
    name: str,
    command: Sequence[str],
    project_root: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    report_path: Path,
    budget: MeasurementBudget,
    raw_output_directory: Path | None,
) -> CollectionResult:
    started = time.monotonic()
    child_environment = dict(environment, POKERED_GATE_REPORT=str(report_path))
    try:
        budget.checkpoint()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        if report_path.exists() or report_path.is_symlink():
            raise ValueError("collection report path is not fresh")
        status, output = measured_command(
            command,
            project_root=project_root,
            environment=child_environment,
            timeout_seconds=timeout_seconds,
            budget=budget,
            raw_output_directory=raw_output_directory,
            filename="collection-" + name,
        )

        def reader(path: Path):
            raw, _digest = read_evidence_bytes(
                path, checkpoint=budget.checkpoint, regular_nofollow=True
            )
            retain_raw(raw_output_directory, "collection-" + name + ".report.json", raw, budget)
            body = loads_strict(raw.decode("utf-8"))
            budget.checkpoint()
            return body

        report = _load_gate_report(
            report_path, expected_returncode=status, reader=reader, checkpoint=budget.checkpoint
        )
        budget.checkpoint()
        problems = []
        if report.error:
            problems.append(report.error)
        if status != 0:
            problems.append(f"pytest collection returned exit code {status}")
        if not report.collection_only:
            problems.append("pytest collection report did not identify a collection-only run")
        if report.collection_errors:
            problems.append(f"pytest reported {len(report.collection_errors)} collection error(s)")
        if report.collection_skips:
            problems.append(f"pytest reported {len(report.collection_skips)} collection skip(s)")
        result = CollectionResult(
            name=name,
            command=list(command),
            status="PASS" if not problems else "FAIL",
            returncode=status,
            nodeids=report.nodeids,
            duration_seconds=time.monotonic() - started,
            output_tail=output[-8000:],
            reason="; ".join(problems),
        )
        budget.checkpoint()
        return result
    except (IdentityDeadline, ProbeProtocolError, OSError, ValueError, RecursionError) as error:
        return refusal(name, command, started, error)
