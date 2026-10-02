"""Acceptance for the #534 import-origin guard.

The repository is developed across many worktrees that share one virtual
environment.  When the editable install points at a different worktree, every
selected tier silently measures that other tree.  These rows pin both
directions: the guard must accept the tree under test and reject any other.
"""

import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

import scripts.check_import_origins as origins
import scripts.production_gate as gate
from scripts.check_import_origins import check_origins, main

REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_package(root: Path, package: str) -> Path:
    """Create a minimal importable package under ``root`` and return its dir."""

    directory = root / package
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text("", encoding="utf-8")
    return directory


def test_repo_checkout_resolves_its_own_packages():
    """The guarded checkout must satisfy its own guard.

    This is the non-vacuous control: without it the FAIL rows below could pass
    for the wrong reason.
    """

    report = check_origins(REPO_ROOT)
    offenders = [item for item in report["packages"] if item["status"] != "PASS"]
    assert offenders == [], f"guard rejects its own checkout: {offenders}"
    assert report["status"] == "PASS"


def test_check_origins_accepts_packages_inside_the_project_root(tmp_path, monkeypatch):
    package = _make_package(tmp_path, "inside_pkg")
    monkeypatch.syspath_prepend(str(tmp_path))

    report = check_origins(tmp_path, ("inside_pkg",))

    assert report["status"] == "PASS"
    assert report["packages"][0]["origin"] == str(package / "__init__.py")


def _fake_install(distributions, package_dirs, installed_from):
    """Build a stand-in for ``importlib.metadata`` describing one install.

    ``installed_from`` is the directory pip recorded in ``direct_url.json``,
    which is the whole discriminator between this checkout's install and a
    sibling worktree's.
    """

    class _Distribution:
        def __init__(self, name):
            self._name = name

        def read_text(self, filename):
            if filename != "direct_url.json":
                return None
            return json.dumps({"dir_info": {"editable": True}, "url": f"file://{installed_from}"})

        def locate_file(self, name):
            # A real ``Distribution.locate_file`` returns a path for any name;
            # only the directories actually installed should be honored, so
            # anything else falls back to an unrelated location.
            return package_dirs.get(name, installed_from / "unrelated" / name)

    table = {name: _Distribution(name) for name in distributions}
    # The real ``importlib.metadata.distribution`` normalizes the name it is
    # asked for (PEP 503), so a lookup of "pyboy" finds an install recorded as
    # "PyBoy".  Key the stand-in the same way or the normalization the checker
    # performs cannot be exercised.
    return {
        re.sub(r"[-_.]+", "-", name).lower(): distribution for name, distribution in table.items()
    }


def _distribution_lookup(table):
    """Return a stand-in for ``importlib.metadata.distribution``.

    A missing name must raise ``PackageNotFoundError``, as the real API does;
    a plain ``dict.__getitem__`` would raise ``KeyError`` and hide the
    checker's normalized-then-literal fallback path behind an unrelated error.
    """

    def lookup(name):
        try:
            return table[name]
        except KeyError:
            raise origins.importlib.metadata.PackageNotFoundError(name) from None

    return lookup


def _pin_vendored_revision(project: Path, revision: str) -> None:
    """Write the vendored PyBoy revision marker ``project`` pins."""

    marker = project / "vendor" / "pyboy-src" / "POKERED_HARNESS_PYBOY_REVISION"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(f"{revision}\n", encoding="utf-8")


def _fake_revision_marker(monkeypatch, revision: str | None) -> None:
    """Make the imported ``pyboy`` report ``revision`` (or nothing at all)."""

    module = types.ModuleType("pyboy")
    if revision is not None:
        module.__pokered_harness_revision__ = revision
    monkeypatch.setitem(sys.modules, "pyboy", module)


