"""Tests for user hard-deletion and account purge CRUD operations."""

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from preloop.models.crud import crud_account, crud_user
from preloop.models.models.api_usage import ApiUsage
from preloop.models.models.audit_log import AuditLog
from preloop.models.models.event import Event
from preloop.models.models.github_oauth_token import OAuthToken
from preloop.models.models.identity_link import IdentityLink
from preloop.models.models.user import User


def _create_account(db: Session, name: str = "Deletion Test Org"):
    return crud_account.create(db, obj_in={"organization_name": name})


def _create_sso_user(db: Session, account, username: str) -> User:
    """Create an OAuth-sourced user with SSO identity fields set."""
    return crud_user.create(
        db,
        obj_in={
            "account_id": account.id,
            "username": username,
            "email": f"{username}@example.com",
            "is_active": True,
            "email_verified": True,
            "hashed_password": None,
            "user_source": "oauth_google",
            "oauth_provider": "google",
            "oauth_id": f"google-{username}",
            "external_id": f"google-{username}",
        },
    )


def _attach_sso_artifacts(db: Session, user: User) -> None:
    """Attach oauth_token and identity_link rows to a user."""
    db.add(
        OAuthToken(
            id=uuid.uuid4(),
            provider="google",
            access_token_encrypted="encrypted-access",
            refresh_token_encrypted="encrypted-refresh",
            token_type="bearer",
            user_id=user.id,
            account_id=user.account_id,
        )
    )
    db.add(
        IdentityLink(
            principal_type="cli_client",
            principal_id=f"cli-{user.username}",
            user_id=user.id,
            account_id=user.account_id,
            source="login",
            first_linked_at=datetime.now(timezone.utc),
            last_seen_at=datetime.now(timezone.utc),
        )
    )
    db.flush()


def _count_artifacts(db: Session, user_id) -> tuple[int, int]:
    tokens = db.query(OAuthToken).filter(OAuthToken.user_id == user_id).count()
    links = db.query(IdentityLink).filter(IdentityLink.user_id == user_id).count()
    return tokens, links


class TestHardDeleteUser:
    def test_hard_delete_removes_user_and_sso_artifacts(self, db_session: Session):
        account = _create_account(db_session)
        user = _create_sso_user(db_session, account, "harddelete1")
        _attach_sso_artifacts(db_session, user)
        user_id = user.id

        deleted = crud_user.hard_delete(db_session, user_id=user_id)

        assert deleted is not None
        assert db_session.query(User).filter(User.id == user_id).first() is None
        tokens, links = _count_artifacts(db_session, user_id)
        assert tokens == 0
        assert links == 0

    def test_hard_delete_missing_user_returns_none(self, db_session: Session):
        assert crud_user.hard_delete(db_session, user_id=uuid.uuid4()) is None

    def test_hard_delete_preserves_audit_logs_and_events(self, db_session: Session):
        account = _create_account(db_session)
        user = _create_sso_user(db_session, account, "harddelete2")
        user_id = user.id

        audit_log = AuditLog(
            account_id=account.id,
            user_id=user_id,
            action="login",
            status="success",
        )
        event = Event(
            account_id=account.id,
            user_id=user_id,
            event_type="page_view",
            timestamp=datetime.now(timezone.utc),
        )
        db_session.add(audit_log)
        db_session.add(event)
        db_session.flush()
        audit_log_id = audit_log.id
        event_id = event.id

        crud_user.hard_delete(db_session, user_id=user_id)

        surviving_log = (
            db_session.query(AuditLog).filter(AuditLog.id == audit_log_id).first()
        )
        surviving_event = db_session.query(Event).filter(Event.id == event_id).first()
        assert surviving_log is not None
        assert surviving_log.user_id is None
        assert surviving_event is not None
        assert surviving_event.user_id is None

    def test_hard_delete_preserves_api_usage(self, db_session: Session):
        account = _create_account(db_session)
        user = _create_sso_user(db_session, account, "harddelete4")
        user_id = user.id

        usage = ApiUsage(
            account_id=account.id,
            user_id=user_id,
            endpoint="/api/v1/gateway/chat",
            method="POST",
            status_code=200,
            duration=0.42,
        )
        db_session.add(usage)
        db_session.flush()
        usage_id = usage.id

        crud_user.hard_delete(db_session, user_id=user_id)

        surviving_usage = (
            db_session.query(ApiUsage).filter(ApiUsage.id == usage_id).first()
        )
        assert surviving_usage is not None
        assert surviving_usage.user_id is None
        assert surviving_usage.account_id == account.id

    def test_hard_delete_no_commit_flushes_only(self, db_session: Session):
        account = _create_account(db_session)
        user = _create_sso_user(db_session, account, "harddelete3")
        user_id = user.id

        crud_user.hard_delete(db_session, user_id=user_id, commit=False)

        assert db_session.query(User).filter(User.id == user_id).first() is None


class TestPurgeAccount:
    def test_purge_removes_account_users_and_artifacts(self, db_session: Session):
        account = _create_account(db_session, name="Purge Test Org")
        primary = _create_sso_user(db_session, account, "purgeprimary")
        member = _create_sso_user(db_session, account, "purgemember")
        _attach_sso_artifacts(db_session, primary)
        _attach_sso_artifacts(db_session, member)
        account.primary_user_id = primary.id
        db_session.flush()
        account_id = str(account.id)
        primary_id = primary.id
        member_id = member.id

        purged = crud_account.purge(db_session, account_id=account_id)

        assert purged is not None
        assert crud_account.get(db_session, id=account_id) is None
        for user_id in (primary_id, member_id):
            assert db_session.query(User).filter(User.id == user_id).first() is None
            tokens, links = _count_artifacts(db_session, user_id)
            assert tokens == 0
            assert links == 0

    def test_purge_missing_account_returns_none(self, db_session: Session):
        assert crud_account.purge(db_session, account_id=str(uuid.uuid4())) is None
