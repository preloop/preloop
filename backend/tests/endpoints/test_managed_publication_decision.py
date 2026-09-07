"""Managed execution keys cannot self-approve publication.

Root reproduction at HEAD 36b56264: a flow-bound runtime API key
authenticated as its owning User with no ``_auth_api_key``, posted the
existing authenticated ``approve`` handler, and recorded a human-style
vote (``decided_by_ai=False``). Pre-mint then accepted the scoped row.
The guard below uses the principal attached at auth time, not body flags.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"

from preloop.api.auth.jwt import create_access_token, get_current_user
from preloop.api.endpoints import approval_requests
from preloop.models.crud import (
    crud_api_key,
    crud_approval_request,
    crud_managed_agent,
)
from preloop.models.schemas.approval_request import (
    ApprovalBatchDecision,
    ApprovalDecision,
)
from preloop.services.product_provenance import (
    ProductProvenanceError,
    enforce_saved_publication_approval,
)

from tests.services.test_supported_publication_approval import (
    APP,
    FIRMWARE,
    SHA_A,
    SHA_B,
    _async_session,
    _call_request_approval,
    _candidate,
    _drop,
    _seed,
)


def _http_request() -> MagicMock:
    request = MagicMock()
    request.base_url = "http://localhost/"
    return request


def _publisher_patch():
    return patch(
        "preloop.services.approval_service.get_task_publisher",
        new=AsyncMock(return_value=None),
    )


def _row_snapshot(db_engine, account_id, request_id) -> SimpleNamespace:
    with Session(db_engine) as db:
        row = crud_approval_request.get(db, id=request_id, account_id=str(account_id))
        assert row is not None
        return SimpleNamespace(
            status=row.status,
            responses=list(row.responses or []),
            decided_by_ai=row.decided_by_ai,
            auto_approved_reason=row.auto_approved_reason,
        )


def _mint_flow_secret(db_engine, seed) -> str:
    with Session(db_engine) as db:
        _record, secret = crud_api_key.create_runtime_key(
            db,
            name="flow-publication-decision",
            account_id=seed.account_id,
            user_id=seed.user_id,
            context_data={"flow_execution_id": str(seed.execution_id)},
            commit=True,
        )
        return secret


def _mint_agent_secret(db_engine, seed) -> str:
    with Session(db_engine) as db:
        agent = crud_managed_agent.create_custom_agent(
            db,
            account_id=seed.account_id,
            display_name="Publication agent",
            owner_user_id=seed.user_id,
            agent_kind="cursor",
            commit=True,
        )
        _record, secret = crud_api_key.create_runtime_key(
            db,
            name="agent-publication-decision",
            account_id=seed.account_id,
            user_id=seed.user_id,
            context_data={"managed_agent_id": str(agent.id)},
            commit=True,
        )
        return secret


def _principal(db, secret: str):
    principal = get_current_user(token=secret, db=db)
    assert principal._auth_api_key is not None
    return principal


@asynccontextmanager
async def _blocked_call(db_engine, secret: str):
    with Session(db_engine) as db, _publisher_patch():
        yield db, _principal(db, secret)


@pytest.mark.asyncio
class TestManagedPublicationDecisionPredicate:
    async def test_mock_managed_key_is_rejected_before_approval_service(self):
        user = MagicMock()
        user.id = uuid.uuid4()
        user.account_id = str(uuid.uuid4())
        user._auth_api_key = SimpleNamespace(
            context_data={"flow_execution_id": str(uuid.uuid4())}
        )
        request = MagicMock()
        request.account_id = user.account_id
        request.status = "pending"
        request.tool_name = "request_approval"
        request.tool_args = {"action": "isolated_publication"}
        service = AsyncMock()
        service.get_approval_request.return_value = request
        with (
            patch(
                "preloop.api.endpoints.approval_requests.get_async_db_session"
            ) as mock_session,
            patch(
                "preloop.api.endpoints.approval_requests.ApprovalService",
                return_value=service,
            ),
        ):
            mock_session.return_value.__aenter__.return_value = AsyncMock()
            with pytest.raises(HTTPException) as exc_info:
                await approval_requests.approve_request(
                    request_id=uuid.uuid4(),
                    decision=ApprovalDecision(approved=True, comment="no"),
                    request=_http_request(),
                    current_user=user,
                    db=MagicMock(),
                )
        assert exc_info.value.status_code == 403
        assert exc_info.value.detail == "managed_credential_cannot_decide"
        service.approve_request.assert_not_called()


@pytest.fixture(scope="class", autouse=True)
def _drop_process_async_engine_after_class():
    """Forget the process-wide async engine after this class's shared loop."""
    yield
    from preloop.models.db import session as db_session

    db_session._async_engine = None
    db_session._async_session_factory = None


