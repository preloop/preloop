"""Failure signatures from CI job logs and JUnit XML.

A signature names *what* failed (framework, test id, error class), not *when*,
so the same broken or flaky test produces the same id on every run, branch
and commit. Messages are normalized (numbers, hex ids, paths and quoted
values removed) before they contribute to the id, and test ids are kept as
the framework prints them.

Supported: pytest (short test summary and ``FAILED``/``ERROR`` lines), go test
(``--- FAIL:`` with its package), jest and vitest (``FAIL`` file lines, ``●``
and ``×`` test lines), and JUnit XML (``<testcase>`` with ``<failure>`` or
``<error>``). Logs from GitHub Actions and GitLab CI carry timestamp prefixes,
group markers and ANSI colour codes; they are stripped first.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional
from xml.etree import ElementTree

#: Most signatures kept per log; the rest are counted in ``truncated``.
MAX_SIGNATURES_PER_LOG = 25
#: Most failure lines kept per log.
MAX_FAILURE_LINES = 8
#: Characters kept per failure line.
LINE_CHARS = 240
#: Largest JUnit document parsed.
MAX_JUNIT_BYTES = 2 * 1024 * 1024

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_GH_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?")
_GITLAB_SECTION = re.compile(r"section_(?:start|end):\d+:[\w.-]+(?:\[[^\]]*\])?\r?")

_PYTEST = re.compile(
    r"^(?P<kind>FAILED|ERROR) (?P<test>\S+?::\S+|\S+\.py)"
    r"(?: - (?P<error>[A-Za-z_][\w.]*)(?::\s*(?P<message>.*))?)?\s*$"
)
_GO_FAIL = re.compile(r"^\s*--- FAIL: (?P<test>\S+) \(\d+(?:\.\d+)?s\)")
_GO_PKG = re.compile(r"^FAIL\s+(?P<pkg>[\w./-]+)\s+\d+(?:\.\d+)?s\s*$")
_GO_PANIC = re.compile(r"^panic: (?P<message>.+)$")
_JS_FILE = re.compile(r"^\s*FAIL\s+(?P<file>\S+\.(?:[cm]?[jt]sx?))(?:\s|$)")
_JEST_TEST = re.compile(r"^\s*● (?P<test>.+?)\s*$")
_VITEST_TEST = re.compile(
    r"^\s*(?:FAIL|×|✗)\s+(?P<file>\S+\.(?:[cm]?[jt]sx?))\s+>\s+(?P<test>.+?)"
    r"(?:\s+\d+ms)?\s*$"
)
_JS_ERROR = re.compile(r"^\s*(?P<error>(?:[A-Z]\w*)?Error|AssertionError):\s")
_FAILURE_LINE = re.compile(
    r"(?:\bError\b|\bFAILED\b|\bFAIL\b|\bTraceback\b|\bpanic:|\bAssertionError\b|"
    r"\bexit code [1-9]|\bTimeout|\bTimed out\b|##\[error\]|\bE\s{2,})",
    re.IGNORECASE,
)

_NORMALIZE = (
    (re.compile(r"0x[0-9a-fA-F]+"), "<hex>"),
    (re.compile(r"\b[0-9a-f]{7,64}\b"), "<sha>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|m)?\b"), "<n>"),
    (re.compile(r"(['\"]).*?\1"), "<str>"),
    (re.compile(r"(?:/[\w.-]+){2,}"), "<path>"),
)


@dataclass(frozen=True)
class Signature:
    """One failing test (or test file, or job step) and why."""

    framework: str
    test: str
    error: str = ""

    @property
    def id(self) -> str:
        """Stable 12-character id for the ledger."""
        raw = f"{self.framework}\x00{self.test}\x00{self.error}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    def as_dict(self) -> dict[str, str]:
        """Serialize for the report and the ledger."""
        return {
            "id": self.id,
            "framework": self.framework,
            "test": self.test,
            "error": self.error,
        }


@dataclass
class LogFindings:
    """What one failed job's log says."""

    signatures: list[Signature] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    truncated: int = 0

    def add(self, signature: Signature) -> None:
        """Add a signature once, respecting the cap."""
        if signature in self.signatures:
            return
        if len(self.signatures) >= MAX_SIGNATURES_PER_LOG:
            self.truncated += 1
            return
        self.signatures.append(signature)


def clean_line(line: str) -> str:
    """Strip CI decorations from one log line.

    Args:
        line: Raw line.

    Returns:
        The line without ANSI codes, timestamp prefix or section markers.
    """
    line = _ANSI.sub("", line)
    line = _GITLAB_SECTION.sub("", line)
    line = _GH_TIMESTAMP.sub("", line)
    return line.rstrip("\r\n")


def normalize_error(text: Optional[str]) -> str:
    """Reduce an error class or message to its stable part.

    Args:
        text: Error text.

    Returns:
        At most 80 characters with volatile tokens replaced.
    """
    if not text:
        return ""
    value = text.strip()
    for pattern, replacement in _NORMALIZE:
        value = pattern.sub(replacement, value)
    return value[:80]


