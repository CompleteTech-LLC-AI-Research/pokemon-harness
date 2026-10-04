"""Acceptance tests for the #534 import-origin guard."""

import importlib
import importlib.util
import json
import sys
import tokenize
import types
from pathlib import Path

import pytest

import scripts.check_import_origins as origins
from scripts.check_import_origins import (
    _finder_code_file,
    _finder_source,
    _is_installation_finder,
    _is_trusted_stdlib_finder,
    _is_within,
    _site_packages_roots,
    check_origins,
)


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