@pytest.mark.asyncio(loop_scope="class")
class TestManagedPublicationDecision:
    async def test_managed_flow_key_cannot_approve_scoped_publication(self, db_engine):
        """Reproduction: managed/flow key + request_approval + /approve."""
        seed = _seed(db_engine)
        candidates = [_candidate(FIRMWARE, SHA_A)]
        try:
            raw = await _call_request_approval(seed, publication_candidates=candidates)
            request_id = uuid.UUID(json.loads(raw)["request_id"])
            before = _row_snapshot(db_engine, seed.account_id, request_id)
            assert before.status == "pending"
            assert before.responses == []
            secret = _mint_flow_secret(db_engine, seed)
            async with _blocked_call(db_engine, secret) as (db, principal):
                context = principal._auth_api_key.context_data
                assert context.get("flow_execution_id") == str(seed.execution_id)
                with pytest.raises(HTTPException) as exc_info:
                    await approval_requests.approve_request(
                        request_id=request_id,
                        decision=ApprovalDecision(
                            approved=True, comment="managed self-approve"
                        ),
                        request=_http_request(),
                        current_user=principal,
                        db=db,
                    )
            assert exc_info.value.status_code == 403
            assert exc_info.value.detail == "managed_credential_cannot_decide"
            after = _row_snapshot(db_engine, seed.account_id, request_id)
            assert after.status == "pending"
            assert after.responses == []
            assert after.decided_by_ai is False
            assert after.auto_approved_reason is None
            with Session(db_engine) as db:
                with pytest.raises(
                    ProductProvenanceError, match="human platform approval"
                ):
                    enforce_saved_publication_approval(
                        db,
                        account_id=str(seed.account_id),
                        execution_id=str(seed.execution_id),
                        candidates=candidates,
                    )
        finally:
            _drop(db_engine, seed.account_id)

    async def test_managed_agent_key_cannot_approve_scoped_publication(self, db_engine):
        seed = _seed(db_engine)
        candidates = [_candidate(FIRMWARE, SHA_A)]
        try:
            raw = await _call_request_approval(seed, publication_candidates=candidates)
            request_id = uuid.UUID(json.loads(raw)["request_id"])
            secret = _mint_agent_secret(db_engine, seed)
            async with _blocked_call(db_engine, secret) as (db, principal):
                assert principal._auth_api_key.context_data.get("managed_agent_id")
                with pytest.raises(HTTPException) as exc_info:
                    await approval_requests.approve_request(
                        request_id=request_id,
                        decision=ApprovalDecision(approved=True, comment="agent"),
                        request=_http_request(),
                        current_user=principal,
                        db=db,
                    )
            assert exc_info.value.status_code == 403
            assert _row_snapshot(db_engine, seed.account_id, request_id).status == (
                "pending"
            )
            with Session(db_engine) as db:
                with pytest.raises(
                    ProductProvenanceError, match="human platform approval"
                ):
                    enforce_saved_publication_approval(
                        db,
                        account_id=str(seed.account_id),
                        execution_id=str(seed.execution_id),
                        candidates=candidates,
                    )
        finally:
            _drop(db_engine, seed.account_id)

    async def test_managed_key_cannot_decline_or_decide_publication(self, db_engine):
        seed = _seed(db_engine)
        try:
            first = uuid.UUID(
                json.loads(
                    await _call_request_approval(
                        seed,
                        publication_candidates=[_candidate(FIRMWARE, SHA_A)],
                    )
                )["request_id"]
            )
            second = uuid.UUID(
                json.loads(
                    await _call_request_approval(
                        seed,
                        publication_candidates=[_candidate(APP, SHA_B)],
                    )
                )["request_id"]
            )
            secret = _mint_flow_secret(db_engine, seed)
            async with _blocked_call(db_engine, secret) as (db, principal):
                with pytest.raises(HTTPException) as declined:
                    await approval_requests.decline_request(
                        request_id=first,
                        decision=ApprovalDecision(
                            approved=False, comment="managed decline"
                        ),
                        request=_http_request(),
                        current_user=principal,
                        db=db,
                    )
                with pytest.raises(HTTPException) as decided:
                    await approval_requests.decide_request(
                        request_id=second,
                        decision=ApprovalDecision(approved=True, comment="decide"),
                        request=_http_request(),
                        current_user=principal,
                        db=db,
                    )
            assert declined.value.detail == "managed_credential_cannot_decide"
            assert decided.value.detail == "managed_credential_cannot_decide"
            assert _row_snapshot(db_engine, seed.account_id, first).status == "pending"
            assert _row_snapshot(db_engine, seed.account_id, second).status == (
                "pending"
            )
            assert _row_snapshot(db_engine, seed.account_id, first).responses == []
            assert _row_snapshot(db_engine, seed.account_id, second).responses == []
        finally:
            _drop(db_engine, seed.account_id)

    async def test_batch_blocks_publication_and_decides_ordinary(self, db_engine):
        seed = _seed(db_engine)
        candidates = [_candidate(FIRMWARE, SHA_A)]
        try:
            publication_id = uuid.UUID(
                json.loads(
                    await _call_request_approval(
                        seed, publication_candidates=candidates
                    )
                )["request_id"]
            )
            ordinary_id = uuid.UUID(
                json.loads(await _call_request_approval(seed))["request_id"]
            )
            secret = _mint_flow_secret(db_engine, seed)
            with Session(db_engine) as db:
                principal = _principal(db, secret)
                async with _async_session() as session:
                    with _publisher_patch():
                        response = await approval_requests.decide_requests_batch(
                            decision=ApprovalBatchDecision(
                                ids=[publication_id, ordinary_id],
                                approved=True,
                                comment="batch",
                            ),
                            request=_http_request(),
                            current_user=principal,
                            db=session,
                        )
            by_id = {item.id: item for item in response.results}
            assert by_id[publication_id].ok is False
            assert by_id[publication_id].error == "managed_credential_cannot_decide"
            assert by_id[ordinary_id].ok is True
            assert by_id[ordinary_id].status == "approved"
            pub = _row_snapshot(db_engine, seed.account_id, publication_id)
            assert pub.status == "pending"
            assert pub.responses == []
            ordinary = _row_snapshot(db_engine, seed.account_id, ordinary_id)
            assert ordinary.status == "approved"
            with Session(db_engine) as db:
                with pytest.raises(
                    ProductProvenanceError, match="human platform approval"
                ):
                    enforce_saved_publication_approval(
                        db,
                        account_id=str(seed.account_id),
                        execution_id=str(seed.execution_id),
                        candidates=candidates,
                    )
        finally:
            _drop(db_engine, seed.account_id)

    async def test_asgi_http_managed_bearer_denied_then_human_jwt_approves(
        self, db_engine
    ) -> None:
        """ASGI routing: managed Bearer 403, then human JWT approves same row."""
        from fastapi.testclient import TestClient

        from preloop.api.app import create_app
        from preloop.models.db.session import get_db_session

        seed = _seed(db_engine)
        candidates = [_candidate(FIRMWARE, SHA_A)]
        try:
            raw = await _call_request_approval(seed, publication_candidates=candidates)
            request_id = uuid.UUID(json.loads(raw)["request_id"])
            secret = _mint_flow_secret(db_engine, seed)
            human_token = create_access_token({"sub": str(seed.user_id)})
            app = create_app()

            def _db():
                db = Session(bind=db_engine)
                try:
                    yield db
                finally:
                    db.close()

            app.dependency_overrides[get_db_session] = _db
            path = f"/api/v1/approval-requests/{request_id}/approve"
            from preloop.models.db import session as db_session_mod

            db_session_mod._async_engine = None
            db_session_mod._async_session_factory = None
            try:
                with _publisher_patch(), TestClient(app) as client:
                    denied = client.post(
                        path,
                        json={"approved": True, "comment": "managed self-approve"},
                        headers={"Authorization": f"Bearer {secret}"},
                    )
                    assert denied.status_code == 403
                    assert denied.json()["detail"] == (
                        "managed_credential_cannot_decide"
                    )
                    pending = _row_snapshot(db_engine, seed.account_id, request_id)
                    assert pending.status == "pending"
                    assert pending.responses == []
                    assert pending.decided_by_ai is False
                    approved = client.post(
                        path,
                        json={"approved": True, "comment": "destinations match"},
                        headers={"Authorization": f"Bearer {human_token}"},
                    )
            finally:
                db_session_mod._async_engine = None
                db_session_mod._async_session_factory = None
            assert approved.status_code == 200
            assert approved.json()["status"] == "approved"
            after = _row_snapshot(db_engine, seed.account_id, request_id)
            assert after.status == "approved"
            assert after.decided_by_ai is False
            assert after.auto_approved_reason is None
            with Session(db_engine) as db:
                enforce_saved_publication_approval(
                    db,
                    account_id=str(seed.account_id),
                    execution_id=str(seed.execution_id),
                    candidates=candidates,
                )
        finally:
            _drop(db_engine, seed.account_id)

    async def test_human_jwt_approve_satisfies_pre_mint(self, db_engine):
        seed = _seed(db_engine)
        candidates = [_candidate(FIRMWARE, SHA_A), _candidate(APP, SHA_B)]
        try:
            request_id = uuid.UUID(
                json.loads(
                    await _call_request_approval(
                        seed, publication_candidates=candidates
                    )
                )["request_id"]
            )
            token = create_access_token({"sub": str(seed.user_id)})
            with Session(db_engine) as db, _publisher_patch():
                principal = get_current_user(token=token, db=db)
                assert getattr(principal, "_auth_api_key", None) is None
                result = await approval_requests.approve_request(
                    request_id=request_id,
                    decision=ApprovalDecision(
                        approved=True, comment="destinations match"
                    ),
                    request=_http_request(),
                    current_user=principal,
                    db=db,
                )
            assert result.status == "approved"
            after = _row_snapshot(db_engine, seed.account_id, request_id)
            assert after.status == "approved"
            assert after.decided_by_ai is False
            assert after.auto_approved_reason is None
            with Session(db_engine) as db:
                enforce_saved_publication_approval(
                    db,
                    account_id=str(seed.account_id),
                    execution_id=str(seed.execution_id),
                    candidates=candidates,
                )
        finally:
            _drop(db_engine, seed.account_id)

    async def test_managed_key_can_still_decide_ordinary_approval(self, db_engine):
        seed = _seed(db_engine)
        try:
            request_id = uuid.UUID(
                json.loads(await _call_request_approval(seed))["request_id"]
            )
            secret = _mint_flow_secret(db_engine, seed)
            async with _blocked_call(db_engine, secret) as (db, principal):
                result = await approval_requests.approve_request(
                    request_id=request_id,
                    decision=ApprovalDecision(approved=True, comment="ordinary"),
                    request=_http_request(),
                    current_user=principal,
                    db=db,
                )
            assert result.status == "approved"
            assert _row_snapshot(db_engine, seed.account_id, request_id).status == (
                "approved"
            )
        finally:
            _drop(db_engine, seed.account_id)

    async def test_legacy_isolated_publication_tool_name_is_blocked(self, db_engine):
        seed = _seed(db_engine)
        try:
            scoped_id = uuid.UUID(
                json.loads(
                    await _call_request_approval(
                        seed,
                        publication_candidates=[_candidate(FIRMWARE, SHA_A)],
                    )
                )["request_id"]
            )
            with Session(db_engine) as db:
                scoped = crud_approval_request.get(
                    db, id=scoped_id, account_id=str(seed.account_id)
                )
                legacy = crud_approval_request.create(
                    db,
                    obj_in={
                        "account_id": seed.account_id,
                        "tool_configuration_id": scoped.tool_configuration_id,
                        "approval_workflow_id": scoped.approval_workflow_id,
                        "execution_id": str(seed.execution_id),
                        "tool_name": "isolated_publication",
                        "tool_args": {
                            "action": "isolated_publication",
                            "candidates": [_candidate(FIRMWARE, SHA_A)],
                        },
                        "status": "pending",
                        "agent_reasoning": "legacy publication form",
                    },
                )
                legacy_id = legacy.id
            secret = _mint_flow_secret(db_engine, seed)
            async with _blocked_call(db_engine, secret) as (db, principal):
                with pytest.raises(HTTPException) as exc_info:
                    await approval_requests.decide_request(
                        request_id=legacy_id,
                        decision=ApprovalDecision(approved=True, comment="legacy"),
                        request=_http_request(),
                        current_user=principal,
                        db=db,
                    )
            assert exc_info.value.detail == "managed_credential_cannot_decide"
            assert _row_snapshot(db_engine, seed.account_id, legacy_id).status == (
                "pending"
            )
        finally:
            _drop(db_engine, seed.account_id)
