"""AzureDevOpsTracker against a stub transport.

Response bodies follow the Azure DevOps REST 7.1 reference examples:
repositories (https://learn.microsoft.com/rest/api/azure/devops/git/
repositories/list), pull requests (.../git/pull-requests/get-pull-request),
threads (.../git/pull-request-threads), iterations and iteration changes
(.../git/pull-request-iterations, .../git/pull-request-iteration-changes)
and service hook subscriptions (.../hooks/subscriptions).
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Tuple

import httpx
import pytest

from preloop.api.endpoints import mcp_azure_devops
from preloop.sync.exceptions import (
    TrackerAuthenticationError,
    TrackerPermissionError,
)
from preloop.sync.trackers.azure_devops import AzureDevOpsTracker
from preloop.sync.trackers.factory import (
    create_tracker_client,
    tracker_hosts_repositories,
)
from preloop.utils.azure_devops import WEBHOOK_SECRET_HEADER

pytestmark = pytest.mark.asyncio

REPO_ID = "5febef5a-833d-4e14-b9c0-14cb638f91e6"
PROJECT_ID = "6ce954b1-ce1f-45d1-b94d-e6bf2464ba2c"
FULL = "fabrikam/Fabrikam-Fiber-Git/Fabrikam-Fiber-Git"
REPO = {
    # Repositories - Get (REST 7.1 example)
    "id": REPO_ID,
    "name": "Fabrikam-Fiber-Git",
    "url": f"https://dev.azure.com/fabrikam/_apis/git/repositories/{REPO_ID}",
    "project": {
        "id": PROJECT_ID,
        "name": "Fabrikam-Fiber-Git",
        "url": f"https://dev.azure.com/fabrikam/_apis/projects/{PROJECT_ID}",
        "state": "wellFormed",
    },
    "defaultBranch": "refs/heads/master",
    "remoteUrl": "https://fabrikam@dev.azure.com/fabrikam/Fabrikam-Fiber-Git/_git/Fabrikam-Fiber-Git",
}
PR = {
    # Pull Requests - Get Pull Request (REST 7.1 example, trimmed)
    "repository": REPO,
    "pullRequestId": 22,
    "codeReviewId": 22,
    "status": "active",
    "createdBy": {"displayName": "Normal Paulk", "uniqueName": "user@example.com"},
    "creationDate": "2016-11-01T16:30:31.6655471Z",
    "title": "A new feature",
    "description": "Adding a new feature",
    "sourceRefName": "refs/heads/npaulk/my_work",
    "targetRefName": "refs/heads/new_feature",
    "mergeStatus": "succeeded",
    "lastMergeSourceCommit": {"commitId": "b60280bc6e62e2f880f1b63c1e24987664d3bda3"},
    "lastMergeTargetCommit": {"commitId": "f47bbc106853afe3c1b07a81754bce5f4b8dbf62"},
    "reviewers": [{"displayName": "Normal Paulk", "vote": 0}],
    "isDraft": False,
}


class Stub:
    """Records requests and answers from a route table."""

    def __init__(self, routes: Dict[Tuple[str, str], Any]) -> None:
        self.routes = routes
        self.calls: List[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        key = (request.method, request.url.path)
        answer = self.routes.get(key)
        if answer is None:
            return httpx.Response(404, json={"message": "not found"})
        if callable(answer):
            return answer(request)
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)


def make(routes: Dict[Tuple[str, str], Any], **details: Any):
    routes.setdefault(("GET", f"{API}/pullRequests/22/threads"), {"value": []})
    stub = Stub(routes)
    client = AzureDevOpsTracker(
        "t1",
        "pat-value",
        {
            "organization": "fabrikam",
            "project": "Fabrikam-Fiber-Git",
            "repository": "Fabrikam-Fiber-Git",
            **details,
        },
        transport=httpx.MockTransport(stub),
    )
    return client, stub


API = "/fabrikam/Fabrikam-Fiber-Git/_apis/git/repositories/Fabrikam-Fiber-Git"


async def test_basic_auth_api_version_and_connection() -> None:
    client, stub = make({("GET", API): REPO})
    result = await client.test_connection()
    assert result.connected, result.message
    request = stub.calls[0]
    expected = "Basic " + base64.b64encode(b":pat-value").decode()
    assert request.headers["authorization"] == expected
    assert request.url.params["api-version"] == "7.1"
    assert request.url.host == "dev.azure.com"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401),
        httpx.Response(203, text="<html>sign in</html>"),
        httpx.Response(302, headers={"Location": "https://login.example.com"}),
    ],
)
async def test_bad_token_is_an_authentication_failure(
    response: httpx.Response,
) -> None:
    client, _ = make(
        {("GET", API): response, ("GET", f"{API}/pullRequests/1"): response}
    )
    result = await client.test_connection()
    assert not result.connected
    assert "personal access token" in result.message
    with pytest.raises(TrackerAuthenticationError):
        await client.get_pull_request(1)


async def test_projects_follow_continuation_and_skip_disabled() -> None:
    pages = iter(
        [
            httpx.Response(
                200,
                json={"value": [REPO], "count": 1},
                headers={"x-ms-continuationtoken": "next"},
            ),
            httpx.Response(
                200,
                json={
                    "value": [
                        {**REPO, "id": PROJECT_ID, "name": "old", "isDisabled": True}
                    ]
                },
            ),
        ]
    )
    client, stub = make(
        {("GET", "/fabrikam/_apis/git/repositories"): lambda _r: next(pages)},
        project=None,
        repository=None,
    )
    projects = await client.get_projects("fabrikam")
    assert [p["id"] for p in projects] == [REPO_ID]
    assert stub.calls[1].url.params["continuationToken"] == "next"
    transformed = client.transform_project(projects[0], "org-id")
    assert transformed["slug"] == FULL
    assert transformed["meta_data"]["clone_url"] == (
        "https://dev.azure.com/fabrikam/Fabrikam-Fiber-Git/_git/Fabrikam-Fiber-Git"
    )
    assert projects[0]["group"] == "Fabrikam-Fiber-Git"


async def test_factory_and_repository_hosting() -> None:
    assert tracker_hosts_repositories("azure_devops")
    client = await create_tracker_client(
        tracker_type="azure_devops",
        tracker_id="t",
        api_key="k",
        connection_details={"organization": "o"},
    )
    assert isinstance(client, AzureDevOpsTracker)


async def test_get_pull_request_tool_maps_threads_and_changes() -> None:
    threads = {
        # Pull Request Threads - List (REST 7.1 example shape)
        "value": [
            {
                "id": 148,
                "status": "active",
                "threadContext": None,
                "comments": [
                    {
                        "id": 1,
                        "parentCommentId": 0,
                        "author": {"displayName": "Normal Paulk"},
                        "content": "Please add tests",
                        "publishedDate": "2016-11-01T16:30:50.083Z",
                        "commentType": "text",
                    },
                    {
                        "id": 2,
                        "parentCommentId": 1,
                        "author": {"displayName": "Jamal Hartnett"},
                        "content": "Done",
                        "commentType": "text",
                    },
                ],
            },
            {
                "id": 149,
                "status": "fixed",
                "threadContext": {
                    "filePath": "/src/app.py",
                    "rightFileStart": {"line": 5, "offset": 1},
                },
                "comments": [
                    {"id": 1, "content": "nit", "commentType": "text"},
                ],
            },
            {
                "id": 150,
                "comments": [
                    {"id": 1, "content": "voted", "commentType": "system"},
                ],
            },
        ]
    }
    iterations = {"value": [{"id": 1}, {"id": 2}]}
    changes = {
        "changeEntries": [
            {"changeType": "edit", "item": {"path": "/src/app.py"}},
            {
                "changeType": "rename",
                "item": {"path": "/b.py"},
                "originalPath": "/a.py",
            },
            {"changeType": "add", "item": {"path": "/src", "isFolder": True}},
        ]
    }
    client, _ = make(
        {
            ("GET", f"{API}/pullRequests/22"): PR,
            ("GET", f"{API}/pullRequests/22/threads"): threads,
            ("GET", f"{API}/pullRequests/22/iterations"): iterations,
            ("GET", f"{API}/pullRequests/22/iterations/2/changes"): changes,
        }
    )
    result = await mcp_azure_devops.get_pull_request(client, "22")
    assert result.url.endswith("/_git/Fabrikam-Fiber-Git/pullrequest/22")
    assert result.source_branch == "npaulk/my_work"
    assert result.head_sha == "b60280bc6e62e2f880f1b63c1e24987664d3bda3"
    assert [c["id"] for c in result.comments] == ["148", "148:2", "149"]
    assert result.comments[1]["in_reply_to_id"] == "148"
    assert result.comments[2]["path"] == "/src/app.py"
    assert result.comments[2]["resolved"] is True
    files = result.changes["changed_files"]
    assert [f["filename"] for f in files] == ["src/app.py", "b.py"]
    assert files[1]["previous_filename"] == "a.py"
    assert "user@example.com" not in result.model_dump_json()


async def test_review_posts_summary_thread_then_vote() -> None:
    posted: List[Dict[str, Any]] = []

    def thread(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        posted.append(body)
        return httpx.Response(
            200, json={"id": 200 + len(posted), "comments": [{"id": 1}]}
        )

    client, stub = make(
        {
            ("POST", f"{API}/pullRequests/22/threads"): thread,
            ("GET", "/fabrikam/_apis/connectionData"): {
                "authenticatedUser": {"id": "d6245f20-2af8-44f4-9451-8107cb2767db"}
            },
            (
                "PUT",
                f"{API}/pullRequests/22/reviewers/d6245f20-2af8-44f4-9451-8107cb2767db",
            ): {"vote": -5},
        },
        cast_token_owner_vote=True,
    )
    result = await mcp_azure_devops.update_pull_request(
        client,
        "22",
        review_action="request_changes",
        review_body="## Review\nNeeds work",
        review_comments=[{"path": "src/app.py", "line": 3, "body": "bug"}],
    )
    assert result.status == "updated"
    assert result.pull_request_id == "202"
    assert "src/app.py" in posted[0]["comments"][0]["content"]
    assert posted[1]["comments"][0]["content"].startswith("Generated by Preloop\n")
    assert (
        "Review decision: **Changes requested**" in posted[1]["comments"][0]["content"]
    )
    assert "## Review\nNeeds work" in posted[1]["comments"][0]["content"]
    assert posted[1]["status"] == "active"
    methods = [(c.method, c.url.path.rsplit("/", 1)[-1]) for c in stub.calls]
    assert methods[-1][0] == "PUT"
    assert json.loads(stub.calls[-1].content) == {"vote": -5}


async def test_review_comment_only_and_refused_vote() -> None:
    client, _ = make(
        {
            ("POST", f"{API}/pullRequests/22/threads"): {
                "id": 7,
                "comments": [{"id": 1}],
            },
            ("GET", "/fabrikam/_apis/connectionData"): {
                "authenticatedUser": {"id": "abc"}
            },
            ("PUT", f"{API}/pullRequests/22/reviewers/abc"): httpx.Response(
                403, json={"message": "no"}
            ),
        },
        cast_token_owner_vote=True,
    )
    comment = await mcp_azure_devops.update_pull_request(
        client, "22", review_action="comment", review_body="LGTM"
    )
    assert comment.pull_request_id == "7"
    approve = await mcp_azure_devops.update_pull_request(
        client, "22", review_action="approve", review_body="LGTM"
    )
    assert approve.status == "updated"
    assert "vote skipped" in approve.message


@pytest.mark.parametrize("state", ["completed", "abandoned", "merged", "closed"])
async def test_refuses_to_complete_or_abandon(state: str) -> None:
    client, stub = make({})
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await mcp_azure_devops.update_pull_request(client, "22", state=state)
    assert exc.value.status_code == 400
    assert stub.calls == []


async def test_add_reply_and_update_comment() -> None:
    client, stub = make(
        {
            ("POST", f"{API}/pullRequests/22/threads/148/comments"): {"id": 3},
            ("PATCH", f"{API}/pullRequests/22/threads/148/comments/3"): {"id": 3},
            ("PATCH", f"{API}/pullRequests/22/threads/148"): {"id": 148},
        }
    )
    reply = await mcp_azure_devops.add_comment(
        client, "22", "thanks", in_reply_to="148"
    )
    assert reply.comment_id == "148:3"
    assert json.loads(stub.calls[0].content)["parentCommentId"] == 1
    updated = await mcp_azure_devops.update_comment(
        client, "22", "148:3", body="edited", resolved=True
    )
    assert updated.comment_id == "148:3"
    assert json.loads(stub.calls[1].content) == {
        "content": "Generated by Preloop\n\nedited"
    }
    assert json.loads(stub.calls[2].content) == {"status": "fixed"}


async def test_permission_error_is_raised_for_threads() -> None:
    client, _ = make(
        {
            ("POST", f"{API}/pullRequests/22/threads"): httpx.Response(
                403, json={"message": "TF401027: needs PullRequestContribute"}
            )
        }
    )
    with pytest.raises(TrackerPermissionError):
        await client.add_pull_request_comment(22, "x")


async def test_ensure_service_hooks_creates_then_updates() -> None:
    callback = "https://preloop.example.com/api/v1/private/webhooks/azure_devops/t1"
    existing: List[Dict[str, Any]] = []
    bodies: List[Tuple[str, Dict[str, Any]]] = []

    def create(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sub = {**body, "id": f"sub-{len(existing)}", "status": "enabled"}
        existing.append(sub)
        bodies.append((request.method, body))
        return httpx.Response(200, json=sub)

    def replace(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append((request.method, body))
        return httpx.Response(200, json={**body, "id": request.url.path[-5:]})

    client, _ = make(
        {
            ("GET", API): REPO,
            ("GET", "/fabrikam/_apis/hooks/subscriptions"): lambda _r: httpx.Response(
                200, json={"value": existing}
            ),
            ("POST", "/fabrikam/_apis/hooks/subscriptions"): create,
            **{
                ("PUT", f"/fabrikam/_apis/hooks/subscriptions/sub-{i}"): replace
                for i in range(5)
            },
        }
    )
    first = await client.ensure_service_hooks(callback, "s1")
    assert first["created"] == 5
    created = {body["eventType"]: body for _m, body in bodies}
    updated_hook = created["git.pullrequest.updated"]
    assert updated_hook["publisherId"] == "tfs"
    assert updated_hook["consumerId"] == "webHooks"
    assert updated_hook["consumerActionId"] == "httpRequest"
    assert updated_hook["publisherInputs"] == {
        "projectId": PROJECT_ID,
        "repository": REPO_ID,
        "notificationType": "PushNotification",
    }
    assert updated_hook["consumerInputs"]["url"] == callback
    assert updated_hook["consumerInputs"]["httpHeaders"] == (
        f"{WEBHOOK_SECRET_HEADER}:s1"
    )
    assert (
        "notificationType" not in created["git.pullrequest.created"]["publisherInputs"]
    )
    # Work item events are project scoped: no repository filter.
    for event in ("workitem.created", "workitem.updated", "workitem.commented"):
        assert created[event]["publisherInputs"] == {"projectId": PROJECT_ID}
        assert created[event]["consumerInputs"]["httpHeaders"] == (
            f"{WEBHOOK_SECRET_HEADER}:s1"
        )
    status = await client.inspect_service_hooks(callback)
    assert status["status"] == "registered"

    bodies.clear()
    second = await client.ensure_service_hooks(callback, "s2")
    assert second["created"] == 0
    assert {m for m, _b in bodies} == {"PUT"}
    assert all(b["consumerInputs"]["httpHeaders"].endswith(":s2") for _m, b in bodies)


@pytest.mark.parametrize("status", [400, 429, 500, "network"])
async def test_failed_vote_keeps_the_posted_review(status: Any) -> None:
    def vote(_request: httpx.Request) -> httpx.Response:
        if status == "network":
            raise httpx.ConnectError("down")
        return httpx.Response(status, json={"message": "vote rule"})

    client, _ = make(
        {
            ("POST", f"{API}/pullRequests/22/threads"): {
                "id": 8,
                "comments": [{"id": 1}],
            },
            ("GET", "/fabrikam/_apis/connectionData"): {
                "authenticatedUser": {"id": "abc"}
            },
            ("PUT", f"{API}/pullRequests/22/reviewers/abc"): vote,
        },
        cast_token_owner_vote=True,
    )
    result = await mcp_azure_devops.update_pull_request(
        client, "22", review_action="approve", review_body="LGTM"
    )
    assert result.status == "updated"
    assert result.pull_request_id == "8"
    assert "vote skipped" in result.message


async def test_repository_outside_the_binding_is_refused() -> None:
    client, stub = make({})
    with pytest.raises(TrackerPermissionError):
        await client.get_pull_request(1, "fabrikam/Other/Fabrikam-Fiber-Git")
    with pytest.raises(TrackerPermissionError):
        await client.get_pull_request(1, "fabrikam/Fabrikam-Fiber-Git/other")
    assert stub.calls == []


def position_routes(change_type: str = "edit") -> Dict[Tuple[str, str], Any]:
    """An iteration with one changed right-side line and immutable contents."""

    def item(request: httpx.Request) -> httpx.Response:
        commit = request.url.params["versionDescriptor.version"]
        return httpx.Response(
            200,
            json={
                "content": "same\nnew\ntail\n"
                if commit == "source"
                else "same\nold\ntail\n"
            },
        )

    return {
        ("GET", f"{API}/pullRequests/22/iterations"): {
            "value": [
                {
                    "id": 3,
                    "sourceRefCommit": {"commitId": "source"},
                    "commonRefCommit": {"commitId": "common"},
                }
            ]
        },
        ("GET", f"{API}/pullRequests/22/iterations/3/changes"): {
            "changeEntries": [
                {
                    "changeTrackingId": 7,
                    "changeType": change_type,
                    "item": {
                        "path": None if change_type == "delete" else "/src/app.py"
                    },
                    "originalPath": "/src/app.py" if change_type == "delete" else None,
                }
            ]
        },
        ("GET", f"{API}/items"): item,
        ("POST", f"{API}/pullRequests/22/threads"): {"id": 9, "comments": [{"id": 1}]},
    }


async def test_positioned_finding_payload() -> None:
    client, stub = make(position_routes())
    result = await mcp_azure_devops.add_comment(
        client, 22, "finding", path="src/app.py", line=2
    )
    payload = json.loads(stub.calls[-1].content)
    assert payload["threadContext"] == {
        "filePath": "/src/app.py",
        "rightFileStart": {"line": 2, "offset": 1},
        "rightFileEnd": {"line": 2, "offset": 1},
    }
    assert payload["pullRequestThreadContext"] == {
        "changeTrackingId": 7,
        "iterationContext": {
            "firstComparingIteration": 3,
            "secondComparingIteration": 3,
        },
    }
    assert payload["comments"][0]["content"].startswith("Generated by Preloop\n")
    assert "finding" in payload["comments"][0]["content"]
    assert "anchored" in result.message
    changes = next(call for call in stub.calls if call.url.path.endswith("/changes"))
    assert changes.url.params["$compareTo"] == "0"


@pytest.mark.parametrize(
    "change_type,path,line,side,reason",
    [
        ("edit", "src/app.py", 1, None, "line is not part of the diff"),
        ("edit", "src/app.py", 99, None, "line is not part of the diff"),
        ("edit", "src/other.py", 2, None, "file is not part of the diff"),
        ("delete", "src/app.py", 2, None, "file was deleted"),
        ("rename, edit", "src/app.py", 2, None, "file was renamed"),
        ("edit", "src/app.py", 2, "LEFT", "only right-side positions are supported"),
    ],
)
async def test_position_fallback_keeps_finding(
    change_type: str, path: str, line: int, side: str | None, reason: str
) -> None:
    client, stub = make(position_routes(change_type))
    result = await client.add_pull_request_comment(
        22, "finding", path=path, line=line, side=side
    )
    assert result["fallback_reason"] == reason
    payload = json.loads(stub.calls[-1].content)
    assert "threadContext" not in payload
    assert "pullRequestThreadContext" not in payload
    assert "finding" in payload["comments"][0]["content"]
    assert f"{path}:{line}" in payload["comments"][0]["content"]


@pytest.mark.parametrize("status", [400, 422])
async def test_azure_rejects_position_then_posts_general_thread(status: int) -> None:
    routes = position_routes()
    payloads: List[Dict[str, Any]] = []

    def post(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        if "threadContext" in payload:
            return httpx.Response(status, json={"message": "invalid position"})
        return httpx.Response(200, json={"id": 10, "comments": [{"id": 1}]})

    routes[("POST", f"{API}/pullRequests/22/threads")] = post
    client, _ = make(routes)
    result = await client.add_pull_request_comment(
        22, "finding", path="src/app.py", line=2
    )
    assert not result["anchored"]
    assert str(status) in result["fallback_reason"]
    assert len(payloads) == 2
    assert payloads[0]["comments"] == payloads[1]["comments"]
    assert "threadContext" not in payloads[1]


@pytest.mark.parametrize("enabled", [False, True])
async def test_vote_opt_in_and_summary_reused_across_two_runs(enabled: bool) -> None:
    threads: List[Dict[str, Any]] = []

    def post(request: httpx.Request) -> httpx.Response:
        thread = {**json.loads(request.content), "id": 100}
        thread["comments"][0]["id"] = 1
        threads.append(thread)
        return httpx.Response(200, json=thread)

    def patch_comment(request: httpx.Request) -> httpx.Response:
        comment = threads[0]["comments"][0]
        comment["content"] = json.loads(request.content)["content"]
        return httpx.Response(200, json=comment)

    client, stub = make(
        {
            ("GET", f"{API}/pullRequests/22/threads"): lambda _: httpx.Response(
                200, json={"value": threads}
            ),
            ("POST", f"{API}/pullRequests/22/threads"): post,
            ("PATCH", f"{API}/pullRequests/22/threads/100/comments/1"): patch_comment,
            ("POST", f"{API}/pullRequests/22/threads/100/comments"): {"id": 2},
            ("GET", "/fabrikam/_apis/connectionData"): {
                "authenticatedUser": {"id": "token-owner"}
            },
            ("PUT", f"{API}/pullRequests/22/reviewers/token-owner"): {"vote": 10},
        },
        cast_token_owner_vote=enabled,
    )
    first = await mcp_azure_devops.update_pull_request(
        client, 22, review_action="request_changes", review_body="notice"
    )
    marker = "<!-- preloop-review:flow-id:pr-reviewer -->"
    for run in (1, 2):
        result = await mcp_azure_devops.add_comment(
            client,
            22,
            f"{marker}\n## Preloop Code Review\nRun {run}\n- [x] Acknowledged",
        )
        assert result.comment_id == first.pull_request_id == "100"
        if run == 1:
            await mcp_azure_devops.update_pull_request(
                client, 22, review_action="approve"
            )
            assert "- [x] Acknowledged" in threads[0]["comments"][0]["content"]
    assert len(threads) == 1
    assert "Run 2" in threads[0]["comments"][0]["content"]
    assert marker in threads[0]["comments"][0]["content"]
    votes = [call for call in stub.calls if call.method == "PUT"]
    assert [json.loads(call.content)["vote"] for call in votes] == (
        [-5, 10] if enabled else []
    )
    if not enabled:
        assert not any("connectionData" in call.url.path for call in stub.calls)


async def test_default_vote_off_and_decision_without_body() -> None:
    client, stub = make(
        {("POST", f"{API}/pullRequests/22/threads"): {"id": 7, "comments": [{"id": 1}]}}
    )
    await mcp_azure_devops.update_pull_request(
        client, 22, review_action="request_changes"
    )
    payload = json.loads(stub.calls[-1].content)
    assert "Changes requested" in payload["comments"][0]["content"]
    assert all(call.method != "PUT" for call in stub.calls)


async def test_reviewed_commit_selects_original_iteration_after_new_push() -> None:
    routes = position_routes()
    original = "a" * 40
    latest = "b" * 40
    routes[("GET", f"{API}/pullRequests/22/iterations")] = {
        "value": [
            {
                "id": 3,
                "sourceRefCommit": {"commitId": original},
                "commonRefCommit": {"commitId": "common"},
            },
            {
                "id": 4,
                "sourceRefCommit": {"commitId": latest},
                "commonRefCommit": {"commitId": "common"},
            },
        ]
    }
    routes[("GET", f"{API}/items")] = lambda request: httpx.Response(
        200,
        json={
            "content": "same\nnew\ntail\n"
            if request.url.params["versionDescriptor.version"] == original
            else "same\nold\ntail\n"
        },
    )
    client, stub = make(routes)
    result = await client.add_pull_request_comment(
        22,
        f"finding\n<!-- preloop-review:reviewed-sha:{original} -->",
        path="src/app.py",
        line=2,
    )
    assert result["anchored"]
    payload = json.loads(stub.calls[-1].content)
    assert (
        payload["pullRequestThreadContext"]["iterationContext"][
            "secondComparingIteration"
        ]
        == 3
    )
    assert not any("/iterations/4/" in call.url.path for call in stub.calls)


async def test_legacy_summary_uses_markers_under_a_human_author() -> None:
    marker = "<!-- preloop-review:flow-id:pr-reviewer -->"
    legacy = {
        "id": 8,
        "comments": [
            {
                "id": 1,
                "author": {"displayName": "Jane Doe"},
                "content": f"{marker}\n## Preloop Code Review\nOld",
            }
        ],
    }
    finding = {
        "id": 7,
        "comments": [
            {
                "id": 1,
                "content": "<!-- preloop-review:flow-id:pr-reviewer:severity:HIGH -->\nMention Preloop Code Review in a finding",
            }
        ],
    }
    client, stub = make(
        {
            ("GET", f"{API}/pullRequests/22/threads"): {"value": [finding, legacy]},
            ("PATCH", f"{API}/pullRequests/22/threads/8/comments/1"): {"id": 1},
        }
    )
    result = await mcp_azure_devops.add_comment(
        client, 22, f"{marker}\n## Preloop Code Review\nNew"
    )
    assert result.comment_id == "8"
    assert [call.method for call in stub.calls] == ["GET", "PATCH"]
    assert json.loads(stub.calls[-1].content)["content"].startswith(
        "Generated by Preloop\n"
    )


@pytest.mark.parametrize("unavailable", ["binary", "missing", "forbidden"])
async def test_unavailable_file_content_falls_back(unavailable: str) -> None:
    routes = position_routes()
    routes[("GET", f"{API}/items")] = {
        "binary": httpx.Response(200, json={"contentMetadata": {"isBinary": True}}),
        "missing": httpx.Response(404, json={"message": "missing"}),
        "forbidden": httpx.Response(403, json={"message": "no code access"}),
    }[unavailable]
    client, stub = make(routes)
    result = await client.add_pull_request_comment(
        22, "finding", path="src/app.py", line=2
    )
    assert result["fallback_reason"] == "diff position could not be verified"
    payload = json.loads(stub.calls[-1].content)
    assert "threadContext" not in payload
    assert "finding" in payload["comments"][0]["content"]


def test_deleted_file_list_uses_original_path() -> None:
    """Azure returns item.path=null for a deletion, preserving only originalPath."""
    result = mcp_azure_devops._changes(
        [
            {
                "changeType": "delete",
                "changeTrackingId": 4,
                "originalPath": "/src/removed.py",
                "item": {"path": None},
            }
        ]
    )
    assert result["changed_files"][0]["filename"] == "src/removed.py"
    assert result["changed_files"][0]["status"] == "removed"
