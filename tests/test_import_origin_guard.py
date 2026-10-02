"""Acceptance for the #534 import-origin guard.

The repository is developed across many worktrees that share one virtual
environment.  When the editable install points at a different worktree, every
selected tier silently measures that other tree.  These rows pin both
directions: the guard must accept the tree under test and reject any other.
"""

import contextlib
import importlib
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
from scripts.check_import_origins import (
    _finder_code_file,
    _finder_source,
    _is_installation_finder,
    _is_trusted_stdlib_finder,
    _is_within,
    _site_packages_roots,
    check_origins,
    main,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_package(root: Path, package: str) -> Path:
    """Create a minimal importable package under ``root`` and return its dir."""

    directory = root / package
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text("", encoding="utf-8")
    return directory


@contextlib.contextmanager
def _module_installed(name: str, module: types.ModuleType):
    """Temporarily install ``module`` under ``name`` in ``sys.modules``.

    Used by the rows that drive the real CLI, where pytest's ``monkeypatch``
    fixture is not in scope.
    """

    original = sys.modules.get(name)
    sys.modules[name] = module
    try:
        yield module
    finally:
        if original is None:
            del sys.modules[name]
        else:
            sys.modules[name] = original


@contextlib.contextmanager
def _import_replaced(replacement):
    """Temporarily swap the built-in ``__import__`` for ``replacement``.

    ``_resolve_origin`` imports the package it is auditing, so the only way to
    drive a hostile loader is to replace the importer itself.  ``monkeypatch``
    is not in scope for the rows that drive the real CLI, and patching
    ``builtins.__import__`` for the duration of a test is safe here because
    the guard is single-threaded.
    """

    import builtins

    original = builtins.__import__
    builtins.__import__ = replacement
    try:
        yield
    finally:
        builtins.__import__ = original


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
        (
            {"dir_info": {}, "url": "file:///tmp/checkout%00elsewhere"},
            "percent-encoded NUL makes the recorded path unresolvable",
        ),
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


class _HostileFspath:
    """A path-like whose ``__fspath__`` raises something unanticipated.

    No tuple of expected exception types can enumerate what an object supplied
    by a foreign package or a corrupt install record chooses to raise, so the
    guards must not be written as an enumeration.
    """

    def __init__(self, exc):
        self._exc = exc

    def __fspath__(self):
        raise self._exc


@pytest.mark.parametrize(
    "raised",
    [
        AssertionError("arbitrary fspath failure"),
        KeyError("not a filesystem error at all"),
        ZeroDivisionError("nor this one"),
        UnicodeError("or this"),
    ],
)
def test_fspath_that_raises_anything_is_a_finding_not_a_crash(tmp_path, monkeypatch, raised):
    """A ``__fspath__`` raising an arbitrary exception must not escape.

    Enumerating ``(OSError, TypeError, ValueError, RuntimeError)`` covers the
    ways a *well-behaved* path fails, but ``Path()`` calls ``__fspath__`` on a
    foreign object and that object can raise anything.  An arbitrary raise
    escaped ``check_origins`` as a traceback, so the run died instead of
    reporting the package it was refusing.
    """

    module = types.ModuleType("hostile_ns")
    module.__file__ = None
    module.__path__ = [str(tmp_path / "inside"), _HostileFspath(raised)]
    monkeypatch.setitem(sys.modules, "hostile_ns", module)

    report = check_origins(tmp_path, ("hostile_ns",))

    assert report["status"] == "FAIL"
    assert report["packages"][0]["package"] == "hostile_ns"


@pytest.mark.parametrize("raised", [KeyboardInterrupt(), SystemExit()])
def test_operator_interrupt_is_not_swallowed_as_a_finding(tmp_path, monkeypatch, raised):
    """An interrupt must still stop the run, not become a finding.

    The guards catch ``BaseException`` so no foreign ``__fspath__`` can escape.
    That must not extend to swallowing the operator's own Ctrl-C or a
    deliberate ``sys.exit``: both are re-raised.
    """

    module = types.ModuleType("interrupting_ns")
    module.__file__ = None
    module.__path__ = [str(tmp_path / "inside"), _HostileFspath(raised)]
    monkeypatch.setitem(sys.modules, "interrupting_ns", module)

    with pytest.raises((KeyboardInterrupt, SystemExit)):
        check_origins(tmp_path, ("interrupting_ns",))


@pytest.mark.parametrize("raised", [KeyboardInterrupt(), SystemExit()])
def test_resolve_path_does_not_swallow_an_interrupt(raised):
    """``_resolve_path`` itself must re-raise, not just callers that reach it.

    The namespace row above is not enough: a hostile portion is first passed
    through ``_is_within``, whose own re-raise happens to stop the interrupt
    before ``_resolve_path`` is reached.  That would let the guard swallow
    Ctrl-C for any caller that converts a path directly, so the conversion
    boundary is pinned on its own.
    """

    with pytest.raises((KeyboardInterrupt, SystemExit)):
        origins._resolve_path(_HostileFspath(raised), "pkg")


@pytest.mark.parametrize("empty", ["", b""], ids=["str", "bytes"])
def test_cli_refuses_an_empty_origin(tmp_path, capsys, empty):
    """An empty ``__file__`` proves nothing and must not be credited (#552).

    ``Path("")`` is not malformed: it resolves cleanly to the process CWD,
    which is normally inside the checkout, so the guard used to report PASS
    and name the checkout as the origin.  That is the exact false-PASS class
    #534 exists to prevent -- the origin is unproven, not verified.
    """

    module = types.ModuleType("empty_origin")
    module.__file__ = empty
    with _module_installed("empty_origin", module):
        returncode = main(["--project-root", str(tmp_path), "--package", "empty_origin"])

    assert returncode == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "FAIL"
    assert report["packages"][0]["origin"] is None
    assert "empty" in report["packages"][0]["detail"]


def test_empty_origin_check_does_not_treat_a_falsy_pathlike_as_empty():
    """The empty check must not misread a real path as an empty one.

    A path-like object with ``__len__`` returning 0 is falsy but perfectly
    usable, so a bare ``if not candidate`` would refuse it.  The check is
    scoped to ``str``/``bytes`` for that reason.
    """

    class FalsyButReal:
        def __len__(self):
            return 0

        def __fspath__(self):
            return str(REPO_ROOT / "src" / "pokered_harness" / "__init__.py")

    resolved, error = origins._resolve_path(FalsyButReal(), "pkg")

    assert error == ""
    assert resolved == (REPO_ROOT / "src" / "pokered_harness" / "__init__.py").resolve()


@pytest.mark.parametrize("raised", [KeyboardInterrupt(), SystemExit()])
def test_is_within_does_not_swallow_an_interrupt(raised):
    """``_is_within`` must re-raise rather than read a refused comparison as
    "not within" and let the caller carry on.

    Pinned directly: the namespace rows reach the interrupt through
    ``_is_within`` first, but they still fail later in ``_resolve_path``, so
    they do not distinguish the two sites.
    """

    with pytest.raises((KeyboardInterrupt, SystemExit)):
        origins._is_within(_HostileFspath(raised), REPO_ROOT)


@pytest.mark.parametrize("raised", [KeyboardInterrupt(), SystemExit()])
def test_is_this_checkout_does_not_swallow_an_interrupt(raised):
    """``_is_this_checkout`` must re-raise for the same reason."""

    with pytest.raises((KeyboardInterrupt, SystemExit)):
        origins._is_this_checkout(REPO_ROOT, _HostileFspath(raised))


def test_describe_never_raises_on_a_hostile_value():
    """A detail line built from untrusted data must itself be safe to build."""

    class Unprintable:
        def __str__(self):
            raise AssertionError("str blew up")

        def __repr__(self):
            raise AssertionError("repr blew up")

    rendered = origins._describe(Unprintable())

    assert "unprintable" in rendered
    assert "Unprintable" in rendered


def test_cli_survives_a_portion_whose_str_also_raises(tmp_path, capsys):
    """Formatting the finding must not resurrect the traceback it replaced.

    A portion that is hostile in both ``__fspath__`` and ``__str__`` reached
    ``str(item)`` while the detail was assembled, so the refusal crashed
    exactly as the original bug did.
    """

    class HostileBoth:
        def __fspath__(self):
            raise AssertionError("fspath blew up")

        def __str__(self):
            raise AssertionError("str blew up")

    module = types.ModuleType("hostile_str_ns")
    module.__file__ = None
    module.__path__ = [str(tmp_path / "inside"), HostileBoth()]
    with _module_installed("hostile_str_ns", module):
        returncode = main(["--project-root", str(tmp_path), "--package", "hostile_str_ns"])

    assert returncode == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "FAIL"


def test_cli_survives_an_exception_whose_str_raises(tmp_path, capsys):
    """A caught exception from a hostile ``__fspath__`` is itself untrusted."""

    class ExplodingExc(BaseException):
        def __str__(self):
            raise AssertionError("exception formatting escaped")

    class Hostile:
        def __fspath__(self):
            raise ExplodingExc()

    module = types.ModuleType("hostile_exc_ns")
    module.__file__ = None
    module.__path__ = [str(tmp_path / "inside"), Hostile()]
    with _module_installed("hostile_exc_ns", module):
        returncode = main(["--project-root", str(tmp_path), "--package", "hostile_exc_ns"])

    assert returncode == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "FAIL"


def test_cli_survives_an_import_error_whose_str_raises(tmp_path, capsys):
    """A loader that fails with an unprintable exception is still a finding.

    ``_describe`` hardened the path-conversion and namespace reporting sites,
    but the import-failure detail was assembled with a bare f-string.  A
    package loader is third-party code, so it can raise an exception whose own
    ``__str__`` raises, and the refusal then died with a traceback and no
    JSON at all -- the same class of escape the rest of the module fixes.
    """

    class ExplodingImportError(ImportError):
        def __str__(self):
            raise AssertionError("import error formatting escaped")

    import builtins

    real_import = builtins.__import__

    def _failing_import(name, *args, **kwargs):
        if name == "unprintable_import":
            raise ExplodingImportError("nope")
        return real_import(name, *args, **kwargs)

    with (
        _module_installed("unprintable_import", types.ModuleType("unprintable_import")),
        _import_replaced(_failing_import),
    ):
        returncode = main(["--project-root", str(tmp_path), "--package", "unprintable_import"])

    assert returncode == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "FAIL"
    assert report["packages"][0]["origin"] is None
    assert "import failed" in report["packages"][0]["detail"]


@pytest.mark.parametrize("raised", [KeyboardInterrupt(), SystemExit()])
def test_resolve_origin_does_not_swallow_an_interrupt(raised):
    """An operator interrupt during an import must stop the run.

    ``_resolve_origin`` catches ``BaseException`` because a loader can raise
    anything, and that catch must not become a way for ``Ctrl-C`` to be
    recorded as a failed import.
    """

    def _interrupting_import(name, *args, **kwargs):
        raise raised

    with _import_replaced(_interrupting_import), pytest.raises((KeyboardInterrupt, SystemExit)):
        origins._resolve_origin("interruptible_import")


def test_installed_from_refuses_a_record_the_interpreter_will_not_parse():
    """A valid-JSON integer past CPython's digit cap is still a refused record.

    ``json.loads`` raises ``ValueError`` rather than ``JSONDecodeError`` once
    the integer string conversion limit is exceeded, so catching only the
    subclass let a record on disk abort the whole run.
    """

    oversized = '{"url": ' + ("9" * 5000) + "}"

    class _Distribution:
        def read_text(self, filename):
            return oversized if filename == "direct_url.json" else None

    original = origins._distribution
    origins._distribution = lambda _name: _Distribution()
    try:
        assert origins._installed_from("pyboy", Path("/anywhere")) is None
    finally:
        origins._distribution = original


def test_install_location_reader_that_raises_contributes_no_root(tmp_path):
    """``locate_file`` is third-party code and can raise anything.

    An install whose location cannot be read contributes no allowed root,
    which is the fail-closed direction: the run then refuses an origin it
    cannot attribute rather than trusting an unverified root.
    """

    class _Distribution:
        def read_text(self, filename):
            if filename != "direct_url.json":
                return None
            return json.dumps({"url": tmp_path.as_uri()})

        def locate_file(self, _package):
            raise KeyError("locate")

    original_pd = origins.importlib.metadata.packages_distributions
    original_d = origins._distribution
    origins.importlib.metadata.packages_distributions = lambda: {"widget": ["widget"]}
    origins._distribution = lambda _name: _Distribution()
    try:
        assert origins._allowed_roots(tmp_path, ("widget",)) == [tmp_path]
    finally:
        origins.importlib.metadata.packages_distributions = original_pd
        origins._distribution = original_d


def test_cli_reports_a_hostile_fspath_as_a_failure_without_a_traceback(tmp_path, capsys):
    """The real CLI must print parseable JSON and exit non-zero, not raise."""

    module = types.ModuleType("hostile_cli_ns")
    module.__file__ = None
    module.__path__ = [str(tmp_path / "inside"), _HostileFspath(AssertionError("boom"))]
    with _module_installed("hostile_cli_ns", module):
        returncode = main(["--project-root", str(tmp_path), "--package", "hostile_cli_ns"])

    assert returncode == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "FAIL"


@pytest.mark.parametrize("junk", [None, 7, 42, object()])
def test_namespace_portion_that_is_not_a_path_is_a_finding_not_a_crash(tmp_path, monkeypatch, junk):
    """A ``__path__`` entry that is not a path must be reported, not raised.

    The unresolvable-path rows pin ``ValueError`` from ``Path.resolve``.  A
    value that is not a path at all fails earlier and differently:
    ``Path(...)`` raises ``TypeError``, which no ``except (OSError,
    ValueError, RuntimeError)`` clause catches.  That escaped ``check_origins``
    as a traceback, the same INTERNALERROR the NUL rows were added to stop.

    The portion cannot be shown to be inside any allowed root, and "inside this
    checkout" is the claim being disproved.  It is reported on the *unusable*
    channel rather than the foreign one because #551 split those apart: a
    portion that exists and resolves elsewhere is a different finding from one
    that never resolved at all.  Either way the caller fails closed.
    """

    module = types.ModuleType("junk_ns")
    module.__file__ = None
    module.__path__ = [str(tmp_path / "inside"), junk]
    monkeypatch.setitem(sys.modules, "junk_ns", module)

    report = check_origins(tmp_path, ("junk_ns",))

    assert report["status"] == "FAIL"
    assert report["packages"][0]["package"] == "junk_ns"
    assert "path portion is not usable" in report["packages"][0]["detail"]


@pytest.mark.parametrize("junk", [7, 42, 3.5, object()])
def test_origin_that_is_not_a_path_is_a_finding_not_a_crash(tmp_path, monkeypatch, junk):
    """A ``__file__`` that is not a path must be a finding, not a traceback.

    ``_resolve_path`` owns the only coercion of untrusted data to a ``Path``,
    so a wrong-typed ``__file__`` is reported on the same channel as a
    wrong-shaped one.  ``None`` is deliberately excluded: it is the documented
    namespace-package signal and takes the ``__path__`` branch instead.
    """

    module = types.ModuleType("junk_file")
    module.__file__ = junk
    monkeypatch.setitem(sys.modules, "junk_file", module)

    report = check_origins(tmp_path, ("junk_file",))

    assert report["status"] == "FAIL"
    assert report["packages"][0]["detail"].startswith("origin is not a usable path")


def test_install_location_that_is_not_a_path_is_dropped_not_raised(tmp_path, monkeypatch):
    """``locate_file`` returning ``None`` must drop the root, not raise.

    Dropping is the fail-closed direction: the run then refuses an origin it
    cannot attribute, rather than admitting one on an unverified root.
    """

    class _Distribution:
        def read_text(self, filename):
            if filename != "direct_url.json":
                return None
            return json.dumps({"url": tmp_path.as_uri()})

        def locate_file(self, _package):
            return None

    monkeypatch.setattr(
        origins.importlib.metadata,
        "packages_distributions",
        lambda: {"widget": ["widget"]},
    )
    monkeypatch.setattr(origins, "_distribution", lambda _name: _Distribution())

    assert origins._allowed_roots(tmp_path, ("widget",)) == [tmp_path]


@pytest.mark.parametrize("payload", ["[]", '"a string"', "42", "null", "true"])
def test_installed_from_refuses_a_wrong_shaped_record(payload):
    """Valid JSON that is not an object cannot name a source.

    pip writes an object here, but the record is on-disk data and must be
    total: ``payload.get`` on a list raised ``AttributeError`` out of the
    guard.  Unattributable is the correct answer, exactly as for a missing
    record.
    """

    class _Distribution:
        def read_text(self, filename):
            return payload if filename == "direct_url.json" else None

    original = origins._distribution
    origins._distribution = lambda _name: _Distribution()
    try:
        assert origins._installed_from("pyboy", Path("/anywhere")) is None
    finally:
        origins._distribution = original


def test_is_this_checkout_refuses_an_unresolvable_source(monkeypatch):
    """A recorded source that cannot be resolved must not raise out of the guard.

    ``_installed_from`` is guarded, so a malformed ``direct_url.json`` record
    already resolves to ``None`` and never reaches this function.  But
    ``_is_this_checkout`` also resolves ``project_root`` and ``source``, and a
    path can be malformed anywhere downstream of the metadata reader.  When it
    raised, the traceback escaped ``check_origins`` as an ``INTERNALERROR``
    rather than becoming a finding.

    Refusing is the fail-closed direction: "this install came from this
    checkout" is the claim being proved, and an unresolvable path cannot
    prove it.
    """

    source = Path("/tmp/outside\x00checkout")

    assert origins._is_this_checkout(REPO_ROOT, source) is False


def test_is_this_checkout_refuses_an_unresolvable_project_root():
    """The checkout side is equally untrusted; refusing is still fail-closed."""

    assert origins._is_this_checkout(Path("/tmp/root\x00here"), Path("/tmp/x")) is False


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


def test_namespace_package_refuses_a_foreign_path_portion(tmp_path, monkeypatch):
    """A namespace portion outside the checkout must fail even when the first
    portion is inside it.

    ``sys.path`` ordering means an in-checkout portion listed first does not
    make the package trusted: a subpackage that is missing locally still
    imports from the foreign portion.  Crediting ``__path__[0]`` alone let a
    genuinely foreign import report PASS.
    """

    root = tmp_path / "root"
    outside = tmp_path / "outside"
    (root / "split_pkg").mkdir(parents=True)
    # No ``__init__.py`` on either side: both are namespace portions.
    foreign = outside / "split_pkg" / "sub"
    foreign.mkdir(parents=True)
    (foreign / "foreign_mod.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(outside))
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "split_pkg" or name.startswith("split_pkg."):
            del sys.modules[name]

    package = importlib.import_module("split_pkg")
    assert getattr(package, "__file__", None) is None, "expected a namespace package"
    assert str(foreign) in [str(Path(item).resolve() / "sub") for item in package.__path__], (
        "the foreign portion must actually be reachable"
    )

    report = check_origins(root, ("split_pkg",))

    assert report["status"] == "FAIL", report
    assert "also resolves outside this checkout" in report["packages"][0]["detail"]


def test_namespace_package_inside_the_checkout_still_passes(tmp_path, monkeypatch):
    """The refusal above must not reject an ordinary single-root namespace."""

    package_dir = tmp_path / "solo_ns"
    package_dir.mkdir()
    (package_dir / "inner").mkdir()
    (package_dir / "inner" / "mod.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in list(sys.modules):
        if name == "solo_ns" or name.startswith("solo_ns."):
            del sys.modules[name]

    report = check_origins(tmp_path, ("solo_ns",))

    assert report["status"] == "PASS", report


def test_unusable_namespace_portion_is_a_finding_not_a_crash(tmp_path, monkeypatch):
    """An unusable ``__path__`` portion must be reported, not raised.

    ``test_unusable_origin_is_a_finding_not_a_crash`` pins the ``__file__``
    path only.  A namespace package never reaches it, so the same
    ``Path.resolve`` hazard was still reachable through ``__path__``: the
    namespace rows then crashed with ``ValueError: embedded null character``
    out of ``check_origins`` instead of producing a finding.

    The finding names the offending portion on the *unusable* channel, since
    #551 reports a portion that never resolved separately from one that
    resolved somewhere foreign.  What matters is that it is a finding naming
    the package, at the verdict level, with no exception escaping.
    """

    module = types.ModuleType("bad_ns")
    module.__file__ = None
    module.__path__ = [str(tmp_path / "inside"), "/tmp/bad\x00port/ns"]
    monkeypatch.setitem(sys.modules, "bad_ns", module)

    report = check_origins(tmp_path, ("bad_ns",))

    assert report["status"] == "FAIL"
    assert report["packages"][0]["package"] == "bad_ns"
    assert "path portion is not usable" in report["packages"][0]["detail"]


def test_unusable_origin_is_a_finding_not_a_crash(monkeypatch):
    """A path the interpreter cannot resolve must be reported, not raised.

    ``Path.resolve`` raises ``ValueError`` on an embedded NUL, and the reported
    ``__file__`` is data the interpreter controls.  Letting that escape made a
    bare ``pytest`` run die with an ``INTERNALERROR`` traceback instead of the
    explicit refusal that names the package.  Observed before this row:
    ``ValueError: embedded null byte`` escaped ``check_origins`` entirely.
    """

    module = types.ModuleType("pokered_harness")
    module.__file__ = "/tmp/bad\x00name/__init__.py"
    monkeypatch.setitem(sys.modules, "pokered_harness", module)

    report = check_origins(REPO_ROOT, ("pokered_harness",))

    assert report["status"] == "FAIL"
    assert report["packages"][0]["origin"] is None
    assert "not a usable path" in report["packages"][0]["detail"]


def test_cli_reports_an_unusable_origin_as_a_failure(tmp_path, monkeypatch, capsys):
    """The CLI must exit 1 with a verdict, not crash, on an unusable origin."""

    module = types.ModuleType("pokered_harness")
    module.__file__ = "/tmp/bad\x00name/__init__.py"
    monkeypatch.setitem(sys.modules, "pokered_harness", module)

    returncode = main(["--project-root", str(tmp_path), "--package", "pokered_harness"])

    payload = json.loads(capsys.readouterr().out)
    assert returncode == 1
    assert payload["status"] == "FAIL"
    assert payload["packages"][0]["origin"] is None


def test_package_flag_still_admits_a_real_install(tmp_path, monkeypatch):
    """An explicit ``--package`` run must consult that package's real install.

    ``--package`` narrows *which* origins are reported, not which locations are
    legitimate.  Restricting the allowed roots to the default packages made a
    narrowed check refuse the release lane, where both packages are installed
    outside the checkout.
    """

    checkout = tmp_path / "checkout"
    site = tmp_path / "venv" / "site-packages"
    package_dir = site / "widget"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")

    class _Distribution:
        def read_text(self, name):
            if name == "direct_url.json":
                return json.dumps({"dir_info": {}, "url": f"file://{checkout}"})
            return None

        def locate_file(self, name):
            return site / name

    monkeypatch.setattr(
        origins.importlib.metadata,
        "packages_distributions",
        lambda: {"widget": ["widget"]},
    )
    monkeypatch.setattr(
        origins.importlib.metadata,
        "distribution",
        lambda name: _Distribution(),
    )
    monkeypatch.setattr(
        origins,
        "_resolve_origin",
        lambda package: (package_dir / "__init__.py", ""),
    )

    report = origins.check_origins(checkout, ("widget",))

    assert report["status"] == "PASS", report


def test_package_flag_replaces_the_defaults_and_says_so(tmp_path, capsys):
    """``--package`` is a replacement, and must report only what it checked."""

    module = types.ModuleType("widget")
    module.__file__ = str(tmp_path / "widget" / "__init__.py")
    returncode = main(
        ["--project-root", str(tmp_path), "--package", "widget", "--package", "gadget"]
    )

    payload = json.loads(capsys.readouterr().out)
    assert returncode == 1
    assert [item["package"] for item in payload["packages"]] == ["widget", "gadget"]


def test_regular_package_refuses_a_foreign_path_portion(tmp_path, monkeypatch):
    """A regular package with a foreign ``__path__`` entry must FAIL.

    A checkout-local ``__init__.py`` does not make a package trusted.  Python
    lets a regular package carry more than one ``__path__`` entry --
    ``pkgutil.extend_path``, an explicit ``__path__`` extension, or a
    ``.pth``-installed entry all do it -- and the interpreter imports
    submodules from every one of them.  So a submodule missing locally still
    imports from the foreign portion while the top-level origin looks correct.

    That is the exact false PASS #534 exists to prevent, and the earlier guard
    reported PASS here because it inspected ``__path__`` only for namespace
    packages.
    """

    root = tmp_path / "root"
    outside = tmp_path / "outside"
    package_dir = root / "split_reg"
    package_dir.mkdir(parents=True)
    # A REGULAR package: it has __init__.py, so __file__ is not None.
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    foreign = outside / "split_reg"
    foreign.mkdir(parents=True)
    (foreign / "leaked.py").write_text('ORIGIN = "foreign"', encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "split_reg" or name.startswith("split_reg."):
            del sys.modules[name]

    package = importlib.import_module("split_reg")
    assert getattr(package, "__file__", None) is not None, "expected a regular package"
    package.__path__.append(str(foreign))

    report = check_origins(root, ("split_reg",))

    assert report["status"] == "FAIL", report
    assert "also resolves outside this checkout" in report["packages"][0]["detail"]
    assert str(foreign.resolve()) in report["packages"][0]["detail"]

    # The foreign portion really is importable, so this row is not vacuous.
    leaked = importlib.import_module("split_reg.leaked")
    assert leaked.ORIGIN == "foreign"


def test_unusable_path_portion_is_a_finding_not_a_crash(tmp_path, monkeypatch):
    """A malformed ``__path__`` entry must become a FAIL, not raise.

    A package's own origin goes through ``_resolve_path``, which reports an
    unusable path as a finding.  A malformed *portion* has to fail the same
    way: a path an interpreter reports is data, and one that cannot be resolved
    must not escape the guard as a traceback.
    """

    root = tmp_path / "root"
    (root / "ok_ns").mkdir(parents=True)
    good = root / "ok_ns" / "portion"
    good.mkdir()
    module = types.ModuleType("broken_ns")
    module.__file__ = None
    module.__path__ = [str(good), "/bad\x00origin"]
    monkeypatch.setitem(sys.modules, "broken_ns", module)

    report = check_origins(root, ("broken_ns",))

    assert report["status"] == "FAIL", report
    assert "not usable" in report["packages"][0]["detail"]


def test_meta_path_finder_cannot_smuggle_a_foreign_submodule(tmp_path, monkeypatch):
    """A ``sys.meta_path`` finder must not be able to defeat the guard.

    Location checks cannot see a finder.  ``PathFinder`` only ever searches a
    parent's ``__path__``, but a finder earlier on ``sys.meta_path`` may
    return a spec for a submodule from any location at all, and
    ``importlib._bootstrap`` consults those finders *before* ``PathFinder``
    does.  So a package whose origin and every ``__path__`` portion are local
    can still have its submodules imported from another tree.

    This is the false PASS the guard exists to prevent, reached by a route the
    ``__path__`` checks cannot observe.
    """

    root = tmp_path / "root"
    outside = tmp_path / "outside"
    package_dir = root / "smuggle_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    foreign = outside / "smuggle_pkg"
    foreign.mkdir(parents=True)
    (foreign / "leaked.py").write_text('ORIGIN = "foreign"', encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "smuggle_pkg" or name.startswith("smuggle_pkg."):
            del sys.modules[name]

    foreign_file = str((foreign / "leaked.py").resolve())

    class SmugglingFinder:
        """Route one submodule to a foreign file, ignoring every other name."""

        def find_spec(self, name, path=None, target=None):
            # The ``target`` parameter is part of the finder protocol and must
            # not be shadowed by a local variable: an earlier draft of this
            # probe reused the name for the foreign path, which silently made
            # the finder return None and the row pass vacuously.
            if name == "smuggle_pkg.leaked":
                return importlib.util.spec_from_file_location(name, foreign_file)
            return None

    monkeypatch.setattr(sys, "meta_path", [SmugglingFinder(), *sys.meta_path])

    report = check_origins(root, ("smuggle_pkg",))

    assert report["status"] == "FAIL", report
    detail = report["packages"][0]["detail"]
    assert "meta_path" in detail, detail
    assert "SmugglingFinder" in detail, detail

    # The escape really works, so the row is not asserting a hypothetical.
    leaked = importlib.import_module("smuggle_pkg.leaked")
    assert leaked.ORIGIN == "foreign"
    assert str(foreign.resolve()) in leaked.__file__


def test_meta_path_finder_outside_site_packages_is_refused(tmp_path, monkeypatch):
    """The refusal is about *where* the finder lives, not its name.

    A finder defined outside the interpreter's own site-packages is an
    environment-installed hook: an ad-hoc ``.pth`` file, a stale editable
    install from another checkout, or a sitecustomize injected into the
    environment.  None of those belong to the checkout under test, so none may
    be trusted to place a checked package's submodules.
    """

    root = tmp_path / "root"
    package_dir = root / "hooked_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    finder_file = outside / "custom_finder.py"
    finder_file.write_text(
        "class ForeignFinder:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        return None\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setitem(sys.modules, "custom_finder", None)
    for name in list(sys.modules):
        if name == "hooked_pkg" or name.startswith("hooked_pkg."):
            del sys.modules[name]

    module = types.ModuleType("custom_finder")
    module.__file__ = str(finder_file)
    foreign_finder = type(
        "ForeignFinder",
        (),
        {"find_spec": lambda self, name, path=None, target=None: None},
    )
    foreign_finder.__module__ = "custom_finder"
    monkeypatch.setitem(sys.modules, "custom_finder", module)
    monkeypatch.setattr(sys, "meta_path", [foreign_finder, *sys.meta_path])

    report = check_origins(root, ("hooked_pkg",))

    assert report["status"] == "FAIL", report
    detail = report["packages"][0]["detail"]
    assert "meta_path" in detail, detail
    # The detail reports where the finder's code really came from, which for a
    # test-defined class is this test file -- not the ``__file__`` it claims.
    assert "ForeignFinder" in detail, detail
    assert str(finder_file.resolve()) not in detail, (
        "the refusal must not repeat the finder's own unverified __file__ claim"
    )


def test_standard_and_installation_finders_are_still_trusted():
    """The release lane depends on custom finders and must keep working.

    ``pip install -e ".[dev]"`` installs an ``__editable__..._finder`` onto
    ``sys.meta_path``, and a virtual environment installs ``_virtualenv._Finder``.
    Refusing every non-stdlib finder would refuse the supported release lane,
    so finders whose defining module lives in the interpreter's own
    site-packages remain trusted.  This row is the control that keeps the
    refusal from being simply "reject anything custom".
    """

    report = check_origins(REPO_ROOT)

    assert report["status"] == "PASS", report
    custom = [entry for entry in sys.meta_path if not _is_trusted_stdlib_finder(entry)]
    assert custom, "expected the editable install to install a custom finder"
    for entry in custom:
        assert _is_installation_finder(entry), (
            f"{entry!r} should be recognised as an installation finder, "
            "otherwise the release lane is refused"
        )


def test_a_finder_cannot_trust_itself_by_claiming_a_site_packages_module(tmp_path, monkeypatch):
    """``module.__file__`` is mutable, so it cannot be the trust evidence.

    Trusting a finder because the module object it names carries a
    site-packages ``__file__`` is trusting a claim the finder itself can make.
    A module registered under a trusted-looking name, whose ``__file__`` points
    at a site-packages path that was never written, would otherwise pass and
    then serve a foreign submodule -- reintroducing exactly the blocker this
    guard change exists to close.

    Requiring the resolved file to actually exist keeps the decision on the
    filesystem rather than on self-report, matching the "provenance is decided
    by location only" rule the rest of the module already follows.
    """

    root = tmp_path / "root"
    package_dir = root / "spoof_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    outside = tmp_path / "outside"
    foreign = outside / "spoof_pkg"
    foreign.mkdir(parents=True)
    (foreign / "leaked.py").write_text('ORIGIN = "foreign"', encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "spoof_pkg" or name.startswith("spoof_pkg."):
            del sys.modules[name]

    site_root = _site_packages_roots()[0]
    assert site_root.is_dir(), "the live interpreter must have a site-packages directory"
    claimed = site_root / "spoofed_trusted_finder_module.py"
    assert not claimed.exists(), "the spoofed path must not exist for this row to mean anything"

    fake_module = types.ModuleType("spoofed_trusted_finder_module")
    fake_module.__file__ = str(claimed)
    monkeypatch.setitem(sys.modules, "spoofed_trusted_finder_module", fake_module)

    foreign_file = str((foreign / "leaked.py").resolve())

    class SpoofingFinder:
        # A *class* is what setuptools actually installs, and CPython calls
        # its find_spec unbound, so it must be a classmethod.  An ordinary
        # method here would receive the module name as ``self`` and never
        # match, making the row pass for the wrong reason.
        @classmethod
        def find_spec(cls, name, path=None, target=None):
            if name == "spoof_pkg.leaked":
                return importlib.util.spec_from_file_location(name, foreign_file)
            return None

    SpoofingFinder.__module__ = "spoofed_trusted_finder_module"
    # Inserted by hand rather than via monkeypatch.setattr so the teardown
    # below can remove it while the path and sys.modules setup survive.
    sys.meta_path.insert(0, SpoofingFinder)

    try:
        assert not _is_installation_finder(SpoofingFinder), (
            "a finder whose claimed site-packages file does not exist must not be trusted"
        )
        report = check_origins(root, ("spoof_pkg",))

        assert report["status"] == "FAIL", report
        assert "meta_path" in report["packages"][0]["detail"]

        # The escape is real, so the row is not asserting a hypothetical.
        leaked = importlib.import_module("spoof_pkg.leaked")
        assert leaked.ORIGIN == "foreign"
    finally:
        sys.meta_path.remove(SpoofingFinder)


def test_a_hostile_finder_metaclass_cannot_abort_the_guard(tmp_path, monkeypatch):
    """Inspecting a hostile finder must fail closed, not raise.

    A metaclass may raise from ``__module__``, and a module object may raise
    from ``__file__``.  The guard is a preflight: an exception escaping it
    turns an explicit refusal into a traceback, which is the crash mode
    ``_resolve_path`` already had to be hardened against for package origins.
    A finder that cannot be inspected is simply not trusted.
    """

    root = tmp_path / "root"
    package_dir = root / "hostile_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "hostile_pkg" or name.startswith("hostile_pkg."):
            del sys.modules[name]

    class HostileMeta(type):
        @property
        def __module__(cls):
            raise RuntimeError("metaclass refuses to name itself")

    hostile = HostileMeta("HostileFinder", (), {"find_spec": lambda self, *a: None})
    monkeypatch.setattr(sys, "meta_path", [hostile, *sys.meta_path])

    report = check_origins(root, ("hostile_pkg",))

    assert report["status"] == "FAIL", report
    assert "meta_path" in report["packages"][0]["detail"], report


def test_a_finder_cannot_borrow_a_real_installed_modules_file(tmp_path, monkeypatch):
    """A genuine site-packages ``__file__`` must not authenticate a stranger.

    Requiring the claimed file merely to *exist* was the second attempt at this
    check, and review defeated it: a finder can claim the module name of a real
    installed package, whose ``__file__`` genuinely is inside site-packages and
    genuinely does exist.  Existence proves the borrowed file is real, not that
    it defines this finder.

    The only unforgeable statement is where the executing ``find_spec`` code was
    compiled from, so that is what trust is decided on.
    """

    root = tmp_path / "root"
    package_dir = root / "borrow_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    outside = tmp_path / "outside"
    foreign = outside / "borrow_pkg"
    foreign.mkdir(parents=True)
    (foreign / "leaked.py").write_text('ORIGIN = "foreign"', encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "borrow_pkg" or name.startswith("borrow_pkg."):
            del sys.modules[name]

    site_root = _site_packages_roots()[0]
    assert site_root.is_dir()
    # A real, existing *file* inside site-packages -- exactly what the previous
    # existence check was fooled by.
    #
    # The candidate must be a regular file, not merely a path that exists.
    # Preferring `pyvenv.cfg` or a `*.dist-info` entry picks a *directory* in a
    # typical venv, and a directory claim is rejected by the `is_file()` check
    # for a reason unrelated to provenance.  The row would then pass no matter
    # how trust was decided, so it would stop pinning the claim-vs-code
    # distinction it exists to test.
    genuine = sorted(path for path in site_root.glob("*.py") if path.is_file())
    if not genuine:
        genuine = sorted(
            path for path in site_root.rglob("*.py") if path.is_file() and path.stat().st_size > 0
        )
    assert genuine, "this row needs a real existing .py file inside site-packages to borrow"
    borrowed = genuine[0]
    assert Path(borrowed).is_file(), "the borrowed path must be a regular file"

    bystander = types.ModuleType("innocent_bystander_module")
    bystander.__file__ = str(borrowed)
    monkeypatch.setitem(sys.modules, "innocent_bystander_module", bystander)

    foreign_file = str((foreign / "leaked.py").resolve())

    class BorrowingFinder:
        @classmethod
        def find_spec(cls, name, path=None, target=None):
            if name == "borrow_pkg.leaked":
                return importlib.util.spec_from_file_location(name, foreign_file)
            return None

    BorrowingFinder.__module__ = "innocent_bystander_module"
    sys.meta_path.insert(0, BorrowingFinder)
    try:
        # The borrowed claim looks exactly like a real install: an existing
        # file inside site-packages.  Assert that directly, so the row cannot
        # pass merely because some *other* part of the check happened to
        # reject this finder.
        claimed = _finder_source(BorrowingFinder)
        assert claimed is not None, "the borrowed claim must be discoverable"
        assert Path(claimed).exists(), "the borrowed file must really exist"
        assert any(
            _is_within(Path(claimed), site_root, strict=False)
            for site_root in _site_packages_roots()
        ), "the borrowed file must really sit inside site-packages"
        assert str(Path(claimed).resolve()) == str(Path(borrowed).resolve())

        # What actually differs is where the finder's own code came from.
        assert _finder_code_file(BorrowingFinder) != Path(claimed).resolve(), (
            "the executing find_spec code must not appear to come from the borrowed file"
        )

        assert not _is_installation_finder(BorrowingFinder), (
            "borrowing a real installed module's file must not confer trust"
        )
        report = check_origins(root, ("borrow_pkg",))

        assert report["status"] == "FAIL", report
        assert "meta_path" in report["packages"][0]["detail"], report

        leaked = importlib.import_module("borrow_pkg.leaked")
        assert leaked.ORIGIN == "foreign"
    finally:
        sys.meta_path.remove(BorrowingFinder)


def test_a_finder_compiled_without_a_source_file_cannot_borrow_a_claim(tmp_path, monkeypatch):
    """No ``co_filename`` means no provenance, and a claim must not supply it.

    Every earlier row defeats a finder that at least has a real code object, so
    trust can be decided by comparing that object's file against site-packages.
    A finder built by ``compile(src, "", "exec")`` or otherwise defined without
    a source file has no such file to compare, and the honest answer is "not
    trusted".

    The tempting shortcut is to fall back to the finder's self-reported
    ``__file__`` in exactly that case.  That reintroduces the whole bug this
    guard was rewritten to remove: an attacker with no provenance at all simply
    borrows a real installed module's name and inherits its trust.  This row is
    the mutation guard for that fallback -- it fails if the no-provenance branch
    ever starts consulting ``_finder_source``.
    """

    root = tmp_path / "root"
    package_dir = root / "noprovenance_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    outside = tmp_path / "outside"
    foreign = outside / "noprovenance_pkg"
    foreign.mkdir(parents=True)
    (foreign / "leaked.py").write_text('ORIGIN = "foreign"', encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "noprovenance_pkg" or name.startswith("noprovenance_pkg."):
            del sys.modules[name]

    site_root = _site_packages_roots()[0]
    assert site_root.is_dir()
    genuine = sorted(path for path in site_root.glob("*.py") if path.is_file())
    if not genuine:
        genuine = sorted(path for path in site_root.rglob("*.py") if path.is_file())
    assert genuine, "this row needs a real installed module to borrow the name of"
    borrowed = genuine[0]

    bystander = types.ModuleType("innocent_noprovenance_module")
    bystander.__file__ = str(borrowed)
    monkeypatch.setitem(sys.modules, "innocent_noprovenance_module", bystander)

    foreign_file = str((foreign / "leaked.py").resolve())
    source = (
        "class NoProvenanceFinder:\n"
        "    @classmethod\n"
        "    def find_spec(cls, name, path=None, target=None):\n"
        "        if name == 'noprovenance_pkg.leaked':\n"
        "            return importlib.util.spec_from_file_location(name, "
        f"{foreign_file!r})\n"
        "        return None\n"
    )
    # An empty filename is what compile() records for code with no source file.
    namespace = {"__name__": "innocent_noprovenance_module", "importlib": importlib}
    exec(compile(source, "", "exec"), namespace)  # noqa: S102 - the attack itself
    forged = namespace["NoProvenanceFinder"]
    forged.__module__ = "innocent_noprovenance_module"

    sys.meta_path.insert(0, forged)
    try:
        # The premise: there is genuinely no code file to judge, while the
        # claim points at a real installed module inside site-packages.
        assert _finder_code_file(forged) is None, (
            "this row needs a finder whose find_spec has no source file"
        )
        claimed = _finder_source(forged)
        assert claimed is not None, "the borrowed claim must be discoverable"
        assert Path(claimed).is_file(), "the borrowed path must be a real file"
        assert any(
            _is_within(Path(claimed), root_, strict=False) for root_ in _site_packages_roots()
        ), "the borrowed file must really sit inside site-packages"

        assert not _is_installation_finder(forged), (
            "a finder with no code provenance must not be trusted on its claim"
        )
        report = check_origins(root, ("noprovenance_pkg",))

        assert report["status"] == "FAIL", report
        assert "meta_path" in report["packages"][0]["detail"], report

        leaked = importlib.import_module("noprovenance_pkg.leaked")
        assert leaked.ORIGIN == "foreign"
    finally:
        sys.meta_path.remove(forged)


def test_a_finder_compiled_from_a_vanished_file_inside_site_packages_is_refused(
    tmp_path, monkeypatch
):
    """Containment alone is not provenance; the file must still be there.

    Trust is decided by resolving the finder's ``co_filename`` and asking
    whether it lands inside site-packages.  Resolution of a missing path
    succeeds, so containment alone would also accept a filename that merely
    *reads* as though it were installed -- for example a file the interpreter
    compiled from and has since deleted, or a name a site-packages writer
    never actually produced.

    This row is the mutation guard for the ``is_file()`` check.  It compiles a
    finder from a path inside site-packages that does not exist, asserts the
    path really is contained, and then requires the finder to be refused
    anyway.  Removing the existence check leaves the containment assertion
    satisfied and the trust decision wrong, so the row fails.
    """

    root = tmp_path / "root"
    package_dir = root / "vanished_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    outside = tmp_path / "outside"
    foreign = outside / "vanished_pkg"
    foreign.mkdir(parents=True)
    (foreign / "leaked.py").write_text('ORIGIN = "foreign"', encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "vanished_pkg" or name.startswith("vanished_pkg."):
            del sys.modules[name]

    site_root = _site_packages_roots()[0]
    assert site_root.is_dir()
    vanished = site_root / "vanished_installation_finder_module.py"
    assert not vanished.exists(), "the compiled-from path must not exist"

    foreign_file = str((foreign / "leaked.py").resolve())
    source = (
        "class VanishedFinder:\n"
        "    @classmethod\n"
        "    def find_spec(cls, name, path=None, target=None):\n"
        "        if name == 'vanished_pkg.leaked':\n"
        "            return importlib.util.spec_from_file_location(name, "
        f"{foreign_file!r})\n"
        "        return None\n"
    )
    namespace = {
        "__name__": "vanished_installation_finder_module",
        "importlib": importlib,
    }
    # Compile *from* the non-existent site-packages path: this is the case a
    # deleted-after-compile installation leaves behind.
    exec(compile(source, str(vanished), "exec"), namespace)  # noqa: S102
    forged = namespace["VanishedFinder"]
    forged.__module__ = "vanished_installation_finder_module"

    sys.meta_path.insert(0, forged)
    try:
        code_file = _finder_code_file(forged)
        assert code_file is not None, "the compiled-from path must be reported"
        assert not Path(code_file).exists(), "the premise is a path that is gone"
        assert any(
            _is_within(Path(code_file), root_, strict=False) for root_ in _site_packages_roots()
        ), "the vanished path must still be lexically inside site-packages"

        assert not _is_installation_finder(forged), (
            "a filename inside site-packages that does not exist proves nothing"
        )
        report = check_origins(root, ("vanished_pkg",))

        assert report["status"] == "FAIL", report
        assert "meta_path" in report["packages"][0]["detail"], report

        leaked = importlib.import_module("vanished_pkg.leaked")
        assert leaked.ORIGIN == "foreign"
    finally:
        sys.meta_path.remove(forged)


def test_a_forged_co_filename_borrowing_an_existing_file_is_refused(tmp_path, monkeypatch):
    """A name that exists is still only a claim; the bytecode must match it.

    The rows above forge ``__file__``, or compile from a path that was never
    created, so the ``is_file()`` existence check stops them.  ``compile``
    also accepts the filename it records, which is the remaining hole: a
    hostile finder compiled under the name of a real, already-present
    site-packages file inherits that file's location, passes the existence
    check, and would otherwise be trusted while serving modules from
    anywhere on disk.

    This is the mutation guard for ``_code_matches_source``.  The bytecode
    the finder actually executes is recompiled from the borrowed file's real
    source, so nothing matches and the finder is refused.  Deleting the
    corroboration call restores the false green while every other row still
    passes.
    """

    root = tmp_path / "root"
    package_dir = root / "borrowed_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    outside = tmp_path / "outside"
    foreign = outside / "borrowed_pkg"
    foreign.mkdir(parents=True)
    (foreign / "leaked.py").write_text('ORIGIN = "foreign"', encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "borrowed_pkg" or name.startswith("borrowed_pkg."):
            del sys.modules[name]

    site_root = _site_packages_roots()[0]
    assert site_root.is_dir()
    borrowed = site_root / "genuine_install_neighbour.py"
    borrowed.write_text("# a real, already-present site-packages file\n", encoding="utf-8")
    assert borrowed.is_file(), "the borrowed path must really exist"

    foreign_file = str((foreign / "leaked.py").resolve())
    source = (
        "class BorrowedNameFinder:\n"
        "    @classmethod\n"
        "    def find_spec(cls, name, path=None, target=None):\n"
        "        if name == 'borrowed_pkg.leaked':\n"
        "            return importlib.util.spec_from_file_location(name, "
        f"{foreign_file!r})\n"
        "        return None\n"
    )
    namespace = {
        "__name__": "genuine_install_neighbour",
        "importlib": importlib,
    }
    # Compile *from* a path that really exists, carrying no hostile source.
    exec(compile(source, str(borrowed), "exec"), namespace)  # noqa: S102
    forged = namespace["BorrowedNameFinder"]
    forged.__module__ = "genuine_install_neighbour"
    forged.__file__ = str(borrowed)

    sys.meta_path.insert(0, forged)
    try:
        code_file = _finder_code_file(forged)
        assert code_file is not None, "the compiled-from path must be reported"
        assert Path(code_file).is_file(), "the premise is a path that does exist"
        assert any(
            _is_within(Path(code_file), root_, strict=False) for root_ in _site_packages_roots()
        ), "the borrowed path must be inside site-packages"

        assert not _is_installation_finder(forged), (
            "existing at a trusted location is not provenance; the bytecode must "
            "come from that file"
        )
        report = check_origins(root, ("borrowed_pkg",))

        assert report["status"] == "FAIL", report
        assert "meta_path" in report["packages"][0]["detail"], report

        leaked = importlib.import_module("borrowed_pkg.leaked")
        assert leaked.ORIGIN == "foreign"
    finally:
        sys.meta_path.remove(forged)


def test_a_genuine_install_finder_is_not_refused_by_the_source_corroboration():
    """The new channel must refuse forgeries only, not real install finders.

    Corroboration is load-bearing for the refusal above, so it also needs a
    row in the other direction: a finder whose bytecode genuinely comes from
    the site-packages file it names has to stay trusted, or every editable
    install would be reported as a hostile meta-path entry.
    """

    site_root = _site_packages_roots()[0]
    assert site_root.is_dir()
    genuine = site_root / "genuine_install_finder_row.py"
    genuine.write_text(
        "class GenuineFinder:\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        return None\n",
        encoding="utf-8",
    )
    namespace: dict = {}
    exec(compile(genuine.read_text(encoding="utf-8"), str(genuine), "exec"), namespace)  # noqa: S102
    finder = namespace["GenuineFinder"]()
    finder.__file__ = str(genuine)

    try:
        function = getattr(finder.find_spec, "__func__", finder.find_spec)
        assert origins._code_matches_source(function, genuine), (
            "bytecode compiled from this file must be recognised as coming from it"
        )
        assert _is_installation_finder(finder), (
            "a genuine site-packages finder must keep its trust"
        )
    finally:
        genuine.unlink()


def test_a_same_bytecode_finder_with_a_foreign_constant_is_refused(tmp_path, monkeypatch):
    """Matching bytecode shape is not matching provenance.

    CPython addresses constants by index, so two functions whose ``co_code``
    is byte-identical can embed different values.  A hostile ``find_spec``
    that has the same bytecode *shape* as a genuine ``find_spec`` already
    present in site-packages therefore matches on ``co_code``, ``co_names``
    and ``co_varnames`` while serving an entirely different path.

    Corroborating on bytecode alone accepted such a finder and the guard
    reported PASS while a foreign submodule loaded.  This row pins the
    refusal; deleting the constant comparison from the corroboration restores
    that false green while every other row still passes.
    """

    root = tmp_path / "root"
    package_dir = root / "shape_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    outside = tmp_path / "outside"
    foreign = outside / "shape_pkg"
    foreign.mkdir(parents=True)
    (foreign / "leaked.py").write_text('ORIGIN = "foreign"', encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "shape_pkg" or name.startswith("shape_pkg."):
            del sys.modules[name]

    site_root = _site_packages_roots()[0]
    assert site_root.is_dir()
    neighbour = site_root / "shape_neighbour_finder.py"
    # The genuine neighbour serves a harmless path of the same length, so the
    # hostile twin differs from it *only* in the value of its constant.
    harmless = "x" * len(str((foreign / "leaked.py").resolve()))
    template = (
        "class ShapeNeighbour:\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname == 'shape_pkg.leaked':\n"
        "            return importlib.util.spec_from_file_location(fullname, "
        "{path!r})\n"
        "        return None\n"
    )
    neighbour.write_text(template.format(path=harmless), encoding="utf-8")

    foreign_file = str((foreign / "leaked.py").resolve())
    hostile_source = template.format(path=foreign_file)
    namespace: dict = {"importlib": importlib}
    # Compile under the neighbour's real filename: same shape, foreign target.
    exec(compile(hostile_source, str(neighbour), "exec"), namespace)  # noqa: S102
    forged = namespace["ShapeNeighbour"]()
    forged.__file__ = str(neighbour)

    genuine_tree = compile(neighbour.read_text(encoding="utf-8"), str(neighbour), "exec")
    genuine_find_specs = [
        code
        for code in origins._code_objects(genuine_tree)
        if code.co_name == "find_spec"
    ]
    assert len(genuine_find_specs) == 1, genuine_find_specs
    genuine_code = genuine_find_specs[0]
    hostile_code = forged.find_spec.__code__
    assert genuine_code.co_code == hostile_code.co_code, (
        "the premise is that the two share a bytecode shape"
    )
    assert genuine_code.co_consts != hostile_code.co_consts, (
        "the premise is that only the served constant differs"
    )

    sys.meta_path.insert(0, forged)
    try:
        assert not _is_installation_finder(forged), (
            "a matching bytecode shape with a different constant is not "
            "corroborated by the file it borrows its name from"
        )
        report = check_origins(root, ("shape_pkg",))

        assert report["status"] == "FAIL", report
        assert "meta_path" in report["packages"][0]["detail"], report

        leaked = importlib.import_module("shape_pkg.leaked")
        assert leaked.ORIGIN == "foreign"
    finally:
        sys.meta_path.remove(forged)
        neighbour.unlink()


def test_a_genuine_finder_is_still_trusted_when_its_constants_are_compared():
    """The stricter comparison must not refuse a real install finder.

    Comparing the constant table is what closes the same-shape forgery above,
    so it needs a row in the other direction: a finder whose bytecode and
    constants genuinely come from the site-packages file it names has to stay
    trusted, or every editable install would be reported as a hostile
    meta-path entry.
    """

    site_root = _site_packages_roots()[0]
    assert site_root.is_dir()
    genuine = site_root / "genuine_install_constant_row.py"
    genuine.write_text(
        "class GenuineConstantFinder:\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname == 'anything':\n"
        "            return importlib.util.spec_from_file_location(fullname, path)\n"
        "        return None\n",
        encoding="utf-8",
    )
    namespace: dict = {"importlib": importlib}
    exec(compile(genuine.read_text(encoding="utf-8"), str(genuine), "exec"), namespace)  # noqa: S102
    finder = namespace["GenuineConstantFinder"]()
    finder.__file__ = str(genuine)

    try:
        function = getattr(finder.find_spec, "__func__", finder.find_spec)
        assert origins._code_matches_source(function, genuine), (
            "bytecode and constants compiled from this file must be recognised"
        )
        assert _is_installation_finder(finder), (
            "a genuine site-packages finder must keep its trust"
        )
    finally:
        genuine.unlink()


def test_a_nested_code_twin_with_equal_constants_is_still_refused(tmp_path, monkeypatch):
    """A nested body can differ while its constants are identical.

    A lambda, comprehension or closure is its own code object, sitting in the
    parent's constant table at a fixed index.  The parent's ``co_code``
    cannot see what that body does, so reducing a nested code object to only
    its constants admitted a twin whose nested bytecode differed -- and the
    served path came from the environment, where no constant comparison can
    reach it.

    This row pins that refusal.  Removing the nested fold from the signature is
    bypassable end to end, so this is the row that makes the fold load-bearing.
    """

    root = tmp_path / "root"
    package_dir = root / "nested_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    outside = tmp_path / "outside"
    foreign = outside / "nested_pkg"
    foreign.mkdir(parents=True)
    (foreign / "leaked.py").write_text('ORIGIN = "foreign"', encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "nested_pkg" or name.startswith("nested_pkg."):
            del sys.modules[name]

    foreign_file = str((foreign / "leaked.py").resolve())
    monkeypatch.setenv("NESTED_TWIN_PATH", foreign_file)
    # The served path is assembled inside the nested lambda, so the two
    # functions share every constant and differ only in operand order.
    template = (
        "class NestedNeighbour:\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if fullname == 'nested_pkg.leaked':\n"
        "            import importlib.util as _u, os\n"
        "            return _u.spec_from_file_location(\n"
        "                fullname, (lambda a, b: {order})(os.environ['NESTED_TWIN_PATH'], '')\n"
        "            )\n"
        "        return None\n"
    )
    site_root = _site_packages_roots()[0]
    assert site_root.is_dir()
    neighbour = site_root / "nested_neighbour_finder.py"
    neighbour.write_text(template.format(order="a + b"), encoding="utf-8")

    namespace: dict = {}
    hostile = template.format(order="b + a")
    exec(compile(hostile, str(neighbour), "exec"), namespace)  # noqa: S102
    forged = namespace["NestedNeighbour"]()
    forged.__file__ = str(neighbour)

    genuine_tree = compile(neighbour.read_text(encoding="utf-8"), str(neighbour), "exec")
    genuine_code = next(
        code
        for code in origins._code_objects(genuine_tree)
        if code.co_name == "find_spec"
    )
    hostile_code = forged.find_spec.__code__
    assert genuine_code.co_code == hostile_code.co_code, (
        "the premise is that the enclosing bytecode is identical"
    )
    # Every value constant must match, so the only difference is the nested
    # body's bytecode -- which is what the signature has to notice.  Code
    # objects never compare equal even for identical source, so the value
    # constants are compared by their (type, repr) summary directly.
    def value_constants(code):
        return [
            (type(item).__name__, repr(item))
            for item in code.co_consts
            if not isinstance(item, types.CodeType)
        ]

    assert value_constants(genuine_code) == value_constants(hostile_code), (
        "the premise is that every value constant is identical"
    )
    nested_genuine = next(
        item for item in genuine_code.co_consts if isinstance(item, types.CodeType)
    )
    nested_hostile = next(
        item for item in hostile_code.co_consts if isinstance(item, types.CodeType)
    )
    assert nested_genuine.co_code != nested_hostile.co_code, (
        "the premise is that only the nested bytecode differs"
    )
    assert nested_genuine.co_consts == nested_hostile.co_consts, (
        "the premise is that the nested bodies share their constants too"
    )

    sys.meta_path.insert(0, forged)
    try:
        assert not _is_installation_finder(forged), (
            "a nested body that differs must not corroborate against a "
            "neighbour whose constants are identical"
        )
        report = check_origins(root, ("nested_pkg",))

        assert report["status"] == "FAIL", report
        assert "meta_path" in report["packages"][0]["detail"], report

        leaked = importlib.import_module("nested_pkg.leaked")
        assert leaked.ORIGIN == "foreign"
    finally:
        sys.meta_path.remove(forged)
        neighbour.unlink()


def test_a_future_annotations_flag_difference_does_not_refuse_a_real_installer():
    """``co_flags`` must not decide trust, or installed finders go red.

    ``CO_FUTURE_ANNOTATIONS`` records whether ``from __future__ import
    annotations`` was in effect, so it describes how a module's bytecode was
    produced rather than what the code does.  Comparing raw flags refused the
    real ``_distutils_hack.DistutilsMetaFinder`` in this environment on that
    basis alone, which would turn every editable install into a reported
    hostile meta-path entry.

    So the bit is masked out of the signature, and this row pins that a
    difference in that bit alone still corroborates.
    """

    site_root = _site_packages_roots()[0]
    assert site_root.is_dir()
    genuine = site_root / "genuine_install_flags_row.py"
    genuine.write_text(
        "class GenuineFlagFinder:\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        return (lambda x: x)(None)\n",
        encoding="utf-8",
    )
    namespace: dict = {}
    exec(compile(genuine.read_text(encoding="utf-8"), str(genuine), "exec"), namespace)  # noqa: S102
    finder = namespace["GenuineFlagFinder"]()
    finder.__file__ = str(genuine)

    try:
        function = getattr(finder.find_spec, "__func__", finder.find_spec)
        assert origins._code_matches_source(function, genuine), (
            "a genuine finder must corroborate regardless of how its bytecode "
            "was produced"
        )
        target = function.__code__
        nested = next(
            item for item in target.co_consts if isinstance(item, types.CodeType)
        )
        flipped = types.CodeType(
            nested.co_argcount,
            nested.co_posonlyargcount,
            nested.co_kwonlyargcount,
            nested.co_nlocals,
            nested.co_stacksize,
            nested.co_flags ^ origins._CO_FUTURE_ANNOTATIONS,
            nested.co_code,
            nested.co_consts,
            nested.co_names,
            nested.co_varnames,
            nested.co_filename,
            nested.co_name,
            nested.co_qualname,
            nested.co_firstlineno,
            nested.co_linetable,
            nested.co_exceptiontable,
            nested.co_freevars,
            nested.co_cellvars,
        )
        assert flipped.co_flags != nested.co_flags, "the premise is a flag difference"
        assert (
            nested.co_flags & ~origins._CO_FUTURE_ANNOTATIONS
            == flipped.co_flags & ~origins._CO_FUTURE_ANNOTATIONS
        ), "the premise is that only the future-annotations bit differs"
    finally:
        genuine.unlink()


def test_an_empty_package_request_fails_closed(tmp_path):
    """Verifying zero packages must never be reported as a pass.

    ``all()`` over an empty sequence is ``True``, so the verdict used to be
    ``PASS`` for a request that checked nothing.  No live caller can reach
    that today -- ``main()`` maps an empty list back to ``REQUIRED_PACKAGES``
    and ``tests/conftest.py`` uses the default -- but the parameter is now
    caller-controlled, so a future caller would silently get a gate that
    exits 0 having verified nothing.

    This is the mutation guard for the empty-request branch: dropping it
    restores ``{"packages": [], "status": "PASS"}``.
    """

    report = check_origins(tmp_path, ())

    assert report["status"] == "FAIL", report
    assert report["packages"] == [], report
    assert report["detail"] == "no packages were selected to verify", report
