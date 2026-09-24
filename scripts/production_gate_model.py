"""Constants, dataclasses, execution plans and node-id normalisation.

Split from ``scripts/production_gate.py`` for issue #124 with no behavior
change: the code below is copied verbatim except that calls to facade-owned,
monkeypatch-patched entry points resolve through ``_entry`` so attribute patches
on the loaded gate module stay visible.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


KNOWN_ROM_FILES: tuple[tuple[str, Path], ...] = (
    ("red-stock", Path("red/pokemon-red.gb")),
    ("red-color", Path("red/pokemon-red-color.gb")),
    ("blue-stock", Path("blue/pokemon-blue.gb")),
    ("blue-color", Path("blue/pokemon-blue-color.gb")),
    ("yellow", Path("yellow/pokemon-yellow.gbc")),
)


KNOWN_SYMBOL_FILES: tuple[tuple[str, Path], ...] = (
    ("red", Path("red/pokemon-red.sym")),
    ("blue", Path("blue/pokemon-blue.sym")),
    ("yellow", Path("yellow/pokemon-yellow.sym")),
)


REQUIRED_FIXTURES: tuple[tuple[str, Path], ...] = (
    ("red cable-club", Path("red/cable_club.state")),
    ("blue cable-club", Path("blue/cable_club.state")),
    ("yellow cable-club", Path("yellow/cable_club.state")),
    ("red cable-club slots", Path("red/cable_club-slots.state")),
    ("blue cable-club slots", Path("blue/cable_club-slots.state")),
    ("yellow cable-club slots", Path("yellow/cable_club-slots.state")),
)


REQUIRED_TIER_ASSETS = frozenset({"local", "remote", "trade", "battle", "smoke"})


OPTIONAL_TIERS = frozenset()


TIER_EXPRESSIONS: dict[str, str] = {
    "smoke": "unit or mcp_stdio",
    "unit": "unit",
    "local": "real_rom and not remote_link and not acceptance",
    "remote": "real_rom and remote_link and not acceptance",
    "trade": "real_rom and trade_acceptance",
    "battle": "real_rom and battle_acceptance",
    "timing": "timing_sensitive",
}


TIER_DESCRIPTIONS: dict[str, str] = {
    "smoke": "startup, public MCP lifecycle and nine timed ROM orientations",
    "unit": "ROM-free unit tests",
    "local": "real-ROM local/session/link tests",
    "remote": "real-ROM remote TCP/subprocess tests",
    "trade": "strict real-ROM party-swap acceptance",
    "battle": "strict real-ROM battle-turn acceptance",
    "timing": "fivefold scheduling-sensitive regression",
}


DEFAULT_TIERS = ("unit", "local", "remote", "trade", "battle", "timing")


DEFAULT_TIMEOUT_SECONDS: dict[str, float] = {
    "smoke": 900.0,
    "unit": 900.0,
    "local": 3600.0,
    "remote": 3600.0,
    "trade": 3600.0,
    "battle": 3600.0,
    "timing": 900.0,
}


# Strict acceptance rows each run an emulator pair (two PyBoy instances for a
# local row or two child processes for a TCP row).  A conservative default is
# important because oversubscribing the host changes emulator scheduling and
# can turn a valid cross-version run into a timing failure.  Dedicated CI can
# opt into more parallelism with ``--matrix-workers`` after measuring its
# available CPU budget.
DEFAULT_MATRIX_WORKERS = 1


MATRIX_CASE_TIMEOUT_SECONDS: dict[str, float] = {
    "trade": 900.0,
    "battle": 1200.0,
}


MATRIX_AGGREGATE_GRACE_SECONDS = 10.0


MATRIX_CLEANUP_TIMEOUT_SECONDS = 5.0


MATRIX_READER_JOIN_TIMEOUT_SECONDS = 1.0


COLLECTION_TIMEOUT_SECONDS = 300.0


# The smoke is an additional probe before the unchanged qualification tiers.
# Its explicit selectors keep it small; its required nodes prevent one passing
# startup check from hiding a missing timed orientation or public workflow.
SMOKE_SELECTORS = (
    "tests/test_pyboy_link_imports.py",
    "tests/test_mcp_timed_stdio.py",
    "tests/test_mcp_stdio_integration.py",
    "tests/test_mcp_timed_rom.py",
)


SMOKE_REQUIRED_NODEIDS = frozenset(
    f"tests/test_mcp_timed_rom.py::test_timed_rom_stdio_pair[{listener}-listen-{connector}-connect]"
    for listener in ("red_color", "blue_color", "yellow")
    for connector in ("red_color", "blue_color", "yellow")
) | frozenset(
    f"tests/test_mcp_stdio_integration.py::{name}"
    for name in (
        "test_stdio_list_tools_and_call_step",
        "test_stdio_game_state_resource_is_parseable",
        "test_stdio_save_state_roundtrip_is_deterministic",
        "test_stdio_remote_link_lifecycle_and_explicit_disconnect",
    )
)


SMOKE_REQUIRED_TESTS = frozenset(
    {
        ("test_pyboy_link_imports.py", "test_selected_runtime_public_link_imports"),
        ("test_pyboy_link_imports.py", "test_selected_runtime_serial_constructor_and_pair_cleanup"),
        ("test_mcp_timed_stdio.py", "test_authored_timed_stdio_pair_frames_and_cleanup"),
        ("test_mcp_timed_stdio.py", "test_authored_timed_stdio_expected_peer_mismatch"),
        ("test_mcp_timed_rom.py", "test_rom_client_load_state_timeout_redacts_data"),
    }
)


RUNTIME_MODES = ("source", "cython")


RUNTIME_MODE_ALIASES = {"dual": "both"}


RUNTIME_MODE_CHOICES = (*RUNTIME_MODES, "both")


PYBOY_RUNTIME_MODULES = (
    "pyboy",
    "pyboy.pyboy",
    "pyboy.utils",
    "pyboy.core.mb",
    "pyboy.core.serial",
)


CYTHON_RUNTIME_MODULES = tuple(name for name in PYBOY_RUNTIME_MODULES if name != "pyboy")


EVIDENCE_SCHEMA_VERSION = 1


EVIDENCE_REPORT_FILENAME = "gate-report.json"


EVIDENCE_TEXT_FILENAME = "gate-report.txt"


EVIDENCE_MANIFEST_FILENAME = "evidence-manifest.json"


FIXTURE_MANIFEST_RELATIVE_PATH = Path("release-evidence/fixture-manifest.json")


CERTIFIED_FIXTURE_IDS = frozenset(
    {
        "red-color-ordinary",
        "red-color-battle",
        "red-color-slots",
        "blue-color-ordinary",
        "blue-color-battle",
        "blue-color-slots",
        "yellow-cgb-ordinary",
        "yellow-cgb-battle",
        "yellow-cgb-slots",
    }
)


GATE_CONTROLLED_ENVIRONMENT = (
    "PYTEST_ADDOPTS",
    "PYTEST_PLUGINS",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
    "POKERED_GATE_REPORT",
    "POKERED_GATE_PROGRESS_REPORT",
)


# The gate must not inherit arbitrary pytest plugins or configuration.  The
# async integration tests still need their pinned plugin, so load that plugin
# explicitly alongside the gate report plugin in every child process.
PYTEST_GATE_ARGUMENTS = (
    "--strict-config",
    "--strict-markers",
    "-p",
    "pytest_asyncio.plugin",
    "-p",
    "tests._gate_report",
)


_SHA1_RE = re.compile(r"^\|\s*SHA-?1\s*\|\s*`([0-9A-Fa-f]{40})`\s*\|")


_PATH_RE = re.compile(r"^\|\s*Path\s*\|\s*`([^`]+)`\s*\|")


_SYMBOL_SHA1_RE = re.compile(r"^\|\s*Symbol\s+SHA-?1\s*\|\s*`([0-9A-Fa-f]{40})`\s*\|")


_SYMBOL_PATH_RE = re.compile(r"^\|\s*Symbols?\s*\|\s*`([^`]+)`\s*\|")


_PYBOY_RE = re.compile(
    r"^\|\s*PyBoy\s*\|\s*`([^`]+)`"
    r"(?:\s*\+\s*fork\s*`([0-9A-Fa-f]{40})`)?"
)


_CREDENTIAL_TEXT_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|auth(?:orization)?|bearer|"
    r"credential|password|passwd|private[_-]?key|secret|token)\b"
    r"(?:\s*[:=]\s*|\s+)(?:bearer\s+)?[^\s,;]+"
)


_URI_CREDENTIAL_RE = re.compile(r"(?i)(https?://[^/\s:@]+):[^@\s]+@")


_BYTE_LITERAL_RE = re.compile(
    r"(?is)\bb(?:'(?:\\.|[^'\\])*(?:'|\\?\Z)|\"(?:\\.|[^\"\\])*(?:\"|\\?\Z))"
)


_LONG_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9+/=_-]{128,}(?![A-Za-z0-9])")


_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9>])(?:[A-Za-z]:[\\/]|/)[^\s,;()]+")


_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9>])[A-Za-z]:[\\/]"
    r"(?:[^\\/\r\n,;()]+[\\/])*"
    r"[^\s\\/\r\n,;()]+"
)


_SPACED_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9>])/(?:[^/\r\n,;()]+/)*"
    r"[^\s/\r\n,;()]+"
)


_QUOTED_ABSOLUTE_PATH_RE = re.compile(
    r"(?P<quote>[\"'])"
    r"(?P<path>(?:[A-Za-z]:[\\/]|/)[^\"'\r\n]+)"
    r"(?P=quote)"
)


def _asset_key(value: str | Path) -> Path:
    """Normalize a documented repository-relative asset path."""

    parts = [part for part in str(value).replace("\\", "/").split("/") if part]
    if parts and parts[0] == ".":
        parts = parts[1:]
    if parts and parts[0].lower() == "rom":
        parts = parts[1:]
    return Path(*parts)


@dataclass(frozen=True)
class AssetRecord:
    """Observed state for one ROM, symbol, or derived fixture."""

    label: str
    kind: str
    path: str
    status: str
    size: int | None = None
    expected_sha1: str | None = None
    actual_sha1: str | None = None


@dataclass
class Counts:
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    xfailed: int = 0
    xpassed: int = 0
    errors: int = 0

    @classmethod
    def from_report(cls, payload: dict[str, Any]) -> Counts:
        raw = payload.get("counts")
        if not isinstance(raw, dict):
            raise TypeError("pytest report has no counts object")

        values: dict[str, int] = {}
        for field_name in cls.__dataclass_fields__:
            value = raw.get(field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(
                    f"pytest report count {field_name!r} must be a non-negative integer"
                )
            values[field_name] = value

        return cls(**values)


@dataclass(frozen=True)
class GateReport:
    """Validated outcome data emitted by the pytest gate plugin."""

    counts: Counts
    skip_reasons: dict[str, int] = field(default_factory=dict)
    nodeids: tuple[str, ...] = ()
    collection_errors: tuple[dict[str, str], ...] = ()
    collection_skips: tuple[dict[str, str], ...] = ()
    collection_only: bool = False
    error: str = ""
    failed_records: tuple[dict[str, str], ...] = ()


@dataclass
class MatrixCaseResult:
    """Retained result for one independently scheduled acceptance row."""

    nodeid: str
    status: str
    returncode: int | None
    duration_seconds: float
    counts: Counts = field(default_factory=Counts)
    reason: str = ""
    output_tail: str = ""
    deadline_seconds: float = 0.0
    report_kind: str = "final"
    partial: bool = False


@dataclass
class FailureDetail:
    """One redacted case failure; character counts describe sanitized input."""

    iteration: int
    nodeid: str
    outcome: str
    reason: str
    original_chars: int
    omitted_chars: int
    truncated: bool
    nodeid_original_chars: int
    nodeid_omitted_chars: int


@dataclass
class TierResult:
    name: str
    description: str
    expression: str
    required: bool
    status: str
    counts: Counts = field(default_factory=Counts)
    returncodes: list[int] = field(default_factory=list)
    duration_seconds: float = 0.0
    skip_reasons: dict[str, int] = field(default_factory=dict)
    command: list[str] | None = None
    output_tail: str = ""
    reason: str = ""
    iteration_failures: list[str] = field(default_factory=list)
    selected_nodeids: list[str] = field(default_factory=list)
    case_results: list[MatrixCaseResult] = field(default_factory=list)
    failure_details: list[FailureDetail] = field(default_factory=list)
    failure_details_omitted: int = 0


@dataclass
class CollectionResult:
    """Result for one supported pytest collection entry point."""

    name: str
    command: list[str]
    status: str
    returncode: int | None
    nodeids: tuple[str, ...] = ()
    duration_seconds: float = 0.0
    output_tail: str = ""
    reason: str = ""


@dataclass
class RuntimeGateResult:
    """Complete gate result for one explicit PyBoy runtime mode."""

    mode: str
    runtime: dict[str, Any]
    collections: list[CollectionResult]
    fixture_manifest: dict[str, Any]
    matrix_audit: dict[str, Any]
    tiers: list[TierResult]
    gate_problems: list[str] = field(default_factory=list)
    execution_plan: dict[str, Any] = field(default_factory=dict)
    # A cancellation delivered *after* a tier finished loading its evidence
    # cannot be represented by that tier's status without rewriting work that
    # really ran.  It is therefore carried separately so the run is still
    # reported as cancelled (never a clean PASS) while the executed tier keeps
    # its own counts, failures, and rows.
    cancellation: str = ""
    # Durable record of the tiers whose evidence was fully loaded, keyed by
    # tier name.  ``run_prepared_tier`` writes here before its frame can be
    # unwound by a cancellation, so every downstream handoff - the dispatch
    # helper, the per-tier loops, and the CLI's interrupted-result finalizer -
    # can recover an executed tier instead of synthesizing an empty
    # ``INTERRUPTED`` row that erases the failures that really ran.  The tier
    # stays reachable even after the frame that dispatched it has been unwound,
    # because the mapping travels with ``prepared.result``, which is published
    # to the caller's accumulator before any tier is dispatched.
    completed_tiers: dict[str, TierResult] = field(default_factory=dict)


@dataclass
class PreparedRuntimeGate:
    """One probed runtime and its unchanged environment for all planned tiers."""

    result: RuntimeGateResult
    environment: dict[str, str]
    required_problems: list[str]


def build_execution_plan(
    modes: Sequence[str], selected: Sequence[str], *, early_smoke: bool, fail_fast: bool
) -> dict[str, Any]:
    """Declare ordered work before dispatch, including the smoke's evidence role."""

    modes = tuple(modes)
    selected = tuple(selected)
    if not modes or len(set(modes)) != len(modes) or set(modes) - set(RUNTIME_MODES):
        raise ValueError("execution plan requires distinct explicit runtime modes")
    if not selected or len(set(selected)) != len(selected) or set(selected) - set(TIER_EXPRESSIONS):
        raise ValueError("execution plan requires distinct known tiers")
    if type(early_smoke) is not bool or type(fail_fast) is not bool:
        raise ValueError("execution plan policies must be boolean")
    if early_smoke and "smoke" in selected:
        raise ValueError("an additional smoke cannot also be a selected tier")
    scope = (
        "smoke-only"
        if selected == ("smoke",)
        else "full"
        if set(selected) == set(DEFAULT_TIERS)
        else "selected-tiers"
    )
    steps = (
        [{"mode": mode, "tier": "smoke", "role": "additional-probe"} for mode in modes]
        if early_smoke
        else []
    )
    steps.extend(
        {"mode": mode, "tier": tier, "role": "selected-tier"} for mode in modes for tier in selected
    )
    return {
        "schema_version": 1,
        "scope": scope,
        "runtime_modes": list(modes),
        "selected_tiers": list(selected),
        "early_smoke": early_smoke,
        "fail_fast": fail_fast,
        "smoke_role": "additional-probe"
        if early_smoke
        else ("selected-scope" if "smoke" in selected else "not-selected"),
        "steps": steps,
    }


