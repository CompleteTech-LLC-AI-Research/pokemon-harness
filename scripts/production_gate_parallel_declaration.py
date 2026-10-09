"""The executable coverage declaration of the concurrent runtime gate (#107)."""

from __future__ import annotations

COVERAGE_DECLARATION: dict[str, list[str]] = {
    "implemented": [
        "opt-in --parallel-runtimes with sequential execution as the unchanged default",
        (
            "immutable frozen plan: run/commit/per-runtime build/input/asset identities, the "
            "complete collected selection, derived finite deadlines, fail_fast and one capacity budget"
        ),
        (
            "private immutable snapshot (own git repository) of the checkout and ROM/fixtures with "
            "a real write-denial probe; verifiable equivalent-root provenance for the native guard"
        ),
        (
            "private frozen copy of the interpreter environment with kernel-checked protection of "
            "the base interpreter, stdlib and mapped libraries (a writable base refuses the run)"
        ),
        (
            "owner-held listening sockets handed to the production listeners with validation and "
            "acknowledgment; the effective child policy digests frozen in the plan"
        ),
        "phases: both additional smokes complete before any selected-tier job; both retained",
        (
            "rows from the producer's actual per-test terminal records, re-derived by the parent "
            "from the verified gate report; independently measured identities; bounded total JSON"
        ),
        (
            "capacity: the one global policy partitioned into per-child policies, session sampling "
            "of the run filesystem under background monitoring, fail-closed admission and loss"
        ),
        (
            "owned process trees: authenticated guardian, parent status-pipe absence proof, "
            "pidfd-verified signals, parent-death signals, proof before any release or removal"
        ),
        "orchestrator subreaper adoption with kernel-attributed pidfd cleanup and an adoption proof",
        "completed failures harvested before cancellation; partial evidence recovered and parsed",
        "a capacity loss reported by one child marks the one budget lost and cancels the siblings",
        (
            "production comparison of two re-validated bundles on parent-observed serial and "
            "concurrent intervals, resource traces and lifecycle proofs"
        ),
    ],
    "exclusions": [
        "full real-ROM dual-runtime run over the six tiers (#107.5) is NOT done",
        "elapsed-time comparison against sequential execution on equivalent conditions is NOT done",
        "operator allocation (#85/#86), #106 policy and the 18 orientation fixtures are unmet",
        "nothing in this candidate has been executed; authored controls are not gameplay evidence",
        (
            "not implemented: private copy of a writable base interpreter prefix, cgroup ownership "
            "of descendants, runtime manifest pinned in the plan, arbitration between child rows"
        ),
        "release status remains PARTIAL",
    ],
}

__all__ = ("COVERAGE_DECLARATION",)
