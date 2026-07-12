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


@pytest.mark.asyncio
async def test_resolve_workflow_prefers_agent_configured_workflow() -> None:
    """A workflow pinned on the managed agent via subject governance must win
    over the account default."""
    account_id = str(uuid.uuid4())
    managed_agent_id = uuid.uuid4()
    pinned = models.ApprovalWorkflow(
        id=uuid.uuid4(),
        account_id=account_id,
        name="Agent-specific workflow",
        approval_type=DEFAULT_APPROVAL_TYPE,
    )
    account = MagicMock()
    account.meta_data = {
        "subject_governance": {
            "managed_agents": {
                str(managed_agent_id): {"approval_workflow_id": str(pinned.id)}
            },
            "api_keys": {},
        }
    }

    account_and_workflow = MagicMock()
    account_and_workflow.first.return_value = (account, pinned)

    db = AsyncMock()
    db.execute = AsyncMock(return_value=account_and_workflow)

    workflow = await _resolve_workflow(
        db, account_id, uuid.uuid4(), managed_agent_id=managed_agent_id
    )

    assert workflow is pinned
    # Account + pinned workflow are loaded in one outerjoin round-trip.
    assert db.execute.await_count == 1


@pytest.mark.asyncio
async def test_resolve_workflow_falls_back_when_agent_pin_missing() -> None:
    """When the pinned workflow no longer exists, resolution falls back to the
    account default instead of failing the permission check."""
    account_id = str(uuid.uuid4())
    managed_agent_id = uuid.uuid4()
    default_workflow = models.ApprovalWorkflow(
        id=uuid.uuid4(),
        account_id=account_id,
        name="Default Approval Workflow",
        approval_type=DEFAULT_APPROVAL_TYPE,
        is_default=True,
    )
    account = MagicMock()
    account.meta_data = {
        "subject_governance": {
            "managed_agents": {
                str(managed_agent_id): {"approval_workflow_id": str(uuid.uuid4())}
            },
            "api_keys": {},
        }
    }

    account_and_missing = MagicMock()
    account_and_missing.first.return_value = (account, None)
    default_result = MagicMock()
    default_result.scalars.return_value.first.return_value = default_workflow

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[account_and_missing, default_result])

    workflow = await _resolve_workflow(
        db, account_id, uuid.uuid4(), managed_agent_id=managed_agent_id
    )

    assert workflow is default_workflow
