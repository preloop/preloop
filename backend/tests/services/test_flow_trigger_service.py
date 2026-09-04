"""Tests for flow trigger service."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from preloop.services.flow_trigger_service import FlowTriggerService

pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_db():
    """Create a mock database session."""
    return MagicMock(spec=Session)


@pytest.fixture
def mock_session_factory():
    """Create a mock session factory."""
    factory = MagicMock()
    mock_session = MagicMock(spec=Session)
    factory.return_value = mock_session
    return factory


@pytest.fixture
def flow_trigger_service(mock_db, mock_session_factory):
    """Create FlowTriggerService instance."""
    return FlowTriggerService(mock_db, mock_session_factory)


@pytest.fixture
def sample_github_pr_event():
    """Sample GitHub PR event data."""
    return {
        "source": "github",
        "type": "pull_request.opened",
        "account_id": str(uuid.uuid4()),
        "payload": {
            "action": "opened",
            "pull_request": {
                "number": 123,
                "title": "Test PR",
            },
            "repository": {
                "full_name": "owner/repo",
            },
        },
    }


@pytest.fixture
def sample_github_issue_event():
    """Sample GitHub issue event data."""
    return {
        "source": "github",
        "type": "issues.opened",
        "account_id": str(uuid.uuid4()),
        "payload": {
            "action": "opened",
            "issue": {
                "number": 456,
                "title": "Test Issue",
            },
            "repository": {
                "full_name": "owner/repo",
            },
        },
    }


@pytest.fixture
def sample_gitlab_mr_event():
    """Sample GitLab MR event data."""
    return {
        "source": "gitlab",
        "type": "merge_request",
        "account_id": str(uuid.uuid4()),
        "payload": {
            "object_kind": "merge_request",
            "object_attributes": {
                "iid": 789,
                "title": "Test MR",
            },
            "project": {
                "path_with_namespace": "group/project",
            },
        },
    }


@pytest.fixture
def sample_flow():
    """Create a sample flow."""
    flow = MagicMock()
    flow.id = uuid.uuid4()
    flow.name = "Test Flow"
    flow.is_enabled = True
    flow.trigger_config = None
    flow.prompt_template = "Test prompt"
    flow.allowed_mcp_tools = []
    return flow


class TestExtractResourceKey:
    """Tests for _extract_resource_key method."""

    def test_github_pr_resource_key(self, flow_trigger_service, sample_github_pr_event):
        """Test extracting resource key from GitHub PR event."""
        result = flow_trigger_service._extract_resource_key(sample_github_pr_event)

        assert result == "github:owner/repo:pr:123"

    def test_github_issue_resource_key(
        self, flow_trigger_service, sample_github_issue_event
    ):
        """Test extracting resource key from GitHub issue event."""
        result = flow_trigger_service._extract_resource_key(sample_github_issue_event)

        assert result == "github:owner/repo:issue:456"

    def test_gitlab_mr_resource_key(self, flow_trigger_service, sample_gitlab_mr_event):
        """Test extracting resource key from GitLab MR event."""
        result = flow_trigger_service._extract_resource_key(sample_gitlab_mr_event)

        assert result == "gitlab:group/project:merge_request:789"

    def test_unknown_source_returns_none(self, flow_trigger_service):
        """Test that unknown source returns None."""
        event_data = {
            "source": "unknown",
            "payload": {},
        }
        result = flow_trigger_service._extract_resource_key(event_data)

        assert result is None

    def test_missing_pr_number_returns_none(self, flow_trigger_service):
        """Test that missing PR number returns None."""
        event_data = {
            "source": "github",
            "payload": {
                "pull_request": {},
                "repository": {"full_name": "owner/repo"},
            },
        }
        result = flow_trigger_service._extract_resource_key(event_data)

        assert result is None

    def test_missing_repo_returns_none(self, flow_trigger_service):
        """Test that missing repository returns None."""
        event_data = {
            "source": "github",
            "payload": {
                "pull_request": {"number": 123},
                "repository": {},
            },
        }
        result = flow_trigger_service._extract_resource_key(event_data)

        assert result is None

    def test_empty_source(self, flow_trigger_service):
        """Test handling empty source."""
        event_data = {
            "source": "",
            "payload": {},
        }
        result = flow_trigger_service._extract_resource_key(event_data)

        assert result is None


class TestMatchesTriggerConfig:
    """Tests for _matches_trigger_config method."""

    def test_no_trigger_config_always_matches(self, flow_trigger_service, sample_flow):
        """Test that no trigger config always matches."""
        sample_flow.trigger_config = None
        event_data = {"payload": {"status": "opened"}}

        result = flow_trigger_service._matches_trigger_config(sample_flow, event_data)

        assert result is True

    def test_single_value_match(self, flow_trigger_service, sample_flow):
        """Test matching a single value condition."""
        sample_flow.trigger_config = {"status": "opened"}
        event_data = {"payload": {"status": "opened"}}

        result = flow_trigger_service._matches_trigger_config(sample_flow, event_data)

        assert result is True

    def test_single_value_mismatch(self, flow_trigger_service, sample_flow):
        """Test non-matching single value condition."""
        sample_flow.trigger_config = {"status": "closed"}
        event_data = {"payload": {"status": "opened"}}

        result = flow_trigger_service._matches_trigger_config(sample_flow, event_data)

        assert result is False

    def test_list_expected_single_actual_match(self, flow_trigger_service, sample_flow):
        """Test list expected value matching single actual value."""
        sample_flow.trigger_config = {"status": ["opened", "reopened"]}
        event_data = {"payload": {"status": "opened"}}

        result = flow_trigger_service._matches_trigger_config(sample_flow, event_data)

        assert result is True

    def test_list_expected_single_actual_no_match(
        self, flow_trigger_service, sample_flow
    ):
        """Test list expected value not matching single actual value."""
        sample_flow.trigger_config = {"status": ["opened", "reopened"]}
        event_data = {"payload": {"status": "closed"}}

        result = flow_trigger_service._matches_trigger_config(sample_flow, event_data)

        assert result is False

    def test_single_expected_list_actual_match(self, flow_trigger_service, sample_flow):
        """Test single expected value matching list actual value."""
        sample_flow.trigger_config = {"labels": "bug"}
        event_data = {"payload": {"labels": ["bug", "priority"]}}

        result = flow_trigger_service._matches_trigger_config(sample_flow, event_data)

        assert result is True

    def test_single_expected_list_actual_no_match(
        self, flow_trigger_service, sample_flow
    ):
        """Test single expected value not in list actual value."""
        sample_flow.trigger_config = {"labels": "security"}
        event_data = {"payload": {"labels": ["bug", "priority"]}}

        result = flow_trigger_service._matches_trigger_config(sample_flow, event_data)

        assert result is False

    def test_list_expected_list_actual_match(self, flow_trigger_service, sample_flow):
        """Test list expected matching list actual (any match)."""
        sample_flow.trigger_config = {"labels": ["bug", "security"]}
        event_data = {"payload": {"labels": ["bug", "priority"]}}

        result = flow_trigger_service._matches_trigger_config(sample_flow, event_data)

        assert result is True

    def test_list_expected_list_actual_no_match(
        self, flow_trigger_service, sample_flow
    ):
        """Test list expected not matching list actual."""
        sample_flow.trigger_config = {"labels": ["security", "urgent"]}
        event_data = {"payload": {"labels": ["bug", "priority"]}}

        result = flow_trigger_service._matches_trigger_config(sample_flow, event_data)

        assert result is False

    def test_missing_key_in_payload(self, flow_trigger_service, sample_flow):
        """Test that missing key returns False."""
        sample_flow.trigger_config = {"branch": "main"}
        event_data = {"payload": {}}

        result = flow_trigger_service._matches_trigger_config(sample_flow, event_data)

        assert result is False

    def test_multiple_conditions_all_match(self, flow_trigger_service, sample_flow):
        """Test multiple conditions all matching."""
        sample_flow.trigger_config = {"status": "opened", "branch": "main"}
        event_data = {"payload": {"status": "opened", "branch": "main"}}

        result = flow_trigger_service._matches_trigger_config(sample_flow, event_data)

        assert result is True

    def test_multiple_conditions_one_fails(self, flow_trigger_service, sample_flow):
        """Test multiple conditions with one failing."""
        sample_flow.trigger_config = {"status": "opened", "branch": "develop"}
        event_data = {"payload": {"status": "opened", "branch": "main"}}

        result = flow_trigger_service._matches_trigger_config(sample_flow, event_data)

        assert result is False

    def test_filter_conditions_nested_format(self, flow_trigger_service, sample_flow):
        """Test backward-compatible filter_conditions nested format."""
        sample_flow.trigger_config = {
            "assignee": "user1",
            "filter_conditions": {"labels": ["bug"]},
        }
        event_data = {"payload": {"assignee": "user1", "labels": ["bug", "urgent"]}}

        result = flow_trigger_service._matches_trigger_config(sample_flow, event_data)

        assert result is True

    def test_github_issue_labels_objects_match_filter(
        self, flow_trigger_service, sample_flow
    ):
        """Raw GitHub ``issue.labels[].name`` objects satisfy labels filters."""
        from preloop.sync.webhook_payloads import GITHUB_ISSUE_OPENED

        sample_flow.trigger_config = {"filter_conditions": {"labels": ["bug"]}}
        event_data = {"payload": GITHUB_ISSUE_OPENED}

        assert (
            flow_trigger_service._matches_trigger_config(sample_flow, event_data)
            is True
        )

    def test_gitlab_issue_labels_objects_match_filter(
        self, flow_trigger_service, sample_flow
    ):
        """Raw GitLab ``labels[].title`` objects satisfy labels filters."""
        from preloop.sync.webhook_payloads import GITLAB_ISSUE_OPENED

        sample_flow.trigger_config = {"filter_conditions": {"labels": ["API"]}}
        event_data = {"payload": GITLAB_ISSUE_OPENED}

        assert (
            flow_trigger_service._matches_trigger_config(sample_flow, event_data)
            is True
        )

    def test_enriched_filter_fields_labels_still_match(
        self, flow_trigger_service, sample_flow
    ):
        """Webhook path merges extract_filter_fields strings into the payload."""
        from preloop.sync.event_normalizer import extract_filter_fields
        from preloop.sync.webhook_payloads import GITHUB_ISSUE_OPENED

        sample_flow.trigger_config = {"filter_conditions": {"labels": ["bug"]}}
        event_data = {
            "payload": {
                **GITHUB_ISSUE_OPENED,
                **extract_filter_fields("github", "issues", GITHUB_ISSUE_OPENED),
            }
        }

        assert (
            flow_trigger_service._matches_trigger_config(sample_flow, event_data)
            is True
        )


class TestHasRunningExecution:
    """Tests for _has_running_execution method."""

    @patch("preloop.services.flow_trigger_service.crud_flow_execution")
    def test_no_running_executions(self, mock_crud, flow_trigger_service):
        """Test when no running executions exist."""
        mock_crud.get_running_by_flow.return_value = []

        result = flow_trigger_service._has_running_execution(
            flow_id=uuid.uuid4(),
            resource_key="github:owner/repo:pr:123",
            account_id=str(uuid.uuid4()),
        )

        assert result is False

    @patch("preloop.services.flow_trigger_service.crud_flow_execution")
    def test_running_execution_same_resource(self, mock_crud, flow_trigger_service):
        """Test when running execution exists for same resource."""
        mock_execution = MagicMock()
        mock_execution.id = uuid.uuid4()
        mock_execution.status = "RUNNING"
        mock_execution.trigger_event_details = {
            "source": "github",
            "payload": {
                "pull_request": {"number": 123},
                "repository": {"full_name": "owner/repo"},
            },
        }
        mock_crud.get_running_by_flow.return_value = [mock_execution]

        result = flow_trigger_service._has_running_execution(
            flow_id=uuid.uuid4(),
            resource_key="github:owner/repo:pr:123",
            account_id=str(uuid.uuid4()),
        )

        assert result is True

    @patch("preloop.services.flow_trigger_service.crud_flow_execution")
    def test_running_execution_different_resource(
        self, mock_crud, flow_trigger_service
    ):
        """Test when running execution exists for different resource."""
        mock_execution = MagicMock()
        mock_execution.id = uuid.uuid4()
        mock_execution.status = "RUNNING"
        mock_execution.trigger_event_details = {
            "source": "github",
            "payload": {
                "pull_request": {"number": 456},
                "repository": {"full_name": "owner/repo"},
            },
        }
        mock_crud.get_running_by_flow.return_value = [mock_execution]

        result = flow_trigger_service._has_running_execution(
            flow_id=uuid.uuid4(),
            resource_key="github:owner/repo:pr:123",
            account_id=str(uuid.uuid4()),
        )

        assert result is False

    @patch("preloop.services.flow_trigger_service.crud_flow_execution")
    def test_handles_none_trigger_details(self, mock_crud, flow_trigger_service):
        """Test handling execution with None trigger details."""
        mock_execution = MagicMock()
        mock_execution.trigger_event_details = None
        mock_crud.get_running_by_flow.return_value = [mock_execution]

        result = flow_trigger_service._has_running_execution(
            flow_id=uuid.uuid4(),
            resource_key="github:owner/repo:pr:123",
            account_id=str(uuid.uuid4()),
        )

        assert result is False


class TestProcessEvent:
    """Tests for process_event method."""

    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    async def test_process_event_missing_source(
        self, mock_crud, mock_nats, flow_trigger_service
    ):
        """Test handling event with missing source."""
        event_data = {"type": "test", "account_id": "123"}

        await flow_trigger_service.process_event(event_data)

        # Should return early without querying flows
        mock_crud.get_by_trigger.assert_not_called()

    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    async def test_process_event_missing_type(
        self, mock_crud, mock_nats, flow_trigger_service
    ):
        """Test handling event with missing type."""
        event_data = {"source": "github", "account_id": "123"}

        await flow_trigger_service.process_event(event_data)

        mock_crud.get_by_trigger.assert_not_called()

    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    async def test_process_event_no_matching_flows(
        self, mock_crud, mock_nats, flow_trigger_service
    ):
        """Test handling event with no matching flows."""
        mock_crud.get_by_trigger.return_value = []

        event_data = {
            "source": "github",
            "type": "push",
            "account_id": str(uuid.uuid4()),
        }

        await flow_trigger_service.process_event(event_data)

        mock_crud.get_by_trigger.assert_called_once()

    @patch("preloop.services.flow_trigger_service.asyncio.create_task")
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    async def test_process_event_triggers_enabled_flow(
        self, mock_crud, mock_nats, mock_create_task, flow_trigger_service, sample_flow
    ):
        """Test that enabled flows are triggered."""
        mock_nats.return_value = AsyncMock()
        mock_crud.get_by_trigger.return_value = [sample_flow]

        event_data = {
            "source": "github",
            "type": "push",
            "account_id": str(uuid.uuid4()),
            "payload": {},
        }

        await flow_trigger_service.process_event(event_data)

        mock_create_task.assert_called_once()

    @patch("preloop.services.flow_trigger_service.asyncio.create_task")
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    async def test_process_event_skips_disabled_flow(
        self, mock_crud, mock_nats, mock_create_task, flow_trigger_service, sample_flow
    ):
        """Test that disabled flows are skipped."""
        sample_flow.is_enabled = False
        mock_nats.return_value = AsyncMock()
        mock_crud.get_by_trigger.return_value = [sample_flow]

        event_data = {
            "source": "github",
            "type": "push",
            "account_id": str(uuid.uuid4()),
            "payload": {},
        }

        await flow_trigger_service.process_event(event_data)

        mock_create_task.assert_not_called()

    @patch("preloop.services.flow_trigger_service.asyncio.create_task")
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    async def test_process_event_skips_non_matching_trigger_config(
        self, mock_crud, mock_nats, mock_create_task, flow_trigger_service, sample_flow
    ):
        """Test that flows with non-matching trigger_config are skipped."""
        sample_flow.trigger_config = {"branch": "develop"}
        mock_nats.return_value = AsyncMock()
        mock_crud.get_by_trigger.return_value = [sample_flow]

        event_data = {
            "source": "github",
            "type": "push",
            "account_id": str(uuid.uuid4()),
            "payload": {"branch": "main"},
        }

        await flow_trigger_service.process_event(event_data)

        mock_create_task.assert_not_called()

    @patch("preloop.services.flow_trigger_service.asyncio.create_task")
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    @patch.object(FlowTriggerService, "_find_running_execution_for_commit")
    async def test_process_event_skips_duplicate_execution(
        self,
        mock_has_commit,
        mock_crud,
        mock_nats,
        mock_create_task,
        flow_trigger_service,
        sample_github_pr_event,
        sample_flow,
    ):
        """Test that duplicate executions are skipped when same repo+commit."""
        mock_has_commit.return_value = MagicMock()  # an existing execution
        mock_nats.return_value = AsyncMock()
        mock_crud.get_by_trigger.return_value = [sample_flow]

        # Add a commit SHA so the dedup path is triggered
        sample_github_pr_event["payload"]["pull_request"]["head"] = {
            "sha": "abc123def456"
        }

        await flow_trigger_service.process_event(sample_github_pr_event)

        mock_create_task.assert_not_called()

    @patch("preloop.services.flow_trigger_service.asyncio.create_task")
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    @patch("preloop.services.flow_pr_binding.bind_resume_or_skip")
    async def test_comment_on_opened_pr_resumes(
        self,
        mock_bind,
        mock_crud,
        mock_nats,
        mock_create_task,
        flow_trigger_service,
        sample_flow,
    ):
        sample_flow.trigger_event_types = ["issue_labeled", "comment_created"]
        mock_bind.return_value = {
            "execution_id": "prior",
            "pr_url": "https://github.com/preloop/preloop/pull/353",
            "source_branch": "feat/x",
        }
        mock_nats.return_value = AsyncMock()
        mock_crud.get_by_trigger.return_value = [sample_flow]
        event = {
            "source": "github",
            "type": "comment_created",
            "account_id": str(uuid.uuid4()),
            "payload": {
                "issue": {
                    "pull_request": {
                        "html_url": "https://github.com/preloop/preloop/pull/353"
                    }
                }
            },
        }

        await flow_trigger_service.process_event(event)

        mock_bind.assert_called_once()
        mock_create_task.assert_called_once()

    @patch("preloop.services.flow_trigger_service.asyncio.create_task")
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    async def test_unmatched_pr_comment_does_not_start_run(
        self,
        mock_crud,
        mock_nats,
        mock_create_task,
        flow_trigger_service,
        sample_flow,
    ):
        sample_flow.trigger_event_types = ["issue_labeled", "comment_created"]
        mock_nats.return_value = AsyncMock()
        mock_crud.get_by_trigger.return_value = [sample_flow]
        event = {
            "source": "github",
            "type": "comment_created",
            "account_id": str(uuid.uuid4()),
            "payload": {
                "issue": {
                    "number": 1,
                    "html_url": "https://github.com/preloop/preloop/issues/1",
                }
            },
        }

        await flow_trigger_service.process_event(event)

        mock_create_task.assert_not_called()


class TestProcessEventReleaseDedupe:
    """process_event must coalesce duplicate release events (issue #241).

    Release events reach process_event via /private/webhooks -> NATS with no
    commit SHA, so only the fallback resource-key dedup can catch retries.
    """

    @staticmethod
    def _release_event(tag: str) -> dict:
        return {
            "source": "github",
            "type": "release",
            "account_id": str(uuid.uuid4()),
            "payload": {
                "action": "published",
                "release": {"tag_name": tag},
                "repository": {"full_name": "owner/repo"},
            },
        }

    @patch("preloop.services.flow_trigger_service.asyncio.create_task")
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    @patch.object(FlowTriggerService, "_find_running_execution_for_resource_key")
    async def test_duplicate_release_event_is_skipped(
        self,
        mock_find_resource,
        mock_crud,
        mock_nats,
        mock_create_task,
        flow_trigger_service,
        sample_flow,
    ):
        """A running execution for the same release tag must not retrigger."""
        mock_find_resource.return_value = MagicMock()  # existing execution
        mock_nats.return_value = AsyncMock()
        mock_crud.get_by_trigger.return_value = [sample_flow]

        await flow_trigger_service.process_event(self._release_event("v1.2.3"))

        mock_create_task.assert_not_called()
        assert mock_find_resource.call_count == 1
        assert mock_find_resource.call_args[0][1] == "github:owner/repo:release:v1.2.3"

    @patch("preloop.services.flow_trigger_service.asyncio.create_task")
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    @patch.object(FlowTriggerService, "_find_running_execution_for_resource_key")
    async def test_different_release_tag_still_triggers(
        self,
        mock_find_resource,
        mock_crud,
        mock_nats,
        mock_create_task,
        flow_trigger_service,
        sample_flow,
    ):
        """A new release tag must trigger a fresh execution."""
        mock_find_resource.return_value = None
        mock_nats.return_value = AsyncMock()
        mock_crud.get_by_trigger.return_value = [sample_flow]

        await flow_trigger_service.process_event(self._release_event("v2.0.0"))

        mock_create_task.assert_called_once()
        assert mock_find_resource.call_args[0][1] == "github:owner/repo:release:v2.0.0"


class TestTriggerFlow:
    """Tests for trigger_flow method."""

    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow_execution")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    async def test_trigger_flow_not_found(
        self, mock_crud_flow, mock_crud_exec, mock_nats, flow_trigger_service
    ):
        """Test triggering a flow that doesn't exist."""
        mock_crud_flow.get.return_value = None

        with pytest.raises(ValueError, match="not found"):
            await flow_trigger_service.trigger_flow(uuid.uuid4())

    @patch("preloop.models.db.session.get_db_session")
    @patch("preloop.services.flow_trigger_service.FlowExecutionOrchestrator")
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow_execution")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    async def test_trigger_flow_creates_execution(
        self,
        mock_crud_flow,
        mock_crud_exec,
        mock_nats,
        mock_orchestrator_class,
        mock_get_db,
        flow_trigger_service,
        sample_flow,
    ):
        """Test that trigger_flow creates an execution record."""
        mock_crud_flow.get.return_value = sample_flow
        mock_nats.return_value = AsyncMock()

        # Mock execution creation
        mock_execution = MagicMock()
        mock_execution.id = uuid.uuid4()
        mock_execution.status = "PENDING"
        mock_crud_exec.create.return_value = mock_execution
        mock_crud_exec.get.return_value = mock_execution

        # Mock db session generator
        mock_session = MagicMock()
        mock_get_db.return_value = iter([mock_session])

        result = await flow_trigger_service.trigger_flow(sample_flow.id, test_mode=True)

        assert "id" in result
        assert result["status"] == "PENDING"
        mock_crud_exec.create.assert_called_once()

    @patch("preloop.models.db.session.get_db_session")
    @patch("preloop.services.flow_trigger_service.FlowExecutionOrchestrator")
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow_execution")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    async def test_trigger_flow_includes_test_mode_in_details(
        self,
        mock_crud_flow,
        mock_crud_exec,
        mock_nats,
        mock_orchestrator_class,
        mock_get_db,
        flow_trigger_service,
        sample_flow,
    ):
        """Test that trigger_flow includes test_mode in trigger details."""
        mock_crud_flow.get.return_value = sample_flow
        mock_nats.return_value = AsyncMock()

        mock_execution = MagicMock()
        mock_execution.id = uuid.uuid4()
        mock_execution.status = "PENDING"
        mock_crud_exec.create.return_value = mock_execution
        mock_crud_exec.get.return_value = mock_execution

        mock_session = MagicMock()
        mock_get_db.return_value = iter([mock_session])

        await flow_trigger_service.trigger_flow(sample_flow.id, test_mode=True)

        # Check that the execution was created with test_mode in details
        create_call = mock_crud_exec.create.call_args
        execution_data = create_call[1]["obj_in"]
        assert execution_data.trigger_event_details["test_mode"] is True

    @patch("preloop.models.db.session.get_db_session")
    @patch("preloop.services.flow_trigger_service.FlowExecutionOrchestrator")
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow_execution")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    async def test_trigger_flow_merges_custom_event_data(
        self,
        mock_crud_flow,
        mock_crud_exec,
        mock_nats,
        mock_orchestrator_class,
        mock_get_db,
        flow_trigger_service,
        sample_flow,
    ):
        """Test triggering a flow with custom event data merges correctly."""
        mock_crud_flow.get.return_value = sample_flow
        mock_nats.return_value = AsyncMock()

        mock_execution = MagicMock()
        mock_execution.id = uuid.uuid4()
        mock_execution.status = "PENDING"
        mock_crud_exec.create.return_value = mock_execution
        mock_crud_exec.get.return_value = mock_execution

        mock_session = MagicMock()
        mock_get_db.return_value = iter([mock_session])

        custom_event = {"source": "manual", "payload": {"test": True}}
        await flow_trigger_service.trigger_flow(
            sample_flow.id,
            test_mode=True,
            trigger_event_data=custom_event,
        )

        # Verify execution was created with merged trigger details
        create_call = mock_crud_exec.create.call_args
        execution_data = create_call[1]["obj_in"]
        assert execution_data.trigger_event_details["test_mode"] is True
        assert execution_data.trigger_event_details["source"] == "manual"


