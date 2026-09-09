#!/usr/bin/env bash
set -euo pipefail
poke_prompt_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cat -- "$poke_prompt_dir/goal.md" "$poke_prompt_dir/meta.md"
