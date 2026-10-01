"""Bounded public-MCP Red/Red trade using a versioned ordinary capture.

Output, including isolated child ROM copies, belongs outside version control.
This command executes one named row, not a full release matrix.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

from scripts._probe_timed_rom_pair_support import runtime_identity
from scripts.normal_red_battle_drive import complete_battle
from scripts.normal_red_battle_return import finish_battle_return
from scripts.normal_red_link_admission import SCENARIO, resolve_normal_red_assets
from scripts.normal_red_link_journal import JournalPair, TimedStream, TimingLedger
from tests._mcp_trade_records_rom_drivers_support import _drive_trade, _enter_trade_flow
from tests._mcp_trade_records_rom_oracle_support import _fixture_pair, assert_paired_exchange
from tests._mcp_trade_records_rom_support import LocalPair, _child_runtime_mode, _stdio_server
from tests._mcp_trade_records_rom_tests_support import _tcp_pair_servers


async def trade_row(output: Path, asset: dict, transport: str, journal, timing=None) -> dict:
    """Offer naturally different species through live public party cursors."""
    mode = _child_runtime_mode()
    context = (
        _stdio_server(output, asset, asset)
        if transport == "local"
        else _tcp_pair_servers(output, asset, asset)
    )
    async with context as endpoint:
        raw_pair = LocalPair(endpoint, mode) if transport == "local" else endpoint
        pair = JournalPair(raw_pair, journal, timing)
        before, peer_before = await _fixture_pair(pair, asset, asset, fixture=SCENARIO)
        assert len(before) == len(peer_before) == 6, (before, peer_before)
        # Ivysaur and Pidgey are ordinary caught/trained records, not synthetic
        # OT-ID edits. Their different species make the selected copy observable.
        assert before[0]["species"] == 9 and peer_before[5]["species"] == 36
        assert before[0]["digest"] != peer_before[5]["digest"]
        await _enter_trade_flow(pair)
        run = await _drive_trade(
            pair,
            party_counts=(6, 6),
            primary_before=before,
            peer_before=peer_before,
            primary_slot=0,
            peer_slot=5,
        )
        verdict = assert_paired_exchange(
            run,
            primary_before=before,
            peer_before=peer_before,
            primary_slot=0,
            peer_slot=5,
            label=f"{SCENARIO}-{transport}",
        )
        assert verdict["distinct_records"] is True
        assert run.offered == {0: 0, 1: 5}, run.offered
        result = {
            "scenario": SCENARIO,
            "transport": transport,
            "runtime_mode": mode,
            "verdict": verdict,
            "primary_before": before,
            "peer_before": peer_before,
            "observed_trade": run._asdict(),
        }
        await pair.eof()
    # The async context has completed every owned process cleanup before PASS.
    result["owned_process_cleanup_completed"] = True
    return result


async def battle_row(output, asset, transport, journal, timing=None):
    mode = _child_runtime_mode()
    context = (
        _stdio_server(output, asset, asset)
        if transport == "local"
        else _tcp_pair_servers(output, asset, asset)
    )
    async with context as endpoint:
        pair = JournalPair(
            LocalPair(endpoint, mode) if transport == "local" else endpoint, journal, timing
        )
        before, peer_before = await _fixture_pair(pair, asset, asset, fixture=SCENARIO)
        assert len(before) == len(peer_before) == 6
        pair.mark("fixture_loaded")
        before_states = [await pair.state(owner) for owner in range(2)]
        with (output / "battle-observations.jsonl").open("x") as raw_observations:
            observations = (
                raw_observations
                if timing is None
                else TimedStream(raw_observations, timing, "observation_stream_write_flush")
            )
            result = await complete_battle(pair, observations)
            pair.mark("battle_complete")
            observations.write(
                json.dumps({"phase": "terminal_edges_only", "result": result}) + "\n"
            )
            observations.flush()
            result["post_match_return"] = await finish_battle_return(
                pair, before_states, [before, peer_before], result["terminal"], result["last_live"]
            )
            pair.mark("return_complete")
            observations.write(
                json.dumps({"phase": "restored_usable_room", "result": result["post_match_return"]})
                + "\n"
            )
            observations.flush()
        result.update({"scenario": SCENARIO, "runtime_mode": mode, "transport": transport})
        await pair.eof()
    result["owned_process_cleanup_completed"] = True
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rom", type=Path, required=True)
    parser.add_argument("--symbols", type=Path, required=True)
    parser.add_argument("--fixture-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--transport", choices=("local", "tcp"), required=True)
    parser.add_argument("--kind", choices=("trade", "battle"), default="trade")
    args = parser.parse_args()
    if os.environ.get("POKERED_SKIP_SHA1"):
        raise ValueError("hash bypass is forbidden")
    asset = resolve_normal_red_assets(args.rom, args.symbols, args.fixture_root)
    import pyboy

    version = getattr(pyboy, "__version__", None)
    revision = getattr(pyboy, "__pokered_harness_revision__", None)
    if (
        version != asset["pins"]["expected_pyboy_version"]
        or revision != asset["pins"]["expected_pyboy_revision"]
    ):
        raise ValueError("active PyBoy version/revision does not match admitted runtime")
    runtime = runtime_identity()
    runtime.update({"pyboy_version": version, "pyboy_revision": revision})
    footprint = {
        name: {
            "path": str(Path(module.__file__).resolve()),
            "sha256": hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest(),
        }
        for name, module in tuple(sys.modules.items())
        if (
            name.startswith(
                ("scripts.normal_red_", "tests._mcp_trade_records", "tests._mcp_battle_phase")
            )
            or name == "__main__"
        )
        and getattr(module, "__file__", None)
    }
    args.output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    timing = TimingLedger()
    receipt = {
        "status": "FAILED",
        "scenario": SCENARIO,
        "transport": args.transport,
        "kind": args.kind,
        "fixture_sha256": asset["provenance"]["registry"]["sha256"],
        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "loaded_source_footprint": footprint,
        "runtime_identity": runtime,
    }
    try:
        row = trade_row if args.kind == "trade" else battle_row
        with (args.output / "pair-operations.jsonl").open("x") as journal:
            receipt.update(
                asyncio.run(
                    asyncio.wait_for(row(args.output, asset, args.transport, journal, timing), 1200)
                )
            )
        receipt["status"] = "PASS"
    except BaseException as exc:
        receipt["error"] = repr(exc)
        raise
    finally:
        receipt["seconds"] = time.monotonic() - started
        # Optional diagnostic field; absent from receipts written before it existed.
        receipt["timing"] = timing.summary()
        (args.output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