class TestCreateOrchestratorSession:
    """Tests for _create_orchestrator_session method."""

    def test_creates_new_session(self, flow_trigger_service, mock_session_factory):
        """Test that a new session is created."""
        result = flow_trigger_service._create_orchestrator_session()

        mock_session_factory.assert_called_once()
        assert result == mock_session_factory.return_value


class TestExtractResourceKeyEdgeCases:
    """Additional edge case tests for _extract_resource_key method."""

    def test_gitlab_issue_resource_key(self, flow_trigger_service):
        """Test extracting resource key from GitLab issue event."""
        event_data = {
            "source": "gitlab",
            "payload": {
                "object_kind": "issue",
                "object_attributes": {
                    "iid": 42,
                },
                "project": {
                    "path_with_namespace": "company/project",
                },
            },
        }
        result = flow_trigger_service._extract_resource_key(event_data)
        assert result == "gitlab:company/project:issue:42"

    def test_gitlab_missing_iid_returns_none(self, flow_trigger_service):
        """Test that missing GitLab iid returns None."""
        event_data = {
            "source": "gitlab",
            "payload": {
                "object_kind": "merge_request",
                "object_attributes": {},
                "project": {
                    "path_with_namespace": "group/project",
                },
            },
        }
        result = flow_trigger_service._extract_resource_key(event_data)
        assert result is None

    def test_gitlab_missing_project_path_returns_none(self, flow_trigger_service):
        """Test that missing project path returns None."""
        event_data = {
            "source": "gitlab",
            "payload": {
                "object_kind": "merge_request",
                "object_attributes": {"iid": 123},
                "project": {},
            },
        }
        result = flow_trigger_service._extract_resource_key(event_data)
        assert result is None

    def test_gitlab_missing_project_returns_none(self, flow_trigger_service):
        """Test that missing project object returns None."""
        event_data = {
            "source": "gitlab",
            "payload": {
                "object_kind": "merge_request",
                "object_attributes": {"iid": 123},
            },
        }
        result = flow_trigger_service._extract_resource_key(event_data)
        assert result is None

    def test_github_empty_full_name_returns_none(self, flow_trigger_service):
        """Test that empty full_name returns None."""
        event_data = {
            "source": "github",
            "payload": {
                "pull_request": {"number": 123},
                "repository": {"full_name": ""},
            },
        }
        result = flow_trigger_service._extract_resource_key(event_data)
        assert result is None

    def test_github_pr_zero_number(self, flow_trigger_service):
        """Test that PR number 0 is treated as falsy and returns None."""
        event_data = {
            "source": "github",
            "payload": {
                "pull_request": {"number": 0},
                "repository": {"full_name": "owner/repo"},
            },
        }
        result = flow_trigger_service._extract_resource_key(event_data)
        # Number 0 is falsy in Python, should return None
        assert result is None

    def test_case_insensitive_source(self, flow_trigger_service):
        """Test that source comparison is case-insensitive."""
        event_data = {
            "source": "GITHUB",
            "payload": {
                "pull_request": {"number": 123},
                "repository": {"full_name": "owner/repo"},
            },
        }
        result = flow_trigger_service._extract_resource_key(event_data)
        assert result == "github:owner/repo:pr:123"

    def test_github_prefers_pr_over_issue(self, flow_trigger_service):
        """Test that PR is preferred over issue when both present."""
        event_data = {
            "source": "github",
            "payload": {
                "pull_request": {"number": 100},
                "issue": {"number": 200},
                "repository": {"full_name": "owner/repo"},
            },
        }
        result = flow_trigger_service._extract_resource_key(event_data)
        assert result == "github:owner/repo:pr:100"


