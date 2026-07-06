"""Tests for agent permission workflow resolution."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from preloop.models import models
from preloop.services.agent_permission_service import (
    AGENT_TOOL_APPROVALS_WORKFLOW_NAME,
    _resolve_workflow,
)
from preloop.services.approval_workflow_service import DEFAULT_APPROVAL_TYPE


@pytest.mark.asyncio
async def test_resolve_workflow_creates_standard_agent_tool_workflow() -> None:
    """Auto-created agent workflows must use the canonical approval type."""
    account_id = str(uuid.uuid4())
    approver_id = uuid.uuid4()
    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.rollback = AsyncMock()

    empty_result = MagicMock()
    empty_result.scalars.return_value.first.return_value = None
    db.execute = AsyncMock(return_value=empty_result)

    workflow = await _resolve_workflow(db, account_id, approver_id)

    assert workflow.name == AGENT_TOOL_APPROVALS_WORKFLOW_NAME
    assert workflow.approval_type == DEFAULT_APPROVAL_TYPE
    assert workflow.approver_user_ids == [str(approver_id)]
    db.add.assert_called_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_workflow_recovers_from_duplicate_name_race() -> None:
    """Concurrent creators should observe the existing workflow after conflict."""
    account_id = str(uuid.uuid4())
    existing = models.ApprovalWorkflow(
        id=uuid.uuid4(),
        account_id=account_id,
        name=AGENT_TOOL_APPROVALS_WORKFLOW_NAME,
        approval_type=DEFAULT_APPROVAL_TYPE,
    )
    db = AsyncMock()
    db.commit = AsyncMock(side_effect=IntegrityError("insert", {}, Exception()))
    db.refresh = AsyncMock()
    db.rollback = AsyncMock()

    empty_result = MagicMock()
    empty_result.scalars.return_value.first.return_value = None
    existing_result = MagicMock()
    existing_result.scalars.return_value.first.return_value = existing
    db.execute = AsyncMock(
        side_effect=[empty_result, empty_result, empty_result, existing_result]
    )

    workflow = await _resolve_workflow(db, account_id, uuid.uuid4())

    assert workflow is existing
    db.rollback.assert_awaited_once()
