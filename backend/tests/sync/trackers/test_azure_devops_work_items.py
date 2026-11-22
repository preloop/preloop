"""AzureDevOpsTracker work item methods against a stub transport.

Response bodies come from ``tests/fixtures/azure_devops/work_items.json``
(each cites its Microsoft REST 7.1 page). The assertions read the request
bodies Preloop sends: JSON Patch operations, WIQL text and comment HTML.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple
from unittest.mock import MagicMock

import httpx
import pytest

from preloop.schemas.tracker_models import IssueCreate, IssueFilter, IssueUpdate
from preloop.services.azure_work_item_pr_link import (
    azure_work_item_key,
    link_pull_request_to_work_item,
)
from preloop.sync.exceptions import TrackerPermissionError, TrackerResponseError
from preloop.sync.trackers.azure_devops import AzureDevOpsTracker

pytestmark = pytest.mark.asyncio

FIXTURES: Dict[str, Any] = json.loads(
    (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "azure_devops"
        / "work_items.json"
    ).read_text()
)["rest"]
PROJECT = "Fabrikam-Fiber-Git"
WI = "/fabrikam/_apis/wit/workitems/297"
TYPES = f"/fabrikam/{PROJECT}/_apis/wit/workitemtypes"
COMMENTS = f"/fabrikam/{PROJECT}/_apis/wit/workItems/297/comments"
REPO_ID = "5febef5a-833d-4e14-b9c0-14cb638f91e6"
PROJECT_ID = "6ce954b1-ce1f-45d1-b94d-e6bf2464ba2c"


def fx(key: str) -> Dict[str, Any]:
    return copy.deepcopy(FIXTURES[key])


Route = Any


class Stub:
    """Answers from a route table and records every request."""

    def __init__(self, routes: Dict[Tuple[str, str], Route]) -> None:
        self.routes = routes
        self.calls: List[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        answer = self.routes.get((request.method, request.url.path))
        if answer is None:
            return httpx.Response(404, json={"message": "not found"})
        if callable(answer):
            return answer(request)
        return httpx.Response(200, json=answer)

    def bodies(self, method: str, path: str) -> List[Any]:
        return [
            json.loads(c.content)
            for c in self.calls
            if c.method == method and c.url.path == path
        ]


def make(
    routes: Dict[Tuple[str, str], Route], **details: Any
) -> Tuple[AzureDevOpsTracker, Stub]:
    stub = Stub(routes)
    client = AzureDevOpsTracker(
        "t1",
        "pat-value",
        {"organization": "fabrikam", "project": PROJECT, **details},
        transport=httpx.MockTransport(stub),
    )
    return client, stub


def echo_patch(base: Dict[str, Any]) -> Callable[[httpx.Request], httpx.Response]:
    """Apply ``add``/``replace`` field operations to ``base`` and return it."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["content-type"] == "application/json-patch+json"
        item = copy.deepcopy(base)
        for op in json.loads(request.content):
            if op["path"].startswith("/fields/"):
                item["fields"][op["path"][len("/fields/") :]] = op["value"]
            elif op["path"] == "/relations/-":
                item.setdefault("relations", []).append(op["value"])
        item["rev"] = item.get("rev", 1) + 1
        return httpx.Response(200, json=item)

    return handler


async def test_get_issue_maps_fields_comments_and_links() -> None:
    client, stub = make(
        {
            ("GET", WI): fx("work_item"),
            ("GET", COMMENTS): fx("comments"),
            ("GET", TYPES): fx("work_item_types"),
        }
    )
    issue = await client.get_issue(
        "https://dev.azure.com/fabrikam/Fabrikam-Fiber-Git/_workitems/edit/297"
    )
    assert issue.key == "fabrikam/Fabrikam-Fiber-Git#297"
    assert issue.status.name == "New" and issue.status.category == "open"
    assert issue.custom_fields["state_category"] == "Proposed"
    assert issue.labels == ["auth", "customer"]
    assert issue.assignee is not None and issue.assignee.name == "Jane Doe"
    assert issue.priority is not None and issue.priority.level == 2
    assert issue.parent is not None and issue.parent.issue_id == "296"
    assert [(r.relation_type, r.issue_id) for r in issue.relations] == [
        ("parent", "296"),
        ("relates_to", "298"),
    ]
    assert [c.body for c in issue.comments] == ["Moving to the right area path"]
    assert issue.comments[0].url.endswith("/_workitems/edit/297#50")
    first = stub.calls[0]
    assert first.url.params["$expand"] == "relations"
    comments_call = next(c for c in stub.calls if c.url.path == COMMENTS)
    assert comments_call.url.params["api-version"] == "7.1-preview.4"


