"""Writing an opened pull request back onto the triggering Jira issue.

The Jira side is a fake site behind ``httpx.MockTransport``: it implements
the remote link upsert rule from the REST v3 docs (a POST whose globalId
matches an existing link updates it, otherwise it creates one) and records
every request path, so the tests can also prove the development information
API is never called.
"""

import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from preloop.services.flow_execution_notifications import notify_terminal_execution
from preloop.services.jira_pr_writeback import (
    format_writeback_comment,
    jira_issue_key,
    remote_link_global_id,
    split_pull_request_url,
    write_pull_request_to_jira,
)
from preloop.sync.trackers.jira import JiraTracker, text_to_adf

pytestmark = pytest.mark.asyncio

_RealAsyncClient = httpx.AsyncClient


class FakeJiraSite:
    """Just enough of Jira Cloud REST for comments and remote links."""

    def __init__(self) -> None:
        self.paths: List[str] = []
        self.comments: List[Dict[str, Any]] = []
        self.links: Dict[str, Dict[str, Any]] = {}
        self.link_statuses: List[int] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        body = json.loads(request.content or b"{}")
        if path.endswith("/comment") and request.method == "POST":
            if not path.startswith("/rest/api/3/"):
                # REST v2 takes a plain string body and rejects ADF.
                return httpx.Response(400, json={"errors": {"comment": "string"}})
            self.comments.append(body)
            return httpx.Response(
                201,
                json={
                    "id": str(len(self.comments)),
                    "body": body["body"],
                    "created": "2026-09-27T10:00:00.000+0000",
                    "author": {"accountId": "bot", "displayName": "Automation"},
                },
            )
        if path.endswith("/remotelink") and request.method == "POST":
            global_id = body["globalId"]
            existed = global_id in self.links
            link_id = self.links[global_id]["id"] if existed else len(self.links) + 1
            self.links[global_id] = {"id": link_id, **body}
            status = 200 if existed else 201
            self.link_statuses.append(status)
            return httpx.Response(status, json={"id": link_id, "self": "x"})
        return httpx.Response(404, json={})


@pytest.fixture
def site() -> FakeJiraSite:
    return FakeJiraSite()


@pytest.fixture
def jira(site: FakeJiraSite):
    with patch("preloop.sync.trackers.jira.JIRA"):
        tracker = JiraTracker(
            "tracker-jira",
            "api-token",
            {"url": "https://example.atlassian.net", "username": "bot@example.com"},
        )

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return _RealAsyncClient(transport=httpx.MockTransport(site.handler))

    with patch("preloop.sync.trackers.jira.httpx.AsyncClient", client_factory):
        yield tracker


class TestTextToAdf:
    def test_urls_become_links_and_lines_paragraphs(self) -> None:
        doc = text_to_adf(
            "Pull request opened: https://github.com/o/r/pull/7\nBranch: b"
        )
        first, second = doc["content"]
        link = first["content"][1]
        assert link["text"] == "https://github.com/o/r/pull/7"
        assert link["marks"] == [
            {"type": "link", "attrs": {"href": "https://github.com/o/r/pull/7"}}
        ]
        assert second["content"] == [{"type": "text", "text": "Branch: b"}]

    def test_trailing_punctuation_is_not_part_of_the_link(self) -> None:
        doc = text_to_adf("See https://example.com/a.")
        nodes = doc["content"][0]["content"]
        assert nodes[1]["marks"][0]["attrs"]["href"] == ("https://example.com/a")
        assert nodes[2]["text"] == "."

    def test_empty_text_is_a_valid_document(self) -> None:
        assert text_to_adf("") == {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": []}],
        }


class TestHelpers:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://github.com/Acme/API/pull/12", ("github.com", "Acme/API", "12")),
            (
                "https://gitlab.example.com/g/sub/r/-/merge_requests/3",
                ("gitlab.example.com", "g/sub/r", "3"),
            ),
            (
                "https://bitbucket.org/ws/repo/pull-requests/5",
                ("bitbucket.org", "ws/repo", "5"),
            ),
            ("https://example.com/other", ("example.com", "other", None)),
        ],
    )
    def test_split_pull_request_url(self, url: str, expected: tuple) -> None:
        assert split_pull_request_url(url) == expected

    def test_global_id_is_per_repository(self) -> None:
        first = remote_link_global_id("https://github.com/Acme/API/pull/12")
        second = remote_link_global_id("https://github.com/acme/api/pull/13")
        other = remote_link_global_id("https://github.com/acme/web/pull/12")
        assert first == second == "preloop:pull-request:github.com/acme/api"
        assert other != first
        assert len(remote_link_global_id("https://h/" + "a/" * 300 + "pull/1")) <= 255

    def test_comment_names_url_and_branch(self) -> None:
        assert format_writeback_comment("https://h/o/r/pull/1", "feat/x") == (
            "Pull request opened: https://h/o/r/pull/1\nBranch: feat/x"
        )
        assert format_writeback_comment("https://h/o/r/pull/1", None) == (
            "Pull request opened: https://h/o/r/pull/1"
        )

    def test_jira_issue_key(self) -> None:
        event = {"source": "jira", "payload": {"issue": {"key": "PROJ-12"}}}
        assert jira_issue_key(event) == "PROJ-12"
        assert jira_issue_key({**event, "source": "github"}) is None
        assert jira_issue_key({"source": "jira", "payload": {}}) is None
        assert jira_issue_key(None) is None


