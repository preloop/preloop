"""Unit tests for issue compliance API endpoints."""

import json
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from pytest_mock import MockerFixture

from preloop.api.endpoints.issue_compliance import (
    _calculate_issue_compliance,
    get_compliance_prompts,
    get_compliance_improvement_suggestion,
    get_issue_compliance,
    update_issue_content,
)
from preloop.models.models import AIModel, Issue, Organization, Project, User
from preloop.services.secret_service import ResolvedModelCredentials


@pytest.fixture
def mock_user() -> MagicMock:
    """Provide a mock User/Account for tests."""
    user = MagicMock(spec=User)
    user.id = "user-123"
    user.account_id = "account-456"
    return user


@pytest.fixture
def mock_issue() -> MagicMock:
    """Provide a mock Issue for tests."""
    issue = MagicMock(spec=Issue)
    issue.id = "issue-789"
    issue.title = "Test Issue"
    issue.description = "Test description"
    issue.project_id = "project-abc"
    return issue


@pytest.fixture
def mock_project() -> MagicMock:
    """Provide a mock Project for tests."""
    project = MagicMock(spec=Project)
    project.id = "project-abc"
    project.name = "Test Project"
    project.organization_id = "org-xyz"
    return project


@pytest.fixture
def mock_organization(mock_user) -> MagicMock:
    """Provide a mock Organization with tracker for tests."""
    org = MagicMock(spec=Organization)
    org.id = "org-xyz"
    org.tracker = MagicMock()
    org.tracker.account_id = mock_user.account_id
    return org


@pytest.fixture
def mock_ai_model() -> MagicMock:
    """Provide a mock AIModel for tests."""
    model = MagicMock(spec=AIModel)
    model.model_identifier = "gpt-5.4"
    model.api_key = "sk-test-key"
    return model


@pytest.fixture
def mock_settings() -> MagicMock:
    """Provide mock Settings for tests."""
    settings = MagicMock()
    settings.PROMPTS_FILE = "/path/to/prompts.yaml"
    return settings


class TestGetCompliancePrompts:
    """Tests for get_compliance_prompts endpoint."""

    @pytest.mark.asyncio
    async def test_returns_prompts_from_config(
        self, mock_settings, mocker: MockerFixture
    ):
        """Returns list of compliance prompts from config."""
        from preloop.schemas.issue_compliance import CompliancePromptMetadata

        mock_prompts = [
            CompliancePromptMetadata(
                id="dor", name="Definition of Ready", short_name="DoR"
            ),
            CompliancePromptMetadata(id="invest", name="INVEST", short_name="INVEST"),
        ]
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.get_compliance_prompts_from_config",
            return_value=mock_prompts,
        )

        result = await get_compliance_prompts(settings=mock_settings)

        assert len(result) == 2
        assert result[0].id == "dor"
        assert result[0].name == "Definition of Ready"
        assert result[1].id == "invest"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_prompts(
        self, mock_settings, mocker: MockerFixture
    ):
        """Returns empty list when config has no prompts."""
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.get_compliance_prompts_from_config",
            return_value=[],
        )

        result = await get_compliance_prompts(settings=mock_settings)

        assert result == []


