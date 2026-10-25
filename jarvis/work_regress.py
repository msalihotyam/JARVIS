"""Pure regression evidence parsing; no commands or files are opened here.

Callers supply complete, Core-captured output and plan/stage fixture manifests.
Unsupported, truncated or inconsistent summaries raise ValueError, never pass.
"""

from dataclasses import dataclass
import hashlib
import json
import re
from collections.abc import Mapping


@dataclass(frozen=True)
class TestResult:
    total: int
    failed_ids: frozenset[str]

    def __post_init__(self):
        object.__setattr__(self, "failed_ids", frozenset(self.failed_ids))
        if type(self.total) is not int or self.total < 0:
            raise ValueError("invalid test total")
        if any(not isinstance(item, str) or not item.strip() for item in self.failed_ids):
            raise ValueError("invalid failure identity")


@dataclass(frozen=True)
class RegressionVerdict:
    passed: bool
    new_failures: frozenset[str]
    reasons: tuple[str, ...]


def _one(pattern: str, output: str) -> str:
    matches = re.findall(pattern, output, re.MULTILINE)
    if len(matches) != 1:
        raise ValueError("missing or ambiguous completed summary")
    return matches[0]


def _node_suite(lines: list[str], index: int, indent: str) -> bool:
    """True when the YAML block after lines[index] declares a describe() suite."""
    if lines[index + 1:index + 2] != [indent + "---"]:
        return False
    for meta in lines[index + 2:]:
        if meta == indent + "...":
            return False
        if meta == indent + "type: 'suite'":
            return True
    return False


