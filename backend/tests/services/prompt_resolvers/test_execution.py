"""Tests for the execution prompt resolver."""

from unittest.mock import MagicMock

import pytest

from preloop.services.prompt_resolvers.base import ResolverContext
from preloop.services.prompt_resolvers.execution import (
    ExecutionResolver,
    RESUME_REBASE_CONFLICT_HINT,
    resume_rebase_conflict_hint,
)


def _context(**event):
    return ResolverContext(
        db=MagicMock(),
        trigger_event_data=event,
        flow_id="flow-1",
        execution_id="83021dcc-4658-45a3-814c-0e67d07642f6",
    )


class TestExecutionResolver:
    def test_prefix(self):
        assert ExecutionResolver().prefix == "execution"

    @pytest.mark.asyncio
    async def test_id(self):
        result = await ExecutionResolver().resolve("id", _context())
        assert result == "83021dcc-4658-45a3-814c-0e67d07642f6"

    @pytest.mark.asyncio
    async def test_url_uses_preloop_url(self, monkeypatch):
        monkeypatch.setenv("PRELOOP_URL", "https://preloop.ai")
        result = await ExecutionResolver().resolve("url", _context())
        assert result == (
            "https://preloop.ai/console/flows/executions/"
            "83021dcc-4658-45a3-814c-0e67d07642f6"
        )

    @pytest.mark.asyncio
    async def test_resume_from(self):
        result = await ExecutionResolver().resolve(
            "resume_from",
            _context(_resume={"execution_id": "prior-exec"}),
        )
        assert result == "prior-exec"

    @pytest.mark.asyncio
    async def test_resume_from_absent(self):
        result = await ExecutionResolver().resolve("resume_from", _context())
        assert result is None

    def test_resume_prompt_tells_the_agent_to_inspect_rebase_conflicts(self):
        hint = resume_rebase_conflict_hint({"_resume": {"execution_id": "prior"}})
        assert RESUME_REBASE_CONFLICT_HINT in hint
        assert "/workspace/evidence/rebase-conflict.txt" in hint
        assert resume_rebase_conflict_hint({}) == ""
        assert resume_rebase_conflict_hint(None) == ""

    @pytest.mark.asyncio
    async def test_rebase_conflict_is_not_a_prompt_placeholder(self, monkeypatch):
        monkeypatch.setenv("PRELOOP_RESUME_REBASE_CONFLICT", "1")
        result = await ExecutionResolver().resolve("rebase_conflict", _context())
        assert result is None

    @pytest.mark.asyncio
    async def test_ci_failure_renders_provider_name_and_url(self):
        result = await ExecutionResolver().resolve(
            "ci_failure",
            _context(
                _ci_failure={
                    "provider": "github",
                    "name": "backend-tests",
                    "url": "https://github.com/preloop/preloop/runs/9",
                    "conclusion": "failure",
                    "head_sha": "abc123",
                    "pr_url": "https://github.com/preloop/preloop/pull/353",
                }
            ),
        )
        assert result == (
            "GitHub backend-tests failure: https://github.com/preloop/preloop/runs/9"
        )

    @pytest.mark.asyncio
    async def test_ci_failure_gitlab_without_url(self):
        result = await ExecutionResolver().resolve(
            "ci_failure",
            _context(
                _ci_failure={
                    "provider": "gitlab",
                    "name": "rspec",
                    "url": None,
                    "conclusion": "failed",
                }
            ),
        )
        assert result == "GitLab rspec failed"

    @pytest.mark.asyncio
    async def test_ci_failure_absent(self):
        result = await ExecutionResolver().resolve("ci_failure", _context())
        assert result is None

    @pytest.mark.asyncio
    async def test_unknown_field(self):
        result = await ExecutionResolver().resolve("nope", _context())
        assert result is None
