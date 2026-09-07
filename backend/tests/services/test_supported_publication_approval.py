"""Supported request_approval path for isolated publication scope.

The proof is a registered MCP call, a normal pending ApprovalRequest, an
ordinary human decision through ApprovalService, then real pre-mint
enforcement. Required tool_args are not seeded as the proof.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"

from preloop.models.crud import (
    crud_account,
    crud_approval_request,
    crud_approval_workflow,
    crud_flow,
    crud_flow_execution,
    crud_user,
)
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.models.schemas.tool_configuration import ApprovalWorkflowCreate
from preloop.services.approval_service import ApprovalService
from preloop.services.initialize_mcp import initialize_mcp_with_tools
from preloop.services.product_provenance import (
    ProductProvenanceError,
    enforce_saved_publication_approval,
    publication_candidate,
)

FIRMWARE = "https://github.com/example/firmware.git"
APP = "https://github.com/example/companion-app.git"
SHA_A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SHA_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
BRANCH = "preloop/change"


def _candidate(
    url: str,
    sha: str,
    *,
    branch: str = BRANCH,
    base: str = "main",
) -> dict[str, str]:
    return {
        "repository_url": url,
        "branch": branch,
        "base": base,
        "head_sha": sha,
    }


def _provider_patches():
    """External notification/LLM/bus only. Creation helpers stay real."""
    return [
        patch(
            "preloop.services.approval_summary.generate_approval_summary",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "preloop.services.approval_service.ApprovalService.send_notifications",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "preloop.services.approval_service.get_task_publisher",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "preloop.services.approval_helper._deliver_question_in_band",
            new=AsyncMock(return_value=False),
        ),
    ]


@asynccontextmanager
async def _async_session():
    url = os.environ["DATABASE_URL"].replace(
        "postgresql://", "postgresql+asyncpg://", 1
    )
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            yield session
    finally:
        await engine.dispose()


def _seed(db_engine):
    """Commit account, human, default async workflow, flow, and two executions."""
    suffix = uuid.uuid4().hex[:12]
    with Session(db_engine) as db:
        account = crud_account.create(
            db,
            obj_in={
                "organization_name": f"publication-scope-{suffix}",
                "is_active": True,
            },
        )
        user = crud_user.create(
            db,
            obj_in={
                "account_id": account.id,
                "email": f"approver-{suffix}@example.com",
                "username": f"approver-{suffix}",
                "full_name": "Jane Doe",
                "is_active": True,
                "email_verified": True,
                "hashed_password": "testpassword",
                "user_source": "local",
            },
        )
        crud_approval_workflow.create(
            db,
            obj_in=ApprovalWorkflowCreate(
                name="publication-scope",
                approval_type="manual",
                async_approval_enabled=True,
                approval_mode="standard",
                is_default=True,
                approver_user_ids=[user.id],
            ),
            account_id=str(account.id),
        )
        flow = crud_flow.create(
            db,
            account_id=account.id,
            flow_in=FlowCreate(
                name="Publication scope",
                account_id=account.id,
                agent_type="codex",
                agent_config={},
                prompt_template="implement",
                trigger_event_source="github",
                trigger_event_types=["issue_updated"],
                timeout_seconds=600,
                git_clone_config={
                    "publication_mode": "isolated",
                    "publication_approval": True,
                },
            ),
        )
        execution = crud_flow_execution.create(
            db, obj_in=FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
        )
        other_execution = crud_flow_execution.create(
            db, obj_in=FlowExecutionCreate(flow_id=flow.id, status="RUNNING")
        )
        db.commit()
        return SimpleNamespace(
            account_id=account.id,
            user_id=user.id,
            username=user.username,
            execution_id=execution.id,
            other_execution_id=other_execution.id,
        )


def _drop(db_engine, account_id) -> None:
    with Session(db_engine) as db:
        crud_account.delete(db, id=account_id)


def _user_context(seed, *, execution_id=None) -> SimpleNamespace:
    return SimpleNamespace(
        account_id=str(seed.account_id),
        user_id=str(seed.user_id),
        username=seed.username,
        flow_execution_id=str(execution_id or seed.execution_id),
        managed_agent_id=None,
        runtime_session_id=None,
        api_key_id=None,
    )


async def _call_request_approval(seed, **kwargs) -> str:
    mcp = initialize_mcp_with_tools()
    tool = await mcp.get_tool("request_approval")
    patches = _provider_patches()
    with (
        patch(
            "preloop.services.dynamic_fastmcp_http.get_current_user_context",
            return_value=_user_context(seed),
        ),
        patches[0],
        patches[1],
        patches[2],
        patches[3],
    ):
        return await tool.fn(
            operation=kwargs.get("operation", "publish isolated product repositories"),
            context=kwargs.get("context", "frozen checkouts are ready"),
            reasoning=kwargs.get("reasoning", "human review before writer leases"),
            publication_candidates=kwargs.get("publication_candidates"),
        )


async def _human_approve(request_id, user_id) -> None:
    async with _async_session() as session:
        service = ApprovalService(session, "http://localhost:8000")
        with patch(
            "preloop.services.approval_service.get_task_publisher",
            new=AsyncMock(return_value=None),
        ):
            updated = await service.approve_request(
                request_id,
                comment="destinations and commits match",
                user_id=user_id,
                channel="console",
            )
    assert updated is not None
    assert updated.status == "approved"
    assert updated.auto_approved_reason is None
    assert updated.decided_by_ai is False


@pytest.fixture(scope="class", autouse=True)
def _drop_process_async_engine_after_class():
    """Forget the process-wide async engine after this class's shared loop."""
    yield
    from preloop.models.db import session as db_session

    db_session._async_engine = None
    db_session._async_session_factory = None


