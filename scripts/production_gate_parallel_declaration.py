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
            "private copy of the interpreter environment that refuses ordinary writes by mode bits "
            "(NOT complete immutability), kernel-checked protection of other inputs, the real "
            "default sys.path; each runtime digest frozen in the plan and verified by the worker"
        ),
        (
            "owner-held listening sockets handed to the production listeners with validation and "
            "acknowledgment; the effective child policy digests frozen in the plan"
        ),
        (
            "live parent arbiter: every child row reservation, release, block, expiry and loss over "
            "a bounded authenticated acknowledged protocol; a lost arbiter (including a dropped "
            "connection, whose reservations are retained until cleanup is proved), guardian, policy "
            "or capacity closes admission and signals every owned guardian at once; explicit "
            "production enrolment bound to the digest of the policy bytes parsed"
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
        (
            "orchestrator subreaper adoption with kernel-attributed pidfd cleanup (retention "
            "evidence, not a closure of the escaped-orphan window); the qualified mode REQUIRES a "
            "delegated per-job cgroup and refuses before any child without it; the explicit "
            "unqualified mode is labelled UNQUALIFIED_COMPATIBILITY"
        ),
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
            "not implemented: writable site dirs of a non-venv interpreter (refused), host-enforced "
            "immutability, a complete library inventory; the qualified mode refuses a host "
            "without cgroup delegation and --parallel-allow-unqualified is never an acceptance"
        ),
        "release status remains PARTIAL",
    ],
}

__all__ = ("COVERAGE_DECLARATION",)
