#!/usr/bin/env bash
# Run the checks from .github/workflows/release-hygiene.yml without a hosted
# runner.  Run this from an activated Python 3.11 or 3.12 virtualenv.

set -euo pipefail

dry_run=0
if [[ "${1:-}" == "--dry-run" ]]; then
    dry_run=1
    shift
fi
if [[ $# -ne 0 ]]; then
    printf 'usage: bash scripts/run_local_ci.sh [--dry-run]\n' >&2
    exit 2
fi

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

if [[ -z "${VIRTUAL_ENV:-}" ]]; then
    printf 'an activated virtual environment is required (set VIRTUAL_ENV)\n' >&2
    exit 2
fi
if [[ ! -d "$VIRTUAL_ENV" ]]; then
    printf 'VIRTUAL_ENV does not name a directory: %s\n' "$VIRTUAL_ENV" >&2
    exit 2
fi
if ! command -v python >/dev/null 2>&1; then
    printf 'python was not found on PATH; activate the supported virtualenv first\n' >&2
    exit 2
fi
if ! python - <<'PY'
import sys

if sys.prefix == sys.base_prefix:
    raise SystemExit("python is not running from a virtual environment")
PY
then
    printf 'the active python is not running from a virtual environment\n' >&2
    exit 2
fi

python_version="$(python -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
case "$python_version" in
    3.11|3.12)
        ;;
    *)
        printf 'Python 3.11 or 3.12 is required; active interpreter is %s\n' "$python_version" >&2
        exit 2
        ;;
esac

