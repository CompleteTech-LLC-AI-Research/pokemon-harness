"""Run TCP link-cable trade/battle proof matrix for Gen 1 versions.

This is intentionally a subprocess runner: each side of the link uses its
own PyBoy process and the harness TCP backend, matching the remote-link
Mode B path more closely than an in-process link demo.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RESULT_MARKER = "__TCP_TRADE_RESULT__ "
VERSIONS = ("red_gb", "red_color", "blue_gb", "blue_color", "yellow")
_VERSION_ASSETS = {
    "red_gb": (
        "red",
        "pokemon-red.gb",
        "pokemon-red.sym",
        "cable_club-vanilla.state",
        "cable_club-battle-vanilla.state",
    ),
    "red_color": (
        "red",
        "pokemon-red-color.gb",
        "pokemon-red.sym",
        "cable_club.state",
        "cable_club-battle.state",
    ),
    "blue_gb": (
        "blue",
        "pokemon-blue.gb",
        "pokemon-blue.sym",
        "cable_club-vanilla.state",
        "cable_club-battle-vanilla.state",
    ),
    "blue_color": (
        "blue",
        "pokemon-blue-color.gb",
        "pokemon-blue.sym",
        "cable_club.state",
        "cable_club-battle.state",
    ),
    "yellow": (
        "yellow",
        "pokemon-yellow.gbc",
        "pokemon-yellow.sym",
        "cable_club.state",
        "cable_club-battle.state",
    ),
}
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
    "05_battle_settled",
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


def _sha256_or_missing(path: Path) -> str:
    """Hash one evidence input, retaining an explicit missing marker."""
    if not path.is_file():
        return "<missing>"
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _matrix_fingerprint(
    *,
    repo_root: Path,
    modes: list[str],
    versions: list[str],
    deadline_seconds: float,
    python: str,
) -> str:
    """Return a content fingerprint for resumable proof evidence.

    A successful row is only reusable when the executable, driver, transport,
    ROM/symbol bytes, fixtures, and deadline contract are unchanged.  This is
    deliberately content-based so an ignored BYO asset root can move without
    making otherwise identical evidence unverifiable.
    """
    configured_rom_root = os.environ.get("POKERED_ROM_ROOT")
    rom_root = Path(configured_rom_root or repo_root / "rom").expanduser()
    if not rom_root.is_absolute():
        rom_root = repo_root / rom_root
    configured_fixture_root = os.environ.get("POKERED_FIXTURE_ROOT")
    fixture_root = Path(
        configured_fixture_root or repo_root / "tests" / "fixtures" / "link"
    ).expanduser()
    if not fixture_root.is_absolute():
        fixture_root = repo_root / fixture_root
    requested_python = Path(python).expanduser()
    resolved_python = requested_python
    if not resolved_python.is_absolute():
        resolved_python = Path(shutil.which(python) or python)
    python_fingerprint = {
        "requested": str(requested_python),
        "resolved": str(resolved_python),
        "sha256": _sha256_or_missing(resolved_python),
    }
    parts: dict[str, Any] = {
        "modes": sorted(modes),
        "versions": sorted(versions),
        "deadline_seconds": float(deadline_seconds),
        "python": python_fingerprint,
        "python_version": sys.version,
        "source": {},
        "assets": {},
    }
    source_paths = [
        repo_root / relative
        for relative in (
            "VERSIONS.md",
            ".gitmodules",
            "pyproject.toml",
            "scripts/tcp_link_matrix.py",
            "tests/_tcp_trade_peer.py",
            "vendor/pyboy-src/POKERED_HARNESS_PYBOY_REVISION",
        )
    ]
    for directory in (
        repo_root / "src" / "pokered_harness",
        repo_root / "vendor" / "pyboy-src" / "pyboy",
    ):
        source_paths.extend(directory.rglob("*.py"))
    parts["source"] = {
        str(path.relative_to(repo_root)): _sha256_or_missing(path)
        for path in sorted(set(source_paths))
    }
    for version in versions:
        game, rom_name, sym_name, trade_state, battle_state = _VERSION_ASSETS[version]
        for kind, relative in (
            ("rom", Path(game) / rom_name),
            ("symbol", Path(game) / sym_name),
            ("trade_fixture", Path(game) / trade_state),
            ("battle_fixture", Path(game) / battle_state),
        ):
            root = fixture_root if kind.endswith("fixture") else rom_root
            key = f"{version}:{kind}"
            parts["assets"][key] = _sha256_or_missing(root / relative)
    encoded = json.dumps(parts, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
            try:
                payload = json.loads(line[len(RESULT_MARKER) :])
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict):
                return payload
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
    fingerprint: str,
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
    # This matrix is the bundled source-runtime proof path. Native
    # qualification is a separate production-gate invocation and must not be
    # silently selected by a stray site-installed extension.
    env["PYBOY_NO_CYTHON"] = "1"

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
    listener: subprocess.Popen[str] | None = None
    connector: subprocess.Popen[str] | None = None
    spawn_error: str | None = None
    timed_out = False

    with (
        listener_stdout_path.open("w", encoding="utf-8") as listener_stdout_file,
        listener_stderr_path.open("w", encoding="utf-8") as listener_stderr_file,
        connector_stdout_path.open("w", encoding="utf-8") as connector_stdout_file,
        connector_stderr_path.open("w", encoding="utf-8") as connector_stderr_file,
    ):
        try:
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
            deadline_at = time.time() + timeout
            for proc in (listener, connector):
                remaining = max(1.0, deadline_at - time.time())
                try:
                    proc.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    try:
                        proc.kill()
                    except OSError:
                        pass
            for proc in (listener, connector):
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                proc.wait()
        except OSError as exc:
            # A failed connector/listener launch must not leave the already
            # started peer orphaned. Keep the failed row inspectable in the
            # manifest instead of aborting the whole matrix process.
            spawn_error = f"{type(exc).__name__}: {exc}"
        finally:
            for proc in (connector, listener):
                if proc is None:
                    continue
                if proc.poll() is None:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                try:
                    proc.wait()
                except OSError:
                    pass

    listener_stdout = listener_stdout_path.read_text(encoding="utf-8")
    listener_stderr = listener_stderr_path.read_text(encoding="utf-8")
    connector_stdout = connector_stdout_path.read_text(encoding="utf-8")
    connector_stderr = connector_stderr_path.read_text(encoding="utf-8")

    peers = [
        PeerRun(
            role="listen",
            version=listen_version,
            returncode=listener.returncode if listener is not None else None,
            stdout=listener_stdout,
            stderr=listener_stderr,
            result=_parse_result(listener_stdout),
        ),
        PeerRun(
            role="connect",
            version=connect_version,
            returncode=connector.returncode if connector is not None else None,
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
    if spawn_error is not None:
        verification["ok"] = False
        verification["errors"].insert(0, f"subprocess spawn failed: {spawn_error}")
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
        "fingerprint": fingerprint,
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
        errors.extend(_verify_party_swap(results))
        for peer, result in zip(peers, results, strict=True):
            if result.get("_AddEnemyMonToPlayerParty", 0) <= 0:
                errors.append(f"{peer.role} did not add enemy mon")
            if result.get("TradeCenter_Trade", 0) <= 0:
                errors.append(f"{peer.role} did not enter TradeCenter_Trade")
    else:
        errors.extend(_verify_battle_turns(results))

    return {
        "ok": not errors,
        "errors": errors,
        "expected_phases": list(expected_phases),
        "shot_count": len(list(run_dir.glob("*.png"))),
        "palette_profiles": palette_profiles,
    }


def _verify_battle_turns(results: list[dict[str, Any]]) -> list[str]:
    """Fail closed unless both peers recorded the same settled turn state.

    The peer records its first loop/faint/end boundary after move exchange
    and execution. A damage-function entry, a sync marker, or final aggregate
    hook counts cannot substitute for the state captured at that boundary.
    """
    if len(results) != 2:
        return ["battle requires exactly two peer results"]

    boundaries = {
        "MainInBattleLoop", "HandlePlayerMonFainted",
        "HandleEnemyMonFainted", "EndOfBattle",
    }
    snapshots = []

    def integer(value, minimum=0, maximum=None):
        return (
            type(value) is int and value >= minimum
            and (maximum is None or value <= maximum)
        )

    for side, result in enumerate(results):
        prefix = f"battle peer {side}"
        if not isinstance(result, dict):
            return [f"{prefix} result is missing"]
        snapshot = result.get("battle_turn")
        if not isinstance(snapshot, dict):
            return [f"{prefix} settled snapshot is missing"]
        boundary = snapshot.get("boundary")
        if type(snapshot.get("schema")) is not int or snapshot["schema"] != 1:
            return [f"{prefix} snapshot schema is unsupported"]
        if not isinstance(boundary, str) or boundary not in boundaries:
            return [f"{prefix} snapshot boundary is invalid"]

        for field, hook in (
            ("exchange_count", "LinkBattleExchangeData"),
            ("player_execute_count", "ExecutePlayerMove"),
            ("enemy_execute_count", "ExecuteEnemyMove"),
            ("boundary_count", boundary),
            ("loop_count", "MainInBattleLoop"),
        ):
            captured = snapshot.get(field)
            total = result.get(hook)
            if not integer(captured) or not integer(total) or captured > total:
                return [f"{prefix} snapshot counts do not match observed hooks"]
        if (
            snapshot["exchange_count"] <= 0 or snapshot["boundary_count"] <= 0
            or snapshot["player_execute_count"] + snapshot["enemy_execute_count"] <= 0
            or snapshot["loop_count"] < (2 if boundary == "MainInBattleLoop" else 1)
            or not integer(snapshot.get("execution_exchange_count"), minimum=1)
            or snapshot["execution_exchange_count"] != snapshot["exchange_count"]
        ):
            return [f"{prefix} snapshot precedes move exchange/execution"]
        if not integer(result.get("DisplayLinkBattleVersusTextBox"), minimum=1):
            return [f"{prefix} did not display battle versus text"]
        if not integer(snapshot.get("result"), maximum=255):
            return [f"{prefix} battle result is invalid"]

        if boundary == "EndOfBattle":
            if (
                type(snapshot.get("in_battle")) is not int
                or snapshot["in_battle"] not in (2, 255)
                or snapshot.get("cleanup_complete") is not True
                or type(snapshot.get("post_cleanup_in_battle")) is not int
                or snapshot["post_cleanup_in_battle"] != 0
            ):
                return [f"{prefix} terminal battle cleanup is incomplete"]
        elif type(snapshot.get("in_battle")) is not int or snapshot["in_battle"] != 2:
            return [f"{prefix} turn boundary was not in a trainer/link battle"]

        for role in ("player", "enemy"):
            mon = snapshot.get(role)
            if (
                not isinstance(mon, dict)
                or not integer(mon.get("species"), minimum=1, maximum=255)
                or not integer(mon.get("max_hp"), minimum=1, maximum=65535)
                or not integer(mon.get("hp"), maximum=65535)
                or mon["hp"] > mon["max_hp"]
                or not integer(mon.get("status"), maximum=255)
            ):
                return [f"{prefix} {role} combatant snapshot is invalid"]
        if (
            boundary == "HandlePlayerMonFainted" and snapshot["player"]["hp"] != 0
        ) or (
            boundary == "HandleEnemyMonFainted" and snapshot["enemy"]["hp"] != 0
        ):
            return [f"{prefix} faint boundary has nonzero HP"]
        snapshots.append(snapshot)

    left, right = snapshots
    if left["exchange_count"] != right["exchange_count"]:
        return ["battle snapshots describe different move exchanges"]
    if any(snapshot["boundary"] == "EndOfBattle" for snapshot in snapshots):
        if not all(snapshot["boundary"] == "EndOfBattle" for snapshot in snapshots):
            return ["battle peers disagree about terminal outcome"]
        # EndOfBattle presents 0 as victory, 1 as loss, and 2 as draw.
        if (left["result"], right["result"]) not in {(0, 1), (1, 0), (2, 2)}:
            return ["battle terminal results are not complementary"]

    fields = ("species", "hp", "max_hp", "status")
    for local, remote in ((left["player"], right["enemy"]), (left["enemy"], right["player"])):
        if any(local[field] != remote[field] for field in fields):
            return ["battle peers disagree on settled combatant state"]
    return []


def _verify_party_swap(results: list[dict[str, Any]]) -> list[str]:
    """Verify the lead-record swap performed by this driver's menu policy.

    Function-entry hooks and screenshots cannot prove the copy completed.
    Identical lead records also cannot distinguish a swap from no operation.
    """
    if len(results) != 2:
        return ["trade requires exactly two peer results"]
    before = [result.get("party_before") for result in results]
    after = [result.get("party_after") for result in results]
    for party in (*before, *after):
        if not isinstance(party, dict):
            return ["trade party snapshot is missing"]
        count = party.get("count")
        records = party.get("mon_records")
        species = party.get("species")
        if (
            type(count) is not int or not 1 <= count <= 6
            or not isinstance(records, list) or len(records) != count
            or not isinstance(species, list) or len(species) != count + 1
            or species[-1] != 0xFF
        ):
            return ["trade party snapshot has invalid count, records, or species"]
        try:
            decoded = [bytes.fromhex(record) for record in records]
        except (TypeError, ValueError):
            return ["trade party record is not hex-encoded bytes"]
        if any(len(record) != 44 for record in decoded):
            return ["trade party record must contain 44 bytes"]
        if [record[0] for record in decoded] != species[:-1]:
            return ["trade party records disagree with species list"]
    if before[0]["mon_records"][0] == before[1]["mon_records"][0]:
        return ["trade lead records are indistinguishable; swap is unproven"]
    errors = []
    for side in range(2):
        expected = [before[1 - side]["mon_records"][0], *before[side]["mon_records"][1:]]
        if after[side]["mon_records"] != expected:
            errors.append(f"peer {side} did not receive the other lead while preserving its party")
    return errors


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


def _write_manifest(
    outdir: Path, entries: list[dict[str, Any]], *, fingerprint: str
) -> None:
    ok = all(entry["verification"]["ok"] for entry in entries)
    manifest = {
        "ok": ok,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "entry_count": len(entries),
        "passed": sum(1 for entry in entries if entry["verification"]["ok"]),
        "failed": sum(1 for entry in entries if not entry["verification"]["ok"]),
        "fingerprint": fingerprint,
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
    outdir = (
        (repo_root / args.outdir).resolve()
        if not args.outdir.is_absolute()
        else args.outdir
    )
    outdir.mkdir(parents=True, exist_ok=True)
    fingerprint = _matrix_fingerprint(
        repo_root=repo_root,
        modes=list(args.modes),
        versions=list(args.versions),
        deadline_seconds=args.deadline_seconds,
        python=args.python,
    )

    entries: list[dict[str, Any]] = []
    completed: set[tuple[str, str, str]] = set()
    manifest_path = outdir / "manifest.json"
    include_any = set(args.include_any_version or [])
    listen_filter = set(args.listen_versions or [])
    connect_filter = set(args.connect_versions or [])

    def selected(key: tuple[str, str, str]) -> bool:
        mode, listen_version, connect_version = key
        if mode not in args.modes:
            return False
        if listen_version not in args.versions or connect_version not in args.versions:
            return False
        if listen_filter and listen_version not in listen_filter:
            return False
        if connect_filter and connect_version not in connect_filter:
            return False
        return (
            not include_any
            or listen_version in include_any
            or connect_version in include_any
        )

    if args.resume_ok and manifest_path.exists():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            previous = None
            print(
                f"[matrix] ignoring unreadable manifest: {type(exc).__name__}: {exc}",
                flush=True,
            )
        if isinstance(previous, dict) and previous.get("fingerprint") == fingerprint:
            prior_entries = previous.get("entries", [])
            if not isinstance(prior_entries, list):
                prior_entries = []
            for entry in prior_entries:
                if not isinstance(entry, dict):
                    continue
                key = (
                    entry.get("mode"),
                    entry.get("listen_version"),
                    entry.get("connect_version"),
                )
                verification = entry.get("verification")
                if (
                    entry.get("fingerprint") == fingerprint
                    and isinstance(verification, dict)
                    and verification.get("ok")
                    and all(key)
                    and selected(key)
                ):
                    entries.append(entry)
                    completed.add(key)
        elif previous is not None:
            print(
                "[matrix] existing manifest fingerprint differs; rerunning "
                "successful rows",
                flush=True,
            )

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
                fingerprint=fingerprint,
            )
            entries.append(entry)
            _write_manifest(outdir, entries, fingerprint=fingerprint)
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
    _write_manifest(outdir, entries, fingerprint=fingerprint)
    return 0 if all(entry["verification"]["ok"] for entry in entries) else 1


if __name__ == "__main__":
    raise SystemExit(main())
