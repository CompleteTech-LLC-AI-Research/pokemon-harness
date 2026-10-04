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

export POKERED_GRAMMAR_EVIDENCE_DIR="$RUNNER_TEMP/pokered-grammar-evidence"

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
# Boundary decision: the two main lanes below cover all of `tests/` (passed as
# the directory itself, so a newly added test file cannot silently escape) plus
# the `scripts/` directory, and the `ruff format --check` lane
# additionally covers exactly one `src/` file -- the #242 facade entry, added so
# that new source path is format-checked and not merely checked. The remaining
# `src/` paths of the runtime/link block stay outside the format boundary on
# purpose and are named in tests/test_local_ci_policy.py. Measured reason: 7 of
# that block's 29 paths are not format-clean today (3 of its 17 `src/` files), so
# format-checking the whole block would fail the gate and belongs in a separate
# change that fixes those files first. The narrow boundary is pre-existing --
# 0 of 16 `src/` files were format-checked at 061fa15c. `scripts/` and `tests/`
# are passed as directories, so a new file in either tree cannot escape the
# gate; that needed `scripts/` to be lint- and format-clean first, which #567
# step 1 did. `scripts/produce_battle_state_fixtures.py` is the single carve-out
# from the `scripts` tree, declared in pyproject.toml: the fixture manifest
# records that tool's SHA-1 as its capture identity, so reformatting it would
# invalidate the only record of which tool captured the boundary fixtures. It is
# pinned against drift by `test_excluded_fixture_producer_still_matches_the_manifest_sha1`,
# and `extend-exclude` drops it from the check lane as well as the format
# lane; it was in neither lane on master, so that is not a coverage regression.
# It returns to
# both lanes when those fixtures can be re-captured from a real ROM. `src/`
# stays enumerated because that tree is not format-clean yet (19
# files needing reformat), so globbing it here would fail the gate and is a
# separate change that fixes those files first. This script stays in lockstep
# with .github/workflows/release-hygiene.yml, and tests/test_local_ci_policy.py
# enforces the `tests/` and `scripts/` coverage rather than trusting this
# comment.
python -m ruff check \
    scripts \
    tests
python -m ruff format --check \
    src/pokered_harness/_mcp_facade_entry.py \
    scripts \
    tests

# Check runtime and link lane lint
python -m ruff check \
    src/pokered_harness/_mcp_facade_entry.py \
    src/pokered_harness/link/_network_backend_io_mixin.py \
    src/pokered_harness/link/_network_backend_lifecycle_mixin.py \
    src/pokered_harness/link/_network_backend_owner_mixin.py \
    src/pokered_harness/link/_network_backend_protocol_mixin.py \
    src/pokered_harness/link/_network_backend_support.py \
    src/pokered_harness/link/_pyboy_link_session_lifecycle_mixin.py \
    src/pokered_harness/link/_pyboy_link_session_network_mixin.py \
    src/pokered_harness/link/_pyboy_link_session_stepping_mixin.py \
    src/pokered_harness/link/_pyboy_link_session_support.py \
    src/pokered_harness/link/_serial_link_support.py \
    src/pokered_harness/link/network_backend.py \
    src/pokered_harness/link/pyboy_link_session.py \
    src/pokered_harness/link/pair.py \
    src/pokered_harness/link/serial_bridge.py \
    src/pokered_harness/link/serial_coordinator.py \
    src/pokered_harness/link/serial_link.py \
    scripts/network_concurrency_probe.py \
    tests/test_network_backend_dispatch.py \
    tests/test_network_backend_rearm.py \
    tests/test_network_backend_serial_transcript.py \
    tests/test_network_backend_transport.py \
    tests/test_network_backend_wire_idle.py \
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
if [[ "$python_version" == "3.11" && -z "${POKERED_GRAMMAR_TEST_PYTHON:-}" ]]; then
    printf 'Python 3.11 requires an isolated Python >=3.12 grammar-test environment; see docs/CI_POLICY.md\n' >&2
    exit 2
fi
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
    "fd765b1808ac9cb192b42ae971987158ff36ae48"
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