class TestMatchesTriggerConfigEdgeCases:
    """Additional edge case tests for _matches_trigger_config method."""

    def test_empty_trigger_config_matches(self, flow_trigger_service, sample_flow):
        """Test that empty trigger config matches."""
        sample_flow.trigger_config = {}
        event_data = {"payload": {"status": "opened"}}

        result = flow_trigger_service._matches_trigger_config(sample_flow, event_data)

        assert result is True

    def test_actual_value_is_empty_string(self, flow_trigger_service, sample_flow):
        """Test matching when actual value is empty string."""
        sample_flow.trigger_config = {"branch": ""}
        event_data = {"payload": {"branch": ""}}

        result = flow_trigger_service._matches_trigger_config(sample_flow, event_data)

        assert result is True

    def test_actual_value_is_zero(self, flow_trigger_service, sample_flow):
        """Test matching when actual value is zero (falsy but not None)."""
        sample_flow.trigger_config = {"priority": 0}
        event_data = {"payload": {"priority": 0}}

        result = flow_trigger_service._matches_trigger_config(sample_flow, event_data)

        assert result is True

    def test_actual_value_is_false(self, flow_trigger_service, sample_flow):
        """Test matching when actual value is False (falsy but not None)."""
        sample_flow.trigger_config = {"draft": False}
        event_data = {"payload": {"draft": False}}

        result = flow_trigger_service._matches_trigger_config(sample_flow, event_data)

        assert result is True

    def test_expected_empty_list_with_empty_actual_list(
        self, flow_trigger_service, sample_flow
    ):
        """Test empty list expected matching empty list actual."""
        sample_flow.trigger_config = {"labels": []}
        event_data = {"payload": {"labels": []}}

        result = flow_trigger_service._matches_trigger_config(sample_flow, event_data)

        # Empty list matches - no items required
        assert result is False  # Empty expected list means nothing can match

    def test_nested_filter_conditions_multiple_keys(
        self, flow_trigger_service, sample_flow
    ):
        """Test nested filter_conditions with multiple keys."""
        sample_flow.trigger_config = {
            "assignee": "user1",
            "filter_conditions": {
                "labels": ["bug"],
                "milestone": "v1.0",
            },
        }
        event_data = {
            "payload": {
                "assignee": "user1",
                "labels": ["bug", "critical"],
                "milestone": "v1.0",
            }
        }

        result = flow_trigger_service._matches_trigger_config(sample_flow, event_data)

        assert result is True

    def test_nested_filter_conditions_partial_match_fails(
        self, flow_trigger_service, sample_flow
    ):
        """Test that partial match in nested filter_conditions fails."""
        sample_flow.trigger_config = {
            "assignee": "user1",
            "filter_conditions": {
                "labels": ["bug"],
                "milestone": "v2.0",
            },
        }
        event_data = {
            "payload": {
                "assignee": "user1",
                "labels": ["bug"],
                "milestone": "v1.0",  # Wrong milestone
            }
        }

        result = flow_trigger_service._matches_trigger_config(sample_flow, event_data)

        assert result is False


