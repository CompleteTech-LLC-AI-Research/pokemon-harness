"""Fail closed when the selected interpreter imports another tree's source.

The repository is developed across many Git worktrees that share one virtual
environment.  An editable install whose finder still points at a different
worktree makes every test result silently describe that other tree instead of
the checkout under test, in whichever direction it errs.  This check resolves
the two packages a release depends on -- ``pokered_harness`` and the vendored
``pyboy`` -- and refuses to continue unless both trace back to ``project_root``.

An origin counts as this checkout's when it either

* lives beneath ``project_root`` (a source tree or ``PYTHONPATH`` checkout), or
* lives in the site-packages directory of an installed distribution that is
  traceable to this checkout.

The second case is the release lane: CI runs ``pip install -e ".[dev]"`` into a
virtual environment created outside the checkout, so ``pyboy`` legitimately
resolves to ``<venv>/site-packages/pyboy``.  An install is traceable only
through the directory pip recorded in ``direct_url.json``, which for a local
install is always the literal source tree that was built.  There are two
legitimate source directories:

* ``project_root`` itself, for the editable harness install and any plain local
  install of the checkout.
* a staging directory *inside* this checkout's own ``build/`` tree, used by the
  native CI lane, which builds ``pyboy`` from a staged copy of
  ``vendor/pyboy-src`` so no generated C or object files can be reused.  The
  staged path is still physically inside the checkout, so location alone
  separates it from a sibling worktree.

Nothing the imported package says about itself is used as evidence.  The
vendored revision marker is a compile-time constant carried by every worktree
at the same pin, so it cannot distinguish two checkouts and an installed copy
could report any value it liked.  Provenance is decided by location only, which
is the property an installed copy cannot fabricate about itself.

A stale worktree satisfies neither: its install records that other directory,
which is neither this checkout nor a staging directory beneath this checkout.

Exit codes: ``0`` when every package traces to ``project_root``, ``1`` on any
mismatch or resolution failure, ``2`` on usage errors.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.metadata
import importlib.util
import json
import re
import sys
import tokenize
import types
from pathlib import Path
from urllib.parse import unquote, urlparse

# The distributions whose import origins decide what the suite actually
# measures.  ``pokered_harness`` is the harness under test and ``pyboy`` is the
# vendored emulator it drives; a stale copy of either invalidates a release.
REQUIRED_PACKAGES = ("pokered_harness", "pyboy")


def _trusted_stdlib_finders() -> set[object]:
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


def _is_trusted_stdlib_finder(finder: object) -> bool:
    """Return whether ``finder`` is one of the interpreter's own finders.

    A set membership test calls ``__hash__`` and ``__eq__`` on the object
    being looked up, and both are supplied by the finder itself.  The lookup
    is therefore attacker-controlled data reached before any of the guards
    around the caller's own attribute reads, so it has to be guarded here.

    An exception that is not an operator interrupt means this finder is not
    one of the interpreter's own, which is the same answer an identity
    mismatch gives: untrusted by default.
    """

    try:
        return finder in _trusted_stdlib_finders()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - untrusted data, see docstring
        return False


def _site_packages_roots() -> list[Path]:
    """Return the interpreter's own site-packages directories.

    These are the only third-party locations a legitimate install finder can
    be loaded from.  The editable install that ``pip install -e ".[dev]"``
    writes, ``_virtualenv``, and vendored wheels all land here; a finder
    defined anywhere else was injected by the environment, not by an install
    of this checkout.
    """

    import site

    def _extend(getter, sink: list[str]) -> None:
        """Append a site directory list, ignoring an unavailable one."""

        if not callable(getter):
            return
        try:
            sink.extend(getter())
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:  # noqa: BLE001 - layout is advisory here
            return

    def _append_one(getter, sink: list[str]) -> None:
        """Append a single site directory, ignoring an unavailable one."""

        if not callable(getter):
            return
        try:
            sink.append(getter())
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:  # noqa: BLE001 - no user site on this layout
            return

    # The attribute reads are guarded for the same reason the calls are: an
    # interpreter that replaced the ``site`` module with a hostile
    # ``ModuleType`` subclass controls what ``getattr`` returns, and reading
    # the attribute is a call into that code.  Without this a direct
    # ``BaseException`` raised here escaped before either helper was reached.
    candidates: list[str] = []
    for attribute, adder in (
        ("getsitepackages", _extend),
        ("getusersitepackages", _append_one),
    ):
        try:
            getter = getattr(site, attribute, None)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:  # noqa: BLE001, S112 - the site module is untrusted
            continue
        adder(getter, candidates)

    roots: list[Path] = []
    for candidate in candidates:
        # The truth test sits inside the boundary for the same reason the
        # conversion does: it interrogates the very object the getter returned,
        # so a path-like whose ``__bool__`` raises must be skipped like any
        # other unusable entry rather than escaping the guard.
        try:
            if not candidate:
                continue
            roots.append(Path(candidate).resolve())
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:  # noqa: BLE001, S112 - entry is untrusted
            # The previous handler named only ``(OSError, ValueError,
            # RuntimeError)``, which left a path-like raising a direct
            # ``BaseException`` subclass from ``__fspath__`` escaping.  A
            # site directory that cannot be converted or resolved simply is
            # not usable, and an unusable one contributes no root -- the same
            # fail-closed direction the site getters themselves take.
            continue
    return roots


def _finder_module(finder: object) -> object | None:
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


def _finder_source(finder: object) -> Path | None:
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
    """

    try:
        module = _finder_module(finder)
    except BaseException:  # noqa: BLE001 - hostile metaclass; untrusted by default
        return None
    if module is not None:
        try:
            origin = getattr(module, "__file__", None)
        except BaseException:  # noqa: BLE001 - hostile module; fall through to find_spec
            origin = None
        if isinstance(origin, str) and origin:
            return _safe_resolve(Path(origin))
    try:
        name = finder.__module__ if isinstance(finder, type) else type(finder).__module__
    except BaseException:  # noqa: BLE001 - hostile metaclass; untrusted by default
        return None
    if not isinstance(name, str) or not name:
        return None
    try:
        spec = importlib.util.find_spec(name)
    except BaseException:  # noqa: BLE001 - a hostile import hook must not abort the guard
        return None
    try:
        origin = getattr(spec, "origin", None) if spec is not None else None
    except BaseException:  # noqa: BLE001 - hostile spec object
        return None
    if not isinstance(origin, str) or origin in ("built-in", "frozen", "namespace"):
        return None
    return _safe_resolve(Path(origin))


