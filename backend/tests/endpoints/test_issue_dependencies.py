import pytest
import json
from unittest.mock import MagicMock
from pytest_mock import MockerFixture

from preloop.api.endpoints.issue_dependencies import (
    detect_issue_dependencies,
    DependencyRequest,
    DependencyResponse,
)
from preloop.models.models import Issue, AIModel, Project, Account
from preloop.services.secret_service import ResolvedModelCredentials


@pytest.fixture
def mock_project() -> MagicMock:
    """Provides a mock Project object."""
    project = MagicMock(spec=Project)
    project.name = "TEST-PROJ"
    return project


@pytest.fixture
def mock_issues(mock_project: MagicMock) -> list[MagicMock]:
    """Provides a list of mock Issue objects for testing."""
    issues = []
    for i, issue_id in enumerate(
        [
            "0000890a-99a1-4d47-ba8b-21b8292bbdc3",
            "00e6a411-f125-4079-ae01-79b5c35048b8",
            "00e8e485-d7e0-4d3d-b55c-be754213ab3b",
        ]
    ):
        issue = MagicMock(spec=Issue)
        issue.id = issue_id
        issue.title = f"Test Issue {i + 1}"
        issue.description = f"Description for issue {i + 1}."
        issue.project = mock_project
        issues.append(issue)
    return issues


def test_detect_issue_dependencies_success(
    mock_issues: list[MagicMock], mocker: MockerFixture
):
    """Tests successful dependency detection between a list of issues."""
    # Arrange
    issue_ids = [issue.id for issue in mock_issues]
    request = DependencyRequest(issue_ids=issue_ids)
    mock_user = MagicMock(spec=Account)
    mock_user.id = "user-123"
    mock_user.account_id = "account-123"

    # Mock CRUD operations
    mock_crud_issue = mocker.patch(
        "preloop.api.endpoints.issue_dependencies.crud_issue"
    )
    mock_crud_issue.get.side_effect = mock_issues

    mock_crud_ai_model = mocker.patch(
        "preloop.api.endpoints.issue_dependencies.crud_ai_model"
    )
    mock_ai_model = MagicMock(spec=AIModel)
    mock_ai_model.model_identifier = "gpt-5.4"
    mock_ai_model.api_key = "fake-key"
    mock_ai_model.credentials_secret = None
    mock_crud_ai_model.get_default_active_model.return_value = mock_ai_model

    # Mock IssueSet to simulate a cache miss
    mock_crud_issue_set = mocker.patch(
        "preloop.api.endpoints.issue_dependencies.crud_issue_set"
    )
    mock_crud_issue_set.get_supersets_by_issues.return_value = []

    # Mock OpenAI client
    mock_openai_client = mocker.patch(
        "preloop.api.endpoints.issue_dependencies.openai.OpenAI"
    )
    mock_completion = MagicMock()
    mock_completion.choices = [
        MagicMock(message=MagicMock(content=json.dumps({"dependencies": []})))
    ]
    mock_openai_client.return_value.chat.completions.create.return_value = (
        mock_completion
    )

    mock_settings = MagicMock()
    mock_settings.PROMPTS_FILE = "/path/to/prompts.yml"
    mocker.patch(
        "preloop.api.endpoints.issue_dependencies.load_dependencies_prompts_config",
        return_value={"dependency_detection_v1": {"system": "Test prompt"}},
    )

    # Act
    result = detect_issue_dependencies(
        request=request, db=MagicMock(), current_user=mock_user, settings=mock_settings
    )

    # Assert
    assert isinstance(result, DependencyResponse)
    assert result.dependencies == []

    # Verify mocks were called
    assert mock_crud_issue.get.call_count == len(issue_ids)
    mock_crud_ai_model.get_default_active_model.assert_called_once_with(
        mocker.ANY, account_id=mock_user.account_id
    )
    mock_openai_client.return_value.chat.completions.create.assert_called_once()
    mock_crud_issue_set.create_and_remove_subsets.assert_called_once()


