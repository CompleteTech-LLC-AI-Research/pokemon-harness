"""Acceptance tests for the #534 import-origin guard."""

import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

from scripts.check_import_origins import (
    _file_digest,
    _is_imported_by_a_pth,
    _is_installation_finder,
    _is_recorded_by_an_install,
    _is_trusted_stdlib_finder,
    _site_packages_roots,
    check_origins,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


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