class TestCalculateIssueCompliance:
    """Tests for _calculate_issue_compliance internal function."""

    def test_prompt_not_found_raises_404(self, mock_user, mock_settings, mocker):
        """Raises 404 when prompt name is not in config."""
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.load_compliance_prompts_config",
            return_value={},
        )

        with pytest.raises(HTTPException) as exc_info:
            _calculate_issue_compliance(
                issue_id="issue-1",
                prompt_name="unknown_prompt",
                db=MagicMock(),
                current_user=mock_user,
                settings=mock_settings,
            )

        assert exc_info.value.status_code == 404
        assert "unknown_prompt" in str(exc_info.value.detail)

    def test_returns_existing_result_when_cached(
        self, mock_user, mock_settings, mock_issue, mock_project, mocker
    ):
        """Returns cached compliance result when one exists."""
        existing_result = MagicMock()
        existing_result.id = "result-1"
        existing_result.compliance_factor = 0.85
        existing_result.short_name = None

        mocker.patch(
            "preloop.api.endpoints.issue_compliance.load_compliance_prompts_config",
            return_value={
                "dor_compliance_v1": {
                    "name": "Definition of Ready",
                    "short_name": "DoR",
                    "evaluate": {"name": "e", "system": "s", "user": "u"},
                    "propose_improvement": {"name": "p", "system": "s", "user": "u"},
                }
            },
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue_compliance_result"
        ).get_by_issue_id_and_prompt_id.return_value = existing_result

        result = _calculate_issue_compliance(
            issue_id="issue-789",
            prompt_name="dor_compliance_v1",
            db=MagicMock(),
            current_user=mock_user,
            settings=mock_settings,
        )

        assert result == existing_result
        assert existing_result.short_name == "DoR"

    def test_issue_not_found_raises_404(self, mock_user, mock_settings, mocker):
        """Raises 404 when issue does not exist."""
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.load_compliance_prompts_config",
            return_value={
                "dor_compliance_v1": {
                    "name": "DoR",
                    "short_name": "DoR",
                    "evaluate": {"name": "e", "system": "s", "user": "u"},
                    "propose_improvement": {"name": "p", "system": "s", "user": "u"},
                }
            },
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue_compliance_result"
        ).get_by_issue_id_and_prompt_id.return_value = None
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue"
        ).get.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            _calculate_issue_compliance(
                issue_id="nonexistent",
                prompt_name="dor_compliance_v1",
                db=MagicMock(),
                current_user=mock_user,
                settings=mock_settings,
            )

        assert exc_info.value.status_code == 404
        assert "Issue not found" in str(exc_info.value.detail)

    def test_no_default_ai_model_raises_500(
        self, mock_user, mock_settings, mock_issue, mock_project, mocker
    ):
        """Raises 500 when no default AI model is configured."""
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.load_compliance_prompts_config",
            return_value={
                "dor_compliance_v1": {
                    "name": "DoR",
                    "short_name": "DoR",
                    "evaluate": {"name": "e", "system": "s", "user": "u"},
                    "propose_improvement": {"name": "p", "system": "s", "user": "u"},
                }
            },
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue_compliance_result"
        ).get_by_issue_id_and_prompt_id.return_value = None
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue"
        ).get.return_value = mock_issue
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_project"
        ).get.return_value = mock_project
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_ai_model"
        ).get_default_active_model.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            _calculate_issue_compliance(
                issue_id="issue-789",
                prompt_name="dor_compliance_v1",
                db=MagicMock(),
                current_user=mock_user,
                settings=mock_settings,
            )

        assert exc_info.value.status_code == 500
        assert "No default active AI model" in str(exc_info.value.detail)

    def test_no_api_key_raises_500(
        self, mock_user, mock_settings, mock_issue, mock_project, mock_ai_model, mocker
    ):
        """Raises 500 when OpenAI API key is not configured."""
        mock_ai_model.api_key = None
        mocker.patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=False)

        mocker.patch(
            "preloop.api.endpoints.issue_compliance.load_compliance_prompts_config",
            return_value={
                "dor_compliance_v1": {
                    "name": "DoR",
                    "short_name": "DoR",
                    "evaluate": {"name": "e", "system": "s", "user": "u"},
                    "propose_improvement": {"name": "p", "system": "s", "user": "u"},
                }
            },
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue_compliance_result"
        ).get_by_issue_id_and_prompt_id.return_value = None
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue"
        ).get.return_value = mock_issue
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_project"
        ).get.return_value = mock_project
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_ai_model"
        ).get_default_active_model.return_value = mock_ai_model

        with pytest.raises(HTTPException) as exc_info:
            _calculate_issue_compliance(
                issue_id="issue-789",
                prompt_name="dor_compliance_v1",
                db=MagicMock(),
                current_user=mock_user,
                settings=mock_settings,
            )

        assert exc_info.value.status_code == 500
        assert "OpenAI API key" in str(exc_info.value.detail)

    def test_success_creates_new_result(
        self, mock_user, mock_settings, mock_issue, mock_project, mock_ai_model, mocker
    ):
        """Successfully calculates compliance and creates new result."""
        llm_response = {
            "compliance_factor": 0.9,
            "reason": "Meets DoR criteria",
            "suggestion": "Consider adding edge cases",
            "annotated_description": [],
        }
        new_result = MagicMock()
        new_result.id = "new-result-1"
        new_result.compliance_factor = 0.9
        new_result.short_name = None

        mocker.patch(
            "preloop.api.endpoints.issue_compliance.load_compliance_prompts_config",
            return_value={
                "dor_compliance_v1": {
                    "name": "Definition of Ready",
                    "short_name": "DoR",
                    "evaluate": {"name": "e", "system": "s", "user": "u"},
                    "propose_improvement": {"name": "p", "system": "s", "user": "u"},
                }
            },
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue_compliance_result"
        ).get_by_issue_id_and_prompt_id.return_value = None
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue"
        ).get.return_value = mock_issue
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_project"
        ).get.return_value = mock_project
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_ai_model"
        ).get_default_active_model.return_value = mock_ai_model

        mock_openai = mocker.patch(
            "preloop.api.endpoints.issue_compliance.openai.OpenAI"
        )
        mock_openai.return_value.chat.completions.create.return_value.choices = [
            MagicMock(message=MagicMock(content=json.dumps(llm_response)))
        ]

        mock_crud = mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue_compliance_result"
        )
        mock_crud.get_by_issue_id_and_prompt_id.return_value = None
        mock_crud.create.return_value = new_result

        result = _calculate_issue_compliance(
            issue_id="issue-789",
            prompt_name="dor_compliance_v1",
            db=MagicMock(),
            current_user=mock_user,
            settings=mock_settings,
        )

        assert result == new_result
        assert new_result.short_name == "DoR"
        mock_crud.create.assert_called_once()

    def test_openai_api_error_raises_500(
        self, mock_user, mock_settings, mock_issue, mock_project, mock_ai_model, mocker
    ):
        """Raises 500 when OpenAI API call fails."""
        import openai

        mocker.patch(
            "preloop.api.endpoints.issue_compliance.load_compliance_prompts_config",
            return_value={
                "dor_compliance_v1": {
                    "name": "DoR",
                    "short_name": "DoR",
                    "evaluate": {"name": "e", "system": "s", "user": "u"},
                    "propose_improvement": {"name": "p", "system": "s", "user": "u"},
                }
            },
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue_compliance_result"
        ).get_by_issue_id_and_prompt_id.return_value = None
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue"
        ).get.return_value = mock_issue
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_project"
        ).get.return_value = mock_project
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_ai_model"
        ).get_default_active_model.return_value = mock_ai_model

        mock_openai = mocker.patch(
            "preloop.api.endpoints.issue_compliance.openai.OpenAI"
        )
        mock_openai.return_value.chat.completions.create.side_effect = openai.APIError(
            "API error", request=MagicMock(), body=None
        )

        with pytest.raises(HTTPException) as exc_info:
            _calculate_issue_compliance(
                issue_id="issue-789",
                prompt_name="dor_compliance_v1",
                db=MagicMock(),
                current_user=mock_user,
                settings=mock_settings,
            )

        assert exc_info.value.status_code == 500
        assert "AI model API error" in str(exc_info.value.detail)

    def test_invalid_json_response_raises_500(
        self, mock_user, mock_settings, mock_issue, mock_project, mock_ai_model, mocker
    ):
        """Raises 500 when LLM returns invalid JSON."""
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.load_compliance_prompts_config",
            return_value={
                "dor_compliance_v1": {
                    "name": "DoR",
                    "short_name": "DoR",
                    "evaluate": {"name": "e", "system": "s", "user": "u"},
                    "propose_improvement": {"name": "p", "system": "s", "user": "u"},
                }
            },
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue_compliance_result"
        ).get_by_issue_id_and_prompt_id.return_value = None
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue"
        ).get.return_value = mock_issue
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_project"
        ).get.return_value = mock_project
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_ai_model"
        ).get_default_active_model.return_value = mock_ai_model

        mock_openai = mocker.patch(
            "preloop.api.endpoints.issue_compliance.openai.OpenAI"
        )
        mock_openai.return_value.chat.completions.create.return_value.choices = [
            MagicMock(message=MagicMock(content="not valid json"))
        ]

        with pytest.raises(HTTPException) as exc_info:
            _calculate_issue_compliance(
                issue_id="issue-789",
                prompt_name="dor_compliance_v1",
                db=MagicMock(),
                current_user=mock_user,
                settings=mock_settings,
            )

        assert exc_info.value.status_code == 500
        assert "Error parsing AI model response" in str(exc_info.value.detail)

    def test_api_key_from_env_when_model_key_none(
        self, mock_user, mock_settings, mock_issue, mock_project, mock_ai_model, mocker
    ):
        """Uses OPENAI_API_KEY from env when model has no api_key."""
        mock_ai_model.api_key = None
        mocker.patch.dict("os.environ", {"OPENAI_API_KEY": "sk-env-key"}, clear=False)

        llm_response = {
            "compliance_factor": 0.9,
            "reason": "Meets DoR",
            "suggestion": "None",
            "annotated_description": [],
        }
        new_result = MagicMock()
        new_result.id = "new-result-1"
        new_result.compliance_factor = 0.9
        new_result.short_name = None

        mocker.patch(
            "preloop.api.endpoints.issue_compliance.load_compliance_prompts_config",
            return_value={
                "dor_compliance_v1": {
                    "name": "Definition of Ready",
                    "short_name": "DoR",
                    "evaluate": {"name": "e", "system": "s", "user": "u"},
                    "propose_improvement": {"name": "p", "system": "s", "user": "u"},
                }
            },
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue_compliance_result"
        ).get_by_issue_id_and_prompt_id.return_value = None
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue"
        ).get.return_value = mock_issue
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_project"
        ).get.return_value = mock_project
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_ai_model"
        ).get_default_active_model.return_value = mock_ai_model

        mock_openai = mocker.patch(
            "preloop.api.endpoints.issue_compliance.openai.OpenAI"
        )
        mock_openai.return_value.chat.completions.create.return_value.choices = [
            MagicMock(message=MagicMock(content=json.dumps(llm_response)))
        ]

        mock_crud = mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue_compliance_result"
        )
        mock_crud.get_by_issue_id_and_prompt_id.return_value = None
        mock_crud.create.return_value = new_result

        result = _calculate_issue_compliance(
            issue_id="issue-789",
            prompt_name="dor_compliance_v1",
            db=MagicMock(),
            current_user=mock_user,
            settings=mock_settings,
        )

        assert result == new_result
        mock_openai.assert_called_once_with(api_key="sk-env-key")

    def test_empty_choices_raises_500(
        self, mock_user, mock_settings, mock_issue, mock_project, mock_ai_model, mocker
    ):
        """Raises 500 when LLM returns empty choices (IndexError)."""
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.load_compliance_prompts_config",
            return_value={
                "dor_compliance_v1": {
                    "name": "DoR",
                    "short_name": "DoR",
                    "evaluate": {"name": "e", "system": "s", "user": "u"},
                    "propose_improvement": {"name": "p", "system": "s", "user": "u"},
                }
            },
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue_compliance_result"
        ).get_by_issue_id_and_prompt_id.return_value = None
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue"
        ).get.return_value = mock_issue
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_project"
        ).get.return_value = mock_project
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_ai_model"
        ).get_default_active_model.return_value = mock_ai_model

        mock_openai = mocker.patch(
            "preloop.api.endpoints.issue_compliance.openai.OpenAI"
        )
        mock_openai.return_value.chat.completions.create.return_value.choices = []

        with pytest.raises(HTTPException) as exc_info:
            _calculate_issue_compliance(
                issue_id="issue-789",
                prompt_name="dor_compliance_v1",
                db=MagicMock(),
                current_user=mock_user,
                settings=mock_settings,
            )

        assert exc_info.value.status_code == 500
        assert "Error parsing AI model response" in str(exc_info.value.detail)