def _safe_resolve(candidate: Path) -> Path | None:
    """Resolve ``candidate``, returning ``None`` when it is not a usable path."""

    try:
        return Path(candidate).resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def _code_objects(code: object):
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
            yield from _code_objects(constant)


def _code_signature(code: object) -> tuple:
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
                        _code_signature(constant),
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
        return _unreadable_signature()


def _unreadable_signature() -> object:
    """Return a signature value that cannot equal any other signature."""

    return object()


def _code_matches_source(function: object, source_file: Path) -> bool:
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
        target_signature = _code_signature(target)
    except BaseException:  # noqa: BLE001 - hostile code object; untrusted by default
        return False
    for candidate in _code_objects(tree):
        try:
            if (
                candidate.co_name == target.co_name
                and candidate.co_code == target.co_code
                and candidate.co_names == target.co_names
                and candidate.co_varnames == target.co_varnames
                and _code_signature(candidate) == target_signature
            ):
                return True
        except BaseException:  # noqa: BLE001, S112 - hostile code object; untrusted
            continue
    return False


def _finder_code_file(finder: object) -> Path | None:
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
    return _safe_resolve(Path(filename))


def _finder_was_imported_from(finder: object, source_file: Path) -> bool:
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

    for root in _site_packages_roots():
        if not _is_within(source_file, root, strict=False):
            continue
        if _is_recorded_by_an_install(source_file, root):
            return True
        if _is_imported_by_a_pth(source_file, root):
            return True
    return False


def _record_digests(root: Path) -> dict[str, set[tuple[str, str]]]:
    """Return the ``RECORD`` hash for every file an installed distribution owns.

    ``RECORD`` is written at install time and names each installed file with
    the hash of its contents.  Keys are resolved paths, so a caller compares
    resolved paths on both sides and cannot be misled by a relative or
    ``..``-laden spelling of the same file.

    The value is a *set* because several records may claim the same path.  The
    caller has to satisfy all of them: if one record disagrees about what a
    file contains, the file is not consistently attested by an install, and
    picking whichever claim sorted first would let a planted record decide.

    A missing or unreadable ``RECORD`` contributes nothing rather than
    aborting: an install that cannot be attested is refused, and refusing is
    the safe direction.
    """

    digests: dict[str, set[tuple[str, str]]] = {}
    try:
        records = sorted(root.glob("*.dist-info/RECORD"))
    except (OSError, ValueError):
        return digests
    for record in records:
        try:
            text = record.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        for line in text.splitlines():
            # ``RECORD`` is CSV with the shape ``path,sha256=<digest>,size``.
            # The path may legally contain a comma, so this is parsed from the
            # right: the size is the last field, the digest the one before it,
            # and the name is everything remaining.  Splitting on every comma
            # would truncate a name that holds one, and a truncated name
            # resolves to a path no record ever listed -- so a genuinely
            # installed finder whose location contains a comma would lose its
            # provenance and be refused.
            head, _, _size = line.rpartition(",")
            name_field, separator, digest = head.rpartition(",")
            if not separator or not name_field:
                continue
            # The name may be a quoted CSV field, and a quoted field may hold
            # its own escaped quotes.  Leaving them attached builds a path no
            # record ever listed, so the digest would be compared against a
            # file that does not exist and a genuinely installed finder would
            # lose its provenance.  Only an actually-quoted field is decoded,
            # so a name that merely *starts* with a quote is left alone.
            if len(name_field) >= 2 and name_field[0] == '"' and name_field[-1] == '"':
                try:
                    decoded = next(csv.reader([name_field]))
                except (csv.Error, StopIteration):
                    continue
                if not decoded:
                    continue
                name_field = decoded[0]
            algorithm, _, expected = digest.partition("=")
            algorithm = algorithm.lower()
            # A record names the algorithm that produced its digest, so the
            # label is part of the claim rather than decoration.  ``RECORD``
            # permits any algorithm ``hashlib`` guarantees and wheel may ship
            # SHA-512, so insisting on ``sha256`` here refused a genuine
            # install outright.  Storing the pair lets the caller recompute
            # under the algorithm the record actually names, which is both
            # wider than trusting the label blindly and narrower than
            # discarding it.
            if not algorithm or not expected or not name_field:
                continue
            # An empty hash field is how ``RECORD`` marks a file it installed
            # without recording contents (a generated script, a compiled
            # extension).  That is not evidence of provenance, so it
            # contributes no claim and the path stays unattested.
            resolved = _safe_resolve(root / name_field)
            if resolved is not None:
                # Collect *every* claim about a path rather than keeping the
                # first.  Two records may disagree about the same file, and
                # the caller must not be able to win by writing one that
                # happens to sort first.
                digests.setdefault(str(resolved), set()).add((algorithm, expected))
    return digests