class TestHasRunningExecutionEdgeCases:
    """Additional edge case tests for _has_running_execution method."""

    @patch("preloop.services.flow_trigger_service.crud_flow_execution")
    def test_handles_empty_trigger_details_dict(self, mock_crud, flow_trigger_service):
        """Test handling execution with empty trigger details dict."""
        mock_execution = MagicMock()
        mock_execution.trigger_event_details = {}
        mock_crud.get_running_by_flow.return_value = [mock_execution]

        result = flow_trigger_service._has_running_execution(
            flow_id=uuid.uuid4(),
            resource_key="github:owner/repo:pr:123",
            account_id=str(uuid.uuid4()),
        )

        assert result is False

    @patch("preloop.services.flow_trigger_service.crud_flow_execution")
    def test_handles_missing_payload_in_trigger_details(
        self, mock_crud, flow_trigger_service
    ):
        """Test handling execution with missing payload in trigger details."""
        mock_execution = MagicMock()
        mock_execution.trigger_event_details = {"source": "github"}
        mock_crud.get_running_by_flow.return_value = [mock_execution]

        result = flow_trigger_service._has_running_execution(
            flow_id=uuid.uuid4(),
            resource_key="github:owner/repo:pr:123",
            account_id=str(uuid.uuid4()),
        )

        assert result is False

    @patch("preloop.services.flow_trigger_service.crud_flow_execution")
    def test_account_id_as_uuid_object(self, mock_crud, flow_trigger_service):
        """Test that account_id can be passed as UUID object."""
        mock_crud.get_running_by_flow.return_value = []
        account_uuid = uuid.uuid4()

        result = flow_trigger_service._has_running_execution(
            flow_id=uuid.uuid4(),
            resource_key="github:owner/repo:pr:123",
            account_id=account_uuid,  # Pass as UUID, not string
        )

        assert result is False
        # Verify the UUID was passed correctly
        call_args = mock_crud.get_running_by_flow.call_args
        assert call_args.kwargs["account_id"] == account_uuid

    @patch("preloop.services.flow_trigger_service.crud_flow_execution")
    def test_multiple_running_executions_only_one_matches(
        self, mock_crud, flow_trigger_service
    ):
        """Test with multiple running executions where only one matches."""
        # Execution 1 - different resource
        mock_execution1 = MagicMock()
        mock_execution1.id = uuid.uuid4()
        mock_execution1.status = "RUNNING"
        mock_execution1.trigger_event_details = {
            "source": "github",
            "payload": {
                "pull_request": {"number": 456},
                "repository": {"full_name": "owner/repo"},
            },
        }

        # Execution 2 - matching resource
        mock_execution2 = MagicMock()
        mock_execution2.id = uuid.uuid4()
        mock_execution2.status = "RUNNING"
        mock_execution2.trigger_event_details = {
            "source": "github",
            "payload": {
                "pull_request": {"number": 123},
                "repository": {"full_name": "owner/repo"},
            },
        }

        mock_crud.get_running_by_flow.return_value = [mock_execution1, mock_execution2]

        result = flow_trigger_service._has_running_execution(
            flow_id=uuid.uuid4(),
            resource_key="github:owner/repo:pr:123",
            account_id=str(uuid.uuid4()),
        )

        assert result is True


