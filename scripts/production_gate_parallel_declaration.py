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
            "pidfd-verified signals, parent-death signals, explicit cleanup proof before any release or removal (an exceptional exit retains by default)"
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
        "only 29 authored V11 controls (43 observed cases) ran, on candidate a10b5854; the post-Astra repairs and their controls have NOT been executed; no control is gameplay, real-ROM or performance evidence",
        (
            "not implemented: writable site dirs of a non-venv interpreter (refused), host-enforced "
            "immutability, a complete library inventory; the qualified mode refuses a host "
            "without cgroup delegation and --parallel-allow-unqualified is never an acceptance"
        ),
        "release status remains PARTIAL",
    ],
}

LIMITATIONS = (
    "authored controls do not close the real-ROM dual-runtime comparison; release stays PARTIAL",
    (
        "the sequential comparison is NOT_RUN unless a serial bundle produced by this "
        "orchestrator for the same immutable plan is supplied and re-validated"
    ),
    "a listener that no owner leased binds normally and is not covered by the socket handoff",
    "private runtime copies refuse ordinary writes by mode bits only: runtime_immutability is NOT_COMPLETE",
    (
        "the arbiter secret reaches the gate through its environment block: the MAC proves job "
        "binding, not secrecy against a process of the same UID"
    ),
    (
        "kernel ownership is proven per job from joined and emptied facts and only on a host that "
        "delegates a cgroup; the unqualified mode is never an acceptance"
    ),
    "the post-Astra repairs and their new controls have not been executed and await a Sol-admitted batch; the earlier 43-case focused run covers only the a10b5854 controls",
)

__all__ = ("COVERAGE_DECLARATION", "LIMITATIONS")
