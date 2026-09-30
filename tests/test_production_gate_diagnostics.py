"""Diagnostic sanitization, evidence bundles and raw-output bounds (#129).

Split from ``tests/test_production_gate.py`` for #129 with no behavior
change: every assertion and test ID below is preserved verbatim from the
original module. The shared ``raw_pytest_output`` fixture stays with the
test that uses it.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._production_gate_support import (
    _GATE_PATH,
    _FakeMatrixPopen,
    _matrix_report,
    _patch_main_inputs,
    _runtime_result,
    gate,
)


def _diagnostic_evidence_payload(roots, diagnostics, *, dual=False):
    """Exercise diagnostic fan-out without unrelated asset-path resolutions."""
    result = _runtime_result("source", gate_problems=tuple(diagnostics))
    result.runtime.update({f"diagnostic_{i}": value for i, value in enumerate(diagnostics)})
    collection = result.collections[0]
    collection.nodeids = tuple(diagnostics)
    collection.output_tail = diagnostics[0]
    collection.reason = diagnostics[0]
    tier = result.tiers[0]
    tier.command = ["python", diagnostics[0]]
    tier.output_tail = diagnostics[0]
    tier.reason = diagnostics[0]
    tier.iteration_failures = list(diagnostics)
    tier.selected_nodeids = list(diagnostics)
    tier.skip_reasons = dict.fromkeys(diagnostics, 1)
    tier.case_results = [
        gate.MatrixCaseResult(
            nodeid=value,
            status="FAIL",
            returncode=1,
            duration_seconds=0.0,
            reason=value,
            output_tail=value,
        )
        for value in diagnostics
    ]
    common = {
        "project_root": roots[0],
        "rom_root": roots[1],
        "fixture_root": roots[2],
        "assets": [],
        "overall": "FAIL",
        "generated_at": "2026-09-05T00:00:00+00:00",
        "evidence_error": diagnostics[0],
    }
    if dual:
        other = _runtime_result("cython", gate_problems=tuple(diagnostics))
        other.runtime = dict(result.runtime, pyboy_mode="cython")
        other.collections = result.collections
        other.tiers = result.tiers
        return gate.build_dual_evidence_payload(**common, runtime_results=[result, other])
    return gate.build_evidence_payload(
        **common,
        runtime=result.runtime,
        collections=result.collections,
        tiers=result.tiers,
        gate_problems=result.gate_problems,
    )


@pytest.mark.parametrize("dual", [False, True], ids=["single", "dual"])
def test_evidence_prepares_roots_once_independent_of_diagnostic_count(tmp_path, monkeypatch, dual):
    roots = (tmp_path / "checkout", tmp_path / "checkout/rom", tmp_path / "fixtures")
    original_resolve = Path.resolve
    calls = []

    def counted_resolve(path, *args, **kwargs):
        calls.append(path)
        return original_resolve(path, *args, **kwargs)

    for count in (1, 128):
        diagnostics = [
            f"row {i}: {roots[0]} | {roots[1]} | {roots[2]} password=hidden" for i in range(count)
        ]
        calls.clear()
        with monkeypatch.context() as patch:
            patch.setattr(Path, "resolve", counted_resolve)
            payload = _diagnostic_evidence_payload(roots, diagnostics, dual=dual)
        assert len(calls) == len(roots)
        assert set(calls) == set(roots)
        expected = [
            f"row {i}: <project-root> | <rom-root> | <fixture-root> [CREDENTIAL REDACTED]"
            for i in range(count)
        ]
        results = payload["runtimes"] if dual else [payload]
        for result in results:
            assert result["gate_problems"] == expected
            assert result["collections"][0]["nodeids"] == expected
            assert result["tiers"][0]["iteration_failures"] == expected
            assert [case["reason"] for case in result["tiers"][0]["case_results"]] == expected
        assert payload["evidence_error"] == expected[0]


@pytest.mark.parametrize("dual", [False, True], ids=["single", "dual"])
@pytest.mark.parametrize("context", ["symlink", "home-expansion"])
def test_evidence_rebuild_refreshes_root_context(tmp_path, monkeypatch, dual, context):
    # Model a retargeted symlink / changed expansion without mutating host paths or HOME.
    roots = tuple(Path("~") / name for name in ("checkout", "rom", "fixtures"))
    state = {"target": tmp_path / "first"}
    calls = []

    def expanduser(path):
        if path in roots and context == "home-expansion":
            return state["target"] / path.name
        return path

    def resolve(path, *args, **kwargs):
        calls.append(path)
        return state["target"] / path.name

    with monkeypatch.context() as patch:
        patch.setattr(Path, "expanduser", expanduser)
        patch.setattr(Path, "resolve", resolve)
        for target in ("first", "second", "first"):
            state["target"] = tmp_path / target
            diagnostics = [
                "roots: " + " | ".join(str(state["target"] / root.name) for root in roots)
            ]
            calls.clear()
            payload = _diagnostic_evidence_payload(roots, diagnostics, dual=dual)
            assert len(calls) == len(roots)
            assert payload["evidence_error"] == (
                "roots: <project-root> | <rom-root> | <fixture-root>"
            )


@pytest.mark.parametrize("case", ["whitespace", "single-quote", "double-quote"])
def test_sanitizer_adversarial_inputs_complete_in_bounded_subprocess(case):
    # A generous process bound catches quadratic/exponential regressions without
    # depending on millisecond timings or hanging the pytest worker on old regexes.
    code = r"""
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("bounded_production_gate", sys.argv[1])
gate = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gate
spec.loader.exec_module(gate)
case = sys.argv[2]
if case == "whitespace":
    for suffix in ("", ",", ";"):
        value = "password" + " " * 200_000 + suffix
        assert gate._safe_text(value, limit=len(value)) == value
    assert gate._safe_text("password" + " " * 200_000 + "=value") == "[CREDENTIAL REDACTED]"
