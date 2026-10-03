"""Acceptance for the #534 import-origin guard.

The repository is developed across many worktrees that share one virtual
environment.  When the editable install points at a different worktree, every
selected tier silently measures that other tree.  These rows pin both
directions: the guard must accept the tree under test and reject any other.
"""

import base64
import contextlib
import csv
import hashlib
import importlib
import importlib.util
import io
import json
import os
import re
import runpy
import subprocess
import sys
import tokenize
import types
from pathlib import Path

import pytest

import scripts.check_import_origins as origins
import scripts.production_gate as gate
from scripts.check_import_origins import (
    _describe,
    _file_digest,
    _finder_code_file,
    _finder_source,
    _is_imported_by_a_pth,
    _is_installation_finder,
    _is_recorded_by_an_install,
    _is_trusted_stdlib_finder,
    _is_within,
    _record_digests,
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


def test_a_finder_that_raises_on_hash_becomes_a_finding_not_a_traceback(tmp_path, monkeypatch):
    """A membership test against a finder set calls the finder's ``__hash__``.

    ``_is_trusted_stdlib_finder`` decides trust with ``finder in <set>``, and
    a set lookup invokes ``__hash__`` on the object being tested.  A hostile
    finder controls that method, so the lookup is attacker-controlled data
    reached before any of the surrounding guards apply.

    A custom ``BaseException`` here is a finding, not an operator interrupt:
    only ``KeyboardInterrupt`` and ``SystemExit`` are re-raised, everywhere
    else in this module.  So the guard must report the finder as untrusted
    rather than letting the exception escape ``check_origins``.
    """

    class ExplodingHash(BaseException):
        """A direct ``BaseException`` subclass, i.e. not an operator interrupt."""

    class HashBomb:
        def __hash__(self):
            raise ExplodingHash("hash escape")

        def find_spec(self, *args):
            return None

    monkeypatch.setattr(sys, "meta_path", [HashBomb(), *sys.meta_path])

    report = check_origins(tmp_path, ("interrupting_ns",))

    assert report["status"] == "FAIL"
    assert any(package["package"] == "<interpreter>" for package in report["packages"]), (
        f"the hostile finder must be refused as an interpreter finding: {report}"
    )


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


def test_a_hostile_path_getter_cannot_abort_the_guard(tmp_path):
    """A namespace package whose ``__path__`` raises must be a finding.

    ``_resolve_origin`` guards the ``__file__`` read, but the namespace
    fallback read ``__path__`` two lines later sat outside any guard, so a
    module whose ``__getattribute__`` raises a direct ``BaseException``
    escaped ``check_origins`` entirely.  ``_foreign_path_locations`` had the
    same unguarded read, and the ``__file__`` corroboration inside it a
    third.  All three now convert the raise into a reported finding.
    """

    class ExplodingPath(BaseException):
        pass

    class BadModule(types.ModuleType):
        def __getattribute__(self, name):
            if name == "__path__":
                raise ExplodingPath("path boom")
            return super().__getattribute__(name)

    package = tmp_path / "path_crash_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")

    module = BadModule("path_crash_pkg")
    module.__file__ = str(package / "__init__.py")
    with _module_installed("path_crash_pkg", module):
        report = check_origins(tmp_path, ("path_crash_pkg",))

    assert report["status"] == "FAIL", report
    assert "__path__ could not be read" in report["packages"][0]["detail"], report


def test_a_hostile_site_module_cannot_abort_the_guard(tmp_path):
    """``site`` getters are untrusted input too, and must fail closed.

    ``_site_packages_roots`` reads ``site.getsitepackages()`` and
    ``site.getusersitepackages()``.  Both calls were guarded with
    ``except Exception``, but ``site`` is an ordinary module attribute and a
    process that replaced it -- or a test that does -- can raise a direct
    ``BaseException`` subclass.  That escaped ``check_origins`` as a
    traceback, with no machine-readable finding at all.

    The repaired guards return no install roots, which makes every consumer
    treat the finder as untrusted: ``_finder_is_installed`` falls through to
    ``False`` and ``_is_trusted_finder`` returns ``False``.  The guard
    therefore refuses instead of crashing.
    """

    class ExplodingSite(BaseException):
        pass

    real_site = sys.modules.get("site")

    def boom(*_args, **_kwargs):
        raise ExplodingSite("site getter boom")

    hostile_site = types.ModuleType("site")
    hostile_site.getsitepackages = boom
    hostile_site.getusersitepackages = boom

    package = tmp_path / "site_boom_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")

    module = types.ModuleType("site_boom_pkg")
    module.__file__ = str(package / "__init__.py")
    sys.modules["site"] = hostile_site
    try:
        with _module_installed("site_boom_pkg", module):
            report = check_origins(tmp_path, ("site_boom_pkg",))
    finally:
        if real_site is None:
            del sys.modules["site"]
        else:
            sys.modules["site"] = real_site

    assert report["status"] == "FAIL", report


def test_an_unreadable_finder_descriptor_refuses_installation_trust(tmp_path, monkeypatch):
    """``_is_installation_finder``'s own descriptor reads must refuse, not escape.

    Round 3 repaired the descriptor reads in ``_finder_code_file``, but the
    same pair in ``_is_installation_finder`` (``find_spec``, then ``__func__``)
    was still guarded with ``except Exception``.  Reaching them requires a
    finder that already looks installed, which is why the round-3 meta-path
    row passes on the unfixed source and cannot catch this.
    """

    class ExplodingDescriptor(BaseException):
        pass

    class HostileFinder:
        @property
        def find_spec(self):
            raise ExplodingDescriptor("descriptor boom")

        def __repr__(self):
            return "<hostile finder>"

    code_file = tmp_path / "installed.py"
    code_file.write_text("", encoding="utf-8")
    monkeypatch.setattr(origins, "_finder_code_file", lambda _finder: code_file)
    monkeypatch.setattr(origins, "_finder_was_imported_from", lambda _finder, _code_file: True)
    monkeypatch.setattr(origins, "_site_packages_roots", lambda: [tmp_path])
    monkeypatch.setattr(origins, "_is_within", lambda *_a, **_k: True)

    assert origins._is_installation_finder(HostileFinder()) is False


def test_an_unreadable_code_object_cannot_abort_the_guard():
    """Code-object introspection must refuse an unreadable code object.

    ``_code_matches_source`` reads ``function.__code__`` and compares code
    signatures, and both walks -- ``_code_objects`` and ``_code_signature`` --
    read ``co_consts`` and the per-field attributes unguarded, while
    ``function`` is the very object a hostile ``find_spec`` descriptor
    supplied.  A code-like object whose ``co_consts`` raises a direct
    ``BaseException`` subclass therefore escaped ``check_origins``.

    Two unreadable signatures must never compare equal to each other, or two
    hostile objects would corroborate one another into a false match, so the
    refusal value is built fresh on every call.
    """

    class ExplodingConsts(BaseException):
        pass

    class HostileCode:
        co_name = "find_spec"

        @property
        def co_consts(self):
            raise ExplodingConsts("co_consts boom")

    def genuine() -> None:
        return None

    assert origins._code_signature(HostileCode()) != origins._code_signature(HostileCode())
    assert isinstance(origins._code_signature(genuine.__code__), tuple)
    # The walk yields the object itself, then stops rather than raising.
    walked = list(origins._code_objects(HostileCode()))
    assert walked[0].co_name == "find_spec"
    assert len(walked) == 1


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

    # The behaviour above is pinned on its own by
    # ``test_package_flag_still_admits_a_real_install``.  This row additionally
    # pins the wording the CLI shows, because the flag silently changes meaning
    # if the help ever reverts to calling it additive.
    #
    # The wording is matched against whitespace-normalised text.  ``argparse``
    # wraps help to the terminal width, and it wraps *inside* the phrase this
    # row exists to pin: at ``COLUMNS=40`` the correct wording renders as
    #
    #     (repeatable;
    #     replaces -- not adds
    #     to --
    #     pokered_harness and
    #     pyboy)
    #
    # so a substring test for "not adds to" fails on a correct tree purely
    # because of the terminal it ran in.  Collapsing runs of whitespace to a
    # single space makes the assertion independent of wrapping while still
    # failing on the misleading pre-#546 wording, which survives normalisation
    # unchanged.
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "replaces" in help_text
    assert "not adds to" in help_text
    assert "additional distribution name" not in help_text


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


@pytest.mark.parametrize("raised", [KeyboardInterrupt(), SystemExit()])
def test_an_interrupt_while_naming_a_refused_finder_still_propagates(tmp_path, monkeypatch, raised):
    """``_describe_finder`` builds a FAIL detail, and must not eat an interrupt.

    This is the one site in the class that is reachable from the public entry
    point: ``check_origins`` joins ``_describe_finder`` over untrusted
    intruders to render the ``<interpreter>`` FAIL detail.  A finder that is
    both an untrusted intruder and unable to report its identity therefore
    swallowed a Ctrl-C while the guard was writing the very message the
    operator needed to read.

    The other sites in this class are currently masked by an earlier guard that
    propagates first, which is why they are pinned structurally by the
    ``ast`` sweep rather than by a row each: a row can only prove a site if it
    can reach it.
    """

    root = tmp_path / "root"
    package_dir = root / "interrupt_named_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")

    class HostileMeta(type):
        def __getattribute__(cls, name):
            if name in ("__module__", "__qualname__", "__name__"):
                raise raised
            return type.__getattribute__(cls, name)

    intruder = HostileMeta(
        "InterruptNamedFinder",
        (),
        {"find_spec": classmethod(lambda cls, name, path=None, target=None: None)},
    )

    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(sys, "meta_path", [intruder, *sys.meta_path])
    for name in list(sys.modules):
        if name == "interrupt_named_pkg" or name.startswith("interrupt_named_pkg."):
            del sys.modules[name]

    with pytest.raises((KeyboardInterrupt, SystemExit)):
        check_origins(root, ("interrupt_named_pkg",))


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


