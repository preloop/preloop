"""
Tests for event normalization and filter field extraction.

Tests use real webhook payload structures from GitLab, GitHub, and Jira.
"""

import pytest
from preloop.models.models.flow_execution import TRIGGER_SUBJECT_KEY
from preloop.sync.event_normalizer import (
    SUBJECT_KEY,
    attach_trigger_subject,
    extract_trigger_subject,
    gitlab_label_delta,
    humanize_event_type,
    normalize_event_type,
    extract_filter_fields,
)
from preloop.sync.webhook_payloads import (
    GITLAB_ISSUE_OPENED,
    GITLAB_ISSUE_CLOSED,
    GITHUB_ISSUE_OPENED,
    GITHUB_ISSUE_CLOSED,
    JIRA_ISSUE_CREATED,
)

# Standardized event envelopes (see preloop.sync.tasks) for PR/MR triggers.
# Declared here rather than in webhook_payloads so the shared fixture module
# stays focused on the raw webhook bodies it already provides.
GITHUB_PULL_REQUEST_UPDATED = {
    "source": "github",
    "type": "pull_request_updated",
    "tracker_id": "11111111-1111-1111-1111-111111111111",
    "payload": {
        "action": "synchronize",
        "repository": {"full_name": "preloop/preloop"},
        "pull_request": {
            "number": 78,
            "title": "Add subject to executions",
            "html_url": "https://github.com/preloop/preloop/pull/78",
            "head": {"sha": "5167595cb0a94f2e1d3c8a7b6e5f4d3c2b1a0987"},
        },
    },
}

GITLAB_MERGE_REQUEST_UPDATED = {
    "source": "gitlab",
    "type": "merge_request_updated",
    "tracker_id": "22222222-2222-2222-2222-222222222222",
    "payload": {
        "object_kind": "merge_request",
        "project": {"path_with_namespace": "acme/backend"},
        "object_attributes": {
            "iid": 45,
            "title": "Tighten retries",
            "url": "https://gitlab.com/acme/backend/-/merge_requests/45",
            "last_commit": {"id": "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"},
        },
    },
}


