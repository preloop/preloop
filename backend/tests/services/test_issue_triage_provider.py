"""Hermetic provider-contract tests for issue triage writes."""

from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from preloop.services.issue_triage_provider import (
    IssueTriageProvider,
    TriageProviderError,
)
from preloop.sync.exceptions import TrackerResponseError
from preloop.sync.trackers.github import GitHubTracker


class FakeTracker:
    """Record provider requests and model delta writes without network access."""

    def __init__(self, kind: str) -> None:
        self.tracker_type = kind
        self.connection_details = (
            {"owner": "example", "repo": "project"}
            if kind == "github"
            else {"project_id": "42", "url": "https://gitlab.example.com"}
        )
        self.calls: list[tuple[str, str, Any, Any]] = []
        self.rows: list[dict[str, Any]] = []
        self.pages: dict[int, Any] = {}
        self.fail: tuple[str, str] | None = None
        self.issue: dict[str, Any] = {
            "title": "Original title",
            "state": "open" if kind == "github" else "opened",
            "updated_at": "2026-09-13T12:00:00Z",
            "number": 7,
            "iid": 7,
            "project_id": 42,
            "body": "Original body",
            "description": "Original body",
            "html_url": "https://github.com/example/project/issues/7",
            "web_url": "https://gitlab.example.com/example/project/-/issues/7",
            "labels": [{"name": "human-label"}, {"name": "complexity:high"}]
            if kind == "github"
            else ["human-label", "complexity:high"],
        }
        self.gl = SimpleNamespace(
            **{
                f"http_{verb.lower()}": verb
                for verb in ["GET", "POST", "PUT", "DELETE"]
            }
        )

    def label_names(self) -> list[str]:
        return (
            [label["name"] for label in self.issue["labels"]]
            if self.tracker_type == "github"
            else list(self.issue["labels"])
        )

    def set_labels(self, labels: list[str]) -> None:
        self.issue["labels"] = (
            [{"name": name} for name in labels]
            if self.tracker_type == "github"
            else labels
        )

    async def _request(
        self, method: str, path: str, data: Any = None, params: Any = None
    ) -> Any:
        from urllib.parse import unquote

        self.calls.append((method, path, deepcopy(data), deepcopy(params)))
        if self.fail == (method, path):
            raise TrackerResponseError("provider response must not escape")
        if "/issues/" not in path:
            if method == "GET":
                page = params["page"]
                return deepcopy(self.pages.get(page, self.rows if page == 1 else []))
            self.rows.append(deepcopy(data))
            return deepcopy(data)
        if method == "GET":
            return deepcopy(self.issue)
        if path.endswith("/labels") and method == "POST":
            self.set_labels(list(dict.fromkeys([*self.label_names(), *data["labels"]])))
            return self.issue["labels"]
        if method == "DELETE":
            name = unquote(path.rsplit("/", 1)[-1])
            self.set_labels([label for label in self.label_names() if label != name])
            return None
        self.issue.update(
            {
                key: value
                for key, value in data.items()
                if key in {"title", "body", "description"}
            }
        )
        if "add_labels" in (data or {}) or "remove_labels" in (data or {}):
            additions = (
                data.get("add_labels", "").split(",") if data.get("add_labels") else []
            )
            removals = (
                data.get("remove_labels", "").split(",")
                if data.get("remove_labels")
                else []
            )
            self.set_labels(
                [
                    name
                    for name in dict.fromkeys([*self.label_names(), *additions])
                    if name not in removals
                ]
            )
        return deepcopy(self.issue)

    async def _make_request(self, method: str, path: str, **kwargs: Any) -> Any:
        return await self._request(
            method, path, kwargs.get("post_data"), kwargs.get("query_data")
        )


@pytest.fixture(params=["github", "gitlab"])
def tracker(request: pytest.FixtureRequest) -> FakeTracker:
    return FakeTracker(request.param)


@pytest.mark.asyncio
async def test_fresh_issue_identity_and_revision_include_label_changes(
    tracker: FakeTracker,
) -> None:
    adapter = IssueTriageProvider(tracker, "7")
    initial = await adapter.read_issue()
    assert initial.state == "open"
    assert initial.body == "Original body"
    tracker.set_labels([*tracker.label_names(), "added-by-human"])
    changed = await adapter.read_issue()
    assert changed.revision != initial.revision
    assert "added-by-human" in changed.labels
    assert len(tracker.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("title", []), ("state", "bogus"), ("labels", "oops"), ("updated_at", {})],
)
async def test_malformed_issue_is_not_accepted(
    tracker: FakeTracker, field: str, value: Any
) -> None:
    tracker.issue[field] = value
    with pytest.raises(TriageProviderError):
        await IssueTriageProvider(tracker, "7").read_issue()