def test_a_rebound_stdlib_finder_loses_trust(tmp_path, monkeypatch):
    """Identity alone does not make a ``sys.meta_path`` entry the interpreter's own.

    A ``sys.meta_path`` entry is a mutable class in a live process, so rebinding
    ``PathFinder.find_spec`` keeps the identical object while replacing the code
    that runs.  Trust decided by identity alone then reads PASS while the
    rebound finder serves submodules from anywhere -- a full false green with no
    ``.pth``, no ``RECORD`` and no new finder in sight.

    The interpreter's own finders are defined in frozen modules, so requiring
    the executing code to be frozen is what separates the two.
    """

    import importlib.machinery

    assert _is_trusted_stdlib_finder(importlib.machinery.PathFinder), (
        "premise: the untouched finder is trusted"
    )

    original = importlib.machinery.PathFinder.find_spec

    def rebound(cls, name, path=None, target=None):
        return original(name, path, target)

    importlib.machinery.PathFinder.find_spec = classmethod(rebound)
    try:
        assert not _is_trusted_stdlib_finder(importlib.machinery.PathFinder), (
            "a finder whose find_spec was rebound in this process is no longer "
            "the interpreter's own, even though the object is unchanged"
        )
    finally:
        importlib.machinery.PathFinder.find_spec = original

    assert _is_trusted_stdlib_finder(importlib.machinery.PathFinder), (
        "restoring the original method restores trust; a genuine environment must not be refused"
    )


def test_a_rebound_stdlib_finder_is_refused_end_to_end(tmp_path, monkeypatch):
    """The rebound-finder false green, measured through the report.

    The unit row above pins the helper.  This row pins the consequence, which
    is what the guard actually promises: a submodule that really did come from
    a foreign tree must not be certified by a report that says PASS.
    """

    import importlib.machinery

    root = tmp_path / "root"
    checkout = root / "checkout"
    (checkout / "rebound_pkg").mkdir(parents=True)
    (checkout / "rebound_pkg" / "__init__.py").write_text("", encoding="utf-8")
    foreign = root / "foreign"
    foreign.mkdir()
    (foreign / "leaked.py").write_text("ORIGIN = 'FOREIGN'\n", encoding="utf-8")

    original = importlib.machinery.PathFinder.find_spec

    def rebound(cls, name, path=None, target=None):
        if name == "rebound_pkg.leaked":
            return importlib.util.spec_from_file_location(name, str(foreign / "leaked.py"))
        return original(name, path, target)

    importlib.machinery.PathFinder.find_spec = classmethod(rebound)
    monkeypatch.syspath_prepend(str(checkout))
    for name in list(sys.modules):
        if name == "rebound_pkg" or name.startswith("rebound_pkg."):
            del sys.modules[name]
    try:
        import rebound_pkg.leaked

        assert rebound_pkg.leaked.ORIGIN == "FOREIGN", (
            "the premise: the rebound finder really did serve a foreign module"
        )
        report = check_origins(checkout, ("rebound_pkg",))
        assert report["status"] == "FAIL", (
            f"the guard certified an interpreter whose finder was rebound: {report}"
        )
    finally:
        importlib.machinery.PathFinder.find_spec = original
        for name in list(sys.modules):
            if name == "rebound_pkg" or name.startswith("rebound_pkg."):
                del sys.modules[name]


def test_a_planted_pth_pair_does_not_certify_a_finder(tmp_path):
    """A ``.pth`` naming a module is not evidence until an install vouches.

    ``_is_imported_by_a_pth`` decides whether a finder module in
    site-packages is trusted.  Requiring only that some ``.pth`` in the same
    directory import it by name is no evidence at all: an attacker who can
    write into site-packages writes both halves.  The pair only counts when
    one distribution's ``RECORD`` claims *both* files.

    Both directions are pinned.  A ``.pth`` nobody installed must not certify
    the module it names, and the genuine editable-install shape -- where one
    owner records the ``.pth`` and the module together -- must stay trusted
    or the release lane refuses every editable checkout.
    """

    root = tmp_path / "site-packages"
    root.mkdir()
    shim = root / "planted_shim.py"
    shim.write_text(
        "class Finder:\n"
        "    @classmethod\n"
        "    def find_spec(cls, name, path=None, target=None):\n"
        "        return None\n",
        encoding="utf-8",
    )
    pth = root / "planted.pth"
    pth.write_text("import planted_shim\n", encoding="utf-8")

    assert "import planted_shim" in pth.read_text(encoding="utf-8"), (
        "the premise: the .pth does name the module"
    )
    assert not _is_imported_by_a_pth(shim, root), (
        "a .pth nobody installed must not certify the module it names"
    )

    # Recording the .pth alone is still not enough: it leaves the module owned
    # by nobody, so no single distribution vouches for the pair.
    dist_info = root / "planted_shim-1.0.dist-info"
    dist_info.mkdir()
    digest = _file_digest(pth)
    assert digest is not None
    (dist_info / "RECORD").write_text(
        f"{pth.name},sha256={digest},{pth.stat().st_size}\n", encoding="utf-8"
    )
    assert not _is_imported_by_a_pth(shim, root), (
        "the .pth being recorded does not carry the module: one install vouched "
        "for the .pth and nobody vouched for the module, so the pair is owned by "
        "no single distribution and must not certify the finder"
    )

    # Recording the module too -- by the *same* owner -- is the shape a real
    # editable install produces, and it must still be accepted.
    shim_digest = _file_digest(shim)
    assert shim_digest is not None
    with open(dist_info / "RECORD", "a", encoding="utf-8") as handle:
        handle.write(f"{shim.name},sha256={shim_digest},{shim.stat().st_size}\n")
    assert _is_imported_by_a_pth(shim, root), (
        "an install-recorded .pth activating an install-recorded shim is the "
        "genuine editable-install shape and must stay trusted"
    )


def test_a_recorded_pth_cannot_certify_a_module_another_owner_planted(tmp_path):
    """One attested half of a ``.pth`` pairing cannot vouch for the other.

    Attesting only the ``.pth`` was a working bypass.  An attacker who can
    write into site-packages does not need to plant a ``.pth`` at all -- every
    editable install already left one behind that a genuine distribution
    recorded -- only a module beside it whose name that ``.pth`` happens to
    import.  The pairing then read as install-attested while the module was
    owned by nobody at all.
    """

    root = tmp_path / "site-packages"
    root.mkdir()

    # A genuine install's .pth -- attested, and naming the planted module.
    pth = root / "__editable__.pokered_harness-0.1.0.pth"
    pth.write_text("import planted_borrower\n", encoding="utf-8")
    pth_digest = _file_digest(pth)
    assert pth_digest is not None
    dist_info = root / "pokered_harness-0.1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "RECORD").write_text(
        f"{pth.name},sha256={pth_digest},{pth.stat().st_size}\n", encoding="utf-8"
    )
    assert _is_recorded_by_an_install(pth, root), (
        "the premise: the .pth itself is genuinely install-attested"
    )

    # The attacker's module: named by that .pth, recorded by nobody.
    borrower = root / "planted_borrower.py"
    borrower.write_text(
        "class Borrower:\n    def find_spec(self, *args, **kwargs):\n        return None\n",
        encoding="utf-8",
    )
    assert not _is_recorded_by_an_install(borrower, root), (
        "the premise: no install claimed the module"
    )
    assert not _is_imported_by_a_pth(borrower, root), (
        "an install-recorded .pth must not certify a module that install never "
        "recorded: the attacker plants only the module, not the .pth"
    )


