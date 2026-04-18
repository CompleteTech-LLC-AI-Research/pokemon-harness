"""Load pinned version data from ``VERSIONS.md``.

The file is primarily human documentation, but the SHA-1 and PyBoy pin
need to be machine-readable so callers don't duplicate them. The parser
is deliberately lenient: it scans the file for exact-shape rows and
ignores everything else. If the format ever drifts, the failure is loud
(``VersionsConfigError``) rather than silent.
"""

from __future__ import annotations

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