class TestGetIssueCompliance:
    """Tests for get_issue_compliance endpoint."""

    def test_delegates_to_calculate(self, mock_user, mock_settings, mocker):
        """Delegates to _calculate_issue_compliance."""
        expected_result = MagicMock()
        mocker.patch(
            "preloop.api.endpoints.issue_compliance._calculate_issue_compliance",
            return_value=expected_result,
        )

        result = get_issue_compliance(
            issue_id="issue-1",
            prompt_name="dor_compliance_v1",
            db=MagicMock(),
            current_user=mock_user,
            settings=mock_settings,
        )

        assert result == expected_result


class TestGetComplianceImprovementSuggestion:
    """Tests for get_compliance_improvement_suggestion endpoint."""

    def test_access_denied_when_org_tracker_mismatch(
        self, mock_user, mock_issue, mock_project, mock_settings, mocker
    ):
        """Raises 403 when organization tracker does not belong to user."""
        compliance_result = MagicMock()
        compliance_result.compliance_factor = 0.8
        compliance_result.reason = "Good"
        compliance_result.suggestion = "Improve"

        org = MagicMock(spec=Organization)
        org.tracker = MagicMock()
        org.tracker.account_id = "other-account"  # Different from mock_user

        mocker.patch(
            "preloop.api.endpoints.issue_compliance._calculate_issue_compliance",
            return_value=compliance_result,
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue"
        ).get.return_value = mock_issue
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_project"
        ).get.return_value = mock_project
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_organization"
        ).get.return_value = org

        with pytest.raises(HTTPException) as exc_info:
            get_compliance_improvement_suggestion(
                issue_id="issue-789",
                prompt_name="dor_compliance_v1",
                db=MagicMock(),
                current_user=mock_user,
                settings=mock_settings,
            )

        assert exc_info.value.status_code == 403
        assert "Access denied" in str(exc_info.value.detail)

    def test_access_denied_when_no_organization(
        self, mock_user, mock_issue, mock_project, mock_settings, mocker
    ):
        """Raises 403 when organization is not found."""
        compliance_result = MagicMock()
        mocker.patch(
            "preloop.api.endpoints.issue_compliance._calculate_issue_compliance",
            return_value=compliance_result,
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue"
        ).get.return_value = mock_issue
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_project"
        ).get.return_value = mock_project
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_organization"
        ).get.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            get_compliance_improvement_suggestion(
                issue_id="issue-789",
                prompt_name="dor_compliance_v1",
                db=MagicMock(),
                current_user=mock_user,
                settings=mock_settings,
            )

        assert exc_info.value.status_code == 403

    def test_access_denied_when_no_tracker(
        self, mock_user, mock_issue, mock_project, mock_settings, mocker
    ):
        """Raises 403 when organization has no tracker."""
        compliance_result = MagicMock()
        org = MagicMock(spec=Organization)
        org.tracker = None  # No tracker

        mocker.patch(
            "preloop.api.endpoints.issue_compliance._calculate_issue_compliance",
            return_value=compliance_result,
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue"
        ).get.return_value = mock_issue
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_project"
        ).get.return_value = mock_project
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_organization"
        ).get.return_value = org

        with pytest.raises(HTTPException) as exc_info:
            get_compliance_improvement_suggestion(
                issue_id="issue-789",
                prompt_name="dor_compliance_v1",
                db=MagicMock(),
                current_user=mock_user,
                settings=mock_settings,
            )

        assert exc_info.value.status_code == 403

    def test_propose_improvement_missing_raises_500(
        self,
        mock_user,
        mock_issue,
        mock_project,
        mock_organization,
        mock_ai_model,
        mock_settings,
        mocker,
    ):
        """Raises 500 when prompt has no propose_improvement section."""
        compliance_result = MagicMock()
        compliance_result.compliance_factor = 0.8
        compliance_result.reason = "Good"
        compliance_result.suggestion = "Improve"

        mocker.patch(
            "preloop.api.endpoints.issue_compliance._calculate_issue_compliance",
            return_value=compliance_result,
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue"
        ).get.return_value = mock_issue
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_project"
        ).get.return_value = mock_project
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_organization"
        ).get.return_value = mock_organization
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_ai_model"
        ).get_default_active_model.return_value = mock_ai_model
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.load_compliance_prompts_config",
            return_value={
                "dor_compliance_v1": {
                    "name": "DoR",
                    "short_name": "DoR",
                    "evaluate": {"name": "e", "system": "s", "user": "u"},
                    # No propose_improvement
                }
            },
        )

        with pytest.raises(HTTPException) as exc_info:
            get_compliance_improvement_suggestion(
                issue_id="issue-789",
                prompt_name="dor_compliance_v1",
                db=MagicMock(),
                current_user=mock_user,
                settings=mock_settings,
            )

        assert exc_info.value.status_code == 500
        # ComplianceWorkflow validation fails when propose_improvement is missing
        assert "Invalid prompt configuration" in str(exc_info.value.detail)

    def test_propose_improvement_none_raises_500(
        self,
        mock_user,
        mock_issue,
        mock_project,
        mock_organization,
        mock_ai_model,
        mock_settings,
        mocker,
    ):
        """Raises 500 when propose_improvement is None (edge case)."""
        compliance_result = MagicMock()
        compliance_result.compliance_factor = 0.8
        compliance_result.reason = "Good"
        compliance_result.suggestion = "Improve"

        mock_workflow = MagicMock()
        mock_workflow.propose_improvement = None

        mocker.patch(
            "preloop.api.endpoints.issue_compliance._calculate_issue_compliance",
            return_value=compliance_result,
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue"
        ).get.return_value = mock_issue
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_project"
        ).get.return_value = mock_project
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_organization"
        ).get.return_value = mock_organization
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_ai_model"
        ).get_default_active_model.return_value = mock_ai_model
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.load_compliance_prompts_config",
            return_value={"dor_compliance_v1": {"name": "DoR"}},
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.ComplianceWorkflow",
            return_value=mock_workflow,
        )

        with pytest.raises(HTTPException) as exc_info:
            get_compliance_improvement_suggestion(
                issue_id="issue-789",
                prompt_name="dor_compliance_v1",
                db=MagicMock(),
                current_user=mock_user,
                settings=mock_settings,
            )

        assert exc_info.value.status_code == 500
        assert "propose_improvement" in str(exc_info.value.detail)

    def test_issue_not_found_raises_404(self, mock_user, mock_settings, mocker):
        """Raises 404 when issue does not exist after compliance calculation."""
        compliance_result = MagicMock()
        mocker.patch(
            "preloop.api.endpoints.issue_compliance._calculate_issue_compliance",
            return_value=compliance_result,
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue"
        ).get.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            get_compliance_improvement_suggestion(
                issue_id="nonexistent",
                prompt_name="dor_compliance_v1",
                db=MagicMock(),
                current_user=mock_user,
                settings=mock_settings,
            )

        assert exc_info.value.status_code == 404
        assert "Issue not found" in str(exc_info.value.detail)

    def test_no_default_model_raises_500(
        self,
        mock_user,
        mock_issue,
        mock_project,
        mock_organization,
        mock_settings,
        mocker,
    ):
        """Raises 500 when no default AI model in suggestion flow."""
        compliance_result = MagicMock()
        compliance_result.compliance_factor = 0.8
        compliance_result.reason = "Good"
        compliance_result.suggestion = "Improve"

        mocker.patch(
            "preloop.api.endpoints.issue_compliance._calculate_issue_compliance",
            return_value=compliance_result,
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue"
        ).get.return_value = mock_issue
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_project"
        ).get.return_value = mock_project
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_organization"
        ).get.return_value = mock_organization
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_ai_model"
        ).get_default_active_model.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            get_compliance_improvement_suggestion(
                issue_id="issue-789",
                prompt_name="dor_compliance_v1",
                db=MagicMock(),
                current_user=mock_user,
                settings=mock_settings,
            )

        assert exc_info.value.status_code == 500
        assert "No default active AI model" in str(exc_info.value.detail)

    def test_prompt_not_found_in_suggestion_raises_404(
        self,
        mock_user,
        mock_issue,
        mock_project,
        mock_organization,
        mock_ai_model,
        mock_settings,
        mocker,
    ):
        """Raises 404 when prompt not in config during suggestion flow."""
        compliance_result = MagicMock()
        mocker.patch(
            "preloop.api.endpoints.issue_compliance._calculate_issue_compliance",
            return_value=compliance_result,
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue"
        ).get.return_value = mock_issue
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_project"
        ).get.return_value = mock_project
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_organization"
        ).get.return_value = mock_organization
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_ai_model"
        ).get_default_active_model.return_value = mock_ai_model
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.load_compliance_prompts_config",
            return_value={},  # No prompts
        )

        with pytest.raises(HTTPException) as exc_info:
            get_compliance_improvement_suggestion(
                issue_id="issue-789",
                prompt_name="unknown_prompt",
                db=MagicMock(),
                current_user=mock_user,
                settings=mock_settings,
            )

        assert exc_info.value.status_code == 404
        assert "unknown_prompt" in str(exc_info.value.detail)

    def test_openai_api_error_raises_500(
        self,
        mock_user,
        mock_issue,
        mock_project,
        mock_organization,
        mock_ai_model,
        mock_settings,
        mocker,
    ):
        """Raises 500 when OpenAI API fails during suggestion."""
        import openai

        compliance_result = MagicMock()
        compliance_result.compliance_factor = 0.8
        compliance_result.reason = "Good"
        compliance_result.suggestion = "Improve"

        mocker.patch(
            "preloop.api.endpoints.issue_compliance._calculate_issue_compliance",
            return_value=compliance_result,
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue"
        ).get.return_value = mock_issue
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_project"
        ).get.return_value = mock_project
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_organization"
        ).get.return_value = mock_organization
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_ai_model"
        ).get_default_active_model.return_value = mock_ai_model
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.load_compliance_prompts_config",
            return_value={
                "dor_compliance_v1": {
                    "name": "DoR",
                    "short_name": "DoR",
                    "evaluate": {"name": "e", "system": "s", "user": "u"},
                    "propose_improvement": {"name": "p", "system": "s", "user": "u"},
                }
            },
        )

        mock_openai = mocker.patch(
            "preloop.api.endpoints.issue_compliance.openai.OpenAI"
        )
        mock_openai.return_value.chat.completions.create.side_effect = openai.APIError(
            "API error", request=MagicMock(), body=None
        )

        with pytest.raises(HTTPException) as exc_info:
            get_compliance_improvement_suggestion(
                issue_id="issue-789",
                prompt_name="dor_compliance_v1",
                db=MagicMock(),
                current_user=mock_user,
                settings=mock_settings,
            )

        assert exc_info.value.status_code == 500
        assert "Failed to get compliance suggestion" in str(exc_info.value.detail)

    def test_success_returns_suggestion(
        self,
        mock_user,
        mock_issue,
        mock_project,
        mock_organization,
        mock_ai_model,
        mock_settings,
        mocker,
    ):
        """Successfully returns compliance improvement suggestion."""
        compliance_result = MagicMock()
        compliance_result.compliance_factor = 0.8
        compliance_result.reason = "Good"
        compliance_result.suggestion = "Improve"

        suggestion_response = {
            "title": "Improved Title",
            "description": "Improved description",
            "changes": "Added acceptance criteria",
        }

        mocker.patch(
            "preloop.api.endpoints.issue_compliance._calculate_issue_compliance",
            return_value=compliance_result,
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue"
        ).get.return_value = mock_issue
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_project"
        ).get.return_value = mock_project
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_organization"
        ).get.return_value = mock_organization
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_ai_model"
        ).get_default_active_model.return_value = mock_ai_model
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.load_compliance_prompts_config",
            return_value={
                "dor_compliance_v1": {
                    "name": "DoR",
                    "short_name": "DoR",
                    "evaluate": {"name": "e", "system": "s", "user": "u"},
                    "propose_improvement": {"name": "p", "system": "s", "user": "u"},
                }
            },
        )

        mock_openai = mocker.patch(
            "preloop.api.endpoints.issue_compliance.openai.OpenAI"
        )
        mock_openai.return_value.chat.completions.create.return_value.choices = [
            MagicMock(message=MagicMock(content=json.dumps(suggestion_response)))
        ]

        result = get_compliance_improvement_suggestion(
            issue_id="issue-789",
            prompt_name="dor_compliance_v1",
            db=MagicMock(),
            current_user=mock_user,
            settings=mock_settings,
        )

        assert result.title == "Improved Title"
        assert result.description == "Improved description"
        assert result.changes == "Added acceptance criteria"

    def test_invalid_prompt_config_raises_500(
        self,
        mock_user,
        mock_issue,
        mock_project,
        mock_organization,
        mock_ai_model,
        mock_settings,
        mocker,
    ):
        """Raises 500 when prompt config fails to parse as ComplianceWorkflow."""
        compliance_result = MagicMock()
        compliance_result.compliance_factor = 0.8
        compliance_result.reason = "Good"
        compliance_result.suggestion = "Improve"

        mocker.patch(
            "preloop.api.endpoints.issue_compliance._calculate_issue_compliance",
            return_value=compliance_result,
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue"
        ).get.return_value = mock_issue
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_project"
        ).get.return_value = mock_project
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_organization"
        ).get.return_value = mock_organization
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_ai_model"
        ).get_default_active_model.return_value = mock_ai_model
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.load_compliance_prompts_config",
            return_value={"dor_compliance_v1": {"name": "DoR", "invalid": "config"}},
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.ComplianceWorkflow",
            side_effect=ValueError("Invalid config"),
        )

        with pytest.raises(HTTPException) as exc_info:
            get_compliance_improvement_suggestion(
                issue_id="issue-789",
                prompt_name="dor_compliance_v1",
                db=MagicMock(),
                current_user=mock_user,
                settings=mock_settings,
            )

        assert exc_info.value.status_code == 500
        assert "Invalid prompt configuration" in str(exc_info.value.detail)

    def test_invalid_suggestion_json_raises_error(
        self,
        mock_user,
        mock_issue,
        mock_project,
        mock_organization,
        mock_ai_model,
        mock_settings,
        mocker,
    ):
        """Raises when LLM returns invalid JSON for suggestion."""
        compliance_result = MagicMock()
        compliance_result.compliance_factor = 0.8
        compliance_result.reason = "Good"
        compliance_result.suggestion = "Improve"

        mocker.patch(
            "preloop.api.endpoints.issue_compliance._calculate_issue_compliance",
            return_value=compliance_result,
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue"
        ).get.return_value = mock_issue
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_project"
        ).get.return_value = mock_project
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_organization"
        ).get.return_value = mock_organization
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_ai_model"
        ).get_default_active_model.return_value = mock_ai_model
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.load_compliance_prompts_config",
            return_value={
                "dor_compliance_v1": {
                    "name": "DoR",
                    "short_name": "DoR",
                    "evaluate": {"name": "e", "system": "s", "user": "u"},
                    "propose_improvement": {"name": "p", "system": "s", "user": "u"},
                }
            },
        )

        mock_openai = mocker.patch(
            "preloop.api.endpoints.issue_compliance.openai.OpenAI"
        )
        mock_openai.return_value.chat.completions.create.return_value.choices = [
            MagicMock(message=MagicMock(content="not valid json {{{"))
        ]

        with pytest.raises((ValueError, json.JSONDecodeError)):
            get_compliance_improvement_suggestion(
                issue_id="issue-789",
                prompt_name="dor_compliance_v1",
                db=MagicMock(),
                current_user=mock_user,
                settings=mock_settings,
            )

    def test_credentials_secret_id_model_resolves_via_vault(
        self,
        mock_user,
        mock_issue,
        mock_project,
        mock_organization,
        mock_settings,
        mocker,
    ):
        """A vault-backed model resolves its key instead of constructing a bare client.

        Regression: this endpoint used to call openai.OpenAI() with no credentials,
        so models using credentials_secret_id (no plaintext api_key column) silently
        401'd. The resolved key and the model's endpoint must both reach the client.
        """
        compliance_result = MagicMock()
        compliance_result.compliance_factor = 0.8
        compliance_result.reason = "Good"
        compliance_result.suggestion = "Improve"

        # Model with no plaintext api_key: credentials live in the secret backend.
        vault_model = MagicMock(spec=AIModel)
        vault_model.id = "model-123"
        vault_model.model_identifier = "gpt-5.4"
        vault_model.api_key = None
        vault_model.api_endpoint = "https://custom.example.com/v1"

        mocker.patch(
            "preloop.api.endpoints.issue_compliance._calculate_issue_compliance",
            return_value=compliance_result,
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue"
        ).get.return_value = mock_issue
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_project"
        ).get.return_value = mock_project
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_organization"
        ).get.return_value = mock_organization
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_ai_model"
        ).get_default_active_model.return_value = vault_model
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.load_compliance_prompts_config",
            return_value={
                "dor_compliance_v1": {
                    "name": "DoR",
                    "short_name": "DoR",
                    "evaluate": {"name": "e", "system": "s", "user": "u"},
                    "propose_improvement": {"name": "p", "system": "s", "user": "u"},
                }
            },
        )

        mock_secret_service = MagicMock()
        mock_secret_service.resolve_ai_model_credentials.return_value = (
            ResolvedModelCredentials(
                credential_type="api_key",
                backend_type="openbao_kv_v2",
                value="sk-resolved-from-vault",
            )
        )
        mocker.patch(
            "preloop.services.model_credentials.get_secret_service",
            return_value=mock_secret_service,
        )

        mock_openai = mocker.patch(
            "preloop.api.endpoints.issue_compliance.openai.OpenAI"
        )
        mock_openai.return_value.chat.completions.create.return_value.choices = [
            MagicMock(
                message=MagicMock(
                    content=json.dumps(
                        {
                            "title": "T",
                            "description": "D",
                            "changes": "C",
                        }
                    )
                )
            )
        ]

        result = get_compliance_improvement_suggestion(
            issue_id="issue-789",
            prompt_name="dor_compliance_v1",
            db=MagicMock(),
            current_user=mock_user,
            settings=mock_settings,
        )

        assert result.title == "T"
        mock_openai.assert_called_once_with(
            api_key="sk-resolved-from-vault",
            base_url="https://custom.example.com/v1",
        )

    def test_missing_credentials_raise_500_without_calling_provider(
        self,
        mock_user,
        mock_issue,
        mock_project,
        mock_organization,
        mock_settings,
        monkeypatch,
        mocker,
    ):
        """With no resolvable key and no env var, fail fast instead of calling out."""
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        compliance_result = MagicMock()
        compliance_result.compliance_factor = 0.8
        compliance_result.reason = "Good"
        compliance_result.suggestion = "Improve"

        keyless_model = MagicMock(spec=AIModel)
        keyless_model.id = "model-123"
        keyless_model.model_identifier = "gpt-5.4"
        keyless_model.api_key = None
        keyless_model.api_endpoint = None

        mocker.patch(
            "preloop.api.endpoints.issue_compliance._calculate_issue_compliance",
            return_value=compliance_result,
        )
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue"
        ).get.return_value = mock_issue
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_project"
        ).get.return_value = mock_project
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_organization"
        ).get.return_value = mock_organization
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_ai_model"
        ).get_default_active_model.return_value = keyless_model
        mocker.patch(
            "preloop.api.endpoints.issue_compliance.load_compliance_prompts_config",
            return_value={
                "dor_compliance_v1": {
                    "name": "DoR",
                    "short_name": "DoR",
                    "evaluate": {"name": "e", "system": "s", "user": "u"},
                    "propose_improvement": {"name": "p", "system": "s", "user": "u"},
                }
            },
        )

        mock_secret_service = MagicMock()
        mock_secret_service.resolve_ai_model_credentials.return_value = None
        mocker.patch(
            "preloop.services.model_credentials.get_secret_service",
            return_value=mock_secret_service,
        )

        mock_openai = mocker.patch(
            "preloop.api.endpoints.issue_compliance.openai.OpenAI"
        )

        with pytest.raises(HTTPException) as exc_info:
            get_compliance_improvement_suggestion(
                issue_id="issue-789",
                prompt_name="dor_compliance_v1",
                db=MagicMock(),
                current_user=mock_user,
                settings=mock_settings,
            )

        assert exc_info.value.status_code == 500
        mock_openai.assert_not_called()