class TestEventNormalization:
    """Test event type normalization for different trackers."""

    def test_gitlab_issue_opened_normalization(self):
        """Test GitLab 'Issue Hook' with action='open' normalizes to 'issue_opened'."""
        normalized = normalize_event_type("gitlab", "Issue Hook", GITLAB_ISSUE_OPENED)
        assert normalized == "issue_opened"

    def test_gitlab_issue_closed_normalization(self):
        """Test GitLab 'Issue Hook' with action='close' normalizes to 'issue_closed'."""
        normalized = normalize_event_type("gitlab", "Issue Hook", GITLAB_ISSUE_CLOSED)
        assert normalized == "issue_closed"

    def test_gitlab_merge_request_normalization(self):
        """Test GitLab 'Merge Request Hook' normalizes to 'merge_request_opened'."""
        normalized = normalize_event_type("gitlab", "Merge Request Hook", {})
        assert normalized == "merge_request_opened"

    def test_github_issue_opened_normalization(self):
        """Test GitHub 'issues' with action='opened' normalizes to 'issue_opened'."""
        normalized = normalize_event_type("github", "issues", GITHUB_ISSUE_OPENED)
        assert normalized == "issue_opened"

    def test_github_issue_closed_normalization(self):
        """Test GitHub 'issues' with action='closed' normalizes to 'issue_closed'."""
        normalized = normalize_event_type("github", "issues", GITHUB_ISSUE_CLOSED)
        assert normalized == "issue_closed"

    def test_github_pull_request_normalization(self):
        """Test GitHub 'pull_request' normalizes to 'pull_request_opened'."""
        normalized = normalize_event_type(
            "github", "pull_request", {"action": "opened"}
        )
        assert normalized == "pull_request_opened"

    def test_jira_issue_created_normalization(self):
        """Test Jira 'jira:issue_created' normalizes to 'issue_opened'."""
        normalized = normalize_event_type(
            "jira", "jira:issue_created", JIRA_ISSUE_CREATED
        )
        assert normalized == "issue_opened"

    def test_unknown_tracker_type(self):
        """Test unknown tracker type returns original event type."""
        normalized = normalize_event_type("unknown", "some_event", {})
        assert normalized == "some_event"

    def test_gitlab_label_add_normalizes_to_issue_labeled(self):
        """GitLab has no labeled event; a label add on update is issue_labeled."""
        payload = {
            "object_attributes": {"action": "update", "iid": 87},
            "changes": {
                "labels": {
                    "previous": [{"title": "glitchtip"}],
                    "current": [
                        {"title": "glitchtip"},
                        {"title": "agent-ready"},
                    ],
                }
            },
        }
        assert normalize_event_type("gitlab", "Issue Hook", payload) == "issue_labeled"

    def test_gitlab_label_remove_normalizes_to_issue_unlabeled(self):
        """A GitLab update that only removes labels is issue_unlabeled."""
        payload = {
            "object_attributes": {"action": "update"},
            "changes": {
                "labels": {
                    "previous": [{"title": "agent-ready"}],
                    "current": [],
                }
            },
        }
        assert (
            normalize_event_type("gitlab", "Issue Hook", payload) == "issue_unlabeled"
        )

    def test_gitlab_title_only_update_stays_issue_updated(self):
        """Title/body edits without a label delta stay issue_updated."""
        payload = {
            "object_attributes": {"action": "update", "title": "new title"},
            "changes": {"title": {"previous": "old", "current": "new title"}},
        }
        assert normalize_event_type("gitlab", "Issue Hook", payload) == "issue_updated"

    def test_gitlab_mixed_add_and_remove_prefers_issue_labeled(self):
        """A GitLab edit that adds and removes labels is issue_labeled.

        Added wins so intake-to-implementation hops still fire. GitHub
        would emit separate labeled and unlabeled events; GitLab cannot.
        Both deltas stay in filter_fields so removed titles are not lost.
        """
        payload = {
            "object_attributes": {"action": "update", "state": "opened"},
            "user": {"username": "root"},
            "labels": [
                {"title": "glitchtip"},
                {"title": "agent-ready"},
            ],
            "changes": {
                "labels": {
                    "previous": [
                        {"title": "glitchtip"},
                        {"title": "needs-triage"},
                    ],
                    "current": [
                        {"title": "glitchtip"},
                        {"title": "agent-ready"},
                    ],
                }
            },
        }
        assert normalize_event_type("gitlab", "Issue Hook", payload) == "issue_labeled"
        fields = extract_filter_fields("gitlab", "Issue Hook", payload)
        assert fields["added_labels"] == ["agent-ready"]
        assert fields["removed_labels"] == ["needs-triage"]
        added, removed = gitlab_label_delta(payload)
        assert added == ["agent-ready"]
        assert removed == ["needs-triage"]

    def test_github_labeled_still_issue_labeled(self):
        """GitHub issues + action labeled stays issue_labeled."""
        payload = {
            "action": "labeled",
            "label": {"name": "agent-ready"},
            "issue": {"number": 269, "labels": [{"name": "agent-ready"}]},
        }
        assert normalize_event_type("github", "issues", payload) == "issue_labeled"


