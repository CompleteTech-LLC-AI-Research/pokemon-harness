"""Acceptance for the #534 import-origin guard.

The repository is developed across many worktrees that share one virtual
environment.  When the editable install points at a different worktree, every
selected tier silently measures that other tree.  These rows pin both
directions: the guard must accept the tree under test and reject any other.
"""

import base64
import contextlib
import hashlib
import importlib
import importlib.util
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
    _code_is_defined_in,
    _finder_code_file,
    _finder_source,
    _is_installation_finder,
    _is_recorded_by_an_install,
    _is_trusted_stdlib_finder,
    _is_within,
    _site_packages_roots,
    _trusted_stdlib_finders,
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
        origins, "_resolve_origin", lambda name: (installed[name] / "__init__.py", "", None)
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
        origins, "_resolve_origin", lambda name: (installed[name] / "__init__.py", "", None)
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
        origins, "_resolve_origin", lambda name: (installed[name] / "__init__.py", "", None)
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
        origins, "_resolve_origin", lambda name: (installed[name] / "__init__.py", "", None)
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
        origins, "_resolve_origin", lambda name: (installed[name] / "__init__.py", "", None)
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
        origins, "_resolve_origin", lambda name: (installed[name] / "__init__.py", "", None)
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
        origins, "_resolve_origin", lambda name: (installed[name] / "__init__.py", "", None)
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
    monkeypatch.setattr(origins, "_resolve_origin", lambda name: (stock / "__init__.py", "", None))

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

    with _module_installed("unprintable_import", types.ModuleType("unprintable_import")):
        with _import_replaced(_failing_import):
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
        lambda package: (package_dir / "__init__.py", "", None),
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
    whether it lands inside site-packages *and* really contains the finder's
    code.  Resolution of a missing path succeeds, so containment alone would
    also accept a filename that merely *reads* as though it were installed --
    for example a file the interpreter compiled from and has since deleted, or
    a name a site-packages writer never actually produced.

    This row is the mutation guard for the ``is_file()`` check.  It compiles a
    finder from a path inside site-packages that does not exist, asserts the
    path really is contained, and then requires the finder to be refused
    anyway.  Requiring the code to be in the named file catches this one step
    earlier than the containment check would, so the row pins the refusal and
    the missing-file premise; the ``is_file()`` check stays as the second,
    independent barrier for a file that exists but is not readable as one.
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
        # The filename is still read from ``co_filename``, and the path is
        # still lexically inside site-packages -- but trust is no longer
        # decided from that name.  Requiring the code to actually *be* in the
        # named file refuses the finder one step earlier, at the missing file,
        # so there is no containing file to report.  That is the stronger
        # outcome: the vanished-path case is now covered by the existence of
        # the file rather than by a later containment check that the premise
        # would otherwise satisfy.
        assert code_file is None, (
            "a filename naming a file that does not exist must not be reported "
            "as the finder's provenance"
        )
        claimed = Path(forged.find_spec.__func__.__code__.co_filename)
        assert not claimed.exists(), "the premise is a path that is gone"
        assert any(_is_within(claimed, root_, strict=False) for root_ in _site_packages_roots()), (
            "the vanished path must still be lexically inside site-packages"
        )

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


