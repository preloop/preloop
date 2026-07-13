"""Unit tests for approval request summary generation."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from preloop.services.approval_summary import (
    SUMMARY_TIMEOUT_SECONDS,
    generate_approval_summary,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_db():
    return MagicMock()


class TestGenerateApprovalSummary:
    async def test_ask_user_question_short_circuits_without_llm(self, mock_db):
        with patch(
            "preloop.services.approval_summary.crud_ai_model.get_default_active_model"
        ) as mock_get_model:
            summary = await generate_approval_summary(
                mock_db,
                account_id="acct-1",
                tool_name="ask_user",
                tool_args={
                    "is_question": True,
                    "question": "Deploy to production tonight?",
                },
            )

        assert summary == "Deploy to production tonight?"
        mock_get_model.assert_not_called()

    async def test_no_default_model_returns_none(self, mock_db):
        with patch(
            "preloop.services.approval_summary.crud_ai_model.get_default_active_model",
            return_value=None,
        ):
            summary = await generate_approval_summary(
                mock_db,
                account_id="acct-1",
                tool_name="force_push",
                tool_args={"branch": "main"},
            )

        assert summary is None

    async def test_litellm_success_returns_summary(self, mock_db):
        model = SimpleNamespace(
            provider_name="openai",
            model_identifier="gpt-4o-mini",
            api_key="sk-test",
            api_endpoint=None,
        )
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(
                message=MagicMock(content="Allow force-push of main on acme/api?")
            )
        ]

        with (
            patch(
                "preloop.services.approval_summary.crud_ai_model.get_default_active_model",
                return_value=model,
            ),
            patch("litellm.completion", return_value=mock_response) as mock_completion,
        ):
            summary = await generate_approval_summary(
                mock_db,
                account_id="acct-1",
                tool_name="force_push",
                tool_args={"branch": "main", "repo": "acme/api"},
                agent_reasoning="Need to update production",
                managed_agent_name="coder",
            )

        assert summary == "Allow force-push of main on acme/api?"
        mock_completion.assert_called_once()
        call_kwargs = mock_completion.call_args.kwargs
        assert call_kwargs["temperature"] == 0.1
        assert call_kwargs["max_tokens"] == 150

    async def test_litellm_timeout_returns_none(self, mock_db):
        model = SimpleNamespace(
            provider_name="openai",
            model_identifier="gpt-4o-mini",
            api_key="sk-test",
            api_endpoint=None,
        )

        async def _timeout_wait_for(awaitable, timeout):
            assert timeout == SUMMARY_TIMEOUT_SECONDS
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise TimeoutError()

        with (
            patch(
                "preloop.services.approval_summary.crud_ai_model.get_default_active_model",
                return_value=model,
            ),
            patch(
                "preloop.services.approval_summary.asyncio.wait_for",
                side_effect=_timeout_wait_for,
            ),
        ):
            summary = await generate_approval_summary(
                mock_db,
                account_id="acct-1",
                tool_name="force_push",
                tool_args={"branch": "main"},
            )

        assert summary is None

    async def test_litellm_failure_returns_none(self, mock_db):
        model = SimpleNamespace(
            provider_name="openai",
            model_identifier="gpt-4o-mini",
            api_key="sk-test",
            api_endpoint=None,
        )

        with (
            patch(
                "preloop.services.approval_summary.crud_ai_model.get_default_active_model",
                return_value=model,
            ),
            patch("litellm.completion", side_effect=RuntimeError("boom")),
        ):
            summary = await generate_approval_summary(
                mock_db,
                account_id="acct-1",
                tool_name="force_push",
                tool_args={"branch": "main"},
            )

        assert summary is None
