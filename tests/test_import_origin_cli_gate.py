"""Acceptance tests for the #534 import-origin guard."""

import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.check_import_origins as origins
import scripts.production_gate as gate
from scripts.check_import_origins import main
from tests._import_origin_test_support import _make_package

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_is_within_resolves_before_comparing(tmp_path):
    """Containment must be decided on real paths, not on their spelling.

    ``Path.relative_to`` compares strings, so a lexical check would call
    ``<checkout>/build/pyboy-native-link`` a child of ``<checkout>/build``
    even when that name is a symlink into a sibling worktree, and would treat
    ``<checkout>/build/../../elsewhere`` as inside without ever applying the
    traversal.  Provenance is a claim about where a file really is.
    """

    project = tmp_path / "checkout"
    (project / "build").mkdir(parents=True)
    sibling = tmp_path / "other-worktree"
    (sibling / "build").mkdir(parents=True)

    link = project / "build" / "pyboy-native-link"
    link.symlink_to(sibling / "build", target_is_directory=True)

    assert not origins._is_within(link / "pyboy-native-x", project / "build")
    assert not origins._is_within(project / "build" / ".." / ".." / "other-worktree", project)
    assert origins._is_within(
        project / "build" / "pyboy-native-abc" / "pyboy-src", project / "build"
    )
    # A sibling checkout is never inside this one, prefix or not.
    assert not origins._is_within(sibling, project)


def test_cli_exit_codes_encode_the_verdict(tmp_path):
    _make_package(tmp_path / "elsewhere", "moved_pkg")
    project = tmp_path / "project"
    project.mkdir()

    rejected = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "check_import_origins.py"),
            "--project-root",
            str(project),
            "--package",
            "moved_pkg",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
        env={"PYTHONPATH": str(tmp_path / "elsewhere"), "PATH": "/usr/bin:/bin"},
    )

    assert rejected.returncode == 1
    assert json.loads(rejected.stdout)["status"] == "FAIL"


def test_cli_accepts_the_repo_checkout():
    accepted = main(["--project-root", str(REPO_ROOT)])
    assert accepted == 0


