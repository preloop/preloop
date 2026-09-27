"""Backport configuration, event extraction and trigger gate (issue #961)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from preloop.models.schemas.flow import Backport, GitCloneConfig
from preloop.services.backport import (
    BackportConfigError,
    BackportEventError,
    MergedChange,
    backport_body,
    backport_event_matches,
    backport_title,
    extract_merged_change,
    resolve_backport_plan,
)
from preloop.services.backport_branches import backport_branch_name
from preloop.services.backport_hosts import (
    BackportHostError,
    GitHubBackportHost,
    GitLabBackportHost,
    backport_host_for,
)

SHA = "a" * 40
CONFIG: Dict[str, Any] = {
    "backport": {
        "enabled": True,
        "source_branch": "release/1.0",
        "target_branches": ["release/1.1", "main"],
        "reviewers": ["maintainer-a"],
    }
}


def github_event(base: str = "release/1.0", **pr: Any) -> Dict[str, Any]:
    pull_request = {
        "number": 812,
        "html_url": "https://github.com/acme/widgets/pull/812",
        "title": "Fix the bug",
        "body": "Fixes a crash.",
        "base": {"ref": base},
        "merged": True,
        "merge_commit_sha": SHA,
        **pr,
    }
    return {
        "source": "github",
        "tracker_id": "tracker-1",
        "type": "pull_request_merged",
        "payload": {
            "action": "closed",
            "pull_request": pull_request,
            "repository": {"clone_url": "https://github.com/acme/widgets.git"},
        },
    }


def gitlab_event(base: str = "release/1.0", **attributes: Any) -> Dict[str, Any]:
    object_attributes = {
        "iid": 34,
        "url": "https://gitlab.com/acme/widgets/-/merge_requests/34",
        "title": "Fix the bug",
        "description": "Fixes a crash.",
        "target_branch": base,
        "action": "merge",
        "merge_commit_sha": SHA,
        **attributes,
    }
    return {
        "source": "gitlab",
        "tracker_id": "tracker-2",
        "type": "merge_request_merged",
        "payload": {
            "object_kind": "merge_request",
            "object_attributes": object_attributes,
            "project": {"git_http_url": "https://gitlab.com/acme/widgets.git"},
        },
    }


class TestSchema:
    def test_valid_block_round_trips(self) -> None:
        config = GitCloneConfig(**CONFIG)
        assert config.backport is not None
        assert config.backport.target_branches == ["release/1.1", "main"]
        assert config.backport.comment_on_original is True

    @pytest.mark.parametrize(
        ("block", "message"),
        [
            (
                {"source_branch": "release/1.0", "target_branches": ["release/1.0"]},
                "may not include backport.source_branch",
            ),
            (
                {"source_branch": "release/1.0", "target_branches": ["main", "main"]},
                "twice",
            ),
            (
                {
                    "source_branch": "release/1.0",
                    "target_branches": ["release/1.1", "release-1.1"],
                },
                "same backport branch name",
            ),
            (
                {"source_branch": "release/1.0", "target_branches": []},
                "at least 1",
            ),
            (
                {"source_branch": "release/1.0", "target_branches": ["-x"]},
                "not a valid branch name",
            ),
            (
                {"source_branch": "a..b", "target_branches": ["main"]},
                "not a valid branch name",
            ),
            (
                {
                    "source_branch": "release/1.0",
                    "target_branches": ["main"],
                    "reviewers": ["two words"],
                },
                "invalid",
            ),
            (
                {
                    "source_branch": "release/1.0",
                    "target_branches": ["main"],
                    "unknown": True,
                },
                "Extra inputs",
            ),
        ],
    )
    def test_invalid_blocks_are_refused(self, block: dict, message: str) -> None:
        with pytest.raises(ValidationError, match=message):
            Backport(enabled=True, **block)

    def test_reviewers_are_trimmed_and_deduplicated(self) -> None:
        block = Backport(
            source_branch="release/1.0",
            target_branches=["main"],
            reviewers=["@alice", "alice", " bob "],
        )
        assert block.reviewers == ["alice", "bob"]

    def test_backport_refuses_agent_publication(self) -> None:
        with pytest.raises(ValidationError, match="create_pull_request"):
            GitCloneConfig(create_pull_request=True, **CONFIG)

    def test_disabled_block_does_not_restrict_publication(self) -> None:
        config = GitCloneConfig(
            create_pull_request=True,
            backport={**CONFIG["backport"], "enabled": False},
        )
        assert config.create_pull_request is True


class TestPlan:
    def test_absent_or_disabled_block_is_no_plan(self) -> None:
        assert resolve_backport_plan(None) is None
        assert resolve_backport_plan({}) is None
        assert (
            resolve_backport_plan(
                {"backport": {**CONFIG["backport"], "enabled": False}}
            )
            is None
        )

    def test_plan_from_stored_dict_and_model(self) -> None:
        plan = resolve_backport_plan(CONFIG)
        assert plan is not None
        assert plan.target_branches == ("release/1.1", "main")
        assert plan.reviewers == ("maintainer-a",)
        assert resolve_backport_plan(GitCloneConfig(**CONFIG)) == plan

    def test_invalid_stored_block_raises(self) -> None:
        stored = {
            "backport": {
                "enabled": True,
                "source_branch": "main",
                "target_branches": ["main"],
            }
        }
        with pytest.raises(BackportConfigError):
            resolve_backport_plan(stored)


class TestGate:
    def test_non_backport_flow_is_not_gated(self) -> None:
        assert backport_event_matches(None, {"type": "issue_opened"})
        assert backport_event_matches({"enabled": True}, {"type": "push"})

    def test_merge_into_source_branch_starts_the_flow(self) -> None:
        assert backport_event_matches(CONFIG, github_event())
        assert backport_event_matches(CONFIG, gitlab_event())

    @pytest.mark.parametrize("base", ["main", "release/1.1", "release/1.0-rc"])
    def test_merge_into_any_other_branch_does_not_start_the_flow(
        self, base: str
    ) -> None:
        assert not backport_event_matches(CONFIG, github_event(base=base))
        assert not backport_event_matches(CONFIG, gitlab_event(base=base))

    def test_non_merge_event_does_not_start_the_flow(self) -> None:
        event = github_event()
        event["type"] = "pull_request_opened"
        assert not backport_event_matches(CONFIG, event)

    def test_invalid_config_never_starts_the_flow_and_says_why(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        stored = {
            "backport": {
                "enabled": True,
                "source_branch": "main",
                "target_branches": ["main"],
            }
        }
        with caplog.at_level("WARNING", logger="preloop.services.backport"):
            assert not backport_event_matches(stored, github_event(base="main"))
        assert "Backport flow not started" in caplog.text
        assert "git_clone_config.backport is invalid" in caplog.text

    def test_trigger_service_applies_the_gate(self) -> None:
        from preloop.services.flow_trigger_service import FlowTriggerService

        service = FlowTriggerService(MagicMock())
        flow = SimpleNamespace(
            id="flow-1", name="Backport", trigger_config=None, git_clone_config=CONFIG
        )
        assert service._matches_trigger_config(flow, github_event())
        assert not service._matches_trigger_config(flow, github_event(base="main"))
        assert service.matches_trigger_config(flow, gitlab_event())
        assert not service.matches_trigger_config(flow, gitlab_event(base="dev"))


class TestExtraction:
    def test_github_merge(self) -> None:
        change = extract_merged_change(github_event())
        assert change == MergedChange(
            host="github",
            number=812,
            url="https://github.com/acme/widgets/pull/812",
            title="Fix the bug",
            description="Fixes a crash.",
            base_branch="release/1.0",
            merge_commit_sha=SHA,
            repository_url="https://github.com/acme/widgets.git",
        )

    def test_gitlab_merge(self) -> None:
        change = extract_merged_change(gitlab_event())
        assert change.number == 34
        assert change.host == "gitlab"
        assert change.url.endswith("/merge_requests/34")
        assert change.repository_url == "https://gitlab.com/acme/widgets.git"

    def test_gitlab_squash_commit_is_used_without_a_merge_commit(self) -> None:
        change = extract_merged_change(
            gitlab_event(merge_commit_sha=None, squash_commit_sha="b" * 40)
        )
        assert change.merge_commit_sha == "b" * 40

    def test_missing_merge_commit_is_refused(self) -> None:
        with pytest.raises(BackportEventError, match="no merge commit"):
            extract_merged_change(github_event(merge_commit_sha=None))

    def test_repository_url_with_credentials_is_refused(self) -> None:
        event = github_event()
        event["payload"]["repository"]["clone_url"] = (
            "https://user:secret@github.com/acme/widgets.git"
        )
        with pytest.raises(BackportEventError, match="HTTPS"):
            extract_merged_change(event)

    def test_non_merge_event_is_refused(self) -> None:
        with pytest.raises(BackportEventError):
            extract_merged_change({"type": "push", "payload": {}})


class TestText:
    def test_branch_name_carries_number_and_target(self) -> None:
        assert backport_branch_name(812, "release/2.4") == (
            "backport/pr-812-to-release-2.4"
        )

    def test_title_and_body_start_from_the_original(self) -> None:
        change = extract_merged_change(github_event())
        assert backport_title(change, "main") == "Fix the bug (backport to main)"
        body = backport_body(change, "main")
        assert body.startswith("Fixes a crash.")
        assert "Backport of https://github.com/acme/widgets/pull/812 to `main`." in body

    def test_long_title_is_bounded(self) -> None:
        change = extract_merged_change(github_event(title="x" * 400))
        title = backport_title(change, "main")
        assert len(title) <= 250
        assert title.endswith("(backport to main)")


class TestHosts:
    def test_bitbucket_is_refused_with_a_pointer_to_the_follow_up(self) -> None:
        with pytest.raises(BackportHostError, match="#955"):
            backport_host_for("bitbucket", object())

    @pytest.mark.asyncio
    async def test_github_prefers_an_open_pull_request(self) -> None:
        tracker = MagicMock()
        tracker.find_pull_requests_by_branch = AsyncMock(
            return_value=[
                {"number": 3, "url": "u3", "state": "closed"},
                {"number": 5, "url": "u5", "state": "open"},
            ]
        )
        found = await GitHubBackportHost(tracker).find_change("b", "main")
        assert found is not None and found.number == 5

    @pytest.mark.asyncio
    async def test_host_errors_never_echo_the_response_body(self) -> None:
        from preloop.sync.exceptions import TrackerResponseError

        tracker = MagicMock()
        tracker.request_pull_request_reviewers = AsyncMock(
            side_effect=TrackerResponseError("body with secret", status_code=422)
        )
        with pytest.raises(BackportHostError) as caught:
            await GitHubBackportHost(tracker).request_reviewers(5, ["alice"])
        assert "secret" not in str(caught.value)
        assert "422" in str(caught.value)

    @pytest.mark.asyncio
    async def test_gitlab_sets_found_reviewers_and_reports_missing_ones(
        self,
    ) -> None:
        tracker = MagicMock()
        tracker.get_user_id_by_username = AsyncMock(side_effect=[11, None])
        tracker.update_merge_request = AsyncMock(return_value={"iid": 7})
        with pytest.raises(BackportHostError, match="ghost"):
            await GitLabBackportHost(tracker).request_reviewers(7, ["alice", "ghost"])
        tracker.update_merge_request.assert_awaited_once_with("7", reviewer_ids=[11])

    @pytest.mark.asyncio
    async def test_gitlab_opens_and_comments_on_merge_requests(self) -> None:
        tracker = MagicMock()
        tracker.create_merge_request = AsyncMock(
            return_value={"iid": 8, "url": "https://gitlab.com/mr/8"}
        )
        tracker.create_mr_discussion = AsyncMock(return_value={})
        host = GitLabBackportHost(tracker)
        opened = await host.open_change("b", "main", "t", "d")
        assert (opened.number, opened.url) == (8, "https://gitlab.com/mr/8")
        await host.comment_on_original(34, "summary")
        tracker.create_mr_discussion.assert_awaited_once_with("34", "summary")
