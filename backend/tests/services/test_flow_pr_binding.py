"""Tests for flow PR binding used by issue-implementation resume."""

from unittest.mock import MagicMock
from uuid import uuid4

from preloop.services.flow_pr_binding import (
    bind_resume_or_skip,
    extract_pr_url_from_comment_event,
    find_bound_execution,
    flow_requires_pr_comment_resume,
    merge_result_preserving_pr_binding,
    normalize_pr_url,
    record_opened_pr,
)


class TestNormalizePrUrl:
    def test_github_pull_url(self):
        assert (
            normalize_pr_url("https://github.com/preloop/preloop/pull/353")
            == "https://github.com/preloop/preloop/pull/353"
        )

    def test_github_issues_url_becomes_pull(self):
        assert (
            normalize_pr_url("https://github.com/preloop/preloop/issues/353/")
            == "https://github.com/preloop/preloop/pull/353"
        )

    def test_empty(self):
        assert normalize_pr_url("") == ""
        assert normalize_pr_url(None) == ""

    def test_non_github_host_does_not_rewrite_issues_path(self):
        assert (
            normalize_pr_url("https://notgithub.com/org/repo/issues/12")
            == "https://notgithub.com/org/repo/issues/12"
        )
        assert (
            normalize_pr_url("https://github.com.evil.example/org/repo/issues/12")
            == "https://github.com.evil.example/org/repo/issues/12"
        )

    def test_www_github_issues_url_becomes_pull(self):
        assert (
            normalize_pr_url("https://www.github.com/preloop/preloop/issues/353")
            == "https://www.github.com/preloop/preloop/pull/353"
        )

    def test_tracker_api_url_is_rejected(self):
        assert (
            normalize_pr_url("https://gitlab.com/api/v4/projects/1/merge_requests/10")
            == ""
        )
        assert normalize_pr_url("https://api.github.com/repos/a/b/pulls/1") == ""


class TestExtractPrUrlFromCommentEvent:
    def test_github_pr_comment(self):
        event = {
            "payload": {
                "issue": {
                    "number": 353,
                    "html_url": "https://github.com/preloop/preloop/issues/353",
                    "pull_request": {
                        "html_url": "https://github.com/preloop/preloop/pull/353",
                    },
                }
            }
        }
        assert (
            extract_pr_url_from_comment_event(event)
            == "https://github.com/preloop/preloop/pull/353"
        )

    def test_github_issue_comment_is_none(self):
        event = {
            "payload": {
                "issue": {
                    "number": 12,
                    "html_url": "https://github.com/preloop/preloop/issues/12",
                }
            }
        }
        assert extract_pr_url_from_comment_event(event) is None

    def test_gitlab_mr_note(self):
        event = {
            "payload": {
                "merge_request": {
                    "iid": 10,
                    "url": "https://gitlab.com/acme/backend/-/merge_requests/10",
                    "source_branch": "feat/x",
                }
            }
        }
        assert (
            extract_pr_url_from_comment_event(event)
            == "https://gitlab.com/acme/backend/-/merge_requests/10"
        )

    def test_gitlab_prefers_web_url_over_api_url(self):
        event = {
            "payload": {
                "merge_request": {
                    "iid": 10,
                    "url": "https://gitlab.com/api/v4/projects/1/merge_requests/10",
                    "web_url": "https://gitlab.com/acme/backend/-/merge_requests/10",
                }
            }
        }
        assert (
            extract_pr_url_from_comment_event(event)
            == "https://gitlab.com/acme/backend/-/merge_requests/10"
        )


class TestMergeResultPreservingPrBinding:
    def test_none_incoming_keeps_existing(self):
        existing = {"pr_url": "https://github.com/a/b/pull/1"}
        assert merge_result_preserving_pr_binding(existing, None) == existing

    def test_incoming_dict_keeps_pr_url(self):
        existing = {
            "pr_url": "https://github.com/a/b/pull/1",
            "pr_source_branch": "feat/x",
        }
        incoming = {"verdict": "ship"}
        merged = merge_result_preserving_pr_binding(existing, incoming)
        assert merged["verdict"] == "ship"
        assert merged["pr_url"] == "https://github.com/a/b/pull/1"
        assert merged["pr_source_branch"] == "feat/x"


class TestFlowRequiresPrCommentResume:
    def test_issue_impl_flow(self):
        flow = MagicMock()
        flow.trigger_event_types = ["issue_labeled", "comment_created"]
        assert flow_requires_pr_comment_resume(flow) is True

    def test_comment_only_flow(self):
        flow = MagicMock()
        flow.trigger_event_types = ["comment_created"]
        assert flow_requires_pr_comment_resume(flow) is False

    def test_mock_types_are_ignored(self):
        flow = MagicMock()
        assert flow_requires_pr_comment_resume(flow) is False