def test_preflight_reports_a_foreign_interpreter_as_a_gate_problem(tmp_path):
    """A stale interpreter must fail before any tier runs.

    The subprocess here is the real gate invocation path, so this proves the
    wiring -- not merely the helper -- refuses to continue.
    """

    foreign = tmp_path / "foreign"
    _make_package(foreign, "pokered_harness")
    _make_package(foreign, "pyboy")
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    (project / "scripts" / "check_import_origins.py").write_text(
        (REPO_ROOT / "scripts" / "check_import_origins.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    python = tmp_path / "bin" / "python"
    python.parent.mkdir()
    python.write_text("", encoding="utf-8")

    result = gate.run_import_origin_preflight(
        project_root=project,
        python_executable=sys.executable,
        environment={"PYTHONPATH": str(foreign)},
        timeout_seconds=60,
    )

    assert result.status == "FAIL"
    assert result.returncode == 1
    assert "outside the checkout under test" in result.reason


def test_gate_wiring_always_runs_the_origin_preflight(tmp_path, monkeypatch):
    """The gate must consult the preflight, not merely define it.

    Without this row the guard could be defined and left unused, which is the
    failure mode that lets a stale interpreter through in the first place.
    """

    python = tmp_path / "bin" / "python"
    python.parent.mkdir()
    python.write_text("", encoding="utf-8")
    (python.parent / "pytest").write_text("", encoding="utf-8")

    seen: list[dict] = []

    def fake_origin(**kwargs):
        seen.append(kwargs)
        return gate.CollectionResult("import-origins", [], "PASS", 0)

    monkeypatch.setattr(gate, "run_import_origin_preflight", fake_origin)
    monkeypatch.setattr(
        gate,
        "_run_collection_command",
        lambda **kwargs: gate.CollectionResult(
            name=kwargs["name"], command=kwargs["command"], status="PASS", returncode=0
        ),
    )

    results = gate.run_collection_preflight(
        project_root=tmp_path,
        python_executable=python,
        environment={},
    )

    assert [result.name for result in results] == [
        "import-origins",
        "python-module",
        "pytest-console",
    ]
    assert len(seen) == 1
    assert seen[0]["project_root"] == tmp_path


def test_suite_refuses_to_collect_against_another_checkout(tmp_path, monkeypatch):
    """The conftest guard is what stops an ad-hoc run from lying about its tree.

    The production gate already prepends this checkout to ``PYTHONPATH``, so it
    was never exposed; a bare ``pytest`` invocation is.
    """

    from tests import conftest

    monkeypatch.setattr(
        conftest,
        "check_origins",
        lambda _root: {
            "status": "FAIL",
            "packages": [
                {
                    "package": "pokered_harness",
                    "origin": "/elsewhere/src/pokered_harness/__init__.py",
                    "status": "FAIL",
                    "detail": "resolves outside the project root",
                }
            ],
        },
    )

    with pytest.raises(pytest.UsageError) as failure:
        conftest._enforce_local_import_origins()

    message = str(failure.value)
    assert "refusing to collect" in message
    assert "/elsewhere/src/pokered_harness/__init__.py" in message


def test_suite_allows_collection_when_origins_match(monkeypatch):
    from tests import conftest

    monkeypatch.setattr(
        conftest,
        "check_origins",
        lambda _root: {"status": "PASS", "packages": []},
    )

    # A guard that always raised would break every run, so the PASS direction
    # must stay silent.
    conftest._enforce_local_import_origins()


def test_suite_hook_actually_calls_the_import_origin_guard(monkeypatch):
    """``pytest_configure`` must invoke the guard, not merely define it.

    The two rows above exercise ``_enforce_local_import_origins`` directly, so
    they all pass while the call inside ``pytest_configure`` is deleted.  That
    mutant silently un-refuses every bare ``pytest`` run in this workspace,
    which is the exact condition #534 exists to prevent.  Mutation testing
    showed ``pass  # MUTATED`` at the hook surviving both the guard suite and a
    153-test wide lane, so this pins the wiring rather than the helper.

    That hook owns a second job -- registering the production-gate markers from
    ``tests/_tier_config.py`` -- which the marker-registration assertion below
    pins.  Recording ``addinivalue_line`` without ever asserting on it would
    leave that half of the hook contract unpinned, so this row and
    ``test_suite_hook_registers_every_production_gate_marker`` are both needed.
    """

    from tests import conftest

    calls: list[str] = []

    class RecordingConfig:
        def __init__(self) -> None:
            self.lines: list[tuple[str, str]] = []

        def addinivalue_line(self, name: str, line: str) -> None:
            self.lines.append((name, line))

    monkeypatch.setattr(
        conftest,
        "_enforce_local_import_origins",
        lambda: calls.append("guard"),
    )

    conftest.pytest_configure(RecordingConfig())

    assert calls == ["guard"], (
        "pytest_configure did not call _enforce_local_import_origins; a bare "
        "pytest run would no longer refuse a foreign checkout (#534)"
    )


def test_suite_hook_registers_every_production_gate_marker(monkeypatch):
    """``pytest_configure`` must also register every marker, not just guard.

    The row above records ``addinivalue_line`` calls but asserts only on the
    guard, so replacing the marker loop with ``pass`` left all 38 rows green
    while a bare ``pytest`` run would reject every ``-m unit`` selection as an
    unknown mark.  ``pytest_configure`` owns both jobs; pinning one job and
    ignoring the other is how the hook silently halves itself.
    """

    from tests import conftest
    from tests._tier_config import MARKERS

    recorded: list[tuple[str, str]] = []

    class RecordingConfig:
        def addinivalue_line(self, name: str, line: str) -> None:
            recorded.append((name, line))

    # The guard is stubbed so this row measures marker registration alone; the
    # guard call itself is pinned by test_suite_hook_actually_calls_the_import_origin_guard.
    monkeypatch.setattr(conftest, "_enforce_local_import_origins", lambda: None)

    conftest.pytest_configure(RecordingConfig())

    registered = {line.split(":", 1)[0].strip() for name, line in recorded if name == "markers"}
    assert registered == set(MARKERS), (
        "pytest_configure no longer registers every production-gate marker; "
        f"missing {sorted(set(MARKERS) - registered)}"
    )
    assert all(name == "markers" for name, _ in recorded), (
        f"pytest_configure registered unexpected ini lines: {recorded}"
    )


def test_a_failing_origin_preflight_stops_before_collection_runs(tmp_path, monkeypatch):
    """Fail closed: a foreign interpreter must not reach the pytest entry points."""

    python = tmp_path / "bin" / "python"
    python.parent.mkdir()
    python.write_text("", encoding="utf-8")
    (python.parent / "pytest").write_text("", encoding="utf-8")

    def fake_origin(**_kwargs):
        return gate.CollectionResult("import-origins", [], "FAIL", 1, reason="foreign checkout")

    def unexpected(**_kwargs):
        pytest.fail("a pytest collection entry point ran after the origin guard failed")

    monkeypatch.setattr(gate, "run_import_origin_preflight", fake_origin)
    monkeypatch.setattr(gate, "_run_collection_command", unexpected)

    results = gate.run_collection_preflight(
        project_root=tmp_path,
        python_executable=python,
        environment={},
    )

    origin = next(result for result in results if result.name == "import-origins")
    assert origin.status == "FAIL"
    assert not any(result.nodeids for result in results)


@pytest.mark.parametrize("package", ["pokered_harness", "pyboy"])
def test_preflight_accepts_the_real_interpreter_on_this_checkout(package, monkeypatch):
    """The non-vacuous PASS direction, proven through the real subprocess path."""

    result = gate.run_import_origin_preflight(
        project_root=REPO_ROOT,
        python_executable=Path(sys.executable),
        environment={
            "PYTHONPATH": os.pathsep.join(
                (
                    str(REPO_ROOT / "src"),
                    str(REPO_ROOT / "vendor" / "pyboy-src"),
                )
            )
        },
        timeout_seconds=60,
    )
    assert result.status == "PASS", result.reason


def test_matrix_audit_ignores_the_nodeidless_origin_row(monkeypatch):
    """The origin row must not be mistaken for the collected pytest tree.

    This PR prepends a nodeidless ``import-origins`` preflight to the
    collection list, so reading ``collection_list[0]`` audits an empty matrix
    and reports ``structural=FAIL``/``collected=0`` even when the real
    pytest collection succeeded.  The audit must read the first entry point
    that actually carries node ids.

    Master had no preflight row, so positional indexing was correct there;
    adding the row is what makes the shape reachable, which is why this row
    belongs with the guard rather than with the matrix auditor.
    """

    audited: list[tuple[str, ...]] = []

    def fake_audit_collection(nodeids, **kwargs):
        audited.append(tuple(nodeids))
        return {
            "structural_pass": True,
            "acceptance_matrix_complete": True,
            "collected": len(nodeids),
            "groups": {},
        }

    monkeypatch.setattr(
        runpy,
        "run_path",
        lambda _path: {
            "audit_collection": fake_audit_collection,
            "STRICT_ACCEPTANCE_NODEIDS": {
                "trade": frozenset({"tests/test_trade.py::test_trade_row"}),
                "battle": frozenset({"tests/test_battle.py::test_battle_row"}),
            },
        },
    )

    real_nodeids = ("tests/test_a.py::test_one", "tests/test_b.py::test_two")
    collections = [
        gate.CollectionResult("import-origins", [], "PASS", 0),
        gate.CollectionResult(
            "python-module",
            ["pytest"],
            "PASS",
            0,
            nodeids=real_nodeids,
        ),
    ]

    audit = gate.run_matrix_collection_audit(
        project_root=REPO_ROOT,
        collections=collections,
    )

    assert audited == [real_nodeids]
    assert audit["status"] == "PASS"
    assert audit["collected"] == len(real_nodeids)


def test_capacity_planning_ignores_the_nodeidless_origin_row():
    """The capacity call site of the shared reader must not drift back.

    ``collected_nodeids`` exists so that neither reader of the preflight
    collection list can index positionally.  ``run_matrix_collection_audit``
    has a row for that; this is its capacity-side twin.  Reinstating the
    pre-PR positional index in ``planned_tier_nodeids`` drops the planned set
    to empty, so ``register_blocked_rows`` records no BLOCKED rows and the
    refusal silently vanishes from capacity accounting.  Mutation testing
    showed that regression surviving the entire capacity suite.

    The real tier manifest is used deliberately: a synthetic row set would
    not exercise the classifier the production path actually calls.
    """

    this_file = Path(__file__).resolve().relative_to(REPO_ROOT).as_posix()
    # Name a row that really exists in this file, so a reader who greps for
    # the referenced test finds it.  ``classify_test`` keys tier membership on
    # the module, so the choice does not change the planned set.  Read the name
    # off the test symbol rather than repeating it as a literal, so renaming
    # that row cannot silently reintroduce the drift this row documents.
    planned_row = f"{this_file}::{test_is_within_resolves_before_comparing.__name__}"
    collections = [
        gate.CollectionResult("import-origins", [], "PASS", 0),
        gate.CollectionResult(
            "python-module",
            ["pytest"],
            "PASS",
            0,
            nodeids=(planned_row,),
        ),
    ]

    planned, problem = gate.planned_tier_nodeids(collections, "unit", REPO_ROOT)

    assert not problem, problem
    assert tuple(planned) == (planned_row,), (
        "the nodeidless origin row was mistaken for the collected tree"
    )
