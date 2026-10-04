"""Acceptance tests for the #534 import-origin guard."""

import json
import sys
import types
from pathlib import Path

import pytest

import scripts.check_import_origins as origins
from scripts.check_import_origins import check_origins, main
from tests._import_origin_test_support import _import_replaced, _make_package, _module_installed

REPO_ROOT = Path(__file__).resolve().parent.parent


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