class TestFindAndBind:
    def test_find_bound_execution_jsonb_hit(self):
        execution = MagicMock()
        db = MagicMock()
        from preloop.services import flow_pr_binding as mod

        original_jsonb = mod.crud_flow_execution.get_by_result_pr_url
        original_flow = mod.crud_flow_execution.get_by_flow
        mod.crud_flow_execution.get_by_result_pr_url = MagicMock(return_value=execution)
        mod.crud_flow_execution.get_by_flow = MagicMock()
        try:
            found = find_bound_execution(
                db,
                flow_id="flow-1",
                pr_url="https://github.com/preloop/preloop/pull/353",
            )
            assert found is execution
            mod.crud_flow_execution.get_by_flow.assert_not_called()
        finally:
            mod.crud_flow_execution.get_by_result_pr_url = original_jsonb
            mod.crud_flow_execution.get_by_flow = original_flow

    def test_find_bound_execution_matches_normalized_url(self):
        execution = MagicMock()
        execution.result = {
            "pr_url": "https://github.com/preloop/preloop/pull/353/",
            "pr_source_branch": "feat/x",
        }
        db = MagicMock()
        from preloop.services import flow_pr_binding as mod

        original_jsonb = mod.crud_flow_execution.get_by_result_pr_url
        original_flow = mod.crud_flow_execution.get_by_flow
        mod.crud_flow_execution.get_by_result_pr_url = MagicMock(return_value=None)
        mod.crud_flow_execution.get_by_flow = MagicMock(return_value=[execution])
        try:
            found = find_bound_execution(
                db,
                flow_id="flow-1",
                pr_url="https://github.com/preloop/preloop/issues/353",
            )
            assert found is execution
        finally:
            mod.crud_flow_execution.get_by_result_pr_url = original_jsonb
            mod.crud_flow_execution.get_by_flow = original_flow

    def test_find_bound_execution_logs_when_missing(self, caplog):
        db = MagicMock()
        from preloop.services import flow_pr_binding as mod

        original_jsonb = mod.crud_flow_execution.get_by_result_pr_url
        original_flow = mod.crud_flow_execution.get_by_flow
        mod.crud_flow_execution.get_by_result_pr_url = MagicMock(return_value=None)
        mod.crud_flow_execution.get_by_flow = MagicMock(return_value=[])
        try:
            with caplog.at_level("INFO"):
                found = find_bound_execution(
                    db,
                    flow_id="flow-1",
                    pr_url="https://github.com/preloop/preloop/pull/999",
                )
            assert found is None
            assert "lookback=" in caplog.text
        finally:
            mod.crud_flow_execution.get_by_result_pr_url = original_jsonb
            mod.crud_flow_execution.get_by_flow = original_flow

    def test_bind_resume_or_skip_attaches_resume(self):
        execution = MagicMock()
        execution.id = uuid4()
        execution.result = {
            "pr_url": "https://github.com/preloop/preloop/pull/353",
            "pr_source_branch": "feat/x",
        }
        flow = MagicMock()
        flow.id = uuid4()
        db = MagicMock()
        event = {
            "payload": {
                "issue": {
                    "pull_request": {
                        "html_url": "https://github.com/preloop/preloop/pull/353",
                    }
                }
            }
        }
        from preloop.services import flow_pr_binding as mod

        original = mod.find_bound_execution
        mod.find_bound_execution = MagicMock(return_value=execution)
        try:
            resume = bind_resume_or_skip(db, flow, event)
            assert resume["source_branch"] == "feat/x"
            assert event["_resume"]["execution_id"] == str(execution.id)
        finally:
            mod.find_bound_execution = original

    def test_bind_skips_issue_comment(self):
        flow = MagicMock()
        event = {"payload": {"issue": {"number": 1}}}
        assert bind_resume_or_skip(MagicMock(), flow, event) is None

    def test_record_opened_pr_merges_result(self):
        execution = MagicMock()
        execution.result = {"other": 1}
        db = MagicMock()
        from preloop.services import flow_pr_binding as mod

        original = mod.crud_flow_execution.get
        mod.crud_flow_execution.get = MagicMock(return_value=execution)
        try:
            record_opened_pr(db, "exec-1", "https://github.com/a/b/pull/1", "feat/x")
            assert execution.result["pr_url"] == "https://github.com/a/b/pull/1"
            assert execution.result["pr_source_branch"] == "feat/x"
            assert execution.result["other"] == 1
            db.commit.assert_called_once()
        finally:
            mod.crud_flow_execution.get = original
