"""Failed execution disclosures preserve recoverable work and human PR prose."""

import json

import pytest

from preloop.utils.pr_metadata import (
    failure_notice,
    merge_failure_notice,
    result_failure_reason,
    select_metadata,
)

EXECUTION_LINK = "https://app.example.com/console/flows/executions/08095fd6-f861-4939-997d-2600d1ec5a80"


@pytest.mark.parametrize("status", ["failure", " ERROR ", "partial", "timeout"])
def test_failure_refs_issue_and_retains_reason(status: str) -> None:
    raw = json.dumps({"status": status, "reason": "A test failed"}).encode()
    assert result_failure_reason(raw) == "A test failed"
    assert "Refs #42" in select_metadata(raw, issue_number="42")[1]


@pytest.mark.parametrize("status", ["success", "succeeded", "fail", "pass"])
def test_completed_reports_have_no_failure_notice(status: str) -> None:
    raw = json.dumps({"status": status, "reason": "Finding recorded"}).encode()
    assert result_failure_reason(raw) == ""
    assert "Closes #42" in select_metadata(raw, issue_number="42")[1]


def test_missing_legacy_report_and_audit_error_are_distinct() -> None:
    assert result_failure_reason(None) == ""
    assert (
        result_failure_reason(b'{"verdict":"error","reason":"Missing input"}')
        == "Missing input"
    )
    assert "completion could not be confirmed" in result_failure_reason(b"invalid")


def test_existing_body_preserved_and_notice_idempotent() -> None:
    notice = failure_notice("Tests still fail", EXECUTION_LINK)
    original = "Human introduction\n\n- [x] Manual check\n"
    first = merge_failure_notice(original, notice)
    assert first.startswith(original)
    assert first == merge_failure_notice(first, notice)
    updated = merge_failure_notice(first, failure_notice("New reason", EXECUTION_LINK))
    assert updated.startswith(original)
    assert "New reason" in updated and "Tests still fail" not in updated
    assert updated.count("### Execution incomplete") == 1


def test_failure_reason_cannot_inject_owned_region_or_html() -> None:
    notice = failure_notice(
        "<script>bad</script>\n[hidden](https://example.com)", EXECUTION_LINK
    )
    assert "<script>" not in notice
    assert "> &lt;script&gt;" in notice
    assert "> \\[hidden\\]" in notice


@pytest.mark.parametrize(
    "link", ["https://evil.example/x", EXECUTION_LINK + "?token=private"]
)
def test_failure_link_requires_execution_identity(link: str) -> None:
    with pytest.raises(ValueError):
        failure_notice("Failed", link)


def test_failed_report_does_not_rewrite_existing_completion_prose() -> None:
    raw = b'{"status":"failure","pr_body":"Original work. Closes #42"}'
    assert select_metadata(raw, issue_number="42")[1] == "Original work. Closes #42"