def _file_digest(candidate: Path, algorithm: str = "sha256") -> str | None:
    """Return the URL-safe base64 digest of ``candidate``, or ``None``.

    The digest is recomputed under ``algorithm`` rather than a hardcoded
    SHA-256, because ``RECORD`` names the algorithm that produced each claim.
    Returns ``None`` for an unknown or unavailable algorithm, so a record
    naming something this interpreter cannot compute attests nothing.
    """

    try:
        data = candidate.read_bytes()
    except (OSError, ValueError):
        return None
    try:
        hasher = hashlib.new(algorithm)
    except (ValueError, TypeError):
        return None
    try:
        hasher.update(data)
        return base64.urlsafe_b64encode(hasher.digest()).rstrip(b"=").decode()
    except (ValueError, TypeError):
        return None


def _is_recorded_by_an_install(source_file: Path, root: Path) -> bool:
    """Return whether an installed distribution recorded this exact file.

    The ``RECORD`` hash is what makes the check a statement about provenance
    rather than about location: an attacker who plants a file in
    site-packages has to also match a hash recorded by an install, and the
    record is what the installer wrote when it laid the file down.

    Every recorded claim must match, so an extra record asserting different
    content for the same path cannot be ignored: keeping only one claim --
    whichever sorted first -- would let a planted record decide.  Each claim
    is recomputed with the algorithm it names, so a row reading
    ``sha512=<a sha256 digest>`` fails rather than being quietly honoured.
    """

    expected = _record_digests(root).get(str(source_file))
    if not expected:
        return False
    for algorithm, digest in expected:
        actual = _file_digest(source_file, algorithm)
        if actual is None or actual != digest:
            return False
    return True


def _is_imported_by_a_pth(source_file: Path, root: Path) -> bool:
    """Return whether a ``.pth`` file in ``root`` imports the defining module.

    ``_virtualenv`` and the ``__editable__`` shim are not owned by any
    distribution's ``RECORD``: they are dropped into site-packages and
    activated by a ``.pth`` file that imports them by name.  Requiring that a
    ``.pth`` in the *same* directory names the module is what distinguishes a
    real environment shim from a module an attacker merely wrote there.
    """

    if source_file.name == "__init__.py":
        stem = source_file.parent.name
    else:
        stem = source_file.stem
    if not stem or not stem.isidentifier():
        return False
    try:
        entries = sorted(root.glob("*.pth"))
    except (OSError, ValueError):
        return False
    needle = f"import {stem}"
    for entry in entries:
        try:
            text = entry.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        if needle in text:
            return True
    return False


def _is_installation_finder(finder: object) -> bool:
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

    if _is_trusted_stdlib_finder(finder):
        return True
    code_file = _finder_code_file(finder)
    if code_file is None:
        return False
    if not _finder_was_imported_from(finder, code_file):
        return False
    try:
        if not code_file.is_file():
            return False
    except (OSError, ValueError):
        return False
    if not any(_is_within(code_file, root, strict=False) for root in _site_packages_roots()):
        return False
    try:
        function = getattr(finder, "find_spec", None)
    except BaseException:  # noqa: BLE001 - hostile descriptor; untrusted by default
        return False
    if function is None:
        return False
    try:
        function = getattr(function, "__func__", function)
    except BaseException:  # noqa: BLE001 - hostile descriptor; untrusted by default
        return False
    return _code_matches_source(function, code_file)


def _untrusted_meta_path_finders() -> list[tuple[object, Path | None]]:
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
        if _is_trusted_stdlib_finder(finder) or _is_installation_finder(finder):
            continue
        # Report the code location: it is the only part of a refused finder's
        # identity that is not simply what the finder claims about itself.
        offenders.append((finder, _finder_code_file(finder) or _finder_source(finder)))
    return offenders


