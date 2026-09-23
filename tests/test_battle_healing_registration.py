"""ROM-free guard for the issue #90 medicine dual-runtime registration record.

This module owns the committed-bundle consistency check for leaf 90.3's runtime
half.  It lives outside the ROM-marked ``test_battle_healing_items_rom.py`` so
it carries no ``real_rom`` tier and needs no ``ROM_FREE_TESTS`` exception, and
so neither file exceeds the repository's 1000-line file-split bound (#122).

Nothing in the bundle it reads is trusted: the guard re-derives every claim it
makes from the committed files themselves, so an overclaim, a dropped tier, a
stale node id, or a leaked machine path fails here rather than at review time.

ROM-free by construction: it reads only committed files.
"""

from __future__ import annotations

import hashlib
import html
import importlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from pokered_harness.config import load_versions
from tests._rom_assets import PROJECT_ROOT
from tests._tier_config import KNOWN_TEST_MODULES

FIXTURE_VERSION = "yellow"
FIXTURE_NAME = "battle_healing.state"
FIXTURE_ID = "yellow-ordinary-battle-healing"
EVIDENCE_PATH = PROJECT_ROOT / "release-evidence" / "battle-healing-fixtures.json"
ROM_PIN_PATH = "rom/yellow/pokemon-yellow.gbc"
SYM_PIN_PATH = "rom/yellow/pokemon-yellow.sym"

# The dual-runtime registration bundle for this leaf's runtime half.  Nothing in
# it is trusted: the function below re-derives every claim it makes from the
# files themselves.  It is a committed record, so an overclaim or a leaked
# machine path must fail here rather than be discovered at review time.
QUALIFICATION_BUNDLE = (
    PROJECT_ROOT / "release-evidence" / "feature-qualification" / "issue90-medicine-yellow-2d87676"
)
ACCEPTANCE_NODE_ID = (
    "tests/test_battle_healing_items_rom.py"
    "::test_potion_heals_the_active_mon_from_the_battle_item_menu"
)
BUNDLE_TIERS = ("source", "cython")

# The committed file set of the bundle, as repository-relative POSIX paths.  An
# allowlist rather than a forbidden-name list: a stray save state or symbol file
# added next to the record must fail, not merely be ignored.
BUNDLE_FILES = frozenset(
    {
        "README.md",
        "results.txt",
        "runtime-identity.json",
        "junit/source-focused.xml",
        "junit/cython-focused.xml",
        "logs/source-focused.log",
        "logs/cython-focused.log",
    }
)

# Absolute-path fragments that must never reach a committed record.  The same
# tuple shape guards the build files in ``tests/test_runtime_packaging.py``; the
# ``/opt``, ``/srv``, ``/media`` and ``/run`` roots are included because they are
# ordinary places for operator data to live, not because this host uses them.
BUNDLE_FORBIDDEN_FRAGMENTS = (
    "/mnt/",
    "/home/",
    "/Users/",
    "C:\\Users\\",
    "C:/Users/",
    "/usr/",
    "/tmp/",
    "/var/",
    "/root/",
    "/etc/",
    "/opt/",
    "/srv/",
    "/media/",
    "/run/",
)

