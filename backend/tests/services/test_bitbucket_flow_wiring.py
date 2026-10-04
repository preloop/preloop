"""Tests for Bitbucket Cloud events in the flow trigger and execution path."""

import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from sqlalchemy.orm import Session

from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from preloop.services.flow_pr_binding import extract_comment_body
from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.services.prompt_resolvers.trigger_event import TriggerEventResolver
from preloop.services.tracker_git_token import resolve_tracker_git_username
from preloop.sync.event_normalizer import normalize_event_type

pytestmark = pytest.mark.asyncio

PR_PAYLOAD = {
    "actor": {"nickname": "dev"},
    "repository": {
        "full_name": "ws/repo",
        "uuid": "{11111111-2222-3333-4444-555555555555}",
        "links": {"html": {"href": "https://bitbucket.org/ws/repo"}},
    },
    "pullrequest": {
        "id": 7,
        "title": "Add parser",
        "description": "Adds a parser for #12",
        "state": "OPEN",
        "author": {"nickname": "dev"},
        "links": {"html": {"href": "https://bitbucket.org/ws/repo/pull-requests/7"}},
        "source": {"branch": {"name": "feature"}, "commit": {"hash": "abc123"}},
        "destination": {"branch": {"name": "main"}},
    },
}


@pytest.fixture
def service() -> FlowTriggerService:
    db = MagicMock(spec=Session)
    filtered = db.query.return_value.filter.return_value
    filtered.filter.return_value = filtered
    filtered.order_by.return_value.first.return_value = None
    return FlowTriggerService(db, MagicMock())


def _event(**overrides):
    event = {
        "source": "bitbucket",
        "type": "pull_request_opened",
        "account_id": str(uuid.uuid4()),
        "tracker_id": str(uuid.uuid4()),
        "payload": PR_PAYLOAD,
    }
    event.update(overrides)
    return event


async def test_resource_repo_and_commit_keys(service: FlowTriggerService) -> None:
    event = _event()
    assert service._extract_resource_key(event) == "bitbucket:ws/repo:pr:7"
    assert service._extract_repo_key(event) == "bitbucket:ws/repo"
    assert service._extract_commit_sha(event) == "abc123"


async def test_project_matched_by_repository_uuid(service: FlowTriggerService) -> None:
    project = SimpleNamespace(
        id="p-1",
        slug="other/name",
        name="renamed",
        identifier="11111111-2222-3333-4444-555555555555",
    )
    with patch(
        "preloop.models.crud.crud_project.get_for_tracker", return_value=[project]
    ):
        assert service._extract_project_id(_event()) == "p-1"


async def test_bot_comment_is_loop_guarded(service: FlowTriggerService) -> None:
    payload = {**PR_PAYLOAD, "actor": {"nickname": "preloop-bot"}}
    event = _event(type="comment_created", payload=payload)
    assert service._is_preloop_triggered_event(event) is True
    human = _event(type="comment_created")
    assert service._is_preloop_triggered_event(human) is False


@patch("preloop.services.flow_trigger_service.asyncio.create_task")
@patch("preloop.services.flow_trigger_service.get_nats_client")
@patch("preloop.services.flow_trigger_service.crud_flow")
async def test_pull_request_created_starts_reviewer_preset(
    mock_crud, mock_nats, mock_create_task, service: FlowTriggerService
) -> None:
    preset_path = (
        Path(__file__).resolve().parents[2]
        / "presets"
        / "002-pull-request-reviewer.yaml"
    )
    preset = yaml.safe_load(preset_path.read_text())
    event_type = normalize_event_type("bitbucket", "pullrequest:created", PR_PAYLOAD)
    assert event_type in preset["trigger_event_types"]

    flow = MagicMock()
    flow.id = uuid.uuid4()
    flow.name = "PR reviewer"
    flow.is_enabled = True
    flow.trigger_config = None
    flow.trigger_event_types = preset["trigger_event_types"]
    flow.trigger_event_source = "tracker"
    flow.flow_feedback_policy = None
    mock_crud.get_by_trigger.return_value = [flow]
    mock_nats.return_value = AsyncMock()

    event = _event(type=event_type)
    with patch.object(service, "_extract_project_id", return_value=None):
        await service.process_event(event)

    assert mock_crud.get_by_trigger.call_args.kwargs["event_type"] == (
        "pull_request_opened"
    )
    mock_create_task.assert_called_once()