class TestProcessEventEdgeCases:
    """Additional edge case tests for process_event method."""

    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    async def test_process_event_handles_exception_in_flow_query(
        self, mock_crud, mock_nats, flow_trigger_service
    ):
        """Test that exceptions in flow query are handled."""
        mock_crud.get_by_trigger.side_effect = Exception("Database error")

        event_data = {
            "source": "github",
            "type": "push",
            "account_id": str(uuid.uuid4()),
            "payload": {},
        }

        # Should not raise
        await flow_trigger_service.process_event(event_data)

    @patch("preloop.services.flow_trigger_service.asyncio.create_task")
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    async def test_process_event_triggers_multiple_matching_flows(
        self,
        mock_crud,
        mock_nats,
        mock_create_task,
        flow_trigger_service,
    ):
        """Test that multiple matching flows are all triggered."""
        mock_nats.return_value = AsyncMock()

        # Create two matching flows
        flow1 = MagicMock()
        flow1.id = uuid.uuid4()
        flow1.name = "Flow 1"
        flow1.is_enabled = True
        flow1.trigger_config = None

        flow2 = MagicMock()
        flow2.id = uuid.uuid4()
        flow2.name = "Flow 2"
        flow2.is_enabled = True
        flow2.trigger_config = None

        mock_crud.get_by_trigger.return_value = [flow1, flow2]

        event_data = {
            "source": "github",
            "type": "push",
            "account_id": str(uuid.uuid4()),
            "payload": {},
        }

        await flow_trigger_service.process_event(event_data)

        # Both flows should be triggered
        assert mock_create_task.call_count == 2

    @patch("preloop.services.flow_trigger_service.asyncio.create_task")
    @patch("preloop.services.flow_trigger_service.get_nats_client")
    @patch("preloop.services.flow_trigger_service.crud_flow")
    async def test_process_event_continues_after_single_flow_error(
        self,
        mock_crud,
        mock_nats,
        mock_create_task,
        flow_trigger_service,
    ):
        """Test that processing continues even if one flow raises an error."""
        mock_nats.return_value = AsyncMock()

        # Create two flows - first will error, second should still trigger
        flow1 = MagicMock()
        flow1.id = uuid.uuid4()
        flow1.name = "Flow 1"
        flow1.is_enabled = True
        flow1.trigger_config = None

        flow2 = MagicMock()
        flow2.id = uuid.uuid4()
        flow2.name = "Flow 2"
        flow2.is_enabled = True
        flow2.trigger_config = None

        mock_crud.get_by_trigger.return_value = [flow1, flow2]

        # First call raises exception, second succeeds
        mock_create_task.side_effect = [Exception("Error"), None]

        event_data = {
            "source": "github",
            "type": "push",
            "account_id": str(uuid.uuid4()),
            "payload": {},
        }

        # Should not raise - should handle error and continue
        await flow_trigger_service.process_event(event_data)

        # Both flows should have been attempted
        assert mock_create_task.call_count == 2


