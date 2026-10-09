"""Structured per-case rows built from the real gate report (#107.3, #107.6).

The sequential gate already exports ``case_results``, ``selected_nodeids``, return
codes and ``failure_details``.  A worker turns them into ordered row records; the
validator checks every row against the frozen plan.  Row identity is mandatory: the
plan validator refuses ``require_row_ids=False`` and there is no counts-only path.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from scripts.production_gate_parallel_validation import is_int, is_text

ROW_KEYS = frozenset(
    {"case", "game", "id", "mode", "outcome", "repetition", "roles", "runtime", "source", "tier"}
)
ROW_SOURCES = frozenset({"case_results", "selected_nodeids+tier_status", "returncodes"})
OUTCOMES = frozenset({"passed", "failed", "timeout", "interrupted", "unrun", "error"})
DIAGNOSTIC_KEYS = (
    "failure_details",
    "failure_details_omitted",
    "iteration_failures",
    "output_tail",
    "reason",
    "skip_reasons",
    "case_results",
)
TIMING_TIER = "timing"
_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+")


def case_identity(nodeid: str, games: Sequence[str], roles: Sequence[str]) -> tuple[str, list[str]]:
    """Derive (game, ordered roles) from a parametrized node id; ('', []) when absent."""

    _, bracket, parameters = nodeid.partition("[")
    tokens = [token for token in _TOKEN_SPLIT.split(parameters if bracket else "") if token]
    game = next((token for token in tokens if token in games), "")
    found = [token for token in tokens if token in roles]
    return game, found


def _outcome(status: object, returncode: object) -> str:
    if status == "PASS":
        return "passed" if returncode in (0, None) else "failed"
    return {
        "FAIL": "failed",
        "TIMEOUT": "timeout",
        "INTERRUPTED": "interrupted",
        "UNRUN": "unrun",
        "BLOCKED": "unrun",
    }.get(str(status), "error")


def _row(
    mode: str,
    tier: str,
    case: str,
    repetition: int,
    outcome: str,
    source: str,
    games: Sequence[str],
    roles: Sequence[str],
) -> dict[str, Any]:
    game, found = case_identity(case, games, roles)
    return {
        "id": f"{mode}/{tier}/{case}",
        "runtime": mode,
        "tier": tier,
        "case": case,
        "game": game,
        "mode": mode,
        "roles": found,
        "repetition": repetition,
        "outcome": outcome,
        "source": source,
    }


def build_rows(
    mode: str, tiers: Sequence[Mapping[str, Any]], games: Sequence[str], roles: Sequence[str]
) -> list[dict[str, Any]]:
    """Build ordered row records from the observed gate-report tiers only."""

    rows: list[dict[str, Any]] = []
    for tier in tiers:
        name = str(tier.get("name"))
        codes = tier.get("returncodes")
        if name == TIMING_TIER:
            for index, code in enumerate(codes if isinstance(codes, list) else [], start=1):
                outcome = "passed" if code == 0 else "failed"
                rows.append(
                    _row(
                        mode,
                        name,
                        f"timing-rep-{index}",
                        index,
                        outcome,
                        "returncodes",
                        games,
                        roles,
                    )
                )
            continue
        cases = tier.get("case_results")
        if isinstance(cases, list) and cases:
            for case in cases:
                if isinstance(case, dict) and is_text(case.get("nodeid")):
                    outcome = _outcome(case.get("status"), case.get("returncode"))
                    rows.append(
                        _row(mode, name, case["nodeid"], 0, outcome, "case_results", games, roles)
                    )
            continue
        selected = tier.get("selected_nodeids")
        details = tier.get("failure_details")
        failed = (
            {d.get("nodeid") for d in details if isinstance(d, dict)}
            if isinstance(details, list)
            else set()
        )
        for nodeid in selected if isinstance(selected, list) else []:
            if not is_text(nodeid):
                continue
            if nodeid in failed:
                outcome = "failed"
            else:
                outcome = "passed" if tier.get("status") == "PASS" else "unrun"
            rows.append(
                _row(mode, name, nodeid, 0, outcome, "selected_nodeids+tier_status", games, roles)
            )
    return rows


def diagnostics_for_tier(tier: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the producer's own failure evidence verbatim; never strip it."""

    return {key: tier[key] for key in DIAGNOSTIC_KEYS if key in tier}