def _describe_finder(entry: tuple[object, Path | None]) -> str:
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
    except BaseException:  # noqa: BLE001 - a hostile metaclass must not abort the report
        name = "<finder with an unreadable identity>"
    if source is not None:
        return f"{name} (from {source})"
    return f"{name} (from an unidentifiable location)"


def _is_within(candidate: Path, root: Path, *, strict: bool = False) -> bool:
    """Return whether ``candidate`` is ``root`` or lives beneath it.

    Both sides are resolved first.  A lexical comparison would treat
    ``<checkout>/build/pyboy-native-link`` as inside ``<checkout>/build``
    even when that name is a symlink into a sibling worktree, and would
    compare ``<checkout>/build/../../elsewhere`` as inside without ever
    applying the traversal.  Provenance is a statement about where a file
    really is, so the paths must be real before they are compared.

    ``strict`` additionally refuses a root that was not itself a real
    directory in the checkout.  Resolving is necessary but not sufficient: if
    ``<checkout>/build`` is a symlink into another worktree then resolving it
    yields that other worktree's build tree, and every source beneath it then
    compares as "inside" while living somewhere else entirely.  The staging
    root must therefore still be a real subdirectory of the checkout after
    resolution.

    A path that cannot be resolved at all compares as *not* within.  Both
    sides are data reported by the interpreter or by install metadata rather
    than paths this checkout chose, and the comparison must stay total:
    letting ``Path.resolve`` raise turned a finding into an ``INTERNALERROR``
    traceback.  Refusing is the fail-closed direction, because "within this
    checkout" is the claim being disproved.  A value that is not a path at all
    is refused for the same reason: ``Path(...)`` raises ``TypeError``, which
    is a failure of the data, not of this checkout.  ``except BaseException``
    rather than an enumeration, because a foreign object's ``__fspath__`` can
    raise anything and both operands here are untrusted.
    """

    try:
        resolved_root = Path(root).resolve()
        resolved_candidate = Path(candidate).resolve()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - untrusted data, see docstring
        return False
    if strict and Path(root).is_symlink():
        return False
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError:
        return False
    return True


def _installed_from(distribution_name: str, project_root: Path) -> Path | None:
    """Return the checkout an installed distribution was built from.

    ``direct_url.json`` is written by pip for both editable and local installs
    and names the directory the install was produced from.  A distribution
    without that record cannot be attributed to a checkout, so it contributes
    no allowed site-packages root.  A record naming a path that cannot be
    resolved is treated the same way: the install is unattributable, not
    crashing the run.
    """

    try:
        distribution = _distribution(distribution_name)
        text = distribution.read_text("direct_url.json")
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - untrusted metadata reader
        # A distribution object is third-party code and ``read_text`` is just a
        # method on it, so it can raise anything -- including a direct
        # ``BaseException`` subclass that bypassed ``except (OSError, ...)``
        # and escaped the guard.  An install whose own record cannot be read
        # cannot be attributed to this checkout, so it contributes no allowed
        # root: fail closed.
        return None
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    except ValueError:
        # Valid JSON can still be refused by the interpreter itself: CPython
        # caps integer string conversion, so a record holding a 5,000-digit
        # integer raises ValueError from json.loads rather than
        # JSONDecodeError.  A record that cannot be parsed is unattributable.
        return None
    if not isinstance(payload, dict):
        # Valid JSON is not necessarily the object pip writes.  A record that
        # parses to a list or a scalar cannot name a source, so it is
        # unattributable rather than a crash.
        return None
    url = payload.get("url")
    if not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme != "file":
        return None
    if parsed.netloc not in ("", "localhost"):
        return None
    try:
        return Path(unquote(parsed.path)).resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def _normalise_distribution_name(name: str) -> str:
    """Return the PEP 503 normalized form of a distribution name."""

    return re.sub(r"[-_.]+", "-", name).lower()


def _distribution(distribution_name: str):
    """Return the installed distribution owning ``distribution_name``.

    ``packages_distributions`` reports owners in the form recorded at install
    time -- the native CI lane's wheel is named ``PyBoy`` -- while lookups by
    an ad-hoc spelling must still resolve.  Try the normalized name, then the
    literal one, so a case or separator difference cannot silently skip a
    distribution that would otherwise be admitted.
    """

    for candidate in dict.fromkeys(
        (
            _normalise_distribution_name(distribution_name),
            distribution_name,
        )
    ):
        try:
            return importlib.metadata.distribution(candidate)
        except importlib.metadata.PackageNotFoundError:
            continue
    raise importlib.metadata.PackageNotFoundError(distribution_name)


def _staging_root(project_root: Path) -> Path:
    """Return the checkout-local staging directory used by the native build."""

    return project_root / "build"