def parse_output(output: str, runner: str) -> TestResult:
    """Parse unittest, pytest, node TAP, or Jest text (explicit runner).

    Pytest requires its short failure summary; Jest requires failure headings.
    ANSI color escapes and CRLF are accepted. Multiple runs must be parsed
    separately rather than silently selecting one summary.
    """
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output).replace("\r\n", "\n")
    ids: list[str] = []
    if runner == "unittest":
        total = int(_one(r"^Ran (\d+) tests? in .+$", output))
        status = _one(r"^(OK(?: \([^\n]*\))?|FAILED \([^\n]*\))$", output)
        ids = re.findall(r"^(?:FAIL|ERROR): (.+)$", output, re.MULTILINE)
        count = sum(int(n) for n in re.findall(r"\b(?<!expected )(?:failures|errors)=(\d+)", status))
        # Unexpected successes and subtest failures cannot safely be assigned
        # to ordinary test identities from a summary alone.
        if "unexpected successes=" in status or (status.startswith("FAILED") and not count):
            raise ValueError("unsupported unexpected success")
    elif runner == "pytest":
        summary = _one(r"^(?:=+ )?(\d+ [^\n]+? in [\d.]+s(?: \([^\n]*\))?)(?: =+)?$", output)
        counts = re.findall(r"(\d+) (passed|failed|skipped|xfailed|xpassed|errors?|deselected)\b", summary)
        if not counts:
            raise ValueError("missing pytest counts")
        total = sum(int(n) for n, kind in counts if kind != "deselected")
        # Non-strict XPASS prints no FAILED line; strict XPASS is "failed".
        count = sum(int(n) for n, kind in counts if kind in {"failed", "error", "errors"})
        # A node id has no spaces outside its [param] part, which may hold
        # " - "; captured log lines ("ERROR    root:...") never match.
        ids = re.findall(r"^(?:FAILED|ERROR) ([^\s\[]+(?:\[[^\n]*?\])?)(?: - .*)?$", output, re.MULTILINE)
    elif runner == "node":
        total = int(_one(r"^# tests (\d+)$", output))
        count = int(_one(r"^# fail (\d+)$", output))
        _one(r"^# duration_ms ([\d.]+)$", output)
        if re.search(r"^# cancelled [1-9]\d*$", output, re.MULTILINE):
            raise ValueError("cancelled node tests")
        # TAP indentation identifies nested suite ancestry. Ordinal numbers
        # are deliberately excluded: inserting a test must not rename failures.
        # "# fail" counts tests (t.test parents included) but not describe()
        # suites or TODO/SKIP directives, so those not-ok lines are skipped.
        parents: list[tuple[int, str]] = []
        lines = output.splitlines()
        for index, line in enumerate(lines):
            sub = re.match(r"^( *)# Subtest: (.+)$", line)
            if sub:
                depth = len(sub[1])
                parents = [(d, name) for d, name in parents if d < depth]
                parents.append((depth, sub[2]))
            failure = re.match(r"^( *)not ok \d+ - (.*?)(?: # (\w+)\b.*)?$", line)
            if (failure and (failure[3] or "").upper() not in {"TODO", "SKIP"}
                    and not _node_suite(lines, index, failure[1] + "  ")):
                depth = len(failure[1])
                names = [name for d, name in parents if d < depth]
                ids.append(" > ".join([*names, failure[2]]))
    elif runner == "jest":
        summary = _one(r"^Tests: +(.+)$", output)
        total = int(_one(r"(\d+) total\b", summary))
        failed = re.search(r"(\d+) failed\b", summary)
        count = int(failed[1]) if failed else 0
        _one(r"^Test Suites: +(.+)$", output)
        file_id = None
        console = None
        for line in output.splitlines():
            # Multi-suite runs repeat every failure after this heading.
            if line.strip() == "Summary of all failing tests":
                break
            indent = len(line) - len(line.lstrip())
            # Logged text is indented under "● Console" and may look like
            # headings or failures; the block ends at its own indent or less.
            if console is not None:
                if not line.strip() or indent > console:
                    continue
                console = None
            heading = re.match(r"^ ?FAIL\s+(.+?)(?: \([\d.]+ m?s\))?$", line)
            if heading:
                file_id = heading[1]
            # Column-0 bullets are config warnings ("● Validation Warning:").
            case = re.match(r"^( +)● (.+)$", line)
            if case and case[2] == "Console":
                console = len(case[1])
            elif case:
                if file_id is None:
                    raise ValueError("Jest failure without file identity")
                ids.append(f"{file_id}::{case[2]}")
        suites_failed = re.search(r"^Test Suites:.*?(\d+) failed", output, re.MULTILINE)
        if suites_failed and not count:
            raise ValueError("Jest suite failed before tests ran")
    else:
        raise ValueError(f"unsupported runner: {runner}")
    if len(ids) != count or len(set(ids)) != count or count > total:
        raise ValueError("failure counts and identities disagree")
    return TestResult(total, frozenset(ids))


def fixture_list_hash(fixtures: Mapping[str, str]) -> str:
    """Hash a complete path-to-content-SHA256 manifest, independent of order.

    Content digests must be captured by the caller; hashing only path names
    would fail to detect edits. Removed and added paths change this hash too.
    """
    if any(not isinstance(path, str) or not path or not isinstance(digest, str)
           or not re.fullmatch(r"[0-9a-f]{64}", digest)
           for path, digest in fixtures.items()):
        raise ValueError("invalid fixture manifest")
    payload = json.dumps(dict(fixtures), sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compare_results(base: TestResult, stage: TestResult, *,
                    locked_fixture_hash: str, stage_fixture_hash: str) -> RegressionVerdict:
    """Compare evidence against the plan-locked fixture manifest digest."""
    for digest in (locked_fixture_hash, stage_fixture_hash):
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("missing or invalid fixture hash")
    new = stage.failed_ids - base.failed_ids
    reasons = []
    if new:
        reasons.append("new_failures")
    if stage.total < base.total:
        reasons.append("test_count_dropped")
    if locked_fixture_hash != stage_fixture_hash:
        reasons.append("locked_fixtures_changed")
    return RegressionVerdict(not reasons, new, tuple(reasons))