# The fragment list only names prefixes that are known in advance; these
# patterns are the general net.  They are applied to text with URLs and
# bracketed placeholder roots neutralized first, so a placeholder-rooted path
# such as ``[source-venv]/lib/python3.11/...`` and a documentation URL are not
# hits, while a bare absolute path is - including single-component forms
# (``/opt/private.log``), forms with spaces inside a component
# (``/opt/Private Data/evidence.log``), Windows drive and UNC forms, and
# ``file://`` URLs.
BUNDLE_URL_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s\"'<>\]\)]*")
BUNDLE_PLACEHOLDER_ROOT_PATTERN = re.compile(r"\[[a-z][a-z0-9\-]*\]")
BUNDLE_UNIX_ABSOLUTE_PATTERN = re.compile(
    # Each component has to carry a word character, so a relative remainder such
    # as ``yellow}/...`` is not read as an absolute path, while a single-component
    # form (``/opt``), a spaced component (``/opt/Private Data``) and a path glued
    # to a preceding colon or bracket are all still hits.  The repeated group is
    # optional precisely so a lone ``/opt`` is a hit too; components stop at markup
    # characters, so an XML self-closing tag is not read as a path.
    r"(?<![\w\]\)<>\}])/(?=[A-Za-z0-9_.~])"
    r"(?:[^/\n<>\"\']*\w[^/\n<>\"\']*/)*[^/\n<>\"\']*\w[^/\n<>\"\']*"
)
BUNDLE_WINDOWS_ABSOLUTE_PATTERN = re.compile(r"(?<![\w])[A-Za-z]:[\\/](?:[^\\/\n]+[\\/])*[^\\/\n]*")
BUNDLE_UNC_PATTERN = re.compile(r"(?<![\w:/])//[A-Za-z0-9_.\-]+[\\/]")
BUNDLE_UNC_BACKSLASH_PATTERN = re.compile(r"(?<![\w:\\])\\\\[A-Za-z0-9_.\-]+[\\/]")

# A PyBoy loader warning.  The payload after any run of spaces or tabs is what
# the operator's symbol table used to put there, so the guard compares whole
# payloads against the counted redaction marker the record names rather than
# looking for a ``<redacted:`` prefix: an extra space cannot smuggle a payload
# past the match, and a fabricated marker of the right shape but the wrong text
# is still symbol-table input.  Symbol-table input must never be committed.
BUNDLE_SYMBOL_WARNING_PATTERN = re.compile(r"Skipping \.sym line:[ \t]*(?P<payload>[^\n]*)")

# pytest's per-test outcome rows.  Anchored, so the identities behind the
# terminal count are read from the log itself rather than inferred from it.
BUNDLE_LOG_OUTCOME_PATTERN = re.compile(
    r"^(?P<outcome>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS) (?P<node_id>\S+)$",
    re.MULTILINE,
)

# pytest's terminal summary line, parsed rather than substring-searched: the
# whole line has to be the summary, so ``125 passed`` cannot satisfy a claim of
# ``25 passed`` and an extra count cannot hide inside a longer number.
BUNDLE_TERMINAL_LINE_PATTERN = re.compile(
    r"^(?P<parts>(?:\d+ (?:failed|passed|skipped|deselected|xfailed|xpassed"
    r"|warnings?|errors?|error))(?:, (?:\d+ (?:failed|passed|skipped|deselected"
    r"|xfailed|xpassed|warnings?|errors?|error)))*) in \d+\.\d+s$",
    re.MULTILINE,
)
BUNDLE_TERMINAL_PART_PATTERN = re.compile(r"(\d+) (\w+)")
BUNDLE_TERMINAL_KINDS = {
    "warning": "warnings",
    "warnings": "warnings",
    "error": "errors",
    "errors": "errors",
}

# One tier's row in ``results.txt``.  ``summary`` is captured whole so it can be
# compared with the log's terminal line, and the counters are captured separately
# so they can be compared with the executed node ids.
BUNDLE_RESULTS_ROW_PATTERN = re.compile(
    r"^(?P<status>PASS|FAIL)  (?P<tier>source|cython)  (?P<summary>.*?)  "
    r"tests=(?P<tests>\d+) failures=(?P<failures>\d+) "
    r"errors=(?P<errors>\d+) skipped=(?P<skipped>\d+)",
    re.MULTILINE,
)


def _is_lower_hex(value: object, length: int) -> bool:
    return (
        isinstance(value, str)
        and len(value) == length
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _load_evidence() -> dict:
    return json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))


