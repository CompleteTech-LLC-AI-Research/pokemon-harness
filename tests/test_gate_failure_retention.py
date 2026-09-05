"""Asset-free regression coverage for durable, bounded gate failure evidence."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "gate_failure_retention_under_test", ROOT / "scripts" / "production_gate.py"
)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)

ORIGINAL_LINE = '>       exercise_original_failure("retention-origin")'
TERMINAL_EXCEPTION = "E       RuntimeError: retention-terminal-exception"


def _trace(lines=180):
    return "\n".join(
        [ORIGINAL_LINE]
        + [f"    frame_{index:05d}: diagnostic context remains available" for index in range(lines)]
        + [TERMINAL_EXCEPTION]
    )


def _report(path, failures, *, kind="failed"):
    records = [
        {
            "nodeid": nodeid,
            "outcome": kind if reason is not None else "passed",
            "when": "call",
            "was_xfail": False,
            "reason": reason or "",
        }
        for nodeid, reason in failures
    ]
    records.append(
        {
            "nodeid": "tests/test_fake.py::test_pass",
            "outcome": "passed",
            "when": "call",
            "was_xfail": False,
            "reason": "",
        }
    )
    counts = {
        "total": len(records),
        "passed": 1,
        "failed": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
        "errors": 0,
    }
    counts[kind] += sum(reason is not None for _, reason in failures)
    counts["passed"] += sum(reason is None for _, reason in failures)
    returncode = int(bool(counts["failed"]))
    payload = {
        "counts": counts,
        "tests": records,
        "collection_errors": [],
        "collection_skips": [],
        "collection_only": False,
        "collected": len(records),
        "nodeids": [record["nodeid"] for record in records],
        "exitstatus": returncode,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    report = gate._load_gate_report(path, expected_returncode=returncode)
    assert report.error == ""
    return returncode, report


@pytest.fixture
def run_records(tmp_path, monkeypatch):
    def run(iterations, *, kind="failed", timing=False):
        calls = []

        def fake_run(**kwargs):
            failures = iterations[len(calls)]
            calls.append(kwargs)
            returncode, report = _report(kwargs["report_path"], failures, kind=kind)
            # The traceback precedes enough PASS noise to defeat output-tail retention.
            output = "\n".join(reason for _, reason in failures if reason is not None)
            output += "\n" + "PASSED tests/test_fake.py::test_later_success\n" * 500
            return returncode, report, output, ["python", "-m", "pytest"]

        monkeypatch.setattr(gate, "run_pytest_once", fake_run)
        result = gate.run_tier(
            name="timing" if timing else "unit",
            project_root=tmp_path,
            python_executable=Path(sys.executable),
            environment={},
            required_problems=[],
            repeat=len(iterations),
            timeout_override=10,
            report_directory=tmp_path,
        )
        assert len(calls) == len(iterations)
        return result

    return run


def _bundle(tmp_path, result):
    arguments = {
        "project_root": tmp_path,
        "rom_root": tmp_path / "rom",
        "fixture_root": tmp_path / "fixtures",
        "runtime": {},
        "assets": [],
        "collections": [],
        "tiers": [result],
        "gate_problems": [],
        "overall": "FAIL",
    }
    payload = gate.build_evidence_payload(**arguments)
    paths = gate.write_evidence_bundle(tmp_path / "evidence", payload)
    saved = json.loads(paths["report"].read_text(encoding="utf-8"))
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert saved == payload
    assert manifest["overall"] == "FAIL"
    assert {entry["path"] for entry in manifest["files"]} == {
        paths["report"].name,
        paths["text"].name,
    }
    for entry in manifest["files"]:
        content = (paths["manifest"].parent / entry["path"]).read_bytes()
        assert entry["sha256"] == hashlib.sha256(content).hexdigest()
        assert entry["size"] == len(content)
    gate.verify_evidence_bundle(paths["manifest"].parent)
    return saved, paths["text"].read_text(encoding="utf-8"), gate.render_text(**arguments)


def test_complete_trace_survives_pass_noise_and_all_evidence_formats(tmp_path, run_records):
    reason = _trace()
    assert 8000 < len(reason) < gate.MAX_FAILURE_DETAIL_CHARS
    result = run_records([[("tests/test_fake.py::test_original", reason)]])
    assert result.status == "FAIL"
    assert result.counts == gate.Counts(total=2, passed=1, failed=1)
    assert result.returncodes == [1]
    assert result.failure_details_omitted == 0
    (detail,) = result.failure_details
    assert detail.iteration == 1
    assert detail.reason == reason
    assert detail.original_chars == len(reason)
    assert detail.omitted_chars == 0
    assert not detail.truncated
    saved, evidence_text, stdout_text = _bundle(tmp_path, result)
    assert saved["tiers"][0]["failure_details"][0]["reason"] == reason
    for text in (evidence_text, stdout_text):
        for line in reason.splitlines():
            assert line in text


def test_oversized_trace_retains_both_ends_with_exact_omission_accounting(tmp_path, run_records):
    reason = _trace(3000)
    assert len(reason) > gate.MAX_FAILURE_DETAIL_CHARS
    result = run_records([[("tests/test_fake.py::test_oversized", reason)]])
    (detail,) = result.failure_details
    assert detail.truncated
    assert len(detail.reason) <= gate.MAX_FAILURE_DETAIL_CHARS
    assert detail.reason.startswith(ORIGINAL_LINE)
    assert detail.reason.endswith(TERMINAL_EXCEPTION)
    assert detail.original_chars == len(reason)
    # Recover retained source spans independently of the marker's spelling.
    head = 0
    while head < len(detail.reason) and detail.reason[head] == reason[head]:
        head += 1
    tail = 0
    while tail < len(detail.reason) - head and detail.reason[-tail - 1] == reason[-tail - 1]:
        tail += 1
    marker = detail.reason[head : len(detail.reason) - tail]
    assert "omitt" in marker.lower() or "truncat" in marker.lower()
    assert abs(head - tail) <= 2
    assert detail.omitted_chars == len(reason) - head - tail
    saved, text, stdout = _bundle(tmp_path, result)
    assert saved["tiers"][0]["failure_details"][0] == asdict(detail)
    for rendered in (text, stdout):
        assert ORIGINAL_LINE in rendered
        assert TERMINAL_EXCEPTION in rendered
        assert marker.strip() in rendered


@pytest.mark.parametrize("large", [False, True])
def test_repeated_failures_share_record_and_character_budgets(run_records, large):
    reason = _trace(3000 if large else 2)
    failures = [(f"tests/test_fake.py::test_failure_{index}", reason) for index in range(40)]
    result = run_records([failures] * 5, timing=True)
    assert result.status == "FAIL"
    assert result.counts == gate.Counts(total=205, passed=5, failed=200)
    assert result.returncodes == [1] * 5
    details = result.failure_details
    assert 0 < len(details) <= gate.MAX_FAILURE_DETAILS
    assert len(details) + result.failure_details_omitted == 200
    assert result.failure_details_omitted > 0
    retained = sum(len(d.reason) + len(d.nodeid) + len(d.outcome) for d in details)
    assert retained <= gate.MAX_FAILURE_DETAILS_CHARS
    assert all(len(d.reason) <= gate.MAX_FAILURE_DETAIL_CHARS for d in details)
    assert details[0].iteration == 1
    assert details[0].nodeid == failures[0][0]
    if not large:
        assert len(details) == gate.MAX_FAILURE_DETAILS
        assert details[-1].iteration > 1


def test_early_failure_survives_four_successful_repeats(tmp_path, run_records):
    nodeid = "tests/test_fake.py::test_original"
    # Keep collection stable across repetitions; subsequent empty reasons are passes.
    result = run_records([[(nodeid, _trace())]] + [[(nodeid, None)]] * 4, timing=True)
    assert result.status == "FAIL"
    assert result.counts == gate.Counts(total=10, passed=9, failed=1)
    assert result.returncodes == [1, 0, 0, 0, 0]
    assert len(result.failure_details) == 1
    assert result.failure_details[0].reason == _trace()
    saved, text, stdout = _bundle(tmp_path, result)
    assert saved["tiers"][0]["failure_details"][0]["iteration"] == 1
    for rendered in (text, stdout):
        assert ORIGINAL_LINE in rendered
        assert TERMINAL_EXCEPTION in rendered


def test_secret_and_rom_blob_redacted_before_clipping_and_counting(tmp_path, run_records):
    secret = "RETENTION_SECRET_7482"
    blob = "ROM_PAYLOAD_7482" * 8000
    reason = f"{ORIGINAL_LINE}\ntoken={secret}\nrom=b'{blob}'\n{TERMINAL_EXCEPTION}"
    nodeid = "tests/test_fake.py::test_secret[" + "short-param;" * 160 + "]"
    result = run_records([[(nodeid, reason)]])
    (detail,) = result.failure_details
    assert secret not in detail.reason
    assert "ROM_PAYLOAD_7482" not in detail.reason
    assert detail.original_chars < len(reason)
    assert detail.original_chars == len(detail.reason)
    assert detail.omitted_chars == 0
    assert len(detail.nodeid) <= gate.MAX_FAILURE_NODEID_CHARS
    assert detail.nodeid_original_chars == len(nodeid)
    assert detail.nodeid_omitted_chars > 0
    saved, text, stdout = _bundle(tmp_path, result)
    for serialized in (json.dumps(saved), text, stdout):
        assert secret not in serialized
        assert "ROM_PAYLOAD_7482" not in serialized
        assert ORIGINAL_LINE in serialized or "retention-origin" in serialized
        assert TERMINAL_EXCEPTION in serialized


def test_required_skip_still_fails_without_changing_counts(run_records):
    result = run_records(
        [[("tests/test_fake.py::test_skip", "coverage unavailable")]], kind="skipped"
    )
    assert result.status == "FAIL"
    assert result.returncodes == [0]
    assert result.counts == gate.Counts(total=2, passed=1, skipped=1)
    assert result.failure_details[0].outcome == "skipped"


def test_direct_constructed_detail_is_sanitized_at_final_evidence_boundary(tmp_path):
    private_root = tmp_path / "private-checkout"
    rom_root = private_root / "rom"
    secret = "DIRECT_CONSTRUCTED_SECRET_428"
    blob = "DIRECT_ROM_BYTES_428"
    reason = (
        _trace()
        + f"\ntoken={secret}\nrom=b'{blob}'\n"
        + f"source={private_root}/source.py\nrom_root={rom_root}\n"
        + TERMINAL_EXCEPTION
    )
    detail = gate.FailureDetail(
        iteration=1,
        nodeid="tests/test_fake.py::test_direct",
        outcome="failed",
        reason=reason,
        original_chars=len(reason),
        omitted_chars=0,
        truncated=False,
        nodeid_original_chars=len("tests/test_fake.py::test_direct"),
        nodeid_omitted_chars=0,
    )
    result = gate.TierResult(
        name="unit",
        description="direct evidence input",
        expression="unit",
        required=True,
        status="FAIL",
        counts=gate.Counts(total=1, failed=1),
        failure_details=[detail],
    )
    roots = (("<rom-root>", rom_root), ("<project-root>", private_root))
    safe = gate._safe_tier(result, roots)
    payload = gate.build_evidence_payload(
        project_root=private_root,
        rom_root=rom_root,
        fixture_root=tmp_path / "fixtures",
        runtime={},
        assets=[],
        collections=[],
        tiers=[result],
        gate_problems=[],
        overall="FAIL",
    )
    paths = gate.write_evidence_bundle(tmp_path / "direct-evidence", payload)
    for encoded in (
        json.dumps(safe),
        paths["report"].read_text(encoding="utf-8"),
        paths["text"].read_text(encoding="utf-8"),
    ):
        assert secret not in encoded
        assert blob not in encoded
        assert str(private_root) not in encoded
        assert "<project-root>" in encoded
        assert "<rom-root>" in encoded
        assert "retention-origin" in encoded
        assert TERMINAL_EXCEPTION in encoded
    assert len(safe["failure_details"][0]["reason"]) > 8000
    for line in _trace().splitlines():
        assert line in safe["failure_details"][0]["reason"]


def test_real_pytest_failure_report_survives_later_pass_output(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / "_gate_report.py").write_text(
        (ROOT / "tests" / "_gate_report.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tests / "test_retention_sample.py").write_text(
        "import pytest\npytestmark = pytest.mark.unit\n"
        "def test_000_failure():\n"
        '    raise RuntimeError("real-retention-terminal")\n'
        '@pytest.mark.parametrize("index", range(240))\n'
        "def test_later_pass(index):\n    assert index >= 0\n",
        encoding="utf-8",
    )
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers = unit: asset-free\n", encoding="utf-8")
    environment = dict(
        os.environ,
        PYTHONPATH=str(tmp_path),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTEST_ADDOPTS="",
        PYTHONDONTWRITEBYTECODE="1",
    )
    result = gate.run_tier(
        name="unit",
        project_root=tmp_path,
        python_executable=Path(sys.executable),
        environment=environment,
        required_problems=[],
        repeat=1,
        timeout_override=30,
        report_directory=tmp_path,
    )
    assert result.status == "FAIL"
    assert result.counts == gate.Counts(total=241, passed=240, failed=1)
    (detail,) = result.failure_details
    assert 'raise RuntimeError("real-retention-terminal")' in detail.reason
    assert "E       RuntimeError: real-retention-terminal" in detail.reason
    assert not detail.truncated
    saved, text, stdout = _bundle(tmp_path, result)
    assert saved["tiers"][0]["failure_details"][0]["reason"] == detail.reason
    for rendered in (text, stdout):
        assert 'raise RuntimeError("real-retention-terminal")' in rendered
        assert "E       RuntimeError: real-retention-terminal" in rendered
