"""Resolution helpers for the import-origin guard.

The facade namespace is passed for calls through mutable seams, keeping
monkeypatch behavior and provenance decisions live at the public boundary.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

OriginApi = dict[str, Any]


def _resolve_origin(package: str, *, api: OriginApi) -> tuple[Path | None, str]:
    """Import ``package`` and return ``(module file, error)`` for its origin."""

    try:
        module = __import__(package)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - a failed import is a finding
        return None, f"import failed: {api['_type_name'](exc)}: {api['_describe'](exc)}"
    # Publish the module this origin was read from, so the portion check can
    # use it without reading ``__file__`` a second time.  ``__file__`` is
    # mutable interpreter state and a hostile path-like can answer
    # differently on each read; re-reading it here let a first read matching
    # the origin be followed by one that did not, and the portion check then
    # returned no findings at all -- a false PASS for a package that really
    # does have a foreign ``__path__``.
    api["_RESOLVED_MODULES"][package] = module
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
        return (
            None,
            f"__file__ could not be read: {api['_type_name'](exc)}: {api['_describe'](exc)}",
        )
    if origin is None:
        # Namespace packages legitimately report ``None``; their search path is
        # the only available statement of where they resolved.
        try:
            locations = list(getattr(module, "__path__", ()) or ())
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:  # noqa: BLE001 - hostile getter is a finding
            return (
                None,
                f"__path__ could not be read: {api['_type_name'](exc)}: {api['_describe'](exc)}",
            )
        if not locations:
            return None, "module exposed neither __file__ nor __path__"
        candidate = locations[0]
    else:
        candidate = origin
    return api["_resolve_path"](candidate, package)


def _resolve_path(candidate: object, package: str, *, api: OriginApi) -> tuple[Path | None, str]:
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
        return None, f"origin is not a usable path: {api['_describe'](exc)}"


def _foreign_path_locations(
    package: str, allowed_roots: list[Path], origin: Path, module: object = None, *, api: OriginApi
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
        return [], [
            f"__path__ could not be read: {api['_type_name'](exc)}: {api['_describe'](exc)}"
        ]
    for location in portions:
        if not any(api["_is_within"](location, allowed, strict=False) for allowed in allowed_roots):
            # Coerce through ``_resolve_path`` rather than ``Path(...).resolve``
            # directly: a portion is untrusted interpreter output, so it can be
            # the wrong type, embed a NUL, or be an object whose ``__fspath__``
            # raises anything at all.  An unusable portion is reported as the
            # path it claims to be and still fails the caller closed, and a
            # hostile ``__str__`` cannot escape while the reason is rendered.
            resolved, error = api["_resolve_path"](location, package)
            if resolved is not None:
                outside.append(resolved)
            else:
                unusable.append(f"{api['_describe'](location)}: {error}")
    return outside, unusable
