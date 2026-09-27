"""Tests for flow PR binding used by issue-implementation resume."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from preloop.models.crud import crud_flow_execution
from preloop.services.flow_pr_binding import (
    bind_resume_or_skip,
    extract_pr_url_from_comment_event,
    find_bound_execution,
    flow_requires_pr_comment_resume,
    merge_result_preserving_pr_binding,
    normalize_pr_url,
    record_cli_session,
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
            with (
                caplog.at_level("INFO"),
                patch.object(
                    mod.logger, "handlers", [*mod.logger.handlers, caplog.handler]
                ),
            ):
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

    def test_record_opened_pr_uses_atomic_crud_binding(self):
        from unittest.mock import patch
        from preloop.services import flow_pr_binding as mod

        execution, db = MagicMock(), MagicMock()
        with (
            patch.object(
                mod.crud_flow_execution, "bind_publication", return_value=execution
            ) as bind,
            patch("preloop.services.flow_feedback.register_thread") as register,
        ):
            record_opened_pr(db, "exec-1", "https://github.com/a/b/pull/1/", "feat/x")
        bind.assert_called_once_with(
            db,
            execution_id="exec-1",
            pr_url="https://github.com/a/b/pull/1",
            source_branch="feat/x",
        )
        register.assert_called_once_with(
            db, execution, "https://github.com/a/b/pull/1", "feat/x"
        )

    def test_noncanonical_stored_url_does_not_conflict(self):
        """Legacy trailing-slash rows stay in the same publication class."""
        execution = SimpleNamespace(
            result={"pr_url": "https://github.com/acme/app/issues/7/"}
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.populate_existing.return_value.with_for_update.return_value.one_or_none.return_value = execution

        bound = crud_flow_execution.bind_publication(
            db,
            execution_id=uuid4(),
            pr_url="https://github.com/acme/app/pull/7",
            source_branch="feat/x",
        )

        assert bound is execution
        assert execution.result["pr_url"] == "https://github.com/acme/app/pull/7"
        assert execution.result["pr_source_branch"] == "feat/x"
        db.flush.assert_called_once()
        db.commit.assert_called_once()

    def test_different_normalized_url_still_conflicts(self):
        execution = SimpleNamespace(
            result={"pr_url": "https://github.com/acme/app/pull/7"}
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.populate_existing.return_value.with_for_update.return_value.one_or_none.return_value = execution

        with pytest.raises(ValueError, match="binding changed"):
            crud_flow_execution.bind_publication(
                db,
                execution_id=uuid4(),
                pr_url="https://github.com/acme/app/pull/8",
            )


class TestRecordCliSession:
    def test_records_session_on_the_execution(self):
        execution = MagicMock()
        db = MagicMock()
        from preloop.services import flow_pr_binding as mod

        original_get = mod.crud_flow_execution.get
        original_set = mod.crud_flow_execution.set_cli_session
        mod.crud_flow_execution.get = MagicMock(return_value=execution)
        mod.crud_flow_execution.set_cli_session = MagicMock()
        try:
            record_cli_session(
                db,
                "exec-1",
                {"agent_type": "opencode", "session_id": "ses_ab12cd34"},
            )
            mod.crud_flow_execution.set_cli_session.assert_called_once_with(
                db,
                db_obj=execution,
                cli_session={
                    "agent_type": "opencode",
                    "session_id": "ses_ab12cd34",
                },
            )
            db.commit.assert_called_once()
        finally:
            mod.crud_flow_execution.get = original_get
            mod.crud_flow_execution.set_cli_session = original_set

    def test_does_not_log_session_id(self, caplog):
        execution = MagicMock()
        db = MagicMock()
        from preloop.services import flow_pr_binding as mod

        original_get = mod.crud_flow_execution.get
        original_set = mod.crud_flow_execution.set_cli_session
        mod.crud_flow_execution.get = MagicMock(return_value=execution)
        mod.crud_flow_execution.set_cli_session = MagicMock()
        try:
            with caplog.at_level("INFO", logger="preloop.services.flow_pr_binding"):
                record_cli_session(
                    db,
                    "exec-1",
                    {"agent_type": "opencode", "session_id": "ses_ab12cd34"},
                )
            joined = "\n".join(caplog.messages)
            assert "ses_ab12cd34" not in joined
            assert "opencode" not in joined
        finally:
            mod.crud_flow_execution.get = original_get
            mod.crud_flow_execution.set_cli_session = original_set

    def test_ignores_missing_session_id(self):
        db = MagicMock()
        from preloop.services import flow_pr_binding as mod

        original_get = mod.crud_flow_execution.get
        mod.crud_flow_execution.get = MagicMock()
        try:
            record_cli_session(db, "exec-1", {"agent_type": "opencode"})
            mod.crud_flow_execution.get.assert_not_called()
            db.commit.assert_not_called()
        finally:
            mod.crud_flow_execution.get = original_get

    def test_ignores_non_dict_payload(self):
        db = MagicMock()
        from preloop.services import flow_pr_binding as mod

        original_get = mod.crud_flow_execution.get
        mod.crud_flow_execution.get = MagicMock()
        try:
            record_cli_session(db, "exec-1", "opencode ses_x")
            mod.crud_flow_execution.get.assert_not_called()
        finally:
            mod.crud_flow_execution.get = original_get


class TestBindResumeCarriesCliSession:
    def _bind(self, execution):
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
            return bind_resume_or_skip(db, flow, event)
        finally:
            mod.find_bound_execution = original

    def test_resume_includes_prior_cli_session(self):
        execution = MagicMock()
        execution.id = uuid4()
        execution.result = {"pr_url": "https://github.com/preloop/preloop/pull/353"}
        execution.cli_session = {
            "agent_type": "opencode",
            "session_id": "ses_ab12cd34",
        }
        resume = self._bind(execution)
        assert resume["cli_session"] == {
            "agent_type": "opencode",
            "session_id": "ses_ab12cd34",
        }

    def test_resume_without_cli_session_omits_the_key(self):
        execution = MagicMock()
        execution.id = uuid4()
        execution.result = {"pr_url": "https://github.com/preloop/preloop/pull/353"}
        execution.cli_session = None
        resume = self._bind(execution)
        assert "cli_session" not in resume


class TestBitbucketCommentBinding:
    BB_REPO = {
        "full_name": "ws/repo",
        "uuid": "{22222222-2222-2222-2222-222222222222}",
    }

    def test_extract_pr_url_from_bitbucket_comment_event(self):
        event = {
            "payload": {
                "pullrequest": {
                    "id": 7,
                    "links": {
                        "html": {
                            "href": "https://bitbucket.org/ws/repo/pull-requests/7"
                        }
                    },
                }
            }
        }
        assert (
            extract_pr_url_from_comment_event(event)
            == "https://bitbucket.org/ws/repo/pull-requests/7"
        )

    def test_bitbucket_api_url_is_rejected(self):
        assert (
            normalize_pr_url(
                "https://api.bitbucket.org/2.0/repositories/ws/repo/pullrequests/7"
            )
            == ""
        )

    def _event_and_flow(self, *, repository=None):
        from preloop.services.flow_pr_binding import is_bound_implementation_comment

        account_id, tracker_id, flow_id = uuid4(), uuid4(), uuid4()
        flow = SimpleNamespace(
            id=flow_id,
            account_id=account_id,
            trigger_event_source=str(tracker_id),
            trigger_event_types=["issue_labeled", "comment_created"],
            trigger_config={"issue_label": "agent-ready"},
        )
        pr_url = "https://bitbucket.org/ws/repo/pull-requests/7"
        event = {
            "type": "comment_created",
            "source": "bitbucket",
            "account_id": str(account_id),
            "tracker_id": str(tracker_id),
            "payload": {
                "pullrequest": {"id": 7, "links": {"html": {"href": pr_url}}},
                "repository": repository or self.BB_REPO,
            },
        }
        execution = SimpleNamespace(
            flow_id=flow_id,
            trigger_event_details={
                "source": "bitbucket",
                "account_id": str(account_id),
                "tracker_id": str(tracker_id),
                "payload": {"repository": self.BB_REPO},
            },
        )
        return is_bound_implementation_comment, flow, event, execution

    def test_bound_bitbucket_comment_matches_repository_identity(self):
        check, flow, event, execution = self._event_and_flow()
        with patch(
            "preloop.services.flow_pr_binding.find_bound_execution",
            return_value=execution,
        ):
            assert check(MagicMock(), flow, event) is True

    def test_renamed_repository_still_matches_by_uuid(self):
        renamed = {
            "full_name": "ws/renamed",
            "uuid": "{22222222-2222-2222-2222-222222222222}",
        }
        check, flow, event, execution = self._event_and_flow(repository=renamed)
        with patch(
            "preloop.services.flow_pr_binding.find_bound_execution",
            return_value=execution,
        ):
            assert check(MagicMock(), flow, event) is True

    def test_other_repository_does_not_bind(self):
        other = {
            "full_name": "ws/other",
            "uuid": "{99999999-9999-9999-9999-999999999999}",
        }
        check, flow, event, execution = self._event_and_flow(repository=other)
        with patch(
            "preloop.services.flow_pr_binding.find_bound_execution",
            return_value=execution,
        ):
            assert check(MagicMock(), flow, event) is False
