"""Collection preflight, pytest execution and process termination helpers.

Split from ``scripts/production_gate.py`` for issue #124 with no behavior
change: the code below is copied verbatim except that calls to facade-owned,
monkeypatch-patched entry points resolve through ``_entry`` so attribute patches
on the loaded gate module stay visible.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Direct names this module calls; cyclic back-edges go through _entry.
from scripts.production_gate_model import (
    CERTIFIED_FIXTURE_IDS,
    COLLECTION_TIMEOUT_SECONDS,
    FIXTURE_MANIFEST_RELATIVE_PATH,
    MAX_FAILED_OUTPUT_CHARS,
    PYTEST_GATE_ARGUMENTS,
    AssetRecord,
    CollectionResult,
    Counts,
    GateReport,
    _asset_key,
)
from scripts.production_gate_runtime import _load_gate_report
from scripts.production_gate_text import _bounded_failure_text


def _terminate_process(process: subprocess.Popen[str]) -> None:
    """Terminate and reap a gate child and its process group."""

    pid = getattr(process, "pid", None)
    if pid is None:
        return
    if os.name == "posix":
        try:
            # ``start_new_session=True`` makes the child PID the process-group
            # ID.  Signal the group even when the parent already exited: a
            # grandchild can otherwise keep the stdout pipe open forever.
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pass
        # Reaping the parent does not prove its descendants have stopped.
        # Clear any remaining members of this owned group even when the
        # parent exited promptly after SIGTERM.
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pass
        return

    # Windows has no killpg.  The caller creates a new process group; taskkill
    # is the portable last resort for descendants when a timeout occurs.
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    try:
        process.wait(timeout=5.0)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pass


def _process_creation_kwargs() -> dict[str, Any]:
    """Return process-group options for a gate subprocess."""

    if os.name == "posix":
        return {"start_new_session": True}
    return {
        "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    }


def _communicate_after_termination(process: subprocess.Popen[str]) -> str:
    """Drain a terminated child without allowing a leaked pipe to hang us."""

    try:
        output, _ = process.communicate(timeout=5.0)
        return output or ""
    except subprocess.TimeoutExpired as exc:
        _entry._terminate_process(process)
        # A descendant that escaped the process-group boundary can retain the
        # read end.  Close this process's copy and retain the diagnostic text
        # already collected instead of waiting forever on EOF.
        stream = getattr(process, "stdout", None)
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass
        partial = exc.output
        if isinstance(partial, bytes):
            return partial.decode(errors="replace")
        return partial or ""


def _pytest_console_script(python_executable: Path) -> Path | None:
    """Find the pytest console script belonging to ``python_executable``."""

    bin_directory = python_executable.parent
    names = ("pytest.exe", "pytest") if os.name == "nt" else ("pytest", "pytest.exe")
    for name in names:
        candidate = bin_directory / name
        if candidate.is_file():
            return candidate
    return None


def run_collection_preflight(
    *,
    project_root: Path,
    python_executable: Path,
    environment: dict[str, str],
    timeout_seconds: float = COLLECTION_TIMEOUT_SECONDS,
    raw_output_directory: Path | None = None,
) -> list[CollectionResult]:
    """Run both supported pytest collection entry points.

    A release must prove that the module invocation and the console script
    resolve the same test tree.  This is intentionally separate from the
    marker-tier subprocesses: a collection failure must remain visible even
    when a selected tier happens to contain no affected tests.
    """

    commands: list[tuple[str, list[str]]] = [
        (
            "python-module",
            [
                str(python_executable),
                "-m",
                "pytest",
                "tests",
                "--collect-only",
                "-q",
                *PYTEST_GATE_ARGUMENTS,
            ],
        )
    ]
    console_script = _entry._pytest_console_script(python_executable)
    console_missing = console_script is None
    if console_missing:
        commands.append(
            (
                "pytest-console",
                [
                    str(python_executable.parent / "pytest"),
                    "tests",
                    "--collect-only",
                    "-q",
                    *PYTEST_GATE_ARGUMENTS,
                ],
            )
        )
    else:
        commands.append(
            (
                "pytest-console",
                [
                    str(console_script),
                    "tests",
                    "--collect-only",
                    "-q",
                    *PYTEST_GATE_ARGUMENTS,
                ],
            )
        )

    with tempfile.TemporaryDirectory(prefix="pokered-collection-") as directory:
        results: list[CollectionResult] = []
        for index, (name, command) in enumerate(commands):
            if any(result.status == "INTERRUPTED" for result in results):
                results.append(
                    CollectionResult(
                        name=name,
                        command=command,
                        status="NOT_STARTED",
                        returncode=None,
                        reason="collection cancelled before this entry point started",
                    )
                )
                continue
            if name == "pytest-console" and console_missing:
                results.append(
                    CollectionResult(
                        name=name,
                        command=command,
                        status="FAIL",
                        returncode=None,
                        reason=(
                            "pytest console script was not found beside the selected "
                            f"interpreter {python_executable}"
                        ),
                    )
                )
                continue
            results.append(
                _entry._run_collection_command(
                    name=name,
                    command=command,
                    project_root=project_root,
                    environment=environment,
                    timeout_seconds=timeout_seconds,
                    report_path=Path(directory) / f"{index}.json",
                    raw_output_directory=raw_output_directory,
                )
            )

    if all(result.status == "PASS" for result in results):
        first, second = results
        first_nodes = set(first.nodeids)
        second_nodes = set(second.nodeids)
        if first_nodes != second_nodes:
            missing_from_second = sorted(first_nodes - second_nodes)
            missing_from_first = sorted(second_nodes - first_nodes)
            reason = (
                "pytest collection entry points selected different test trees: "
                f"missing_from_console={missing_from_second!r}; "
                f"missing_from_module={missing_from_first!r}"
            )
            for result in results:
                result.status = "FAIL"
                result.reason = reason
    return results


def run_fixture_manifest_validation(
    *,
    project_root: Path,
    python_executable: Path,
    environment: dict[str, str],
    fixture_root: Path,
    validate_bytes: bool,
    timeout_seconds: float = COLLECTION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Validate the tracked external-fixture manifest with the gate runtime."""

    manifest_path = project_root / FIXTURE_MANIFEST_RELATIVE_PATH
    validator_path = project_root / "scripts" / "validate_fixture_manifest.py"
    mode = "byte" if validate_bytes else "schema"
    command = [
        str(python_executable),
        str(validator_path),
        "--manifest",
        str(manifest_path),
    ]
    if validate_bytes:
        command.extend(("--fixture-root", str(fixture_root)))
    else:
        command.append("--schema-only")

    try:
        completed = subprocess.run(
            command,
            cwd=project_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "status": "FAIL",
            "mode": mode,
            "returncode": 124 if isinstance(exc, subprocess.TimeoutExpired) else None,
            "entries": None,
            "reason": f"fixture manifest validator failed: {type(exc).__name__}: {exc}",
        }

    output = "\n".join(
        part for part in (completed.stdout.strip(), completed.stderr.strip()) if part
    )
    entries: int | None = None
    match = re.search(r"validation passed: (\d+) entries", completed.stdout)
    if match is not None:
        entries = int(match.group(1))
    return {
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "mode": mode,
        "returncode": completed.returncode,
        "entries": entries,
        "reason": "" if completed.returncode == 0 else output[-4000:],
    }


