#!/usr/bin/env python3
"""Produce immutable link-battle save-state fixtures at battle boundaries.

This operator utility drives a real Cable Club link battle through repeated
turns with the repository's existing battle driver and hooks.  It never writes
emulator RAM: every advance is a real ``PyBoyLinkSession`` step and every menu
choice is a real joypad event.  It saves three operator fixture pairs per game:

* ``cable_club-battle-faint.state``  - the session whose own party mon just
  fainted and whose replacement party menu is open and awaiting input
  (``wInHandlePlayerMonFainted != 0``, ``wPartyMenuTypeOrMessageID == 2``, and
  the ROM's own ``wPartyMenuAnimMonEnabled == $40`` liveness witness).
  Every forced-replacement boundary is captured and the final one before the
  deciding knockout is the one kept, so a consumer can drive the few
  remaining turns to the terminal return from the fixture;
* ``cable_club-pre-terminal.state``  - both sessions are back at a battle
  command boundary and the battle reaches the terminal return from there.  The
  pair is rewritten at every command boundary, so the last one written is the
  last reloadable both-command boundary before ``EndOfBattle``;
* ``cable_club-terminal.state``      - the battle has ended and
  ``wIsInBattle == 0`` after ``EndOfBattle`` fired.

Each named fixture also gets a ``-peer.state`` sibling captured at the same
emulated instant so the pair can be reloaded consistently.  A
``.provenance.json`` sidecar records the pinned ROM/SYM SHA-1 values, the
repository commit, the turn count, and the final party/HP snapshot.  The ROM
recorded is the exact ROM the session opens (the color-patched Red/Blue ROM,
never the vanilla file), and the provenance binds every derived state to the
admitted ``cable_club-battle.state`` bytes it was driven from by SHA-1 and
SHA-256.

``--emit-manifest-rows`` writes the ``release-evidence/fixture-manifest.json``
rows for every fixture pair produced (``kind: boundary``); each row records
the same SHA-1/SHA-256 digests the capture wrote, so the manifest can be
updated mechanically instead of by hand.

ROMs, symbol files, and save states are operator-managed inputs and stay
outside version control.  The command validates the selected ROM and symbol
file against ``VERSIONS.md`` before starting and exits non-zero without
writing a fixture if a boundary is not reached within the finite budget.

Example::

    python scripts/produce_battle_state_fixtures.py \\
        --version blue \\
        --rom-root /path/to/rom \\
        --fixture-root /path/to/tests/fixtures/link
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile  # noqa: F401  (retained facade attribute: producer.tempfile)
import time
from datetime import (  # noqa: F401  (retained facade attributes: producer.UTC, producer.datetime)
    UTC,
    datetime,
)
from pathlib import Path

from scripts.produce_battle_state_fixtures_drive import (
    WRAM_BANK_PORT,  # noqa: F401  (retained facade attribute)
    WRAM_BATTLE_BANK,  # noqa: F401  (retained facade attribute)
    WRAM_SWITCHABLE_END,  # noqa: F401  (retained facade attribute)
    WRAM_SWITCHABLE_START,  # noqa: F401  (retained facade attribute)
    _active_hp,  # noqa: F401  (retained facade attribute)
    _active_moves,  # noqa: F401  (retained facade attribute)
    _banked_readable,  # noqa: F401  (retained facade attribute)
    _baseline,
    _battle_active,  # noqa: F401  (retained facade attribute)
    _battle_ended,
    _battle_menu_input_ready,  # noqa: F401  (retained facade attribute)
    _byte,  # noqa: F401  (retained facade attribute)
    _choose_healthy_replacement,
    _choose_supported_or_any,  # noqa: F401  (retained facade attribute)
    _committed_move,  # noqa: F401  (retained facade attribute)
    _counters,  # noqa: F401  (retained facade attribute)
    _drive_next_turn,
    _faint_deltas,  # noqa: F401  (retained facade attribute)
    _first_usable_move_slot,  # noqa: F401  (retained facade attribute)
    _menu_awaiting_a,  # noqa: F401  (retained facade attribute)
    _menu_fields,  # noqa: F401  (retained facade attribute)
    _move_menu_input_ready,  # noqa: F401  (retained facade attribute)
    _party_hp,  # noqa: F401  (retained facade attribute)
    _party_hp_or_empty,  # noqa: F401  (retained facade attribute)
    _party_menu_live,  # noqa: F401  (retained facade attribute)
    _party_menu_ready,  # noqa: F401  (retained facade attribute)
    _party_summary,
    _planned_move_cursor,  # noqa: F401  (retained facade attribute)
    _pump_menu_input,  # noqa: F401  (retained facade attribute)
    _record_confirmed_slot,  # noqa: F401  (retained facade attribute)
    _replayable_turn_plan,
    _settle_turn,
    _slot_usable,  # noqa: F401  (retained facade attribute)
    _wait_for_both_ended,
    _wait_for_fainted_side,
    _wait_for_side_menu,
    _word,  # noqa: F401  (retained facade attribute)
    _wram_bank,  # noqa: F401  (retained facade attribute)
    _wram_byte,  # noqa: F401  (retained facade attribute)
)
from scripts.produce_battle_state_fixtures_manifest import (
    _capture_provenance,
    _fixture_path,
    _manifest_row,
    _resolve_root,
    _validate_fixture,
    _write_fixture,
)
from scripts.produce_battle_state_fixtures_model import (
    _REPO,
    BATTLE_PARTY_MENU,  # noqa: F401  (retained facade attribute)
    CAPTURE_COMMAND_TEMPLATE,  # noqa: F401  (retained facade attribute)
    COLOSSEUM_MAP_ID,
    COMMAND_MENU_MAX_ITEM,  # noqa: F401  (retained facade attribute)
    COMMAND_MENU_WATCHED_KEYS,  # noqa: F401  (retained facade attribute)
    FAINT_STEM,
    MENU_WATCHED_A,  # noqa: F401  (retained facade attribute)
    MOVE_MENU_MAX_ITEM,  # noqa: F401  (retained facade attribute)
    MOVE_MENU_WATCHED_KEYS,  # noqa: F401  (retained facade attribute)
    PARTY_MENU_ANIM_WITNESS,  # noqa: F401  (retained facade attribute)
    PARTY_MON_SIZE,  # noqa: F401  (retained facade attribute)
    PRE_TERMINAL_STEM,
    PRODUCER_PATH,  # noqa: F401  (retained facade attribute)
    REPLACEMENT_END_GRACE_FRAMES,  # noqa: F401  (retained facade attribute)
    TERMINAL_STEM,
    VARIANT_BY_VERSION,
    _atomic_write,
    _now_utc,
    _repo_commit,
    _runtime_label,  # noqa: F401  (retained facade attribute)
    _sha1,
    _sha256_of_bytes,
)


def produce(
    *,
    version: str,
    repo_root: Path,
    rom_root: Path,
    fixture_root: Path,
    max_turns: int,
    step_frames: int,
    first_turn_frames: int,
    turn_frames: int,
    faint_frames: int,
    replacement_frames: int,
    chunk_cycles: int,
    emit_manifest_rows: Path | None = None,
) -> int:
    # Asset resolution for the imported test helpers must be configured before
    # the module is imported: it caches ROM and fixture roots at import time.
    os.environ["POKERED_ROM_ROOT"] = str(rom_root)
    os.environ["POKERED_FIXTURE_ROOT"] = str(fixture_root)

    from pokered_harness.config import load_versions
    from pokered_harness.link.pyboy_link_session import PyBoyLinkSession
    from tests.test_pyboy_link_session_roms import (
        _ROM_PATHS,
        _assert_battle_fixture_is_legal,
        _battle_state_path,
        _close_linked_pair,
        _drive_complete_battle_turn,
        _drive_past_link_menu_to_colosseum,
        _install_battle_diag_counters,
        _open_session,
        _open_session_pair,
    )

    if os.environ.get("POKERED_SKIP_SHA1"):
        print("POKERED_SKIP_SHA1 must not be set for fixture production", file=sys.stderr)
        return 2

    pins = load_versions(repo_root / "VERSIONS.md")
    # The session opens the variant ROM selected by the canonical test map
    # (color-patched Red/Blue, canonical Yellow); recording anything else
    # would bind the fixture to a ROM it was never driven on.
    rom, sym = _ROM_PATHS[version]
    variant = VARIANT_BY_VERSION[version]
    if not rom.is_file():
        print(f"ROM not found: {rom}", file=sys.stderr)
        return 2
    if not sym.is_file():
        print(f"symbol file not found: {sym}", file=sys.stderr)
        return 2

    expected_rom_sha = pins.sha1_for_path(rom)
    expected_sym_sha = pins.symbol_sha1_for_path(sym)
    actual_rom_sha = _sha1(rom)
    actual_sym_sha = _sha1(sym)
    if expected_rom_sha is None or actual_rom_sha != expected_rom_sha:
        print(
            f"ROM pin mismatch for {rom}: expected={expected_rom_sha} actual={actual_rom_sha}",
            file=sys.stderr,
        )
        return 2
    if expected_sym_sha is None or actual_sym_sha != expected_sym_sha:
        print(
            f"SYM pin mismatch for {sym}: expected={expected_sym_sha} actual={actual_sym_sha}",
            file=sys.stderr,
        )
        return 2
    rom_root_resolved = Path(rom_root).resolve()
    try:
        rom_relative = f"rom/{Path(rom).resolve().relative_to(rom_root_resolved).as_posix()}"
        sym_relative = f"rom/{Path(sym).resolve().relative_to(rom_root_resolved).as_posix()}"
    except ValueError:
        print(
            f"ROM/SYM {rom} / {sym} are outside the declared --rom-root {rom_root}",
            file=sys.stderr,
        )
        return 2
    commit = _repo_commit(repo_root)
    print(
        f"producing {version} fixtures: ROM sha1={actual_rom_sha} "
        f"SYM sha1={actual_sym_sha} commit={commit}",
        flush=True,
    )

    fixture_root = fixture_root.resolve()
    faint_main = _fixture_path(fixture_root, version, FAINT_STEM, peer=False)
    faint_peer = _fixture_path(fixture_root, version, FAINT_STEM, peer=True)
    pre_main = _fixture_path(fixture_root, version, PRE_TERMINAL_STEM, peer=False)
    pre_peer = _fixture_path(fixture_root, version, PRE_TERMINAL_STEM, peer=True)
    terminal_main = _fixture_path(fixture_root, version, TERMINAL_STEM, peer=False)
    terminal_peer = _fixture_path(fixture_root, version, TERMINAL_STEM, peer=True)

    # Bind every derived state to the admitted battle fixture it was driven
    # from, by digest, so the manifest records a reproducible derivation chain.
    source_state_path = _battle_state_path(version)
    if not source_state_path.is_file():
        print(f"source battle fixture not found: {source_state_path}", file=sys.stderr)
        return 2
    source_bytes = source_state_path.read_bytes()
    try:
        source_relative = Path(source_state_path).resolve().relative_to(fixture_root).as_posix()
    except ValueError:
        print(
            f"source battle fixture {source_state_path} is outside {fixture_root}",
            file=sys.stderr,
        )
        return 2
    source_sha1 = hashlib.sha1(source_bytes).hexdigest()
    source_sha256 = _sha256_of_bytes(source_bytes)

    a = b = link = None
    started = time.monotonic()

    def note(message: str) -> None:
        print(f"[{time.monotonic() - started:7.1f}s] {message}", flush=True)

    assets = {"rom_path": str(rom), "sym_path": str(sym)}
    captured: dict[str, tuple[Path, dict, dict]] = {}
    captures: dict[str, dict] = {}

    def save_pair(
        *,
        boundary: str,
        rows: tuple[tuple[str, Path, object], ...],
        facts: dict,
        capture: dict | None = None,
    ) -> None:
        """Write a fixture pair plus the manifest row for each file."""

        provenance = _capture_provenance(
            source_relative=source_relative,
            source_sha1=source_sha1,
            source_sha256=source_sha256,
            version=version,
            variant=variant,
            pyboy_version=pins.pyboy_version,
            pyboy_revision=pins.pyboy_revision,
            max_turns=max_turns,
            boundary=boundary,
            captured_at_utc=_now_utc(),
            facts=facts,
        )
        for slug, path, session in rows:
            record = _write_fixture(
                path,
                session,
                provenance=provenance,
                boundary=boundary,
                assets=assets,
            )
            captured[slug] = (path, record, provenance)
        if capture is not None:
            for slug, _path, _session in rows:
                captures.setdefault(slug, {}).update(capture)

    def emit_rows() -> None:
        if emit_manifest_rows is None:
            return
        rows = [
            _manifest_row(
                slug=slug,
                record=record,
                provenance=provenance,
                fixture_root=fixture_root,
                version=version,
                variant=variant,
                rom_relative=rom_relative,
                rom_sha1=actual_rom_sha,
                sym_relative=sym_relative,
                sym_sha1=actual_sym_sha,
                capture=captures.get(slug) or None,
            )
            for slug, (_path, record, provenance) in sorted(captured.items())
        ]
        destination = Path(emit_manifest_rows).expanduser()
        _atomic_write(
            destination,
            json.dumps(rows, indent=2, sort_keys=True).encode(),
        )
        print(f"wrote {len(rows)} manifest rows to {destination}", flush=True)

    try:
        a, b = _open_session_pair(
            lambda: _open_session(version, state_path=_battle_state_path(version)),
            lambda: _open_session(version, state_path=_battle_state_path(version)),
        )
        _assert_battle_fixture_is_legal(a)
        _assert_battle_fixture_is_legal(b)

        link = PyBoyLinkSession.local()
        link.attach(a._pyboy)
        link.attach(b._pyboy)
        counters = _install_battle_diag_counters(a, b, versions=(version, version))

        warp = _drive_past_link_menu_to_colosseum(a, b, link)
        assert warp["final_map_a"] == COLOSSEUM_MAP_ID
        assert warp["final_map_b"] == COLOSSEUM_MAP_ID

        first_turn_baseline = _baseline(counters)
        _drive_complete_battle_turn(
            a,
            b,
            link,
            counters=counters,
            battle_budget_frames=first_turn_frames,
            completion="turn",
        )
        turn = 1
        note(f"turn {turn} resolved; settling first turn")
        result = _settle_turn(
            a,
            b,
            link,
            counters,
            step_frames=step_frames,
            frame_budget=turn_frames,
            chunk_cycles=chunk_cycles,
            baseline=first_turn_baseline,
            keep_alive=True,
        )

        faint_record = None
        terminal_record = None
        pre_terminal_record = None
        faints = 0
        pre_terminal_turn = None

        def capture_pre_terminal(next_turn: int) -> None:
            """Snapshot the command boundary the next driven turn starts from.

            Rewritten at every boundary, so the pair left on disk after the
            terminal is the last reloadable both-command boundary before the
            return.  A consumer that reloads it and replays the recorded plan
            reaches ``EndOfBattle`` from there, bridging any turn whose menu
            the ROM skips.
            """

            nonlocal pre_terminal_record, pre_terminal_turn
            pre_terminal_turn = next_turn
            save_pair(
                boundary="pre-terminal",
                rows=(
                    ("battle-pre-terminal", pre_main, a),
                    ("battle-pre-terminal-peer", pre_peer, b),
                ),
                facts={
                    "turns": next_turn,
                    "faints_observed": faints,
                    "commit": commit,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                },
                capture={"boundary_turn": next_turn},
            )
            pre_terminal_record = captured["battle-pre-terminal"][1]
            note(f"  pre-terminal boundary before turn {next_turn}")

        def begin_turn() -> None:
            """Count and snapshot the boundary the next driven turn starts from.

            Called only once both owners are at a live command menu, which is
            the only geometry a consumer can fold back into FIGHT and move
            selection.  The counter therefore counts real reloadable command
            boundaries, so the recorded ``pre_terminal_turn`` always names the
            pair left on disk.
            """

            nonlocal turn
            turn += 1
            capture_pre_terminal(turn)

        while turn <= max_turns:
            status = result.get("status")
            if status == "timeout":
                print(
                    f"turn {turn} did not reach a boundary within its finite budget",
                    file=sys.stderr,
                )
                return 1
            if status == "terminal":
                if not _wait_for_both_ended(
                    a,
                    b,
                    link,
                    step_frames=step_frames,
                    frame_budget=turn_frames,
                    chunk_cycles=chunk_cycles,
                ):
                    print(
                        "terminal edge observed but both sessions did not end",
                        file=sys.stderr,
                    )
                    return 1
                note(f"terminal reached after turn {turn}")
                plan = result.get("turn")
                if not _replayable_turn_plan(plan):
                    print(
                        "the terminal turn plan is not replayable per owner; no fixture fabricated",
                        file=sys.stderr,
                    )
                    return 1
                save_pair(
                    boundary="terminal",
                    rows=(
                        ("battle-terminal-return", terminal_main, a),
                        ("battle-terminal-return-peer", terminal_peer, b),
                    ),
                    facts={
                        "turns": turn,
                        "faints_observed": faints,
                        "commit": commit,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                    },
                    capture={
                        # Both rows describe the same driven turn: the captured
                        # boundary and the plan recorded with it are the ones
                        # the terminal return was reached from, so the two rows
                        # agree by construction rather than by accident.
                        "terminal_turn": pre_terminal_turn,
                        "pre_terminal_turn": pre_terminal_turn,
                        "terminal_turn_plan": plan,
                        # The party summaries come from the live sessions at
                        # the terminal instant: ``save_pair`` fills ``captured``
                        # while it writes, so its records cannot be read back
                        # while the call arguments are being evaluated.
                        "party": {
                            "primary": _party_summary(a),
                            "peer": _party_summary(b),
                        },
                    },
                )
                terminal_record = captured["battle-terminal-return"][1]
                if pre_terminal_record is not None:
                    captures.setdefault("battle-pre-terminal", {}).update(
                        {
                            "terminal_turn": pre_terminal_turn,
                            "terminal_turn_plan": plan,
                        }
                    )
                    captures.setdefault("battle-pre-terminal-peer", {}).update(
                        {
                            "terminal_turn": pre_terminal_turn,
                            "terminal_turn_plan": plan,
                        }
                    )
                note(f"  terminal primary party={terminal_record['party']}")
                break

            if status == "faint":
                # The turn that produced this faint is the one the recorded
                # plan drove, so carry that plan forward through every exit
                # below: whichever of them reaches the terminal return must
                # keep naming the turn the terminal was reached from.
                turn_plan = result.get("turn")
                index, waited = _wait_for_fainted_side(
                    a,
                    b,
                    link,
                    step_frames=step_frames,
                    frame_budget=faint_frames,
                    chunk_cycles=chunk_cycles,
                )
                if index is None:
                    if _battle_ended(a) and _battle_ended(b):
                        # The deciding turn was already driven by the recorded
                        # plan; carry it forward so the terminal row keeps the
                        # replay of the turn that ended the battle.
                        result = {
                            "status": "terminal",
                            "spent": waited,
                            "turn": turn_plan,
                        }
                        continue
                    print(
                        "a faint was observed but no fainted side resolved",
                        file=sys.stderr,
                    )
                    return 1
                fainted = (a, b)[index]
                other = (b, a)[index]
                faints += 1
                menu_spent = _wait_for_side_menu(
                    fainted,
                    other,
                    link,
                    step_frames=step_frames,
                    frame_budget=faint_frames,
                    chunk_cycles=chunk_cycles,
                )
                if menu_spent is None and faint_record is None:
                    print(
                        "the first faint did not open a replacement menu",
                        file=sys.stderr,
                    )
                    return 1
                if menu_spent is not None:
                    # The last forced-replacement boundary before the deciding
                    # knockout stays on disk, so a consumer can drive the few
                    # remaining turns to the terminal return from the fixture.
                    save_pair(
                        boundary="forced-replacement",
                        rows=(
                            ("battle-faint", faint_main, fainted),
                            ("battle-faint-peer", faint_peer, other),
                        ),
                        facts={
                            "turns": turn,
                            "faints_observed": faints,
                            "fainted_side": "primary" if index == 0 else "peer",
                            "commit": commit,
                            "elapsed_seconds": round(time.monotonic() - started, 3),
                        },
                    )
                    faint_record = captured["battle-faint"][1]
                    note(f"  forced-replacement party={faint_record['party']}")
                if not _choose_healthy_replacement(
                    fainted,
                    link,
                    step_frames=step_frames,
                    frame_budget=replacement_frames,
                    chunk_cycles=chunk_cycles,
                ):
                    if _wait_for_both_ended(
                        a,
                        b,
                        link,
                        step_frames=step_frames,
                        frame_budget=turn_frames,
                        chunk_cycles=chunk_cycles,
                    ):
                        result = {
                            "status": "terminal",
                            "spent": 0,
                            "turn": turn_plan,
                        }
                        continue
                    print(
                        "could not select a healthy replacement mon",
                        file=sys.stderr,
                    )
                    return 1
                note(f"turn {turn}: replacement selected; resuming")
                resumed = _settle_turn(
                    a,
                    b,
                    link,
                    counters,
                    step_frames=step_frames,
                    frame_budget=turn_frames,
                    chunk_cycles=chunk_cycles,
                    require_counter=False,
                    keep_alive=True,
                    # The replacement is answered inside the turn the recorded
                    # plan drove, so the same plan supplies this settlement's
                    # move cursors.  Without it every cursor is unknown, the
                    # move branch of the pump refuses to answer a live move
                    # menu (a stale cursor byte alone cannot prove one is
                    # live), and the pair deadlocks the moment the replacement
                    # hands control back to a move menu.
                    plan=turn_plan,
                )
                # The replacement is answered inside the same driven turn, so
                # a terminal return observed from here still belongs to the
                # plan recorded for that turn.  Without this the terminal row
                # would carry no plan and the capture would be refused.
                resumed.setdefault("turn", turn_plan)
                result = resumed
                continue

            # Boundary: start the next turn.
            result = _drive_next_turn(
                a,
                b,
                link,
                counters,
                step_frames=step_frames,
                frame_budget=turn_frames,
                chunk_cycles=chunk_cycles,
                on_boundary=begin_turn,
            )
            note(f"turn {turn} resolved: {result.get('status')} frames={result.get('spent')}")
        else:
            print(
                f"no terminal boundary within {max_turns} turns; no fixture fabricated",
                file=sys.stderr,
            )
            return 1

        if faint_record is None:
            print(
                "no forced-replacement boundary was reached; no fixture fabricated",
                file=sys.stderr,
            )
            return 1
        if pre_terminal_record is None:
            print(
                "no command boundary preceded the terminal return; no fixture fabricated",
                file=sys.stderr,
            )
            return 1
        if terminal_record is None:
            print("no terminal boundary was reached; no fixture fabricated", file=sys.stderr)
            return 1

        note("validating produced fixtures by reload")
        for path, boundary in (
            (faint_main, "faint"),
            (pre_main, "pre-terminal"),
            (terminal_main, "terminal"),
        ):
            if not path.is_file():
                print(f"expected fixture missing: {path}", file=sys.stderr)
                return 1
            _validate_fixture(version, path, boundary)
        emit_rows()
        elapsed = round(time.monotonic() - started, 3)
        print(
            f"DONE {version}: turns={turn} elapsed_seconds={elapsed} "
            f"faint={faint_main} pre_terminal={pre_main} terminal={terminal_main}",
            flush=True,
        )
        return 0
    finally:
        if link is not None or a is not None or b is not None:
            _close_linked_pair(link, *[session for session in (a, b) if session is not None])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, choices=("red", "blue", "yellow"))
    parser.add_argument("--repo-root", type=Path, default=_REPO)
    parser.add_argument("--rom-root", type=Path, default=None)
    parser.add_argument("--fixture-root", type=Path, default=None)
    parser.add_argument("--max-turns", type=int, default=80)
    parser.add_argument("--step-frames", type=int, default=20)
    parser.add_argument("--first-turn-frames", type=int, default=12000)
    parser.add_argument("--turn-frames", type=int, default=8000)
    parser.add_argument("--faint-frames", type=int, default=6000)
    parser.add_argument("--replacement-frames", type=int, default=4000)
    parser.add_argument("--chunk-cycles", type=int, default=256)
    parser.add_argument(
        "--emit-manifest-rows",
        type=Path,
        default=None,
        help=(
            "write the release-evidence/fixture-manifest.json rows for every "
            "fixture pair produced, as a JSON array"
        ),
    )
    args = parser.parse_args(argv)

    repo_root = args.repo_root.expanduser().resolve()
    rom_root = _resolve_root(
        args.rom_root or os.environ.get("ROM_ROOT") or os.environ.get("POKERED_ROM_ROOT") or "rom",
        repo_root,
    )
    fixture_root = _resolve_root(
        args.fixture_root
        or os.environ.get("FIXTURE_ROOT")
        or os.environ.get("POKERED_FIXTURE_ROOT")
        or "tests/fixtures/link",
        repo_root,
    )
    sys.path.insert(0, str(repo_root / "src"))
    return produce(
        version=args.version,
        repo_root=repo_root,
        rom_root=rom_root,
        fixture_root=fixture_root,
        max_turns=args.max_turns,
        step_frames=args.step_frames,
        first_turn_frames=args.first_turn_frames,
        turn_frames=args.turn_frames,
        faint_frames=args.faint_frames,
        replacement_frames=args.replacement_frames,
        chunk_cycles=args.chunk_cycles,
        emit_manifest_rows=args.emit_manifest_rows,
    )


if __name__ == "__main__":
    raise SystemExit(main())