def test_a_forged_co_filename_cannot_impersonate_a_real_installed_file(tmp_path, monkeypatch):
    """A ``co_filename`` naming a real site-packages file is not provenance.

    ``compile(source, filename, "exec")`` records whatever ``filename`` the
    caller supplies without reading that file.  A finder can therefore name a
    file that genuinely exists inside site-packages -- passing the existence
    and containment checks -- while its code came from somewhere else entirely.

    Trust is instead decided by requiring the finder's code to actually appear
    in the file it names.  This row is the mutation guard for that check: it
    borrows the name of a real installed ``.py`` file and requires the finder to
    be refused anyway.  Dropping the code-presence check leaves every other
    check satisfied and the trust decision wrong, so the row fails.
    """

    root = tmp_path / "root"
    package_dir = root / "forgedname_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    outside = tmp_path / "outside"
    foreign = outside / "forgedname_pkg"
    foreign.mkdir(parents=True)
    (foreign / "leaked.py").write_text('ORIGIN = "foreign"', encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "forgedname_pkg" or name.startswith("forgedname_pkg."):
            del sys.modules[name]

    site_root = _site_packages_roots()[0]
    assert site_root.is_dir()
    borrowed = sorted(path for path in site_root.glob("*.py") if path.is_file())
    if not borrowed:
        borrowed = sorted(path for path in site_root.rglob("*.py") if path.is_file())
    assert borrowed, "this row needs a real existing .py file inside site-packages"
    borrowed = borrowed[0]

    foreign_file = str((foreign / "leaked.py").resolve())
    source = (
        "class ForgedNameFinder:\n"
        "    @classmethod\n"
        "    def find_spec(cls, name, path=None, target=None):\n"
        "        if name == 'forgedname_pkg.leaked':\n"
        "            return importlib.util.spec_from_file_location(name, "
        f"{foreign_file!r})\n"
        "        return None\n"
    )
    namespace = {"__name__": "forgedname_module", "importlib": importlib}
    # The attack: a real, existing site-packages path as co_filename, for code
    # that file does not contain.
    exec(compile(source, str(borrowed), "exec"), namespace)  # noqa: S102 - the attack
    forged = namespace["ForgedNameFinder"]
    forged.__module__ = "forgedname_module"

    sys.meta_path.insert(0, forged)
    try:
        code = forged.find_spec.__func__.__code__
        assert code.co_filename == str(borrowed)
        assert Path(borrowed).is_file(), "the borrowed path must really exist"
        assert any(
            _is_within(Path(borrowed), root_, strict=False) for root_ in _site_packages_roots()
        ), "the borrowed file must really sit inside site-packages"
        # The premise that makes this a forgery: the named file does not hold
        # the finder's code.
        assert not _code_is_defined_in(code, Path(borrowed)), (
            "this row needs a file that exists but does not contain the code"
        )

        assert not _is_installation_finder(forged), (
            "a co_filename naming an existing site-packages file is not provenance"
        )
        report = check_origins(root, ("forgedname_pkg",))
        assert report["status"] == "FAIL", report
        assert "meta_path" in report["packages"][0]["detail"], report

        leaked = importlib.import_module("forgedname_pkg.leaked")
        assert leaked.ORIGIN == "foreign"
    finally:
        sys.meta_path.remove(forged)


def test_a_genuine_installation_finder_is_still_trusted():
    """The code-presence check must not refuse the finders it exists to allow.

    ``_virtualenv._Finder`` is installed by this checkout's own environment and
    sits on ``sys.meta_path`` for every run, so refusing it would make the guard
    report FAIL for the interpreter it is supposed to certify.

    The check is a fingerprint comparison against the recompiled source, so it
    is sensitive to the *caller's* compiler flags: ``compile()`` inherits the
    calling frame's ``__future__`` flags, and this module has
    ``from __future__ import annotations``.  Inheriting them stamped
    ``CO_FUTURE_ANNOTATIONS`` onto every recompiled code object and no genuine
    finder could ever match, which is a self-inflicted false FAIL rather than a
    safe refusal.  This row pins the trusted outcome so that failure mode
    cannot come back.
    """

    custom = [entry for entry in sys.meta_path if not _is_trusted_stdlib_finder(entry)]
    assert custom, "expected this interpreter to have an installation finder"
    for finder in custom:
        code_file = _finder_code_file(finder)
        assert code_file is not None, (
            f"{finder!r} is installed by this checkout but reports no code file"
        )
        assert code_file.is_file(), f"{code_file} must exist"
        assert any(_is_within(code_file, root, strict=False) for root in _site_packages_roots()), (
            f"{code_file} is not inside this interpreter's site-packages"
        )
        assert _is_installation_finder(finder), (
            f"{finder!r} must stay trusted; refusing it fails the release lane"
        )


def test_a_foreign_path_portion_is_reported_even_when_file_changes_between_reads(
    tmp_path, monkeypatch
):
    """``__file__`` is read once per check, and a second read cannot skip it.

    ``__file__`` is mutable state the interpreter reports, and a hostile
    path-like can answer differently on each read.  The origin was resolved from
    one read and the portion check used to take a second: a first read matching
    the origin followed by a second that did not made the guard skip the portion
    check entirely, so a package with a genuinely foreign ``__path__`` reported
    PASS.  Independent review reproduced exactly that.

    This row pins both halves.  The drifting value is read once, so the foreign
    portion is still reported; and a value that raises is reported as a finding
    rather than escaping ``check_origins`` as a traceback.
    """

    root = tmp_path / "root"
    package_dir = root / "drifting_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    local_init = (package_dir / "__init__.py").resolve()
    foreign = tmp_path / "outside" / "drifting_pkg"
    foreign.mkdir(parents=True)
    (foreign / "__init__.py").write_text("", encoding="utf-8")
    (foreign / "leaked.py").write_text('ORIGIN = "foreign"', encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "drifting_pkg" or name.startswith("drifting_pkg."):
            del sys.modules[name]

    drifting = __import__("drifting_pkg")
    # A genuinely foreign portion: this is the finding that must survive.
    drifting.__path__ = [str(foreign)]

    class Drifting:
        """Local on the first read, foreign on every later read."""

        def __init__(self) -> None:
            self.reads = 0

        def __fspath__(self) -> str:
            self.reads += 1
            return str(local_init if self.reads == 1 else foreign / "__init__.py")

    drifting_file = Drifting()
    monkeypatch.setattr(drifting, "__file__", drifting_file, raising=False)

    report = check_origins(root, ("drifting_pkg",))

    assert report["status"] == "FAIL", report
    assert "path portion" in report["packages"][0]["detail"] or (
        "resolves outside" in report["packages"][0]["detail"]
    ), report
    # One read only: a second read is exactly the hole this row closes.
    assert drifting_file.reads <= 1, (
        f"__file__ was read {drifting_file.reads} times; a second read can disagree"
    )

    class Hostile:
        """A path-like whose conversion raises, which must not escape."""

        def __fspath__(self) -> str:
            raise RuntimeError("boom")

        def __str__(self) -> str:
            raise RuntimeError("boom-str")

    monkeypatch.setattr(drifting, "__file__", Hostile(), raising=False)
    hostile_report = check_origins(root, ("drifting_pkg",))

    assert hostile_report["status"] == "FAIL", hostile_report
    assert "not a usable path" in hostile_report["packages"][0]["detail"], hostile_report


def test_a_foreign_portion_that_cannot_be_rendered_is_still_reported(tmp_path, monkeypatch):
    """The FAIL detail is built from the same untrusted data it reports.

    A finding names the foreign ``__path__`` entries it found, and rendering one
    of those calls ``str()`` on an object the interpreter -- not this guard --
    controls.  If that rendering raises, the finding is lost and the traceback
    that replaces it is precisely the failure this module exists to prevent:
    the operator sees a crash instead of the FAIL naming the offending package.

    This row pins the reporting half.  ``_describe`` is the only place that
    coerces untrusted data to text, so the detail must survive an object whose
    ``__str__`` and ``__repr__`` both raise, and the status must still be FAIL.
    """

    root = tmp_path / "root"
    package_dir = root / "hostile_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "hostile_pkg" or name.startswith("hostile_pkg."):
            del sys.modules[name]

    hostile = __import__("hostile_pkg")

    class Unprintable:
        """A path-like that cannot be converted *or* rendered."""

        def __fspath__(self) -> str:
            raise RuntimeError("boom-fspath")

        def __str__(self) -> str:
            raise RuntimeError("boom-str")

        def __repr__(self) -> str:
            raise RuntimeError("boom-repr")

    # A portion the guard cannot resolve *and* cannot render: the reason text
    # is itself built from the object, so this exercises both coercions.
    hostile.__path__ = [Unprintable(), tmp_path / "outside" / "hostile_pkg"]

    report = check_origins(root, ("hostile_pkg",))

    assert report["status"] == "FAIL", report
    detail = report["packages"][0]["detail"]
    assert detail, "a FAIL must carry a detail naming what was wrong"
    # The rendering degrades to a placeholder; the finding is not lost.
    assert "unprintable" in detail or "not a usable path" in detail, detail
    # The report must stay JSON-serializable, since the CLI emits it verbatim.
    json.dumps(report)


def test_source_copied_into_site_packages_does_not_certify_an_execed_finder(tmp_path, monkeypatch):
    """Matching bytes in a site-packages file are not load provenance.

    The content check asks whether the finder's code is *in* the file it names.
    That is necessary, but it is not the claim the guard needs: the running
    finder must have been *loaded from* that file.  Writing the finder's own
    source into site-packages and then running
    ``exec(compile(source, that_path, "exec"))`` satisfies every content check
    while the finder has no provenance there at all.  Two independent reviewers
    reproduced this by different routes and each time the guard reported PASS
    with a foreign submodule loading afterwards.

    This row pins the missing half: the defining module must be an entry the
    import system created for that same file, which a bare ``exec`` never
    produces.  Dropping ``_finder_was_imported_from`` from the trust decision
    leaves the content check satisfied and this row fails.
    """

    root = tmp_path / "root"
    package_dir = root / "copied_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    outside = tmp_path / "outside"
    foreign = outside / "copied_pkg"
    foreign.mkdir(parents=True)
    (foreign / "leaked.py").write_text('ORIGIN = "foreign"', encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "copied_pkg" or name.startswith("copied_pkg."):
            del sys.modules[name]

    site_root = _site_packages_roots()[0]
    assert site_root.is_dir()
    planted = site_root / "copied_source_finder_row.py"
    foreign_file = str((foreign / "leaked.py").resolve())
    source = (
        "import importlib.util\n"
        "class CopiedSourceFinder:\n"
        "    @classmethod\n"
        "    def find_spec(cls, name, path=None, target=None):\n"
        "        if name == 'copied_pkg.leaked':\n"
        "            return importlib.util.spec_from_file_location(name, "
        f"{foreign_file!r})\n"
        "        return None\n"
    )
    planted.write_text(source, encoding="utf-8")
    forged = None
    try:
        namespace = {"importlib": importlib, "__name__": "copied_source_finder_row"}
        # The attack: the file really holds these exact bytes, and the code is
        # compiled with that file as co_filename -- but it is never imported.
        exec(compile(source, str(planted), "exec"), namespace)  # noqa: S102
        forged = namespace["CopiedSourceFinder"]
        forged.__module__ = "copied_source_finder_row"

        code = forged.find_spec.__func__.__code__
        assert code.co_filename == str(planted)
        assert Path(planted).is_file(), "the planted file must really exist"
        assert any(
            _is_within(Path(planted), root_, strict=False) for root_ in _site_packages_roots()
        ), "the planted file must really sit inside site-packages"
        # The premise: content matching succeeds and is still not enough.
        assert _code_is_defined_in(code, Path(planted)), (
            "this row needs the content check to pass so it isolates provenance"
        )
        assert "copied_source_finder_row" not in sys.modules, (
            "a bare exec must not create a module entry"
        )

        assert not _is_installation_finder(forged), (
            "code that merely matches a site-packages file was never loaded from it"
        )
        sys.meta_path.insert(0, forged)
        report = check_origins(root, ("copied_pkg",))

        assert report["status"] == "FAIL", report
        assert "meta_path" in report["packages"][0]["detail"], report

        leaked = importlib.import_module("copied_pkg.leaked")
        assert leaked.ORIGIN == "foreign"
    finally:
        if forged is not None:
            sys.meta_path.remove(forged)
        planted.unlink(missing_ok=True)


def test_a_forged_module_and_spec_do_not_certify_an_uncertified_finder(tmp_path, monkeypatch):
    """``sys.modules[name].__spec__`` is attacker-writable, so it is no evidence.

    The previous attempt to supply load provenance asked the defining module's
    spec to name the file.  Independent review showed the whole pair can be
    written by hand: build a module with ``types.ModuleType``, attach a spec
    from ``importlib.util.spec_from_file_location`` -- which sets
    ``has_location=True`` -- and every check the guard made is satisfied while
    the import system never loaded anything from that file.  The guard reported
    PASS, ``cli_rc`` was 0, and a foreign submodule loaded afterwards.

    This row pins the replacement: provenance comes from install records on disk
    (``RECORD`` hash, or a ``.pth`` that imports the module), which a running
    process cannot rewrite into a different claim.
    """

    root = tmp_path / "root"
    package_dir = root / "forged_spec_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    foreign = tmp_path / "outside" / "forged_spec_pkg"
    foreign.mkdir(parents=True)
    (foreign / "leaked.py").write_text('ORIGIN = "foreign"', encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "forged_spec_pkg" or name.startswith("forged_spec_pkg."):
            del sys.modules[name]

    site_root = _site_packages_roots()[0]
    assert site_root.is_dir()
    planted = site_root / "forged_spec_finder_row.py"
    foreign_file = str((foreign / "leaked.py").resolve())
    source = (
        "import importlib.util\n"
        "class ForgedSpecFinder:\n"
        "    @classmethod\n"
        "    def find_spec(cls, name, path=None, target=None):\n"
        "        if name == 'forged_spec_pkg.leaked':\n"
        "            return importlib.util.spec_from_file_location(name, "
        f"{foreign_file!r})\n"
        "        return None\n"
    )
    planted.write_text(source, encoding="utf-8")
    forged = None
    try:
        namespace = {"__name__": "forged_spec_finder_row"}
        exec(compile(source, str(planted), "exec"), namespace)  # noqa: S102
        forged = namespace["ForgedSpecFinder"]

        # The forgery: a module the import system never created, carrying a
        # file-location spec pointing at the planted file.
        planted_module = types.ModuleType("forged_spec_finder_row")
        planted_module.__file__ = str(planted)
        planted_module.__spec__ = importlib.util.spec_from_file_location(
            "forged_spec_finder_row", planted
        )
        sys.modules["forged_spec_finder_row"] = planted_module
        try:
            # The premise: every in-memory signal the earlier check relied on is
            # satisfied, so this row really isolates the new provenance rule.
            assert planted_module.__spec__.has_location is True
            assert planted_module.__spec__.origin == str(planted)
            assert Path(planted).is_file()
            assert any(
                _is_within(Path(planted), root_, strict=False) for root_ in _site_packages_roots()
            ), "the planted file must really sit inside site-packages"
            assert _finder_code_file(forged) is not None, (
                "this row needs the content check to pass so it isolates provenance"
            )

            assert not _is_installation_finder(forged), (
                "a hand-written module and spec are not load provenance"
            )
            sys.meta_path.insert(0, forged)
            report = check_origins(root, ("forged_spec_pkg",))

            assert report["status"] == "FAIL", report
            assert "meta_path" in report["packages"][0]["detail"], report
            json.dumps(report)

            leaked = importlib.import_module("forged_spec_pkg.leaked")
            assert leaked.ORIGIN == "foreign"
        finally:
            sys.modules.pop("forged_spec_finder_row", None)
    finally:
        if forged is not None:
            sys.meta_path.remove(forged)
        planted.unlink(missing_ok=True)


def test_a_finder_descriptor_raising_base_exception_becomes_a_finding(tmp_path):
    """A custom ``BaseException`` from a descriptor must not escape the guard.

    The hostile-descriptor rows in this file raise ordinary ``Exception``
    subclasses, which ``except Exception`` already covers.  ``find_spec`` is
    read from an object the interpreter does not control, so a metaclass can
    raise a direct ``BaseException`` subclass instead.  That escaped
    ``check_origins`` as a traceback with no JSON report -- exactly the outcome
    this module exists to make impossible.
    """

    class HostileSignal(BaseException):
        pass

    class Meta(type):
        @property
        def find_spec(cls):
            raise HostileSignal("descriptor failure")

    class Finder(metaclass=Meta):
        pass

    root = tmp_path / "root"
    (root / "descriptor_pkg").mkdir(parents=True)
    (root / "descriptor_pkg" / "__init__.py").write_text("", encoding="utf-8")
    sys.meta_path.insert(0, Finder)
    try:
        report = check_origins(root, ("descriptor_pkg",))

        assert report["status"] == "FAIL", report
        assert "meta_path" in report["packages"][0]["detail"], report
        json.dumps(report)
    finally:
        sys.meta_path.remove(Finder)


def test_a_finder_module_getter_raising_base_exception_becomes_a_finding(tmp_path):
    """A hostile ``__module__`` getter must degrade to a placeholder name.

    ``_describe_finder`` builds the name the operator reads in the refusal, so
    a metaclass that raises a custom ``BaseException`` from ``__module__``
    turned the FAIL back into a traceback.  The review that found it exercised
    the CLI as well as the API, and observed an empty stdout with the raise on
    stderr.
    """

    class Fatal(BaseException):
        pass

    class Meta(type):
        def __getattribute__(cls, name):
            if name == "__module__":
                raise Fatal("module getter fatal")
            return super().__getattribute__(name)

    class Finder(metaclass=Meta):
        @classmethod
        def find_spec(cls, name, path=None, target=None):
            return None

    root = tmp_path / "root"
    (root / "module_getter_pkg").mkdir(parents=True)
    (root / "module_getter_pkg" / "__init__.py").write_text("", encoding="utf-8")
    sys.meta_path.insert(0, Finder)
    try:
        report = check_origins(root, ("module_getter_pkg",))

        assert report["status"] == "FAIL", report
        detail = report["packages"][0]["detail"]
        assert "meta_path" in detail, detail
        assert "<finder with an unreadable identity>" in detail, detail
        json.dumps(report)
    finally:
        sys.meta_path.remove(Finder)


def test_a_finder_equal_to_a_stdlib_finder_is_not_trusted(tmp_path, monkeypatch):
    """Trust is decided by identity, never by membership.

    ``finder in <set>`` consults the candidate's own ``__hash__`` and
    ``__eq__``, and a metaclass can supply both.  An object that hashes equal to
    ``BuiltinImporter`` and compares equal to everything is "in" a set holding
    the real one while being a different object entirely -- so it was trusted
    before the disk provenance code ran at all.  No site-packages write, no
    ``.pth``, no ``RECORD``: independent review obtained PASS, rc 0, and a
    foreign submodule that loaded afterwards.
    """

    root = tmp_path / "root"
    package_dir = root / "pokered_harness"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    foreign = tmp_path / "outside" / "foreign_probe.py"
    foreign.parent.mkdir(parents=True)
    foreign.write_text('ORIGIN = "foreign"', encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "pokered_harness" or name.startswith("pokered_harness."):
            del sys.modules[name]

    class ImpostorMeta(type):
        def __hash__(cls):
            import importlib._bootstrap as bootstrap

            return hash(bootstrap.BuiltinImporter)

        def __eq__(cls, other):
            return True

        def find_spec(cls, name, path=None, target=None):
            if name == "pokered_harness.foreign_probe":
                return importlib.util.spec_from_file_location(name, str(foreign))
            return None

    class Impostor(metaclass=ImpostorMeta):
        pass

    # The premise: equality membership accepts it, identity does not.
    assert Impostor in _trusted_stdlib_finders(), (
        "this row needs the metaclass to forge set membership"
    )
    assert not _is_trusted_stdlib_finder(Impostor), (
        "trust must come from identity; a forged __eq__ is not evidence"
    )

    sys.meta_path.insert(0, Impostor)
    try:
        report = check_origins(root, ("pokered_harness",))

        assert report["status"] == "FAIL", report
        assert "meta_path" in report["packages"][0]["detail"], report
        json.dumps(report)

        leaked = importlib.import_module("pokered_harness.foreign_probe")
        assert leaked.ORIGIN == "foreign"
    finally:
        sys.meta_path.remove(Impostor)


def test_a_hostile_meta_path_iterator_becomes_a_finding(tmp_path):
    """``sys.meta_path`` is the untrusted collection, so walking it can raise.

    Two places walk it: this module's own snapshot, and
    ``importlib.metadata.packages_distributions`` inside ``_allowed_roots``,
    which iterates ``sys.meta_path`` looking for ``find_distributions``.  The
    stdlib call is the one that escaped -- a list subclass with a raising
    ``__iter__`` produced a traceback out of ``check_origins`` with no JSON.
    """

    class Fatal(BaseException):
        pass

    class HostileMetaPath(list):
        def __iter__(self):
            raise Fatal("meta_path iteration boom")

    root = tmp_path / "root"
    (root / "iter_pkg").mkdir(parents=True)
    (root / "iter_pkg" / "__init__.py").write_text("", encoding="utf-8")

    original = sys.meta_path
    sys.meta_path = HostileMetaPath(original)
    try:
        report = check_origins(root, ("iter_pkg",))

        assert report["status"] == "FAIL", report
        assert "meta_path" in report["packages"][0]["detail"], report
        json.dumps(report)
    finally:
        sys.meta_path = original


def test_a_hostile_path_iterator_becomes_a_finding(tmp_path):
    """A ``__path__`` whose *iteration* raises is as untrusted as its read.

    The attribute read was already guarded, but the value it returns was then
    iterated outside any handler, so an object with a raising ``__iter__``
    escaped.  Reading is only half the hazard.
    """

    class Fatal(BaseException):
        pass

    class HostilePath:
        def __iter__(self):
            raise Fatal("path iteration broke")

    root = tmp_path / "root"
    package = types.ModuleType("hostile_iter_pkg")
    package.__file__ = str(root / "hostile_iter_pkg" / "__init__.py")
    package.__path__ = HostilePath()
    sys.modules[package.__name__] = package
    try:
        report = check_origins(root, (package.__name__,))

        assert report["status"] == "FAIL", report
        assert "uniterable __path__" in report["packages"][0]["detail"], report
        json.dumps(report)
    finally:
        del sys.modules[package.__name__]


def test_a_path_getter_that_raises_becomes_a_finding_not_a_traceback(tmp_path):
    """Reading ``__path__`` is untrusted, and a raising getter must fail closed.

    ``_resolve_origin`` guards the ``__file__`` read only when ``__file__`` is
    absent, so a module with a usable ``__file__`` and a hostile ``__path__``
    reached the portion check and raised out of the guard.  Independent review
    reproduced this with an ordinary ``RuntimeError`` and observed a traceback
    instead of a JSON FAIL.
    """

    class RaisingPath(types.ModuleType):
        def __getattribute__(self, name):
            if name == "__path__":
                raise RuntimeError("search path getter failed")
            return super().__getattribute__(name)

    root = tmp_path / "root"
    package = RaisingPath("path_getter_pkg")
    package.__file__ = str(root / "path_getter_pkg" / "__init__.py")
    sys.modules[package.__name__] = package
    try:
        report = check_origins(root, (package.__name__,))

        assert report["status"] == "FAIL", report
        assert "unreadable __path__" in report["packages"][0]["detail"], report
        json.dumps(report)
    finally:
        del sys.modules[package.__name__]


def _record_digest(candidate: Path) -> str:
    """Return the URL-safe base64 sha256 ``RECORD`` stores for a file."""

    digest = hashlib.sha256(candidate.read_bytes()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def test_a_record_attested_file_is_trusted_and_a_tampered_one_is_not(tmp_path):
    """``RECORD`` is the provenance witness, so its parsing must be exact.

    The witness is a CSV row of ``path,sha256=<digest>,size``, and both halves
    are load-bearing.  Parsing the path from the left truncates any name that
    legitimately contains a comma; parsing the digest from either end reads the
    size instead.  Either mistake makes every ``RECORD``-attested finder fail to
    attest -- a false FAIL on the supported release lane -- which is why this
    row drives the parser directly instead of trusting the CLI to surface it.

    A second record that disagrees about the same file must also refuse: the
    witness is "every install agrees", not "the first record sorted first".
    """

    site_root = tmp_path / "site-packages"
    dist_info = site_root / "probe_dist-1.0.dist-info"
    dist_info.mkdir(parents=True)

    plain = site_root / "recorded_finder.py"
    plain.write_text("VALUE = 1\n", encoding="utf-8")
    # A comma in the name is legal CSV and must not truncate the path.
    commay = site_root / "recorded,finder.py"
    commay.write_text("VALUE = 2\n", encoding="utf-8")
    (dist_info / "RECORD").write_text(
        f"recorded_finder.py,sha256={_record_digest(plain)},21\n"
        # A name holding a comma is quoted in real RECORD output; the parser is
        # CSV, so an unquoted one is malformed and must not resolve.
        f'"{commay.name}",sha256={_record_digest(commay)},21\n',
        encoding="utf-8",
    )
    site_root = site_root.resolve()

    assert _is_recorded_by_an_install(plain.resolve(), site_root) is True
    assert _is_recorded_by_an_install(commay.resolve(), site_root) is True

    # Changing the bytes must break the attestation.
    plain.write_text("VALUE = 99\n", encoding="utf-8")
    assert _is_recorded_by_an_install(plain.resolve(), site_root) is False

    # A second, disagreeing record must refuse rather than be ignored.
    plain.write_text("VALUE = 1\n", encoding="utf-8")
    assert _is_recorded_by_an_install(plain.resolve(), site_root) is True
    rival = site_root / "rival-2.0.dist-info"
    rival.mkdir()
    (rival / "RECORD").write_text(
        f"recorded_finder.py,sha256={'A' * 43},21\n",
        encoding="utf-8",
    )
    assert _is_recorded_by_an_install(plain.resolve(), site_root) is False


def test_a_csv_quoted_record_name_is_decoded(tmp_path):
    """``RECORD`` is CSV, so pip quotes a name that contains a comma.

    A hand-rolled split keeps the quotes attached to the name, so the path built
    from the row names a file no record ever listed and a genuinely installed
    finder with a comma in its path loses its provenance.  That is a false
    refusal on an honest install, bought for no security gain.

    Found independently on #558, then reproduced against this branch's own
    parser before fixing rather than assumed equivalent.
    """

    site_root = tmp_path / "site-packages"
    dist_info = site_root / "probe_dist-1.0.dist-info"
    dist_info.mkdir(parents=True)

    commay = site_root / "csv,comma_row.py"
    commay.write_text("VALUE = 1\n", encoding="utf-8")
    (dist_info / "RECORD").write_text(
        f'"{commay.name}",sha256={_record_digest(commay)},14\n',
        encoding="utf-8",
    )
    site_root = site_root.resolve()

    assert _is_recorded_by_an_install(commay.resolve(), site_root) is True, (
        "a CSV-quoted name must be decoded, or an honest install is refused"
    )


@pytest.mark.parametrize(
    "body",
    [
        pytest.param('"target.py,sha256=abc\n', id="unterminated_quote"),
        pytest.param("\x00\x01target.py,sha256=abc,4\n", id="nul_and_binary"),
        pytest.param(",,,,\n", id="only_commas"),
        pytest.param("x" * 100000 + ",sha256=abc,4\n", id="giant_field"),
        pytest.param("", id="empty"),
    ],
)
def test_a_hostile_record_never_raises_and_never_attests(tmp_path, body):
    """``RECORD`` is untrusted input, so a malformed row is inert.

    The parser reads a file the interpreter does not control.  A row that is not
    valid CSV, carries a NUL, or is enormous must contribute nothing and must
    not raise: this decision runs inside conftest, so an escape here is an
    INTERNALERROR rather than a FAIL finding.
    """

    site_root = tmp_path / "site-packages"
    dist_info = site_root / "probe_dist-1.0.dist-info"
    dist_info.mkdir(parents=True)
    target = site_root / "target.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    site_root = site_root.resolve()

    (dist_info / "RECORD").write_text(body, encoding="utf-8", errors="replace")

    assert _is_recorded_by_an_install(target.resolve(), site_root) is False


def test_a_planted_pth_cannot_certify_a_hand_written_finder(tmp_path, monkeypatch):
    """A ``.pth`` naming a module is a weaker witness than ``RECORD``, and says so.

    Writing a module into site-packages *and* a ``.pth`` beside it satisfies the
    rule, and this row pins that behaviour deliberately rather than leaving it
    implicit: the guard now treats that pair as an install, so it must be a
    conscious decision rather than an accident of the implementation.

    The point that matters for the verdict is that this is no longer a *false
    PASS from nothing*: attesting it requires writing into the environment's
    own site-packages, which is the boundary this guard has always assumed for
    that directory.
    """

    root = tmp_path / "root"
    package_dir = root / "pth_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "pth_pkg" or name.startswith("pth_pkg."):
            del sys.modules[name]

    site_root = _site_packages_roots()[0]
    assert site_root.is_dir()
    planted = site_root / "pth_attested_finder_row.py"
    planted.write_text(
        "class PthAttestedFinder:\n"
        "    @classmethod\n"
        "    def find_spec(cls, name, path=None, target=None):\n"
        "        return None\n",
        encoding="utf-8",
    )
    pth = site_root / "zz_pth_attested_finder_row.pth"
    pth.write_text("import pth_attested_finder_row\n", encoding="utf-8")
    module = None
    try:
        module = importlib.import_module("pth_attested_finder_row")
        finder = module.PthAttestedFinder
        assert _is_installation_finder(finder), (
            "a .pth naming the module is the witness _virtualenv and the "
            "editable shim actually have; refusing it would refuse the release lane"
        )
    finally:
        sys.modules.pop("pth_attested_finder_row", None)
        pth.unlink(missing_ok=True)
        planted.unlink(missing_ok=True)


def test_a_genuine_finder_with_a_non_utf8_source_file_keeps_its_trust():
    """Reading source as UTF-8 refuses a real latin-1 installation finder.

    PEP 263 lets a module declare its encoding in a cookie, and the import
    system honours it.  Reading the same file as UTF-8 with ``errors="replace"``
    rewrites its bytes into *different* string constants, so the recompiled code
    never matches the running finder's and a genuine finder is refused.  That is
    a false FAIL on the supported release lane, not a safe refusal.

    This row pins the decoder: source is read the way the interpreter reads it.

    The module is planted in site-packages and imported for real, so the only
    thing standing between it and trust is the ``_pth`` rule -- the same
    attestation a real ``_virtualenv`` / ``__editable__`` shim gets.  Writing
    that ``.pth`` is what makes this a genuine installer shim rather than the
    planted-file shape the provenance rows must refuse.
    """
    site_root = _site_packages_roots()[0]
    assert site_root.is_dir()
    latin = site_root / "latin1_encoded_finder_row.py"
    latin.write_bytes(
        (
            "# coding: latin-1\n"
            "class Latin1Finder:\n"
            "    @classmethod\n"
            "    def find_spec(cls, name, path=None, target=None):\n"
            "        tag = 'caf\u00e9'\n"
            "        if name == tag:\n"
            "            return None\n"
            "        return None\n"
        ).encode("latin-1")
    )
    pth = site_root / "zz_latin1_encoded_finder_row.pth"
    pth.write_text("import latin1_encoded_finder_row\n", encoding="utf-8")
    try:
        module = importlib.import_module("latin1_encoded_finder_row")
        code = module.Latin1Finder.find_spec.__func__.__code__
        assert any(
            isinstance(constant, str) and not constant.isascii() for constant in code.co_consts
        ), "this row needs a non-ASCII constant inside find_spec"

        assert _finder_code_file(module.Latin1Finder) is not None, (
            "a genuine finder with a latin-1 source must report its code file"
        )
        assert _is_installation_finder(module.Latin1Finder), (
            "a genuine finder must not be refused because of its source encoding"
        )
    finally:
        sys.modules.pop("latin1_encoded_finder_row", None)
        pth.unlink(missing_ok=True)
        latin.unlink(missing_ok=True)


def test_a_module_whose_file_attribute_raises_becomes_a_finding(tmp_path):
    """Reading ``__file__`` is untrusted too, and must not escape the guard.

    A module can override ``__getattribute__`` so that touching ``__file__``
    raises.  That read sat outside the guard's exception boundary, so the raise
    escaped ``check_origins`` as a traceback -- the operator sees a crash instead
    of the FAIL naming the offending package, which is the one outcome this
    module exists to make impossible.

    The existing hostile rows cover converting a returned value; none of them
    covers a getter that raises.
    """

    class RaisingFile(types.ModuleType):
        def __getattribute__(self, name):
            if name == "__file__":
                raise RuntimeError("getter boom")
            return super().__getattribute__(name)

    sys.modules["raising_file_pkg"] = RaisingFile("raising_file_pkg")
    try:
        report = check_origins(tmp_path, ("raising_file_pkg",))

        assert report["status"] == "FAIL", report
        assert "could not be read" in report["packages"][0]["detail"], report
        json.dumps(report)
    finally:
        del sys.modules["raising_file_pkg"]