def test_check_origins_accepts_a_venv_installed_from_this_checkout(tmp_path, monkeypatch):
    """The release lane: CI installs into a venv outside the checkout.

    ``scripts/run_native_unit_ci.sh`` creates its venv under ``$TMPDIR``, so
    ``pyboy`` resolves to ``<venv>/site-packages/pyboy`` rather than to
    ``vendor/pyboy-src``.  That is correct behavior, and the guard must accept
    it -- an earlier containment-only check failed the real CI lane.
    """

    project = tmp_path / "checkout"
    project.mkdir()
    site_packages = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    installed = {name: site_packages / name for name in ("pokered_harness", "pyboy")}
    for name, directory in installed.items():
        _make_package(directory.parent, name)
    monkeypatch.setattr(
        origins.importlib.metadata,
        "distribution",
        _distribution_lookup(
            _fake_install(
                ["pokered-harness"],
                {name: path for name, path in installed.items()},
                project,
            )
        ),
    )
    monkeypatch.setattr(
        origins.importlib.metadata,
        "packages_distributions",
        lambda: {
            "pokered_harness": ["pokered-harness"],
            "pyboy": ["pokered-harness"],
        },
    )
    monkeypatch.setattr(
        origins, "_resolve_origin", lambda name: (installed[name] / "__init__.py", "")
    )

    report = check_origins(project)

    assert report["status"] == "PASS", report


def test_check_origins_accepts_pyboy_owned_by_its_own_distribution(tmp_path, monkeypatch):
    """The shape the native CI lane actually produces.

    ``run_native_unit_ci.sh`` builds ``pyboy`` from this checkout's vendored
    source and installs it as a *separate* ``pyboy`` distribution, so the
    package's owner is ``pyboy`` rather than ``pokered-harness``.  An earlier
    fix admitted only ``pokered-harness``-owned site-packages and so failed
    the real CI run with ``pyboy`` reported as outside the checkout.

    The install source cannot be the discriminator: the native lane builds from
    a *staged copy* of ``vendor/pyboy-src`` in a temporary directory, so
    ``direct_url.json`` records a throwaway path that never equals the
    checkout.  What separates it from a stock PyBoy is that it reports this
    checkout's vendored revision, which is what this row pins.
    """

    project = tmp_path / "checkout"
    project.mkdir()
    _pin_vendored_revision(project, "rev-from-this-checkout")
    staged = tmp_path / "build" / "pyboy-native-abc" / "pyboy-src"
    staged.mkdir(parents=True)
    site_packages = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    installed = {name: site_packages / name for name in ("pokered_harness", "pyboy")}
    for name, directory in installed.items():
        _make_package(directory.parent, name)
    # The harness is an editable install of the checkout; pyboy is a separate
    # distribution built from the staged copy, so it records that temp path.
    distributions = {
        **_fake_install(
            ["pokered-harness"], {"pokered_harness": installed["pokered_harness"]}, project
        ),
        **_fake_install(["pyboy"], {"pyboy": installed["pyboy"]}, staged),
    }
    monkeypatch.setattr(
        origins.importlib.metadata, "distribution", _distribution_lookup(distributions)
    )
    monkeypatch.setattr(
        origins.importlib.metadata,
        "packages_distributions",
        lambda: {"pokered_harness": ["pokered-harness"], "pyboy": ["pyboy"]},
    )
    monkeypatch.setattr(
        origins, "_resolve_origin", lambda name: (installed[name] / "__init__.py", "")
    )
    _fake_revision_marker(monkeypatch, "rev-from-this-checkout")

    report = check_origins(project)

    assert report["status"] == "PASS", report