def _is_this_checkout(project_root: Path, source: Path | None) -> bool:
    """Return whether a recorded install source belongs to this checkout.

    Two locations qualify, and both are decided by the filesystem rather than
    by anything the installed distribution reports about itself:

    * ``project_root`` -- an editable or plain local install of the checkout.
    * a directory inside this checkout's own ``build/`` tree -- the native lane
      stages ``vendor/pyboy-src`` there before building it, and pip records
      that staging path as the install source.  Requiring the recorded source
      to be *physically inside this checkout* is what a stale worktree cannot
      satisfy, because its own ``build/`` tree belongs to that other checkout.

    Anything else, including a missing record, an unresolvable path, or a
    staging directory that merely shares a name, is refused.
    """

    if source is None:
        return False
    try:
        root = project_root.resolve()
        source = Path(source).resolve()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - untrusted data; see _resolve_path
        return False
    if source == root:
        return True
    return _is_within(source, _staging_root(root), strict=True)


def _allowed_roots(
    project_root: Path,
    packages: tuple[str, ...] = REQUIRED_PACKAGES,
) -> list[Path]:
    """Return every directory whose contents legitimately serve this checkout.

    Mirrors ``bootstrap_pyboy._runtime_roots``: a distribution's own
    site-packages directory is allowed only when the distribution records this
    checkout as its source, and only for that distribution's own package
    directory.  Never the whole site-packages tree, which would let an
    unrelated copy of the package shadow the pinned one.

    ``packages`` names the import origins actually being checked, so an
    explicit ``--package`` run still admits a legitimate install of *that*
    package.  Restricting the lookup to the defaults made the narrowed check
    refuse the release lane, which installs both packages outside the checkout.
    """

    roots = [project_root]
    try:
        distributions = importlib.metadata.packages_distributions()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - metadata is advisory here
        # ``packages_distributions`` imports helper modules, so a hostile
        # meta-path finder can raise from it directly.  An interpreter whose
        # finder refuses to be inspected gets no site-packages roots, which
        # fails closed: the packages are then reported as foreign rather than
        # being silently admitted.
        return roots
    for package in packages:
        try:
            owners = distributions.get(package, ()) or ()
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:  # noqa: BLE001, S112 - metadata mapping is untrusted
            # ``packages_distributions`` returns an attacker-reachable
            # mapping, so the ``.get`` that reads it is a call into arbitrary
            # code and can raise a direct ``BaseException`` subclass.  Without
            # this the exception escaped ``check_origins`` with a traceback.
            # An owner list that cannot be read attributes no install to this
            # checkout, so no root is admitted -- fail closed.
            continue
        try:
            owner_list = list(owners)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:  # noqa: BLE001, S112 - metadata iterable is untrusted
            # The guarded ``.get`` above protects the *lookup*, but the value it
            # returns is just as attacker-controlled and need not be a list:
            # iterating it runs its ``__iter__``, which can raise a direct
            # ``BaseException`` subclass and escape ``check_origins``.  An
            # owner list that cannot be enumerated attributes no install to
            # this checkout, so no root is admitted -- fail closed.
            continue
        for owner in owner_list:
            if not _is_this_checkout(project_root, _installed_from(owner, project_root)):
                continue
            try:
                located = _distribution(owner).locate_file(package)
            except (OSError, importlib.metadata.PackageNotFoundError):
                continue
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException:  # noqa: BLE001, S112 - untrusted metadata reader
                # A distribution object is third-party code; any of its
                # methods can raise anything.  An install whose location
                # cannot be read contributes no allowed root, which is the
                # fail-closed direction.
                continue
            resolved, _ = _resolve_path(located, package)
            if resolved is not None:
                roots.append(resolved)
    return roots


# Module objects published by ``_resolve_origin``, keyed by package name.  The
# origin is read from ``module.__file__`` exactly once, and the portion check
# reuses the same module rather than re-reading that mutable value.
_RESOLVED_MODULES: dict = {}


def _resolve_origin(package: str) -> tuple[Path | None, str]:
    """Import ``package`` and return ``(module file, error)`` for its origin."""

    try:
        module = __import__(package)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - a failed import is a finding
        return None, f"import failed: {_type_name(exc)}: {_describe(exc)}"
    # Publish the module this origin was read from, so the portion check can
    # use it without reading ``__file__`` a second time.  ``__file__`` is
    # mutable interpreter state and a hostile path-like can answer
    # differently on each read; re-reading it here let a first read matching
    # the origin be followed by one that did not, and the portion check then
    # returned no findings at all -- a false PASS for a package that really
    # does have a foreign ``__path__``.
    _RESOLVED_MODULES[package] = module
    # Reading ``__file__`` is an untrusted read like any other: a module can
    # override ``__getattribute__`` so that touching ``__file__`` raises.  That
    # read sat outside the guard's exception boundary, so the raise escaped
    # ``check_origins`` as a traceback and the operator saw a crash instead of
    # the FAIL naming the offending package -- the one outcome this module
    # exists to make impossible.  A module that cannot report where it lives
    # is a finding, never a crash.
    try:
        origin = getattr(module, "__file__", None)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - hostile getter is a finding
        return None, f"__file__ could not be read: {_type_name(exc)}: {_describe(exc)}"
    if origin is None:
        # Namespace packages legitimately report ``None``; their search path is
        # the only available statement of where they resolved.
        try:
            locations = list(getattr(module, "__path__", ()) or ())
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # noqa: BLE001 - hostile getter is a finding
            return None, f"__path__ could not be read: {_type_name(exc)}: {_describe(exc)}"
        if not locations:
            return None, "module exposed neither __file__ nor __path__"
        candidate = locations[0]
    else:
        candidate = origin
    return _resolve_path(candidate, package)


