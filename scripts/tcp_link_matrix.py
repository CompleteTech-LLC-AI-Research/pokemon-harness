"""Run TCP link-cable trade/battle proof matrix for Gen 1 versions.

This is intentionally a subprocess runner: each side of the link uses its
own PyBoy process and the harness TCP backend, matching the remote-link
Mode B path more closely than an in-process link demo.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RESULT_MARKER = "__TCP_TRADE_RESULT__ "
VERSIONS = ("red_gb", "red_color", "blue_gb", "blue_color", "yellow")
TRADE_PHASES = (
    "00_loaded",
    "01_link_menu",
    "02_trade_center",
    "03_post_warp_sync",
    "04_select_mon_ready",
    "05_select_mon_converged",
    "06_select_mon_sync",
    "07_post_trade",
)
BATTLE_PHASES = (
    "00_loaded",
    "01_link_menu",
    "02_battle_menu",
    "03_colosseum",
    "04_battle_launch",
    "05_battle_damage",
    "06_battle_synced",
)

PALETTE_CHECK_PHASES = ("00_loaded", "01_link_menu")


@dataclass(frozen=True)
class PeerRun:
    role: str
    version: str
    returncode: int | None
    stdout: str
    stderr: str
    result: dict[str, Any] | None


def _free_port() -> int:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _parse_result(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        if line.startswith(RESULT_MARKER):
            return json.loads(line[len(RESULT_MARKER) :])
    return None


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _peer_command(
    *,
    python: str,
    repo_root: Path,
    role: str,
    version: str,
    port: int,
    mode: str,
    deadline_seconds: float,
    record_dir: Path,
    label: str,
) -> list[str]:
    return [
        python,
        str(repo_root / "tests" / "_tcp_trade_peer.py"),
        "--role",
        role,
        "--version",
        version,
        "--port",
        str(port),
        "--goal",
        mode,
        "--deadline-seconds",
        str(deadline_seconds),
        "--repo-root",
        str(repo_root),
        "--record-dir",
        str(record_dir),
        "--label",
        label,
    ]


def _run_pair(
    *,
    repo_root: Path,
    outdir: Path,
    mode: str,
    listen_version: str,
    connect_version: str,
    deadline_seconds: float,
    python: str,
) -> dict[str, Any]:
    pair_name = f"{listen_version}_to_{connect_version}"
    label = f"{mode}_{pair_name}"
    run_dir = outdir / mode / pair_name
    run_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()

    env = os.environ.copy()
    py_path_parts = [
        str(repo_root),
        str(repo_root / "vendor" / "pyboy-src"),
        str(repo_root / "src"),
    ]
    if env.get("PYTHONPATH"):
        py_path_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(py_path_parts)

    listener_cmd = _peer_command(
        python=python,
        repo_root=repo_root,
        role="listen",
        version=listen_version,
        port=port,
        mode=mode,
        deadline_seconds=deadline_seconds,
        record_dir=run_dir,
        label=label,
    )
    connector_cmd = _peer_command(
        python=python,
        repo_root=repo_root,
        role="connect",
        version=connect_version,
        port=port,
        mode=mode,
        deadline_seconds=deadline_seconds,
        record_dir=run_dir,
        label=label,
    )

    started_at = time.time()
    listener_stdout_path = run_dir / "listener.stdout"
    listener_stderr_path = run_dir / "listener.stderr"
    connector_stdout_path = run_dir / "connector.stdout"
    connector_stderr_path = run_dir / "connector.stderr"

    with (
        listener_stdout_path.open("w", encoding="utf-8") as listener_stdout_file,
        listener_stderr_path.open("w", encoding="utf-8") as listener_stderr_file,
        connector_stdout_path.open("w", encoding="utf-8") as connector_stdout_file,
        connector_stderr_path.open("w", encoding="utf-8") as connector_stderr_file,
    ):
        listener = subprocess.Popen(
            listener_cmd,
            cwd=repo_root,
            env=env,
            text=True,
            stdout=listener_stdout_file,
            stderr=listener_stderr_file,
        )
        time.sleep(1.0)
        connector = subprocess.Popen(
            connector_cmd,
            cwd=repo_root,
            env=env,
            text=True,
            stdout=connector_stdout_file,
            stderr=connector_stderr_file,
        )

        timeout = deadline_seconds + 45.0
        timed_out = False
        deadline_at = time.time() + timeout
        for proc in (listener, connector):
            remaining = max(1.0, deadline_at - time.time())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                timed_out = True
                proc.kill()
        for proc in (listener, connector):
            if proc.poll() is None:
                proc.kill()
            proc.wait()

    listener_stdout = listener_stdout_path.read_text(encoding="utf-8")
    listener_stderr = listener_stderr_path.read_text(encoding="utf-8")
    connector_stdout = connector_stdout_path.read_text(encoding="utf-8")
    connector_stderr = connector_stderr_path.read_text(encoding="utf-8")

    peers = [
        PeerRun(
            role="listen",
            version=listen_version,
            returncode=listener.returncode,
            stdout=listener_stdout,
            stderr=listener_stderr,
            result=_parse_result(listener_stdout),
        ),
        PeerRun(
            role="connect",
            version=connect_version,
            returncode=connector.returncode,
            stdout=connector_stdout,
            stderr=connector_stderr,
            result=_parse_result(connector_stdout),
        ),
    ]
    verification = _verify_run(
        repo_root=repo_root,
        mode=mode,
        run_dir=run_dir,
        label=label,
        peers=peers,
        timed_out=timed_out,
    )
    _write_contact_sheets(
        run_dir=run_dir,
        label=label,
        mode=mode,
        listen_version=listen_version,
        connect_version=connect_version,
    )
    elapsed_seconds = time.time() - started_at
    entry = {
        "mode": mode,
        "pair": pair_name,
        "listen_version": listen_version,
        "connect_version": connect_version,
        "port": port,
        "elapsed_seconds": elapsed_seconds,
        "timed_out": timed_out,
        "commands": {
            "listener": listener_cmd,
            "connector": connector_cmd,
        },
        "peers": [
            {
                "role": peer.role,
                "version": peer.version,
                "returncode": peer.returncode,
                "result": peer.result,
            }
            for peer in peers
        ],
        "verification": verification,
    }
    (run_dir / "result.json").write_text(
        json.dumps(entry, indent=2, sort_keys=True), encoding="utf-8"
    )
    return entry


def _verify_run(
    *,
    repo_root: Path,
    mode: str,
    run_dir: Path,
    label: str,
    peers: list[PeerRun],
    timed_out: bool,
) -> dict[str, Any]:
    def display_path(path: Path) -> str:
        """Render an artifact path without requiring outdir under repo root."""
        try:
            return str(path.relative_to(repo_root))
        except ValueError:
            return str(path)

    errors: list[str] = []
    palette_profiles: dict[str, dict[str, Any]] = {}
    if timed_out:
        errors.append("subprocess timeout")
    for peer in peers:
        if peer.returncode != 0:
            errors.append(f"{peer.role} returncode={peer.returncode}")
        if peer.result is None:
            errors.append(f"{peer.role} missing {RESULT_MARKER.strip()} JSON")

    expected_phases = TRADE_PHASES if mode == "trade" else BATTLE_PHASES
    shot_errors: list[str] = []
    for phase in expected_phases:
        for peer in peers:
            path = run_dir / f"{label}.{phase}.{peer.role}.{peer.version}.png"
            if not path.exists():
                shot_errors.append(f"missing {display_path(path)}")
            elif path.stat().st_size <= 0:
                shot_errors.append(f"empty {display_path(path)}")
            elif phase in PALETTE_CHECK_PHASES:
                profile, palette_errors = _verify_palette(
                    path=path,
                    repo_root=repo_root,
                    version=peer.version,
                )
                palette_profiles[
                    f"{phase}.{peer.role}.{peer.version}"
                ] = profile
                errors.extend(palette_errors)
    errors.extend(shot_errors)

    results = [peer.result or {} for peer in peers]
    if mode == "trade":
        for peer, result in zip(peers, results, strict=True):
            if result.get("_AddEnemyMonToPlayerParty", 0) <= 0:
                errors.append(f"{peer.role} did not add enemy mon")
            if result.get("TradeCenter_Trade", 0) <= 0:
                errors.append(f"{peer.role} did not enter TradeCenter_Trade")
    else:
        for peer, result in zip(peers, results, strict=True):
            if result.get("PlayerCalcMoveDamage", 0) <= 0:
                errors.append(f"{peer.role} did not calculate battle damage")
            if result.get("LinkBattleExchangeData", 0) <= 0:
                errors.append(f"{peer.role} did not exchange battle data")
            if result.get("DisplayLinkBattleVersusTextBox", 0) <= 0:
                errors.append(f"{peer.role} did not display battle versus text")

    return {
        "ok": not errors,
        "errors": errors,
        "expected_phases": list(expected_phases),
        "shot_count": len(list(run_dir.glob("*.png"))),
        "palette_profiles": palette_profiles,
    }


def _verify_palette(
    *,
    path: Path,
    repo_root: Path,
    version: str,
) -> tuple[dict[str, Any], list[str]]:
    def display_path() -> str:
        try:
            return str(path.relative_to(repo_root))
        except ValueError:
            return str(path)

    try:
        from PIL import Image
    except Exception as exc:
        return (
            {"path": display_path(), "error": type(exc).__name__},
            [f"cannot verify palette without Pillow: {type(exc).__name__}"],
        )

    with Image.open(path) as img:
        rgb = img.convert("RGB")
        pixels = list(rgb.getdata())
    total = len(pixels)
    blue_dominant = sum(
        1 for r, g, b in pixels if b > 150 and b > r + 35 and b > g + 20
    )
    warm_brown = sum(
        1 for r, g, b in pixels if r >= 100 and 60 <= g <= 180 and b <= 120
    )
    grayscale = sum(1 for r, g, b in pixels if abs(r - g) < 6 and abs(g - b) < 6)
    profile = {
        "path": display_path(),
        "version": version,
        "total_pixels": total,
        "blue_dominant_pixels": blue_dominant,
        "warm_brown_pixels": warm_brown,
        "grayscale_pixels": grayscale,
    }

    errors: list[str] = []
    if version in {"red_color", "blue_color"}:
        # The color IPS variants should render the Cable Club with the
        # richer brown/gold Pokemon Center palette. A blue-dominant
        # 4-color frame here usually means the Yellow side was mistaken
        # for Blue, or the wrong ROM/palette path was used.
        if warm_brown < 700:
            errors.append(
                f"{version} palette lacks warm/brown PC colors in "
                f"{display_path()}: warm_brown={warm_brown}"
            )
        if blue_dominant > 1200:
            errors.append(
                f"{version} palette is blue-dominant in "
                f"{display_path()}: blue={blue_dominant}"
            )
    return profile, errors


def _write_contact_sheets(
    *,
    run_dir: Path,
    label: str,
    mode: str,
    listen_version: str,
    connect_version: str,
) -> None:
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return

    phases = TRADE_PHASES if mode == "trade" else BATTLE_PHASES
    proof_dir = run_dir / "proof_sheets"
    proof_dir.mkdir(exist_ok=True)
    for phase in phases:
        left = run_dir / f"{label}.{phase}.listen.{listen_version}.png"
        right = run_dir / f"{label}.{phase}.connect.{connect_version}.png"
        if not left.exists() or not right.exists():
            continue
        with Image.open(left) as left_img, Image.open(right) as right_img:
            left_rgb = left_img.convert("RGB")
            right_rgb = right_img.convert("RGB")
            width = left_rgb.width + right_rgb.width
            label_height = 24
            height = max(left_rgb.height, right_rgb.height) + label_height
            sheet = Image.new("RGB", (width, height), "white")
            sheet.paste(left_rgb, (0, label_height))
            sheet.paste(right_rgb, (left_rgb.width, label_height))
            draw = ImageDraw.Draw(sheet)
            draw.text((2, 1), f"LISTEN {listen_version}", fill="black")
            draw.text(
                (left_rgb.width + 2, 1),
                f"CONNECT {connect_version}",
                fill="black",
            )
            draw.text((2, 12), phase, fill="black")
            sheet.save(proof_dir / f"{phase}.png")


def _write_manifest(outdir: Path, entries: list[dict[str, Any]]) -> None:
    ok = all(entry["verification"]["ok"] for entry in entries)
    manifest = {
        "ok": ok,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "entry_count": len(entries),
        "passed": sum(1 for entry in entries if entry["verification"]["ok"]),
        "failed": sum(1 for entry in entries if not entry["verification"]["ok"]),
        "entries": entries,
    }
    (outdir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-root", type=Path, default=Path.cwd())
    ap.add_argument("--outdir", type=Path, default=Path("artifacts/link-proof-matrix"))
    ap.add_argument("--modes", nargs="+", choices=("trade", "battle"), default=["trade", "battle"])
    ap.add_argument("--versions", nargs="+", choices=VERSIONS, default=list(VERSIONS))
    ap.add_argument(
        "--include-any-version",
        nargs="+",
        choices=VERSIONS,
        default=None,
        help="Only run ordered pairs where either side is one of these versions.",
    )
    ap.add_argument("--deadline-seconds", type=float, default=210.0)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--fail-fast", action="store_true")
    ap.add_argument(
        "--listen-versions",
        nargs="+",
        choices=VERSIONS,
        default=None,
        help="Only run ordered pairs with these listener versions.",
    )
    ap.add_argument(
        "--connect-versions",
        nargs="+",
        choices=VERSIONS,
        default=None,
        help="Only run ordered pairs with these connector versions.",
    )
    ap.add_argument(
        "--resume-ok",
        action="store_true",
        help="Load existing ok entries from manifest.json and skip rerunning them.",
    )
    args = ap.parse_args()

    repo_root = args.repo_root.resolve()
    outdir = (repo_root / args.outdir).resolve() if not args.outdir.is_absolute() else args.outdir
    outdir.mkdir(parents=True, exist_ok=True)

    entries: list[dict[str, Any]] = []
    completed: set[tuple[str, str, str]] = set()
    manifest_path = outdir / "manifest.json"
    if args.resume_ok and manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        for entry in previous.get("entries", []):
            key = (
                entry.get("mode"),
                entry.get("listen_version"),
                entry.get("connect_version"),
            )
            if entry.get("verification", {}).get("ok") and all(key):
                entries.append(entry)
                completed.add(key)

    include_any = set(args.include_any_version or [])
    listen_filter = set(args.listen_versions or [])
    connect_filter = set(args.connect_versions or [])
    for mode in args.modes:
        for listen_version, connect_version in itertools.product(args.versions, repeat=2):
            if listen_filter and listen_version not in listen_filter:
                continue
            if connect_filter and connect_version not in connect_filter:
                continue
            if include_any and not (
                listen_version in include_any or connect_version in include_any
            ):
                continue
            key = (mode, listen_version, connect_version)
            if key in completed:
                print(
                    f"[matrix] skip ok {mode} {listen_version}_to_{connect_version}",
                    flush=True,
                )
                continue
            name = f"{mode} {listen_version}_to_{connect_version}"
            print(f"[matrix] start {name}", flush=True)
            entry = _run_pair(
                repo_root=repo_root,
                outdir=outdir,
                mode=mode,
                listen_version=listen_version,
                connect_version=connect_version,
                deadline_seconds=args.deadline_seconds,
                python=args.python,
            )
            entries.append(entry)
            _write_manifest(outdir, entries)
            status = "ok" if entry["verification"]["ok"] else "FAIL"
            print(
                f"[matrix] {status} {name} "
                f"elapsed={entry['elapsed_seconds']:.1f}s",
                flush=True,
            )
            if args.fail_fast and not entry["verification"]["ok"]:
                return 1
    if not entries:
        # A filter typo must not look like a successful zero-row matrix.  An
        # empty result cannot establish any transport or gameplay evidence.
        print(
            "[matrix] no ordered pairs selected; check --versions and "
            "listener/connector filters",
            file=sys.stderr,
        )
        return 2
    _write_manifest(outdir, entries)
    return 0 if all(entry["verification"]["ok"] for entry in entries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