def fixture_manifest_provenance_problems(
    manifest_path: Path,
    required_ids: Iterable[str] = CERTIFIED_FIXTURE_IDS,
) -> list[str]:
    """Return missing or non-verified provenance for certified fixtures."""

    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"fixture manifest provenance could not be read: {type(exc).__name__}: {exc}"]
    fixtures = document.get("fixtures") if isinstance(document, dict) else None
    if not isinstance(fixtures, list):
        return ["fixture manifest provenance has no fixture list"]
    by_id = {
        fixture.get("id"): fixture
        for fixture in fixtures
        if isinstance(fixture, dict) and isinstance(fixture.get("id"), str)
    }
    problems: list[str] = []
    for fixture_id in sorted(set(required_ids)):
        fixture = by_id.get(fixture_id)
        if fixture is None:
            problems.append(f"certified fixture is absent from manifest: {fixture_id}")
            continue
        provenance = fixture.get("provenance")
        status = provenance.get("status") if isinstance(provenance, dict) else None
        if status != "verified":
            problems.append(
                f"certified fixture provenance is not verified: {fixture_id} ({status!r})"
            )
    return problems


def fixture_manifest_input_problems(
    manifest_path: Path,
    *,
    rom_root: Path,
    assets: Iterable[AssetRecord],
    expected_sha1: dict[Path, str],
) -> list[str]:
    """Check fixture input pins against the inspected ROM/SYM inventory.

    Byte validation proves that a state matches the hashes written in the
    manifest.  This second check proves that those manifest pins still agree
    with ``VERSIONS.md`` and with the files the gate actually inspected.  A
    manifest that is internally self-consistent but points at a different
    ROM/SYM set must not be accepted as release evidence.
    """

    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"fixture manifest input pins could not be read: {type(exc).__name__}: {exc}"]
    fixtures = document.get("fixtures") if isinstance(document, dict) else None
    if not isinstance(fixtures, list):
        return ["fixture manifest input pins have no fixture list"]

    try:
        resolved_rom_root = rom_root.expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        resolved_rom_root = rom_root
    records_by_key: dict[Path, AssetRecord] = {}
    for record in assets:
        if record.kind not in {"rom", "symbol"}:
            continue
        try:
            relative = Path(record.path).resolve(strict=False).relative_to(resolved_rom_root)
        except (OSError, RuntimeError, ValueError):
            continue
        records_by_key[_asset_key(relative)] = record

    problems: list[str] = []
    seen: set[str] = set()

    def add(problem: str) -> None:
        if problem not in seen:
            seen.add(problem)
            problems.append(problem)

    for index, fixture in enumerate(fixtures):
        if not isinstance(fixture, dict):
            add(f"fixture manifest entry {index} is not an object")
            continue
        fixture_id = fixture.get("id", f"index {index}")
        for field_name, kind in (("expected_rom", "rom"), ("expected_symbols", "symbol")):
            pin = fixture.get(field_name)
            if not isinstance(pin, dict):
                add(f"fixture {fixture_id} has no valid {field_name} pin")
                continue
            raw_path = pin.get("path")
            pin_sha1 = pin.get("sha1")
            if not isinstance(raw_path, str) or not isinstance(pin_sha1, str):
                add(f"fixture {fixture_id} has an invalid {field_name} pin")
                continue
            key = _asset_key(raw_path)
            expected = expected_sha1.get(key)
            if expected is None:
                add(f"fixture {fixture_id} {field_name} is not pinned in VERSIONS.md: {raw_path}")
            elif expected != pin_sha1.lower():
                add(
                    f"fixture {fixture_id} {field_name} pin disagrees with VERSIONS.md: "
                    f"expected {expected}, got {pin_sha1.lower()}"
                )
            record = records_by_key.get(key)
            if record is None or record.kind != kind:
                add(
                    f"fixture {fixture_id} {field_name} is outside the inspected {kind} set: "
                    f"{raw_path}"
                )
                continue
            if record.status != "ok":
                add(
                    f"fixture {fixture_id} {field_name} uses an unverified asset: "
                    f"{record.label} {record.status}"
                )
            elif record.actual_sha1 != pin_sha1.lower():
                add(
                    f"fixture {fixture_id} {field_name} does not match inspected bytes: "
                    f"expected {record.actual_sha1}, got {pin_sha1.lower()}"
                )
    return problems


