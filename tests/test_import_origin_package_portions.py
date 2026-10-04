"""Acceptance tests for the #534 import-origin guard."""

import importlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

import scripts.check_import_origins as origins
from scripts.check_import_origins import check_origins, main

REPO_ROOT = Path(__file__).resolve().parent.parent


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
