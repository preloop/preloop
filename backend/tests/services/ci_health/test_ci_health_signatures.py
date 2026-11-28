"""Failure signatures from recorded pytest, go, vitest, jest and JUnit output."""

from preloop.services.ci_health import signatures as sig

from tests.services.ci_health.conftest import FIXTURES


def _tests(findings: sig.LogFindings) -> list[tuple[str, str, str]]:
    return [(s.framework, s.test, s.error) for s in findings.signatures]


def test_pytest_summary_lines_with_actions_decorations() -> None:
    findings = sig.parse_log((FIXTURES / "github/log_1011.txt").read_text())
    assert _tests(findings) == [
        (
            "pytest",
            "tests/api/test_orders.py::TestCheckout::test_retry_after_timeout",
            "AssertionError",
        ),
        (
            "pytest",
            "tests/db/test_migrations.py::test_upgrade_head",
            "sqlalchemy.exc.OperationalError",
        ),
    ]
    # Failure lines are kept without timestamps or colour codes.
    assert any(line.startswith("E       AssertionError") for line in findings.lines)
    assert all(
        "\x1b" not in line and "2026-01-01T" not in line for line in findings.lines
    )


def test_ids_are_stable_across_runs_and_volatile_text() -> None:
    a = sig.parse_log(
        "FAILED tests/a.py::test_x - TimeoutError: waited 3.2s for 0xdeadbeef"
    )
    b = sig.parse_log(
        "FAILED tests/a.py::test_x - TimeoutError: waited 9.9s for 0xcafe"
    )
    assert a.signatures[0].id == b.signatures[0].id
    assert len(a.signatures[0].id) == 12
    assert sig.normalize_error("got 503 after 3.21s at /srv/app/x.py") == (
        "got <n> after <n> at <path>"
    )


def test_go_test_with_package_and_gitlab_sections() -> None:
    findings = sig.parse_log((FIXTURES / "gitlab/trace_3011.txt").read_text())
    assert _tests(findings) == [
        ("go", "example.com/svc/internal/queue.TestQueueDrain", "fail"),
        ("go", "example.com/svc/internal/queue.TestRetryBackoff/jitter", "fail"),
    ]
    assert "ERROR: Job failed: exit code 1" in findings.lines


def test_vitest_and_jest() -> None:
    vitest = sig.parse_log((FIXTURES / "github/log_1033.txt").read_text())
    assert [s.test for s in vitest.signatures] == [
        "src/components/flow-editor.test.ts > flow editor > saves a schedule change",
        "src/components/run-list.test.ts > run list > paginates 50 rows",
    ]
    jest = sig.parse_log((FIXTURES / "jest_sample.txt").read_text())
    assert _tests(jest) == [
        ("jest", "src/cart/cart.test.js > cart › applies a coupon", "Error"),
        ("jest", "src/cart/cart.test.js > cart › removes the last item", "TypeError"),
    ]


def test_junit_xml_and_hostile_documents() -> None:
    findings = sig.parse_junit((FIXTURES / "junit_sample.xml").read_text())
    assert _tests(findings) == [
        ("junit", "checkout.cart::applies a coupon", "AssertionError"),
        ("junit", "checkout.payment::declines an expired card", "TypeError"),
    ]
    assert (
        findings.lines[0] == "checkout.cart::applies a coupon: expected 90 to equal 81"
    )
    bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'
    assert sig.parse_junit(bomb).signatures == []
    assert sig.parse_junit("<not xml").signatures == []


def test_caps_and_job_fallback() -> None:
    text = "\n".join(f"FAILED tests/t.py::test_{i} - ValueError" for i in range(40))
    findings = sig.parse_log(text)
    assert len(findings.signatures) == sig.MAX_SIGNATURES_PER_LOG
    assert findings.truncated == 40 - sig.MAX_SIGNATURES_PER_LOG
    assert len(findings.lines) == sig.MAX_FAILURE_LINES
    fallback = sig.job_signature("CI", "deploy-preview", "Upload")
    assert (fallback.framework, fallback.test) == (
        "job",
        "CI / deploy-preview / Upload",
    )


def test_junit_report_printed_into_a_log() -> None:
    xml = (FIXTURES / "junit_sample.xml").read_text()
    log = "\n".join(
        f"2026-01-01T10:00:00.0000000Z {line}"
        for line in ["$ cat report.xml", *xml.splitlines()]
    )
    findings = sig.parse_log(log)
    assert ("junit", "checkout.cart::applies a coupon", "AssertionError") in _tests(
        findings
    )
