"""Test modules of the concurrent runtime gate (#107), kept out of the 999-line registry."""

PARALLEL_RUNTIME_PARTS = (
    "adoption",
    "arbiter",
    "artifacts",
    "capacity",
    "cgroup",
    "comparison",
    "consumers",
    "enrollment",
    "gate",
    "holder",
    "identity",
    "isolation",
    "lifecycle",
    "measurement_budget",
    "nested_retention",
    "phases",
    "plan",
    "ports",
    "probe_descriptors",
    "probe_owner",
    "probe_protocol",
    "probe_repairs",
    "probe_launch",
    "probe_session",
    "collection",
    "probe_stopping",
    "processes",
    "records",
    "runtime_inputs",
    "validation",
)
PARALLEL_RUNTIME_MODULES = tuple(
    f"test_parallel_runtime_{part}.py" for part in PARALLEL_RUNTIME_PARTS
)