async def test_trigger_resolver_builds_object_attributes() -> None:
    resolver = TriggerEventResolver()
    normalized = resolver._normalize_event_data(
        {"source": "bitbucket", "type": "pull_request_opened", "payload": PR_PAYLOAD}
    )
    attrs = normalized["payload"]["object_attributes"]
    assert attrs["title"] == "Add parser"
    assert attrs["url"] == "https://bitbucket.org/ws/repo/pull-requests/7"
    assert attrs["source_branch"] == "feature"
    assert attrs["target_branch"] == "main"
    assert attrs["author"] == "dev"
    assert attrs["number"] == 7
    assert "referenced_issues" in attrs
    # The original event is not mutated.
    assert "object_attributes" not in PR_PAYLOAD


async def test_comment_body_from_bitbucket_payload() -> None:
    event = {"payload": {"comment": {"content": {"raw": "Please re-review"}}}}
    assert extract_comment_body(event) == "Please re-review"


@pytest.mark.parametrize(
    ("tracker", "expected"),
    [
        (SimpleNamespace(tracker_type="github"), None),
        (None, None),
        (
            SimpleNamespace(
                tracker_type="bitbucket",
                auth_type="api_token",
                connection_details={"username": "dev", "email": "dev@example.com"},
            ),
            "dev",
        ),
        (
            SimpleNamespace(
                tracker_type="bitbucket",
                auth_type="oauth_token",
                connection_details={},
            ),
            "x-token-auth",
        ),
    ],
)
async def test_resolve_tracker_git_username(tracker, expected) -> None:
    assert resolve_tracker_git_username(tracker) == expected


@pytest.fixture
def orchestrator() -> FlowExecutionOrchestrator:
    return FlowExecutionOrchestrator(
        db=MagicMock(),
        flow_id="flow",
        trigger_event_data={"source": "bitbucket", "payload": PR_PAYLOAD},
        nats_client=AsyncMock(),
    )


async def test_orchestrator_reads_bitbucket_payload(
    orchestrator: FlowExecutionOrchestrator,
) -> None:
    assert orchestrator._extract_commit_sha() == "abc123"
    assert orchestrator._extract_pr_branch_from_trigger() == "feature"


async def test_commit_status_names_the_pull_request_source_branch(
    orchestrator: FlowExecutionOrchestrator,
) -> None:
    client = MagicMock()
    client.connection_details = {"repository": "ws/repo"}
    client.create_commit_status = AsyncMock(return_value={"state": "SUCCESSFUL"})
    with patch.object(
        orchestrator,
        "_get_tracker_client_for_status",
        AsyncMock(return_value=client),
    ):
        await orchestrator._update_commit_status("success", "Approved")
    client.create_commit_status.assert_awaited_once()
    kwargs = client.create_commit_status.await_args.kwargs
    assert kwargs["sha"] == "abc123"
    assert kwargs["refname"] == "feature"


async def test_orchestrator_credentials_carry_bitbucket_username(
    orchestrator: FlowExecutionOrchestrator,
) -> None:
    tracker = SimpleNamespace(
        tracker_type="bitbucket",
        auth_type="api_token",
        connection_details={"token_kind": "access_token", "repository": "repo"},
    )
    with (
        patch("preloop.models.crud.crud_tracker.get", return_value=tracker),
        patch(
            "preloop.services.flow_orchestrator.resolve_tracker_git_token",
            new=AsyncMock(return_value="tok"),
        ),
    ):
        creds = await orchestrator._get_tracker_credentials_by_id("t-1")
    assert creds["username"] == "x-token-auth"

    credential = orchestrator._build_clone_credential(
        "https://bitbucket.org/ws/repo.git", creds
    )
    assert credential.username == "x-token-auth"
    default = orchestrator._build_clone_credential(
        "https://bitbucket.org/ws/repo.git",
        {"token": "tok", "tracker_type": "bitbucket"},
    )
    assert default.username == "x-bitbucket-api-token-auth"


