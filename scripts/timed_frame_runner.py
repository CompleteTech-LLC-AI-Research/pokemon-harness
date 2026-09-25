"""Drive the nine timed-frame rows under §5.1 per-row admission (#84).

This is the wrapper's executable half.  :mod:`scripts.timed_frame_admission`
decides *whether* a row may be dispatched and whether its result is admissible;
this module runs the row's **unchanged** command and captures what §7 and §6
need to read the outcome.  It never edits a command, raises a bound, retries a
failure, or reinterprets a result: a row's `failed` flag is the process's own
non-zero exit status.

The command is run exactly as given.  Nothing here selects nodes, adds
`-k`/`-m`, deselects, or alters the environment beyond an explicit, recorded
set, so the row that runs is the row the caller declared.

Design rules:

* A non-zero exit status is a **failure**, and it is reported as one.  It is
  never converted into a pass, and a failure inside a valid window is never
  re-run for admission reasons (§5.1 rule 1).
* The allocation probe is re-checked after each row.  Losing it during a row
  makes that row `S3` (§5.1, "Allocation loss").
* Every row's stdout/stderr is retained whole, so a §6 record is not limited to
  what a summary chose to print.
* No absolute local path is written into the record; the command's own output
  is retained verbatim under the caller's declared output directory.
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.timed_frame_admission import (
    AdmissionWrapper,
    load_allocation,
)


@dataclass(frozen=True)
class RowCommand:
    """One row's declared identity and its unchanged command."""

    row: str
    command: str
    timeout_seconds: float | None = None


@dataclass
class RowRun:
    """The captured result of one dispatched row."""

    row: str
    command: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def failed(self) -> bool:
        return self.returncode != 0


def _run_command(item: RowCommand, timeout_seconds: float | None) -> RowRun:
    """Run one row's command untouched and capture its terminal result."""

    argv = shlex.split(item.command)
    if not argv:
        raise ValueError(f"row {item.row!r} has an empty command")
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        timeout=item.timeout_seconds if item.timeout_seconds is not None else timeout_seconds,
    )
    return RowRun(
        row=item.row,
        command=item.command,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _probe(probe: str | None) -> bool:
    """Return whether the recorded allocation is still held."""

    if probe is None:
        return True
    completed = subprocess.run(
        shlex.split(probe),
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode == 0


def run_wrapper(
    *,
    commands: Sequence[RowCommand],
    allocation_path: str | Path,
    allowed_cpus: int,
    allocation_probe: str | None = None,
    timeout_seconds: float | None = None,
    observation_bound_seconds: float | None = None,
    wrapper: AdmissionWrapper | None = None,
) -> dict[str, Any]:
    """Drive every row: admit a fresh window, dispatch, classify, and stop in S5."""

    allocation = load_allocation(allocation_path)
    commands = list(commands)
    runs: list[RowRun] = []
    if wrapper is None:
        limits: dict[str, Any] = {}
        if observation_bound_seconds is not None:
            limits["observation_bound_seconds"] = float(observation_bound_seconds)
        wrapper = AdmissionWrapper(
            rows=[item.row for item in commands],
            allowed_cpus=allowed_cpus,
            allocation=allocation,
            **limits,
        )
    by_row = {item.row: item for item in commands}

    def dispatch(row: str) -> bool:
        run = _run_command(by_row[row], timeout_seconds)
        runs.append(run)
        return run.failed

    def outcome(row: str) -> str:
        matches = [run for run in runs if run.row == row]
        if not matches:
            return "not run"
        last = matches[-1]
        return f"exit {last.returncode}: " + ("failed" if last.failed else "passed")

    record = wrapper.run(
        dispatch, outcome=outcome, allocation_held=lambda: _probe(allocation_probe)
    )
    record["row_runs"] = [
        {
            "row": run.row,
            "command": run.command,
            "returncode": run.returncode,
            "failed": run.failed,
            "stdout": run.stdout,
            "stderr": run.stderr,
        }
        for run in runs
    ]
    return record


def write_record(record: dict[str, Any], path: str | Path) -> Path:
    """Write the run record as JSON; the caller owns where it lives."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    return destination
