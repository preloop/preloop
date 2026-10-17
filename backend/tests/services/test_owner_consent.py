"""Owner consent is human-only and bound to one exact session intent."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from preloop.services.approval_service import ApprovalService
from preloop.services.owner_consent import (
    OwnerConsentResult,
    check_owner_consent,
    require_owner_consent,
)


def test_check_binds_requester_to_persisted_intent() -> None:
    owner, account, actor, approval_id = (uuid4() for _ in range(4))
    consent = SimpleNamespace(consumed_at=None)
    approval = SimpleNamespace(id=approval_id, status="pending")
    with patch("preloop.services.owner_consent.crud.get_or_create") as create:
        create.return_value = consent, approval, True
        result = check_owner_consent(
            MagicMock(),
            account_id=account,
            owner_user_id=owner,
            requester_user_id=actor,
            action="agent_session_start",
            subject_id="agent",
            request_key="exact-intent",
            details={"name": "Agent"},
        )
    assert result == OwnerConsentResult("pending", approval_id, True)
    assert create.call_args.kwargs["details"] == {
        "name": "Agent",
        "requester_user_id": str(actor),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code",
    [("pending", 409), ("declined", 403), ("expired", 403), ("consumed", 403)],
)
async def test_nonapproved_intent_never_dispatches(status: str, code: int) -> None:
    approval_id = uuid4()
    with (
        patch(
            "preloop.services.owner_consent.check_owner_consent",
            return_value=OwnerConsentResult(status, approval_id, True),
        ),
        patch(
            "preloop.services.owner_consent.notify_owner_consent",
            new_callable=AsyncMock,
        ) as notify,
        patch("preloop.services.owner_consent.crud.consume") as consume,
    ):
        with pytest.raises(HTTPException) as exc:
            await require_owner_consent(
                MagicMock(),
                account_id=uuid4(),
                owner_user_id=uuid4(),
                requester_user_id=uuid4(),
                subject_id=str(uuid4()),
                action="agent_session_start",
                request_key="intent",
                details={},
            )
    assert exc.value.status_code == code
    assert exc.value.detail["approval_id"] == str(approval_id)
    notify.assert_awaited_once_with(approval_id)
    consume.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed", [True, False])
async def test_approved_intent_requires_atomic_single_use(claimed: bool) -> None:
    approval_id = uuid4()
    with (
        patch(
            "preloop.services.owner_consent.check_owner_consent",
            return_value=OwnerConsentResult("approved", approval_id, False),
        ),
        patch("preloop.services.owner_consent.crud.consume", return_value=claimed),
    ):
        kwargs = dict(
            account_id=uuid4(),
            owner_user_id=uuid4(),
            requester_user_id=uuid4(),
            subject_id=str(uuid4()),
            action="agent_session_start",
            request_key="intent",
            details={},
        )
        if claimed:
            assert await require_owner_consent(MagicMock(), **kwargs) == approval_id
        else:
            with pytest.raises(HTTPException) as exc:
                await require_owner_consent(MagicMock(), **kwargs)
            assert exc.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["owner", "admin", "public"])
async def test_approval_service_only_accepts_bound_owner(actor: str) -> None:
    owner = uuid4()
    approval = SimpleNamespace(id=uuid4(), tool_name="owner_consent")
    service = ApprovalService(AsyncMock(), "https://example.com")
    user_id = owner if actor == "owner" else uuid4() if actor == "admin" else None
    with patch(
        "preloop.models.crud.owner_consent.get_by_approval_async",
        new_callable=AsyncMock,
    ) as get:
        get.return_value = SimpleNamespace(owner_user_id=owner)
        if actor == "owner":
            await service._require_consent_owner(approval, user_id)
        else:
            with pytest.raises(HTTPException) as exc:
                await service._require_consent_owner(approval, user_id)
            assert exc.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["approve_request", "decline_request"])
async def test_decision_rejects_other_account_admin_before_writing(method: str) -> None:
    service = ApprovalService(AsyncMock(), "https://example.com")
    approval = SimpleNamespace(id=uuid4(), tool_name="owner_consent")
    service.get_approval_request_for_update = AsyncMock(return_value=approval)
    service.update_approval_request = AsyncMock()
    service._record_event = AsyncMock()
    with patch(
        "preloop.models.crud.owner_consent.get_by_approval_async",
        new_callable=AsyncMock,
    ) as get:
        get.return_value = SimpleNamespace(owner_user_id=uuid4())
        with pytest.raises(HTTPException):
            await getattr(service, method)(approval.id, user_id=uuid4())
    service.update_approval_request.assert_not_awaited()
    service._record_event.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["_auto_approve_request", "_auto_deny_request"])
async def test_ai_cannot_decide_owner_consent(method: str) -> None:
    service = ApprovalService(AsyncMock(), "https://example.com")
    approval = SimpleNamespace(id=uuid4(), tool_name="owner_consent")
    service.get_approval_request = AsyncMock(return_value=approval)
    service.update_approval_request = AsyncMock()
    with patch(
        "preloop.models.crud.owner_consent.get_by_approval_async",
        new_callable=AsyncMock,
    ) as get:
        get.return_value = SimpleNamespace(owner_user_id=uuid4())
        with pytest.raises(HTTPException):
            await getattr(service, method)(approval.id, reason="AI allowed")
    service.update_approval_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_bypass_cannot_approve_owner_consent() -> None:
    service = ApprovalService(AsyncMock(), "https://example.com")
    approval = SimpleNamespace(id=uuid4(), tool_name="owner_consent")
    service.update_approval_request = AsyncMock()
    with patch(
        "preloop.models.crud.owner_consent.get_by_approval_async",
        new_callable=AsyncMock,
    ) as get:
        get.return_value = SimpleNamespace(owner_user_id=uuid4())
        with pytest.raises(HTTPException):
            await service._auto_approve_without_review(
                approval, reason_code="bypass", reason="Bypass active"
            )
    service.update_approval_request.assert_not_awaited()


def test_managed_owner_credential_cannot_vote() -> None:
    from preloop.api.endpoints.approval_requests import (
        _reject_managed_publication_decision,
    )

    with patch(
        "preloop.api.endpoints.approval_requests._managed_execution_credential",
        return_value=True,
    ):
        with pytest.raises(HTTPException) as exc:
            _reject_managed_publication_decision(
                MagicMock(), SimpleNamespace(tool_name="owner_consent")
            )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_mutated_tool_name_does_not_remove_owner_binding() -> None:
    service = ApprovalService(AsyncMock(), "https://example.com")
    approval = SimpleNamespace(id=uuid4(), tool_name="other_tool")
    with patch(
        "preloop.models.crud.owner_consent.get_by_approval_async",
        new_callable=AsyncMock,
    ) as get:
        get.return_value = SimpleNamespace(owner_user_id=uuid4())
        with pytest.raises(HTTPException):
            await service._require_consent_owner(approval, uuid4())


@pytest.mark.asyncio
async def test_notification_failure_preserves_pending_response() -> None:
    approval_id = uuid4()
    with (
        patch(
            "preloop.services.owner_consent.check_owner_consent",
            return_value=OwnerConsentResult("pending", approval_id, True),
        ),
        patch(
            "preloop.services.owner_consent.notify_owner_consent",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Unavailable"),
        ),
    ):
        with pytest.raises(HTTPException) as exc:
            await require_owner_consent(
                MagicMock(),
                account_id=uuid4(),
                owner_user_id=uuid4(),
                requester_user_id=uuid4(),
                subject_id=str(uuid4()),
                action="agent_session_start",
                request_key="intent",
                details={},
            )
    assert exc.value.status_code == 409
    assert exc.value.detail["approval_id"] == str(approval_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", [True, False])
async def test_notification_completion_only_after_delivery(sent: bool) -> None:
    from contextlib import asynccontextmanager
    from preloop.services.owner_consent import notify_owner_consent

    db = AsyncMock()

    @asynccontextmanager
    async def session():
        yield db

    owner_id, approval_id = uuid4(), uuid4()
    consent = SimpleNamespace(owner_user_id=owner_id, notification_sent_at=None)
    workflow = SimpleNamespace(approver_user_ids=[uuid4()], approver_team_ids=[uuid4()])
    approval = SimpleNamespace(
        id=approval_id,
        status="pending",
        approval_workflow=workflow,
        account_id=uuid4(),
        execution_id=None,
    )
    service = MagicMock()
    service.get_approval_request = AsyncMock(return_value=approval)
    service.send_notifications = AsyncMock(
        return_value={"mobile_push": {"success": sent}, "chat": {"queued": 0}}
    )
    with (
        patch("preloop.services.owner_consent.get_async_db_session", session),
        patch(
            "preloop.services.approval_service.ApprovalService", return_value=service
        ),
        patch("preloop.services.approval_service._log_approval_lifecycle_async"),
        patch(
            "preloop.services.owner_consent.crud.get_by_approval_async",
            new_callable=AsyncMock,
            return_value=consent,
        ),
        patch(
            "preloop.services.owner_consent.crud.mark_notified", new_callable=AsyncMock
        ) as mark,
    ):
        await notify_owner_consent(approval_id)
    assert workflow.approver_user_ids == [owner_id]
    assert workflow.approver_team_ids == []
    assert workflow.channel_configs is None
    assert mark.await_count == int(sent)


@pytest.mark.asyncio
async def test_retry_sweep_recovers_durable_requests_despite_one_failure() -> None:
    import asyncio
    from contextlib import asynccontextmanager
    from preloop.services.owner_consent import retry_owner_notifications

    db = AsyncMock()

    @asynccontextmanager
    async def session():
        yield db

    first, second = uuid4(), uuid4()
    with (
        patch("preloop.services.owner_consent.get_async_db_session", session),
        patch(
            "preloop.services.owner_consent.crud.pending_notifications",
            new_callable=AsyncMock,
            return_value=[first, second],
        ),
        patch(
            "preloop.services.owner_consent.notify_owner_consent",
            new_callable=AsyncMock,
            side_effect=[RuntimeError("Transport down"), None],
        ) as notify,
        patch(
            "preloop.services.owner_consent.asyncio.sleep",
            new_callable=AsyncMock,
            side_effect=asyncio.CancelledError,
        ),
    ):
        with pytest.raises(asyncio.CancelledError):
            await retry_owner_notifications()
    assert notify.await_count == 2
    assert [call.args[0] for call in notify.await_args_list] == [first, second]


@pytest.mark.parametrize("permission", ["view_approvals", "decide_approvals"])
@pytest.mark.parametrize(
    "is_owner,managed", [(True, False), (False, False), (True, True)]
)
def test_private_consent_permission_is_bound_to_human_owner(
    permission: str, is_owner: bool, managed: bool
) -> None:
    from preloop.api.endpoints import approval_requests as endpoint
    from preloop.models import models

    actor = models.User(id=uuid4(), account_id=uuid4())

    def deny_global(permission: str):
        def decorate(func):
            def denied(**kwargs):
                raise HTTPException(status_code=403, detail="Missing global permission")

            return denied

        return decorate

    with (
        patch.object(endpoint, "require_permission", deny_global),
        patch.object(endpoint, "_managed_execution_credential", return_value=managed),
        patch(
            "preloop.models.crud.owner_consent.belongs_to_owner", return_value=is_owner
        ) as belongs,
        patch("preloop.utils.permissions._check_authorizer") as authorize,
    ):
        if is_owner and not managed:
            endpoint._check_approval_permission(
                permission, request_id=uuid4(), current_user=actor, db=MagicMock()
            )
            assert authorize.call_args.args[0] == (
                "view_owner_consent"
                if permission == "view_approvals"
                else "decide_owner_consent"
            )
            assert belongs.call_args.kwargs["owner_user_id"] == actor.id
        else:
            with pytest.raises(HTTPException) as exc:
                endpoint._check_approval_permission(
                    permission, request_id=uuid4(), current_user=actor, db=MagicMock()
                )
            assert exc.value.status_code == 403
            authorize.assert_not_called()


def test_private_consent_account_authorizer_still_has_veto() -> None:
    from preloop.api.endpoints import approval_requests as endpoint
    from preloop.models import models

    actor = models.User(id=uuid4(), account_id=uuid4())

    def deny_global(permission: str):
        def decorate(func):
            def denied(**kwargs):
                raise HTTPException(status_code=403)

            return denied

        return decorate

    with (
        patch.object(endpoint, "require_permission", deny_global),
        patch.object(endpoint, "_managed_execution_credential", return_value=False),
        patch("preloop.models.crud.owner_consent.belongs_to_owner", return_value=True),
        patch(
            "preloop.utils.permissions._check_authorizer",
            side_effect=HTTPException(status_code=403, detail="Account policy denied"),
        ),
    ):
        with pytest.raises(HTTPException, match="Account policy denied"):
            endpoint._check_approval_permission(
                "decide_approvals",
                request_id=uuid4(),
                current_user=actor,
                db=MagicMock(),
            )


@pytest.mark.asyncio
async def test_runner_session_consent_has_no_agent_link() -> None:
    approval_id = uuid4()
    with patch(
        "preloop.services.owner_consent.check_owner_consent",
        return_value=OwnerConsentResult("pending", approval_id, False),
    ) as check:
        with pytest.raises(HTTPException) as exc:
            await require_owner_consent(
                MagicMock(),
                account_id=uuid4(),
                owner_user_id=uuid4(),
                requester_user_id=uuid4(),
                subject_id="example-runner",
                action="runner_session_start",
                request_key="intent",
                details={},
                managed_agent_id=None,
            )
    assert check.call_args.kwargs["managed_agent_id"] is None
    assert exc.value.detail["message"] == "Waiting for runner owner"


@pytest.mark.asyncio
async def test_session_gate_performs_sync_database_work_off_event_loop() -> None:
    import threading

    loop_thread = threading.get_ident()
    observed: list[int] = []
    approval_id = uuid4()

    def check(*args, **kwargs):
        observed.append(threading.get_ident())
        return OwnerConsentResult("approved", approval_id, False)

    def consume(*args, **kwargs):
        observed.append(threading.get_ident())
        return True

    with (
        patch("preloop.services.owner_consent.check_owner_consent", side_effect=check),
        patch("preloop.services.owner_consent.crud.consume", side_effect=consume),
    ):
        result = await require_owner_consent(
            MagicMock(),
            account_id=uuid4(),
            owner_user_id=uuid4(),
            requester_user_id=uuid4(),
            subject_id=str(uuid4()),
            action="agent_session_start",
            request_key="intent",
            details={},
        )
    assert result == approval_id
    assert len(observed) == 2
    assert all(thread != loop_thread for thread in observed)


@pytest.mark.parametrize("managed", [True, False])
def test_consent_status_rejects_managed_and_preserves_account_veto(
    managed: bool,
) -> None:
    from preloop.api.endpoints import approval_requests as endpoint
    from preloop.models import models

    actor = models.User(id=uuid4(), account_id=uuid4())
    with (
        patch.object(endpoint, "_managed_execution_credential", return_value=managed),
        patch(
            "preloop.models.crud.owner_consent.status_for_requester",
            return_value={"status": "pending", "expires_at": None},
        ) as lookup,
        patch(
            "preloop.utils.permissions._check_authorizer",
            side_effect=HTTPException(status_code=403, detail="Account policy denied"),
        ) as authorize,
    ):
        with pytest.raises(HTTPException) as exc:
            endpoint.get_owner_consent_status(
                uuid4(), current_user=actor, db=MagicMock()
            )
        assert exc.value.status_code == 403
        if managed:
            lookup.assert_not_called()
            authorize.assert_not_called()
        else:
            authorize.assert_called_once()


def test_consent_status_returns_no_approval_details() -> None:
    from preloop.api.endpoints import approval_requests as endpoint
    from preloop.models import models

    actor = models.User(id=uuid4(), account_id=uuid4())
    with (
        patch.object(endpoint, "_managed_execution_credential", return_value=False),
        patch(
            "preloop.models.crud.owner_consent.status_for_requester",
            return_value={"status": "approved", "expires_at": None},
        ),
        patch("preloop.utils.permissions._check_authorizer"),
    ):
        result = endpoint.get_owner_consent_status(
            uuid4(), current_user=actor, db=MagicMock()
        )
    assert result.model_dump() == {"status": "approved", "expires_at": None}