class TestIsPreloopTriggeredEvent:
    """Bot-loop guard: reaction events from Preloop bots are dropped,
    but intentional PR/MR opens, label hops, and human events pass."""

    # -- Bot reaction events: MUST be blocked --

    def test_github_bot_comment_is_ignored(self, flow_trigger_service):
        """Bot-posted comments are side-effects and must be dropped."""
        event = {
            "source": "github",
            "type": "comment_created",
            "payload": {"sender": {"login": "preloop[bot]"}},
        }
        assert flow_trigger_service._is_preloop_triggered_event(event) is True

    def test_gitlab_bot_issue_updated_is_ignored(self, flow_trigger_service):
        """Bot-edited issue bodies are side-effects and must be dropped."""
        event = {
            "source": "gitlab",
            "type": "issue_updated",
            "payload": {"user": {"username": "preloop"}},
        }
        assert flow_trigger_service._is_preloop_triggered_event(event) is True

    def test_github_bot_pr_updated_is_ignored(self, flow_trigger_service):
        """Bot pushing status/body edits on an existing PR: drop."""
        event = {
            "source": "github",
            "type": "pull_request_updated",
            "payload": {"sender": {"login": "preloop[bot]"}},
        }
        assert flow_trigger_service._is_preloop_triggered_event(event) is True

    # -- Bot-opened PR events: MUST pass (the fix for #306/#307) --

    def test_github_bot_pr_opened_passes(self, flow_trigger_service):
        """PR opened by the Preloop App is an intentional action, not a
        loop vector.  This is the core fix for PRs #306 and #307."""
        event = {
            "source": "github",
            "type": "pull_request_opened",
            "payload": {"sender": {"login": "preloop[bot]"}},
        }
        assert flow_trigger_service._is_preloop_triggered_event(event) is False

    def test_github_bot_pr_reopened_passes(self, flow_trigger_service):
        event = {
            "source": "github",
            "type": "pull_request_reopened",
            "payload": {"sender": {"login": "preloop[bot]"}},
        }
        assert flow_trigger_service._is_preloop_triggered_event(event) is False

    def test_gitlab_bot_mr_opened_passes(self, flow_trigger_service):
        event = {
            "source": "gitlab",
            "type": "merge_request_opened",
            "payload": {"user": {"username": "preloop"}},
        }
        assert flow_trigger_service._is_preloop_triggered_event(event) is False

    def test_loop_guard_exempt_types_are_underscore_forms(self, flow_trigger_service):
        """Dotted GitHub-style names never match; keep only normalized forms."""
        exempt = flow_trigger_service._LOOP_GUARD_EXEMPT_EVENT_TYPES
        assert "pull_request.opened" not in exempt
        assert "pull_request.reopened" not in exempt
        assert "merge_request.opened" not in exempt
        assert "merge_request.reopened" not in exempt
        assert exempt == frozenset(
            {
                "pull_request_opened",
                "pull_request_reopened",
                "merge_request_opened",
                "merge_request_reopened",
            }
        )

    # -- Label hop events: MUST pass --

    def test_github_bot_issue_labeled_is_not_ignored(self, flow_trigger_service):
        event = {
            "source": "github",
            "type": "issue_labeled",
            "payload": {
                "sender": {"login": "preloop[bot]"},
                "label": {"name": "agent-ready"},
            },
        }
        assert flow_trigger_service._is_preloop_triggered_event(event) is False

    def test_gitlab_bot_issue_labeled_is_not_ignored(self, flow_trigger_service):
        event = {
            "source": "gitlab",
            "type": "issue_labeled",
            "payload": {"user": {"username": "preloop"}},
        }
        assert flow_trigger_service._is_preloop_triggered_event(event) is False

    # -- Human events: MUST always pass --

    def test_human_issue_labeled_is_not_ignored(self, flow_trigger_service):
        event = {
            "source": "github",
            "type": "issue_labeled",
            "payload": {"sender": {"login": "dimitris"}},
        }
        assert flow_trigger_service._is_preloop_triggered_event(event) is False

    def test_human_comment_passes(self, flow_trigger_service):
        """Human comments must never be blocked."""
        event = {
            "source": "github",
            "type": "comment_created",
            "payload": {"sender": {"login": "dimitris"}},
        }
        assert flow_trigger_service._is_preloop_triggered_event(event) is False

    def test_human_pr_opened_passes(self, flow_trigger_service):
        event = {
            "source": "github",
            "type": "pull_request.opened",
            "payload": {"sender": {"login": "dimitris"}},
        }
        assert flow_trigger_service._is_preloop_triggered_event(event) is False

    # -- Lookalike usernames: MUST pass (no prefix matching) --

    def test_lookalike_username_preloop_fan_passes(self, flow_trigger_service):
        """A human named 'preloop-fan' must not be caught by the guard.
        The old startswith('preloop') check would incorrectly drop this."""
        event = {
            "source": "github",
            "type": "comment_created",
            "payload": {"sender": {"login": "preloop-fan"}},
        }
        assert flow_trigger_service._is_preloop_triggered_event(event) is False

    def test_lookalike_username_prelooper_passes(self, flow_trigger_service):
        """Username 'prelooper' must not be caught."""
        event = {
            "source": "github",
            "type": "pull_request_updated",
            "payload": {"sender": {"login": "prelooper"}},
        }
        assert flow_trigger_service._is_preloop_triggered_event(event) is False

    def test_lookalike_gitlab_username_passes(self, flow_trigger_service):
        """GitLab user 'preloop-contributor' must not be caught."""
        event = {
            "source": "gitlab",
            "type": "issue_updated",
            "payload": {"user": {"username": "preloop-contributor"}},
        }
        assert flow_trigger_service._is_preloop_triggered_event(event) is False


