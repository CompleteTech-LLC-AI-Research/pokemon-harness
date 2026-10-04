"""Finders helpers for the import-origin guard.

The facade namespace is passed for calls through mutable seams, keeping
monkeypatch behavior and provenance decisions live at the public boundary.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import sys
import tokenize
import types
from pathlib import Path
from typing import Any

OriginApi = dict[str, Any]


def _trusted_stdlib_finders(*, api: OriginApi) -> set[object]:
    """Return the import machinery finders that ship with the interpreter.

    Identity, not name, is the test: these are the exact objects CPython
    installs on a fresh ``sys.meta_path``, and nothing a site-packages
    package can define can impersonate them.
    """

    import importlib._bootstrap as bootstrap
    import importlib._bootstrap_external as bootstrap_external

    return {
        bootstrap.BuiltinImporter,
        bootstrap.FrozenImporter,
        bootstrap_external.PathFinder,
    }


def _is_trusted_stdlib_finder(finder: object, *, api: OriginApi) -> bool:
    """Return whether ``finder`` is one of the interpreter's own finders.

    A set membership test calls ``__hash__`` and ``__eq__`` on the object
    being looked up, and both are supplied by the finder itself.  The lookup
    is therefore attacker-controlled data reached before any of the guards
    around the caller's own attribute reads, so it has to be guarded here.

    Identity alone is not enough, and this is the third round of that lesson.
    A ``sys.meta_path`` entry is a mutable class in a live process: rebinding
    ``PathFinder.find_spec`` keeps the very same object while replacing the
    code that runs.  Every check below then reads PASS while the rebound
    finder serves submodules from anywhere, and the guard certifies an
    interpreter it has just been shown to be compromised.  So the interpreter's
    own finders are additionally required to be *frozen* -- the three classes
    below are defined in ``_frozen_importlib``, so the code that executes
    cannot have been reassigned in this process, and a rebound one reports
    wherever the attacker compiled it instead.

    An exception that is not an operator interrupt means this finder is not
    one of the interpreter's own, which is the same answer an identity
    mismatch gives: untrusted by default.
    """

    try:
        if finder not in api["_trusted_stdlib_finders"]():
            return False
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - untrusted data, see docstring
        return False
    return api["_is_unmodified"](finder)


def _is_unmodified(finder: object, *, api: OriginApi) -> bool:
    """Return whether ``finder``'s ``find_spec`` is the interpreter's own code.

    ``finder`` has already passed the identity check, so this only has to
    answer whether the callable that will run is still the one the interpreter
    shipped.  A genuine finder is defined in a frozen module and reports a
    ``<frozen ...>`` code file; code an attacker compiled and assigned in this
    process reports the path it was compiled under.

    Every read is guarded: ``find_spec`` is an attribute on a class the caller
    supplied, so reaching for it is a call into code the caller controls.
    """

    try:
        method = getattr(finder, "find_spec", None)
        function = getattr(method, "__func__", method)
        code = getattr(function, "__code__", None)
        code_file = getattr(code, "co_filename", None)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - untrusted data, see docstring
        return False
    return isinstance(code_file, str) and code_file.startswith("<frozen ")


def _finder_module(finder: object, *, api: OriginApi) -> object | None:
    """Return the module object that defines ``finder``, if it is loaded."""

    try:
        name = finder.__module__ if isinstance(finder, type) else type(finder).__module__
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - hostile metaclass; untrusted by default
        return None
    if not isinstance(name, str):
        return None
    return sys.modules.get(name)


def _finder_source(finder: object, *, api: OriginApi) -> Path | None:
    """Return the file that defines ``finder``, resolved without importing.

    A finder class is frequently installed before its module is executed --
    setuptools writes a stub that puts the class straight onto
    ``sys.meta_path`` -- so the defining module is often absent from
    ``sys.modules``.  ``find_spec`` locates it without executing the guarded
    packages.

    Every lookup is defensive because the value being inspected is attacker-
    controlled: a hostile metaclass can raise from ``__module__`` or a module
    can raise from ``__file__``.  A finder that cannot be located is not
    trusted, so an exception here must resolve to ``None`` rather than escape.

    Every clause below re-raises ``KeyboardInterrupt`` and ``SystemExit``
    ahead of the broad handler, and that is not decoration.  ``_finder_module``
    already re-raises them precisely so an operator interrupt reaches the
    operator; catching its result here and returning ``None`` discarded that
    decision, so a Ctrl-C arriving while the guard was identifying a finder was
    recorded as an unreadable finder instead.  The rule is the same at each
    site: hostile input is refused, an interrupt is not.
    """

    try:
        module = api["_finder_module"](finder)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - hostile metaclass; untrusted by default
        return None
    if module is not None:
        try:
            origin = getattr(module, "__file__", None)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:  # noqa: BLE001 - hostile module; fall through to find_spec
            origin = None
        if isinstance(origin, str) and origin:
            return api["_safe_resolve"](Path(origin))
    try:
        name = finder.__module__ if isinstance(finder, type) else type(finder).__module__
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - hostile metaclass; untrusted by default
        return None
    if not isinstance(name, str) or not name:
        return None
    try:
        spec = importlib.util.find_spec(name)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - a hostile import hook must not abort the guard
        return None
    try:
        origin = getattr(spec, "origin", None) if spec is not None else None
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - hostile spec object
        return None
    if not isinstance(origin, str) or origin in ("built-in", "frozen", "namespace"):
        return None
    return api["_safe_resolve"](Path(origin))


def _code_objects(code: object, *, api: OriginApi):
    """Yield ``code`` and every code object nested inside its constants."""

    yield code
    try:
        constants = getattr(code, "co_consts", ())
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - hostile code object; nothing to descend
        return
    for constant in constants:
        if isinstance(constant, types.CodeType):
            yield from api["_code_objects"](constant)


def _code_signature(code: object, *, api: OriginApi) -> tuple:
    """Return a value-comparable signature for a whole code object.

    Values are represented by type and ``repr`` rather than compared directly:
    some constants (an open file, a module) are not equal to themselves
    across two compilations, and an equality test on those would refuse genuine
    finders for no good reason.

    The signature carries every field that can change what the code *does*, not
    only the instruction stream.  Two functions can share ``co_code`` while
    differing in ``co_consts``, because CPython addresses constants by index,
    so the constant table is load-bearing rather than belt-and-braces.  The
    same argument applies to the remaining fields: ``co_flags`` separates a
    coroutine from a generator, ``co_argcount``/``co_kwonlyargcount``/
    ``co_nlocals``/``co_cellvars`` fix how the frame is built and which locals
    exist, and ``co_freevars`` names the closure cells the body reads.

    A nested code object is summarised by its own signature, and recursion is
    what makes that work at all, because code objects compare by identity and a
    recompiled copy is never the same object even for identical source.  The
    nesting is load-bearing rather than decorative: a nested body -- a lambda,
    comprehension or closure -- is itself a code object sitting in the parent's
    ``co_consts`` at a fixed index, so the parent's ``co_code`` cannot see what
    the nested body does.  A signature that recursed into the nested
    *constants* alone would still compare two functions equal whenever the
    attacker keeps the constants identical while changing what the nested body
    computes (``_n + ''`` against ``'' + _n``).  Recursing on the whole nested
    signature closes that, to any depth rather than only one level.

    ``co_firstlineno``, ``co_linetable`` and ``co_qualname`` are deliberately
    left out: they record where the source sat and what the author called the
    object, not what it does, and comparing them would refuse a genuine finder
    over a difference that cannot change behaviour.
    """

    items = []
    try:
        for constant in getattr(code, "co_consts", ()):
            if isinstance(constant, types.CodeType):
                items.append(
                    (
                        constant.co_name,
                        constant.co_code,
                        constant.co_names,
                        constant.co_varnames,
                        constant.co_flags,
                        constant.co_argcount,
                        constant.co_posonlyargcount,
                        constant.co_kwonlyargcount,
                        constant.co_nlocals,
                        constant.co_freevars,
                        constant.co_cellvars,
                        api["_code_signature"](constant),
                    )
                )
            else:
                # This site reads ``type(constant).__name__`` directly rather
                # than through ``_type_name``, unlike the finding-rendering
                # paths.  It is safe anyway, and deliberately left alone:
                # the whole loop sits inside this function's ``except
                # BaseException`` boundary, so a hostile metaclass here
                # refuses the *signature* -- it returns a fresh object that
                # compares equal only to itself, so the code cannot match.
                # Swapping in ``_describe`` would look tidier and would be a
                # real behaviour change: ``repr`` and ``str`` differ for
                # strings, and these tuples are compared for equality
                # against a recompiled copy of the same function.  Making the
                # rendering "safer" without need would risk refusing genuine
                # finders, which is the worse failure here.
                items.append((type(constant).__name__, repr(constant)))
        return (
            tuple(items),
            getattr(code, "co_flags", None),
            getattr(code, "co_argcount", None),
            getattr(code, "co_posonlyargcount", None),
            getattr(code, "co_kwonlyargcount", None),
            getattr(code, "co_nlocals", None),
            getattr(code, "co_freevars", None),
            getattr(code, "co_cellvars", None),
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - hostile code object; refuse to match it
        # A fresh object each call: it compares equal only to itself, so an
        # unreadable code object can never match another one -- not even a
        # second unreadable one, which a shared sentinel would have matched.
        return api["_unreadable_signature"]()


def _unreadable_signature(*, api: OriginApi) -> object:
    """Return a signature value that cannot equal any other signature."""

    return object()


def _code_matches_source(function: object, source_file: Path, *, api: OriginApi) -> bool:
    """Return whether ``function``'s bytecode really occurs in ``source_file``.

    ``compile`` accepts the filename it records, so ``co_filename`` on its own
    is a *claim* by whoever called ``compile``, not a compiler attestation: a
    hostile finder can be compiled under the name of any file that exists and
    will carry that name forever.  Location therefore cannot carry provenance
    by itself.

    This is the second, independent channel.  The source is read back from
    disk and compiled again; only a finder whose actual bytecode is produced by
    that file can match.  A forged ``co_filename`` points at a real file whose
    source does not contain the hostile bytecode, so nothing matches and the
    finder is refused.  Genuine install finders -- the editable-install shim
    and the virtualenv helper -- do match, because their bytecode really does
    come from the file they name.

    Constants are compared as well as the instruction stream, and that is
    load-bearing rather than belt-and-braces.  CPython addresses constants by
    *index*, not by value, so ``co_code`` is byte-identical for two functions
    that differ only in what their constants are.  Comparing
    ``co_name``/``co_code``/``co_names``/``co_varnames`` alone therefore lets a
    hostile ``find_spec`` be corroborated by any real site-packages file that
    happens to define a same-shape ``find_spec`` -- the planted twin supplies
    the bytecode shape while the constant supplies the foreign path.  That was
    measured as a full false PASS, so the constant tuple is part of the match.

    Residual boundary: this establishes that the executing code was compiled
    from that file's source, not that the file is benign.  A hostile finder
    written into site-packages in the first place satisfies both channels and
    is out of scope -- as is code that runs before the guard does.
    """

    try:
        target = function.__code__
        # Decode the way the interpreter itself does, via PEP 263, so an
        # encoding cookie is honoured.  Reading as UTF-8 raised
        # UnicodeDecodeError on a genuine latin-1 module, and the blanket
        # ``except`` below turned that into a refusal -- a false FAIL on a
        # real installation finder, not the safe direction for a trust
        # decision.  ``dont_inherit`` matters for the same reason: this module
        # uses ``from __future__ import annotations``, and inheriting that flag
        # would stamp CO_FUTURE_ANNOTATIONS onto every recompiled code object
        # so no genuine finder could ever match.
        with tokenize.open(source_file) as handle:
            source = handle.read()
        tree = compile(source, str(source_file), "exec", dont_inherit=True)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - unreadable/undecodable/uncompilable; untrusted
        return False
    try:
        target_signature = api["_code_signature"](target)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - hostile code object; untrusted by default
        return False
    for candidate in api["_code_objects"](tree):
        try:
            if (
                candidate.co_name == target.co_name
                and candidate.co_code == target.co_code
                and candidate.co_names == target.co_names
                and candidate.co_varnames == target.co_varnames
                and api["_code_signature"](candidate) == target_signature
            ):
                return True
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:  # noqa: BLE001, S112 - hostile code object; untrusted
            continue
    return False


def _finder_code_file(finder: object, *, api: OriginApi) -> Path | None:
    """Return the file the finder's own ``find_spec`` was compiled from.

    ``co_filename`` names the file the code was compiled with.  It is not by
    itself trustworthy -- ``compile`` lets the caller choose it -- so the
    returned path is only a *candidate*: the caller must corroborate it with
    ``_code_matches_source`` before treating it as provenance.

    Returns ``None`` when the finder has no Python-level ``find_spec`` -- a C
    implementation (a builtin or extension module) has no code object to
    attest, so it is not treated as an installation finder.  ``None`` means
    "not trusted", never "trust me".
    """

    try:
        function = getattr(finder, "find_spec", None)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - hostile descriptor; untrusted by default
        return None
    if function is None:
        return None
    # A class is installed on meta_path with find_spec called unbound, so the
    # attribute is a plain function; an instance yields a bound method.
    try:
        function = getattr(function, "__func__", function)
        code = function.__code__
        filename = code.co_filename
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - not a Python function; untrusted by default
        return None
    if not isinstance(filename, str) or not filename:
        return None
    # Definitions from an interactive session, an exec() of a string, or a
    # doctest have no real file behind them and can never be an install.
    if filename.startswith("<") and filename.endswith(">"):
        return None
    return api["_safe_resolve"](Path(filename))


def _finder_was_imported_from(finder: object, source_file: Path, *, api: OriginApi) -> bool:
    """Return whether the interpreter itself imported ``finder`` from ``source_file``.

    Matching the finder's code against the bytes of a site-packages file proves
    only that *equivalent code exists there*, never that the running finder was
    loaded from there.  Those are different claims, and the gap between them is
    a false PASS: writing the finder's own source into site-packages and then
    running ``exec(compile(source, that_path, "exec"))`` produces a finder whose
    code matches that file exactly while having no provenance in it at all.
    Independent review reproduced this four times, by four different routes, and
    each time the guard reported ``PASS`` and a foreign submodule loaded
    afterwards.

    Every *in-memory* witness is forgeable, so none is consulted here.  The
    first attempt asked ``sys.modules[name].__spec__`` to name the file, and
    review showed the whole pair is attacker-writable: create a module with
    ``types.ModuleType``, attach a spec from
    ``importlib.util.spec_from_file_location`` -- which sets
    ``has_location=True`` -- and the guard certifies a finder the import system
    never loaded, then lets it serve a submodule from another tree.  Nothing
    about ``__spec__`` distinguishes that from a real install.

    So provenance is decided on disk instead, from the records pip and the
    environment leave behind, which a running process cannot rewrite into a
    different claim:

    * a distribution's ``RECORD`` lists the file with a SHA-256 of its
      contents, so the bytes the finder runs from are the bytes an install
      wrote; or
    * a ``.pth`` file in the same site-packages directory imports the defining
      module by name, which is how ``_virtualenv`` and the ``__editable__``
      shims are loaded in the first place.

    Both are filesystem facts about an install rather than claims by a live
    object, which is the distinction the earlier rounds kept failing to make.

    Returns ``False`` on every difficulty -- a missing module, a spec without an
    origin, or a raising attribute -- so an unanswerable question never becomes
    trust.
    """

    for root in api["_site_packages_roots"]():
        if not api["_is_within"](source_file, root, strict=False):
            continue
        if api["_is_recorded_by_an_install"](source_file, root):
            return True
        if api["_is_imported_by_a_pth"](source_file, root):
            return True
    return False


def _is_installation_finder(finder: object, *, api: OriginApi) -> bool:
    """Return whether ``finder`` was installed with this interpreter.

    A custom finder is legitimate when an install of this checkout put it
    there: the editable-install shim and the virtual-environment helper both
    live in this interpreter's own site-packages.  The check is on the
    defining file's *location*, never its name, so a renamed or lookalike
    module does not inherit trust and a genuine install finder is not refused.

    Location alone is not evidence, because ``__file__`` is ordinary mutable
    state that the finder controls.  Two earlier attempts at this check were
    defeated in review: a finder could claim a trusted module name with a
    ``__file__`` that does not exist, and then borrow the ``__file__`` of a
    genuine installed module that does.  Neither is forge-proof, because both
    read the *claim* rather than the code.

    ``co_filename`` is no better on its own: ``compile`` takes the filename it
    records, so a hostile finder can be compiled under the name of any file
    that exists and inherit its location.  Trust therefore requires two
    independent channels to agree: the finder's ``co_filename`` must name a
    real file inside site-packages, *and* the bytecode actually executing must
    be reproduced by recompiling that file's source from disk.  A forged
    filename names a real file that does not contain the hostile bytecode, so
    the second channel refuses it.

    That is necessary but not sufficient, and the gap was the last false PASS:
    code *matching* a site-packages file is not the same as code *loaded from*
    it.  Writing the finder's own source into site-packages and then running
    ``exec(compile(source, that_path, "exec"))`` satisfies every content check
    while having no provenance there whatsoever.  So the defining module must
    also be an entry the import system actually created for that same file,
    which a bare ``exec`` never produces.  Anything a finder can merely *say*
    about itself is ignored for this decision.

    Residual boundary: an attacker who can write a file into this
    interpreter's site-packages, or who runs code before the guard does, is out
    of scope -- at that point they own the interpreter rather than the finder.
    """

    if api["_is_trusted_stdlib_finder"](finder):
        return True
    code_file = api["_finder_code_file"](finder)
    if code_file is None:
        return False
    if not api["_finder_was_imported_from"](finder, code_file):
        return False
    try:
        if not code_file.is_file():
            return False
    except (OSError, ValueError):
        return False
    if not any(
        api["_is_within"](code_file, root, strict=False) for root in api["_site_packages_roots"]()
    ):
        return False
    try:
        function = getattr(finder, "find_spec", None)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - hostile descriptor; untrusted by default
        return False
    if function is None:
        return False
    try:
        function = getattr(function, "__func__", function)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - hostile descriptor; untrusted by default
        return False
    return api["_code_matches_source"](function, code_file)


def _untrusted_meta_path_finders(*, api: OriginApi) -> list[tuple[object, Path | None]]:
    """Return installed finders that may intercept the checked packages.

    ``sys.meta_path`` finders are consulted before ``PathFinder``, so one
    placed ahead of it can return a spec for a submodule from any directory in
    the filesystem.  No amount of origin or ``__path__`` checking observes
    that, which is how a foreign submodule can load after the guard reported
    PASS.  The guard therefore refuses to certify an interpreter whose
    meta-path contains a finder this checkout did not install.
    """

    # ``sys.meta_path`` is itself untrusted: an attacker who installs a finder
    # controls the list it lives in, and materialising it calls that
    # container's ``__iter__``.  Reading it therefore fails the same way a
    # hostile ``__path__`` does -- with a traceback instead of a verdict --
    # unless it is guarded here.  An unreadable meta-path means the guard
    # cannot establish that this checkout installed every finder, which is
    # exactly the claim it refuses to make without evidence.
    try:
        finders = list(sys.meta_path)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - untrusted data, see docstring
        return [(sys.meta_path, None)]

    offenders: list[tuple[object, Path | None]] = []
    for finder in finders:
        if api["_is_trusted_stdlib_finder"](finder) or api["_is_installation_finder"](finder):
            continue
        # Report the code location: it is the only part of a refused finder's
        # identity that is not simply what the finder claims about itself.
        offenders.append(
            (finder, api["_finder_code_file"](finder) or api["_finder_source"](finder))
        )
    return offenders


def _describe_finder(entry: tuple[object, Path | None], *, api: OriginApi) -> str:
    """Return a stable, human-readable name for a refused finder.

    Naming a hostile finder must not itself raise: the detail string is part
    of the refusal the operator has to read, so an unnameable finder degrades
    to a placeholder instead of turning the FAIL into a traceback.
    """

    finder, source = entry
    try:
        if isinstance(finder, type):
            name = f"{finder.__module__}.{getattr(finder, '__qualname__', None) or finder.__name__}"
        else:
            name = f"{type(finder).__module__}.{type(finder).__qualname__}"
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - a hostile metaclass must not abort the report
        name = "<finder with an unreadable identity>"
    if source is not None:
        return f"{name} (from {source})"
    return f"{name} (from an unidentifiable location)"
