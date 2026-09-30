#
# License: See the vendored PyBoy LICENSE.md file (LGPL-3.0-only).
"""Checksum-verified assembly of bounded .pxi components into one module source.

The components of a split module (pyboy.py, pyboy/core/mb.py, pyboy/core/lcd.py)
are never imported as separate modules. They are concatenated back into the
exact pre-split bytes of the single module they belong to, so one namespace, one
Cython translation unit, private name mangling and public identity are all
preserved.

Each component is pinned by SHA-256 in a literal-only manifest, and the joined
result is pinned by a whole-source digest. A missing, edited, reordered, blanked
or symlinked component therefore fails loudly instead of silently producing a
partial or wrong implementation.

This module deliberately has no PyBoy imports: the native build loads it before
PyBoy's extensions exist.
"""
from __future__ import annotations

import ast
import hashlib
import linecache
import os
import re
import tempfile
from pathlib import Path

MANIFEST_SUFFIX = "_components_manifest.py"
_KEYS = {"FORMAT_VERSION", "MAX_LINES", "SOURCE_SHA256", "SOURCE_PARTS"}
_NAME = re.compile(r"[a-z]+_components/[a-z]+_[a-z]+_[0-9]{3}_[0-9a-f]{12}\.pxi\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}")


def _manifest(directory, stem):
    """Read only literal assignments; never execute a manifest as Python code."""
    path = Path(directory) / (stem + MANIFEST_SUFFIX)
    tree = ast.parse(path.read_bytes().decode("utf-8"), filename=str(path))
    values = {}
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            raise ValueError("component manifest must contain literal assignments only")
        key = node.targets[0].id
        if key not in _KEYS or key in values:
            raise ValueError("unknown or duplicate component manifest key: " + key)
        values[key] = ast.literal_eval(node.value)
    if set(values) != _KEYS or values["FORMAT_VERSION"] != 1:
        raise ValueError("unsupported or incomplete component manifest: " + stem)
    limit = values["MAX_LINES"]
    if type(limit) is not int or not 1 <= limit < 1000:
        raise ValueError("component line bound must be below 1000")
    return values


def _join(directory, entries, expected_digest, limit, stem):
    if not isinstance(entries, tuple) or not entries:
        raise ValueError("component manifest must be a nonempty tuple")
    if not isinstance(expected_digest, str) or not _DIGEST.fullmatch(expected_digest):
        raise ValueError("invalid component whole-source digest")
    chunks, seen = [], set()
    for entry in entries:
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise ValueError("invalid component entry")
        name, digest = entry
        if (not isinstance(name, str) or not _NAME.fullmatch(name)
                or name in seen or not isinstance(digest, str)
                or not _DIGEST.fullmatch(digest)):
            raise ValueError("invalid or duplicate component path/hash")
        seen.add(name)
        path = Path(directory) / name
        # A source snapshot is self-contained: reject symlinks and traversal.
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("components cannot be symbolic links")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("component checksum mismatch: " + name)
        if len(data.decode("utf-8").splitlines()) > limit:
            raise ValueError("component exceeds its line bound: " + name)
        chunks.append(data)
    result = b"".join(chunks)
    if hashlib.sha256(result).hexdigest() != expected_digest:
        raise ValueError("component reassembly checksum mismatch")
    return result


def assemble(directory, stem):
    """Return the exact pre-split module bytes, failing closed on any drift."""
    directory = Path(directory)
    values = _manifest(directory, stem)
    return _join(directory, values["SOURCE_PARTS"], values["SOURCE_SHA256"],
                 values["MAX_LINES"], stem)


def load_into(namespace, stem):
    """Execute the reassembled source once in the module's own namespace."""
    filename = namespace["__file__"]
    try:
        source = assemble(Path(filename).parent, stem)
        text = source.decode("utf-8")
        code = compile(text, filename, "exec", dont_inherit=True)
    except (OSError, ValueError, SyntaxError) as exc:
        raise ImportError("cannot load " + stem + " components: " + str(exc)) from exc
    # Keep inspect.getsource and tracebacks on the original logical source lines.
    linecache.cache[filename] = (len(source), None, text.splitlines(keepends=True), filename)
    exec(code, namespace, namespace)


def _write_if_changed(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_bytes() == data:
        return
    fd, temp = tempfile.mkstemp(prefix=".component-native-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def stage_native_source(directory, stem, relative, declarations=None, build_root="build/components"):
    """Stage reassembled compiler input outside the importable package.

    The returned path is compiler input, not a new import path. Extension names
    and every cimport stay unchanged; the augmenting .pxd is staged alongside.
    """
    directory = Path(directory)
    destination = Path(build_root) / relative
    resolved_source = directory.resolve()
    resolved_destination = destination.resolve()
    if resolved_destination == resolved_source or resolved_source in resolved_destination.parents:
        raise ValueError("component staging must be outside the importable source package")
    source = assemble(directory, stem)
    _write_if_changed(destination, source)
    if declarations is not None:
        _write_if_changed(destination.with_suffix(".pxd"), Path(directory, declarations).read_bytes())
    return destination
