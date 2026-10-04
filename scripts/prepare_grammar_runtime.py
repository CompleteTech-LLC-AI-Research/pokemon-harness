"""Prepare an isolated real Python >=3.12 environment for authored grammar tests."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

if __package__:
    from scripts.bootstrap_pyboy import _run_bounded
else:
    from bootstrap_pyboy import _run_bounded

ROOT = Path(__file__).resolve().parents[1]


def prepare(python: Path, output: Path) -> Path:
    """Create fresh test-only inputs outside the checkout; never replace a directory."""
    python = Path(os.path.abspath(python.expanduser()))
    output = output.expanduser().resolve()
    if output.is_relative_to(ROOT.resolve()):
        raise ValueError("grammar environment/evidence must be outside the checkout")
    if not python.is_file():
        raise ValueError("the auxiliary interpreter does not exist")
    output.mkdir(parents=True, exist_ok=False)
    environment = os.environ.copy()
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONOPTIMIZE", "PYTEST_ADDOPTS", "PYTEST_PLUGINS"):
        environment.pop(name, None)
    environment.update(PYTHONNOUSERSITE="1", PYBOY_NO_CYTHON="1")

    def run(command: list[str], name: str, timeout: int) -> None:
        log = output / f"{name}.log"
        with log.open("w", encoding="utf-8") as terminal:
            result = _run_bounded(
                command,
                cwd=ROOT,
                env=environment,
                timeout=timeout,
                stdout=terminal,
                stderr=subprocess.STDOUT,
            )
        if result.returncode:
            raise RuntimeError(f"grammar {name} exited {result.returncode}; full result: {log}")

    run(
        [
            str(python),
            "-c",
            "import sys; print(sys.version); raise SystemExit(sys.version_info < (3, 12))",
        ],
        "interpreter",
        30,
    )
    venv = output / "venv"
    run([str(python), "-m", "venv", str(venv)], "venv", 180)
    executable = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    run([str(executable), "-m", "pip", "install", "-e", ".[dev]"], "dependencies", 1800)
    run([str(executable), "-m", "pip", "check"], "pip-check", 180)
    run(
        [str(executable), str(ROOT / "scripts/bootstrap_pyboy.py"), "--mode", "source", "--check"],
        "bootstrap-check",
        180,
    )
    run(
        [
            str(executable),
            str(ROOT / "scripts/check_import_origins.py"),
            "--project-root",
            str(ROOT),
        ],
        "import-origins",
        180,
    )
    (output / "preparation.json").write_text(
        json.dumps({"status": "PASS", "root": str(ROOT), "python": str(executable)}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return executable


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    executable = prepare(args.python, args.output)
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"python={executable}\n")
    print(f"POKERED_GRAMMAR_TEST_PYTHON={executable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
