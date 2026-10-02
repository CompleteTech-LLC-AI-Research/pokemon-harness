# #551 — round 4 review: wheel-install finding adjudicated as PRE-EXISTING

## Review outcome

Independent review of `c1bef30`
(`ledger/REVIEW_551_c1bef30_indep.md`, SHA256
`9dee0b278db3421739339a5ba092c5fe5fef5745f383dfaecf819689b4b4f3fb`) returned REQUEST CHANGES with
one blocking finding and an explicit statement that the required mutation runs were **not
completed**, so the reviewer declined to certify the trust boundary. Reporting an incomplete
review as INCOMPLETE rather than issuing a soft approval is the correct behaviour and is recorded
as a successful review.

Two things came out of it: a claimed regression, and a genuine gap in my own verification.

## The claimed regression is pre-existing on trunk, not caused by this branch

The reviewer reported: *"a non-editable wheel built from the checkout is rejected by the guard"*,
with `pokered_harness` and `pyboy` both FAILing when installed from a locally built wheel.

I built the wheel from **trunk at `49d5136`** — the commit before any of my three #551 fix commits
— installed it non-editably into a clean venv, and ran the **trunk** guard:

    $ python3 -m pip wheel --no-deps -w ./house ./trunk
    $ wvenv/bin/python -m pip install --no-deps ./house/*.whl
    imported from: .../wvenv/lib/python3.11/site-packages/pokered_harness/__init__.py
    TRUNK guard status: FAIL
       pokered_harness FAIL .../site-packages/pokered_harness/__init__.py
       pyboy FAIL None

Trunk fails the same way. Running my head against the same wheel install also FAILs, with zero
untrusted meta-path finders — so my change is not involved in the refusal.

Mechanism, confirmed from the installed metadata rather than inferred:

    editable install direct_url.json:
      {"url":"file:///.../wt/fix534-path","dir_info":{"editable":true}}
    wheel install direct_url.json:
      {"archive_info": {}, "url": "file:///.../house/pokered_harness-0.1.0-py3-none-any.whl"}

A wheel's `direct_url.json` names the **`.whl` in the wheelhouse**, not the source tree it was
built from. `_is_this_checkout` resolves that path and finds neither `project_root` nor a
staging directory beneath it, so the distribution is not attributable to this checkout and
contributes no allowed root.

That logic is **byte-identical between `49d5136` and `c1bef30`**:

    $ diff <(git show 49d5136:scripts/check_import_origins.py) \
           <(git show c1bef30:scripts/check_import_origins.py) \
        | grep -E '^[<>].*(_installed_from|_is_this_checkout|direct_url|_allowed_roots)'
    (no output)

So this is a **pre-existing limitation of the guard's install-trust model**, not a regression
introduced by the meta-path work. It is out of scope for #551's blocker and I am not silently
folding an install-model change into a security fix.

### Is it a defect at all?

Arguably yes, and it is worth its own issue: a non-editable wheel install cannot be attributed to
its source checkout, so the guard refuses a legitimate lane. But:

- it is not caused by this PR and not introduced by it;
- the documented and CI-exercised release lane is `pip install -e ".[dev]"`, which works;
- widening `_is_this_checkout` to accept wheelhouse paths would *weaken* the provenance guarantee
  (any `.whl` on disk would become an allowed root), which is exactly the property #534 exists to
  protect. That trade-off needs its own decision and its own review, not a drive-by change.

Recorded as a new finding for triage rather than fixed here. It should not block #551's blocker.

## The genuine gap: my mutation matrix was incomplete

The reviewer did not finish the six required mutation runs, so **the code-provenance fix has not
been independently mutation-tested**. My own evidence for `c1bef30` is therefore weaker than for
`bdd9786` and `947e3ec`, and I am not claiming otherwise. This is the part of the review that
matters, and it must be closed before #551 can merge.

The mutation `functools.partial` and callable-object probes the reviewer *did* complete confirmed
the intended fail-closed behaviour: neither exposes readable Python code, so
`_finder_code_file` returns `None` and both are refused. Overwriting a function's `__code__` with
a chosen `co_filename` was also insufficient, because the claimed file did not exist.

## Status

- `c1bef30` is **not merged** and is **not certified**. The trust boundary needs the full mutation
  matrix run independently.
- The wheel finding is adjudicated pre-existing and triaged separately; it does not reopen the
  meta-path blocker.
- `#551` still has no APPROVE on any head. Every round so far has been REQUEST CHANGES, and each
  round has found a real defect in my previous fix — two of them the same class of mistake,
  trusting a self-report the finder controls.
