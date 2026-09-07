# Upstream branch readiness template

> **Not a current readiness assertion.** The audited harness checkout does not
> contain an upstream branch or verifiable output for the historical commit
> names that previously appeared in this file. The harness does contain a
> pinned vendor runtime for its own release path; that is not an upstream
> branch.

Use this page only after recreating a candidate PyBoy branch and attaching
fresh evidence. The [APPLY.md](APPLY.md) recipe is illustrative and requires
review against the exact PyBoy base commit.

## Required branch record

- [ ] PyBoy upstream repository and exact base commit are recorded.
- [ ] The candidate branch name, head commit, and complete changed-file list
  are recorded.
- [ ] The harness source commit used for the import is recorded.
- [ ] Any Cython/build-system changes are reviewed and tested on each claimed
  platform.
- [ ] No generated ROM, save state, virtual environment, or vendor checkout is
  included in the branch record unless the upstream project explicitly
  requires it.

## Required verification

- [ ] New serial-core, coordinator, session, protocol, and network tests pass.
- [ ] The full PyBoy regression command passes with the exact output attached.
- [ ] Save-state compatibility is tested for both legacy and new state data,
  or the incompatibility and migration plan are explicit.
- [ ] The stock Cython build and any source/non-Cython development build have
  separately documented results.
- [ ] Real-ROM results, if claimed, identify ROM hashes, symbol hashes,
  fixture hashes, runtime build, deadlines, and teardown. A skipped test is
  not a pass.
- [ ] Review confirms that LinkMenu, transport, or synthetic protocol results
  are not described as a completed trade or battle.

## Decision

Until all required records and outputs are attached, the branch status is
`NOT READY FOR UPSTREAM CLAIMS`. Do not reuse historical test counts, timing,
commit IDs, or screenshots as current evidence.