@pytest.mark.asyncio
async def test_wrong_issue_or_project_is_rejected(tracker: FakeTracker) -> None:
    tracker.issue["number" if tracker.tracker_type == "github" else "iid"] = 8
    with pytest.raises(TriageProviderError, match="identity_mismatch"):
        await IssueTriageProvider(tracker, "7").read_issue()
    tracker.issue["iid"] = 7
    tracker.issue["project_id"] = 99
    if tracker.tracker_type == "gitlab":
        with pytest.raises(TriageProviderError, match="project_mismatch"):
            await IssueTriageProvider(tracker, "7").read_issue()


@pytest.mark.asyncio
async def test_github_pr_cannot_be_triaged_as_issue() -> None:
    tracker = FakeTracker("github")
    tracker.issue["pull_request"] = {
        "url": "https://api.github.com/repos/example/project/pulls/7"
    }
    with pytest.raises(TriageProviderError, match="pull_request"):
        await IssueTriageProvider(tracker, "7").read_issue()


@pytest.mark.parametrize("number", ["0", "-1", "7/labels", "../7", "7?x=1", "", 7])
def test_issue_number_is_not_a_provider_path(number: Any) -> None:
    with pytest.raises(TriageProviderError, match="invalid_issue_number"):
        IssueTriageProvider(FakeTracker("github"), number)


@pytest.mark.parametrize(
    "kind,config",
    [
        ("github", {"owner": "../example", "repo": "project"}),
        ("gitlab", {"project_id": "7?scope=all"}),
        ("gitlab", {"project_id": True}),
        ("jira", {}),
    ],
)
def test_missing_or_unsafe_scope_and_unsupported_tracker(
    kind: str, config: dict
) -> None:
    tracker = FakeTracker(kind)
    tracker.connection_details = config
    with pytest.raises(TriageProviderError):
        IssueTriageProvider(tracker, "7")


@pytest.mark.asyncio
async def test_catalogue_paginates_and_preserves_descriptions(
    tracker: FakeTracker,
) -> None:
    tracker.pages[1] = [{"name": f"label-{i}", "description": None} for i in range(100)]
    tracker.pages[2] = [
        {"name": "complexity:low", "description": "Preloop issue complexity: low"}
    ]
    rows = await IssueTriageProvider(tracker, "7").catalogue()
    assert len(rows) == 101
    assert rows[0]["description"] == ""
    assert rows[-1]["description"] == "Preloop issue complexity: low"
    assert [call[3]["page"] for call in tracker.calls] == [1, 2]
    if tracker.tracker_type == "gitlab":
        assert all(call[3]["include_ancestor_groups"] for call in tracker.calls)


@pytest.mark.asyncio
async def test_catalogue_never_returns_truncated_prefix(tracker: FakeTracker) -> None:
    tracker.pages = {
        page: [{"name": f"label-{page}-{i}"} for i in range(100)]
        for page in range(1, 11)
    }
    with pytest.raises(TriageProviderError, match="limit_exceeded"):
        await IssueTriageProvider(tracker, "7").catalogue()
    assert len(tracker.calls) == 10


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        [{"name": "same"}, {"name": "same"}],
        [{"name": "x", "description": []}],
        {"values": []},
        ["label"],
    ],
)
async def test_bad_catalogue_cannot_establish_absence(
    tracker: FakeTracker, rows: Any
) -> None:
    tracker.pages[1] = rows
    with pytest.raises(TriageProviderError):
        await IssueTriageProvider(tracker, "7").catalogue()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "level,color", [("low", "0e8a16"), ("medium", "fbca04"), ("high", "b60205")]
)
async def test_create_standard_label_ownership_metadata(
    tracker: FakeTracker, level: str, color: str
) -> None:
    await IssueTriageProvider(tracker, "7").create_label(f"complexity:{level}")
    assert tracker.calls[-1][2] == {
        "name": f"complexity:{level}",
        "description": f"Preloop issue complexity: {level}",
        "color": ("#" if tracker.tracker_type == "gitlab" else "") + color,
    }


@pytest.mark.asyncio
async def test_cannot_create_unrelated_or_dispatch_label(tracker: FakeTracker) -> None:
    with pytest.raises(TriageProviderError, match="invalid_standard"):
        await IssueTriageProvider(tracker, "7").create_label("agent-ready")
    assert tracker.calls == []


