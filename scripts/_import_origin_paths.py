"""Paths helpers for the import-origin guard.

The facade namespace is passed for calls through mutable seams, keeping
monkeypatch behavior and provenance decisions live at the public boundary.
"""

from __future__ import annotations

import importlib.metadata
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

OriginApi = dict[str, Any]


def _site_packages_roots(*, api: OriginApi) -> list[Path]:
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


def _safe_resolve(candidate: Path, *, api: OriginApi) -> Path | None:
    """Resolve ``candidate``, returning ``None`` when it is not a usable path."""

    try:
        return Path(candidate).resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def _is_within(candidate: Path, root: Path, *, strict: bool = False, api: OriginApi) -> bool:
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


def _normalise_distribution_name(name: str, *, api: OriginApi) -> str:
    """Return the PEP 503 normalized form of a distribution name."""

    return re.sub(r"[-_.]+", "-", name).lower()


def _distribution(distribution_name: str, *, api: OriginApi):
    """Return the installed distribution owning ``distribution_name``.

    ``packages_distributions`` reports owners in the form recorded at install
    time -- the native CI lane's wheel is named ``PyBoy`` -- while lookups by
    an ad-hoc spelling must still resolve.  Try the normalized name, then the
    literal one, so a case or separator difference cannot silently skip a
    distribution that would otherwise be admitted.
    """

    for candidate in dict.fromkeys(
        (
            api["_normalise_distribution_name"](distribution_name),
            distribution_name,
        )
    ):
        try:
            return importlib.metadata.distribution(candidate)
        except importlib.metadata.PackageNotFoundError:
            continue
    raise importlib.metadata.PackageNotFoundError(distribution_name)


def _staging_root(project_root: Path, *, api: OriginApi) -> Path:
    """Return the checkout-local staging directory used by the native build."""

    return project_root / "build"


def _installed_from(distribution_name: str, project_root: Path, *, api: OriginApi) -> Path | None:
    """Return the checkout an installed distribution was built from.

    ``direct_url.json`` is written by pip for both editable and local installs
    and names the directory the install was produced from.  A distribution
    without that record cannot be attributed to a checkout, so it contributes
    no allowed site-packages root.  A record naming a path that cannot be
    resolved is treated the same way: the install is unattributable, not
    crashing the run.
    """

    try:
        distribution = api["_distribution"](distribution_name)
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


def _is_this_checkout(project_root: Path, source: Path | None, *, api: OriginApi) -> bool:
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
    return api["_is_within"](source, api["_staging_root"](root), strict=True)


def _allowed_roots(
    project_root: Path,
    packages: tuple[str, ...],
    *,
    api: OriginApi,
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
            if not api["_is_this_checkout"](
                project_root, api["_installed_from"](owner, project_root)
            ):
                continue
            try:
                located = api["_distribution"](owner).locate_file(package)
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
            resolved, _ = api["_resolve_path"](located, package)
            if resolved is not None:
                roots.append(resolved)
    return roots