class TestExtractResourceKeyRelease:
    """Release events must produce a resource key (issue #241)."""

    def test_github_release_resource_key(self, flow_trigger_service):
        event = {
            "source": "github",
            "type": "release",
            "payload": {
                "action": "published",
                "release": {"tag_name": "v1.2.3"},
                "repository": {"full_name": "owner/repo"},
            },
        }
        result = flow_trigger_service._extract_resource_key(event)
        assert result == "github:owner/repo:release:v1.2.3"

    def test_github_release_missing_tag_returns_none(self, flow_trigger_service):
        event = {
            "source": "github",
            "payload": {
                "release": {},
                "repository": {"full_name": "owner/repo"},
            },
        }
        assert flow_trigger_service._extract_resource_key(event) is None

    def test_gitlab_release_resource_key(self, flow_trigger_service):
        """Real GitLab Release Hook shape: tag is a top-level field."""
        event = {
            "source": "gitlab",
            "object_kind": "release",
            "payload": {
                "object_kind": "release",
                "tag": "v2.0.0",
                "commit": {"id": "abcdef1234567890"},
                "project": {"path_with_namespace": "group/project"},
            },
        }
        result = flow_trigger_service._extract_resource_key(event)
        assert result == "gitlab:group/project:release:v2.0.0"

    def test_gitlab_release_missing_project_path_returns_none(
        self, flow_trigger_service
    ):
        event = {
            "source": "gitlab",
            "payload": {
                "object_kind": "release",
                "tag": "v2.0.0",
            },
        }
        assert flow_trigger_service._extract_resource_key(event) is None


