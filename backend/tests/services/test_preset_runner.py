"""Tests for resolve-or-create and issue trigger payloads."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from preloop.services.preset_runner import (
    IMPLEMENTER_SLUG,
    REVIEWER_SLUG,
    TRIAGE_SLUG,
    PresetRunnerError,
    _load_visible_issue,
    build_issue_trigger_payload,
    build_pull_request_trigger_payload,
    resolve_or_create_flow,
    run_preset_on_target,
)
from preloop.services.prompt_resolvers.base import ResolverContext
from preloop.services.prompt_resolvers.trigger_event import TriggerEventResolver
from preloop.sync.exceptions import TrackerResponseError


class _Simple:
    """Plain object so MagicMock return_value does not try to parent it."""

    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


def _preset(name: str = "Automated Issue Implementation") -> _Simple:
    return _Simple(
        id=uuid.uuid4(),
        name=name,
        is_preset=True,
        prompt_template="implement {{trigger_event.payload.object_attributes.title}}",
        allowed_mcp_tools=[],
        agent_type="codex",
        ai_model_id=None,
        agent_config={},
        description="implement",
        trigger_event_source=None,
        trigger_event_types=["issue_labeled"],
    )


def _account_flow(
    *, enabled: bool = True, name: str = "Automated Issue Implementation"
) -> _Simple:
    return _Simple(
        id=uuid.uuid4(),
        name=name,
        is_preset=False,
        is_enabled=enabled,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _user(account_id: uuid.UUID) -> MagicMock:
    user = MagicMock()
    user.account_id = account_id
    user.id = uuid.uuid4()
    user.is_active = True
    return user


def test_resolve_flow_finds_existing_by_source_preset():
    account_id = uuid.uuid4()
    preset = _preset()
    existing = _account_flow()
    crud = MagicMock()
    crud.get_global_preset_by_name.return_value = preset
    crud.get_by_source_preset.return_value = existing

    with patch(
        "preloop.services.preset_runner.PRESET_SLUGS", {IMPLEMENTER_SLUG: preset.name}
    ):
        flow, created = resolve_or_create_flow(
            MagicMock(),
            account_id=account_id,
            preset_slug=IMPLEMENTER_SLUG,
            confirm_create=False,
            current_user=_user(account_id),
            flow_crud=crud,
        )

    assert flow is existing
    assert created is False
    crud.get_by_name_and_account.assert_not_called()
    crud.create.assert_not_called()


def test_resolve_flow_falls_back_to_name():
    account_id = uuid.uuid4()
    preset = _preset()
    renamed = _account_flow()
    crud = MagicMock()
    crud.get_global_preset_by_name.return_value = preset
    crud.get_by_source_preset.return_value = None
    crud.get_by_name_and_account.return_value = renamed

    with patch(
        "preloop.services.preset_runner.PRESET_SLUGS", {IMPLEMENTER_SLUG: preset.name}
    ):
        flow, created = resolve_or_create_flow(
            MagicMock(),
            account_id=account_id,
            preset_slug=IMPLEMENTER_SLUG,
            confirm_create=False,
            current_user=_user(account_id),
            flow_crud=crud,
        )

    assert flow is renamed
    assert created is False


def test_create_flow_uses_preset_name_and_default_model():
    account_id = uuid.uuid4()
    preset = _preset()
    created_flow = _account_flow()
    crud = MagicMock()
    crud.get_global_preset_by_name.return_value = preset
    crud.get_by_source_preset.return_value = None
    crud.get_by_name_and_account.return_value = None
    crud.create.return_value = created_flow

    with (
        patch(
            "preloop.services.preset_runner.PRESET_SLUGS",
            {IMPLEMENTER_SLUG: preset.name},
        ),
        patch("preloop.services.preset_runner.has_permission", return_value=True),
        patch(
            "preloop.services.preset_runner.clone_preset_for_account",
            return_value=created_flow,
        ) as clone,
    ):
        flow, created = resolve_or_create_flow(
            MagicMock(),
            account_id=account_id,
            preset_slug=IMPLEMENTER_SLUG,
            confirm_create=True,
            current_user=_user(account_id),
            flow_crud=crud,
        )

    assert created is True
    assert flow is created_flow
    clone.assert_called_once()
    assert clone.call_args.kwargs["name"] == preset.name
    assert clone.call_args.kwargs["clear_event_triggers"] is True


def test_create_flow_403_without_create_flows():
    account_id = uuid.uuid4()
    preset = _preset()
    crud = MagicMock()
    crud.get_global_preset_by_name.return_value = preset
    crud.get_by_source_preset.return_value = None
    crud.get_by_name_and_account.return_value = None

    with (
        patch(
            "preloop.services.preset_runner.PRESET_SLUGS",
            {IMPLEMENTER_SLUG: preset.name},
        ),
        patch("preloop.services.preset_runner.has_permission", return_value=False),
        patch("preloop.services.preset_runner.clone_preset_for_account") as clone,
    ):
        with pytest.raises(PresetRunnerError) as exc:
            resolve_or_create_flow(
                MagicMock(),
                account_id=account_id,
                preset_slug=IMPLEMENTER_SLUG,
                confirm_create=True,
                current_user=_user(account_id),
                flow_crud=crud,
            )

    assert exc.value.status_code == 403
    assert "not create them" in str(exc.value.detail)
    clone.assert_not_called()


def test_create_flow_422_without_model():
    account_id = uuid.uuid4()
    preset = _preset()
    crud = MagicMock()
    crud.get_global_preset_by_name.return_value = preset
    crud.get_by_source_preset.return_value = None
    crud.get_by_name_and_account.return_value = None

    with (
        patch(
            "preloop.services.preset_runner.PRESET_SLUGS",
            {IMPLEMENTER_SLUG: preset.name},
        ),
        patch("preloop.services.preset_runner.has_permission", return_value=True),
        patch(
            "preloop.services.preset_runner.clone_preset_for_account",
            side_effect=HTTPException(status_code=422, detail="Add an AI model"),
        ),
    ):
        with pytest.raises(PresetRunnerError) as exc:
            resolve_or_create_flow(
                MagicMock(),
                account_id=account_id,
                preset_slug=IMPLEMENTER_SLUG,
                confirm_create=True,
                current_user=_user(account_id),
                flow_crud=crud,
            )

    assert exc.value.status_code == 422
    assert "AI model" in str(exc.value.detail)


def test_resolve_flow_disabled_409_carries_flow_name():
    account_id = uuid.uuid4()
    preset = _preset()
    existing = _account_flow(enabled=False, name="Custom Implementer")
    crud = MagicMock()
    crud.get_global_preset_by_name.return_value = preset
    crud.get_by_source_preset.return_value = existing

    with patch(
        "preloop.services.preset_runner.PRESET_SLUGS", {IMPLEMENTER_SLUG: preset.name}
    ):
        with pytest.raises(PresetRunnerError) as exc:
            resolve_or_create_flow(
                MagicMock(),
                account_id=account_id,
                preset_slug=IMPLEMENTER_SLUG,
                confirm_create=False,
                current_user=_user(account_id),
                flow_crud=crud,
            )

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "flow_disabled"
    assert exc.value.detail["flow_id"] == str(existing.id)
    assert exc.value.detail["flow_name"] == "Custom Implementer"


def test_load_visible_issue_404_for_other_account():
    issue_id = uuid.uuid4()
    account_id = uuid.uuid4()
    issue = MagicMock()
    issue.tracker_id = uuid.uuid4()
    issue.project_id = uuid.uuid4()
    other_tracker = MagicMock()
    other_tracker.id = uuid.uuid4()

    with (
        patch("preloop.services.preset_runner.crud_issue") as issue_crud,
        patch("preloop.services.preset_runner.crud_tracker") as tracker_crud,
        patch("preloop.services.preset_runner.crud_project") as project_crud,
    ):
        issue_crud.get.return_value = issue
        tracker_crud.get_for_account.return_value = [other_tracker]
        with pytest.raises(PresetRunnerError) as exc:
            _load_visible_issue(MagicMock(), issue_id=issue_id, account_id=account_id)

    assert exc.value.status_code == 404
    project_crud.get.assert_not_called()


def _github_issue_payload(*, tracker_url: str = "https://github.com") -> dict:
    issue = MagicMock()
    issue.external_id = "2451234567"
    issue.key = "example/repo#42"
    issue.title = "Broken search"
    issue.description = "Search returns 500"
    issue.status = "open"
    issue.updated_at = None
    issue.meta_data = {
        "url": "https://github.com/example/repo/issues/42",
        "assignees": [{"name": "janedoe"}],
        "labels": [{"name": "bug"}],
    }
    issue.external_url = None
    project = MagicMock()
    project.id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    project.name = "repo"
    project.slug = "example/repo"
    project.identifier = "99"
    project.settings = {}
    project.meta_data = {"default_branch": "main"}
    tracker = MagicMock()
    tracker.id = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
    tracker.account_id = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
    tracker.tracker_type = "github"
    tracker.url = tracker_url
    return build_issue_trigger_payload(issue, project, tracker)


def _gitlab_issue_payload() -> dict:
    issue = MagicMock()
    issue.external_id = "2451234567"
    issue.key = "group/project2#7"
    issue.title = "Fix login"
    issue.description = "Login 401"
    issue.status = "opened"
    issue.updated_at = None
    issue.meta_data = {"url": "https://gitlab.example.com/group/project2/-/issues/7"}
    issue.external_url = None
    project = MagicMock()
    project.id = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    project.name = "project"
    project.slug = "group/project2"
    project.identifier = "321"
    project.settings = {}
    project.meta_data = {}
    tracker = MagicMock()
    tracker.id = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
    tracker.account_id = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
    tracker.tracker_type = "gitlab"
    tracker.url = "https://gitlab.example.com"
    return build_issue_trigger_payload(issue, project, tracker)


@pytest.mark.asyncio
async def test_github_issue_payload_resolves_object_attributes():
    event = _github_issue_payload()
    resolver = TriggerEventResolver()
    context = ResolverContext(
        db=MagicMock(),
        trigger_event_data=event,
        flow_id="flow-1",
        execution_id="exec-1",
    )
    title = await resolver.resolve("payload.object_attributes.title", context)
    number = await resolver.resolve("payload.object_attributes.number", context)
    url = await resolver.resolve("payload.object_attributes.url", context)
    assert title == "Broken search"
    assert int(number) == 42
    assert url == "https://github.com/example/repo/issues/42"
    assert event["payload"]["issue"]["user"]["login"] == "janedoe"
    assert event["payload"]["issue"]["labels"] == ["bug"]
    assert event["payload"]["issue"]["updated_at"] is None


def test_jira_issue_payload_is_rejected():
    issue = MagicMock()
    project = MagicMock()
    tracker = MagicMock()
    tracker.tracker_type = "jira"
    with pytest.raises(PresetRunnerError) as exc:
        build_issue_trigger_payload(issue, project, tracker)
    assert exc.value.status_code == 400
    assert "GitHub and GitLab" in str(exc.value.detail)


def test_jira_triage_payload_uses_object_attributes():
    issue = MagicMock()
    issue.external_id = "10001"
    issue.key = "ALP-9"
    issue.title = "Broken search"
    issue.description = "Search returns 500"
    issue.status = "open"
    issue.updated_at = "2026-01-04T00:00:00Z"
    issue.meta_data = {
        "url": "https://jira.example.com/browse/ALP-9",
        "labels": ["bug"],
        "assignees": [{"login": "assigned-user"}],
    }
    issue.external_url = None
    project = MagicMock()
    project.id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    project.name = "Alpha"
    project.slug = "alpha"
    project.identifier = "ALP"
    project.settings = {}
    project.meta_data = {}
    tracker = MagicMock()
    tracker.id = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
    tracker.account_id = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
    tracker.tracker_type = "jira"
    tracker.url = "https://jira.example.com"
    event = build_issue_trigger_payload(issue, project, tracker, git_only=False)
    assert event["source"] == "jira"
    assert "repository" not in event["payload"]
    assert "default_branch" not in str(event)
    assert ".git" not in str(event)
    assert event["payload"]["object_attributes"]["number"] == "ALP-9"
    assert event["payload"]["object_attributes"]["author"] == ""
    assert event["payload"]["object_attributes"]["title"] == "Broken search"
    assert event["payload"]["object_attributes"]["labels"] == ["bug"]
    assert event["payload"]["object_attributes"]["updated_at"] == "2026-01-04T00:00:00Z"


@pytest.mark.asyncio
async def test_gitlab_issue_payload_number_alias():
    event = _gitlab_issue_payload()
    resolver = TriggerEventResolver()
    normalized = resolver._normalize_event_data(event)
    attrs = normalized["payload"]["object_attributes"]
    assert attrs["iid"] == 7
    assert attrs["number"] == 7
    assert attrs["labels"] == []
    context = ResolverContext(
        db=MagicMock(),
        trigger_event_data=event,
        flow_id="flow-1",
        execution_id="exec-1",
    )
    number = await resolver.resolve("payload.object_attributes.number", context)
    assert int(number) == 7


def test_payload_sets_project_id():
    event = _github_issue_payload()
    assert event["payload"]["project_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert event["project_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert event["tracker_id"] == "cccccccc-cccc-cccc-cccc-cccccccccccc"
    assert event["account_id"] == "dddddddd-dddd-dddd-dddd-dddddddddddd"


def test_payload_resolves_trigger_project_id_when_flow_has_no_projects():
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    event = _github_issue_payload()
    shaped = _Simple(
        trigger_event_data=event,
        flow=_Simple(trigger_project_ids=[]),
        db=MagicMock(),
    )
    resolved = FlowExecutionOrchestrator._resolve_trigger_project_id(shaped)
    assert resolved == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def test_preset_runner_payload_has_clone_fields():
    github = _github_issue_payload()
    repo = github["payload"]["repository"]
    assert repo["clone_url"] == "https://github.com/example/repo.git"
    assert repo["html_url"] == "https://github.com/example/repo"
    assert repo["full_name"] == "example/repo"
    assert repo["name"] == "repo"
    assert repo["default_branch"] == "main"

    gitlab = _gitlab_issue_payload()
    project = gitlab["payload"]["project"]
    assert (
        project["http_url_to_repo"] == "https://gitlab.example.com/group/project2.git"
    )
    assert project["web_url"] == "https://gitlab.example.com/group/project2"
    assert project["path_with_namespace"] == "group/project2"
    repo = gitlab["payload"]["repository"]
    assert repo["http_url_to_repo"] == project["http_url_to_repo"]
    assert repo["clone_url"] == project["http_url_to_repo"]


def test_github_api_host_uses_github_com_clone_url():
    event = _github_issue_payload(tracker_url="https://api.github.com")
    repo = event["payload"]["repository"]
    assert repo["clone_url"] == "https://github.com/example/repo.git"
    assert repo["html_url"] == "https://github.com/example/repo"


def _github_project_tracker(*, tracker_url: str = "https://github.com"):
    project = MagicMock()
    project.id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    project.name = "repo"
    project.slug = "example/repo"
    project.identifier = "99"
    project.settings = {}
    project.meta_data = {"default_branch": "main"}
    tracker = MagicMock()
    tracker.id = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
    tracker.account_id = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
    tracker.tracker_type = "github"
    tracker.url = tracker_url
    return project, tracker


def _gitlab_project_tracker():
    project = MagicMock()
    project.id = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    project.name = "project"
    project.slug = "group/project"
    project.identifier = "321"
    project.settings = {}
    project.meta_data = {}
    tracker = MagicMock()
    tracker.id = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
    tracker.account_id = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
    tracker.tracker_type = "gitlab"
    tracker.url = "https://gitlab.example.com"
    return project, tracker


def _github_pr_payload(*, tracker_url: str = "https://github.com") -> dict:
    project, tracker = _github_project_tracker(tracker_url=tracker_url)
    pr = {
        "id": "2451234567",
        "number": 12,
        "title": "Add login",
        "description": "Please review the login form",
        "url": "https://github.com/example/repo/pull/12",
        "author": {"login": "janedoe"},
        "source_branch": "feature",
        "target_branch": "main",
        "state": "open",
        "is_draft": False,
    }
    return build_pull_request_trigger_payload(pr, project, tracker)


def _gitlab_mr_payload() -> dict:
    project, tracker = _gitlab_project_tracker()
    mr = {
        "id": "2451234567",
        "iid": 7,
        "title": "Fix login",
        "description": "Login 401",
        "url": "https://gitlab.example.com/group/project/-/merge_requests/7",
        "author": {"username": "janedoe"},
        "source_branch": "feature",
        "target_branch": "main",
        "state": "opened",
        "work_in_progress": False,
    }
    return build_pull_request_trigger_payload(mr, project, tracker)


@pytest.mark.asyncio
async def test_github_pr_payload_resolves_object_attributes():
    event = _github_pr_payload()
    assert event["type"] == "pull_request_run"
    assert event["source"] == "github"
    assert event["payload"]["project_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    resolver = TriggerEventResolver()
    context = ResolverContext(
        db=MagicMock(),
        trigger_event_data=event,
        flow_id="flow-1",
        execution_id="exec-1",
    )
    title = await resolver.resolve("payload.object_attributes.title", context)
    description = await resolver.resolve(
        "payload.object_attributes.description", context
    )
    author = await resolver.resolve("payload.object_attributes.author", context)
    url = await resolver.resolve("payload.object_attributes.url", context)
    source_branch = await resolver.resolve(
        "payload.object_attributes.source_branch", context
    )
    target_branch = await resolver.resolve(
        "payload.object_attributes.target_branch", context
    )
    trigger = await resolver.resolve("type", context)
    number = await resolver.resolve("payload.object_attributes.number", context)
    assert title == "Add login"
    assert description == "Please review the login form"
    assert author == "janedoe"
    assert url == "https://github.com/example/repo/pull/12"
    assert source_branch == "feature"
    assert target_branch == "main"
    assert trigger == "pull_request_run"
    assert int(number) == 12
    assert event["payload"]["pull_request"]["number"] == 12


@pytest.mark.asyncio
async def test_gitlab_mr_payload_branches_and_url():
    event = _gitlab_mr_payload()
    assert event["payload"]["object_kind"] == "merge_request"
    resolver = TriggerEventResolver()
    normalized = resolver._normalize_event_data(event)
    attrs = normalized["payload"]["object_attributes"]
    assert attrs["iid"] == 7
    assert attrs["number"] == 7
    assert attrs["source_branch"] == "feature"
    assert attrs["target_branch"] == "main"
    assert attrs["url"] == (
        "https://gitlab.example.com/group/project/-/merge_requests/7"
    )
    assert attrs["author"] == "janedoe"
    context = ResolverContext(
        db=MagicMock(),
        trigger_event_data=event,
        flow_id="flow-1",
        execution_id="exec-1",
    )
    source_branch = await resolver.resolve(
        "payload.object_attributes.source_branch", context
    )
    url = await resolver.resolve("payload.object_attributes.url", context)
    assert source_branch == "feature"
    assert url == "https://gitlab.example.com/group/project/-/merge_requests/7"


def test_pr_payload_sets_top_level_project_id_and_web_host_clone_fields():
    event = _github_pr_payload(tracker_url="https://api.github.com")
    assert event["project_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert event["tracker_id"] == "cccccccc-cccc-cccc-cccc-cccccccccccc"
    assert event["account_id"] == "dddddddd-dddd-dddd-dddd-dddddddddddd"
    assert event["payload"]["project_id"] == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    repo = event["payload"]["repository"]
    assert repo["clone_url"] == "https://github.com/example/repo.git"
    assert repo["html_url"] == "https://github.com/example/repo"
    assert event["payload"]["pull_request"]["user"]["login"] == "janedoe"


@pytest.mark.asyncio
async def test_run_preset_on_pull_request_probe_then_trigger() -> None:
    account_id = uuid.uuid4()
    project_id = uuid.uuid4()
    flow = _account_flow(name="Pull Request Reviewer")
    project, tracker = _github_project_tracker()
    organization = MagicMock()
    organization.id = uuid.uuid4()
    user = _user(account_id)
    target = _Simple(kind="pull_request", project_id=project_id, number=12)
    tracker_client = MagicMock()
    tracker_client.get_pull_request = AsyncMock(
        return_value={
            "id": "2451234567",
            "number": 12,
            "title": "Add login",
            "description": "Please review",
            "url": "https://github.com/example/repo/pull/12",
            "author": {"login": "janedoe"},
            "source_branch": "feature",
            "target_branch": "main",
            "state": "open",
            "is_draft": False,
        }
    )
    trigger = AsyncMock(return_value={"id": str(uuid.uuid4())})

    with (
        patch(
            "preloop.services.preset_runner._load_visible_project",
            return_value=(project, tracker, organization),
        ),
        patch(
            "preloop.services.preset_runner.resolve_or_create_flow",
            return_value=(flow, False),
        ),
        patch(
            "preloop.api.common.get_tracker_client",
            new_callable=AsyncMock,
            return_value=tracker_client,
        ),
        patch(
            "preloop.services.flow_trigger_service.FlowTriggerService.trigger_flow",
            trigger,
        ),
    ):
        probe = await run_preset_on_target(
            MagicMock(),
            current_user=user,
            preset_slug=REVIEWER_SLUG,
            target=target,
            confirm_create=False,
            triggered_by="Jane Doe",
        )
        assert probe["execution_id"] is None
        tracker_client.get_pull_request.assert_not_awaited()
        trigger.assert_not_awaited()

        confirmed = await run_preset_on_target(
            MagicMock(),
            current_user=user,
            preset_slug=REVIEWER_SLUG,
            target=target,
            confirm_create=True,
            triggered_by="Jane Doe",
        )

    tracker_client.get_pull_request.assert_awaited_once_with("12")
    trigger.assert_awaited_once()
    assert trigger.await_args.kwargs["test_mode"] is False
    payload = trigger.await_args.kwargs["trigger_event_data"]["payload"]
    assert payload["pull_request"]["number"] == 12
    assert confirmed["execution_id"] is not None


@pytest.mark.asyncio
async def test_run_preset_on_pull_request_502_when_tracker_fails() -> None:
    account_id = uuid.uuid4()
    project_id = uuid.uuid4()
    flow = _account_flow(name="Pull Request Reviewer")
    project, tracker = _github_project_tracker()
    organization = MagicMock()
    organization.id = uuid.uuid4()
    user = _user(account_id)
    target = _Simple(kind="pull_request", project_id=project_id, number=12)
    tracker_client = MagicMock()
    tracker_client.get_pull_request = AsyncMock(
        side_effect=TrackerResponseError("GitHub API error: 502")
    )

    with (
        patch(
            "preloop.services.preset_runner._load_visible_project",
            return_value=(project, tracker, organization),
        ),
        patch(
            "preloop.services.preset_runner.resolve_or_create_flow",
            return_value=(flow, False),
        ),
        patch(
            "preloop.api.common.get_tracker_client",
            new_callable=AsyncMock,
            return_value=tracker_client,
        ),
        patch(
            "preloop.services.flow_trigger_service.FlowTriggerService.trigger_flow",
        ) as trigger,
    ):
        with pytest.raises(PresetRunnerError) as exc:
            await run_preset_on_target(
                MagicMock(),
                current_user=user,
                preset_slug=REVIEWER_SLUG,
                target=target,
                confirm_create=True,
                triggered_by="Jane Doe",
            )

    assert exc.value.status_code == 502
    assert exc.value.detail == "Tracker request failed"
    trigger.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_preset_on_pull_request_500_when_trigger_omits_execution_id() -> None:
    account_id = uuid.uuid4()
    project_id = uuid.uuid4()
    flow = _account_flow(name="Pull Request Reviewer")
    project, tracker = _github_project_tracker()
    organization = MagicMock()
    organization.id = uuid.uuid4()
    user = _user(account_id)
    target = _Simple(kind="pull_request", project_id=project_id, number=12)
    tracker_client = MagicMock()
    tracker_client.get_pull_request = AsyncMock(
        return_value={
            "id": "2451234567",
            "number": 12,
            "title": "Add login",
            "description": "Please review",
            "url": "https://github.com/example/repo/pull/12",
            "author": {"login": "janedoe"},
            "source_branch": "feature",
            "target_branch": "main",
            "state": "open",
            "is_draft": False,
        }
    )
    trigger = AsyncMock(return_value={})

    with (
        patch(
            "preloop.services.preset_runner._load_visible_project",
            return_value=(project, tracker, organization),
        ),
        patch(
            "preloop.services.preset_runner.resolve_or_create_flow",
            return_value=(flow, False),
        ),
        patch(
            "preloop.api.common.get_tracker_client",
            new_callable=AsyncMock,
            return_value=tracker_client,
        ),
        patch(
            "preloop.services.flow_trigger_service.FlowTriggerService.trigger_flow",
            trigger,
        ),
    ):
        with pytest.raises(PresetRunnerError) as exc:
            await run_preset_on_target(
                MagicMock(),
                current_user=user,
                preset_slug=REVIEWER_SLUG,
                target=target,
                confirm_create=True,
                triggered_by="Jane Doe",
            )

    assert exc.value.status_code == 500
    assert "execution id" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_reviewer_slug_still_rejected_on_issue_target() -> None:
    user = _user(uuid.uuid4())
    target = _Simple(kind="issue", issue_id=uuid.uuid4())
    with pytest.raises(PresetRunnerError) as exc:
        await run_preset_on_target(
            MagicMock(),
            current_user=user,
            preset_slug=REVIEWER_SLUG,
            target=target,
            confirm_create=False,
            triggered_by="Jane Doe",
        )
    assert exc.value.status_code == 400
    assert "issue" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_implementer_batch_targets_rejected() -> None:
    user = _user(uuid.uuid4())
    targets = [
        _Simple(kind="issue", issue_id=uuid.uuid4()),
        _Simple(kind="issue", issue_id=uuid.uuid4()),
    ]
    with pytest.raises(PresetRunnerError) as exc:
        await run_preset_on_target(
            MagicMock(),
            current_user=user,
            preset_slug=IMPLEMENTER_SLUG,
            targets=targets,
            confirm_create=False,
            triggered_by="Jane Doe",
        )
    assert exc.value.status_code == 400
    assert "issue-triage-assistant" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_triage_batch_dedupes_and_returns_per_item_errors() -> None:
    account_id = uuid.uuid4()
    good_id = uuid.uuid4()
    missing_id = uuid.uuid4()
    flow = _account_flow(name="Issue Triage Assistant")
    user = _user(account_id)
    issue = MagicMock()
    issue.external_id = "42"
    issue.key = "example/repo#42"
    issue.title = "Broken search"
    issue.description = "Search returns 500"
    issue.status = "open"
    issue.updated_at = None
    issue.meta_data = {"url": "https://github.com/example/repo/issues/42", "labels": []}
    issue.external_url = None
    project, tracker = _github_project_tracker()
    trigger = AsyncMock(return_value={"id": str(uuid.uuid4())})

    def _load(_db, *, issue_id, account_id):  # noqa: ARG001
        if issue_id == good_id:
            return issue, project, tracker
        raise PresetRunnerError(404, "Issue not found")

    with (
        patch(
            "preloop.services.preset_runner._load_visible_issue",
            side_effect=_load,
        ),
        patch(
            "preloop.services.preset_runner.resolve_or_create_flow",
            return_value=(flow, False),
        ),
        patch(
            "preloop.services.flow_trigger_service.FlowTriggerService.trigger_flow",
            trigger,
        ),
    ):
        probe = await run_preset_on_target(
            MagicMock(),
            current_user=user,
            preset_slug=TRIAGE_SLUG,
            targets=[
                _Simple(kind="issue", issue_id=good_id),
                _Simple(kind="issue", issue_id=good_id),
                _Simple(kind="issue", issue_id=missing_id),
            ],
            confirm_create=False,
            triggered_by="Jane Doe",
        )
        assert probe["execution_id"] is None
        assert [item["issue_id"] for item in probe["results"]] == [
            str(good_id),
            str(missing_id),
        ]
        trigger.assert_not_awaited()

        confirmed = await run_preset_on_target(
            MagicMock(),
            current_user=user,
            preset_slug=TRIAGE_SLUG,
            targets=[
                _Simple(kind="issue", issue_id=good_id),
                _Simple(kind="issue", issue_id=good_id),
                _Simple(kind="issue", issue_id=missing_id),
            ],
            confirm_create=True,
            triggered_by="Jane Doe",
        )

    trigger.assert_awaited_once()
    assert trigger.await_args.kwargs["test_mode"] is False
    by_id = {item["issue_id"]: item for item in confirmed["results"]}
    assert by_id[str(good_id)]["execution_id"] is not None
    assert by_id[str(missing_id)]["error"] == "Issue not found"
    assert confirmed["execution_id"] == by_id[str(good_id)]["execution_id"]


def test_run_preset_request_requires_exactly_one_of_target_or_targets() -> None:
    from pydantic import ValidationError

    from preloop.models.schemas.flow import RunPresetRequest, RunPresetTarget

    issue_id = uuid.uuid4()
    target = RunPresetTarget(kind="issue", issue_id=issue_id)
    with pytest.raises(ValidationError):
        RunPresetRequest(preset_slug=TRIAGE_SLUG)
    with pytest.raises(ValidationError):
        RunPresetRequest(preset_slug=TRIAGE_SLUG, target=target, targets=[target])
    with pytest.raises(ValidationError):
        RunPresetRequest(preset_slug=TRIAGE_SLUG, targets=[])
    too_many = [RunPresetTarget(kind="issue", issue_id=uuid.uuid4()) for _ in range(26)]
    with pytest.raises(ValidationError):
        RunPresetRequest(preset_slug=TRIAGE_SLUG, targets=too_many)
    ok = RunPresetRequest(preset_slug=TRIAGE_SLUG, target=target)
    assert ok.targets is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["dispatch", "validation"])
async def test_triage_batch_preserves_partial_results(failure_kind: str) -> None:
    from preloop.models.schemas.flow import RunPresetResponse
    from preloop.services.flow_trigger_service import FlowDispatchError

    targets = [_Simple(kind="issue", issue_id=uuid.uuid4()) for _ in range(3)]
    ids = [str(uuid.uuid4()) for _ in targets]
    failure = (
        FlowDispatchError(ids[1], "PENDING", RuntimeError("dispatch transport failed"))
        if failure_kind == "dispatch"
        else ValueError("Flow is unavailable")
    )
    trigger = AsyncMock(
        side_effect=[
            {"id": ids[0], "status": "PENDING"},
            failure,
            {"id": ids[2], "status": "PENDING"},
        ]
    )
    with (
        patch(
            "preloop.services.preset_runner._load_visible_issue",
            return_value=(MagicMock(), MagicMock(), MagicMock()),
        ),
        patch(
            "preloop.services.preset_runner.resolve_or_create_flow",
            return_value=(_account_flow(name="Issue Triage Assistant"), False),
        ),
        patch(
            "preloop.services.preset_runner.build_issue_trigger_payload",
            return_value={},
        ),
        patch(
            "preloop.services.flow_trigger_service.FlowTriggerService.trigger_flow",
            trigger,
        ),
    ):
        response = await run_preset_on_target(
            MagicMock(),
            current_user=_user(uuid.uuid4()),
            preset_slug=TRIAGE_SLUG,
            targets=targets,
            confirm_create=True,
            triggered_by="Jane Doe",
        )
    # Ensure the public response schema does not drop the durable identifier/status.
    result = RunPresetResponse.model_validate(response).model_dump()
    assert trigger.await_count == 3
    assert [item["issue_id"] for item in result["results"]] == [
        str(item.issue_id) for item in targets
    ]
    first, middle, last = result["results"]
    assert first["execution_id"] == ids[0]
    assert last["execution_id"] == ids[2]
    assert middle["error"]
    if failure_kind == "dispatch":
        assert middle["execution_id"] == ids[1]
        assert middle["execution_status"] == "PENDING"
        assert middle["execution_url"] == f"/console/flows/executions/{ids[1]}"
        assert "before retrying" in middle["error"]
        assert "dispatch transport failed" not in middle["error"]
    else:
        assert middle["execution_id"] is None


@pytest.mark.asyncio
async def test_single_issue_run_preserves_execution_on_dispatch_error() -> None:
    from preloop.models.schemas.flow import RunPresetResponse
    from preloop.services.flow_trigger_service import FlowDispatchError

    issue_id = uuid.uuid4()
    execution_id = str(uuid.uuid4())
    flow = _account_flow(name="Issue Triage Assistant")
    trigger = AsyncMock(
        side_effect=FlowDispatchError(
            execution_id, "PENDING", RuntimeError("dispatch transport failed")
        )
    )
    with (
        patch(
            "preloop.services.preset_runner._load_visible_issue",
            return_value=(MagicMock(), MagicMock(), MagicMock()),
        ),
        patch(
            "preloop.services.preset_runner.resolve_or_create_flow",
            return_value=(flow, False),
        ),
        patch(
            "preloop.services.preset_runner.build_issue_trigger_payload",
            return_value={},
        ),
        patch(
            "preloop.services.flow_trigger_service.FlowTriggerService.trigger_flow",
            trigger,
        ),
    ):
        response = await run_preset_on_target(
            MagicMock(),
            current_user=_user(uuid.uuid4()),
            preset_slug=TRIAGE_SLUG,
            target=_Simple(kind="issue", issue_id=issue_id),
            confirm_create=True,
            triggered_by="Jane Doe",
        )
    result = RunPresetResponse.model_validate(response).model_dump()
    assert result["execution_id"] == execution_id
    assert result["execution_url"] == f"/console/flows/executions/{execution_id}"
    item = result["results"][0]
    assert item["issue_id"] == str(issue_id)
    assert item["execution_id"] == execution_id
    assert item["execution_status"] == "PENDING"
    assert "before retrying" in item["error"]
    assert "dispatch transport failed" not in item["error"]


@pytest.mark.asyncio
async def test_triage_batch_skips_flow_create_when_no_issue_loads() -> None:
    from preloop.models.schemas.flow import RunPresetResponse

    targets = [_Simple(kind="issue", issue_id=uuid.uuid4()) for _ in range(2)]
    resolve = MagicMock()
    with (
        patch(
            "preloop.services.preset_runner._load_visible_issue",
            side_effect=PresetRunnerError(404, "Issue not found"),
        ),
        patch(
            "preloop.services.preset_runner.resolve_or_create_flow",
            resolve,
        ),
    ):
        response = await run_preset_on_target(
            MagicMock(),
            current_user=_user(uuid.uuid4()),
            preset_slug=TRIAGE_SLUG,
            targets=targets,
            confirm_create=True,
            triggered_by="Jane Doe",
        )
    resolve.assert_not_called()
    result = RunPresetResponse.model_validate(response).model_dump()
    assert result["execution_id"] is None
    assert result["flow_created"] is False
    assert [item["error"] for item in result["results"]] == [
        "Issue not found",
        "Issue not found",
    ]


@pytest.mark.asyncio
async def test_pull_request_dispatch_failure_returns_typed_warning_receipt() -> None:
    from preloop.models.schemas.flow import RunPresetResponse
    from preloop.services.flow_trigger_service import FlowDispatchError

    execution_id = str(uuid.uuid4())
    project_id = uuid.uuid4()
    flow = _account_flow(name="Pull Request Reviewer")
    trigger = AsyncMock(
        side_effect=FlowDispatchError(
            execution_id, "PENDING", RuntimeError("private transport detail")
        )
    )
    with (
        patch(
            "preloop.services.preset_runner._load_visible_project",
            return_value=(MagicMock(), MagicMock(), MagicMock()),
        ),
        patch(
            "preloop.services.preset_runner.resolve_or_create_flow",
            return_value=(flow, False),
        ),
        patch(
            "preloop.services.preset_runner._fetch_pull_request_detail",
            new_callable=AsyncMock,
            return_value={},
        ),
        patch(
            "preloop.services.preset_runner.build_pull_request_trigger_payload",
            return_value={},
        ),
        patch(
            "preloop.services.flow_trigger_service.FlowTriggerService.trigger_flow",
            trigger,
        ),
    ):
        response = await run_preset_on_target(
            MagicMock(),
            current_user=_user(uuid.uuid4()),
            preset_slug=REVIEWER_SLUG,
            target=_Simple(kind="pull_request", project_id=project_id, number=12),
            confirm_create=True,
            triggered_by="Jane Doe",
        )
    result = RunPresetResponse.model_validate(response).model_dump()
    item = result["results"][0]
    assert item["issue_id"] is None
    assert item["project_id"] == str(project_id)
    assert item["number"] == 12
    assert item["execution_id"] == result["execution_id"] == execution_id
    assert item["execution_status"] == "PENDING"
    assert item["execution_url"] == result["execution_url"]
    assert "before retrying" in item["error"]
    assert "private transport detail" not in item["error"]
