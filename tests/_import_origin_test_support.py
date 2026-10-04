"""Shared factories used by the import-origin acceptance tests."""

from __future__ import annotations

import contextlib
import json
import re
import sys
import types
from pathlib import Path

import scripts.check_import_origins as origins


def _make_package(root: Path, package: str) -> Path:
    """Create a minimal importable package under ``root`` and return its dir."""

    directory = root / package
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_text("", encoding="utf-8")
    return directory


@contextlib.contextmanager
def _module_installed(name: str, module: types.ModuleType):
    """Temporarily install ``module`` under ``name`` in ``sys.modules``.

    Used by the rows that drive the real CLI, where pytest's ``monkeypatch``
    fixture is not in scope.
    """

    original = sys.modules.get(name)
    sys.modules[name] = module
    try:
        yield module
    finally:
        if original is None:
            del sys.modules[name]
        else:
            sys.modules[name] = original


@contextlib.contextmanager
def _import_replaced(replacement):
    """Temporarily swap the built-in ``__import__`` for ``replacement``.

    ``_resolve_origin`` imports the package it is auditing, so the only way to
    drive a hostile loader is to replace the importer itself.  ``monkeypatch``
    is not in scope for the rows that drive the real CLI, and patching
    ``builtins.__import__`` for the duration of a test is safe here because
    the guard is single-threaded.
    """

    import builtins

    original = builtins.__import__
    builtins.__import__ = replacement
    try:
        yield
    finally:
        builtins.__import__ = original


def _fake_install(distributions, package_dirs, installed_from):
    """Build a stand-in for ``importlib.metadata`` describing one install.

    ``installed_from`` is the directory pip recorded in ``direct_url.json``,
    which is the whole discriminator between this checkout's install and a
    sibling worktree's.
    """

    class _Distribution:
        def __init__(self, name):
            self._name = name

        def read_text(self, filename):
            if filename != "direct_url.json":
                return None
            return json.dumps({"dir_info": {"editable": True}, "url": f"file://{installed_from}"})

        def locate_file(self, name):
            # A real ``Distribution.locate_file`` returns a path for any name;
            # only the directories actually installed should be honored, so
            # anything else falls back to an unrelated location.
            return package_dirs.get(name, installed_from / "unrelated" / name)

    table = {name: _Distribution(name) for name in distributions}
    # The real ``importlib.metadata.distribution`` normalizes the name it is
    # asked for (PEP 503), so a lookup of "pyboy" finds an install recorded as
    # "PyBoy".  Key the stand-in the same way or the normalization the checker
    # performs cannot be exercised.
    return {
        re.sub(r"[-_.]+", "-", name).lower(): distribution for name, distribution in table.items()
    }


def _distribution_lookup(table):
    """Return a stand-in for ``importlib.metadata.distribution``.

    A missing name must raise ``PackageNotFoundError``, as the real API does;
    a plain ``dict.__getitem__`` would raise ``KeyError`` and hide the
    checker's normalized-then-literal fallback path behind an unrelated error.
    """

    def lookup(name):
        try:
            return table[name]
        except KeyError:
            raise origins.importlib.metadata.PackageNotFoundError(name) from None

    return lookup
