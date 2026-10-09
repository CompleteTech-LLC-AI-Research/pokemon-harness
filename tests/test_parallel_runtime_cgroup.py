"""Authored controls: kernel-owned job membership through a delegated cgroup (#107 F4).

The hierarchy is a temporary directory laid out like cgroup v2, so no real cgroup is created.  The
membership reader and the kill/empty proof are driven through their injectable roots and clocks.
"""

from pathlib import Path

from scripts.production_gate_parallel_cgroup import (
    JobCgroup,
    kernel_ownership,
    own_cgroup_path,
    qualification_after,
)


def hierarchy(tmp_path):
    root = tmp_path / "cgroup"
    parent = root / "user.slice" / "session"
    parent.mkdir(parents=True)
    (parent / "cgroup.procs").write_text("", encoding="ascii")
    return root


def test_the_own_cgroup_is_the_unified_hierarchy_line(tmp_path):
    proc = tmp_path / "proc" / "self"
    proc.mkdir(parents=True)
    (proc / "cgroup").write_text("12:cpu:/legacy\n0::/user.slice/session\n", encoding="ascii")
    assert own_cgroup_path(tmp_path / "proc") == "/user.slice/session"
    assert own_cgroup_path(tmp_path / "absent") is None


def test_a_delegated_parent_yields_a_job_cgroup_that_needs_the_kernel_control_files(tmp_path):
    root = hierarchy(tmp_path)
    refused = JobCgroup.create("job", root=root, own="/user.slice/session")
    assert not refused.available and "exposes no" in refused.reason
    assert not (root / "user.slice" / "session" / "job").exists(), "no half-created cgroup is left"
    created = JobCgroup.create("job", root=root, own="/user.slice/session", required=())
    assert created.available and created.relative == "/user.slice/session/job"


def test_a_parent_the_user_cannot_write_is_not_delegated(tmp_path, monkeypatch):
    root = hierarchy(tmp_path)
    monkeypatch.setattr(
        "scripts.production_gate_parallel_cgroup.os.access", lambda *_a, **_k: False
    )
    result = JobCgroup.create("job", root=root, own="/user.slice/session", required=())
    assert not result.available and "not delegated" in result.reason
    assert not (root / "user.slice" / "session" / "job").exists()
    assert not JobCgroup.create("job", root=root, own="/nonexistent", required=()).available


def test_membership_is_read_from_the_pids_own_proc_entry(tmp_path):
    root = hierarchy(tmp_path)
    group = JobCgroup.create("job", root=root, own="/user.slice/session", required=())
    group.proc_root = tmp_path / "proc"
    (group.proc_root / "11").mkdir(parents=True)
    (group.proc_root / "12").mkdir()
    (group.proc_root / "11" / "cgroup").write_text("0::/user.slice/session/job\n", encoding="ascii")
    (group.proc_root / "12" / "cgroup").write_text("0::/user.slice/session\n", encoding="ascii")
    assert group.contains(11) and not group.contains(12) and not group.contains(13)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_kill_is_proven_only_when_the_kernel_reports_the_group_empty(tmp_path):
    root = hierarchy(tmp_path)
    group = JobCgroup.create("job", root=root, own="/user.slice/session", required=())
    assert isinstance(group.path, Path)
    events = group.path / "cgroup.events"
    clock = Clock()
    events.write_text("populated 1\nfrozen 0\n", encoding="ascii")
    assert group.kill(clock=clock, sleeper=clock.sleep) is False, "a populated group is not proof"
    assert (group.path / "cgroup.kill").read_text() == "1"
    events.write_text("populated 0\nfrozen 0\n", encoding="ascii")
    assert group.kill(clock=Clock(), sleeper=lambda _s: None) is True
    assert JobCgroup(None).kill() is False and not JobCgroup(None).contains(1)


def test_kernel_ownership_is_required_or_explicitly_unqualified(tmp_path):
    root = hierarchy(tmp_path)

    def available(name):
        return JobCgroup.create(name, root=root, own="/user.slice/session", required=())

    def missing(name):
        return JobCgroup(None, "this process's cgroup is not delegated to the executing user")

    ready = kernel_ownership(True, create=available)
    assert ready["qualified"] and ready["admitted"] and "not claimed" in ready["qualification"]
    assert not list((root / "user.slice" / "session").glob("pokered-probe-*")), "probe removed"
    refused = kernel_ownership(True, create=missing)
    assert not refused["qualified"] and not refused["admitted"] and refused["reason"]
    permitted = kernel_ownership(False, create=missing)
    assert not permitted["qualified"] and permitted["admitted"]
    assert permitted["qualification"].startswith("UNQUALIFIED_COMPATIBILITY")


def _facts(joined, emptied):
    return {"process_tree": {"adoption": {"cgroup": {"joined": joined, "emptied": emptied}}}}


def test_qualification_comes_from_per_job_facts_never_from_a_label():
    probe = {"required": True, "qualified": True, "admitted": True}
    both = {"a": _facts(True, True), "b": _facts(True, True)}
    assert qualification_after(probe, both)["qualified_final"] is True
    assert (
        qualification_after(probe, {"a": _facts(True, True), "b": _facts(True, False)})[
            "qualified_final"
        ]
        is False
    )
    assert qualification_after(probe, {"a": _facts(False, True)})["qualified_final"] is False
    assert qualification_after(probe, {})["qualified_final"] is False, (
        "nothing started proves nothing"
    )
    assert qualification_after({**probe, "required": False}, both)["qualified_final"] is False
    assert qualification_after({**probe, "qualified": False}, both)["qualified_final"] is False
    assert (
        qualification_after(probe, {"a": {"process_tree": "garbage"}})["qualified_final"] is False
    )