def _resolve_path(candidate: object, package: str) -> tuple[Path | None, str]:
    """Return ``candidate`` resolved, or the error that made it unusable.

    A path an interpreter reports is data, and a path can be malformed rather
    than merely foreign.  It can also be the wrong *type* entirely: an
    embedded NUL raises from ``Path.resolve``, and a value that is not a path
    at all raises from ``Path(...)`` before this function is ever reached.
    Worse, the value may be an object with a ``__fspath__`` that raises
    anything at all -- ``Path(...)`` calls it, and no list of expected
    exception types can anticipate what a foreign object chooses to raise.
    Every one of these let a finding escape as a traceback, so a bare
    ``pytest`` run died with an ``INTERNALERROR`` instead of the explicit
    refusal that names the offending package.

    Conversion therefore happens *inside* the guard, and is the only place in
    the module that coerces untrusted data to a ``Path``.  An unusable origin
    is a finding like any other, so report it and let the caller fail closed.

    The clause is therefore ``except BaseException`` rather than an
    enumeration: the input is untrusted interpreter or metadata output, and
    "this path is unusable" must hold for *every* way it can fail to be one.
    Narrowing this to a tuple reintroduces the escape the moment some caller
    passes an object that raises something unanticipated.  ``KeyboardInterrupt``
    and ``SystemExit`` are re-raised so an operator interrupt still stops the
    run rather than being recorded as a finding.

    The message is rendered with ``_describe`` rather than an f-string: the
    caught exception is itself untrusted, since it can come straight out of a
    foreign ``__fspath__``, and ``str(exc)`` on such an object can raise.  A
    detail line that is itself hostile must still produce a finding.

    An empty candidate is refused before conversion.  ``Path("")`` is not
    malformed -- it resolves cleanly to the process CWD, which is normally
    inside this checkout -- so it would otherwise be credited here and turn an
    origin that says nothing about where the package came from into a PASS.
    The guard is meant to *prove* a package was imported from this tree, and
    an empty ``__file__`` proves no such thing.

    The test is scoped to ``str``/``bytes`` on purpose.  A path-like object
    with ``__len__`` returning 0 is falsy but perfectly usable, so a bare
    ``if not candidate`` would refuse a real path.  It also sits inside the
    ``try`` so a candidate that raises while being interrogated is reported
    as a finding rather than escaping.
    """

    try:
        if isinstance(candidate, (str, bytes)) and not candidate:
            return None, "origin is an empty path"
        return Path(candidate).resolve(), ""
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - untrusted data, see docstring
        return None, f"origin is not a usable path: {_describe(exc)}"


def _type_name(value: object) -> str:
    """Return ``type(value).__name__`` without trusting the metaclass.

    A hostile object controls its own metaclass, so ``type(value)`` is an
    object whose ``__name__`` lookup can run attacker code or raise.  This is
    the same exposure ``_describe`` closes for ``str``, applied to the type of
    the value rather than the value, so a finding can still be rendered when
    the thing being reported about is itself a trap.
    """

    try:
        name = type(value).__name__
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - untrusted data, see docstring
        return "<unknown type>"
    try:
        return str(name)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - untrusted data, see docstring
        return "<unknown type name>"


def _describe(value: object) -> str:
    """Return a printable rendering of ``value`` that cannot itself raise.

    Everything this module reports came from the interpreter or from install
    metadata, so any of it may be an object with a hostile ``__str__``.  A
    detail string that raises while being built turns the finding back into a
    traceback, which is the exact failure this module exists to prevent.
    """

    try:
        return str(value)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001 - untrusted data, see docstring
        return f"<unprintable {_type_name(value)}>"