def test_detect_issue_dependencies_uses_secret_service_credentials(
    mock_issues: list[MagicMock], mocker: MockerFixture
):
    """Dependency detection resolves credentials through the shared helper.

    Also asserts the model's custom endpoint is forwarded as base_url, which the
    direct resolve_ai_model_api_key() call this replaced silently dropped.
    """
    issue_ids = [issue.id for issue in mock_issues]
    request = DependencyRequest(issue_ids=issue_ids)
    mock_user = MagicMock(spec=Account)
    mock_user.id = "user-123"
    mock_user.account_id = "account-123"

    mock_crud_issue = mocker.patch(
        "preloop.api.endpoints.issue_dependencies.crud_issue"
    )
    mock_crud_issue.get.side_effect = mock_issues

    mock_crud_ai_model = mocker.patch(
        "preloop.api.endpoints.issue_dependencies.crud_ai_model"
    )
    mock_ai_model = MagicMock(spec=AIModel)
    mock_ai_model.id = "model-123"
    mock_ai_model.model_identifier = "gpt-5.4"
    mock_ai_model.api_key = "legacy-plaintext-key"
    mock_ai_model.credentials_secret = None
    mock_ai_model.api_endpoint = "https://custom.example.com/v1"
    mock_crud_ai_model.get_default_active_model.return_value = mock_ai_model

    mock_crud_issue_set = mocker.patch(
        "preloop.api.endpoints.issue_dependencies.crud_issue_set"
    )
    mock_crud_issue_set.get_supersets_by_issues.return_value = []

    mock_secret_service = MagicMock()
    mock_secret_service.resolve_ai_model_credentials.return_value = (
        ResolvedModelCredentials(
            credential_type="api_key",
            backend_type="openbao_kv_v2",
            value="resolved-external-key",
        )
    )
    mocker.patch(
        "preloop.services.model_credentials.get_secret_service",
        return_value=mock_secret_service,
    )

    mock_openai_client = mocker.patch(
        "preloop.api.endpoints.issue_dependencies.openai.OpenAI"
    )
    mock_completion = MagicMock()
    mock_completion.choices = [
        MagicMock(message=MagicMock(content=json.dumps({"dependencies": []})))
    ]
    mock_openai_client.return_value.chat.completions.create.return_value = (
        mock_completion
    )

    mock_settings = MagicMock()
    mock_settings.PROMPTS_FILE = "/path/to/prompts.yml"
    mocker.patch(
        "preloop.api.endpoints.issue_dependencies.load_dependencies_prompts_config",
        return_value={"dependency_detection_v1": {"system": "Test prompt"}},
    )

    result = detect_issue_dependencies(
        request=request, db=MagicMock(), current_user=mock_user, settings=mock_settings
    )

    assert isinstance(result, DependencyResponse)
    mock_secret_service.resolve_ai_model_credentials.assert_called_once()
    mock_openai_client.assert_called_once_with(
        api_key="resolved-external-key",
        base_url="https://custom.example.com/v1",
    )


def test_detect_issue_dependencies_retries_transient_429(
    mock_issues: list[MagicMock], mocker: MockerFixture
) -> None:
    """A single provider 429 must not fail dependency detection (#269)."""
    import httpx
    import openai

    request = httpx.Request("POST", "https://example.com/v1/chat/completions")
    response = httpx.Response(429, headers={"retry-after": "2"}, request=request)
    rate_limit_error = openai.RateLimitError(
        "Error code: 429 - Too Many Requests", response=response, body=None
    )

    issue_ids = [issue.id for issue in mock_issues]
    request = DependencyRequest(issue_ids=issue_ids)
    mock_user = MagicMock(spec=Account)
    mock_user.id = "user-123"
    mock_user.account_id = "account-123"

    mock_crud_issue = mocker.patch(
        "preloop.api.endpoints.issue_dependencies.crud_issue"
    )
    mock_crud_issue.get.side_effect = mock_issues

    mock_crud_ai_model = mocker.patch(
        "preloop.api.endpoints.issue_dependencies.crud_ai_model"
    )
    mock_ai_model = MagicMock(spec=AIModel)
    mock_ai_model.model_identifier = "gpt-5.4"
    mock_ai_model.id = "model-1"
    mock_ai_model.provider_name = "openai"
    mock_ai_model.api_key = "fake-key"
    mock_ai_model.credentials_secret = None
    mock_crud_ai_model.get_default_active_model.return_value = mock_ai_model

    mock_crud_issue_set = mocker.patch(
        "preloop.api.endpoints.issue_dependencies.crud_issue_set"
    )
    mock_crud_issue_set.get_supersets_by_issues.return_value = []

    mock_openai_client = mocker.patch(
        "preloop.api.endpoints.issue_dependencies.openai.OpenAI"
    )
    mock_completion = MagicMock()
    mock_completion.choices = [
        MagicMock(message=MagicMock(content=json.dumps({"dependencies": []})))
    ]
    mock_openai_client.return_value.chat.completions.create.side_effect = [
        rate_limit_error,
        mock_completion,
    ]
    mocker.patch("preloop.services.aux_model_retry.time.sleep", lambda _: None)

    mock_settings = MagicMock()
    mock_settings.PROMPTS_FILE = "/path/to/prompts.yml"
    mocker.patch(
        "preloop.api.endpoints.issue_dependencies.load_dependencies_prompts_config",
        return_value={"dependency_detection_v1": {"system": "Test prompt"}},
    )

    result = detect_issue_dependencies(
        request=request, db=MagicMock(), current_user=mock_user, settings=mock_settings
    )

    assert isinstance(result, DependencyResponse)
    assert result.dependencies == []
    assert mock_openai_client.return_value.chat.completions.create.call_count == 2
