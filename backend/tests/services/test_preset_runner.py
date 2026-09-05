"""Tests for resolve-or-create and issue trigger payloads."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from preloop.services.preset_runner import (
    IMPLEMENTER_SLUG,
    REVIEWER_SLUG,
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


def test_jira_issue_payload_is_rejected():
    issue = MagicMock()
    project = MagicMock()
    tracker = MagicMock()
    tracker.tracker_type = "jira"
    with pytest.raises(PresetRunnerError) as exc:
        build_issue_trigger_payload(issue, project, tracker)
    assert exc.value.status_code == 400
    assert "GitHub and GitLab" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_gitlab_issue_payload_number_alias():
    event = _gitlab_issue_payload()
    resolver = TriggerEventResolver()
    normalized = resolver._normalize_event_data(event)
    attrs = normalized["payload"]["object_attributes"]
    assert attrs["iid"] == 7
    assert attrs["number"] == 7
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