def _foreign_path_locations(
    package: str,
    allowed_roots: list[Path],
    origin: Path,
    module: object = None,
) -> tuple[list[Path], list[str]]:
    """Return importable ``__path__`` entries of ``package`` that are not local.

    Every entry of a package's ``__path__`` is a directory the interpreter will
    import from, whether or not the package also has a ``__file__``.  A regular
    package carrying a checkout-local ``__init__.py`` is no safer than a
    namespace package: ``pkgutil.extend_path``, an explicit ``__path__``
    extension, or a ``.pth``-installed path entry can add a second portion from
    another worktree, and a submodule missing locally then imports from that
    foreign portion while the top-level origin still looks correct.

    Crediting only the top-level origin (or only ``__path__[0]``) therefore
    reports PASS for a package that is genuinely partly foreign -- the exact
    false PASS this guard exists to prevent.  So this applies to regular
    packages too, not only to namespaces.

    Returns the foreign locations and the reasons any entry was unusable.  A
    portion that cannot be resolved is a finding, never an exception: the
    caller fails closed on either.

    ``module`` is the module whose ``__file__`` the caller already resolved
    into ``origin``.  Passing it is load-bearing: ``__file__`` is mutable
    interpreter state and a hostile path-like can answer differently on each
    read, so reading it a second time here let a first read matching ``origin``
    be followed by one that did not -- and the function then returned no
    findings at all, so a package with a genuinely foreign ``__path__``
    reported PASS.  That is a false PASS, reproduced and pinned by
    ``test_a_foreign_path_portion_is_reported_even_when_file_changes_between_
    reads``.  With the caller supplying the module, its ``__file__`` is read
    exactly once.
    """

    origin_is_this_module = module is not None
    if module is None:
        module = sys.modules.get(package)
    if module is None:
        return [], []
    # Judge the module that actually corresponds to the origin being reported.
    # A caller may have resolved the origin from install metadata rather than
    # from the live ``sys.modules`` entry (the release lane does exactly that),
    # and then the ambient module is some *other* tree's package.  Comparing
    # that module's portions against these roots would report a foreign path
    # for a package that is genuinely installed from an allowed root.
    if not origin_is_this_module:
        try:
            module_origin = getattr(module, "__file__", None)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException:  # noqa: BLE001 - hostile module; fail closed
            # A module that cannot say where it lives cannot corroborate this
            # origin, so contribute no portions -- the fail-closed direction.
            return [], []
        if module_origin is not None:
            try:
                if Path(module_origin).resolve() != origin:
                    return [], []
            except (OSError, ValueError, RuntimeError):
                return [], []
    outside: list[Path] = []
    unusable: list[str] = []
    try:
        portions = list(getattr(module, "__path__", ()) or ())
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - hostile module; a finding, not a crash
        # The portions are how a package can smuggle a foreign submodule, so
        # an unreadable ``__path__`` is exactly the condition being guarded.
        # Report it instead of letting the getter's exception escape.
        return [], [f"__path__ could not be read: {_type_name(exc)}: {_describe(exc)}"]
    for location in portions:
        if not any(_is_within(location, allowed, strict=False) for allowed in allowed_roots):
            # Coerce through ``_resolve_path`` rather than ``Path(...).resolve``
            # directly: a portion is untrusted interpreter output, so it can be
            # the wrong type, embed a NUL, or be an object whose ``__fspath__``
            # raises anything at all.  An unusable portion is reported as the
            # path it claims to be and still fails the caller closed, and a
            # hostile ``__str__`` cannot escape while the reason is rendered.
            resolved, error = _resolve_path(location, package)
            if resolved is not None:
                outside.append(resolved)
            else:
                unusable.append(f"{_describe(location)}: {error}")
    return outside, unusable


def check_origins(project_root: Path, packages: tuple[str, ...] = REQUIRED_PACKAGES) -> dict:
    """Return a JSON-serializable report of every package's resolved origin.

    Every value inspected below comes from outside this process: ``sys``,
    ``site``, ``sys.modules``, the meta-path, and the on-disk layout an install
    produced.  Thirteen review rounds across #558 and #559 each found one more
    such read that a hostile object could raise a direct ``BaseException``
    subclass from, so the reads are individually guarded.

    That is not sufficient on its own and this boundary is the backstop for
    the same reason: a guard whose only answer to a hostile input is a
    traceback is fail-open, because it emits no machine-readable finding for a
    caller to gate on.  A read nobody has thought of yet must still produce a
    refusal.  So any ``BaseException`` that escapes the analysis below is
    converted here into the same FAIL document every other refusal produces.
    ``KeyboardInterrupt`` and ``SystemExit`` deliberately pass through: an
    operator interrupt must still stop the process.
    """

    try:
        return _check_origins(project_root, packages)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - the guard must never traceback
        return {
            "project_root": _describe(project_root),
            "packages": [
                {
                    "package": "<guard>",
                    "origin": None,
                    "status": "FAIL",
                    "detail": (
                        "the import-origin guard could not complete: "
                        f"{_type_name(exc)}: {_describe(exc)}"
                    ),
                }
            ],
            "status": "FAIL",
        }