class TestFilterFieldExtraction:
    """Test extraction of filter fields from webhook payloads."""

    def test_gitlab_issue_opened_filters(self):
        """Test GitLab issue opened event extracts correct filter fields."""
        fields = extract_filter_fields("gitlab", "Issue Hook", GITLAB_ISSUE_OPENED)

        assert fields["author"] == "root"
        assert fields["assignee"] == ["user1"]
        assert set(fields["labels"]) == {"API", "Feature"}
        assert fields["state"] == "opened"
        assert fields["action"] == "open"

    def test_gitlab_label_add_sets_added_labels(self):
        """GitLab label delta is exposed as added_labels for trigger_config."""
        payload = {
            "object_attributes": {"action": "update", "state": "opened"},
            "user": {"username": "root"},
            "labels": [
                {"title": "glitchtip"},
                {"title": "agent-ready"},
            ],
            "changes": {
                "labels": {
                    "previous": [{"title": "glitchtip"}],
                    "current": [
                        {"title": "glitchtip"},
                        {"title": "agent-ready"},
                    ],
                }
            },
        }
        fields = extract_filter_fields("gitlab", "Issue Hook", payload)
        assert fields["added_labels"] == ["agent-ready"]
        assert "removed_labels" not in fields
        added, removed = gitlab_label_delta(payload)
        assert added == ["agent-ready"]
        assert removed == []

    def test_github_labeled_sets_added_labels(self):
        """GitHub issues.labeled exposes the new label as added_labels."""
        payload = {
            "action": "labeled",
            "label": {"name": "agent-ready"},
            "issue": {
                "state": "open",
                "user": {"login": "octocat"},
                "labels": [{"name": "glitchtip"}, {"name": "agent-ready"}],
            },
            "sender": {"login": "preloop[bot]"},
        }
        fields = extract_filter_fields("github", "issues", payload)
        assert fields["added_labels"] == ["agent-ready"]
        assert "agent-ready" in fields["labels"]

    def test_gitlab_issue_closed_filters(self):
        """Test GitLab issue closed event extracts correct filter fields."""
        fields = extract_filter_fields("gitlab", "Issue Hook", GITLAB_ISSUE_CLOSED)

        assert fields["author"] == "root"
        assert fields["state"] == "closed"
        assert fields["action"] == "close"

    def test_github_issue_opened_filters(self):
        """Test GitHub issue opened event extracts correct filter fields."""
        fields = extract_filter_fields("github", "issues", GITHUB_ISSUE_OPENED)

        assert fields["author"] == "octocat"
        assert fields["assignee"] == ["octocat"]
        assert fields["labels"] == ["bug"]
        assert fields["state"] == "open"
        assert fields["action"] == "opened"
        assert fields["sender"] == "octocat"

    def test_github_issue_closed_filters(self):
        """Test GitHub issue closed event extracts correct filter fields."""
        fields = extract_filter_fields("github", "issues", GITHUB_ISSUE_CLOSED)

        assert fields["state"] == "closed"
        assert fields["action"] == "closed"

    def test_jira_issue_created_filters(self):
        """Test Jira issue created event extracts correct filter fields."""
        fields = extract_filter_fields("jira", "jira:issue_created", JIRA_ISSUE_CREATED)

        assert (
            "Creator Name" in fields["author"]
            or fields["author"] == "5b10a2844c20165700ede21g"
        )
        assert (
            "Assignee Name" in fields["assignee"]
            or fields["assignee"] == "5b10a2844c20165700ede21g"
        )
        assert set(fields["labels"]) == {"backend", "api"}
        assert fields["priority"] == "Medium"
        assert fields["state"] == "To Do"
        assert fields["issue_type"] == "Task"

    def test_filter_fields_missing_data(self):
        """Test that missing data doesn't cause errors."""
        # Empty GitHub payload
        fields = extract_filter_fields("github", "issues", {"issue": {}})

        # Should have action but other fields may be None/empty
        assert "action" in fields

    def test_filter_fields_single_assignee(self):
        """Test single assignee (not a list) is handled correctly."""
        payload = {
            "object_attributes": {"assignee_id": 51, "state": "opened"},
            "user": {"username": "testuser"},
            "assignee": {"username": "single_assignee"},
        }

        fields = extract_filter_fields("gitlab", "Issue Hook", payload)

        # Should be a single string, not a list
        assert fields["assignee"] == "single_assignee"


