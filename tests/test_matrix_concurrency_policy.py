"""Policy and accounting tests for the #106 matrix-concurrency benchmark.

The selection logic is the part that can silently do the wrong thing: a
benchmark that drops failed rows from its denominator will recommend a faster
configuration precisely because it did less work.  These tests pin that
behaviour down, along with the comparability rule and the fresh-directory
requirement that keeps two arms from colliding.
"""

# Class re-exports stay in source order to preserve pytest collection order.
# ruff: noqa: I001
from __future__ import annotations

import importlib.util  # noqa: F401
import json  # noqa: F401
import sys  # noqa: F401
from pathlib import Path  # noqa: F401

import pytest  # noqa: F401

from tests._matrix_concurrency_policy_test_support import (
    PROJECT_ROOT,  # noqa: F401
    _arm,  # noqa: F401
    _full_set,  # noqa: F401
    _load_benchmark,  # noqa: F401
    _plan,  # noqa: F401
    _tier,  # noqa: F401
    bench,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_accounting import (
    TestThroughputAccounting,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_selection import (
    TestCompleteness,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_selection import (
    TestComparability,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_selection import (
    TestSelectionOutcomes,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_reporting import (
    TestReportParsing,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_reporting import (
    TestPercentiles,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_execution import (
    TestArmCommand,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_reporting import (
    TestReportShape,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_reporting import (
    TestPlanValidation,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_selection import (
    TestGateVerdictGatesSelection,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_accounting import (
    TestMatrixRowAccounting,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_execution import (
    TestInputRootsAreForwarded,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_execution import (
    TestGateVerdictIsRecordedOnBothPaths,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_execution import (
    TestDeadlineHeadroomIsPerRow,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_selection import (
    TestSelectionCannotBypassTheShippedDefault,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_accounting import (
    TestThroughputCountsAllRequiredWork,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_execution import (
    TestArmTimeoutBound,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_selection import (
    TestUndeclaredWorkerCountsCannotBeSelected,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_selection import (
    TestPerTierRowIdentityIsComparableWork,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_selection import (
    TestPerTierConcurrencyIsReportedHonestly,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_reporting import (
    TestArmBoundIsScopedToTheRequestedRoot,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_selection import (
    TestDeclaredRowsMustAllProduceAnOutcome,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_selection import (
    TestRuntimesMustMeasureTheSameMatrix,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_accounting import (
    TestBlockedTiersKeepTheirDeclaredRows,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_selection import (
    TestRowIdentityNotJustCounts,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_accounting import (
    TestArmTotalsMustBeInternallyConsistent,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_selection import (
    TestDuplicateArmsCannotFakeAnImprovement,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_accounting import (
    TestThroughputIsLabelledForWhatItDivides,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_accounting import (
    TestGateRowStatusesAreAllAccountedFor,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_execution import (
    TestArmTimeoutChargesTheWholeMatrix,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_selection import (
    TestPerTierWorkerEvidenceMustAgreeWithTheArm,  # noqa: F401
)

from tests._matrix_concurrency_policy_tests_accounting import (
    TestDeclaredTotalsMustMatchRecordedRowIds,  # noqa: F401
)