def _retain_raw_output(directory: Path | None, filename: str, output: str) -> str:
    """Retain complete captured text privately, without replacing an earlier log."""

    if directory is None:
        return ""
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(directory / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(output)
    except (OSError, UnicodeError) as exc:
        # Exception messages can contain private paths. The stable filename
        # and exception type identify the failed capture without exposing them.
        return f"could not retain raw output {filename}: {type(exc).__name__}"
    return ""


def _run_collection_command(
    *,
    name: str,
    command: list[str],
    project_root: Path,
    environment: dict[str, str],
    timeout_seconds: float,
    report_path: Path,
    raw_output_directory: Path | None = None,
) -> CollectionResult:
    started = time.monotonic()
    child_environment = dict(environment)
    child_environment["POKERED_GATE_REPORT"] = str(report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        report_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        return CollectionResult(
            name=name,
            command=command,
            status="FAIL",
            returncode=None,
            duration_seconds=time.monotonic() - started,
            reason=f"could not prepare collection report: {type(exc).__name__}: {exc}",
        )
    try:
        process = subprocess.Popen(
            command,
            cwd=project_root,
            env=child_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            **_process_creation_kwargs(),
        )
    except OSError as exc:
        return CollectionResult(
            name=name,
            command=command,
            status="FAIL",
            returncode=None,
            duration_seconds=time.monotonic() - started,
            reason=f"could not start collection command: {type(exc).__name__}: {exc}",
        )

    try:
        output, _ = process.communicate(timeout=timeout_seconds)
    except KeyboardInterrupt:
        _entry._terminate_process(process)
        output = _entry._communicate_after_termination(process)
        capture_error = _retain_raw_output(raw_output_directory, f"collection-{name}.log", output)
        return CollectionResult(
            name=name,
            command=command,
            status="INTERRUPTED",
            returncode=130,
            duration_seconds=time.monotonic() - started,
            output_tail=output[-8000:],
            reason="; ".join(filter(None, ("pytest collection interrupted", capture_error))),
        )
    except subprocess.TimeoutExpired:
        _entry._terminate_process(process)
        output = _entry._communicate_after_termination(process)
        capture_error = _retain_raw_output(raw_output_directory, f"collection-{name}.log", output)
        return CollectionResult(
            name=name,
            command=command,
            status="FAIL",
            returncode=124,
            duration_seconds=time.monotonic() - started,
            output_tail=(
                f"pytest collection timed out after {timeout_seconds:.1f}s\n{output[-8000:]}"
            ),
            reason="; ".join(filter(None, ("collection timeout", capture_error))),
        )

    raw_returncode = process.returncode
    returncode = int(raw_returncode) if raw_returncode is not None else 125
    report = _load_gate_report(report_path, expected_returncode=returncode)
    problems: list[str] = []
    capture_error = _retain_raw_output(raw_output_directory, f"collection-{name}.log", output)
    if capture_error:
        problems.append(capture_error)
    if report.error:
        problems.append(report.error)
    if returncode != 0:
        problems.append(f"pytest collection returned exit code {returncode}")
    if not report.collection_only:
        problems.append("pytest collection report did not identify a collection-only run")
    if report.collection_errors:
        problems.append(f"pytest reported {len(report.collection_errors)} collection error(s)")
    if report.collection_skips:
        problems.append(f"pytest reported {len(report.collection_skips)} collection skip(s)")
    return CollectionResult(
        name=name,
        command=command,
        status="PASS" if not problems else "FAIL",
        returncode=returncode,
        nodeids=report.nodeids,
        duration_seconds=time.monotonic() - started,
        output_tail=output[-8000:],
        reason="; ".join(problems),
    )


def run_pytest_once(
    *,
    project_root: Path,
    python_executable: Path,
    environment: dict[str, str],
    expression: str,
    timeout_seconds: float,
    report_path: Path,
    selectors: Sequence[str] = (),
    raw_output_directory: Path | None = None,
) -> tuple[int, GateReport, str, list[str]]:
    command = [str(python_executable), "-m", "pytest"]
    command.extend(selectors or ("tests",))
    command.extend(
        (
            "-m",
            expression,
            *PYTEST_GATE_ARGUMENTS,
            "-rA",
            "--maxfail=0",
        )
    )
    child_environment = dict(environment)
    child_environment["POKERED_GATE_REPORT"] = str(report_path)
    progress_path = report_path.with_suffix(".progress.json")
    child_environment["POKERED_GATE_PROGRESS_REPORT"] = str(progress_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        report_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        return (
            127,
            GateReport(
                Counts(errors=1),
                error=f"could not prepare pytest report path: {type(exc).__name__}: {exc}",
            ),
            "",
            command,
        )
    try:
        progress_path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        return (
            127,
            GateReport(
                Counts(errors=1),
                error=(
                    f"could not prepare pytest progress report path: {type(exc).__name__}: {exc}"
                ),
            ),
            "",
            command,
        )

    try:
        process = subprocess.Popen(
            command,
            cwd=project_root,
            env=child_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            **_process_creation_kwargs(),
        )
    except OSError as exc:
        return (
            127,
            GateReport(
                Counts(errors=1),
                error=f"could not start pytest: {type(exc).__name__}: {exc}",
            ),
            "",
            command,
        )

    timed_out = False
    interrupted = False
    try:
        output, _ = process.communicate(timeout=timeout_seconds)
    except KeyboardInterrupt:
        interrupted = True
        _entry._terminate_process(process)
        output = _entry._communicate_after_termination(process)
        returncode = 130
    except subprocess.TimeoutExpired:
        timed_out = True
        _entry._terminate_process(process)
        output = _entry._communicate_after_termination(process)
        returncode = 124
    else:
        raw_returncode = process.returncode
        returncode = int(raw_returncode) if raw_returncode is not None else 125

    capture_error = _retain_raw_output(
        raw_output_directory, report_path.with_suffix(".log").name, output
    )
    report = _load_gate_report(
        report_path,
        expected_returncode=None if timed_out or interrupted else returncode,
    )
    # A progress report is evidence of the tests that had reached a terminal
    # outcome only after an actual timeout or interruption. A missing or malformed final
    # report on an otherwise exited process must remain an error; falling back
    # to stale/partial progress there could turn a broken runner green.
    if (timed_out or interrupted) and report.error:
        progress_report = _load_gate_report(progress_path, allow_partial=True)
        if not progress_report.error:
            report = progress_report
    if timed_out or interrupted:
        stop_reason = (
            "pytest interrupted"
            if interrupted
            else f"pytest timed out after {timeout_seconds:.1f}s"
        )
        report = GateReport(
            counts=report.counts,
            skip_reasons=report.skip_reasons,
            nodeids=report.nodeids,
            collection_errors=report.collection_errors,
            collection_skips=report.collection_skips,
            failed_records=report.failed_records,
            error=(f"{stop_reason}; {report.error}" if report.error else stop_reason),
        )
    if capture_error:
        report = replace(report, error="; ".join(filter(None, (report.error, capture_error))))
    if report.error:
        output = f"{output}\n{report.error}"
    # Keep the command and enough output to diagnose a failed gate without
    # flooding the orchestrator with every emulator trace.
    return (
        returncode,
        report,
        _bounded_failure_text(output, MAX_FAILED_OUTPUT_CHARS, tail=True),
        command,
    )


# Call-time indirection so facade-level monkeypatches stay visible here.
import scripts.production_gate as _entry