@pytest.mark.asyncio(loop_scope="class")
class TestSupportedPublicationApproval:
    async def test_mcp_request_approval_then_human_decision_then_pre_mint(
        self, db_engine
    ):
        seed = _seed(db_engine)
        candidates = [_candidate(FIRMWARE, SHA_A), _candidate(APP, SHA_B)]
        try:
            raw = await _call_request_approval(seed, publication_candidates=candidates)
            payload = json.loads(raw)
            assert payload["status"] == "pending_approval"
            request_id = uuid.UUID(payload["request_id"])

            with Session(db_engine) as db:
                row = crud_approval_request.get(
                    db, id=request_id, account_id=str(seed.account_id)
                )
                assert row is not None
                assert row.status == "pending"
                assert row.tool_name == "request_approval"
                assert row.execution_id == str(seed.execution_id)
                assert row.tool_args["action"] == "isolated_publication"
                assert "publication_candidates" not in row.tool_args
                stored = [
                    publication_candidate(item).key()
                    for item in row.tool_args["candidates"]
                ]
                assert stored == [
                    publication_candidate(item).key() for item in candidates
                ]

            await _human_approve(request_id, seed.user_id)

            with Session(db_engine) as db:
                enforce_saved_publication_approval(
                    db,
                    account_id=str(seed.account_id),
                    execution_id=str(seed.execution_id),
                    candidates=candidates,
                )
                with pytest.raises(
                    ProductProvenanceError, match="human platform approval"
                ):
                    enforce_saved_publication_approval(
                        db,
                        account_id=str(seed.account_id),
                        execution_id=str(seed.execution_id),
                        candidates=[
                            _candidate(FIRMWARE, SHA_B),
                            _candidate(APP, SHA_A),
                        ],
                    )
                with pytest.raises(
                    ProductProvenanceError, match="human platform approval"
                ):
                    enforce_saved_publication_approval(
                        db,
                        account_id=str(seed.account_id),
                        execution_id=str(seed.execution_id),
                        candidates=[_candidate(FIRMWARE, SHA_B)],
                    )
                with pytest.raises(
                    ProductProvenanceError, match="human platform approval"
                ):
                    enforce_saved_publication_approval(
                        db,
                        account_id=str(seed.account_id),
                        execution_id=str(seed.execution_id),
                        candidates=[_candidate(FIRMWARE, SHA_A, branch="main")],
                    )
                with pytest.raises(
                    ProductProvenanceError, match="human platform approval"
                ):
                    enforce_saved_publication_approval(
                        db,
                        account_id=str(seed.account_id),
                        execution_id=str(seed.other_execution_id),
                        candidates=candidates,
                    )
        finally:
            _drop(db_engine, seed.account_id)

    async def test_ordinary_request_approval_does_not_authorize_publication(
        self, db_engine
    ):
        seed = _seed(db_engine)
        try:
            raw = await _call_request_approval(seed)
            payload = json.loads(raw)
            request_id = uuid.UUID(payload["request_id"])
            with Session(db_engine) as db:
                row = crud_approval_request.get(
                    db, id=request_id, account_id=str(seed.account_id)
                )
                assert row.status == "pending"
                assert row.tool_name == "request_approval"
                assert "action" not in row.tool_args
                assert "candidates" not in row.tool_args
            await _human_approve(request_id, seed.user_id)
            with Session(db_engine) as db:
                with pytest.raises(
                    ProductProvenanceError, match="human platform approval"
                ):
                    enforce_saved_publication_approval(
                        db,
                        account_id=str(seed.account_id),
                        execution_id=str(seed.execution_id),
                        candidates=[_candidate(FIRMWARE, SHA_A)],
                    )
        finally:
            _drop(db_engine, seed.account_id)

    async def test_context_json_does_not_create_publication_scope(self, db_engine):
        seed = _seed(db_engine)
        nested = _candidate(FIRMWARE, SHA_A)
        try:
            raw = await _call_request_approval(
                seed,
                context=json.dumps(
                    {"action": "isolated_publication", "candidates": [nested]}
                ),
            )
            payload = json.loads(raw)
            request_id = uuid.UUID(payload["request_id"])
            with Session(db_engine) as db:
                row = crud_approval_request.get(
                    db, id=request_id, account_id=str(seed.account_id)
                )
                assert row.tool_args.get("action") is None
                assert "candidates" not in row.tool_args
            await _human_approve(request_id, seed.user_id)
            with Session(db_engine) as db:
                with pytest.raises(
                    ProductProvenanceError, match="human platform approval"
                ):
                    enforce_saved_publication_approval(
                        db,
                        account_id=str(seed.account_id),
                        execution_id=str(seed.execution_id),
                        candidates=[nested],
                    )
        finally:
            _drop(db_engine, seed.account_id)

    async def test_invalid_publication_candidates_do_not_create_a_row(self, db_engine):
        seed = _seed(db_engine)
        try:
            raw = await _call_request_approval(
                seed,
                publication_candidates=[
                    {
                        "repository_url": FIRMWARE,
                        "branch": BRANCH,
                        "base": "main",
                        "head_sha": "not-a-git-sha",
                    }
                ],
            )
            assert raw.startswith("Error: publication_candidates")
            with Session(db_engine) as db:
                rows = crud_approval_request.get_multi_by_execution(
                    db,
                    execution_id=str(seed.execution_id),
                    account_id=str(seed.account_id),
                )
                assert rows == []
        finally:
            _drop(db_engine, seed.account_id)