def _evidence_fixture() -> dict:
    fixtures = [item for item in _load_evidence()["fixtures"] if item["id"] == FIXTURE_ID]
    assert len(fixtures) == 1, f"expected one {FIXTURE_ID!r} entry, found {len(fixtures)}"
    return fixtures[0]


def _bundle_junit(path: Path) -> dict:
    """Read one tier's JUnit XML, deriving outcomes from the testcase elements.

    The suite counters are claims about the run; the per-testcase elements are
    the evidence.  Every counter is recomputed from the children, so a
    ``<failure>`` added to a testcase cannot keep passing behind an unchanged
    ``failures="0"``, a dropped testcase cannot keep the old total, and a second
    suite cannot arrive unnoticed.
    """
    root = ET.parse(path).getroot()
    assert root.tag in {"testsuite", "testsuites"}, f"{path} is not a JUnit document"
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    assert len(suites) == 1, f"{path} carries {len(suites)} test suites; expected exactly one"
    suite = suites[0]
    assert not suite.findall("testsuite"), f"{path} nests an unexpected test suite"

    node_ids: list[str] = []
    outcomes: dict[str, str] = {}
    for case in suite.iter("testcase"):
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        assert classname and name, f"{path} carries a testcase without a class and a name"
        node_id = f"{classname.replace('.', '/')}.py::{name}"
        assert node_id not in outcomes, f"{path} lists {node_id} more than once"
        children = sorted(child.tag for child in case)
        assert set(children) <= {"failure", "error", "skipped"}, (
            f"{path}:{node_id} carries unexpected children {children}"
        )
        node_ids.append(node_id)
        outcomes[node_id] = children[0] if children else "passed"

    derived = {
        "tests": len(node_ids),
        "failures": list(outcomes.values()).count("failure"),
        "errors": list(outcomes.values()).count("error"),
        "skipped": list(outcomes.values()).count("skipped"),
    }
    for attribute, value in derived.items():
        assert int(suite.get(attribute, -1)) == value, (
            f"{path} reports {attribute}={suite.get(attribute)!r} but its testcases give {value}"
        )
    assert derived["tests"] > 0, f"{path} carries no testcases"
    return {"node_ids": node_ids, "outcomes": outcomes, **derived}


def _bundle_terminal_summary(log: str) -> dict:
    """Parse the single pytest terminal summary line out of a captured log.

    Parsing the whole line is the point: a substring search for ``N passed``
    also matches ``1N passed``, so a drifted count could satisfy it.
    """
    matches = list(BUNDLE_TERMINAL_LINE_PATTERN.finditer(log))
    assert len(matches) == 1, (
        f"expected exactly one pytest terminal summary line, found {len(matches)}"
    )
    line = matches[0].group(0)
    parts: dict[str, int] = {}
    for count, raw_kind in BUNDLE_TERMINAL_PART_PATTERN.findall(matches[0].group("parts")):
        kind = BUNDLE_TERMINAL_KINDS.get(raw_kind, raw_kind)
        assert kind not in parts, f"the terminal summary {line!r} repeats {kind}"
        parts[kind] = int(count)
    assert parts.get("passed"), f"the terminal summary {line!r} reports no passing tests"
    for kind in ("failed", "errors", "skipped", "xfailed", "error"):
        assert parts.get(kind, 0) == 0, f"the terminal summary {line!r} reports {kind}"
    return {"line": line, "passed": parts["passed"], "parts": parts}


def _bundle_logged_outcomes(log: str) -> list[str]:
    """The node ids one log reports, in the order it lists them.

    The terminal summary is only a count.  These rows are the identities behind
    that count, so reconciling them is what stops a coordinated total change -
    dropping a node from the record while the log still lists it, or adding one
    the log never ran - from passing on the counter alone.
    """
    rows = [
        (match.group("outcome"), match.group("node_id"))
        for match in BUNDLE_LOG_OUTCOME_PATTERN.finditer(log)
    ]
    assert rows, "the log reports no per-test outcome rows"
    non_passing = sorted({outcome for outcome, _ in rows} - {"PASSED"})
    assert not non_passing, f"the log reports non-passing outcome rows: {non_passing}"
    node_ids = [node_id for _, node_id in rows]
    assert len(set(node_ids)) == len(node_ids), "the log lists a node id more than once"
    return node_ids