else:
    quote = "'" if case == "single-quote" else '"'
    value = "b" + quote + "\\" * 96
    assert gate._safe_text(value, limit=len(value)) == "[BINARY DATA REDACTED]"
    assert gate._safe_text(value + quote) == "[BINARY DATA REDACTED]"
    escaped_quote = "b" + quote + "left\\" + quote + "right" + quote
    assert gate._safe_text(escaped_quote) == "[BINARY DATA REDACTED]"
print("bounded sanitizer semantics passed")
"""
    completed = subprocess.run(
        [sys.executable, "-B", "-c", code, str(_GATE_PATH), case],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "bounded sanitizer semantics passed"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Authorization: Bearer hidden; done", "[CREDENTIAL REDACTED]; done"),
        ("API_KEY = hidden, done", "[CREDENTIAL REDACTED], done"),
        ("token\t hidden\nnext", "[CREDENTIAL REDACTED]\nnext"),
        ("password", "password"),
        ("password , next", "password , next"),
        ("ordinary diagnostic\nassertion failed", "ordinary diagnostic\nassertion failed"),
        ("b'payload'", "[BINARY DATA REDACTED]"),
        ('B"payload"', "[BINARY DATA REDACTED]"),
        (r"b'left\'right'", "[BINARY DATA REDACTED]"),
        ('b"left\\"right"', "[BINARY DATA REDACTED]"),
        ("b'unterminated", "[BINARY DATA REDACTED]"),
        ('b"unterminated', "[BINARY DATA REDACTED]"),
        ("b'ROM_BYTES\\", "[BINARY DATA REDACTED]"),
        ('b"ROM_BYTES\\', "[BINARY DATA REDACTED]"),
        ("prefix " + "x" * 128 + "; tail", "prefix [LONG TOKEN REDACTED]; tail"),
        ("\x00tail", r"\x00tail"),
    ],
)
def test_sanitizer_preserves_representative_diagnostic_semantics(value, expected):
    assert gate._safe_text(value) == expected


@pytest.mark.parametrize("kind", ["credential", "bytes"])
def test_safe_diagnostic_sanitizes_full_value_before_tail_trim(kind):
    private = "private-fragment-" * 700
    value = f"password={private}" if kind == "credential" else f"b'{private}'"
    expected = "[CREDENTIAL REDACTED]" if kind == "credential" else "[BINARY DATA REDACTED]"
    assert gate._safe_diagnostic(value + "\nassertion-tail", (), limit=64) == (
        expected + "\nassertion-tail"
    )


@pytest.mark.parametrize("quote", ["'", '"'])
@pytest.mark.parametrize("suffix", ["", "\\"])
def test_safe_diagnostic_redacts_unclosed_bytes_before_tail_trim(quote, suffix):
    # Spaces keep individual fragments below the generic long-token threshold.
    # Trimming first would lose the byte-literal prefix and expose ROM fragments.
    value = "b" + quote + "ROM-fragment " * 700 + suffix
    assert gate._safe_diagnostic(value, (), limit=64) == "[BINARY DATA REDACTED]"


def test_safe_diagnostic_legacy_and_prepared_context_have_identical_root_priority(
    tmp_path, monkeypatch
):
    project = tmp_path / "checkout"
    roots = gate._evidence_roots(project, project / "rom", project / "tests/fixtures")
    # Distinct canonical targets model symlink aliases for every named root.
    canonical = {root: tmp_path / "canonical" / label for label, root in roots}
    calls = []

    def resolve(path, *args, **kwargs):
        calls.append(path)
        return canonical[path]

    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", resolve)
        replacements = gate._prepare_root_replacements(roots)
        assert len(calls) == len(roots)
        for label, root in roots:
            for spelling in (root, canonical[root]):
                value = f"failed at {spelling}/case.py; password=hidden; b'payload'"
                expected = (
                    f"failed at <{label}>/case.py; [CREDENTIAL REDACTED]; [BINARY DATA REDACTED]"
                )
                calls.clear()
                assert gate._safe_diagnostic(value, roots, replacements=replacements) == expected
                assert calls == []
                assert gate._safe_diagnostic(value, roots) == expected
                assert len(calls) == len(roots)
        calls.clear()
        assert gate._safe_diagnostic("plain", roots, replacements=()) == "plain"
        assert calls == []


def test_evidence_bundle_is_portable_sanitized_and_diagnostic(tmp_path):
    project_root = tmp_path / "checkout"
    rom_root = project_root / "rom"
    fixture_root = project_root / "tests" / "fixtures" / "link"
    asset_path = rom_root / "red" / "pokemon-red.gb"
    asset_path.parent.mkdir(parents=True)

    runtime = {
        "python_executable": str(project_root / ".venv" / "bin" / "python"),
        "python_version": "3.12.13",
        "pyboy_module": str(project_root / "vendor" / "pyboy-src" / "pyboy"),
        "pyboy_import_error": (
            "Authorization: Bearer topsecret b'ROM_BYTES' "
            r"path=C:\agent work\private\secret.gb"
        ),
    }
    collections = [
        gate.CollectionResult(
            name="python-module",
            command=[str(project_root / ".venv" / "bin" / "python"), "-m", "pytest"],
            status="PASS",
            returncode=0,
            nodeids=(
                "tests/test_gate.py::test_case[password=topsecret]",
                str(project_root / "tests" / "test_gate.py") + "::test_path",
            ),
        )
    ]
    tiers = [
        gate.TierResult(
            name="remote",
            description="remote",
            expression="remote",
            required=True,
            status="FAIL",
            counts=gate.Counts(total=1, failed=1),
            returncodes=[1],
            output_tail="password=topsecret b'ROM_BYTES'\nassertion failed",
            reason="peer failed at /sensitive/path",
            iteration_failures=["iteration 1: token=topsecret"],
        )
    ]
    payload = gate.build_evidence_payload(
        project_root=project_root,
        rom_root=rom_root,
        fixture_root=fixture_root,
        runtime=runtime,
        assets=[
            gate.AssetRecord(
                label="red-stock",
                kind="rom",
                path=str(asset_path),
                status="ok",
                size=123,
                actual_sha1="a" * 40,
            )
        ],
        collections=collections,
        tiers=tiers,
        gate_problems=["credential=topsecret at " + str(project_root)],
        overall="FAIL",
    )

    serialized = json.dumps(payload, sort_keys=True)
    assert "topsecret" not in serialized
    assert "ROM_BYTES" not in serialized
    assert "secret.gb" not in serialized
    assert str(tmp_path) not in serialized
    assert "/sensitive/path" not in serialized
    assert payload["assets"][0]["path"] == "<rom-root>/red/pokemon-red.gb"
    assert all("topsecret" not in nodeid for nodeid in payload["collections"][0]["nodeids"])
    assert all(str(tmp_path) not in nodeid for nodeid in payload["collections"][0]["nodeids"])
    assert payload["tiers"][0]["status"] == "FAIL"
    assert "assertion failed" in payload["tiers"][0]["output_tail"]

    evidence_dir = tmp_path / "evidence" / "partial-run"
    paths = gate.write_evidence_bundle(evidence_dir, payload)
    assert set(paths) == {"report", "text", "manifest"}
    assert {path.name for path in paths.values()} == {
        "gate-report.json",
        "gate-report.txt",
        "evidence-manifest.json",
    }
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert report["overall"] == "FAIL"
    assert report["tiers"][0]["iteration_failures"]
    assert manifest["overall"] == "FAIL"
    assert {entry["path"] for entry in manifest["files"]} == {
        "gate-report.json",
        "gate-report.txt",
    }
    report_hash = hashlib.sha256(paths["report"].read_bytes()).hexdigest()
    assert (
        next(entry for entry in manifest["files"] if entry["path"] == "gate-report.json")["sha256"]
        == report_hash
    )
    text = paths["text"].read_text(encoding="utf-8")
    assert "overall: FAIL" in text
    assert "assertion failed" in text
    assert "topsecret" not in text
    assert "ROM_BYTES" not in text
    gate.verify_evidence_bundle(evidence_dir)

    paths["report"].write_text(
        paths["report"].read_text(encoding="utf-8") + "tampered\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="(?:size|sha256) mismatch"):
        gate.verify_evidence_bundle(evidence_dir)


def test_evidence_bundle_rejects_unlisted_files(tmp_path):
    payload = gate.build_evidence_payload(
        project_root=tmp_path / "checkout",
        rom_root=tmp_path / "rom",
        fixture_root=tmp_path / "fixtures",
        runtime={},
        assets=[],
        collections=[],
        tiers=[],
        gate_problems=[],
        overall="PASS",
    )
    evidence_dir = tmp_path / "evidence"
    gate.write_evidence_bundle(evidence_dir, payload)
    (evidence_dir / "raw-debug.log").write_text("unredacted", encoding="utf-8")

    with pytest.raises(ValueError, match="unexpected files"):
        gate.verify_evidence_bundle(evidence_dir)


def test_parser_accepts_evidence_directory():
    args = gate.build_parser().parse_args(["--evidence-dir", "retained-evidence"])
    assert args.evidence_dir == Path("retained-evidence")


@pytest.fixture
def raw_pytest_output(monkeypatch):
    """Keep the real runner/report loader; replace only the pytest process."""

    outputs = {}

    class CompletedPytest:
        def __init__(self, command, *, env, **kwargs):
            report_path = Path(env["POKERED_GATE_REPORT"])
            collection = "--collect-only" in command
            failed = report_path.stem == "timing-2"
            self.returncode = int(failed)
            payload = _matrix_report(
                "tests/test_sample.py::test_one", outcome="failed" if failed else "passed"
            )
            payload["exitstatus"] = self.returncode
            if collection:
                payload.update(
                    collection_only=True,
                    counts={name: 0 for name in payload["counts"]},
                    tests=[],
                )
            report_path.write_text(json.dumps(payload), encoding="utf-8")
            self.output = (
                f"BEGIN {env.get('runtime_mode', 'source')} {report_path.stem}\n"
                "token=private-capture-secret\n" + "complete captured line\n" * 600 + "END\n"
            )
            outputs[(env.get("runtime_mode", "source"), report_path.stem)] = self.output

        def communicate(self, timeout=None):
            return self.output, None

    monkeypatch.setattr(gate.subprocess, "Popen", CompletedPytest)
    return outputs


def test_dual_gate_retains_complete_private_logs_and_sanitized_evidence(
    tmp_path, monkeypatch, capsys, raw_pytest_output
):
    monkeypatch.setattr(
        gate,
        "build_test_environment",
        lambda *args, runtime_mode: {"runtime_mode": runtime_mode},
    )
    monkeypatch.setattr(
        gate,
        "probe_runtime",
        lambda _python, _root, env: {"pyboy_mode": env["runtime_mode"]},
    )
    monkeypatch.setattr(gate, "runtime_problems", lambda *args, **kwargs: [])
    monkeypatch.setattr(gate, "environment_policy_problems", lambda **kwargs: [])
    monkeypatch.setattr(gate, "_pytest_console_script", lambda path: path.parent / "pytest")
    monkeypatch.setattr(
        gate,
        "run_fixture_manifest_validation",
        lambda **kwargs: {"status": "PASS", "mode": "schema"},
    )
    monkeypatch.setattr(gate, "run_matrix_collection_audit", lambda **kwargs: {"status": "PASS"})
    raw_directory = tmp_path / "private-output"
    evidence_directory = tmp_path / "evidence"
    args = _patch_main_inputs(monkeypatch, tmp_path)
    args += [
        "--runtime-mode",
        "both",
        "--raw-output-dir",
        str(raw_directory),
        "--evidence-dir",
        str(evidence_directory),
    ]

    assert gate.main(args) == 1
    report = json.loads(capsys.readouterr().out)
    for runtime in report["runtimes"]:
        assert runtime["tiers"][0]["status"] == "PASS"
        timing = runtime["tiers"][1]
        assert timing["status"] == "FAIL"
        assert timing["counts"]["passed"] == 4
        assert timing["counts"]["failed"] == 1
        assert timing["returncodes"] == [0, 1, 0, 0, 0]
    for mode in ("source", "cython"):
        paths = list((raw_directory / mode).glob("*.log"))
        assert len(paths) == 8  # Both collection entrypoints, unit, five timing iterations.
        for path in paths:
            stem = {"collection-python-module": "0", "collection-pytest-console": "1"}.get(
                path.stem, path.stem
            )
            assert path.read_text(encoding="utf-8") == raw_pytest_output[(mode, stem)]
            if os.name == "posix":
                assert path.stat().st_mode & 0o777 == 0o600
        if os.name == "posix":
            assert (raw_directory / mode).stat().st_mode & 0o777 == 0o700
    if os.name == "posix":
        assert raw_directory.stat().st_mode & 0o777 == 0o700
    for filename in ("gate-report.json", "gate-report.txt"):
        text = (evidence_directory / filename).read_text(encoding="utf-8")
        assert "private-capture-secret" not in text
        assert str(raw_directory) not in text
    gate.verify_evidence_bundle(evidence_directory)


def test_raw_output_collision_preserves_old_log_and_fails_passing_tier(tmp_path, raw_pytest_output):
    raw_directory = tmp_path / "private-output"
    raw_directory.mkdir()
    retained = raw_directory / "unit-1.log"
    retained.write_text("earlier evidence", encoding="utf-8")

    result = gate.run_tier(
        name="unit",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        repeat=1,
        timeout_override=1,
        report_directory=tmp_path,
        raw_output_directory=raw_directory,
    )

    assert result.status == "FAIL"
    assert result.counts == gate.Counts(total=1, passed=1)
    assert result.returncodes == [0]
    assert any(
        "could not retain raw output unit-1.log: FileExistsError" in failure
        for failure in result.iteration_failures
    )
    assert retained.read_text(encoding="utf-8") == "earlier evidence"


@pytest.mark.parametrize("collision", (False, True))
def test_matrix_raw_output_keeps_complete_passed_row_or_reports_collision(
    tmp_path, monkeypatch, collision
):
    output = "BEGIN token=private-capture-secret\n" + "full trace\n" * 700 + "END\n"

    class VerboseMatrix(_FakeMatrixPopen):
        mode = "pass"

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.stdout = io.StringIO(output)

    monkeypatch.setattr(gate.subprocess, "Popen", VerboseMatrix)
    nodeid = "tests/test_matrix.py::test_pair[red-blue]"
    raw_directory = tmp_path / "private-output"
    filename = "matrix-" + hashlib.sha256(nodeid.encode()).hexdigest()[:20] + ".log"
    retained = raw_directory / filename
    if collision:
        raw_directory.mkdir()
        retained.write_text("earlier evidence", encoding="utf-8")
    result = gate.run_tier(
        name="trade",
        project_root=tmp_path,
        python_executable=Path("python"),
        environment={},
        required_problems=[],
        repeat=1,
        timeout_override=1,
        report_directory=tmp_path,
        required_nodeids=(nodeid,),
        raw_output_directory=raw_directory,
    )

    assert result.status == ("FAIL" if collision else "PASS")
    assert result.counts == gate.Counts(total=1, passed=1)
    assert result.case_results[0].returncode == 0
    assert len(result.case_results[0].output_tail) <= 4000
    assert retained.read_text(encoding="utf-8") == ("earlier evidence" if collision else output)
    if collision:
        assert "could not retain raw output" in result.case_results[0].reason


def test_collection_timeout_retains_complete_captured_output(tmp_path, monkeypatch):
    output = "BEGIN\n" + "partial collection\n" * 700 + "END\n"

    class TimedOutCollection:
        def __init__(self, *args, **kwargs):
            pass

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired("pytest", timeout)

    monkeypatch.setattr(gate.subprocess, "Popen", TimedOutCollection)
    monkeypatch.setattr(gate, "_terminate_process", lambda process: None)
    monkeypatch.setattr(gate, "_communicate_after_termination", lambda process: output)
    result = gate._run_collection_command(
        name="python-module",
        command=["python", "-m", "pytest"],
        project_root=tmp_path,
        environment={},
        timeout_seconds=1,
        report_path=tmp_path / "collection.json",
        raw_output_directory=tmp_path / "private-output",
    )

    assert result.status == "FAIL"
    assert result.returncode == 124
    assert result.reason == "collection timeout"
    retained = tmp_path / "private-output" / "collection-python-module.log"
    assert retained.read_text(encoding="utf-8") == output


@pytest.mark.parametrize("location", ("equal", "nested", "existing"))
def test_main_rejects_unsafe_raw_output_directory(tmp_path, capsys, location):
    evidence = tmp_path / "evidence"
    raw_directory = evidence / "raw" if location == "nested" else evidence
    if location == "existing":
        raw_directory = tmp_path / "earlier-output"
        raw_directory.mkdir()
        (raw_directory / "keep").write_text("earlier evidence", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        gate.main(["--evidence-dir", str(evidence), "--raw-output-dir", str(raw_directory)])

    assert error.value.code == 2
    assert "--raw-output-dir" in capsys.readouterr().err
    if location == "existing":
        assert (raw_directory / "keep").read_text(encoding="utf-8") == "earlier evidence"
    else:
        assert not evidence.exists()