class TestFilterMatching:
    """Test that filter matching works as expected with FlowTriggerService logic."""

    def test_label_filter_matching(self):
        """Test that label filters would match correctly."""
        # Extract fields from GitHub issue with "bug" label
        fields = extract_filter_fields("github", "issues", GITHUB_ISSUE_OPENED)

        # Simulate trigger_config: {"labels": ["bug"]}
        trigger_config_labels = ["bug"]
        actual_labels = fields.get("labels", [])

        # Check if any trigger label is in actual labels
        matches = any(label in actual_labels for label in trigger_config_labels)
        assert matches is True

    def test_label_filter_no_match(self):
        """Test that label filters correctly don't match when label is absent."""
        fields = extract_filter_fields("github", "issues", GITHUB_ISSUE_OPENED)

        # Simulate trigger_config: {"labels": ["feature"]}
        trigger_config_labels = ["feature"]
        actual_labels = fields.get("labels", [])

        matches = any(label in actual_labels for label in trigger_config_labels)
        assert matches is False

    def test_author_filter_matching(self):
        """Test that author filters would match correctly."""
        fields = extract_filter_fields("github", "issues", GITHUB_ISSUE_OPENED)

        # Simulate trigger_config: {"author": "octocat"}
        trigger_author = "octocat"
        actual_author = fields.get("author")

        assert actual_author == trigger_author

    def test_assignee_filter_matching_list(self):
        """Test that assignee filters work with list of assignees."""
        fields = extract_filter_fields("github", "issues", GITHUB_ISSUE_OPENED)

        # Simulate trigger_config: {"assignee": "octocat"}
        trigger_assignee = "octocat"
        actual_assignees = fields.get("assignee", [])

        # Check if trigger assignee is in the list
        matches = trigger_assignee in actual_assignees
        assert matches is True


@pytest.mark.parametrize(
    "tracker_type,event_type,payload,expected_normalized,expected_fields",
    [
        # GitLab test cases
        (
            "gitlab",
            "Issue Hook",
            GITLAB_ISSUE_OPENED,
            "issue_opened",
            {"author": "root", "state": "opened", "action": "open"},
        ),
        (
            "gitlab",
            "Merge Request Hook",
            {},
            "merge_request_opened",
            {},
        ),
        # GitHub test cases
        (
            "github",
            "issues",
            GITHUB_ISSUE_OPENED,
            "issue_opened",
            {"author": "octocat", "state": "open", "action": "opened"},
        ),
        (
            "github",
            "pull_request",
            {"action": "opened"},
            "pull_request_opened",
            {"action": "opened"},
        ),
        # Jira test cases
        (
            "jira",
            "jira:issue_created",
            JIRA_ISSUE_CREATED,
            "issue_opened",
            {"priority": "Medium", "state": "To Do", "issue_type": "Task"},
        ),
    ],
)
def test_end_to_end_normalization_and_extraction(
    tracker_type, event_type, payload, expected_normalized, expected_fields
):
    """Test complete normalization and extraction flow for various trackers."""
    # Test normalization
    normalized = normalize_event_type(tracker_type, event_type, payload)
    assert normalized == expected_normalized

    # Test extraction
    fields = extract_filter_fields(tracker_type, event_type, payload)

    # Check expected fields are present and match
    for key, value in expected_fields.items():
        assert key in fields
        assert fields[key] == value