def _bundle_symbol_warning_payloads(contents: str) -> set[str]:
    """Every PyBoy ``Skipping .sym line`` payload in one committed file."""
    return {match.group("payload") for match in BUNDLE_SYMBOL_WARNING_PATTERN.finditer(contents)}


def _bundle_json_strings(value: object):
    """Every string in a decoded JSON document, keys included."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _bundle_json_strings(key)
            yield from _bundle_json_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _bundle_json_strings(item)


def _bundle_scan_views(relative: str, contents: str) -> list[str]:
    """The textual views of one committed file the sanitizer must sweep.

    The serialized bytes are not enough.  A JSON string escapes a quote and an
    XML character reference spells a slash, so a payload or an absolute path can
    sit in a structured container that the raw scan never reads as one.  Each
    container is therefore decoded to the strings it actually holds - JSON string
    values, XML text, tails and attribute values - and those views are swept in
    addition to the raw text.
    """
    if relative.endswith(".json"):
        views = list(_bundle_json_strings(json.loads(contents)))
    elif relative.endswith(".xml"):
        root = ET.fromstring(contents)
        views = []
        for element in root.iter():
            views.append(element.text or "")
            views.append(element.tail or "")
            views.extend(str(value) for value in element.attrib.values())
    else:
        views = [contents]
    decoded = html.unescape(contents)
    if decoded != contents:
        views.append(decoded)
    return views


def _bundle_marker_payload(marker: str) -> str:
    """The payload of a recorded redaction marker, validated to be a warning."""
    line = marker.rstrip("\n")
    match = BUNDLE_SYMBOL_WARNING_PATTERN.search(line)
    assert match is not None, f"the recorded marker is not a PyBoy .sym warning: {marker!r}"
    assert match.end() == len(line), (
        f"the recorded marker carries trailing text after the warning: {line[match.end() :]!r}"
    )
    return match.group("payload")


def _bundle_results_row(results_text: str, tier: str) -> dict:
    """Parse one tier's row out of ``results.txt``."""
    rows = [
        match
        for match in BUNDLE_RESULTS_ROW_PATTERN.finditer(results_text)
        if match.group("tier") == tier
    ]
    assert len(rows) == 1, f"results.txt carries {len(rows)} rows for the {tier} tier"
    row = rows[0]
    assert row.group("status") == "PASS", f"the {tier} row is not a PASS row"
    return {
        "summary": row.group("summary"),
        "tests": int(row.group("tests")),
        "failures": int(row.group("failures")),
        "errors": int(row.group("errors")),
        "skipped": int(row.group("skipped")),
    }


def _bundle_absolute_path_leaks(contents: str) -> list[str]:
    """Absolute-path forms present in one committed file."""
    leaks = [fragment for fragment in BUNDLE_FORBIDDEN_FRAGMENTS if fragment in contents]
    if "file://" in contents:
        leaks.append("file://")
    # Documentation URLs are allowed, so they are neutralized before scanning.
    scrubbed = BUNDLE_URL_PATTERN.sub(" ", contents)
    # A bracketed placeholder stands in for a machine root.  Replacing it with a
    # word character leaves the remainder relative, so the scan does not fire on
    # the placeholder itself while still seeing a real absolute path that
    # follows it on the same line.
    scrubbed = BUNDLE_PLACEHOLDER_ROOT_PATTERN.sub("X", scrubbed)
    for pattern in (
        BUNDLE_UNIX_ABSOLUTE_PATTERN,
        BUNDLE_WINDOWS_ABSOLUTE_PATTERN,
        BUNDLE_UNC_PATTERN,
        BUNDLE_UNC_BACKSLASH_PATTERN,
    ):
        leaks.extend(match.group(0) for match in pattern.finditer(scrubbed))
    return sorted(set(leaks))


