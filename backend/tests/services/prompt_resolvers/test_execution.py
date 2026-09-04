"""Tests for the execution prompt resolver."""

from unittest.mock import MagicMock

import pytest

from preloop.services.prompt_resolvers.base import ResolverContext
from preloop.services.prompt_resolvers.execution import ExecutionResolver


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

    @pytest.mark.asyncio
    async def test_rebase_conflict_unset(self):
        result = await ExecutionResolver().resolve("rebase_conflict", _context())
        assert result is None

    @pytest.mark.asyncio
    async def test_rebase_conflict_set(self, monkeypatch):
        monkeypatch.setenv("PRELOOP_RESUME_REBASE_CONFLICT", "1")
        result = await ExecutionResolver().resolve("rebase_conflict", _context())
        assert result == "1"

    @pytest.mark.asyncio
    async def test_rebase_conflict_empty_env_is_absent(self, monkeypatch):
        monkeypatch.setenv("PRELOOP_RESUME_REBASE_CONFLICT", "")
        result = await ExecutionResolver().resolve("rebase_conflict", _context())
        assert result is None

    @pytest.mark.asyncio
    async def test_unknown_field(self):
        result = await ExecutionResolver().resolve("nope", _context())
        assert result is None