class TestResolveRepositoryUrlFromTrigger:
    """The repository URL is found in nested and bare trigger payloads."""

    def _orchestrator(self, trigger_event_data) -> FlowExecutionOrchestrator:
        return FlowExecutionOrchestrator(
            db=MagicMock(),
            flow_id="flow",
            trigger_event_data=trigger_event_data,
            nats_client=AsyncMock(),
        )

    def test_nested_payload_shape(self) -> None:
        orch = self._orchestrator({"source": "bitbucket", "payload": PR_PAYLOAD})
        assert (
            orch._resolve_repository_url_from_trigger()
            == "https://bitbucket.org/ws/repo"
        )

    def test_bare_provider_payload_shape(self) -> None:
        orch = self._orchestrator(dict(PR_PAYLOAD))
        assert (
            orch._resolve_repository_url_from_trigger()
            == "https://bitbucket.org/ws/repo"
        )

    def test_clone_url_wins_over_html_link(self) -> None:
        payload = {
            "repository": {
                "clone_url": "https://github.com/acme/app.git",
                "html_url": "https://github.com/acme/app",
            }
        }
        orch = self._orchestrator({"payload": payload})
        assert (
            orch._resolve_repository_url_from_trigger()
            == "https://github.com/acme/app.git"
        )

    def test_gitlab_project_shape(self) -> None:
        payload = {"project": {"http_url_to_repo": "https://gitlab.com/g/app.git"}}
        orch = self._orchestrator({"payload": payload})
        assert (
            orch._resolve_repository_url_from_trigger()
            == "https://gitlab.com/g/app.git"
        )

    def test_no_repository_returns_none(self) -> None:
        orch = self._orchestrator({"payload": {"other": 1}})
        assert orch._resolve_repository_url_from_trigger() is None


class TestPublishedPrLookup:
    """Branch lookup goes through the uniform tracker method, host-free."""

    def test_tracker_kind_from_tracker_type(self) -> None:
        assert (
            FlowExecutionOrchestrator._tracker_kind(
                SimpleNamespace(tracker_type="bitbucket")
            )
            == "bitbucket"
        )
        assert (
            FlowExecutionOrchestrator._tracker_kind(
                SimpleNamespace(tracker_type="GitHub")
            )
            == "github"
        )
        assert (
            FlowExecutionOrchestrator._tracker_kind(
                SimpleNamespace(tracker_type="jira")
            )
            is None
        )

    async def test_lookup_returns_matching_pr(
        self, orchestrator: FlowExecutionOrchestrator
    ) -> None:
        client = SimpleNamespace(
            tracker_type="bitbucket",
            list_open_pull_requests_by_source_branch=AsyncMock(
                return_value={
                    "items": [
                        {"source_branch": "other", "url": "https://x/1"},
                        {
                            "source_branch": "feature",
                            "url": "https://bitbucket.org/ws/repo/pull-requests/7",
                        },
                    ]
                }
            ),
        )
        found = await orchestrator._lookup_published_pr(client, "feature")
        assert found == {
            "url": "https://bitbucket.org/ws/repo/pull-requests/7",
            "branch": "feature",
            "provider": "bitbucket",
        }

    async def test_issue_tracker_client_is_skipped(
        self, orchestrator: FlowExecutionOrchestrator
    ) -> None:
        client = SimpleNamespace(
            tracker_type="jira",
            list_open_pull_requests_by_source_branch=AsyncMock(),
        )
        assert await orchestrator._lookup_published_pr(client, "feature") is None
        client.list_open_pull_requests_by_source_branch.assert_not_awaited()


class TestBitbucketCredentialEmail:
    """The Atlassian email ships only for personal API token trackers."""

    async def _creds(self, orchestrator, details, auth_type="api_token"):
        tracker = SimpleNamespace(
            tracker_type="bitbucket",
            auth_type=auth_type,
            connection_details=details,
        )
        with (
            patch("preloop.models.crud.crud_tracker.get", return_value=tracker),
            patch(
                "preloop.services.flow_orchestrator.resolve_tracker_git_token",
                new=AsyncMock(return_value="tok"),
            ),
        ):
            return await orchestrator._get_tracker_credentials_by_id("t-1")

    async def test_api_token_tracker_ships_email(
        self, orchestrator: FlowExecutionOrchestrator
    ) -> None:
        creds = await self._creds(
            orchestrator, {"email": "dev@example.com", "token_kind": "api_token"}
        )
        assert creds["email"] == "dev@example.com"

    async def test_access_token_tracker_ships_no_email(
        self, orchestrator: FlowExecutionOrchestrator
    ) -> None:
        creds = await self._creds(
            orchestrator,
            {"email": "dev@example.com", "token_kind": "access_token"},
        )
        assert "email" not in creds

    async def test_oauth_tracker_ships_no_email(
        self, orchestrator: FlowExecutionOrchestrator
    ) -> None:
        creds = await self._creds(
            orchestrator,
            {"email": "dev@example.com"},
            auth_type="oauth_token",
        )
        assert "email" not in creds


