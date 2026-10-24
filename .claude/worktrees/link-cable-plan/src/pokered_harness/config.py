"""Load pinned version data from ``VERSIONS.md`` and read per-session
env vars for the MCP server entry point.

The version pin file is primarily human documentation, but the SHA-1 and
PyBoy pin need to be machine-readable so callers don't duplicate them.
The parser is deliberately lenient: it scans the file for exact-shape
rows and ignores everything else. If the format ever drifts, the failure
is loud (``VersionsConfigError``) rather than silent.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


class VersionsConfigError(ValueError):
    """Raised when VERSIONS.md is missing an expected pin or malformed."""


@dataclass(frozen=True, slots=True)
class VersionsConfig:
    rom_sha1: str
    pyboy_version: str


_SHA1_ROW = re.compile(r"^\|\s*SHA-?1\s*\|\s*`([0-9A-Fa-f]{40})`\s*\|", re.MULTILINE)
_PYBOY_ROW = re.compile(r"^\|\s*PyBoy\s*\|\s*`([^`]+)`\s*\|", re.MULTILINE)


def load_versions(path: str | Path | None = None) -> VersionsConfig:
    """Parse ``VERSIONS.md`` and return the machine-readable pins.

    When ``path`` is ``None``, looks for ``VERSIONS.md`` relative to the
    current working directory. That's the convention used by the MCP
    server's entry point.
    """
    target = Path(path) if path is not None else Path("VERSIONS.md")
    if not target.is_file():
        raise VersionsConfigError(f"versions file not found: {target}")

    text = target.read_text(encoding="utf-8")

    sha = _SHA1_ROW.search(text)
    if sha is None:
        raise VersionsConfigError(
            f"no SHA-1 row found in {target} — expected `| SHA-1 | \\`...\\` |`"
        )

    pyboy = _PYBOY_ROW.search(text)
    if pyboy is None:
        raise VersionsConfigError(
            f"no PyBoy version row found in {target}"
        )

    return VersionsConfig(
        rom_sha1=sha.group(1).lower(),
        pyboy_version=pyboy.group(1),
    )


# -- per-session env vars ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionEnv:
    """Per-session env-var bundle (primary OR peer).

    ``rom_path`` / ``sym_path`` are the only required fields for a working
    session; ``rom_sha1`` is optional (None → skip hash enforcement). A
    peer bundle with ``rom_path is None`` means "peer not configured" and
    is the signal the MCP server uses to skip peer construction.
    """

    rom_path: str | None
    sym_path: str | None
    rom_sha1: str | None
    version: str


def _derive_version(rom_path: str | None, explicit: str | None) -> str:
    """Return a lowercase version string for ``rom_path``.

    Heuristic — if an explicit override is set (e.g. via
    ``POKERED_ROM_VERSION``), use it; otherwise inspect the filename.
    Unknown filenames default to ``"red"``.
    """
    if explicit:
        return explicit.strip().lower()
    if not rom_path:
        return "red"
    lower = rom_path.lower()
    if "yellow" in lower:
        return "yellow"
    if "blue" in lower:
        return "blue"
    return "red"


def load_primary_env() -> SessionEnv:
    """Read the primary session's env vars (``POKERED_*``).

    ``rom_path`` / ``sym_path`` may be ``None`` here — the MCP ``main()``
    entry point validates them; library callers can consume the bundle
    even when incomplete.
    """
    return SessionEnv(
        rom_path=os.environ.get("POKERED_ROM_PATH"),
        sym_path=os.environ.get("POKERED_SYM_PATH"),
        rom_sha1=os.environ.get("POKERED_ROM_SHA1"),
        version=_derive_version(
            os.environ.get("POKERED_ROM_PATH"),
            os.environ.get("POKERED_ROM_VERSION"),
        ),
    )


def load_peer_env() -> SessionEnv:
    """Read the peer session's env vars (``POKERED_PEER_*``).

    All three peer vars are optional. When ``rom_path`` or ``sym_path`` is
    ``None`` the server runs in single-session mode; link-cable tools will
    refuse to ``pair`` until the peer is configured.
    """
    return SessionEnv(
        rom_path=os.environ.get("POKERED_PEER_ROM_PATH"),
        sym_path=os.environ.get("POKERED_PEER_SYM_PATH"),
        rom_sha1=os.environ.get("POKERED_PEER_ROM_SHA1"),
        version=_derive_version(
            os.environ.get("POKERED_PEER_ROM_PATH"),
            os.environ.get("POKERED_PEER_ROM_VERSION"),
        ),
    )


__all__ = [
    "SessionEnv",
    "VersionsConfig",
    "VersionsConfigError",
    "load_peer_env",
    "load_primary_env",
    "load_versions",
]
