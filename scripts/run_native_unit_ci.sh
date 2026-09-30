#!/usr/bin/env bash
# A fresh native build plus the complete, unchanged ROM-free unit tier.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$repo_root"
base_python="${PYTHON:-python3}"
: "${NATIVE_UNIT_OUTPUT:?Set NATIVE_UNIT_OUTPUT to a NEW absolute directory outside the checkout}"
case "$NATIVE_UNIT_OUTPUT" in
    /*) ;;
    *) printf '%s\n' 'NATIVE_UNIT_OUTPUT must be absolute' >&2; exit 2 ;;
esac

# The prepare command refuses an existing directory without changing its files.
# Prerequisite failures retain preflight.json with BLOCKED, never a test PASS.
"$base_python" scripts/native_unit_ci.py prepare "$NATIVE_UNIT_OUTPUT"
initial_head="$(git rev-parse HEAD)"
evidence="$NATIVE_UNIT_OUTPUT/evidence"
work="$NATIVE_UNIT_OUTPUT/work"
export TMPDIR="$work/tmp"
mkdir -- "$TMPDIR"
export PYTHONNOUSERSITE=1

finish() {
    status=$?
    trap - EXIT
    set +e
    # Do not promote evidence after checkout mutation, even if pytest succeeded.
    git status --porcelain --untracked-files=all > "$evidence/final-worktree-status.txt"
    git_status=$?
    git rev-parse HEAD > "$evidence/final-head.txt"
    head_status=$?
    if [[ "$git_status" -ne 0 || "$head_status" -ne 0 \
        || -s "$evidence/final-worktree-status.txt" \
        || "$(cat "$evidence/final-head.txt")" != "$initial_head" ]]; then
        [[ "$status" -ne 0 ]] || status=2
    fi
    printf '%s\n' "$status" > "$evidence/exit-code.txt"
    printf 'Native unit lane exit %s; evidence retained at: %s\n' "$status" "$evidence"
    exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

run_logged() {
    local name="$1"
    shift
    "$@" 2>&1 | tee "$evidence/$name.log"
}

run_logged venv "$base_python" -m venv "$work/venv"
native_python="$work/venv/bin/python"
run_logged dependencies "$native_python" -m pip install -e ".[dev]"
run_logged lint "$native_python" -m ruff check \
    scripts/native_unit_ci.py tests/test_vendored_provenance_wording.py
run_logged format "$native_python" -m ruff format --check \
    scripts/native_unit_ci.py tests/test_vendored_provenance_wording.py
run_logged build "$native_python" scripts/bootstrap_pyboy.py --mode cython \
    --build-evidence "$evidence/native-build.json"
run_logged dependencies-check "$native_python" -m pip check
run_logged bootstrap-check "$native_python" scripts/bootstrap_pyboy.py --mode cython --check
run_logged pinball-proof "$native_python" scripts/native_unit_ci.py verify "$NATIVE_UNIT_OUTPUT"

# No -k filter, xfail/skip exemption, source fallback, retry, or longer deadline.
# The existing gate owns the selected count, runtime proof, and terminal verdict.
run_logged unit-gate "$native_python" scripts/production_gate.py \
    --runtime-mode cython --tier unit \
    --evidence-dir "$evidence/gate" --raw-output-dir "$evidence/raw"