class TestUUIDSerialization:
    """Test UUID serialization for JSON storage."""

    def test_serialize_uuids_in_dict(self):
        """Test that UUIDs in dictionaries are converted to strings."""
        from uuid import UUID
        from preloop.sync.tasks import serialize_uuids

        test_uuid = UUID("9607c913-df61-4a24-9179-b6e83893c501")
        data = {"tracker_id": test_uuid, "name": "test"}

        serialized = serialize_uuids(data)

        assert serialized["tracker_id"] == "9607c913-df61-4a24-9179-b6e83893c501"
        assert serialized["name"] == "test"
        assert isinstance(serialized["tracker_id"], str)

    def test_serialize_uuids_in_nested_dict(self):
        """Test that UUIDs in nested structures are converted to strings."""
        from uuid import UUID
        from preloop.sync.tasks import serialize_uuids

        test_uuid1 = UUID("9607c913-df61-4a24-9179-b6e83893c501")
        test_uuid2 = UUID("f3dd00c0-7316-411d-aea7-2fee793b5c08")

        data = {
            "tracker_id": test_uuid1,
            "organization_id": test_uuid2,
            "nested": {"another_uuid": test_uuid1, "value": 42},
            "list": [test_uuid2, "string", 123],
        }

        serialized = serialize_uuids(data)

        assert serialized["tracker_id"] == "9607c913-df61-4a24-9179-b6e83893c501"
        assert serialized["organization_id"] == "f3dd00c0-7316-411d-aea7-2fee793b5c08"
        assert (
            serialized["nested"]["another_uuid"]
            == "9607c913-df61-4a24-9179-b6e83893c501"
        )
        assert serialized["nested"]["value"] == 42
        assert serialized["list"][0] == "f3dd00c0-7316-411d-aea7-2fee793b5c08"
        assert serialized["list"][1] == "string"
        assert serialized["list"][2] == 123

    def test_serialize_uuids_preserves_non_uuid_data(self):
        """Test that non-UUID data is preserved unchanged."""
        from preloop.sync.tasks import serialize_uuids

        data = {
            "string": "test",
            "number": 42,
            "float": 3.14,
            "bool": True,
            "none": None,
            "list": [1, 2, 3],
            "dict": {"key": "value"},
        }

        serialized = serialize_uuids(data)

        assert serialized == data


class TestHumanizeEventType:
    """Test conversion of normalized event types to display labels."""

    def test_known_event_type_uses_curated_label(self):
        """Known event types map to the same labels the flow editor shows."""
        assert humanize_event_type("pull_request_updated") == "Pull Request Updated"
        assert humanize_event_type("merge_request_merged") == "Merge Request Merged"
        assert humanize_event_type("push") == "Push to Repository"

    def test_unknown_event_type_falls_back_to_title_case(self):
        """Unknown/future event types still render readably, not as slugs."""
        assert humanize_event_type("pull_request_auto_merged") == (
            "Pull Request Auto Merged"
        )

    def test_missing_event_type_returns_empty_string(self):
        """No event type yields an empty label rather than 'None'."""
        assert humanize_event_type(None) == ""
        assert humanize_event_type("") == ""


