"""Authored NOTRUN broker/collection controls; fake producer, actual report validation."""

import json
import os
import subprocess

import pytest

from scripts import production_gate_execution as execution
from scripts import production_gate_parallel_collection as collection
from scripts.production_gate_parallel_measurement_budget import MeasurementBudget


def report_body():
    return {
        "counts": {
            "total": 0,
            "passed": 0,
            "failed": 0,
            "skipped": 0,
            "xfailed": 0,
            "xpassed": 0,
            "errors": 0,
        },
        "tests": [],
        "collection_only": True,
        "collection_errors": [],
        "collection_skips": [],
        "exitstatus": 0,
        "collected": 1,
        "nodeids": ["tests/controlled.py::test_owned"],
    }


def collection_case(tmp_path, body, *, prepare=None, now=None):
    calls = []
    clock = now if now is not None else [10.0]

    def runner(command, **kwargs):
        calls.append(kwargs)
        path = tmp_path / "report.json"
        if prepare is None:
            path.write_text(json.dumps(body))
        else:
            prepare(path)
        return subprocess.CompletedProcess(command, 0, b"stdout\x00bytes", b"stderr bytes")

    budget = MeasurementBudget(100.0, 120.0, clock=lambda: clock[0], probe_runner=runner)
    result = collection.run_collection_probe(
        name="python-module",
        command=["controlled-unused"],
        project_root=tmp_path,
        environment={},
        timeout_seconds=30.0,
        report_path=tmp_path / "report.json",
        budget=budget,
        raw_output_directory=tmp_path / "raw",
    )
    return result, calls


def test_broker_collection_preserves_exact_separate_streams_and_validated_nodes(tmp_path):
    body = report_body()
    result, calls = collection_case(tmp_path, body)
    assert result.status == "PASS" and result.nodeids == tuple(body["nodeids"])
    assert calls[0]["environment"]["POKERED_GATE_REPORT"] == str(tmp_path / "report.json")
    assert calls[0]["budget"].deadline == 40.0 and calls[0]["budget"].cleanup_bound() == 120.0
    assert (tmp_path / "raw/collection-python-module.stdout.log").read_bytes() == b"stdout\x00bytes"
    assert (tmp_path / "raw/collection-python-module.stderr.log").read_bytes() == b"stderr bytes"
    assert json.loads((tmp_path / "raw/collection-python-module.report.json").read_bytes()) == body


@pytest.mark.parametrize("fault", ["duplicate", "exit", "not_collection", "skip", "error"])
def test_broker_collection_cannot_bypass_existing_gate_report_constraints(tmp_path, fault):
    body = report_body()
    if fault == "duplicate":
        body["nodeids"] *= 2
        body["collected"] = 2
    elif fault == "exit":
        body["exitstatus"] = 1
    elif fault == "not_collection":
        body["collection_only"] = False
    elif fault == "skip":
        body["collection_skips"] = [{"nodeid": "controlled", "reason": "missing acceptance"}]
    else:
        body["collection_errors"] = [{"nodeid": "controlled", "reason": "controlled error"}]
        body["counts"]["errors"] = 1
    result, _ = collection_case(tmp_path, body)
    assert result.status == "FAIL" and result.reason


def test_collection_report_symlink_is_refused(tmp_path):
    target = tmp_path / "foreign.json"
    target.write_text(json.dumps(report_body()))
    result, _ = collection_case(
        tmp_path, report_body(), prepare=lambda path: path.symlink_to(target)
    )
    assert (
        result.status == "FAIL"
        and not (tmp_path / "raw/collection-python-module.report.json").exists()
    )


def test_collection_report_fifo_is_refused_without_a_blocking_read(tmp_path):
    result, _ = collection_case(tmp_path, report_body(), prepare=os.mkfifo)
    assert (
        result.status == "FAIL"
        and not (tmp_path / "raw/collection-python-module.report.json").exists()
    )


def test_report_parse_crossing_original_bound_cannot_publish_collection_pass(tmp_path, monkeypatch):
    now, decode = [10.0], collection.loads_strict

    def late_parse(raw):
        body = decode(raw)
        now[0] = 100.0
        return body

    monkeypatch.setattr(collection, "loads_strict", late_parse)
    result, _ = collection_case(tmp_path, report_body(), now=now)
    assert result.status == "FAIL" and result.returncode == 124


@pytest.mark.parametrize("filename", ["..", "../outside"])
def test_raw_capture_cannot_escape_owned_directory(tmp_path, filename):
    budget = MeasurementBudget(100.0, 120.0, clock=lambda: 10.0)
    with pytest.raises(ValueError, match="one component"):
        collection.retain_raw(tmp_path / "raw", filename, b"controlled", budget)
    assert not (tmp_path / "raw").exists()


def test_origin_budget_route_does_not_allocate_legacy_unowned_process(tmp_path, monkeypatch):
    calls = []

    def runner(command, **kwargs):
        calls.append(kwargs)
        return subprocess.CompletedProcess(command, 0, b"controlled origin", b"")

    def forbidden_popen(*args, **kwargs):
        raise AssertionError("legacy Popen must not handle a broker-backed origin probe")

    monkeypatch.setattr(execution.subprocess, "Popen", forbidden_popen)
    budget = MeasurementBudget(100.0, 120.0, clock=lambda: 10.0, probe_runner=runner)
    result = execution.run_import_origin_preflight(
        project_root=tmp_path,
        python_executable=tmp_path / "python",
        environment={},
        timeout_seconds=30.0,
        probe_budget=budget,
    )
    assert result.status == "PASS" and len(calls) == 1
    assert calls[0]["environment"]["PYTHONWARNINGS"] == "ignore"