def _check_origins(project_root: Path, packages: tuple[str, ...]) -> dict:
    """Return the report, or raise whatever a hostile input raises.

    The analysis proper.  ``check_origins`` wraps this so that a hostile input
    escaping any read becomes a refusal rather than a traceback; nothing here
    catches on its behalf.
    """

    # Start from a clean slate.  An entry left over from an earlier call would
    # pair this call's origin -- which the release lane may have resolved from
    # install metadata rather than from a live import -- with that earlier
    # tree's module, and report a foreign portion for an honest install.
    _RESOLVED_MODULES.clear()
    # ``project_root`` is supplied by the caller, and a path-like whose
    # ``resolve`` raises would otherwise abort the guard before any report
    # exists -- the same traceback-instead-of-a-verdict failure the rest of
    # this module is written to prevent.  A root that cannot be resolved
    # cannot be shown to be the checkout under test, so it is refused.
    try:
        root = project_root.resolve()
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - untrusted data, see docstring
        return {
            # ``_describe`` rather than a bare ``str``: the root that failed to
            # resolve is itself the hostile path-like, so rendering it here
            # would raise out of the very handler meant to report the escape.
            "project_root": _describe(project_root),
            "packages": [
                {
                    "package": "<project-root>",
                    "origin": None,
                    "status": "FAIL",
                    "detail": (
                        "project root could not be resolved: "
                        f"{_type_name(exc)}: {_describe(exc)}"
                    ),
                }
            ],
            "status": "FAIL",
            "detail": "project root could not be resolved",
        }
    allowed_roots = _allowed_roots(root, packages)
    findings: list[dict] = []
    # A meta-path finder this checkout did not install can place a submodule
    # anywhere, and it runs before PathFinder, so every origin and __path__
    # check below would still read PASS while foreign code loaded.  Refuse the
    # interpreter itself rather than certifying a provenance it cannot
    # establish.
    intruders = _untrusted_meta_path_finders()
    if intruders:
        return {
            "project_root": str(root),
            "packages": [
                {
                    "package": "<interpreter>",
                    "origin": None,
                    "status": "FAIL",
                    "detail": (
                        "untrusted sys.meta_path finder(s) can import a checked "
                        "package's submodules from outside this checkout: "
                        + ", ".join(_describe_finder(item) for item in intruders)
                    ),
                }
            ],
            "status": "FAIL",
        }
    for package in packages:
        origin, error = _resolve_origin(package)
        if error:
            findings.append(
                {
                    "package": package,
                    "origin": None,
                    "status": "FAIL",
                    "detail": error,
                }
            )
            continue
        assert origin is not None  # guaranteed when ``error`` is empty
        # Judge the top-level origin first.  When the package itself resolves
        # outside every allowed root that is the whole finding, and reporting a
        # path-portion detail as well would bury it.
        if not any(_is_within(origin, allowed) for allowed in allowed_roots):
            findings.append(
                {
                    "package": package,
                    "origin": str(origin),
                    "status": "FAIL",
                    "detail": (
                        f"resolves outside this checkout {root} and outside the "
                        "site-packages of an install made from it; the "
                        "interpreter is importing a different checkout"
                    ),
                }
            )
            continue
        # Use the module ``_resolve_origin`` actually imported, so ``__file__``
        # is read once and a hostile value cannot change its answer between the
        # origin resolution and the portion check.  When the origin came from
        # install metadata instead -- the release lane resolves it that way --
        # nothing was published and the ambient module is some *other* tree's
        # package, which must not be judged against these roots, so the
        # function's own fallback handles it.
        live = _RESOLVED_MODULES.get(package)
        foreign, unusable = _foreign_path_locations(package, allowed_roots, origin, live)
        if foreign or unusable:
            detail = ""
            if foreign:
                detail = "package also resolves outside this checkout: " + ", ".join(
                    _describe(item) for item in foreign
                )
            if unusable:
                joined = "path portion is not usable: " + ", ".join(unusable)
                detail = f"{detail}; {joined}" if detail else joined
            findings.append(
                {
                    "package": package,
                    "origin": str(origin),
                    "status": "FAIL",
                    "detail": detail,
                }
            )
            continue
        findings.append(
            {
                "package": package,
                "origin": str(origin),
                "status": "PASS",
                "detail": "",
            }
        )
    # `all()` over an empty sequence is True, so an empty request would report
    # PASS for verifying nothing.  A gate that checked no packages must not be
    # able to certify that all is well; fail closed at the report boundary so
    # the verdict holds no matter which caller supplied the list.
    if not findings:
        return {
            "project_root": str(root),
            "packages": [],
            "status": "FAIL",
            "detail": "no packages were selected to verify",
        }
    return {
        "project_root": str(root),
        "packages": findings,
        "status": "PASS" if all(item["status"] == "PASS" for item in findings) else "FAIL",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that the selected interpreter imports pokered_harness and the "
            "vendored pyboy from the checkout under test."
        )
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="checkout under test; defaults to this script's repository root",
    )
    parser.add_argument(
        "--package",
        action="append",
        dest="packages",
        help=(
            "distribution name to verify *instead of* the defaults "
            "(repeatable; replaces -- not adds to -- pokered_harness and pyboy)"
        ),
    )
    arguments = parser.parse_args(argv)

    packages = tuple(arguments.packages or ())
    if not packages:
        packages = REQUIRED_PACKAGES
    report = check_origins(arguments.project_root, packages)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