class TestUpdateIssueContent:
    """Tests for update_issue_content endpoint."""

    @pytest.mark.asyncio
    async def test_deletes_compliance_results_and_calls_update(self, mock_user, mocker):
        """Deletes compliance results, duplicate pairs, and calls update_issue."""
        from preloop.schemas.issue import IssueUpdate, IssueResponse

        expected_issue = MagicMock(spec=IssueResponse)
        expected_issue.id = "issue-789"
        expected_issue.title = "Updated Title"

        mock_update_issue = mocker.patch(
            "preloop.api.endpoints.issue_compliance.update_issue",
            return_value=expected_issue,
        )
        mock_crud_compliance = mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue_compliance_result"
        )
        mock_crud_duplicate = mocker.patch(
            "preloop.api.endpoints.issue_compliance.crud_issue_duplicate"
        )
        mock_db = MagicMock()

        issue_update = IssueUpdate(title="Updated Title", description="Updated desc")

        result = await update_issue_content(
            issue_id="issue-789",
            issue_update=issue_update,
            db=mock_db,
            current_user=mock_user,
        )

        assert result == expected_issue
        mock_crud_compliance.delete_by_issue_id.assert_called_once_with(
            mock_db, issue_id="issue-789"
        )
        mock_crud_duplicate.remove_by_issue_id.assert_called_once_with(
            mock_db, issue_id="issue-789"
        )
        mock_update_issue.assert_called_once()
        call_kwargs = mock_update_issue.call_args[1]
        assert call_kwargs["issue_id"] == "issue-789"
        assert call_kwargs["issue_update"].title == "Updated Title"
        assert call_kwargs["issue_update"].description == "Updated desc"