class TestResolveJsonPath:
    """Tests for the dotted-path resolver used by webhook dedupe."""

    def test_simple_dict_path(self, flow_trigger_service):
        assert flow_trigger_service._resolve_json_path({"a": {"b": 1}}, "a.b") == 1

    def test_numeric_index_into_list(self, flow_trigger_service):
        payload = {"attachments": [{"title_link": "https://example.com/x"}]}
        assert (
            flow_trigger_service._resolve_json_path(payload, "attachments.0.title_link")
            == "https://example.com/x"
        )

    def test_missing_key_returns_none(self, flow_trigger_service):
        assert flow_trigger_service._resolve_json_path({"a": {}}, "a.b") is None

    def test_out_of_range_index_returns_none(self, flow_trigger_service):
        assert flow_trigger_service._resolve_json_path([], "0.x") is None

    def test_non_numeric_index_into_list_returns_none(self, flow_trigger_service):
        assert flow_trigger_service._resolve_json_path({"a": [1]}, "a.b") is None

    def test_scalar_traversal_returns_none(self, flow_trigger_service):
        assert flow_trigger_service._resolve_json_path({"a": "str"}, "a.b") is None


class TestExtractWebhookResourceKey:
    """Webhook dedupe key extraction (issue #241)."""

    def _flow(self, webhook_config=None):
        flow = MagicMock()
        flow.webhook_config = webhook_config or {}
        return flow

    def test_default_glitchtip_title_link(self, flow_trigger_service):
        flow = self._flow()
        payload = {
            "text": "alert",
            "attachments": [{"title_link": "https://glitchtip/issues/42"}],
        }
        result = flow_trigger_service._extract_webhook_resource_key(flow, payload)
        assert result == "webhook:attachments.0.title_link=https://glitchtip/issues/42"

    def test_default_falls_back_to_sentry_issue_id(self, flow_trigger_service):
        flow = self._flow()
        payload = {"data": {"issue": {"id": 99}}}
        result = flow_trigger_service._extract_webhook_resource_key(flow, payload)
        assert result == "webhook:data.issue.id=99"

    def test_custom_dedupe_path_overrides_defaults(self, flow_trigger_service):
        flow = self._flow(webhook_config={"dedupe_path": "event_id"})
        payload = {
            "event_id": "abc-123",
            "attachments": [{"title_link": "https://x"}],
        }
        result = flow_trigger_service._extract_webhook_resource_key(flow, payload)
        assert result == "webhook:event_id=abc-123"

    def test_custom_dedupe_path_no_match_returns_none(self, flow_trigger_service):
        flow = self._flow(webhook_config={"dedupe_path": "missing.path"})
        assert (
            flow_trigger_service._extract_webhook_resource_key(flow, {"a": 1}) is None
        )

    def test_no_defaults_match_returns_none(self, flow_trigger_service):
        flow = self._flow()
        assert flow_trigger_service._extract_webhook_resource_key(flow, {}) is None

    def test_empty_string_value_is_skipped(self, flow_trigger_service):
        flow = self._flow()
        payload = {"data": {"issue": {"id": ""}}, "other": "v"}
        assert flow_trigger_service._extract_webhook_resource_key(flow, payload) is None

    def test_non_dict_webhook_config_is_tolerated(self, flow_trigger_service):
        flow = self._flow()
        flow.webhook_config = None
        payload = {"attachments": [{"title_link": "https://x"}]}
        result = flow_trigger_service._extract_webhook_resource_key(flow, payload)
        assert result == "webhook:attachments.0.title_link=https://x"


class TestFallbackDedupeResourceKey:
    """The fallback key must apply to webhooks and releases only."""

    @patch("preloop.services.flow_trigger_service.crud_flow_execution")
    def test_webhook_event_gets_key(self, mock_crud, flow_trigger_service):
        flow = MagicMock()
        flow.webhook_config = {}
        event = {
            "source": "webhook",
            "payload": {"data": {"issue": {"id": 7}}},
        }
        result = flow_trigger_service._fallback_dedupe_resource_key(flow, event)
        assert result == "webhook:data.issue.id=7"

    def test_github_issue_event_gets_no_fallback_key(self, flow_trigger_service):
        """Issue/PR dedup behavior must remain commit-SHA-only."""
        flow = MagicMock()
        flow.webhook_config = {}
        event = {
            "source": "github",
            "payload": {
                "issue": {"number": 5},
                "repository": {"full_name": "owner/repo"},
            },
        }
        assert flow_trigger_service._fallback_dedupe_resource_key(flow, event) is None

    def test_gitlab_mr_event_gets_no_fallback_key(self, flow_trigger_service):
        flow = MagicMock()
        flow.webhook_config = {}
        event = {
            "source": "gitlab",
            "payload": {
                "object_attributes": {"iid": 9},
                "project": {"path_with_namespace": "g/p"},
            },
        }
        assert flow_trigger_service._fallback_dedupe_resource_key(flow, event) is None


class TestFindDuplicateExecutionResourceKey:
    """find_duplicate_execution falls back to resource keys when no SHA."""

    def _flow(self):
        flow = MagicMock()
        flow.id = uuid.uuid4()
        flow.account_id = uuid.uuid4()
        flow.webhook_config = {}
        return flow

    def _running_execution(self, payload):
        execution = MagicMock()
        execution.id = uuid.uuid4()
        execution.status = "PENDING"
        execution.trigger_event_details = {
            "source": "webhook",
            "type": "webhook",
            "payload": payload,
        }
        return execution

    @patch("preloop.services.flow_trigger_service.crud_flow_execution")
    def test_commitless_identical_payload_deduplicates(
        self, mock_crud, flow_trigger_service
    ):
        flow = self._flow()
        payload = {"attachments": [{"title_link": "https://gt/issues/1"}]}
        mock_crud.get_running_by_flow.return_value = [self._running_execution(payload)]

        duplicate = flow_trigger_service.find_duplicate_execution(
            flow,
            {"source": "webhook", "type": "webhook", "payload": dict(payload)},
        )
        assert duplicate is not None

    @patch("preloop.services.flow_trigger_service.crud_flow_execution")
    def test_commitless_different_payload_not_deduplicated(
        self, mock_crud, flow_trigger_service
    ):
        flow = self._flow()
        mock_crud.get_running_by_flow.return_value = [
            self._running_execution(
                {"attachments": [{"title_link": "https://gt/issues/1"}]}
            )
        ]

        duplicate = flow_trigger_service.find_duplicate_execution(
            flow,
            {
                "source": "webhook",
                "type": "webhook",
                "payload": {"data": {"issue": {"id": 55}}},
            },
        )
        assert duplicate is None

    @patch("preloop.services.flow_trigger_service.crud_flow_execution")
    def test_payload_without_any_identity_returns_none(
        self, mock_crud, flow_trigger_service
    ):
        flow = self._flow()
        duplicate = flow_trigger_service.find_duplicate_execution(
            flow,
            {"source": "webhook", "type": "webhook", "payload": {"foo": "bar"}},
        )
        assert duplicate is None
        mock_crud.get_running_by_flow.assert_not_called()