def test_runtime_registration_bundle_is_sanitized_and_consistent() -> None:
    """The committed dual-runtime record must be true, complete and clean.

    ROM-free: it reads only committed files.  It fails closed when the bundle is
    absent, disagrees with the harness pins, hides a missing tier, reports a
    non-terminal row, drops the acceptance node id, or carries an absolute local
    path, still carries symbol-table input, or gains a file the record does not
    describe.  A registration record that can drift from the run it describes is
    not evidence, so the drift is made to fail here.
    """
    acceptance_module_path, _, acceptance_name = ACCEPTANCE_NODE_ID.partition("::")
    acceptance_module = importlib.import_module(
        acceptance_module_path[: -len(".py")].replace("/", ".")
    )
    assert callable(getattr(acceptance_module, acceptance_name, None)), (
        "the registered acceptance node id does not name a live function"
    )

    present = {
        path.relative_to(QUALIFICATION_BUNDLE).as_posix()
        for path in QUALIFICATION_BUNDLE.rglob("*")
        if path.is_file()
    }
    assert present == set(BUNDLE_FILES), (
        "the bundle file set changed: "
        f"missing {sorted(set(BUNDLE_FILES) - present)}, "
        f"unexpected {sorted(present - set(BUNDLE_FILES))}"
    )

    identity = json.loads(
        (QUALIFICATION_BUNDLE / "runtime-identity.json").read_text(encoding="utf-8")
    )
    assert identity["issue"] == 90
    assert identity["leaf"] == "90.3"
    assert identity["acceptance_node_id"] == ACCEPTANCE_NODE_ID
    assert identity["worktree_clean_at_run"] is True

    # The record's own guardrail declaration has to keep saying that nothing was
    # weakened; a record that admits to a skip, an xfail, a widened bound or a
    # RAM edit is not a qualification record any more.
    guardrails = identity["guardrails"]
    weakened = sorted(key for key, value in guardrails.items() if isinstance(value, bool) and value)
    assert not weakened, f"the record declares a weakened guardrail: {weakened}"
    assert all(
        isinstance(value, str) and value
        for value in guardrails.values()
        if not isinstance(value, bool)
    ), "a guardrail is declared as an empty note"

    # The record names exactly one tested state, and it names it in full.
    for key in ("worktree_head", "worktree_tree"):
        assert _is_lower_hex(identity[key], 40), f"{key} is not a lowercase hex object id"

    pins = load_versions(PROJECT_ROOT / "VERSIONS.md")
    declared_revision = identity["vendored_revision_marker"]
    assert declared_revision == pins.pyboy_revision, (
        "the bundle's vendored revision marker disagrees with VERSIONS.md"
    )
    assert set(identity["tiers"]) == set(BUNDLE_TIERS), "a declared runtime is missing"

    for tier in BUNDLE_TIERS:
        tier_identity = identity["tiers"][tier]
        assert tier_identity["requested_tier"] == tier
        assert (
            tier_identity["python_version"] == identity["tiers"][BUNDLE_TIERS[0]]["python_version"]
        ), "the two tiers did not run the same interpreter version"
        assert tier_identity["pyboy_version"] == pins.pyboy_version
        assert tier_identity["pyboy_revision"] == pins.pyboy_revision
        assert tier_identity["revision_matches_vendored_pin"] is True

    # The dual-runtime claim rests on the imports each tier actually resolved:
    # source must come from the vendored tree with no compiled extension, and
    # cython must come from outside it with compiled extensions loaded.  The
    # declared-runtime flag is derived from those measured fields rather than
    # trusted, and the interpreter, import root, loader switch and module total
    # each have to agree with the tier being claimed, so swapping fields between
    # the two tiers cannot satisfy them all.
    source = identity["tiers"]["source"]
    cython = identity["tiers"]["cython"]
    for tier, tier_identity in (("source", source), ("cython", cython)):
        kinds = tier_identity["imported_by_kind"]
        assert kinds, f"the {tier} tier recorded no imported module kinds"
        assert all(isinstance(count, int) and count > 0 for count in kinds.values())
        assert tier_identity["imported_pyboy_modules"] == sum(kinds.values()), (
            f"the {tier} tier's module total disagrees with its imported kinds"
        )
        vendored_file = tier_identity["pyboy_file"].startswith("[worktree]/vendor/pyboy-src/")
        assert tier_identity["pyboy_imported_from_vendored_tree"] is vendored_file, (
            f"the {tier} tier's import root contradicts the module file it resolved"
        )
        loader = tier_identity["pyboy_no_cython"]
        declared = (
            vendored_file and not kinds.get(".so") and loader == "1"
            if tier == "source"
            else not vendored_file and bool(kinds.get(".so")) and loader is None
        )
        # The recorded flag is a claim and the derived predicate is the
        # measurement.  Requiring the measurement to be true - not merely to
        # equal the claim - is what stops a record that admits it did not run the
        # required runtime from passing by agreeing with itself.
        assert declared is True, (
            f"the {tier} tier does not measure as its declared runtime: "
            f"vendored={vendored_file}, extensions={kinds.get('.so', 0)}, loader={loader!r}"
        )
        assert tier_identity["is_declared_runtime"] is True, (
            f"the {tier} tier records is_declared_runtime={tier_identity['is_declared_runtime']!r}"
        )

    assert source["pyboy_imported_from_vendored_tree"] is True
    assert not source["imported_by_kind"].get(".so"), "the source tier loaded compiled extensions"
    assert "[worktree]/vendor/pyboy-src" in source["pythonpath"]
    assert source["python_executable"].startswith("[source-venv]/")
    assert cython["pyboy_imported_from_vendored_tree"] is False, (
        "the cython tier fell back to the vendored source tree"
    )
    assert cython["imported_by_kind"].get(".so"), "the cython tier loaded no compiled extensions"
    assert "vendor/pyboy-src" not in cython["pythonpath"]
    assert cython["python_executable"].startswith("[native-venv]/")
    assert source["python_executable"] != cython["python_executable"]

    results = identity["results_by_tier"]
    assert set(results) == set(BUNDLE_TIERS)
    results_text = (QUALIFICATION_BUNDLE / "results.txt").read_text(encoding="utf-8")
    for prefix, expected in (
        ("tested head   : ", identity["worktree_head"]),
        ("tested tree   : ", identity["worktree_tree"]),
        ("acceptance    : ", ACCEPTANCE_NODE_ID),
        ("modules       : ", " ".join(identity["modules"])),
    ):
        assert results_text.count(f"{prefix}{expected}") == 1, (
            f"results.txt does not name {prefix.strip()} exactly once as the record does"
        )

    executed: dict[str, list[str]] = {}
    for tier in BUNDLE_TIERS:
        recorded = results[tier]
        assert recorded["failures"] == 0 and recorded["errors"] == 0, (
            f"the {tier} row is not terminal: {recorded}"
        )
        assert recorded["skipped"] == 0, f"the {tier} row skipped a required test"
        assert recorded["tests"] > 0
        assert len(set(recorded["node_ids"])) == len(recorded["node_ids"]), (
            f"the {tier} row lists a node id more than once"
        )
        assert recorded["tests"] == len(recorded["node_ids"]), (
            f"the {tier} row's test count and its node ids disagree"
        )
        assert ACCEPTANCE_NODE_ID in recorded["node_ids"], (
            f"the {tier} row does not contain the acceptance node id"
        )

        junit = _bundle_junit(QUALIFICATION_BUNDLE / "junit" / f"{tier}-focused.xml")
        assert junit["node_ids"] == recorded["node_ids"], (
            f"the {tier} JUnit XML and the recorded node ids disagree"
        )
        assert junit["tests"] == recorded["tests"]
        assert (junit["failures"], junit["errors"], junit["skipped"]) == (0, 0, 0), (
            f"the {tier} JUnit XML records a non-passing testcase"
        )
        assert set(junit["outcomes"].values()) == {"passed"}, (
            f"the {tier} JUnit XML records a non-passing outcome"
        )
        executed[tier] = junit["node_ids"]

        log = (QUALIFICATION_BUNDLE / "logs" / f"{tier}-focused.log").read_text(encoding="utf-8")
        terminal = _bundle_terminal_summary(log)
        assert terminal["passed"] == recorded["tests"] == len(junit["node_ids"]), (
            f"the {tier} terminal count, the record and the JUnit XML disagree"
        )
        assert terminal["line"] == recorded["summary"], (
            f"the {tier} terminal line and the recorded summary disagree"
        )
        # The counter is a claim; the log's own outcome rows are the identities
        # behind it.  Reconciling them with the record, the JUnit XML and the
        # terminal count is what makes a coordinated count change fail: the rows
        # still name whatever really ran.
        logged = _bundle_logged_outcomes(log)
        assert logged == recorded["node_ids"], (
            f"the {tier} log's outcome rows disagree with the recorded node ids: "
            f"missing {sorted(set(recorded['node_ids']) - set(logged))}, "
            f"unexpected {sorted(set(logged) - set(recorded['node_ids']))}"
        )
        assert logged == junit["node_ids"], (
            f"the {tier} log's outcome rows disagree with the JUnit XML"
        )
        assert len(logged) == terminal["passed"], (
            f"the {tier} log lists {len(logged)} outcome rows but its summary claims "
            f"{terminal['passed']}"
        )
        assert logged.count(ACCEPTANCE_NODE_ID) == 1, (
            f"the {tier} log does not report the acceptance node id exactly once as passed"
        )

        row = _bundle_results_row(results_text, tier)
        assert row["summary"] == recorded["summary"], (
            f"the {tier} results.txt row and the record disagree on the summary"
        )
        assert (row["tests"], row["failures"], row["errors"], row["skipped"]) == (
            recorded["tests"],
            recorded["failures"],
            recorded["errors"],
            recorded["skipped"],
        ), f"the {tier} results.txt row and the record disagree on the counts"

    # The two tiers declare one shared selection, so their node lists must agree.
    assert executed[BUNDLE_TIERS[0]] == executed[BUNDLE_TIERS[1]], (
        "the two tiers did not execute the same selection"
    )

    # The module list is derived from the same run as the node ids, so it has to
    # name exactly the modules those node ids live in.  A hand-edited list would
    # otherwise let the record advertise a selection the tiers never executed.
    executed_modules: list[str] = []
    for node_id in executed[BUNDLE_TIERS[0]]:
        module = node_id.partition("::")[0]
        if module not in executed_modules:
            executed_modules.append(module)
    assert identity["modules"] == executed_modules, (
        "the recorded module list does not match the modules the tiers executed: "
        f"recorded {identity['modules']}, executed {executed_modules}"
    )
    for module in identity["modules"]:
        assert re.fullmatch(r"tests/[A-Za-z0-9_]+\.py", module), (
            f"the recorded module {module!r} is not a test module path"
        )
        assert module.partition("tests/")[2] in KNOWN_TEST_MODULES, (
            f"the recorded module {module!r} is not a reviewed test module"
        )

    # A record whose node ids no longer name live tests describes a tree that no
    # longer exists, so it must stop being citable rather than linger.  Every
    # recorded tier is checked, not only the first one.
    for tier in BUNDLE_TIERS:
        for node_id in results[tier]["node_ids"]:
            module_path, _, test_name = node_id.partition("::")
            module = importlib.import_module(module_path[: -len(".py")].replace("/", "."))
            assert callable(getattr(module, test_name, None)), (
                f"{node_id} is registered in the {tier} row but does not exist in this tree"
            )

    # The record's inputs are the harness's own declared inputs, not merely
    # strings that agree with each other.
    assets = {entry["label"]: entry for entry in identity["assets"]}
    fixture_label = f"tests/fixtures/link/{FIXTURE_VERSION}/{FIXTURE_NAME}"
    assert set(assets) == {ROM_PIN_PATH, SYM_PIN_PATH, fixture_label}
    assert assets[ROM_PIN_PATH]["sha1"] == pins.sha1_for_path(ROM_PIN_PATH), (
        "the ROM digest in the bundle disagrees with VERSIONS.md"
    )
    assert assets[SYM_PIN_PATH]["sha1"] == pins.symbol_sha1_for_path(SYM_PIN_PATH), (
        "the symbol-table digest in the bundle disagrees with VERSIONS.md"
    )
    fixture = _evidence_fixture()
    assert assets[fixture_label]["sha1"] == fixture["sha1"]
    assert assets[fixture_label]["sha256"] == fixture["sha256"]
    assert assets[fixture_label]["size_bytes"] == fixture["size_bytes"]
    for label, entry in assets.items():
        assert _is_lower_hex(entry["sha1"], 40), f"{label} carries no lowercase SHA-1"
        assert _is_lower_hex(entry["sha256"], 64), f"{label} carries no lowercase SHA-256"
        assert entry["size_bytes"] > 0

    # The redaction is a counted claim about the committed bytes: the file the
    # guard just read must be the redacted file the record names, the marker must
    # appear exactly once with the recorded count, and the elided payloads must
    # remain auditable outside the repository.
    redactions = identity["redactions"]
    assert set(redactions["tiers"]) == set(BUNDLE_TIERS)
    permitted_symbol_payloads: set[str] = set()
    for tier in BUNDLE_TIERS:
        record = redactions["tiers"][tier]
        assert record["file"] == f"logs/{tier}-focused.log"
        raw = (QUALIFICATION_BUNDLE / record["file"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == record["redacted_sha256"], (
            f"the committed {tier} log is not the redacted file the record names"
        )
        assert len(raw) == record["redacted_size_bytes"]
        text = raw.decode("utf-8")
        assert record["payload_lines_elided"] > 0, (
            f"the {tier} redaction elides no payloads, so it records nothing"
        )
        assert text.count(record["marker"]) == 1, (
            f"the committed {tier} log does not carry the recorded redaction marker exactly once"
        )
        assert f"<redacted: {record['payload_lines_elided']} private symbol payloads" in text, (
            f"the committed {tier} log marker does not carry the recorded elided count"
        )
        permitted_symbol_payloads.add(_bundle_marker_payload(record["marker"]))
        assert _is_lower_hex(record["private_original_sha256"], 64), (
            "the private original digest is not a lowercase SHA-256"
        )
        assert record["private_original_size_bytes"] > record["redacted_size_bytes"] > 0
        assert record["private_original_label"].startswith("private-game-states/"), (
            "the private originals must be retained outside the repository"
        )

    for relative in sorted(present):
        contents = (QUALIFICATION_BUNDLE / relative).read_text(encoding="utf-8", errors="replace")
        leaked: set[str] = set()
        for view in _bundle_scan_views(relative, contents):
            smuggled = _bundle_symbol_warning_payloads(view) - permitted_symbol_payloads
            assert not smuggled, (
                f"{relative} carries unredacted symbol-table input: {sorted(smuggled)}"
            )
            leaked |= set(_bundle_absolute_path_leaks(view))
        assert not leaked, f"{relative} leaks {sorted(leaked)}"