def parse_log(text: str) -> LogFindings:
    """Extract failure signatures and the most telling lines from a job log.

    Args:
        text: Job log text (any size; only lines are scanned).

    Returns:
        The findings. A log with failure lines but no recognized test output
        yields no signature; callers then fall back to a job-level signature.
    """
    findings = LogFindings()
    go_pending: list[str] = []
    js_file: Optional[str] = None
    jest_test: Optional[str] = None
    for raw in text.splitlines():
        line = clean_line(raw)
        if not line.strip():
            continue
        if _FAILURE_LINE.search(line) and len(findings.lines) < MAX_FAILURE_LINES:
            short = line.strip()[:LINE_CHARS]
            if short not in findings.lines:
                findings.lines.append(short)

        match = _PYTEST.match(line.strip())
        if match:
            findings.add(
                Signature(
                    "pytest",
                    match["test"],
                    normalize_error(match["error"] or match["kind"].lower()),
                )
            )
            continue
        match = _GO_FAIL.match(line)
        if match:
            go_pending.append(match["test"])
            continue
        match = _GO_PKG.match(line)
        if match:
            for test in go_pending:
                findings.add(Signature("go", f"{match['pkg']}.{test}", "fail"))
            go_pending = []
            continue
        match = _GO_PANIC.match(line)
        if match and go_pending:
            findings.add(
                Signature("go", go_pending[-1], "panic: " + normalize_error(match[1]))
            )
            continue
        match = _VITEST_TEST.match(line)
        if match:
            findings.add(Signature("vitest", f"{match['file']} > {match['test']}"))
            continue
        match = _JS_FILE.match(line)
        if match:
            js_file = match["file"]
            jest_test = None
            continue
        match = _JEST_TEST.match(line)
        if match and js_file:
            jest_test = match["test"]
            findings.add(Signature("jest", f"{js_file} > {jest_test}"))
            continue
        match = _JS_ERROR.match(line)
        if match and js_file and jest_test:
            previous = Signature("jest", f"{js_file} > {jest_test}")
            if previous in findings.signatures:
                index = findings.signatures.index(previous)
                findings.signatures[index] = Signature(
                    "jest", previous.test, normalize_error(match["error"])
                )
            jest_test = None
    # go test without a trailing package line (truncated log).
    for test in go_pending:
        findings.add(Signature("go", test, "fail"))
    # A JUnit report printed into the log (a step that cats its report).
    junit = _junit_block(text)
    if junit:
        findings = merge([findings, parse_junit(junit)])
    return findings


_JUNIT_OPEN = re.compile(r"<testsuites?\b")


def _junit_block(text: str) -> Optional[str]:
    """The first JUnit document in a log, with CI line prefixes removed."""
    if "<testsuite" not in text:
        return None
    lines = [clean_line(line) for line in text.splitlines()]
    joined = "\n".join(lines)
    match = _JUNIT_OPEN.search(joined)
    if not match:
        return None
    tag = (
        "testsuites" if joined.startswith("<testsuites", match.start()) else "testsuite"
    )
    end = joined.find(f"</{tag}>", match.start())
    if end < 0:
        return None
    return joined[match.start() : end + len(tag) + 3]


def parse_junit(document: str | bytes) -> LogFindings:
    """Extract signatures from a JUnit XML report.

    Args:
        document: The XML text. Documents over :data:`MAX_JUNIT_BYTES` or
            with a DOCTYPE (entity expansion) are refused and yield nothing.

    Returns:
        The findings.
    """
    findings = LogFindings()
    data = document.encode() if isinstance(document, str) else document
    if len(data) > MAX_JUNIT_BYTES or b"<!DOCTYPE" in data[:4096].upper():
        return findings
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return findings
    for case in root.iter("testcase"):
        for tag in ("failure", "error"):
            problem = case.find(tag)
            if problem is None:
                continue
            classname = case.get("classname") or case.get("file") or ""
            name = case.get("name") or ""
            test = f"{classname}::{name}" if classname else name
            error = problem.get("type") or tag
            findings.add(Signature("junit", test, normalize_error(error)))
            message = (problem.get("message") or "").strip()
            if message and len(findings.lines) < MAX_FAILURE_LINES:
                findings.lines.append(f"{test}: {message}"[:LINE_CHARS])
            break
    return findings


def job_signature(workflow: str, job: str, step: Optional[str]) -> Signature:
    """Fallback signature for a failed job whose log names no test.

    Args:
        workflow: Workflow or pipeline name.
        job: Job name with matrix suffixes removed.
        step: Failed step name, if known.

    Returns:
        A ``job`` signature.
    """
    test = f"{workflow} / {job}" + (f" / {step}" if step else "")
    return Signature("job", test, "failed")


def merge(findings: Iterable[LogFindings]) -> LogFindings:
    """Combine findings from several sources for one job.

    Args:
        findings: Findings to merge, in priority order.

    Returns:
        One findings object.
    """
    merged = LogFindings()
    for item in findings:
        for signature in item.signatures:
            merged.add(signature)
        for line in item.lines:
            if len(merged.lines) < MAX_FAILURE_LINES and line not in merged.lines:
                merged.lines.append(line)
        merged.truncated += item.truncated
    return merged