async def test_work_item_outside_the_bound_project_is_refused() -> None:
    other = fx("work_item")
    other["fields"]["System.TeamProject"] = "Other"
    client, _ = make({("GET", WI): other})
    with pytest.raises(TrackerPermissionError):
        await client.get_issue("297")
    with pytest.raises(TrackerResponseError, match="another Azure DevOps"):
        await client.get_issue("https://dev.azure.com/contoso/P/_workitems/edit/297")


async def test_create_issue_sends_json_patch_with_html_and_parent() -> None:
    created = fx("work_item")
    created["id"] = 300
    client, stub = make(
        {
            ("GET", TYPES): fx("work_item_types"),
            (
                "POST",
                f"/fabrikam/{PROJECT}/_apis/wit/workitems/$Product Backlog Item",
            ): echo_patch(created),
        }
    )
    issue = await client.create_issue(
        f"fabrikam/{PROJECT}/{PROJECT}",
        IssueCreate(
            title="preloop-test new item",
            description="Do **this**\n\n- a\n- b",
            labels=["agent-ready", "triage"],
            priority="Highest",
            parent="296",
        ),
    )
    (ops,) = stub.bodies(
        "POST", f"/fabrikam/{PROJECT}/_apis/wit/workitems/$Product Backlog Item"
    )
    by_path = {op["path"]: op for op in ops}
    assert by_path["/fields/System.Title"]["value"] == "preloop-test new item"
    assert by_path["/fields/System.Description"]["value"] == (
        "<p>Do <strong>this</strong></p><ul><li>a</li><li>b</li></ul>"
    )
    assert by_path["/fields/System.Tags"] == {
        "op": "replace",
        "path": "/fields/System.Tags",
        "value": "agent-ready; triage",
    }
    assert by_path["/fields/Microsoft.VSTS.Common.Priority"]["value"] == 1
    assert by_path["/relations/-"]["value"] == {
        "rel": "System.LinkTypes.Hierarchy-Reverse",
        "url": "https://dev.azure.com/fabrikam/_apis/wit/workItems/296",
    }
    assert issue.key == "fabrikam/Fabrikam-Fiber-Git#300"
    assert issue.description == "Do **this**\n\n- a\n- b"


async def test_update_issue_closes_through_the_localized_completed_state() -> None:
    client, stub = make(
        {
            ("GET", WI): fx("work_item"),
            ("GET", TYPES): fx("work_item_types"),
            ("PATCH", WI): echo_patch(fx("work_item")),
        }
    )
    issue = await client.update_issue(
        "fabrikam/Fabrikam-Fiber-Git#297",
        IssueUpdate(status="closed", labels=["done"], title="Renamed"),
    )
    (ops,) = stub.bodies("PATCH", WI)
    assert {"op": "add", "path": "/fields/System.State", "value": "Erledigt"} in ops
    assert {"op": "replace", "path": "/fields/System.Tags", "value": "done"} in ops
    assert {"op": "add", "path": "/fields/System.Title", "value": "Renamed"} in ops
    assert issue.status.name == "Erledigt"
    assert issue.status.category == "closed"
    assert issue.labels == ["done"]

    with pytest.raises(TrackerResponseError, match="Valid states: New, Genehmigt"):
        await client.update_issue("297", IssueUpdate(status="Resolved"))


