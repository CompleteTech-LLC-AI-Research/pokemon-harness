#!/usr/bin/env bash
# Pip-less uv qualification of the unchanged native bootstrap. This is NOT the
# full unit and timing gate; the pip lane (run_native_unit_ci.sh) owns that.
# ensurepip unavailability is a fault injected ONLY into the owned ephemeral uv
# environment to reach the bootstrap's documented uv fallback. It simulates a
# Python without ensurepip and is not a native platform absence.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd -- "$repo_root"
base_python="${PYTHON:-python3}"
: "${NATIVE_UNIT_OUTPUT:?Set NATIVE_UNIT_OUTPUT to a NEW absolute directory outside the checkout}"
case "$NATIVE_UNIT_OUTPUT" in
    /*) ;;
    *) printf '%s\n' 'NATIVE_UNIT_OUTPUT must be absolute' >&2; exit 2 ;;
esac

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
    printf 'Native uv lane exit %s; evidence retained at: %s\n' "$status" "$evidence"
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

# The pinned uv lives in the hosted base environment, never in the target env.
command -v uv > "$evidence/uv-path.txt"
run_logged uv-version uv --version
case "$(uv --version)" in
    "uv 0.12.17"*) ;;
    *) printf '%s\n' 'uv 0.12.17 is required' >&2; exit 2 ;;
esac

uv_env="$work/uvenv"
uv_python="$uv_env/bin/python"
run_logged uv-venv uv venv --python "$base_python" "$uv_env"
run_logged uv-state-before "$uv_python" scripts/native_unit_ci.py uv-state "$NATIVE_UNIT_OUTPUT" --label before
run_logged uv-instrument "$uv_python" scripts/native_unit_ci.py uv-instrument "$NATIVE_UNIT_OUTPUT"
run_logged uv-state-faulted "$uv_python" scripts/native_unit_ci.py uv-state "$NATIVE_UNIT_OUTPUT" --label faulted

run_logged dependencies uv pip install --python "$uv_python" -e ".[dev]"
run_logged lint "$uv_python" -m ruff check \
    scripts/native_unit_ci.py tests/test_vendored_provenance_wording.py
run_logged format "$uv_python" -m ruff format --check \
    scripts/native_unit_ci.py tests/test_vendored_provenance_wording.py

# The unchanged bootstrap runs for real; only its subprocess argv is audited.
run_logged build env NATIVE_UV_AUDIT_ACTIVE=1 "$uv_python" scripts/bootstrap_pyboy.py \
    --mode cython --build-evidence "$evidence/native-build.json"
run_logged dependencies-check uv pip check --python "$uv_python"
run_logged bootstrap-check env NATIVE_UV_AUDIT_ACTIVE=1 "$uv_python" scripts/bootstrap_pyboy.py \
    --mode cython --check
run_logged pinball-proof "$uv_python" scripts/native_unit_ci.py verify "$NATIVE_UNIT_OUTPUT"
run_logged uv-state-after "$uv_python" scripts/native_unit_ci.py uv-state "$NATIVE_UNIT_OUTPUT" --label after
run_logged uv-qualification "$uv_python" scripts/native_unit_ci.py uv-verify "$NATIVE_UNIT_OUTPUT" --uv "$(cat "$evidence/uv-path.txt")"