@pytest.mark.asyncio
async def test_content_update_never_replaces_labels_or_other_fields(
    tracker: FakeTracker,
) -> None:
    before = tracker.label_names()
    await IssueTriageProvider(tracker, "7").write_content(
        "Improved title", "Clear acceptance"
    )
    payload = tracker.calls[-1][2]
    assert payload == {
        "title": "Improved title",
        "body"
        if tracker.tracker_type == "github"
        else "description": "Clear acceptance",
    }
    assert tracker.label_names() == before


@pytest.mark.asyncio
async def test_label_deltas_preserve_unrelated_and_concurrent_labels(
    tracker: FakeTracker,
) -> None:
    adapter = IssueTriageProvider(tracker, "7")
    await adapter.read_issue()
    tracker.set_labels([*tracker.label_names(), "new-human-label"])
    await adapter.update_labels(["complexity:low"], ["complexity:high"])
    assert tracker.label_names() == ["human-label", "new-human-label", "complexity:low"]
    assert all("state" not in (call[2] or {}) for call in tracker.calls)
    if tracker.tracker_type == "github":
        assert tracker.calls[-1][1].endswith("/labels/complexity%3Ahigh")
    else:
        assert tracker.calls[-1][2] == {
            "add_labels": "complexity:low",
            "remove_labels": "complexity:high",
        }


@pytest.mark.asyncio
async def test_empty_delta_does_not_write(tracker: FakeTracker) -> None:
    await IssueTriageProvider(tracker, "7").update_labels([], [])
    assert tracker.calls == []


@pytest.mark.asyncio
async def test_add_failure_does_not_remove_previous_complexity() -> None:
    tracker = FakeTracker("github")
    tracker.fail = ("POST", "/repos/example/project/issues/7/labels")
    with pytest.raises(TriageProviderError, match="request_failed"):
        await IssueTriageProvider(tracker, "7").update_labels(
            ["complexity:low"], ["complexity:high"]
        )
    assert tracker.label_names() == ["human-label", "complexity:high"]
    assert len(tracker.calls) == 1


@pytest.mark.asyncio
async def test_remove_failure_is_not_reported_as_success_after_add() -> None:
    tracker = FakeTracker("github")
    tracker.fail = (
        "DELETE",
        "/repos/example/project/issues/7/labels/complexity%3Ahigh",
    )
    with pytest.raises(TriageProviderError, match="request_failed"):
        await IssueTriageProvider(tracker, "7").update_labels(
            ["complexity:low"], ["complexity:high"]
        )
    assert tracker.label_names() == ["human-label", "complexity:high", "complexity:low"]


@pytest.mark.asyncio
async def test_github_successful_empty_response_is_not_a_json_failure() -> None:
    tracker = GitHubTracker(
        "synthetic-tracker", "synthetic-token", {"owner": "example", "repo": "project"}
    )
    response = httpx.Response(204, headers={"x-test": "empty"})
    client = AsyncMock()
    client.request.return_value = response
    with patch("preloop.sync.trackers.github.httpx.AsyncClient") as factory:
        factory.return_value.__aenter__.return_value = client
        body, headers = await tracker._request_with_headers(
            "DELETE", "/repos/example/project/issues/7/labels/old"
        )
    assert body is None
    assert headers["x-test"] == "empty"
    assert client.request.call_count == 1


@pytest.mark.asyncio
async def test_issue_url_cannot_redirect_identity_to_a_foreign_host(
    tracker: FakeTracker,
) -> None:
    field = "html_url" if tracker.tracker_type == "github" else "web_url"
    tracker.issue[field] = (
        tracker.issue[field]
        .replace("github.com", "other.example.com")
        .replace("gitlab.example.com", "other.example.com")
    )
    with pytest.raises(TriageProviderError, match="invalid_issue_url"):
        await IssueTriageProvider(tracker, "7").read_issue()


@pytest.mark.parametrize(
    "kind,config",
    [
        ("github", {"owner": "..", "repo": "project"}),
        ("gitlab", {"project_id": "example/../project"}),
    ],
)
def test_scope_dot_segments_are_rejected(kind: str, config: dict) -> None:
    tracker = FakeTracker(kind)
    tracker.connection_details = config
    with pytest.raises(TriageProviderError):
        IssueTriageProvider(tracker, "7")


@pytest.mark.asyncio
async def test_gitlab_namespaced_project_uses_encoded_api_path() -> None:
    tracker = FakeTracker("gitlab")
    tracker.connection_details["project_id"] = "example/project"
    await IssueTriageProvider(tracker, "7").read_issue()
    assert tracker.calls[0][1] == "/projects/example%2Fproject/issues/7"