class TestManagedCloudCredentials:
    """Managed grants (issue #1065) in clone credentials and feedback reads."""

    @staticmethod
    def _managed_tracker():
        return SimpleNamespace(
            id=uuid.uuid4(),
            account_id=uuid.uuid4(),
            name="Bitbucket (managed)",
            tracker_type="bitbucket",
            auth_type="managed_oauth",
            resolved_api_key="",
            url="https://bitbucket.org",
            oauth_installation_id=None,
            connection_details={
                "workspace": "ws",
                "repository": "repo",
                "auth_type": "oauth_token",
                "token_kind": "access_token",
                "managed_oauth": True,
            },
        )

    async def test_clone_credentials_carry_fresh_managed_token(
        self, orchestrator: FlowExecutionOrchestrator
    ) -> None:
        tracker = self._managed_tracker()
        with (
            patch("preloop.models.crud.crud_tracker.get", return_value=tracker),
            patch(
                "preloop.services.flow_orchestrator.resolve_tracker_git_token",
                new=AsyncMock(return_value="managed-token-a"),
            ),
        ):
            creds = await orchestrator._get_tracker_credentials_by_id(str(tracker.id))
        assert creds["token"] == "managed-token-a"
        assert creds["auth_type"] == "managed_oauth"
        assert creds["username"] == "x-token-auth"
        assert "email" not in creds
        credential = orchestrator._build_clone_credential(
            "https://bitbucket.org/ws/repo.git", creds
        )
        assert credential.username == "x-token-auth"
        assert "managed-token-a" not in credential.repo_url

    async def test_reconnect_required_fails_the_run_instead_of_anonymous_clone(
        self, orchestrator: FlowExecutionOrchestrator
    ) -> None:
        from preloop.services.managed_credentials import (
            ManagedReconnectRequiredError,
        )

        tracker = self._managed_tracker()
        with (
            patch("preloop.models.crud.crud_tracker.get", return_value=tracker),
            patch(
                "preloop.services.flow_orchestrator.resolve_tracker_git_token",
                new=AsyncMock(
                    side_effect=ManagedReconnectRequiredError(
                        "invalid_grant", provider="bitbucket"
                    )
                ),
            ),
        ):
            with pytest.raises(RuntimeError) as error:
                await orchestrator._get_tracker_credentials_by_id(str(tracker.id))
            assert "requires reconnect" in str(error.value)
            assert "invalid_grant" in str(error.value)
            # The same failure through the attach path propagates too: no
            # silent "no credentials" warning for a managed grant.
            context = {"git_credentials_map": {}}
            orchestrator.trigger_event_data["tracker_id"] = str(tracker.id)
            with patch.object(
                orchestrator, "_resolve_project_tracker_id", return_value=None
            ):
                with pytest.raises(RuntimeError):
                    await orchestrator._attach_trigger_tracker_credentials(context)
            assert context["git_credentials_map"] == {}

    async def test_feedback_provider_binds_the_grant_for_late_reads(self) -> None:
        from preloop.services.flow_feedback_provider import FeedbackProvider

        tracker = self._managed_tracker()
        thread = SimpleNamespace(
            tracker_id=tracker.id,
            account_id=tracker.account_id,
            provider="bitbucket",
            repository_id="{repo-uuid}",
            pr_number=7,
            policy={},
        )
        db = MagicMock(spec=Session)
        create = AsyncMock(return_value=MagicMock())
        with (
            patch(
                "preloop.services.flow_feedback_provider.crud_tracker.get",
                return_value=tracker,
            ),
            patch(
                "preloop.services.flow_feedback_provider.crud_flow_feedback.release_read"
            ),
            patch(
                "preloop.services.flow_feedback_provider.create_tracker_client",
                new=create,
            ),
        ):
            await FeedbackProvider.for_thread(db, thread)
        args, kwargs = create.await_args
        assert args[0] == "bitbucket"
        assert args[2] == ""  # never a stored key for a managed grant
        assert args[3]["auth_type"] == "managed_oauth"
        source = kwargs["credential_source"]
        assert (source.account_id, source.tracker_id) == (
            tracker.account_id,
            tracker.id,
        )
        assert source.repository == "repo"
