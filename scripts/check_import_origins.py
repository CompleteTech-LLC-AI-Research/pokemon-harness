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
import importlib.metadata
import importlib.util
import json
import re
import sys
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
    """Return whether ``finder`` is one of the interpreter's own finders."""

    return finder in _trusted_stdlib_finders()


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
        except Exception:  # noqa: BLE001 - layout is advisory here
            return

    def _append_one(getter, sink: list[str]) -> None:
        """Append a single site directory, ignoring an unavailable one."""

        if not callable(getter):
            return
        try:
            sink.append(getter())
        except Exception:  # noqa: BLE001 - no user site on this layout
            return

    candidates: list[str] = []
    _extend(getattr(site, "getsitepackages", None), candidates)
    _append_one(getattr(site, "getusersitepackages", None), candidates)

    roots: list[Path] = []
    for candidate in candidates:
        if not candidate:
            continue
        try:
            roots.append(Path(candidate).resolve())
        except (OSError, ValueError, RuntimeError):
            continue
    return roots


def _finder_module(finder: object) -> object | None:
    """Return the module object that defines ``finder``, if it is loaded."""

    name = finder.__module__ if isinstance(finder, type) else type(finder).__module__
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
    except Exception:  # noqa: BLE001 - hostile metaclass; untrusted by default
        return None
    if module is not None:
        try:
            origin = getattr(module, "__file__", None)
        except Exception:  # noqa: BLE001 - hostile module; fall through to find_spec
            origin = None
        if isinstance(origin, str) and origin:
            return _safe_resolve(Path(origin))
    try:
        name = finder.__module__ if isinstance(finder, type) else type(finder).__module__
    except Exception:  # noqa: BLE001 - hostile metaclass; untrusted by default
        return None
    if not isinstance(name, str) or not name:
        return None
    try:
        spec = importlib.util.find_spec(name)
    except Exception:  # noqa: BLE001 - a hostile import hook must not abort the guard
        return None
    try:
        origin = getattr(spec, "origin", None) if spec is not None else None
    except Exception:  # noqa: BLE001 - hostile spec object
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
    for constant in getattr(code, "co_consts", ()):
        if isinstance(constant, types.CodeType):
            yield from _code_objects(constant)


def _constant_signature(code: object):
    """Return a comparable summary of ``code``'s constant table.

    CPython addresses constants by *index*, not by value, so the marshalled
    ``co_code`` of two functions is byte-identical when they differ only in
    what their constants *are*.  Comparing ``co_code`` alone therefore cannot
    tell a genuine ``find_spec`` from a hostile one that merely has the same
    bytecode shape and serves a different path.

    Nested code objects are reduced to their own constant signature rather
    than compared by identity, because the recompiled copy is a different
    object even when the source is identical.  Comparing code objects with
    ``==`` would always fail for them, which is why the signature recurses
    instead.
    """

    return tuple(
        _constant_signature(item)
        if isinstance(item, types.CodeType)
        else (type(item).__name__, repr(item))
        for item in getattr(code, "co_consts", ())
    )


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

    Bytecode alone is not enough to compare, because CPython addresses
    constants by index: two functions whose ``co_code`` is byte-identical can
    still embed different values, and a hostile ``find_spec`` sharing the shape
    of a genuine one would otherwise corroborate while serving a foreign path.
    The constant table is therefore part of the comparison, summarised
    recursively so a nested code object's own constants count too.

    This is a strong corroboration, not a proof of provenance.  It closes the
    case where a hostile finder differs from a genuine one only in the values it
    serves; it does not claim to defeat an attacker who can make the named file
    itself contain the hostile source.
    """

    try:
        target = function.__code__
        source = source_file.read_text(encoding="utf-8")
        tree = compile(source, str(source_file), "exec")
    except Exception:  # noqa: BLE001 - unreadable/undecodable/uncompilable; untrusted
        return False
    target_signature = _constant_signature(target)
    for candidate in _code_objects(tree):
        if (
            candidate.co_name == target.co_name
            and candidate.co_code == target.co_code
            and candidate.co_names == target.co_names
            and candidate.co_varnames == target.co_varnames
            and _constant_signature(candidate) == target_signature
        ):
            return True
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
    except Exception:  # noqa: BLE001 - hostile descriptor; untrusted by default
        return None
    if function is None:
        return None
    # A class is installed on meta_path with find_spec called unbound, so the
    # attribute is a plain function; an instance yields a bound method.
    try:
        function = getattr(function, "__func__", function)
        code = function.__code__
        filename = code.co_filename
    except Exception:  # noqa: BLE001 - not a Python function; untrusted by default
        return None
    if not isinstance(filename, str) or not filename:
        return None
    # Definitions from an interactive session, an exec() of a string, or a
    # doctest have no real file behind them and can never be an install.
    if filename.startswith("<") and filename.endswith(">"):
        return None
    return _safe_resolve(Path(filename))


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

    Residual boundary: an attacker who can write a file into this
    interpreter's site-packages, or who runs code before the guard does, is out
    of scope -- at that point they own the interpreter rather than the finder.
    """

    if _is_trusted_stdlib_finder(finder):
        return True
    code_file = _finder_code_file(finder)
    if code_file is None:
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
    except Exception:  # noqa: BLE001 - hostile descriptor; untrusted by default
        return False
    if function is None:
        return False
    try:
        function = getattr(function, "__func__", function)
    except Exception:  # noqa: BLE001 - hostile descriptor; untrusted by default
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

    offenders: list[tuple[object, Path | None]] = []
    for finder in list(sys.meta_path):
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
    except Exception:  # noqa: BLE001 - a hostile metaclass must not abort the report
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
    except (OSError, importlib.metadata.PackageNotFoundError):
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
    except Exception:  # noqa: BLE001 - metadata is advisory here
        return roots
    for package in packages:
        for owner in distributions.get(package, ()) or ():
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


def _resolve_origin(package: str) -> tuple[Path | None, str]:
    """Import ``package`` and return ``(module file, error)`` for its origin."""

    try:
        module = __import__(package)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - a failed import is a finding
        return None, f"import failed: {type(exc).__name__}: {_describe(exc)}"
    origin = getattr(module, "__file__", None)
    if origin is None:
        # Namespace packages legitimately report ``None``; their search path is
        # the only available statement of where they resolved.
        locations = list(getattr(module, "__path__", ()) or ())
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
        return f"<unprintable {type(value).__name__}>"


def _foreign_path_locations(
    package: str, allowed_roots: list[Path], origin: Path
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
    """

    module = sys.modules.get(package)
    if module is None:
        return [], []
    # Judge the module that actually corresponds to the origin being reported.
    # A caller may have resolved the origin from install metadata rather than
    # from the live ``sys.modules`` entry (the release lane does exactly that),
    # and then the ambient module is some *other* tree's package.  Comparing
    # that module's portions against these roots would report a foreign path
    # for a package that is genuinely installed from an allowed root.
    module_origin = getattr(module, "__file__", None)
    if module_origin is not None:
        try:
            if Path(module_origin).resolve() != origin:
                return [], []
        except (OSError, ValueError, RuntimeError):
            return [], []
    outside: list[Path] = []
    unusable: list[str] = []
    for location in getattr(module, "__path__", ()) or ():
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
    """Return a JSON-serializable report of every package's resolved origin."""

    root = project_root.resolve()
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
        foreign, unusable = _foreign_path_locations(package, allowed_roots, origin)
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
