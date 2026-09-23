"""Shared constants, builders and merge helper for the battle coverage tests (#143).

Split from ``tests/test_battle_coverage.py`` for #143 with no behavior
change. Every helper and constant below is copied verbatim from the original
module. These tests never instantiate PyBoy or load a ROM/state: the
builders assemble synthetic report documents for the coverage report's
fail-closed accounting.
"""

import json
from pathlib import Path

from scripts import coverage_report as coverage

ROOT = Path(__file__).resolve().parents[1]


CATALOG_PATH = ROOT / "release-evidence" / "battle-scenarios.json"


MANIFEST_PATH = ROOT / "release-evidence" / "fixture-manifest.json"


_EXPECTED_RUNTIMES = ["source", "cython"]


_TERMINAL_FAILURES = ("skipped", "xfailed", "timed_out", "not_run", "failed", "error")


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _pinned_revision(catalog: dict) -> str:
    return catalog["coverage"]["evidence_policy"]["expected_pyboy_revision"]


def _selectors(catalog: dict) -> list[str]:
    return [case["selector"] for case in coverage.one_turn_pairing_cases(catalog)]


def _passing_collections(nodeids: list[str]) -> list[dict]:
    return [{"name": "python-module", "status": "PASS", "nodeids": list(nodeids)}]


def _case_input_hashes(catalog: dict, case: dict) -> dict:
    """Return observed per-endpoint ROM/symbol/fixture hashes for one case."""
    hashes: dict[str, str] = {}
    for role, scenario in coverage._case_endpoint_scenarios(catalog, case).items():
        hashes[f"{role}_rom_sha1"] = scenario["game"]["rom_sha1"]
        hashes[f"{role}_sym_sha1"] = scenario["game"]["sym_sha1"]
        hashes[f"{role}_fixture_sha1"] = scenario["fixture"]["sha1"]
    return hashes


def _passing_document(
    catalog: dict,
    *,
    runtimes: tuple[str, ...] = ("source", "cython"),
    revision: str | None = None,
    status: str = "passed",
) -> dict:
    revision = revision or _pinned_revision(catalog)
    runtime_blocks = [
        {
            "mode": mode,
            "runtime": {
                "pyboy_mode": mode,
                "pyboy_version": "2.7.0",
                "pyboy_revision": revision,
            },
            "collections": _passing_collections(_selectors(catalog)),
        }
        for mode in runtimes
    ]
    records = [
        {
            "nodeid": case["selector"],
            "runtime": mode,
            "status": status,
            "input_hashes": _case_input_hashes(catalog, case),
        }
        for case in coverage.one_turn_pairing_cases(catalog)
        for mode in runtimes
    ]
    return {
        "run_id": "synthetic-coverage-test",
        "commit": "a" * 40,
        "runtimes": runtime_blocks,
        "records": records,
    }


def _case_report(report: dict, selector: str, runtime: str) -> dict:
    return next(
        case
        for case in report["cases"]
        if case["selector"] == selector and case["runtime"] == runtime
    )


def _split_partial_document(catalog: dict, *, partial: bool, with_identity: bool) -> dict:
    cases = coverage.one_turn_pairing_cases(catalog)
    half = len(cases) // 2
    subsets = (cases[:half], cases[half:])
    revision = _pinned_revision(catalog)
    blocks: list[dict] = []
    for mode in ("source", "cython"):
        for index, subset in enumerate(subsets):
            block = {
                "mode": mode,
                "partial": partial,
                "runtime": {
                    "pyboy_mode": mode,
                    "pyboy_version": "2.7.0",
                    "pyboy_revision": revision,
                },
                "records": [
                    {"nodeid": case["selector"], "runtime": mode, "status": "passed"}
                    for case in subset
                ],
            }
            if with_identity:
                block["run_id"] = f"partial-run-{index}"
                block["commit"] = ("a" if index == 0 else "b") * 40
            blocks.append(block)
    return {"runtimes": blocks}


def _mechanics_case(catalog: dict) -> dict:
    return next(
        case
        for case in coverage.required_cases(catalog)
        if case["case_id"] == "mechanics_effect_0_no_additional_effect"
    )


def _mechanics_document(
    catalog: dict,
    *,
    runtimes: tuple[str, ...] = ("source", "cython"),
    status: str = "passed",
    effect: int | None = 0,
    hashes: dict | None = None,
) -> dict:
    case = _mechanics_case(catalog)
    selector = case["selector"]
    revision = _pinned_revision(catalog)
    blocks = [
        {
            "mode": mode,
            "runtime": {
                "pyboy_mode": mode,
                "pyboy_version": "2.7.0",
                "pyboy_revision": revision,
            },
            "collections": _passing_collections([selector]),
        }
        for mode in runtimes
    ]
    records = []
    for mode in runtimes:
        record = {
            "nodeid": selector,
            "runtime": mode,
            "status": status,
            "commit": "a" * 40,
            "input_hashes": dict(
                hashes if hashes is not None else _case_input_hashes(catalog, case)
            ),
        }
        if effect is not None:
            record["effect_id"] = effect
        records.append(record)
    return {
        "run_id": "synthetic-mechanics-test",
        "runtimes": blocks,
        "records": records,
    }


def _mechanics_status(catalog: dict, report: dict, runtime: str) -> dict:
    case = _mechanics_case(catalog)
    return next(
        item
        for item in report["cases"]
        if item["case_id"] == case["case_id"] and item["runtime"] == runtime
    )


def _dimension(catalog: dict, dimension_id: str) -> dict:
    return next(
        item for item in catalog["coverage"]["dimensions"] if item["dimension_id"] == dimension_id
    )
