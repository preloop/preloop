"""Tests for Bitbucket Cloud event normalization, subjects and filter fields."""

import pytest

from preloop.sync.event_normalizer import (
    BITBUCKET_EVENT_MAP,
    EVENT_TYPE_LABELS,
    extract_filter_fields,
    extract_trigger_subject,
    normalize_event_type,
)
from preloop.utils.bitbucket import BITBUCKET_WEBHOOK_EVENTS

PR_PAYLOAD = {
    "actor": {"nickname": "reviewer-1"},
    "repository": {"full_name": "ws/repo"},
    "pullrequest": {
        "id": 7,
        "title": "Add parser",
        "state": "OPEN",
        "draft": False,
        "author": {"nickname": "dev"},
        "reviewers": [{"nickname": "rev"}, {"display_name": "Second Reviewer"}],
        "links": {"html": {"href": "https://bitbucket.org/ws/repo/pull-requests/7"}},
        "source": {"branch": {"name": "feature"}, "commit": {"hash": "abcdef123456"}},
        "destination": {"branch": {"name": "main"}},
    },
}

PUSH_PAYLOAD = {
    "actor": {"display_name": "Dev"},
    "repository": {"full_name": "ws/repo"},
    "push": {"changes": [{"new": {"name": "main", "target": {"hash": "fedcba987"}}}]},
}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("pullrequest:created", "pull_request_opened"),
        ("pullrequest:updated", "pull_request_updated"),
        ("pullrequest:fulfilled", "pull_request_merged"),
        ("pullrequest:rejected", "pull_request_closed"),
        ("pullrequest:approved", "pull_request_approved"),
        ("pullrequest:changes_request_created", "pull_request_changes_requested"),
        ("pullrequest:comment_created", "comment_created"),
        ("repo:push", "push"),
        ("repo:fork", "repo:fork"),
    ],
)
def test_normalize_event_type(raw: str, expected: str) -> None:
    assert normalize_event_type("bitbucket", raw, {}) == expected


def test_every_subscribed_event_has_a_mapping_and_label() -> None:
    for raw in BITBUCKET_WEBHOOK_EVENTS:
        normalized = BITBUCKET_EVENT_MAP[raw]
        assert normalized in EVENT_TYPE_LABELS, normalized


def test_pr_subject() -> None:
    subject = extract_trigger_subject(
        {"source": "bitbucket", "type": "pull_request_opened", "payload": PR_PAYLOAD}
    )
    assert subject["repo"] == "ws/repo"
    assert subject["reference"] == "#7"
    assert subject["title"] == "Add parser"
    assert subject["url"].endswith("/pull-requests/7")
    assert subject["event"] == "Pull Request Opened"
    assert "abcdef1" in subject["commit"]


def test_push_subject() -> None:
    subject = extract_trigger_subject(
        {"source": "bitbucket", "type": "push", "payload": PUSH_PAYLOAD}
    )
    assert subject["reference"] == "main"
    assert subject["commit"].startswith("fedcba9")


def test_pr_filter_fields() -> None:
    fields = extract_filter_fields("bitbucket", "pullrequest:created", PR_PAYLOAD)
    assert fields["sender"] == "reviewer-1"
    assert fields["action"] == "created"
    assert fields["author"] == "dev"
    assert fields["reviewer"] == ["rev", "Second Reviewer"]
    assert fields["state"] == "open"
    assert fields["merged"] is False
    assert fields["draft"] is False
    assert fields["source_branch"] == "feature"
    assert fields["target_branch"] == "main"


def test_push_filter_fields() -> None:
    fields = extract_filter_fields("bitbucket", "repo:push", PUSH_PAYLOAD)
    assert fields["sender"] == "Dev"
    assert fields["ref"] == "main"
    assert "author" not in fields