def test_a_rewritten_recorded_pth_stops_certifying_its_module(tmp_path):
    """An owner that no longer matches the bytes on disk cannot certify anything.

    A ``RECORD`` entry is a historical claim, not a live one.  A ``.pth`` can
    be rewritten after its install without the record changing at all, so the
    file then imports whatever the attacker chose while the ``RECORD`` still
    carries the original digest for it.  Consulting only *which* owner claimed
    the ``.pth`` reads that rewritten file as attested and hands it the module.

    Both halves therefore have to match the owner's recorded digests as they
    are now, not merely be named by the same owner.
    """

    root = tmp_path / "site-packages"
    root.mkdir()

    # A genuine install: the .pth and two modules it could name, both recorded.
    pth = root / "__editable__.pokered_harness-0.1.0.pth"
    pth.write_text("import harmless_thing\n", encoding="utf-8")
    module = root / "some_other_installed_module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    harmless = root / "harmless_thing.py"
    harmless.write_text("VALUE = 0\n", encoding="utf-8")

    dist_info = root / "pokered_harness-0.1.0.dist-info"
    dist_info.mkdir()
    rows = [
        f"{path.name},sha256={_file_digest(path)},{path.stat().st_size}"
        for path in (pth, module, harmless)
    ]
    (dist_info / "RECORD").write_text("\n".join(rows) + "\n", encoding="utf-8")

    assert _is_recorded_by_an_install(pth, root), "the premise: the .pth is attested"
    assert not _is_imported_by_a_pth(module, root), (
        "the premise: the .pth does not yet import this module"
    )

    # The attack: rewrite only the .pth so it imports a *different* module the
    # same install also recorded.  The RECORD is untouched and now stale.
    pth.write_text("import some_other_installed_module\n", encoding="utf-8")
    assert not _is_recorded_by_an_install(pth, root), (
        "the premise: the rewritten .pth no longer matches its RECORD"
    )
    assert not _is_imported_by_a_pth(module, root), (
        "a .pth whose bytes no longer match its RECORD must not certify anything, "
        "even though the same owner recorded both files"
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


def test_a_hostile_finder_raising_base_exception_cannot_abort_the_guard(tmp_path, monkeypatch):
    """A ``BaseException`` from a hostile metaclass must not escape either.

    The row above raises ``RuntimeError``, which ``except Exception`` already
    caught.  A metaclass is arbitrary code and need not stay inside
    ``Exception``: raising a direct ``BaseException`` subclass bypassed every
    such clause in ``_finder_source`` and escaped ``check_origins`` with a
    traceback, which is the exact crash mode the preflight exists to prevent.
    """

    class ExplodingBase(BaseException):
        """Not an ``Exception``; that distinction is the whole point of the row."""

    class ExplodingMeta(type):
        @property
        def __module__(cls):
            raise ExplodingBase("metaclass refuses to name itself")

    root = tmp_path / "root"
    package_dir = root / "base_exception_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "base_exception_pkg" or name.startswith("base_exception_pkg."):
            del sys.modules[name]

    hostile = ExplodingMeta("HostileFinder", (), {"find_spec": lambda self, *a: None})
    monkeypatch.setattr(sys, "meta_path", [hostile, *sys.meta_path])

    report = check_origins(root, ("base_exception_pkg",))

    assert report["status"] == "FAIL", report
    assert "meta_path" in report["packages"][0]["detail"], report


def test_a_hostile_finder_descriptor_cannot_abort_the_guard(tmp_path, monkeypatch):
    """A ``find_spec`` descriptor raising ``BaseException`` must be a finding.

    Two sites leaked this.  ``_finder_code_file`` guarded its descriptor
    lookups with ``except Exception``.  And ``_allowed_roots`` wrapped
    ``importlib.metadata.packages_distributions()`` the same way -- but that
    call imports helper modules, so a hostile finder raises straight out of
    it before any distribution is even examined.  Both now catch
    ``BaseException``; the metadata one returns no site-packages roots, which
    fails closed.
    """

    class ExplodingDescriptor(BaseException):
        pass

    class HostileDescriptor:
        @property
        def find_spec(self):
            raise ExplodingDescriptor("descriptor boom")

    root = tmp_path / "root"
    package_dir = root / "descriptor_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "descriptor_pkg" or name.startswith("descriptor_pkg."):
            del sys.modules[name]

    monkeypatch.setattr(sys, "meta_path", [HostileDescriptor(), *sys.meta_path])

    report = check_origins(root, ("descriptor_pkg",))

    assert report["status"] == "FAIL", report


def test_a_hostile_meta_path_container_cannot_abort_the_guard(tmp_path, monkeypatch):
    """An unreadable ``sys.meta_path`` must be a finding, not a traceback.

    ``sys.meta_path`` was read with a bare ``for finder in list(sys.meta_path)``.
    The container is attacker-controlled in the same way a hostile
    ``__path__`` is: whoever installs a finder chooses what list it lands in,
    and materialising it calls that container's ``__iter__``.  A direct
    ``BaseException`` subclass raised there bypassed every clause in the loop
    body -- there was none -- and escaped ``check_origins`` before a report
    existed.  Materialisation is now guarded, and an unreadable meta-path is
    reported as the untrusted container it is, which fails closed.
    """

    class ExplodingIter(BaseException):
        """Not an ``Exception``; that distinction is the whole point of the row."""

    class HostileMetaPath:
        def __iter__(self):
            raise ExplodingIter("meta_path refuses to be iterated")

    root = tmp_path / "root"
    package_dir = root / "meta_path_container_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "meta_path_container_pkg" or name.startswith("meta_path_container_pkg."):
            del sys.modules[name]

    hostile = HostileMetaPath()
    monkeypatch.setattr(sys, "meta_path", hostile)

    report = check_origins(root, ("meta_path_container_pkg",))

    assert report["status"] == "FAIL", report
    assert "meta_path" in report["packages"][0]["detail"], report


def test_a_hostile_project_root_resolve_cannot_abort_the_guard(tmp_path, monkeypatch):
    """A ``resolve()`` that raises must produce a structured ``FAIL``.

    ``project_root.resolve()`` ran unguarded, before any report existed, so a
    caller-supplied path-like that raised a direct ``BaseException`` subclass
    escaped as a traceback.  A root that cannot be resolved cannot be shown to
    be the checkout under test, so the guard refuses it.

    The hostile root is an ``os.PathLike`` rather than a ``Path`` subclass:
    ``pathlib.Path`` cannot be subclassed this way on Python 3.11 (the
    subclass has no ``_flavour``), so the ``Path`` form of this row could
    never be constructed and therefore never exercised the guard at all.
    """

    class ExplodingResolve(BaseException):
        pass

    class HostileRoot(os.PathLike):
        def __init__(self, path):
            self._path = path

        def __fspath__(self):
            return str(self._path)

        def resolve(self, *args, **kwargs):
            raise ExplodingResolve("root refuses to resolve")

    package_dir = tmp_path / "hostile_root_pkg"
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in list(sys.modules):
        if name == "hostile_root_pkg" or name.startswith("hostile_root_pkg."):
            del sys.modules[name]

    report = check_origins(HostileRoot(tmp_path), ("hostile_root_pkg",))

    assert report["status"] == "FAIL", report
    assert "could not be resolved" in report["detail"], report


def test_a_hostile_project_root_cannot_escape_while_being_reported(tmp_path):
    """Reporting an unresolvable root must not re-raise on ``str(root)``.

    The first fix rendered the failed root with a bare ``str(project_root)`` --
    the same hostile object whose ``resolve`` had just raised.  A root that
    also traps ``__str__`` therefore escaped out of the handler meant to
    report the escape, reproducing the very defect the row above closes.  The
    report renders it through ``_describe``.
    """

    class ExplodingStr(BaseException):
        pass

    class ExplodingResolve(BaseException):
        pass

    class HostileRoot:
        def resolve(self, *args, **kwargs):
            raise ExplodingResolve("root refuses to resolve")

        def __str__(self):
            raise ExplodingStr("root refuses to be printed")

    report = check_origins(HostileRoot(), ("whatever_pkg",))

    assert report["status"] == "FAIL", report
    assert "could not be resolved" in report["detail"], report
    assert report["project_root"].startswith("<unprintable"), report


def test_a_hostile_metaclass_cannot_break_finding_rendering():
    """``_describe`` and ``_type_name`` must render a hostile metaclass.

    Both helpers close the same class of hole in different places: everything
    this module reports came from the interpreter, so a value can carry a
    metaclass that explodes on ``__name__``.  ``str(value)`` is guarded by
    ``_describe``, but the type name is read off ``type(value)`` and is itself
    a lookup on an object the attacker controls -- so it needs its own guard.

    This is asserted against the helpers directly on purpose.  Driving the
    metaclass through ``check_origins`` instead would make pytest itself
    crash while formatting the failure (``object.__repr__`` reads
    ``type(self).__name__``), so a regression here would abort the whole
    session rather than report one failing row.
    """

    class ExplodingName(BaseException):
        pass

    class ExplodingMeta(type):
        @property
        def __name__(cls):
            raise ExplodingName("metaclass refuses to name itself")

    class Hostile(metaclass=ExplodingMeta):
        def __str__(self):
            raise ExplodingName("hostile refuses to be printed")

    hostile = Hostile()

    def render(call):
        """Run ``call``, reporting an escape as a printable string.

        Both helpers are called through this so that a regression can be
        *reported*.  An escaping exception would otherwise be handed to
        pytest, whose repr of it reads the hostile metaclass and aborts the
        entire session -- turning one broken row into an unreadable run.  The
        class name used here comes from a real exception class, so it is safe
        to render.
        """

        try:
            return str(call())
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # noqa: BLE001 - see docstring
            return f"<escaped {type(exc).__name__}>"

    rendered = render(lambda: _describe(hostile))
    type_name = render(lambda: origins._type_name(hostile))

    assert rendered.startswith("<unprintable"), rendered
    # Referenced off the module, not imported at the top of the suite: a top
    # level ``from ... import _type_name`` would abort collection outright
    # against a tree that does not define it, which would mask the very
    # regression this row exists to catch.
    assert type_name == "<unknown type>", type_name
    assert _describe(1) == "1", _describe(1)
    assert origins._type_name(1) == "int", origins._type_name(1)


def test_an_interrupt_from_the_meta_path_container_still_propagates(tmp_path, monkeypatch):
    """An operator interrupt must not be laundered into a finding.

    This module catches ``BaseException`` broadly so that hostile input cannot
    crash the guard, and re-raises ``KeyboardInterrupt`` and ``SystemExit`` so a
    real interrupt still stops the run.  The new meta-path guard must not
    swallow one: a Ctrl-C arriving while the guard is reading ``sys.meta_path``
    has to reach the operator, not be recorded as a finding about the tree.
    """

    class HostileMetaPath:
        def __iter__(self):
            raise KeyboardInterrupt

    root = tmp_path / "root"
    package_dir = root / "interrupt_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(sys, "meta_path", HostileMetaPath())

    with pytest.raises(KeyboardInterrupt):
        check_origins(root, ("interrupt_pkg",))


def test_an_interrupt_from_the_project_root_still_propagates(tmp_path):
    """The root guard must also leave an operator interrupt alone.

    Same contract as the meta-path row above, at the other new guard: a
    ``KeyboardInterrupt`` raised while the root is being resolved belongs to
    the operator, not to the tree under test.

    """

    class HostileRoot(os.PathLike):
        def __init__(self, path):
            self._path = path

        def __fspath__(self):
            return str(self._path)

        def resolve(self, *args, **kwargs):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        check_origins(HostileRoot(tmp_path), ("whatever_pkg",))


def test_a_hostile_distribution_object_cannot_abort_the_guard(tmp_path, monkeypatch):
    """A distribution whose ``read_text`` raises must not escape.

    ``_installed_from`` caught only ``(OSError, PackageNotFoundError)`` around
    ``distribution.read_text``.  A distribution is third-party code and that
    call is just a method on it, so a direct ``BaseException`` subclass
    bypassed the enumeration and escaped ``check_origins`` as a traceback.

    The row also pins the *direction* of the repair.  Returning ``None`` is
    only safe because ``None`` means "this install cannot be attributed to
    this checkout", which withholds an allowed root rather than granting one.
    A generic handler that instead admitted the install would turn a hostile
    metadata object into a way to widen the trusted tree, and would still
    produce no traceback -- so the absence of an escape proves nothing on its
    own.  The package here is planted outside the root and genuinely absent
    from the checkout, so a correct refusal is ``FAIL`` and only a widened
    allowed-root set could report ``PASS``.
    """

    class ExplodingReadText(BaseException):
        pass

    class EvilDistribution:
        def read_text(self, name):
            raise ExplodingReadText("metadata read escape")

    outside = tmp_path / "outside"
    outside.mkdir()
    _make_package(outside, "unattributable_pkg")
    monkeypatch.syspath_prepend(str(outside))
    for name in list(sys.modules):
        if name == "unattributable_pkg" or name.startswith("unattributable_pkg."):
            del sys.modules[name]

    monkeypatch.setattr(
        origins.importlib.metadata,
        "packages_distributions",
        lambda: {"unattributable_pkg": ["evil"]},
    )
    monkeypatch.setattr(origins.importlib.metadata, "distribution", lambda name: EvilDistribution())

    report = check_origins(tmp_path / "checkout", ("unattributable_pkg",))

    assert report["status"] == "FAIL", report


def test_a_hostile_owners_mapping_cannot_abort_the_guard(tmp_path, monkeypatch):
    """An owner mapping that raises on ``.get`` must not escape.

    ``_allowed_roots`` called ``distributions.get(package, ())`` on whatever
    ``packages_distributions()`` returned.  That mapping is attacker-reachable,
    so the lookup is a call into arbitrary code; a direct ``BaseException``
    subclass raised there escaped both ``_allowed_roots`` and
    ``check_origins``.  As above, the package is planted outside the root so
    that only a withheld allowed root can produce the expected ``FAIL``.
    """

    class ExplodingMapping(BaseException):
        pass

    class HostileOwners(dict):
        def get(self, *args, **kwargs):
            raise ExplodingMapping("hostile owners mapping")

    outside = tmp_path / "outside"
    outside.mkdir()
    _make_package(outside, "unmappable_pkg")
    monkeypatch.syspath_prepend(str(outside))
    for name in list(sys.modules):
        if name == "unmappable_pkg" or name.startswith("unmappable_pkg."):
            del sys.modules[name]

    monkeypatch.setattr(origins.importlib.metadata, "packages_distributions", HostileOwners)

    report = check_origins(tmp_path / "checkout", ("unmappable_pkg",))

    assert report["status"] == "FAIL", report


def test_an_interrupt_from_the_site_layout_still_propagates(monkeypatch):
    """``_site_packages_roots`` must not swallow an operator interrupt.

    The two site-layout helpers catch ``BaseException`` so an unreadable
    layout cannot crash the guard, but they previously swallowed
    ``KeyboardInterrupt`` and ``SystemExit`` too -- which contradicts the
    contract the rest of this module keeps and that the hash and root rows
    above pin.  A layout error is advisory and may be ignored; a Ctrl-C
    belongs to the operator.
    """

    import site

    for attribute in ("getsitepackages", "getusersitepackages"):

        def interrupt(*args, **kwargs):
            raise KeyboardInterrupt

        monkeypatch.setattr(site, attribute, interrupt, raising=False)

        with pytest.raises(KeyboardInterrupt):
            origins._site_packages_roots()


class _Interrupting:
    """Attribute access that raises the interrupt the test is parameterised on."""

    def __init__(self, raised):
        self._raised = raised

    def __getattr__(self, name):
        raise self._raised


def _interrupting_finder(raised):
    """A finder whose ``__module__`` raises, built without inheriting the metaclass.

    Only usable for the clauses at or before the ``__module__`` lookup.  The
    later clauses need a finder whose ``__module__`` resolves normally, so the
    hostile read has to be moved to the clause under test -- see
    ``_plain_finder`` for those.
    """

    class Meta(type):
        def __getattribute__(cls, name):
            if name == "__module__":
                raise raised
            return type.__getattribute__(cls, name)

    class Finder(metaclass=Meta):
        pass

    return Finder()


def _plain_finder(raised):
    """A finder whose ``__module__`` is an ordinary name, so later clauses run.

    A finder that interrupts on ``__module__`` would abort at the ``__module__``
    clause before ``find_spec`` or ``spec.origin`` is ever consulted, so the
    later rows would pass without ever reaching the clause they name.
    """

    class Finder:
        pass

    instance = Finder()
    # Set on the class, not the instance: ``_finder_source`` reads
    # ``type(finder).__module__`` for a non-class finder.
    type(instance).__module__ = "plain_finder_module"
    return instance


@pytest.mark.parametrize("raised", [KeyboardInterrupt(), SystemExit()])
@pytest.mark.parametrize(
    "clause",
    [
        "_finder_module",
        "module __file__",
        "finder __module__",
        "find_spec",
        "spec origin",
    ],
)
def test_an_interrupt_while_identifying_a_finder_still_propagates(monkeypatch, raised, clause):
    """``_finder_source`` must not launder an operator interrupt into a finding.

    ``_finder_module`` re-raises ``KeyboardInterrupt`` and ``SystemExit`` on
    purpose, so an operator interrupt reaches the operator.  The caller threw
    that decision away: every one of its five broad ``except BaseException``
    clauses returned ``None`` instead, so a Ctrl-C arriving while the guard was
    identifying a finder was recorded as an unreadable finder and the run
    continued.  Each clause now re-raises first.

    All five are parameterised because they are five separate ``try`` blocks
    with five separate handlers.  A re-raise added to one of them proves
    nothing about the other four, which is how the defect survived a row that
    exercised only the first.

    The interrupt is raised from a hostile attribute, which is the same
    attacker-controlled surface the surrounding guards exist for -- the
    difference is only whether the value carried is an interrupt or ordinary
    hostile data, and that distinction is exactly what these clauses preserve.
    """

    def interrupt(*args, **kwargs):
        raise raised

    if clause == "_finder_module":
        finder = _interrupting_finder(raised)
        monkeypatch.setattr(origins, "_finder_module", interrupt)

    elif clause == "module __file__":
        finder = _interrupting_finder(raised)
        monkeypatch.setattr(origins, "_finder_module", lambda f: _Interrupting(raised))

    elif clause == "finder __module__":
        finder = _interrupting_finder(raised)
        monkeypatch.setattr(origins, "_finder_module", lambda f: None)

    elif clause == "find_spec":
        finder = _plain_finder(raised)
        monkeypatch.setattr(origins, "_finder_module", lambda f: None)
        monkeypatch.setattr(importlib.util, "find_spec", interrupt)

    else:
        finder = _plain_finder(raised)
        monkeypatch.setattr(origins, "_finder_module", lambda f: None)
        monkeypatch.setattr(importlib.util, "find_spec", lambda *a, **k: _Interrupting(raised))

    with pytest.raises((KeyboardInterrupt, SystemExit)):
        origins._finder_source(finder)


def test_a_hostile_owner_iterable_cannot_abort_the_guard(tmp_path, monkeypatch):
    """The owner list must be materialised inside a fail-closed boundary.

    The previous repair guarded ``distributions.get(...)``, which protected the
    *lookup* but not the value it returns.  ``packages_distributions`` is
    attacker-reachable, so the returned value need not be a list at all:
    iterating it runs its ``__iter__``, and a direct ``BaseException`` subclass
    raised there escaped ``check_origins``.  As in the two rows above, the
    package is planted outside the root so only a withheld allowed root can
    produce the expected ``FAIL``.
    """

    class ExplodingOwners(BaseException):
        pass

    class HostileOwners:
        def __iter__(self):
            raise ExplodingOwners("owners iteration")

    class HostileMapping:
        def get(self, key, default=()):
            return HostileOwners()

    outside = tmp_path / "outside"
    outside.mkdir()
    _make_package(outside, "unenumerable_pkg")
    monkeypatch.syspath_prepend(str(outside))
    for name in list(sys.modules):
        if name == "unenumerable_pkg" or name.startswith("unenumerable_pkg."):
            del sys.modules[name]

    monkeypatch.setattr(origins.importlib.metadata, "packages_distributions", HostileMapping)

    report = check_origins(tmp_path / "checkout", ("unenumerable_pkg",))

    assert report["status"] == "FAIL", report


def test_a_hostile_exception_metaclass_cannot_abort_the_guard(tmp_path, monkeypatch):
    """No finding may read ``type(exc).__name__`` while formatting itself.

    ``_type_name`` was added for exactly this, but the first round used it in
    one place only.  Four other sites interpolated ``type(exc).__name__``
    directly into their detail line -- so an exception class with a metaclass
    that raises on ``__name__`` turned the act of *reporting* a failure into
    the failure itself.  The row asserts the property rather than the sites:
    every detail line the guard builds must survive an exception whose type
    cannot be named.

    ``_resolve_origin`` imports the package it is auditing, so the hostile
    exception is raised by replacing the importer for the duration.
    """

    class HostileMeta(type):
        def __getattribute__(cls, name):
            if name == "__name__":
                raise RuntimeError("hostile exception typename")
            return super().__getattribute__(name)

    class Hostile(BaseException, metaclass=HostileMeta):
        pass

    _make_package(tmp_path, "trap_pkg")
    monkeypatch.syspath_prepend(str(tmp_path))
    for name in list(sys.modules):
        if name == "trap_pkg" or name.startswith("trap_pkg."):
            del sys.modules[name]

    import builtins

    real_import = builtins.__import__

    def hostile_import(name, *args, **kwargs):
        if name == "trap_pkg":
            raise Hostile()
        return real_import(name, *args, **kwargs)

    def run():
        with _import_replaced(hostile_import):
            return check_origins(tmp_path, ("trap_pkg",))

    # Called through a reporting wrapper: when the guard regresses, the
    # escaping ``RuntimeError`` names the hostile metaclass, and pytest's
    # repr of that exception reads the metaclass too -- so a bare
    # ``check_origins`` call here would abort the whole session instead of
    # reporting this one row.
    try:
        report = run()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - see comment above
        report = {"status": f"<escaped {type(exc).__name__}>"}

    assert report["status"] == "FAIL", report


def test_no_detail_line_names_an_exception_type_unsafely():
    """Guard against re-introducing a bare ``type(exc).__name__``.

    The round that added ``_type_name`` fixed one site out of five, which is
    exactly the failure mode a reviewer has to catch by hand.  This row reads
    the module's own source and fails if a ``type(exc).__name__`` reappears, so
    the next person to add an error message gets told instead of shipping
    another escape.  It reads the source rather than the behaviour because the
    behaviour is already covered above; this is the cheap guard on the pattern.
    """

    source = Path(origins.__file__).read_text(encoding="utf-8")

    assert "type(exc).__name__" not in source, (
        "exception type names must go through _type_name(): a hostile exception "
        "class controls its own metaclass"
    )


def test_a_hostile_site_module_attribute_cannot_abort_the_guard(tmp_path, monkeypatch):
    """Reading ``site``'s attributes must not escape.

    Round 4 guarded the two site *getters* but left the attribute reads that
    fetch them unguarded.  An interpreter that replaced ``site`` with a hostile
    ``ModuleType`` subclass controls what ``getattr`` returns, and that lookup
    is a call into its code -- so a direct ``BaseException`` subclass raised
    there escaped before either helper was reached.  The package is planted
    outside the root so only withheld trust can produce the expected ``FAIL``.

    This is the attribute *read*, which is a separate site from the getter
    ``test_a_hostile_site_module_cannot_abort_the_guard`` covers: that row
    installs a module whose ``getsitepackages`` raises, while this one installs
    a module that raises merely when the attribute is looked up.
    """

    class ExplodingAttribute(BaseException):
        pass

    class EvilSite(types.ModuleType):
        def __getattribute__(self, name):
            if name == "getsitepackages":
                raise ExplodingAttribute("site attribute escaped")
            return super().__getattribute__(name)

    outside = tmp_path / "outside"
    outside.mkdir()
    _make_package(outside, "evil_site_pkg")
    monkeypatch.syspath_prepend(str(outside))
    for name in list(sys.modules):
        if name == "evil_site_pkg" or name.startswith("evil_site_pkg."):
            del sys.modules[name]
    monkeypatch.setitem(sys.modules, "site", EvilSite("site"))

    report = check_origins(tmp_path / "checkout", ("evil_site_pkg",))

    assert report["status"] == "FAIL", report


def test_a_hostile_site_path_cannot_abort_the_guard(tmp_path, monkeypatch):
    """A site entry must be interrogated inside a fail-closed boundary.

    This is the half-guard shape again, one level down: guarding the getter
    does not guard the value the getter returns.  The conversion was handled
    for ``(OSError, ValueError, RuntimeError)`` only, and the truth test sat
    outside the ``try`` entirely -- so a returned path-like raising from
    ``__fspath__`` or ``__bool__`` escaped.  A site directory that cannot be
    converted is simply not usable, and an unusable one contributes no root.
    """

    class ExplodingSiteEntry(BaseException):
        pass

    outside = tmp_path / "outside"
    outside.mkdir()
    _make_package(outside, "hostile_site_entry_pkg")
    monkeypatch.syspath_prepend(str(outside))
    for name in list(sys.modules):
        if name == "hostile_site_entry_pkg" or name.startswith("hostile_site_entry_pkg."):
            del sys.modules[name]

    class HostileFspath:
        def __fspath__(self):
            raise ExplodingSiteEntry("site path escaped")

    class HostileTruth:
        def __bool__(self):
            raise ExplodingSiteEntry("site path truth test escaped")

        def __fspath__(self):
            return "/tmp"

    import site as site_module

    for entry in (HostileFspath(), HostileTruth()):
        monkeypatch.setattr(site_module, "getsitepackages", lambda entry=entry: [entry])

        report = check_origins(tmp_path / "checkout", ("hostile_site_entry_pkg",))

        assert report["status"] == "FAIL", report


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

    The trusted direction is demonstrated with the finders this interpreter
    actually has installed, not with a file written into site-packages during
    the test.  Provenance is decided from install records -- a distribution's
    ``RECORD`` hash, or a ``.pth`` in the same directory that imports the
    defining module -- so a source file the test just dropped there is, by
    construction, not something any install vouched for.
    """

    custom = [entry for entry in sys.meta_path if not _is_trusted_stdlib_finder(entry)]
    assert custom, "expected this interpreter to have an installation finder"
    for finder in custom:
        code_file = _finder_code_file(finder)
        assert code_file is not None, (
            f"{finder!r} is installed by this checkout but reports no code file"
        )
        assert code_file.is_file(), f"{code_file} must exist"
        function = getattr(finder.find_spec, "__func__", finder.find_spec)
        assert origins._code_matches_source(function, code_file), (
            "bytecode compiled from this file must be recognised as coming from it"
        )
        assert _is_installation_finder(finder), (
            f"{finder!r} must stay trusted; refusing it fails the release lane"
        )


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


def test_a_same_shape_twin_in_site_packages_cannot_corroborate_a_forged_finder(
    tmp_path, monkeypatch
):
    """Bytecode shape is not provenance when constants differ.

    CPython addresses constants by *index*, so two functions that differ only
    in what their constants are compile to byte-identical ``co_code``.
    Comparing ``co_name``/``co_code``/``co_names``/``co_varnames`` therefore
    lets any real site-packages file that happens to define a same-shape
    ``find_spec`` vouch for a hostile one: the planted twin supplies the
    shape, the constant supplies the foreign path.  Measured on #556, that
    produced ``PASS`` with a foreign submodule loading.

    This is the mutation guard for ``_code_signature``.  Without it the
    hostile finder is corroborated; with it, nothing matches.
    """

    root = tmp_path / "root"
    package_dir = root / "twin_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    outside = tmp_path / "outside"
    foreign = outside / "twin_pkg"
    foreign.mkdir(parents=True)
    (foreign / "leaked.py").write_text('ORIGIN = "foreign"', encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "twin_pkg" or name.startswith("twin_pkg."):
            del sys.modules[name]

    site_root = _site_packages_roots()[0]
    assert site_root.is_dir()
    twin = site_root / "benign_shape_neighbour.py"
    foreign_file = str((foreign / "leaked.py").resolve())
    benign_file = "/nonexistent/benign/target.py"
    shape = (
        "import importlib.util\n"
        "def find_spec(self, fullname, path=None, target=None):\n"
        "    if fullname == 'twin_pkg.leaked':\n"
        "        return importlib.util.spec_from_file_location(fullname, %r)\n"
        "    return None\n"
    )
    # A genuine-looking file whose find_spec has the *same shape* as the
    # hostile one below, differing only in the path constant.
    twin.write_text(shape % benign_file, encoding="utf-8")

    namespace = {"importlib": importlib}
    exec(compile(shape % foreign_file, str(twin), "exec"), namespace)  # noqa: S102
    forged = namespace["find_spec"]
    holder = type("TwinShapeFinder", (), {"find_spec": staticmethod(forged)})()
    holder.__module__ = "benign_shape_neighbour"
    holder.__file__ = str(twin)

    sys.meta_path.insert(0, holder)
    try:
        benign_namespace: dict = {}
        exec(compile(twin.read_text(encoding="utf-8"), str(twin), "exec"), benign_namespace)  # noqa: S102
        benign = benign_namespace["find_spec"]
        assert benign.__code__.co_code == forged.__code__.co_code, (
            "the premise is that co_code is identical for both"
        )
        assert benign.__code__.co_consts != forged.__code__.co_consts, (
            "the premise is that co_consts is what distinguishes them"
        )

        assert not _is_installation_finder(holder), (
            "a same-shape twin must not corroborate a finder serving a different constant"
        )
        report = check_origins(root, ("twin_pkg",))
        assert report["status"] == "FAIL", report
        assert "meta_path" in report["packages"][0]["detail"], report
    finally:
        sys.meta_path.remove(holder)


def test_a_genuine_finder_with_nested_code_still_matches_its_source(tmp_path):
    """Constant comparison recurses, and must not refuse a real installer.

    ``_code_signature`` walks nested code objects so a closure or
    comprehension inside ``find_spec`` is covered.  A real installer written
    that way has to keep its trust, or the extra strictness becomes a false
    red on ordinary editable installs.

    The finder is built outside this checkout on purpose.  This row is about
    the *content* channel -- that a source containing nested code compiles to a
    signature the guard reproduces -- so it exercises
    ``_code_matches_source`` directly.  Load provenance is a separate channel
    with its own rows; testing it here with a file no install ever recorded
    would only re-test the wrong thing.
    """

    genuine = tmp_path / "nested_finder_source.py"
    genuine.write_text(
        "class NestedFinder:\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        names = [name for name in (fullname,) if name]\n"
        "        if not names:\n"
        "            return None\n"
        "        return None\n",
        encoding="utf-8",
    )
    namespace: dict = {}
    exec(compile(genuine.read_text(encoding="utf-8"), str(genuine), "exec"), namespace)  # noqa: S102
    finder = namespace["NestedFinder"]()
    function = getattr(finder.find_spec, "__func__", finder.find_spec)

    assert origins._code_signature(function.__code__), "constants are captured"
    assert origins._code_matches_source(function, genuine), (
        "a source whose find_spec contains nested code must still match itself"
    )


def test_a_nested_code_twin_with_equal_constants_is_still_refused(tmp_path, monkeypatch):
    """The constant signature must cover nested BYTECODE, not just constants.

    A constant-only signature has a residual gap that this row pins shut.  A
    nested body -- a lambda, comprehension or closure -- is itself a code
    object sitting in the parent's ``co_consts`` at a fixed index, so the
    parent's ``co_code`` cannot see what the nested body does.  If the attacker
    writes the nested body so its *constants* stay identical while its
    *bytecode* differs, a signature that recurses only into constants compares
    equal and the hostile finder is corroborated.

    Here the nested lambda is ``_n + ''`` in the planted file and ``'' + _n``
    in the hostile one: same constant ``''``, same result, different nested
    bytecode.  Every field compared at the top level is equal.

    This is the mutation guard for the nested half of ``_code_signature``:
    folding a nested code object's own ``co_code``/``co_names``/``co_varnames``
    into its signature.  Removing that fold makes the signature constants-only
    again, the twin is corroborated, and this row fails (verified: the mutant
    is bypassable end to end, guard PASS with a foreign submodule loading).
    """

    root = tmp_path / "root"
    package_dir = root / "nested_twin_pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    outside = tmp_path / "outside"
    foreign = outside / "nested_twin_pkg"
    foreign.mkdir(parents=True)
    (foreign / "leaked.py").write_text('ORIGIN = "foreign"', encoding="utf-8")
    monkeypatch.syspath_prepend(str(root))
    for name in list(sys.modules):
        if name == "nested_twin_pkg" or name.startswith("nested_twin_pkg."):
            del sys.modules[name]

    site_root = _site_packages_roots()[0]
    assert site_root.is_dir()
    twin = site_root / "nested_twin_neighbour.py"
    shape = (
        "import importlib.util\n"
        "def find_spec(self, fullname, path=None, target=None):\n"
        "    inner = lambda _n: %s\n"
        "    if fullname == 'nested_twin_pkg.leaked':\n"
        "        return importlib.util.spec_from_file_location(\n"
        "            fullname, inner(__import__('os').environ['LEAKED_PATH']))\n"
        "    return None\n"
    )
    twin.write_text(shape % "_n + ''", encoding="utf-8")

    namespace: dict = {}
    # The hostile twin differs ONLY in the nested body; the served path comes
    # from the environment, so it is never a constant in either function.
    exec(compile(shape % "'' + _n", str(twin), "exec"), namespace)  # noqa: S102
    forged = namespace["find_spec"]
    holder = type("NestedTwinFinder", (), {"find_spec": staticmethod(forged)})()
    holder.__module__ = "nested_twin_neighbour"
    holder.__file__ = str(twin)

    monkeypatch.setenv("LEAKED_PATH", str((foreign / "leaked.py").resolve()))
    sys.meta_path.insert(0, holder)
    try:
        genuine_namespace: dict = {}
        exec(compile(twin.read_text(encoding="utf-8"), str(twin), "exec"), genuine_namespace)  # noqa: S102
        genuine = genuine_namespace["find_spec"]

        # Pin the premise: the top-level fields #556 compares are all equal,
        # and so is the nested constant table. Only the nested bytecode differs.
        assert forged.__code__.co_code == genuine.__code__.co_code
        assert forged.__code__.co_names == genuine.__code__.co_names
        assert forged.__code__.co_varnames == genuine.__code__.co_varnames

        # Every field the signature reads *besides* the nested recursion is
        # equal here, so the whole decision rests on recursing into the nested
        # code object. Assert that directly: comparing only the nested
        # constant tables would NOT distinguish this pair, which is the entire
        # gap #557 left open.
        def _nested_constant_tables(code: object) -> tuple:
            return tuple(
                tuple(
                    (type(item).__name__, repr(item)) for item in getattr(constant, "co_consts", ())
                )
                for constant in getattr(code, "co_consts", ())
                if isinstance(constant, types.CodeType)
            )

        assert _nested_constant_tables(forged.__code__) == _nested_constant_tables(
            genuine.__code__
        ), "the premise: nested constants are identical, only bytecode differs"
        assert origins._code_signature(forged.__code__) != origins._code_signature(
            genuine.__code__
        ), "the signature must distinguish a nested body that differs"
        for field in ("co_flags", "co_argcount", "co_nlocals", "co_freevars"):
            assert getattr(forged.__code__, field) == getattr(genuine.__code__, field), (
                f"the premise: {field} is equal on both"
            )

        assert not _is_installation_finder(holder), (
            "a nested-code twin with equal constants must not corroborate"
        )
        report = check_origins(root, ("nested_twin_pkg",))
        assert report["status"] == "FAIL", report
        assert "meta_path" in report["packages"][0]["detail"], report
    finally:
        sys.meta_path.remove(holder)
        try:
            twin.unlink()
        except OSError:
            pass


def test_a_genuine_latin1_finder_is_still_corroborated():
    """Decoding must follow PEP 263, or a real finder is refused.

    Reading the named file as UTF-8 raises ``UnicodeDecodeError`` on a genuine
    module that declares a latin-1 encoding cookie and holds non-ASCII bytes.
    The corroboration step catches every exception and refuses, so the finder
    lost its trust for a reason that has nothing to do with provenance.  That is
    a false FAIL on the supported install lane, which is the wrong direction to
    fail: the guard would report an honest install as untrusted.

    This row pins the fix.  ``tokenize.open`` is the import system's own
    decoder, so the cookie is honoured and the recompiled code matches the
    running one.
    """

    site_root = _site_packages_roots()[0]
    assert site_root.is_dir()
    genuine = site_root / "genuine_latin1_finder_row.py"
    source = (
        "# -*- coding: latin-1 -*-\n"
        "def find_spec(self, fullname, path=None, target=None):\n"
        "    tag = 'café'\n"
        "    return None\n"
    )
    genuine.write_bytes(source.encode("latin-1"))
    try:
        with tokenize.open(genuine) as handle:
            decoded = handle.read()
        namespace: dict = {}
        exec(compile(decoded, str(genuine), "exec", dont_inherit=True), namespace)  # noqa: S102
        function = namespace["find_spec"]

        # The premise: plain UTF-8 reading cannot decode this module at all.
        with pytest.raises(UnicodeDecodeError):
            genuine.read_text(encoding="utf-8")

        assert origins._code_matches_source(function, genuine), (
            "a genuine latin-1 finder must still corroborate against its source"
        )
    finally:
        genuine.unlink()


def test_a_foreign_path_portion_is_reported_even_when_file_changes_between_reads(
    tmp_path, monkeypatch
):
    """``__file__`` is read once per check, and a second read cannot skip it.

    ``__file__`` is mutable interpreter state, and a hostile path-like can
    answer differently on each read.  The origin was resolved from one read and
    the portion check used to take a second: a first read matching the origin
    followed by one that did not made the guard skip the portion check entirely,
    so a package with a genuinely foreign ``__path__`` reported PASS.

    Measured on this branch before the fix, with the suite green at 121
    passed:

        status         : PASS
        __file__ reads : 2

    The caller now passes the module whose ``__file__`` it already resolved, so
    the value is read exactly once and the drifting answer cannot be used to
    skip the check.

    This is the mutation guard for that plumbing: removing the ``module``
    argument, or ignoring it in the function, restores the second read and this
    row reports PASS again.
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


def test_source_copied_into_site_packages_does_not_certify_an_execed_finder(tmp_path, monkeypatch):
    """Matching bytes in a site-packages file are not load provenance.

    The content check asks whether the finder's code is *in* the file it names.
    That is necessary, but it is not the claim the guard needs: the running
    finder must have been *loaded from* that file.  Writing the finder's own
    source into site-packages and then running
    ``exec(compile(source, that_path, "exec"))`` satisfies every content check
    while the finder has no provenance there at all.

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
        assert origins._code_matches_source(forged.find_spec, Path(planted)), (
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
        if forged is not None and forged in sys.meta_path:
            sys.meta_path.remove(forged)
        planted.unlink(missing_ok=True)


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
        if forged is not None and forged in sys.meta_path:
            sys.meta_path.remove(forged)
        planted.unlink(missing_ok=True)


def test_a_recorded_file_stops_being_recorded_when_its_bytes_change():
    """``RECORD`` is provenance only while the bytes still hash to what it says.

    The disk-recorded channel is what replaces the forgeable in-memory one, so
    the part of it that actually carries the claim is the *hash*: a file being
    listed in a ``RECORD`` says only that some install laid down that path, not
    that the bytes now there are the ones it laid down.

    Treating mere presence in ``RECORD`` as provenance was measured to survive
    its own mutation: replacing the digest comparison with a membership test
    left every row in the suite green.  This row pins the difference.
    """

    root = _site_packages_roots()[0]
    dist_info = root / "pokemon_hash_pin_row.dist-info"
    dist_info.mkdir(exist_ok=True)
    planted = root / "pokemon_hash_pin_row_module.py"
    try:
        planted.write_text("VALUE = 'original'\n", encoding="utf-8")
        recorded = dist_info / "RECORD"
        digest = _file_digest(planted)
        assert digest is not None, "the planted file must be readable"
        recorded.write_text(f"pokemon_hash_pin_row_module.py,sha256={digest},6\n", encoding="utf-8")

        assert _is_recorded_by_an_install(planted, root), (
            "a file whose bytes match its RECORD entry is install-recorded"
        )

        planted.write_text("VALUE = 'tampered'\n", encoding="utf-8")
        assert not _is_recorded_by_an_install(planted, root), (
            "the same path with different bytes is no longer what the install wrote"
        )
    finally:
        sys.modules.pop("pokemon_hash_pin_row_module", None)
        for disposable in (planted, dist_info / "RECORD"):
            disposable.unlink(missing_ok=True)
        dist_info.rmdir()


@pytest.mark.parametrize("algorithm", ["sha512", "sha384", "blake2b", "md5"])
def test_a_record_naming_a_non_sha256_algorithm_still_establishes_provenance(algorithm):
    """``RECORD`` permits any algorithm ``hashlib`` guarantees, so honour it.

    The guard used to skip every row whose label was not ``sha256``, which
    meant ``_record_digests`` found no claim for the file and
    ``_is_recorded_by_an_install`` refused it.  Wheel may legitimately ship a
    SHA-512 ``RECORD``, so that is a false refusal of a genuine install: the
    origin is well attested and the guard reports it as unproven.

    The label is honoured rather than trusted, so this is wider than believing
    whatever the record says and narrower than discarding the row: the digest
    is recomputed under the named algorithm and must match.
    """

    root = _site_packages_roots()[0]
    dist_info = root / "pokemon_record_algo_row.dist-info"
    dist_info.mkdir(exist_ok=True)
    planted = root / "pokemon_record_algo_row_module.py"
    try:
        planted.write_text("VALUE = 'original'\n", encoding="utf-8")
        digest = _file_digest(planted, algorithm)
        assert digest is not None, f"the planted file must be readable as {algorithm}"
        recorded = dist_info / "RECORD"
        recorded.write_text(
            f"pokemon_record_algo_row_module.py,{algorithm}={digest},{planted.stat().st_size}\n",
            encoding="utf-8",
        )

        assert _is_recorded_by_an_install(planted, root), (
            f"a genuine {algorithm} RECORD must establish provenance, not refuse it"
        )

        planted.write_text("VALUE = 'tampered'\n", encoding="utf-8")
        assert not _is_recorded_by_an_install(planted, root), (
            f"a {algorithm} record must still pin the bytes it claims"
        )
    finally:
        sys.modules.pop("pokemon_record_algo_row_module", None)
        for disposable in (planted, dist_info / "RECORD"):
            disposable.unlink(missing_ok=True)
        dist_info.rmdir()


@pytest.mark.parametrize(
    ("algorithm", "output_length"), [("shake_128", 16), ("shake_128", 32), ("shake_256", 64)]
)
def test_a_record_naming_a_variable_length_algorithm_still_establishes_provenance(
    algorithm, output_length
):
    """SHAKE is the one ``RECORD`` algorithm that needs an output length.

    ``shake_128`` and ``shake_256`` are extendable-output functions, so
    ``hashlib`` guarantees them and ``RECORD`` may name either -- but their
    ``digest()`` takes a required length where every fixed-size algorithm takes
    none.  Calling it bare raises ``TypeError``, which the guard's own
    ``except`` turns into ``None``: the file then has no claim and is refused.

    That is precisely the false refusal of a genuine install that honouring the
    label exists to remove, so the record's own digest length is what the file
    is hashed at.  Honouring the label is still conditional on recomputing it:
    a mangled digest, and a record whose length disagrees with the one it
    claims, are both still refused.
    """

    root = _site_packages_roots()[0]
    dist_info = root / "pokemon_record_shake_row.dist-info"
    dist_info.mkdir(exist_ok=True)
    planted = root / "pokemon_record_shake_row_module.py"
    try:
        planted.write_text("VALUE = 'original'\n", encoding="utf-8")
        data = planted.read_bytes()
        hasher = hashlib.new(algorithm)
        hasher.update(data)
        digest = base64.urlsafe_b64encode(hasher.digest(output_length)).rstrip(b"=").decode()
        assert len(base64.urlsafe_b64decode(digest + "=" * (-len(digest) % 4))) == output_length, (
            "the premise: the record carries a digest of exactly this length"
        )
        recorded = dist_info / "RECORD"
        recorded.write_text(
            f"pokemon_record_shake_row_module.py,{algorithm}={digest},{planted.stat().st_size}\n",
            encoding="utf-8",
        )

        assert _is_recorded_by_an_install(planted, root), (
            f"a genuine {algorithm} RECORD must establish provenance at its own length"
        )

        # A record whose digest is the right algorithm at the right length but
        # the wrong *bytes* is still refused: honouring the label never means
        # believing it, only recomputing under it.
        flipped = "B" if digest[0] != "B" else "C"
        other_digest = flipped + digest[1:]
        recorded.write_text(
            f"pokemon_record_shake_row_module.py,{algorithm}={other_digest},"
            f"{planted.stat().st_size}\n",
            encoding="utf-8",
        )
        assert not _is_recorded_by_an_install(planted, root), (
            "a record whose digest does not match the bytes must be refused"
        )
    finally:
        sys.modules.pop("pokemon_record_shake_row_module", None)
        for disposable in (planted, dist_info / "RECORD"):
            disposable.unlink(missing_ok=True)
        dist_info.rmdir()


@pytest.mark.parametrize("algorithm", ["shake_128", "sha256"])
def test_a_record_whose_digest_length_is_absurd_is_refused_without_allocating_it(algorithm):
    """A ``RECORD`` may not choose the size of an allocation the guard makes.

    The digest's own length says how large a ``shake_128`` output was, so the
    guard has to read that length before hashing.  Reading it by *decoding* the
    claim is the trap: a planted row naming a gigabyte of output would make the
    decode allocate a gigabyte before anything was compared, turning a refused
    file into an out-of-memory failure.

    This row claims far more output than any digest can be, and requires the
    guard to refuse it.  The bound is textual, so it costs nothing to apply and
    nothing to exceed with a legitimate record.
    """

    root = _site_packages_roots()[0]
    dist_info = root / "pokemon_record_huge_row.dist-info"
    dist_info.mkdir(exist_ok=True)
    planted = root / "pokemon_record_huge_row_module.py"
    try:
        planted.write_text("VALUE = 'original'\n", encoding="utf-8")
        absurd = "A" * 2_000_000
        recorded = dist_info / "RECORD"
        recorded.write_text(
            f"pokemon_record_huge_row_module.py,{algorithm}={absurd},{planted.stat().st_size}\n",
            encoding="utf-8",
        )

        assert not _is_recorded_by_an_install(planted, root), (
            "a record claiming an impossible digest must attest nothing"
        )
    finally:
        sys.modules.pop("pokemon_record_huge_row_module", None)
        for disposable in (planted, dist_info / "RECORD"):
            disposable.unlink(missing_ok=True)
        dist_info.rmdir()


def test_a_record_whose_label_does_not_match_its_digest_is_refused():
    """The algorithm label is part of the claim, so a mismatch must fail.

    Honouring the label is only safe because the digest is recomputed under
    it.  A row reading ``sha512=<a sha256 digest>`` is therefore not evidence
    of anything: trusting the label without recomputing would attest it, and
    discarding labelled rows would reintroduce the false refusal above.
    """

    root = _site_packages_roots()[0]
    dist_info = root / "pokemon_record_mislabel_row.dist-info"
    dist_info.mkdir(exist_ok=True)
    planted = root / "pokemon_record_mislabel_row_module.py"
    try:
        planted.write_text("VALUE = 'original'\n", encoding="utf-8")
        sha256_digest = _file_digest(planted, "sha256")
        assert sha256_digest is not None
        recorded = dist_info / "RECORD"
        recorded.write_text(
            f"pokemon_record_mislabel_row_module.py,sha512={sha256_digest},"
            f"{planted.stat().st_size}\n",
            encoding="utf-8",
        )

        assert not _is_recorded_by_an_install(planted, root), (
            "a sha256 digest labelled sha512 is not a claim about these bytes"
        )
    finally:
        sys.modules.pop("pokemon_record_mislabel_row_module", None)
        for disposable in (planted, dist_info / "RECORD"):
            disposable.unlink(missing_ok=True)
        dist_info.rmdir()


def test_a_record_naming_an_unknown_algorithm_attests_nothing():
    """A label this interpreter cannot compute must fail the whole set.

    Falling back to a default algorithm would turn an unverifiable claim into
    a passing one, which is exactly the fail-open direction the guard exists to
    prevent.
    """

    root = _site_packages_roots()[0]
    dist_info = root / "pokemon_record_unknown_algo_row.dist-info"
    dist_info.mkdir(exist_ok=True)
    planted = root / "pokemon_record_unknown_algo_row_module.py"
    try:
        planted.write_text("VALUE = 'original'\n", encoding="utf-8")
        recorded = dist_info / "RECORD"
        recorded.write_text(
            f"pokemon_record_unknown_algo_row_module.py,notarealalgo=AAAA,"
            f"{planted.stat().st_size}\n",
            encoding="utf-8",
        )

        assert _file_digest(planted, "notarealalgo") is None, (
            "the premise: an unknown algorithm has no digest to compare"
        )
        assert not _is_recorded_by_an_install(planted, root), (
            "a claim this interpreter cannot check must not certify the file"
        )
    finally:
        sys.modules.pop("pokemon_record_unknown_algo_row_module", None)
        for disposable in (planted, dist_info / "RECORD"):
            disposable.unlink(missing_ok=True)
        dist_info.rmdir()


def test_a_quoted_record_path_containing_a_comma_still_establishes_provenance():
    """``RECORD`` is CSV, and a quoted field's comma must not truncate its name.

    The file name is parsed by splitting from the right -- size, then digest,
    then everything remaining is the name -- because a name may legally contain
    a comma.  But pip writes such a field *quoted*, so a name that survives the
    split still arrives wrapped in quotes, and joining the raw text onto the
    site-packages root builds a path no record ever listed.  The digest is then
    compared against a file that does not exist, so a genuinely installed
    finder whose location contains a comma silently loses its provenance and is
    refused: a false red, in exchange for no security gain.

    This row pins the decode.  The writer here is ``csv.writer``, the same one
    pip uses, so the bytes under test are bytes pip really emits.
    """

    root = _site_packages_roots()[0]
    dist_info = root / "pokemon_csv_path_row.dist-info"
    dist_info.mkdir(exist_ok=True)
    planted = root / "pokemon,comma_row_module.py"
    try:
        planted.write_text("VALUE = 'original'\n", encoding="utf-8")
        digest = _file_digest(planted)
        assert digest is not None
        buffer = io.StringIO()
        csv.writer(buffer, lineterminator="\n").writerow(
            [planted.name, f"sha256={digest}", str(planted.stat().st_size)]
        )
        (dist_info / "RECORD").write_text(buffer.getvalue(), encoding="utf-8")

        assert '"' in (dist_info / "RECORD").read_text(encoding="utf-8"), (
            "the premise: a comma in the name forces a quoted CSV field"
        )
        assert _is_recorded_by_an_install(planted, root), (
            "an installed file whose name contains a comma keeps its provenance"
        )
    finally:
        sys.modules.pop(planted.stem, None)
        for disposable in (planted, dist_info / "RECORD"):
            disposable.unlink(missing_ok=True)
        dist_info.rmdir()


def test_a_recorded_path_that_another_record_contradicts_is_not_attested():
    """Every ``RECORD`` claim about a path must hold, not just the first one.

    ``_record_digests`` maps a path to the digests every record claims for it.
    Keeping only one claim would let an attacker write a record that agrees
    with their own bytes and rely on ordering to win against the install that
    actually laid the file down.  A file whose installs disagree about its
    contents is not consistently attested by any of them, and the safe answer
    is to refuse.

    This row kills a mutant that keeps whichever claim sorts *last*, and pins
    the collection of both claims before the decision is made.  It cannot kill
    a keep-the-first mutant, and that is a property of the rule rather than a
    gap in the row: on a genuine conflict the first-claim mutant also refuses
    here.  Distinguishing those two would need a case where the first claim
    *agrees* with the file and a later one does not, which is the far more
    dangerous ordering and is what the ``forgery`` below deliberately is not.
    """

    root = _site_packages_roots()[0]
    dist_info = root / "pokemon_conflict_row.dist-info"
    dist_info.mkdir(exist_ok=True)
    planted = root / "pokemon_conflict_row_module.py"
    try:
        planted.write_text("VALUE = 'original'\n", encoding="utf-8")
        digest = _file_digest(planted)
        assert digest is not None
        record = dist_info / "RECORD"
        buffer = io.StringIO()
        csv.writer(buffer, lineterminator="\n").writerow(
            [planted.name, f"sha256={digest}", str(planted.stat().st_size)]
        )
        record.write_text(buffer.getvalue(), encoding="utf-8")
        assert _is_recorded_by_an_install(planted, root), "the premise: attested"

        # A second, disagreeing record for the same path.  Its digest is chosen
        # to sort *before* the real one, so a keep-only-the-first-claim mutation
        # would pick the forgery and this row would wrongly survive.
        forgery = "0" * 43 + "="
        assert forgery < digest, "the forgery must sort first to isolate the rule"
        with open(record, "a", encoding="utf-8") as handle:
            handle.write(f"{planted.name},sha256={forgery},{planted.stat().st_size}\n")

        # Assert what the rule actually decides, not just the refusal: a
        # keep-only-the-first-claim mutant also refuses here, so asserting only
        # "not recorded" would let that mutant survive this row unchallenged.
        # The premise already established that the real digest *is* claimed,
        # so "one claim matches and one does not" is the state under test.
        claims = _record_digests(root).get(str(planted))
        assert claims is not None and len(claims) == 2, (
            f"the premise: two records disagree about this path, got {claims}"
        )
        assert ("sha256", digest) in claims and ("sha256", forgery) in claims, (
            "both claims must be collected before the decision is made"
        )
        assert not _is_recorded_by_an_install(planted, root), (
            "installs that disagree about a file's bytes attest to nothing"
        )
    finally:
        sys.modules.pop(planted.stem, None)
        for disposable in (planted, dist_info / "RECORD"):
            disposable.unlink(missing_ok=True)
        dist_info.rmdir()


def test_the_guard_refuses_rather_than_traceback_on_any_escape(tmp_path, monkeypatch):
    """``check_origins`` must always answer, even for a read nobody guarded.

    Thirteen review rounds across #558 and #559 each widened one more
    hostile read, and several of them repaired an *adjacent* read rather
    than the reported one.  Widening read N therefore never proved read N+1
    was safe, and this module cannot enumerate every value an attacker
    controls: ``sys``, ``site``, ``sys.modules``, the meta-path and the
    on-disk install layout all sit outside the process's own control.

    So the entry point carries the guarantee instead: anything escaping the
    analysis becomes the same machine-readable FAIL every other refusal
    produces.  A guard that answers with a traceback is fail-open, because a
    caller gating on ``status`` sees nothing at all.

    ``KeyboardInterrupt`` and ``SystemExit`` must still escape, so an
    operator can always stop the run.
    """

    class Exploding(BaseException):
        pass

    def explode(*_args, **_kwargs):
        raise Exploding("novel read boom")

    monkeypatch.setattr(origins, "_allowed_roots", explode)

    report = check_origins(tmp_path, ("anything",))

    assert report["status"] == "FAIL", report
    assert report["packages"][0]["package"] == "<guard>"
    assert "could not complete" in report["packages"][0]["detail"]

    def raise_interrupt(*_args, **_kwargs):
        raise interrupt

    for interrupt in (KeyboardInterrupt(), SystemExit()):
        monkeypatch.setattr(origins, "_allowed_roots", raise_interrupt)
        with pytest.raises(type(interrupt)):
            check_origins(tmp_path, ("anything",))
