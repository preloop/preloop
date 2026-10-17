"""Real PostgreSQL lifecycle coverage for durable owner consent."""

import os
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import owner_consent as crud
from preloop.services.owner_consent import check_owner_consent


@pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="Requires isolated PostgreSQL"
)
def test_owner_consent_lifecycle() -> None:
    engine = create_engine(os.environ["DATABASE_URL"])
    account_id, owner_id = uuid4(), uuid4()
    with Session(engine) as db:
        account = models.Account(
            id=account_id,
            organization_name="Example Organization",
            meta_data={"approval_window_max_seconds": 60},
        )
        db.add(account)
        db.flush()
        db.add(
            models.User(
                id=owner_id,
                account_id=account_id,
                username=f"owner-{owner_id}",
                email=f"{owner_id}@example.com",
                hashed_password="unused",
                is_active=True,
            )
        )
        db.commit()
        try:
            kwargs = dict(
                account_id=account_id,
                owner_user_id=owner_id,
                action="runner_flow",
                subject_id="example-runner",
                request_key="example-intent",
                details={"runner_name": "Example Runner"},
                execution_id="example-execution",
            )
            first = check_owner_consent(db, **kwargs)
            second = check_owner_consent(db, **kwargs)
            assert first.created and not second.created
            assert first.approval_id == second.approval_id
            row = db.get(models.ApprovalRequest, first.approval_id)
            assert row.approval_workflow.approver_user_ids == [owner_id]
            assert row.approval_workflow.approvals_required == 1
            assert 0 < (row.expires_at - row.requested_at).total_seconds() <= 60
            with pytest.raises(ValueError):
                check_owner_consent(db, **{**kwargs, "subject_id": "other-runner"})
            row = db.get(models.ApprovalRequest, first.approval_id)
            row.status = "approved"
            db.commit()
            assert crud.consume(db, approval_id=first.approval_id)
            assert not crud.consume(db, approval_id=first.approval_id)
            pending = check_owner_consent(
                db, **{**kwargs, "request_key": "another-intent"}
            )
            row = db.get(models.ApprovalRequest, pending.approval_id)
            row.expires_at = datetime.utcnow() - timedelta(seconds=1)
            db.commit()
            expired = check_owner_consent(
                db, **{**kwargs, "request_key": "another-intent"}
            )
            assert expired.status == "expired"
            assert not crud.consume(db, approval_id=pending.approval_id)
        finally:
            db.rollback()
            account = db.get(models.Account, account_id)
            db.delete(account)
            db.commit()
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="Requires isolated PostgreSQL"
)
async def test_notification_completion_survives_delivery_commit() -> None:
    from contextlib import asynccontextmanager
    from unittest.mock import patch
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from preloop.services.owner_consent import notify_owner_consent

    engine = create_engine(os.environ["DATABASE_URL"])
    async_engine = create_async_engine(
        os.environ["DATABASE_URL"].replace(
            "postgresql+psycopg://", "postgresql+asyncpg://"
        )
    )
    factory = async_sessionmaker(async_engine, expire_on_commit=False)
    account_id, owner_id = uuid4(), uuid4()

    @asynccontextmanager
    async def async_session():
        async with factory() as db:
            yield db

    async def delivered(service, approval, workflow):
        # send_notifications records its timeline with an internal commit.
        # The owner binding must remain usable by mark_notified afterwards.
        await service.db.commit()
        return {"mobile_push": {"success": True}}

    with Session(engine) as db:
        db.add(models.Account(id=account_id, organization_name="Example Organization"))
        db.flush()
        db.add(
            models.User(
                id=owner_id,
                account_id=account_id,
                username=f"owner-{owner_id}",
                email=f"{owner_id}@example.com",
                hashed_password="unused",
                is_active=True,
            )
        )
        db.commit()
        try:
            result = check_owner_consent(
                db,
                account_id=account_id,
                owner_user_id=owner_id,
                action="runner_flow",
                subject_id="example-runner",
                request_key="notification-intent",
                details={},
            )
            with (
                patch(
                    "preloop.services.owner_consent.get_async_db_session", async_session
                ),
                patch(
                    "preloop.services.approval_service.ApprovalService.send_notifications",
                    delivered,
                ),
                patch(
                    "preloop.services.approval_service._log_approval_lifecycle_async"
                ),
            ):
                await notify_owner_consent(result.approval_id)
            binding = (
                db.query(models.OwnerConsent)
                .filter_by(approval_request_id=result.approval_id)
                .one()
            )
            assert binding.notification_sent_at is not None
        finally:
            db.rollback()
            db.delete(db.get(models.Account, account_id))
            db.commit()
    await async_engine.dispose()
    engine.dispose()
