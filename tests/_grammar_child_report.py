"""Pytest receipt for one actual authored grammar case, without copied assertions."""

from __future__ import annotations

import inspect
import json
import os
import sys
from pathlib import Path

import pytest

_receipt: dict[str, object] = {"reports": [], "collection_errors": []}


def pytest_configure(config: pytest.Config) -> None:
    root = Path(__file__).resolve().parents[1]
    expected_root = os.environ["POKERED_GRAMMAR_CHILD_ROOT"]
    _receipt.update(
        expected_nodeid=os.environ["POKERED_GRAMMAR_CHILD_NODEID"],
        root=str(root.resolve()),
        python_version=list(sys.version_info[:3]),
        interpreter=sys.executable,
        prefix=sys.prefix,
        base_prefix=sys.base_prefix,
    )
    if sys.version_info < (3, 12):
        raise pytest.UsageError("authored grammar child requires real Python >=3.12")
    if str(root.resolve()) != expected_root:
        raise pytest.UsageError("grammar child plugin belongs to another checkout")


def pytest_collection_finish(session: pytest.Session) -> None:
    expected = _receipt["expected_nodeid"]
    nodeids = [item.nodeid for item in session.items]
    _receipt["collected_nodeids"] = nodeids
    if nodeids != [expected]:
        raise pytest.UsageError("grammar child must collect exactly its expected node")
    function = session.items[0].function
    from tests import _timed_menu_milestone_sentinel_support as support

    analyzer = function.__globals__["_is_enforced"]
    _receipt.update(
        function_source=str(Path(inspect.getsourcefile(function)).resolve()),
        support_source=str(Path(support.__file__).resolve()),
        analyzer_source=str(Path(analyzer.__code__.co_filename).resolve()),
        same_analyzer_object=analyzer is support._is_enforced,
    )
    root = Path(str(_receipt["root"]))
    for key in ("function_source", "support_source", "analyzer_source"):
        if not Path(str(_receipt[key])).resolve(strict=True).is_relative_to(root / "tests"):
            raise pytest.UsageError(f"grammar child {key} belongs to another checkout")
    if analyzer is not support._is_enforced:
        raise pytest.UsageError("grammar case is not using the actual support analyzer")


def pytest_collectreport(report: pytest.CollectReport) -> None:
    if report.failed:
        _receipt["collection_errors"].append(report.nodeid)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    _receipt["reports"].append(
        {
            "nodeid": report.nodeid,
            "when": report.when,
            "outcome": report.outcome,
            "wasxfail": hasattr(report, "wasxfail"),
        }
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    _receipt["exitstatus"] = int(exitstatus)
    target = Path(os.environ["POKERED_GRAMMAR_CHILD_REPORT"])
    target.write_text(json.dumps(_receipt, indent=2) + "\n", encoding="utf-8")