def _row_shape_problems(rows: list[Any]) -> list[str]:
    problems: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != ROW_KEYS:
            problems.append(f"row {index} is not a structured row record")
            continue
        texts = ("case", "game", "id", "mode", "runtime", "tier")
        if not all(isinstance(row[key], str) for key in texts) or not is_text(row["case"]):
            problems.append(f"row {index} has non-text identity fields")
        if not is_int(row["repetition"]) or row["repetition"] < 0:
            problems.append(f"row {index} has an invalid repetition")
        if not isinstance(row["roles"], list) or not all(isinstance(r, str) for r in row["roles"]):
            problems.append(f"row {index} has invalid roles")
        if not isinstance(row["outcome"], str) or row["outcome"] not in OUTCOMES:
            problems.append(f"row {index} has an invalid outcome")
        if not isinstance(row["source"], str) or row["source"] not in ROW_SOURCES:
            problems.append(f"row {index} has an unknown evidence source")
    return problems


def row_problems(
    rows: object, job: Mapping[str, Any], plan: Mapping[str, Any], tiers: Sequence[Any]
) -> list[str]:
    """Validate rows against the frozen plan; any problem withholds the job's PASS."""

    if not isinstance(rows, list) or not rows:
        return ["rows must be a non-empty list of structured row records"]
    problems = _row_shape_problems(rows)
    if problems:
        return problems
    mode, planned = job["mode"], list(job["tiers"])
    ids = [row["id"] for row in rows]
    if len(set(ids)) != len(ids):
        problems.append(f"duplicate row ids {sorted({i for i in ids if ids.count(i) > 1})[:5]}")
    order = [planned.index(row["tier"]) if row["tier"] in planned else -1 for row in rows]
    if -1 in order:
        problems.append("a row names a tier outside the job")
    elif order != sorted(order):
        problems.append("rows are not in the planned tier order")
    for row in rows:
        game, found = case_identity(row["case"], plan["games"], plan["roles"])
        if row["runtime"] != mode or row["mode"] != mode:
            problems.append(f"{row['id']}: row names a different runtime or mode")
        if row["id"] != f"{row['runtime']}/{row['tier']}/{row['case']}":
            problems.append(f"{row['id']}: id does not match its runtime/tier/case")
        if (row["game"], row["roles"]) != (game, found):
            problems.append(f"{row['id']}: game or ordered roles differ from the case identity")
        if row["outcome"] != "passed":
            problems.append(f"{row['id']}: outcome {row['outcome']}")
    # Timing is attested per repetition (the producer exports one return code per
    # repetition, not one outcome per case per repetition), so only repetition rows
    # are required for it.
    required = [
        (tier, case)
        for tier in planned
        if tier != TIMING_TIER
        for case in plan["required_nodeids"].get(tier, [])
    ]
    present = {(row["tier"], row["case"]) for row in rows}
    missing = [item for item in required if item not in present]
    if missing:
        problems.append(f"missing required rows ({len(missing)}): {missing[:3]}")
    for rep_index, rep in enumerate(job["timing_rep_ids"], start=1):
        match = [r for r in rows if r["tier"] == TIMING_TIER and r["case"] == rep]
        if len(match) != 1 or match[0]["repetition"] != rep_index:
            problems.append(f"timing repetition {rep} is missing or mis-numbered")
    if TIMING_TIER in planned:
        extra = [
            r["case"]
            for r in rows
            if r["tier"] == TIMING_TIER and r["case"] not in job["timing_rep_ids"]
        ]
        if extra:
            problems.append(f"unplanned timing repetitions {extra[:3]}")
    for tier in tiers:
        if not isinstance(tier, dict) or tier.get("status") != "PASS":
            continue
        counts = tier.get("counts")
        total = counts.get("total") if isinstance(counts, dict) else None
        owned = [r for r in rows if r["tier"] == tier.get("name")]
        expected = (
            len(tier["returncodes"])
            if tier.get("name") == TIMING_TIER and isinstance(tier.get("returncodes"), list)
            else total
        )
        if is_int(expected) and expected != len(owned):
            problems.append(
                f"{tier.get('name')}: {len(owned)} rows but the tier reports {expected}"
            )
    return problems