# Keep this directory outside the checkout.  It contains the production-gate
# report and wheel, and is intentionally retained for inspection on success
# and failure; there is no cleanup trap.
temp_parent="${TMPDIR:-/tmp}"
if [[ "$temp_parent" != /* ]]; then
    temp_parent=/tmp
fi
case "$temp_parent" in
    "$repo_root"|"$repo_root"/*)
        temp_parent=/var/tmp
        ;;
esac
RUNNER_TEMP="$(mktemp -d "$temp_parent/pokered-local-ci.XXXXXX")"
export RUNNER_TEMP

report_retained() {
    local exit_code=$?
    printf '\nLocal CI temporary evidence retained at: %s\n' "$RUNNER_TEMP"
    exit "$exit_code"
}
trap report_retained EXIT

case "$RUNNER_TEMP" in
    "$repo_root"/*)
        printf 'RUNNER_TEMP must be outside the repository: %s\n' "$RUNNER_TEMP" >&2
        exit 2
        ;;
esac

printf 'Running local release-hygiene checks with Python %s\n' "$python_version"
printf 'Local CI temporary evidence: %s\n' "$RUNNER_TEMP"

if (( dry_run )); then
    printf 'Dry run requested; checks were not executed.\n'
    printf 'The full local run retains evidence at the path above.\n'
    exit 0
fi

# Reject tracked ROM-derived artifacts
set -euo pipefail
python - <<'PY'
import subprocess
from pathlib import PurePosixPath

tracked = [
    PurePosixPath(path.decode("utf-8"))
    for path in subprocess.check_output(["git", "ls-files", "-z"]).split(b"\0")
    if path
]
forbidden_roots = (
    ("rom",),
    ("roms",),
    ("tests", "fixtures", "link"),
)
forbidden_suffixes = {
    ".gb",
    ".gbc",
    ".sym",
    ".map",
    ".sav",
    ".state",
    ".ram",
    ".sqlite",
    ".sqlite3",
}
bad = [
    path.as_posix()
    for path in tracked
    if any(path.parts[: len(root)] == root for root in forbidden_roots)
    or path.suffix.lower() in forbidden_suffixes
]
if bad:
    raise SystemExit(
        "tracked ROM-derived artifacts are forbidden: " + ", ".join(sorted(bad))
    )
print(f"tracked artifact policy verified: {len(tracked)} paths inspected")
PY

# Install development dependencies
python -m pip install -e ".[dev]"

# Check packaging and gate lint/format
python -m ruff check \
    scripts/bootstrap_pyboy.py \
    scripts/coverage_report.py \
    scripts/network_concurrency_probe.py \
    scripts/produce_battle_scenario.py \
    scripts/production_gate.py \
    scripts/qualification_runner.py \
    scripts/tcp_link_matrix.py \
    scripts/validate_battle_scenarios.py \
    scripts/validate_fixture_manifest.py \
    tests/_battle_item_evidence.py \
    tests/_gate_report.py \
    tests/_rom_assets.py \
    tests/_tier_config.py \
    tests/conftest.py \
    tests/test_battle_coverage.py \
    tests/test_battle_item_evidence.py \
    tests/test_battle_scenario_fixtures.py \
    tests/test_fixture_provenance.py \
    tests/test_gate_early_smoke.py \
    tests/test_party_record_audit.py \
    tests/test_production_gate.py \
    tests/test_qualification_runner.py \
    tests/test_runtime_packaging.py
python -m ruff format --check \
    scripts/bootstrap_pyboy.py \
    scripts/coverage_report.py \
    scripts/network_concurrency_probe.py \
    scripts/produce_battle_scenario.py \
    scripts/production_gate.py \
    scripts/qualification_runner.py \
    scripts/tcp_link_matrix.py \
    scripts/validate_battle_scenarios.py \
    scripts/validate_fixture_manifest.py \
    tests/_battle_item_evidence.py \
    tests/_gate_report.py \
    tests/_rom_assets.py \
    tests/_tier_config.py \
    tests/conftest.py \
    tests/test_battle_coverage.py \
    tests/test_battle_item_evidence.py \
    tests/test_battle_scenario_fixtures.py \
    tests/test_fixture_provenance.py \
    tests/test_gate_early_smoke.py \
    tests/test_party_record_audit.py \
    tests/test_production_gate.py \
    tests/test_qualification_runner.py \
    tests/test_runtime_packaging.py

# Check runtime and link lane lint
python -m ruff check \
    src/pokered_harness/link/network_backend.py \
    src/pokered_harness/link/pyboy_link_session.py \
    src/pokered_harness/link/pair.py \
    src/pokered_harness/link/serial_bridge.py \
    src/pokered_harness/link/serial_coordinator.py \
    src/pokered_harness/link/serial_link.py \
    scripts/network_concurrency_probe.py \
    tests/test_network_backend.py \
    tests/test_fixture_provenance.py \
    tests/test_pyboy_link_session.py \
    tests/test_link_pair.py \
    tests/test_link_serial_bridge.py \
    tests/test_serial_coordinator.py \
    tests/test_serial_link.py

# Validate fixture-manifest schema
python scripts/validate_fixture_manifest.py --schema-only

# Validate battle-scenario catalog schema
python scripts/validate_battle_scenarios.py --schema-only

# Audit collected matrix and tier declarations
python scripts/tcp_link_matrix.py --format text

# Run bounded network concurrency probe
python scripts/network_concurrency_probe.py

# Run ROM-free production gate
python scripts/production_gate.py --runtime-mode source --unit-only --repeat-timing 5 \
    --evidence-dir "$RUNNER_TEMP/pokered-unit-evidence"

# Build wheel without ROM-derived artifacts
python -m pip wheel --no-deps --wheel-dir "$RUNNER_TEMP/pokered-wheels" .

# Verify wheel archive contents
set -euo pipefail
python - "$RUNNER_TEMP/pokered-wheels" <<'PY'
from pathlib import Path, PurePosixPath
import re
import sys
from zipfile import ZipFile

wheel_dir = Path(sys.argv[1])
wheels = sorted(wheel_dir.glob("*.whl"))
if len(wheels) != 1:
    raise SystemExit(f"expected exactly one wheel, found {len(wheels)}")

forbidden_suffixes = {
    ".gb", ".gbc", ".sym", ".map", ".sav", ".state", ".ram",
    ".sqlite", ".sqlite3", ".so", ".pyd", ".dylib", ".dll",
}
forbidden_parts = {"rom", "roms", "fixtures", "release-evidence", "artifacts"}
windows_absolute = re.compile(r"^[A-Za-z]:[\\/]")
bad = []
with ZipFile(wheels[0]) as archive:
    for name in archive.namelist():
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or windows_absolute.match(name)
            or ".." in path.parts
            or path.suffix.lower() in forbidden_suffixes
            or forbidden_parts.intersection(path.parts)
        ):
            bad.append(name)
if bad:
    raise SystemExit("forbidden wheel entries: " + ", ".join(sorted(bad)))
print(f"wheel archive verified: {wheels[0].name}")
PY

# Verify clean wheel installation
set -euo pipefail
clean_venv="$RUNNER_TEMP/pokered-clean-venv"
python -m venv "$clean_venv"
if [[ -x "$clean_venv/bin/python" || -f "$clean_venv/bin/python" ]]; then
    clean_python="$clean_venv/bin/python"
elif [[ -x "$clean_venv/Scripts/python.exe" || -f "$clean_venv/Scripts/python.exe" ]]; then
    # Python's Windows venv layout is used by Git Bash.
    clean_python="$clean_venv/Scripts/python.exe"
elif [[ -x "$clean_venv/Scripts/python" || -f "$clean_venv/Scripts/python" ]]; then
    clean_python="$clean_venv/Scripts/python"
else
    printf 'the clean venv has no usable Python executable\n' >&2
    exit 1
fi
"$clean_python" -m pip install --no-cache-dir "$RUNNER_TEMP"/pokered-wheels/*.whl
"$clean_python" -m pip check
"$clean_python" scripts/bootstrap_pyboy.py --mode source --check
"$clean_python" - <<'PY'
import os
import subprocess
import sys
from pathlib import Path

import pyboy
from pyboy import utils
from pyboy.core.serial import Serial
import pokered_harness

assert pyboy.__version__ == "2.7.0"
assert pyboy.__pokered_harness_revision__ == (
    "c565df66c3731fad2856169a90f6bbec99925915"
)
assert utils.cython_compiled is False
serial = Serial(False)
assert all(
    hasattr(serial, name)
    for name in ("backend", "apply_external_edge", "peek_out_bit")
)

clean_env = os.environ.copy()
for name in (
    "POKERED_ROM_PATH",
    "POKERED_SYM_PATH",
    "POKERED_ROM_SHA1",
    "POKERED_SYM_SHA1",
    "POKERED_VERSIONS_PATH",
    "POKERED_SKIP_SHA1",
):
    clean_env.pop(name, None)
entrypoints = [
    [sys.executable, "-m", "pokered_harness"],
    [
        str(
            Path(sys.executable).with_name(
                "pokered-harness.exe" if os.name == "nt" else "pokered-harness"
            )
        )
    ],
]
for command in entrypoints:
    result = subprocess.run(
        command,
        cwd=Path.cwd(),
        env=clean_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, (command, result.stdout, result.stderr)
    assert "set POKERED_ROM_PATH and POKERED_SYM_PATH" in (
        result.stdout + result.stderr
    )

print(pokered_harness.__file__)
print(pyboy.__file__)
PY