async def test_search_issues_runs_wiql_then_batches() -> None:
    items = [fx("work_item"), {**fx("work_item"), "id": 299}]
    client, stub = make(
        {
            ("GET", TYPES): fx("work_item_types"),
            ("POST", f"/fabrikam/{PROJECT}/_apis/wit/wiql"): fx("wiql_result"),
            ("POST", "/fabrikam/_apis/wit/workitemsbatch"): {
                "count": 2,
                "value": items,
            },
        }
    )
    issues, total = await client.search_issues(
        f"fabrikam/{PROJECT}",
        IssueFilter(status=["open"], labels=["auth"]),
        limit=1,
        offset=1,
    )
    assert total == 2
    assert [i.key for i in issues] == ["fabrikam/Fabrikam-Fiber-Git#299"]
    (wiql,) = stub.bodies("POST", f"/fabrikam/{PROJECT}/_apis/wit/wiql")
    assert "[System.TeamProject] = 'Fabrikam-Fiber-Git'" in wiql["query"]
    assert "[System.State] NOT IN ('Erledigt', 'Entfernt')" in wiql["query"]
    assert "[System.Tags] CONTAINS 'auth'" in wiql["query"]
    wiql_call = next(c for c in stub.calls if c.url.path.endswith("/wiql"))
    assert wiql_call.url.params["timePrecision"] == "true"
    (batch,) = stub.bodies("POST", "/fabrikam/_apis/wit/workitemsbatch")
    assert batch == {"ids": [299], "$expand": "Relations", "errorPolicy": "Omit"}


async def test_add_and_update_comment_send_html() -> None:
    comment = fx("comments")["comments"][0]
    client, stub = make(
        {
            ("GET", WI): fx("work_item"),
            ("POST", COMMENTS): {
                **comment,
                "text": "<p>Looks <strong>good</strong></p>",
            },
            ("PATCH", f"{COMMENTS}/50"): {**comment, "text": "<p>edited</p>"},
        }
    )
    added = await client.add_comment("297", "Looks **good**")
    assert stub.bodies("POST", COMMENTS) == [
        {"text": "<p>Looks <strong>good</strong></p>"}
    ]
    assert (added.id, added.body) == ("50", "Looks **good**")
    updated = await client.update_comment("297", "50", "edited")
    assert stub.bodies("PATCH", f"{COMMENTS}/50") == [{"text": "<p>edited</p>"}]
    assert updated.body == "edited"


async def test_add_relation_and_pull_request_link() -> None:
    client, stub = make(
        {("GET", WI): fx("work_item"), ("PATCH", WI): echo_patch(fx("work_item"))}
    )
    assert await client.add_relation("297", "301", "blocks") is True
    (ops,) = stub.bodies("PATCH", WI)
    assert ops == [
        {
            "op": "add",
            "path": "/relations/-",
            "value": {
                "rel": "System.LinkTypes.Dependency-Forward",
                "url": "https://dev.azure.com/fabrikam/_apis/wit/workItems/301",
            },
        }
    ]
    # Already linked (fixture relation to 298) -> no write.
    assert await client.add_relation("297", "298", "relates_to") is False
    # The fixture already links pull request 22; pull request 23 is added.
    assert (
        await client.link_pull_request(
            "297", project_id=PROJECT_ID, repository_id=REPO_ID, pr_id=22
        )
        is False
    )
    stub.calls.clear()
    await client.link_pull_request(
        "297", project_id=PROJECT_ID, repository_id=REPO_ID, pr_id=23
    )
    (ops,) = stub.bodies("PATCH", WI)
    assert ops[0]["value"] == {
        "rel": "ArtifactLink",
        "url": f"vstfs:///Git/PullRequestId/{PROJECT_ID}%2F{REPO_ID}%2F23",
        "attributes": {"name": "Pull Request"},
    }