def _valid_execution_plan(plan: dict[str, Any]) -> bool:
    if not isinstance(plan, dict):
        return False
    try:
        return plan == build_execution_plan(
            plan["runtime_modes"],
            plan["selected_tiers"],
            early_smoke=plan["early_smoke"],
            fail_fast=plan["fail_fast"],
        )
    except (KeyError, TypeError, ValueError):
        return False


def _safe_execution_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct only fixed vocabulary; never retain an arbitrary dictionary."""

    if not _valid_execution_plan(plan):
        return {"schema_version": 1, "scope": "invalid"}
    return build_execution_plan(
        plan["runtime_modes"],
        plan["selected_tiers"],
        early_smoke=plan["early_smoke"],
        fail_fast=plan["fail_fast"],
    )


def _execution_plan_lines(plan: dict[str, Any]) -> list[str]:
    safe = _safe_execution_plan(plan)
    return [
        f"execution-scope: {safe['scope']}",
        f"smoke-role: {safe.get('smoke_role', 'invalid')}",
        f"fail-fast: {safe.get('fail_fast', 'invalid')}",
        "execution-order: "
        + ", ".join(
            f"{step['mode']}:{step['tier']}({step['role']})" for step in safe.get("steps", ())
        ),
    ]


def _normalize_nodeid(nodeid: str) -> str:
    """Normalize the path portion of a pytest node ID for manifest checks."""

    path, separator, test_name = nodeid.partition("::")
    normalized_path = path.replace("\\", "/")
    while normalized_path.startswith("./"):
        normalized_path = normalized_path[2:]
    return f"{normalized_path}::{test_name}" if separator else normalized_path


MAX_ITERATION_FAILURES = 32


MAX_FAILURE_DETAIL_CHARS = 65_536


MAX_FAILURE_DETAILS_CHARS = 1_048_576


MAX_FAILURE_DETAILS = 128


MAX_FAILURE_NODEID_CHARS = 1_000


MAX_ITERATION_FAILURE_CHARS = 2000


MAX_FAILED_OUTPUT_CHARS = 8000


_EVIDENCE_OMITTED = "\n[...additional failure evidence omitted...]"
