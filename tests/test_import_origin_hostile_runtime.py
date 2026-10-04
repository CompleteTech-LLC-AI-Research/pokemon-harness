"""Acceptance tests for the #534 import-origin guard."""

import importlib
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

import pytest

import scripts.check_import_origins as origins
from scripts.check_import_origins import _describe, check_origins
from tests._import_origin_test_support import _import_replaced, _make_package


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
    """Protect the facade and each extracted helper from bare exception names.

    The same scan predicate checks a disposable mutated helper copy so the
    negative control covers implementation files outside the facade.
    """

    helper_modules = (
        origins._import_origin_attestations,
        origins._import_origin_finders,
        origins._import_origin_paths,
        origins._import_origin_resolution,
    )
    scanned_helper_names = {module.__name__.rsplit(".", 1)[-1] for module in helper_modules}
    available_helper_names = {
        name
        for name, value in vars(origins).items()
        if name.startswith("_import_origin_") and isinstance(value, types.ModuleType)
    }
    assert available_helper_names == scanned_helper_names
    source_paths = [
        Path(origins.__file__),
        *(Path(module.__file__) for module in helper_modules),
    ]

    def unsafe_type_name_sources(paths):
        return [path for path in paths if "type(exc).__name__" in path.read_text(encoding="utf-8")]

    assert unsafe_type_name_sources(source_paths) == [], (
        "exception type names must go through _type_name(): a hostile exception "
        "class controls its own metaclass"
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        mutated_helper = Path(temporary_directory) / "_import_origin_mutated.py"
        mutated_helper.write_text("type(exc).__name__\n", encoding="utf-8")
        assert unsafe_type_name_sources([*source_paths, mutated_helper]) == [mutated_helper]


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