async def test_orchestrator_link_helper_reads_the_pull_request() -> None:
    trigger = {
        "source": "azure_devops",
        "payload": {"issue": {"id": 297, "key": "fabrikam/Fabrikam-Fiber-Git#297"}},
    }
    assert azure_work_item_key(trigger) == "fabrikam/Fabrikam-Fiber-Git#297"
    assert azure_work_item_key({"source": "jira", "payload": {"issue": {}}}) is None

    class FakeTracker:
        linked: Any = None

    tracker: Any = FakeTracker()

    async def get_pull_request(pr_id: int, repo: str) -> Dict[str, Any]:
        assert (pr_id, repo) == (23, "fabrikam/Fabrikam-Fiber-Git/web")
        return {"repository": {"id": REPO_ID, "project": {"id": PROJECT_ID}}}

    async def link(issue_key: str, **kwargs: Any) -> bool:
        tracker.linked = (issue_key, kwargs)
        return True

    tracker.get_pull_request = get_pull_request
    tracker.link_pull_request = link
    ok = await link_pull_request_to_work_item(
        client=tracker,
        issue_key="fabrikam/Fabrikam-Fiber-Git#297",
        pr_url="https://dev.azure.com/fabrikam/Fabrikam-Fiber-Git/_git/web/pullrequest/23",
        execution_id="e1",
    )
    assert ok is True
    assert tracker.linked == (
        "fabrikam/Fabrikam-Fiber-Git#297",
        {"project_id": PROJECT_ID, "repository_id": REPO_ID, "pr_id": 23},
    )
    # Not an Azure pull request URL: nothing is called.
    assert not await link_pull_request_to_work_item(
        client=tracker,
        issue_key="1",
        pr_url="https://github.com/o/r/pull/1",
        execution_id="e1",
    )


async def test_get_issues_only_for_the_work_item_home_repository() -> None:
    repo = {
        "id": REPO_ID,
        "name": PROJECT,
        "project": {"id": PROJECT_ID, "name": PROJECT},
        "defaultBranch": "refs/heads/main",
    }
    other = {**repo, "id": "11111111-2222-3333-4444-555555555555", "name": "web"}
    client, stub = make(
        {
            ("GET", f"/fabrikam/{PROJECT}/_apis/git/repositories"): {
                "value": [repo, other]
            },
            ("POST", f"/fabrikam/{PROJECT}/_apis/wit/wiql"): fx("wiql_result"),
            ("POST", "/fabrikam/_apis/wit/workitemsbatch"): {
                "value": [fx("work_item")]
            },
            ("GET", TYPES): fx("work_item_types"),
        }
    )
    # Two repositories: the one named like the project is the home.
    assert await client.get_issues("fabrikam", other["id"]) == []
    items = await client.get_issues("fabrikam", REPO_ID)
    assert [i["key"] for i in items] == ["fabrikam/Fabrikam-Fiber-Git#297"]
    assert items[0]["state_category"] == "Proposed"
    listing = [c for c in stub.calls if c.url.path.endswith("/git/repositories")]
    assert len(listing) == 1  # cached across repositories

    project = MagicMock()
    project.id = "p1"
    row = client.transform_issue(items[0], project)
    assert row["external_id"] == "297"
    assert row["key"] == "fabrikam/Fabrikam-Fiber-Git#297"
    assert row["status"] == "open"
    assert row["meta_data"]["labels"] == ["auth", "customer"]
    assert row["meta_data"]["work_item_type"] == "Product Backlog Item"
    assert row["priority"] == "High"
    assert row["updated_at"].year == 2014


async def test_service_hook_context_records_identity_and_states() -> None:
    client, _ = make(
        {
            ("GET", "/fabrikam/_apis/connectionData"): fx("connection_data"),
            ("GET", TYPES): fx("work_item_types"),
        }
    )
    context = await client.work_item_context()
    assert context["token_identity_id"] == "77777777-8888-9999-aaaa-bbbbbbbbbbbb"
    assert context["token_identity_unique_name"] == "preloop-bot@example.com"
    states = context["work_item_states"]["fabrikam-fiber-git"]
    assert states["product backlog item"]["erledigt"] == "Completed"