class TestExtractTriggerSubject:
    """Test the compact subject derived for flow execution list rows."""

    def test_github_pull_request_subject(self):
        """A GitHub PR event yields repo, number, event label and short SHA."""
        subject = extract_trigger_subject(GITHUB_PULL_REQUEST_UPDATED)

        assert subject["text"] == (
            "preloop/preloop #78 · Pull Request Updated · 5167595c"
        )
        assert subject["repo"] == "preloop/preloop"
        assert subject["reference"] == "#78"
        assert subject["commit"] == "5167595c"
        assert subject["url"] == "https://github.com/preloop/preloop/pull/78"
        assert subject["title"] == "Add subject to executions"

    def test_gitlab_merge_request_subject(self):
        """A GitLab MR event uses GitLab's !N convention and last_commit."""
        subject = extract_trigger_subject(GITLAB_MERGE_REQUEST_UPDATED)

        assert subject["text"] == "acme/backend !45 · Merge Request Updated · a1b2c3d4"
        assert subject["repo"] == "acme/backend"
        assert subject["reference"] == "!45"
        assert subject["commit"] == "a1b2c3d4"
        assert subject["url"] == "https://gitlab.com/acme/backend/-/merge_requests/45"

    def test_github_issue_subject_has_no_commit(self):
        """Issue events identify by repo and number; there is no commit."""
        subject = extract_trigger_subject(
            {
                "source": "github",
                "type": "issue_opened",
                "payload": GITHUB_ISSUE_OPENED,
            }
        )

        assert subject["text"] == "octocat/Hello-World #1 · Issue Opened"
        assert "commit" not in subject

    def test_github_push_subject_uses_branch_and_head_commit(self):
        """Push events identify by branch plus the head commit SHA."""
        subject = extract_trigger_subject(
            {
                "source": "github",
                "type": "push",
                "payload": {
                    "ref": "refs/heads/main",
                    "repository": {"full_name": "preloop/preloop"},
                    "head_commit": {"id": "deadbeefcafe1234"},
                    "compare": "https://github.com/preloop/preloop/compare/a...b",
                },
            }
        )

        assert subject["text"] == (
            "preloop/preloop main · Push to Repository · deadbeef"
        )
        assert subject["url"] == "https://github.com/preloop/preloop/compare/a...b"

    def test_gitlab_push_falls_back_to_checkout_sha(self):
        """GitLab push events carry checkout_sha rather than a head commit."""
        subject = extract_trigger_subject(
            {
                "source": "gitlab",
                "type": "push",
                "payload": {
                    "ref": "refs/heads/release",
                    "project": {"path_with_namespace": "acme/backend"},
                    "checkout_sha": "0123456789abcdef",
                },
            }
        )

        assert subject["text"] == "acme/backend release · Push to Repository · 01234567"

    def test_jira_issue_subject_uses_issue_key(self):
        """Jira has no repo or commit, so the issue key carries the identity."""
        subject = extract_trigger_subject(
            {
                "source": "jira",
                "type": "issue_updated",
                "payload": JIRA_ISSUE_CREATED,
            }
        )

        assert subject["reference"]
        assert "Issue Updated" in subject["text"]
        assert "commit" not in subject

    def test_test_mode_overrides_event_label(self):
        """Manually triggered runs are labelled as such."""
        subject = extract_trigger_subject(
            {"source": "manual", "type": None, "test_mode": True}
        )

        assert subject["text"] == "Manual Test Run"

    def test_unknown_source_still_yields_event_label(self):
        """An unrecognised trigger source degrades to the event label alone."""
        subject = extract_trigger_subject(
            {"source": "webhook", "type": "webhook", "payload": {}}
        )

        assert subject["text"] == "Webhook"

    def test_returns_none_when_nothing_identifying(self):
        """With no source, type or payload there is no subject to store."""
        assert extract_trigger_subject({}) is None
        assert extract_trigger_subject({"source": "github", "payload": {}}) is None

    def test_non_dict_input_is_tolerated(self):
        """Malformed trigger data must not raise during execution creation."""
        assert extract_trigger_subject(None) is None
        assert extract_trigger_subject("not a dict") is None

    def test_malformed_payload_does_not_raise(self):
        """Unexpected payload shapes degrade instead of blowing up."""
        subject = extract_trigger_subject(
            {
                "source": "github",
                "type": "pull_request_updated",
                "payload": {"pull_request": "unexpected", "repository": None},
            }
        )

        assert subject["text"] == "Pull Request Updated"


class TestAttachTriggerSubject:
    """Test persistence of the subject onto the trigger details snapshot."""

    def test_attaches_subject_under_reserved_key(self):
        """The subject is stored under the key the CRUD layer projects."""
        details = dict(GITHUB_PULL_REQUEST_UPDATED)

        attach_trigger_subject(details)

        assert details[TRIGGER_SUBJECT_KEY]["text"] == (
            "preloop/preloop #78 · Pull Request Updated · 5167595c"
        )

    def test_returns_same_object(self):
        """Callers may use the return value or the mutated dict."""
        details = dict(GITHUB_PULL_REQUEST_UPDATED)

        assert attach_trigger_subject(details) is details

    def test_no_subject_leaves_snapshot_untouched(self):
        """Nothing identifying means no empty blob is written."""
        details = {"source": "github", "payload": {}}

        attach_trigger_subject(details)

        assert TRIGGER_SUBJECT_KEY not in details

    def test_non_dict_input_is_returned_unchanged(self):
        """Guard against malformed snapshots reaching execution creation."""
        assert attach_trigger_subject(None) is None

    def test_subject_key_matches_model_constant(self):
        """The writer and the CRUD reader must agree on the storage key."""
        assert SUBJECT_KEY == TRIGGER_SUBJECT_KEY
