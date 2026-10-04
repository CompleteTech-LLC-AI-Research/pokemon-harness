"""Run authored language-specific cases on an explicit auxiliary interpreter."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

from scripts.bootstrap_pyboy import _run_bounded

ROOT = Path(__file__).resolve().parents[1]
CHILD_TIMEOUT_SECONDS = 60
TEST_MODULE = "tests/test_timed_menu_milestone_sentinels.py"
TEST_FUNCTION = "test_class_carrier_declines_generic_metaclass_lookup_scope"


def _owned_path(value: object, parent: Path) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return Path(value).resolve(strict=True).is_relative_to(parent.resolve(strict=True))
    except (OSError, ValueError):
        return False


def _validate_child_report(report: object, expected: str) -> None:
    assert isinstance(report, dict), "grammar child did not report an object"
    assert report.get("expected_nodeid") == expected, "grammar child expected another node"
    assert report.get("collected_nodeids") == [expected], "grammar child collection differs"
    version = report.get("python_version")
    assert isinstance(version, list) and len(version) == 3, "grammar child version missing"
    assert all(type(part) is int for part in version), "grammar child version is malformed"
    assert tuple(version) >= (3, 12, 0), "grammar child requires real Python >=3.12"
    assert report.get("root") == str(ROOT.resolve()), "grammar child uses another checkout"
    assert _owned_path(report.get("function_source"), ROOT / "tests"), "foreign test function"
    assert report.get("support_source") == str(
        (ROOT / "tests/_timed_menu_milestone_sentinel_support.py").resolve()
    ), "foreign sentinel support"
    assert _owned_path(report.get("analyzer_source"), ROOT / "tests"), "foreign analyzer"
    assert report.get("same_analyzer_object") is True, "test uses another analyzer"
    assert report.get("collection_errors") == [], "grammar child collection failed"
    phases = report.get("reports")
    assert isinstance(phases, list) and len(phases) == 3, "grammar child phases missing"
    assert all(isinstance(phase, dict) for phase in phases), "grammar child phases malformed"
    assert {phase.get("when") for phase in phases} == {"setup", "call", "teardown"}
    assert all(
        phase.get("nodeid") == expected
        and phase.get("outcome") == "passed"
        and phase.get("wasxfail") is False
        for phase in phases
    ), "grammar child was not exactly one fully passed case"
    assert report.get("exitstatus") == 0, "grammar child pytest did not succeed"


def run_grammar_case(parameter: str, error: type[Exception]) -> None:
    """Require the same real authored case; a missing prerequisite fails closed."""
    expected_suffix = {"Meta": "TypeError", "T": "AssertionError"}
    assert expected_suffix.get(parameter) == error.__name__, "unknown grammar case"
    executable = os.environ.get("POKERED_GRAMMAR_TEST_PYTHON")
    if not executable:
        raise AssertionError(
            "Python 3.11 grammar checks require POKERED_GRAMMAR_TEST_PYTHON pointing "
            "to a Python >=3.12 test environment installed from this checkout; "
            "see docs/CI_POLICY.md"
        )
    # Keep the venv entrypoint: resolving its symlink would run the base Python.
    executable_path = Path(os.path.abspath(Path(executable).expanduser()))
    assert executable_path.is_file(), "grammar child interpreter is missing"
    expected = f"{TEST_MODULE}::{TEST_FUNCTION}[{parameter}-{error.__name__}]"
    evidence = Path(
        os.environ.get("POKERED_GRAMMAR_EVIDENCE_DIR")
        or tempfile.mkdtemp(prefix="pokered-grammar-evidence-")
    ).expanduser()
    evidence.mkdir(parents=True, exist_ok=True)
    stem = f"{parameter}-{error.__name__}-{uuid.uuid4().hex}"
    report_path = evidence / f"{stem}.json"
    log_path = evidence / f"{stem}.log"
    environment = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        environment.pop(name, None)
    environment.update(
        PYTHONNOUSERSITE="1",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        POKERED_GRAMMAR_CHILD_NODEID=expected,
        POKERED_GRAMMAR_CHILD_REPORT=str(report_path.resolve()),
        POKERED_GRAMMAR_CHILD_ROOT=str(ROOT.resolve()),
    )
    command = [
        str(executable_path),
        "-m",
        "pytest",
        "-o",
        "addopts=",
        "--strict-config",
        "--strict-markers",
        "--import-mode=importlib",
        "-p",
        "pytest_asyncio.plugin",
        "-p",
        "tests._grammar_child_report",
        "-vv",
        expected,
    ]
    with log_path.open("w", encoding="utf-8") as terminal:
        try:
            result = _run_bounded(
                command,
                cwd=ROOT,
                env=environment,
                timeout=CHILD_TIMEOUT_SECONDS,
                stdout=terminal,
                stderr=subprocess.STDOUT,
            )
        except BaseException:
            print(f"Grammar child terminal result retained at {log_path}")
            raise
    print(f"Grammar child terminal result: {log_path}; report: {report_path}")
    assert result.returncode == 0, (
        f"grammar child exited {result.returncode}; full terminal result: {log_path}"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert isinstance(report, dict), "grammar child did not report an object"
    assert report.get("interpreter") == str(executable_path), (
        "grammar child used another interpreter"
    )
    assert report.get("prefix") != report.get("base_prefix"), "grammar child is not isolated"
    _validate_child_report(report, expected)
