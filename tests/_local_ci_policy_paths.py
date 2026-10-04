from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_local_ci.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "release-hygiene.yml"
