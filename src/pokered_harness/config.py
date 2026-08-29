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


SUPPORTED_ROM_VERSIONS: frozenset[str] = frozenset({"red", "blue", "yellow"})


class VersionsConfigError(ValueError):
    """Raised when VERSIONS.md is missing an expected pin or malformed."""

    code = "invalid_versions_config"


@dataclass(frozen=True, slots=True)
class VersionsConfig:
    rom_sha1: str
    pyboy_version: str


_SHA1_ROW = re.compile(r"^\|\s*SHA-?1\s*\|\s*`([0-9A-Fa-f]{40})`\s*\|", re.MULTILINE)
_PYBOY_ROW = re.compile(r"^\|\s*PyBoy\s*\|\s*`([^`]+)`\s*\|", re.MULTILINE)


def load_versions(path: str | Path | None = None) -> VersionsConfig:
    """Parse ``VERSIONS.md`` and return the machine-readable pins.

    When ``path`` is ``None``, resolves the explicit
    ``POKERED_VERSIONS_PATH`` override, then the repository's VERSIONS.md,
    and finally the current working directory. This keeps an MCP process
    independent of the directory from which its client launched it while
    retaining a useful fallback for installed copies.
    """
    target = Path(path) if path is not None else default_versions_path()
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


def default_versions_path() -> Path:
    """Return the best available machine-readable versions file path."""
    explicit = _optional_env("POKERED_VERSIONS_PATH")
    if explicit is not None:
        return Path(explicit).expanduser()

    # In a source checkout this is the project root. In a wheel it normally
    # does not exist, so the cwd fallback below remains useful for a deployed
    # package that ships VERSIONS.md beside its launcher.
    project_root = Path(__file__).resolve().parents[2]
    bundled = project_root / "VERSIONS.md"
    if bundled.is_file():
        return bundled
    return Path.cwd() / "VERSIONS.md"


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

    def validate(self, *, role: str = "session") -> None:
        """Validate required/paired environment values before boot."""
        has_rom = self.rom_path is not None
        has_sym = self.sym_path is not None
        if has_rom != has_sym:
            raise VersionsConfigError(
                f"{role} requires both ROM and symbol paths; got "
                f"rom_path={self.rom_path!r}, sym_path={self.sym_path!r}"
            )
        if self.rom_sha1 is not None:
            _validate_sha1(self.rom_sha1, f"{role} ROM SHA-1")
        if self.version not in SUPPORTED_ROM_VERSIONS:
            raise VersionsConfigError(
                f"{role} ROM version must be one of "
                f"{sorted(SUPPORTED_ROM_VERSIONS)}, got {self.version!r}"
            )

    def resolved_paths(self, base_dir: str | Path | None = None) -> tuple[Path | None, Path | None]:
        """Resolve relative ROM/SYM paths against an explicit launch root."""
        base = Path(base_dir).expanduser() if base_dir is not None else Path.cwd()
        return (
            _resolve_path(self.rom_path, base),
            _resolve_path(self.sym_path, base),
        )


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
        rom_path=_optional_env("POKERED_ROM_PATH"),
        sym_path=_optional_env("POKERED_SYM_PATH"),
        rom_sha1=_optional_env("POKERED_ROM_SHA1"),
        version=_derive_version(
            _optional_env("POKERED_ROM_PATH"),
            _optional_env("POKERED_ROM_VERSION"),
        ),
    )


def load_peer_env() -> SessionEnv:
    """Read the peer session's env vars (``POKERED_PEER_*``).

    All three peer vars are optional. When ``rom_path`` or ``sym_path`` is
    ``None`` the server runs in single-session mode; link-cable tools will
    refuse to ``pair`` until the peer is configured.
    """
    return SessionEnv(
        rom_path=_optional_env("POKERED_PEER_ROM_PATH"),
        sym_path=_optional_env("POKERED_PEER_SYM_PATH"),
        rom_sha1=_optional_env("POKERED_PEER_ROM_SHA1"),
        version=_derive_version(
            _optional_env("POKERED_PEER_ROM_PATH"),
            _optional_env("POKERED_PEER_ROM_VERSION"),
        ),
    )


def _optional_env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _resolve_path(value: str | None, base: Path) -> Path | None:
    if value is None:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve(strict=False)


_SHA1_RE = re.compile(r"^[0-9a-fA-F]{40}$")


def _validate_sha1(value: str, label: str) -> None:
    if not _SHA1_RE.fullmatch(value.strip()):
        raise VersionsConfigError(
            f"{label} must be exactly 40 hexadecimal characters, got {value!r}"
        )


__all__ = [
    "SUPPORTED_ROM_VERSIONS",
    "SessionEnv",
    "VersionsConfig",
    "VersionsConfigError",
    "default_versions_path",
    "load_peer_env",
    "load_primary_env",
    "load_versions",
]