class TestWriteBack:
    async def test_second_run_updates_the_link(self, jira, site) -> None:
        first = await write_pull_request_to_jira(
            jira_client=jira,
            issue_key="PROJ-12",
            pr_url="https://github.com/acme/api/pull/12",
            branch="preloop/proj-12",
            execution_id="exec-1",
        )
        second = await write_pull_request_to_jira(
            jira_client=jira,
            issue_key="PROJ-12",
            pr_url="https://github.com/acme/api/pull/13",
            branch="preloop/proj-12-b",
            execution_id="exec-2",
        )

        assert first.comment_posted and first.remote_link_written
        assert second.comment_posted and second.remote_link_written
        # One link, created then updated in place.
        assert site.link_statuses == [201, 200]
        assert len(site.links) == 1
        (link,) = site.links.values()
        assert link["globalId"] == "preloop:pull-request:github.com/acme/api"
        assert link["object"]["url"] == "https://github.com/acme/api/pull/13"
        assert link["object"]["title"] == "Pull request acme/api#13"
        assert link["object"]["summary"] == "Branch preloop/proj-12-b"
        assert link["object"]["status"] == {"resolved": False}
        # Every write is a full object: omitted fields would be nulled.
        assert link["application"] == {"type": "ai.preloop", "name": "Preloop"}
        # Both comments carry the URL as a link.
        assert len(site.comments) == 2
        text = json.dumps(site.comments[0]["body"])
        assert "https://github.com/acme/api/pull/12" in text
        assert "preloop/proj-12" in text
        assert set(site.paths) == {
            "/rest/api/3/issue/PROJ-12/comment",
            "/rest/api/3/issue/PROJ-12/remotelink",
        }

    async def test_development_information_api_is_never_called(
        self, jira, site
    ) -> None:
        await write_pull_request_to_jira(
            jira_client=jira,
            issue_key="PROJ-1",
            pr_url="https://gitlab.example.com/g/r/-/merge_requests/2",
            branch="b",
            execution_id="exec",
        )
        assert site.paths
        assert not any("devinfo" in path or "dev-status" in path for path in site.paths)

    async def test_link_failure_keeps_the_comment(self) -> None:
        client = MagicMock()
        client.add_comment = AsyncMock()
        client.add_remote_link = AsyncMock(side_effect=RuntimeError("linking off"))
        outcome = await write_pull_request_to_jira(
            jira_client=client,
            issue_key="PROJ-1",
            pr_url="https://github.com/o/r/pull/1",
            branch=None,
            execution_id="exec",
        )
        assert outcome.comment_posted is True
        assert outcome.remote_link_written is False

    async def test_comment_failure_still_links(self) -> None:
        client = MagicMock()
        client.add_comment = AsyncMock(side_effect=RuntimeError("no permission"))
        client.add_remote_link = AsyncMock()
        outcome = await write_pull_request_to_jira(
            jira_client=client,
            issue_key="PROJ-1",
            pr_url="https://github.com/o/r/pull/1",
            branch="b",
            execution_id="exec",
        )
        assert outcome.comment_posted is False
        assert outcome.remote_link_written is True

    async def test_no_client(self) -> None:
        outcome = await write_pull_request_to_jira(
            jira_client=None,
            issue_key="PROJ-1",
            pr_url="https://github.com/o/r/pull/1",
            branch=None,
            execution_id="exec",
        )
        assert outcome.skipped_reason == "no_jira_client"

    async def test_code_host_client_is_never_used(self) -> None:
        client = MagicMock(spec=["add_comment"])
        client.add_comment = AsyncMock()
        outcome = await write_pull_request_to_jira(
            jira_client=client,
            issue_key="PROJ-1",
            pr_url="https://github.com/o/r/pull/1",
            branch=None,
            execution_id="exec",
        )
        assert outcome.skipped_reason == "not_a_jira_client"
        client.add_comment.assert_not_called()


class TestGenericCommentIsNotDoubled:
    @pytest.mark.parametrize("skip", [True, False])
    async def test_skip_success_comment(self, skip: bool) -> None:
        client = MagicMock()
        client.add_comment = AsyncMock()
        outcome = await notify_terminal_execution(
            notifications={"on_success": {"comment_on_trigger_issue": True}},
            status="SUCCEEDED",
            execution_id="exec",
            trigger_event_details={
                "source": "jira",
                "payload": {"issue": {"key": "PROJ-1"}},
            },
            result={"pr_url": "https://github.com/o/r/pull/1"},
            tracker_client=client,
            skip_success_comment=skip,
        )
        assert outcome.success_comment_posted is not skip
        assert client.add_comment.await_count == (0 if skip else 1)
