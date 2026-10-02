"""Acceptance for the #534 import-origin guard.

The repository is developed across many worktrees that share one virtual
environment.  When the editable install points at a different worktree, every
selected tier silently measures that other tree.  These rows pin both
directions: the guard must accept the tree under test and reject any other.
"""

import json
import os
import re
import runpy
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

    The install source is not the checkout itself: the native lane builds from
    a *staged copy* of ``vendor/pyboy-src`` so no generated C or object files
    can be reused, and ``direct_url.json`` records that staging path.  It is
    still a legitimate install of this checkout because the staging directory
    lives inside this checkout's own ``build/`` tree, which is what this row
    pins.  Location, not a self-reported revision, is the discriminator.
    """

    project = tmp_path / "checkout"
    project.mkdir()
    staged = project / "build" / "pyboy-native-abc" / "pyboy-src"
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

    report = check_origins(project)

    assert report["status"] == "PASS", report


def test_check_origins_rejects_a_foreign_pyboy_distribution(tmp_path, monkeypatch):
    """A pyboy distribution from another checkout must not be admitted.

    The companion to the row above: admitting a separately distributed
    ``pyboy`` is only safe while it is traceable to this checkout.  One built
    from a sibling worktree records that other directory, which is neither this
    checkout nor a staging directory beneath it.
    """

    project = tmp_path / "checkout"
    project.mkdir()
    other = tmp_path / "other-worktree"
    # The other checkout staged its own build inside *its own* build/ tree.
    # Sharing the directory name is not sharing provenance.
    (other / "build" / "pyboy-native-abc" / "pyboy-src").mkdir(parents=True)
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

    report = check_origins(project)

    assert report["status"] == "FAIL", report
    assert report["packages"][1]["package"] == "pyboy"


def test_check_origins_rejects_a_foreign_staging_directory(tmp_path, monkeypatch):
    """A sibling worktree's own ``build/`` tree is not this checkout's.

    The staging rule is scoped to ``project_root``, so a staged path under
    *another* checkout's ``build/`` must not be admitted.  The rule above
    records the sibling worktree root; this one records a staging path, which
    is the shape the native lane actually produces.
    """

    project = tmp_path / "checkout"
    project.mkdir()
    other = tmp_path / "other-worktree"
    # The other checkout staged its own build inside *its own* build/ tree.
    # Sharing the directory name is not sharing provenance.
    (other / "build" / "pyboy-native-abc" / "pyboy-src").mkdir(parents=True)
    site_packages = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    installed = {name: site_packages / name for name in ("pokered_harness", "pyboy")}
    for name, directory in installed.items():
        _make_package(directory.parent, name)
    distributions = {
        **_fake_install(
            ["pokered-harness"], {"pokered_harness": installed["pokered_harness"]}, project
        ),
        **_fake_install(
            ["pyboy"],
            {"pyboy": installed["pyboy"]},
            other / "build" / "pyboy-native-abc" / "pyboy-src",
        ),
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

    report = check_origins(project)

    assert report["status"] == "FAIL", report
    assert report["packages"][1]["package"] == "pyboy"


def test_check_origins_scopes_the_staging_rule_to_this_checkout(tmp_path, monkeypatch):
    """Another checkout's ``build`` directory must not be admitted by name.

    The rule is "inside *this* checkout's ``build/``", a containment test, not
    a name test.  These rows record the foreign ``build`` directory itself, so
    an implementation matching on ``source.name == "build"`` -- which would
    admit any such directory anywhere -- is caught.  No other fixture produces
    that shape, so without this row the scope is unpinned.
    """

    project = tmp_path / "checkout"
    project.mkdir()
    other_build = tmp_path / "other-worktree" / "build"
    other_build.mkdir(parents=True)
    site_packages = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    installed = {name: site_packages / name for name in ("pokered_harness", "pyboy")}
    for name, directory in installed.items():
        _make_package(directory.parent, name)
    distributions = {
        **_fake_install(
            ["pokered-harness"], {"pokered_harness": installed["pokered_harness"]}, project
        ),
        **_fake_install(["pyboy"], {"pyboy": installed["pyboy"]}, other_build),
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

    report = check_origins(project)

    assert report["status"] == "FAIL", report
    assert report["packages"][1]["package"] == "pyboy"


def test_check_origins_rejects_pyboy_reporting_this_checkouts_revision(tmp_path, monkeypatch):
    """A self-reported revision must not admit another checkout's install.

    The vendored PyBoy revision is a compile-time constant: every worktree at
    the same pin carries it, and an installed copy could report any value it
    liked.  Admitting an install on that basis let a ``pyboy`` from a sibling
    worktree pass the guard, which is the #534 defect.  Location is the only
    evidence the guard may use, so this row pins that refusal even when the
    imported module claims the right revision.
    """

    project = tmp_path / "checkout"
    project.mkdir()
    revision = "fd765b1808ac9cb192b42ae971987158ff36ae48"
    marker = project / "vendor" / "pyboy-src" / "POKERED_HARNESS_PYBOY_REVISION"
    marker.parent.mkdir(parents=True)
    marker.write_text(f"{revision}\n", encoding="utf-8")
    other = tmp_path / "other-worktree"
    other.mkdir()
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
    # The stale install reports exactly the revision this checkout pins.
    module = types.ModuleType("pyboy")
    module.__pokered_harness_revision__ = revision
    monkeypatch.setitem(sys.modules, "pyboy", module)

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


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"dir_info": {}, "url": "https://example.invalid/checkout"}, "https scheme"),
        ({"dir_info": {}, "url": "https:///checkout"}, "https scheme, empty netloc"),
        ({"dir_info": {}, "url": "https://localhost/checkout"}, "https scheme, localhost netloc"),
        ({"dir_info": {}, "url": "ssh://localhost/checkout"}, "non-file scheme"),
        ({"dir_info": {}, "url": "file://evil-host/checkout"}, "foreign netloc"),
        ({"dir_info": {}}, "missing url key"),
        ({"dir_info": {}, "url": None}, "null url"),
        ({"dir_info": {}, "url": 17}, "non-string url"),
        ("not json at all", "malformed json"),
    ],
)
def test_installed_from_refuses_an_unattributable_record(payload, reason):
    """Only a plain ``file://`` record of a real local directory attributes.

    A remote or host-qualified URL names a location this process never
    verified, so it must contribute no allowed site-packages root.  These
    branches are load-bearing for failing closed, and ``_fake_install`` only
    ever builds well-formed ``file://`` URLs, so without these rows the scheme
    and netloc guards were never exercised.
    """

    class _Distribution:
        def read_text(self, filename):
            if filename != "direct_url.json":
                return None
            return payload if isinstance(payload, str) else json.dumps(payload)

    assert origins._installed_from is not None  # the helper under test
    original = origins._distribution
    origins._distribution = lambda _name: _Distribution()
    try:
        assert origins._installed_from("pyboy", Path("/anywhere")) is None, reason
    finally:
        origins._distribution = original


def test_installed_from_accepts_a_plain_local_record():
    """The positive case, so the refusals above are not vacuous."""

    source = Path(__file__).resolve().parent.parent

    class _Distribution:
        def read_text(self, filename):
            if filename != "direct_url.json":
                return None
            return json.dumps({"dir_info": {"editable": True}, "url": f"file://{source}"})

    original = origins._distribution
    origins._distribution = lambda _name: _Distribution()
    try:
        assert origins._installed_from("pokered-harness", source) == source
    finally:
        origins._distribution = original


def test_installed_from_resolves_parent_traversal():
    """A recorded source containing ``..`` is normalized before it is judged.

    ``_is_within`` compares paths lexically, so an unresolved
    ``<checkout>/build/../elsewhere`` still sits *textually* beneath
    ``<checkout>/build`` while pointing somewhere else entirely.  Dropping the
    ``resolve()`` in ``_installed_from`` would let that through the staging
    rule, so this row pins the normalization the rule depends on.
    """

    source = Path(__file__).resolve().parent.parent
    escaping = source / "build" / ".." / ".." / "elsewhere"
    assert escaping.resolve() == source.parent / "elsewhere"
    # The lexical form is beneath build/; the resolved form is not.
    assert escaping.is_relative_to(source / "build")

    class _Distribution:
        def read_text(self, filename):
            if filename != "direct_url.json":
                return None
            return json.dumps({"dir_info": {}, "url": f"file://{escaping}"})

    original = origins._distribution
    origins._distribution = lambda _name: _Distribution()
    try:
        recorded = origins._installed_from("pyboy", source)
    finally:
        origins._distribution = original

    assert recorded == source.parent / "elsewhere"
    assert not origins._is_this_checkout(source, recorded)


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


def test_is_this_checkout_refuses_a_symlinked_staging_path(tmp_path):
    """A symlink under ``build/`` must not smuggle in a sibling worktree.

    The native lane's provenance is its staging location, so the staging
    location has to be the real one.  A ``build/`` entry pointing at another
    checkout would otherwise satisfy a lexical containment test while the
    install it describes was built from a different tree entirely.
    """

    project = tmp_path / "checkout"
    (project / "build").mkdir(parents=True)
    sibling = tmp_path / "other-worktree"
    (sibling / "build" / "pyboy-native-x").mkdir(parents=True)

    link = project / "build" / "pyboy-native-link"
    link.symlink_to(sibling / "build", target_is_directory=True)

    assert not origins._is_this_checkout(project, link / "pyboy-native-x")
    assert not origins._is_this_checkout(project, sibling / "build" / "pyboy-native-x")
    assert origins._is_this_checkout(project, project)
    assert origins._is_this_checkout(project, project / "build" / "pyboy-native-abc" / "pyboy-src")


def test_check_origins_reports_an_unimportable_package(tmp_path):
    report = check_origins(tmp_path, ("pokered_definitely_missing_pkg_xyz",))

    assert report["status"] == "FAIL"
    assert "import failed" in report["packages"][0]["detail"]


def test_is_this_checkout_refuses_a_symlinked_staging_root(tmp_path):
    """Resolving ``build/`` is not enough when ``build/`` itself is a symlink.

    ``_is_this_checkout`` resolves both operands before comparing.  When
    ``<checkout>/build`` is itself a symlink into a sibling worktree, resolving
    it yields *that* worktree's build tree, and every source beneath it then
    compares as "inside this checkout" while physically living elsewhere.
    ``_is_within`` is only correct if the containment root is also a real
    subdirectory of the checkout.
    """

    project = tmp_path / "checkout"
    project.mkdir()
    sibling = tmp_path / "other-worktree"
    (sibling / "build" / "pyboy-native-x").mkdir(parents=True)

    (project / "build").symlink_to(sibling / "build", target_is_directory=True)

    assert origins._staging_root(project).resolve() == (sibling / "build").resolve()
    assert not origins._is_within(
        sibling / "build" / "pyboy-native-x",
        origins._staging_root(project),
        strict=True,
    )
    assert not origins._is_this_checkout(project, sibling / "build" / "pyboy-native-x")

    # The native lane creates ``build/`` as a real directory, so the legitimate
    # shape must still be admitted once the symlink is gone.
    (project / "build").unlink()
    (project / "build" / "pyboy-native-x").mkdir(parents=True)
    assert origins._is_this_checkout(project, project / "build" / "pyboy-native-x")


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
    # the module, so the choice does not change the planned set.
    planned_row = f"{this_file}::test_is_within_resolves_before_comparing"
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