def test_check_origins_rejects_a_foreign_pyboy_distribution(tmp_path, monkeypatch):
    """A pyboy distribution from another checkout must not be admitted.

    The companion to the row above: admitting a separately distributed
    ``pyboy`` is only safe while it is traceable to this checkout.  One built
    from a sibling worktree records that other directory *and* pins that
    worktree's own vendored revision, so it fails both tests — which is the
    original #534 defect, not a loosened one.
    """

    project = tmp_path / "checkout"
    project.mkdir()
    _pin_vendored_revision(project, "rev-from-this-checkout")
    other = tmp_path / "other-worktree"
    site_packages = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    installed = {name: site_packages / name for name in ("pokered_harness", "pyboy")}
    for name, directory in installed.items():
        _make_package(directory.parent, name)
    distributions = {
        **_fake_install(
            ["pokered-harness"], {"pokered_harness": installed["pokered_harness"]}, project
        ),
        **_fake_install(["pyboy"], {"pyboy": installed["pyboy"]}, other),
    }
    monkeypatch.setattr(
        origins.importlib.metadata, "distribution", _distribution_lookup(distributions)
    )
    monkeypatch.setattr(
        origins.importlib.metadata,
        "packages_distributions",
        lambda: {"pokered_harness": ["pokered-harness"], "pyboy": ["pyboy"]},
    )
    monkeypatch.setattr(
        origins, "_resolve_origin", lambda name: (installed[name] / "__init__.py", "")
    )
    _fake_revision_marker(monkeypatch, "rev-from-the-other-worktree")

    report = check_origins(project)

    assert report["status"] == "FAIL", report
    assert report["packages"][1]["package"] == "pyboy"


def test_check_origins_rejects_a_venv_installed_from_another_checkout(tmp_path, monkeypatch):
    """The #534 shape, in its most dangerous form: a venv off another worktree.

    This is exactly the stale-editable-install bug -- the packages are in a
    real site-packages directory, so a naive containment check accepts them.  It
    is only safe to accept them when the install was made *from this checkout*.
    """

    project = tmp_path / "checkout"
    project.mkdir()
    other = tmp_path / "other-worktree"
    site_packages = tmp_path / "venv" / "lib" / "site-packages"
    installed = {name: site_packages / name for name in ("pokered_harness", "pyboy")}
    for name, directory in installed.items():
        _make_package(directory.parent, name)
    monkeypatch.setattr(
        origins.importlib.metadata,
        "distribution",
        _distribution_lookup(_fake_install(["pokered-harness"], installed, other)),
    )
    monkeypatch.setattr(
        origins.importlib.metadata,
        "packages_distributions",
        lambda: {
            "pokered_harness": ["pokered-harness"],
            "pyboy": ["pokered-harness"],
        },
    )
    monkeypatch.setattr(
        origins, "_resolve_origin", lambda name: (installed[name] / "__init__.py", "")
    )

    report = check_origins(project)

    assert report["status"] == "FAIL", report
    assert all(item["status"] == "FAIL" for item in report["packages"])


def test_check_origins_rejects_a_competing_pyboy_distribution(tmp_path, monkeypatch):
    """A stock PyBoy alongside the harness must not satisfy the guard."""

    project = tmp_path / "checkout"
    project.mkdir()
    site_packages = tmp_path / "venv" / "lib" / "site-packages"
    stock = site_packages / "pyboy"
    _make_package(site_packages, "pyboy")
    monkeypatch.setattr(
        origins.importlib.metadata,
        "distribution",
        _distribution_lookup(_fake_install(["pokered-harness"], {"pyboy": stock}, project)),
    )
    monkeypatch.setattr(
        origins.importlib.metadata,
        "packages_distributions",
        lambda: {"pokered_harness": ["pokered-harness"], "pyboy": ["PyBoy"]},
    )
    monkeypatch.setattr(origins, "_resolve_origin", lambda name: (stock / "__init__.py", ""))

    report = check_origins(project)

    assert report["status"] == "FAIL", report


def test_check_origins_rejects_a_package_from_another_checkout(tmp_path, monkeypatch):
    """The #534 shape: a sibling worktree shadows the tree under test."""

    other = tmp_path / "other-worktree" / "src"
    package = _make_package(other, "moved_pkg")
    monkeypatch.syspath_prepend(str(other))
    project = tmp_path / "project"
    project.mkdir()

    report = check_origins(project, ("moved_pkg",))

    assert report["status"] == "FAIL"
    finding = report["packages"][0]
    assert finding["origin"] == str(package / "__init__.py")
    assert "importing a different checkout" in finding["detail"]


def test_check_origins_reports_an_unimportable_package(tmp_path):
    report = check_origins(tmp_path, ("pokered_definitely_missing_pkg_xyz",))

    assert report["status"] == "FAIL"
    assert "import failed" in report["packages"][0]["detail"]


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
